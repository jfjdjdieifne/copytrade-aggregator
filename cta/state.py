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
KEY_OWNER_CHAT = "owner_chat"
KEY_PUBLIC_BASE = "public_base"


class Store(Protocol):
    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def append(self, key: str, item: Any, cap: int = 500) -> list: ...
    async def delete(self, key: str) -> None: ...


class StoreError(RuntimeError):
    """The state backend is reachable in principle but failed in practice."""


class UnavailableStore:
    """A state binding exists but could not be used.

    Deliberately *not* a silent in-memory fallback. Returning an empty
    MemoryStore when a configured Durable Object is broken is how the live
    dashboard ended up reporting "0 signals, 0 traders, platforms unreachable"
    — numbers that read as measurements but were only the absence of data.

    Every method raises, so the caller either degrades explicitly or shows an
    error. `/health` reports it; the page says state is unavailable instead of
    printing zeros.
    """

    def __init__(self, reason: str = "binding") -> None:
        self.reason = reason

    def _fail(self, what: str):
        raise StoreError(
            f"state backend unavailable ({self.reason}): cannot {what}. "
            "env.STATE must resolve to a Durable Object stub with storage_get."
        )

    async def get(self, key: str, default: Any = None) -> Any:
        self._fail(f"read {key!r}")

    async def set(self, key: str, value: Any) -> None:
        self._fail(f"write {key!r}")

    async def append(self, key: str, item: Any, cap: int = 500) -> list:
        self._fail(f"append to {key!r}")

    async def delete(self, key: str) -> None:
        self._fail(f"delete {key!r}")


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
        """Call the Durable Object. Failures propagate — see `UnavailableStore`.

        `MemoryStore`-style swallowing here is what made the live dashboard show
        fabricated zeros: a broken binding looked exactly like an empty database.
        """
        fn = getattr(self.stub, method, None)
        if fn is None:
            raise StoreError(f"durable object has no method {method!r}")
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


# The single Durable Object instance name this deployment uses. One instance is
# the point: it is the serialisation point for the cron, the webhook and /kick.
DO_INSTANCE_NAME = "radar"


def _resolve_binding(binding: Any) -> Any:
    """Turn whatever `env.STATE` hands us into something with `storage_get`.

    The runtime does not give us a stub. `_EnvWrapper` detects
    `DurableObjectNamespace` and returns a `_DurableObjectNamespaceWrapper`,
    whose `get` is `_FetcherWrapper(namespace.get(...))` — a *fetch* interface
    with no RPC methods at all:

        class _DurableObjectNamespaceWrapper:
            def get(self, *args, **kwargs):
                return _FetcherWrapper(self._binding.get(*args, **kwargs))

    (workers-runtime-sdk `rpc.py`, and the identical built-in in workerd.)
    Calling `storage_get` on that wrapper reaches the JS namespace, which has no
    such method, so every read and write raised — and because `DurableStore`
    swallowed the failure, the dashboard showed zeros that look like real
    measurements. That is why the live page claimed the platforms were
    unreachable while the site itself worked: the health probe could not be
    stored, so `live` was computed from an empty dict.

    So: if the binding can mint an id, mint one and take the stub. A stub
    (`FetcherWrapper`) forwards method names to the object over RPC, which is
    what `DurableStore` needs.
    """
    if binding is None or binding == "":
        return None

    # Already the object itself (or a stub that answers RPC directly).
    if any("storage_get" in vars(k) for k in type(binding).__mro__):
        return binding

    id_from_name = getattr(binding, "idFromName", None)
    if not callable(id_from_name):
        return None

    try:
        stub = binding.get(id_from_name(DO_INSTANCE_NAME))
    except Exception:
        return None
    return stub or None


def store_from_env(env) -> Store:
    """Pick the best available store, degrading rather than failing.

    Order matters:
      * an injected Store          -> use as-is (tests, alternate backends)
      * a namespace or stub        -> resolve to a stub, wrap it
      * unbound / empty string     -> in-memory, so the radar still runs locally

    A store that is unreachable must be *reported*, not silently swapped for an
    empty one — see `UnavailableStore`.
    """
    binding = getattr(env, "STATE", None)

    if _is_store_like(binding):
        return binding

    stub = _resolve_binding(binding)
    if stub is not None:
        return DurableStore(stub)

    # No usable Durable Object. Distinguish "not configured" from "configured
    # but unreachable", because only the second one is a bug worth surfacing.
    if binding in (None, ""):
        return MemoryStore()

    try:
        note = type(binding).__name__
    except Exception:
        note = "binding"
    return UnavailableStore(note)


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
