"""Anti-nuke.

Watches the audit log in real time and counts destructive actions per person.
Cross a threshold and the bot strips your roles (or bans you) and shouts about
it in the security channel.

The whole point is speed: a compromised admin account can delete forty channels
in ten seconds, so reacting to each audit entry as it arrives matters more than
any periodic scan would.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.config import config
from core.logs import send_log
from core.security import RateTracker, can_global_ban, is_whitelisted
from core.store import JSONStore

log = logging.getLogger("blueprint.antinuke")

store = JSONStore("antinuke", {})

# audit action -> (config key, human label)
WATCHED: dict[discord.AuditLogAction, tuple[str, str]] = {
    discord.AuditLogAction.ban: ("ban", "banning members"),
    discord.AuditLogAction.kick: ("kick", "kicking members"),
    discord.AuditLogAction.channel_delete: ("channel_delete", "deleting channels"),
    discord.AuditLogAction.channel_create: ("channel_create", "creating channels"),
    discord.AuditLogAction.role_delete: ("role_delete", "deleting roles"),
    discord.AuditLogAction.role_create: ("role_create", "creating roles"),
    discord.AuditLogAction.webhook_create: ("webhook_create", "creating webhooks"),
    discord.AuditLogAction.member_role_update: ("role_grant", "handing out roles"),
    discord.AuditLogAction.member_prune: ("prune", "pruning members"),
    discord.AuditLogAction.guild_update: ("guild_update", "changing server settings"),
}

# Sensible defaults: count within window seconds triggers a response.
DEFAULT_THRESHOLDS = {
    "ban": {"count": 3, "window": 20},
    "kick": {"count": 4, "window": 20},
    "channel_delete": {"count": 3, "window": 20},
    "channel_create": {"count": 5, "window": 20},
    "role_delete": {"count": 3, "window": 20},
    "role_create": {"count": 5, "window": 20},
    "webhook_create": {"count": 3, "window": 20},
    "role_grant": {"count": 5, "window": 20},
    "prune": {"count": 1, "window": 60},
    "guild_update": {"count": 3, "window": 30},
}

DANGEROUS_PERMS = (
    "administrator",
    "ban_members",
    "kick_members",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
)


def enabled() -> bool:
    return bool(config.get("security.antinuke.enabled", True))


def threshold_for(key: str) -> tuple[int, int]:
    configured = (config.get("security.antinuke.thresholds", {}) or {}).get(key)
    default = DEFAULT_THRESHOLDS.get(key, {"count": 5, "window": 20})
    if not configured:
        return default["count"], default["window"]
    return (
        int(configured.get("count", default["count"])),
        int(configured.get("window", default["window"])),
    )


def punishment() -> str:
    return str(config.get("security.antinuke.punishment", "quarantine")).lower()


class AntiNuke(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tracker = RateTracker()
        self._acting: set[tuple[int, int]] = set()  # guilds/actors mid-punishment

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        if not enabled():
            return

        watched = WATCHED.get(entry.action)
        if watched is None:
            return

        key, label = watched
        actor = entry.user
        guild = entry.guild
        if actor is None or guild is None or actor.bot and actor.id == self.bot.user.id:
            return

        if is_whitelisted(self.bot, actor.id):
            return

        # Granting a role only counts if the role actually carries power.
        if key == "role_grant" and not self._granted_dangerous_role(entry):
            return

        count, window = threshold_for(key)
        seen = self.tracker.hit(guild.id, actor.id, key, window)

        if seen < count:
            return

        # Don't stack punishments while the first is still running.
        marker = (guild.id, actor.id)
        if marker in self._acting:
            return
        self._acting.add(marker)
        try:
            await self.respond(guild, actor, key, label, seen, window)
        finally:
            self.tracker.clear_actor(guild.id, actor.id)
            self._acting.discard(marker)

    def _granted_dangerous_role(self, entry: discord.AuditLogEntry) -> bool:
        """True if this member_role_update handed over a privileged role."""
        try:
            after = entry.changes.after
            roles = getattr(after, "roles", None) or []
        except AttributeError:
            return False

        guild = entry.guild
        for stub in roles:
            role = guild.get_role(stub.id) if guild else None
            if role is None:
                continue
            perms = role.permissions
            if any(getattr(perms, p, False) for p in DANGEROUS_PERMS):
                return True
        return False

    async def respond(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
        key: str,
        label: str,
        seen: int,
        window: int,
    ) -> None:
        action = punishment()
        member = guild.get_member(actor.id)
        outcome = "alert only"

        if member is not None and member.id == guild.owner_id:
            # The owner cannot be removed; surface it loudly instead.
            outcome = "server owner — cannot act, alerted only"
        elif member is not None:
            outcome = await self._punish(guild, member, key, action)

        record = {
            "guild": guild.id,
            "actor": actor.id,
            "actor_name": str(actor),
            "action": key,
            "count": seen,
            "window": window,
            "outcome": outcome,
            "at": int(time.time()),
        }
        async with store.edit() as data:
            data.setdefault("incidents", []).append(record)
            # Keep the file from growing without bound.
            data["incidents"] = data["incidents"][-500:]

        view = ui.panel(
            "Anti-Nuke Triggered",
            "\n".join(
                [
                    ui.field("Server", guild.name),
                    ui.field("Who", f"{actor} (`{actor.id}`)"),
                    ui.field("What", f"{label} — **{seen}** in {window}s"),
                    ui.field("Response", outcome),
                ]
            ),
            color=ui.RED_HEX,
        )
        await send_log(self.bot, "security_log", view)
        log.warning(
            "anti-nuke: %s did %s x%d in %s -> %s", actor, key, seen, guild.name, outcome
        )

    async def _punish(
        self, guild: discord.Guild, member: discord.Member, key: str, action: str
    ) -> str:
        reason = f"[Sail's Customs Anti-Nuke] {key} threshold exceeded"

        if member.top_role >= guild.me.top_role:
            return "above me in the role list — could not act"

        try:
            if action == "ban":
                await member.ban(reason=reason, delete_message_days=0)
                return "banned"

            if action == "kick":
                await member.kick(reason=reason)
                return "kicked"

            if action == "quarantine":
                strip = [
                    r
                    for r in member.roles
                    if not r.is_default()
                    and r < guild.me.top_role
                    and not r.managed
                ]
                if strip:
                    await member.remove_roles(*strip, reason=reason)
                    return f"stripped {len(strip)} role(s)"
                return "no removable roles"

        except discord.Forbidden:
            return "missing permission to act"
        except discord.HTTPException as exc:
            return f"failed ({exc.status})"

        return "alert only"

    # -- commands ---------------------------------------------------------

    antinuke = app_commands.Group(name="antinuke", description="Anti-nuke protection")

    @antinuke.command(name="status", description="Show anti-nuke settings")
    async def status(self, interaction: discord.Interaction) -> None:
        if not can_global_ban(self.bot, interaction.user):
            await interaction.response.send_message(view=ui.err("No permission."), ephemeral=True)
            return

        lines = []
        for action_key, label in sorted({v[0]: v[1] for v in WATCHED.values()}.items()):
            count, window = threshold_for(action_key)
            lines.append(f"`{action_key}` — {count} in {window}s  _{label}_")

        body = (
            f"**Status:** {'on' if enabled() else 'off'}\n"
            f"**Response:** {punishment()}\n\n"
            "**Thresholds**\n" + "\n".join(lines)
        )
        await interaction.response.send_message(
            view=ui.panel("Anti-Nuke", body), ephemeral=True
        )

    @antinuke.command(name="toggle", description="Turn anti-nuke on or off")
    async def toggle(self, interaction: discord.Interaction, on: bool) -> None:
        if not can_global_ban(self.bot, interaction.user):
            await interaction.response.send_message(view=ui.err("No permission."), ephemeral=True)
            return

        config.set("security.antinuke.enabled", on)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"Anti-nuke **{'on' if on else 'off'}**."), ephemeral=True
        )

    @antinuke.command(name="incidents", description="Recent anti-nuke triggers")
    async def incidents(self, interaction: discord.Interaction) -> None:
        if not can_global_ban(self.bot, interaction.user):
            await interaction.response.send_message(view=ui.err("No permission."), ephemeral=True)
            return

        data = await store.read()
        rows = (data.get("incidents") or [])[-15:][::-1]
        if not rows:
            body = "Nothing has tripped it."
        else:
            body = "\n".join(
                f"<t:{r['at']}:R> **{r['actor_name']}** — {r['action']} x{r['count']} "
                f"→ {r['outcome']}"
                for r in rows
            )
        await interaction.response.send_message(
            view=ui.panel("Anti-Nuke Incidents", body), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiNuke(bot))
