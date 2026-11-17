"""The A/B lab: does requiring more traders actually help?

The operator's question was the right one and needed answering with a
measurement, not an opinion: *do three or more traders ever agree, or is that a
once-a-year event?*

These tests protect the instrument. A lab that quietly reports the wrong
numbers is worse than no lab, because it produces a confident threshold out of
noise — which is exactly the failure this module was built to prevent.
"""

from datetime import datetime, timezone

from cta.config import Config
from cta.models import Position
from cta.shadow import (
    KEY_SHADOW,
    chance_baseline,
    empty_lab,
    mark_all,
    observe_clusters,
    render_lab,
    size_histogram,
    summarize_lab,
)

UTC = timezone.utc
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
CFG = Config()


def member(wallet, coin, entry, stop, side="LONG", source="sim"):
    from cta.models import Cluster  # noqa: F401  (documented shape below)
    return Position(
        wallet=wallet, source=source, coin=coin,
        signed_size=(1.0 if side == "LONG" else -1.0) * 1000.0,
        entry=entry, size_usd=100_000.0, leverage=3.0,
        opened_at=NOW, mark_price=entry, stop=stop,
        quality="PROVEN", quality_score=80,
    )


def cluster(coin, side, n, entry=100.0):
    """A cluster of `n` traders on the same coin and side."""
    from cta.models import Cluster

    stop = entry * 0.98 if side == "LONG" else entry * 1.02
    members = [member(f"w{i}", coin, entry, stop, side) for i in range(n)]
    return Cluster(
        coin=coin, side=side, members=members, entry_center=entry,
        spread_bps=5.0, time_span_min=10.0, sources=["sim"] * n,
    )


# ── the histogram is the answer to "is three common?" ────────────────────────

def test_every_cluster_size_is_counted_with_no_quality_filter():
    lab = empty_lab()
    observe_clusters(lab, [cluster("BTC", "LONG", 2), cluster("ETH", "LONG", 3),
                           cluster("SOL", "SHORT", 3), cluster("ARB", "LONG", 5)],
                     CFG, {}, NOW)
    hist = size_histogram(lab)
    assert hist == {2: 1, 3: 2, 5: 1}
    assert lab["clusters_seen"] == 4


def test_a_size_is_counted_even_when_no_threshold_trades_it():
    """A size-7 cluster must appear even if the lab only bets on 2/3/4/5.

    Otherwise raising the threshold would erase the evidence that bigger
    agreements exist at all.
    """
    lab = empty_lab(thresholds=(2, 3))
    observe_clusters(lab, [cluster("BTC", "LONG", 7)], CFG, {}, NOW)
    assert size_histogram(lab) == {7: 1}


def test_the_histogram_survives_a_broken_cluster():
    lab = empty_lab()
    observe_clusters(lab, [cluster("BTC", "LONG", 3), None, "junk",
                           cluster("ETH", "LONG", 2)], CFG, {}, NOW)
    assert size_histogram(lab) == {2: 1, 3: 1}


# ── thresholds ───────────────────────────────────────────────────────────────

def test_a_threshold_only_bets_when_enough_traders_agree():
    lab = empty_lab(thresholds=(2, 3, 4, 5))
    observe_clusters(lab, [cluster("BTC", "LONG", 3)], CFG, {"BTC": 100.0}, NOW)
    opened = {b: len(a.get("open", []))
              for b, a in lab["accounts"].items()}
    # >=2 and >=3 qualify; >=4 and >=5 must stay flat.
    assert opened["n2"] == 1
    assert opened["n3"] == 1
    assert opened["n4"] == 0
    assert opened["n5"] == 0


def test_a_larger_agreement_feeds_every_threshold_below_it():
    lab = empty_lab(thresholds=(2, 3, 4, 5))
    observe_clusters(lab, [cluster("BTC", "LONG", 5)], CFG, {"BTC": 100.0}, NOW)
    for bucket in ("n2", "n3", "n4", "n5"):
        assert len(lab["accounts"][bucket].get("open", [])) == 1


def test_each_threshold_keeps_its_own_balance():
    """Separate accounts, so one threshold's loss cannot mask another's win."""
    lab = empty_lab(thresholds=(2, 3), start_balance=100.0)
    for bucket in ("n2", "n3"):
        assert lab["accounts"][bucket]["start_balance"] == 100.0
    assert lab["accounts"]["n2"] is not lab["accounts"]["n3"]


def test_the_same_coin_and_side_is_not_opened_twice():
    lab = empty_lab(thresholds=(2,))
    observe_clusters(lab, [cluster("BTC", "LONG", 3)], CFG, {"BTC": 100.0}, NOW)
    observe_clusters(lab, [cluster("BTC", "LONG", 4)], CFG, {"BTC": 100.0}, NOW)
    assert len(lab["accounts"]["n2"].get("open", [])) == 1


# ── the chance baseline ──────────────────────────────────────────────────────

def test_chance_baseline_agrees_with_hand_computed_poisson():
    """45 positions over 15 coins -> 30 buckets -> lambda 1.5.

    P(X>=3) = 1 - e^-1.5 (1 + 1.5 + 1.125) = 0.1912, so 30 * 0.1912 = 5.74.
    """
    out = chance_baseline(45, 15)
    assert abs(out[3] - 5.74) < 0.1
    assert out[3] > out[4] > out[5]


def test_chance_baseline_returns_nothing_when_there_is_nothing_to_scatter():
    for positions, coins in ((0, 10), (10, 0), (-5, 10), (10, -5)):
        assert chance_baseline(positions, coins) == {}


def test_three_traders_is_expected_by_chance_not_rare():
    """The whole point of the baseline, stated as an assertion.

    With 150 watched traders and a modest 20 coins, agreements of three happen
    several times per cycle by shuffling alone. "Three traders agreed" is
    therefore not evidence of anything until the gates are applied.
    """
    out = chance_baseline(150, 20)
    assert out[3] > 5, "3+ should be common under pure chance"
    assert out[5] < out[3], "bigger agreements must be rarer"


# ── reporting ────────────────────────────────────────────────────────────────

def test_a_win_rate_over_zero_trades_is_shown_as_unknown_not_zero():
    """`0.0%` over no trades reads as 'lost everything'. It is unknown."""
    lab = empty_lab(thresholds=(4,))
    observe_clusters(lab, [cluster("BTC", "LONG", 2)], CFG, {"BTC": 100.0}, NOW)
    text = render_lab(lab)
    row = next(line for line in text.splitlines()
               if line.strip().startswith("≥4"))
    # The profit column may legitimately read +0.0% -- a real zero. The
    # win-rate column must read as unknown, not as a number.
    assert "+0.0%" in row
    assert "—" in row
    assert "نجاح" in text


def test_win_rate_is_printed_as_a_percentage_not_multiplied_again():
    """`paper.summarize` already returns a percentage.

    Multiplying it by 100 printed "2500%" and made a real result look absurd.
    """
    lab = empty_lab(thresholds=(2,))
    for _ in range(6):
        observe_clusters(lab, [cluster("BTC", "LONG", 3)], CFG, {"BTC": 100.0}, NOW)
    rows = summarize_lab(lab)["rows"]
    assert rows[0]["win_rate"] <= 100.0


def test_the_summary_carries_n_on_every_row():
    lab = empty_lab(thresholds=(2, 3))
    observe_clusters(lab, [cluster("BTC", "LONG", 3)], CFG, {"BTC": 100.0}, NOW)
    for row in summarize_lab(lab)["rows"]:
        assert "trades" in row
        assert row["trades"] >= 0


def test_render_survives_an_empty_lab():
    assert "ما عندنا بيانات" in render_lab(None)
    assert "ما شفنا ولا عنقود" in render_lab(empty_lab())


def test_render_never_raises_on_partial_data():
    lab = empty_lab()
    lab["sizes"] = {"3": 4, "not-a-number": 2}
    lab["clusters_seen"] = 6
    lab["cycles"] = 3
    lab["position_sum"] = 60
    lab["coin_sum"] = 6
    text = render_lab(lab)
    assert "مختبر الاتفاق" in text


# ── the lab must never break the radar ───────────────────────────────────────

def test_jam_in_the_lab_does_not_raise():
    lab = empty_lab()
    for junk in (None, [], {}, [1, 2, 3], "x", [cluster("BTC", "LONG", 2), None]):
        observe_clusters(lab, junk, CFG, None, NOW)
        mark_all(lab, None)
        mark_all(lab, {"BTC": float("nan")})


def test_the_lab_key_is_stable():
    """Renaming it silently orphans every running deployment's data."""
    assert KEY_SHADOW == "shadow_lab"
