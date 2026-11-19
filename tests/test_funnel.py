"""The funnel: telling patience apart from a misconfiguration.

Both look like silence. One is the market being quiet; the other is a radar
that will never fire. The operator cannot tell them apart from the outside,
and the difference costs days.
"""

from datetime import datetime, timedelta, timezone

from cta.config import Config
from cta.funnel import diagnose, near_misses
from cta.models import Position

UTC = timezone.utc
CFG = Config()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def mk(wallet, coin, side, entry, size=100_000.0, when=None, src="sim",
       q="ELITE", lev=3.0):
    return Position(
        wallet=wallet, source=src, coin=coin,
        signed_size=(1.0 if side == "LONG" else -1.0) * size / max(entry, 1e-9),
        entry=entry, size_usd=size, leverage=lev,
        opened_at=when or NOW, mark_price=entry, quality=q, quality_score=90,
    )


def test_no_positions_at_all_points_at_the_venues():
    report = diagnose([], [], CFG, now=NOW)
    assert "ما نشوف ولا مركز" in report["verdict"]
    assert report["near_misses"] == []


def test_traders_on_different_coins_is_a_quiet_market():
    pos = [mk("w1", "BTC", "LONG", 100), mk("w2", "ETH", "LONG", 100)]
    report = diagnose(pos, [{"wallet": "w1"}, {"wallet": "w2"}], CFG, now=NOW)
    assert "ما بيثبت إن السوق هادي" in report["verdict"]
    assert report["near_misses"] == []


def test_a_group_stopped_by_the_time_window_names_that_window():
    pos = [
        mk("w1", "BTC", "LONG", 100.0, when=NOW),
        mk("w2", "BTC", "LONG", 100.2, when=NOW - timedelta(hours=5)),
    ]
    misses = near_misses(pos, CFG, now=NOW)
    assert len(misses) == 1
    assert misses[0]["coin"] == "BTC"
    assert misses[0]["n_independent"] == 2
    assert any("تباعد الوقت" in g for g in misses[0]["failed_gates"])
    assert misses[0]["passed"] is False
    assert "تباعد الوقت" in diagnose(pos, [{"wallet": "w1"}, {"wallet": "w2"}], CFG, now=NOW)["verdict"]


def test_a_group_stopped_by_price_spread_says_so_not_nobody_agrees():
    """Two professionals long the same coin at very different prices.

    Clustering at the configured tolerance used to split them into two silent
    groups, and the report said "nobody is on the same coin" — which is false,
    and sends the operator looking at the wrong knob.
    """
    pos = [
        mk("w1", "BTC", "LONG", 100.0, when=NOW),
        mk("w2", "BTC", "LONG", 108.0, when=NOW - timedelta(minutes=5)),
    ]
    misses = near_misses(pos, CFG, now=NOW)
    assert len(misses) == 1
    assert any("انتشار السعر" in g for g in misses[0]["failed_gates"])
    assert "انتشار السعر" in diagnose(pos, [{"wallet": "w1"}, {"wallet": "w2"}], CFG, now=NOW)["verdict"]


def test_a_group_that_passes_every_gate_is_reported_as_ready():
    pos = [
        mk("w1", "BTC", "LONG", 100.0, when=NOW - timedelta(minutes=3)),
        mk("w2", "BTC", "LONG", 100.2, when=NOW - timedelta(minutes=5),
           src="sim-beta"),
        mk("w3", "BTC", "LONG", 100.1, when=NOW - timedelta(minutes=8)),
    ]
    misses = near_misses(pos, CFG, now=NOW)
    assert misses[0]["passed"] is True
    assert misses[0]["failed_gates"] == []
    assert misses[0]["n_independent"] == 3
    assert misses[0]["n_sources"] == 2
    verdict = diagnose(pos, [{"wallet": "w1"}, {"wallet": "w2"}], CFG, now=NOW)["verdict"]
    assert "الفحوص الأولية فقط" in verdict
    assert "إجماع جاهز" not in verdict


def test_an_old_cluster_is_reported_as_stale():
    """Everyone agrees, but they agreed this morning. Too late to join."""
    old = NOW - timedelta(hours=6)
    pos = [
        mk("w1", "BTC", "LONG", 100.0, when=old),
        mk("w2", "BTC", "LONG", 100.1, when=old - timedelta(minutes=2)),
    ]
    misses = near_misses(pos, CFG, now=NOW)
    assert any("عمر الأحدث" in g for g in misses[0]["failed_gates"])


def test_opposite_sides_are_never_one_group():
    pos = [
        mk("w1", "BTC", "LONG", 100.0),
        mk("w2", "BTC", "SHORT", 100.0),
    ]
    assert near_misses(pos, CFG, now=NOW) == []


def test_hedged_wallets_are_excluded_before_grouping():
    """A wallet holding both directions has no view; it must not look like one."""
    pos = [
        mk("w1", "BTC", "LONG", 100.0, size=100_000.0),
        mk("w1", "BTC", "SHORT", 100.0, size=100_000.0),
        mk("w2", "BTC", "LONG", 100.1, size=100_000.0),
    ]
    misses = near_misses(pos, CFG, now=NOW)
    # w1 nets to zero, so it drops out and only w2 remains: no consensus.
    assert misses == []


def test_the_report_carries_the_thresholds_so_it_can_be_calibrated():
    report = diagnose([mk("w1", "BTC", "LONG", 100.0)], [{"wallet": "w1"}],
                      CFG, now=NOW)
    t = report["thresholds"]
    assert t["entry_tolerance_bps"] == CFG.entry_tolerance_bps
    assert t["convergence_window_min"] == CFG.convergence_window_min
    assert t["min_independent"] == CFG.min_independent_traders


def test_the_closest_miss_is_reported_first():
    """Sorted so the operator sees the one worth acting on, not a random one."""
    close = [mk("w1", "BTC", "LONG", 100.0, when=NOW - timedelta(minutes=2)),
             mk("w2", "BTC", "LONG", 100.1, when=NOW - timedelta(minutes=3))]
    # 12% apart and nine hours apart: two gates failed, so it is further from
    # firing than the BTC group above.
    far = [mk("w3", "ETH", "LONG", 100.0, when=NOW - timedelta(hours=9)),
           mk("w4", "ETH", "LONG", 112.0, when=NOW - timedelta(hours=10))]
    misses = near_misses(close + far, CFG, now=NOW)
    assert misses[0]["coin"] == "BTC", "the nearly-ready group should come first"
    assert misses[-1]["coin"] == "ETH"


def test_wide_diagnostic_group_does_not_hide_a_tight_pair():
    # 100 and 100.2 agree on price. 110 must not cause the diagnostic to
    # attribute the entire group to the price gate and hide the passing pair.
    positions = [
        mk('w1', 'BTC', 'LONG', 100, size=100_000),
        mk('w2', 'BTC', 'LONG', 100.2, size=130_000),
        mk('w3', 'BTC', 'LONG', 110, size=160_000),
    ]
    misses = near_misses(positions, CFG, now=NOW)
    assert misses[0]['n_independent'] == 2
    assert misses[0]['passed']
    assert misses[0]['spread_bps'] < CFG.entry_tolerance_bps
    assert any(m['n_raw'] == 3 and not m['passed'] for m in misses)


def test_identical_tight_and_wide_groups_are_not_reported_twice():
    positions = [mk('w1', 'BTC', 'LONG', 100, size=100_000),
                 mk('w2', 'BTC', 'LONG', 100.2, size=130_000)]
    assert len(near_misses(positions, CFG, now=NOW)) == 1
