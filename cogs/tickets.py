"""Ticket and order intake.

Flow: panel dropdown -> intake modal -> channel created in the category the
config maps that choice to -> staff pinged -> claim/close controls.

All buttons here are `DynamicItem`s whose custom_id carries the ticket's channel
ID, so they keep working after a restart without registering one persistent view
per open ticket.
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
from core.store import tickets as store

log = logging.getLogger("blueprint.tickets")

PANEL_SELECT_ID = "bp:ticket:open"
PRICING_BUTTON_ID = "bp:ticket:pricing"
MAX_MODAL_QUESTIONS = 5  # Discord's hard limit on modal inputs


# -- transcript backend ---------------------------------------------------

try:  # optional dependency; transcripts degrade to plain text without it
    import chat_exporter
except ImportError:  # pragma: no cover
    chat_exporter = None


async def build_transcript(channel: discord.TextChannel) -> discord.File | None:
    """Render an HTML transcript, falling back to plain text."""
    if chat_exporter is not None:
        try:
            html = await chat_exporter.export(channel, military_time=True)
            if html:
                return discord.File(
                    io.BytesIO(html.encode("utf-8")),
                    filename=f"transcript-{channel.name}.html",
                )
        except Exception:  # noqa: BLE001 - never let logging break closing
            log.exception("chat_exporter failed for #%s, falling back to text", channel)

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
        log.exception("could not read history for #%s", channel)
        return None


# -- persistence helpers --------------------------------------------------


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


# -- ticket controls ------------------------------------------------------


class ClaimButton(dui.DynamicItem[dui.Button], template=r"bp:ticket:claim:(?P<cid>\d+)"):
    def __init__(self, cid: int, claimed_by: int | None = None) -> None:
        label = "Claimed" if claimed_by else "Claim"
        super().__init__(
            dui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                emoji="🙋",
                custom_id=f"bp:ticket:claim:{cid}",
                disabled=bool(claimed_by),
            )
        )
        self.cid = cid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not has_tier(interaction.user, "support", "designer", "hr"):
            await interaction.response.send_message(
                view=ui.err("Staff only."), ephemeral=True
            )
            return

        async with store.edit() as data:
            ticket = (data.get("tickets") or {}).get(str(self.cid))
            if ticket is None:
                await interaction.response.send_message(
                    view=ui.err("This isn't a ticket I know about."),
                    ephemeral=True,
                )
                return
            if ticket.get("claimed_by"):
                holder = ticket["claimed_by"]
                await interaction.response.send_message(
                    view=ui.warn(f"Already claimed by <@{holder}>."),
                    ephemeral=True,
                )
                return
            ticket["claimed_by"] = interaction.user.id

        await interaction.response.send_message(
            view=ui.ok(f"{interaction.user.mention} claimed this ticket.")
        )

        # Grey out the claim button on the original control panel.
        try:
            message = interaction.message
            if message is not None:
                view = TicketControls(self.cid, claimed_by=interaction.user.id)
                await message.edit(view=view)
        except discord.HTTPException:
            pass


class CloseButton(dui.DynamicItem[dui.Button], template=r"bp:ticket:close:(?P<cid>\d+)"):
    def __init__(self, cid: int) -> None:
        super().__init__(
            dui.Button(
                label="Close",
                style=discord.ButtonStyle.danger,
                emoji="🔒",
                custom_id=f"bp:ticket:close:{cid}",
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
                view=ui.err("This isn't a ticket I know about."), ephemeral=True
            )
            return

        is_owner = ticket.get("user") == interaction.user.id
        if not (is_owner or has_tier(interaction.user, "support", "designer", "hr")):
            await interaction.response.send_message(
                view=ui.err("Not your ticket to close."), ephemeral=True
            )
            return

        await interaction.response.send_message(
            view=ConfirmClose(self.cid), ephemeral=True
        )


class ConfirmClose(ui.BaseLayout):
    """Ephemeral yes/no guard so a stray click can't delete a ticket."""

    def __init__(self, cid: int) -> None:
        super().__init__(timeout=120)
        self.cid = cid

        confirm = dui.Button(label="Close ticket", style=discord.ButtonStyle.danger, emoji="🔒")
        cancel = dui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback = self._confirm
        cancel.callback = self._cancel

        self.add_item(
            ui.container(
                ui.text(
                    "**Close this ticket?**\n"
                    "We'll save a transcript first, then the channel goes."
                ),
                ui.row(confirm, cancel),
            )
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ui.notice("Left it open."),
            content=None,
            embeds=[],
            attachments=[],
        )

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ui.notice("Saving transcript, closing up."),
            content=None,
            embeds=[],
            attachments=[],
        )
        channel = interaction.guild.get_channel(self.cid) if interaction.guild else None
        if isinstance(channel, discord.TextChannel):
            await close_ticket(interaction.client, channel, interaction.user)


async def close_ticket(
    bot: discord.Client, channel: discord.TextChannel, closer: discord.abc.User
) -> None:
    """Archive and delete a ticket channel."""
    ticket = await get_ticket(channel.id) or {}
    transcript = await build_transcript(channel)

    number = ticket.get("number", "?")
    opener_id = ticket.get("user")
    label = ticket.get("label", "Ticket")

    summary = ui.panel(
        f"Ticket #{number} closed",
        "\n".join(
            [
                ui.field("Category", label),
                ui.field("Opened by", f"<@{opener_id}>" if opener_id else "unknown"),
                ui.field("Closed by", closer.mention),
                ui.field("Claimed by", f"<@{ticket['claimed_by']}>" if ticket.get("claimed_by") else "unclaimed"),
                ui.field("Channel", f"#{channel.name}"),
            ]
        ),
    )

    log_channel = await get_channel(bot, "ticket_transcripts")
    if log_channel is not None:
        try:
            await log_channel.send(view=summary)
            if transcript is not None:
                await log_channel.send(file=transcript)
        except discord.HTTPException:
            log.exception("failed to post transcript for #%s", channel)

    if config.get("tickets.transcript_dm", True) and opener_id:
        try:
            user = bot.get_user(opener_id) or await bot.fetch_user(opener_id)
            await user.send(view=summary)
            if transcript is not None:
                transcript.reset()
                await user.send(file=transcript)
        except (discord.HTTPException, discord.Forbidden):
            pass  # DMs closed; not an error worth surfacing

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


class TicketControls(ui.BaseLayout):
    """The claim/close panel pinned at the top of every ticket."""

    def __init__(self, cid: int, claimed_by: int | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(
            ui.container(
                ui.text("### Ticket controls\nStaff can claim this ticket; either side can close it."),
                ui.separator(),
                ui.row(ClaimButton(cid, claimed_by), CloseButton(cid)),
            )
        )


# -- intake ---------------------------------------------------------------


class IntakeModal(dui.Modal):
    """Per-category question form. Modals stay classic -- V2 has no modal layout."""

    def __init__(self, key: str, category: dict) -> None:
        super().__init__(title=f"{category.get('label', 'Ticket')}"[:45])
        self.key = key
        self.category = category
        self.fields: list[tuple[str, dui.TextInput]] = []

        for question in (category.get("questions") or [])[:MAX_MODAL_QUESTIONS]:
            box = dui.TextInput(
                label=question[:45],
                placeholder=question[:100] if len(question) > 45 else None,
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=900,
            )
            self.add_item(box)
            self.fields.append((question, box))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers = [(q, box.value) for q, box in self.fields]
        await create_ticket(interaction, self.key, self.category, answers)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await ui.report_error(interaction, error)


async def create_ticket(
    interaction: discord.Interaction,
    key: str,
    category: dict,
    answers: list[tuple[str, str]],
) -> None:
    guild = interaction.guild
    if guild is None:
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)

    limit = int(config.get("tickets.max_open_per_user", 2) or 0)
    if limit and await open_count(interaction.user.id) >= limit:
        await interaction.followup.send(
            view=ui.warn(f"You already have **{limit}** open ticket(s). "
                "Close one first."
            ),
            ephemeral=True,
        )
        return

    parent = guild.get_channel(category["category_id"]) if category.get("category_id") else None
    if not isinstance(parent, discord.CategoryChannel):
        await interaction.followup.send(
            view=ui.err(f"**{category.get('label', key)}** has no valid category configured "
                f"(`tickets.categories.{key}.category_id`). Ask an admin to set it."
            ),
            ephemeral=True,
        )
        return

    number = await store.next_id("_counter")

    ping_role_ids = [int(r) for r in (category.get("ping_roles") or []) if r]
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
        ),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True, read_message_history=True
        ),
    }
    for rid in ping_role_ids:
        role = guild.get_role(rid)
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True
            )

    name_format = config.get("tickets.name_format", "{type}-{user}")
    raw_name = name_format.format(
        type=key, user=interaction.user.name, number=number, label=category.get("label", key)
    )
    channel_name = re.sub(r"[^a-z0-9\-]+", "-", raw_name.lower()).strip("-")[:95] or f"ticket-{number}"

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=parent,
            overwrites=overwrites,
            topic=f"{category.get('label', key)} · opened by {interaction.user} · #{number}",
            reason=f"Ticket #{number} for {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            view=ui.err(f"I lack permission to create channels in **{parent.name}**."),
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            view=ui.err(f"Could not create the channel: {exc}"), ephemeral=True
        )
        return

    async with store.edit() as data:
        data.setdefault("tickets", {})[str(channel.id)] = {
            "number": number,
            "user": interaction.user.id,
            "category": key,
            "label": category.get("label", key),
            "claimed_by": None,
            "status": "open",
            "created_at": int(time.time()),
            "answers": [{"q": q, "a": a} for q, a in answers],
        }

    # Pings go in their own plain message: a V2 message can't carry content.
    mentions = " ".join(f"<@&{rid}>" for rid in ping_role_ids)
    if mentions:
        try:
            await channel.send(
                f"{interaction.user.mention} {mentions}",
                allowed_mentions=discord.AllowedMentions(roles=True, users=True),
            )
        except discord.HTTPException:
            pass

    body = [
        ui.field("Ticket", f"#{number}"),
        ui.field("Category", category.get("label", key)),
        ui.field("Opened by", interaction.user.mention),
    ]
    if answers:
        body.append("")
        for question, answer in answers:
            body.append(f"**{question}**\n{answer}")

    opening = ui.panel(
        f"{category.get('emoji', '🎫')} {category.get('label', key)}",
        "\n".join(body),
        banner=category.get("banner"),
        footer="Someone will pick this up shortly.",
    )

    try:
        await ui.send_panel(channel, opening)
        await channel.send(view=TicketControls(channel.id))
    except discord.HTTPException:
        log.exception("failed to post opening messages in #%s", channel)

    await interaction.followup.send(
        view=ui.ok(f"Opened {channel.mention}"), ephemeral=True
    )


def panel_groups() -> dict[str, dict]:
    """Panel definitions, falling back to one panel with every category."""
    groups = config.get("tickets.panels")
    if groups:
        return groups
    return {
        "orders": {
            "title": "Order Here",
            "placeholder": "Select an order type",
            "show_status": True,
            "show_pricing": True,
            "categories": list(config.ticket_categories()),
        }
    }


class TicketPanel(ui.BaseLayout):
    """A public panel users pick a category from. Persistent across restarts.

    Each panel shows only the categories listed for it in `tickets.panels`, so
    orders and support can live in separate channels with their own copy.
    """

    def __init__(self, group: str = "orders") -> None:
        super().__init__(timeout=None)

        self.group = group
        spec = panel_groups().get(group, {})

        categories = config.ticket_categories()
        wanted = spec.get("categories") or list(categories)
        options = ui.check_options(
            [
                discord.SelectOption(
                    label=categories[key].get("label", key)[:100],
                    value=key,
                    description=(categories[key].get("description") or None),
                    emoji=categories[key].get("emoji") or None,
                )
                for key in wanted
                if key in categories
            ],
            f"tickets.panels.{group}",
        )

        select = dui.Select(
            custom_id=f"{PANEL_SELECT_ID}:{group}",
            placeholder=spec.get("placeholder", "Select an option")[:150],
            options=options or [discord.SelectOption(label="Nothing configured", value="_none")],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select

        children: list[dui.Item] = []

        banner_url, banner_file = ui.resolve_media(
            spec.get("banner", config.get("tickets.panel_banner"))
        )
        if banner_url:
            media = ui.gallery(banner_url)
            if media:
                children.append(media)
                self.track(banner_file)

        children.append(
            ui.text(f"## {spec.get('title', 'Tickets')}\n{spec.get('body', '')}")
        )

        if spec.get("show_status"):
            children.append(ui.separator())
            children.append(
                ui.text(
                    "**Order Status**\n"
                    + ui.status_block(config.get("order_status", {}) or {})
                )
            )

        # Pricing opens the pricing panel inline, so it works with no config.
        # The link buttons only appear once their URL is set.
        buttons: list[dui.Item] = []
        if spec.get("show_pricing"):
            pricing = dui.Button(
                label="Pricing",
                style=discord.ButtonStyle.success,
                emoji="💸",
                custom_id=f"{PRICING_BUTTON_ID}:{group}",
            )
            pricing.callback = self._show_pricing
            buttons.append(pricing)

        for label, key in (
            ("Ordering ToS", "links.ordering_tos"),
            ("Roblox Group", "links.roblox_group"),
        ):
            url = config.get(key)
            if url:
                buttons.append(
                    dui.Button(label=label, url=url, style=discord.ButtonStyle.link)
                )

        children.append(ui.separator())
        if buttons:
            children.append(ui.row(*buttons[:5]))
        children.append(ui.row(select))

        self.add_item(ui.container(*children))
        self.validate()

    async def _show_pricing(self, interaction: discord.Interaction) -> None:
        """Show panels/pricing.json privately, so no hosted page is needed."""
        from cogs.panels import InfoPanel, load_panels  # local: avoids import cycle

        spec = load_panels().get("pricing")
        if spec is None:
            await interaction.response.send_message(
                view=ui.warn("No pricing panel set up yet."), ephemeral=True
            )
            return

        view = InfoPanel("pricing", spec)
        await interaction.response.send_message(
            view=view, files=view.files(), ephemeral=True
        )

    async def _on_select(self, interaction: discord.Interaction) -> None:
        key = interaction.data["values"][0]  # type: ignore[index]
        category = config.ticket_category(key)
        if category is None:
            await interaction.response.send_message(
                view=ui.err("That category no longer exists."), ephemeral=True
            )
            return

        questions = (category.get("questions") or [])[:MAX_MODAL_QUESTIONS]
        if questions:
            await interaction.response.send_modal(IntakeModal(key, category))
        else:
            await create_ticket(interaction, key, category, [])


# -- cog ------------------------------------------------------------------


@app_commands.guild_only()
class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Registered once so panel dropdowns and ticket buttons keep responding
        # after a restart.
        self.bot.add_dynamic_items(ClaimButton, CloseButton)
        # One persistent view per panel, so each keeps working after a restart.
        for group in panel_groups():
            self.bot.add_view(TicketPanel(group))

    ticket = app_commands.Group(name="ticket", description="Manage tickets")

    @ticket.command(name="panel", description="Post a ticket panel here")
    @app_commands.describe(which="Which panel to post")
    @require("admin", "hr")
    async def panel(self, interaction: discord.Interaction, which: str = "orders") -> None:
        groups = panel_groups()
        if which not in groups:
            await interaction.response.send_message(
                view=ui.err(
                    f"No panel `{which}`. Options: "
                    + ", ".join(f"`{g}`" for g in groups)
                ),
                ephemeral=True,
            )
            return

        await ui.send_panel(interaction.channel, TicketPanel(which))
        await interaction.response.send_message(
            view=ui.ok(f"Posted the **{which}** panel."), ephemeral=True
        )

    @panel.autocomplete("which")
    async def panel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=g, value=g)
            for g in panel_groups()
            if current in g.lower()
        ][:25]

    @ticket.command(name="add", description="Add a member to this ticket")
    @require("support", "designer", "hr")
    async def add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if await get_ticket(interaction.channel.id) is None:
            await interaction.response.send_message(
                view=ui.err("This isn't a ticket channel."), ephemeral=True
            )
            return

        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True
        )
        await interaction.response.send_message(
            view=ui.ok(f"{member.mention} added.")
        )

    @ticket.command(name="remove", description="Remove a member from this ticket")
    @require("support", "designer", "hr")
    async def remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if await get_ticket(interaction.channel.id) is None:
            await interaction.response.send_message(
                view=ui.err("This isn't a ticket channel."), ephemeral=True
            )
            return

        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(
            view=ui.ok(f"{member.mention} removed.")
        )

    @ticket.command(name="rename", description="Rename this ticket channel")
    @require("support", "designer", "hr")
    async def rename(self, interaction: discord.Interaction, name: str) -> None:
        if await get_ticket(interaction.channel.id) is None:
            await interaction.response.send_message(
                view=ui.err("This isn't a ticket channel."), ephemeral=True
            )
            return

        clean = re.sub(r"[^a-z0-9\-]+", "-", name.lower()).strip("-")[:95]
        await interaction.channel.edit(name=clean or "ticket")
        await interaction.response.send_message(
            view=ui.ok(f"Renamed to `{clean}`."), ephemeral=True
        )

    @ticket.command(name="close", description="Close this ticket")
    async def close(self, interaction: discord.Interaction) -> None:
        ticket = await get_ticket(interaction.channel.id)
        if ticket is None:
            await interaction.response.send_message(
                view=ui.err("This isn't a ticket channel."), ephemeral=True
            )
            return

        is_owner = ticket.get("user") == interaction.user.id
        if not (is_owner or has_tier(interaction.user, "support", "designer", "hr")):
            await interaction.response.send_message(
                view=ui.err("Not your ticket to close."), ephemeral=True
            )
            return

        await interaction.response.send_message(
            view=ConfirmClose(interaction.channel.id), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Keep the store honest when a ticket channel is deleted manually."""
        async with store.edit() as data:
            entry = (data.get("tickets") or {}).get(str(channel.id))
            if entry is not None and entry.get("status") == "open":
                entry["status"] = "closed"
                entry["closed_at"] = int(time.time())


# Consumed by scripts/preview.py for offline layout validation.
PREVIEW_VIEWS = [
    ("panel", TicketPanel),
    ("controls", lambda: TicketControls(1)),
    ("controls-claimed", lambda: TicketControls(1, claimed_by=2)),
    ("confirm-close", lambda: ConfirmClose(1)),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
