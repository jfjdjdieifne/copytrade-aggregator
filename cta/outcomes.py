"""Forward outcome tracking — the only honest way to know if the radar works.

Every emitted signal is re-checked at fixed horizons against live prices. We record
both what happened at the horizon *and* the extremes along the way (max favourable
and max adverse excursion), because a signal that touched target and then reversed
tells a very different story from one that never moved.

No backtest fantasy: this measures what happened after we published.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


UTC = timezone.utc

# horizons in minutes
HORIZONS = (60, 240, 1440, 4320)


def next_horizon(evaluated: list[int]) -> Optional[int]:
    for h in HORIZONS:
        if h not in evaluated:
            return h
    return None


def _price_path(prices: list[float], side: str) -> tuple[float, float]:
    """Best and worst excursion relative to the first observed price."""
    if not prices:
        return 0.0, 0.0
    first = prices[0]
    if first <= 0:
        return 0.0, 0.0
    if side == "LONG":
        return (max(prices) - first) / first, (min(prices) - first) / first
    return (first - min(prices)) / first, (first - max(prices)) / first


def evaluate(
    signal: dict,
    current_price: float,
    prices_seen: Optional[list[float]] = None,
    horizon_min: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Score one signal against the current market price.

    Returns a partial outcome dict; the caller stores it and later aggregates with
    `summarize`.
    """
    now = now or datetime.now(UTC)
    side = signal.get("side", "LONG")
    plan = signal.get("plan") or {}

    entry = float(plan.get("entry") or signal.get("entry_center") or 0.0)
    stop = plan.get("stop")
    target = plan.get("target")

    created_raw = signal.get("created_at")
    age_min = 0.0
    if created_raw:
        try:
            created = datetime.fromisoformat(str(created_raw))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_min = (now - created).total_seconds() / 60.0
        except ValueError:
            age_min = 0.0

    path = list(prices_seen or [])
    if current_price and current_price > 0:
        path.append(current_price)

    mfe, mae = _price_path(path or ([entry] if entry else []), side)

    hit_target = False
    hit_stop = False
    if entry > 0 and current_price > 0:
        if side == "LONG":
            hit_target = bool(target and current_price >= float(target))
            hit_stop = bool(stop and current_price <= float(stop))
        else:
            hit_target = bool(target and current_price <= float(target))
            hit_stop = bool(stop and current_price >= float(stop))

    r_multiple: Optional[float] = None
    if entry > 0 and current_price > 0 and stop is not None:
        risk = abs(entry - float(stop))
        if risk > 0:
            move = (current_price - entry) if side == "LONG" else (entry - current_price)
            r_multiple = move / risk

    return {
        "signal_id": signal.get("signal_id"),
        "coin": signal.get("coin"),
        "side": side,
        "evaluated_at": now.isoformat(),
        "age_min": round(age_min, 1),
        "horizon_min": horizon_min,
        "price": current_price,
        "hit_target": hit_target,
        "hit_stop": hit_stop,
        "mfe_pct": round(mfe * 100, 4),
        "mae_pct": round(mae * 100, 4),
        "r_multiple": None if r_multiple is None else round(r_multiple, 3),
    }


def summarize(outcomes: list[dict]) -> dict:
    """Aggregate evaluated signals into the numbers `/stats` reports.

    Signals still inside their first hour are excluded from the quality verdict:
    judging a trade before it has had time to work is noise, not evidence.
    """
    if not outcomes:
        return {"evaluated": 0}

    # Keep the latest evaluation per signal.
    latest: dict[str, dict] = {}
    for o in outcomes:
        sid = o.get("signal_id") or ""
        prev = latest.get(sid)
        if prev is None or (o.get("age_min", 0) > prev.get("age_min", 0)):
            latest[sid] = o

    scored = [o for o in latest.values() if (o.get("age_min") or 0) >= 60]
    n = len(scored)
    if n == 0:
        return {"evaluated": 0, "pending": len(latest)}

    hit_target = sum(1 for o in scored if o.get("hit_target"))
    hit_stop = sum(1 for o in scored if o.get("hit_stop"))

    r_values = [o["r_multiple"] for o in scored if o.get("r_multiple") is not None]
    mean_r = sum(r_values) / len(r_values) if r_values else None

    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (None if not wins else 99.0)

    mean_mfe = sum(o.get("mfe_pct", 0) for o in scored) / n
    mean_mae = sum(o.get("mae_pct", 0) for o in scored) / n

    return {
        "evaluated": n,
        "pending": len(latest) - n,
        "hit_target": hit_target,
        "hit_stop": hit_stop,
        "open_or_timeout": n - hit_target - hit_stop,
        "mean_r": None if mean_r is None else round(mean_r, 3),
        "profit_factor": None if profit_factor is None else round(profit_factor, 3),
        "mean_mfe_pct": round(mean_mfe, 3),
        "mean_mae_pct": round(mean_mae, 3),
        "best_r": None if not r_values else round(max(r_values), 2),
        "worst_r": None if not r_values else round(min(r_values), 3),
    }
