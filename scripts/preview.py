"""Offline validation of every Components V2 view.

Builds each view the bot can send and serialises it, asserting it stays inside
Discord's limits. Catches malformed layouts without a token or a test guild.

    python scripts/preview.py
    python scripts/preview.py --dump   # also print the JSON payloads
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ui  # noqa: E402
from core.config import config  # noqa: E402


def discover_cog_views() -> list[tuple[str, ui.BaseLayout]]:
    """Collect PREVIEW_VIEWS from every cog.

    A cog opts in by defining `PREVIEW_VIEWS = [("name", lambda: SomeView()), ...]`,
    so new views are covered by this harness automatically.
    """
    found: list[tuple[str, ui.BaseLayout]] = []
    cogs_dir = Path(__file__).resolve().parent.parent / "cogs"
    if not cogs_dir.exists():
        return found

    for path in sorted(cogs_dir.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"cogs.{path.stem}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  import cogs.{path.stem}: {type(exc).__name__}: {exc}")
            continue

        for name, factory in getattr(module, "PREVIEW_VIEWS", []):
            try:
                found.append((f"{path.stem}:{name}", factory()))
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL  build cogs.{path.stem}:{name}: {type(exc).__name__}: {exc}")

    return found


def build_cases() -> list[tuple[str, ui.BaseLayout]]:
    """Every view shape the bot renders."""
    cases: list[tuple[str, ui.BaseLayout]] = []

    cases.append(("panel", ui.panel("Heading", "Body copy goes here.")))
    cases.append(("panel-footer", ui.panel("Heading", "Body.", footer="Blueprint Designs")))
    cases.append(("ok", ui.ok("That worked.")))
    cases.append(("err", ui.err("That didn't.")))
    cases.append(("warn", ui.warn("Worked, with a caveat.")))
    cases.append(
        (
            "panel-buttons",
            ui.panel(
                "With controls",
                "A panel carrying a link button.",
                rows=[ui.row(ui.link_button("Open", "https://example.com"))],
            ),
        )
    )

    cases.extend(discover_cog_views())
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", action="store_true", help="print serialised payloads")
    args = parser.parse_args()

    failures = 0
    try:
        cases = build_cases()
    except ui.LimitError as exc:
        print(f"FAIL  (while building) {exc}")
        return 1

    for name, view in cases:
        try:
            payload = view.to_components()
            count = view.total_children_count
            length = view.content_length()
            view.validate()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        print(f"ok    {name}: {count} components, {length} chars")
        if args.dump:
            print(json.dumps(payload, indent=2))

    # Config must stay loadable and self-consistent.
    try:
        config.load()
        print(f"ok    config.json parses, {len(config.missing_keys())} value(s) unset")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  config.json: {exc}")
        failures += 1

    print()
    if failures:
        print(f"{failures} failure(s)")
        return 1
    print(f"all {len(cases)} view(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
