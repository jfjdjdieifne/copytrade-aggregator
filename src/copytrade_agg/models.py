"""
نماذج البيانات لنظام نسخ الصفقات المجمّع (Copy-Trade Aggregator).

يحتوي على:
- Signal: إشارة نسخ تداول واحدة من متداول واحد
- AggregatedSignal: إشارة مجمعة بعد تجميع عدة متداولين
- CopyTradeAggregator: منطق التجميع والتجميع العنقودي لمستويات الدخول

هذا الملف مستقل تماماً عن ZONES v1 (مجمد).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc


@dataclass
class Signal:
    """إشارة نسخ تداول واحدة من متداول واحد من موقع واحد."""

    pair: str                 # مثال: "EURUSD" أو "BTC-USD"
    direction: str            # "BUY" أو "SELL"
    entry: float              # مستوى الدخول المقترح
    stop: Optional[float]     # مستوى إيقاف الخسارة (قد يكون None)
    target: Optional[float]   # مستوى الهدف (قد يكون None)
    trader_id: str            # معرف المتداول في المصدر
    trader_rating: float      # تقييم المتداول (مثل 4.5 من 5)
    source: str               # اسم المصدر (مثل "liquid.trade", "ForexFactory")
    received_at: datetime     # وقت استلام الإشارة
    raw_note: str = ""        # أي ملاحظات إضافية من المصدر


@dataclass
class AggregatedSignal:
    """إشارة مجمعة بعد تجميع إشارات متداولين متعددين."""

    pair: str
    direction: str
    n_traders: int            # عدد المتداولين المتفقين
    confidence: float         # درجة الثقة من 0 إلى 1
    entry_cluster_center: float  # وسط مجموعة مستويات الدخول
    entry_spread: float       # مدى انتشار مستويات الدخول (max - min)
    stop_consensus: Optional[float]  # متوسط الستوب (إذا توفرت)
    target_consensus: Optional[float]  # متوسط الهدف (إذا توفرت)
    trader_ids: list[str]     # معرفات المتداولين المشاركين
    sources: list[str]        # المصادر المشاركة
    created_at: datetime


class CopyTradeAggregator:
    """
    يجمّع إشارات النسخ ويصدر إشارات مجمعة ذات درجة ثقة.

    قواعد التجميع:
    1. نفس الزوج (pair) ونفس الاتجاه (direction) → يجب أن يتطابقا تماماً.
    2. داخل المجموعة، تُCluster مستويات الدخول باستخدام `entry_tolerance`.
    3. إذا كانت مستويات الدخول ضمن التolerance من وسط المجموعة → تُدمج.
    4. إذا خرج مستوى عن التolerance → يبدأ مجموعة جديدة.
    5. الحد الأدنى: متداولان (2) أو أكثر لتفعيل الإشارة المجمعة.
    6. درجة الثقة تزداد مع عدد المتداولين المتفقين ومع تقييماتهم.
    """

    def __init__(self, entry_tolerance: float = 0.0020, min_traders: int = 2):
        """
        Args:
            entry_tolerance: التحمل المقبول لمستوى الدخول.
                             للسوق الفوركس النموذجية: 0.0020 = 20 نقطة.
                             يمكن ضبطه لكل زوج على حدة.
            min_traders: الحد الأدنى من عدد المتداولين لتفعيل الإشارة.
        """
        self.entry_tolerance = entry_tolerance
        self.min_traders = min_traders

    def aggregate(self, signals: list[Signal]) -> list[AggregatedSignal]:
        """
        تجميع قائمة الإشارات إلى إشارات مجمعة.

        Algorithm:
        - group by (pair, direction)
        - dentro من كل مجموعة، Cluster مستويات الدخول باستخدام tolerance
        - لكل cluster به min_traders أو أكثر → AggregatedSignal
        """
        # Step 1: regroup by (pair, direction)
        groups: dict[tuple[str, str], list[Signal]] = {}
        for s in signals:
            key = (s.pair, s.direction.upper())
            groups.setdefault(key, []).append(s)

        aggregated: list[AggregatedSignal] = []

        for (pair, direction), group_signals in groups.items():
            clusters = self._cluster_entries(group_signals)
            for cluster in clusters:
                if len(cluster) < self.min_traders:
                    continue
                agg = self._build_aggregated(pair, direction, cluster)
                aggregated.append(agg)

        # ترتيب حسب درجة الثقة نزولياً
        aggregated.sort(key=lambda a: a.confidence, reverse=True)
        return aggregated

    def _cluster_entries(self, signals: list[Signal]) -> list[list[Signal]]:
        """
        تجميع مستويات الدخول المتقاربة ضمن tolerance.
        تستخدم greedy clustering: ترتيب حسب الدخول ثم دمج المتقاربة.
        """
        if not signals:
            return []

        sorted_sigs = sorted(signals, key=lambda s: s.entry)
        clusters: list[list[Signal]] = [[sorted_sigs[0]]]

        for s in sorted_sigs[1:]:
            last_cluster = clusters[-1]
            # تقريب الدخول إلى وسط المجموعة الأخيرة
            cluster_center = sum(x.entry for x in last_cluster) / len(last_cluster)
            if abs(s.entry - cluster_center) <= self.entry_tolerance:
                last_cluster.append(s)
            else:
                clusters.append([s])

        return clusters

    def _build_aggregated(
        self, pair: str, direction: str, cluster: list[Signal]
    ) -> AggregatedSignal:
        n = len(cluster)
        entries = [s.entry for s in cluster]
        entry_center = sum(entries) / n
        entry_spread = max(entries) - min(entries)

        # ستوب وهدف: متوسط إذا توفرت جميعاً، وإلا None
        stops = [s.stop for s in cluster if s.stop is not None]
        targets = [s.target for s in cluster if s.target is not None]
        stop_consensus = sum(stops) / len(stops) if stops else None
        target_consensus = sum(targets) / len(targets) if targets else None

        # درجة الثقة:
        # - مكون عدد المتداولين: min(1, n / 5)  → يتطلب 5 متداولين لدرجة كاملة
        # - مكون التقييم المتوسط: متوسط تقييم المتداولين / 5
        # - مكون الانتشار: كلما كان انتشار الدخول أقل، زادت الثقة
        trader_score = min(1.0, n / 5.0)
        avg_rating = sum(s.trader_rating for s in cluster) / n
        rating_score = avg_rating / 5.0
        spread_penalty = max(0.0, 1.0 - (entry_spread / self.entry_tolerance))
        confidence = (0.5 * trader_score + 0.3 * rating_score + 0.2 * spread_penalty)

        return AggregatedSignal(
            pair=pair,
            direction=direction,
            n_traders=n,
            confidence=round(confidence, 3),
            entry_cluster_center=round(entry_center, 5),
            entry_spread=round(entry_spread, 5),
            stop_consensus=round(stop_consensus, 5) if stop_consensus else None,
            target_consensus=round(target_consensus, 5) if target_consensus else None,
            trader_ids=sorted({s.trader_id for s in cluster}),
            sources=sorted({s.source for s in cluster}),
            created_at=datetime.now(UTC),
        )


def run_models_demo() -> None:
    """تشغيل توضيحي مع بيانات افتراضية لاختبار المنطق."""
    print("=" * 70)
    print("Copy Trade Aggregator — Models Demo (بيانات افتراضية)")
    print("=" * 70)

    signals: list[Signal] = []

    # مجموعة متفقين على شراء EURUSD حول 1.0850
    for entry, trader, rating, source in [
        (1.0850, "T001", 4.8, "liquid.trade"),
        (1.0852, "T002", 4.5, "ForexFactory"),
        (1.0848, "T003", 4.2, "liquid.trade"),
        (1.0860, "T004", 3.9, "Bloomberg"),  # خارج التolerance قليلاً
    ]:
        signals.append(Signal(
            pair="EURUSD", direction="BUY", entry=entry,
            stop=1.0820, target=1.0900,
            trader_id=trader, trader_rating=rating,
            source=source, received_at=datetime.now(UTC),
        ))

    # مجموعة متفقين على بيع GBPUSD حول 1.2650 (متداولان فقط = الحد الأدنى)
    for entry, trader, rating, source in [
        (1.2650, "T010", 4.7, "liquid.trade"),
        (1.2653, "T011", 4.3, "ForexFactory"),
    ]:
        signals.append(Signal(
            pair="GBPUSD", direction="SELL", entry=entry,
            stop=1.2680, target=1.2600,
            trader_id=trader, trader_rating=rating,
            source=source, received_at=datetime.now(UTC),
        ))

    # متداول واحد فقط على EURUSD sell → يجب أن يُهمل (أقل من min_traders=2)
    signals.append(Signal(
        pair="EURUSD", direction="SELL", entry=1.0800,
        stop=1.0830, target=1.0750,
        trader_id="T020", trader_rating=4.0,
        source="liquid.trade", received_at=datetime.now(UTC),
    ))

    agg = CopyTradeAggregator(entry_tolerance=0.0020, min_traders=2)
    results = agg.aggregate(signals)

    print(f"\nإشارات واردة: {len(signals)}")
    print(f"إشارات مجمعة (تجاوزت الحد الأدنى {agg.min_traders} متداول): {len(results)}\n")

    for r in results:
        print(f"- {r.pair} {r.direction}")
        print(f"  متداولين: {r.n_traders} | الثقة: {r.confidence}")
        print(f"  دخول مجمّع: {r.entry_cluster_center} (انتشار: {r.entry_spread})")
        print(f"  ستوب: {r.stop_consensus} | هدف: {r.target_consensus}")
        print(f"  مصادر: {r.sources} | معرفات: {r.trader_ids}")
        print()

    print("ملاحظة: هذه بيانات افتراضية للتوضيح فقط (HYPOTHETICAL_).")
    print("لا تمثل نصيحة استثمارية ولا تضمن أي ربح.")


if __name__ == "__main__":
    run_models_demo()