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
    """Key/value storage on the Durable Object's KV API.

    Why not SQL
    -----------
    The SQLite API returns a `SqlStorageCursor`. In Python each row comes back as
    a `pyodide.ffi.JsProxy`, and `row["v"]` raises

        TypeError: 'pyodide.ffi.JsProxy' object is not subscriptable

    which is exactly what the live bot reported. Converting proxies correctly
    means knowing the exact Pyodide version's semantics, per row, on every read —
    a lot of surface area for a key/value store.

    The KV API is what Cloudflare's own documentation uses for this purpose:

        value = (await self.ctx.storage.get("value")) or 0
        await self.ctx.storage.put("value", value)

    It is async, returns and accepts **Python** values (no cursors, no proxies),
    and needs no schema. This class stores the JSON strings `DurableStore`
    already produces, so the contract above it is unchanged.

    One Durable Object instance is still the right primitive: it serialises the
    cron, the webhook-less poller and any manual command onto one writer.
    """

    def __init__(self, ctx, env=None):
        super().__init__(ctx, env)

    # ── async KV, with a sync fallback for older runtimes ────────────────────
    def _storage(self):
        return getattr(self.ctx, "storage", None)

    async def _get(self, key: str):
        storage = self._storage()
        if storage is None:
            return None
        fn = getattr(storage, "get", None)
        if fn is None:
            return None
        value = fn(key)
        if hasattr(value, "__await__"):
            value = await value
        return value

    async def _put(self, key: str, value) -> None:
        storage = self._storage()
        if storage is None:
            return
        fn = getattr(storage, "put", None)
        if fn is None:
            return
        result = fn(key, value)
        if hasattr(result, "__await__"):
            await result

    async def _delete(self, key: str) -> None:
        storage = self._storage()
        if storage is None:
            return
        fn = getattr(storage, "delete", None)
        if fn is None:
            return
        result = fn(key)
        if hasattr(result, "__await__"):
            await result

    @staticmethod
    def _to_text(value):
        """Normalise whatever came back into a str, or None.

        The KV API can hand back a JsProxy for values written by another
        language. Everything here is JSON written by `DurableStore`, so the
        common path is a plain str; the rest is defensive.
        """
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", "replace")
        for method in ("to_py", "toString"):
            fn = getattr(value, method, None)
            if callable(fn):
                try:
                    converted = fn()
                    if isinstance(converted, str):
                        return converted
                    if converted is not None and not hasattr(converted, "to_py"):
                        return str(converted) if not isinstance(converted, (dict, list)) else None
                except Exception:
                    continue
        try:
            return str(value)
        except Exception:
            return None

    # ── the RPC surface `DurableStore` calls ────────────────────────────────
    async def storage_get(self, key: str):
        return self._to_text(await self._get(key))

    async def storage_set(self, key: str, value: str) -> None:
        await self._put(key, value)

    async def storage_append(self, key: str, item: str, cap: int = 500) -> str:
        """Append to a JSON list, keeping only the newest `cap` entries.

        `item` arrives JSON-encoded from `DurableStore.append`, so it is decoded
        before storage — otherwise readers would have to double-decode.
        """
        import json as _json

        current = await self.storage_get(key)
        try:
            items = _json.loads(current) if current else []
            if not isinstance(items, list):
                items = []
        except (TypeError, ValueError):
            items = []

        try:
            items.append(_json.loads(item))
        except (TypeError, ValueError):
            items.append(item)
        if len(items) > cap:
            items = items[-cap:]

        payload = _json.dumps(items)
        await self._put(key, payload)
        return payload

    async def storage_delete(self, key: str) -> None:
        await self._delete(key)

    async def fetch(self, request=None):  # pragma: no cover - RPC surface
        return None
