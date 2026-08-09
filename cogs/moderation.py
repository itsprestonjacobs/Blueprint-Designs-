"""Moderation with a persistent case log.

Every action gets a numbered case, logged and DM'd, so history survives staff
turnover. Guards are shared rather than repeated per command: acting on someone
at or above your own role height, on the owner, or on the bot are all refused
in one place.
"""

from __future__ import annotations

import datetime
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.config import config
from core.logs import get_channel
from core.perms import has_tier, require
from core.store import JSONStore

log = logging.getLogger("blueprint.moderation")

store = JSONStore("modcases", {})

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MAX_TIMEOUT = 28 * 86400  # Discord's hard cap

COLOURS = {
    "warn": ui.AMBER_HEX,
    "timeout": 0xE67E22,
    "kick": ui.RED_HEX,
    "ban": 0x992D22,
    "unban": ui.GREEN_HEX,
    "untimeout": ui.GREEN_HEX,
}


def parse_duration(text: str) -> int | None:
    total = 0
    for amount, unit in DURATION_RE.findall(text or ""):
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total or None


async def record_case(
    action: str, target_id: int, moderator_id: int, reason: str, extra: dict | None = None
) -> int:
    number = await store.next_id("_counter")
    async with store.edit() as data:
        data.setdefault("cases", {})[str(number)] = {
            "number": number,
            "action": action,
            "target": target_id,
            "moderator": moderator_id,
            "reason": reason,
            "at": int(time.time()),
            **(extra or {}),
        }
    return number


async def cases_for(user_id: int) -> list[dict]:
    data = await store.read()
    rows = [c for c in (data.get("cases") or {}).values() if c.get("target") == user_id]
    rows.sort(key=lambda c: c.get("at", 0), reverse=True)
    return rows


def case_view(number: int, action: str, target, moderator, reason: str, extra: str = "") -> ui.BaseLayout:
    body = [
        ui.field("Member", f"{target.mention} (`{target.id}`)"),
        ui.field("Moderator", moderator.mention),
        ui.field("Reason", reason),
    ]
    if extra:
        body.append(extra)
    return ui.panel(
        f"{action.title()} — Case #{number}",
        "\n".join(body),
        color=COLOURS.get(action, ui.RED_HEX),
        footer=config.get("branding.footer", "Sail's Customs"),
    )


@app_commands.guild_only()
class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def guard(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> bool:
        """Shared safety checks. Replies and returns False when blocked."""
        reason = None

        if member.id == interaction.user.id:
            reason = "You can't do that to yourself."
        elif self.bot.user and member.id == self.bot.user.id:
            reason = "That's me."
        elif member.id == member.guild.owner_id:
            reason = "That's the server owner."
        elif isinstance(interaction.user, discord.Member) and (
            interaction.user.id != member.guild.owner_id
            and member.top_role >= interaction.user.top_role
        ):
            reason = f"{member.mention} is at or above your role height."
        elif member.guild.me and member.top_role >= member.guild.me.top_role:
            reason = f"{member.mention} is above me — move my role higher."

        if reason is None:
            return True

        send = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        await send(view=ui.err(reason), ephemeral=True)
        return False

    async def announce(self, view: ui.BaseLayout) -> None:
        for key in ("mod_log", "security_log"):
            channel = await get_channel(self.bot, key)
            if channel is not None:
                try:
                    await channel.send(view=view)
                except discord.HTTPException:
                    log.exception("could not post to %s", key)
                return

    async def notify(self, member: discord.Member, title: str, body: str, colour: int) -> bool:
        try:
            await member.send(view=ui.panel(title, body, color=colour))
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    # -- commands ---------------------------------------------------------

    @app_commands.command(name="warn", description="Warn a member")
    @require("support", "hr", "admin")
    async def warn(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ) -> None:
        if not await self.guard(interaction, member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        number = await record_case("warn", member.id, interaction.user.id, reason)
        dmed = await self.notify(
            member, "You were warned", ui.field("Reason", reason), ui.AMBER_HEX
        )
        await self.announce(
            case_view(number, "warn", member, interaction.user, reason)
        )

        prior = len(await cases_for(member.id)) - 1
        note = f"\n-# {prior} prior case(s) on record." if prior > 0 else ""
        note += "" if dmed else "\n-# Couldn't DM them."
        await interaction.followup.send(
            view=ui.ok(f"Warned {member.mention} — case **#{number}**.{note}"),
            ephemeral=True,
        )

    @app_commands.command(name="timeout", description="Time a member out")
    @app_commands.describe(duration="e.g. 10m, 2h, 1d — max 28d")
    @require("support", "hr", "admin")
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: str = "No reason given",
    ) -> None:
        if not await self.guard(interaction, member):
            return

        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                view=ui.err("I couldn't read that. Try `10m`, `2h` or `1d`."), ephemeral=True
            )
            return
        if seconds > MAX_TIMEOUT:
            await interaction.response.send_message(
                view=ui.err("28 days is Discord's maximum."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await member.timeout(
                datetime.timedelta(seconds=seconds), reason=f"{interaction.user}: {reason}"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return

        until = int(time.time()) + seconds
        number = await record_case(
            "timeout", member.id, interaction.user.id, reason, {"until": until}
        )
        await self.notify(
            member,
            "You were timed out",
            f"{ui.field('Reason', reason)}\n{ui.field('Until', f'<t:{until}:F>')}",
            COLOURS["timeout"],
        )
        await self.announce(
            case_view(
                number, "timeout", member, interaction.user, reason,
                ui.field("Until", f"<t:{until}:F> (<t:{until}:R>)"),
            )
        )
        await interaction.followup.send(
            view=ui.ok(f"Timed out {member.mention} until <t:{until}:R> — case **#{number}**."),
            ephemeral=True,
        )

    @app_commands.command(name="untimeout", description="Lift a member's timeout")
    @require("support", "hr", "admin")
    async def untimeout(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await member.timeout(None, reason=f"Lifted by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return
        await interaction.followup.send(
            view=ui.ok(f"Timeout lifted for {member.mention}."), ephemeral=True
        )

    @app_commands.command(name="kick", description="Kick a member")
    @require("hr", "admin")
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason given",
    ) -> None:
        if not await self.guard(interaction, member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        # DM before removing them, or they can't receive it.
        await self.notify(
            member, "You were kicked", ui.field("Reason", reason), ui.RED_HEX
        )
        try:
            await member.kick(reason=f"{interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.followup.send(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return

        number = await record_case("kick", member.id, interaction.user.id, reason)
        await self.announce(case_view(number, "kick", member, interaction.user, reason))
        await interaction.followup.send(
            view=ui.ok(f"Kicked {member.mention} — case **#{number}**."), ephemeral=True
        )

    @app_commands.command(name="ban", description="Ban a member from this server")
    @app_commands.describe(delete_days="Days of their messages to delete (0-7)")
    @require("hr", "admin")
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason given",
        delete_days: int = 0,
    ) -> None:
        if not await self.guard(interaction, member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.notify(
            member, "You were banned", ui.field("Reason", reason), COLOURS["ban"]
        )
        try:
            await member.ban(
                reason=f"{interaction.user}: {reason}",
                delete_message_days=max(0, min(7, delete_days)),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return

        number = await record_case("ban", member.id, interaction.user.id, reason)
        await self.announce(case_view(number, "ban", member, interaction.user, reason))
        await interaction.followup.send(
            view=ui.ok(
                f"Banned {member.mention} — case **#{number}**."
                "\n-# This is one server only. Use `/gban add` to ban everywhere."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="unban", description="Unban a user by ID")
    @require("hr", "admin")
    async def unban(
        self, interaction: discord.Interaction, user_id: str, reason: str = "Appeal accepted"
    ) -> None:
        try:
            uid = int(user_id.strip().strip("<@!>"))
        except ValueError:
            await interaction.response.send_message(
                view=ui.err("That's not a valid user ID."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=reason)
        except discord.NotFound:
            await interaction.followup.send(
                view=ui.warn("They aren't banned here."), ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                view=ui.err("I don't have permission for that."), ephemeral=True
            )
            return

        await record_case("unban", uid, interaction.user.id, reason)
        await interaction.followup.send(view=ui.ok(f"Unbanned `{uid}`."), ephemeral=True)

    @app_commands.command(name="purge", description="Bulk delete recent messages")
    @app_commands.describe(
        amount="How many to check (1-100)", member="Only delete this member's messages"
    )
    @require("support", "hr", "admin")
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
            view=ui.ok(
                f"Deleted **{len(deleted)}** message(s)"
                + (f" from {member.mention}." if member else ".")
                + "\n-# Discord can't bulk delete anything older than 14 days."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="cases", description="A member's moderation history")
    @require("support", "hr", "admin")
    async def cases(self, interaction: discord.Interaction, member: discord.Member) -> None:
        rows = await cases_for(member.id)
        if not rows:
            body = f"{member.mention} has a clean record."
        else:
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["action"]] = counts.get(r["action"], 0) + 1
            tally = " · ".join(f"{v}× {k}" for k, v in counts.items())
            lines = [
                f"`#{r['number']}` **{r['action'].title()}** — {r.get('reason', '')[:55]} "
                f"(<t:{r.get('at', 0)}:d>, by <@{r['moderator']}>)"
                for r in rows[:15]
            ]
            body = f"**{len(rows)}** case(s) · {tally}\n\n" + "\n".join(lines)
            if len(rows) > 15:
                body += f"\n-# …and {len(rows) - 15} older."

        await interaction.response.send_message(
            view=ui.panel(f"Cases — {member.display_name}", body), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
