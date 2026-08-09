"""Applications, run entirely in DMs.

A button in the apply channel starts a private conversation: the bot asks each
question in turn, collects a portfolio of past work, then posts the finished
application to the review channel with accept/deny controls.

Attachments are re-uploaded to the review channel rather than linked. Discord
signs CDN URLs and they expire within a day, so a linked portfolio would rot
before anyone went back to it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import discord
from discord import app_commands, ui as dui
from discord.ext import commands, tasks

from core import ui
from core.config import config
from core.logs import get_channel
from core.perms import has_tier, require
from core.store import applications as store

log = logging.getLogger("blueprint.applications")

START_BUTTON_ID = "bp:app:start"
SESSION_TIMEOUT = 30 * 60      # abandoned DM sessions expire after 30 minutes
MAX_PORTFOLIO_FILES = 10       # Discord's per-message attachment cap
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
CANCEL_WORDS = {"cancel", "stop", "quit", "exit"}
DONE_WORDS = {"done", "finished", "finish", "next", "skip"}


def positions() -> dict[str, dict]:
    return config.get("applications.positions", {}) or {}


def apps_closed() -> bool:
    return bool(config.get("applications.closed", False))


class Session:
    """One in-progress DM application."""

    def __init__(self, user_id: int, guild_id: int, key: str, spec: dict) -> None:
        self.user_id = user_id
        self.guild_id = guild_id
        self.key = key
        self.spec = spec
        self.questions: list[str] = list(spec.get("questions") or [])
        self.answers: list[tuple[str, str]] = []
        self.index = 0
        self.stage = "questions" if self.questions else "portfolio"
        self.portfolio: list[discord.Attachment] = []
        self.touched = time.time()

    @property
    def wants_portfolio(self) -> bool:
        return bool(self.spec.get("portfolio", True))

    def current_question(self) -> str | None:
        if self.index < len(self.questions):
            return self.questions[self.index]
        return None

    def expired(self) -> bool:
        return time.time() - self.touched > SESSION_TIMEOUT


# -- review controls ------------------------------------------------------


class AppDecision(
    dui.DynamicItem[dui.Button], template=r"bp:app:(?P<action>accept|deny):(?P<aid>\d+)"
):
    def __init__(self, action: str, aid: int) -> None:
        accept = action == "accept"
        super().__init__(
            dui.Button(
                label="Accept" if accept else "Deny",
                style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
                emoji="✅" if accept else "❌",
                custom_id=f"bp:app:{action}:{aid}",
            )
        )
        self.action = action
        self.aid = aid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not has_tier(interaction.user, "hr"):
            await interaction.response.send_message(view=ui.err("HR only."), ephemeral=True)
            return

        async with store.edit() as data:
            entry = (data.get("applications") or {}).get(str(self.aid))
            if entry is None:
                await interaction.response.send_message(
                    view=ui.err("That application is gone."), ephemeral=True
                )
                return
            if entry.get("status") != "pending":
                await interaction.response.send_message(
                    view=ui.warn(f"Already {entry['status']}."), ephemeral=True
                )
                return
            entry["status"] = "accepted" if self.action == "accept" else "denied"
            entry["decided_by"] = interaction.user.id
            entry["decided_at"] = int(time.time())
            applicant_id = entry["user"]
            position_key = entry.get("position")
            label = entry.get("label", position_key)

        accepted = self.action == "accept"
        note = ""

        if accepted and interaction.guild is not None:
            role_id = (positions().get(position_key) or {}).get("role_id")
            member = interaction.guild.get_member(applicant_id)
            if role_id and member is not None:
                role = interaction.guild.get_role(int(role_id))
                if role is not None:
                    try:
                        await member.add_roles(
                            role, reason=f"Application accepted by {interaction.user}"
                        )
                        note = f"\n-# Granted {role.mention}."
                    except discord.Forbidden:
                        note = f"\n-# Couldn't grant {role.mention}, my role needs to sit above it."

        await interaction.response.edit_message(
            view=ui.panel(
                f"Application {'Accepted' if accepted else 'Denied'}",
                "\n".join(
                    [
                        ui.field("Application", f"#{self.aid}"),
                        ui.field("Applicant", f"<@{applicant_id}>"),
                        ui.field("Position", label),
                        ui.field("Decided by", interaction.user.mention),
                    ]
                )
                + note,
                color=ui.GREEN_HEX if accepted else ui.RED_HEX,
            ),
            content=None,
            embeds=[],
            attachments=[],
        )

        user = interaction.client.get_user(applicant_id)
        if user is None:
            try:
                user = await interaction.client.fetch_user(applicant_id)
            except discord.HTTPException:
                user = None
        if user is not None:
            try:
                await user.send(
                    view=ui.panel(
                        f"Application {'Accepted' if accepted else 'Denied'}",
                        (
                            f"You're in. Welcome to the team as **{label}**."
                            if accepted
                            else f"Thanks for applying for **{label}**. We're not moving "
                            "forward this time, but you're welcome to apply again."
                        ),
                        color=ui.GREEN_HEX if accepted else ui.RED_HEX,
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass


class ApplicationReview(ui.BaseLayout):
    def __init__(self, aid: int, body: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            ui.container(
                ui.text(f"## Application #{aid}\n{body}"),
                ui.separator(),
                ui.row(AppDecision("accept", aid), AppDecision("deny", aid)),
            )
        )
        self.validate()


# -- the public panel -----------------------------------------------------


class ApplyPanel(ui.BaseLayout):
    """Sits in the apply channel. One button, opens a DM."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

        open_roles = ", ".join(
            f"**{spec.get('label', key)}**" for key, spec in positions().items()
        ) or "_none right now_"

        button = dui.Button(
            label="Apply",
            style=discord.ButtonStyle.success,
            emoji="📋",
            custom_id=START_BUTTON_ID,
            disabled=not positions(),
        )
        button.callback = self._start

        children: list[dui.Item] = []
        banner_url, banner_file = ui.resolve_media(
            config.get("applications.banner", "assets/blueprint.png")
        )
        if banner_url:
            media = ui.gallery(banner_url)
            if media:
                children.append(media)
                self.track(banner_file)

        children.append(
            ui.text(
                "## Applications\n"
                "We're hiring for " + open_roles + ".\n\n"
                "Hit **Apply** and I'll message you. The whole thing happens in "
                "your DMs so you can upload examples of your past work as you go.\n\n"
                "-# Your DMs need to be open. Takes about five minutes."
            )
        )
        children.append(ui.separator())
        children.append(ui.row(button))

        self.add_item(ui.container(*children))
        self.validate()

    async def _start(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Applications")
        if cog is None:
            await interaction.response.send_message(
                view=ui.err("Applications aren't loaded right now."), ephemeral=True
            )
            return
        await cog.begin(interaction)


class PositionSelect(ui.BaseLayout):
    """Shown in the DM when more than one role is open."""

    def __init__(self, cog: "Applications") -> None:
        super().__init__(timeout=600)
        self.cog = cog

        select = dui.Select(
            placeholder="Which role are you applying for?",
            options=ui.check_options(
                [
                    discord.SelectOption(
                        label=spec.get("label", key)[:100],
                        value=key,
                        description=(spec.get("description") or None),
                        emoji=spec.get("emoji") or None,
                    )
                    for key, spec in positions().items()
                ],
                "applications.positions",
            ),
        )
        select.callback = self._picked

        self.add_item(
            ui.container(
                ui.text("## Applications\nPick the role you want to apply for."),
                ui.separator(),
                ui.row(select),
            )
        )
        self.validate()

    async def _picked(self, interaction: discord.Interaction) -> None:
        key = interaction.data["values"][0]  # type: ignore[index]
        await interaction.response.edit_message(
            view=ui.ok(f"**{positions().get(key, {}).get('label', key)}** it is."),
            content=None,
            embeds=[],
            attachments=[],
        )
        await self.cog.launch(interaction.user, key)


# -- cog ------------------------------------------------------------------


@app_commands.guild_only()
class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sessions: dict[int, Session] = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(AppDecision)
        self.bot.add_view(ApplyPanel())
        self.reaper.start()

    async def cog_unload(self) -> None:
        self.reaper.cancel()

    @tasks.loop(minutes=5)
    async def reaper(self) -> None:
        """Drop abandoned DM sessions so a stale one can't block a new attempt."""
        for uid, session in list(self.sessions.items()):
            if session.expired():
                self.sessions.pop(uid, None)
                user = self.bot.get_user(uid)
                if user is not None:
                    try:
                        await user.send(
                            view=ui.warn("Your application timed out. Start again whenever.")
                        )
                    except discord.HTTPException:
                        pass

    @reaper.before_loop
    async def before_reaper(self) -> None:
        await self.bot.wait_until_ready()

    # -- starting ---------------------------------------------------------

    async def begin(self, interaction: discord.Interaction) -> None:
        """Handle the Apply button: validate, then move the user into DMs."""
        if apps_closed():
            await interaction.response.send_message(
                view=ui.warn("Applications are closed right now."), ephemeral=True
            )
            return

        open_positions = positions()
        if not open_positions:
            await interaction.response.send_message(
                view=ui.warn("Nothing's open at the moment."), ephemeral=True
            )
            return

        if interaction.user.id in self.sessions:
            await interaction.response.send_message(
                view=ui.warn("You've already got one going. Check your DMs."),
                ephemeral=True,
            )
            return

        data = await store.read()
        pending = [
            a
            for a in (data.get("applications") or {}).values()
            if a.get("user") == interaction.user.id and a.get("status") == "pending"
        ]
        if pending:
            await interaction.response.send_message(
                view=ui.warn("You've already got an application waiting on a decision."),
                ephemeral=True,
            )
            return

        # Confirm we can DM them before promising anything.
        try:
            if len(open_positions) == 1:
                key = next(iter(open_positions))
                await interaction.user.send(
                    view=ui.ok("Starting your application. Answer below.")
                )
            else:
                await interaction.user.send(view=PositionSelect(self))
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                view=ui.err(
                    "I can't DM you. Turn on **Settings → Privacy → Direct Messages** "
                    "for this server, then hit Apply again."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            view=ui.ok("Check your DMs."), ephemeral=True
        )

        if len(open_positions) == 1:
            await self.launch(interaction.user, next(iter(open_positions)), interaction.guild_id)

    async def launch(
        self, user: discord.abc.User, key: str, guild_id: int | None = None
    ) -> None:
        """Create the session and ask the first question."""
        spec = positions().get(key)
        if spec is None:
            return

        session = Session(user.id, guild_id or config.guild_id or 0, key, spec)
        self.sessions[user.id] = session
        await self.ask(user, session)

    # -- the conversation -------------------------------------------------

    async def ask(self, user: discord.abc.User, session: Session) -> None:
        """Send whatever comes next in the flow."""
        session.touched = time.time()

        question = session.current_question()
        if session.stage == "questions" and question is not None:
            step = f"Question {session.index + 1} of {len(session.questions)}"
            await self._dm(
                user,
                ui.panel(
                    session.spec.get("label", session.key),
                    f"**{question}**\n\n-# {step} · type `cancel` to stop",
                ),
            )
            return

        if session.stage == "questions":
            session.stage = "portfolio" if session.wants_portfolio else "finish"

        if session.stage == "portfolio":
            await self._dm(
                user,
                ui.panel(
                    "Past work",
                    "Upload examples of your work now. Images, links, whatever you've got.\n\n"
                    f"Send as many messages as you like, then type `done`.\n\n"
                    f"-# Up to {MAX_PORTFOLIO_FILES} files · type `done` if you have none",
                ),
            )
            return

        await self.finish(user, session)

    async def handle_dm(self, message: discord.Message) -> None:
        """Route one DM into the active session."""
        session = self.sessions.get(message.author.id)
        if session is None:
            return

        content = (message.content or "").strip()

        if content.lower() in CANCEL_WORDS:
            self.sessions.pop(message.author.id, None)
            await self._dm(message.author, ui.warn("Cancelled. Nothing was sent."))
            return

        session.touched = time.time()

        if session.stage == "questions":
            question = session.current_question()
            if question is None:
                await self.ask(message.author, session)
                return
            if not content:
                await self._dm(message.author, ui.err("Type your answer as text."))
                return
            session.answers.append((question, content[:1000]))
            session.index += 1
            await self.ask(message.author, session)
            return

        if session.stage == "portfolio":
            added = 0
            for attachment in message.attachments:
                if len(session.portfolio) >= MAX_PORTFOLIO_FILES:
                    break
                if attachment.size > MAX_UPLOAD_BYTES:
                    await self._dm(
                        message.author,
                        ui.warn(f"`{attachment.filename}` is too big, skipped it."),
                    )
                    continue
                session.portfolio.append(attachment)
                added += 1

            # A link counts as portfolio too.
            if content and content.lower() not in DONE_WORDS:
                session.answers.append(("Portfolio note", content[:1000]))

            if content.lower() in DONE_WORDS:
                session.stage = "finish"
                await self.finish(message.author, session)
                return

            if added:
                await self._dm(
                    message.author,
                    ui.ok(
                        f"Got {added} file(s). {len(session.portfolio)}/{MAX_PORTFOLIO_FILES} "
                        "saved. Type `done` when you're finished."
                    ),
                )

    async def finish(self, user: discord.abc.User, session: Session) -> None:
        """Store the application and post it for review."""
        self.sessions.pop(user.id, None)

        aid = await store.next_id("_counter")
        async with store.edit() as data:
            data.setdefault("applications", {})[str(aid)] = {
                "id": aid,
                "user": user.id,
                "position": session.key,
                "label": session.spec.get("label", session.key),
                "answers": [{"q": q, "a": a} for q, a in session.answers],
                "files": len(session.portfolio),
                "status": "pending",
                "at": int(time.time()),
            }

        channel = await get_channel(self.bot, "application_review")
        if channel is None:
            await self._dm(
                user,
                ui.warn(
                    "Your application was saved, but the review channel isn't set up. "
                    "Tell an admin."
                ),
            )
            return

        header = [
            ui.field("Applicant", f"{user.mention} (`{user.id}`)"),
            ui.field("Position", session.spec.get("label", session.key)),
            ui.field("Account created", f"<t:{int(user.created_at.timestamp())}:R>"),
            ui.field("Files attached", len(session.portfolio)),
            "",
        ]
        for question, answer in session.answers:
            header.append(f"**{question}**\n{answer}")

        body = "\n".join(header)
        if len(body) > 3600:  # leave room for the heading and separator
            body = body[:3600] + "\n-# …truncated."

        try:
            await channel.send(view=ApplicationReview(aid, body))
        except discord.HTTPException:
            log.exception("could not post application %s", aid)
            await self._dm(user, ui.err("Something broke posting that. Tell an admin."))
            return

        # Re-upload the portfolio so it outlives Discord's signed CDN links.
        if session.portfolio:
            files = []
            for attachment in session.portfolio:
                try:
                    files.append(await attachment.to_file())
                except (discord.HTTPException, discord.NotFound):
                    continue
            if files:
                try:
                    await channel.send(files=files)
                except discord.HTTPException:
                    log.exception("could not upload portfolio for application %s", aid)

        await self._dm(
            user,
            ui.ok(
                f"Application **#{aid}** is in. We'll message you when there's a decision."
            ),
        )

    async def _dm(self, user: discord.abc.User, view: ui.BaseLayout) -> bool:
        try:
            await user.send(view=view, files=view.files())
            return True
        except (discord.Forbidden, discord.HTTPException):
            self.sessions.pop(user.id, None)
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        if message.author.id not in self.sessions:
            return
        try:
            await self.handle_dm(message)
        except Exception:  # noqa: BLE001 - never leave a session wedged
            log.exception("DM application flow failed for %s", message.author)
            self.sessions.pop(message.author.id, None)
            await self._dm(message.author, ui.err("Something broke. Start again."))

    # -- commands ---------------------------------------------------------

    application = app_commands.Group(name="applications", description="Manage applications")

    @application.command(name="panel", description="Post the applications panel here")
    @require("admin", "hr")
    async def panel(self, interaction: discord.Interaction) -> None:
        await ui.send_panel(interaction.channel, ApplyPanel())
        await interaction.response.send_message(view=ui.ok("Posted."), ephemeral=True)

    @application.command(name="toggle", description="Open or close applications")
    @require("admin", "hr")
    async def toggle(self, interaction: discord.Interaction, closed: bool) -> None:
        config.set("applications.closed", closed)
        config.save()
        await interaction.response.send_message(
            view=ui.warn("Applications are **closed**.")
            if closed
            else ui.ok("Applications are **open**."),
            ephemeral=True,
        )

    @application.command(name="pending", description="List pending applications")
    @require("hr")
    async def pending(self, interaction: discord.Interaction) -> None:
        data = await store.read()
        waiting = [
            a for a in (data.get("applications") or {}).values() if a.get("status") == "pending"
        ]
        if not waiting:
            body = "Nothing waiting."
        else:
            body = "\n".join(
                f"`#{a['id']}` <@{a['user']}> — **{a.get('label', '?')}** "
                f"(<t:{a.get('at', 0)}:R>)"
                for a in waiting[:20]
            )
        active = len(self.sessions)
        if active:
            body += f"\n\n-# {active} application(s) in progress in DMs."

        await interaction.response.send_message(
            view=ui.panel("Pending Applications", body), ephemeral=True
        )


PREVIEW_VIEWS = [
    ("apply-panel", ApplyPanel),
    ("review", lambda: ApplicationReview(1, "**Applicant:** @someone\n**Position:** Designer")),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Applications(bot))
