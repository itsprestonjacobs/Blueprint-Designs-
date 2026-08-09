"""Channel name cleaning.

Strips decoration -- emoji, separator dots, bars -- from a channel name while
leaving the actual words intact.

Kept separate from the cog so it can be tested against real server names
without touching Discord.
"""

from __future__ import annotations

import re
import unicodedata

# Custom Discord emoji, e.g. <:name:1234> or <a:name:1234>
CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")

# Decorative separators people put between an emoji and the name.
#
# Deliberately absent: hyphen, underscore, colon, apostrophe, dash and tilde.
# All of them appear inside real names ("order-here", "All Members: 229",
# "Preston's Room"), so stripping them would mangle the result rather than
# clean it.
SEPARATORS = "・·•‧∙⋅｜|┃│▏▎▍⁝⦙⋮※★☆✦✧➤➣»«‹›"

# Unicode blocks that are emoji or emoji-adjacent symbols.
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),   # pictographs, emoticons, transport, symbols, extended
    (0x1F1E6, 0x1F1FF),   # regional indicators (flags)
    (0x2600, 0x27BF),     # misc symbols + dingbats
    (0x2B00, 0x2BFF),     # misc symbols and arrows
    (0x2190, 0x21FF),     # arrows
    (0x2300, 0x23FF),     # misc technical (⌚ ⏰ …)
    (0x25A0, 0x25FF),     # geometric shapes
    (0x2900, 0x297F),     # supplemental arrows
    (0xFE00, 0xFE0F),     # variation selectors
    (0x1F900, 0x1F9FF),   # supplemental symbols
    (0xE000, 0xF8FF),     # private use (custom font icons)
)


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    if code == 0x200D:          # zero-width joiner, glues emoji sequences
        return True
    if code == 0x20E3:          # combining enclosing keycap
        return True
    return any(low <= code <= high for low, high in EMOJI_RANGES)


def clean_name(name: str, *, text_channel: bool = True) -> str:
    """Return `name` with emoji and separator decoration removed.

    `text_channel` mirrors Discord's own normalisation: text and forum channel
    names get lowercased with spaces turned into hyphens, while categories,
    voice and stage channels keep their casing and spaces.
    """
    cleaned = CUSTOM_EMOJI.sub(" ", name)
    cleaned = "".join(" " if _is_emoji(ch) else ch for ch in cleaned)
    cleaned = "".join(" " if ch in SEPARATORS else ch for ch in cleaned)

    # Drop anything left that isn't a real character (stray combining marks).
    cleaned = "".join(
        ch for ch in cleaned if not unicodedata.category(ch).startswith("C")
    )

    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")

    if text_channel:
        cleaned = cleaned.lower().replace(" ", "-")
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")

    return cleaned[:100]


def needs_cleaning(name: str, *, text_channel: bool = True) -> bool:
    result = clean_name(name, text_channel=text_channel)
    return bool(result) and result != name
