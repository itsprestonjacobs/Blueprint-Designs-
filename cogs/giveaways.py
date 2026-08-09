"""Giveaways with persistent entry buttons.

Entrants are stored on disk keyed by giveaway ID, so a restart mid-giveaway
loses nothing and the entry button keeps working. A background loop draws
winners when the timer runs out.
"""

from __future__ import annotations

import logging
import random
import re
import time

import discord
from discord import app_commands, ui as dui
from discord.ext import commands, tasks

from core import ui
from core.perms import require
from core.store import giveaways as store

log = logging.getLogger("blueprint.giveaways")

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int | None:
    total = 0
    for amount, unit in DURATION_RE.findall(text or ""):
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total or None


def pick_winners(entrants: list[int], count: int) -> list[int]:
    """Draw up to `count` distinct winners."""
    pool = list(dict.fromkeys(entrants))
    if not pool:
        return []
    return random.sample(pool, k=min(count, len(pool)))


class EnterButton(dui.DynamicItem[dui.Button], template=r"bp:gw:enter:(?P<gid>\d+)"):
    def __init__(self, gid: int, entries: int = 0, ended: bool = False) -> None:
        super().__init__(
            dui.Button(
                label="Ended" if ended else f"Enter ({entries})",
                style=discord.ButtonStyle.secondary if ended else discord.ButtonStyle.success,
                emoji="🎉",
                custom_id=f"bp:gw:enter:{gid}",
                disabled=ended,
            )
        )
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["gid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        async with store.edit() as data:
            entry = (data.get("giveaways") or {}).get(str(self.gid))
            if entry is None or entry.get("status") != "running":
                await interaction.response.send_message(
                    view=ui.warn("That one's over."), ephemeral=True
                )
                return

            required = entry.get("required_role")
            if required and isinstance(interaction.user, discord.Member):
                if not any(r.id == required for r in interaction.user.roles):
                    await interaction.response.send_message(
                        view=ui.err(f"You need <@&{required}> to enter this one."),
                        ephemeral=True,
                    )
                    return

            entrants: list[int] = entry.setdefault("entrants", [])
            if interaction.user.id in entrants:
                entrants.remove(interaction.user.id)
                left = True
            else:
                entrants.append(interaction.user.id)
                left = False
            count = len(entrants)
            snapshot = dict(entry)

        await interaction.response.edit_message(
            view=GiveawayView(self.gid, snapshot, count),
            content=None,
            embeds=[],
            attachments=[],
        )
        await interaction.followup.send(
            view=ui.warn("You're out.") if left else ui.ok("You're in. Press again to leave."),
            ephemeral=True,
        )


class GiveawayView(ui.BaseLayout):
    def __init__(self, gid: int, entry: dict, entries: int = 0, ended: bool = False) -> None:
        super().__init__(timeout=None)

        ends = entry.get("ends", 0)
        body = [
            f"### {entry.get('prize', 'Prize')}",
        ]
        if entry.get("subtext"):
            body.append(entry["subtext"])
        body.append("")
        body.append(ui.field("Winners", entry.get("winners", 1)))
        body.append(
            ui.field("Ends", f"<t:{ends}:F> (<t:{ends}:R>)" if not ended else "now")
        )
        body.append(ui.field("Hosted by", f"<@{entry.get('host')}>"))
        if entry.get("required_role"):
            body.append(ui.field("Requirement", f"<@&{entry['required_role']}>"))

        children: list[dui.Item] = []
        if entry.get("image"):
            media = ui.gallery(entry["image"])
            if media:
                children.append(media)
        children.append(ui.text(f"## {entry.get('title', 'Giveaway')}\n" + "\n".join(body)))
        children.append(ui.separator())
        children.append(ui.row(EnterButton(gid, entries, ended)))

        self.add_item(ui.container(*children))
        self.validate()


@app_commands.guild_only()
class Giveaways(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(EnterButton)
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @tasks.loop(seconds=30)
    async def ticker(self) -> None:
        """Draw any giveaway whose timer has expired."""
        now = int(time.time())
        data = await store.read()
        due = [
            int(gid)
            for gid, e in (data.get("giveaways") or {}).items()
            if e.get("status") == "running" and e.get("ends", 0) <= now
        ]
        for gid in due:
            try:
                await self.finish(gid)
            except Exception:  # noqa: BLE001 - one bad giveaway shouldn't stop the loop
                log.exception("failed to end giveaway %s", gid)

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    async def finish(self, gid: int, reroll: bool = False) -> list[int]:
        """End a giveaway, announce winners, and return them."""
        async with store.edit() as data:
            entry = (data.get("giveaways") or {}).get(str(gid))
            if entry is None:
                return []
            entrants = entry.get("entrants", [])
            winners = pick_winners(entrants, int(entry.get("winners", 1)))
            entry["status"] = "ended"
            entry["winners_drawn"] = winners
            snapshot = dict(entry)

        channel = self.bot.get_channel(snapshot.get("channel", 0))
        if channel is None:
            return winners

        # Disable the entry button on the original message.
        try:
            message = await channel.fetch_message(snapshot["message"])
            await message.edit(
                view=GiveawayView(gid, snapshot, len(entrants), ended=True),
                content=None,
                embeds=[],
                attachments=[],
            )
        except (discord.NotFound, discord.HTTPException):
            message = None

        if winners:
            mentions = " ".join(f"<@{w}>" for w in winners)
            title = "Giveaway Rerolled" if reroll else "Giveaway Ended"
            await channel.send(
                mentions, allowed_mentions=discord.AllowedMentions(users=True)
            )
            await channel.send(
                view=ui.panel(
                    title,
                    "\n".join(
                        [
                            ui.field("Prize", snapshot.get("prize", "—")),
                            ui.field("Winner(s)", mentions),
                            ui.field("Entries", len(entrants)),
                        ]
                    ),
                    color=0x2ECC71,
                )
            )
        else:
            await channel.send(
                view=ui.panel(
                    "Giveaway Ended",
                    f"Nobody entered **{snapshot.get('prize', 'the giveaway')}**, so there's no winner.",
                    color=0xE74C3C,
                )
            )

        return winners

    giveaway = app_commands.Group(name="giveaway", description="Run giveaways")

    @giveaway.command(name="create", description="Create a giveaway")
    @app_commands.describe(
        title="Heading shown on the giveaway",
        prize="What someone wins",
        winners="How many winners to draw",
        duration="How long it runs, e.g. 1d, 12h, 30m",
        subtext="Extra line under the prize",
        required_role="Role needed to enter",
        image="Banner image for the giveaway",
    )
    @require("admin", "hr")
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        prize: str,
        winners: int,
        duration: str,
        subtext: str | None = None,
        required_role: discord.Role | None = None,
        image: discord.Attachment | None = None,
    ) -> None:
        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                view=ui.err(f"I couldn't read `{duration}`. Try `1d`, `12h` or `30m`."),
                ephemeral=True,
            )
            return
        if winners < 1:
            await interaction.response.send_message(
                view=ui.err("Need at least one winner."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        gid = await store.next_id("_counter")
        entry = {
            "id": gid,
            "title": title,
            "prize": prize,
            "subtext": subtext,
            "winners": winners,
            "ends": int(time.time()) + seconds,
            "host": interaction.user.id,
            "required_role": required_role.id if required_role else None,
            "image": image.url if image else None,
            "entrants": [],
            "status": "running",
            "channel": interaction.channel.id,
            "message": None,
        }

        message = await interaction.channel.send(view=GiveawayView(gid, entry))
        entry["message"] = message.id

        async with store.edit() as data:
            data.setdefault("giveaways", {})[str(gid)] = entry

        await interaction.followup.send(
            view=ui.ok(f"Giveaway **#{gid}** started — ends <t:{entry['ends']}:R>."),
            ephemeral=True,
        )

    @giveaway.command(name="end", description="End a giveaway early")
    @app_commands.describe(giveaway_id="The giveaway number")
    @require("admin", "hr")
    async def end(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        data = await store.read()
        entry = (data.get("giveaways") or {}).get(str(giveaway_id))
        if entry is None:
            await interaction.response.send_message(
                view=ui.err(f"No giveaway **#{giveaway_id}**."), ephemeral=True
            )
            return
        if entry.get("status") != "running":
            await interaction.response.send_message(
                view=ui.warn("Already ended."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        winners = await self.finish(giveaway_id)
        await interaction.followup.send(
            view=ui.ok(f"Ended **#{giveaway_id}**. "
                + (f"{len(winners)} winner(s) drawn." if winners else "nobody entered.")
            ),
            ephemeral=True,
        )

    @giveaway.command(name="reroll", description="Reroll winner(s) for an ended giveaway")
    @app_commands.describe(giveaway_id="The giveaway number", winners="How many to redraw")
    @require("admin", "hr")
    async def reroll(
        self, interaction: discord.Interaction, giveaway_id: int, winners: int = 1
    ) -> None:
        data = await store.read()
        entry = (data.get("giveaways") or {}).get(str(giveaway_id))
        if entry is None:
            await interaction.response.send_message(
                view=ui.err(f"No giveaway **#{giveaway_id}**."), ephemeral=True
            )
            return
        if entry.get("status") == "running":
            await interaction.response.send_message(
                view=ui.warn("Still running. End it first."),
                ephemeral=True,
            )
            return

        entrants = entry.get("entrants", [])
        previous = entry.get("winners_drawn", [])
        # Prefer people who haven't already won.
        pool = [e for e in entrants if e not in previous] or entrants
        drawn = pick_winners(pool, winners)

        if not drawn:
            await interaction.response.send_message(
                view=ui.err("Nobody entered that giveaway, so there's nothing to reroll."),
                ephemeral=True,
            )
            return

        async with store.edit() as d:
            target = (d.get("giveaways") or {}).get(str(giveaway_id))
            if target is not None:
                target.setdefault("winners_drawn", []).extend(drawn)

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = self.bot.get_channel(entry.get("channel", 0)) or interaction.channel
        mentions = " ".join(f"<@{w}>" for w in drawn)
        await channel.send(mentions, allowed_mentions=discord.AllowedMentions(users=True))
        await channel.send(
            view=ui.panel(
                "Giveaway Rerolled",
                "\n".join(
                    [
                        ui.field("Prize", entry.get("prize", "—")),
                        ui.field("New winner(s)", mentions),
                        ui.field("Rerolled by", interaction.user.mention),
                    ]
                ),
                color=0x2ECC71,
            )
        )
        await interaction.followup.send(
            view=ui.ok(f"Rerolled **#{giveaway_id}**."), ephemeral=True
        )

    @giveaway.command(name="list", description="Show running giveaways")
    @require("admin", "hr")
    async def list_giveaways(self, interaction: discord.Interaction) -> None:
        data = await store.read()
        running = [
            e for e in (data.get("giveaways") or {}).values() if e.get("status") == "running"
        ]
        if not running:
            body = "Nothing running."
        else:
            body = "\n".join(
                f"`#{e['id']}` **{e.get('prize', '—')}** — "
                f"{len(e.get('entrants', []))} entries, ends <t:{e.get('ends', 0)}:R>"
                for e in running[:20]
            )
        await interaction.response.send_message(
            view=ui.panel("Running Giveaways", body), ephemeral=True
        )


PREVIEW_VIEWS = [
    (
        "giveaway",
        lambda: GiveawayView(
            1,
            {
                "title": "Blueprint Giveaway",
                "prize": "500 Robux",
                "subtext": "Good luck!",
                "winners": 2,
                "ends": 1800000000,
                "host": 1,
            },
            entries=12,
        ),
    ),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))
