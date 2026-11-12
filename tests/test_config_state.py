"""Configuration parsing and state persistence."""

from __future__ import annotations

from cta.config import GRADE_ORDER, Config
from cta.state import (
    DurableStore,
    MemoryStore,
    bump_counter,
    get_chat_history,
    record_chat,
    remember_signal,
    store_from_env,
)

from .conftest import FakeEnv


def test_config_accepts_a_mapping():
    cfg = Config.from_env({"MIN_INDEPENDENT_TRADERS": "4", "DRY_RUN": "true"})
    assert cfg.min_independent_traders == 4
    assert cfg.dry_run is True


def test_config_accepts_an_attribute_env():
    """Regression: the Worker's env object is not a dict and not iterable."""
    cfg = Config.from_env(FakeEnv(MIN_INDEPENDENT_TRADERS="3", ENTRY_TOLERANCE_BPS="25"))
    assert cfg.min_independent_traders == 3
    assert cfg.entry_tolerance_bps == 25.0


def test_config_ignores_blank_values():
    cfg = Config.from_env({"MIN_INDEPENDENT_TRADERS": "", "AI_MODEL": ""})
    assert cfg.min_independent_traders == 2          # default preserved
    assert cfg.ai_model.startswith("@cf/")


def test_config_survives_garbage_values():
    cfg = Config.from_env({"MIN_INDEPENDENT_TRADERS": "not-a-number"})
    assert cfg.min_independent_traders == 2


def test_config_parses_lists():
    cfg = Config.from_env({"ENABLED_SOURCES": "hyperliquid, dydx"})
    assert cfg.enabled_sources == ("hyperliquid", "dydx")


def test_source_enabled():
    cfg = Config(enabled_sources=("hyperliquid",))
    assert cfg.source_enabled("hyperliquid") is True
    assert cfg.source_enabled("gmx") is False


def test_watchlist_min_rank_maps_grade_names():
    assert Config(watchlist_min_grade="ELITE").watchlist_min_rank == GRADE_ORDER["ELITE"]
    assert Config(watchlist_min_grade="nonsense").watchlist_min_rank == 2


async def test_memory_store_roundtrip():
    store = MemoryStore()
    await store.set("k", {"a": 1})
    assert await store.get("k") == {"a": 1}
    assert await store.get("missing", "fallback") == "fallback"


async def test_append_is_capped():
    store = MemoryStore()
    for i in range(10):
        await store.append("log", i, cap=5)
    items = await store.get("log")
    assert items == [5, 6, 7, 8, 9]


async def test_store_from_env_falls_back_to_memory():
    assert isinstance(store_from_env(FakeEnv()), MemoryStore)


async def test_store_from_env_uses_durable_object():
    class Stub:
        def __init__(self):
            self.data = {}

        async def storage_set(self, key, value):
            self.data[key] = value

        async def storage_get(self, key):
            return self.data.get(key)

    store = store_from_env(FakeEnv(STATE=Stub()))
    assert isinstance(store, DurableStore)
    await store.set("x", {"y": 2})
    assert await store.get("x") == {"y": 2}


async def test_signal_and_counter_helpers():
    store = MemoryStore()
    await remember_signal(store, {"id": "s1"})
    signals = await store.get("signals")
    assert signals[0]["id"] == "s1"

    assert await bump_counter(store, "cycles") == 1
    assert await bump_counter(store, "cycles") == 2


async def test_chat_history_is_bounded():
    store = MemoryStore()
    for i in range(20):
        await record_chat(store, "42", "user", f"m{i}", cap=10)
    history = await get_chat_history(store, "42", limit=5)
    assert len(history) == 5
    assert history[-1]["content"] == "m19"


async def test_chat_history_truncates_long_messages():
    store = MemoryStore()
    await record_chat(store, "1", "user", "x" * 5000)
    history = await get_chat_history(store, "1")
    assert len(history[0]["content"]) <= 1500
