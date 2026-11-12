"""Convergence gates — the rules that stop the system crying wolf."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cta.config import Config
from cta.convergence import (
    detect_convergence,
    enforce_independence,
    net_deltas,
    strip_hedged,
)
from cta.models import Position

from .conftest import make_position

UTC = timezone.utc
CFG = Config()


def test_three_agreeing_traders_produce_one_cluster(three_traders):
    clusters = detect_convergence(three_traders, CFG)
    assert len(clusters) == 1
    assert clusters[0].n_members == 3
    assert clusters[0].coin == "BTC"
    assert clusters[0].side == "LONG"


def test_single_trader_produces_nothing():
    assert detect_convergence([make_position("0xsolo")], CFG) == []


def test_two_traders_are_enough():
    pair = [
        make_position("0xa", entry=65_000, minutes_ago=20),
        make_position("0xb", entry=65_020, minutes_ago=35),
    ]
    clusters = detect_convergence(pair, CFG)
    assert len(clusters) == 1
    assert clusters[0].n_members == 2


def test_opposite_directions_never_cluster():
    mixed = [
        make_position("0xa", side="LONG"),
        make_position("0xb", side="SHORT"),
    ]
    assert detect_convergence(mixed, CFG) == []


def test_wide_price_spread_is_rejected():
    """1.5% apart is not agreement; it is two different trades."""
    wide = [
        make_position("0xa", entry=65_000),
        make_position("0xb", entry=66_000),
    ]
    assert detect_convergence(wide, CFG) == []


def test_stale_entries_are_rejected():
    """Regression: relative timing alone let a 6-hour-old cluster through."""
    stale = [
        make_position("0xa", minutes_ago=360),
        make_position("0xb", minutes_ago=380),
    ]
    assert detect_convergence(stale, CFG) == []


def test_cluster_with_large_internal_time_span_is_rejected():
    spread_in_time = [
        make_position("0xa", minutes_ago=5),
        make_position("0xb", minutes_ago=180),
    ]
    assert detect_convergence(spread_in_time, CFG) == []


def test_hedged_wallet_is_stripped():
    """Long + short of equal size is no opinion at all."""
    hedged = [
        make_position("0xh", side="LONG", size_usd=100_000),
        make_position("0xh", side="SHORT", size_usd=100_000, source="dydx"),
    ]
    assert strip_hedged(hedged) == []


def test_net_delta_nets_across_venues():
    positions = [
        make_position("0xh", side="LONG", size_usd=100_000),
        make_position("0xh", side="SHORT", size_usd=40_000, source="dydx"),
    ]
    net = net_deltas(positions)
    assert net[("0xh", "BTC")] == 60_000


def test_partially_hedged_wallet_keeps_its_net_view():
    positions = [
        make_position("0xh", side="LONG", size_usd=100_000),
        make_position("0xh", side="SHORT", size_usd=40_000, source="dydx"),
        make_position("0xother", side="LONG", size_usd=50_000),
    ]
    clusters = detect_convergence(positions, CFG)
    assert len(clusters) == 1
    assert clusters[0].n_members == 2


def test_only_proven_traders_count():
    watchers = [
        make_position("0xa", quality="WATCH", quality_score=55),
        make_position("0xb", quality="WATCH", quality_score=57),
    ]
    assert detect_convergence(watchers, CFG) == []


def test_grades_mix_but_need_two_qualified():
    mixed = [
        make_position("0xa", quality="ELITE", quality_score=90),
        make_position("0xb", quality="REGULAR", quality_score=40),
    ]
    assert detect_convergence(mixed, CFG) == []


def test_high_leverage_median_is_rejected():
    degen = [
        make_position("0xa", leverage=40, quality_score=80),
        make_position("0xb", leverage=50, quality_score=80),
    ]
    assert detect_convergence(degen, CFG) == []


def test_independence_drops_twinned_wallets():
    """Two wallets opening the same coin within seconds are one actor."""
    now = datetime.now(UTC)
    twins = [
        Position("0xa", "hyperliquid", "BTC", 2.0, 65_000, 100_000, 3.0,
                 opened_at=now, quality_score=90, quality="ELITE"),
        Position("0xb", "hyperliquid", "BTC", 2.0, 65_000, 100_000, 3.0,
                 opened_at=now + timedelta(seconds=4), quality_score=80, quality="PROVEN"),
    ]
    kept = enforce_independence(twins)
    assert len(kept) == 1
    assert kept[0].wallet == "0xa"  # higher quality survives


def test_clusters_are_sorted_by_strength():
    strong = [
        make_position("0xa", entry=65_000, minutes_ago=5),
        make_position("0xb", entry=65_005, minutes_ago=11),
        make_position("0xc", entry=65_002, minutes_ago=17),
    ]
    weaker = [
        make_position("0xd", coin="ETH", entry=3_000, minutes_ago=30),
        make_position("0xe", coin="ETH", entry=3_008, minutes_ago=40),
    ]
    clusters = detect_convergence(strong + weaker, CFG)
    assert [c.n_members for c in clusters] == [3, 2]


def test_empty_input_is_safe():
    assert detect_convergence([], CFG) == []


def test_zero_size_position_ignored():
    flat = make_position("0xa")
    flat.size_usd = 0.0
    assert detect_convergence([flat, make_position("0xb")], CFG) == []
