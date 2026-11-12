"""The Durable Object contract.

Exercised against a fake KV context so the persistence path — the one thing that
cannot be re-run in production without losing state — is verified before deploy.

The fake models Cloudflare's **KV** API (`ctx.storage.get/put/delete`), which is
async and returns Python values. An earlier version of this file modelled the
SQLite API instead, with rows shaped the way a fake assumes; production returned
`pyodide.ffi.JsProxy` rows and every read raised

    TypeError: 'pyodide.ffi.JsProxy' object is not subscriptable

so the suite was green while the bot was broken. The fake now matches the API
Cloudflare's own documentation uses for this purpose.
"""

from __future__ import annotations

import json

import pytest

from cta.durable_object import RadarState


class FakeStorage:
    """`ctx.storage` as the KV API: async get/put/delete over Python values."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.writes = 0

    async def get(self, key, default=None):
        return self.kv.get(key, default)

    async def put(self, key, value):
        self.writes += 1
        self.kv[key] = value

    async def delete(self, key):
        self.kv.pop(key, None)


class FakeCtx:
    def __init__(self):
        self.storage = FakeStorage()


@pytest.fixture
def state() -> RadarState:
    return RadarState(FakeCtx())


async def test_set_and_get_roundtrip(state):
    await state.storage_set("k", json.dumps({"a": 1}))
    assert json.loads(await state.storage_get("k")) == {"a": 1}


async def test_missing_key_returns_none(state):
    assert await state.storage_get("nope") is None


async def test_append_creates_list(state):
    await state.storage_append("log", json.dumps({"i": 1}), cap=10)
    await state.storage_append("log", json.dumps({"i": 2}), cap=10)
    items = json.loads(await state.storage_get("log"))
    assert items == [{"i": 1}, {"i": 2}]


async def test_append_respects_cap(state):
    for i in range(10):
        await state.storage_append("log", json.dumps({"i": i}), cap=3)
    items = json.loads(await state.storage_get("log"))
    assert items == [{"i": 7}, {"i": 8}, {"i": 9}]


async def test_append_recovers_from_corrupt_storage(state):
    await state.storage_set("log", "not json at all")
    await state.storage_append("log", json.dumps({"i": 1}), cap=5)
    assert json.loads(await state.storage_get("log")) == [{"i": 1}]


async def test_delete_removes_key(state):
    await state.storage_set("k", "v")
    await state.storage_delete("k")
    assert await state.storage_get("k") is None


async def test_kv_api_is_used_not_sql():
    """SQL rows arrive as JsProxy and `row["v"]` raises TypeError.

    The KV API is async and returns Python values, which is why Cloudflare's own
    Durable Object example uses it:
        value = (await self.ctx.storage.get("value")) or 0
        await self.ctx.storage.put("value", value)
    """
    ctx = FakeCtx()
    state = RadarState(ctx)
    await state.storage_set("k", json.dumps({"a": 1}))
    got = await state.storage_get("k")

    assert json.loads(got) == {"a": 1}
    assert ctx.storage.kv["k"] == json.dumps({"a": 1})


async def test_no_sql_handle_is_required():
    """The object must work on a context that has no `.sql` at all."""
    ctx = FakeCtx()
    assert not hasattr(ctx.storage, "sql")

    state = RadarState(ctx)
    await state.storage_set("x", "1")
    assert await state.storage_get("x") == "1"


async def test_a_sync_storage_is_tolerated():
    """Older runtimes expose sync methods; awaiting only when needed handles both."""

    class SyncStorage:
        def __init__(self):
            self.kv = {}

        def get(self, key, default=None):
            return self.kv.get(key, default)

        def put(self, key, value):
            self.kv[key] = value

        def delete(self, key):
            self.kv.pop(key, None)

    class Ctx:
        def __init__(self):
            self.storage = SyncStorage()

    state = RadarState(Ctx())
    await state.storage_set("y", "2")
    got = await state.storage_get("y")
    payload = await state.storage_append("L", '"a"')
    assert got == "2"
    assert json.loads(payload) == ["a"]


async def test_a_proxy_shaped_value_is_decoded_not_crashed():
    """The live failure: a value whose subscripting raises must not explode."""

    class Proxyish:
        def to_py(self):
            return "plain"

    class Storage(FakeStorage):
        async def get(self, key, default=None):
            return Proxyish()

    class Ctx:
        def __init__(self):
            self.storage = Storage()

    assert await RadarState(Ctx()).storage_get("k") == "plain"


async def test_a_store_round_trip_through_the_durable_object():
    """The path Telegram depends on: DurableStore -> stub -> RadarState."""

    class Ctx:
        def __init__(self):
            self.storage = FakeStorage()

    obj = RadarState(Ctx(), None)

    from cta.state import DurableStore

    store = DurableStore(obj)
    await store.set("counters", {"cycles": 3})
    assert await store.get("counters") == {"cycles": 3}
    await store.append("signals", {"id": "x"}, cap=10)
    assert (await store.get("signals"))[0]["id"] == "x"


# ── the exact live failure ──────────────────────────────────────────────────
async def test_a_jsproxy_row_never_reaches_the_reader():
    """The reported crash, reproduced as a test.

    Live traceback:
        File ".../cta/durable_object.py", line 114, in storage_get
            return row["v"]
        TypeError: 'pyodide.ffi.JsProxy' object is not subscriptable

    SQLite rows are JsProxy objects. The store no longer reads rows at all — it
    uses the KV API — so this class of failure cannot recur. This test asserts
    the storage layer never exposes an object that has to be subscripted.
    """

    class JsProxyLike:
        """Shaped like a Pyodide proxy: attribute access only, no __getitem__."""

        def __getitem__(self, key):
            raise TypeError("'pyodide.ffi.JsProxy' object is not subscriptable")

        def to_py(self):
            return '{"recovered": true}'

    class Storage(FakeStorage):
        async def get(self, key, default=None):
            return JsProxyLike()

    class Ctx:
        def __init__(self):
            self.storage = Storage()

    got = await RadarState(Ctx()).storage_get("anything")
    assert isinstance(got, str)
    assert json.loads(got) == {"recovered": True}


async def test_storage_returns_python_strings_only():
    """Every read must be a str or None — never a proxy, never a row object."""
    ctx = FakeCtx()
    state = RadarState(ctx)

    await state.storage_set("a", '{"x": 1}')
    assert isinstance(await state.storage_get("a"), str)
    assert await state.storage_get("missing") is None


async def test_the_full_bot_path_works_on_the_kv_durable_object():
    """End to end: DurableStore -> RadarState -> KV -> back, as Telegram uses it."""
    from cta.state import DurableStore

    ctx = FakeCtx()
    store = DurableStore(RadarState(ctx, None))

    await store.set("counters", {"cycles": 7})
    assert await store.get("counters") == {"cycles": 7}

    await store.append("trace", {"event": "update"}, cap=5)
    await store.append("trace", {"event": "reply"}, cap=5)
    trace = await store.get("trace")
    assert [t["event"] for t in trace] == ["update", "reply"]

    await store.delete("counters")
    assert await store.get("counters") is None
