"""
Trader Classifier — تصنيف المتداولين المحترفين والناشطين.

هذا الملف يحدد معايير تصنيف المتداولين إلى:
- PROFESSIONAL_ACTIVE: متداول محترف ونشط (يؤثر في درجة الثقة)
- PROFESSIONAL_INACTIVE: متداول محترف ولكن غير نشط حالياً
- REGULAR: متداول عادي
- SUSPICIOUS: متداول مشبوه (نتائج غير واقعية)

معايير التصنيف مبنية على الأبحاث الفعلية لكل منصة (Hyperliquid، Collective2، Myfxbook، ZuluTrade، Darwinex، إلخ).

التحديثات وفق المواصفات الجديدة:
- market_type: spot أو futures (يُفصل بينهما)
- performance_window_days: نافذة الأداء 6–12 شهر (180–365 يوماً)
- حجم المحفظة (AUM) وحده لا يجعل الشخص محترفاً
- data_confirmed: هل البيانات مؤكدة من المصدر أم ناقصة/مقدرة
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

UTC = timezone.utc

# نافذة الأداء المقبولة للمحترف (6–12 شهر)
MIN_PERFORMANCE_WINDOW_DAYS = 180   # 6 أشهر
MAX_PERFORMANCE_WINDOW_DAYS = 365   # 12 شهر


@dataclass
class TraderProfile:
    """ملف متداول واحد من أي منصة."""

    trader_id: str               # معرف فريد للمتداول (يُستخدم لإزالة التكرار عبر المنصات)
    platform: str                # hyperliquid, dydx, collective2, myfxbook, zulutrade, darwinex, birdeye, debank
    market_type: str             # "spot" أو "futures" — يُفصل بينهما ولا يُخلط
    pair: str                    # مثال: BTC-USD, ETH-USD, EURUSD
    direction: str               # BUY أو SELL
    entry_price: float           # مستوى الدخول كما ذكره المتداول
    stop_loss: Optional[float] = None      # وقف الخسارة كما ورد (لا يُخترع من البوت)
    take_profit: Optional[float] = None    # الهدف كما ورد (لا يُخترع من البوت)
    leverage: Optional[float] = None       # الرافعة كما ورد (لا يُخترع من البوت)
    position_size: Optional[float] = None  # حجم الصفقة

    # إحصائيات المتداول (من المنصة)
    total_trades: int = 0              # عدد الصفقات المغلقة
    winning_trades: int = 0            # عدد الصفقات الرابحة
    losing_trades: int = 0             # عدد الصفقات الخاسرة
    win_rate: float = 0.0              # نسبة الصفقات الرابحة (0-1)
    profit_factor: float = 0.0         # عامل الربح (الأرباح الإجمالية / الخسائر الإجمالية)
    max_drawdown: float = 0.0          # أقصى سحب نسبي (0-1، مثال 0.15 = 15%)
    sharpe_ratio: Optional[float] = None
    total_return_pct: float = 0.0      # العائد التراكمي بالنسبة المئوية
    annual_return_pct: float = 0.0     # العائد السنوي
    account_age_days: int = 0          # عمر الحساب بالأيام
    last_trade_days_ago: int = 999     # أيام منذ آخر صفقة

    # نافذة الأداء (الأشهر 6–12 الأخيرة)
    performance_window_days: int = 0   # عدد الأيام في نافذة الأداء المُحللة
    trades_in_window: int = 0          # عدد الصفقات داخل نافذة الأداء
    win_rate_in_window: float = 0.0    # نسبة الربح داخل نافذة الأداء

    followers_count: int = 0           # عدد المتابعين/المستنسخين
    aum_usd: float = 0.0               # الأصول تحت الإدارة بالدولار (ملي)')
    platform_score: Optional[float] = None  # تقييم المنصة الداخلية (ZuluRank، C2Score، إلخ)

    # موثوقية البيانات
    data_confirmed: bool = True        # صحيحة: البيانات مؤكدة من المصدر، خاطئة: ناقصة/مقدرة
    data_source_url: str = ""          # رابط مصدر البيانات للرجوع إليه
    data_freshness_hours: int = 0      # كم مضى من ساعات على تحديث البيانات

    # بيانات الإشارة الحالية
    signal_timestamp: Optional[datetime] = None
    signal_source: str = ""            # مثال: "telegram", "web", "api"

    # تصنيف داخلي (يُحسب بواسطة TraderClassifier)
    classification: str = "UNKNOWN"    # PROFESSIONAL_ACTIVE, PROFESSIONAL_INACTIVE, REGULAR, SUSPICIOUS
    classification_score: float = 0.0  # 0-100
    classification_reasons: list[str] = field(default_factory=list)


class TraderClassifier:
    """
    يصنف المتداولين إلى فئات بناءً على إحصائياتهم الفعلية من المنصات.

    معايير PROFESSIONAL_ACTIVE (جميعها يجب أن تتحقق):
    1. عدد الصفقات المغلقة ≥ 100 (يثبت أن النتائج من استراتيجية حقيقية)
    2. عمر الحساب ≥ 180 يوماً (6 أشهر على الأقل)
    3. نافذة أداء 6–12 شهر مع صفقات موثقة داخلها
    4. آخر صفقة خلال 30 يوماً (نشط حالياً)
    5. Maximum Drawdown ≤ 20% (ليست مقامرة)
    6. Profit Factor ≥ 1.5 (الأرباح تفوق الخسائر)
    7. Win Rate بين 40% و 85% (واقعي، ليس مشبوهاً)
    8. Sharpe Ratio ≥ 1.0 (إذا توفرت)
    9. حجم المحفظة (AUM) وحده لا يجعل الشخص محترفاً — يجب استيفاء المعايير أعلاه

    إذا كانت البيانات غير مؤكدة (data_confirmed=False)، يخفض التصنيف تلقائياً.
    """

    # عتبات التصنيف
    MIN_TRADES_FOR_PRO = 100
    MIN_ACCOUNT_AGE_DAYS_FOR_PRO = 180
    MAX_DAYS_SINCE_LAST_TRADE_FOR_ACTIVE = 30
    MAX_DRAWDOWN_FOR_PRO = 0.20          # 20%
    MIN_PROFIT_FACTOR_FOR_PRO = 1.5
    MIN_WIN_RATE_FOR_PRO = 0.40          # 40%
    MAX_WIN_RATE_FOR_PRO = 0.90          # 90% (أعلى من ذلك مشبوه)
    MIN_SHARPE_FOR_PRO = 1.0

    # عقوبة للبيانات غير المؤكدة
    UNCONFIRMED_DATA_PENALTY = 20  # تُطرح 20 نقطة إذا كانت البيانات غير مؤكدة

    def classify(self, profile: TraderProfile) -> TraderProfile:
        """يصنف متداول واحد ويعيد ملفه المحدث مع التصنيف."""
        reasons: list[str] = []
        score = 0.0

        # --- فحص التشبه (SUSPICIOUS) أولاً ---
        is_suspicious = False

        if profile.win_rate > self.MAX_WIN_RATE_FOR_PRO and profile.total_trades >= 20:
            is_suspicious = True
            reasons.append(f"نسبة ربح غير واقعية ({profile.win_rate:.1%}) مع {profile.total_trades} صفقة")

        if profile.max_drawdown > 0.50 and profile.total_trades >= 50:
            is_suspicious = True
            reasons.append(f"سحب أقصى مرتفع جداً ({profile.max_drawdown:.1%})")

        if profile.account_age_days < 30 and profile.total_return_pct > 1.0:
            is_suspicious = True
            reasons.append("عائد مرتفع جداً على حساب جديد (أقل من شهر)")

        if is_suspicious:
            profile.classification = "SUSPICIOUS"
            profile.classification_score = 0.0
            profile.classification_reasons = reasons
            return profile

        # --- حساب النقاط لكل معيار ---

        # 1. عدد الصفقات (حتى 25 نقطة)
        if profile.total_trades >= self.MIN_TRADES_FOR_PRO:
            score += 25
            reasons.append(f"عدد صفقات كافٍ ({profile.total_trades})")
        elif profile.total_trades >= 50:
            score += 15
            reasons.append(f"عدد صفقات متوسط ({profile.total_trades})")
        elif profile.total_trades >= 20:
            score += 8
            reasons.append(f"عدد صفقات قليل ({profile.total_trades})")

        # 2. عمر الحساب (حتى 20 نقطة)
        if profile.account_age_days >= self.MIN_ACCOUNT_AGE_DAYS_FOR_PRO:
            score += 20
            reasons.append(f"عمر حساب كافٍ ({profile.account_age_days} يوم)")
        elif profile.account_age_days >= 90:
            score += 12
            reasons.append(f"عمر حساب متوسط ({profile.account_age_days} يوم)")
        elif profile.account_age_days >= 30:
            score += 5
            reasons.append(f"عمر حساب قصير ({profile.account_age_days} يوم)")

        # 3. نافذة الأداء 6–12 شهر (حتى 15 نقطة)
        if MIN_PERFORMANCE_WINDOW_DAYS <= profile.performance_window_days <= MAX_PERFORMANCE_WINDOW_DAYS:
            if profile.trades_in_window >= 50:
                score += 15
                reasons.append(f"نافذة أداء 6–12 شهر مع {profile.trades_in_window} صفقة موثقة")
            elif profile.trades_in_window >= 20:
                score += 10
                reasons.append(f"نافذة أداء 6–12 شهر مع {profile.trades_in_window} صفقة")
            else:
                score += 5
                reasons.append(f"نافذة أداء 6–12 شهر مع {profile.trades_in_window} صفقة فقط")
        else:
            reasons.append(f"نافذة أداء غير مكتملة ({profile.performance_window_days} يوم، المطلوب 180–365)")

        # 4. النشاط الحالي (حتى 15 نقطة)
        if profile.last_trade_days_ago <= 7:
            score += 15
            reasons.append("نشط جداً (آخر صفقة خلال أسبوع)")
        elif profile.last_trade_days_ago <= self.MAX_DAYS_SINCE_LAST_TRADE_FOR_ACTIVE:
            score += 10
            reasons.append(f"نشط (آخر صفقة منذ {profile.last_trade_days_ago} أيام)")
        elif profile.last_trade_days_ago <= 90:
            score += 5
            reasons.append(f"شبه نشط (آخر صفقة منذ {profile.last_trade_days_ago} أيام)")

        # 5. السحب الأقصى (حتى 15 نقطة)
        if profile.max_drawdown <= 0.10:
            score += 15
            reasons.append(f"سحب أقصى منخفض ({profile.max_drawdown:.1%})")
        elif profile.max_drawdown <= self.MAX_DRAWDOWN_FOR_PRO:
            score += 10
            reasons.append(f"سحب أقصى مقبول ({profile.max_drawdown:.1%})")
        elif profile.max_drawdown <= 0.30:
            score += 5
            reasons.append(f"سحب أقصى مرتفع ({profile.max_drawdown:.1%})")

        # 6. عامل الربح (حتى 15 نقطة)
        if profile.profit_factor >= self.MIN_PROFIT_FACTOR_FOR_PRO:
            score += 15
            reasons.append(f"عامل ربح جيد ({profile.profit_factor:.2f})")
        elif profile.profit_factor >= 1.2:
            score += 10
            reasons.append(f"عامل ربح مقبول ({profile.profit_factor:.2f})")
        elif profile.profit_factor >= 1.0:
            score += 3
            reasons.append(f"عامل ربح ضعيف ({profile.profit_factor:.2f})")

        # 7. نسبة الربح (حتى 10 نقاط)
        if self.MIN_WIN_RATE_FOR_PRO <= profile.win_rate <= self.MAX_WIN_RATE_FOR_PRO:
            score += 10
            reasons.append(f"نسبة ربح واقعية ({profile.win_rate:.1%})")
        elif profile.win_rate > self.MAX_WIN_RATE_FOR_PRO:
            score += 5
            reasons.append(f"نسبة ربح عالية (مراجعة يدوية مطلوبة: {profile.win_rate:.1%})")

        # 8. شارب راتيو (حتى 10 نقاط)
        if profile.sharpe_ratio is not None:
            if profile.sharpe_ratio >= self.MIN_SHARPE_FOR_PRO:
                score += 10
                reasons.append(f"شارب راتيو جيد ({profile.sharpe_ratio:.2f})")
            elif profile.sharpe_ratio >= 0.5:
                score += 5
                reasons.append(f"شارب راتيو مقبول ({profile.sharpe_ratio:.2f})")

        # 9. بيانات مؤكدة أم ناقصة (عقوبة حتى 20 نقطة)
        if not profile.data_confirmed:
            score -= self.UNCONFIRMED_DATA_PENALTY
            reasons.append(f"بيانات غير مؤكدة من المصدر (-{self.UNCONFIRMED_DATA_PENALTY} نقطة)")
        else:
            reasons.append("بيانات مؤكدة من المصدر")

        # 10. حجم المحفظة (AUM) — نقطة إضافية فقط، لا تُDecision maker
        if profile.aum_usd >= 1_000_000:
            score += 5
            reasons.append(f"رأس مال تحت الإدارة كبير (${profile.aum_usd:,.0f})")
        elif profile.aum_usd >= 100_000:
            score += 2
            reasons.append(f"رأس مال تحت الإدارة متوسط (${profile.aum_usd:,.0f})")
        else:
            reasons.append(f"حجم محفظة عادي (${profile.aum_usd:,.0f}) — لا يؤثر في التصنيف وحده")

        # --- تحديد التصنيف النهائي ---
        # PROFESSIONAL_ACTIVE يحتاج score ≥ 70 AND نشط AND بيانات مؤكدة
        if (
            score >= 70
            and profile.last_trade_days_ago <= self.MAX_DAYS_SINCE_LAST_TRADE_FOR_ACTIVE
            and profile.data_confirmed
        ):
            profile.classification = "PROFESSIONAL_ACTIVE"
        elif score >= 50:
            profile.classification = "PROFESSIONAL_INACTIVE"
        elif score >= 30:
            profile.classification = "REGULAR"
        else:
            profile.classification = "SUSPICIOUS"

        profile.classification_score = min(100.0, max(0.0, score))
        profile.classification_reasons = reasons
        return profile

    def classify_batch(self, profiles: list[TraderProfile]) -> list[TraderProfile]:
        """تصنيف مجموعة من المتداولين."""
        return [self.classify(p) for p in profiles]

    def get_professional_active(self, profiles: list[TraderProfile]) -> list[TraderProfile]:
        """إرجاع المتداولين المحترفين والنشطين فقط."""
        return [p for p in profiles if p.classification == "PROFESSIONAL_ACTIVE"]

    def get_professional(self, profiles: list[TraderProfile]) -> list[TraderProfile]:
        """إرجاع جميع المتداولين المحترفين (نشطين وغير نشطين)."""
        return [
            p for p in profiles
            if p.classification in ("PROFESSIONAL_ACTIVE", "PROFESSIONAL_INACTIVE")
        ]

    def deduplicate_by_trader_id(
        self, profiles: list[TraderProfile]
    ) -> list[TraderProfile]:
        """
        إزالة التكرار: إذا ظهر نفس المتداول (نفس trader_id) في منصات متعددة،
        يُحتسب مرة واحدة فقط — يُحتفظ بالنسخة ذات أعلى درجة/classification.
        """
        seen: dict[str, TraderProfile] = {}
        for p in profiles:
            tid = p.trader_id
            if tid not in seen:
                seen[tid] = p
            else:
                existing = seen[tid]
                # احتفظ بالنسخة ذات التصنيف الأعلى أو الدرجة الأعلى
                if p.classification_score > existing.classification_score:
                    seen[tid] = p
                elif p.classification_score == existing.classification_score:
                    # إذا متساويين، احتفظ بالبيانات الأكثر اكتمالاً (data_confirmed=True)
                    if p.data_confirmed and not existing.data_confirmed:
                        seen[tid] = p
        return list(seen.values())


# ---------------------------------------------------------------------------
# مثال توضيحي
# ---------------------------------------------------------------------------

def demo() -> None:
    """تشغيل توضيحي لنظام التصنيف."""
    print("=" * 70)
    print("Trader Classifier — Demo (تصنيف المتداولين)")
    print("=" * 70)

    classifier = TraderClassifier()

    # متداول محترف ونشط (كريبتو سبوت)
    pro_active = TraderProfile(
        trader_id="0xpro1",
        platform="collective2",
        market_type="spot",
        pair="BTC-USD",
        direction="BUY",
        entry_price=65000.0,
        stop_loss=63000.0,
        take_profit=69000.0,
        leverage=1.0,
        total_trades=450,
        winning_trades=270,
        losing_trades=180,
        win_rate=0.60,
        profit_factor=1.85,
        max_drawdown=0.12,
        sharpe_ratio=1.45,
        total_return_pct=45.5,
        annual_return_pct=18.2,
        account_age_days=730,
        last_trade_days_ago=2,
        performance_window_days=270,  # 9 أشهر داخل النافذة
        trades_in_window=300,
        win_rate_in_window=0.59,
        followers_count=1200,
        aum_usd=4500000,
        platform_score=85.0,
        data_confirmed=True,
        data_source_url="https://collective2.com/system/0xpro1",
        data_freshness_hours=4,
    )

    # متداول محترف ولكن غير نشط (كريبتو فيوتشرز)
    pro_inactive = TraderProfile(
        trader_id="0xpro2",
        platform="myfxbook",
        market_type="futures",
        pair="ETH-USD",
        direction="SELL",
        entry_price=3400.0,
        stop_loss=3500.0,
        take_profit=3200.0,
        leverage=3.0,
        total_trades=320,
        winning_trades=192,
        losing_trades=128,
        win_rate=0.60,
        profit_factor=1.65,
        max_drawdown=0.15,
        sharpe_ratio=1.20,
        total_return_pct=38.0,
        annual_return_pct=12.5,
        account_age_days=540,
        last_trade_days_ago=45,  # غير نشط
        performance_window_days=240,
        trades_in_window=200,
        win_rate_in_window=0.58,
        followers_count=800,
        aum_usd=2800000,
        platform_score=78.0,
        data_confirmed=True,
        data_source_url="https://myfxbook.com/trader/0xpro2",
        data_freshness_hours=12,
    )

    # متداول عادي (حجم محفظة كبير ولكن لا يستوفي معايير المحترف)
    regular_big_aum = TraderProfile(
        trader_id="0xbig",
        platform="darwinex",
        market_type="spot",
        pair="SOL-USD",
        direction="BUY",
        entry_price=140.0,
        total_trades=45,
        winning_trades=23,
        losing_trades=22,
        win_rate=0.51,
        profit_factor=1.10,
        max_drawdown=0.08,
        account_age_days=90,
        last_trade_days_ago=5,
        performance_window_days=90,  # أقل من 6 أشهر
        trades_in_window=30,
        win_rate_in_window=0.50,
        followers_count=500,
        aum_usd=10_000_000,  # محفظة ضخمة!
        data_confirmed=True,
        data_source_url="https://darwinex.com/darwin/0xbig",
        data_freshness_hours=24,
    )

    # متداول مشبوه
    suspicious = TraderProfile(
        trader_id="0xsus1",
        platform="zulutrade",
        market_type="spot",
        pair="XAU-USD",
        direction="BUY",
        entry_price=2650.0,
        total_trades=15,
        winning_trades=14,
        losing_trades=1,
        win_rate=0.93,
        profit_factor=5.20,
        max_drawdown=0.02,
        account_age_days=12,
        last_trade_days_ago=1,
        performance_window_days=12,
        trades_in_window=15,
        win_rate_in_window=0.93,
        followers_count=20,
        aum_usd=5000,
        data_confirmed=False,  # بيانات غير مؤكدة
        data_source_url="",
        data_freshness_hours=168,
    )

    # نفس المتداول ظهر في منصتين (لاختبار الإزالة)
    duplicate = TraderProfile(
        trader_id="0xpro1",  # نفس الـ ID
        platform="hyperliquid",
        market_type="futures",
        pair="BTC-USD",
        direction="BUY",
        entry_price=65100.0,
        total_trades=400,
        winning_trades=240,
        losing_trades=160,
        win_rate=0.60,
        profit_factor=1.70,
        max_drawdown=0.14,
        sharpe_ratio=1.30,
        account_age_days=700,
        last_trade_days_ago=1,
        performance_window_days=300,
        trades_in_window=280,
        win_rate_in_window=0.59,
        followers_count=900,
        aum_usd=3800000,
        platform_score=82.0,
        data_confirmed=True,
        data_source_url="https://hyperliquid.xyz/trader/0xpro1",
        data_freshness_hours=2,
    )

    all_profiles = [pro_active, pro_inactive, regular_big_aum, suspicious, duplicate]
    classified = classifier.classify_batch(all_profiles)

    print("\n--- قبل إزالة التكرار ---")
    for p in classified:
        print(f"  {p.trader_id[:12]}... | {p.platform} | {p.market_type} | {p.classification} | {p.classification_score:.0f} | AUM: ${p.aum_usd:,.0f}")

    # إزالة التكرار
    deduped = classifier.deduplicate_by_trader_id(classified)
    print(f"\n--- بعد إزالة التكرار (من {len(classified)} إلى {len(deduped)}) ---")
    for p in deduped:
        print(f"  {p.trader_id[:12]}... | {p.platform} | {p.market_type} | {p.classification} | {p.classification_score:.0f}")

    print(f"\nالمحترفون والنشطون: {len(classifier.get_professional_active(deduped))}")
    for p in classifier.get_professional_active(deduped):
        print(f"  ✓ {p.trader_id[:12]}... | {p.platform} | {p.market_type} | {p.pair} | {p.direction} @ {p.entry_price} | نقاط: {p.classification_score:.0f} | بيانات مؤكدة: {p.data_confirmed}")


if __name__ == "__main__":
    demo()