"""Did we actually look? Measuring the monitor, not guessing about it.

The failure this prevents
-------------------------
A dashboard showing `0 signals · 0 traders · 🟢 live` is indistinguishable from
a healthy radar watching a quiet market. Those two states are *opposite* and
look identical. If the code silently fails — an unreachable venue, a broken
binding, a mis-parsed leaderboard — the page reports calm, and the operator
waits days for a signal that was never going to arrive.

So the system now records evidence that it looked, separately from what it
found:

* `source_health`   — per venue, did the last call succeed
* `last_cycle_at`   — when a cycle last completed
* `probe`           — a small, fixed set of facts fetched on demand: a price, a
                      leaderboard page, a wallet. If these fail, the radar is
                      blind and it says so instead of showing zeros.

`verdict()` combines them into one of three states that the dashboard renders
directly:

    BLIND     nothing reaches the platforms  -> "الأرقام ليست صفراً، نحن لا نرى"
    WATCHING  data flows, no convergence yet -> "نراقب ولا شيء بعد"
    SIGNAL    a signal exists                -> normal
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .models import Fill, Position

UTC = timezone.utc

# How stale a cycle may be before "watching" is a lie.
STALE_MINUTES = 15


def verdict(
    *,
    source_health: dict,
    counters: dict,
    last_cycle: Optional[str],
    signals: int = 0,
    now: Optional[datetime] = None,
) -> dict:
    """One honest summary of whether the radar can see anything.

    `reachable` is about connectivity, not results. A venue that answers with
    an empty leaderboard is healthy; a venue that times out is blind. Reporting
    them as the same thing is what makes a broken radar look like a quiet one.
    """
    now = now or datetime.now(UTC)
    health = {k: bool(v) for k, v in (source_health or {}).items()}
    reachable = any(health.values())
    attempted = bool(health)

    cycles = int((counters or {}).get("cycles", 0) or 0)
    completed = int((counters or {}).get("completed", 0) or 0)
    attempts = int((counters or {}).get("attempts", 0) or 0)
    age_minutes = None
    if last_cycle:
        age_minutes = _age_minutes(last_cycle, now)

    if not attempted:
        state = "BLIND"
        reason = "لم تُنفَّذ أي دورة بعد — النظام لم يحاول الوصول للمنصات."
    elif not reachable:
        state = "BLIND"
        reason = "كل المنصات فشلت — النظام يعمل لكنه لا يرى أي بيانات."
    elif attempts and not completed and age_minutes is None:
        # Cycles are starting but none has ever finished. Before this branch,
        # this state was reported as BLIND with the reason "no cycle yet", which
        # was both wrong and unhelpful: the system *was* running, and dying
        # partway. Naming it separately is what turns a mystery into a lead.
        state = "BLIND"
        reason = (
            f"بدأت {attempts} دورة ولم تكتمل ولا واحدة — "
            "النظام يعمل ثم يتوقف في منتصف الدورة. هذه مشكلة في البرنامج."
        )
    elif age_minutes is not None and age_minutes > STALE_MINUTES:
        state = "BLIND"
        reason = f"آخر دورة ناجحة قبل {age_minutes:.0f} دقيقة — المراقبة متوقفة."
    elif signals:
        state = "SIGNAL"
        reason = "توجد إشارة مُخزّنة."
    else:
        state = "WATCHING"
        reason = "البيانات تصل، ولم يتحقق اتفاق بعد. هذا هو الوضع الطبيعي."

    return {
        "state": state,
        "reason": reason,
        "reachable": reachable,
        "attempted": attempted,
        "cycles": cycles,
        "attempts": attempts,
        "completed": completed,
        "age_minutes": age_minutes,
        "healthy_sources": [k for k, v in health.items() if v],
        "blind_sources": [k for k, v in health.items() if not v],
        "checked_at": now.isoformat(),
    }


def _age_minutes(stamp: str, now: datetime) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (now - parsed).total_seconds() / 60.0)


async def probe_async(venues: dict, store: Any) -> dict:
    """Ask every venue for one cheap real fact and record the answer.

    This is the difference between "no signals" and "no eyes". Each probe is a
    single network call whose success proves the pipeline end to end: fetch ->
    parse -> value. A price is ideal — small, always present, easy to compare
    between runs.
    """
    results: dict[str, Any] = {}
    checked = datetime.now(UTC).isoformat()

    for name, venue in (venues or {}).items():
        entry: dict[str, Any] = {"ok": False}
        try:
            prices = await venue.prices()
            sample = {k: v for k, v in list(prices.items())[:3]}
            entry = {
                "ok": bool(prices),
                "assets": len(prices),
                "sample": sample,
            }
        except Exception as exc:
            entry = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        results[name] = entry

    payload = {
        "checked_at": checked,
        "venues": results,
        "any_ok": any(v.get("ok") for v in results.values()),
    }
    try:
        await store.set("probe", payload)
    except Exception:
        pass
    return payload


async def self_check_async(env, fetch) -> dict:
    """Prove that a signal *could* be detected, right now, without waiting.

    Waiting days to discover the detector is broken is the worst outcome in
    this system — it costs the most time and produces the least information.
    Instead, inject a synthetic two-trader convergence through the real engine
    and confirm it produces a signal that clears the thresholds. It runs
    entirely in memory, touches no real state, and sends nothing.

    If this passes, the detection path is wired correctly and silence means
    silence. If it fails, the failure is here and now, not after two days.
    """
    from .engine import run_cycle
    from .config import Config
    from .state import MemoryStore

    cfg = Config.from_env(env)
    store = MemoryStore()

    venue = _SyntheticVenue()
    try:
        # Register under "hyperliquid": `profile_watchlist` reads the leaderboard
        # from that key specifically, so a venue named anything else is silently
        # skipped and the cycle reports zero — the same silent-zero class of bug
        # this check exists to catch.
        report = await run_cycle({"hyperliquid": venue}, store, cfg, now=_now())
        signals = report.get("signal_objects", []) or []
        return {
            "ok": bool(signals),
            "signals": len(signals),
            "clusters": report.get("clusters", 0),
            "watchlist": len(report.get("watchlist", []) or []),
            "sample": _signal_summary(signals[0]) if signals else None,
            "note": (
                "إشارة اصطناعية عبر المحرّك الحقيقي — لا تُرسَل ولا تُخزَّن."
                if signals
                else "المحرّك لم يولّد إشارة من مدخلات معروفة النتيجة — "
                     "هذا يعني أن الكشف معطّل، وليس أن السوق هادئ."
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "note": "المحرّك انفجر على مدخلات معروفة النتيجة.",
        }


async def discovery_proof_async(store) -> dict:
    """Report the live state of the trader search, with numbers.

    `/prove` used to prove only that the *detector* works on synthetic input.
    That answered "would a signal be seen?" but not the operator's actual
    question, which was "are you watching anyone?". Both matter, and they fail
    independently: a working detector over an empty watchlist watches nothing.

    Every field here is read from persisted state, so it describes what really
    happened rather than what should have happened.
    """
    from .discovery_resume import describe, load_progress, needs_more_work

    try:
        progress = await load_progress(store)
    except Exception as exc:
        return {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}"[:160],
            "note": "تعذّرت قراءة حالة البحث.",
        }

    watchlist = []
    try:
        watchlist = await store.get("watchlist", []) or []
    except Exception:
        watchlist = []

    total = int(progress.get("total", 0) or 0)
    cursor = int(progress.get("cursor", 0) or 0)
    return {
        "available": True,
        "line": describe(progress),
        "watching": len(watchlist),
        "candidates": total,
        "cursor": cursor,
        "scored": int(progress.get("scored", 0) or 0),
        "passed": int(progress.get("passed", 0) or 0),
        "passes": int(progress.get("passes", 0) or 0),
        "searching": needs_more_work(progress),
        "last_run_at": progress.get("last_run_at", ""),
        "last_error": progress.get("last_error", ""),
        "verdict": _discovery_verdict(progress, len(watchlist)),
    }


def _discovery_verdict(progress: dict, watching: int) -> str:
    """One honest sentence about whether the radar is actually watching."""
    if watching > 0:
        return f"يراقب {watching} متداولاً"
    if not progress.get("total"):
        return "لم يبدأ البحث بعد"
    if progress.get("last_error") and not progress.get("scored"):
        return f"البحث متعثّر: {str(progress['last_error'])[:80]}"
    return f"يبحث الآن — فُحص {progress.get('scored', 0)} محفظة"


def _signal_summary(signal) -> dict:
    """Shape the proof so a person can read it, not just a test.

    The point of the check is to show *what would have been sent*. Dumping the
    raw dict hides the trade plan, which is the part that matters.
    """
    plan = getattr(signal, "plan", None)
    return {
        "coin": signal.coin,
        "side": signal.side,
        "score": signal.score,
        "level": signal.level,
        "n_independent": signal.n_independent,
        "sources": list(signal.sources),
        "entry": getattr(plan, "entry", None) or signal.entry_center,
        "stop": getattr(plan, "stop", None),
        "target": getattr(plan, "target", None),
        "rr": getattr(plan, "risk_reward", None),
        "breakdown": dict(getattr(signal, "breakdown", {}) or {}),
        "reasons": list(getattr(signal, "reasons", []) or [])[:4],
    }


def _now() -> datetime:
    return datetime.now(UTC)


class _SyntheticVenue:
    """Two independent proven traders, same coin, same direction, tight spread.

    It implements the full adapter surface (`leaderboard`, `parse_leaderboard_row`,
    `positions`, `fills`, `parse_fills`, `prices`, `health`) because the engine
    calls all of it. An earlier version supplied raw dicts only, and the engine
    silently produced nothing — which is exactly the class of bug this check
    exists to catch: a detector that fails looks identical to a market that is
    quiet.
    """

    name = "synthetic"

    def __init__(self) -> None:
        # Two clearly elite histories: high win rate, strong profit factor, long
        # track record. The fixture must clear every gate — a fixture that only
        # *almost* clears them produces the same "no signal" as a broken engine,
        # which is the ambiguity this check exists to remove.
        self._fills: dict[str, list[Fill]] = {
            # Elite histories: enough trades, ~4 months active, high win rate,
            # strong profit factor, low drawdown. They must clear *every* gate,
            # because a fixture that only almost clears them produces exactly
            # the same "no signal" as a genuinely broken engine.
            "0xa": _make_fills("0xa", 240, win_ratio=0.85, win=1500.0, loss=380.0),
            "0xb": _make_fills("0xb", 240, win_ratio=0.82, win=1450.0, loss=400.0),
        }
        self._values = {"0xa": 1_400_000.0, "0xb": 1_100_000.0}

    async def health(self) -> bool:
        return True

    async def leaderboard(self) -> list[dict]:
        return [{"wallet": w} for w in self._fills]

    def parse_leaderboard_row(self, row: dict) -> dict:
        return {"wallet": str(row.get("wallet", "")), "account_value": self._values.get(row.get("wallet"))}

    async def positions(self, wallet: str) -> list[dict]:
        size = 120_000.0 if wallet == "0xa" else 80_000.0
        return [
            {
                "wallet": wallet,
                "coin": "BTC",
                "side": "LONG",
                "size": size,
                "entry_price": 65000.0,
                "leverage": 3.0,
                "opened_at": _now().isoformat(),
                "signed_size": size / 65000.0,
            }
        ]

    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]:
        # The engine calls `venue.fills(wallet)` and then `venue.parse_fills(raw)`
        # with no wallet argument, so the identity has to travel inside the rows.
        return [{"wallet": wallet, "n": len(self._fills.get(wallet, []))}]

    def parse_positions(self, wallet: str, raw_positions) -> list[Position]:
        out: list[Position] = []
        for row in raw_positions or []:
            size_usd = float(row.get("size", 0.0))
            entry = float(row.get("entry_price", 0.0)) or 1.0
            out.append(
                Position(
                    wallet=str(row.get("wallet", wallet)),
                    source=self.name,
                    coin=str(row.get("coin", "")),
                    signed_size=size_usd / entry,
                    entry=entry,
                    size_usd=size_usd,
                    leverage=float(row.get("leverage", 1.0)),
                    opened_at=_parse_dt(row.get("opened_at")),
                )
            )
        return out

    def parse_fills(self, rows, wallet: str = "") -> list[Fill]:
        key = wallet or (rows[0].get("wallet") if rows else "")
        return list(self._fills.get(key, []))

    async def prices(self) -> dict[str, float]:
        return {"BTC": 65000.0}


def _parse_dt(value):
    """`Position.opened_at` is a datetime; a string silently breaks age checks."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _make_fills(wallet: str, count: int, win_ratio: float,
                win: float = 900.0, loss: float = 520.0) -> list[Fill]:
    """A plausible history: enough trades, positive expectancy, low drawdown."""
    now_ms = int(_now().timestamp() * 1000)
    day_ms = 86_400_000
    out: list[Fill] = []
    for i in range(count):
        wins = i % 100 < int(win_ratio * 100)
        pnl = win if wins else -loss
        out.append(
            Fill(
                # One fill per day across ~4 months: the classifier requires 90
                # active days, so a compressed history is REJECTed on "short
                # activity" before any quality metric is examined.
                time_ms=now_ms - i * day_ms,
                coin="BTC" if i % 2 == 0 else "ETH",
                side="BUY" if wins else "SELL",
                price=65000.0,
                size=0.05,
                closed_pnl=pnl,
                fee=2.0,
                raw={"wallet": wallet},
            )
        )
    return out
