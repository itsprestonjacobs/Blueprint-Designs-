"""Rotating bot presence.

Cycles the activity shown under the bot's name -- Playing, Watching, Listening,
Streaming, Competing, or a plain custom status -- on a timer, pulling live
numbers from the stores so the text isn't static.

Everything is driven by `presence` in config.json, so adding a line to the
rotation is a config edit.
"""

from __future__ import annotations

import itertools
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import ui
from core.config import config
from core.perms import require
from core.store import orders as order_store
from core.store import tickets as ticket_store

log = logging.getLogger("blueprint.presence")

ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}

STATUS_MODES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}

DEFAULT_ROTATION = [
    {"type": "watching", "text": "{members} members"},
    {"type": "playing", "text": "with blueprints"},
    {"type": "listening", "text": "/ticket panel"},
    {"type": "watching", "text": "{open_tickets} open tickets"},
    {"type": "competing", "text": "the design queue"},
]


def rotation() -> list[dict]:
    entries = config.get("presence.statuses")
    return entries if entries else DEFAULT_ROTATION


def build_activity(entry: dict, text: str) -> discord.BaseActivity:
    """Turn one rotation entry into a Discord activity."""
    kind = str(entry.get("type", "playing")).lower()

    if kind == "streaming":
        # Discord only renders the purple "streaming" style for a real
        # Twitch/YouTube URL; without one it silently falls back to Playing.
        url = entry.get("url") or config.get("presence.stream_url")
        if url:
            return discord.Streaming(name=text, url=url)
        log.warning("streaming status %r has no url, showing it as Playing", text)
        return discord.Activity(type=discord.ActivityType.playing, name=text)

    if kind == "custom":
        return discord.CustomActivity(name=text)

    return discord.Activity(
        type=ACTIVITY_TYPES.get(kind, discord.ActivityType.playing), name=text
    )


class Presence(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cycle = itertools.cycle(range(max(len(rotation()), 1)))
        self._pinned: discord.BaseActivity | None = None

    async def cog_load(self) -> None:
        self.rotator.change_interval(seconds=self.interval)
        self.rotator.start()

    async def cog_unload(self) -> None:
        self.rotator.cancel()

    @property
    def interval(self) -> int:
        # Discord rate-limits presence updates; below ~15s it starts dropping them.
        return max(int(config.get("presence.interval", 60) or 60), 15)

    async def tokens(self) -> dict[str, object]:
        """Live values available as {placeholders} in status text."""
        guild = self.bot.get_guild(config.guild_id) if config.guild_id else None

        tickets = await ticket_store.read()
        open_tickets = sum(
            1
            for t in (tickets.get("tickets") or {}).values()
            if t.get("status") == "open"
        )

        orders = await order_store.read()
        order_rows = (orders.get("orders") or {}).values()

        return {
            "members": guild.member_count if guild else 0,
            "guild": guild.name if guild else "Blueprint",
            "open_tickets": open_tickets,
            "orders": len(order_rows),
            "robux": sum(o.get("price", 0) for o in order_rows),
            "designers": len(config.role_ids("designer")),
        }

    async def apply(self, entry: dict) -> str:
        values = await self.tokens()
        raw = str(entry.get("text", ""))
        try:
            text = raw.format(**values)
        except (KeyError, IndexError, ValueError):
            # A bad placeholder shouldn't wedge the rotation.
            log.warning("status %r has an unknown placeholder", raw)
            text = raw

        status = STATUS_MODES.get(
            str(entry.get("status", config.get("presence.status", "online"))).lower(),
            discord.Status.online,
        )
        await self.bot.change_presence(
            activity=build_activity(entry, text), status=status
        )
        return text

    @tasks.loop(seconds=60)
    async def rotator(self) -> None:
        if self._pinned is not None or not config.get("presence.enabled", True):
            return

        entries = rotation()
        if not entries:
            return

        index = next(self._cycle) % len(entries)
        try:
            await self.apply(entries[index])
        except discord.HTTPException:
            log.warning("could not update presence", exc_info=True)

    @rotator.before_loop
    async def before_rotator(self) -> None:
        await self.bot.wait_until_ready()

    # -- commands ---------------------------------------------------------

    presence = app_commands.Group(name="presence", description="Bot status rotation")

    @presence.command(name="set", description="Pin the bot to one status")
    @app_commands.describe(
        type="Activity type", text="What it says", url="Twitch/YouTube URL, streaming only"
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name=n.title(), value=n)
            for n in ("playing", "watching", "listening", "streaming", "competing", "custom")
        ]
    )
    @require("admin")
    async def set_presence(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        text: str,
        url: str | None = None,
    ) -> None:
        entry = {"type": type.value, "text": text}
        if url:
            entry["url"] = url

        rendered = await self.apply(entry)
        self._pinned = True  # stop the rotation until cleared

        note = ""
        if type.value == "streaming" and not (url or config.get("presence.stream_url")):
            note = "\n-# No URL given, so Discord will show this as Playing."

        await interaction.response.send_message(
            view=ui.ok(f"Status pinned to **{type.name} {rendered}**.{note}"),
            ephemeral=True,
        )

    @presence.command(name="resume", description="Resume the rotating status")
    @require("admin")
    async def resume(self, interaction: discord.Interaction) -> None:
        self._pinned = None
        config.set("presence.enabled", True)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"Rotation back on, every **{self.interval}s**."), ephemeral=True
        )

    @presence.command(name="pause", description="Stop the rotation where it is")
    @require("admin")
    async def pause(self, interaction: discord.Interaction) -> None:
        config.set("presence.enabled", False)
        config.save()
        await interaction.response.send_message(view=ui.ok("Rotation paused."), ephemeral=True)

    @presence.command(name="next", description="Jump to the next status now")
    @require("admin", "hr")
    async def next_status(self, interaction: discord.Interaction) -> None:
        entries = rotation()
        if not entries:
            await interaction.response.send_message(
                view=ui.warn("Nothing in the rotation."), ephemeral=True
            )
            return

        self._pinned = None
        entry = entries[next(self._cycle) % len(entries)]
        rendered = await self.apply(entry)
        await interaction.response.send_message(
            view=ui.ok(f"Now **{entry.get('type', 'playing')} {rendered}**."), ephemeral=True
        )

    @presence.command(name="list", description="Show the status rotation")
    @require("admin", "hr")
    async def list_statuses(self, interaction: discord.Interaction) -> None:
        entries = rotation()
        values = await self.tokens()

        lines = []
        for i, entry in enumerate(entries, 1):
            raw = str(entry.get("text", ""))
            try:
                rendered = raw.format(**values)
            except (KeyError, IndexError, ValueError):
                rendered = f"{raw}  (bad placeholder)"
            lines.append(f"`{i}.` **{entry.get('type', 'playing')}** — {rendered}")

        body = "\n".join(lines) or "Nothing configured."
        body += (
            f"\n\nRotating every **{self.interval}s** · "
            f"{'on' if config.get('presence.enabled', True) else 'paused'}"
            f"\n-# Placeholders: {', '.join('{%s}' % k for k in values)}"
        )

        await interaction.response.send_message(
            view=ui.panel("Status Rotation", body), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Presence(bot))
