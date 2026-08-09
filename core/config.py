"""Loads config.json and reports which IDs are still unset.

The bot is designed to boot with a completely blank config so setup can happen
incrementally -- anything unconfigured simply disables the feature that needs it
rather than raising at startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_ACCENT = 0x3B82F6


class Config:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self.path.open("r", encoding="utf-8") as f:
            self._data = json.load(f)

    def save(self) -> None:
        """Only used by commands that mutate config (e.g. /updatestatus)."""
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.path)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        """Fetch a value by dotted path, e.g. cfg.get('tickets.max_open_per_user')."""
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self._data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    # -- typed helpers ----------------------------------------------------

    @property
    def accent(self) -> int:
        raw = self.get("accent_color", DEFAULT_ACCENT)
        if isinstance(raw, int):
            return raw
        try:
            return int(str(raw), 16 if str(raw).lower().startswith("0x") else 10)
        except ValueError:
            return DEFAULT_ACCENT

    @property
    def guild_id(self) -> int | None:
        gid = self.get("guild_id")
        return int(gid) if gid else None

    def channel_id(self, key: str) -> int | None:
        cid = self.get(f"channels.{key}")
        return int(cid) if cid else None

    def role_ids(self, tier: str) -> list[int]:
        raw = self.get(f"roles.{tier}", [])
        if raw is None:
            return []
        if isinstance(raw, (str, int)):
            return [int(raw)]
        return [int(r) for r in raw if r]

    def ticket_categories(self) -> dict[str, dict[str, Any]]:
        return self.get("tickets.categories", {}) or {}

    def ticket_category(self, key: str) -> dict[str, Any] | None:
        return self.ticket_categories().get(key)

    # -- validation -------------------------------------------------------

    def missing_keys(self) -> list[str]:
        """Every configured-by-ID slot that is still empty, for the boot report."""
        missing: list[str] = []

        if not self.guild_id:
            missing.append("guild_id")

        for key, value in (self.get("channels", {}) or {}).items():
            if not value:
                missing.append(f"channels.{key}")

        for tier in ("admin", "hr", "support", "designer"):
            if not self.role_ids(tier):
                missing.append(f"roles.{tier}")

        for key, cat in self.ticket_categories().items():
            if not cat.get("category_id"):
                missing.append(f"tickets.categories.{key}.category_id")

        return missing


config = Config()
