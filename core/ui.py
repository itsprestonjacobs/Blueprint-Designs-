"""Components V2 building blocks.

Every panel in the bot is assembled here so branding stays consistent and the
platform limits are enforced in exactly one place.

Things the Discord API will reject, which this module guards against:
  * more than MAX_COMPONENTS components in a single message
  * more than MAX_CONTENT characters of text across all TextDisplays
  * more than MAX_SELECT_OPTIONS options in a select menu
  * mixing `content=` / `embeds=` with a V2 message (not allowed at all)
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import ui

from .config import config

log = logging.getLogger("blueprint.ui")

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "assets"

MAX_COMPONENTS = 40
MAX_CONTENT = 4000
MAX_SELECT_OPTIONS = 25

# Status pills reused by the order status board and ticket states.
GREEN = "🟢"
RED = "🔴"
YELLOW = "🟡"

# --- status badges -------------------------------------------------------
#
# Discord renders a subset of ANSI inside ```ansi code blocks, which is the only
# way to get genuinely coloured text in a message. Foreground codes 30-37 work
# reliably; the background codes use a Solarized palette that has no true green,
# so coloured *text* reads better than a filled badge.
#
# `branding.status_style` picks between:
#   "ansi"   - coloured text in a code block (default; no setup needed)
#   "emoji"  - your own uploaded badge emoji, set under `emoji.*` in config
#   "circles"- plain 🟢/🔴 unicode circles

ANSI_RESET = "[0m"
ANSI_GREEN = "[1;32m"
ANSI_RED = "[1;31m"
ANSI_YELLOW = "[1;33m"
ANSI_GRAY = "[0;30m"
ANSI_WHITE = "[0;37m"

STATE_COLORS = {
    "OPENED": ANSI_GREEN,
    "OPEN": ANSI_GREEN,
    "CLOSED": ANSI_RED,
    "LIMITED": ANSI_YELLOW,
}

STATE_CIRCLES = {
    "OPENED": GREEN,
    "OPEN": GREEN,
    "CLOSED": RED,
    "LIMITED": YELLOW,
}


def status_style() -> str:
    return str(config.get("branding.status_style", "ansi")).lower()


def status_emoji(state: str) -> str:
    """Custom badge emoji for a state, falling back to a circle."""
    key = state.lower()
    custom = config.get(f"emoji.{key}")
    return custom or STATE_CIRCLES.get(state.upper(), YELLOW)


def status_block(statuses: dict[str, str]) -> str:
    """Render a service -> state mapping as an aligned, coloured status list."""
    if not statuses:
        return "_No services configured._"

    style = status_style()

    if style == "ansi":
        width = max(len(name) for name in statuses)
        lines = []
        for name, state in statuses.items():
            state_text = str(state).upper()
            color = STATE_COLORS.get(state_text, ANSI_YELLOW)
            lines.append(
                f"{ANSI_WHITE}{name.ljust(width)}{ANSI_RESET}  "
                f"{color}{state_text}{ANSI_RESET}"
            )
        return "```ansi\n" + "\n".join(lines) + "\n```"

    marker = status_emoji if style == "emoji" else (
        lambda s: STATE_CIRCLES.get(s.upper(), YELLOW)
    )
    return "\n".join(
        f"{marker(str(state))} **{name}** — `{str(state).upper()}`"
        for name, state in statuses.items()
    )


def accent() -> int:
    return config.accent


class LimitError(RuntimeError):
    """Raised at build time when a view would exceed Discord's limits."""


def resolve_media(ref: str | None) -> tuple[str | None, Path | None]:
    """Turn a banner reference into (url, local file to upload).

    A plain URL is used as-is. Anything else is treated as a path relative to
    the project root (e.g. `assets/blueprint.png`) and referenced as
    `attachment://name` -- Discord's own CDN links are signed and expire after
    24 hours, so local assets are the only way to make a banner permanent.
    """
    if not ref:
        return None, None

    if ref.startswith(("http://", "https://")):
        return ref, None

    path = Path(ref)
    if not path.is_absolute():
        path = ROOT / path

    if not path.is_file():
        log.warning("banner %r not found at %s; skipping it", ref, path)
        return None, None

    return f"attachment://{path.name}", path


class BaseLayout(ui.LayoutView):
    """LayoutView with limit checking and a friendly error handler.

    Subclasses (and the helpers below) should call `validate()` once the view is
    fully assembled -- it turns a confusing 400 from the API into a clear error
    pointing at the panel that overflowed.

    `assets` holds any local images the view references. Send the view with
    `files=view.files()` so they actually get uploaded.
    """

    def __init__(self, *, timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)
        self.assets: list[Path] = []

    def track(self, path: Path | None) -> None:
        if path is not None and path not in self.assets:
            self.assets.append(path)

    def files(self) -> list[discord.File]:
        """Fresh File objects for this view's local images.

        Built on demand because a File's handle is consumed once sent, so a
        view posted twice needs two sets.
        """
        return [discord.File(p, filename=p.name) for p in self.assets]

    def validate(self) -> "BaseLayout":
        count = self.total_children_count
        if count > MAX_COMPONENTS:
            raise LimitError(
                f"{type(self).__name__}: {count} components exceeds the "
                f"{MAX_COMPONENTS} allowed in one message."
            )
        length = self.content_length()
        if length > MAX_CONTENT:
            raise LimitError(
                f"{type(self).__name__}: {length} characters of text exceeds "
                f"the {MAX_CONTENT} allowed in one message."
            )
        return self

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: ui.Item,
    ) -> None:
        await report_error(interaction, error)


async def report_error(interaction: discord.Interaction, error: Exception) -> None:
    """Surface an error to the user without leaking a traceback."""
    message = f"{RED} Something went wrong: `{type(error).__name__}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# -- element helpers ------------------------------------------------------


def text(content: str) -> ui.TextDisplay:
    return ui.TextDisplay(content)


def separator(large: bool = False, visible: bool = True) -> ui.Separator:
    spacing = discord.SeparatorSpacing.large if large else discord.SeparatorSpacing.small
    return ui.Separator(spacing=spacing, visible=visible)


def gallery(*urls: str) -> ui.MediaGallery | None:
    items = [discord.MediaGalleryItem(u) for u in urls if u]
    return ui.MediaGallery(*items) if items else None


def row(*items: ui.Item) -> ui.ActionRow:
    ar = ui.ActionRow()
    for item in items:
        ar.add_item(item)
    return ar


def link_button(label: str, url: str, emoji: str | None = None) -> ui.Button:
    return ui.Button(label=label, url=url, emoji=emoji, style=discord.ButtonStyle.link)


def section(body: str, accessory: ui.Item) -> ui.Section:
    """A text block with a button or thumbnail pinned to its right."""
    return ui.Section(ui.TextDisplay(body), accessory=accessory)


def container(*children: ui.Item, color: int | None = None, spoiler: bool = False) -> ui.Container:
    box = ui.Container(accent_color=accent() if color is None else color, spoiler=spoiler)
    for child in children:
        if child is not None:
            box.add_item(child)
    return box


def panel(
    title: str | None,
    body: str | None = None,
    *,
    banner: str | None = None,
    rows: list[ui.Item] | None = None,
    color: int | None = None,
    footer: str | None = None,
    timeout: float | None = None,
) -> BaseLayout:
    """Standard Blueprint panel: banner, heading, body, divider, controls.

    This is the function nearly every cog should call rather than assembling a
    Container by hand.
    """
    view = BaseLayout(timeout=timeout)

    children: list[ui.Item] = []

    banner_ref = banner if banner is not None else config.get("branding.banner_url")
    banner_url, banner_file = resolve_media(banner_ref)
    if banner_url:
        media = gallery(banner_url)
        if media:
            children.append(media)
            view.track(banner_file)

    heading = []
    if title:
        heading.append(f"## {title}")
    if body:
        heading.append(body)
    if heading:
        children.append(text("\n".join(heading)))

    if rows:
        children.append(separator())
        children.extend(rows)

    if footer:
        children.append(separator())
        children.append(text(f"-# {footer}"))

    view.add_item(container(*children, color=color))
    return view.validate()


def notice(body: str, *, title: str | None = None, color: int | None = None) -> BaseLayout:
    """Small single-block message for confirmations and errors."""
    view = BaseLayout(timeout=None)
    parts = []
    if title:
        parts.append(f"**{title}**")
    parts.append(body)
    view.add_item(container(text("\n".join(parts)), color=color))
    return view.validate()


# Terse status marks. Reply copy should read like a person typed it, so these
# stay small and the message stays short -- no headings, no sign-off.
OK = "✓"
ERR = "✕"
WARN = "!"

GREEN_HEX = 0x43B581
RED_HEX = 0xF04747
AMBER_HEX = 0xFAA61A


def ok(body: str) -> BaseLayout:
    """Confirmation reply."""
    return notice(f"{OK}  {body}", color=GREEN_HEX)


def err(body: str) -> BaseLayout:
    """Something the user did wrong, or a permission block."""
    return notice(f"{ERR}  {body}", color=RED_HEX)


def warn(body: str) -> BaseLayout:
    """Worked, but with a caveat worth reading."""
    return notice(f"{WARN}  {body}", color=AMBER_HEX)


async def send_panel(target, view: BaseLayout, **kwargs):
    """Post a view to a channel, uploading any local images it references."""
    return await target.send(view=view, files=view.files(), **kwargs)


async def respond(
    interaction: discord.Interaction,
    view: BaseLayout,
    *,
    ephemeral: bool = False,
    **kwargs,
):
    """Reply to an interaction, uploading any local images the view references."""
    files = view.files()
    if interaction.response.is_done():
        return await interaction.followup.send(
            view=view, files=files, ephemeral=ephemeral, **kwargs
        )
    return await interaction.response.send_message(
        view=view, files=files, ephemeral=ephemeral, **kwargs
    )


def field(name: str, value: object) -> str:
    """Consistent `**Name:** value` line used across every log embed."""
    return f"**{name}:** {value}"


def check_options(options: list, where: str) -> list:
    if len(options) > MAX_SELECT_OPTIONS:
        raise LimitError(
            f"{where}: {len(options)} select options exceeds the "
            f"{MAX_SELECT_OPTIONS} Discord allows."
        )
    return options
