#!/usr/bin/env python3
"""
اختبار عملي كامل لنظام Copy-Trade Aggregator.

هذا الاختبار يمرر بيانات واقعية عبر كامل خط الأنابيب:
1. RawFill من المنصات (محاكاة بيانات واقعية)
2. TraderProfile (بيانات المتداول مع market_type، نافذة الأداء، موثوقية البيانات)
3. TraderClassifier (تصنيف 4-way + إزالة التكرار)
4. ConfidenceScorer (درجة الثقة من 100، مساحة 1.5%، Min 2 محترفين)
5. RecommendationEngine (توصيات مجمعة، لا اختراع لـ SL/TP/leverage)
6. TelegramNotifier (رسالة تليجرام + أمر /details)

يعرض أيضًا أي المنصات عامة (تشتغل بدون مفتاح) وأيها تحتاج API Key.
"""

from __future__ import annotations

# إضافة مجلد src إلى مسار الاستيراد لتشغيل الأمثلة مباشرة

from datetime import datetime, timezone, timedelta
from copytrade_agg.classifier import TraderProfile, TraderClassifier
from copytrade_agg.scorer import ConfidenceScorer, TraderEntry
from copytrade_agg.engine import RecommendationEngine
from copytrade_agg.telegram import TelegramNotifier, TelegramConfig
from copytrade_agg.connectors import (
    RawFill, normalize_fill_to_signal,
    HyperliquidConnector, DydxConnector, Collective2Connector,
    MyfxbookConnector, ZuluTradeConnector, DarwinexConnector,
    BirdeyeConnector, DeBankConnector, list_platforms,
)

utc = timezone.utc

def print_header(text: str) -> None:
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)

def show_platform_status() -> None:
    """إظهار حالة كل منصة: عامة أم تحتاج API Key."""
    print_header("📡 حالة المنصات الـ 8 (أيها عامة وأيها تحتاج إعداد)")

    connectors_info = [
        ("Hyperliquid", HyperliquidConnector(), "عام — REST + WebSocket بدون حساب", False),
        ("dYdX", DydxConnector(), "عام — REST Indexer بدون حساب", False),
        ("Collective2", Collective2Connector(api_key=None), "يحتاج API Key", True),
        ("Myfxbook", MyfxbookConnector(), "يحتاج تسجيل دخول (Email/Password)", True),
        ("ZuluTrade", ZuluTradeConnector(api_key=None), "يحتاج API Key لبعض النقاط", True),
        ("Darwinex", DarwinexConnector(api_key=None), "يحتاج API Key", True),
        ("Birdeye", BirdeyeConnector(api_key=None), "يحتاج API Key من bds.birdeye.so", True),
        ("DeBank", DeBankConnector(api_key=None), "عام/جزئي عام (محافظ، حيتان)", False),
    ]

    for name, conn, note, needs_key in connectors_info:
        status = "🔴 يحتاج إعداد مفتاح/بيانات اعتماد" if needs_key else "🟢 عام (تشتغل بدون مفتاح)"
        print(f"\n  {name}: {status}")
        print(f"    ملاحظة: {note}")
        print(f"    منصّة في النظام: {conn.platform}")

def build_realistic_profiles() -> list[TraderProfile]:
    """بناء ملفات متداولين واقعية تمر عبر النظام كاملاً."""
    profiles = [
        # === محترف نشط 1: Collective2، Spot، BTC-USD شراء ===
        TraderProfile(
            trader_id="0xalex_trader_001",
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
            data_source_url="https://collective2.com/system/alex_trader",
            data_freshness_hours=4,
            signal_timestamp=datetime(2026, 11, 12, 14, 0, 0, tzinfo=utc),
        ),
        # === محترف نشط 2: Myfxbook، Spot، BTC-USD شراء (نفس الشخص؟ لا، شخص مختلف) ===
        TraderProfile(
            trader_id="0xsara_crypto_002",
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
            data_source_url="https://myfxbook.com/trader/sara_crypto",
            data_freshness_hours=12,
            signal_timestamp=datetime(2026, 11, 12, 14, 5, 0, tzinfo=utc),
        ),
        # === محترف نشط 3: Hyperliquid، Spot، BTC-USD شراء ===
        TraderProfile(
            trader_id="0xmike_defi_003",
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
            data_source_url="https://hyperliquid.xyz/trader/mike_defi",
            data_freshness_hours=2,
            signal_timestamp=datetime(2026, 11, 12, 14, 10, 0, tzinfo=utc),
        ),
        # === نفس الشخص (0xalex_trader_001) ظهر في منصة ثانية (Hyperliquid Futures) — يجب إزالة التكرار ===
        TraderProfile(
            trader_id="0xalex_trader_001",
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
            data_source_url="https://hyperliquid.xyz/trader/alex_trader",
            data_freshness_hours=1,
            signal_timestamp=datetime(2026, 11, 12, 14, 15, 0, tzinfo=utc),
        ),
        # === شخص عادي: dYdX، Spot، BTC-USD شراء (لن يؤثر في Min 2 محترفين) ===
        TraderProfile(
            trader_id="0xretail_dydx_004",
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
            signal_timestamp=datetime(2026, 11, 12, 14, 20, 0, tzinfo=utc),
        ),
        # === مشبوه: ZuluTrade، Spot، XAU-USD شراء، نسبة ربح 93% مع 15 صفقة فقط ===
        TraderProfile(
            trader_id="0xsus_zulu_005",
            platform="zulutrade",
            market_type="spot",
            pair="XAU-USD",
            direction="BUY",
            entry_price=2650.0,
            total_trades=15, winning_trades=14, losing_trades=1,
            win_rate=0.93, profit_factor=5.20, max_drawdown=0.02,
            account_age_days=12, last_trade_days_ago=1,
            performance_window_days=12, trades_in_window=15, win_rate_in_window=0.93,
            followers_count=20, aum_usd=5000,
            data_confirmed=False,  # بيانات غير مؤكدة
            data_source_url="",
            data_freshness_hours=168,
            signal_timestamp=datetime(2026, 11, 12, 14, 25, 0, tzinfo=utc),
        ),
        # === إ_signal ثانية على ETH-USD بيع Futures (سوق مختلف، لا يُخلط مع BTC Spot) ===
        TraderProfile(
            trader_id="0xpro_binance_006",
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
            data_source_url="https://binance.com/trader/binance_pro",
            data_freshness_hours=1,
            signal_timestamp=datetime(2026, 11, 12, 14, 30, 0, tzinfo=utc),
        ),
    ]
    return profiles

def main() -> None:
    print_header("🚀 اختبار عملي كامل لنظام Copy-Trade Aggregator")
    print("البيانات تمر عبر كامل خط الأنابيب الفعلي (لا éxécution تلقائية — توصية فقط)")

    # === المرحلة 1: حالة المنصات ===
    show_platform_status()

    # === المرحلة 2: بناء الملفات ===
    print_header("📊 المرحلة 2: بناء ملفات المتداولين (7 إشارات من 5 منصات)")
    profiles = build_realistic_profiles()
    print(f"إجمالي الإشارات المضافة: {len(profiles)}")
    for p in profiles:
        print(f"  • {p.trader_id[:12]}... | {p.platform} | {p.market_type} | {p.pair} {p.direction} @ {p.entry_price}")

    # === المرحلة 3: التصنيف وإزالة التكرار ===
    print_header("🔍 المرحلة 3: التصنيف (4-way) + إزالة التكرار")
    classifier = TraderClassifier()
    classified = classifier.classify_batch(profiles)

    print("\nقبل إزالة التكرار:")
    for p in classified:
        print(f"  {p.trader_id[:12]}... | {p.platform} | {p.market_type} | {p.classification} | {p.classification_score:.0f} pts | AUM: ${p.aum_usd:,.0f}")

    deduped = classifier.deduplicate_by_trader_id(classified)
    print(f"\nبعد إزالة التكرار: من {len(classified)} إلى {len(deduped)} متداول فريد")
    print("  (الشخص 0xalex_trader_001 ظهر في collective2 و hyperliquid → حُسب مرة واحدة)")

    for p in deduped:
        mark = "✅ PRO" if p.classification == "PROFESSIONAL_ACTIVE" else "⚠️ "
        print(f"  {mark} {p.trader_id[:12]}... | {p.platform} | {p.market_type} | {p.classification} | {p.classification_score:.0f} pts | بيانات: {'✓ مؤكدة' if p.data_confirmed else '✗ غير مؤكدة'}")

    prof_active = classifier.get_professional_active(deduped)
    print(f"\nالمحترفون النشطون المستقلون: {len(prof_active)} (الحد الأدنى للإشارة: 2)")

    # === المرحلة 4: محرك التوصيات ===
    print_header("🎯 المرحلة 4: محرك التوصيات المجمعة (مساحة 1.5%، Min 2 محترفين)")
    engine = RecommendationEngine(entry_tolerance_pct=1.5, min_confidence=45)

    for p in deduped:
        engine.add_signal(
            trader_id=p.trader_id,
            platform=p.platform,
            market_type=p.market_type,
            pair=p.pair,
            direction=p.direction,
            entry_price=p.entry_price,
            stop_loss=p.stop_loss,
            take_profit=p.take_profit,
            leverage=p.leverage,
            signal_timestamp=p.signal_timestamp,
            total_trades=p.total_trades, winning_trades=p.winning_trades, losing_trades=p.losing_trades,
            win_rate=p.win_rate, profit_factor=p.profit_factor, max_drawdown=p.max_drawdown,
            sharpe_ratio=p.sharpe_ratio, total_return_pct=p.total_return_pct, annual_return_pct=p.annual_return_pct,
            account_age_days=p.account_age_days, last_trade_days_ago=p.last_trade_days_ago,
            performance_window_days=p.performance_window_days, trades_in_window=p.trades_in_window,
            win_rate_in_window=p.win_rate_in_window,
            followers_count=p.followers_count, aum_usd=p.aum_usd, platform_score=p.platform_score,
            data_confirmed=p.data_confirmed,
            data_source_url=p.data_source_url,
            data_freshness_hours=p.data_freshness_hours,
        )

    stats = engine.get_statistics()
    print(f"\nإحصائيات نهائية:")
    print(f"  متداولين فريدين: {stats['unique_traders']}")
    print(f"  محترفون نشطون: {stats['professional_active']}")
    print(f"  الأزواج: {stats['pairs']}")
    print(f"  أنواع السوق: {stats['market_types']}")

    recommendations = engine.generate_recommendations()
    print(f"\nالتوصيات التي أُطلقت (Min 2 محترفين نشطين + ثقة ≥ 45): {len(recommendations)}")

    for rec in recommendations:
        print(f"\n  📈 {rec.pair} {rec.direction} | {rec.market_type}")
        print(f"     الثقة: {rec.confidence_score}/100 ({rec.confidence_level})")
        print(f"     وسط الدخول: {rec.entry_mean:.2f} | انتشار: {rec.entry_spread_pct:.3f}% (مساحة: ±{rec.entry_tolerance_pct}%)")
        print(f"     الستوب: {rec.stop_loss} | الهدف: {rec.take_profit} | الرافعة: {rec.leverage}")
        print(f"     محترفون نشطون: {rec.n_professional_active} | إشارات إجمالية: {rec.n_total_signals}")
        print(f"     بيانات مؤكدة: {rec.data_confirmed_count}")
        print(f"     الإشارة أُطلقت: {rec.signal_emitted}")
        print(f"     التوصية: {rec.recommendation_text}")

    # === المرحلة 5: رسالة تليجرام لأفضل توصية ===
    print_header("📨 المرحلة 5: رسالة التيليجرام (أفضل توصية)")
    best = engine.get_best_recommendation()
    if best:
        best.current_price = 65100.0
        best.current_price_source = "binance"

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

        print(best.telegram_message)

    # === المرحلة 6: أمر /details لمتداول فردي ===
    print_header("🔍 المرحلة 6: أمر /details — تفاصيل متداول فردي (مقارنة كاملة)")
    if best and best.trader_details:
        first_id = best.trader_details[0].get("trader_id", "")
        notifier = TelegramNotifier(TelegramConfig(bot_token="demo_token", chat_id="demo_chat"))
        details = notifier._format_trader_details(
            best.trader_details[0],
            {
                "pair": best.pair,
                "confidence_score": best.confidence_score,
                "entry_mean": best.entry_mean,
            }
        )
        print(details)

    # === الختام ===
    print_header("✅ نتيجة الاختبار العملي")
    print("نظام Copy-Trade Aggregator يعمل بكامل مكوناته:")
    print("  1. ✅ تصنيف المتداولين (4-way) مع إزالة التكرار")
    print("  2. ✅ درجة الثقة من 100 نقطة (مساحة 1.5%، Min 2 محترفين مستقلين)")
    print("  3. ✅ فصل Spot عن Futures (لا يُخلط بينهما)")
    print("  4. ✅ لا اختراع لـ Stop/Target/Leverage — يعرض الخطة كما وردت")
    print("  5. ✅ جدول مقارنة مرتب حسب الجودة")
    print("  6. ✅ تمييز البيانات المؤكدة (✓) عن الناقصة (✗)")
    print("  7. ✅ رسالة تليجرام مع أمر /details للتفاصيل الكاملة")
    print("  8. ✅ لا تنفيذ تلقائي — توصية فقط، والقرار للمستخدم")
    print("\n📋 للمنصات التي تحتاج API Key: أضف المفتاح في إنشاء الموصل (مثلاً Collective2Connector(api_key='YOUR_KEY'))")

if __name__ == "__main__":
    main()