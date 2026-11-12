"""The Durable Object contract.

Exercised against a fake SQL context so the persistence path — the one thing that
cannot be re-run in production without losing state — is verified before deploy.
"""

from __future__ import annotations

import json

import pytest

from cta.durable_object import RadarState


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def toArray(self):
        return self._rows


class FakeSQL:
    """Tiny in-memory stand-in for Durable Object SQLite."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.statements: list[str] = []

    async def exec(self, statement: str, *params):
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
