"""Generate wide panel banners from the Blueprint logo.

Discord renders a media gallery full width, so a square logo becomes a huge
square block. Panel banners want to be a wide strip (3:1) with the panel name
set into the artwork -- that is what makes a panel header read as designed
rather than as an uploaded image.

This crops the blueprint texture out of assets/blueprint.png, darkens it, and
sets the panel title over the top.

    python scripts/make_banners.py            # regenerate all
    python scripts/make_banners.py --list     # show what it would build
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "blueprint.png"
OUT_DIR = ASSETS / "banners"

WIDTH, HEIGHT = 1200, 400
BRAND = "BLUEPRINT DESIGNS"
ACCENT = (59, 130, 246)          # #3B82F6

# The logo's lower third carries its own wordmark; crop above it so the
# generated title isn't fighting baked-in text.
TEXTURE_CROP = (0.0, 0.02, 1.0, 0.52)

FONT_CANDIDATES = [
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
]

# panel file stem -> title shown on the banner
BANNERS = {
    "order": "ORDER HERE",
    "support": "SUPPORT",
    "dashboard": "DASHBOARD",
    "pricing": "PRICING",
    "applications": "APPLICATIONS",
    "quality_control": "QUALITY CONTROL",
    "store": "STORE",
    "courses": "COURSES",
    "designer_info": "DESIGNER INFO",
    "support_info": "SUPPORT INFO",
    "hr_dashboard": "HR DASHBOARD",
    "welcome": "WELCOME",
    "giveaway": "GIVEAWAY",
}


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def build_background() -> Image.Image:
    """Wide, darkened blueprint texture to set type over."""
    src = Image.open(SOURCE).convert("RGB")
    w, h = src.size
    box = (
        int(w * TEXTURE_CROP[0]),
        int(h * TEXTURE_CROP[1]),
        int(w * TEXTURE_CROP[2]),
        int(h * TEXTURE_CROP[3]),
    )
    texture = src.crop(box)

    # Cover the target box without distorting the aspect ratio.
    scale = max(WIDTH / texture.width, HEIGHT / texture.height)
    resized = texture.resize(
        (max(int(texture.width * scale), WIDTH), max(int(texture.height * scale), HEIGHT)),
        Image.LANCZOS,
    )
    left = (resized.width - WIDTH) // 2
    top = (resized.height - HEIGHT) // 2
    canvas = resized.crop((left, top, left + WIDTH, top + HEIGHT))

    # Push the texture back so white type stays legible on top of it.
    canvas = canvas.filter(ImageFilter.GaussianBlur(1.2))
    canvas = ImageEnhance.Brightness(canvas).enhance(0.62)
    canvas = ImageEnhance.Contrast(canvas).enhance(0.9)

    # Left-to-right shade so the title side is darkest.
    shade = Image.new("L", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(shade)
    for x in range(WIDTH):
        draw.line([(x, 0), (x, HEIGHT)], fill=int(150 * (1 - x / WIDTH) + 40))
    canvas = Image.composite(Image.new("RGB", (WIDTH, HEIGHT), (6, 20, 42)), canvas, shade)

    return canvas


def make_banner(title: str, out: Path) -> None:
    canvas = build_background()
    draw = ImageDraw.Draw(canvas)

    margin = 70

    # Shrink the title until it fits the available width.
    size = 116
    font = load_font(size)
    while size > 40:
        font = load_font(size)
        if draw.textlength(title, font=font) <= WIDTH - margin * 2 - 20:
            break
        size -= 4

    ascent, descent = font.getmetrics()
    title_h = ascent + descent
    brand_font = load_font(30)
    block_h = title_h + 20 + 34
    top = (HEIGHT - block_h) // 2

    # Accent rule above the title.
    draw.rectangle([margin, top - 20, margin + 90, top - 14], fill=ACCENT)

    # Soft drop shadow, then the title.
    draw.text((margin + 3, top + 3), title, font=font, fill=(0, 0, 0))
    draw.text((margin, top), title, font=font, fill=(255, 255, 255))

    # Brand line under it, letter-spaced by hand for a wordmark feel.
    y = top + title_h + 16
    x = margin + 2
    for ch in BRAND:
        draw.text((x, y), ch, font=brand_font, fill=(190, 214, 255))
        x += draw.textlength(ch, font=brand_font) + 3

    # Accent edge down the left, echoing the container stripe.
    draw.rectangle([0, 0, 7, HEIGHT], fill=ACCENT)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="show planned banners only")
    args = parser.parse_args()

    if not SOURCE.exists():
        print(f"missing source image: {SOURCE}")
        return 1

    if args.list:
        for name, title in BANNERS.items():
            print(f"{name:<18} {title}")
        return 0

    for name, title in BANNERS.items():
        out = OUT_DIR / f"{name}.png"
        make_banner(title, out)
        kb = out.stat().st_size // 1024
        print(f"ok  {out.relative_to(ROOT)}  {WIDTH}x{HEIGHT}  {kb}KB  '{title}'")

    print(f"\n{len(BANNERS)} banner(s) written to {OUT_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
