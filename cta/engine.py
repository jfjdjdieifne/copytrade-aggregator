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
from .state import KEY_PROFILES, KEY_WATCHLIST, Store, get_watchlist

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


async def profile_watchlist_resumable(
    venues: dict,
    store: Store,
    cfg: Optional[Config] = None,
    budget=None,
    now: Optional[datetime] = None,
) -> dict:
    """Score as many leaderboard candidates as this invocation can afford.

    Why this exists
    ---------------
    ``profile_watchlist`` scored up to 300 candidates in one invocation. Each
    fill call costs up to 3 subrequests (2 retries), so the worst case was 901
    against the free plan's 50-subrequest ceiling. The platform terminated the
    invocation and ``except Exception: continue`` reported the termination as
    "no candidates" — three green platforms next to "0 متداول".

    This version never attempts more than the budget allows, persists a cursor,
    and resumes next invocation. Discovery becomes a patient multi-run search
    instead of a doomed all-at-once one.

    Returns a report describing the pass, never a bare list: the caller needs to
    distinguish "nothing good found yet" from "we are still looking".
    """
    from .budget import SubrequestBudget
    from .discovery_resume import (
        empty_progress,
        load_progress,
        merge_watchlist,
        plan_slice,
        resume_index,
        save_progress,
        score_slice,
        needs_more_work,
    )
    from .state import KEY_PROFILES

    cfg = cfg or Config()
    now = now or datetime.now(UTC)
    budget = budget or SubrequestBudget(cfg.subrequest_limit, cfg.subrequest_reserve)

    progress = await load_progress(store)
    if not isinstance(progress, dict):
        progress = empty_progress()

    # ── 1. triage the leaderboard (1 subrequest) ────────────────────────────
    # Charge first, but never let an exhausted budget raise out of this
    # function: the cron shares the call with the Telegram poll, so an
    # exception here is a bot that stops answering.
    from .budget import BudgetExhausted

    hl = venues.get("hyperliquid")
    rows: list = []
    try:
        budget.spend(1)
    except BudgetExhausted as exc:
        progress["last_error"] = f"budget: {exc}"[:160]
        progress["budget"] = budget.report()
        progress["last_run_at"] = now.isoformat()
        await save_progress(store, progress)
        return {
            "ok": False,
            "scored": 0,
            "passed": 0,
            "total": 0,
            "cursor": 0,
            "complete": False,
            "reason": progress["last_error"],
            "budget": budget.report(),
            "progress": progress,
            "watchlist_size": len(await get_watchlist(store)),
        }

    if hl is not None:
        try:
            rows = await hl.leaderboard()
        except Exception as exc:
            progress["last_error"] = f"leaderboard: {type(exc).__name__}: {exc}"[:160]
            rows = []
    else:
        progress["last_error"] = "no hyperliquid adapter"

    parsed = [hl.parse_leaderboard_row(r) for r in rows] if hl is not None else []
    candidates = select_candidates(parsed, cfg, "hyperliquid")

    # Seeds on venues without a leaderboard contribute directly.
    for name in ("dydx", "gmx"):
        venue = venues.get(name)
        if venue is None:
            continue
        try:
            seeds = await venue.leaderboard()
        except Exception:
            seeds = []
        from .discovery import Candidate

        for row in seeds:
            wallet = (row.get("wallet") or "").lower()
            if wallet:
                candidates.append(Candidate(wallet=wallet, source=name, reasons=["seed wallet"]))

    total = len(candidates)
    progress["total"] = total
    if rows:
        progress["last_leaderboard_at"] = now.isoformat()

    # ── 2. decide how much of it we can afford ──────────────────────────────
    start = resume_index(progress, total)
    from .budget import cost_of_fetch

    per_candidate = cost_of_fetch(cfg.discovery_fetch_retries)
    count = cfg.candidate_page_size or plan_slice(total, start, budget, per_candidate)
    count = min(count, total - start) if total else 0

    # A leaderboard we could not read is not "zero candidates found": it is a
    # failed search. Reporting ok=True here would be the same lie as the empty
    # watchlist that started this — the distinction the operator needs is
    # "nothing good yet" versus "we never got to look".
    if not total and progress.get("last_error"):
        progress["budget"] = budget.report()
        progress["last_run_at"] = now.isoformat()
        await save_progress(store, progress)
        return {
            "ok": False,
            "scored": 0,
            "passed": 0,
            "total": 0,
            "cursor": start,
            "complete": False,
            "reason": progress["last_error"],
            "budget": budget.report(),
            "progress": progress,
            "watchlist_size": len(await get_watchlist(store)),
        }

    if total and count <= 0:
        # Budget was consumed before discovery could start. Record it; do not
        # pretend the search ran.
        progress["last_error"] = "budget exhausted before discovery could start"
        progress["budget"] = budget.report()
        progress["last_run_at"] = now.isoformat()
        await save_progress(store, progress)
        return {
            "ok": False,
            "scored": 0,
            "passed": 0,
            "total": total,
            "cursor": start,
            "complete": False,
            "reason": progress["last_error"],
            "budget": budget.report(),
            "progress": progress,
            "watchlist_size": len(await get_watchlist(store)),
        }

    # ── 3. score the affordable slice ──────────────────────────────────────
    stats, done, failures = await score_slice(candidates, start, count, venues, cfg, budget)
    progress["cursor"] = start + done
    progress["scored"] = int(progress.get("scored", 0) or 0) + len(stats)

    if progress["cursor"] >= total and total:
        progress["passes"] = int(progress.get("passes", 0) or 0) + 1
        progress["cursor"] = 0          # a completed pass restarts on fresh data
    progress["skipped"] = max(0, count - done)

    # ── 4. merge into the persistent watchlist ─────────────────────────────
    existing = await store.get(KEY_WATCHLIST, []) or []
    merged = merge_watchlist(existing, stats, cfg)
    await store.set(KEY_WATCHLIST, merged)

    passed = len([s for s in stats if getattr(s, "grade", "REJECT") in ("ELITE", "PROVEN")])
    progress["passed"] = int(progress.get("passed", 0) or 0) + passed

    # Also accumulate the full scored set so /stats can show the funnel.
    all_scored = await store.get(KEY_PROFILES, []) or []
    known = {str(p.get("wallet")) for p in all_scored if isinstance(p, dict)}
    for s in stats:
        if s.wallet not in known:
            all_scored.append(_stats_to_dict(s))
    await store.set(KEY_PROFILES, all_scored[:400])

    progress["budget"] = budget.report()
    progress["last_run_at"] = now.isoformat()
    if failures:
        progress["last_error"] = failures[0].get("why", "")[:160]
    await save_progress(store, progress)

    return {
        "ok": True,
        "scored": len(stats),
        "passed": passed,
        "total": total,
        "cursor": progress["cursor"],
        "complete": not needs_more_work(progress),
        "watchlist_size": len(merged),
        "failures": failures[:5],
        "budget": budget.report(),
        "progress": progress,
    }


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


def cost_of_position(cfg: Config) -> int:
    """Worst-case subrequests for one wallet's positions call.

    `clearinghouseState` is normally a single subrequest, but `http_json`
    retries, and a rotating sweep must assume the expensive case or it will
    overshoot the ceiling exactly when the venue is flaky.
    """
    return 1 + max(0, int(getattr(cfg, "discovery_fetch_retries", 2)))


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

    # ── rotate the sweep so every watched wallet gets polled ────────────────
    # `collect_positions` costs one subrequest per wallet, and an invocation may
    # only make 50. With 150 watched wallets the old code died at wallet 50 on
    # every single tick: the last hundred traders were never polled at all. The
    # radar looked busy and watched two thirds of nobody.
    #
    # Now each tick polls a rotating window sized to the budget, and persists
    # where it stopped. Every trader is covered every ceil(n/window) minutes.
    from .budget import SubrequestBudget

    sweep = SubrequestBudget(cfg.subrequest_limit, cfg.subrequest_reserve)
    window = max(1, min(len(watchlist), sweep.ceiling // cost_of_position(cfg)))
    offset = int(await store.get("scan_cursor", 0) or 0) % max(1, len(watchlist))
    rotated = list(watchlist[offset:]) + list(watchlist[:offset])
    batch = rotated[:window]
    await store.set("scan_cursor", (offset + len(batch)) % max(1, len(watchlist)))
    await store.set(
        "scan_rotation",
        {
            "offset": offset,
            "polled": len(batch),
            "watching": len(watchlist),
            "minutes_per_sweep": (len(watchlist) + window - 1) // window,
        },
    )

    positions = await collect_positions(venues, batch, cfg, limit=window)

    # Record what was actually observed, so `/positions` costs nothing later.
    # Writing the snapshot here rather than re-polling keeps one sweep per tick.
    try:
        await store.set("last_positions", [
            {
                "wallet": p.wallet,
                "source": p.source,
                "coin": p.coin,
                "side": p.side,
                "entry": p.entry,
                "size_usd": round(p.size_usd, 2),
                "leverage": p.leverage,
                "quality": p.quality,
                "quality_score": p.quality_score,
            }
            for p in positions[:400]
        ])
    except Exception:
        pass

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
    rejected: list[dict] = []
    for cluster in clusters:
        price = prices.get(cluster.coin)
        sig = build_signal(cluster, cfg, market_price=price, now=now)
        if sig is None:
            # Record *why* a cluster died. "No signal" with no explanation is the
            # single least trustworthy thing a monitoring system can report.
            conf = score_cluster(cluster, cfg)
            reason = (conf.reasons or ["لم تجتز شروط الجودة"])[0]
            rejected.append(
                {
                    "coin": cluster.coin,
                    "side": cluster.side,
                    "n_independent": cluster.n_members,
                    "score": conf.total,
                    "reason": reason,
                }
            )
            continue
        if sig.signal_id in seen:
            continue
        seen.add(sig.signal_id)
        emitted.append(sig)

    await store.set("seen_signal_ids", sorted(seen)[-500:])
    if rejected:
        try:
            await store.set("last_rejections", rejected[-40:])
        except Exception:
            pass
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
