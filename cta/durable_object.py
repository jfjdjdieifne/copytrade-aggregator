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
        """Fallback so this module imports cleanly under pytest.

        The signature must mirror the real base class exactly —
        `__init__(self, ctx, env)` — or the subclass's `super().__init__(ctx,
        env)` raises here while working on Cloudflare, which is the worst kind
        of divergence: tests pass for the wrong reason.
        """

        def __init__(self, ctx=None, env=None):
            self.ctx = ctx
            self.env = env


class RadarState(DurableObject):
    """Key/value storage with bounded append lists, on top of DO SQLite."""

    def __init__(self, ctx, env=None):
        # `DurableObject.__init__(self, ctx, env)` takes TWO arguments — verified
        # against the built-in module and the official docs:
        #
        #     class Counter(DurableObject):
        #         def __init__(self, ctx, env):
        #             super().__init__(ctx, env)
        #
        # Calling `super().__init__(ctx)` raises
        #     TypeError: DurableObject.__init__() missing 1 required positional
        #     argument: 'env'
        # which breaks instantiation, so every RPC call fails and the HTTP
        # handler returns 500. Telegram reported exactly that:
        #     Wrong response from the webhook: 500 Internal Server Error
        #
        # `env` is defaulted so the class still imports and instantiates in
        # tests where only a context exists.
        super().__init__(ctx, env)
        self._initialised = False

    async def _ensure_schema(self) -> None:
        if self._initialised:
            return
        sql = self._sql()
        if sql is not None:
            sql.exec(
                "CREATE TABLE IF NOT EXISTS kv ("
                "  k TEXT PRIMARY KEY,"
                "  v TEXT NOT NULL"
                ")"
            )
        self._initialised = True

    def _sql(self):
        """The DO SQL handle, or None on a KV-backed object."""
        return getattr(getattr(self.ctx, "storage", None), "sql", None)

    def _rows(self, cursor):
        """Materialise a `SqlStorageCursor` into a Python list of dicts.

        In Python the DO SQL API is **synchronous** — the docs show
        `self.sql.exec(...)` with no `await`, and `exec` returns a cursor, not a
        promise. Our code awaited both the call and `toArray()`, which raises
            TypeError: object SqlStorageCursor can't be used in 'await' expression
        A cursor is iterable, so collecting it is the reliable path.
        """
        if cursor is None:
            return []
        rows = getattr(cursor, "toArray", None)
        if callable(rows):
            try:
                return list(rows())
            except Exception:
                pass
        try:
            return list(cursor)
        except TypeError:
            return []

    async def _maybe(self, value):
        """Await only when the platform actually returned a promise."""
        if hasattr(value, "__await__"):
            return await value
        return value

    async def storage_get(self, key: str):
        await self._ensure_schema()
        sql = self._sql()
        if sql is None:  # pragma: no cover - KV-backed object
            return await self._maybe(self.ctx.storage.get(key))
        rows = self._rows(sql.exec("SELECT v FROM kv WHERE k = ?", key))
        if not rows:
            return None
        row = rows[0]
        try:
            return row["v"]
        except (TypeError, KeyError, IndexError):
            return row[0]

    async def storage_set(self, key: str, value: str) -> None:
        await self._ensure_schema()
        sql = self._sql()
        if sql is None:  # pragma: no cover
            await self._maybe(self.ctx.storage.put(key, value))
            return
        sql.exec(
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
        sql = self._sql()
        if sql is None:  # pragma: no cover
            await self._maybe(self.ctx.storage.delete(key))
            return
        sql.exec("DELETE FROM kv WHERE k = ?", key)

    async def fetch(self, request=None):  # pragma: no cover - RPC surface
        return None
