"""Leave of absence.

Staff request time off with `/loa request`; it posts to the LOA channel with
Approve and Deny for HR. Approved leave is tracked so it can be listed, ended
early, and expired automatically.

Requests are stored rather than living only in the message, so an approved
leave survives someone deleting the post.
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
from core.perms import has_tier
from core.store import JSONStore

log = logging.getLogger("blueprint.loa")

store = JSONStore("loa", {})

DURATION_RE = re.compile(r"(\d+)\s*([dhw])", re.IGNORECASE)
UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int | None:
    """Parse '5d', '2w', '36h', or combinations like '1w3d'. None if unreadable."""
    total = 0
    for amount, unit in DURATION_RE.findall(text or ""):
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total or None


def max_seconds() -> int:
    return int(config.get("loa.max_days", 30) or 30) * 86400


NICK_LIMIT = 32  # Discord's hard cap on a nickname


def nick_prefix() -> str:
    return str(config.get("loa.nick_prefix", "LOA | ") or "")


async def apply_prefix(member: discord.Member) -> str | None:
    """Prefix a member's display name, keeping the rest of it.

    Returns the name to restore later, or None if nothing was changed. The
    original is handed back rather than recomputed because `nick` is None for
    someone using their plain username, and we need to put that back exactly.
    """
    prefix = nick_prefix()
    if not prefix:
        return None

    # Capture before editing -- member.nick reflects the new value afterwards.
    current = member.display_name
    if current.startswith(prefix):
        return None

    # Trim the name, never the prefix, so it stays recognisable at the cap.
    room = NICK_LIMIT - len(prefix)
    new = prefix + current[:room]

    try:
        await member.edit(nick=new, reason="Leave of absence approved")
    except discord.Forbidden:
        log.warning("cannot rename %s -- they're above me or I lack Manage Nicknames", member)
        return None
    except discord.HTTPException:
        log.exception("failed to rename %s", member)
        return None

    return current


async def remove_prefix(member: discord.Member, original: str) -> None:
    """Restore the name we saved when the prefix was applied.

    Only ever called with a stored original, so a member who already had the
    prefix in their own name keeps it -- we undo what we did, nothing else.
    """
    prefix = nick_prefix()
    if not prefix or not member.display_name.startswith(prefix):
        return

    # Setting nick to their username clears the nickname entirely.
    restore = None if original == member.name else original

    try:
        await member.edit(nick=restore, reason="Leave ended")
    except (discord.Forbidden, discord.HTTPException):
        log.warning("could not restore nickname for %s", member)


async def active_for(user_id: int) -> dict | None:
    data = await store.read()
    for entry in (data.get("requests") or {}).values():
        if entry.get("user") == user_id and entry.get("status") in ("pending", "approved"):
            return entry
    return None


def request_view(lid: int, entry: dict, decided: bool = False) -> ui.BaseLayout:
    status = entry.get("status", "pending")
    colour = {"approved": ui.GREEN_HEX, "denied": ui.RED_HEX, "ended": None}.get(
        status, ui.AMBER_HEX
    )

    until = entry.get("until", 0)
    body = [
        ui.field("Member", f"<@{entry['user']}> (`{entry['user']}`)"),
        ui.field("Duration", entry.get("duration", "?")),
        ui.field("Returns", f"<t:{until}:D> (<t:{until}:R>)"),
        "",
        "**Reason**",
        entry.get("reason", "")[:800],
        "",
        ui.field("Status", status.title()),
    ]
    if entry.get("decided_by"):
        body.append(ui.field("Decided by", f"<@{entry['decided_by']}>"))
    if entry.get("note"):
        body.append(ui.field("Note", entry["note"]))

    children: list[dui.Item] = [
        ui.text(f"## Leave of Absence #{lid}\n" + "\n".join(body))
    ]
    if not decided:
        children.append(ui.separator(large=True))
        children.append(ui.row(LoaDecision("approve", lid), LoaDecision("deny", lid)))

    view = ui.BaseLayout(timeout=None)
    view.add_item(ui.container(*children, color=colour))
    return view.validate()


class NoteModal(dui.Modal):
    note = dui.TextInput(
        label="Note (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, action: str, lid: int) -> None:
        super().__init__(title=f"{action.title()} LOA #{lid}"[:45])
        self.action = action
        self.lid = lid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        approved = self.action == "approve"

        async with store.edit() as data:
            entry = (data.get("requests") or {}).get(str(self.lid))
            if entry is None:
                await interaction.response.send_message(
                    view=ui.err("That request is gone."), ephemeral=True
                )
                return
            if entry.get("status") != "pending":
                await interaction.response.send_message(
                    view=ui.warn(f"Already {entry['status']}."), ephemeral=True
                )
                return
            entry["status"] = "approved" if approved else "denied"
            entry["decided_by"] = interaction.user.id
            entry["decided_at"] = int(time.time())
            entry["note"] = self.note.value or None
            snapshot = dict(entry)

        try:
            await interaction.response.edit_message(
                view=request_view(self.lid, snapshot, decided=True),
                content=None,
                embeds=[],
                attachments=[],
            )
        except discord.HTTPException:
            log.exception("could not re-render LOA %s", self.lid)

        note = ""
        member = interaction.guild.get_member(snapshot["user"]) if interaction.guild else None

        if approved and member is not None:
            # Prefix the nickname so the whole team can see who's away.
            original = await apply_prefix(member)
            if original is not None:
                async with store.edit() as data:
                    row = (data.get("requests") or {}).get(str(self.lid))
                    if row is not None:
                        row["original_nick"] = original
                note += f"\n-# Renamed to `{member.display_name}`."
            elif nick_prefix():
                note += "\n-# Couldn't rename them — they're above me in the role list."

            # Grant the LOA role too, if one is configured.
            role_id = config.get("loa.role_id")
            role = interaction.guild.get_role(int(role_id)) if role_id else None
            if role is not None:
                try:
                    await member.add_roles(role, reason=f"LOA approved by {interaction.user}")
                    note += f"\n-# Granted {role.mention}."
                except discord.Forbidden:
                    note += f"\n-# Couldn't grant {role.mention}, my role needs to sit above it."

        user = interaction.client.get_user(snapshot["user"])
        dmed = False
        if user is not None:
            try:
                await user.send(
                    view=ui.panel(
                        f"Leave {'Approved' if approved else 'Denied'}",
                        (
                            f"You're off until <t:{snapshot['until']}:D>. "
                            "Hand off anything you've claimed before you go."
                            if approved
                            else "Your leave request was denied."
                        )
                        + (f"\n\n**Note:** {self.note.value}" if self.note.value else ""),
                        color=ui.GREEN_HEX if approved else ui.RED_HEX,
                    )
                )
                dmed = True
            except (discord.Forbidden, discord.HTTPException):
                pass

        summary = f"LOA #{self.lid} **{snapshot['status']}**."
        summary += "" if dmed else "\n-# Couldn't DM them."
        summary += note
        try:
            await interaction.followup.send(view=ui.ok(summary), ephemeral=True)
        except discord.HTTPException:
            pass


class LoaDecision(
    dui.DynamicItem[dui.Button], template=r"loa:(?P<action>approve|deny):(?P<lid>\d+)"
):
    def __init__(self, action: str, lid: int) -> None:
        approve = action == "approve"
        super().__init__(
            dui.Button(
                label="Approve" if approve else "Deny",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"loa:{action}:{lid}",
            )
        )
        self.action = action
        self.lid = lid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["lid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not has_tier(interaction.user, "hr", "admin"):
            await interaction.response.send_message(view=ui.err("HR only."), ephemeral=True)
            return

        # Repair a stale post rather than refusing, in case an edit failed.
        data = await store.read()
        entry = (data.get("requests") or {}).get(str(self.lid))
        if entry and entry.get("status") != "pending":
            await interaction.response.edit_message(
                view=request_view(self.lid, entry, decided=True),
                content=None,
                embeds=[],
                attachments=[],
            )
            return

        await interaction.response.send_modal(NoteModal(self.action, self.lid))


@app_commands.guild_only()
class LOA(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(LoaDecision)
        self.expirer.start()

    async def cog_unload(self) -> None:
        self.expirer.cancel()

    @tasks.loop(hours=1)
    async def expirer(self) -> None:
        """End approved leave once its return date has passed."""
        now = int(time.time())
        expired: list[dict] = []

        async with store.edit() as data:
            for entry in (data.get("requests") or {}).values():
                if entry.get("status") == "approved" and entry.get("until", 0) <= now:
                    entry["status"] = "ended"
                    entry["ended_at"] = now
                    expired.append(dict(entry))

        for entry in expired:
            await self._restore(entry["user"], entry.get("original_nick"))
            channel = await get_channel(self.bot, "loa_log")
            if channel is not None:
                try:
                    await channel.send(
                        view=ui.panel(
                            "Leave Ended",
                            f"<@{entry['user']}>'s leave has run out. They're expected back.",
                        )
                    )
                except discord.HTTPException:
                    pass

    @expirer.before_loop
    async def before_expirer(self) -> None:
        await self.bot.wait_until_ready()

    async def _restore(self, user_id: int, original_nick: str | None = None) -> None:
        """Undo everything approval applied: the nickname prefix and the role."""
        guild = self.bot.get_guild(config.guild_id) if config.guild_id else None
        if guild is None:
            return

        member = guild.get_member(user_id)
        if member is None:
            return

        # Absent means we never renamed them, so there's nothing of ours to undo.
        if original_nick is not None:
            await remove_prefix(member, original_nick)

        role_id = config.get("loa.role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="Leave ended")
            except discord.HTTPException:
                pass

    # -- commands ---------------------------------------------------------

    loa = app_commands.Group(name="loa", description="Leave of absence")

    @loa.command(name="request", description="Request time off")
    @app_commands.describe(
        duration="How long, e.g. 5d, 2w, 36h", reason="Why you need the time"
    )
    async def request(
        self, interaction: discord.Interaction, duration: str, reason: str
    ) -> None:
        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                view=ui.err("I couldn't read that. Use formats like `5d`, `2w` or `36h`."),
                ephemeral=True,
            )
            return

        cap = max_seconds()
        if seconds > cap:
            await interaction.response.send_message(
                view=ui.err(f"That's longer than the {cap // 86400} day maximum."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        existing = await active_for(interaction.user.id)
        if existing:
            state = existing.get("status")
            await interaction.followup.send(
                view=ui.warn(
                    "You already have a leave request waiting on a decision."
                    if state == "pending"
                    else "You're already on approved leave. Use `/loa end` to come back early."
                ),
                ephemeral=True,
            )
            return

        channel = await get_channel(self.bot, "loa_log")
        if channel is None:
            await interaction.followup.send(
                view=ui.err("The LOA channel isn't configured. Tell an admin."),
                ephemeral=True,
            )
            return

        lid = await store.next_id("_counter")
        entry = {
            "id": lid,
            "user": interaction.user.id,
            "duration": duration,
            "reason": reason,
            "until": int(time.time()) + seconds,
            "status": "pending",
            "at": int(time.time()),
        }
        async with store.edit() as data:
            data.setdefault("requests", {})[str(lid)] = entry

        await channel.send(view=request_view(lid, entry))
        await interaction.followup.send(
            view=ui.ok(f"Request **#{lid}** sent to {channel.mention}."), ephemeral=True
        )

    @loa.command(name="end", description="Come back from leave early")
    async def end(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        found = None
        async with store.edit() as data:
            for entry in (data.get("requests") or {}).values():
                if entry.get("user") == interaction.user.id and entry.get("status") == "approved":
                    entry["status"] = "ended"
                    entry["ended_at"] = int(time.time())
                    found = dict(entry)
                    break

        if found is None:
            await interaction.followup.send(
                view=ui.warn("You're not on approved leave."), ephemeral=True
            )
            return

        await self._restore(interaction.user.id, found.get("original_nick"))

        channel = await get_channel(self.bot, "loa_log")
        if channel is not None:
            await channel.send(
                view=ui.panel(
                    "Leave Ended Early",
                    f"{interaction.user.mention} is back before their return date.",
                )
            )

        await interaction.followup.send(view=ui.ok("Welcome back."), ephemeral=True)

    @loa.command(name="list", description="Who is currently on leave")
    async def list_active(self, interaction: discord.Interaction) -> None:
        data = await store.read()
        rows = [
            e for e in (data.get("requests") or {}).values() if e.get("status") == "approved"
        ]
        pending = [
            e for e in (data.get("requests") or {}).values() if e.get("status") == "pending"
        ]

        rows.sort(key=lambda e: e.get("until", 0))
        body = (
            "\n".join(
                f"<@{e['user']}> — back <t:{e.get('until', 0)}:R> · {e.get('reason', '')[:50]}"
                for e in rows[:20]
            )
            if rows
            else "Nobody is on leave."
        )
        if pending:
            body += f"\n\n-# {len(pending)} request(s) waiting on a decision."

        await interaction.response.send_message(
            view=ui.panel("Leave of Absence", body), ephemeral=True
        )


PREVIEW_VIEWS = [
    (
        "request",
        lambda: request_view(
            1,
            {
                "user": 1,
                "duration": "5d",
                "reason": "Exams coming up.",
                "until": 1800000000,
                "status": "pending",
            },
        ),
    ),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LOA(bot))
