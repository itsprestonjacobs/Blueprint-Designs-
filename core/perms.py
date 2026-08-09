"""Role-tier permission checks.

Tiers are defined by role ID in config.json. `admin` implicitly satisfies every
other tier, and a guild administrator always passes so the server owner can
never lock themselves out of their own bot before roles are configured.
"""

from __future__ import annotations

import discord
from discord import app_commands

from .config import config

TIER_ORDER = ("designer", "support", "hr", "admin")


class MissingTier(app_commands.CheckFailure):
    def __init__(self, tiers: tuple[str, ...]) -> None:
        self.tiers = tiers
        pretty = " or ".join(t.upper() for t in tiers)
        super().__init__(f"You need the {pretty} role to use this.")


def has_tier(member: discord.Member, *tiers: str) -> bool:
    if not isinstance(member, discord.Member):
        return False

    if member.guild_permissions.administrator:
        return True

    member_roles = {r.id for r in member.roles}

    # admin satisfies everything, so always test it alongside the request.
    for tier in {*tiers, "admin"}:
        if member_roles.intersection(config.role_ids(tier)):
            return True
    return False


def require(*tiers: str):
    """Decorator gating a slash command behind one or more tiers."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            raise MissingTier(tiers)
        if has_tier(interaction.user, *tiers):
            return True
        raise MissingTier(tiers)

    return app_commands.check(predicate)


def staff_role_mentions(*tiers: str) -> str:
    ids: list[int] = []
    for tier in tiers:
        ids.extend(config.role_ids(tier))
    return " ".join(f"<@&{rid}>" for rid in dict.fromkeys(ids))
