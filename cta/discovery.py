"""Discovery — 47,000 wallets down to a shortlist worth the expensive calls.

Leaderboard headline numbers are used *only* to triage. Nothing here decides
whether a trader is good: that judgement is made later from real fills in
`classifier.py`. Discovery's job is to spend the smallest possible number of API
calls on the wallets most likely to pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import Config


@dataclass(slots=True)
class Candidate:
    wallet: str
    source: str
    account_value: float = 0.0
    month_pnl: float = 0.0
    month_roi: float = 0.0
    week_pnl: float = 0.0
    week_volume: float = 0.0
    triage_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def triage_row(row: dict, cfg: Config, source: str = "hyperliquid") -> Candidate | None:
    """Convert one leaderboard row into a candidate, or None if it fails triage."""
    wallet = (row.get("wallet") or "").lower()
    if not wallet:
        return None

    windows = row.get("windows") or {}
    month = windows.get("month", {})
    week = windows.get("week", {})

    account_value = float(row.get("account_value") or 0.0)
    month_pnl = float(month.get("pnl") or 0.0)
    month_roi = float(month.get("roi") or 0.0)
    week_pnl = float(week.get("pnl") or 0.0)
    week_volume = float(week.get("vlm") or 0.0)

    reasons: list[str] = []

    # ── gates ───────────────────────────────────────────────────────────────
    if account_value < cfg.min_account_value_usd:
        return None

    # Seeds on venues without a leaderboard have no window data: accept them.
    no_window_data = not windows
    if not no_window_data:
        if month_pnl <= 0 or week_pnl <= 0:
            return None
        if not (cfg.min_month_roi_pct <= month_roi <= cfg.max_month_roi_pct):
            return None
        if week_volume < cfg.min_window_volume_ratio * account_value:
            return None
        reasons.append("ربح في نافذتين مستقلتين")
        reasons.append("نشاط كافٍ")

    # ── triage score (cost-efficiency, not skill) ───────────────────────────
    score = 0.0
    if not no_window_data:
        score += min(40.0, month_pnl / max(account_value, 1.0) * 100.0)   # ROI contribution
        score += min(30.0, week_volume / max(account_value, 1.0) * 3.0)    # liquidity
        score += min(30.0, account_value / 100_000.0 * 10.0)               # capital

    return Candidate(
        wallet=wallet,
        source=source,
        account_value=account_value,
        month_pnl=month_pnl,
        month_roi=month_roi,
        week_pnl=week_pnl,
        week_volume=week_volume,
        triage_score=round(score, 2),
        reasons=reasons,
    )


def select_candidates(
    rows: Iterable[dict], cfg: Config, source: str = "hyperliquid"
) -> list[Candidate]:
    """Triage every row, dedupe by wallet, keep the strongest N."""
    best: dict[str, Candidate] = {}
    for row in rows:
        cand = triage_row(row, cfg, source)
        if cand is None:
            continue
        existing = best.get(cand.wallet)
        if existing is None or cand.triage_score > existing.triage_score:
            best[cand.wallet] = cand

    ordered = sorted(best.values(), key=lambda c: c.triage_score, reverse=True)
    return ordered[: cfg.discovery_max_candidates]


def build_watchlist(
    scored_profiles: Sequence,
    cfg: Config,
) -> list:
    """Keep only wallets at or above the configured grade, best first."""
    min_rank = cfg.watchlist_min_rank
    from .config import GRADE_ORDER

    keep = [
        p for p in scored_profiles
        if GRADE_ORDER.get(getattr(p, "grade", "REJECT"), 0) >= min_rank
    ]
    keep.sort(key=lambda p: getattr(p, "score", 0), reverse=True)
    return keep[: cfg.watchlist_max]


def control_group(candidates: Sequence[Candidate], size: int = 100) -> list[Candidate]:
    """A random sample kept *unfiltered* by quality.

    Without this baseline you can never tell whether the radar's signals beat
    chance. It costs nothing to keep and is the difference between belief and
    evidence.
    """
    pool = list(candidates)
    if len(pool) <= size:
        return pool
    # Deterministic spread sample: no RNG state to persist across isolates.
    step = len(pool) / size
    return [pool[int(i * step) % len(pool)] for i in range(size)]
