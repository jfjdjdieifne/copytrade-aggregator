#!/usr/bin/env python3
"""
Run script for Copy-Trade Aggregator (copytrade-aggregator).

هذا السكربت يشغل خط الأنابيب الكامل للمجمع وفق المواصفات الجديدة:
1. يبدأ بالكريبتو، ويفصل Spot عن Futures
2. مسafe دخول 1.5% لاعتبار الدخولات متقاربة
3. الشخص نفسه يُحتسب مرة واحدة عبر المنصات (إزالة التكرار)
4. Min 2 محترفين مستقلين متفقين لإطلاق الإشارة
5. يعرض دخول كل محترف كما ورد، ويحسب الوسط الحسابي كملخص (لا سعر مخترع)
6. لا يخترع ستوب/هدف/رافعة — يعرض الخطة كما وردت من المصدر
7. جدول مقارنة مرتب حسب الجودة
8. تليجرام: ملخص + أمر /details للمقارنة الكاملة
9. يميز البيانات المؤكدة عن الناقصة

لا تنفيذ تلقائي — توصية فقط، والقرار للم usuário بعد المراجعة.

ملاحظة: هذا السكربت يُسلم إلى git قبل التشغيل (راجع القيد R2).
"""

from __future__ import annotations

from datetime import datetime, timezone

# يتطلب تثبيت الحزمة أولاً: pip install -e .

from copytrade_agg.classifier import TraderProfile
from copytrade_agg.scorer import ConfidenceScorer
from copytrade_agg.engine import RecommendationEngine
from copytrade_agg.telegram import TelegramNotifier, TelegramConfig

def build_demo_signals(engine: RecommendationEngine) -> None:
    """بناء إشارات توضيحية من منصات متعددة (كريبتو، Spot/Futures)."""
    utc = timezone.utc

    # --- BTC-USD شراء Spot: 3 محترفين نشطين + شخص عادي ---
    engine.add_signal(
        trader_id="0xpro_collective2_001",
        platform="collective2",
        market_type="spot",
        pair="BTC-USD",
        direction="BUY",
        entry_price=65000.0,
        stop_loss=63000.0,
        take_profit=69000.0,
        leverage=1.0,
        total_trades=450, winning_trades=270, losing_trades=180,
        win_rate=0.60, profit_factor=1.85, max_drawdown=0.12,
        sharpe_ratio=1.45, total_return_pct=45.5, annual_return_pct=18.2,
        account_age_days=730, last_trade_days_ago=2,
        performance_window_days=270, trades_in_window=300, win_rate_in_window=0.59,
        followers_count=1200, aum_usd=4500000, platform_score=85.0,
        data_confirmed=True,
        data_source_url="https://collective2.com/system/0xpro_collective2_001",
        data_freshness_hours=4,
        signal_timestamp=datetime(2026, 11, 12, 10, 0, 0, tzinfo=utc),
    )
    engine.add_signal(
        trader_id="0xpro_myfxbook_002",
        platform="myfxbook",
        market_type="spot",
        pair="BTC-USD",
        direction="BUY",
        entry_price=65200.0,
        stop_loss=63200.0,
        take_profit=69200.0,
        leverage=1.0,
        total_trades=320, winning_trades=192, losing_trades=128,
        win_rate=0.60, profit_factor=1.65, max_drawdown=0.15,
        sharpe_ratio=1.20, total_return_pct=38.0, annual_return_pct=12.5,
        account_age_days=540, last_trade_days_ago=5,
        performance_window_days=240, trades_in_window=200, win_rate_in_window=0.58,
        followers_count=800, aum_usd=2800000, platform_score=78.0,
        data_confirmed=True,
        data_source_url="https://myfxbook.com/trader/0xpro_myfxbook_002",
        data_freshness_hours=12,
        signal_timestamp=datetime(2026, 11, 12, 10, 1, 0, tzinfo=utc),
    )
    engine.add_signal(
        trader_id="0xpro_hyperliquid_003",
        platform="hyperliquid",
        market_type="spot",
        pair="BTC-USD",
        direction="BUY",
        entry_price=64900.0,
        stop_loss=62900.0,
        take_profit=68900.0,
        leverage=2.0,
        total_trades=280, winning_trades=168, losing_trades=112,
        win_rate=0.60, profit_factor=1.55, max_drawdown=0.18,
        sharpe_ratio=1.10, total_return_pct=32.0, annual_return_pct=14.0,
        account_age_days=365, last_trade_days_ago=1,
        performance_window_days=200, trades_in_window=180, win_rate_in_window=0.59,
        followers_count=450, aum_usd=1200000, platform_score=72.0,
        data_confirmed=True,
        data_source_url="https://hyperliquid.xyz/trader/0xpro_hyperliquid_003",
        data_freshness_hours=2,
        signal_timestamp=datetime(2026, 11, 12, 10, 2, 0, tzinfo=utc),
    )
    # شخص عادي على نفس الزوج (لن يؤثر في حد الإطلاق، أقل من Min 2 محترفين seuls ne comptent pas)
    engine.add_signal(
        trader_id="0xreg_dydx_004",
        platform="dydx",
        market_type="spot",
        pair="BTC-USD",
        direction="BUY",
        entry_price=65500.0,
        total_trades=45, winning_trades=23, losing_trades=22,
        win_rate=0.51, profit_factor=1.10, max_drawdown=0.08,
        account_age_days=90, last_trade_days_ago=3,
        performance_window_days=90, trades_in_window=30, win_rate_in_window=0.50,
        followers_count=30, aum_usd=80000,
        data_confirmed=True,
        data_freshness_hours=24,
        signal_timestamp=datetime(2026, 11, 12, 10, 3, 0, tzinfo=utc),
    )

    # --- ETH-USD بيع Futures (سوق مختلف، لا يُخلط مع BTC Spot) ---
    engine.add_signal(
        trader_id="0xpro_binance_005",
        platform="binance",
        market_type="futures",
        pair="ETH-USD",
        direction="SELL",
        entry_price=3400.0,
        stop_loss=3500.0,
        take_profit=3200.0,
        leverage=3.0,
        total_trades=380, winning_trades=228, losing_trades=152,
        win_rate=0.60, profit_factor=1.70, max_drawdown=0.14,
        sharpe_ratio=1.30, total_return_pct=41.0, annual_return_pct=16.0,
        account_age_days=650, last_trade_days_ago=1,
        performance_window_days=300, trades_in_window=250, win_rate_in_window=0.59,
        followers_count=950, aum_usd=3200000, platform_score=80.0,
        data_confirmed=True,
        data_source_url="https://binance.com/trader/0xpro_binance_005",
        data_freshness_hours=1,
        signal_timestamp=datetime(2026, 11, 12, 10, 4, 0, tzinfo=utc),
    )

    # --- نفس المتداول ظهر في منصتين (اختبار إزالة التكرار) ---
    engine.add_signal(
        trader_id="0xpro_collective2_001",  # نفس الـ ID كما في الإشارة الأولى
        platform="hyperliquid",
        market_type="futures",
        pair="BTC-USD",
        direction="BUY",
        entry_price=65100.0,
        stop_loss=63100.0,
        take_profit=69100.0,
        leverage=3.0,
        total_trades=400, winning_trades=240, losing_trades=160,
        win_rate=0.60, profit_factor=1.70, max_drawdown=0.14,
        sharpe_ratio=1.30, total_return_pct=42.0, annual_return_pct=17.0,
        account_age_days=700, last_trade_days_ago=1,
        performance_window_days=300, trades_in_window=280, win_rate_in_window=0.59,
        followers_count=900, aum_usd=3800000, platform_score=82.0,
        data_confirmed=True,
        data_source_url="https://hyperliquid.xyz/trader/0xpro_collective2_001",
        data_freshness_hours=1,
        signal_timestamp=datetime(2026, 11, 12, 10, 5, 0, tzinfo=utc),
    )

def main() -> None:
    print("=" * 70)
    print("Copy-Trade Aggregator — Run Script (مجمع نسخ الصفقات)")
    print("=" * 70)
    print("المواصفات: كريبتو أولاً، Spot/Futures منفصلان، مساحة 1.5%، Min 2 محترفين مستقلين")
    print("لا تنفيذ تلقائي — توصية فقط، والقرار للمستخدم بعد المراجعة.")
    print("=" * 70)

    # 1. إنشاء المحرك
    engine = RecommendationEngine(
        entry_tolerance_pct=1.5,
        min_confidence=45,
    )

    # 2. إضافة الإشارات التوضيحية
    build_demo_signals(engine)

    # 3. إحصائيات
    stats = engine.get_statistics()
    print("\n📊 إحصائيات الإشارات المضافة:")
    print(f"   الإجمالي: {stats['total_signals']}")
    print(f"   متداولين فريدين (بعد إزالة التكرار): {stats['unique_traders']}")
    print(f"   محترفون نشطون: {stats['professional_active']}")
    print(f"   محترفون غير نشطين: {stats['professional_inactive']}")
    print(f"   عاديون: {stats['regular']}")
    print(f"   مشبوهون: {stats['suspicious']}")
    print(f"   الأزواج: {stats['pairs']}")
    print(f"   المنصات: {stats['platforms']}")
    print(f"   أنواع السوق: {stats['market_types']}")

    # 4. توليد التوصيات
    print("\n📈 التوصيات المجمعة:")
    recommendations = engine.generate_recommendations()
    for rec in recommendations:
        print(f"\n   {rec.pair} {rec.direction} | {rec.market_type}")
        print(f"      الثقة: {rec.confidence_score}/100 ({rec.confidence_level})")
        print(f"      وسط الدخول: {rec.entry_mean} | الستوب: {rec.stop_loss} | الهدف: {rec.take_profit} | الرافعة: {rec.leverage}")
        print(f"      محترفون نشطون: {rec.n_professional_active} (الحد الأدنى: 2)")
        print(f"      إشارات إجمالية: {rec.n_total_signals}")
        print(f"      المصادر: {', '.join(rec.sources)}")
        print(f"      بيانات مؤكدة: {rec.data_confirmed_count}")
        print(f"      الإشارة أُطلقت: {rec.signal_emitted}")
        print(f"      التوصية: {rec.recommendation_text}")

    # 5. أفضل توصية
    best = engine.get_best_recommendation()
    if best:
        # إضافة السعر الحالي للعرض (comes from external price feed in production)
        best.current_price = 65100.0
        best.current_price_source = "binance"
        # إعادة صياغة رسالة التيليجرام مع السعر الحالي
        from copytrade_agg.scorer import ConfidenceScorer
        scorer = ConfidenceScorer(entry_tolerance_pct=1.5)
        best.telegram_message = scorer.format_telegram_message(
            type('CR', (), {
                'total_score': best.confidence_score,
                'confidence_level': best.confidence_level,
                'n_professional_active': best.n_professional_active,
                'n_professional_inactive': best.n_professional_inactive,
                'n_regular': best.n_regular,
                'n_suspicious': best.n_suspicious,
                'trader_entries': [type('TE', (), d)() for d in best.trader_details],
                'entry_mean': best.entry_mean,
                'entry_spread_pct': best.entry_spread_pct,
                'entry_tolerance_pct': best.entry_tolerance_pct,
                'sources': best.sources,
                'market_types': [best.market_type],
                'data_confirmed_count': best.data_confirmed_count,
                'reasons': [],
                'recommendation': best.recommendation_text,
                'signal_emitted': best.signal_emitted,
            })(),
            best.pair, best.direction, best.market_type,
            current_price=best.current_price,
            current_price_source=best.current_price_source,
        )

        print("\n" + "=" * 70)
        print("📨 أفضل توصية — رسالة التيليجرام:")
        print("=" * 70)
        print(best.telegram_message)

    # 6. عرض تفاصيل متداول فردي (/details)
    print("\n" + "=" * 70)
    print("🔍 أمر /details — تفاصيل متداول فردي:")
    print("=" * 70)
    if best and best.trader_details:
        first_trader_id = best.trader_details[0].get("trader_id", "")
        notifier = TelegramNotifier(
            TelegramConfig(bot_token="demo_token", chat_id="demo_chat")
        )
        details_text = notifier._format_trader_details(
            best.trader_details[0],
            {
                "pair": best.pair,
                "confidence_score": best.confidence_score,
                "entry_mean": best.entry_mean,
            }
        )
        print(details_text)

    print("\n" + "=" * 70)
    print("اكتمل التشغيل (لا مكالمات API حقيقية تمت، لا تنفيذ تلقائي).")
    print("=" * 70)

if __name__ == "__main__":
    main()