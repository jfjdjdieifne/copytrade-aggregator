"""
Confidence Scorer — نظام حساب الثقة من 100 نقطة.

يقوم بحساب درجة الثقة للإشارة المجمعة بناءً على:
1. عدد المتداولين المحترفين والنشطين المتفقين على نفس الصفقة (Min 2 مستقلين)
2. جودة كل متداول (تصنيفه من TraderClassifier)
3. قرب مستويات الدخول منهم ضمن مساحة 1.5% (التوافق)
4. جودة مصدر البيانات (مؤكدة أم ناقصة)

النتيجة: عدد من 0 إلى 100 نقطة.

مواصفات التحديث الجديد:
- مساحة دخول 1.5% (بلايين/n1.5%) لاعتبار الدخولات متقاربة
- الشخص نفسه يُحتسب مرة واحدة حتى لو ظهر في منصات متعددة
- Min 2 محترفين مستقلين متفقين لإطلاق الإشارة
- عرض وسط الدخولات المتقاربة كملخص (ليس سعرًا مخترعًا من البوت)
- فصل spot عن futures (لا يُخلط بينهما)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Optional

from .classifier import TraderProfile, TraderClassifier


@dataclass
class TraderEntry:
    """مدخل دخول واحد من متداول — يُعرض كما ورد من المصدر، لا يُعدل."""
    trader_id: str
    platform: str
    market_type: str          # "spot" أو "futures"
    pair: str
    direction: str            # "BUY" أو "SELL"
    entry_price: float        # مستوى الدخول كما ذكره المتداول
    stop_loss: Optional[float] = None   # كما ورد، لا يُخترع
    take_profit: Optional[float] = None   # كما ورد، لا يُخترع
    leverage: Optional[float] = None      # كما ورد، لا يُخترع
    classification: str = ""              # PROFESSIONAL_ACTIVE / INACTIVE / REGULAR / SUSPICIOUS
    classification_score: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    total_trades: int = 0
    account_age_days: int = 0
    last_trade_days_ago: int = 999
    trades_in_window: int = 0          # عدد الصفقات في نافذة الأداء 6–12 شهر
    win_rate_in_window: float = 0.0    # نسبة الربح في نافذة الأداء
    followers_count: int = 0
    aum_usd: float = 0.0
    data_confirmed: bool = True
    data_source_url: str = ""
    data_freshness_hours: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class ConfidenceResult:
    """نتيجة حساب الثقة."""
    total_score: int                 # 0-100
    confidence_level: str            # VERY_HIGH, HIGH, MEDIUM, LOW, INSUFFICIENT
    n_professional_active: int       # عدد المتداولين المحترفين والنشطين المتفقين (مستقلين)
    n_professional_inactive: int     # عدد المحترفين غير النشطين
    n_regular: int                   # عدد المتداولين العاديين
    n_suspicious: int                # عدد المشبوهين
    trader_entries: list[TraderEntry]  # تفاصيل كل متداول (كما ورد)
    entry_mean: float                # الوسيط الحسابي لدخولات المحترفين المتفقين
    entry_spread_pct: float          # انتشار مستويات الدخول بالنسبة المئوية
    entry_tolerance_pct: float       # المساحة المستخدمة (1.5%)
    sources: list[str]               # المصادر المشاركة
    market_types: list[str]          # أنواع السوق (spot/futures) — لا يُخلط
    data_confirmed_count: int        # عدد المتداولين الذين لديهم بيانات مؤكدة
    reasons: list[str]               # أسباب الحساب
    recommendation: str              # توصية نصية بالعربية
    signal_emitted: bool             # هل أُطلقت الإشارة (يتطلب Min 2 PROFESSIONAL_ACTIVE)


class ConfidenceScorer:
    """
    يحسب درجة الثقة من 100 نقطة للإشارة المجمعة.

    الصيغة (مجموع 100 نقطة):
    -----------------------
    1. نقاط المتداولين المحترفين النشطين: 40 نقطة max
       = min(40, عدد_المحترفين_النشطين_المستقلين × 8)
       (كل متداول محترف نشط مستقل يضيف 8 نقاط، الحد الأقصى 5 متداولين)

    2. نقاط جودة المتداولين: 25 نقطة max
       = متوسط (تصنيف كل متداول / 100) × 25

    3. نقاط توافق الدخول: 20 نقطة max
       = 20 × (1 - انتشار_الدخول / المساحة_المسموحة)
       المساحة المسموحة = 1.5%

    4. نقاط جودة المصدر: 15 نقطة max
       = 15 إذا كانت البيانات مؤكدة من جميع المصادر
       = أقل إذا كانت بعض البيانات ناقصة
    """

    WEIGHT_TRADERS = 40      # نقاط المتداولين
    WEIGHT_QUALITY = 25      # نقاط جودة كل متداول
    WEIGHT_ENTRY = 20        # نقاط توافق الدخول
    WEIGHT_SOURCE = 15       # نقاط جودة المصدر

    DEFAULT_ENTRY_TOLERANCE_PCT = 1.5  # مسافة الدخول المسموحة: 1.5%

    THRESHOLD_VERY_HIGH = 80
    THRESHOLD_HIGH = 65
    THRESHOLD_MEDIUM = 45
    THRESHOLD_LOW = 25

    MIN_PROFESSIONAL_ACTIVE_FOR_SIGNAL = 2  # Min 2 محترفين مستقلين لإطلاق الإشارة

    def __init__(self, entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT):
        self.entry_tolerance_pct = entry_tolerance_pct
        self.classifier = TraderClassifier()

    def score(
        self,
        profiles: list[TraderProfile],
        pair: str,
        direction: str,
        market_type: str,
        entry_tolerance_pct: Optional[float] = None,
    ) -> ConfidenceResult:
        """
        يحسب درجة الثقة لمجموعة من المتداولين المتفقين على نفس الصفقة.

        Args:
            profiles: قائمة ملفات المتداولين (قد تحتوي على تكرار لنفس الشخص عبر منصات)
            pair: الزوج المتفق عليه
            direction: الاتجاه المتفق عليه (BUY/SELL)
            market_type: نوع السوق ("spot" أو "futures") — لا يُخلط بين النوعين
            entry_tolerance_pct: مساحة الدخول المسموحة (الافتراضي 1.5%)
        """
        tol = entry_tolerance_pct or self.DEFAULT_ENTRY_TOLERANCE_PCT

        # 1. تصنيف جميع المتداولين
        classified = self.classifier.classify_batch(profiles)

        # 2. إزالة التكرار: نفس الشخص يُحتسب مرة واحدة عبر المنصات
        deduped = self.classifier.deduplicate_by_trader_id(classified)

        # 3. تصفية حسب الزوج والاتجاه ونوع السوق (لا يُخلط spot مع futures)
        matching = [
            p for p in deduped
            if p.pair == pair
            and p.direction.upper() == direction.upper()
            and p.market_type == market_type
        ]

        if not matching:
            return self._empty_result(pair, direction, market_type, tol)

        # 4. فرز حسب التصنيف
        prof_active = self.classifier.get_professional_active(matching)
        prof_inactive = [p for p in matching if p.classification == "PROFESSIONAL_INACTIVE"]
        regular = [p for p in matching if p.classification == "REGULAR"]
        suspicious = [p for p in matching if p.classification == "SUSPICIOUS"]

        n_active = len(prof_active)

        # 5. التحقق من حد الإطلاق: Min 2 محترفين نشطين مستقلين
        signal_emitted = n_active >= self.MIN_PROFESSIONAL_ACTIVE_FOR_SIGNAL

        # 6. نقاط المتداولين المحترفين النشطين (40 نقطة max)
        trader_points = min(self.WEIGHT_TRADERS, n_active * 8)

        # 7. نقاط جودة المتداولين (25 نقطة max)
        if matching:
            avg_quality = sum(p.classification_score for p in matching) / len(matching)
            quality_points = int(round((avg_quality / 100.0) * self.WEIGHT_QUALITY))
        else:
            avg_quality = 0.0
            quality_points = 0

        # 8. نقاط توافق الدخول (20 نقطة max) — ضمن مساحة 1.5%
        entries = [p.entry_price for p in matching if p.entry_price > 0]
        if entries:
            entry_center = mean(entries)
            entry_spread = max(entries) - min(entries)
            entry_spread_pct = (entry_spread / entry_center) * 100 if entry_center > 0 else 0.0

            # التوافق: إذا كان الانتشار ضمن المساحة المسموحة، تُحسب النقاط
            if entry_spread_pct <= tol:
                entry_points = int(round(
                    self.WEIGHT_ENTRY * (1.0 - (entry_spread_pct / (tol * 2)))
                ))
            else:
                entry_points = 0  # الانتشار أكبر من المسموح (1.5%)
        else:
            entry_center = 0.0
            entry_spread_pct = 0.0
            entry_points = 0

        # 9. نقاط جودة المصدر (15 نقطة max)
        source_points = 0
        sources = list(set(p.platform for p in matching))
        data_confirmed_count = sum(1 for p in matching if p.data_confirmed)

        for src in sources:
            # источники معقدة تؤكد البيانات تُعطى نقاط أعلى
            if src in ("collective2", "myfxbook", "darwinex"):
                source_points += 5  # مصادر موثقة ومدققة
            elif src in ("hyperliquid", "dydx"):
                source_points += 3  # مصادر عامة ولكن موثوقة تقنياً
            elif src in ("zulutrade", "birdeye", "debank"):
                source_points += 2  # مصادر اجتماعية، تحتاج التحقق
            else:
                source_points += 1

        # خصم إذا كانت بعض البيانات غير مؤكدة
        if data_confirmed_count < len(matching):
            source_points = max(0, source_points - 5)

        source_points = min(self.WEIGHT_SOURCE, source_points)

        # 10. المجموع النهائي
        total_score = trader_points + quality_points + entry_points + source_points
        total_score = min(100, max(0, total_score))

        # 11. مستوى الثقة
        if total_score >= self.THRESHOLD_VERY_HIGH:
            confidence_level = "VERY_HIGH"
        elif total_score >= self.THRESHOLD_HIGH:
            confidence_level = "HIGH"
        elif total_score >= self.THRESHOLD_MEDIUM:
            confidence_level = "MEDIUM"
        elif total_score >= self.THRESHOLD_LOW:
            confidence_level = "LOW"
        else:
            confidence_level = "INSUFFICIENT"

        # 12. التوصية بالعربية
        recommendation = self._arabic_recommendation(
            confidence_level, n_active, signal_emitted, pair, direction, market_type
        )

        # 13. تفاصيل كل متداول (كما ورد من المصدر، لا تعديل)
        trader_entries = []
        for p in matching:
            trader_entries.append(TraderEntry(
                trader_id=p.trader_id,
                platform=p.platform,
                market_type=p.market_type,
                pair=p.pair,
                direction=p.direction,
                entry_price=p.entry_price,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                leverage=p.leverage,
                classification=p.classification,
                classification_score=p.classification_score,
                win_rate=p.win_rate,
                profit_factor=p.profit_factor,
                max_drawdown=p.max_drawdown,
                total_trades=p.total_trades,
                account_age_days=p.account_age_days,
                last_trade_days_ago=p.last_trade_days_ago,
                followers_count=p.followers_count,
                aum_usd=p.aum_usd,
                data_confirmed=p.data_confirmed,
                data_source_url=p.data_source_url,
                data_freshness_hours=p.data_freshness_hours,
                reasons=p.classification_reasons,
            ))

        # 14. أسباب النتيجة
        reasons = []
        reasons.append(f"متداولين محترفين نشطين مستقلين متفقين: {n_active}")
        reasons.append(f"متداولين محترفين غير نشطين: {len(prof_inactive)}")
        reasons.append(f"متداولين عاديين: {len(regular)}")
        reasons.append(f"مشبوهين: {len(suspicious)}")
        if entries:
            reasons.append(f"انتشار الدخول: {entry_spread_pct:.3f}% (المساحة المسموحة: {tol}%)")
        reasons.append(f"وسط دخول المحترفين: {entry_center:.6f}")
        reasons.append(f"مصادر البيانات: {', '.join(sources)}")
        reasons.append(f"بيانات مؤكدة: {data_confirmed_count} من {len(matching)}")
        reasons.append(f"نوع السوق: {market_type}")
        if not signal_emitted:
            reasons.append(f"الإشارة لم تُطلق (يحتاج Min {self.MIN_PROFESSIONAL_ACTIVE_FOR_SIGNAL} محترفين نشطين)")

        return ConfidenceResult(
            total_score=total_score,
            confidence_level=confidence_level,
            n_professional_active=n_active,
            n_professional_inactive=len(prof_inactive),
            n_regular=len(regular),
            n_suspicious=len(suspicious),
            trader_entries=trader_entries,
            entry_mean=round(entry_center, 6),
            entry_spread_pct=round(entry_spread_pct, 4),
            entry_tolerance_pct=tol,
            sources=sources,
            market_types=list(set(p.market_type for p in matching)),
            data_confirmed_count=data_confirmed_count,
            reasons=reasons,
            recommendation=recommendation,
            signal_emitted=signal_emitted,
        )

    def _empty_result(
        self, pair: str, direction: str, market_type: str, tol: float
    ) -> ConfidenceResult:
        """نتيجة فارغة عندما لا توجد إشارات مطابقة."""
        return ConfidenceResult(
            total_score=0,
            confidence_level="INSUFFICIENT",
            n_professional_active=0,
            n_professional_inactive=0,
            n_regular=0,
            n_suspicious=0,
            trader_entries=[],
            entry_mean=0.0,
            entry_spread_pct=0.0,
            entry_tolerance_pct=tol,
            sources=[],
            market_types=[market_type],
            data_confirmed_count=0,
            reasons=[
                f"لا توجد إشارات مطابقة لـ {pair} {direction} في سوق {market_type}",
                f"الإشارة لم تُطلق (يحتاج Min {self.MIN_PROFESSIONAL_ACTIVE_FOR_SIGNAL} محترفين نشطين)",
            ],
            recommendation=(
                f"لا توجد إشارة ({pair} {direction} في سوق {market_type}) — "
                f"لا يوجد عدد كافٍ من المتداولين المحترفين المتفقين. لا تدخل بناءً على هذه الإشارة."
            ),
            signal_emitted=False,
        )

    def _arabic_recommendation(
        self,
        level: str,
        n_active: int,
        signal_emitted: bool,
        pair: str,
        direction: str,
        market_type: str,
    ) -> str:
        """توليد توصية نصية بالعربية."""
        ar_direction = "شراء 📈" if direction.upper() == "BUY" else "بيع 📉"
        ar_market = "سوق فوري (Spot)" if market_type == "spot" else "سوق عقود آجلة (Futures)"

        if not signal_emitted:
            return (
                f"لا توجد إشارة ({pair} {ar_direction} في {ar_market}) — "
                f"يحتاج Min {self.MIN_PROFESSIONAL_ACTIVE_FOR_SIGNAL} محترفين نشطين مستقلين متفقين. لا تدخل."
            )

        if level == "VERY_HIGH":
            return (
                f"إشارة قوية جداً ({pair} {ar_direction} في {ar_market}) — "
                f"{n_active} متداول محترف نشط مستقل متفقون. الثقة عالية جداً، يمكن النظر في الدخول مع إدارة مخاطر مناسبة."
            )
        elif level == "HIGH":
            return (
                f"إشارة جيدة ({pair} {ar_direction} في {ar_market}) — "
                f"{n_active} متداول محترف نشط مستقل متفقون. الثقة عالية، يفضل الانتظار لتأكيد السعر قبل الدخول."
            )
        elif level == "MEDIUM":
            return (
                f"إشارة متوسطة ({pair} {ar_direction} في {ar_market}) — "
                f"{n_active} متداول محترف نشط مستقل متفقون. الثقة متوسطة، يفضل مراقبة السعر وعدم الدخول العاجل."
            )
        elif level == "LOW":
            return (
                f"إشارة ضعيفة ({pair} {ar_direction} في {ar_market}) — "
                f"عدد محدود من المتداولين المتفقين. الثقة منخفضة، لا ينصح بالدخول بناءً على هذه الإشارة وحدها."
            )
        else:
            return (
                f"إشارة غير كافية ({pair} {ar_direction} في {ar_market}) — "
                f"لا يوجد عدد كافي من المتداولين المحترفين المتفقين. لا تدخل بناءً على هذه الإشارة."
            )

    def format_telegram_message(
        self,
        result: ConfidenceResult,
        pair: str,
        direction: str,
        market_type: str,
        current_price: Optional[float] = None,
        current_price_source: str = "",
    ) -> str:
        """تنسيق الرسالة لإرسالها إلى التيليجرام بالعربية."""
        ar_direction = "شراء 📈" if direction.upper() == "BUY" else "بيع 📉"
        ar_market = "Spot" if market_type == "spot" else "Futures"

        lines = [
            "🔔 *إشارة نسخ صفقات مجمعة (توصية فقط — لا تنفيذ تلقائي)*",
            "",
            f"*الزوج:* `{pair}`",
            f"*الاتجاه:* {ar_direction}",
            f"*نوع السوق:* {ar_market}",
            f"*مستوى الدخول المجمّع (وسيط دخول المحترفين):* `{result.entry_mean:.6f}`",
            f"*مساحة الدخول:* `±{result.entry_tolerance_pct}%`",
            f"*انتشار الدخول الفعلي:* `{result.entry_spread_pct:.3f}%`",
        ]

        if current_price is not None:
            lines.append(f"*السعر الحالي:* `{current_price:.6f}` ({current_price_source})")

        lines.extend([
            f"*درجة الثقة:* `{result.total_score}/100` ({result.confidence_level})",
            f"*متداولين محترفين نشطين مستقلين:* `{result.n_professional_active}` (الحد الأدنى: 2)",
            f"*متداولين محترفين غير نشطين:* `{result.n_professional_inactive}`",
            f"*متداولين عاديين:* `{result.n_regular}`",
            f"*مشبوهين:* `{result.n_suspicious}`",
            f"*بيانات مؤكدة:* `{result.data_confirmed_count} من {len(result.trader_entries)}`",
        ])

        if not result.signal_emitted:
            lines.extend([
                "",
                "⚠️ *الإشارة لم تُطلق* — لا يوجد عدد كافٍ من المحترفين النشطين المستقلين.",
            ])
        else:
            lines.extend([
                "",
                "*تفاصيل المتداولين المحترفين (كما وردت من المصدر):*",
            ])

            # ترتيب حسب الدرجة نزولياً
            sorted_entries = sorted(
                result.trader_entries,
                key=lambda t: t.classification_score,
                reverse=True,
            )

            for t in sorted_entries[:10]:
                status = "✅" if t.classification == "PROFESSIONAL_ACTIVE" else "⚠️"
                confirmed = "✓" if t.data_confirmed else "✗"
                lines.append(
                    f"{status} `{t.trader_id[:10]}...` | {t.platform} | {t.market_type} | "
                    f"{t.classification} | نقاط: {t.classification_score:.0f}/100 | "
                    f"الدخول: `{t.entry_price:.6f}` | WR: {t.win_rate:.1%} | PF: {t.profit_factor:.2f} | "
                    f"DD: {t.max_drawdown:.1%} | صفقات: {t.total_trades} | بيانات: {confirmed}"
                )
                if t.stop_loss is not None:
                    lines.append(f"    🛑 ستوب: `{t.stop_loss:.6f}` | 🎯 هدف: `{t.take_profit:.6f}`" + (
                        f" | رافعة: {t.leverage}x" if t.leverage else ""
                    ))

        lines.extend([
            "",
            "*توصية:* " + result.recommendation,
            "",
            "⚠️ *إشعار:* هذه إشارة افتراضية (HYPOTHETICAL_) وليست نصيحة استثمارية. "
            "البوت لا ينفذ صفقات تلقائياً — القرارك أنت بعد المراجعة.",
        ])

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# مثال توضيحي
# ---------------------------------------------------------------------------

def demo() -> None:
    """تشغيل توضيحي لنظام الثقة."""
    print("=" * 70)
    print("Confidence Scorer — Demo (نظام الثقة من 100 نقطة)")
    print("=" * 70)

    from .classifier import TraderProfile

    scorer = ConfidenceScorer(entry_tolerance_pct=1.5)

    # ثلاثة محترفين نشطين متفقين على شراء BTC-USD في سوق spot
    profiles = [
        TraderProfile(
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
        ),
        TraderProfile(
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
        ),
        TraderProfile(
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
        ),
        # شخص عادي (لن يؤثر في حد الإطلاق)
        TraderProfile(
            trader_id="0xreg1", platform="dydx", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65500.0,
            total_trades=45, winning_trades=23, losing_trades=22,
            win_rate=0.51, profit_factor=1.10, max_drawdown=0.08,
            account_age_days=90, last_trade_days_ago=3,
            performance_window_days=90, trades_in_window=30, win_rate_in_window=0.50,
            followers_count=30, aum_usd=80000,
            data_confirmed=True, data_freshness_hours=24,
        ),
    ]

    result = scorer.score(profiles, "BTC-USD", "BUY", "spot")

    print(f"\nالزوج: BTC-USD | الاتجاه: شراء | السوق: Spot")
    print(f"الإشارة أُطلقت: {result.signal_emitted}")
    print(f"درجة الثقة: {result.total_score}/100 ({result.confidence_level})")
    print(f"المحترفون النشطون المستقلون: {result.n_professional_active}")
    print(f"وسط الدخول: {result.entry_mean:.2f} | انتشار الدخول: {result.entry_spread_pct:.4f}% (المساحة: {result.entry_tolerance_pct}%)")
    print(f"بيانات مؤكدة: {result.data_confirmed_count} من {len(result.trader_entries)}")
    print(f"\nالتوصية: {result.recommendation}")
    print(f"\nأسباب النتيجة:")
    for r in result.reasons:
        print(f"  - {r}")

    print(f"\n--- رسالة التيليجرام ---")
    print(scorer.format_telegram_message(
        result, "BTC-USD", "BUY", "spot",
        current_price=65100.0,
        current_price_source="binance",
    ))


if __name__ == "__main__":
    demo()