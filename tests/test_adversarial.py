"""Adversarial regression tests.

Every test here corresponds to a real fault found by running the radar, not by
reading it. They are kept together because they share one purpose: to make the
ways this system can lie expensive to reintroduce.

The faults, and what they did:

1. `attach_protective` matched protective orders by wallet alone. A trader long
   BTC with an ETH stop had the ETH price attached as their BTC stop — an
   entry of 64,801 with a stop of 3,271, an R:R of 0.05. A "golden trade"
   whose stop was another asset's price.
2. A single NaN entry price hung `_cluster_by_price` forever. `base <= 0` is
   false for NaN, the band was NaN, no position compared within it, the cluster
   came back empty, and `remaining` never shrank. On a server: CPU pegged, no
   error, no alerts, and no sign anything was wrong.
3. A venue answering `None` (or a list where a dict belongs) propagated out of
   `parse_positions` and killed the entire cycle — one bad source silencing
   every other source.
4. A failing store killed the cycle. State is telemetry; a measurement that can
   destroy the thing it measures is worse than no measurement.
5. A market split 4-against-4 produced two STRONG signals for the same coin in
   the same minute, one LONG and one SHORT, each 88/100 on its own merits.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from cta.config import Config
from cta.engine import profile_watchlist_resumable, scan
from cta.models import Position
from cta.simulator import build_sim_venues
from cta.state import MemoryStore

UTC = timezone.utc
CFG = Config()


def _src_of(venues: dict, wallet: str):
    for name, venue in venues.items():
        if wallet in venue.wallets:
            return name
    return None


async def _ready(seed: int = 5, n: int = 24):
    """A simulator with a graded watchlist, and no events of its own."""
    venues = build_sim_venues(
        n_traders=n, seed=seed, auto_events=False, fresh=True
    )
    store = MemoryStore()
    for _ in range(5):
        await profile_watchlist_resumable(venues, store, CFG)
    return venues, store


def _place(venues, wallet, coin, signed, entry, size_usd, when, lev=3.0):
    src = _src_of(venues, wallet)
    assert src, f"wallet {wallet} belongs to no venue"
    venues[src]._positions.setdefault(wallet, []).append(
        Position(
            wallet=wallet, source=src, coin=coin, signed_size=signed,
            entry=entry, size_usd=size_usd, leverage=lev,
            opened_at=when, mark_price=entry,
        )
    )


async def _elites(store, k=4):
    wl = await store.get("watchlist", []) or []
    return [w for w in wl if w.get("grade") in ("ELITE", "PROVEN")][:k]


def _plan_of(sig):
    p = getattr(sig, "plan", None)
    return p


# ── 1. a stop must belong to the position's own market ───────────────────────
async def test_no_signal_carries_another_coin_stop():
    """The cross-coin stop.

    A trader holding BTC long, with a resting ETH stop, had the ETH price
    attached to their BTC position. The side check passed — 3,271 is below
    64,801 — so it looked like a valid stop and produced an R:R of 0.05.
    """
    from cta.orders import attach_protective

    venues, store = await _ready()
    el = await _elites(store, 2)
    assert len(el) >= 2

    wallet = el[0]["wallet"]
    venues[_src_of(venues, wallet)]._positions[wallet] = [
        Position(wallet=wallet, source="sim", coin="BTC", signed_size=1.0,
                 entry=64_801.0, size_usd=100_000.0, leverage=3.0,
                 opened_at=datetime.now(UTC), mark_price=64_801.0)
    ]
    from cta.models import Order

    # A stop and a target belonging to a completely different market.
    orders = [
        Order(wallet=wallet, source="sim", coin="ETH", side="LONG",
              kind="sl", price=3_271.0, size=1.0, reduce_only=True),
        Order(wallet=wallet, source="sim", coin="ETH", side="LONG",
              kind="tp", price=3_400.0, size=1.0, reduce_only=True),
    ]
    attach_protective(venues[_src_of(venues, wallet)]._positions[wallet], orders)

    pos = venues[_src_of(venues, wallet)]._positions[wallet][0]
    assert pos.stop is None, f"an ETH stop was attached to a BTC position: {pos.stop}"
    assert pos.target is None

    # And the matching-coin case must still work — the fix must not have
    # merely disabled the feature.
    btc_orders = [
        Order(wallet=wallet, source="sim", coin="BTC", side="LONG",
              kind="sl", price=63_000.0, size=1.0, reduce_only=True),
        Order(wallet=wallet, source="sim", coin="BTC", side="LONG",
              kind="tp", price=67_000.0, size=1.0, reduce_only=True),
    ]
    attach_protective(venues[_src_of(venues, wallet)]._positions[wallet], btc_orders)
    pos = venues[_src_of(venues, wallet)]._positions[wallet][0]
    assert pos.stop == 63_000.0
    assert pos.target == 67_000.0


# ── 2. a NaN must not hang the clustering ────────────────────────────────────
def test_a_nan_entry_price_cannot_hang_clustering():
    """One NaN used to spin `_cluster_by_price` forever.

    Run in a subprocess, on purpose. A CPU-bound infinite loop never yields, so
    asyncio cannot time it out and cannot cancel it — an in-process test would
    hang the whole suite instead of failing, which is the one outcome a
    regression test must never produce. A subprocess can be killed, so the
    failure is loud and the suite survives it.
    """
    import subprocess
    import sys

    code = (
        "from datetime import datetime, timezone;"
        "from cta.convergence import _cluster_by_price;"
        "from cta.models import Position;"
        "n=float('nan');"
        "mk=lambda e: Position(wallet='a', source='sim', coin='BTC',"
        " signed_size=1.0, entry=e, size_usd=1000.0, leverage=1.0,"
        " opened_at=datetime.now(timezone.utc), mark_price=100.0);"
        "print(len(_cluster_by_price([mk(n), mk(100.0), mk(101.0)], 50.0)))"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("clustering hung on a NaN entry price (killed after 20s)")
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() != ""


def test_non_finite_prices_are_never_clustered():
    from cta.convergence import _cluster_by_price

    def pos(entry):
        return Position(wallet="a", source="sim", coin="BTC", signed_size=1.0,
                        entry=entry, size_usd=1000.0, leverage=1.0,
                        opened_at=datetime.now(UTC), mark_price=100.0)

    for bad in (float("nan"), float("inf"), float("-inf"), 0.0, -5.0):
        clusters = _cluster_by_price([pos(bad), pos(bad)], 50.0)
        assert clusters == [], f"{bad} produced clusters"


# ── 3. a misbehaving venue must not take the cycle down ──────────────────────
@pytest.mark.parametrize("payload", [None, "not-a-list", 42, {"unexpected": True}])
async def test_a_venue_returning_nonsense_does_not_kill_the_cycle(payload):
    """A transient null, an API change, an error page parsed as JSON."""
    venues, store = await _ready()

    class Broken:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, k):
            return getattr(self.inner, k)

        async def positions(self, wallet):
            return payload

    venues = dict(venues)
    venues["sim"] = Broken(venues["sim"])

    # Must not raise, and the other venue must still be polled.
    sigs, clusters = await scan(venues, store, CFG)
    assert isinstance(sigs, list)


async def test_a_venue_that_raises_does_not_kill_the_cycle():
    venues, store = await _ready()

    class Exploding:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, k):
            return getattr(self.inner, k)

        async def positions(self, wallet):
            raise RuntimeError("venue exploded")

        async def prices(self):
            raise RuntimeError("venue exploded")

        async def health(self):
            return False

    venues = dict(venues)
    venues["sim"] = Exploding(venues["sim"])
    sigs, clusters = await scan(venues, store, CFG)
    assert isinstance(sigs, list)


# ── 4. a failing store must not stop the radar ───────────────────────────────
async def test_a_failing_store_does_not_stop_the_radar():
    """State is telemetry. A full disk must not silence the alerts."""
    from cta.state import MemoryStore, ResilientStore

    class Broken(MemoryStore):
        async def set(self, key, value):
            raise OSError("disk full")

    store = ResilientStore(Broken())
    await store.set("anything", 1)          # must not raise
    assert store.degraded is True
    assert "disk full" in store.last_error
    # A later read must see what the failed write claimed, or the rest of the
    # cycle would contradict itself.
    assert await store.get("anything") == 1


async def test_a_degraded_store_is_reported_not_hidden():
    """Silently keeping the radar alive while its memory rots would leave the
    operator trusting numbers that are no longer being saved."""
    from cta.state import MemoryStore, ResilientStore

    class Broken(MemoryStore):
        async def set(self, key, value):
            if key == "watchlist":
                raise OSError("locked")
            await super().set(key, value)

    store = ResilientStore(Broken())
    await store.set("watchlist", [{"wallet": "a"}])
    assert store.degraded is True
    assert store.last_error.startswith("write watchlist")


# ── 5. a split market is not two signals ─────────────────────────────────────
async def _split(n_long: int, n_short: int, seed: int = 5):
    venues, store = await _ready(seed=seed, n=30)
    el = await _elites(store, n_long + n_short + 2)
    price = venues["sim"].market.price("BTC")
    now = datetime.now(UTC)
    rng = random.Random(7)
    idx = 0
    for count, sign in ((n_long, 1.0), (n_short, -1.0)):
        for _ in range(count):
            if idx >= len(el):
                break
            w = el[idx]["wallet"]
            idx += 1
            _place(venues, w, "BTC", sign * 1000.0,
                   price * (1 + rng.uniform(-8, 8) / 10_000),
                   rng.uniform(40_000, 260_000),
                   now - timedelta(minutes=rng.uniform(0, 40)))
    sigs, _ = await scan(venues, store, CFG)
    return [s for s in sigs if s.coin == "BTC"]


async def test_a_balanced_market_emits_nothing():
    """4 pros long and 4 pros short is not a consensus in either direction.

    It used to emit both, each at 88/100 STRONG. Both were arithmetically
    correct and jointly useless: buy and sell at the same moment.
    """
    sigs = await _split(4, 4)
    assert sigs == [], [f"{s.side} {s.score}" for s in sigs]


async def test_a_two_to_one_majority_still_fires():
    """The guard against over-correction.

    Silencing every contested call would re-create the most expensive failure
    this system has: silence that looks exactly like a quiet market. A 2:1
    majority among professionals is still information.
    """
    sigs = await _split(4, 0)
    assert sigs, "an unopposed 4-trader consensus produced no signal"
    assert sigs[0].score >= 70

    majority = await _split(4, 2)
    assert majority, "a 4-against-2 majority was silenced entirely"
    # Discounted, but still delivered.
    assert majority[0].score < sigs[0].score


async def test_a_near_even_split_is_suppressed():
    """4 against 3 is not a consensus."""
    sigs = await _split(4, 3)
    assert sigs == [], [f"{s.side} {s.score}" for s in sigs]


# ── 6. copy-bots are not independent traders ─────────────────────────────────
async def test_byte_identical_entries_are_not_independent_traders():
    """Eight wallets entering at the same instant with the same size are one
    trader wearing eight hats."""
    venues, store = await _ready(seed=5, n=30)
    el = await _elites(store, 10)
    if len(el) < 6:
        pytest.skip("not enough graded traders in this world")
    price = venues["sim"].market.price("ARB")
    now = datetime.now(UTC)
    for w in el[:8]:
        _place(venues, w["wallet"], "ARB", 100_000.0 / price, price,
               100_000.0, now)
    sigs, _ = await scan(venues, store, CFG)
    arbs = [s for s in sigs if s.coin == "ARB"]
    assert arbs == [], [f"n={getattr(s, 'n_independent', 0)}" for s in arbs]


async def test_agreement_hours_apart_is_not_agreement():
    venues, store = await _ready(seed=5, n=30)
    el = await _elites(store, 4)
    price = venues["sim"].market.price("ETH")
    now = datetime.now(UTC)
    rng = random.Random(11)
    for i, w in enumerate(el):
        _place(venues, w["wallet"], "ETH", 1000.0,
               price * (1 + rng.uniform(-8, 8) / 10_000),
               rng.uniform(40_000, 260_000), now - timedelta(hours=5 * i))
    sigs, _ = await scan(venues, store, CFG)
    assert [s for s in sigs if s.coin == "ETH"] == []


async def test_one_whale_is_not_a_consensus():
    venues, store = await _ready(seed=5, n=30)
    el = await _elites(store, 2)
    price = venues["sim"].market.price("DOGE")
    _place(venues, el[0]["wallet"], "DOGE", 1.0, price, 50_000_000.0,
           datetime.now(UTC))
    sigs, _ = await scan(venues, store, CFG)
    assert [s for s in sigs if s.coin == "DOGE"] == []


# ── 7. the signal itself must always be tradeable ────────────────────────────
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
async def test_every_signal_is_tradeable(seed):
    """No signal may reach the operator with an untradeable plan.

    Checked across many worlds, because a single seed proves nothing.
    """
    venues, store = await _ready(seed=seed, n=24)
    el = await _elites(store, 4)
    if len(el) < 3:
        pytest.skip("not enough graded traders")
    price = venues["sim"].market.price("ARB")
    now = datetime.now(UTC)
    rng = random.Random(seed)
    for w in el:
        _place(venues, w["wallet"], "ARB", 1000.0,
               price * (1 + rng.uniform(-8, 8) / 10_000),
               rng.uniform(40_000, 260_000),
               now - timedelta(minutes=rng.uniform(0, 40)))
    sigs, _ = await scan(venues, store, CFG)

    for sig in sigs:
        p = _plan_of(sig)
        assert p is not None
        for name, val in (("entry", p.entry), ("stop", p.stop),
                          ("target", p.target)):
            if val is None:
                continue
            assert math.isfinite(val), f"{name} is {val}"
            assert val > 0, f"{name} is {val}"
        if p.entry and p.stop and p.target:
            if sig.side == "LONG":
                assert p.stop < p.entry < p.target, "LONG levels out of order"
            else:
                assert p.target < p.entry < p.stop, "SHORT levels out of order"
        if p.rr is not None:
            assert math.isfinite(p.rr) and p.rr >= 1.0, f"R:R {p.rr}"
        assert getattr(sig, "n_independent", 0) >= CFG.min_independent_traders


# ── 8. the simulator must model a market that can be agreed on ───────────────
def test_taking_a_view_closes_the_opposite_view():
    """A trader flipping direction closes their old position first.

    Without this, a long run degenerated: every trader held a LONG and a SHORT
    on every coin at once, so the radar — correctly — found no consensus for
    400 cycles. That silence was the radar being right, and it hid a real
    fault: without the disagreement logic, the same stale book would have
    produced LONG and SHORT alerts on the same coin simultaneously, which is
    what a real deployment would have done after a few days.
    """
    venues = build_sim_venues(n_traders=12, seed=3, auto_events=False, fresh=True)
    v = venues["sim"]
    wallet = v.traders[0].wallet

    v.inject_cluster("BTC", "SHORT")
    shorts = [p for p in v._positions.get(wallet, []) if p.coin == "BTC"]
    assert shorts, "no short position was opened"
    assert all(p.signed_size < 0 for p in shorts)

    v.inject_cluster("BTC", "LONG")
    after = [p for p in v._positions.get(wallet, []) if p.coin == "BTC"]
    assert after, "no long position was opened"
    assert all(p.signed_size > 0 for p in after), (
        "the old short survived the flip; a trader cannot hold both"
    )


def test_positions_do_not_live_forever():
    """Positions that never close make every long run unrepresentative."""
    venues = build_sim_venues(n_traders=12, seed=3, auto_events=False, fresh=True)
    v = venues["sim"]
    v.inject_cluster("BTC", "LONG")
    assert v._positions, "no positions were opened"

    # Far past any plausible holding period.
    for _ in range(200):
        v.advance(60)
    assert not v._positions, (
        "positions survived 200 hours: the book accumulates without bound"
    )


def test_resting_orders_expire():
    venues = build_sim_venues(n_traders=12, seed=3, auto_events=False, fresh=True)
    v = venues["sim"]
    v.inject_cluster("BTC", "LONG", resting=True)
    assert v._orders, "no resting orders were placed"
    for _ in range(48):
        v.advance(60)
    assert not v._orders, "resting orders never expire"
