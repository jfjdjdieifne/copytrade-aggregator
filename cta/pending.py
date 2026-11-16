"""Pending-order consensus — the signal you can still act on at their price.

This is the layer that turns "copy trading" from an exercise in arriving late
into something structurally better. Everything else in this project reads what
already happened. This reads what traders are asking for *right now*.

How a pending cluster is judged
------------------------------
The same gates as a filled cluster, and one extra:

1. net out hedging (a wallet resting both a bid and an ask has no view)
2. group by (coin, side) taken from the intended position, not the order's own
   buy/sell label
3. cluster by price within `entry_tolerance_bps`
4. require >= 2 independent wallets of at least PROVEN grade
5. **require the level to still be reachable** — a bid far above the market has
   already been overtaken; standing there means buying at a worse price than
   the traders would have paid

Gate 5 has no analogue for filled positions, and it is the one that decides
whether a pending signal is a genuine "stand in the queue" or a trap.

Why synthetic positions
-----------------------
Downstream (`confidence`, `plan`, telegram, outcomes, the paper account) all
speak `Position`. Rather than duplicate every one of those modules for a second
data type, resting orders are projected into positions whose `entry` is the
order price. That is a faithful projection — the entry price of a trade you
have not taken yet *is* the order price — and it keeps one code path honest
instead of two drifting apart. The resulting Signal is tagged `kind="pending"`
so nobody has to guess which world they are looking at.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from .config import GRADE_ORDER, Config
from .convergence import detect_convergence
from .models import BPS, Cluster, Order, Position
from .orders import DEFAULT_MAX_ORDER_AGE_MIN, fresh

UTC = timezone.utc


def as_positions(
    orders: Iterable[Order],
    min_quality_rank: int = 2,
) -> list[Position]:
    """Project resting entry orders into Position objects.

    Only orders from graded wallets survive: an ungraded wallet's resting order
    carries no information about whether we want to stand next to it.

    `size_usd` is the order's notional where the venue lets us compute it. Where
    it does not (OKX publishes no multiplier for contract sizes) we fall back to
    price x size, which is wrong in absolute terms but preserves the *relative*
    weighting that the hedging and netting maths depends on — and it is recorded
    so the failure mode is visible rather than silent.
    """
    out: list[Position] = []
    for o in orders:
        if o.kind != "entry" or o.price <= 0:
            continue
        if GRADE_ORDER.get(o.quality, 0) < min_quality_rank:
            continue
        size_usd = o.size_usd or (abs(o.price * o.size) if o.size else 0.0)
        if size_usd <= 0:
            continue
        out.append(
            Position(
                wallet=o.wallet,
                source=o.source,
                coin=o.coin,
                signed_size=abs(o.size) if o.side == "LONG" else -abs(o.size),
                entry=o.price,
                size_usd=size_usd,
                leverage=1.0,            # a resting order reveals no leverage
                opened_at=o.placed_at(),
                quality=o.quality,
                quality_score=o.quality_score,
                stop=None,
                target=None,
            )
        )
    return out


def _cluster_by_price(
    positions: Sequence[Position], tolerance_bps: float
) -> list[list[Position]]:
    """Greedy 1-D clustering anchored on the tightest group.

    Mirrors `convergence._cluster_by_price` deliberately: the same anchoring
    rule must apply to pending and filled clusters, or the two would not be
    comparable and the operator could not tell which kind of signal is rarer.
    """
    if not positions:
        return []
    remaining = sorted(positions, key=lambda p: p.entry)
    clusters: list[list[Position]] = []
    while remaining:
        seed = remaining[0]
        base = seed.entry
        if base <= 0:
            remaining = remaining[1:]
            continue
        band = base * tolerance_bps / BPS
        cluster = [p for p in remaining if abs(p.entry - base) <= band]
        clusters.append(cluster)
        remaining = [p for p in remaining if p not in cluster]
    return clusters


def reachable(
    position: Position,
    market_price: Optional[float],
    tolerance_bps: float,
) -> bool:
    """Is this trader's resting level still something we can trade at?

    A bid sitting far *above* the market has already been overtaken: the
    traders were willing to pay up and the window to stand with them at their
    price has closed. Filling there means buying worse than they would have.

    Applied per member, before clustering, rather than to a cluster average — an
    average would hide the one member that makes the whole signal untradeable.
    """
    if not market_price or market_price <= 0:
        return True          # no reference price: do not invent a verdict
    diff_bps = (position.entry - market_price) / market_price * BPS
    overrun = diff_bps if position.is_long else -diff_bps
    return overrun <= tolerance_bps


def detect_pending(
    orders: Iterable[Order],
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
    prices: Optional[dict[str, float]] = None,
    max_age_min: float = DEFAULT_MAX_ORDER_AGE_MIN,
) -> list[Cluster]:
    """Resting-order clusters that pass every gate, strongest first.

    After the pending-specific gates (grade, freshness, reachability) the
    remaining work is *identical* to filled-position convergence, so it is
    delegated rather than reimplemented. Two code paths answering the same
    question is how a radar ends up treating a resting order and a filled
    position as incomparable things.
    """
    cfg = cfg or Config()
    now = now or datetime.now(UTC)
    prices = prices or {}

    min_rank = GRADE_ORDER.get(cfg.watchlist_min_grade.upper(), 2)
    now_ms = int(now.timestamp() * 1000)

    usable = [
        o
        for o in orders
        if o.kind == "entry"
        and o.price > 0
        and GRADE_ORDER.get(o.quality, 0) >= min_rank
        and fresh(o, now_ms=now_ms, max_age_min=max_age_min)
    ]

    pool = as_positions(usable, min_quality_rank=min_rank)
    # Reachability needs a reference price per coin, so it is applied here
    # where the mapping is available rather than inside `detect_convergence`.
    pool = [
        p for p in pool if reachable(p, prices.get(p.coin), cfg.max_slippage_bps)
    ]
    if not pool:
        return []

    return detect_convergence(pool, cfg, now=now)
