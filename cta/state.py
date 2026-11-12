"""State persistence behind a tiny interface.

Two implementations:
  * MemoryStore   — tests, and a safe fallback when no Durable Object is bound
  * DurableStore  — production; delegates to a SQLite-backed Durable Object

The interface is intentionally narrow (get/set/list/append) so swapping Cloudflare
D1 in later requires no changes above this layer.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

KEY_WATCHLIST = "watchlist"
KEY_PROFILES = "profiles"
KEY_SIGNALS = "signals"
KEY_MEMORY = "memory"
KEY_STATS = "stats"
KEY_COUNTERS = "counters"


class Store(Protocol):
    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def append(self, key: str, item: Any, cap: int = 500) -> list: ...
    async def delete(self, key: str) -> None: ...


class MemoryStore:
    """In-process store. Deterministic, instant, used by every test."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    async def append(self, key: str, item: Any, cap: int = 500) -> list:
        bucket = list(self._data.get(key, []))
        bucket.append(item)
        if len(bucket) > cap:
            bucket = bucket[-cap:]
        self._data[key] = bucket
        return bucket

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


class DurableStore:
    """Adapter over a Durable Object's SQLite-backed storage.

    The DO is a single serialisation point, which is exactly what we need: the
    poller, the Telegram handler and the cron all mutate the same watchlist.
    """

    def __init__(self, stub):
        self.stub = stub

    async def _rpc(self, method: str, *args) -> Any:
        fn = getattr(self.stub, method, None)
        if fn is None:
            return None
        result = fn(*args)
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def get(self, key: str, default: Any = None) -> Any:
        raw = await self._rpc("storage_get", key)
        if raw is None:
            return default
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return default

    async def set(self, key: str, value: Any) -> None:
        await self._rpc("storage_set", key, json.dumps(value, default=str))

    async def append(self, key: str, item: Any, cap: int = 500) -> list:
        raw = await self._rpc("storage_append", key, json.dumps(item, default=str), cap)
        try:
            return json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (TypeError, ValueError):
            return []

    async def delete(self, key: str) -> None:
        await self._rpc("storage_delete", key)


def _is_store_like(obj: Any) -> bool:
    """Already a Store? Accept it instead of wrapping it again."""
    return all(callable(getattr(obj, m, None)) for m in ("get", "set", "append"))


def store_from_env(env) -> Store:
    """Pick the best available store, degrading rather than failing.

    Three cases must all work:
      * a Durable Object stub  -> wrap it
      * an injected Store      -> use as-is (tests, alternate backends)
      * unbound / empty string -> in-memory, so the radar still runs locally

    A stub of `""` would otherwise swallow every write and the service would look
    alive while remembering nothing.
    """
    binding = getattr(env, "STATE", None)

    # `hasattr` alone cannot be trusted: an RPC stub answers every attribute
    # access through __getattr__, so `hasattr(binding, "storage_get")` is True
    # even when no such method exists on the far side. Accept it only if the
    # real class defines `storage_get`, or if the binding is genuinely a
    # Durable Object stub/namespace.
    cls = type(binding)
    type_name = cls.__name__ if binding is not None else ""
    defines_storage = any("storage_get" in vars(klass) for klass in cls.__mro__)
    is_durable = type_name in ("DurableObjectStub", "DurableObjectNamespace")
    try:
        is_durable = is_durable or hasattr(binding, "idFromName")
    except Exception:
        pass

    if _is_store_like(binding):
        return binding
    if binding and (defines_storage or is_durable):
        return DurableStore(binding)
    return MemoryStore()


# ── higher-level helpers ─────────────────────────────────────────────────────
async def remember_signal(store: Store, signal_dict: dict, cap: int = 300) -> None:
    await store.append(KEY_SIGNALS, signal_dict, cap=cap)


async def get_signals(store: Store) -> list[dict]:
    return await store.get(KEY_SIGNALS, []) or []


async def upsert_watchlist(store: Store, entries: list[dict]) -> None:
    await store.set(KEY_WATCHLIST, entries)


async def get_watchlist(store: Store) -> list[dict]:
    return await store.get(KEY_WATCHLIST, []) or []


async def bump_counter(store: Store, name: str, by: int = 1) -> int:
    counters = await store.get(KEY_COUNTERS, {}) or {}
    counters[name] = int(counters.get(name, 0)) + by
    await store.set(KEY_COUNTERS, counters)
    return counters[name]


async def append_outcome(store: Store, outcome: dict, cap: int = 2000) -> list:
    """Store one signal evaluation (bounded so a long-running deployment stays lean)."""
    return await store.append("outcomes", outcome, cap=cap)


async def record_chat(store: Store, chat_id: str, role: str, content: str, cap: int = 40) -> None:
    key = f"{KEY_MEMORY}:{chat_id}"
    await store.append(key, {"role": role, "content": content[:1500]}, cap=cap)


async def get_chat_history(store: Store, chat_id: str, limit: int = 8) -> list[dict]:
    history = await store.get(f"{KEY_MEMORY}:{chat_id}", []) or []
    return history[-limit:]
