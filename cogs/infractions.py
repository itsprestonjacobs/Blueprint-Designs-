"""Staff infractions.

Issue a disciplinary record against a staff member. Every one is numbered,
logged to the punishment channel, DM'd to the member, and kept on their record
so escalation is based on history rather than memory.

Records are never deleted -- voiding one marks it void and keeps the trail,
because a disciplinary history that can be quietly erased isn't a record.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.config import config
from core.logs import get_channel
from core.perms import require
from core.store import JSONStore

log = logging.getLogger("blueprint.infractions")

store = JSONStore("infractions", {})

# Ordered least to most severe; the index drives the colour and the escalation
# hint shown to whoever is issuing.
SEVERITY = ["Notice", "Warning", "Strike", "Suspension", "Demotion", "Termination"]

COLOURS = {
    "Notice": ui.AMBER_HEX,
    "Warning": ui.AMBER_HEX,
    "Strike": 0xE67E22,
    "Suspension": ui.RED_HEX,
    "Demotion": ui.RED_HEX,
    "Termination": 0x992D22,
}


def types() -> list[str]:
    return config.get("infractions.types", SEVERITY) or SEVERITY


async def records_for(user_id: int, include_void: bool = False) -> list[dict]:
    data = await store.read()
    rows = [
        r
        for r in (data.get("records") or {}).values()
        if r.get("user") == user_id and (include_void or not r.get("void"))
    ]
    rows.sort(key=lambda r: r.get("at", 0), reverse=True)
    return rows


def record_view(number: int, entry: dict) -> ui.BaseLayout:
    kind = entry.get("type", "Warning")
    body = [
        ui.field("Member", f"<@{entry['user']}> (`{entry['user']}`)"),
        ui.field("Type", kind),
        ui.field("Issued by", f"<@{entry['issued_by']}>"),
        ui.field("When", f"<t:{entry.get('at', 0)}:F>"),
        "",
        "**Reason**",
        entry.get("reason", "")[:800],
    ]
    if entry.get("notes"):
        body += ["", ui.field("Notes", entry["notes"])]
    if entry.get("void"):
        body += ["", ui.field("Voided by", f"<@{entry['void_by']}>")]
        if entry.get("void_reason"):
            body.append(ui.field("Void reason", entry["void_reason"]))

    return ui.panel(
        f"Infraction #{number}" + (" (void)" if entry.get("void") else ""),
        "\n".join(body),
        color=None if entry.get("void") else COLOURS.get(kind, ui.RED_HEX),
        footer=config.get("branding.footer", "Sail's Customs"),
    )


@app_commands.guild_only()
class Infractions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    infraction = app_commands.Group(
        name="infraction", description="Staff disciplinary records"
    )

    @infraction.command(name="issue", description="Issue an infraction")
    @app_commands.describe(
        member="Who it's against",
        type="Severity",
        reason="Why it's being issued",
        notes="Anything extra for the record",
    )
    @app_commands.choices(
        type=[app_commands.Choice(name=t, value=t) for t in SEVERITY]
    )
    @require("hr", "admin")
    async def issue(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        type: app_commands.Choice[str],
        reason: str,
        notes: str | None = None,
    ) -> None:
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                view=ui.err("You can't infract yourself."), ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message(
                view=ui.err("Bots don't take infractions."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        number = await store.next_id("_counter")
        entry = {
            "number": number,
            "user": member.id,
            "type": type.value,
            "reason": reason,
            "notes": notes,
            "issued_by": interaction.user.id,
            "at": int(time.time()),
            "void": False,
        }
        async with store.edit() as data:
            data.setdefault("records", {})[str(number)] = entry

        view = record_view(number, entry)
        channel = await get_channel(self.bot, "infraction_log")
        if channel is not None:
            try:
                await channel.send(view=view)
            except discord.HTTPException:
                log.exception("could not log infraction %s", number)

        dmed = False
        if config.get("infractions.dm_member", True):
            try:
                await member.send(
                    view=ui.panel(
                        f"You received a {type.value.lower()}",
                        "\n".join(
                            [
                                ui.field("Reason", reason),
                                ui.field("Issued by", str(interaction.user)),
                                "",
                                "-# Reply in a support ticket if you want to appeal.",
                            ]
                        ),
                        color=COLOURS.get(type.value, ui.RED_HEX),
                    )
                )
                dmed = True
            except (discord.Forbidden, discord.HTTPException):
                pass

        prior = len(await records_for(member.id)) - 1
        summary = f"Infraction **#{number}** issued to {member.mention}."
        if prior > 0:
            summary += f"\n-# That's **{prior}** prior on record."
        if not dmed:
            summary += "\n-# Couldn't DM them."
        if channel is None:
            summary += "\n-# No infraction log channel set, so nothing was posted."

        await interaction.followup.send(view=ui.ok(summary), ephemeral=True)

    @infraction.command(name="history", description="Someone's infraction record")
    @app_commands.describe(member="Whose record", show_void="Include voided entries")
    @require("hr", "admin")
    async def history(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        show_void: bool = False,
    ) -> None:
        rows = await records_for(member.id, include_void=show_void)

        if not rows:
            body = f"{member.mention} has a clean record."
        else:
            counts: dict[str, int] = {}
            for r in rows:
                if not r.get("void"):
                    counts[r["type"]] = counts.get(r["type"], 0) + 1
            tally = " · ".join(f"{v}× {k}" for k, v in counts.items()) or "all void"

            lines = [
                f"`#{r['number']}` **{r['type']}**{' (void)' if r.get('void') else ''} — "
                f"{r.get('reason', '')[:60]} (<t:{r.get('at', 0)}:d>, by <@{r['issued_by']}>)"
                for r in rows[:15]
            ]
            body = f"**{len(rows)}** on record · {tally}\n\n" + "\n".join(lines)
            if len(rows) > 15:
                body += f"\n-# …and {len(rows) - 15} older."

        await interaction.response.send_message(
            view=ui.panel(f"Record — {member.display_name}", body), ephemeral=True
        )

    @infraction.command(name="view", description="Show one infraction in full")
    @require("hr", "admin")
    async def view_one(self, interaction: discord.Interaction, number: int) -> None:
        data = await store.read()
        entry = (data.get("records") or {}).get(str(number))
        if entry is None:
            await interaction.response.send_message(
                view=ui.err(f"No infraction **#{number}**."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            view=record_view(number, entry), ephemeral=True
        )

    @infraction.command(name="void", description="Void an infraction, keeping the record")
    @app_commands.describe(number="Infraction number", reason="Why it's being voided")
    @require("hr", "admin")
    async def void(
        self, interaction: discord.Interaction, number: int, reason: str
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with store.edit() as data:
            entry = (data.get("records") or {}).get(str(number))
            if entry is None:
                await interaction.followup.send(
                    view=ui.err(f"No infraction **#{number}**."), ephemeral=True
                )
                return
            if entry.get("void"):
                await interaction.followup.send(
                    view=ui.warn("That one is already void."), ephemeral=True
                )
                return
            entry["void"] = True
            entry["void_by"] = interaction.user.id
            entry["void_reason"] = reason
            entry["void_at"] = int(time.time())
            snapshot = dict(entry)

        channel = await get_channel(self.bot, "infraction_log")
        if channel is not None:
            try:
                await channel.send(view=record_view(number, snapshot))
            except discord.HTTPException:
                pass

        user = self.bot.get_user(snapshot["user"])
        if user is not None:
            try:
                await user.send(
                    view=ui.panel(
                        "Infraction Voided",
                        f"Infraction **#{number}** has been voided.\n\n"
                        f"**Reason:** {reason}",
                        color=ui.GREEN_HEX,
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send(
            view=ui.ok(f"Infraction **#{number}** voided."), ephemeral=True
        )


PREVIEW_VIEWS = [
    (
        "record",
        lambda: record_view(
            1,
            {
                "user": 1,
                "type": "Strike",
                "reason": "Claimed three orders and went inactive for a week.",
                "issued_by": 2,
                "at": 1786290000,
            },
        ),
    ),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Infractions(bot))
