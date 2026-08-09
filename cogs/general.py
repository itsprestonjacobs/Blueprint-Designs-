"""Health and setup-inspection commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.config import config
from core.perms import require


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot is alive")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        view = ui.panel(
            "Pong",
            f"Gateway latency **{latency}ms**",
            footer=config.get("branding.footer", "Blueprint Utilities"),
        )
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="config", description="Show which config values still need IDs")
    @require("admin")
    async def config_check(self, interaction: discord.Interaction) -> None:
        missing = config.missing_keys()

        if not missing:
            body = "Everything's set."
        else:
            shown = missing[:25]
            lines = "\n".join(f"- `{key}`" for key in shown)
            body = (
                f"**{len(missing)}** value(s) still unset in `config.json`. "
                "Features depending on them stay disabled.\n\n" + lines
            )
            if len(missing) > len(shown):
                body += f"\n-# ...and {len(missing) - len(shown)} more."

        await interaction.response.send_message(
            view=ui.panel("Configuration", body), ephemeral=True
        )

    @app_commands.command(name="reload", description="Reload a cog without restarting")
    @app_commands.describe(cog="Cog name, e.g. tickets")
    @require("admin")
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        ext = f"cogs.{cog}"
        try:
            await self.bot.reload_extension(ext)
        except commands.ExtensionError as exc:
            await interaction.response.send_message(
                view=ui.err(f"Failed to reload `{ext}`:\n```\n{exc}\n```"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            view=ui.ok(f"Reloaded `{ext}`."), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
