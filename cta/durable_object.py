"""RadarState — the Durable Object that owns all persisted state.

Why a Durable Object at all? Because the three writers (cron scan, Telegram
webhook, manual /kick) mutate the same watchlist and the same signal log. A DO is
Cloudflare's single-serialisation primitive: one instance per deployment, backed by
SQLite, with strong consistency and no race between a cycle and a chat message.

`wrangler.toml` binds it as `STATE` with `class_name = "RadarState"`.

The methods below mirror `cta.state.DurableStore` exactly — that is the contract
between Python code and the runtime.
"""

from __future__ import annotations

import json

try:  # Cloudflare runtime
    from workers import DurableObject  # type: ignore
except Exception:  # pragma: no cover - local test runs

    class DurableObject:  # type: ignore
        """Fallback so this module imports cleanly under pytest."""

        def __init__(self, ctx=None):
            self.ctx = ctx


class RadarState(DurableObject):
    """Key/value storage with bounded append lists, on top of DO SQLite."""

    def __init__(self, ctx):
        # Signature must match the Cloudflare SDK exactly: the runtime calls this
        # with the DurableObjectState only, and wraps `env` itself. The previous
        # `(ctx, env=None)` form raised TypeError on every invocation, and its
        # `self.env` was never read anywhere in this class.
        super().__init__(ctx)
        self._initialised = False

    async def _ensure_schema(self) -> None:
        if self._initialised:
            return
        storage = self.ctx.storage
        sql = getattr(storage, "sql", None)
        if sql is not None:
            await sql.exec(
                "CREATE TABLE IF NOT EXISTS kv ("
                "  k TEXT PRIMARY KEY,"
                "  v TEXT NOT NULL"
                ")"
            )
        self._initialised = True

    async def storage_get(self, key: str):
        await self._ensure_schema()
        sql = getattr(self.ctx.storage, "sql", None)
        if sql is None:  # pragma: no cover - older runtime without SQLite
            return await self.ctx.storage.get(key)
        cursor = await sql.exec("SELECT v FROM kv WHERE k = ?", key)
        rows = await cursor.toArray()
        if not rows:
            return None
        row = rows[0]
        return row["v"] if isinstance(row, dict) else row[0]

    async def storage_set(self, key: str, value: str) -> None:
        await self._ensure_schema()
        sql = getattr(self.ctx.storage, "sql", None)
        if sql is None:  # pragma: no cover
            await self.ctx.storage.put(key, value)
            return
        await sql.exec(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            key,
            value,
        )

    async def storage_append(self, key: str, item: str, cap: int = 500) -> str:
        """Append to a JSON list, keeping only the newest `cap` entries.

        `item` arrives JSON-encoded from `DurableStore.append`, so it is decoded
        before being stored — otherwise the list would fill with JSON *strings*
        and every reader would have to double-decode.
        """
        await self._ensure_schema()
        current = await self.storage_get(key)
        try:
            items = json.loads(current) if current else []
            if not isinstance(items, list):
                items = []
        except (TypeError, ValueError):
            items = []

        try:
            items.append(json.loads(item))
        except (TypeError, ValueError):
            items.append(item)
        if len(items) > cap:
            items = items[-cap:]

        payload = json.dumps(items)
        await self.storage_set(key, payload)
        return payload

    async def storage_delete(self, key: str) -> None:
        await self._ensure_schema()
        sql = getattr(self.ctx.storage, "sql", None)
        if sql is None:  # pragma: no cover
            await self.ctx.storage.delete(key)
            return
        await sql.exec("DELETE FROM kv WHERE k = ?", key)

    async def fetch(self, request=None):  # pragma: no cover - RPC surface
        return None
