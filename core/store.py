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
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("blueprint.store")

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
            # A damaged file shouldn't take the bot down, and it shouldn't
            # silently lose everything either. Keep the bad copy, then fall
            # back to the last good backup before giving up on the data.
            log.error("%s is unreadable; keeping it as .corrupt", self.path.name)
            try:
                self.path.replace(self.path.with_suffix(".json.corrupt"))
            except OSError:
                pass

            backup = self.path.with_suffix(".json.bak")
            if backup.exists():
                try:
                    with backup.open("r", encoding="utf-8") as f:
                        recovered = json.load(f)
                    log.warning("recovered %s from its backup", self.path.name)
                    return recovered
                except (json.JSONDecodeError, OSError):
                    log.error("backup for %s is unreadable too", self.path.name)

            return json.loads(json.dumps(self.default))

    def _write_sync(self, data: Any) -> None:
        # Keep the previous version before replacing it. os.replace is atomic,
        # so a crash can't truncate the file -- but nothing protects against a
        # logic bug writing wrong data, and on a host there's no undo.
        if self.path.exists():
            try:
                backup = self.path.with_suffix(".json.bak")
                backup.write_bytes(self.path.read_bytes())
            except OSError:
                pass  # a failed backup must never block the write

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


# Cogs declare their own stores. Two JSONStore instances for the same file
# share a lock through the class-level registry above, so read-only views from
# another module (e.g. cogs/lookup.py) are safe.
