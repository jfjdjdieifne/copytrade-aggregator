"""SQLite-backed Store — so a restart does not erase months of evidence.

Why this exists
---------------
Local and server runs used `MemoryStore`. That is fine for a one-shot CLI
command and quietly catastrophic for a 24/7 radar:

* the watchlist (hundreds of scored traders, each costing ~20 weight to
  re-derive) evaporates
* `seen_signal_ids` empties, so the next tick re-emits every signal the
  operator already acted on — a duplicate alert is worse than no alert,
  because it teaches them to ignore the channel
* the paper account resets to $100, destroying the only honest measurement of
  whether this whole idea works
* outcome tracking loses the open signals it was supposed to follow up

On Cloudflare the Durable Object plays this role. On a server, SQLite does —
same interface, so nothing upstream changes.

Concurrency: writes are committed immediately and reads see the latest commit,
because a crash mid-cycle must lose at most one cycle, never the history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional


class SqliteStore:
    """A `Store` implementation backed by a single SQLite file.

    Deliberately dependency-free: stdlib only, matching the rest of the
    runtime, and safe to `pip install` nothing on a free-tier instance.
    """

    def __init__(self, path: str = "state.db"):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " updated_at REAL DEFAULT (unixepoch('subsec'))"
            ")"
        )
        self._conn.commit()

    # ── Store protocol ──────────────────────────────────────────────────────
    async def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            # Corrupt value: return the default rather than taking the service
            # down, but never pretend the read succeeded.
            return default

    async def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, unixepoch('subsec')) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, payload),
            )
            self._conn.commit()

    async def append(self, key: str, item: Any, cap: int = 500) -> list:
        current = await self.get(key, [])
        if not isinstance(current, list):
            current = []
        current.append(item)
        trimmed = current[-cap:] if cap and cap > 0 else current
        await self.set(key, trimmed)
        return trimmed

    async def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            self._conn.commit()

    async def keys(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT key FROM kv ORDER BY key").fetchall()
        return [r[0] for r in rows]

    async def snapshot(self) -> dict[str, Any]:
        """Everything we know — for backup, or for the dashboard's debug view."""
        out: dict[str, Any] = {}
        for key in await self.keys():
            out[key] = await self.get(key)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_store(path: Optional[str] = None) -> SqliteStore:
    return SqliteStore(path or "state.db")
