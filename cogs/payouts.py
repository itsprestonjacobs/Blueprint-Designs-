"""Designer statistics and payout tracking.

Everything here is derived from the order log written by `/log`, so an order
that was never logged never counts toward stats or payouts.
"""

from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.config import config
from core.perms import require
from core.store import orders as order_store
from core.store import payouts as payout_store
from core.store import reviews as review_store

PERIODS = {
    "week": 7 * 86400,
    "month": 30 * 86400,
    "all": None,
}


def commission_rate() -> float:
    """Share of an order's price the designer keeps."""
    raw = config.get("payouts.commission_rate", 1.0)
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return min(max(rate, 0.0), 1.0)


async def orders_for(designer_id: int | None, since: int | None) -> list[dict]:
    data = await order_store.read()
    result = []
    for order in (data.get("orders") or {}).values():
        if designer_id is not None and order.get("designer") != designer_id:
            continue
        if since is not None and order.get("at", 0) < since:
            continue
        result.append(order)
    return result


async def paid_out(designer_id: int) -> int:
    data = await payout_store.read()
    return sum(
        p.get("amount", 0)
        for p in (data.get("payouts") or {}).values()
        if p.get("designer") == designer_id
    )


@app_commands.guild_only()
class Payouts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="stats", description="Order stats for a designer")
    @app_commands.describe(designer="Whose stats (defaults to you)", period="Time range")
    @app_commands.choices(
        period=[app_commands.Choice(name=p.title(), value=p) for p in PERIODS]
    )
    async def stats(
        self,
        interaction: discord.Interaction,
        designer: discord.Member | None = None,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        target = designer or interaction.user
        window = PERIODS[period.value] if period else None
        since = int(time.time()) - window if window else None

        found = await orders_for(target.id, since)
        gross = sum(o.get("price", 0) for o in found)
        rate = commission_rate()
        earned = int(gross * rate)
        already_paid = await paid_out(target.id)

        review_data = await review_store.read()
        their_reviews = [
            r for r in (review_data.get("reviews") or {}).values() if r.get("designer") == target.id
        ]
        avg = (
            sum(r.get("score", 0) for r in their_reviews) / len(their_reviews)
            if their_reviews
            else None
        )

        body = [
            ui.field("Designer", target.mention),
            ui.field("Period", (period.name if period else "All time")),
            ui.field("Orders completed", len(found)),
            ui.field("Gross value", f"{gross:,} Robux"),
        ]
        if rate < 1.0:
            body.append(ui.field(f"Their share ({rate:.0%})", f"{earned:,} Robux"))
        body.append(ui.field("Paid out to date", f"{already_paid:,} Robux"))
        body.append(ui.field("Outstanding", f"{max(earned - already_paid, 0):,} Robux"))
        if avg is not None:
            body.append(ui.field("Average review", f"{avg:.1f}/5 from {len(their_reviews)}"))

        await interaction.followup.send(
            view=ui.panel(f"Stats — {target.display_name}", "\n".join(body)), ephemeral=True
        )

    @app_commands.command(name="leaderboard", description="Top designers by completed orders")
    @app_commands.describe(period="Time range", by="Rank by order count or value")
    @app_commands.choices(
        period=[app_commands.Choice(name=p.title(), value=p) for p in PERIODS],
        by=[
            app_commands.Choice(name="Orders", value="orders"),
            app_commands.Choice(name="Value", value="value"),
        ],
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        by: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        window = PERIODS[period.value] if period else None
        since = int(time.time()) - window if window else None
        rank_by = by.value if by else "orders"

        found = await orders_for(None, since)
        if not found:
            await interaction.followup.send(
                view=ui.panel("Leaderboard", "No orders logged for this period yet.")
            )
            return

        totals: dict[int, dict[str, int]] = {}
        for order in found:
            entry = totals.setdefault(order.get("designer", 0), {"orders": 0, "value": 0})
            entry["orders"] += 1
            entry["value"] += order.get("price", 0)

        ranked = sorted(totals.items(), key=lambda kv: kv[1][rank_by], reverse=True)[:15]

        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = [
            f"{medals.get(i, f'`{i + 1}.`')} <@{uid}> — "
            f"**{stats['orders']}** order(s), {stats['value']:,} Robux"
            for i, (uid, stats) in enumerate(ranked)
        ]

        await interaction.followup.send(
            view=ui.panel(
                "Leaderboard",
                f"Ranked by **{rank_by}** · {period.name if period else 'All time'}\n\n"
                + "\n".join(lines),
            )
        )

    payout = app_commands.Group(name="payout", description="Record designer payouts")

    @payout.command(name="record", description="Record a payout to a designer")
    @app_commands.describe(designer="Who was paid", amount="How much, in Robux", note="Optional note")
    @require("admin", "hr")
    async def record(
        self,
        interaction: discord.Interaction,
        designer: discord.Member,
        amount: int,
        note: str | None = None,
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                view=ui.err("Amount must be greater than zero."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        pid = await payout_store.next_id("_counter")
        async with payout_store.edit() as data:
            data.setdefault("payouts", {})[str(pid)] = {
                "id": pid,
                "designer": designer.id,
                "amount": amount,
                "note": note,
                "paid_by": interaction.user.id,
                "at": int(time.time()),
            }

        total = await paid_out(designer.id)
        await interaction.followup.send(
            view=ui.ok(f"Recorded **{amount:,}** Robux to {designer.mention}.\n"
                f"-# Lifetime total: {total:,} Robux."
            ),
            ephemeral=True,
        )

    @payout.command(name="owed", description="What every designer is currently owed")
    @require("admin", "hr")
    async def owed(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        found = await orders_for(None, None)
        rate = commission_rate()

        gross: dict[int, int] = {}
        for order in found:
            gross[order.get("designer", 0)] = gross.get(order.get("designer", 0), 0) + order.get("price", 0)

        payout_data = await payout_store.read()
        paid: dict[int, int] = {}
        for p in (payout_data.get("payouts") or {}).values():
            paid[p.get("designer", 0)] = paid.get(p.get("designer", 0), 0) + p.get("amount", 0)

        rows = []
        for designer_id, total in sorted(gross.items(), key=lambda kv: kv[1], reverse=True):
            earned = int(total * rate)
            outstanding = earned - paid.get(designer_id, 0)
            if outstanding > 0:
                rows.append(f"<@{designer_id}> — **{outstanding:,}** Robux outstanding")

        body = "\n".join(rows[:20]) if rows else "Everyone's paid up."
        await interaction.followup.send(
            view=ui.panel("Outstanding Payouts", body), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Payouts(bot))
