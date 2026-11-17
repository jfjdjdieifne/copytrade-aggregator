"""A venue that does not exist, so the rest of the system can be trusted.

Why a simulator, and why it is not cheating
-------------------------------------------
This project cannot be tested against live APIs from a development machine that
has no route to them, and "it ran in production" is a terrible way to discover a
bug. So this module fabricates a venue — prices, traders, fills, positions and
resting orders — and feeds it through the *real* pipeline: the real classifier,
the real convergence gates, the real confidence scorer, the real plan builder,
the real Telegram formatter, the real 24/7 loop.

What is fake is only the market. What is real is every decision made about it.

The traders are drawn from archetypes that exist on every leaderboard, because
the interesting question was never "does the code run" — it is **does the
grading separate skill from luck**:

| archetype | what it looks like on a leaderboard | what it really is |
|---|---|---|
| `elite` | steady high ROI | genuine edge, consistent, uses stops |
| `lucky` | spectacular ROI | one or two enormous wins, no edge |
| `rich` | huge AUM, mediocre ROI | no edge at all, just capital |
| `hedger` | active, big volume | long and short at once: no view |
| `twin` | a second "independent" elite | one operator running two accounts |

If the pipeline is honest, only `elite` should reach ELITE/PROVEN, `twin` should
be collapsed into one opinion, and `hedger` should never contribute to a
signal. Those are assertions in the tests, not hopes.

Everything is seeded. A run is reproducible, so a failure can always be
replayed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from typing import Optional

from .models import Fill, Order, Position

UTC = timezone.utc

COINS = ("BTC", "ETH", "SOL", "ARB", "DOGE")
DAY_MS = 86_400_000


# ── market ───────────────────────────────────────────────────────────────────
@dataclass
class Market:
    """A seeded random walk per coin. Not a model of anything real."""

    prices: dict[str, float] = field(default_factory=dict)
    vol: dict[str, float] = field(default_factory=dict)
    rng: random.Random = field(default_factory=lambda: random.Random(7))

    # The clock lives here because every venue that quotes this market must
    # agree on what "now" is — and on which tick of the story we are on.
    now: Optional[datetime] = None
    tick: int = 0
    auto_events: bool = False
    event_every: int = 3
    _advanced_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.prices:
            self.prices = {
                "BTC": 65_000.0, "ETH": 3_200.0, "SOL": 145.0,
                "ARB": 1.10, "DOGE": 0.155,
            }
        if not self.vol:
            self.vol = {
                "BTC": 0.011, "ETH": 0.015, "SOL": 0.024,
                "ARB": 0.031, "DOGE": 0.038,
            }

    def step(self, minutes: float = 1.0) -> None:
        """Advance every price. Volatility scales with sqrt(time)."""
        scale = (minutes / 1440.0) ** 0.5
        for coin, px in list(self.prices.items()):
            drift = self.rng.gauss(0.0, self.vol.get(coin, 0.02) * scale)
            self.prices[coin] = max(0.0001, px * (1.0 + drift))

    def price(self, coin: str) -> float:
        return float(self.prices.get(coin, 100.0))


# ── traders ──────────────────────────────────────────────────────────────────
ARCHETYPES = ("elite", "lucky", "rich", "hedger", "twin")


@dataclass
class SimTrader:
    wallet: str
    source: str
    archetype: str
    account_value: float
    rng: random.Random
    # Behavioural knobs, all archetype-derived
    win_rate: float = 0.5
    avg_win: float = 1.0
    avg_loss: float = 1.0
    trades_per_week: float = 8.0
    leverage: float = 3.0
    sets_tpsl: bool = True
    hedge_prob: float = 0.0
    twin_of: Optional[str] = None

    def realised_pnl(self) -> float:
        """One trade outcome for this archetype.

        `avg_win`/`avg_loss` are set so that the *net* expectation matches the
        archetype: an elite has a positive one, a rich trader's is zero minus
        fees, and a lucky trader's is slightly negative but with a fat tail that
        makes a headline ROI look spectacular for a while.
        """
        if self.rng.random() < self.win_rate:
            return abs(self.rng.gauss(self.avg_win, self.avg_win * 0.45))
        return -abs(self.rng.gauss(self.avg_loss, self.avg_loss * 0.45))


def _make_trader(rng: random.Random, index: int, source: str,
                 prefix: str = "0xsim") -> SimTrader:
    # Wallet ids must be unique *across* venues. They were not, and two
    # different simulated platforms shared addresses — so the pipeline's own
    # cross-wallet de-duplication collapsed two genuinely independent traders
    # into one and the strongest signal we claim to wait for never formed.
    wallet = f"{prefix}{index:04d}"
    archetype = ARCHETYPES[index % len(ARCHETYPES)]
    acct = rng.uniform(20_000, 400_000)

    if archetype == "elite":
        # Real edge: wins more often than it loses and wins bigger.
        return SimTrader(wallet, source, archetype, acct, rng,
                         win_rate=0.61, avg_win=1.35, avg_loss=1.0,
                         trades_per_week=9.0, leverage=3.0,
                         sets_tpsl=True)
    if archetype == "lucky":
        # No edge, but a fat right tail: one 30x win carries the whole record.
        # This is the profile that fools a leaderboard sorted by ROI.
        t = SimTrader(wallet, source, archetype, acct, rng,
                      win_rate=0.34, avg_win=4.2, avg_loss=1.0,
                      trades_per_week=5.0, leverage=12.0, sets_tpsl=False)
        return t
    if archetype == "rich":
        # Capital is not skill. Zero edge, tiny losses from fees.
        return SimTrader(wallet, source, archetype, acct * 8, rng,
                         win_rate=0.49, avg_win=1.0, avg_loss=1.0,
                         trades_per_week=14.0, leverage=2.0, sets_tpsl=True)
    if archetype == "hedger":
        # Holds both sides: high volume, no directional view at all.
        return SimTrader(wallet, source, archetype, acct, rng,
                         win_rate=0.5, avg_win=1.0, avg_loss=1.0,
                         trades_per_week=18.0, leverage=4.0,
                         hedge_prob=0.85, sets_tpsl=False)
    # twin: a second account mirroring an elite, to test de-duplication.
    return SimTrader(wallet, source, archetype, acct, rng,
                     win_rate=0.61, avg_win=1.35, avg_loss=1.0,
                     trades_per_week=9.0, leverage=3.0, sets_tpsl=True,
                     twin_of=f"{prefix}{index - 4:04d}")


# ── the venue ────────────────────────────────────────────────────────────────
class SimVenue:
    """Implements the venue adapter interface over generated data."

    It is intentionally complete: leaderboard, fills, positions, resting orders
    and prices. An adapter that only implemented some of those would let the
    pipeline skip the code paths we most need to exercise.
    """

    name = "sim"

    def __init__(
        self,
        n_traders: int = 40,
        seed: int = 7,
        history_days: int = 220,
        now: Optional[datetime] = None,
        wallet_prefix: str = "0xsim",
        market: Optional[Market] = None,
        max_position_age_h: float = 36.0,
        max_order_age_h: float = 6.0,
    ):
        self.rng = random.Random(seed)
        # One shared market by default. Two venues must quote the same asset at
        # the same price — giving each its own random walk made cross-venue
        # consensus impossible for a reason that does not exist in the real
        # world, and hid whether the clustering actually worked.
        self.market = market or Market(rng=random.Random(seed + 1))
        # The clock belongs to the market, not to the venue.
        #
        # Each venue used to keep its own `now`, and `advance()` moved only the
        # caller's. Two venues sharing a market therefore drifted apart — in one
        # observed run, by nearly two hours — so their traders' entries were
        # never "at nearly the same time" and cross-venue agreement was
        # impossible for a reason that had nothing to do with trading.
        if self.market.now is None:
            self.market.now = now or datetime.now(UTC)
        self.history_days = history_days
        self.traders: list[SimTrader] = [
            _make_trader(random.Random(seed + 100 + i), i, "sim",
                         prefix=wallet_prefix)
            for i in range(n_traders)
        ]
        self.wallets = {t.wallet: t for t in self.traders}
        self._history: dict[str, list[Fill]] = {}
        self._positions: dict[str, list[Position]] = {}
        self._orders: dict[str, list[Order]] = {}
        self._realised: dict[str, list[Position]] = {}
        # Positions and orders do not live forever. See `settle`.
        self.max_position_age_h = max_position_age_h
        self.max_order_age_ms = int(max_order_age_h * 3_600_000)
        self._build_history()
        self.events: list[dict] = []
        # A sibling venue, so an agreement can span two platforms: the rarest
        # signal the radar waits for, and the one that can never be produced by
        # one venue alone.
        self.partner: Optional[SimVenue] = None
        # Only one venue drives the clock and the storyline, otherwise every
        # tick would be applied twice.
        self._primary = False

    # ── clock ───────────────────────────────────────────────────────────────
    @property
    def now(self) -> datetime:
        # `Optional` only so a shared market can be constructed before any
        # venue sets the clock; by the time anything reads it, it is set.
        assert self.market.now is not None
        return self.market.now

    def advance(self, minutes: float) -> None:
        assert self.market.now is not None
        self.market.now = self.market.now + timedelta(minutes=minutes)
        # Only the first venue to be advanced moves the shared clock forward;
        # advancing it twice would double the volatility for one tick.
        if self.market._advanced_at != self.market.now:
            self.market.step(minutes)
            self.market._advanced_at = self.market.now
            # Prices moved, so stops and targets may have been reached — and
            # with them the reason a position is still open.
            self.settle()
            if self._primary:
                for partner in (self.partner,):
                    if partner is not None:
                        partner.settle()

    def now_ms(self) -> int:
        return int(self.now.timestamp() * 1000)

    def _close_opposing(self, wallet: str, coin: str, side: str) -> None:
        """Close this wallet's positions on `coin` that face the other way."""
        book = self._positions.get(wallet)
        if not book:
            return
        want_positive = side == "LONG"
        kept = [p for p in book if not (
            p.coin == coin and (p.signed_size >= 0) != want_positive
        )]
        if len(kept) != len(book):
            self._realised.setdefault(wallet, []).extend(
                p for p in book if p not in kept
            )
            if kept:
                self._positions[wallet] = kept
            else:
                self._positions.pop(wallet, None)
        # Resting orders facing the other way go too.
        orders = self._orders.get(wallet)
        if orders:
            kept_o = [o for o in orders if not (
                o.coin == coin and o.side != side
            )]
            if kept_o:
                self._orders[wallet] = kept_o
            else:
                self._orders.pop(wallet, None)

    def settle(self) -> int:
        """Close positions whose stop or target the market has reached.

        Without this, simulated positions never closed, and a long run became
        steadily less like reality: every trader accumulated a LONG and a SHORT
        on every coin at once. By cycle 40 the radar was looking at a market
        where nobody agreed with anybody, and stayed silent for 400 cycles.

        That silence was the *radar being right* — and it hid a genuine fault.
        Had the disagreement logic not existed, the same stale book would have
        produced LONG and SHORT alerts on the same coin at the same time, which
        is what a real 24/7 deployment would have done after a few days.

        Positions also expire, because a trader who never closes is not
        modelling anything.
        """
        closed = 0
        max_age = timedelta(hours=self.max_position_age_h)
        for wallet, book in list(self._positions.items()):
            kept: list[Position] = []
            for p in book:  # noqa: B007
                px = self.market.price(p.coin)
                hit = False
                if p.signed_size >= 0:
                    if p.stop and px <= p.stop:
                        hit = True
                    elif p.target and px >= p.target:
                        hit = True
                else:
                    if p.stop and px >= p.stop:
                        hit = True
                    elif p.target and px <= p.target:
                        hit = True
                aged_out = p.opened_at is not None and (self.now - p.opened_at) > max_age
                if hit or aged_out:
                    self._realised.setdefault(wallet, []).append(p)
                    closed += 1
                else:
                    kept.append(p)
            if kept:
                self._positions[wallet] = kept
            else:
                self._positions.pop(wallet, None)
        # Resting orders expire too: an order left open forever is a lie about
        # intent, and it would keep generating "pending" consensus forever.
        for wallet, order_book in list(self._orders.items()):
            kept_orders: list[Order] = [
                resting for resting in order_book
                if not (resting.placed_ms and
                        (self.now_ms() - resting.placed_ms) > self.max_order_age_ms)
            ]
            if kept_orders:
                self._orders[wallet] = kept_orders
            else:
                self._orders.pop(wallet, None)
        return closed

    # ── history: what the classifier judges ─────────────────────────────────
    def _build_history(self) -> None:
        end_ms = self.now_ms()
        start_ms = end_ms - self.history_days * DAY_MS
        for t in self.traders:
            rng = random.Random(hash((t.wallet, "hist")) & 0xFFFF)
            n = max(60, int(t.trades_per_week * self.history_days / 7.0))
            fills: list[Fill] = []
            for i in range(n):
                coin = COINS[rng.randrange(len(COINS))]
                when = start_ms + int((end_ms - start_ms) * (i + 1) / (n + 1))
                price = self.market.price(coin) * (1.0 + rng.gauss(0, 0.02))
                # Fee as a fraction of traded notional, not of price: a
                # per-unit fee made every simulated trade unprofitable for
                # reasons that had nothing to do with skill, so the classifier
                # rejected traders who genuinely had an edge.
                notional = t.account_value * 0.02
                pnl = t.realised_pnl() * (t.account_value * 0.004)
                fee = notional * 0.0005
                fills.append(
                    Fill(
                        time_ms=when,
                        coin=coin,
                        side="BUY" if rng.random() < 0.5 else "SELL",
                        price=price,
                        size=abs(t.account_value * 0.02 / max(price, 1e-9)),
                        closed_pnl=pnl,
                        fee=fee,
                        raw={"sim": True},
                    )
                )
            fills.sort(key=lambda f: f.time_ms)
            self._history[t.wallet] = fills

    # ── cluster events: the thing the radar is looking for ──────────────────
    def inject_cluster(
        self,
        coin: str,
        side: str,
        members: Optional[list[str]] = None,
        resting: bool = False,
    ) -> dict:
        """Make several traders agree, at nearly the same price and time.

        This is the only realistic way a convergence ever happens — traders
        reacting to the same market structure — so the simulator has to be able
        to produce it on demand rather than waiting for coincidence.

        `resting=True` places unfilled orders instead of opening positions,
        which exercises the pending-consensus path.
        """
        elites = [t for t in self.traders if t.archetype in ("elite", "twin")]
        if members:
            chosen = [self.wallets[w] for w in members if w in self.wallets]
        else:
            chosen = elites[: max(2, min(4, len(elites)))]

        base = self.market.price(coin)
        placed: list[dict] = []
        for i, t in enumerate(chosen):
            # Taking a view closes the opposite view. A trader who opens a LONG
            # while still holding a SHORT on the same coin is not "hedging",
            # they are a simulator artefact — and a book full of them makes
            # every coin look permanently split, so the radar (correctly) never
            # finds a consensus. Real traders flip; they do not accumulate both
            # sides for a week.
            self._close_opposing(t.wallet, coin, side)
            # A few basis points apart: inside the tolerance, not identical.
            px = base * (1.0 + self.rng.uniform(-0.0012, 0.0012))
            size_usd = t.account_value * t.leverage * 0.20
            if resting:
                order = Order(
                    wallet=t.wallet, source=self.name, coin=coin, side=side,
                    kind="entry", price=px,
                    size=size_usd / max(px, 1e-9), size_usd=size_usd,
                    order_id=f"o{self.now_ms()}{i}",
                    placed_ms=self.now_ms() - i * 60_000,
                    kind_confidence="reported",
                )
                self._orders.setdefault(t.wallet, []).append(order)
                # Same discipline as a real professional: a stop and a target.
                if t.sets_tpsl:
                    rr = 2.0
                    if side == "LONG":
                        stop, target = px * 0.975, px * (1 + 0.025 * rr)
                    else:
                        stop, target = px * 1.025, px * (1 - 0.025 * rr)
                    self._orders[t.wallet].append(
                        Order(wallet=t.wallet, source=self.name, coin=coin, side=side,
                              kind="sl", price=stop, size=order.size,
                              size_usd=size_usd, reduce_only=True,
                              placed_ms=order.placed_ms, kind_confidence="reported")
                    )
                    self._orders[t.wallet].append(
                        Order(wallet=t.wallet, source=self.name, coin=coin, side=side,
                              kind="tp", price=target, size=order.size,
                              size_usd=size_usd, reduce_only=True,
                              placed_ms=order.placed_ms, kind_confidence="reported")
                    )
            else:
                pos = Position(
                    wallet=t.wallet, source=self.name, coin=coin,
                    signed_size=(size_usd / max(px, 1e-9)) * (1 if side == "LONG" else -1),
                    entry=px, size_usd=size_usd, leverage=t.leverage,
                    opened_at=self.now - timedelta(minutes=self.rng.uniform(0, 25)),
                    mark_price=self.market.price(coin),
                )
                # A hedger answers by taking the other side too. Its net
                # exposure is then near zero and `strip_hedged` should discard
                # it — this is the only place hedging is observable, since a
                # closed-trade history shows sides but never netting.
                if t.hedge_prob and self.rng.random() < t.hedge_prob:
                    self._positions.setdefault(t.wallet, []).append(
                        Position(
                            wallet=t.wallet, source=self.name, coin=coin,
                            signed_size=-pos.signed_size,
                            entry=px * (1 + self.rng.uniform(-0.002, 0.002)),
                            size_usd=size_usd, leverage=t.leverage,
                            opened_at=pos.opened_at,
                            mark_price=self.market.price(coin),
                        )
                    )
                if t.sets_tpsl:
                    pos.stop = px * (0.975 if side == "LONG" else 1.025)
                    pos.target = px * (1.05 if side == "LONG" else 0.95)
                self._positions.setdefault(t.wallet, []).append(pos)
            placed.append({"wallet": t.wallet, "price": px, "archetype": t.archetype})

        event = {
            "coin": coin, "side": side, "resting": resting,
            "members": placed, "at": self.now.isoformat(),
        }
        self.events.append(event)
        return event

    # ── venue interface ─────────────────────────────────────────────────────
    async def health(self) -> bool:
        return True

    async def prices(self) -> dict[str, float]:
        # `prices()` is called exactly once per cycle per venue, so the primary
        # venue uses it as the cycle boundary: advance the market, then decide
        # whether traders should agree on something this cycle.
        if self._primary and getattr(self.market, "auto_events", False):
            self._auto_event()
        return dict(self.market.prices)

    def _auto_event(self) -> None:
        """Advance the story without anyone calling in.

        Without this, running the real 24/7 loop against the simulator would
        require reaching into the venue objects to inject an agreement — which
        is impossible once `build_venues` creates them internally. It makes the
        simulator usable from `cli run` and from the runtime tests.
        """
        m = self.market
        self.advance(30)
        m.tick += 1
        every = int(m.event_every or 0)
        if not every or m.tick % every:
            return
        coin = COINS[m.tick % len(COINS)]
        side = "LONG" if (m.tick // len(COINS)) % 2 == 0 else "SHORT"
        # Alternate on the event *number*, not on the tick.
        #
        # `resting = tick % 2 == 0` combined with `event_every = 2` means the
        # event only ever fires on an even tick, so `resting` was permanently
        # true and the simulator produced nothing but unfilled orders — no open
        # position, ever. Half the pipeline went untested for a reason that
        # looked like a coincidence.
        resting = ((m.tick // every) % 2) == 1
        self.inject_cluster(coin, side, resting=resting)
        if self.partner is not None:
            self.partner.inject_cluster(coin, side, resting=resting)

    async def leaderboard(self) -> list[dict]:
        return [
            {"wallet": t.wallet, "account_value": t.account_value,
             "windows": {}, "archetype": t.archetype}
            for t in self.traders
        ]

    @staticmethod
    def parse_leaderboard_row(row: dict) -> dict:
        return {
            "wallet": (row.get("wallet") or "").lower(),
            "account_value": float(row.get("account_value") or 0.0),
            "windows": row.get("windows") or {},
        }

    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]:
        return [f.raw | {
            "time": f.time_ms, "coin": f.coin, "px": f.price, "sz": f.size,
            "closedPnl": f.closed_pnl, "fee": f.fee, "side": f.side,
        } for f in self._history.get(wallet, [])][:limit]

    @staticmethod
    def parse_fills(rows: list[dict]) -> list[Fill]:
        out = []
        for r in rows:
            try:
                out.append(
                    Fill(
                        time_ms=int(r.get("time") or 0),
                        coin=str(r.get("coin") or "UNKNOWN"),
                        side=str(r.get("side") or "BUY").upper(),
                        price=float(r.get("px") or 0.0),
                        size=abs(float(r.get("sz") or 0.0)),
                        closed_pnl=float(r.get("closedPnl") or 0.0),
                        fee=float(r.get("fee") or 0.0),
                        raw=r,
                    )
                )
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda f: f.time_ms)
        return out

    async def positions(self, wallet: str) -> list[dict]:
        # `asdict`, not `.__dict__`: every model in this project is a slotted
        # dataclass, so `.__dict__` raises AttributeError — which the caller
        # swallows, turning "this trader has positions" into a silent zero.
        return [asdict(p) for p in self._positions.get(wallet, [])]

    def parse_positions(self, wallet: str, rows: list[dict]) -> list[Position]:
        # An instance method, not a static one: the source name is the venue's,
        # and hard-coding "sim" made the second venue's positions claim to come
        # from the first.
        out = []
        for r in rows:
            try:
                p = Position(
                    wallet=wallet, source=self.name, coin=str(r.get("coin") or ""),
                    signed_size=float(r.get("signed_size") or 0.0),
                    entry=float(r.get("entry") or 0.0),
                    size_usd=float(r.get("size_usd") or 0.0),
                    leverage=float(r.get("leverage") or 1.0),
                    opened_at=r.get("opened_at"),
                    mark_price=r.get("mark_price"),
                    stop=r.get("stop"), target=r.get("target"),
                )
            except (TypeError, ValueError):
                continue
            out.append(p)
        return out

    async def open_orders(self, wallet: str) -> list[dict]:
        return [asdict(o) for o in self._orders.get(wallet, [])]

    def parse_open_orders(self, wallet: str, rows: list[dict],
                          prices: Optional[dict] = None) -> list[Order]:
        out = []
        for r in rows:
            try:
                out.append(
                    Order(
                        wallet=wallet, source=self.name, coin=str(r.get("coin") or ""),
                        side=str(r.get("side") or "LONG"),
                        kind=str(r.get("kind") or "entry"),
                        price=float(r.get("price") or 0.0),
                        size=float(r.get("size") or 0.0),
                        size_usd=float(r.get("size_usd") or 0.0),
                        order_id=str(r.get("order_id") or ""),
                        reduce_only=bool(r.get("reduce_only")),
                        placed_ms=int(r.get("placed_ms") or 0),
                        kind_confidence=str(r.get("kind_confidence") or "reported"),
                    )
                )
            except (TypeError, ValueError):
                continue
        return out

    # ── inspection ──────────────────────────────────────────────────────────
    def truth(self) -> dict:
        """Who is *actually* good — the ground truth the tests compare against."""
        return {t.wallet: t.archetype for t in self.traders}

    def close_positions(self, wallet: str) -> None:
        self._positions.pop(wallet, None)

    def clear_orders(self, wallet: str) -> None:
        self._orders.pop(wallet, None)


# One simulated world per process, keyed by how it was built. See the docstring
# of `build_sim_venues` for why the world must outlive a single cycle.
_SIM_WORLDS: dict[tuple, dict] = {}


def build_sim_venues(
    n_traders: int = 40,
    seed: int = 7,
    now: Optional[datetime] = None,
    auto_events: bool = True,
    event_every: int = 3,
    fresh: bool = False,
) -> dict[str, SimVenue]:
    """Two sim venues, so cross-venue independence can be exercised.

    A single simulated venue would make every consensus intra-platform, which is
    the weaker case. Traders on `sim-alpha` and `sim-beta` are independent by
    construction, which is exactly the signal we claim to be waiting for.

    The result is memoised per process, because `build_venues` is called on
    every cycle and a simulated venue carries state: its traders, their open
    positions, and the market's tick. Rebuilding it each cycle reset the tick to
    zero, so the scheduled agreement never arrived and a 40-cycle run produced
    no clusters at all — the simulator looked inert when it was simply being
    reborn, empty, sixty times a minute.
    """
    key = (n_traders, seed, auto_events, event_every)
    if not fresh:
        cached = _SIM_WORLDS.get(key)
        if cached is not None:
            return cached

    shared = Market(rng=random.Random(seed + 1))
    shared._advanced_at = None
    alpha = SimVenue(n_traders=n_traders, seed=seed, now=now,
                     wallet_prefix="0xsim", market=shared)
    beta = SimVenue(n_traders=max(8, n_traders // 2), seed=seed + 5000, now=now,
                    wallet_prefix="0xsimb", market=shared)
    beta.name = "sim-beta"
    for t in beta.traders:
        t.source = "sim-beta"
    alpha.partner = beta
    beta.partner = alpha
    alpha._primary = True
    # Agreement appears every few cycles on its own, so `cli run` against the
    # simulator produces signals to look at without being driven by hand.
    alpha.market.auto_events = auto_events
    alpha.market.event_every = event_every
    alpha.market.tick = 0
    if fresh:
        # A throwaway world: measured, then discarded. The negative control
        # depends on this — otherwise it would inherit agreements injected by
        # whatever ran before it and "prove" the radar invents consensus by
        # finding evidence another test left behind.
        return {"sim": alpha, "sim-beta": beta}
    _SIM_WORLDS[key] = {"sim": alpha, "sim-beta": beta}
    return _SIM_WORLDS[key]
