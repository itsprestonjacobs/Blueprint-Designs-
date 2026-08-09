"""Customer reviews and vouches."""

from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from core.logs import get_channel
from core.store import reviews as store

MAX_STARS = 5


def stars(score: int) -> str:
    return "★" * score + "☆" * (MAX_STARS - score)


class Reviews(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="review", description="Leave a review for your order")
    @app_commands.describe(
        designer="Who worked on your order",
        score="Rating out of 5",
        comment="What you thought of the work",
        image="A screenshot of what you received",
    )
    @app_commands.choices(
        score=[
            app_commands.Choice(name=f"{n} — {stars(n)}", value=n)
            for n in range(MAX_STARS, 0, -1)
        ]
    )
    async def review(
        self,
        interaction: discord.Interaction,
        designer: discord.Member,
        score: app_commands.Choice[int],
        comment: str,
        image: discord.Attachment | None = None,
    ) -> None:
        if designer.id == interaction.user.id:
            await interaction.response.send_message(
                view=ui.err("Can't review yourself."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        rid = await store.next_id("_counter")
        async with store.edit() as data:
            data.setdefault("reviews", {})[str(rid)] = {
                "id": rid,
                "reviewer": interaction.user.id,
                "designer": designer.id,
                "score": score.value,
                "comment": comment,
                "image": image.url if image else None,
                "at": int(time.time()),
            }

        body = "\n".join(
            [
                ui.field("Designer", designer.mention),
                ui.field("Reviewer", f"{interaction.user.mention} (`{interaction.user.id}`)"),
                ui.field("Score", f"{score.value}/5 {stars(score.value)}"),
                "",
                comment,
            ]
        )

        view = ui.panel(
            "New review posted",
            body,
            banner=image.url if image else None,
            color=0x2ECC71 if score.value >= 4 else (0xF1C40F if score.value == 3 else 0xE74C3C),
        )

        channel = await get_channel(self.bot, "review_channel")
        if channel is not None:
            await channel.send(view=view)

        await interaction.followup.send(
            view=ui.ok(f"Review of {designer.mention} posted."
                + ("" if channel else "\n-# No review channel set, so it wasn't posted publicly.")
            ),
            ephemeral=True,
        )

    @app_commands.command(name="reviews", description="See a designer's reviews")
    async def list_reviews(
        self, interaction: discord.Interaction, designer: discord.Member
    ) -> None:
        data = await store.read()
        found = [
            r for r in (data.get("reviews") or {}).values() if r.get("designer") == designer.id
        ]

        if not found:
            await interaction.response.send_message(
                view=ui.panel(
                    f"Reviews — {designer.display_name}",
                    "No reviews yet.",
                ),
                ephemeral=True,
            )
            return

        found.sort(key=lambda r: r.get("at", 0), reverse=True)
        average = sum(r.get("score", 0) for r in found) / len(found)

        lines = [
            f"{stars(r.get('score', 0))} — {r.get('comment', '')[:120]} "
            f"(<@{r['reviewer']}>, <t:{r.get('at', 0)}:d>)"
            for r in found[:10]
        ]
        body = (
            f"**{average:.1f}/5** from **{len(found)}** review(s)\n\n" + "\n".join(lines)
        )
        if len(found) > 10:
            body += f"\n-# …and {len(found) - 10} more."

        await interaction.response.send_message(
            view=ui.panel(f"Reviews — {designer.display_name}", body), ephemeral=True
        )


PREVIEW_VIEWS = [
    (
        "review",
        lambda: ui.panel(
            "New review posted",
            f"**Designer:** @someone\n**Score:** 5/5 {stars(5)}\n\nGreat work, fast delivery.",
            color=0x2ECC71,
        ),
    ),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reviews(bot))
