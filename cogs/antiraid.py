"""Anti-raid.

Two independent defences:

* **Join rate** — too many joins inside a window flips the server into lockdown
  and holds every new arrival until a human clears it.
* **Account age** — brand new accounts are the raw material of most raids, so
  they can be held or kicked on sight.

Lockdown raises the server's verification level and (optionally) quarantines
arrivals rather than banning them, because raids sweep up innocent people and a
ban is much harder to walk back than a role.
"""

from __future__ import annotations

import logging
import time
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import ui
from core.config import config
from core.logs import get_channel
from core.security import can_global_ban
from core.store import JSONStore

log = logging.getLogger("blueprint.antiraid")

store = JSONStore("antiraid", {})


def cfg(key: str, default):
    value = config.get(f"security.antiraid.{key}")
    return default if value is None else value


async def alert(bot, view) -> None:
    """Post to the raid channel, falling back to the general security log."""
    for key in ("raid_alerts", "security_log"):
        channel = await get_channel(bot, key)
        if channel is not None:
            files = view.files() if hasattr(view, "files") else []
            await channel.send(view=view, files=files)
            return


def enabled() -> bool:
    return bool(cfg("enabled", True))


class AntiRaid(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.joins: dict[int, deque[float]] = {}
        self.lockdown: dict[int, float] = {}   # guild id -> started at

    async def cog_load(self) -> None:
        self.auto_release.start()

    async def cog_unload(self) -> None:
        self.auto_release.cancel()

    # -- detection --------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not enabled() or member.bot:
            return

        guild = member.guild
        now = time.time()

        window = int(cfg("join_window", 10))
        limit = int(cfg("join_limit", 6))

        bucket = self.joins.setdefault(guild.id, deque())
        bucket.append(now)
        while bucket and now - bucket[0] > window:
            bucket.popleft()

        # Account-age gate applies whether or not a raid is underway.
        min_age_hours = int(cfg("min_account_age_hours", 0))
        if min_age_hours:
            age_hours = (discord.utils.utcnow() - member.created_at).total_seconds() / 3600
            if age_hours < min_age_hours:
                await self._handle_young_account(member, age_hours, min_age_hours)
                return

        if guild.id in self.lockdown:
            await self._hold(member, "server is in lockdown")
            return

        if len(bucket) >= limit:
            await self.engage_lockdown(guild, len(bucket), window)
            await self._hold(member, "joined during a raid")

    async def _handle_young_account(
        self, member: discord.Member, age_hours: float, minimum: int
    ) -> None:
        action = str(cfg("young_account_action", "hold")).lower()
        reason = f"[Sail's Customs Anti-Raid] account {age_hours:.1f}h old, minimum {minimum}h"

        try:
            if action == "kick":
                await member.kick(reason=reason)
                outcome = "kicked"
            elif action == "ban":
                await member.ban(reason=reason, delete_message_days=0)
                outcome = "banned"
            else:
                await self._hold(member, f"account only {age_hours:.1f}h old")
                outcome = "held"
        except discord.Forbidden:
            outcome = "no permission to act"
        except discord.HTTPException as exc:
            outcome = f"failed ({exc.status})"

        await alert(
            self.bot,
            ui.panel(
                "New Account Blocked",
                "\n".join(
                    [
                        ui.field("User", f"{member} (`{member.id}`)"),
                        ui.field("Account age", f"{age_hours:.1f} hours"),
                        ui.field("Minimum", f"{minimum} hours"),
                        ui.field("Action", outcome),
                    ]
                ),
                color=ui.AMBER_HEX,
            ),
        )

    async def _hold(self, member: discord.Member, why: str) -> None:
        """Quarantine an arrival instead of banning them.

        Raids catch bystanders, so the reversible option is the right default.
        """
        role_id = config.get("security.quarantine_role")
        if not role_id:
            return

        role = member.guild.get_role(int(role_id))
        if role is None:
            return

        try:
            await member.add_roles(role, reason=f"[Sail's Customs Anti-Raid] {why}")
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not quarantine %s in %s", member, member.guild)

    # -- lockdown ---------------------------------------------------------

    async def engage_lockdown(self, guild: discord.Guild, joins: int, window: int) -> None:
        if guild.id in self.lockdown:
            return

        self.lockdown[guild.id] = time.time()

        raised = "unchanged"
        try:
            if guild.verification_level < discord.VerificationLevel.high:
                await guild.edit(
                    verification_level=discord.VerificationLevel.high,
                    reason="[Sail's Customs Anti-Raid] raid detected",
                )
                raised = "raised to High"
        except (discord.Forbidden, discord.HTTPException):
            raised = "could not change (missing permission)"

        async with store.edit() as data:
            data.setdefault("lockdowns", []).append(
                {"guild": guild.id, "joins": joins, "window": window, "at": int(time.time())}
            )
            data["lockdowns"] = data["lockdowns"][-200:]

        minutes = int(cfg("lockdown_minutes", 10))
        await alert(
            self.bot,
            ui.panel(
                "Raid Detected — Lockdown",
                "\n".join(
                    [
                        ui.field("Server", guild.name),
                        ui.field("Joins", f"**{joins}** in {window}s"),
                        ui.field("Verification", raised),
                        ui.field("New arrivals", "held in quarantine"),
                        ui.field("Auto-release", f"in {minutes} min, or `/antiraid release`"),
                    ]
                ),
                color=ui.RED_HEX,
            ),
        )
        log.warning("lockdown engaged in %s (%d joins in %ds)", guild.name, joins, window)

    async def release(self, guild: discord.Guild) -> bool:
        if guild.id not in self.lockdown:
            return False

        self.lockdown.pop(guild.id, None)
        self.joins.pop(guild.id, None)

        try:
            await guild.edit(
                verification_level=discord.VerificationLevel.medium,
                reason="[Sail's Customs Anti-Raid] lockdown lifted",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        await alert(
            self.bot,
            ui.panel(
                "Lockdown Lifted",
                f"**{guild.name}** is back to normal. Quarantined members still "
                "need clearing by hand.",
                color=ui.GREEN_HEX,
            ),
        )
        return True

    @tasks.loop(minutes=1)
    async def auto_release(self) -> None:
        """Lift lockdowns that have aged out."""
        minutes = int(cfg("lockdown_minutes", 10))
        cutoff = time.time() - minutes * 60
        for guild_id, started in list(self.lockdown.items()):
            if started < cutoff:
                guild = self.bot.get_guild(guild_id)
                if guild is not None:
                    await self.release(guild)
                else:
                    self.lockdown.pop(guild_id, None)

    @auto_release.before_loop
    async def before_release(self) -> None:
        await self.bot.wait_until_ready()

    # -- commands ---------------------------------------------------------

    antiraid = app_commands.Group(name="antiraid", description="Raid protection")

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if can_global_ban(self.bot, interaction.user):
            return True
        await interaction.response.send_message(view=ui.err("No permission."), ephemeral=True)
        return False

    @antiraid.command(name="status", description="Show raid protection settings")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._gate(interaction):
            return

        guild = interaction.guild
        locked = guild is not None and guild.id in self.lockdown

        body = "\n".join(
            [
                ui.field("Status", "on" if enabled() else "off"),
                ui.field("Lockdown", "**ACTIVE**" if locked else "no"),
                ui.field("Join limit", f"{cfg('join_limit', 6)} in {cfg('join_window', 10)}s"),
                ui.field("Min account age", f"{cfg('min_account_age_hours', 0)}h"),
                ui.field("New account action", cfg("young_account_action", "hold")),
                ui.field("Lockdown length", f"{cfg('lockdown_minutes', 10)} min"),
                ui.field(
                    "Quarantine role",
                    f"<@&{config.get('security.quarantine_role')}>"
                    if config.get("security.quarantine_role")
                    else "not set",
                ),
            ]
        )
        await interaction.response.send_message(
            view=ui.panel("Anti-Raid", body), ephemeral=True
        )

    @antiraid.command(name="lock", description="Force the server into lockdown")
    async def lock(self, interaction: discord.Interaction) -> None:
        if not await self._gate(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.engage_lockdown(interaction.guild, 0, 0)
        await interaction.followup.send(view=ui.ok("Lockdown engaged."), ephemeral=True)

    @antiraid.command(name="release", description="Lift the lockdown")
    async def release_cmd(self, interaction: discord.Interaction) -> None:
        if not await self._gate(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        lifted = await self.release(interaction.guild)
        await interaction.followup.send(
            view=ui.ok("Lockdown lifted.") if lifted else ui.warn("Not in lockdown."),
            ephemeral=True,
        )

    @antiraid.command(name="toggle", description="Turn raid protection on or off")
    async def toggle(self, interaction: discord.Interaction, on: bool) -> None:
        if not await self._gate(interaction):
            return

        config.set("security.antiraid.enabled", on)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"Anti-raid **{'on' if on else 'off'}**."), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiRaid(bot))
