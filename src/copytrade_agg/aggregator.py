"""
Copy Trade Aggregation Engine — محرك تجميع نسخ الصفقات.

يربط بيانات المنصات العامة بمجمع الإشارات (aggregator) ويصدر إشارات مجمعة
مع درجات ثقة. هذا الملف مستقل عن ZONES v1.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .models import Signal, AggregatedSignal, CopyTradeAggregator
from .connectors import RawFill, normalize_fill_to_signal

UTC = timezone.utc


@dataclass
class EngineConfig:
    entry_tolerance: float = 0.0020   # 20 نقطة افتراضياً لليورو/دولار
    min_traders: int = 2              # الحد الأدنى من المتداولين المتفقين
    min_confidence: float = 0.50      # الحد الأدنى لدرجة الثقة لإصدار الإشارة
    default_trader_rating: float = 3.0


@dataclass
class AggregationResult:
    signals: list[AggregatedSignal]
    n_raw_fills: int
    n_platforms: int
    generated_at: datetime


class CopyTradeEngine:
    """
    المحرك الرئيسي:
    1. يستقبل صفقات خام من منصات متعددة
    2. يحولها إلى إشارات Signal
    3. يمررها عبر CopyTradeAggregator للتجميع
    4. يُعيد إشارات مجمعة تمرّت على thresholds الثقة
    """

    def __init__(self, config: Optional[EngineConfig] = None):
        self.config = config or EngineConfig()
        self.aggregator = CopyTradeAggregator(
            entry_tolerance=self.config.entry_tolerance,
            min_traders=self.config.min_traders,
        )
        self._raw_fills: list[RawFill] = []

    def ingest_raw_fill(self, raw: RawFill) -> None:
        """إضافة صفقة خام واحدة إلى المخزن المؤقت."""
        self._raw_fills.append(raw)

    def ingest_raw_fills(self, fills: list[RawFill]) -> None:
        """إضافة عدة صفقات خام."""
        self._raw_fills.extend(fills)

    def clear_fills(self) -> None:
        """مسح المخزن المؤقت (يُستخدم بين الدورات)."""
        self._raw_fills.clear()

    def aggregate(self) -> AggregationResult:
        """
        تحويل الصفقات الخام إلى إشارات مجمعة.
        """
        # 1. تحويل كل صفقة خام إلى Signal
        signals: list[Signal] = []
        platforms = set()
        for raw in self._raw_fills:
            d = normalize_fill_to_signal(
                raw, trader_rating=self.config.default_trader_rating
            )
            platforms.add(raw.platform)
            signals.append(Signal(**d))

        # 2. تمرير الإشارات عبر المجمع
        aggregated = self.aggregator.aggregate(signals)

        # 3. تصفية حسب الحد الأدنى للثقة
        filtered = [
            a for a in aggregated if a.confidence >= self.config.min_confidence
        ]

        return AggregationResult(
            signals=filtered,
            n_raw_fills=len(signals),
            n_platforms=len(platforms),
            generated_at=datetime.now(UTC),
        )

    def latest_signal(self, pair: Optional[str] = None) -> Optional[AggregatedSignal]:
        """
        إرجاع أفضل إشارة مجمعة لزوج معين (أو الأفضل عموماً).
        """
        result = self.aggregate()
        if not result.signals:
            return None
        if pair:
            candidates = [s for s in result.signals if s.pair == pair]
            return candidates[0] if candidates else None
        return result.signals[0]


# ---------------------------------------------------------------------------
# Demo مع بيانات اصطناعية لاختبار المحرك
# ---------------------------------------------------------------------------


def _make_raw_fill(
    trader: str, pair: str, side: str, price: float,
    size: float, platform: str,
) -> RawFill:
    from datetime import timedelta
    return RawFill(
        trader=trader, pair=pair, side=side, price=price, size=size,
        timestamp=datetime.now(UTC), platform=platform, raw={},
    )


def run_engine_demo() -> None:
    print("=" * 70)
    print("Copy Trade Engine — Demo (بيانات اصطناعية للاختبار)")
    print("=" * 70)

    engine = CopyTradeEngine(
        EngineConfig(entry_tolerance=0.0020, min_traders=2, min_confidence=0.40)
    )

    # صفقات من منصات مختلفة على EURUSD BUY حول 1.0850
    fills = [
        _make_raw_fill("0xaaa", "EURUSD", "BUY", 1.0850, 1.0, "hyperliquid"),
        _make_raw_fill("0xbbb", "EURUSD", "BUY", 1.0852, 1.0, "dydx"),
        _make_raw_fill("0xccc", "EURUSD", "BUY", 1.0848, 1.0, "hyperliquid"),
        # صفقة خارج التolerance قليلاً (1.0860) — قد تشكل مجموعة منفصلة أو تُدمج
        _make_raw_fill("0xddd", "EURUSD", "BUY", 1.0860, 1.0, "dydx"),
        # صفقات على GBPUSD SELL (متداولان فقط = الحد الأدنى)
        _make_raw_fill("0xeee", "GBPUSD", "SELL", 1.2650, 1.0, "hyperliquid"),
        _make_raw_fill("0xfff", "GBPUSD", "SELL", 1.2653, 1.0, "dydx"),
        # صفقة وحيدة على EURUSD SELL → يجب أن تُهمل (أقل من 2 متداولين)
        _make_raw_fill("0x111", "EURUSD", "SELL", 1.0800, 1.0, "hyperliquid"),
    ]

    engine.ingest_raw_fills(fills)
    result = engine.aggregate()

    print(f"\nصفقات خام واردت: {result.n_raw_fills}")
    print(f"منصات مشاركة: {result.n_platforms}")
    print(f"إشارات مجمعة بعد التصفية: {len(result.signals)}\n")

    for sig in result.signals:
        print(f"- {sig.pair} {sig.direction}")
        print(f"  متداولين: {sig.n_traders} | الثقة: {sig.confidence}")
        print(f"  دخول مجمّع: {sig.entry_cluster_center} (انتشار: {sig.entry_spread})")
        print(f"  ستوب: {sig.stop_consensus} | هدف: {sig.target_consensus}")
        print(f"  مصادر: {sig.sources}")
        print()

    best = engine.latest_signal("EURUSD")
    if best:
        print(f"أفضل إشارة لـ EURUSD: {best.direction} @ {best.entry_cluster_center} (ثقة {best.confidence})")

    print("\nملاحظة: بيانات افتراضية للاختبار فقط (HYPOTHETICAL_).")


if __name__ == "__main__":
    run_engine_demo()