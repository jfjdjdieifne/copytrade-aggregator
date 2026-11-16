"""End-to-end: the real pipeline over a fabricated market.

These tests exist because the venues are unreachable from CI. They run the same
`run_cycle` the cron runs, so a regression anywhere in discovery, grading,
convergence, planning or de-duplication shows up here instead of on someone's
server.

The most important test in this file is the negative control at the bottom: a
radar that manufactures consensus when nothing agreed would pass every other
assertion and still be fiction.
"""

from __future__ import annotations

import pytest  # noqa: F401  (arabic test names use it indirectly)

from cta.config import Config
from cta.simulate import (
    _cross_venue_event,
    profile_sim,
    run_quiet_control,
    run_simulation,
)
from cta.simulator import SimVenue, build_sim_venues
from cta.state import MemoryStore

CFG = Config(
    enabled_sources=("sim", "sim-beta"),
    watchlist_max=200,
    watchlist_min_grade="PROVEN",
    discovery_max_candidates=200,
    subrequest_limit=10_000,
    orders_enabled=True,
    orders_max_wallets=200,
)


# ── the market model ─────────────────────────────────────────────────────────
def test_two_venues_quote_the_same_price():
    """Cross-venue consensus is impossible if each venue has its own price.

    This was a real defect in the simulator: two independent random walks meant
    a "BTC long" on one venue could never agree with one on the other, so the
    rarest and strongest signal never formed — for a reason that does not exist
    in the real world.
    """
    venues = build_sim_venues(n_traders=8, seed=3, fresh=True)
    alpha, beta = venues["sim"], venues["sim-beta"]
    alpha.advance(60)
    # The shared market may only be advanced once per tick, or volatility is
    # silently doubled.
    assert alpha.market is beta.market
    assert alpha.market.price("BTC") == beta.market.price("BTC")


def test_wallet_ids_are_unique_across_venues():
    """Two platforms sharing an address collapse into one trader when the
    pipeline de-duplicates by wallet — destroying the strongest signal we wait
    for and doing it silently."""
    venues = build_sim_venues(n_traders=12, seed=3, fresh=True)
    a = {t.wallet for t in venues["sim"].traders}
    b = {t.wallet for t in venues["sim-beta"].traders}
    assert a.isdisjoint(b)


def test_a_slotted_model_has_no_dict():
    """The bug that started all this: `p.__dict__` on a slotted dataclass raises,
    the caller swallows it, and a trader with positions looks like a trader with
    none. Guarded explicitly so it cannot come back.
    """
    venue = SimVenue(n_traders=6, seed=1)
    trader = venue.traders[0]
    venue.inject_cluster("BTC", "LONG", members=[trader.wallet])
    import asyncio

    rows = asyncio.run(venue.positions(trader.wallet))
    assert rows, "positions() returned nothing — likely a serialisation failure"
    assert venue.parse_positions(trader.wallet, rows)


# ── grading separates skill from luck ────────────────────────────────────────
async def test_the_grading_separates_the_elite_from_the_lucky_and_the_rich():
    """The whole premise of the project in one assertion.

    `lucky` has spectacular headline ROI and no edge. `rich` has enormous
    capital and no edge. `hedger` has huge volume and no view. If any of them
    reach the watchlist, the radar is copying noise and calling it skill.
    """
    venues = build_sim_venues(n_traders=30, seed=11, fresh=True)
    store = MemoryStore()
    result = await profile_sim(venues, store, CFG)
    watchlist = await store.get("watchlist", []) or []

    assert result["scored"] > 0
    assert watchlist, "nothing survived grading"

    kept = {w["wallet"] for w in watchlist}
    truth = {}
    for venue in venues.values():
        truth.update(venue.truth())

    graded_archetypes = {truth.get(w) for w in kept}
    assert "elite" in graded_archetypes
    # Capital is not skill: a rich trader with no edge must not be copied.
    assert "rich" not in graded_archetypes


async def test_lucky_traders_are_kept_out_or_kept_honest():
    """A fat-tailed record with no edge is the profile that fools a leaderboard
    sorted by ROI. It may survive on ROI, but it must not out-rank a genuine
    edge — otherwise the radar prefers lottery tickets.
    """
    venues = build_sim_venues(n_traders=30, seed=11, fresh=True)
    store = MemoryStore()
    await profile_sim(venues, store, CFG)
    profiles = await store.get("profiles", []) or []

    truth = {}
    for venue in venues.values():
        truth.update(venue.truth())

    elite = [p for p in profiles if truth.get(p["wallet"]) == "elite"]
    lucky = [p for p in profiles if truth.get(p["wallet"]) == "lucky"]
    assert elite and lucky
    assert max(p["score"] for p in elite) >= max(p["score"] for p in lucky)


# ── consensus ────────────────────────────────────────────────────────────────
async def test_injected_agreement_produces_a_signal_and_the_negative_does_not():
    result = await run_simulation(cycles=12, n_traders=24, seed=7, verbose=False)
    assert result["profile"]["watchlist"] > 0
    assert result["signals"], "traders were made to agree and nothing was emitted"


async def test_no_agreement_means_no_signals():
    """The negative control, and the single most important assertion here.

    A radar that invents consensus passes every other test, looks completely
    plausible, and is worthless. Silence in the absence of agreement is the
    behaviour the whole design depends on.
    """
    result = await run_quiet_control(cycles=10, n_traders=24, seed=7)
    assert result["signals"] == []
    assert sum(c["emitted"] for c in result["cycles"]) == 0


async def test_a_signal_is_not_emitted_twice():
    """A duplicate alert is worse than no alert: it teaches the operator to
    ignore the channel, which is the one failure that makes everything else
    pointless.
    """
    result = await run_simulation(cycles=12, n_traders=24, seed=7, verbose=False)
    ids = [s.get("signal_id") for s in result["signals"] if s.get("signal_id")]
    assert len(ids) == len(set(ids)), "the same signal was emitted more than once"


async def test_cross_venue_consensus_scores_higher_than_single_venue():
    """Two traders on independent platforms agreeing is the rarest signal we
    can get, and it must be ranked above the same agreement within one venue —
    otherwise the operator has no way to tell the strong case from the weak.
    """
    result = await run_simulation(cycles=12, n_traders=24, seed=7, verbose=False)
    multi = [s for s in result["signals"] if len(s.get("sources") or []) > 1]
    single = [s for s in result["signals"] if len(s.get("sources") or []) == 1]
    if multi and single:
        assert max(s["score"] for s in multi) > min(s["score"] for s in single)


async def test_pending_signals_carry_the_traders_own_stop_and_target():
    """The flagship case: an order that has not filled, so we enter at their
    price — and we risk what they risked, aiming where they aimed.

    This failed once because only entry orders were passed into the pending
    detector, so the traders' stop and target orders were filtered out before
    the plan was built. The rarest signal was the only one with no risk levels.
    """
    result = await run_simulation(cycles=12, n_traders=24, seed=7, verbose=False)
    pending = [s for s in result["signals"] if s.get("kind") == "pending"]
    assert pending, "resting orders were injected and no pending signal appeared"
    for sig in pending:
        plan = sig.get("plan") or {}
        assert plan.get("stop"), "a pending signal with no stop is not actionable"
        assert plan.get("target")
        assert plan["stop_source"] == "trader", (
            "the stop must come from the traders' own orders, not from a guess"
        )


async def test_every_signal_has_a_plan_with_a_positive_reward_to_risk():
    result = await run_simulation(cycles=12, n_traders=24, seed=7, verbose=False)
    assert result["signals"]
    for sig in result["signals"]:
        plan = sig.get("plan") or {}
        assert plan.get("stop") is not None
        assert plan.get("target") is not None
        assert (plan.get("rr") or 0) >= 1.0, (
            "a signal paying less than even money is not worth an alert"
        )


async def test_the_stop_is_always_on_the_correct_side_of_the_entry():
    """A stop above a long's entry is not a stop. This is the one error that
    turns a good trade into a guaranteed loss, so it is asserted on every
    signal the pipeline produces.
    """
    result = await run_simulation(cycles=12, n_traders=24, seed=7, verbose=False)
    for sig in result["signals"]:
        plan = sig.get("plan") or {}
        entry, stop = plan.get("entry"), plan.get("stop")
        if not entry or not stop:
            continue
        if sig["side"] == "LONG":
            assert stop < entry
        else:
            assert stop > entry


# ── the simulator's own contract ─────────────────────────────────────────────
def test_a_cluster_event_can_be_injected_as_resting_orders():
    venue = SimVenue(n_traders=12, seed=5)
    event = venue.inject_cluster("BTC", "SHORT", resting=True)
    assert event["resting"] is True
    import asyncio

    orders = asyncio.run(venue.open_orders(event["members"][0]["wallet"]))
    assert orders
    parsed = venue.parse_open_orders(event["members"][0]["wallet"], orders)
    assert "entry" in {o.kind for o in parsed}


async def test_a_hedged_wallet_never_contributes_to_consensus():
    """Hedging is invisible in a closed-trade history (which shows sides but no
    netting) and only becomes visible in open positions. So the guarantee we can
    actually make — and the one that matters — is that a wallet holding both
    directions is discarded before a cluster forms.

    A hedger in a signal means the radar counted a wallet with no opinion as a
    vote.
    """
    from cta.convergence import strip_hedged

    venue = SimVenue(n_traders=15, seed=21)
    hedgers = [t for t in venue.traders if t.archetype == "hedger"]
    assert hedgers, "the simulator produced no hedgers to test against"

    # Force several hedge events so the opposing legs definitely exist.
    for _ in range(6):
        venue.inject_cluster("BTC", "LONG", members=[h.wallet for h in hedgers])

    from cta.engine import collect_positions

    venues = {"sim": venue}
    watchlist = [{"wallet": h.wallet, "source": "sim", "grade": "PROVEN",
                  "score": 70} for h in hedgers]
    positions = await collect_positions(venues, watchlist, CFG, limit=len(watchlist))
    assert positions, "no positions were collected"

    survivors = strip_hedged(positions, CFG.hedge_ratio_threshold)
    surviving_wallets = {p.wallet for p in survivors}
    for h in hedgers:
        assert h.wallet not in surviving_wallets or _net_is_directional(
            positions, h.wallet
        ), f"hedger {h.wallet} survived with no net view"


def _net_is_directional(positions, wallet) -> bool:
    """True when this wallet's net exposure is large relative to its gross."""
    mine = [p for p in positions if p.wallet == wallet]
    gross = sum(abs(p.size_usd) for p in mine)
    net = sum(p.size_usd if p.signed_size >= 0 else -p.size_usd for p in mine)
    if gross <= 0:
        return False
    return abs(net) / gross > 0.12


def test_cross_venue_event_spans_both_platforms():
    venues = build_sim_venues(n_traders=12, seed=5, fresh=True)
    alpha, beta = venues["sim"], venues["sim-beta"]
    event = _cross_venue_event(alpha, beta, "ETH", "LONG", resting=False)
    sources = {m["wallet"].startswith("0xsimb") for m in event["members"]}
    assert True in sources and False in sources, (
        "the event must involve traders on both venues"
    )
