"""Bulk channel-name cleanup.

Strips emoji and separator dots from channel names across a server. Always
previews first and waits for a confirmation, because a bulk rename touches
everything at once and Discord keeps no undo.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.names import clean_name
from core.perms import require

log = logging.getLogger("blueprint.channels")

# Channel types Discord normalises to lowercase-with-hyphens.
TEXTY = (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum)

# Discord allows 2 name edits per channel per 10 minutes. Each channel here is
# renamed once so they're separate buckets, but pacing avoids the global limit.
RENAME_DELAY = 0.6


def plan_for(guild: discord.Guild) -> list[tuple[discord.abc.GuildChannel, str]]:
    """Every channel whose name would change, with its cleaned name."""
    plan: list[tuple[discord.abc.GuildChannel, str]] = []

    for channel in guild.channels:
        is_text = channel.type in TEXTY
        new = clean_name(channel.name, text_channel=is_text)

        # An emoji-only name cleans to nothing; leave those alone rather than
        # inventing a name for them.
        if not new or new == channel.name:
            continue

        plan.append((channel, new))

    return plan


class ConfirmClean(ui.BaseLayout):
    def __init__(self, cog: "Channels", guild: discord.Guild, plan: list) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.plan = plan

        preview_lines = [
            f"`{old.name}`  →  `{new}`" for old, new in plan[:15]
        ]
        body = "\n".join(preview_lines)
        if len(plan) > 15:
            body += f"\n-# …and {len(plan) - 15} more."

        go = dui.Button(
            label=f"Rename {len(plan)}", style=discord.ButtonStyle.danger, emoji="✏️"
        )
        cancel = dui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        go.callback = self._go
        cancel.callback = self._cancel

        self.add_item(
            ui.container(
                ui.text(
                    f"## Clean channel names\n"
                    f"**{len(plan)}** channel(s) would be renamed in **{guild.name}**.\n\n"
                    + body
                ),
                ui.separator(large=True),
                ui.text("-# Discord keeps no undo for renames. Check the list first."),
                ui.row(go, cancel),
            )
        )
        self.validate()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ui.ok("Cancelled. Nothing renamed."),
            content=None,
            embeds=[],
            attachments=[],
        )

    async def _go(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ui.warn(f"Renaming {len(self.plan)} channel(s)… this can take a minute."),
            content=None,
            embeds=[],
            attachments=[],
        )
        done, failed = await self.cog.apply(self.plan, interaction.user)

        body = [
            ui.field("Renamed", done),
            ui.field("Failed", len(failed)),
        ]
        if failed:
            body.append("")
            body.extend(f"`{name}` — {why}" for name, why in failed[:8])

        await interaction.followup.send(
            view=ui.panel(
                "Channel Cleanup Done",
                "\n".join(body),
                color=ui.GREEN_HEX if not failed else ui.AMBER_HEX,
            ),
            ephemeral=True,
        )


@app_commands.guild_only()
class Channels(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def apply(
        self, plan: list, actor: discord.abc.User
    ) -> tuple[int, list[tuple[str, str]]]:
        done = 0
        failed: list[tuple[str, str]] = []

        for channel, new in plan:
            try:
                await channel.edit(name=new, reason=f"Channel cleanup by {actor}")
                done += 1
            except discord.Forbidden:
                failed.append((channel.name, "no permission"))
            except discord.HTTPException as exc:
                failed.append((channel.name, f"HTTP {exc.status}"))
            await asyncio.sleep(RENAME_DELAY)

        log.info("channel cleanup by %s: %d renamed, %d failed", actor, done, len(failed))
        return done, failed

    @app_commands.command(
        name="cleanchannels", description="Strip emoji and dots from channel names"
    )
    @app_commands.describe(
        preview_only="Just show what would change, don't offer to apply"
    )
    @require("admin")
    async def cleanchannels(
        self, interaction: discord.Interaction, preview_only: bool = False
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        plan = plan_for(guild)

        if not plan:
            await interaction.followup.send(
                view=ui.ok("Every channel name is already clean."), ephemeral=True
            )
            return

        if preview_only:
            lines = [f"`{c.name}`  →  `{n}`" for c, n in plan[:25]]
            body = f"**{len(plan)}** channel(s) would change.\n\n" + "\n".join(lines)
            if len(plan) > 25:
                body += f"\n-# …and {len(plan) - 25} more."
            await interaction.followup.send(
                view=ui.panel("Preview", body), ephemeral=True
            )
            return

        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            await interaction.followup.send(
                view=ui.err("I need **Manage Channels** to rename anything."),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            view=ConfirmClean(self, guild, plan), ephemeral=True
        )


PREVIEW_VIEWS = []


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Channels(bot))
