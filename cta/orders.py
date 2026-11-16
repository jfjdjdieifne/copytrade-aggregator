"""Resting orders — reading intent instead of history.

The difference this module exists for
-------------------------------------
A fill tells you what a trader did. By the time you see it, the price is gone:
you are copying at *their* price plus however long it took the data to reach
you. A resting order tells you what the trader is *willing* to do, and it is
still there when you read it. If two proven traders have asked to buy the same
asset at the same price, you can stand in the same queue rather than chase them.

That is the only structural advantage available to someone copying traders, and
it is why this layer is built on top of the convergence gates rather than beside
them.

What we refuse to do here
-------------------------
* Infer a stop or a target the trader never published. Hyperliquid marks an
  order as protective but not *which* one it is; that is resolved against the
  live reference price, and when there is no reference price the order stays
  unclassified instead of being pushed into a plan.
* Attribute an order to a trader we have not graded. Orders carry the grade of
  their owner, and ungraded wallets are dropped.
* Let one trader count twice. Orders are netted per wallet per coin per side
  before clustering, exactly as positions are.
"""

from __future__ import annotations

from statistics import median
from typing import Iterable, Optional, Sequence

from .models import BPS, Order, Position

# A resting order older than this is treated as stale: the trader may have
# forgotten it, and a limit that has been sitting unfilled for a day is a very
# different thing from one placed in the last hour.
DEFAULT_MAX_ORDER_AGE_MIN = 24 * 60


def entry_orders(orders: Iterable[Order]) -> list[Order]:
    """Only the orders that express a wish to *open* a position."""
    return [o for o in orders if o.kind == "entry" and o.price > 0]


def protective_orders(orders: Iterable[Order], side: str) -> list[Order]:
    """TP/SL orders belonging to a position on `side`."""
    return [
        o
        for o in orders
        if o.kind in ("tp", "sl") and o.side == side and o.price > 0
    ]


def net_orders(orders: Iterable[Order]) -> list[Order]:
    """Collapse a wallet's orders to one opinion per (coin, side, kind).

    Ten stacked limit orders at slightly different prices are one opinion, not
    ten — the same rule `Position` applies to fills, for the same reason: a
    trader scaling into a position must not be counted as ten independent
    traders agreeing.
    """
    grouped: dict[tuple, list[Order]] = {}
    order_index: list[tuple] = []
    for o in orders:
        key = (o.wallet, o.coin, o.side, o.kind)
        if key not in grouped:
            grouped[key] = []
            order_index.append(key)
        grouped[key].append(o)

    out: list[Order] = []
    for key in order_index:
        bucket = grouped[key]
        total_size = sum(abs(o.size or 1.0) for o in bucket)
        if total_size <= 0:
            total_size = float(len(bucket))
        # Size-weighted mean price: a large order should move the level more
        # than a small one, which is what "where is this trader actually
        # willing to trade" means.
        weights = [abs(o.size or 1.0) for o in bucket]
        price = sum(o.price * w for o, w in zip(bucket, weights)) / total_size
        best = max(bucket, key=lambda o: o.size_usd)
        out.append(
            Order(
                wallet=best.wallet,
                source=best.source,
                coin=best.coin,
                side=best.side,
                kind=best.kind,
                price=price,
                size=total_size,
                order_id=best.order_id,
                size_usd=sum(o.size_usd for o in bucket),
                reduce_only=best.reduce_only,
                trigger_px=best.trigger_px,
                placed_ms=max(o.placed_ms for o in bucket),
                kind_confidence=best.kind_confidence,
                quality=best.quality,
                quality_score=best.quality_score,
                raw=best.raw,
            )
        )
    return out


def attach_protective(
    positions: Sequence[Position],
    orders: Sequence[Order],
) -> int:
    """Copy each trader's own stop/target onto their position, in place.

    Returns how many levels were attached. This is the difference between a
    plan that says "the traders put their stop here" and one that says "we
    computed a stop from ATR" — both are useful, but they are not the same
    claim, and `plan.py` labels them differently for exactly that reason.

    Only levels that agree with the position are attached: a take-profit above
    a long and a stop-loss below it. Anything on the wrong side is ignored,
    because the trader may be hedging, closing partially, or simply wrong.
    """
    by_wallet: dict[str, list[Order]] = {}
    for o in orders:
        if o.kind not in ("tp", "sl"):
            continue
        by_wallet.setdefault(o.wallet, []).append(o)

    attached = 0
    for pos in positions:
        mine = by_wallet.get(pos.wallet)
        if not mine:
            continue
        if pos.is_long:
            tps = [o.price for o in mine if o.kind == "tp" and o.price > pos.entry]
            sls = [o.price for o in mine if o.kind == "sl" and o.price < pos.entry]
        else:
            tps = [o.price for o in mine if o.kind == "tp" and o.price < pos.entry]
            sls = [o.price for o in mine if o.kind == "sl" and o.price > pos.entry]
        if tps:
            pos.target = median(sorted(tps))
            attached += 1
        if sls:
            pos.stop = median(sorted(sls))
            attached += 1
    return attached


def executable_now(
    order_price: float,
    market_price: Optional[float],
    side: str,
    tolerance_bps: float,
) -> bool:
    """Can we still trade at (or better than) the traders' level?

    A bid below the market is still reachable — you can buy at the market or
    better. A bid more than `tolerance_bps` *above* the market means the
    traders were willing to pay up and the opportunity to stand with them has
    already been taken; chasing it is how a copy trade turns into a bad fill.
    """
    if not market_price or market_price <= 0 or order_price <= 0:
        return True          # no reference price: do not invent a verdict
    diff_bps = (order_price - market_price) / market_price * BPS
    if side == "LONG":
        return diff_bps <= tolerance_bps
    return -diff_bps <= tolerance_bps


def fresh(
    order: Order,
    now_ms: Optional[int] = None,
    max_age_min: float = DEFAULT_MAX_ORDER_AGE_MIN,
) -> bool:
    """Has the trader abandoned this order?

    A limit left at a price the market left behind hours ago is not intent, it
    is litter. Without this, a wallet that places one order a week looks like a
    permanent consensus partner for every future cluster.
    """
    if not order.placed_ms:
        return True          # unknown age: treated as fresh, not as stale
    now_ms = now_ms if now_ms is not None else _now_ms()
    return (now_ms - order.placed_ms) <= max_age_min * 60_000.0


def _now_ms() -> int:
    from datetime import datetime, timezone

    return int(datetime.now(timezone.utc).timestamp() * 1000)


def summarise(orders: Sequence[Order]) -> dict:
    """Counts by kind and source — for the health page and the heartbeat.

    Deliberately counts *what we could see*, not what we assume exists: OKX
    publishes no resting orders at all, and the report should say so rather
    than show a zero that looks like a bug.
    """
    by_source: dict[str, int] = {}
    for o in orders:
        by_source[o.source] = by_source.get(o.source, 0) + 1
    return {
        "total": len(orders),
        "entry": sum(1 for o in orders if o.kind == "entry"),
        "tp": sum(1 for o in orders if o.kind == "tp"),
        "sl": sum(1 for o in orders if o.kind == "sl"),
        "unknown": sum(1 for o in orders if o.kind_confidence == "unknown"),
        "by_source": by_source,
    }
