"""Blueprint Utilities -- entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core import ui
from core.config import config
from core.perms import MissingTier

ROOT = Path(__file__).resolve().parent
COGS_DIR = ROOT / "cogs"

# Discord names routinely contain emoji. On Windows a redirected stdout defaults
# to cp1252, which raises UnicodeEncodeError mid-log and can take the bot down --
# so force UTF-8 and degrade unencodable characters instead of raising.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - non-reconfigurable stream
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("blueprint")


class Blueprint(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True          # welcome, autorole, member lookups
        intents.message_content = True  # transcripts and giveaway hosting
        super().__init__(command_prefix="bp!", intents=intents, help_command=None)
        # on_ready fires again on every reconnect; syncing is once per process.
        self._synced = False

    async def setup_hook(self) -> None:
        # Commands sync from on_ready, once the guild list exists.
        await self.load_cogs()

    async def load_cogs(self) -> None:
        """Load every cog present.

        The bot is built in phases, so cogs/ may only hold some of them. A cog
        that fails to import is logged and skipped rather than aborting boot.
        """
        if not COGS_DIR.exists():
            return

        for path in sorted(COGS_DIR.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            ext = f"cogs.{path.stem}"
            try:
                await self.load_extension(ext)
                log.info("loaded %s", ext)
            except Exception:
                log.exception("failed to load %s", ext)

    async def sync_commands(self) -> None:
        """Sync commands to every guild the bot is in.

        Guild syncs appear instantly, where a global sync can take an hour to
        propagate. We also clear the global list: globally-registered commands
        show up in *every* guild, so any left over from an earlier run would
        haunt servers that were never meant to see them.

        This runs from on_ready rather than setup_hook because the guild list
        isn't populated until the bot has connected.
        """
        try:
            existing_global = await self.tree.fetch_commands()
            if existing_global:
                # Overwrite the *remote* global list with nothing, via HTTP
                # directly. tree.clear_commands(guild=None) would empty the
                # local tree as well, leaving copy_global_to nothing to copy.
                await self.http.bulk_upsert_global_commands(self.application_id, [])
                log.info("cleared %d stale global command(s)", len(existing_global))
        except discord.HTTPException:
            log.exception("could not clear global commands")

        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("synced %d commands to %s (%s)", len(synced), guild.name, guild.id)
            except discord.HTTPException:
                log.exception("command sync failed for %s", guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """A newly added server needs its own copy of the commands."""
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to new guild %s", len(synced), guild.name)
        except discord.HTTPException:
            log.exception("command sync failed for new guild %s", guild.id)

    async def on_ready(self) -> None:
        log.info("online as %s (%s)", self.user, self.user.id if self.user else "?")

        if not self._synced:
            self._synced = True
            await self.sync_commands()
            report_config()

        # Presence is owned by cogs/presence.py, which rotates it on a timer.


def report_config() -> None:
    """Print which config slots still need IDs."""
    missing = config.missing_keys()
    if not missing:
        log.info("config: all IDs set")
        return

    log.warning("config: %d value(s) still unset -- related features stay off:", len(missing))
    for key in missing:
        log.warning("   - %s", key)


async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, MissingTier):
        message = f"{ui.RED} {error}"
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"{ui.YELLOW} Slow down -- try again in {error.retry_after:.0f}s."
    elif isinstance(error, ui.LimitError):
        message = f"{ui.RED} That panel is too large to send: {error}"
    else:
        log.exception("command error", exc_info=error)
        message = f"{ui.RED} Something went wrong running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.")
        sys.exit(1)

    bot = Blueprint()
    bot.tree.on_error = on_tree_error

    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
