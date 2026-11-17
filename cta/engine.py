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
from .models import Cluster, Order, Position, Signal, TraderStats
from .orders import attach_protective, net_orders
from .pending import detect_pending
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


def _venue_confirms_data(venue) -> bool:
    """Is this venue's trade history authoritative?

    This used to be written as `cand.source == "hyperliquid"`, which meant every
    other source — OKX, dYdX, GMX, and anything added later — had its scores cut
    by 15% and was permanently barred from ELITE, no matter how good the trader.
    A hard-coded source name is not a statement about data quality; the venue
    knows whether its own fills are confirmed, so we ask it.

    The gate still exists, because it is a real safeguard: an unverified feed
    must not be able to manufacture an ELITE. It is now opt-out per venue
    (`data_confirmed = False`) instead of opt-in for one.
    """
    return bool(getattr(venue, "data_confirmed", True))


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
            data_confirmed=_venue_confirms_data(venue),
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

    # ── 1. assemble candidates: seeds first, leaderboard opportunistically ──
    #
    # The leaderboard is a ~39 MB file of ~47,000 wallets. On the Workers free
    # plan an invocation gets 10 ms of CPU, which cannot deserialise it. The
    # failure is silent and lands between the health write and the progress
    # write, so the dashboard showed green platforms, climbing cycles, and a
    # search that had never started.
    #
    # So the leaderboard is now *optional enrichment*. Seeds are cheap (one
    # subrequest per wallet to poll) and give the radar real traders to watch on
    # the first tick, whether or not the big file ever loads.
    from .budget import BudgetExhausted
    from .seeds import merge_seeds, seeds_from_config

    hl = venues.get("hyperliquid")

    # (a) seeds: configured, then previously persisted, then the built-in list
    try:
        stored_seeds = await store.get("seed_wallets", []) or []
    except Exception:
        stored_seeds = []
    seeds = merge_seeds(stored_seeds, seeds_from_config(cfg))
    progress["seeds"] = len(seeds)

    from .discovery import Candidate

    candidates: list = [
        Candidate(wallet=w, source="hyperliquid", reasons=["seed wallet"]) for w in seeds
    ]

    # (b) leaderboard: attempted only when the budget can spare it, and never
    # fatal. A failure here is recorded and the seeds carry the cycle.
    leaderboard_rows = 0
    if hl is not None and budget.affordable(1):
        budget.spend(1)
        try:
            rows = await hl.leaderboard()
            leaderboard_rows = len(rows or [])
            parsed = [hl.parse_leaderboard_row(r) for r in rows or []]
            candidates += select_candidates(parsed, cfg, "hyperliquid")
        except BudgetExhausted:
            raise
        except Exception as exc:
            # Includes the memory/CPU kill: the point is that it must not stop
            # discovery from having work to do.
            progress["last_error"] = f"leaderboard: {type(exc).__name__}: {exc}"[:160]
    elif hl is not None:
        progress["last_error"] = "leaderboard skipped: budget reserved for polling"
    else:
        progress["last_error"] = "no hyperliquid adapter"

    progress["leaderboard_rows"] = leaderboard_rows
    try:
        await store.set("seed_wallets", seeds)
    except Exception:
        pass

    # (b2) OKX: a ranked, server-side-filtered board of lead traders. Unlike
    # Hyperliquid's 39 MB dump this arrives in a handful of small pages, so it
    # is the one leaderboard that reliably works inside a tight budget. Its rows
    # carry no window PnL, so discovery treats them as "no window data" and
    # accepts them on account value — the real verdict comes from the closed
    # subposition history, which is the number we actually trust.
    okx = venues.get("okx")
    if okx is not None:
        try:
            okx_rows = await okx.leaderboard()
            progress["okx_rows"] = len(okx_rows or [])
            parsed = [okx.parse_leaderboard_row(r) for r in okx_rows or []]
            candidates += select_candidates(parsed, cfg, "okx")
        except BudgetExhausted:
            raise
        except Exception as exc:
            progress["last_error"] = f"okx leaderboard: {type(exc).__name__}: {exc}"[:160]

    # (c) every other venue that can name traders contributes them directly.
    #
    # This list used to be the literal string "dydx, gmx". That hard-coding is
    # why discovery ignored any source added later: a new venue could be built,
    # enabled, and reported healthy, and still be invisible to the search
    # because it was not in this tuple. Discovery now asks the venues what they
    # have instead of keeping its own list of names.
    for name, venue in venues.items():
        if name in ("hyperliquid", "okx"):
            continue  # already handled above, with their own parsing
        if not hasattr(venue, "leaderboard"):
            continue
        try:
            venue_seeds = await venue.leaderboard()
        except BudgetExhausted:
            raise
        except Exception as exc:
            progress["last_error"] = f"{name} leaderboard: {type(exc).__name__}: {exc}"[:160]
            continue
        for row in venue_seeds or []:
            if not isinstance(row, dict):
                continue
            wallet = (row.get("wallet") or "").lower()
            if not wallet:
                continue
            # The account value MUST travel with the candidate.
            #
            # It used to be dropped here, so every wallet from these venues was
            # scored with `account_value = 0`. Drawdown is measured against
            # account value, so a 0 denominator produced an 85% "catastrophic"
            # drawdown for *every* trader — dYdX and GMX seeds were rejected
            # wholesale, and two of the four sources contributed nothing to the
            # watchlist while reporting themselves healthy.
            candidates.append(
                Candidate(
                    wallet=wallet,
                    source=name,
                    account_value=float(row.get("account_value") or 0.0),
                    reasons=["seed wallet"],
                )
            )

    # ── 2. decide how much of it we can afford ──────────────────────────────
    total = len(candidates)
    progress["total"] = total
    start = resume_index(progress, total)
    from .budget import cost_of_fetch

    per_candidate = cost_of_fetch(cfg.discovery_fetch_retries)
    count = cfg.candidate_page_size or plan_slice(total, start, budget, per_candidate)
    count = min(count, total - start) if total else 0

    # A leaderboard we could not read is not "zero candidates found". But with
    # seeds present there is still work to do, so the failure is reported
    # alongside the progress rather than replacing it.
    if not total and progress.get("last_error") and not progress.get("seeds"):
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
        if not isinstance(raw, list):
            # A venue that answers `None` — a transient null, an API change, a
            # proxy's error page parsed as JSON — used to raise straight through
            # `parse_positions` and kill the whole cycle. One bad venue took the
            # radar down for every other source too.
            continue
        try:
            parsed = venue.parse_positions(wallet, raw)
        except Exception:
            continue
        grade, score = quality_by_wallet.get(wallet, ("WATCH", 0))
        for p in parsed:
            p.quality = grade
            p.quality_score = int(score)
            positions.append(p)

    return merge_members(dedupe_positions(positions))


async def market_prices(venues: dict) -> dict[str, float]:
    """A reference price for as many coins as any venue can name.

    This used to ask Hyperliquid and nobody else. That is fine right up until
    Hyperliquid is unreachable — or deliberately disabled — at which point the
    slippage gate silently stops working, the reachability gate for resting
    orders stops working, and `plan.py` quietly loses the market price it needs
    to decide whether an entry is still live. The radar keeps emitting signals;
    they are just no longer checked against reality.

    Every venue that can price a coin contributes, and the first one to answer
    wins so a slow venue cannot block the cycle. Coins only Hyperliquid lists
    still work when it is up.
    """
    merged: dict[str, float] = {}
    for _name, venue in venues.items():
        if not hasattr(venue, "prices"):
            continue
        try:
            quote = await venue.prices()
        except Exception:
            continue
        if not isinstance(quote, dict):
            # A list, a string, a null: not a price book. Skipping one venue's
            # quotes is a degraded cycle; crashing is a dead radar.
            continue
        for coin, px in (quote or {}).items():
            try:
                value = float(px)
            except (TypeError, ValueError):
                continue
            if value > 0 and coin not in merged:
                merged[coin] = value
    return merged


async def collect_orders(
    venues: dict,
    watchlist: Sequence[dict],
    cfg: Optional[Config] = None,
    limit: Optional[int] = None,
    prices: Optional[dict[str, float]] = None,
) -> list[Order]:
    """Fetch resting orders for a rotating window of watched wallets.

    Two things make this the most expensive call in the system:

    * Hyperliquid charges `frontendOpenOrders` the same ~20 weight as `userFills`
      — ten times the cost of polling a position.
    * Only some venues publish orders at all. OKX's public copy-trading
      endpoints expose no resting orders whatsoever, and pretending otherwise
      would be the same silent-zero bug this project keeps paying for.

    So the sweep is bounded (`orders_max_wallets`) and rotates independently of
    the position sweep, so every wallet's orders are still seen regularly
    without spending the whole budget on them in one tick.
    """
    cfg = cfg or Config()
    if not getattr(cfg, "orders_enabled", True):
        return []
    limit = limit or int(getattr(cfg, "orders_max_wallets", 40))

    quality_by_wallet = {
        w["wallet"]: (w.get("grade", "WATCH"), w.get("score", 0)) for w in watchlist
    }
    out: list[Order] = []
    for entry in watchlist[:limit]:
        wallet = entry.get("wallet")
        source = entry.get("source", "hyperliquid")
        venue = venues.get(source)
        if venue is None or not wallet:
            continue
        # A venue with no order endpoint is skipped, not simulated.
        if not hasattr(venue, "open_orders") or not hasattr(venue, "parse_open_orders"):
            continue
        try:
            raw = await venue.open_orders(wallet)
        except Exception:
            continue
        if not isinstance(raw, list):
            continue  # see `collect_positions`: one bad venue must not end the cycle
        try:
            parsed = venue.parse_open_orders(wallet, raw, prices=prices)
        except Exception:
            continue
        grade, score = quality_by_wallet.get(wallet, ("WATCH", 0))
        for o in parsed:
            o.quality = grade
            o.quality_score = int(score)
            out.append(o)
    return out


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
    kind: str = "filled",
    opposition: float = 0.0,
) -> Optional[Signal]:
    """Turn a passing cluster into a fully explainable Signal (or None).

    `opposition` is the weight of independent traders who clustered on the other
    side of this coin. See `score_cluster` for why it is part of the score.
    """
    cfg = cfg or Config()
    now = now or datetime.now(UTC)

    conf = score_cluster(cluster, cfg, opposition=opposition)
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
        kind=kind,
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
        prices = await market_prices(venues)

    # ── resting orders: their stops, their targets, and their intent ────────
    # One fetch serves two purposes, which is why it happens before scoring:
    #   1. a trader's own TP/SL levels are attached to their position, so the
    #      plan quotes *their* stop instead of one we computed from ATR;
    #   2. resting entry orders are clustered separately — those are trades we
    #      can still enter at the traders' price instead of chasing them.
    orders: list[Order] = []
    if getattr(cfg, "orders_enabled", True):
        order_window = max(
            1, min(len(watchlist), int(getattr(cfg, "orders_max_wallets", 40)))
        )
        orders = await collect_orders(venues, batch, cfg, limit=order_window, prices=prices)
        if orders:
            attach_protective(positions, orders)

    filled_clusters = detect_convergence(positions, cfg, now=now)

    pending_clusters: list = []
    if orders:
        # The *whole* order list goes in, not just the entry orders. Passing
        # only entries meant the traders' own stop and target orders were
        # filtered out before the pending cluster was built, so the rarest and
        # most actionable signal was also the only one with no risk levels on
        # it. `detect_pending` picks the entries it needs and attaches the
        # protective levels to them.
        pending_clusters = detect_pending(
            net_orders([o for o in orders if o.kind == "entry"])
            + [o for o in orders if o.kind in ("tp", "sl")],
            cfg,
            now=now,
            prices=prices,
            max_age_min=getattr(cfg, "orders_max_age_min", 1440.0),
        )

    # Pending consensus is rarer and more actionable than filled consensus: the
    # entry is still reachable. It is reported first for exactly that reason.
    clusters = list(pending_clusters) + [
        c for c in filled_clusters
        if (c.coin, c.side) not in {(p.coin, p.side) for p in pending_clusters}
    ]
    pending_keys = {(c.coin, c.side, round(c.entry_center, 6)) for c in pending_clusters}

    # How contested is each call? Measured across every cluster found this
    # cycle — including the ones that will not be emitted — because the
    # operator's question is not "do four traders agree" but "does anyone
    # disagree as strongly".
    opposition_by_key: dict[tuple[str, str], float] = {}
    for cluster in clusters:
        other = "SHORT" if cluster.side == "LONG" else "LONG"
        opposition_by_key.setdefault((cluster.coin, other), 0.0)
        opposition_by_key[(cluster.coin, other)] += float(cluster.n_members)

    seen = set(await store.get("seen_signal_ids", []) or [])
    emitted: list[Signal] = []
    rejected: list[dict] = []
    for cluster in clusters:
        price = prices.get(cluster.coin)
        kind = (
            "pending"
            if (cluster.coin, cluster.side, round(cluster.entry_center, 6)) in pending_keys
            else "filled"
        )
        against = opposition_by_key.get((cluster.coin, cluster.side), 0.0)
        sig = build_signal(cluster, cfg, market_price=price, now=now, kind=kind,
                           opposition=against)
        if sig is None:
            # Record *why* a cluster died. "No signal" with no explanation is the
            # single least trustworthy thing a monitoring system can report.
            conf = score_cluster(cluster, cfg, opposition=against)
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

    # Keep searching while the pass is unfinished, not only while the watchlist
    # is empty.
    #
    # `if not watchlist` looks reasonable and is wrong: the moment twelve wallets
    # passed, the search declared itself done and froze at 12 of 30. A watchlist
    # that stops growing at the first success is not a search, it is a sample.
    from .discovery_resume import needs_more_work as _needs_more_work

    _progress: dict = {}
    try:
        _progress = await store.get("discovery_progress", {}) or {}
    except Exception:
        _progress = {}

    if not watchlist or _needs_more_work(_progress):
        # This MUST be the resumable profiler.
        #
        # It used to call `profile_watchlist`, the un-budgeted original that
        # fetched the ~39 MB / 47k-row leaderboard inline. That cannot finish
        # inside the free plan's 10 ms CPU budget, so it died -- and because it
        # died inside `run_cycle` rather than in `run_profile_async`, the
        # discovery cursor was never written at all. `/health` therefore reported
        # "لم تبدأ عملية انتقاء المتداولين بعد" next to a cycle counter that
        # climbed forever (6, 13, 30, 85 in the operator's screenshots).
        #
        # The two paths had to agree. There is now exactly one way to discover,
        # and it is the one that budgets, checkpoints, and resumes.
        from .discovery_resume import describe

        profile = await profile_watchlist_resumable(venues, store, cfg, now=now)
        profiled = True
        watchlist = await store.get(KEY_WATCHLIST, []) or []
        _ = describe(profile.get("progress", {}))

    signals, clusters = await scan(venues, store, cfg, now=now, watchlist=watchlist)

    return {
        "profiled": profiled,
        "watchlist_size": len(watchlist),
        "clusters": len(clusters),
        "signals": len(signals),
        "signal_objects": signals,
    }
