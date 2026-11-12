"""Resumable discovery — scoring the leaderboard across many invocations.

The failure this replaces
-------------------------
``profile_watchlist`` fetched one leaderboard and then scored up to 300
candidates with ``venue.fills()`` inside a single invocation. Each fill call
costs up to 3 subrequests (2 retries). 1 + 300*3 = 901 against a 50-subrequest
ceiling. The platform killed the invocation; ``except Exception: continue``
turned that into an empty list; the dashboard reported "0 متداول" next to three
green platforms.

Raising the ceiling is not available (it is the free plan), and lowering the
candidate count alone is not enough either: 15 candidates is 45 subrequests
with no room for anything else, and a genuinely patient search needs hundreds.

So discovery becomes a **cursor**, not a loop:

    run N:  score candidates 0..k    -> persist cursor k, keep the shortlist
    run N+1: score candidates k..m   -> persist cursor m
    ...until the leaderboard is consumed, then start over on fresh data

Each invocation stays under the ceiling, progress is never lost, and — most
importantly — the *state of the search is inspectable*. `/prove` can say "scored
90 of 312 candidates, 4 passed, resume at 90" which is a measurement, not a
shrug.

Why the cursor is persisted per-run rather than per-candidate: a write per
candidate would be one Durable Object RPC per wallet, which is itself a cost and
a failure point. Per-run is enough to guarantee forward progress.
"""

from __future__ import annotations

from typing import Any, Sequence

from .budget import BudgetExhausted, SubrequestBudget, cost_of_fetch
from .config import Config
from .sources.base import RateLimited


# ── persisted shape ─────────────────────────────────────────────────────────
# One dict under `discovery_progress`. Deliberately flat and JSON-safe: it is
# written through the Durable Object, which stores JSON strings.
CURSOR_KEY = "discovery_cursor"
PROGRESS_KEY = "discovery_progress"


def empty_progress() -> dict[str, Any]:
    return {
        "cursor": 0,             # how many candidates have been scored
        "total": 0,              # how many candidates the last triage produced
        "scored": 0,             # cumulative across passes
        "passed": 0,             # cumulative wallets that reached PROVEN/ELITE
        "passes": 0,             # how many times the leaderboard was consumed
        "skipped": 0,            # candidates skipped for budget this run
        "last_leaderboard_at": "",
        "last_run_at": "",
        "last_error": "",
        "budget": {},
    }


def resume_index(progress: dict, total: int) -> int:
    """Where to resume, clamped to the current leaderboard size.

    A leaderboard that shrank (or a stale cursor from a previous deployment)
    must not point past the end and stall discovery forever — that is a silent
    zero of the same family as the one this module exists to fix.
    """
    try:
        cursor = int(progress.get("cursor", 0) or 0)
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0:
        cursor = 0
    if total and cursor >= total:
        return 0            # finished a full pass; start over
    return cursor


def plan_slice(
    total: int,
    cursor: int,
    budget: SubrequestBudget,
    cost_per_candidate: int,
) -> int:
    """How many candidates this invocation can afford.

    Returns at least 1 when anything is affordable, so progress is guaranteed
    on a pathological config where the per-candidate cost exceeds the ceiling —
    otherwise discovery would stall at cursor 0 forever, which is precisely the
    "monitoring but finding nothing" state the user was shown.
    """
    if total <= 0:
        return 0
    per = max(1, cost_per_candidate)
    affordable = budget.ceiling // per
    if affordable <= 0:
        # The ceiling cannot fit a single candidate at worst-case cost. Try one
        # anyway: the pessimistic cost includes retries that may not happen, and
        # a partial attempt that is recorded beats a guaranteed no-op.
        return min(1, total - cursor)
    return max(0, min(affordable, total - cursor))


async def score_slice(
    candidates: Sequence,
    start: int,
    count: int,
    venues: dict,
    cfg: Config,
    budget: SubrequestBudget,
) -> tuple[list, int, list[dict]]:
    """Score `count` candidates from `start`. Returns (stats, scored, failures).

    Failures are returned, not swallowed: a per-wallet fetch error is expected
    (a wallet may have no fills, or the venue may rate-limit), but the operator
    needs to know the difference between "this wallet is not good enough" and
    "we never managed to look at this wallet".
    """
    from .classifier import classify_trader, stats_from_fills

    scored: list = []
    failures: list[dict] = []
    done = 0

    for cand in candidates[start:start + count]:
        venue = venues.get(cand.source)
        if venue is None:
            failures.append({"wallet": _short(cand.wallet), "why": "no venue adapter"})
            done += 1
            continue

        # Charge before the call, using the worst case. If this raises we stop
        # the slice immediately and report it — we do not continue to the next
        # candidate hoping it is cheaper, because that is the pattern that
        # produced 901 subrequests.
        try:
            budget.spend(cost_of_fetch(retries=cfg.discovery_fetch_retries))
        except BudgetExhausted as exc:
            failures.append({"wallet": _short(cand.wallet), "why": f"budget: {exc}"})
            break

        try:
            raw_fills = await venue.fills(cand.wallet, limit=cfg.max_fills_per_profile)
        except RateLimited as exc:
            # The venue refused us. This is NOT "the wallet has no trades", and
            # crucially it is not this wallet's fault: the next one will be
            # refused too. Stop the slice and let the cursor resume here later,
            # rather than marching through the remaining candidates recording
            # 200 fabricated "no fills" verdicts.
            failures.append({"wallet": _short(cand.wallet), "why": f"rate limited: {exc}"[:120]})
            break
        except Exception as exc:
            failures.append(
                {"wallet": _short(cand.wallet), "why": f"{type(exc).__name__}: {exc}"[:120]}
            )
            done += 1
            continue

        try:
            fills = venue.parse_fills(raw_fills)
        except Exception as exc:
            failures.append(
                {"wallet": _short(cand.wallet), "why": f"parse: {type(exc).__name__}"}
            )
            done += 1
            continue

        if not fills:
            failures.append({"wallet": _short(cand.wallet), "why": "no fills"})
            done += 1
            continue

        try:
            stats = stats_from_fills(
                cand.wallet,
                fills,
                account_value_usd=cand.account_value,
                source=cand.source,
                data_confirmed=cand.source == "hyperliquid",
            )
            classify_trader(stats)
        except Exception as exc:
            failures.append(
                {"wallet": _short(cand.wallet), "why": f"score: {type(exc).__name__}"}
            )
            done += 1
            continue

        scored.append(stats)
        done += 1

    return scored, done, failures


def _short(wallet: str) -> str:
    wallet = str(wallet or "")
    return wallet[:10] + "…" if len(wallet) > 10 else wallet


async def load_progress(store) -> dict:
    """Read the persisted cursor, tolerating a missing or corrupt record."""
    raw = await store.get(PROGRESS_KEY, None)
    if not isinstance(raw, dict):
        return empty_progress()
    merged = empty_progress()
    merged.update({k: v for k, v in raw.items() if k in merged})
    return merged


async def save_progress(store, progress: dict) -> None:
    await store.set(PROGRESS_KEY, progress)


def describe(progress: dict) -> str:
    """One line an operator can read. Used by `/prove` and the dashboard."""
    if not progress or not progress.get("total"):
        return "لم تبدأ عملية انتقاء المتداولين بعد"

    cursor = int(progress.get("cursor", 0) or 0)
    total = int(progress.get("total", 0) or 0)
    passed = int(progress.get("passed", 0) or 0)
    scored = int(progress.get("scored", 0) or 0)
    passes = int(progress.get("passes", 0) or 0)

    pct = round(100.0 * cursor / total) if total else 0
    line = f"فُحص {scored} محفظة · نجح {passed} · الموقع {cursor}/{total} ({pct}%)"
    if passes:
        line += f" · {passes} دورة كاملة"
    if progress.get("skipped"):
        line += f" · تخطّي {progress['skipped']} بسبب الميزانية"
    return line


def needs_more_work(progress: dict) -> bool:
    """True while a pass is unfinished — i.e. the radar should keep going."""
    total = int(progress.get("total", 0) or 0)
    cursor = int(progress.get("cursor", 0) or 0)
    return total > 0 and cursor < total


def merge_watchlist(existing: list, fresh: list, cfg: Config) -> list:
    """Union the persistent watchlist with newly scored wallets.

    This is what makes resumable discovery useful: a pass that scores 15
    candidates must not throw away the 8 good wallets found on the previous
    run. Existing entries win on ties by wallet address.
    """
    from .discovery import build_watchlist

    by_wallet: dict[str, Any] = {}
    for entry in list(existing or []) + list(fresh or []):
        wallet = str((entry.get("wallet") if isinstance(entry, dict) else getattr(entry, "wallet", "")) or "")
        if wallet:
            by_wallet[wallet] = entry

    combined = list(by_wallet.values())
    # `build_watchlist` sorts by score and applies the grade gate, so it accepts
    # the mixed dict/TraderStats list only after normalisation. Normalise here.
    normalised = [_as_stats(item) for item in combined]
    return [_as_dict(s) for s in build_watchlist([s for s in normalised if s is not None], cfg)]


def _as_stats(item):
    """Rebuild a `TraderStats` from its dict form.

    The watchlist is persisted as JSON, so a resumed run reads dicts while a
    fresh run produces objects. Everything downstream expects objects, so the
    conversion lives here rather than being repeated at each call site.

    Field names are taken from `cta.models.TraderStats` exactly. Guessing them
    would silently zero out `score`/`grade` and turn every resumed wallet into a
    REJECT — the same class of quiet-zero bug this module exists to remove.
    """
    from .models import TraderStats

    if isinstance(item, TraderStats):
        return item
    if not isinstance(item, dict):
        return None
    try:
        return TraderStats(
            wallet=str(item.get("wallet", "")),
            n_trades=int(item.get("n_trades", 0) or 0),
            win_rate=float(item.get("win_rate", 0.0) or 0.0),
            profit_factor=float(item.get("profit_factor", 0.0) or 0.0),
            max_drawdown=float(item.get("max_drawdown", 0.0) or 0.0),
            sharpe=float(item.get("sharpe", 0.0) or 0.0),
            consistency=float(item.get("consistency", 0.0) or 0.0),
            concentration=float(item.get("concentration", 1.0) or 1.0),
            active_days=int(item.get("active_days", 0) or 0),
            avg_hold_hours=float(item.get("avg_hold_hours", 0.0) or 0.0),
            max_leverage=float(item.get("max_leverage", 0.0) or 0.0),
            account_value_usd=float(item.get("account_value_usd", 0.0) or 0.0),
            source=str(item.get("source", "hyperliquid")),
            data_confirmed=bool(item.get("data_confirmed", False)),
            score=int(item.get("score", 0) or 0),
            grade=str(item.get("grade", "REJECT")),
            reasons=list(item.get("reasons", []) or []),
        )
    except Exception:
        return None


def _as_dict(stats) -> dict:
    from .engine import _stats_to_dict

    return _stats_to_dict(stats)
