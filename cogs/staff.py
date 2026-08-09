"""Staff management: infractions, promotions, LOA, activity checks, QC.

Every action posts a V2 log to its configured channel and DMs the affected
member where that makes sense. A missing log channel never blocks the command.
"""

from __future__ import annotations

import logging
import re
import time

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.logs import get_channel, send_log
from core.perms import has_tier, require
from core.store import activity as activity_store
from core.store import infractions as infraction_store
from core.store import loa as loa_store
from core.store import qc as qc_store

log = logging.getLogger("blueprint.staff")

INFRACTION_TYPES = ("Warning", "Strike", "Suspension", "Demotion", "Termination")
INFRACTION_ICON = {
    "Warning": ui.YELLOW,
    "Strike": ui.YELLOW,
    "Suspension": ui.RED,
    "Demotion": ui.RED,
    "Termination": ui.RED,
}

DURATION_RE = re.compile(r"(\d+)\s*([dhwm])", re.IGNORECASE)
UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}


def parse_duration(text: str) -> int | None:
    """Parse '3d', '2w', '12h' into seconds. None if nothing parses."""
    total = 0
    for amount, unit in DURATION_RE.findall(text or ""):
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total or None


async def try_dm(user: discord.abc.User, view: ui.BaseLayout) -> bool:
    try:
        await user.send(view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# -- LOA approval controls ------------------------------------------------


class LoaDecision(dui.DynamicItem[dui.Button], template=r"bp:loa:(?P<action>approve|deny):(?P<lid>\d+)"):
    def __init__(self, action: str, lid: int) -> None:
        approve = action == "approve"
        super().__init__(
            dui.Button(
                label="Approve" if approve else "Deny",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                emoji="✅" if approve else "❌",
                custom_id=f"bp:loa:{action}:{lid}",
            )
        )
        self.action = action
        self.lid = lid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["lid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not has_tier(interaction.user, "hr"):
            await interaction.response.send_message(
                view=ui.err("HR only."), ephemeral=True
            )
            return

        async with loa_store.edit() as data:
            entry = (data.get("requests") or {}).get(str(self.lid))
            if entry is None:
                await interaction.response.send_message(
                    view=ui.err("That request is gone."), ephemeral=True
                )
                return
            if entry.get("status") != "pending":
                await interaction.response.send_message(
                    view=ui.warn(f"Already {entry['status']}."), ephemeral=True
                )
                return
            entry["status"] = "approved" if self.action == "approve" else "denied"
            entry["decided_by"] = interaction.user.id
            entry["decided_at"] = int(time.time())
            requester_id = entry["user"]
            reason = entry.get("reason", "")
            until = entry.get("until")

        approved = self.action == "approve"
        icon = ui.GREEN if approved else ui.RED
        verdict = "approved" if approved else "denied"

        decision = ui.panel(
            f"LOA {verdict.title()}",
            "\n".join(
                [
                    ui.field("Member", f"<@{requester_id}>"),
                    ui.field("Decided by", interaction.user.mention),
                    ui.field("Returns", f"<t:{until}:D>" if until else "unspecified"),
                    ui.field("Reason", reason or "—"),
                ]
            ),
        )

        await interaction.response.edit_message(
            view=decision, content=None, embeds=[], attachments=[]
        )

        user = interaction.client.get_user(requester_id)
        if user is None:
            try:
                user = await interaction.client.fetch_user(requester_id)
            except discord.HTTPException:
                user = None
        if user is not None:
            await try_dm(
                user,
                ui.notice(
                    f"{icon} Your leave of absence was **{verdict}** by {interaction.user}.",
                    title="Leave of Absence",
                ),
            )


class LoaReview(ui.BaseLayout):
    def __init__(self, lid: int, body: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            ui.container(
                ui.text(f"## Leave of Absence Request\n{body}"),
                ui.separator(),
                ui.row(LoaDecision("approve", lid), LoaDecision("deny", lid)),
            )
        )


# -- activity check -------------------------------------------------------


class ActivityRespond(dui.DynamicItem[dui.Button], template=r"bp:activity:(?P<aid>\d+)"):
    def __init__(self, aid: int, count: int = 0) -> None:
        super().__init__(
            dui.Button(
                label=f"I'm active ({count})",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"bp:activity:{aid}",
            )
        )
        self.aid = aid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        async with activity_store.edit() as data:
            entry = (data.get("checks") or {}).get(str(self.aid))
            if entry is None:
                await interaction.response.send_message(
                    view=ui.err("That check has closed."), ephemeral=True
                )
                return

            responders: list[int] = entry.setdefault("responders", [])
            if interaction.user.id in responders:
                await interaction.response.send_message(
                    view=ui.warn("Already got you down."), ephemeral=True
                )
                return
            responders.append(interaction.user.id)
            count = len(responders)
            body = entry.get("body", "")

        await interaction.response.edit_message(
            view=ActivityCheck(self.aid, body, count),
            content=None,
            embeds=[],
            attachments=[],
        )
        await interaction.followup.send(
            view=ui.ok("Marked active."), ephemeral=True
        )


class ActivityCheck(ui.BaseLayout):
    def __init__(self, aid: int, body: str, count: int = 0) -> None:
        super().__init__(timeout=None)
        self.add_item(
            ui.container(
                ui.text(f"## Activity Check\n{body}"),
                ui.separator(),
                ui.row(ActivityRespond(aid, count)),
            )
        )


# -- quality control ------------------------------------------------------


class QcDecision(dui.DynamicItem[dui.Button], template=r"bp:qc:(?P<action>pass|fail):(?P<qid>\d+)"):
    def __init__(self, action: str, qid: int) -> None:
        passed = action == "pass"
        super().__init__(
            dui.Button(
                label="Approve" if passed else "Request changes",
                style=discord.ButtonStyle.success if passed else discord.ButtonStyle.danger,
                emoji="✅" if passed else "🔁",
                custom_id=f"bp:qc:{action}:{qid}",
            )
        )
        self.action = action
        self.qid = qid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["qid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not has_tier(interaction.user, "hr", "support"):
            await interaction.response.send_message(
                view=ui.err("Reviewers only."), ephemeral=True
            )
            return

        async with qc_store.edit() as data:
            entry = (data.get("submissions") or {}).get(str(self.qid))
            if entry is None:
                await interaction.response.send_message(
                    view=ui.err("That submission is gone."), ephemeral=True
                )
                return
            entry["status"] = "approved" if self.action == "pass" else "changes requested"
            entry["reviewer"] = interaction.user.id
            submitter = entry["user"]
            image = entry.get("image")
            note = entry.get("note")

        passed = self.action == "pass"
        view = ui.panel(
            "Quality Control — " + ("Approved" if passed else "Changes Requested"),
            "\n".join(
                [
                    ui.field("Designer", f"<@{submitter}>"),
                    ui.field("Reviewer", interaction.user.mention),
                    ui.field("Notes", note or "—"),
                ]
            ),
            banner=image,
            color=0x2ECC71 if passed else 0xE74C3C,
        )
        await interaction.response.edit_message(
            view=view, content=None, embeds=[], attachments=[]
        )

        user = interaction.client.get_user(submitter)
        if user is not None:
            await try_dm(
                user,
                ui.notice(
                    f"{ui.GREEN if passed else ui.YELLOW} Your QC submission was "
                    f"**{'approved' if passed else 'sent back for changes'}** by {interaction.user}.",
                    title="Quality Control",
                ),
            )


class QcReview(ui.BaseLayout):
    def __init__(self, qid: int, body: str, image: str | None) -> None:
        super().__init__(timeout=None)
        children = []
        if image:
            media = ui.gallery(image)
            if media:
                children.append(media)
        children.append(ui.text(f"## Quality Control Review\n{body}"))
        children.append(ui.separator())
        children.append(ui.row(QcDecision("pass", qid), QcDecision("fail", qid)))
        self.add_item(ui.container(*children))


# -- cog ------------------------------------------------------------------


@app_commands.guild_only()
class Staff(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(LoaDecision, ActivityRespond, QcDecision)

    # -- infractions ------------------------------------------------------

    @app_commands.command(name="infraction", description="Issue an infraction")
    @app_commands.describe(
        member="Who is being disciplined",
        type="Severity of the infraction",
        reason="Why this is being issued",
        notes="Extra context for the log",
    )
    @app_commands.choices(
        type=[app_commands.Choice(name=t, value=t) for t in INFRACTION_TYPES]
    )
    @require("hr")
    async def infraction(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        type: app_commands.Choice[str],
        reason: str,
        notes: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        number = await infraction_store.next_id("_counter")
        async with infraction_store.edit() as data:
            data.setdefault("records", {})[str(number)] = {
                "number": number,
                "user": member.id,
                "type": type.value,
                "reason": reason,
                "notes": notes,
                "issued_by": interaction.user.id,
                "at": int(time.time()),
            }

        body = [
            ui.field("Member", f"{member.mention} (`{member.id}`)"),
            ui.field("Type", f"{INFRACTION_ICON.get(type.value, '')} {type.value}"),
            ui.field("Issued by", interaction.user.mention),
            ui.field("Reason", reason),
        ]
        if notes:
            body.append(ui.field("Notes", notes))

        view = ui.panel(f"Infraction #{number}", "\n".join(body), color=0xE74C3C)
        await send_log(self.bot, "infraction_log", view)

        dmed = await try_dm(
            member,
            ui.panel(
                f"You received a {type.value.lower()}",
                "\n".join([ui.field("Reason", reason), ui.field("Issued by", str(interaction.user))]),
                color=0xE74C3C,
            ),
        )

        await interaction.followup.send(
            view=ui.ok(f"Infraction **#{number}** issued to {member.mention}."
                + ("" if dmed else "\n-# Could not DM them — their DMs are closed.")
            ),
            ephemeral=True,
        )

    @app_commands.command(name="infractions", description="List a member's infractions")
    @require("hr")
    async def infraction_list(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        data = await infraction_store.read()
        records = [
            r for r in (data.get("records") or {}).values() if r.get("user") == member.id
        ]
        records.sort(key=lambda r: r.get("at", 0), reverse=True)

        if not records:
            body = f"{member.mention} has a clean record."
        else:
            lines = [
                f"`#{r['number']}` **{r['type']}** — {r['reason']} "
                f"(<t:{r.get('at', 0)}:d>, by <@{r['issued_by']}>)"
                for r in records[:15]
            ]
            body = f"**{len(records)}** total\n\n" + "\n".join(lines)
            if len(records) > 15:
                body += f"\n-# …and {len(records) - 15} older."

        await interaction.response.send_message(
            view=ui.panel(f"Infractions — {member.display_name}", body), ephemeral=True
        )

    # -- promotions -------------------------------------------------------

    @app_commands.command(name="promotion", description="Issue a promotion")
    @app_commands.describe(
        member="Who is being promoted",
        new_rank="Their new rank",
        old_rank="Their previous rank",
        reason="Why they earned it",
        role="Role to grant them",
    )
    @require("hr")
    async def promotion(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        new_rank: str,
        old_rank: str | None = None,
        reason: str | None = None,
        role: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        granted = ""
        if role is not None:
            try:
                await member.add_roles(role, reason=f"Promotion by {interaction.user}")
                granted = f"\n-# Granted {role.mention}."
            except discord.Forbidden:
                granted = f"\n-# Couldn't grant {role.mention}, my role needs to sit above it."

        body = [
            ui.field("Member", member.mention),
            ui.field("New rank", f"**{new_rank}**"),
        ]
        if old_rank:
            body.insert(1, ui.field("Previous rank", old_rank))
        body.append(ui.field("Promoted by", interaction.user.mention))
        if reason:
            body.append(ui.field("Reason", reason))

        view = ui.panel("Promotion", "\n".join(body), color=0x2ECC71)
        await send_log(self.bot, "promotion_log", view)
        await try_dm(
            member,
            ui.panel(
                "You've been promoted!",
                f"Congratulations — you are now **{new_rank}**.",
                color=0x2ECC71,
            ),
        )

        await interaction.followup.send(
            view=ui.ok(f"Promoted {member.mention} to **{new_rank}**.{granted}"),
            ephemeral=True,
        )

    # -- leave of absence -------------------------------------------------

    loa = app_commands.Group(name="loa", description="Leave of absence")

    @loa.command(name="request", description="Request a leave of absence")
    @app_commands.describe(duration="How long, e.g. 5d or 2w", reason="Why you need the time")
    async def loa_request(
        self, interaction: discord.Interaction, duration: str, reason: str
    ) -> None:
        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                view=ui.err(f"I couldn't read `{duration}`. Use formats like `5d`, `2w`, `12h`."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        until = int(time.time()) + seconds
        lid = await loa_store.next_id("_counter")
        async with loa_store.edit() as data:
            data.setdefault("requests", {})[str(lid)] = {
                "id": lid,
                "user": interaction.user.id,
                "reason": reason,
                "duration": duration,
                "until": until,
                "status": "pending",
                "at": int(time.time()),
            }

        body = "\n".join(
            [
                ui.field("Member", f"{interaction.user.mention} (`{interaction.user.id}`)"),
                ui.field("Duration", duration),
                ui.field("Returns", f"<t:{until}:D> (<t:{until}:R>)"),
                ui.field("Reason", reason),
            ]
        )

        channel = await get_channel(self.bot, "loa_log")
        if channel is None:
            await interaction.followup.send(
                view=ui.warn("Request saved, but `channels.loa_log` isn't configured "
                    "so HR wasn't notified."
                ),
                ephemeral=True,
            )
            return

        await channel.send(view=LoaReview(lid, body))
        await interaction.followup.send(
            view=ui.ok(f"LOA request **#{lid}** submitted for review."),
            ephemeral=True,
        )

    @loa.command(name="end", description="End your leave of absence early")
    async def loa_end(self, interaction: discord.Interaction) -> None:
        async with loa_store.edit() as data:
            mine = [
                e
                for e in (data.get("requests") or {}).values()
                if e.get("user") == interaction.user.id and e.get("status") == "approved"
            ]
            if not mine:
                await interaction.response.send_message(
                    view=ui.warn("You're not on leave."),
                    ephemeral=True,
                )
                return
            for entry in mine:
                entry["status"] = "ended"
                entry["ended_at"] = int(time.time())

        await send_log(
            self.bot,
            "loa_log",
            ui.panel(
                "Leave Ended",
                f"{interaction.user.mention} has returned from leave early.",
            ),
        )
        await interaction.response.send_message(
            view=ui.ok("Welcome back."), ephemeral=True
        )

    @loa.command(name="list", description="Show everyone currently on leave")
    @require("hr")
    async def loa_list(self, interaction: discord.Interaction) -> None:
        data = await loa_store.read()
        active = [
            e for e in (data.get("requests") or {}).values() if e.get("status") == "approved"
        ]
        if not active:
            body = "Nobody's on leave."
        else:
            body = "\n".join(
                f"<@{e['user']}> — returns <t:{e.get('until', 0)}:R> · {e.get('reason', '—')}"
                for e in active[:20]
            )
        await interaction.response.send_message(
            view=ui.panel("Active Leave", body), ephemeral=True
        )

    # -- activity check ---------------------------------------------------

    @app_commands.command(name="activitycheck", description="Start an activity check for a role")
    @app_commands.describe(
        role="Role being checked",
        duration="How long members have to respond, e.g. 2d",
        requirement="What members must do to pass",
    )
    @require("hr")
    async def activity_check(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        duration: str,
        requirement: str | None = None,
    ) -> None:
        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                view=ui.err(f"I couldn't read `{duration}`. Try `2d` or `12h`."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        ends = int(time.time()) + seconds
        aid = await activity_store.next_id("_counter")

        body = "\n".join(
            [
                ui.field("Role", role.mention),
                ui.field("Ends", f"<t:{ends}:F> (<t:{ends}:R>)"),
                ui.field("Requirement", requirement or "Hit the button so we know you're around."),
                ui.field("Started by", interaction.user.mention),
            ]
        )

        async with activity_store.edit() as data:
            data.setdefault("checks", {})[str(aid)] = {
                "id": aid,
                "role": role.id,
                "ends": ends,
                "body": body,
                "responders": [],
                "started_by": interaction.user.id,
            }

        target = await get_channel(self.bot, "activity_check") or interaction.channel
        await target.send(
            role.mention, allowed_mentions=discord.AllowedMentions(roles=True)
        )
        await target.send(view=ActivityCheck(aid, body))

        await interaction.followup.send(
            view=ui.ok(f"Activity check **#{aid}** started in {target.mention}."),
            ephemeral=True,
        )

    @app_commands.command(name="activityresults", description="Who responded to an activity check")
    @app_commands.describe(check_id="The activity check number")
    @require("hr")
    async def activity_results(self, interaction: discord.Interaction, check_id: int) -> None:
        data = await activity_store.read()
        entry = (data.get("checks") or {}).get(str(check_id))
        if entry is None:
            await interaction.response.send_message(
                view=ui.err(f"No activity check **#{check_id}**."), ephemeral=True
            )
            return

        responders = entry.get("responders", [])
        role = interaction.guild.get_role(entry["role"]) if interaction.guild else None
        expected = [m.id for m in role.members] if role else []
        missing = [uid for uid in expected if uid not in responders]

        body = [
            ui.field("Role", role.mention if role else "deleted role"),
            ui.field("Responded", f"{len(responders)}/{len(expected) or '?'}"),
        ]
        if missing:
            shown = " ".join(f"<@{uid}>" for uid in missing[:30])
            body.append(f"\n**Did not respond:**\n{shown}")
            if len(missing) > 30:
                body.append(f"-# …and {len(missing) - 30} more.")

        await interaction.response.send_message(
            view=ui.panel(f"Activity Check #{check_id}", "\n".join(body)), ephemeral=True
        )

    # -- quality control --------------------------------------------------

    @app_commands.command(name="qc", description="Submit work for quality control review")
    @app_commands.describe(image="The work to review", note="Anything the reviewer should know")
    @require("designer", "support", "hr")
    async def quality_control(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        note: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        qid = await qc_store.next_id("_counter")
        async with qc_store.edit() as data:
            data.setdefault("submissions", {})[str(qid)] = {
                "id": qid,
                "user": interaction.user.id,
                "image": image.url,
                "note": note,
                "status": "pending",
                "at": int(time.time()),
            }

        body = "\n".join(
            [
                ui.field("Submission", f"#{qid}"),
                ui.field("Designer", interaction.user.mention),
                ui.field("Notes", note or "—"),
            ]
        )

        channel = await get_channel(self.bot, "quality_control")
        if channel is None:
            await interaction.followup.send(
                view=ui.warn("Saved, but `channels.quality_control` isn't configured."
                ),
                ephemeral=True,
            )
            return

        await channel.send(view=QcReview(qid, body, image.url))
        await interaction.followup.send(
            view=ui.ok(f"Submitted **#{qid}** for review."), ephemeral=True
        )


PREVIEW_VIEWS = [
    ("loa-review", lambda: LoaReview(1, "**Member:** @someone\n**Duration:** 5d")),
    ("activity-check", lambda: ActivityCheck(1, "**Role:** @Designers\n**Ends:** soon")),
    ("qc-review", lambda: QcReview(1, "**Designer:** @someone", None)),
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Staff(bot))
