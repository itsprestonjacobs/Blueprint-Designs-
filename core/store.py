"""Atomic, lock-guarded JSON persistence.

Plain JSON files corrupt easily when two coroutines write at once, so every
write in the bot goes through this one module. Each file gets its own
asyncio.Lock, writes land in a temp file and are committed with os.replace
(atomic on Windows and POSIX alike), and the parsed data is cached in memory so
reads never touch disk.

Typical use::

    tickets = JSONStore("tickets", default={})

    async with tickets.edit() as data:
        data[channel_id] = {...}
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


class JSONStore:
    _locks: dict[Path, asyncio.Lock] = {}

    def __init__(self, name: str, default: Any = None) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self.path = DATA_DIR / f"{name}.json"
        self.default = {} if default is None else default
        self._cache: Any = None

    @property
    def _lock(self) -> asyncio.Lock:
        # Created lazily so a store can be constructed at import time, before
        # any event loop exists.
        lock = JSONStore._locks.get(self.path)
        if lock is None:
            lock = JSONStore._locks[self.path] = asyncio.Lock()
        return lock

    def _load_sync(self) -> Any:
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # A truncated file shouldn't take the bot down. Keep the damaged
            # copy for inspection and carry on from the default.
            if self.path.exists():
                self.path.replace(self.path.with_suffix(".json.corrupt"))
            return json.loads(json.dumps(self.default))

    def _write_sync(self, data: Any) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    async def read(self) -> Any:
        if self._cache is None:
            async with self._lock:
                if self._cache is None:
                    self._cache = await asyncio.to_thread(self._load_sync)
        return self._cache

    async def write(self, data: Any) -> None:
        async with self._lock:
            self._cache = data
            await asyncio.to_thread(self._write_sync, data)

    @asynccontextmanager
    async def edit(self):
        """Read-modify-write under a single lock.

        Holding the lock across the whole block is what makes concurrent
        ticket/order writes safe -- two handlers can't interleave a read and a
        write and lose each other's changes.
        """
        async with self._lock:
            if self._cache is None:
                self._cache = await asyncio.to_thread(self._load_sync)
            data = self._cache
            try:
                yield data
            finally:
                await asyncio.to_thread(self._write_sync, data)

    async def next_id(self, key: str = "_counter") -> int:
        """Monotonic counter used for ticket/order/case numbers."""
        async with self.edit() as data:
            current = int(data.get(key, 0)) + 1
            data[key] = current
            return current


# Shared stores. Declared here so cog reloads reuse the same lock and cache.
tickets = JSONStore("tickets", {})
orders = JSONStore("orders", {})
infractions = JSONStore("infractions", {})
loa = JSONStore("loa", {})
giveaways = JSONStore("giveaways", {})
applications = JSONStore("applications", {})
reviews = JSONStore("reviews", {})
suggestions = JSONStore("suggestions", {})
payouts = JSONStore("payouts", {})
modcases = JSONStore("modcases", {})
counters = JSONStore("counters", {})
boards = JSONStore("boards", {})
activity = JSONStore("activity", {})
qc = JSONStore("qc", {})
