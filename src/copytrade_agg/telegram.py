"""
Telegram Notifier — نظام إشعارات التيليجرام مع أمر /details.

يقوم بإرسال توصيات النسخ المجمعة إلى قناة/مجموعة تليجرام، ويدعم أمر /details
لعرض تفاصيل صفقات كل متداول محترف على حدة في جدول مقارنة مرتب.

مواصفات التحديث الجديد:
- ملخص التوصية مع درجة الثقة، عدد المحترفين، وسط الدخول، المساحة 1.5%
- أمر /details <trader_id> لعرض صفحة تفصيلية لكل متداول (دخول، ستوب، هدف، رافعة، إحصائيات، مصدر)
- تمييز البيانات المؤكدة (✓) عن الناقصة (✗)
- إظهار عمر الصفقة والسعر الحالي للتنبيه إذا كان الدخول متأخراً
- لا تنفيذ تلقائي — توصية فقط، والقرار للمستخدم
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

UTC = timezone.utc


@dataclass
class TelegramConfig:
    """إعدادات بات التيليجرام."""
    bot_token: str
    chat_id: str | int
    parse_mode: str = "Markdown"
    disable_web_page_preview: bool = True


@dataclass
class PendingDetailRequest:
    """طلب معلق لعرض تفاصيل متداول محدد (يستخدمه أمر /details)."""
    chat_id: str | int
    trader_id: str
    platform: str
    pair: str
    direction: str
    market_type: str
    requested_at: datetime


class TelegramNotifier:
    """
    مرسل إشعارات التيليجرام للتوصيات المجمعة.

    الميزات:
    - إرسال إشارة مجمعة مع درجة الثقة، عدد المتداولين، تفاصيل الاتفاق
    - أمر /details <trader_id> لعرض صفقات متداول محترف فردي
    - تخزين الطلبات المعلقة لعرض التفاصيل لاحقًا
    - تمييز البيانات المؤكدة عن الناقصة
    """

    def __init__(self, config: TelegramConfig):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"
        self._pending_details: dict = {}
        self._last_recommendation: Optional[dict] = None

    # ------------------------------------------------------------------
    # low-level HTTP sender
    # ------------------------------------------------------------------

    async def _send_request(self, method: str, params: dict) -> dict:
        """إرسال طلب إلى واجهة تليجرام HTTP."""
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for TelegramNotifier")
        url = f"{self.base_url}/{method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"Telegram API error {resp.status}: {text}"
                    )
                return await resp.json()

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[dict] = None,
    ) -> dict:
        """إرسال رسالة نصية."""
        params: dict = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": self.config.disable_web_page_preview,
        }
        if parse_mode is None:
            parse_mode = self.config.parse_mode
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return await self._send_request("sendMessage", params)

    async def send_message_sync(self, chat_id: str | int, text: str) -> None:
        """إرسال متزامن (للاستخدام من خارج async loop)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(self.send_message(chat_id, text))

    # ------------------------------------------------------------------
    # Recommendation → Telegram message
    # ------------------------------------------------------------------

    async def send_recommendation(
        self,
        recommendation,
        chat_id: Optional[str | int] = None,
    ) -> dict:
        """
        إرسال توصية مجمعة إلى التيليجرام.

        Args:
            recommendation: كائن Recommendation من RecommendationEngine
            chat_id: معرف الدردشة (يُستخدم config.chat_id إذا لم يُعطَ)
        """
        target = chat_id if chat_id is not None else self.config.chat_id
        text = recommendation.telegram_message

        # إضافة أزرار تفاعلية لعرض التفاصيل (inline keyboard)
        buttons = []
        for t in recommendation.trader_details[:5]:
            tid = t.get("trader_id", "")
            btn_text = f"📊 تفاصيل {tid[:8]}..."
            buttons.append(
                [{"text": btn_text, "callback_data": f"/details {tid}"}]
            )

        reply_markup = None
        if buttons:
            reply_markup = {"inline_keyboard": buttons}

        # تخزين التوصية الأخيرة للرجوع إليها في أمر /details
        self._last_recommendation = {
            "pair": recommendation.pair,
            "direction": recommendation.direction,
            "market_type": recommendation.market_type,
            "trader_details": recommendation.trader_details,
            "confidence_score": recommendation.confidence_score,
            "confidence_level": recommendation.confidence_level,
            "entry_mean": recommendation.entry_mean,
            "current_price": recommendation.current_price,
            "current_price_source": recommendation.current_price_source,
        }

        return await self.send_message(
            target, text, reply_markup=reply_markup
        )

    def send_recommendation_sync(
        self, recommendation, chat_id: Optional[str | int] = None
    ) -> None:
        """إرسال متزامن للتوصية."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(self.send_recommendation(recommendation, chat_id))

    # ------------------------------------------------------------------
    # /details command handler
    # ------------------------------------------------------------------

    async def handle_details_command(
        self, trader_id: str, chat_id: str | int
    ) -> dict:
        """
        معالجة أمر /details <trader_id> — عرض صفقات متداول محترف فردي.

        Args:
            trader_id: معرف المتداول المطلوب
            chat_id: معرف الدردشة للرد
        """
        if self._last_recommendation is None:
            return await self.send_message(
                chat_id,
                "⚠️ لا توجد توصية حديثة مخزنة. انتظر إرسال إشارة جديدة أولاً."
            )

        details = self._last_recommendation["trader_details"]
        trader = next((t for t in details if t.get("trader_id") == trader_id), None)

        if trader is None:
            available = ", ".join(d.get('trader_id', 'N/A')[:12] for d in details)
            return await self.send_message(
                chat_id,
                f"❌ لم يتم العثور على متداول بالمعرف `{trader_id}` في التوصية الأخيرة.\n"
                f"المتداولون المتاحون: {available}"
            )

        # تسجيل الطلب المعلق (للتتبع)
        req = PendingDetailRequest(
            chat_id=chat_id,
            trader_id=trader_id,
            platform=trader.get("platform", ""),
            pair=self._last_recommendation["pair"],
            direction=self._last_recommendation["direction"],
            requested_at=datetime.now(UTC),
        )
        self._pending_details[trader_id] = req

        # بناء رسالة التفاصيل
        text = self._format_trader_details(trader, self._last_recommendation)
        return await self.send_message(chat_id, text)

    def handle_details_command_sync(
        self, trader_id: str, chat_id: str | int
    ) -> None:
        """معالجة متزامنة لأمر /details."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(self.handle_details_command(trader_id, chat_id))

    def _format_trader_details(
        self, trader: dict, recommendation_ctx: dict
    ) -> str:
        """تنسيق تفاصيل متداول فردي للعرض في التيليجرام."""
        ar_direction = "شراء 📈" if trader.get("direction", "").upper() == "BUY" else "بيع 📉"
        ar_market = "Spot" if trader.get("market_type", "spot") == "spot" else "Futures"
        confirmed = "✓ مؤكدة" if trader.get("data_confirmed", False) else "✗ غير مؤكدة"

        lines = [
            f"👤 *تفاصيل المتداول:* `{trader.get('trader_id', 'N/A')[:16]}...`",
            "",
            f"*المنصة:* {trader.get('platform', 'N/A')}",
            f"*نوع السوق:* {ar_market}",
            f"*الزوج:* {trader.get('pair', 'N/A')} | *الاتجاه:* {ar_direction}",
            f"*التصنيف:* {trader.get('classification', 'UNKNOWN')}",
            f"*نقاط الجودة:* {trader.get('classification_score', 0):.0f}/100",
            f"*مستوى الدخول (كما ورد):* `{trader.get('entry_price', 0):.6f}`",
        ]

        if trader.get("stop_loss") is not None:
            lines.append(f"*الستوب (كما ورد):* `{trader['stop_loss']:.6f}`")
        if trader.get("take_profit") is not None:
            lines.append(f"*الهدف (كما ورد):* `{trader['take_profit']:.6f}`")
        if trader.get("leverage") is not None:
            lines.append(f"*الرافعة (كما ورد):* `{trader['leverage']}x`")

        lines.extend([
            "",
            "*الإحصائيات (آخر 6–12 شهر):*",
            f"• نسبة الربح (Win Rate): {trader.get('win_rate', 0):.1%}",
            f"• عامل الربح (Profit Factor): {trader.get('profit_factor', 0):.2f}",
            f"• السحب الأقصى (Max DD): {trader.get('max_drawdown', 0):.1%}",
            f"• إجمالي الصفقات: {trader.get('total_trades', 0)}",
            f"• صفقات في نافذة الأداء: {trader.get('trades_in_window', 'N/A')} | ربح النافذة: {trader.get('win_rate_in_window', 0):.1%}",
            f"• عمر الحساب: {trader.get('account_age_days', 0)} يومًا",
            f"• آخر صفقة: منذ {trader.get('last_trade_days_ago', 0)} يومًا",
            f"• المتابعون: {trader.get('followers_count', 'غير معروف')}",
            f"• رأس المال المُدار: ${trader.get('aum_usd', 0):,.0f}",
        ])

        lines.extend([
            "",
            "*موثوقية البيانات:*",
            f"• الحالة: {confirmed}",
            f"• مصدر البيانات: {trader.get('data_source_url', 'غير محدد')}",
            f"• حداثة البيانات: منذ {trader.get('data_freshness_hours', 0)} ساعة",
        ])

        if trader.get("reasons"):
            lines.extend([
                "",
                "*أسباب التصنيف:*",
            ])
            for r in trader["reasons"]:
                lines.append(f"• {r}")

        ctx = recommendation_ctx
        lines.extend([
            "",
            f"*السياق:* {ctx.get('pair', 'N/A')} {ar_direction} في {ar_market} | "
            f"الثقة: {ctx.get('confidence_score', 0)}/100 | وسط الدخول: {ctx.get('entry_mean', 0):.6f}",
            "",
            "⚠️ *إشعار:* بيانات افتراضية (HYPOTHETICAL_) — ليست نصيحة استثمارية. "
            "الخطة معروضة كما وردت من المصدر، لا تنفيذ تلقائي.",
        ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """التحقق من أن بات التيليجرام يعمل."""
        if aiohttp is None:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/getMe",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    return data.get("ok", False)
        except Exception:
            return False


# ===========================================================================
# Self-test
# ===========================================================================

def test_message_format() -> None:
    """اختبار تنسيق الرسائل بدون اتصال حقيقي."""
    from .engine import Recommendation
    from .scorer import ConfidenceResult, TraderEntry

    result = ConfidenceResult(
        total_score=85,
        confidence_level="VERY_HIGH",
        n_professional_active=3,
        n_professional_inactive=0,
        n_regular=1,
        n_suspicious=0,
        trader_entries=[
            TraderEntry(
                trader_id="0xpro1abc123",
                platform="collective2",
                market_type="spot",
                pair="BTC-USD",
                direction="BUY",
                entry_price=65000.0,
                stop_loss=63000.0,
                take_profit=69000.0,
                leverage=1.0,
                classification="PROFESSIONAL_ACTIVE",
                classification_score=100.0,
                win_rate=0.60,
                profit_factor=1.85,
                max_drawdown=0.12,
                total_trades=450,
                account_age_days=730,
                last_trade_days_ago=2,
                trades_in_window=300,
                win_rate_in_window=0.59,
                followers_count=1200,
                aum_usd=4500000,
                data_confirmed=True,
                data_source_url="https://collective2.com/system/0xpro1",
                data_freshness_hours=4,
                reasons=["عدد صفقات كافٍ (450)", "عمر حساب كافٍ (730 يوم)"],
            ),
        ],
        entry_mean=65000.0,
        entry_spread_pct=0.5,
        entry_tolerance_pct=1.5,
        sources=["collective2"],
        market_types=["spot"],
        data_confirmed_count=1,
        reasons=["متداولين محترفين نشطين مستقلين متفقين: 3"],
        recommendation="إشارة قوية جداً",
        signal_emitted=True,
    )

    notifier = TelegramNotifier(
        TelegramConfig(bot_token="test_token", chat_id="123456")
    )
    msg = notifier._format_trader_details(result.trader_entries[0].__dict__, {
        "pair": "BTC-USD",
        "confidence_score": 85,
        "entry_mean": 65000.0,
    })
    assert "0xpro1abc123" in msg
    assert "PROFESSIONAL_ACTIVE" in msg
    assert "collective2" in msg
    assert "Spot" in msg
    assert "✓ مؤكدة" in msg
    print("Telegram message format test PASSED")


if __name__ == "__main__":
    test_message_format()
    print("TelegramNotifier module structure OK (no live API calls made).")