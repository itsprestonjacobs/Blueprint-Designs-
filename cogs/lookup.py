"""Member lookup.

Pulls everything the bot knows about someone into one place: account age,
join date, roles, global ban status, infractions, moderation cases, leave, and
open tickets.

Staff otherwise have to run five commands and read three channels to decide
whether someone is a problem. Account age is called out because a new account
is the single most useful signal when something feels off.
"""

from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.perms import require
from core.store import JSONStore

# Read-only views of the other modules' stores. Separate instances share the
# same lock via JSONStore's class-level registry.
gban_store = JSONStore("globalbans", {})
infraction_store = JSONStore("infractions", {})
case_store = JSONStore("modcases", {})
loa_store = JSONStore("loa", {})
ticket_store = JSONStore("tickets", {})

YOUNG_ACCOUNT_DAYS = 7


@app_commands.guild_only()
class Lookup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="lookup", description="Everything the bot knows about a member")
    @app_commands.describe(member="Who to look up", user_id="Or a raw ID, if they've left")
    @require("support", "designer", "hr", "admin")
    async def lookup(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        user_id: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        target_id = member.id if member else None
        if target_id is None and user_id:
            try:
                target_id = int(user_id.strip().strip("<@!>"))
            except ValueError:
                await interaction.followup.send(
                    view=ui.err("That's not a valid user ID."), ephemeral=True
                )
                return
        if target_id is None:
            await interaction.followup.send(
                view=ui.err("Give me a member or a user ID."), ephemeral=True
            )
            return

        user = member
        if user is None:
            try:
                user = await self.bot.fetch_user(target_id)
            except discord.HTTPException:
                user = None

        now = int(time.time())
        body: list[str] = []
        flags: list[str] = []

        # --- identity ---
        if user is not None:
            created = int(user.created_at.timestamp())
            age_days = (now - created) / 86400
            body.append(ui.field("User", f"{user} (`{target_id}`)"))
            body.append(
                ui.field("Account made", f"<t:{created}:D> (<t:{created}:R>)")
            )
            if age_days < YOUNG_ACCOUNT_DAYS:
                flags.append(f"account is only **{age_days:.1f} days** old")
        else:
            body.append(ui.field("User", f"`{target_id}` — couldn't fetch"))

        if member is not None:
            joined = int(member.joined_at.timestamp()) if member.joined_at else 0
            body.append(ui.field("Joined server", f"<t:{joined}:D> (<t:{joined}:R>)"))
            roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
            body.append(
                ui.field("Roles", " ".join(roles[:8]) + ("…" if len(roles) > 8 else "") or "none")
            )
            if member.is_timed_out():
                until = int(member.timed_out_until.timestamp())
                flags.append(f"currently timed out until <t:{until}:R>")
        else:
            body.append(ui.field("In this server", "no"))

        # --- record ---
        gban = (await gban_store.read()).get("bans", {}).get(str(target_id))
        if gban:
            flags.append(f"**globally banned** — {gban.get('reason', 'no reason')}")

        infractions = [
            r
            for r in (await infraction_store.read()).get("records", {}).values()
            if r.get("user") == target_id and not r.get("void")
        ]
        cases = [
            c
            for c in (await case_store.read()).get("cases", {}).values()
            if c.get("target") == target_id
        ]

        body.append("")
        body.append(ui.field("Infractions", len(infractions) or "none"))
        if infractions:
            latest = max(infractions, key=lambda r: r.get("at", 0))
            body.append(
                f"-# latest: {latest['type']} — {latest.get('reason', '')[:50]} "
                f"(<t:{latest.get('at', 0)}:R>)"
            )
        body.append(ui.field("Mod cases", len(cases) or "none"))
        if cases:
            latest = max(cases, key=lambda c: c.get("at", 0))
            body.append(
                f"-# latest: {latest['action']} — {latest.get('reason', '')[:50]} "
                f"(<t:{latest.get('at', 0)}:R>)"
            )

        # --- current state ---
        leave = next(
            (
                e
                for e in (await loa_store.read()).get("requests", {}).values()
                if e.get("user") == target_id and e.get("status") == "approved"
            ),
            None,
        )
        if leave:
            body.append(ui.field("On leave", f"back <t:{leave.get('until', 0)}:R>"))

        ticket_data = await ticket_store.read()
        tickets = ticket_data.get("tickets", {})
        open_tickets = [
            t for t in tickets.values()
            if t.get("user") == target_id and t.get("status") == "open"
        ]
        handled = [t for t in tickets.values() if t.get("claimed_by") == target_id]
        if open_tickets:
            body.append(ui.field("Open tickets", len(open_tickets)))
        if handled:
            body.append(ui.field("Tickets handled", len(handled)))

        if str(target_id) in (ticket_data.get("blocked") or {}):
            flags.append("blocked from closing tickets")

        # Flags decide the colour, so a problem account reads as one at a glance.
        if flags:
            body.append("")
            body.append("**Flags**")
            body.extend(f"- {f}" for f in flags)

        colour = ui.RED_HEX if gban else (ui.AMBER_HEX if flags else ui.GREEN_HEX)
        name = user.display_name if isinstance(user, discord.Member) else (str(user) if user else target_id)

        await interaction.followup.send(
            view=ui.panel(f"Lookup — {name}", "\n".join(body), color=colour),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Lookup(bot))
