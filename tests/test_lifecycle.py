"""The trade lifecycle: one idea, followed from entry to exit.

The properties that matter most here are the ones that would cost money if
they were wrong, and they are not the obvious ones.

The obvious property is "an exit is reported when the traders leave". The
expensive one is the opposite: **an outage must never look like an exit**. If
we cannot see a wallet, that is ignorance, not information. Getting that wrong
means every venue hiccup pages the operator with "the pros are leaving" on
every open idea at once — and they would close winning positions because a
server blinked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cta.lifecycle import (
    Idea,
    exposure_map,
    ledger_entry,
    reconcile,
    summarize_ledger,
)


UTC = timezone.utc


def _signal(coin="BTC", side="LONG", score=85, wallets=("a", "b", "c"),
            sources=("sim",), entry=100.0, stop=97.0, target=106.0,
            signal_id="s1", size=100_000.0):
    class Plan:
        pass

    plan = Plan()
    plan.entry, plan.stop, plan.target = entry, stop, target
    plan.rr = 2.0
    plan.stop_source = "trader"
    plan.target_source = "trader"

    class Sig:
        pass

    s = Sig()
    s.coin, s.side, s.score, s.level = coin, side, score, "STRONG"
    s.entry_center = entry
    s.spread_bps = 5.0
    s.n_independent = len(wallets)
    s.sources = list(sources)
    s.plan = plan
    s.signal_id = signal_id
    s.members = [
        {"wallet": w, "source": sources[i % len(sources)], "entry": entry,
         "size_usd": size, "quality": "ELITE", "quality_score": 90}
        for i, w in enumerate(wallets)
    ]
    s.kind = "filled"
    s.market_price = entry
    s.breakdown = {}
    s.reasons = []
    return s


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _observe(ideas, wallets=("a", "b", "c"), coin="BTC", price=100.0,
             at=None):
    """A cycle in which every member is visible and still holding.

    An idea cannot be judged until it has been seen once — see
    `test_an_idea_cannot_exit_on_the_cycle_it_opens`.
    """
    return reconcile(
        ideas, [],
        _exposure([(w, coin, 100_000.0) for w in wallets]),
        {"sim", "sim-beta"}, {coin: price}, at or NOW,
    )


def _exposure(pairs):
    return exposure_map(
        [type("P", (), {"wallet": w, "coin": c, "size_usd": s})()
         for (w, c, s) in pairs]
    )


# ── entry ───────────────────────────────────────────────────────────────────
def test_a_new_consensus_opens_an_idea():
    ideas: dict = {}
    events = reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    assert len(events) == 1
    assert events[0]["kind"] == "enter"
    assert events[0]["idea"].key == "BTC:LONG"
    assert set(events[0]["idea"].members) == {"a", "b", "c"}


def test_a_second_consensus_on_the_same_idea_does_not_reopen_it():
    """One thesis per coin per direction. A repeat is confirmation."""
    ideas: dict = {}
    reconcile(ideas, [_signal(signal_id="s1")], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(
        ideas, [_signal(signal_id="s2", wallets=("a", "b", "c", "d"))],
        _exposure([("a", "BTC", 100_000.0), ("b", "BTC", 100_000.0),
                   ("c", "BTC", 100_000.0)]),
        {"sim"}, {"BTC": 100.0}, NOW + timedelta(minutes=5),
    )
    assert [e["kind"] for e in events] == ["strengthen"]
    assert events[0]["joined"] == 1
    assert len(ideas["BTC:LONG"].members) == 4


def test_the_opposite_direction_is_a_different_idea():
    ideas: dict = {}
    reconcile(ideas, [_signal(side="LONG")], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(ideas, [_signal(side="SHORT", signal_id="s2")],
                       {}, {"sim"}, {"BTC": 100.0}, NOW)
    assert [e["kind"] for e in events] == ["enter"]
    assert set(ideas) == {"BTC:LONG", "BTC:SHORT"}


# ── exit ────────────────────────────────────────────────────────────────────
def test_an_idea_cannot_exit_on_the_cycle_it_opens():
    """The ENTER-then-EXIT bug.

    On the cycle an idea opens the exposure map may not have been polled yet.
    Reading that emptiness as "they all left" produced an entry alert and an
    exit alert in the same breath.
    """
    ideas: dict = {}
    events = reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    assert [e["kind"] for e in events] == ["enter"]


def test_the_traders_leaving_closes_the_idea():
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    _observe(ideas)                      # seen once, all still in
    # Two of the three are gone.
    events = reconcile(
        ideas, [], _exposure([("a", "BTC", 100_000.0)]), {"sim"},
        {"BTC": 101.0}, NOW + timedelta(minutes=30),
    )
    assert [e["kind"] for e in events] == ["exit"]
    assert events[0]["left"] == 2
    assert events[0]["remaining"] == 1
    assert events[0]["r"] == pytest.approx(1 / 3, abs=1e-6)


def test_a_partial_exit_does_not_close_the_idea():
    """One of three leaving is not the trade being over."""
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(
        ideas, [],
        _exposure([("a", "BTC", 100_000.0), ("b", "BTC", 100_000.0)]),
        {"sim"}, {"BTC": 100.0}, NOW + timedelta(minutes=30),
    )
    assert events == []


def test_shrinking_below_half_counts_as_leaving():
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(
        ideas, [],
        _exposure([("a", "BTC", 10.0), ("b", "BTC", 10.0), ("c", "BTC", 10.0)]),
        {"sim"}, {"BTC": 100.0}, NOW + timedelta(minutes=30),
    )
    assert [e["kind"] for e in events] == ["exit"]


def test_adding_to_a_position_is_not_leaving():
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(
        ideas, [],
        _exposure([(w, "BTC", 500_000.0) for w in "abc"]),
        {"sim"}, {"BTC": 100.0}, NOW + timedelta(minutes=30),
    )
    assert events == []


# ── the expensive property ──────────────────────────────────────────────────
def test_an_unreachable_venue_is_never_read_as_an_exit():
    """The outage rule.

    Every wallet is invisible because its venue did not report. That is
    ignorance, not information: reporting an exit here would tell the operator
    to close winning positions because a server blinked.
    """
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(
        ideas, [],
        {},                    # we can see nothing at all
        set(),                 # and no venue reported healthy
        {"BTC": 100.0}, NOW + timedelta(minutes=30),
    )
    assert events == [], "a blind cycle was reported as the traders leaving"
    assert ideas["BTC:LONG"].exited_at is None


def test_only_the_wallets_we_can_see_are_judged():
    """Two members vanish but their venue is dark; the visible one is still in."""
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(
        ideas, [],
        _exposure([("a", "BTC", 100_000.0)]),   # only 'a' visible
        set(),                                   # no source confirmed healthy
        {"BTC": 100.0}, NOW + timedelta(minutes=30),
    )
    # 'a' is still in; b and c are unknown, not gone. So no exit.
    assert [e["kind"] for e in events] == []


def test_a_confirmed_venue_reporting_nothing_means_gone():
    """When the venue answers and the position is not there, it is gone."""
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    _observe(ideas)
    events = reconcile(
        ideas, [],
        _exposure([("a", "BTC", 100_000.0)]),   # only 'a' has a row
        {"sim"},                                 # and 'sim' reported in
        {"BTC": 100.0}, NOW + timedelta(minutes=30),
    )
    assert [e["kind"] for e in events] == ["exit"]


# ── expiry ──────────────────────────────────────────────────────────────────
def test_an_idea_that_runs_too_long_is_closed_as_unresolved():
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    events = reconcile(
        ideas, [],
        _exposure([(w, "BTC", 100_000.0) for w in "abc"]),
        {"sim"}, {"BTC": 100.0},
        NOW + timedelta(hours=100), max_age_h=72.0,
    )
    assert [e["kind"] for e in events] == ["expired"]


def test_a_closed_idea_is_not_resurrected_by_the_next_consensus():
    ideas: dict = {}
    reconcile(ideas, [_signal(signal_id="s1")], {}, {"sim"}, {"BTC": 100.0}, NOW)
    _observe(ideas, at=NOW + timedelta(minutes=1))
    reconcile(ideas, [], _exposure([]), {"sim"}, {"BTC": 100.0},
              NOW + timedelta(minutes=30))
    assert ideas["BTC:LONG"].exited_at is not None
    # A fresh consensus later is a genuinely new trade, so it must open again.
    events = reconcile(
        ideas, [_signal(signal_id="s2")], {}, {"sim"}, {"BTC": 100.0},
        NOW + timedelta(hours=5),
    )
    assert [e["kind"] for e in events] == ["enter"]


# ── measurement ─────────────────────────────────────────────────────────────
def test_r_is_measured_in_multiples_of_risk():
    ideas: dict = {}
    reconcile(ideas, [_signal(entry=100.0, stop=97.0)], {}, {"sim"},
              {"BTC": 100.0}, NOW)
    idea = ideas["BTC:LONG"]
    assert idea.r_of(103.0) == pytest.approx(1.0)
    assert idea.r_of(94.0) == pytest.approx(-2.0)
    assert idea.r_of(None) is None


def test_a_short_idea_measures_r_in_the_other_direction():
    ideas: dict = {}
    reconcile(ideas, [_signal(side="SHORT", entry=100.0, stop=103.0,
                              target=94.0)], {}, {"sim"}, {"BTC": 100.0}, NOW)
    idea = ideas["BTC:SHORT"]
    assert idea.r_of(97.0) == pytest.approx(1.0)
    assert idea.r_of(106.0) == pytest.approx(-2.0)


def test_the_best_and_worst_prices_are_tracked():
    ideas: dict = {}
    reconcile(ideas, [_signal(side="LONG")], {}, {"sim"}, {"BTC": 100.0}, NOW)
    idea = ideas["BTC:LONG"]
    for px in (102.0, 98.0, 105.0, 101.0):
        reconcile(ideas, [],
                  _exposure([(w, "BTC", 100_000.0) for w in "abc"]),
                  {"sim"}, {"BTC": px}, NOW)
    assert idea.best_price == 105.0
    assert idea.worst_price == 98.0


def test_a_tighter_later_stop_never_widens_risk():
    """Moving the stop further from entry would silently increase the risk."""
    ideas: dict = {}
    reconcile(ideas, [_signal(entry=100.0, stop=97.0)], {}, {"sim"},
              {"BTC": 100.0}, NOW)
    idea = ideas["BTC:LONG"]
    idea.absorb(_signal(signal_id="s2", entry=100.0, stop=90.0), NOW)
    assert idea.stop == 97.0, "a later, wider stop was accepted"
    idea.absorb(_signal(signal_id="s3", entry=100.0, stop=98.5), NOW)
    assert idea.stop == 98.5, "a tighter stop should be accepted"


# ── round trip and the ledger ───────────────────────────────────────────────
def test_an_idea_survives_a_json_round_trip():
    ideas: dict = {}
    reconcile(ideas, [_signal()], {}, {"sim"}, {"BTC": 100.0}, NOW)
    idea = ideas["BTC:LONG"]
    idea.observe_price(103.0)
    restored = Idea.from_dict(idea.to_dict())
    assert restored.key == idea.key
    assert restored.members == idea.members
    assert restored.member_sources == idea.member_sources
    assert restored.best_price == idea.best_price
    assert restored.entry == idea.entry


def test_a_closed_idea_is_recorded_with_attribution():
    """Without attribution the system can only have opinions."""
    ideas: dict = {}
    reconcile(ideas, [_signal(sources=("sim", "sim-beta"))], {},
              {"sim", "sim-beta"}, {"BTC": 100.0}, NOW)
    idea = ideas["BTC:LONG"]
    idea.exited_at = NOW + timedelta(hours=2)
    idea.exit_reason = "members_left"
    entry = ledger_entry(idea, 103.0, NOW + timedelta(hours=2))

    assert entry["coin"] == "BTC"
    assert entry["sources"] == ["sim", "sim-beta"]
    assert entry["n_sources"] == 2
    assert entry["n_members"] == 3
    assert entry["grades"] == ["ELITE", "ELITE", "ELITE"]
    assert entry["r"] == pytest.approx(1.0)
    assert entry["exit_reason"] == "members_left"


def test_the_ledger_summarises_by_the_things_we_choose_on():
    rows = [
        {"coin": "BTC", "sources": ["sim"], "r": 1.0, "peak_r": 1.5},
        {"coin": "BTC", "sources": ["sim"], "r": -1.0, "peak_r": 0.2},
        {"coin": "ETH", "sources": ["sim-beta"], "r": 2.0, "peak_r": 2.0},
    ]
    out = summarize_ledger(rows)
    assert out["n"] == 3
    assert out["all"]["total_r"] == pytest.approx(2.0)
    assert out["all"]["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert out["by_source"]["sim"]["n"] == 2
    assert out["by_source"]["sim"]["avg_r"] == pytest.approx(0.0)
    assert out["by_coin"]["ETH"]["n"] == 1


def test_an_empty_ledger_says_so_rather_than_reporting_zero():
    out = summarize_ledger([])
    assert out["n"] == 0
    assert "note" in out


def test_a_three_trade_average_is_not_pretended_to_be_significant():
    """An average over three trades is an anecdote with a decimal point.

    The count travels with every average so the number cannot be read without
    knowing how thin it is.
    """
    rows = [{"coin": "BTC", "sources": ["sim"], "r": 3.0, "peak_r": 3.0}]
    out = summarize_ledger(rows)
    assert out["by_source"]["sim"]["n"] == 1
    assert out["by_source"]["sim"]["avg_r"] == 3.0
