"""Moderation with a persistent case log."""

from __future__ import annotations

import datetime
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.logs import send_log
from core.perms import require
from core.store import modcases as store

log = logging.getLogger("blueprint.moderation")

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MAX_TIMEOUT = 28 * 86400  # Discord's hard cap


def parse_duration(text: str) -> int | None:
    total = 0
    for amount, unit in DURATION_RE.findall(text or ""):
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total or None


async def record_case(action: str, target_id: int, moderator_id: int, reason: str) -> int:
    number = await store.next_id("_counter")
    async with store.edit() as data:
        data.setdefault("cases", {})[str(number)] = {
            "number": number,
            "action": action,
            "target": target_id,
            "moderator": moderator_id,
            "reason": reason,
            "at": int(time.time()),
        }
    return number


def outranks(actor: discord.Member, target: discord.Member) -> bool:
    """Whether `actor` may act on `target`.

    Guards against a moderator disciplining someone at or above their own role
    height, and against acting on the guild owner.
    """
    if target.id == target.guild.owner_id:
        return False
    if actor.id == actor.guild.owner_id:
        return True
    return actor.top_role > target.top_role


@app_commands.guild_only()
class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _guard(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> bool:
        """Shared safety checks. Replies and returns False when blocked."""
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                view=ui.err("Can't target yourself."), ephemeral=True
            )
            return False
        if member.id == self.bot.user.id:
            await interaction.response.send_message(
                view=ui.err("Not happening."), ephemeral=True
            )
            return False
        if not outranks(interaction.user, member):
            await interaction.response.send_message(
                view=ui.err(f"{member.mention} is at or above your role height."),
                ephemeral=True,
            )
            return False
        if member.top_role >= member.guild.me.top_role:
            await interaction.response.send_message(
                view=ui.err(f"{member.mention} is above me in the role list — I can't act on them."
                ),
                ephemeral=True,
            )
            return False
        return True

    async def _announce(
        self, action: str, number: int, target: discord.abc.User, moderator: discord.abc.User, reason: str, extra: str | None = None
    ) -> None:
        body = [
            ui.field("Member", f"{target.mention} (`{target.id}`)"),
            ui.field("Moderator", moderator.mention),
            ui.field("Reason", reason),
        ]
        if extra:
            body.append(extra)
        await send_log(
            self.bot,
            "mod_log",
            ui.panel(f"{action} — Case #{number}", "\n".join(body), color=0xE74C3C),
        )

    @app_commands.command(name="warn", description="Warn a member")
    @require("support", "hr")
    async def warn(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ) -> None:
        if not await self._guard(interaction, member):
            return

        number = await record_case("warn", member.id, interaction.user.id, reason)
        try:
            await member.send(
                view=ui.panel("You were warned", ui.field("Reason", reason), color=0xE74C3C)
            )
            dm_note = ""
        except (discord.Forbidden, discord.HTTPException):
            dm_note = "\n-# Couldn't DM them."

        await self._announce("Warning", number, member, interaction.user, reason)
        await interaction.response.send_message(
            view=ui.ok(f"Warned {member.mention} — case **#{number}**.{dm_note}"),
            ephemeral=True,
        )

    @app_commands.command(name="timeout", description="Time a member out")
    @app_commands.describe(duration="e.g. 10m, 2h, 1d (max 28d)")
    @require("support", "hr")
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: str = "No reason given",
    ) -> None:
        if not await self._guard(interaction, member):
            return

        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                view=ui.err(f"I couldn't read `{duration}`. Try `10m`, `2h` or `1d`."),
                ephemeral=True,
            )
            return
        if seconds > MAX_TIMEOUT:
            await interaction.response.send_message(
                view=ui.err("28 days is the max."), ephemeral=True
            )
            return

        try:
            await member.timeout(
                datetime.timedelta(seconds=seconds),
                reason=f"{interaction.user}: {reason}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                view=ui.err("I don't have permission for that."),
                ephemeral=True,
            )
            return

        number = await record_case("timeout", member.id, interaction.user.id, reason)
        until = int(time.time()) + seconds
        await self._announce(
            "Timeout", number, member, interaction.user, reason, ui.field("Until", f"<t:{until}:F>")
        )
        await interaction.response.send_message(
            view=ui.ok(f"Timed out {member.mention} until <t:{until}:R> — case **#{number}**."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="untimeout", description="Remove a member's timeout")
    @require("support", "hr")
    async def untimeout(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        try:
            await member.timeout(None, reason=f"Lifted by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                view=ui.err("Couldn't lift it."), ephemeral=True
            )
            return

        await interaction.response.send_message(
            view=ui.ok(f"Timeout lifted for {member.mention}."), ephemeral=True
        )

    @app_commands.command(name="kick", description="Kick a member")
    @require("hr")
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason given",
    ) -> None:
        if not await self._guard(interaction, member):
            return

        number = await record_case("kick", member.id, interaction.user.id, reason)
        try:
            await member.send(
                view=ui.panel("You were kicked", ui.field("Reason", reason), color=0xE74C3C)
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            await member.kick(reason=f"{interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return

        await self._announce("Kick", number, member, interaction.user, reason)
        await interaction.response.send_message(
            view=ui.ok(f"Kicked {member.mention} — case **#{number}**."),
            ephemeral=True,
        )

    @app_commands.command(name="ban", description="Ban a member")
    @app_commands.describe(delete_days="Days of their messages to delete (0-7)")
    @require("hr")
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason given",
        delete_days: int = 0,
    ) -> None:
        if not await self._guard(interaction, member):
            return

        delete_days = max(0, min(7, delete_days))
        number = await record_case("ban", member.id, interaction.user.id, reason)
        try:
            await member.send(
                view=ui.panel("You were banned", ui.field("Reason", reason), color=0xE74C3C)
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            await member.ban(
                reason=f"{interaction.user}: {reason}", delete_message_days=delete_days
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return

        await self._announce("Ban", number, member, interaction.user, reason)
        await interaction.response.send_message(
            view=ui.ok(f"Banned {member.mention} — case **#{number}**."),
            ephemeral=True,
        )

    @app_commands.command(name="unban", description="Unban a user by ID")
    @require("hr")
    async def unban(
        self, interaction: discord.Interaction, user_id: str, reason: str = "No reason given"
    ) -> None:
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message(
                view=ui.err(f"`{user_id}` isn't a valid user ID."), ephemeral=True
            )
            return

        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=reason)
        except discord.NotFound:
            await interaction.response.send_message(
                view=ui.warn("They're not banned."), ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return

        await interaction.response.send_message(
            view=ui.ok(f"Unbanned `{uid}`."), ephemeral=True
        )

    @app_commands.command(name="purge", description="Bulk-delete recent messages")
    @app_commands.describe(amount="How many to delete (1-100)", member="Only delete this member's messages")
    @require("support", "hr")
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: int,
        member: discord.Member | None = None,
    ) -> None:
        amount = max(1, min(100, amount))
        await interaction.response.defer(ephemeral=True, thinking=True)

        def check(message: discord.Message) -> bool:
            return member is None or message.author.id == member.id

        try:
            deleted = await interaction.channel.purge(limit=amount, check=check)
        except discord.Forbidden:
            await interaction.followup.send(
                view=ui.err("I can't delete messages here."), ephemeral=True
            )
            return

        await interaction.followup.send(
            view=ui.ok(f"Deleted **{len(deleted)}** message(s)."), ephemeral=True
        )

    @app_commands.command(name="cases", description="Show a member's moderation history")
    @require("support", "hr")
    async def cases(self, interaction: discord.Interaction, member: discord.Member) -> None:
        data = await store.read()
        found = [c for c in (data.get("cases") or {}).values() if c.get("target") == member.id]
        found.sort(key=lambda c: c.get("at", 0), reverse=True)

        if not found:
            body = f"{member.mention} has a clean record."
        else:
            lines = [
                f"`#{c['number']}` **{c['action'].title()}** — {c['reason']} "
                f"(<t:{c.get('at', 0)}:d>, by <@{c['moderator']}>)"
                for c in found[:15]
            ]
            body = f"**{len(found)}** case(s)\n\n" + "\n".join(lines)
            if len(found) > 15:
                body += f"\n-# …and {len(found) - 15} older."

        await interaction.response.send_message(
            view=ui.panel(f"Cases — {member.display_name}", body), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
