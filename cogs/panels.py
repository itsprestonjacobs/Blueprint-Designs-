"""Data-driven information panels.

Each file in panels/*.json defines one panel: heading, body, link buttons and an
optional dropdown whose options each carry their own content. Picking an option
shows that content privately, so the panel itself never changes for anyone else.

Adding or rewording a panel is a JSON edit -- no code change needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import discord
from discord import app_commands, ui as dui
from discord.ext import commands

from core import ui
from core.config import config
from core.perms import require

log = logging.getLogger("blueprint.panels")

PANELS_DIR = Path(__file__).resolve().parent.parent / "panels"

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
}


def load_panels() -> dict[str, dict]:
    """Read every panel definition off disk."""
    panels: dict[str, dict] = {}
    if not PANELS_DIR.exists():
        return panels

    for path in sorted(PANELS_DIR.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                panels[path.stem] = json.load(f)
        except (json.JSONDecodeError, OSError):
            log.exception("could not read panel %s", path.name)
    return panels


def resolve_url(raw: str | None) -> str | None:
    """Resolve a button URL, following `config:` references.

    Panels reference links as `"config:links.roblox_group"` so every URL in the
    server lives in config.json rather than being scattered across panel files.
    """
    if not raw:
        return None
    if raw.startswith("config:"):
        return config.get(raw[len("config:") :]) or None
    return raw


def build_buttons(spec: list[dict]) -> list[dui.Button]:
    """Turn button JSON into components, skipping any that are unusable.

    A link button whose URL isn't configured yet is dropped rather than raising
    -- an unfinished config shouldn't stop the whole panel from posting.
    """
    buttons: list[dui.Button] = []
    for entry in spec:
        style = BUTTON_STYLES.get(str(entry.get("style", "link")).lower(), discord.ButtonStyle.link)
        url = resolve_url(entry.get("url"))

        if style is discord.ButtonStyle.link:
            if not url:
                log.warning(
                    "panel button %r has no usable url (%r); skipping",
                    entry.get("label"),
                    entry.get("url"),
                )
                continue
            buttons.append(
                dui.Button(
                    label=entry.get("label", "Open")[:80],
                    url=url,
                    emoji=entry.get("emoji") or None,
                    style=discord.ButtonStyle.link,
                )
            )
        else:
            buttons.append(
                dui.Button(
                    label=entry.get("label", "Button")[:80],
                    style=style,
                    emoji=entry.get("emoji") or None,
                    custom_id=entry.get("custom_id") or f"bp:panel:btn:{entry.get('label', 'x')}",
                )
            )
    return buttons


class InfoPanel(ui.BaseLayout):
    """One panel rendered from its JSON definition."""

    def __init__(self, name: str, spec: dict) -> None:
        super().__init__(timeout=None)
        self.name = name
        self.spec = spec

        children: list[dui.Item] = []

        banner_url, banner_file = ui.resolve_media(
            spec.get("banner", config.get("branding.banner_url"))
        )
        if banner_url:
            media = ui.gallery(banner_url)
            if media:
                children.append(media)
                self.track(banner_file)

        heading = []
        if spec.get("title"):
            heading.append(f"## {spec['title']}")
        if spec.get("body"):
            heading.append(spec["body"])
        if heading:
            children.append(ui.text("\n".join(heading)))

        buttons = build_buttons(spec.get("buttons", []))
        if buttons:
            children.append(ui.separator())
            # Discord allows at most 5 buttons per row.
            for i in range(0, len(buttons), 5):
                children.append(ui.row(*buttons[i : i + 5]))

        options_spec = spec.get("select", {}).get("options", [])
        if options_spec:
            options = ui.check_options(
                [
                    discord.SelectOption(
                        label=o.get("label", "Option")[:100],
                        value=o.get("value", o.get("label", "option"))[:100],
                        description=(o.get("description") or None),
                        emoji=o.get("emoji") or None,
                    )
                    for o in options_spec
                ],
                f"panels/{name}.json select",
            )
            select = dui.Select(
                custom_id=f"bp:panel:{name}",
                placeholder=spec.get("select", {}).get("placeholder", "Select an option")[:150],
                options=options,
            )
            select.callback = self._on_select
            if not buttons:
                children.append(ui.separator())
            children.append(ui.row(select))

        if spec.get("footer"):
            children.append(ui.separator())
            children.append(ui.text(f"-# {spec['footer']}"))

        self.add_item(ui.container(*children, color=parse_color(spec.get("color"))))
        self.validate()

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = interaction.data["values"][0]  # type: ignore[index]
        for option in self.spec.get("select", {}).get("options", []):
            if option.get("value", option.get("label")) == value:
                await interaction.response.send_message(
                    view=ui.panel(
                        option.get("title", option.get("label", "Information")),
                        option.get("content", "_No content set for this option yet._"),
                        banner=option.get("banner"),
                        color=parse_color(option.get("color")),
                    ),
                    ephemeral=True,
                )
                return

        await interaction.response.send_message(
            view=ui.err("That option is gone."), ephemeral=True
        )


def parse_color(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 16 if str(raw).lower().startswith("0x") else 10)
    except ValueError:
        return None


@app_commands.guild_only()
class Panels(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panels = load_panels()

    async def cog_load(self) -> None:
        # Re-register every panel so its dropdown keeps working after a restart.
        for name, spec in self.panels.items():
            try:
                self.bot.add_view(InfoPanel(name, spec))
            except Exception:  # noqa: BLE001
                log.exception("could not register panel %s", name)

    panel = app_commands.Group(name="panel", description="Post information panels")

    @panel.command(name="send", description="Post a panel in this channel")
    @app_commands.describe(name="Which panel to post")
    @require("admin", "hr")
    async def send(self, interaction: discord.Interaction, name: str) -> None:
        spec = self.panels.get(name)
        if spec is None:
            available = ", ".join(f"`{n}`" for n in self.panels) or "none"
            await interaction.response.send_message(
                view=ui.err(f"No panel called `{name}`. Available: {available}"),
                ephemeral=True,
            )
            return

        try:
            view = InfoPanel(name, spec)
        except ui.LimitError as exc:
            await interaction.response.send_message(
                view=ui.err(f"`{name}` is too large to send:\n```\n{exc}\n```"),
                ephemeral=True,
            )
            return

        await ui.send_panel(interaction.channel, view)
        await interaction.response.send_message(
            view=ui.ok(f"Posted **{name}**."), ephemeral=True
        )

    @send.autocomplete("name")
    async def panel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=n, value=n) for n in self.panels if current in n.lower()
        ][:25]

    @panel.command(name="list", description="List available panels")
    @require("admin", "hr")
    async def list_panels(self, interaction: discord.Interaction) -> None:
        if not self.panels:
            body = "No panel files found in `panels/`."
        else:
            body = "\n".join(
                f"- `{name}` — {spec.get('title', 'untitled')}"
                for name, spec in self.panels.items()
            )
        await interaction.response.send_message(
            view=ui.panel("Panels", body), ephemeral=True
        )

    @panel.command(name="reload", description="Re-read panel files from disk")
    @require("admin")
    async def reload_panels(self, interaction: discord.Interaction) -> None:
        self.panels = load_panels()
        await interaction.response.send_message(
            view=ui.ok(f"Reloaded **{len(self.panels)}** panel(s)."),
            ephemeral=True,
        )


def _preview_all():
    return [
        (name, (lambda s=spec, n=name: InfoPanel(n, s)))
        for name, spec in load_panels().items()
    ]


PREVIEW_VIEWS = _preview_all()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Panels(bot))
