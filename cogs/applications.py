"""Applications, run in DMs.

An Apply button opens a DM: pick the categories you can work in, write your
response, optionally attach past work. The finished application posts to the
review channel with Accept and Deny.

Attachments are re-uploaded to the review channel rather than linked. Discord
signs CDN URLs and they expire within a day, so a linked portfolio would rot
before anyone reviewed it.
"""

from __future__ import annotations

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
from core.store import JSONStore

log = logging.getLogger("blueprint.applications")

store = JSONStore("applications", {})

APPLY_BUTTON_ID = "app:start"
SESSION_TIMEOUT = 30 * 60
MAX_FILES = 10
MAX_UPLOAD = 8 * 1024 * 1024
CANCEL_WORDS = {"cancel", "stop", "quit", "exit"}
DONE_WORDS = {"done", "finished", "finish", "skip", "none"}


def categories() -> list[str]:
    return config.get("applications.categories", []) or []


def closed() -> bool:
    return bool(config.get("applications.closed", False))


class Session:
    """One in-progress DM application."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.categories: list[str] = []
        self.response: str = ""
        self.files: list[discord.Attachment] = []
        self.stage = "categories"
        self.touched = time.time()

    def expired(self) -> bool:
        return time.time() - self.touched > SESSION_TIMEOUT


# -- review post ----------------------------------------------------------


def review_view(aid: int, entry: dict, decided: bool = False) -> ui.BaseLayout:
    """The application as reviewers see it."""
    status = entry.get("status", "pending")
    colour = {
        "accepted": ui.GREEN_HEX,
        "denied": ui.RED_HEX,
    }.get(status, ui.AMBER_HEX)

    body = [
        ui.field("Applicant", f"<@{entry['user']}> (`{entry['user']}`)"),
        ui.field("Categories", ", ".join(entry.get("categories") or []) or "none"),
        "",
        "**Response**",
        entry.get("response", "")[:1200],
        "",
        ui.field("Files", entry.get("files", 0)),
        ui.field("Status", status.title()),
    ]
    if entry.get("reviewer"):
        body.append(ui.field("Reviewed by", f"<@{entry['reviewer']}>"))
    if entry.get("reason"):
        body.append(ui.field("Reason", entry["reason"]))

    view = ui.BaseLayout(timeout=None)
    children: list[dui.Item] = [
        ui.text(f"## Application #{aid}\n" + "\n".join(body))
    ]
    if not decided:
        children.append(ui.separator())
        children.append(ui.row(Decide("accept", aid), Decide("deny", aid)))

    view.add_item(ui.container(*children, color=colour))
    return view.validate()


class ReasonModal(dui.Modal):
    reason = dui.TextInput(
        label="Reason", style=discord.TextStyle.paragraph, required=True, max_length=400
    )

    def __init__(self, action: str, aid: int) -> None:
        super().__init__(title=f"{action.title()} application #{aid}"[:45])
        self.action = action
        self.aid = aid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        accepted = self.action == "accept"

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
            entry["status"] = "accepted" if accepted else "denied"
            entry["reviewer"] = interaction.user.id
            entry["reason"] = self.reason.value
            entry["decided_at"] = int(time.time())
            snapshot = dict(entry)

        await interaction.response.edit_message(
            view=review_view(self.aid, snapshot, decided=True),
            content=None,
            embeds=[],
            attachments=[],
        )

        # Grant the role and tell the applicant.
        applicant = snapshot["user"]
        note = ""
        role_id = config.get("applications.role_id")
        if accepted and role_id and interaction.guild:
            member = interaction.guild.get_member(applicant)
            role = interaction.guild.get_role(int(role_id))
            if member and role:
                try:
                    await member.add_roles(role, reason=f"Application accepted by {interaction.user}")
                    note = f"\n-# Granted {role.mention}."
                except discord.Forbidden:
                    note = f"\n-# Couldn't grant {role.mention}, my role needs to sit above it."

        user = interaction.client.get_user(applicant)
        if user is None:
            try:
                user = await interaction.client.fetch_user(applicant)
            except discord.HTTPException:
                user = None
        if user is not None:
            try:
                await user.send(
                    view=ui.panel(
                        f"Application {'Accepted' if accepted else 'Denied'}",
                        (
                            f"You're in. Welcome to the team.\n\n**Reason:** {self.reason.value}"
                            if accepted
                            else "Thanks for applying. We're not moving forward this "
                            f"time.\n\n**Reason:** {self.reason.value}"
                        ),
                        color=ui.GREEN_HEX if accepted else ui.RED_HEX,
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if note:
            await interaction.followup.send(view=ui.ok(f"Done.{note}"), ephemeral=True)


class Decide(dui.DynamicItem[dui.Button], template=r"app:(?P<action>accept|deny):(?P<aid>\d+)"):
    def __init__(self, action: str, aid: int) -> None:
        accept = action == "accept"
        super().__init__(
            dui.Button(
                label="Accept" if accept else "Deny",
                style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
                custom_id=f"app:{action}:{aid}",
            )
        )
        self.action = action
        self.aid = aid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not has_tier(interaction.user, "hr", "admin"):
            await interaction.response.send_message(view=ui.err("HR only."), ephemeral=True)
            return
        await interaction.response.send_modal(ReasonModal(self.action, self.aid))


# -- the apply panel ------------------------------------------------------


class ApplyPanel(ui.BaseLayout):
    def __init__(self) -> None:
        super().__init__(timeout=None)

        button = dui.Button(
            label="Apply",
            style=discord.ButtonStyle.primary,
            custom_id=APPLY_BUTTON_ID,
        )
        button.callback = self._start

        brand = config.get("branding.name", "Sail's Customs")
        self.add_item(
            ui.container(
                ui.text(
                    f"## {brand} Application\n"
                    f"**Want to join the {brand} team?**\n\n"
                    "Press **Apply** and I'll message you. The whole thing happens "
                    "in your DMs so you can attach examples of your work.\n\n"
                    "-# Your DMs need to be open. Takes a few minutes."
                ),
                ui.separator(large=True),
                ui.row(button),
            )
        )
        self.validate()

    async def _start(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Applications")
        if cog is None:
            await interaction.response.send_message(
                view=ui.err("Applications aren't loaded."), ephemeral=True
            )
            return
        await cog.begin(interaction)


class CategorySelect(ui.BaseLayout):
    """Sent in the DM: pick everything you can work in."""

    def __init__(self, cog: "Applications") -> None:
        super().__init__(timeout=900)
        self.cog = cog

        opts = [discord.SelectOption(label=c) for c in categories()][:25]
        select = dui.Select(
            placeholder="Pick everything you can work in",
            options=opts or [discord.SelectOption(label="none configured", value="_")],
            min_values=1,
            max_values=max(len(opts), 1),
        )
        select.callback = self._picked

        self.add_item(
            ui.container(
                ui.text(
                    "## Application\n**Step 1 of 3** — which categories can you work in?\n\n"
                    "-# Type `cancel` at any point to stop."
                ),
                ui.separator(),
                ui.row(select),
            )
        )
        self.validate()

    async def _picked(self, interaction: discord.Interaction) -> None:
        picked = interaction.data["values"]  # type: ignore[index]
        session = self.cog.sessions.get(interaction.user.id)
        if session is None:
            await interaction.response.edit_message(
                view=ui.warn("That application expired. Press Apply again."),
                content=None, embeds=[], attachments=[],
            )
            return

        session.categories = list(picked)
        session.stage = "response"
        session.touched = time.time()

        question = config.get(
            "applications.question",
            "Tell us about yourself, your experience, and why you want to join.",
        )
        await interaction.response.edit_message(
            view=ui.panel(
                "Application",
                f"**Step 2 of 3**\n\n{question}\n\n-# Reply here with your answer.",
            ),
            content=None, embeds=[], attachments=[],
        )


@app_commands.guild_only()
class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sessions: dict[int, Session] = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(Decide)
        self.bot.add_view(ApplyPanel())
        self.reaper.start()

    async def cog_unload(self) -> None:
        self.reaper.cancel()

    @tasks.loop(minutes=5)
    async def reaper(self) -> None:
        for uid, session in list(self.sessions.items()):
            if session.expired():
                self.sessions.pop(uid, None)

    @reaper.before_loop
    async def before_reaper(self) -> None:
        await self.bot.wait_until_ready()

    # -- start ------------------------------------------------------------

    async def begin(self, interaction: discord.Interaction) -> None:
        if closed():
            await interaction.response.send_message(
                view=ui.warn("Applications are closed right now."), ephemeral=True
            )
            return

        if interaction.user.id in self.sessions:
            await interaction.response.send_message(
                view=ui.warn("You've already got one going. Check your DMs."), ephemeral=True
            )
            return

        data = await store.read()
        if any(
            a.get("user") == interaction.user.id and a.get("status") == "pending"
            for a in (data.get("applications") or {}).values()
        ):
            await interaction.response.send_message(
                view=ui.warn("You've already got an application waiting on a decision."),
                ephemeral=True,
            )
            return

        self.sessions[interaction.user.id] = Session(interaction.user.id)
        try:
            await interaction.user.send(view=CategorySelect(self))
        except (discord.Forbidden, discord.HTTPException):
            self.sessions.pop(interaction.user.id, None)
            await interaction.response.send_message(
                view=ui.err(
                    "I can't DM you. Turn on **Settings → Privacy → Direct Messages** "
                    "for this server, then press Apply again."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(view=ui.ok("Check your DMs."), ephemeral=True)

    # -- the DM conversation ----------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        session = self.sessions.get(message.author.id)
        if session is None:
            return

        content = (message.content or "").strip()
        if content.lower() in CANCEL_WORDS:
            self.sessions.pop(message.author.id, None)
            await message.author.send(view=ui.warn("Cancelled. Nothing was sent."))
            return

        session.touched = time.time()

        try:
            if session.stage == "response":
                if not content:
                    await message.author.send(view=ui.err("Send your answer as text."))
                    return
                session.response = content[:1500]

                if config.get("applications.portfolio", True):
                    session.stage = "files"
                    await message.author.send(
                        view=ui.panel(
                            "Application",
                            "**Step 3 of 3** — attach examples of your work.\n\n"
                            f"Send up to {MAX_FILES} images across as many messages "
                            "as you like, then type `done`.\n\n"
                            "-# Type `done` if you have none.",
                        )
                    )
                else:
                    await self.finish(message.author, session)
                return

            if session.stage == "files":
                added = 0
                for att in message.attachments:
                    if len(session.files) >= MAX_FILES:
                        break
                    if att.size > MAX_UPLOAD:
                        await message.author.send(
                            view=ui.warn(f"`{att.filename}` is too big, skipped.")
                        )
                        continue
                    session.files.append(att)
                    added += 1

                if content.lower() in DONE_WORDS:
                    await self.finish(message.author, session)
                    return

                if added:
                    await message.author.send(
                        view=ui.ok(
                            f"Got {added}. {len(session.files)}/{MAX_FILES} saved. "
                            "Type `done` when finished."
                        )
                    )
        except Exception:  # noqa: BLE001 - never leave a session wedged
            log.exception("application DM flow failed for %s", message.author)
            self.sessions.pop(message.author.id, None)
            await message.author.send(view=ui.err("Something broke. Press Apply again."))

    async def finish(self, user: discord.abc.User, session: Session) -> None:
        self.sessions.pop(user.id, None)

        aid = await store.next_id("_counter")
        entry = {
            "id": aid,
            "user": user.id,
            "categories": session.categories,
            "response": session.response,
            "files": len(session.files),
            "status": "pending",
            "at": int(time.time()),
        }
        async with store.edit() as data:
            data.setdefault("applications", {})[str(aid)] = entry

        channel = await get_channel(self.bot, "application_review")
        if channel is None:
            await user.send(
                view=ui.warn(
                    "Saved, but the review channel isn't configured. Tell an admin."
                )
            )
            return

        try:
            await channel.send(view=review_view(aid, entry))
        except discord.HTTPException:
            log.exception("could not post application %s", aid)
            await user.send(view=ui.err("Something broke posting that. Tell an admin."))
            return

        # Re-upload the portfolio so it outlives Discord's signed CDN links.
        if session.files:
            files = []
            for att in session.files:
                try:
                    files.append(await att.to_file())
                except (discord.HTTPException, discord.NotFound):
                    continue
            if files:
                try:
                    await channel.send(f"Portfolio for application #{aid}", files=files)
                except discord.HTTPException:
                    log.exception("could not upload portfolio for %s", aid)

        await user.send(
            view=ui.ok(f"Application **#{aid}** is in. We'll message you with a decision.")
        )

    # -- commands ---------------------------------------------------------

    apps = app_commands.Group(name="apply", description="Applications")

    @apps.command(name="panel", description="Post the Apply button here")
    @require("admin", "hr")
    async def panel(self, interaction: discord.Interaction) -> None:
        await ui.send_panel(interaction.channel, ApplyPanel())
        await interaction.response.send_message(view=ui.ok("Posted."), ephemeral=True)

    @apps.command(name="toggle", description="Open or close applications")
    @require("admin", "hr")
    async def toggle(self, interaction: discord.Interaction, closed_: bool) -> None:
        config.set("applications.closed", closed_)
        config.save()
        await interaction.response.send_message(
            view=ui.warn("Applications are **closed**.")
            if closed_
            else ui.ok("Applications are **open**."),
            ephemeral=True,
        )

    @apps.command(name="pending", description="List pending applications")
    @require("admin", "hr")
    async def pending(self, interaction: discord.Interaction) -> None:
        data = await store.read()
        waiting = [
            a for a in (data.get("applications") or {}).values() if a.get("status") == "pending"
        ]
        body = (
            "\n".join(
                f"`#{a['id']}` <@{a['user']}> — {', '.join(a.get('categories') or [])} "
                f"(<t:{a.get('at', 0)}:R>)"
                for a in waiting[:20]
            )
            if waiting
            else "Nothing waiting."
        )
        if self.sessions:
            body += f"\n\n-# {len(self.sessions)} in progress in DMs."
        await interaction.response.send_message(
            view=ui.panel("Pending Applications", body), ephemeral=True
        )


PREVIEW_VIEWS = [
    ("apply-panel", ApplyPanel),
    (
        "review",
        lambda: review_view(
            1,
            {
                "user": 1,
                "categories": ["Web Dev", "Discord", "Bot Dev"],
                "response": "My name is Red and I enjoy creating a wide variety of projects.",
                "files": 3,
                "status": "pending",
            },
        ),
    ),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Applications(bot))
