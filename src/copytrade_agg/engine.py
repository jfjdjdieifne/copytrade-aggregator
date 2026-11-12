"""
Recommendation Engine — محرك التوصيات المجمعة (توصية فقط، لا تنفيذ تلقائي).

يربط بين وصلات المنصات، مصنف المتداولين، وم계數 الثقة، ويصدر توصيات
يمكن عرضها أو إرسالها إلى التيليجرام.

مواصفات التحديث الجديد:
- يبدأ بالكريبتو، ويفصل Spot عن Futures (لا يُخلط بينهما)
- مسافة دخول 1.5% لاعتبار الدخولات متقاربة
- Min 2 محترفين مستقلين متفقين لإطلاق الإشارة
- يعرض دخول كل محترف كما ذكر، ويحسب الوسط الحسابي كملخص (لا سعر مخترع)
- لا يغير ولا يركّب هدفاً أو وقفاً أو رافعة من عنده
- جدول مقارنة مرتب حسب الجودة
- ي distinguish البيانات المؤكدة عن الناقصة
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .classifier import TraderProfile, TraderClassifier
from .scorer import ConfidenceScorer, ConfidenceResult
from .connectors import RawFill, normalize_fill_to_signal

UTC = timezone.utc


@dataclass
class Recommendation:
    """توصية تداول مجمعة (توصية فقط — لا تنفيذ تلقائي)."""
    pair: str
    direction: str
    market_type: str              # "spot" أو "futures"
    confidence_score: int
    confidence_level: str
    entry_mean: float             # وسط دخول المحترفين (كما وردوا، لا مخترع)
    entry_spread_pct: float
    entry_tolerance_pct: float
    stop_loss: Optional[float]    # كما ورد من المحترفين، لا يُخترع
    take_profit: Optional[float]  # كما ورد من المحترفين، لا يُخترع
    leverage: Optional[float]     # كما ورد، لا يُخترع
    n_professional_active: int
    n_professional_inactive: int
    n_regular: int
    n_suspicious: int
    n_total_signals: int
    sources: list[str]
    data_confirmed_count: int
    trader_details: list[dict]
    recommendation_text: str
    generated_at: datetime
    signal_emitted: bool          # هل أُطلقت الإشارة فعلياً
    telegram_message: str
    current_price: Optional[float] = None
    current_price_source: str = ""


class RecommendationEngine:
    """
    محرك التوصيات الرئيسي.

    يربط البيانات من منصات متعددة، يصنف المتداولين، يزيل التكرار (نفس الشخص
    يُحتسب مرة واحدة)، يحسب درجة الثقة، ويصدر توصيات مجمعة.

    لا يخترع أهدافاً أو أوقفاً أو رافعات — يعرض خطة كل متداول كما وردت من المصدر.
    """

    def __init__(self, entry_tolerance_pct: float = 1.5, min_confidence: int = 45):
        """
        Args:
            entry_tolerance_pct: مساحة الدخول المسموحة (1.5% افتراضياً)
            min_confidence: الحد الأدنى لدرجة الثقة لعرض التوصية
        """
        self.entry_tolerance_pct = entry_tolerance_pct
        self.min_confidence = min_confidence
        self.classifier = TraderClassifier()
        self.scorer = ConfidenceScorer(entry_tolerance_pct=entry_tolerance_pct)
        self._signals: list[TraderProfile] = []

    def add_signal(
        self,
        trader_id: str,
        platform: str,
        market_type: str,           # "spot" أو "futures"
        pair: str,
        direction: str,
        entry_price: float,
        stop_loss: Optional[float] = None,      # كما ورد، لا يُخترع
        take_profit: Optional[float] = None,    # كما ورد، لا يُخترع
        leverage: Optional[float] = None,       # كما ورد، لا يُخترع
        signal_timestamp: Optional[datetime] = None,
        **stats_kwargs,
    ) -> None:
        """إضافة إشارة تداول واحدة من متداول."""
        if signal_timestamp is None:
            signal_timestamp = datetime.now(UTC)
        profile = TraderProfile(
            trader_id=trader_id,
            platform=platform,
            market_type=market_type,
            pair=pair,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=leverage,
            signal_timestamp=signal_timestamp,
            **stats_kwargs,
        )
        self._signals.append(profile)

    def add_raw_fill(self, raw: RawFill, trader_stats: Optional[dict] = None) -> None:
        """تحويل RawFill من منصة إلى إشارة تداول وإضافتها."""
        d = normalize_fill_to_signal(raw, trader_rating=3.0)
        profile = TraderProfile(
            trader_id=raw.trader,
            platform=raw.platform,
            pair=raw.pair,
            direction=raw.side.upper(),
            entry_price=raw.price,
            signal_timestamp=raw.timestamp,
            market_type=raw.raw.get("market_type", "spot"),
            **(trader_stats or {}),
        )
        self._signals.append(profile)

    def clear_signals(self) -> None:
        """مسح جميع الإشارات المخزنة."""
        self._signals.clear()

    def generate_recommendations(
        self,
        pair: Optional[str] = None,
        direction: Optional[str] = None,
        market_type: Optional[str] = None,
    ) -> list[Recommendation]:
        """
        توليد توصيات مجمعة من الإشارات المخزنة.

        Args:
            pair: تصفية لزوج معين
            direction: تصفية لاتجاه معين
            market_type: تصفية لنوع السوق ("spot" أو "futures") — لا يُخلط

        Returns:
            قائمة توصيات مرتبة حسب درجة الثقة نزولياً.
        """
        filtered = [
            s for s in self._signals
            if (pair is None or s.pair == pair)
            and (direction is None or s.direction.upper() == direction.upper())
            and (market_type is None or s.market_type == market_type)
        ]

        if not filtered:
            return []

        # تجميع الإشارات حسب (الزوج، الاتجاه، نوع السوق)
        groups: dict[tuple[str, str, str], list[TraderProfile]] = {}
        for s in filtered:
            key = (s.pair, s.direction.upper(), s.market_type)
            groups.setdefault(key, []).append(s)

        recommendations: list[Recommendation] = []

        for (pair_key, direction_key, market_key), profiles in groups.items():
            # حساب درجة الثقة
            result = self.scorer.score(
                profiles, pair_key, direction_key, market_key,
                entry_tolerance_pct=self.entry_tolerance_pct,
            )

            # تصفية حسب الحد الأدنى للثقة وإطلاق الإشارة
            if not result.signal_emitted:
                continue
            if result.total_score < self.min_confidence:
                continue

            # حساب مستويات الدخول والستوب والهدف كما وردت من المحترفين (لا تُخترع)
            entries = [p.entry_price for p in profiles if p.entry_price > 0]
            entry_mean = sum(entries) / len(entries) if entries else 0.0

            # الستوب والهدف كما وردوا (متوسط ما تم ذكره فقط، لا اختراع)
            stops = [p.stop_loss for p in profiles if p.stop_loss is not None]
            stop_loss = sum(stops) / len(stops) if stops else None

            targets = [p.take_profit for p in profiles if p.take_profit is not None]
            take_profit = sum(targets) / len(targets) if targets else None

            leverages = [p.leverage for p in profiles if p.leverage is not None]
            leverage = sum(leverages) / len(leverages) if leverages else None

            # رسالة التيليجرام
            telegram_msg = self.scorer.format_telegram_message(
                result, pair_key, direction_key, market_key,
                current_price=None,  # يُملأ من outside عند الإرسال الفعلي
            )

            rec = Recommendation(
                pair=pair_key,
                direction=direction_key,
                market_type=market_key,
                confidence_score=result.total_score,
                confidence_level=result.confidence_level,
                entry_mean=round(entry_mean, 6),
                entry_spread_pct=result.entry_spread_pct,
                entry_tolerance_pct=result.entry_tolerance_pct,
                stop_loss=round(stop_loss, 6) if stop_loss else None,
                take_profit=round(take_profit, 6) if take_profit else None,
                leverage=round(leverage, 2) if leverage else None,
                n_professional_active=result.n_professional_active,
                n_professional_inactive=result.n_professional_inactive,
                n_regular=result.n_regular,
                n_suspicious=result.n_suspicious,
                n_total_signals=len(profiles),
                sources=result.sources,
                data_confirmed_count=result.data_confirmed_count,
                trader_details=[e.__dict__ for e in result.trader_entries],
                recommendation_text=result.recommendation,
                generated_at=datetime.now(UTC),
                telegram_message=telegram_msg,
                signal_emitted=result.signal_emitted,
            )
            recommendations.append(rec)

        # ترتيب حسب درجة الثقة نزولياً
        recommendations.sort(key=lambda r: r.confidence_score, reverse=True)
        return recommendations

    def get_best_recommendation(
        self, pair: Optional[str] = None, market_type: Optional[str] = None
    ) -> Optional[Recommendation]:
        """إرجاع أفضل توصية (أو لزوج/نوع سوق معين)."""
        recs = self.generate_recommendations(pair=pair, market_type=market_type)
        return recs[0] if recs else None

    def get_statistics(self) -> dict:
        """إرجاع إحصائيات عن الإشارات المخزنة."""
        total = len(self._signals)
        if total == 0:
            return {"total_signals": 0}

        # إزالة التكرار للإحصاء الدقيق
        deduped = self.classifier.deduplicate_by_trader_id(self._signals)
        classified = self.classifier.classify_batch(deduped)

        prof_active = len(self.classifier.get_professional_active(classified))
        prof_inactive = len([p for p in classified if p.classification == "PROFESSIONAL_INACTIVE"])
        regular = len([p for p in classified if p.classification == "REGULAR"])
        suspicious = len([p for p in classified if p.classification == "SUSPICIOUS"])

        pairs = list(set(s.pair for s in deduped))
        platforms = list(set(s.platform for s in deduped))
        market_types = list(set(s.market_type for s in deduped))

        return {
            "total_signals": total,
            "unique_traders": len(deduped),
            "professional_active": prof_active,
            "professional_inactive": prof_inactive,
            "regular": regular,
            "suspicious": suspicious,
            "pairs": pairs,
            "platforms": platforms,
            "market_types": market_types,
        }


# ---------------------------------------------------------------------------
# مثال توضيحي
# ---------------------------------------------------------------------------

def demo() -> None:
    """تشغيل توضيحي لمحرك التوصيات."""
    print("=" * 70)
    print("Recommendation Engine — Demo (محرك التوصيات)")
    print("=" * 70)

    engine = RecommendationEngine(
        entry_tolerance_pct=1.5, min_confidence=45
    )

    utc = timezone.utc

    # إشارات من منصات مختلفة على BTC-USD شراء Spot
    engine.add_signal(
        trader_id="0xpro1", platform="collective2", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65000.0,
        stop_loss=63000.0, take_profit=69000.0, leverage=1.0,
        total_trades=450, winning_trades=270, losing_trades=180,
        win_rate=0.60, profit_factor=1.85, max_drawdown=0.12,
        sharpe_ratio=1.45, total_return_pct=45.5, annual_return_pct=18.2,
        account_age_days=730, last_trade_days_ago=2,
        performance_window_days=270, trades_in_window=300, win_rate_in_window=0.59,
        followers_count=1200, aum_usd=4500000, platform_score=85.0,
        data_confirmed=True, data_source_url="https://collective2.com/system/0xpro1",
        data_freshness_hours=4,
        signal_timestamp=datetime(2026, 11, 12, 10, 0, 0, tzinfo=utc),
    )
    engine.add_signal(
        trader_id="0xpro2", platform="myfxbook", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65200.0,
        stop_loss=63200.0, take_profit=69200.0, leverage=1.0,
        total_trades=320, winning_trades=192, losing_trades=128,
        win_rate=0.60, profit_factor=1.65, max_drawdown=0.15,
        sharpe_ratio=1.20, total_return_pct=38.0, annual_return_pct=12.5,
        account_age_days=540, last_trade_days_ago=5,
        performance_window_days=240, trades_in_window=200, win_rate_in_window=0.58,
        followers_count=800, aum_usd=2800000, platform_score=78.0,
        data_confirmed=True, data_source_url="https://myfxbook.com/trader/0xpro2",
        data_freshness_hours=12,
        signal_timestamp=datetime(2026, 11, 12, 10, 1, 0, tzinfo=utc),
    )
    engine.add_signal(
        trader_id="0xpro3", platform="hyperliquid", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=64900.0,
        stop_loss=62900.0, take_profit=68900.0, leverage=2.0,
        total_trades=280, winning_trades=168, losing_trades=112,
        win_rate=0.60, profit_factor=1.55, max_drawdown=0.18,
        sharpe_ratio=1.10, total_return_pct=32.0, annual_return_pct=14.0,
        account_age_days=365, last_trade_days_ago=1,
        performance_window_days=200, trades_in_window=180, win_rate_in_window=0.59,
        followers_count=450, aum_usd=1200000, platform_score=72.0,
        data_confirmed=True, data_source_url="https://hyperliquid.xyz/trader/0xpro3",
        data_freshness_hours=2,
        signal_timestamp=datetime(2026, 11, 12, 10, 2, 0, tzinfo=utc),
    )
    # شخص عادي على نفس الزوج (لن يؤثر في حد الإطلاق)
    engine.add_signal(
        trader_id="0xreg1", platform="dydx", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65500.0,
        total_trades=45, winning_trades=23, losing_trades=22,
        win_rate=0.51, profit_factor=1.10, max_drawdown=0.08,
        account_age_days=90, last_trade_days_ago=3,
        performance_window_days=90, trades_in_window=30, win_rate_in_window=0.50,
        followers_count=30, aum_usd=80000,
        data_confirmed=True, data_freshness_hours=24,
        signal_timestamp=datetime(2026, 11, 12, 10, 3, 0, tzinfo=utc),
    )
    # إشارة على ETH-USD بيع Futures (لسوق مختلف، لا يُخلط مع BTC spot)
    engine.add_signal(
        trader_id="0xpro4", platform="binance", market_type="futures",
        pair="ETH-USD", direction="SELL", entry_price=3400.0,
        stop_loss=3500.0, take_profit=3200.0, leverage=3.0,
        total_trades=380, winning_trades=228, losing_trades=152,
        win_rate=0.60, profit_factor=1.70, max_drawdown=0.14,
        sharpe_ratio=1.30, total_return_pct=41.0, annual_return_pct=16.0,
        account_age_days=650, last_trade_days_ago=1,
        performance_window_days=300, trades_in_window=250, win_rate_in_window=0.59,
        followers_count=950, aum_usd=3200000, platform_score=80.0,
        data_confirmed=True, data_source_url="https://binance.com/trader/0xpro4",
        data_freshness_hours=1,
        signal_timestamp=datetime(2026, 11, 12, 10, 4, 0, tzinfo=utc),
    )

    # إحصائيات
    stats = engine.get_statistics()
    print(f"\nإحصائيات الإشارات:")
    print(f"  الإجمالي: {stats['total_signals']}")
    print(f"  متداولين فريدين (بعد إزالة التكرار): {stats['unique_traders']}")
    print(f"  محترفون نشطون: {stats['professional_active']}")
    print(f"  محترفون غير نشطين: {stats['professional_inactive']}")
    print(f"  عاديون: {stats['regular']}")
    print(f"  مشبوهون: {stats['suspicious']}")
    print(f"  الأزواج: {stats['pairs']}")
    print(f"  المنصات: {stats['platforms']}")
    print(f"  أنواع السوق: {stats['market_types']}")

    # التوصيات
    print(f"\n--- التوصيات ---")
    recs = engine.generate_recommendations()
    for rec in recs:
        print(f"\n{rec.pair} {rec.direction} | {rec.market_type}")
        print(f"  الثقة: {rec.confidence_score}/100 ({rec.confidence_level})")
        print(f"  وسط الدخول: {rec.entry_mean} | الستوب: {rec.stop_loss} | الهدف: {rec.take_profit} | الرافعة: {rec.leverage}")
        print(f"  محترفون نشطون: {rec.n_professional_active} | إشارات إجمالية: {rec.n_total_signals}")
        print(f"  المصادر: {', '.join(rec.sources)}")
        print(f"  بيانات مؤكدة: {rec.data_confirmed_count}")
        print(f"  الإشارة أُطلقت: {rec.signal_emitted}")
        print(f"  التوصية: {rec.recommendation_text}")

    print(f"\n--- أفضل توصية (تيليجرام) ---")
    best = engine.get_best_recommendation()
    if best:
        print(best.telegram_message)


if __name__ == "__main__":
    demo()