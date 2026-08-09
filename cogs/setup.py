"""In-Discord configuration.

`/setup auto` scans the server and matches its categories, channels and roles to
the config keys by name, shows you exactly what it found, and only writes after
you confirm. Anything it guesses wrong can be fixed with the manual setters.

Everything here writes to config.json, so changes survive a restart.
"""

from __future__ import annotations

import difflib
import logging
import re

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.config import config
from core.perms import require

log = logging.getLogger("blueprint.setup")

# How close a name has to be before we'll call it a match.
FUZZY_THRESHOLD = 0.78

# Words we look for when guessing which channel is which. First match wins, so
# order matters -- more specific keys sit above the generic ones.
CHANNEL_HINTS: dict[str, tuple[str, ...]] = {
    "ticket_transcripts": ("transcript", "ticket log", "ticket-logs", "closed ticket"),
    "order_log": ("order log", "completed order", "order-logs", "orders"),
    "mod_log": ("mod log", "moderation", "mod-logs", "punishment"),
    "infraction_log": ("infraction", "discipline", "strike"),
    "promotion_log": ("promotion", "promo"),
    "loa_log": ("loa", "leave of absence", "absence"),
    "activity_check": ("activity", "activity check"),
    "quality_control": ("quality", "qc", "quality control"),
    "application_review": ("application", "app review", "applications"),
    "review_channel": ("review", "vouch", "feedback", "testimonial"),
    "suggestions": ("suggestion", "suggest", "idea"),
    "partnerships": ("partner", "partnership", "affiliate"),
    "queue_board": ("queue", "live queue"),
    "status_board": ("status", "order status"),
    "welcome": ("welcome", "greet", "join"),
    "giveaways": ("giveaway", "giveaways"),
}

ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "admin": ("admin", "owner", "director", "management", "founder"),
    "hr": ("hr", "high rank", "highrank", "manager", "supervisor", "executive"),
    "support": ("support", "customer support", "helper", "moderator", "staff"),
    "designer": ("designer", "design", "graphic", "artist", "creator"),
}


def normalise(name: str) -> str:
    """Strip emoji, punctuation and casing so names compare sensibly.

    Discord category names are full of decoration -- '🌐 Website Development
    Orders', '┃ Support Tickets ┃' -- none of which should affect matching.
    """
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def score(a: str, b: str) -> float:
    """Similarity between two already-normalised names, 0.0 to 1.0."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Containment counts as a strong match: 'support tickets' vs 'support'.
    if a in b or b in a:
        shorter, longer = sorted((a, b), key=len)
        return 0.9 if len(shorter) >= 4 else 0.8 * (len(shorter) / len(longer))
    return difflib.SequenceMatcher(None, a, b).ratio()


def best_match(target: str, candidates: list) -> tuple[object | None, float]:
    """Pick the candidate whose name best matches `target`."""
    wanted = normalise(target)
    best, best_score = None, 0.0
    for candidate in candidates:
        current = score(wanted, normalise(candidate.name))
        if current > best_score:
            best, best_score = candidate, current
    return best, best_score


def match_by_hints(hints: tuple[str, ...], candidates: list) -> object | None:
    """Find a candidate whose name contains any of the hint phrases."""
    for hint in hints:
        wanted = normalise(hint)
        for candidate in candidates:
            if wanted in normalise(candidate.name):
                return candidate
    return None


class Plan:
    """A set of proposed config changes, held until the user confirms."""

    def __init__(self) -> None:
        self.changes: list[tuple[str, object, str]] = []  # (dotted key, value, human label)
        self.skipped: list[str] = []

    def add(self, key: str, value: object, label: str) -> None:
        self.changes.append((key, value, label))

    def skip(self, label: str) -> None:
        self.skipped.append(label)

    def apply(self) -> int:
        for key, value, _ in self.changes:
            config.set(key, value)
        config.save()
        return len(self.changes)

    def summary(self) -> str:
        lines: list[str] = []
        if self.changes:
            lines.append(f"**{len(self.changes)} match(es) found:**")
            for _, _, label in self.changes[:28]:
                lines.append(f"{ui.OK} {label}")
            if len(self.changes) > 28:
                lines.append(f"-# …and {len(self.changes) - 28} more.")
        if self.skipped:
            lines.append("")
            lines.append(f"**{len(self.skipped)} not found:**")
            shown = self.skipped[:12]
            lines.append(", ".join(f"`{s}`" for s in shown))
            if len(self.skipped) > len(shown):
                lines.append(f"-# …and {len(self.skipped) - len(shown)} more.")
        return "\n".join(lines) or "Nothing to change."


class ConfirmPlan(ui.BaseLayout):
    def __init__(self, plan: Plan, title: str) -> None:
        super().__init__(timeout=180)
        self.plan = plan

        apply_btn = dui.Button(
            label=f"Apply {len(plan.changes)} change(s)",
            style=discord.ButtonStyle.success,
            emoji="💾",
            disabled=not plan.changes,
        )
        cancel = dui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        apply_btn.callback = self._apply
        cancel.callback = self._cancel

        self.add_item(
            ui.container(
                ui.text(f"## {title}\n{plan.summary()}"),
                ui.separator(),
                ui.text("-# Nothing is written to `config.json` until you press Apply."),
                ui.row(apply_btn, cancel),
            )
        )
        self.validate()

    async def _apply(self, interaction: discord.Interaction) -> None:
        count = self.plan.apply()
        await interaction.response.edit_message(
            view=ui.ok(f"Saved **{count}** value(s) to `config.json`.\n"
                "-# Run `/config` to see what's still unset."
            ),
            content=None,
            embeds=[],
            attachments=[],
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ui.notice("Cancelled — nothing was changed."),
            content=None,
            embeds=[],
            attachments=[],
        )


@app_commands.guild_only()
class Setup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    setup = app_commands.Group(
        name="setup", description="Configure the bot from inside Discord"
    )

    # -- automatic detection ----------------------------------------------

    @setup.command(name="auto", description="Scan the server and fill in config automatically")
    @app_commands.describe(
        categories="Match ticket categories by name",
        channels="Match log channels by name",
        roles="Match staff roles by name",
    )
    @require("admin")
    async def auto(
        self,
        interaction: discord.Interaction,
        categories: bool = True,
        channels: bool = True,
        roles: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        plan = Plan()

        if not config.guild_id:
            plan.add("guild_id", guild.id, f"**Server** → {guild.name}")

        if categories:
            self._plan_categories(guild, plan)
        if channels:
            self._plan_channels(guild, plan)
        if roles:
            self._plan_roles(guild, plan)

        await interaction.followup.send(
            view=ConfirmPlan(plan, "Detected Configuration"), ephemeral=True
        )

    def _plan_categories(self, guild: discord.Guild, plan: Plan) -> None:
        available = list(guild.categories)
        used: set[int] = set()

        # Highest-confidence matches claim their category first, so a strong
        # match isn't stolen by a weaker one competing for the same category.
        scored = []
        for key, spec in config.ticket_categories().items():
            label = spec.get("label", key)
            match, confidence = best_match(label, available)
            scored.append((confidence, key, label, match))
        scored.sort(key=lambda row: row[0], reverse=True)

        for confidence, key, label, match in scored:
            if match is None or confidence < FUZZY_THRESHOLD or match.id in used:
                plan.skip(label)
                continue
            used.add(match.id)
            if config.get(f"tickets.categories.{key}.category_id") == match.id:
                continue  # already correct
            plan.add(
                f"tickets.categories.{key}.category_id",
                match.id,
                f"**{label}** → {match.name}",
            )

    def _plan_channels(self, guild: discord.Guild, plan: Plan) -> None:
        text_channels = list(guild.text_channels)
        for key, hints in CHANNEL_HINTS.items():
            if config.channel_id(key):
                continue  # don't overwrite something already set
            match = match_by_hints(hints, text_channels)
            if match is None:
                plan.skip(f"channels.{key}")
                continue
            plan.add(f"channels.{key}", match.id, f"**channels.{key}** → #{match.name}")

    def _plan_roles(self, guild: discord.Guild, plan: Plan) -> None:
        roles = [r for r in guild.roles if not r.is_default()]
        for tier, hints in ROLE_HINTS.items():
            if config.role_ids(tier):
                continue
            match = match_by_hints(hints, roles)
            if match is None:
                plan.skip(f"roles.{tier}")
                continue
            plan.add(f"roles.{tier}", [match.id], f"**roles.{tier}** → @{match.name}")

    # -- manual setters ---------------------------------------------------

    @setup.command(name="category", description="Point a ticket type at a category")
    @app_commands.describe(ticket_type="Which ticket type", category="The Discord category")
    @require("admin")
    async def set_category(
        self,
        interaction: discord.Interaction,
        ticket_type: str,
        category: discord.CategoryChannel,
    ) -> None:
        spec = config.ticket_category(ticket_type)
        if spec is None:
            await interaction.response.send_message(
                view=ui.err(f"No ticket type `{ticket_type}`. "
                    f"Valid: {', '.join(f'`{k}`' for k in config.ticket_categories())}"
                ),
                ephemeral=True,
            )
            return

        config.set(f"tickets.categories.{ticket_type}.category_id", category.id)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"**{spec.get('label', ticket_type)}** tickets will open in "
                f"**{category.name}**."
            ),
            ephemeral=True,
        )

    @set_category.autocomplete("ticket_type")
    async def ticket_type_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=spec.get("label", key), value=key)
            for key, spec in config.ticket_categories().items()
            if current in key.lower() or current in spec.get("label", "").lower()
        ][:25]

    @setup.command(name="ping", description="Set which roles get pinged for a ticket type")
    @app_commands.describe(ticket_type="Which ticket type", role="Role to ping", add="Add to the list instead of replacing it")
    @require("admin")
    async def set_ping(
        self,
        interaction: discord.Interaction,
        ticket_type: str,
        role: discord.Role,
        add: bool = False,
    ) -> None:
        spec = config.ticket_category(ticket_type)
        if spec is None:
            await interaction.response.send_message(
                view=ui.err(f"No ticket type `{ticket_type}`."), ephemeral=True
            )
            return

        current = list(spec.get("ping_roles") or []) if add else []
        if role.id not in current:
            current.append(role.id)

        config.set(f"tickets.categories.{ticket_type}.ping_roles", current)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"**{spec.get('label', ticket_type)}** tickets now ping "
                + " ".join(f"<@&{r}>" for r in current)
            ),
            ephemeral=True,
        )

    @set_ping.autocomplete("ticket_type")
    async def ping_type_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self.ticket_type_autocomplete(interaction, current)

    @setup.command(name="channel", description="Set a log or board channel")
    @app_commands.describe(key="Which channel setting", channel="The channel to use")
    @require("admin")
    async def set_channel(
        self, interaction: discord.Interaction, key: str, channel: discord.TextChannel
    ) -> None:
        known = config.get("channels", {}) or {}
        if key not in known:
            await interaction.response.send_message(
                view=ui.err(f"Unknown channel key `{key}`. "
                    f"Valid: {', '.join(f'`{k}`' for k in known)}"
                ),
                ephemeral=True,
            )
            return

        config.set(f"channels.{key}", channel.id)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"`channels.{key}` → {channel.mention}"), ephemeral=True
        )

    @set_channel.autocomplete("key")
    async def channel_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=k, value=k)
            for k in (config.get("channels", {}) or {})
            if current in k.lower()
        ][:25]

    @setup.command(name="role", description="Set a staff role tier")
    @app_commands.describe(tier="Permission tier", role="The role", add="Add to the tier instead of replacing it")
    @app_commands.choices(
        tier=[app_commands.Choice(name=t.title(), value=t) for t in ROLE_HINTS]
        + [
            app_commands.Choice(name="Verified", value="verified"),
            app_commands.Choice(name="Autoroles", value="autoroles"),
        ]
    )
    @require("admin")
    async def set_role(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        role: discord.Role,
        add: bool = False,
    ) -> None:
        # `verified` is a single ID; every other tier is a list.
        if tier.value == "verified":
            config.set("roles.verified", role.id)
            value = role.mention
        else:
            current = config.role_ids(tier.value) if add else []
            if role.id not in current:
                current.append(role.id)
            config.set(f"roles.{tier.value}", current)
            value = " ".join(f"<@&{r}>" for r in current)

        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"`roles.{tier.value}` → {value}"), ephemeral=True
        )

    @setup.command(name="link", description="Set a URL used by panel buttons")
    @app_commands.describe(key="Which link", url="The URL")
    @require("admin")
    async def set_link(self, interaction: discord.Interaction, key: str, url: str) -> None:
        known = config.get("links", {}) or {}
        if key not in known:
            await interaction.response.send_message(
                view=ui.err(f"Unknown link key `{key}`. "
                    f"Valid: {', '.join(f'`{k}`' for k in known)}"
                ),
                ephemeral=True,
            )
            return
        if not url.startswith(("http://", "https://")):
            await interaction.response.send_message(
                view=ui.err("That doesn't look like a URL — it must start with `https://`."),
                ephemeral=True,
            )
            return

        config.set(f"links.{key}", url)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"`links.{key}` set.\n"
                "-# Run `/panel reload` then repost the panel to pick it up."
            ),
            ephemeral=True,
        )

    @set_link.autocomplete("key")
    async def link_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=k, value=k)
            for k in (config.get("links", {}) or {})
            if current in k.lower()
        ][:25]

    @setup.command(name="guild", description="Bind the bot to this server")
    @require("admin")
    async def set_guild(self, interaction: discord.Interaction) -> None:
        config.set("guild_id", interaction.guild.id)
        config.save()
        await interaction.response.send_message(
            view=ui.ok(f"Bound to **{interaction.guild.name}**.\n"
                "-# Restart the bot so commands sync instantly to this server."
            ),
            ephemeral=True,
        )

    # -- inspection -------------------------------------------------------

    @setup.command(name="show", description="Show the current configuration")
    @app_commands.describe(section="Which part to show")
    @app_commands.choices(
        section=[
            app_commands.Choice(name="Ticket categories", value="tickets"),
            app_commands.Choice(name="Channels", value="channels"),
            app_commands.Choice(name="Roles", value="roles"),
            app_commands.Choice(name="Links", value="links"),
        ]
    )
    @require("admin")
    async def show(
        self, interaction: discord.Interaction, section: app_commands.Choice[str]
    ) -> None:
        guild = interaction.guild
        lines: list[str] = []

        if section.value == "tickets":
            for key, spec in config.ticket_categories().items():
                cid = spec.get("category_id")
                found = guild.get_channel(cid) if cid else None
                mark = ui.GREEN if found else ui.RED
                where = found.name if found else ("missing category" if cid else "not set")
                pings = spec.get("ping_roles") or []
                ping_text = f" · pings {' '.join(f'<@&{r}>' for r in pings)}" if pings else ""
                lines.append(f"{mark} `{key}` — {spec.get('label', key)} → {where}{ping_text}")

        elif section.value == "channels":
            for key in config.get("channels", {}) or {}:
                cid = config.channel_id(key)
                channel = guild.get_channel(cid) if cid else None
                mark = ui.GREEN if channel else ui.RED
                lines.append(
                    f"{mark} `{key}` → " + (channel.mention if channel else "not set")
                )

        elif section.value == "roles":
            for tier in ("admin", "hr", "support", "designer"):
                ids = config.role_ids(tier)
                mark = ui.GREEN if ids else ui.RED
                lines.append(
                    f"{mark} `{tier}` → "
                    + (" ".join(f"<@&{r}>" for r in ids) if ids else "not set")
                )
            verified = config.get("roles.verified")
            lines.append(
                f"{ui.GREEN if verified else ui.RED} `verified` → "
                + (f"<@&{verified}>" if verified else "not set")
            )

        else:
            for key, value in (config.get("links", {}) or {}).items():
                mark = ui.GREEN if value else ui.RED
                lines.append(f"{mark} `{key}` → " + (str(value) if value else "not set"))

        body = "\n".join(lines) or "Nothing configured in this section."
        # Long sections can exceed the 4000-char text limit.
        if len(body) > 3500:
            body = body[:3500] + "\n-# …truncated."

        await interaction.response.send_message(
            view=ui.panel(f"Config — {section.name}", body), ephemeral=True
        )


PREVIEW_VIEWS = [
    (
        "confirm-plan",
        lambda: ConfirmPlan(_demo_plan(), "Detected Configuration"),
    ),
]


def _demo_plan() -> Plan:
    plan = Plan()
    plan.add("tickets.categories.livery.category_id", 1, "**Livery Orders** → Livery Orders")
    plan.skip("channels.mod_log")
    return plan


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Setup(bot))
