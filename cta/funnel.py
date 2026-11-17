"""Why is there no signal? The question no monitoring system answers well.

`doctor` says whether the venues answer. It cannot tell the difference between
two very different situations:

  * the radar is watching 140 graded professionals and none of them agree
    right now — the market is quiet, and waiting is correct
  * the radar can see nobody at all, or its thresholds are tighter than any
    real convergence ever is — it will never fire, and waiting is a waste

Both look identical from the outside: silence. One is patience, the other is a
misconfiguration costing days.

This module opens the funnel and reports, for every group of traders who are
*nearly* in agreement, exactly which gate stopped them. After one cycle on a
live machine it answers the question the operator actually has: will this thing
ever fire, and if not, what is too tight?
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .config import Config
from .convergence import _cluster_by_price, enforce_independence, strip_hedged
from .models import Position

UTC = timezone.utc


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def near_misses(
    positions: Sequence[Position],
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Every group of professionals who nearly agreed, and what stopped them.

    Groups are reported even when they fail — that is the point. A group that
    failed only on the time window is a real convergence that arrived too
    slowly; a group that failed on price is two unrelated trades. The operator
    needs to know which, because only one of them is worth waiting for.
    """
    cfg = cfg or Config()
    now = now or datetime.now(UTC)

    survivors = strip_hedged(positions, cfg.hedge_ratio_threshold)

    # Group by coin and NET direction, exactly as detection does.
    by_key: dict[tuple[str, str], list[Position]] = {}
    for p in survivors:
        if not p.coin:
            continue
        by_key.setdefault((p.coin, p.side), []).append(p)

    out: list[dict[str, Any]] = []
    for (coin, side), group in by_key.items():
        if len(group) < 2:
            continue
        # Grouped with a deliberately wide band.
        #
        # Clustering at the configured tolerance hid the most useful diagnostic
        # of all: two professionals on the same coin and side whose entries are
        # simply far apart. That is not "nobody agrees", it is "they agree on
        # direction but not on price" — a completely different problem, and one
        # the operator can only see if we show them the spread instead of
        # splitting them into two silent groups.
        grouping_bps = max(float(cfg.entry_tolerance_bps or 50.0) * 10.0, 1500.0)
        for cluster in _cluster_by_price(group, grouping_bps):
            if len(cluster) < 2:
                continue
            independent = enforce_independence(
                cluster, getattr(cfg, "twin_window_sec", 30.0)
            )
            entries = [p.entry for p in independent]
            spread_bps = ((max(entries) - min(entries)) / _median(entries)) * 10_000

            stamps = [p.opened_at for p in independent if p.opened_at is not None]
            span_min = 0.0
            age_min = 0.0
            if stamps:
                span_min = (max(stamps) - min(stamps)).total_seconds() / 60.0
                age_min = max(0.0, (now - max(stamps)).total_seconds() / 60.0)

            leverage = _median([p.leverage for p in independent])
            elites = sum(
                1 for p in independent if p.quality in ("ELITE", "PROVEN")
            )
            sources = sorted({p.source for p in independent})

            failed: list[str] = []
            if len(independent) < cfg.min_independent_traders:
                failed.append(
                    f"المستقلون {len(independent)} < {cfg.min_independent_traders}"
                )
            if elites < cfg.min_elite_or_proven:
                failed.append(
                    f"المحترفون {elites} < {cfg.min_elite_or_proven}"
                )
            if spread_bps > cfg.entry_tolerance_bps:
                failed.append(
                    f"انتشار السعر {spread_bps:.0f}bps > {cfg.entry_tolerance_bps:.0f}"
                )
            if span_min > cfg.convergence_window_min:
                failed.append(
                    f"تباعد الوقت {span_min:.0f}د > {cfg.convergence_window_min:.0f}"
                )
            if age_min > cfg.convergence_window_min:
                failed.append(
                    f"عمر الأحدث {age_min:.0f}د > {cfg.convergence_window_min:.0f}"
                )
            if cfg.max_median_leverage and leverage > cfg.max_median_leverage:
                failed.append(
                    f"الرافعة {leverage:.1f}x > {cfg.max_median_leverage:.1f}x"
                )

            out.append({
                "coin": coin,
                "side": side,
                "n_raw": len(cluster),
                "n_independent": len(independent),
                "n_sources": len(sources),
                "sources": sources,
                "elites": elites,
                "spread_bps": round(spread_bps, 1),
                "span_min": round(span_min, 1),
                "age_min": round(age_min, 1),
                "median_leverage": round(leverage, 2),
                "passed": not failed,
                "failed_gates": failed,
            })

    # Closest to firing first: the ones that failed on the fewest gates, then
    # the largest groups.
    out.sort(key=lambda r: (len(r["failed_gates"]), -r["n_independent"],
                            r["spread_bps"]))
    return out[:limit]


def diagnose(
    positions: Sequence[Position],
    watchlist: Sequence[dict],
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One paragraph an operator can act on.

    Returns the counts, the near misses, and — the part that matters — a plain
    reading of what the silence means.
    """
    cfg = cfg or Config()
    now = now or datetime.now(UTC)

    wallets_with_positions = {p.wallet for p in positions if p.coin}
    graded = len(watchlist or [])
    misses = near_misses(positions, cfg, now=now)

    if not positions:
        verdict = (
            "ما نشوف ولا مركز مفتوح. إما المنصات ما بتستجيب، أو البحث لسه "
            "ما بنى قائمة المراقبة. شغّل doctor أول."
        )
    elif graded == 0:
        verdict = (
            f"شايف {len(positions)} مركز بس قائمة المراقبة فاضية — البحث ما "
            "لاقى متداولين اجتازو التقييم."
        )
    elif not misses:
        verdict = (
            f"مراقب {graded} متداول و{len(wallets_with_positions)} منهم بمركز مفتوح، "
            "بس ما في اتنين على نفس العملة والاتجاه. السوق هادي — الانتظار صح."
        )
    else:
        closest = misses[0]
        if closest["passed"]:
            verdict = (
                f"في إجماع جاهز: {closest['coin']} {closest['side']} "
                f"({closest['n_independent']} متداول)."
            )
        else:
            gates = "، ".join(closest["failed_gates"])
            verdict = (
                f"أقرب إجماع: {closest['coin']} {closest['side']} "
                f"{closest['n_independent']} متداول، وقفو عند: {gates}."
            )

    return {
        "watchlist": graded,
        "positions_seen": len(positions),
        "wallets_holding": len(wallets_with_positions),
        "coins_seen": sorted({p.coin for p in positions if p.coin}),
        "near_misses": misses,
        "verdict": verdict,
        "thresholds": {
            "min_independent": cfg.min_independent_traders,
            "min_elite_or_proven": cfg.min_elite_or_proven,
            "entry_tolerance_bps": cfg.entry_tolerance_bps,
            "convergence_window_min": cfg.convergence_window_min,
            "max_median_leverage": cfg.max_median_leverage,
            "min_signal_score": cfg.min_signal_score,
        },
    }
