"""Trader classification from raw fills.

The regression these tests lock down: `stats_from_fills` used to return a
`TraderStats` whose `grade` was still the "REJECT" placeholder unless the caller
remembered to call `classify_trader` afterwards. Any caller that forgot produced
a fully un-graded roster, every convergence cluster collapsed to zero members,
and the demo silently emitted no signals at all.
"""

from __future__ import annotations

from cta.classifier import classify_trader, stats_from_fills

from .conftest import make_fills

STRONG_FILLS = dict(n=260, days=420, edge=120.0, seed=11)


def test_stats_from_fills_grades_by_default():
    stats = stats_from_fills("0xgood", make_fills(**STRONG_FILLS), account_value_usd=80_000)
    assert stats.grade in ("ELITE", "PROVEN")
    assert stats.score > 0


def test_skipping_grading_is_opt_in_and_stats_stay_consistent():
    stats = stats_from_fills(
        "0xgood", make_fills(**STRONG_FILLS), account_value_usd=80_000, grade=False
    )
    assert stats.grade == "REJECT"
    # Explicit skip must not change the numbers, only the grade.
    graded = stats_from_fills("0xgood", make_fills(**STRONG_FILLS), account_value_usd=80_000)
    assert stats.n_trades == graded.n_trades
    assert stats.profit_factor == graded.profit_factor
    grade, _ = classify_trader(stats)
    assert grade in ("ELITE", "PROVEN")


def test_empty_fills_are_rejected_with_zero_score():
    stats = stats_from_fills("0xnone", [], account_value_usd=10_000)
    assert stats.grade == "REJECT"
    assert stats.score == 0
    assert stats.reasons


def test_weak_edge_is_not_promoted_to_elite():
    """Noise should not be crowned: a coin-flip trader must not score ELITE."""
    stats = stats_from_fills("0xnoise", make_fills(edge=0.0, seed=7), account_value_usd=50_000)
    assert stats.grade != "ELITE"


def test_profitable_but_thin_history_stays_watchful():
    """14 days of history is not enough evidence for ELITE regardless of PnL."""
    stats = stats_from_fills("0xnew", make_fills(n=40, days=14, edge=200.0), account_value_usd=50_000)
    assert stats.grade != "ELITE"
    # The rejection must say *why*, in the user's language, not just fail silently.
    assert any("صفقات" in r or "عمر" in r for r in stats.reasons)
