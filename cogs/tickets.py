"""Tickets, opened from a plain dropdown.

No embeds and no Components V2 anywhere here: the panel is a line of text and a
select menu, and the ticket itself opens with a normal message. Two panels are
configured -- orders and support -- each listing its own categories.

The claim and close buttons are DynamicItems whose custom_id carries the ticket
channel ID, so they keep working after a restart without registering one
persistent view per open ticket.
"""

from __future__ import annotations

import io
import logging
import re
import time

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.config import config
from core.logs import get_channel
from core.perms import has_tier, require
from core.store import JSONStore

log = logging.getLogger("blueprint.tickets")

store = JSONStore("tickets", {})

STAFF_TIERS = ("support", "designer", "hr")


def panels() -> dict[str, dict]:
    return config.get("tickets.panels", {}) or {}


def categories() -> dict[str, dict]:
    return config.get("tickets.categories", {}) or {}


async def get_ticket(channel_id: int) -> dict | None:
    data = await store.read()
    return (data.get("tickets") or {}).get(str(channel_id))


async def open_count(user_id: int) -> int:
    data = await store.read()
    return sum(
        1
        for t in (data.get("tickets") or {}).values()
        if t.get("user") == user_id and t.get("status") == "open"
    )


async def build_transcript(channel: discord.TextChannel) -> discord.File | None:
    try:
        lines = []
        async for message in channel.history(limit=None, oldest_first=True):
            stamp = message.created_at.strftime("%Y-%m-%d %H:%M")
            content = message.clean_content or ""
            for att in message.attachments:
                content += f"\n    [attachment] {att.url}"
            lines.append(f"[{stamp}] {message.author}: {content}")
        body = "\n".join(lines) or "(no messages)"
        return discord.File(
            io.BytesIO(body.encode("utf-8")), filename=f"transcript-{channel.name}.txt"
        )
    except discord.HTTPException:
        return None


# -- ticket controls ------------------------------------------------------


class ClaimButton(dui.DynamicItem[dui.Button], template=r"tk:claim:(?P<cid>\d+)"):
    def __init__(self, cid: int, claimed: bool = False) -> None:
        super().__init__(
            dui.Button(
                label="Claimed" if claimed else "Claim",
                style=discord.ButtonStyle.success,
                custom_id=f"tk:claim:{cid}",
                disabled=claimed,
            )
        )
        self.cid = cid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not has_tier(interaction.user, *STAFF_TIERS):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return

        async with store.edit() as data:
            ticket = (data.get("tickets") or {}).get(str(self.cid))
            if ticket is None:
                await interaction.response.send_message(
                    "I have no record of this ticket.", ephemeral=True
                )
                return
            if ticket.get("claimed_by"):
                await interaction.response.send_message(
                    f"Already claimed by <@{ticket['claimed_by']}>.", ephemeral=True
                )
                return
            ticket["claimed_by"] = interaction.user.id
            snapshot = dict(ticket)

        category = categories().get(snapshot.get("category"), {})
        await interaction.response.edit_message(
            view=opening_view(self.cid, snapshot, category),
            content=None,
            embeds=[],
            attachments=[],
        )
        await interaction.followup.send(
            f"{interaction.user.mention} is handling this ticket."
        )


class CloseButton(dui.DynamicItem[dui.Button], template=r"tk:close:(?P<cid>\d+)"):
    def __init__(self, cid: int) -> None:
        super().__init__(
            dui.Button(
                label="Close", style=discord.ButtonStyle.danger, custom_id=f"tk:close:{cid}"
            )
        )
        self.cid = cid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        ticket = await get_ticket(self.cid)
        if ticket is None:
            await interaction.response.send_message(
                "I have no record of this ticket.", ephemeral=True
            )
            return

        if ticket.get("user") != interaction.user.id and not has_tier(
            interaction.user, *STAFF_TIERS
        ):
            await interaction.response.send_message(
                "You can't close this ticket.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Close this ticket? A transcript is saved first.",
            view=ConfirmClose(self.cid),
            ephemeral=True,
        )


def opening_view(cid: int, ticket: dict, category: dict) -> ui.BaseLayout:
    """The panel pinned at the top of a ticket.

    Rebuilt from the stored ticket whenever something changes, so claiming
    updates this message in place rather than adding another one below it.
    """
    claimed_by = ticket.get("claimed_by")

    header = f"{category.get('emoji', '🎫')} {ticket.get('label', 'Ticket')}"
    body = [
        f"**Ticket #{ticket.get('number', '?')}** · opened by <@{ticket.get('user')}>",
        "",
        category.get("prompt", "Tell us what you need and someone will help."),
        "",
        ui.field(
            "Status",
            f"Claimed by <@{claimed_by}>" if claimed_by else "Waiting for staff",
        ),
        ui.field("Opened", f"<t:{ticket.get('created_at', 0)}:R>"),
    ]

    view = ui.BaseLayout(timeout=None)
    view.add_item(
        ui.container(
            ui.text(f"## {header}\n" + "\n".join(body)),
            ui.separator(large=True),
            ui.row(ClaimButton(cid, bool(claimed_by)), CloseButton(cid)),
            color=ui.GREEN_HEX if claimed_by else None,
        )
    )
    return view.validate()


class TicketControls(dui.View):
    """Fallback controls for tickets opened before the panel existed."""

    def __init__(self, cid: int, claimed: bool = False) -> None:
        super().__init__(timeout=None)
        self.add_item(ClaimButton(cid, claimed))
        self.add_item(CloseButton(cid))


class ConfirmClose(dui.View):
    def __init__(self, cid: int) -> None:
        super().__init__(timeout=120)
        self.cid = cid

    @dui.button(label="Close ticket", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: dui.Button) -> None:
        await interaction.response.edit_message(
            content="Saving transcript and closing…", view=None
        )
        channel = interaction.guild.get_channel(self.cid) if interaction.guild else None
        if isinstance(channel, discord.TextChannel):
            await close_ticket(interaction.client, channel, interaction.user)

    @dui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: dui.Button) -> None:
        await interaction.response.edit_message(content="Left it open.", view=None)


async def close_ticket(
    bot: discord.Client, channel: discord.TextChannel, closer: discord.abc.User
) -> None:
    ticket = await get_ticket(channel.id) or {}
    transcript = await build_transcript(channel)

    summary = (
        f"**Ticket #{ticket.get('number', '?')} closed**\n"
        f"Category: {ticket.get('label', 'unknown')}\n"
        f"Opened by: <@{ticket.get('user')}>\n"
        f"Closed by: {closer.mention}"
    )

    log_channel = await get_channel(bot, "ticket_transcripts")
    if log_channel is not None:
        try:
            if transcript is not None:
                await log_channel.send(summary, file=transcript)
            else:
                await log_channel.send(summary)
        except discord.HTTPException:
            log.exception("could not post transcript for #%s", channel)

    async with store.edit() as data:
        entry = (data.get("tickets") or {}).get(str(channel.id))
        if entry is not None:
            entry["status"] = "closed"
            entry["closed_by"] = closer.id
            entry["closed_at"] = int(time.time())

    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except discord.HTTPException:
        log.exception("could not delete #%s", channel)


# -- the panel ------------------------------------------------------------


class TicketPanel(dui.View):
    """A bare dropdown. No embed, no container -- just the select menu."""

    def __init__(self, group: str) -> None:
        super().__init__(timeout=None)
        self.group = group

        spec = panels().get(group, {})
        cats = categories()
        wanted = spec.get("categories") or list(cats)

        options = [
            discord.SelectOption(
                label=cats[key].get("label", key)[:100],
                value=key,
                description=(cats[key].get("description") or None),
            )
            for key in wanted
            if key in cats
        ][:25]

        select = dui.Select(
            custom_id=f"tk:open:{group}",
            placeholder=spec.get("placeholder", "Select an option")[:150],
            options=options or [discord.SelectOption(label="Nothing configured", value="_none")],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        key = interaction.data["values"][0]  # type: ignore[index]
        category = categories().get(key)
        if category is None:
            await interaction.response.send_message(
                "That option no longer exists.", ephemeral=True
            )
            return
        await create_ticket(interaction, key, category)


async def create_ticket(
    interaction: discord.Interaction, key: str, category: dict
) -> None:
    guild = interaction.guild
    if guild is None:
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    limit = int(config.get("tickets.max_open_per_user", 2) or 0)
    if limit and await open_count(interaction.user.id) >= limit:
        await interaction.followup.send(
            f"You already have {limit} open ticket(s). Close one first.", ephemeral=True
        )
        return

    parent = guild.get_channel(category.get("category_id") or 0)
    if not isinstance(parent, discord.CategoryChannel):
        await interaction.followup.send(
            f"**{category.get('label', key)}** has no valid category set. Tell an admin.",
            ephemeral=True,
        )
        return

    number = await store.next_id("_counter")

    ping_roles = [int(r) for r in (category.get("ping_roles") or []) if r]
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True,
            read_message_history=True,
        ),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            read_message_history=True,
        ),
    }
    for rid in ping_roles:
        role = guild.get_role(rid)
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True,
                read_message_history=True,
            )

    raw = config.get("tickets.name_format", "{type}-{user}").format(
        type=key, user=interaction.user.name, number=number
    )
    name = re.sub(r"[^a-z0-9\-]+", "-", raw.lower()).strip("-")[:95] or f"ticket-{number}"

    try:
        channel = await guild.create_text_channel(
            name=name,
            category=parent,
            overwrites=overwrites,
            topic=f"{category.get('label', key)} · {interaction.user} · #{number}",
            reason=f"Ticket #{number} for {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            f"I can't create channels in **{parent.name}**.", ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(f"Couldn't create the channel: {exc}", ephemeral=True)
        return

    ticket = {
        "number": number,
        "user": interaction.user.id,
        "category": key,
        "label": category.get("label", key),
        "claimed_by": None,
        "status": "open",
        "created_at": int(time.time()),
    }
    async with store.edit() as data:
        data.setdefault("tickets", {})[str(channel.id)] = ticket

    try:
        # Pings go in their own message: a V2 message can't carry text content,
        # and a mention only fires from real content.
        mentions = " ".join(f"<@&{r}>" for r in ping_roles)
        await channel.send(
            f"{interaction.user.mention} {mentions}".strip(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        message = await channel.send(view=opening_view(channel.id, ticket, category))
        await message.pin(reason="Ticket controls")
    except discord.HTTPException:
        log.exception("could not post opening message in #%s", channel)

    await interaction.followup.send(f"Opened {channel.mention}", ephemeral=True)


# -- cog ------------------------------------------------------------------


@app_commands.guild_only()
class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ClaimButton, CloseButton)
        for group in panels():
            self.bot.add_view(TicketPanel(group))

    ticket = app_commands.Group(name="ticket", description="Tickets")

    @ticket.command(name="panel", description="Post a ticket dropdown here")
    @app_commands.describe(which="Which panel: orders or support")
    @require("admin", "hr")
    async def panel(self, interaction: discord.Interaction, which: str = "orders") -> None:
        spec = panels().get(which)
        if spec is None:
            await interaction.response.send_message(
                f"No panel `{which}`. Options: {', '.join(panels())}", ephemeral=True
            )
            return

        await interaction.channel.send(
            spec.get("text", ""), view=TicketPanel(which)
        )
        await interaction.response.send_message(f"Posted the {which} dropdown.", ephemeral=True)

    @panel.autocomplete("which")
    async def panel_ac(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=g, value=g)
            for g in panels()
            if current.lower() in g.lower()
        ][:25]

    @ticket.command(name="add", description="Add a member to this ticket")
    @require(*STAFF_TIERS)
    async def add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if await get_ticket(interaction.channel.id) is None:
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True
        )
        await interaction.response.send_message(f"{member.mention} added.")

    @ticket.command(name="remove", description="Remove a member from this ticket")
    @require(*STAFF_TIERS)
    async def remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if await get_ticket(interaction.channel.id) is None:
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(f"{member.mention} removed.")

    @ticket.command(name="close", description="Close this ticket")
    async def close(self, interaction: discord.Interaction) -> None:
        ticket = await get_ticket(interaction.channel.id)
        if ticket is None:
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        if ticket.get("user") != interaction.user.id and not has_tier(
            interaction.user, *STAFF_TIERS
        ):
            await interaction.response.send_message(
                "You can't close this ticket.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Close this ticket? A transcript is saved first.",
            view=ConfirmClose(interaction.channel.id),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        async with store.edit() as data:
            entry = (data.get("tickets") or {}).get(str(channel.id))
            if entry is not None and entry.get("status") == "open":
                entry["status"] = "closed"
                entry["closed_at"] = int(time.time())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
