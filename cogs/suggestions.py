"""Suggestion board with voting, plus partnership requests."""

from __future__ import annotations

import re
import time

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.logs import get_channel
from core.perms import has_tier, require
from core.store import suggestions as store


class VoteButton(
    dui.DynamicItem[dui.Button], template=r"bp:sugg:(?P<dir>up|down):(?P<sid>\d+)"
):
    def __init__(self, direction: str, sid: int, count: int = 0) -> None:
        up = direction == "up"
        super().__init__(
            dui.Button(
                label=str(count),
                style=discord.ButtonStyle.success if up else discord.ButtonStyle.danger,
                emoji="👍" if up else "👎",
                custom_id=f"bp:sugg:{direction}:{sid}",
            )
        )
        self.direction = direction
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["dir"], int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        async with store.edit() as data:
            entry = (data.get("suggestions") or {}).get(str(self.sid))
            if entry is None:
                await interaction.response.send_message(
                    view=ui.err("That suggestion is gone."), ephemeral=True
                )
                return
            if entry.get("status") != "open":
                await interaction.response.send_message(
                    view=ui.warn("Voting's closed."),
                    ephemeral=True,
                )
                return

            up: list[int] = entry.setdefault("up", [])
            down: list[int] = entry.setdefault("down", [])
            uid = interaction.user.id

            # A vote is exclusive: voting one way clears the other.
            target, other = (up, down) if self.direction == "up" else (down, up)
            if uid in target:
                target.remove(uid)
            else:
                target.append(uid)
                if uid in other:
                    other.remove(uid)

            snapshot = dict(entry)

        await interaction.response.edit_message(
            view=SuggestionView(self.sid, snapshot),
            content=None,
            embeds=[],
            attachments=[],
        )


class SuggestionView(ui.BaseLayout):
    def __init__(self, sid: int, entry: dict) -> None:
        super().__init__(timeout=None)

        up = len(entry.get("up", []))
        down = len(entry.get("down", []))
        status = entry.get("status", "open")

        body = [
            ui.field("Suggested by", f"<@{entry.get('user')}>"),
            "",
            entry.get("text", ""),
        ]
        if status != "open":
            body.append("")
            body.append(
                ui.field(
                    "Status",
                    f"{ui.GREEN if status == 'approved' else ui.RED} **{status.title()}**",
                )
            )
            if entry.get("response"):
                body.append(ui.field("Staff response", entry["response"]))

        children: list[dui.Item] = [
            ui.text(f"## Suggestion #{sid}\n" + "\n".join(body)),
            ui.separator(),
        ]
        if status == "open":
            children.append(ui.row(VoteButton("up", sid, up), VoteButton("down", sid, down)))
        else:
            children.append(ui.text(f"-# Final vote: 👍 {up} · 👎 {down}"))

        color = None
        if status == "approved":
            color = 0x2ECC71
        elif status == "denied":
            color = 0xE74C3C

        self.add_item(ui.container(*children, color=color))
        self.validate()


class PartnershipModal(dui.Modal, title="Partnership Request"):
    server_name = dui.TextInput(label="Server or brand name", max_length=100)
    member_count = dui.TextInput(label="Member count", max_length=20)
    invite = dui.TextInput(label="Invite link", max_length=200)
    offer = dui.TextInput(
        label="What are you offering?",
        style=discord.TextStyle.paragraph,
        max_length=800,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        body = "\n".join(
            [
                ui.field("Requested by", f"{interaction.user.mention} (`{interaction.user.id}`)"),
                ui.field("Server", self.server_name.value),
                ui.field("Members", self.member_count.value),
                ui.field("Invite", self.invite.value),
                "",
                f"**What they're offering**\n{self.offer.value}",
            ]
        )

        channel = await get_channel(interaction.client, "partnerships")
        if channel is None:
            await interaction.followup.send(
                view=ui.warn("`channels.partnerships` isn't configured, so this wasn't sent."
                ),
                ephemeral=True,
            )
            return

        await channel.send(view=ui.panel("Partnership Request", body))
        await interaction.followup.send(
            view=ui.ok("Sent. We'll take a look."),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await ui.report_error(interaction, error)


@app_commands.guild_only()
class Suggestions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(VoteButton)

    @app_commands.command(name="suggest", description="Suggest an improvement")
    @app_commands.describe(suggestion="What would you change?")
    async def suggest(self, interaction: discord.Interaction, suggestion: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = await get_channel(self.bot, "suggestions")
        if channel is None:
            await interaction.followup.send(
                view=ui.warn("`channels.suggestions` isn't configured yet."),
                ephemeral=True,
            )
            return

        sid = await store.next_id("_counter")
        entry = {
            "id": sid,
            "user": interaction.user.id,
            "text": suggestion,
            "up": [],
            "down": [],
            "status": "open",
            "at": int(time.time()),
        }

        message = await channel.send(view=SuggestionView(sid, entry))
        entry["channel"] = channel.id
        entry["message"] = message.id

        async with store.edit() as data:
            data.setdefault("suggestions", {})[str(sid)] = entry

        await interaction.followup.send(
            view=ui.ok(f"Suggestion **#{sid}** posted in {channel.mention}."),
            ephemeral=True,
        )

    @app_commands.command(name="suggestion", description="Approve or deny a suggestion")
    @app_commands.describe(
        suggestion_id="The suggestion number",
        decision="Your verdict",
        response="Explain the decision",
    )
    @app_commands.choices(
        decision=[
            app_commands.Choice(name="Approve", value="approved"),
            app_commands.Choice(name="Deny", value="denied"),
            app_commands.Choice(name="Implemented", value="implemented"),
        ]
    )
    @require("admin", "hr")
    async def decide(
        self,
        interaction: discord.Interaction,
        suggestion_id: int,
        decision: app_commands.Choice[str],
        response: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with store.edit() as data:
            entry = (data.get("suggestions") or {}).get(str(suggestion_id))
            if entry is None:
                await interaction.followup.send(
                    view=ui.err(f"No suggestion **#{suggestion_id}**."), ephemeral=True
                )
                return
            entry["status"] = decision.value
            entry["response"] = response
            entry["decided_by"] = interaction.user.id
            snapshot = dict(entry)

        channel = self.bot.get_channel(snapshot.get("channel", 0))
        if channel is not None and snapshot.get("message"):
            try:
                message = await channel.fetch_message(snapshot["message"])
                await message.edit(
                    view=SuggestionView(suggestion_id, snapshot),
                    content=None,
                    embeds=[],
                    attachments=[],
                )
            except (discord.NotFound, discord.HTTPException):
                pass

        await interaction.followup.send(
            view=ui.ok(f"Suggestion **#{suggestion_id}** marked **{decision.value}**."),
            ephemeral=True,
        )

    @app_commands.command(name="partnership", description="Request a partnership with us")
    async def partnership(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PartnershipModal())


PREVIEW_VIEWS = [
    (
        "suggestion-open",
        lambda: SuggestionView(
            1, {"user": 1, "text": "Add a portfolio channel.", "up": [1, 2], "down": [], "status": "open"}
        ),
    ),
    (
        "suggestion-decided",
        lambda: SuggestionView(
            1,
            {
                "user": 1,
                "text": "Add a portfolio channel.",
                "up": [1, 2],
                "down": [3],
                "status": "approved",
                "response": "Good idea — added.",
            },
        ),
    ),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestions(bot))
