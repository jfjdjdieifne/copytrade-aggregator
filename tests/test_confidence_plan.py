"""Confidence arithmetic and trade-plan safety."""

from __future__ import annotations

from cta.confidence import score_cluster
from cta.config import Config
from cta.models import Cluster
from cta.plan import _trimmed_median, build_plan

from .conftest import make_position

CFG = Config()


def _cluster(n: int = 3, quality: int = 80, spread_bps: float = 10.0, sources=None):
    members = [
        make_position(f"0x{i}", entry=65_000 + i, quality_score=quality,
                      source=(sources[i] if sources else "hyperliquid"))
        for i in range(n)
    ]
    return Cluster(
        coin="BTC",
        side="LONG",
        members=members,
        entry_center=65_001.0,
        spread_bps=spread_bps,
        time_span_min=20.0,
        sources=sorted({m.source for m in members}),
    )


# ── confidence ───────────────────────────────────────────────────────────────
def test_score_is_within_bounds():
    conf = score_cluster(_cluster(), CFG)
    assert 0 <= conf.total <= 100


def test_more_traders_score_higher():
    two = score_cluster(_cluster(n=2), CFG)
    four = score_cluster(_cluster(n=4), CFG)
    assert four.total > two.total
    assert four.breakdown["consensus"] > two.breakdown["consensus"]


def test_tighter_spread_scores_higher():
    tight = score_cluster(_cluster(spread_bps=5), CFG)
    loose = score_cluster(_cluster(spread_bps=45), CFG)
    assert tight.total > loose.total


def test_source_diversity_adds_points():
    same = score_cluster(_cluster(sources=["hyperliquid"] * 3), CFG)
    mixed = score_cluster(_cluster(sources=["hyperliquid", "dydx", "gmx"]), CFG)
    assert mixed.breakdown["diversity"] == 5
    assert same.breakdown["diversity"] == 0


def test_single_trader_can_never_be_emitted():
    conf = score_cluster(_cluster(n=1), CFG)
    assert conf.level == "REJECT"
    assert conf.emitted is False


def test_low_score_is_downgraded_to_watch():
    conf = score_cluster(_cluster(n=2, quality=40, spread_bps=48), CFG)
    assert conf.level in ("WATCH", "REJECT")
    assert conf.emitted is False


def test_breakdown_sums_to_total():
    conf = score_cluster(_cluster(), CFG)
    assert sum(conf.breakdown.values()) == conf.total


def test_every_score_has_reasons():
    conf = score_cluster(_cluster(), CFG)
    assert len(conf.reasons) >= 5


# ── plan ─────────────────────────────────────────────────────────────────────
def test_stop_from_traders_is_used_and_flagged_as_source():
    cluster = _cluster(n=3)
    for i, m in enumerate(cluster.members):
        m.stop = 64_000 + i * 10
        m.target = 68_000 + i * 10
    plan = build_plan(cluster, CFG, market_price=65_001)
    assert plan is not None
    assert plan.stop_source == "trader"
    assert plan.target_source == "trader"
    assert plan.stop < plan.entry < plan.target


def test_missing_stop_falls_back_to_atr_and_is_labelled():
    plan = build_plan(_cluster(), CFG, atr_hint=400.0, market_price=65_001)
    assert plan is not None
    assert plan.stop_source == "atr"
    assert any("محسوب" in n for n in plan.notes)


def test_no_stop_and_no_atr_yields_unjustified_plan_flag():
    plan = build_plan(_cluster(), CFG, market_price=65_001)
    assert plan is not None
    assert plan.stop_source == "none"
    assert any("بلا ستوب" in n for n in plan.notes)


def test_wrong_side_stop_is_rejected():
    cluster = _cluster(n=2)
    for m in cluster.members:
        m.stop = 70_000.0  # above entry on a long -> nonsense
    plan = build_plan(cluster, CFG, market_price=65_001)
    assert plan is not None
    assert plan.stop is None
    assert any("فوق الدخول" in n for n in plan.notes)


def test_short_plan_orientation_is_mirrored():
    cluster = _cluster(n=2)
    cluster.side = "SHORT"
    for m in cluster.members:
        m.stop = 66_000.0
        m.target = 61_000.0
    plan = build_plan(cluster, CFG, market_price=65_001)
    assert plan is not None
    assert plan.stop > plan.entry > plan.target


def test_excessive_slippage_refuses_the_plan():
    """If price already ran away, there is no trade — say so by returning None."""
    cluster = _cluster(n=3)
    plan = build_plan(cluster, CFG, market_price=66_500)  # +2.3%
    assert plan is None


def test_rr_is_computed_from_real_levels():
    cluster = _cluster(n=2)
    for m in cluster.members:
        m.stop = 64_000.0
        m.target = 68_000.0
    plan = build_plan(cluster, CFG, market_price=65_000)
    assert plan is not None
    assert plan.rr == round(3_000 / 1_000, 2)


def test_trimmed_median_ignores_outliers():
    values = [100, 102, 101, 99, 98, 5_000]
    assert _trimmed_median(values) == 100.5


def test_trimmed_median_handles_empty():
    assert _trimmed_median([]) is None
    assert _trimmed_median([0, -1]) is None
