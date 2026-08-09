"""Order logging, the service status board, and the live queue.

Boards are single messages the bot edits in place rather than reposting, so
their links stay valid. Their locations live in data/boards.json.
"""

from __future__ import annotations

import logging
import math
import time

import discord
from discord import app_commands, ui as dui
from discord.ext import commands, tasks

from core import ui
from core.config import config
from core.logs import get_channel
from core.perms import require
from core.store import boards, orders as order_store, tickets as ticket_store

log = logging.getLogger("blueprint.orders")

# Roblox takes a 30% cut of every gamepass sale, so a designer who wants to
# receive N robux must price the pass at N / 0.7.
ROBLOX_PAYOUT_RATE = 0.70

STATUS_STATES = ("OPENED", "CLOSED", "LIMITED")
STATUS_ICON = {"OPENED": ui.GREEN, "CLOSED": ui.RED, "LIMITED": ui.YELLOW}


def gamepass_price(target_robux: int) -> int:
    """Price a gamepass must be listed at for the seller to net `target_robux`."""
    return math.ceil(target_robux / ROBLOX_PAYOUT_RATE)


# -- board rendering ------------------------------------------------------


def status_view() -> ui.BaseLayout:
    status = config.get("order_status", {}) or {}
    if not status:
        body = "_No services configured in `order_status`._"
    else:
        body = "\n".join(
            f"**{name}:** {STATUS_ICON.get(str(state).upper(), ui.YELLOW)} `{state}`"
            for name, state in status.items()
        )
    return ui.panel(
        "Order Status",
        body,
        footer=f"Last updated <t:{int(time.time())}:R>",
    )


async def queue_view(bot: discord.Client) -> ui.BaseLayout:
    """Live queue built from currently-open order tickets."""
    data = await ticket_store.read()
    open_tickets = [
        t for t in (data.get("tickets") or {}).values() if t.get("status") == "open"
    ]
    open_tickets.sort(key=lambda t: t.get("created_at", 0))

    if not open_tickets:
        body = "Queue is empty."
    else:
        lines = []
        # Cap the list so a busy queue can't blow the 4000-char text limit.
        for t in open_tickets[:20]:
            claimed = f"<@{t['claimed_by']}>" if t.get("claimed_by") else "_unclaimed_"
            lines.append(
                f"`#{t.get('number', '?')}` **{t.get('label', 'Ticket')}** — "
                f"<@{t.get('user')}> · {claimed} · <t:{t.get('created_at', 0)}:R>"
            )
        body = "\n".join(lines)
        if len(open_tickets) > 20:
            body += f"\n-# …and {len(open_tickets) - 20} more."

    return ui.panel(
        "Live Queue",
        f"**{len(open_tickets)}** order(s) in progress.\n\n{body}",
        footer=f"Refreshed <t:{int(time.time())}:R>",
    )


async def remember_board(key: str, message: discord.Message) -> None:
    async with boards.edit() as data:
        data[key] = {"channel": message.channel.id, "message": message.id}


async def refresh_board(bot: discord.Client, key: str, view: ui.BaseLayout) -> bool:
    """Edit a stored board message in place. False if it's gone or unset."""
    data = await boards.read()
    entry = data.get(key)
    if not entry:
        return False

    channel = bot.get_channel(entry["channel"])
    if channel is None:
        return False

    try:
        message = await channel.fetch_message(entry["message"])
        # A V2 edit must explicitly clear the classic message fields.
        await message.edit(view=view, content=None, embeds=[], attachments=[])
        return True
    except (discord.NotFound, discord.Forbidden):
        async with boards.edit() as d:
            d.pop(key, None)
        return False
    except discord.HTTPException:
        log.exception("failed to refresh board %s", key)
        return False


# -- cog ------------------------------------------------------------------


@app_commands.guild_only()
class Orders(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.queue_refresher.start()

    async def cog_unload(self) -> None:
        self.queue_refresher.cancel()

    @tasks.loop(minutes=5)
    async def queue_refresher(self) -> None:
        await refresh_board(self.bot, "queue_board", await queue_view(self.bot))

    @queue_refresher.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    # -- order logging ----------------------------------------------------

    @app_commands.command(name="log", description="Log a completed order")
    @app_commands.describe(
        customer="Who the order was for",
        service="What was delivered",
        price="Price in Robux",
        designer="Who completed it (defaults to you)",
        notes="Anything worth recording",
        proof="Screenshot of the finished work",
    )
    @require("designer", "support", "hr")
    async def log_order(
        self,
        interaction: discord.Interaction,
        customer: discord.User,
        service: str,
        price: int,
        designer: discord.Member | None = None,
        notes: str | None = None,
        proof: discord.Attachment | None = None,
    ) -> None:
        if price < 0:
            await interaction.response.send_message(
                view=ui.err("Price can't be negative."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        worker = designer or interaction.user
        number = await order_store.next_id("_counter")

        async with order_store.edit() as data:
            data.setdefault("orders", {})[str(number)] = {
                "number": number,
                "customer": customer.id,
                "designer": worker.id,
                "service": service,
                "price": price,
                "notes": notes,
                "proof": proof.url if proof else None,
                "logged_by": interaction.user.id,
                "at": int(time.time()),
            }

        body = [
            ui.field("Order", f"#{number}"),
            ui.field("Customer", customer.mention),
            ui.field("Designer", worker.mention),
            ui.field("Service", service),
            ui.field("Price", f"{price:,} Robux"),
        ]
        if notes:
            body.append(ui.field("Notes", notes))

        view = ui.panel(
            "Order Logged",
            "\n".join(body),
            banner=proof.url if proof and proof.content_type and proof.content_type.startswith("image/") else None,
        )

        channel = await get_channel(self.bot, "order_log")
        if channel is not None:
            await channel.send(view=view)

        await interaction.followup.send(
            view=ui.ok(f"Logged order **#{number}**"
                + ("" if channel else "\n-# No order log set, so nothing was posted publicly.")
            ),
            ephemeral=True,
        )

    # -- gamepass pricing -------------------------------------------------

    @app_commands.command(name="pass", description="Work out the gamepass price for an order")
    @app_commands.describe(
        amount="Robux you want to actually receive",
        customer="Who the pass is for",
    )
    @require("designer", "support", "hr")
    async def pass_price(
        self,
        interaction: discord.Interaction,
        amount: int,
        customer: discord.User | None = None,
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                view=ui.err("Amount must be greater than zero."), ephemeral=True
            )
            return

        listed = gamepass_price(amount)
        body = [
            ui.field("You receive", f"{amount:,} Robux"),
            ui.field("Set the gamepass to", f"**{listed:,} Robux**"),
            ui.field("Roblox cut (30%)", f"{listed - amount:,} Robux"),
        ]
        if customer:
            body.insert(0, ui.field("Customer", customer.mention))

        await interaction.response.send_message(
            view=ui.panel(
                "Gamepass Price",
                "\n".join(body),
                footer="Roblox takes 30% of every gamepass sale.",
            )
        )

    # -- status board -----------------------------------------------------

    status = app_commands.Group(name="status", description="Service status board")

    @status.command(name="post", description="Post the order status board in this channel")
    @require("admin", "hr")
    async def status_post(self, interaction: discord.Interaction) -> None:
        message = await interaction.channel.send(view=status_view())
        await remember_board("status_board", message)
        await interaction.response.send_message(
            view=ui.ok("Board posted. `/updatestatus` edits it in place."),
            ephemeral=True,
        )

    @app_commands.command(name="updatestatus", description="Update a service's order status")
    @app_commands.describe(service="Which service", state="New status")
    @app_commands.choices(
        state=[app_commands.Choice(name=s.title(), value=s) for s in STATUS_STATES]
    )
    @require("admin", "hr")
    async def updatestatus(
        self,
        interaction: discord.Interaction,
        service: str,
        state: app_commands.Choice[str],
    ) -> None:
        statuses = config.get("order_status", {}) or {}
        if service not in statuses:
            await interaction.response.send_message(
                view=ui.err(f"`{service}` isn't in `order_status`. "
                    f"Known services: {', '.join(f'`{s}`' for s in statuses) or 'none'}"
                ),
                ephemeral=True,
            )
            return

        config.set(f"order_status.{service}", state.value)
        config.save()

        edited = await refresh_board(self.bot, "status_board", status_view())

        await interaction.response.send_message(
            view=ui.notice(
                f"{STATUS_ICON[state.value]} **{service}** is now `{state.value}`."
                + ("" if edited else "\n-# No status board posted yet — run `/status post`.")
            ),
            ephemeral=True,
        )

    @updatestatus.autocomplete("service")
    async def service_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        statuses = config.get("order_status", {}) or {}
        current = current.lower()
        return [
            app_commands.Choice(name=name, value=name)
            for name in statuses
            if current in name.lower()
        ][:25]

    # -- queue board ------------------------------------------------------

    queue = app_commands.Group(name="queue", description="Live order queue")

    @queue.command(name="post", description="Post the live queue board in this channel")
    @require("admin", "hr")
    async def queue_post(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await interaction.channel.send(view=await queue_view(self.bot))
        await remember_board("queue_board", message)
        await interaction.followup.send(
            view=ui.ok("Queue posted. Refreshes every 5 minutes."),
            ephemeral=True,
        )

    @queue.command(name="refresh", description="Refresh the live queue now")
    @require("admin", "hr", "support")
    async def queue_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await refresh_board(self.bot, "queue_board", await queue_view(self.bot))
        await interaction.followup.send(
            view=ui.ok("Queue refreshed."
                if ok
                else "No queue board up yet. Run `/queue post`."
            ),
            ephemeral=True,
        )


PREVIEW_VIEWS = [
    ("status-board", status_view),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Orders(bot))
