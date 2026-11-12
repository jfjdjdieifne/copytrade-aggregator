"""Trade plan construction — never invent what the traders did not say.

Rules:
  * stop / target come from the traders who supplied them (median, outlier-trimmed)
  * if nobody supplied a stop, derive one from ATR and *label it as computed*
  * the stop is always on the correct side of entry (programmatic guarantee)
  * if a plan is structurally invalid, return None instead of a plausible lie
"""

from __future__ import annotations

from statistics import median
from typing import Iterable, Optional, Sequence

from .config import Config
from .models import BPS, Cluster, TradePlan

DEFAULT_ATR_MULT = 2.0
DEFAULT_RR_TARGET = 1.6


def _trimmed_median(values: Sequence[float], trim: float = 0.15) -> Optional[float]:
    """Median after dropping the extreme tails, so one wild value cannot rule."""
    vals = sorted(v for v in values if v and v > 0)
    if not vals:
        return None
    if len(vals) >= 5:
        cut = int(len(vals) * trim)
        vals = vals[cut: len(vals) - cut] or vals
    return median(vals)


def _atr(prices: Iterable[float]) -> Optional[float]:
    """Average absolute step between consecutive observed prices (cheap ATR proxy)."""
    seq = [p for p in prices if p and p > 0]
    if len(seq) < 2:
        return None
    steps = [abs(seq[i] - seq[i - 1]) for i in range(1, len(seq))]
    if not steps:
        return None
    return sum(steps) / len(steps)


def build_plan(
    cluster: Cluster,
    cfg: Optional[Config] = None,
    atr_hint: Optional[float] = None,
    market_price: Optional[float] = None,
) -> Optional[TradePlan]:
    """Build entry/stop/target/RR for a cluster, or None when it cannot be valid."""
    cfg = cfg or Config()
    notes: list[str] = []

    entry = market_price if market_price and market_price > 0 else cluster.entry_center
    if entry <= 0:
        return None

    # ── entry sanity: refuse a plan that has already run away ───────────────
    if market_price and cluster.entry_center > 0:
        slip = abs(market_price - cluster.entry_center) / cluster.entry_center * BPS
        if slip > cfg.max_slippage_bps:
            return None
        notes.append(f"انزلاق {slip:.0f}bps داخل الحد")

    # ── stop ────────────────────────────────────────────────────────────────
    stops = [m.stop for m in cluster.members if m.stop]
    stop = _trimmed_median(stops)
    stop_source = "trader"
    if stop is None:
        if atr_hint and atr_hint > 0:
            stop = entry - DEFAULT_ATR_MULT * atr_hint if cluster.side == "LONG" \
                else entry + DEFAULT_ATR_MULT * atr_hint
            stop_source = "atr"
            notes.append(f"ستوب محسوب من ATR×{DEFAULT_ATR_MULT:g} — ليس من المصدر")
        else:
            stop_source = "none"
            notes.append("لا ستوب من المصدر ولا ATR متاح — الخطة بلا ستوب مبرر")

    # ── target ──────────────────────────────────────────────────────────────
    targets = [m.target for m in cluster.members if m.target]
    target = _trimmed_median(targets)
    target_source = "trader"
    if target is None:
        target_source = "none"
        notes.append("لا هدف من المصدر")

    # ── orientation checks: a LONG stop must sit below entry ────────────────
    if stop is not None:
        if cluster.side == "LONG" and stop >= entry:
            notes.append("ستوب فوق الدخول في صفقة شراء — تم تجاهله")
            stop, stop_source = None, "none"
        elif cluster.side == "SHORT" and stop <= entry:
            notes.append("ستوب تحت الدخول في صفقة بيع — تم تجاهله")
            stop, stop_source = None, "none"

    if target is not None:
        if cluster.side == "LONG" and target <= entry:
            notes.append("هدف تحت الدخول في صفقة شراء — تم تجاهله")
            target, target_source = None, "none"
        elif cluster.side == "SHORT" and target >= entry:
            notes.append("هدف فوق الدخول في صفقة بيع — تم تجاهله")
            target, target_source = None, "none"

    # ── synthetic target: only from a stop we actually trust ────────────────
    if target is None and stop is not None:
        risk = abs(entry - stop)
        target = entry + DEFAULT_RR_TARGET * risk if cluster.side == "LONG" \
            else entry - DEFAULT_RR_TARGET * risk
        target_source = "atr"
        notes.append(f"هدف محسوب R:R {DEFAULT_RR_TARGET:g} — ليس من المصدر")

    rr: Optional[float] = None
    if stop is not None and target is not None:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = round(reward / risk, 2) if risk > 0 else None
        if rr is not None and rr < 0.5:
            notes.append(f"R:R ضعيف ({rr}) — الصفقة غير جاذبة")

    return TradePlan(
        entry=round(entry, 8),
        stop=None if stop is None else round(stop, 8),
        target=None if target is None else round(target, 8),
        rr=rr,
        stop_source=stop_source,
        target_source=target_source,
        risk_pct=cfg.risk_per_trade_pct,
        notes=notes,
    )
