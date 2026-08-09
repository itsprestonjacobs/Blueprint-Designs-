"""Helpers for posting to the configured log channels."""

from __future__ import annotations

import logging

import discord
from discord import ui

from .config import config

log = logging.getLogger("blueprint.logs")


async def get_channel(
    bot: discord.Client, key: str
) -> discord.TextChannel | None:
    """Resolve a channel by its config key, or None if unset/missing."""
    channel_id = config.channel_id(key)
    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("channels.%s points at %s which I can't access", key, channel_id)
            return None

    # Threads are valid log destinations too, and they are not TextChannels.
    # Anything else (category, forum, voice) can't take a message.
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel

    log.warning(
        "channels.%s points at %s, which I can't post to (%s)",
        key,
        channel_id,
        type(channel).__name__,
    )
    return None


async def send_log(
    bot: discord.Client,
    key: str,
    view: ui.LayoutView,
    *,
    content: str | None = None,
) -> discord.Message | None:
    """Post a V2 view to a log channel.

    Silently no-ops when the channel isn't configured yet -- an unconfigured log
    destination should never break the command that triggered it.

    `content` is only used for role pings; a V2 message can't carry both text
    and components, so it is sent as a separate leading message.
    """
    channel = await get_channel(bot, key)
    if channel is None:
        return None

    try:
        if content:
            await channel.send(
                content, allowed_mentions=discord.AllowedMentions(roles=True, users=True)
            )
        # Upload any local images the view references (see core.ui.BaseLayout).
        files = view.files() if hasattr(view, "files") else []
        return await channel.send(view=view, files=files)
    except discord.HTTPException as exc:
        log.warning("failed to post to channels.%s: %s", key, exc)
        return None
