"""Global bans across every Blueprint Designs server.

One ban entry applies everywhere the bot is. Adding a ban fans it out to all
current guilds; joining a new guild replays the whole list into it; and anyone
on the list is banned the moment they try to join anywhere.

Authority comes from a single role in the home guild -- see core/security.py.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.config import config
from core.logs import send_log
from core.security import can_global_ban, protected_from_gban
from core.store import JSONStore

log = logging.getLogger("blueprint.globalban")

store = JSONStore("globalbans", {})

BAN_REASON_PREFIX = "[Blueprint Global Ban]"


async def ban_entries() -> dict[str, dict]:
    data = await store.read()
    return data.get("bans") or {}


async def is_banned(user_id: int) -> dict | None:
    return (await ban_entries()).get(str(user_id))


class GlobalBanned(app_commands.CheckFailure):
    def __init__(self) -> None:
        super().__init__("You don't have permission to issue global bans.")


def requires_gban():
    async def predicate(interaction: discord.Interaction) -> bool:
        if can_global_ban(interaction.client, interaction.user):
            return True
        raise GlobalBanned()

    return app_commands.check(predicate)


class GlobalBan(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- fan-out ----------------------------------------------------------

    async def apply_ban(
        self, user_id: int, reason: str, delete_days: int = 0
    ) -> tuple[list[str], list[str]]:
        """Ban a user in every guild. Returns (succeeded, failed) guild names."""
        ok: list[str] = []
        failed: list[str] = []
        full_reason = f"{BAN_REASON_PREFIX} {reason}"[:500]

        for guild in self.bot.guilds:
            try:
                await guild.ban(
                    discord.Object(id=user_id),
                    reason=full_reason,
                    delete_message_days=max(0, min(7, delete_days)),
                )
                ok.append(guild.name)
            except discord.Forbidden:
                failed.append(f"{guild.name} (no permission)")
            except discord.HTTPException as exc:
                failed.append(f"{guild.name} ({exc.status})")

        return ok, failed

    async def lift_ban(self, user_id: int, reason: str) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        failed: list[str] = []

        for guild in self.bot.guilds:
            try:
                await guild.unban(discord.Object(id=user_id), reason=reason[:500])
                ok.append(guild.name)
            except discord.NotFound:
                ok.append(guild.name)  # already not banned there; that's fine
            except discord.Forbidden:
                failed.append(f"{guild.name} (no permission)")
            except discord.HTTPException as exc:
                failed.append(f"{guild.name} ({exc.status})")

        return ok, failed

    # -- events -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Enforce the list on anyone trying to join."""
        entry = await is_banned(member.id)
        if entry is None:
            return

        try:
            await member.guild.ban(
                discord.Object(id=member.id),
                reason=f"{BAN_REASON_PREFIX} {entry.get('reason', 'no reason given')}"[:500],
                delete_message_days=0,
            )
        except discord.HTTPException:
            log.warning("could not enforce global ban on %s in %s", member.id, member.guild)
            return

        await send_log(
            self.bot,
            "security_log",
            ui.panel(
                "Global Ban Enforced",
                "\n".join(
                    [
                        ui.field("User", f"{member} (`{member.id}`)"),
                        ui.field("Server", member.guild.name),
                        ui.field("Original reason", entry.get("reason", "—")),
                        ui.field("Banned by", f"<@{entry.get('by')}>"),
                    ]
                ),
                color=ui.RED_HEX,
            ),
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Replay the ban list into a newly added server."""
        entries = await ban_entries()
        if not entries:
            return

        applied = 0
        for uid, entry in entries.items():
            try:
                await guild.ban(
                    discord.Object(id=int(uid)),
                    reason=f"{BAN_REASON_PREFIX} {entry.get('reason', '')}"[:500],
                )
                applied += 1
            except discord.HTTPException:
                continue

        log.info("synced %d/%d global bans into %s", applied, len(entries), guild.name)

    # -- commands ---------------------------------------------------------

    gban = app_commands.Group(
        name="gban", description="Global bans across all Blueprint servers"
    )

    @gban.command(name="add", description="Globally ban a user from every Blueprint server")
    @app_commands.describe(
        user="User ID, or mention someone in this server",
        reason="Why they're being banned",
        delete_days="Days of their messages to delete (0-7)",
    )
    @requires_gban()
    async def add(
        self,
        interaction: discord.Interaction,
        user: str,
        reason: str,
        delete_days: int = 0,
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            user_id = int(user.strip().strip("<@!>"))
        except ValueError:
            await interaction.followup.send(
                view=ui.err("That's not a valid user ID or mention.")
            )
            return

        blocked = protected_from_gban(self.bot, user_id)
        if blocked:
            await interaction.followup.send(
                view=ui.err(f"Can't global ban them — {blocked}.")
            )
            return

        if await is_banned(user_id):
            await interaction.followup.send(view=ui.warn("They're already global banned."))
            return

        # Resolve a name for the record while we still can.
        try:
            target = await self.bot.fetch_user(user_id)
            display = str(target)
        except discord.HTTPException:
            display = f"Unknown user {user_id}"

        async with store.edit() as data:
            data.setdefault("bans", {})[str(user_id)] = {
                "user": user_id,
                "username": display,
                "reason": reason,
                "by": interaction.user.id,
                "by_name": str(interaction.user),
                "at": int(time.time()),
            }

        ok, failed = await self.apply_ban(user_id, reason, delete_days)

        body = [
            ui.field("User", f"{display} (`{user_id}`)"),
            ui.field("Reason", reason),
            ui.field("Banned by", interaction.user.mention),
            ui.field("Servers", f"{len(ok)} of {len(self.bot.guilds)}"),
        ]
        if failed:
            body.append(ui.field("Failed", ", ".join(failed[:5])))

        view = ui.panel("Global Ban Issued", "\n".join(body), color=ui.RED_HEX)
        await interaction.followup.send(view=view)
        await send_log(self.bot, "security_log", view)

    @gban.command(name="remove", description="Lift a global ban everywhere")
    @app_commands.describe(user_id="The banned user's ID", reason="Why it's being lifted")
    @requires_gban()
    async def remove(
        self, interaction: discord.Interaction, user_id: str, reason: str = "Appeal accepted"
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            uid = int(user_id.strip().strip("<@!>"))
        except ValueError:
            await interaction.followup.send(view=ui.err("That's not a valid user ID."))
            return

        entry = await is_banned(uid)
        if entry is None:
            await interaction.followup.send(view=ui.warn("They aren't global banned."))
            return

        async with store.edit() as data:
            (data.get("bans") or {}).pop(str(uid), None)
            data.setdefault("lifted", {})[str(uid)] = {
                "user": uid,
                "reason": reason,
                "by": interaction.user.id,
                "at": int(time.time()),
                "original": entry,
            }

        ok, failed = await self.lift_ban(uid, reason)

        body = [
            ui.field("User", f"{entry.get('username', uid)} (`{uid}`)"),
            ui.field("Reason", reason),
            ui.field("Lifted by", interaction.user.mention),
            ui.field("Servers", f"{len(ok)} of {len(self.bot.guilds)}"),
        ]
        if failed:
            body.append(ui.field("Failed", ", ".join(failed[:5])))

        view = ui.panel("Global Ban Lifted", "\n".join(body), color=ui.GREEN_HEX)
        await interaction.followup.send(view=view)
        await send_log(self.bot, "security_log", view)

    @gban.command(name="check", description="Is this user global banned?")
    @app_commands.describe(user_id="User ID to look up")
    async def check(self, interaction: discord.Interaction, user_id: str) -> None:
        try:
            uid = int(user_id.strip().strip("<@!>"))
        except ValueError:
            await interaction.response.send_message(
                view=ui.err("That's not a valid user ID."), ephemeral=True
            )
            return

        entry = await is_banned(uid)
        if entry is None:
            await interaction.response.send_message(
                view=ui.ok(f"`{uid}` is not global banned."), ephemeral=True
            )
            return

        await interaction.response.send_message(
            view=ui.panel(
                "Global Banned",
                "\n".join(
                    [
                        ui.field("User", f"{entry.get('username', uid)} (`{uid}`)"),
                        ui.field("Reason", entry.get("reason", "—")),
                        ui.field("Banned by", f"<@{entry.get('by')}>"),
                        ui.field("When", f"<t:{entry.get('at', 0)}:F>"),
                    ]
                ),
                color=ui.RED_HEX,
            ),
            ephemeral=True,
        )

    @gban.command(name="list", description="Everyone on the global ban list")
    @requires_gban()
    async def list_bans(self, interaction: discord.Interaction) -> None:
        entries = await ban_entries()
        if not entries:
            await interaction.response.send_message(
                view=ui.panel("Global Bans", "Nobody is global banned."), ephemeral=True
            )
            return

        rows = sorted(entries.values(), key=lambda e: e.get("at", 0), reverse=True)
        lines = [
            f"`{e['user']}` **{e.get('username', 'unknown')}** — {e.get('reason', '—')[:60]} "
            f"(<t:{e.get('at', 0)}:d>)"
            for e in rows[:20]
        ]
        body = f"**{len(entries)}** banned\n\n" + "\n".join(lines)
        if len(entries) > 20:
            body += f"\n-# …and {len(entries) - 20} more."

        await interaction.response.send_message(
            view=ui.panel("Global Bans", body), ephemeral=True
        )

    @gban.command(name="sync", description="Re-apply the whole ban list to every server")
    @requires_gban()
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        entries = await ban_entries()
        if not entries:
            await interaction.followup.send(view=ui.warn("Nothing on the list to sync."))
            return

        applied = 0
        failures = 0
        for uid, entry in entries.items():
            ok, failed = await self.apply_ban(
                int(uid), entry.get("reason", "global ban"), 0
            )
            applied += len(ok)
            failures += len(failed)

        await interaction.followup.send(
            view=ui.panel(
                "Sync Complete",
                "\n".join(
                    [
                        ui.field("Entries", len(entries)),
                        ui.field("Servers", len(self.bot.guilds)),
                        ui.field("Bans applied", applied),
                        ui.field("Failures", failures),
                    ]
                ),
            )
        )

    @gban.command(name="servers", description="Which servers this bot protects")
    @requires_gban()
    async def servers(self, interaction: discord.Interaction) -> None:
        lines = []
        for g in self.bot.guilds:
            me = g.me
            can_ban = me.guild_permissions.ban_members if me else False
            lines.append(
                f"{ui.OK if can_ban else ui.ERR} **{g.name}** — {g.member_count} members"
                + ("" if can_ban else "  _(missing Ban Members)_")
            )

        await interaction.response.send_message(
            view=ui.panel(
                "Protected Servers",
                f"**{len(self.bot.guilds)}** server(s)\n\n" + "\n".join(lines),
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GlobalBan(bot))
