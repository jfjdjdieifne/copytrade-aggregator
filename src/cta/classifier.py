"""Trader quality — the function that creates most of the system's value.

Turning 47k leaderboard wallets into ~150 proven traders. Everything here is
computed from real fills, never from a leaderboard's aggregated headline numbers,
because headline ROI rewards leverage and luck.

Seven criteria, 100 points:
    trades 20 · activity 15 · profit factor 20 · drawdown 15
    consistency 15 · concentration 10 · leverage 5
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from math import sqrt
from typing import Iterable, Sequence

from .models import Fill, TraderStats

UTC = timezone.utc
WEEK_MS = 7 * 24 * 3600 * 1000


# ── curve reconstruction ─────────────────────────────────────────────────────
@dataclass(slots=True)
class EquityPoint:
    time_ms: int
    equity: float
    peak: float
    drawdown: float


def equity_from_fills(
    fills: Sequence[Fill], start_equity: float = 0.0, base: float = 0.0
) -> list[EquityPoint]:
    """Rebuild the equity curve by walking fills forward in time.

    Only realised PnL and fees move equity; unrealised swings are ignored so the
    curve answers one honest question: *what did this wallet actually bank?*

    Drawdown needs a denominator. When the starting capital is known (leaderboard
    `accountValue`) it is the right one. When it is unknown — seed wallets on
    venues without portfolios — we fall back to the peak banked PnL, which is a
    defined but conservative proxy; callers mark such data unconfirmed.
    """
    ordered = sorted(fills, key=lambda f: f.time_ms)
    curve: list[EquityPoint] = []
    equity = start_equity
    peak = start_equity
    floor = max(base, 1.0)

    for f in ordered:
        equity += f.closed_pnl - f.fee
        peak = max(peak, equity)
        denominator = max(peak, floor)
        dd = (peak - equity) / denominator if denominator > 0 else 0.0
        curve.append(EquityPoint(f.time_ms, equity, peak, min(1.0, max(0.0, dd))))

    return curve


def _max_drawdown(curve: Sequence[EquityPoint]) -> float:
    return max((p.drawdown for p in curve), default=0.0)


def _week_buckets(fills: Sequence[Fill]) -> dict[int, float]:
    buckets: dict[int, float] = {}
    for f in fills:
        wk = f.time_ms // WEEK_MS
        buckets[wk] = buckets.get(wk, 0.0) + f.closed_pnl - f.fee
    return buckets


def _consistency(fills: Sequence[Fill]) -> float:
    buckets = _week_buckets(fills)
    if not buckets:
        return 0.0
    return sum(1 for v in buckets.values() if v > 0) / len(buckets)


def _concentration(fills: Sequence[Fill]) -> float:
    wins = [f.closed_pnl for f in fills if f.closed_pnl > 0]
    total = sum(wins)
    if total <= 0:
        return 1.0
    return max(wins) / total


def _sharpe(fills: Sequence[Fill]) -> float:
    """Annualised Sharpe from weekly PnL buckets (needs >= 3 weeks)."""
    buckets = list(_week_buckets(fills).values())
    if len(buckets) < 3:
        return 0.0
    mean = sum(buckets) / len(buckets)
    var = sum((b - mean) ** 2 for b in buckets) / (len(buckets) - 1)
    sd = sqrt(var)
    if sd <= 0:
        return 0.0
    return mean / sd * sqrt(52)


# ── stats ────────────────────────────────────────────────────────────────────
def stats_from_fills(
    wallet: str,
    fills: Sequence[Fill],
    account_value_usd: float = 0.0,
    source: str = "hyperliquid",
    data_confirmed: bool = True,
    avg_hold_hours: float = 0.0,
    max_leverage: float = 0.0,
) -> TraderStats:
    """Derive every metric the classifier needs from raw fills."""
    closed = [f for f in fills if f.closed_pnl != 0.0]
    n = len(closed)
    if n == 0:
        return TraderStats(
            wallet=wallet,
            source=source,
            account_value_usd=account_value_usd,
            data_confirmed=data_confirmed,
            reasons=["لا توجد صفقات مغلقة"],
        )

    wins = [f.closed_pnl for f in closed if f.closed_pnl > 0]
    losses = [f.closed_pnl for f in closed if f.closed_pnl < 0]
    gain = sum(wins)
    loss = abs(sum(losses))

    if loss > 0:
        profit_factor = gain / loss
    else:
        profit_factor = 10.0 if gain > 0 else 0.0

    times = [f.time_ms for f in closed]
    active_days = max(0, int((max(times) - min(times)) / 86_400_000))

    return TraderStats(
        wallet=wallet,
        n_trades=n,
        win_rate=len(wins) / n,
        profit_factor=profit_factor,
        max_drawdown=_max_drawdown(
            equity_from_fills(
                closed,
                start_equity=account_value_usd,
                base=max(account_value_usd, sum(abs(f.closed_pnl) for f in closed) * 0.02),
            )
        ),
        sharpe=_sharpe(closed),
        consistency=_consistency(closed),
        concentration=_concentration(closed),
        active_days=active_days,
        avg_hold_hours=avg_hold_hours,
        max_leverage=max_leverage,
        account_value_usd=account_value_usd,
        source=source,
        data_confirmed=data_confirmed,
    )


# ── scoring ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Thresholds:
    trades_full: int = 200
    trades_min: int = 60
    activity_full: int = 365
    activity_min: int = 90
    pf_full: float = 2.0
    pf_min: float = 1.2
    dd_full: float = 0.10
    dd_max: float = 0.30
    consistency_full: float = 0.75
    consistency_min: float = 0.45
    concentration_full: float = 0.15
    concentration_max: float = 0.50
    leverage_full: float = 5.0
    leverage_max: float = 15.0


T = Thresholds()


def _linear(value: float, lo: float, hi: float, points: float) -> float:
    """Map value onto 0..points, clamped, linear between lo and hi."""
    if hi <= lo:
        return points if value >= hi else 0.0
    ratio = (value - lo) / (hi - lo)
    return max(0.0, min(1.0, ratio)) * points


def _linear_inverse(value: float, best: float, worst: float, points: float) -> float:
    """Lower is better (drawdown, concentration)."""
    if worst <= best:
        return points if value <= best else 0.0
    ratio = (worst - value) / (worst - best)
    return max(0.0, min(1.0, ratio)) * points


def classify_trader(stats: TraderStats, thresholds: Thresholds = T) -> tuple[str, int]:
    """Grade a wallet. Returns (grade, score) and mutates stats in place with reasons.

    Hard gates come first: a wallet that cannot clear *minimum* viability is
    REJECT regardless of how good its other numbers look.
    """
    t = thresholds
    reasons: list[str] = []

    # ── hard gates ──────────────────────────────────────────────────────────
    if stats.n_trades < t.trades_min:
        stats.grade = "REJECT"
        stats.score = 0
        stats.reasons = [f"صفقات غير كافية ({stats.n_trades} < {t.trades_min})"]
        return stats.grade, stats.score

    if stats.active_days < t.activity_min:
        stats.grade = "REJECT"
        stats.score = 0
        stats.reasons = [f"عمر نشاط قصير ({stats.active_days} يوم < {t.activity_min})"]
        return stats.grade, stats.score

    if stats.profit_factor < t.pf_min:
        stats.grade = "REJECT"
        stats.score = 0
        stats.reasons = [f"معامل ربح ضعيف ({stats.profit_factor:.2f} < {t.pf_min})"]
        return stats.grade, stats.score

    if stats.max_drawdown > t.dd_max:
        stats.grade = "REJECT"
        stats.score = 0
        stats.reasons = [f"سحب أقصى خطير ({stats.max_drawdown:.1%} > {t.dd_max:.0%})"]
        return stats.grade, stats.score

    # ── weighted score ──────────────────────────────────────────────────────
    score = 0.0

    s = _linear(stats.n_trades, t.trades_min, t.trades_full, 20)
    score += s
    reasons.append(f"عدد الصفقات {stats.n_trades} → {s:.0f}/20")

    s = _linear(stats.active_days, t.activity_min, t.activity_full, 15)
    score += s
    reasons.append(f"عمر النشاط {stats.active_days} يوم → {s:.0f}/15")

    s = _linear(stats.profit_factor, t.pf_min, t.pf_full, 20)
    score += s
    reasons.append(f"معامل الربح {stats.profit_factor:.2f} → {s:.0f}/20")

    s = _linear_inverse(stats.max_drawdown, t.dd_full, t.dd_max, 15)
    score += s
    reasons.append(f"السحب الأقصى {stats.max_drawdown:.1%} → {s:.0f}/15")

    s = _linear(stats.consistency, t.consistency_min, t.consistency_full, 15)
    score += s
    reasons.append(f"الثبات {stats.consistency:.0%} → {s:.0f}/15")

    s = _linear_inverse(stats.concentration, t.concentration_full, t.concentration_max, 10)
    score += s
    reasons.append(f"تركيز الربح {stats.concentration:.0%} → {s:.0f}/10")

    s = _linear_inverse(stats.max_leverage, t.leverage_full, t.leverage_max, 5)
    score += s
    reasons.append(f"أقصى رافعة {stats.max_leverage:.1f}x → {s:.0f}/5")

    # Unconfirmed data is worth less — never let it reach ELITE.
    if not stats.data_confirmed:
        score *= 0.85
        reasons.append("بيانات غير مؤكدة (−15%)")

    stats.score = int(round(max(0.0, min(100.0, score))))

    if stats.score >= 80 and stats.data_confirmed:
        stats.grade = "ELITE"
    elif stats.score >= 65:
        stats.grade = "PROVEN"
    elif stats.score >= 50:
        stats.grade = "WATCH"
    else:
        stats.grade = "REJECT"

    stats.reasons = reasons
    return stats.grade, stats.score


def rank_traders(stats_list: Iterable[TraderStats]) -> list[TraderStats]:
    """Score every wallet and return them best-first."""
    scored = list(stats_list)
    for s in scored:
        if s.grade == "REJECT" and s.score == 0:
            classify_trader(s)
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
