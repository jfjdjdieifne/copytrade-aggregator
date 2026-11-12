"""The orchestrator: one cycle of the radar, end to end.

    profile_watchlist()  daily  : candidates -> real fills -> grades -> watchlist
    scan()               minute : watchlist -> positions -> clusters -> signals
    build_signal()              : cluster + confidence + plan -> explainable Signal

Every step takes its dependencies as arguments (store, venues, clock) so a whole
cycle can be exercised in a test with canned venue responses and no network.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional, Sequence

from .classifier import classify_trader, stats_from_fills
from .confluence_helpers import dedupe_positions, merge_members
from .confidence import score_cluster
from .config import Config
from .convergence import detect_convergence
from .discovery import build_watchlist, select_candidates
from .models import Cluster, Position, Signal, TraderStats
from .plan import build_plan
from .state import KEY_PROFILES, KEY_WATCHLIST, Store

UTC = timezone.utc


def signal_id_for(coin: str, side: str, entry: float, opened_ms: int) -> str:
    """Stable id so the same cluster never notifies twice.

    Bucketing entry to 10 bps means trivial price jitter does not create a new id;
    a genuinely new agreement will.
    """
    bucket = int(round(entry * 10)) if entry > 0 else 0
    raw = f"{coin}:{side}:{bucket}:{opened_ms // 900_000}"   # 15-minute bucket
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ── phase 1: profiling ───────────────────────────────────────────────────────
async def profile_watchlist(
    venues: dict,
    store: Store,
    cfg: Optional[Config] = None,
    limit: Optional[int] = None,
) -> list[TraderStats]:
    """Build or refresh the watchlist by scoring real fills for each candidate."""
    cfg = cfg or Config()
    limit = limit or cfg.discovery_max_candidates

    hl = venues.get("hyperliquid")
    candidates = []
    if hl is not None:
        try:
            rows = await hl.leaderboard()
        except Exception:
            rows = []
        parsed = [hl.parse_leaderboard_row(r) for r in rows]
        candidates = select_candidates(parsed, cfg, "hyperliquid")

    # Seed venues contribute directly — no leaderboard to triage.
    for name in ("dydx", "gmx"):
        venue = venues.get(name)
        if venue is None:
            continue
        try:
            seeds = await venue.leaderboard()
        except Exception:
            seeds = []
        for row in seeds:
            wallet = (row.get("wallet") or "").lower()
            if wallet:
                from .discovery import Candidate

                candidates.append(
                    Candidate(wallet=wallet, source=name, reasons=["seed wallet"])
                )

    scored: list[TraderStats] = []
    for cand in candidates[:limit]:
        venue = venues.get(cand.source)
        if venue is None:
            continue
        try:
            raw_fills = await venue.fills(cand.wallet, limit=cfg.max_fills_per_profile)
        except Exception:
            continue
        fills = venue.parse_fills(raw_fills)
        if not fills:
            continue

        stats = stats_from_fills(
            cand.wallet,
            fills,
            account_value_usd=cand.account_value,
            source=cand.source,
            data_confirmed=cand.source == "hyperliquid",
        )
        classify_trader(stats)
        scored.append(stats)

    watchlist = build_watchlist(scored, cfg)
    await store.set(KEY_PROFILES, [_stats_to_dict(s) for s in scored[:400]])
    await store.set(KEY_WATCHLIST, [_stats_to_dict(s) for s in watchlist])
    return watchlist


def _stats_to_dict(s: TraderStats) -> dict:
    return {
        "wallet": s.wallet,
        "source": s.source,
        "grade": s.grade,
        "score": s.score,
        "n_trades": s.n_trades,
        "win_rate": round(s.win_rate, 4),
        "profit_factor": round(s.profit_factor, 3),
        "max_drawdown": round(s.max_drawdown, 4),
        "consistency": round(s.consistency, 4),
        "concentration": round(s.concentration, 4),
        "active_days": s.active_days,
        "account_value_usd": s.account_value_usd,
        "reasons": s.reasons[:6],
    }


# ── phase 2: scanning ────────────────────────────────────────────────────────
async def collect_positions(
    venues: dict,
    watchlist: Sequence[dict],
    cfg: Optional[Config] = None,
    limit: Optional[int] = None,
) -> list[Position]:
    """Poll positions for every watched wallet — the cheap call, done often."""
    cfg = cfg or Config()
    limit = limit or cfg.watchlist_max
    quality_by_wallet = {w["wallet"]: (w.get("grade", "WATCH"), w.get("score", 0)) for w in watchlist}

    positions: list[Position] = []
    for entry in watchlist[:limit]:
        wallet = entry.get("wallet")
        source = entry.get("source", "hyperliquid")
        venue = venues.get(source)
        if venue is None or not wallet:
            continue
        try:
            raw = await venue.positions(wallet)
        except Exception:
            continue
        parsed = venue.parse_positions(wallet, raw)
        grade, score = quality_by_wallet.get(wallet, ("WATCH", 0))
        for p in parsed:
            p.quality = grade
            p.quality_score = int(score)
            positions.append(p)

    return merge_members(dedupe_positions(positions))


def build_signal(
    cluster: Cluster,
    cfg: Optional[Config] = None,
    market_price: Optional[float] = None,
    atr_hint: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Optional[Signal]:
    """Turn a passing cluster into a fully explainable Signal (or None)."""
    cfg = cfg or Config()
    now = now or datetime.now(UTC)

    conf = score_cluster(cluster, cfg)
    if not conf.emitted:
        return None

    plan = build_plan(cluster, cfg, atr_hint=atr_hint, market_price=market_price)
    if plan is None:
        return None

    opened_ms = 0
    stamps = [m.opened_at for m in cluster.members if m.opened_at is not None]
    if stamps:
        opened_ms = int(min(stamps).timestamp() * 1000)

    members = [
        {
            "wallet": m.wallet,
            "source": m.source,
            "entry": m.entry,
            "size_usd": round(m.size_usd, 2),
            "leverage": m.leverage,
            "quality": m.quality,
            "quality_score": m.quality_score,
            "age_min": round(m.age_minutes(now), 0),
        }
        for m in sorted(cluster.members, key=lambda x: x.quality_score, reverse=True)
    ]

    return Signal(
        coin=cluster.coin,
        side=cluster.side,
        score=conf.total,
        level=conf.level,
        entry_center=cluster.entry_center,
        spread_bps=cluster.spread_bps,
        n_independent=cluster.n_members,
        sources=cluster.sources,
        plan=plan,
        members=members,
        created_at=now,
        signal_id=signal_id_for(cluster.coin, cluster.side, cluster.entry_center, opened_ms),
        market_price=market_price,
        breakdown=conf.breakdown,
        reasons=conf.reasons,
    )


async def scan(
    venues: dict,
    store: Store,
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
    prices: Optional[dict[str, float]] = None,
    watchlist: Optional[Sequence[dict]] = None,
) -> tuple[list[Signal], list[Cluster]]:
    """One monitoring cycle. Returns (emitted signals, all raw clusters)."""
    cfg = cfg or Config()
    now = now or datetime.now(UTC)

    if watchlist is None:
        watchlist = await store.get(KEY_WATCHLIST, []) or []

    if not watchlist:
        return [], []

    positions = await collect_positions(venues, watchlist, cfg)

    if prices is None:
        prices = {}
        hl = venues.get("hyperliquid")
        if hl is not None:
            try:
                prices = await hl.prices()
            except Exception:
                prices = {}

    clusters = detect_convergence(positions, cfg, now=now)

    seen = set(await store.get("seen_signal_ids", []) or [])
    emitted: list[Signal] = []
    for cluster in clusters:
        price = prices.get(cluster.coin)
        sig = build_signal(cluster, cfg, market_price=price, now=now)
        if sig is None:
            continue
        if sig.signal_id in seen:
            continue
        seen.add(sig.signal_id)
        emitted.append(sig)

    await store.set("seen_signal_ids", sorted(seen)[-500:])
    return emitted, clusters


async def run_cycle(
    venues: dict,
    store: Store,
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
) -> dict:
    """A full cycle used by the cron trigger: refresh watchlist if empty, then scan."""
    cfg = cfg or Config()
    now = now or datetime.now(UTC)

    watchlist = await store.get(KEY_WATCHLIST, []) or []
    profiled = False
    if not watchlist:
        watchlist = await profile_watchlist(venues, store, cfg)
        profiled = True
        watchlist = await store.get(KEY_WATCHLIST, []) or watchlist

    signals, clusters = await scan(venues, store, cfg, now=now, watchlist=watchlist)

    return {
        "profiled": profiled,
        "watchlist_size": len(watchlist),
        "clusters": len(clusters),
        "signals": len(signals),
        "signal_objects": signals,
    }
