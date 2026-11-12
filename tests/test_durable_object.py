"""The Durable Object contract.

Exercised against a fake SQL context so the persistence path — the one thing that
cannot be re-run in production without losing state — is verified before deploy.
"""

from __future__ import annotations

import json

import pytest

from cta.durable_object import RadarState


class FakeCursor:
    """A `SqlStorageCursor`: iterable, sync `toArray()`, not a promise.

    Official docs for Python show `self.sql.exec(...)` with **no await**; the
    real cursor is synchronous. This fake previously used `async def`, which
    hid a bug where the code awaited a non-awaitable and every DO call raised
    TypeError -> HTTP 500 -> "Wrong response from the webhook: 500".
    """

    def __init__(self, rows):
        self._rows = list(rows)

    def toArray(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class FakeSQL:
    """Tiny in-memory stand-in for Durable Object SQLite."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.statements: list[str] = []

    def exec(self, statement: str, *params):
        self.statements.append(statement.strip().split("\n")[0])
        sql = " ".join(statement.split())
        if sql.startswith("CREATE TABLE"):
            return FakeCursor([])
        if sql.startswith("SELECT v FROM kv"):
            key = params[0]
            return FakeCursor([{"v": self.kv[key]}] if key in self.kv else [])
        if sql.startswith("INSERT INTO kv"):
            key, value = params[0], params[1]
            self.kv[key] = value
            return FakeCursor([])
        if sql.startswith("DELETE FROM kv"):
            self.kv.pop(params[0], None)
            return FakeCursor([])
        return FakeCursor([])


class FakeStorage:
    def __init__(self):
        self.sql = FakeSQL()


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


async def test_schema_created_once(state):
    await state.storage_set("a", "1")
    await state.storage_set("b", "2")
    creates = [s for s in state.ctx.storage.sql.statements if s.startswith("CREATE TABLE")]
    assert len(creates) == 1


async def test_radar_state_works_as_a_store():
    """The DO must satisfy the same interface the engine expects."""
    from cta.state import DurableStore

    store = DurableStore(RadarState(FakeCtx()))
    await store.set("watchlist", [{"wallet": "0x1"}])
    assert await store.get("watchlist") == [{"wallet": "0x1"}]
    await store.append("signals", {"id": "s1"}, cap=10)
    assert await store.get("signals") == [{"id": "s1"}]


# ── the real platform contract, which our fake used to contradict ───────────
def test_durable_object_takes_ctx_and_env():
    """`DurableObject.__init__(self, ctx, env)` — verified against the SDK.

    Calling `super().__init__(ctx)` raises
        TypeError: DurableObject.__init__() missing 1 required positional
        argument: 'env'
    so the object cannot be constructed, every RPC fails, and the HTTP handler
    returns 500. Telegram reported exactly that:
        Wrong response from the webhook: 500 Internal Server Error
    """
    import inspect

    from cta.durable_object import DurableObject, RadarState

    base = list(inspect.signature(DurableObject.__init__).parameters)
    assert base[:2] == ["self", "ctx"]
    assert "env" in base
    # and RadarState must accept both, so the runtime can pass them
    params = list(inspect.signature(RadarState.__init__).parameters)
    assert params[0] == "self" and params[1] == "ctx"
    assert params[2] == "env"


def test_radar_state_accepts_the_two_arguments_the_runtime_sends():
    class Ctx:
        class storage:
            pass

    inst = RadarState(Ctx(), object())
    assert inst.ctx is not None
    assert inst.env is not None


async def test_sql_api_is_used_synchronously():
    """Python's DO SQL API is synchronous — the docs show no `await`.

        self.sql.exec("CREATE TABLE ...")     # not awaited
        cursor = self.sql.exec("SELECT ...")  # returns a cursor
        rows = cursor.toArray()               # synchronous

    Awaiting either raises
        TypeError: object SqlStorageCursor can't be used in 'await' expression
    """

    class StrictCursor:
        def __init__(self, rows):
            self._rows = rows

        def toArray(self):  # sync on purpose
            return self._rows

        def __iter__(self):
            return iter(self._rows)

    class StrictSQL:
        """Rejects being awaited: raises TypeError if the caller awaits it."""

        def __init__(self):
            self.kv = {}

        def exec(self, statement, *params):
            sql = " ".join(statement.split())
            if sql.startswith("CREATE TABLE"):
                return StrictCursor([])
            if sql.startswith("SELECT"):
                key = params[0]
                return StrictCursor([{"v": self.kv[key]}] if key in self.kv else [])
            if sql.startswith("INSERT"):
                self.kv[params[0]] = params[1]
                return StrictCursor([])
            if sql.startswith("DELETE"):
                self.kv.pop(params[0], None)
                return StrictCursor([])
            return StrictCursor([])

    class Ctx:
        def __init__(self):
            self.storage = type("S", (), {})()
            self.storage.sql = StrictSQL()

    state = RadarState(Ctx(), None)
    await state.storage_set("k", json.dumps({"a": 1}))
    assert json.loads(await state.storage_get("k")) == {"a": 1}

    payload = await state.storage_append("list", json.dumps({"n": 1}))
    assert json.loads(payload) == [{"n": 1}]

    await state.storage_delete("k")
    assert await state.storage_get("k") is None


async def test_a_store_round_trip_through_the_durable_object():
    """The path Telegram depends on: DurableStore -> stub -> RadarState."""

    class StrictCursor:
        def __init__(self, rows):
            self._rows = rows

        def toArray(self):
            return self._rows

    class Sql:
        def __init__(self):
            self.kv = {}

        def exec(self, statement, *params):
            sql = " ".join(statement.split())
            if sql.startswith("CREATE"):
                return StrictCursor([])
            if sql.startswith("SELECT"):
                return StrictCursor([{"v": self.kv[params[0]]}] if params[0] in self.kv else [])
            if sql.startswith("INSERT"):
                self.kv[params[0]] = params[1]
                return StrictCursor([])
            if sql.startswith("DELETE"):
                self.kv.pop(params[0], None)
                return StrictCursor([])
            return StrictCursor([])

    class Ctx:
        def __init__(self):
            self.storage = type("S", (), {})()
            self.storage.sql = Sql()

    obj = RadarState(Ctx(), None)

    from cta.state import DurableStore

    store = DurableStore(obj)
    await store.set("counters", {"cycles": 3})
    assert await store.get("counters") == {"cycles": 3}
    await store.append("signals", {"id": "x"}, cap=10)
    assert (await store.get("signals"))[0]["id"] == "x"
