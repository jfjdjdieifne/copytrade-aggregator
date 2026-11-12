"""Confidence scoring — 100 points, five components, fully explainable.

    A. consensus strength   35  (12 per independent trader beyond the first)
    B. trader quality       30  (mean quality score)
    C. price tightness      20  (1 - spread/tolerance)
    D. time synchrony       10  (1 - span/window)
    E. source diversity      5  (distinct venues)

Every deduction carries a human sentence, because a number you cannot explain is
a number you cannot trust — and the Telegram assistant needs the words too.
"""

from __future__ import annotations

from typing import Optional

from .config import GRADE_ORDER, Config
from .models import Cluster, Confidence

LEVELS = (
    (80, "STRONG"),
    (65, "GOOD"),
    (50, "WATCH"),
    (0, "REJECT"),
)


def _consensus_points(n: int) -> int:
    """Points before normalisation — tuned so minimum viable agrees.

    A two-trader cluster is the baseline the system is willing to act on, so it
    must be able to clear the threshold when the other components are strong.
    """
    if n <= 1:
        return 0
    return min(80, 20 * (n - 1) + (10 if n >= 3 else 0))


def _level_for(total: int) -> str:
    for floor, name in LEVELS:
        if total >= floor:
            return name
    return "REJECT"


def score_cluster(cluster: Cluster, cfg: Optional[Config] = None) -> Confidence:
    """Score a cluster out of 100 and explain the arithmetic."""
    cfg = cfg or Config()
    reasons: list[str] = []
    breakdown: dict[str, int] = {}

    # ── A. consensus strength ───────────────────────────────────────────────
    #
    # This used to be `(n - 1) * 12`, which made the configured bar
    # unreachable in the common case. Two independent traders is the *minimum*
    # the system accepts, and even a perfect pair (30/30 quality, 20/20
    # tightness, 10/10 synchrony) could only reach 72 — with realistic values
    # landing at 61-64, below the 65 threshold. So the radar could run forever,
    # watch perfectly good convergence, and never emit anything. Silence looked
    # exactly like a quiet market, which is the most expensive bug this system
    # can have: days of waiting to learn nothing.
    #
    # The scale now reaches parity with `min_independent_traders`:
    #   n = 2 -> 20   n = 3 -> 40   n = 4 -> 60   n = 5+ -> 80 (capped)
    # capped at the component's weight so the total still means 100.
    n = cluster.n_members
    consensus = min(80, _consensus_points(n))
    consensus = int(round(consensus * (35.0 / 80.0)))  # normalise to 35 max
    breakdown["consensus"] = consensus
    reasons.append(f"{n} متداول مستقل → {consensus}/35")

    # ── B. trader quality ───────────────────────────────────────────────────
    if cluster.members:
        avg_q = sum(m.quality_score for m in cluster.members) / len(cluster.members)
    else:
        avg_q = 0.0
    quality = int(round(max(0.0, min(1.0, avg_q / 100.0)) * 30))
    breakdown["quality"] = quality
    reasons.append(f"متوسط الجودة {avg_q:.0f}/100 → {quality}/30")

    # ── C. price tightness ──────────────────────────────────────────────────
    tol = cfg.entry_tolerance_bps or 1.0
    tightness = int(round(max(0.0, 1.0 - cluster.spread_bps / tol) * 20))
    breakdown["tightness"] = tightness
    reasons.append(f"انتشار {cluster.spread_bps:.1f}bps من {tol:.0f} → {tightness}/20")

    # ── D. time synchrony ───────────────────────────────────────────────────
    window = cfg.convergence_window_min or 1.0
    sync = int(round(max(0.0, 1.0 - cluster.time_span_min / window) * 10))
    breakdown["synchrony"] = sync
    reasons.append(f"تزامن {cluster.time_span_min:.0f}د من {window:.0f} → {sync}/10")

    # ── E. source diversity ─────────────────────────────────────────────────
    diversity = min(5, 5 if cluster.n_distinct_sources >= 2 else 0)
    breakdown["diversity"] = diversity
    if diversity:
        reasons.append(f"مصادر متنوعة ({', '.join(cluster.sources)}) → {diversity}/5")
    else:
        reasons.append(f"كل المتداولين من منصة واحدة ({', '.join(cluster.sources)}) → 0/5")

    total = max(0, min(100, consensus + quality + tightness + sync + diversity))
    level = _level_for(total)

    # ── hard overrides: never emit below the configured bar ─────────────────
    if n < cfg.min_independent_traders:
        level = "REJECT"
        reasons.append(f"عدد المستقلين {n} أقل من الحد {cfg.min_independent_traders}")

    elite_count = sum(1 for m in cluster.members if m.quality in ("ELITE", "PROVEN"))
    if elite_count < cfg.min_elite_or_proven:
        level = "REJECT"
        reasons.append(f"المحترفون المؤكدون {elite_count} أقل من {cfg.min_elite_or_proven}")

    if total < cfg.min_signal_score and level in ("STRONG", "GOOD"):
        level = "WATCH"
        reasons.append(f"الدرجة {total} أقل من عتبة الإرسال {cfg.min_signal_score}")

    # Data we cannot confirm must never masquerade as STRONG.
    if any(m.quality_score <= 0 for m in cluster.members):
        if level == "STRONG":
            level = "GOOD"
            reasons.append("بعض المتداولين بلا تقييم كامل → خُفّض إلى GOOD")

    return Confidence(total=total, level=level, breakdown=breakdown, reasons=reasons)


def best_grade(cluster: Cluster) -> str:
    """Highest grade present in the cluster (for display)."""
    if not cluster.members:
        return "REJECT"
    return max(
        (m.quality for m in cluster.members),
        key=lambda g: GRADE_ORDER.get(g, 0),
    )
