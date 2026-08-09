"""Shared security helpers.

Permission for global actions is deliberately checked against the *home* guild
(`guild_id` in config), not the guild the command was run in. A global ban is
cross-server, so authority has to come from one place -- otherwise anyone who
happened to hold a similarly-named role in a satellite server could use it.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

import discord

from .config import config

log = logging.getLogger("blueprint.security")


def gban_role_id() -> int | None:
    rid = config.get("security.gban_role")
    return int(rid) if rid else None


def home_guild(bot: discord.Client) -> discord.Guild | None:
    return bot.get_guild(config.guild_id) if config.guild_id else None


def can_global_ban(bot: discord.Client, user: discord.abc.User) -> bool:
    """True if `user` holds the global-ban role in the home guild."""
    guild = home_guild(bot)
    if guild is None:
        return False

    member = guild.get_member(user.id)
    if member is None:
        return False

    if member.id == guild.owner_id:
        return True

    role_id = gban_role_id()
    if role_id is None:
        return False

    return any(r.id == role_id for r in member.roles)


def is_whitelisted(bot: discord.Client, user_id: int) -> bool:
    """Users and roles that anti-nuke should never act against."""
    if bot.user is not None and user_id == bot.user.id:
        return True

    if user_id in {int(u) for u in (config.get("security.whitelist_users", []) or [])}:
        return True

    guild = home_guild(bot)
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            if member.id == guild.owner_id:
                return True
            trusted = {int(r) for r in (config.get("security.whitelist_roles", []) or [])}
            if trusted and any(r.id in trusted for r in member.roles):
                return True

    return False


def protected_from_gban(bot: discord.Client, user_id: int) -> str | None:
    """Reason this user must not be global-banned, or None if they may be."""
    if bot.user and user_id == bot.user.id:
        return "that's me"

    guild = home_guild(bot)
    if guild is not None and user_id == guild.owner_id:
        return "they own the server"

    if can_global_ban(bot, discord.Object(id=user_id)):  # type: ignore[arg-type]
        return "they hold the global-ban role"

    if is_whitelisted(bot, user_id):
        return "they're whitelisted"

    return None


class RateTracker:
    """Rolling per-actor event counter used by the anti-nuke thresholds.

    Keeps a deque of timestamps per (guild, actor, action) and reports how many
    landed inside the window. Old entries are dropped on read, so memory stays
    bounded without a cleanup task.
    """

    def __init__(self) -> None:
        self._events: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)

    def hit(self, guild_id: int, actor_id: int, action: str, window: int) -> int:
        key = (guild_id, actor_id, action)
        now = time.time()
        bucket = self._events[key]
        bucket.append(now)
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        return len(bucket)

    def reset(self, guild_id: int, actor_id: int, action: str) -> None:
        self._events.pop((guild_id, actor_id, action), None)

    def clear_actor(self, guild_id: int, actor_id: int) -> None:
        for key in [k for k in self._events if k[0] == guild_id and k[1] == actor_id]:
            self._events.pop(key, None)
