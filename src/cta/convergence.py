"""Convergence detection — where independent traders become one signal.

The gates are deliberately brutal, because a weak convergence is worse than no
convergence: it consumes attention and teaches you to distrust the system.

Pipeline:
    1. net out hedging per wallet (a long on HL + short on dYdX is not an opinion)
    2. group by (coin, side) from net delta — never from a raw BUY/SELL label
    3. cluster by entry price within `entry_tolerance_bps`
    4. require the positions to have opened inside `convergence_window_min`
    5. require >= 2 independent wallets of at least PROVEN grade
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from .config import GRADE_ORDER, Config
from .models import BPS, Cluster, Position

UTC = timezone.utc


# ── step 1: hedging ──────────────────────────────────────────────────────────
def net_deltas(positions: Sequence[Position]) -> dict[tuple[str, str], float]:
    """Collapse all positions into signed USD notional per (wallet, coin).

    Positive = net long exposure, negative = net short.
    """
    net: dict[tuple[str, str], float] = {}
    for p in positions:
        signed = p.size_usd if p.signed_size >= 0 else -p.size_usd
        key = (p.wallet, p.coin)
        net[key] = net.get(key, 0.0) + signed
    return net


def strip_hedged(
    positions: Sequence[Position],
    hedge_ratio_threshold: float = 0.12,
) -> list[Position]:
    """Drop exposure that nets out — those wallets hold no directional view.

    A wallet is considered hedged on a coin when its net exposure is smaller than
    `hedge_ratio_threshold` of its gross exposure on that coin.
    """
    gross: dict[tuple[str, str], float] = {}
    for p in positions:
        key = (p.wallet, p.coin)
        gross[key] = gross.get(key, 0.0) + abs(p.size_usd)

    net = net_deltas(positions)

    kept: list[Position] = []
    for p in positions:
        key = (p.wallet, p.coin)
        g = gross.get(key, 0.0)
        if g <= 0:
            continue
        if abs(net.get(key, 0.0)) / g < hedge_ratio_threshold:
            continue  # hedged: no directional opinion
        kept.append(p)
    return kept


# ── step 2: independence ─────────────────────────────────────────────────────
def _is_twin(a: Position, b: Position, window_sec: float = 30.0,
             size_tolerance: float = 0.01) -> bool:
    """Fingerprint of a mirrored copy-bot pair.

    Two genuinely independent traders can and do open within minutes of each
    other — that is the entire point of convergence detection. A copy bot is far
    tighter than that: the mirrored order fires within seconds *and* carries a
    proportional size. Requiring both keeps the independence rule from eating the
    very signals it is meant to protect.
    """
    if a.coin != b.coin or a.opened_at is None or b.opened_at is None:
        return False

    delta = abs((a.opened_at - b.opened_at).total_seconds())
    if delta > window_sec:
        return False

    if a.size_usd <= 0 or b.size_usd <= 0:
        return False
    ratio = min(a.size_usd, b.size_usd) / max(a.size_usd, b.size_usd)
    return ratio >= 1.0 - size_tolerance


def enforce_independence(
    members: Sequence[Position],
    twin_window_sec: float = 30.0,
) -> list[Position]:
    """Keep the stronger wallet when two look like one mirrored account.

    Multiple wallets driven by the same bot produce a fake consensus; keeping only
    the best-graded of each twinned pair protects the whole premise without
    penalising traders who merely act at the same time.
    """
    if len(members) <= 1:
        return list(members)

    ordered = sorted(
        members,
        key=lambda p: (GRADE_ORDER.get(p.quality, 0), p.quality_score, p.size_usd),
        reverse=True,
    )
    kept: list[Position] = []
    for cand in ordered:
        if any(_is_twin(cand, k, twin_window_sec) for k in kept):
            continue
        kept.append(cand)
    return kept


# ── step 3: clustering ───────────────────────────────────────────────────────
def _cluster_by_price(
    positions: Sequence[Position], tolerance_bps: float
) -> list[list[Position]]:
    """Greedy 1-D clustering on entry price, seeded from the tightest group.

    Anchoring on a *seed* rather than a running mean prevents a slow drift from
    walking a cluster across a wide price band.
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


# ── step 4: orchestration ────────────────────────────────────────────────────
def _cluster_age_min(members: Sequence[Position], now: datetime) -> float:
    """Age of the most recent open in the cluster, in minutes (0 if unknown)."""
    stamps = [p.opened_at for p in members if p.opened_at is not None]
    if not stamps:
        return 0.0
    newest = max(stamps)
    return max(0.0, (now - newest).total_seconds() / 60.0)


def _build_cluster(
    coin: str, side: str, members: Sequence[Position], now: datetime
) -> Optional[Cluster]:
    if not members:
        return None

    entries = [p.entry for p in members]
    center = sum(entries) / len(entries)
    if center <= 0:
        return None
    spread_bps = (max(entries) - min(entries)) / center * BPS

    stamps = [p.opened_at for p in members if p.opened_at is not None]
    if stamps:
        span = (max(stamps) - min(stamps)).total_seconds() / 60.0
    else:
        span = 0.0

    return Cluster(
        coin=coin,
        side=side,
        members=list(members),
        entry_center=center,
        spread_bps=spread_bps,
        time_span_min=span,
        sources=sorted({p.source for p in members}),
    )


def detect_convergence(
    positions: Iterable[Position],
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
) -> list[Cluster]:
    """Find every cluster that passes the gates, strongest first."""
    cfg = cfg or Config()
    now = now or datetime.now(UTC)

    live = [p for p in positions if p.size_usd > 0]
    directional = strip_hedged(live, cfg.hedge_ratio_threshold)

    # Only proven traders are allowed to contribute to consensus.
    min_rank = GRADE_ORDER.get(cfg.watchlist_min_grade.upper(), 2)
    qualified = [p for p in directional if GRADE_ORDER.get(p.quality, 0) >= min_rank]

    clusters: list[Cluster] = []
    seen: set[tuple[str, str]] = {(p.coin, p.side) for p in qualified}

    for coin, side in sorted(seen):
        bucket = [p for p in qualified if p.coin == coin and p.side == side]
        if len(bucket) < cfg.min_independent_traders:
            continue

        for raw_cluster in _cluster_by_price(bucket, cfg.entry_tolerance_bps):
            independent = enforce_independence(raw_cluster)
            if len(independent) < cfg.min_independent_traders:
                continue

            elite_proven = [p for p in independent if p.quality in ("ELITE", "PROVEN")]
            if len(elite_proven) < cfg.min_elite_or_proven:
                continue

            cluster = _build_cluster(coin, side, independent, now)
            if cluster is None:
                continue

            if cluster.spread_bps > cfg.entry_tolerance_bps:
                continue
            if cluster.time_span_min > cfg.convergence_window_min:
                continue
            # Independent of internal synchrony: the whole cluster must still be
            # fresh, otherwise we would alert on a move that already happened.
            if _cluster_age_min(independent, now) > cfg.convergence_window_min:
                continue
            if cluster.median_leverage > cfg.max_median_leverage:
                continue

            clusters.append(cluster)

    clusters.sort(
        key=lambda c: (c.n_members, -c.spread_bps, -c.time_span_min), reverse=True
    )
    return clusters
