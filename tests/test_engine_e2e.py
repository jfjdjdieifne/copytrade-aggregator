"""End-to-end: the exact path production runs, with canned venue data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cta.config import Config
from cta.demo import FakeVenue, run_demo
from cta.discovery import control_group, select_candidates, triage_row
from cta.engine import collect_positions, run_cycle, scan, signal_id_for
from cta.models import Position
from cta.outcomes import evaluate, summarize
from cta.state import KEY_WATCHLIST, MemoryStore
from cta.worker import _json, _method, _path


UTC = timezone.utc
NOW = datetime.now(UTC)


# ── discovery ────────────────────────────────────────────────────────────────
def test_triage_rejects_thin_accounts():
    row = {"wallet": "0xa", "account_value": 100, "windows": {
        "month": {"pnl": 50, "roi": 10, "vlm": 5000},
        "week": {"pnl": 10, "roi": 2, "vlm": 900}}}
    assert triage_row(row, Config()) is None


def test_triage_rejects_impossible_roi():
    row = {"wallet": "0xa", "account_value": 50_000, "windows": {
        "month": {"pnl": 90_000, "roi": 400, "vlm": 5_000_000},
        "week": {"pnl": 1_000, "roi": 5, "vlm": 900_000}}}
    assert triage_row(row, Config()) is None


def test_triage_accepts_a_viable_wallet():
    row = {"wallet": "0xA", "account_value": 50_000, "windows": {
        "month": {"pnl": 8_000, "roi": 16, "vlm": 3_000_000},
        "week": {"pnl": 1_500, "roi": 3, "vlm": 600_000}}}
    cand = triage_row(row, Config())
    assert cand is not None
    assert cand.wallet == "0xa"
    assert cand.triage_score > 0


def test_candidates_are_deduped_and_ordered():
    rows = []
    for i in range(5):
        rows.append({"wallet": f"0x{i}", "account_value": 10_000 * (i + 1), "windows": {
            "month": {"pnl": 1_000 * (i + 1), "roi": 10 + i, "vlm": 3_000_000},
            "week": {"pnl": 500, "roi": 2, "vlm": 600_000 * (i + 1)}}})
    rows.append(rows[0])  # duplicate
    selected = select_candidates(rows, Config())
    assert len(selected) == 5
    assert selected[0].triage_score >= selected[-1].triage_score


def test_control_group_is_bounded():
    rows = [{"wallet": f"0x{i}", "account_value": 20_000, "windows": {
        "month": {"pnl": 2_000, "roi": 10, "vlm": 500_000},
        "week": {"pnl": 400, "roi": 2, "vlm": 200_000}}} for i in range(500)]
    cands = select_candidates(rows, Config(discovery_max_candidates=500))
    assert len(control_group(cands, size=50)) == 50


# ── engine ───────────────────────────────────────────────────────────────────
def _venues(positions: list[Position], prices=None):
    rows = [
        {"wallet": p.wallet, "coin": p.coin, "signed_size": p.signed_size,
         "entry": p.entry, "size_usd": p.size_usd, "leverage": p.leverage}
        for p in positions
    ]
    hl = FakeVenue("hyperliquid", [r for r, p in zip(rows, positions) if p.source == "hyperliquid"],
                   fills={}, prices=prices or {"BTC": 65_030.0})
    dydx = FakeVenue("dydx", [r for r, p in zip(rows, positions) if p.source == "dydx"],
                     fills={}, prices={})
    return {"hyperliquid": hl, "dydx": dydx}


async def test_collect_positions_attaches_quality(three_traders):
    venues = _venues(three_traders)
    watchlist = [
        {"wallet": "0xalice", "source": "hyperliquid", "grade": "ELITE", "score": 90},
        {"wallet": "0xbob", "source": "dydx", "grade": "PROVEN", "score": 78},
        {"wallet": "0xcarol", "source": "hyperliquid", "grade": "PROVEN", "score": 81},
    ]
    positions = await collect_positions(venues, watchlist, Config())
    assert len(positions) == 3
    assert all(p.quality in ("ELITE", "PROVEN") for p in positions)


async def test_scan_emits_a_signal_for_three_agreeing_traders(three_traders):
    store = MemoryStore()
    watchlist = [
        {"wallet": p.wallet, "source": p.source, "grade": p.quality, "score": p.quality_score}
        for p in three_traders
    ]
    await store.set(KEY_WATCHLIST, watchlist)

    signals, clusters = await scan(_venues(three_traders), store, Config(), now=NOW)
    assert len(clusters) == 1
    assert len(signals) == 1
    assert signals[0].score >= 65
    assert signals[0].n_independent == 3


async def test_scan_never_emits_the_same_signal_twice(three_traders):
    store = MemoryStore()
    watchlist = [
        {"wallet": p.wallet, "source": p.source, "grade": p.quality, "score": p.quality_score}
        for p in three_traders
    ]
    await store.set(KEY_WATCHLIST, watchlist)
    venues = _venues(three_traders)

    first, _ = await scan(venues, store, Config(), now=NOW)
    second, _ = await scan(venues, store, Config(), now=NOW)
    assert len(first) == 1
    assert second == []


async def test_scan_with_empty_watchlist_is_safe():
    store = MemoryStore()
    signals, clusters = await scan({}, store, Config(), now=NOW)
    assert signals == [] and clusters == []


def test_signal_ids_are_stable_for_identical_clusters():
    a = signal_id_for("BTC", "LONG", 65_000.0, 1_700_000_000_000)
    b = signal_id_for("BTC", "LONG", 65_000.0, 1_700_000_000_000)
    c = signal_id_for("BTC", "LONG", 70_000.0, 1_700_000_000_000)
    assert a == b
    assert a != c


async def test_run_cycle_profiles_then_scans():
    """A cold start must build a watchlist and still return a report."""
    store = MemoryStore()

    leaderboard = [{"ethAddress": f"0x{i:02x}", "accountValue": "100000",
                    "windowPerformances": [
                        ["week", {"pnl": "2000", "roi": "0.04", "vlm": "900000"}],
                        ["month", {"pnl": "9000", "roi": "0.18", "vlm": "3000000"}]]}
                   for i in range(3)]
    fills = [
        {"time": 1_700_000_000_000 + i * 86_400_000, "coin": "BTC", "side": "B",
         "px": "65000", "sz": "0.1", "closedPnl": "120", "fee": "2"}
        for i in range(260)
    ]

    class Venue:
        name = "hyperliquid"

        async def health(self):
            return True

        async def leaderboard(self):
            return leaderboard

        @staticmethod
        def parse_leaderboard_row(row):
            from cta.sources import HyperliquidVenue

            return HyperliquidVenue.parse_leaderboard_row(row)

        async def fills(self, wallet, limit=2000):
            return fills

        @staticmethod
        def parse_fills(rows):
            from cta.sources import HyperliquidVenue

            return HyperliquidVenue.parse_fills(rows)

        async def positions(self, wallet):
            return []

        @staticmethod
        def parse_positions(wallet, rows):
            return []

        async def prices(self):
            return {"BTC": 65_000.0}

    report = await run_cycle({"hyperliquid": Venue()}, store, Config(), now=NOW)
    assert report["watchlist_size"] >= 1
    assert report["profiled"] is True


# ── outcomes ─────────────────────────────────────────────────────────────────
def test_outcome_detects_target_hit():
    sig = {"signal_id": "s1", "coin": "BTC", "side": "LONG",
           "entry_center": 65_000, "created_at": (NOW - timedelta(hours=5)).isoformat(),
           "plan": {"entry": 65_000, "stop": 63_000, "target": 67_000}}
    out = evaluate(sig, 67_500, now=NOW)
    assert out["hit_target"] is True
    assert out["r_multiple"] == pytest.approx(1.25, abs=0.01)


def test_outcome_detects_stop_hit_on_short():
    sig = {"signal_id": "s2", "coin": "ETH", "side": "SHORT",
           "entry_center": 3_000, "created_at": (NOW - timedelta(hours=5)).isoformat(),
           "plan": {"entry": 3_000, "stop": 3_100, "target": 2_800}}
    out = evaluate(sig, 3_150, now=NOW)
    assert out["hit_stop"] is True
    assert out["r_multiple"] < 0


def test_summarize_excludes_signals_younger_than_one_hour():
    outcomes = [
        {"signal_id": "a", "age_min": 20, "hit_target": True, "r_multiple": 1.0},
        {"signal_id": "b", "age_min": 300, "hit_target": False, "r_multiple": -1.0},
    ]
    stats = summarize(outcomes)
    assert stats["evaluated"] == 1
    assert stats["pending"] == 1


def test_summarize_keeps_the_latest_evaluation_per_signal():
    outcomes = [
        {"signal_id": "a", "age_min": 120, "hit_target": False, "r_multiple": -0.2},
        {"signal_id": "a", "age_min": 600, "hit_target": True, "r_multiple": 1.8},
    ]
    stats = summarize(outcomes)
    assert stats["evaluated"] == 1
    assert stats["hit_target"] == 1
    assert stats["mean_r"] == pytest.approx(1.8)


def test_summarize_empty():
    assert summarize([])["evaluated"] == 0


# ── offline demo (the acceptance test) ───────────────────────────────────────
async def test_demo_runs_end_to_end():
    """The demo is the project's smoke test: one signal, all negatives filtered."""
    assert await run_demo() == 0


# ── worker routing ───────────────────────────────────────────────────────────
def test_path_extraction():
    class Req:
        url = "https://radar.example.workers.dev/health?x=1"

    assert _path(Req()) == "/health"

    class Root:
        url = "https://radar.example.workers.dev"

    assert _path(Root()) == "/"


def test_method_defaults_to_get():
    class Req:
        method = "post"

    assert _method(Req()) == "POST"


def test_json_helper_sets_content_type():
    body, headers = _json({"ok": True})
    assert "application/json" in headers["content-type"]
    assert headers["status"] == 200
