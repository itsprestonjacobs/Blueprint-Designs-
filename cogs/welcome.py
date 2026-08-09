"""Welcome messages, autoroles, and button verification."""

from __future__ import annotations

import logging

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.config import config
from core.logs import get_channel
from core.perms import require

log = logging.getLogger("blueprint.welcome")

VERIFY_ID = "bp:verify"


class VerifyPanel(ui.BaseLayout):
    def __init__(self) -> None:
        super().__init__(timeout=None)

        button = dui.Button(
            label="Verify",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=VERIFY_ID,
        )
        button.callback = self._verify

        self.add_item(
            ui.container(
                ui.text(
                    "## Verification\n"
                    "Hit the button to unlock the rest of the server."
                ),
                ui.separator(),
                ui.row(button),
            )
        )
        self.validate()

    async def _verify(self, interaction: discord.Interaction) -> None:
        role_id = config.get("roles.verified")
        if not role_id:
            await interaction.response.send_message(
                view=ui.err("`roles.verified` isn't configured yet."), ephemeral=True
            )
            return

        role = interaction.guild.get_role(int(role_id)) if interaction.guild else None
        if role is None:
            await interaction.response.send_message(
                view=ui.err("That role's gone. Tell an admin."), ephemeral=True
            )
            return

        if role in interaction.user.roles:
            await interaction.response.send_message(
                view=ui.warn("Already verified."), ephemeral=True
            )
            return

        try:
            await interaction.user.add_roles(role, reason="Verified via button")
        except discord.Forbidden:
            await interaction.response.send_message(
                view=ui.err("I can't grant that role — my own role needs to sit above it."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            view=ui.ok("You're in."), ephemeral=True
        )


@app_commands.guild_only()
class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyPanel())

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._grant_autoroles(member)

        if not config.get("welcome.enabled", False):
            return

        channel = await get_channel(self.bot, "welcome")
        if channel is None:
            return

        template = config.get(
            "welcome.message", "Welcome to **{guild}**, {mention}!"
        )
        message = template.format(
            guild=member.guild.name,
            mention=member.mention,
            user=member.name,
            count=member.guild.member_count,
        )

        try:
            # The mention goes in its own message so the ping actually fires --
            # a V2 message can't carry text content.
            await channel.send(
                member.mention, allowed_mentions=discord.AllowedMentions(users=True)
            )
            await ui.send_panel(
                channel,
                ui.panel(
                    f"Welcome, {member.display_name}!",
                    message,
                    banner=config.get("welcome.banner_url"),
                ),
            )
        except discord.HTTPException:
            log.exception("failed to send welcome for %s", member)

    async def _grant_autoroles(self, member: discord.Member) -> None:
        role_ids = config.get("roles.autoroles", []) or []
        roles = [
            member.guild.get_role(int(rid)) for rid in role_ids if rid
        ]
        roles = [r for r in roles if r is not None]
        if not roles:
            return

        try:
            await member.add_roles(*roles, reason="Autorole on join")
        except discord.Forbidden:
            log.warning("could not grant autoroles in %s -- check my role position", member.guild)

    @app_commands.command(name="verifypanel", description="Post the verification panel here")
    @require("admin", "hr")
    async def verify_panel(self, interaction: discord.Interaction) -> None:
        await interaction.channel.send(view=VerifyPanel())
        await interaction.response.send_message(
            view=ui.ok("Posted."), ephemeral=True
        )


PREVIEW_VIEWS = [
    ("verify-panel", VerifyPanel),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
