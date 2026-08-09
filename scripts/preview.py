"""Offline validation of every Components V2 view.

Builds each panel the bot can send and serialises it, asserting it stays inside
Discord's limits. Catches malformed layouts without a token or a test guild.

    python scripts/preview.py
    python scripts/preview.py --dump   # also print the JSON payloads
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ui  # noqa: E402
from core.config import config  # noqa: E402
from core.store import JSONStore  # noqa: E402


def discover_cog_views() -> list[tuple[str, ui.BaseLayout]]:
    """Collect PREVIEW_VIEWS from every cog.

    A cog opts in by defining `PREVIEW_VIEWS = [("name", lambda: SomeView()), ...]`,
    so new panels are covered by this harness automatically.
    """
    import importlib

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
    """Every view shape the bot renders. Extend as phases land."""
    cases: list[tuple[str, ui.BaseLayout]] = []

    cases.append(("ping", ui.panel("Pong", f"{ui.GREEN} Gateway latency: **42ms**")))
    cases.append(("notice", ui.notice("A short confirmation message.")))
    cases.append(
        (
            "panel-with-rows",
            ui.panel(
                "Order Here",
                "Want to make a purchase? Please check our order status below.",
                rows=[ui.row(ui.link_button("Pricing", "https://example.com"))],
                footer="Blueprint Utilities",
            ),
        )
    )

    # Worst case for the text limit: every ticket category listed at once.
    categories = config.ticket_categories()
    body = "\n".join(
        f"{c.get('emoji', '•')} **{c.get('label', k)}** — {c.get('description', '')}"
        for k, c in categories.items()
    )
    cases.append(("all-ticket-categories", ui.panel("Ticket Categories", body)))

    # Worst case for the status board.
    status = config.get("order_status", {}) or {}
    status_body = "\n".join(
        f"**{name}:** {ui.GREEN if state == 'OPENED' else ui.RED} `{state}`"
        for name, state in status.items()
    )
    cases.append(("order-status-board", ui.panel("Order Status", status_body)))

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
        except Exception as exc:  # noqa: BLE001 - report, don't crash the harness
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        print(f"ok    {name}: {count} components, {length} chars")
        if args.dump:
            print(json.dumps(payload, indent=2))

    # Panel files must be valid JSON. The loader deliberately swallows parse
    # errors at runtime so one bad file can't stop the bot, which means only
    # this check will tell you a panel silently stopped existing.
    panels_dir = Path(__file__).resolve().parent.parent / "panels"
    for path in sorted(panels_dir.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"FAIL  {path.name} is not valid JSON: {exc}")
            failures += 1
            continue

        # Each dropdown option renders as its own ephemeral panel.
        for option in spec.get("select", {}).get("options", []):
            try:
                sub = ui.panel(
                    option.get("title", option.get("label", "?")),
                    option.get("content", ""),
                    banner=option.get("banner"),
                )
                sub.to_components()
                sub.validate()
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL  {path.stem}/{option.get('value')}: {exc}")
                failures += 1
        print(f"ok    {path.name}: valid, {len(spec.get('select', {}).get('options', []))} option(s)")

    # Select-menu option caps.
    try:
        ui.check_options(list(config.ticket_categories()), "tickets.categories")
        print(f"ok    ticket category select: {len(config.ticket_categories())} options")
    except ui.LimitError as exc:
        print(f"FAIL  {exc}")
        failures += 1

    # Store round-trip, so a broken persistence layer shows up here too.
    store = JSONStore("_preview_selftest", {})
    if store.path.exists():
        store.path.unlink()
    data = store._load_sync()
    data["x"] = 1
    store._write_sync(data)
    assert store._load_sync()["x"] == 1, "JSONStore round-trip failed"
    store.path.unlink()
    print("ok    JSONStore atomic round-trip")

    print()
    if failures:
        print(f"{failures} failure(s)")
        return 1
    print(f"all {len(cases)} view(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
