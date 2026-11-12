# دليل إعداد المنصات — copytrade-aggregator

يشرح هذا الملف كيفية إعداد كل منصة للاتصال الفعلي بالنظام.

## المنصات العامة (لا تحتاج API Key)

### Hyperliquid

**النوع:** REST + WebSocket عام (بدون حساب)
**نقطة النهاية:** `https://api.hyperliquid.xyz`
**WebSocket:** `wss://api.hyperliquid.xyz/ws`
**مفتاح API:** غير مطلوب للبيانات العامة

```python
from copytrade_agg.connectors import HyperliquidConnector

connector = HyperliquidConnector()
await connector.health_check()  # يتحقق من توفر المنصة
```

**ملاحظات:**
- لجلب `userFills` تحتاج عنوان محفظة المتداول
- للصفقات العامة، استخدم قناة `trades` عبر WebSocket

### dYdX v4 Indexer

**النوع:** REST عام (بدون حساب)
**نقطة النهاية:** `https://indexer.dydx.trade/v4`
**مفتاح API:** غير مطلوب للقراءة العامة

```python
from copytrade_agg.connectors import DydxConnector

connector = DydxConnector()
await connector.health_check()
fills = await connector.fetch_recent_fills(limit=200)
```

### DeBank

**النوع:** عام/جزئي (محافظ، حيتان، عدد المتابعين)
**نقطة النهاية:** `https://pro.openapi.debank.com`
**مفتاح API:** غير مطلوب للوصول العام الأساسي

```python
from copytrade_agg.connectors import DeBankConnector

connector = DeBankConnector()
await connector.health_check()
```

---

## المنصات التي تحتاج إعداد (API Key / تسجيل دخول)

### Collective2

**النوع:** API Key مطلوب
**توثيق:** https://www.collective2.com/api-docs/latest
**نقاط النهاية:** `listAllSystems`, `GetLeaderBoard`, `GetStrategyDetails`

```python
from copytrade_agg.connectors import Collective2Connector

connector = Collective2Connector(api_key="YOUR_API_KEY_HERE")
await connector.health_check()
```

**ملاحظات:** يحتاج API Key فعال. يوفر track records مؤكدة مع Drawdown و Sharpe ونسبة الربح.

### Myfxbook

**النوع:** تسجيل دخول مطلوب (Email/Password)
**توثيق:** https://www.myfxbook.com
**نقاط النهاية:** `GetAccount`, `GetGain`, `GetTrades`

```python
from copytrade_agg.connectors import MyfxbookConnector

connector = MyfxbookConnector(email="YOUR_EMAIL", password="YOUR_PASSWORD")
await connector.health_check()
```

**ملاحظات:** يحتاج جلسة تسجيل دخول. لا يوفر تصدير عام لصفقات النظام بدون اعتماد.

### ZuluTrade

**النوع:** API Key لبعض النقاط
**توثيق:** https://us.zulutrade.com/restapi-reference
**نقاط النهاية:** زلورانك (ZuluRank)، لائحة المتصدرين

```python
from copytrade_agg.connectors import ZuluTradeConnector

connector = ZuluTradeConnector(api_key="YOUR_API_KEY_HERE")
await connector.health_check()
```

### Darwinex

**النوع:** API Key مطلوب
**توثيق:** https://www.darwinex.com
**نقاط النهاية:** DARWINs، السمات الاستثمارية (0–10)

```python
from copytrade_agg.connectors import DarwinexConnector

connector = DarwinexConnector(api_key="YOUR_API_KEY_HERE")
await connector.health_check()
```

### Birdeye Data API

**النوع:** API Key مطلوب من bds.birdeye.so
**توثيق:** https://docs.birdeye.so
**نقاط النهاية:** wallet PnL، smart money، trader leaderboards

```python
from copytrade_agg.connectors import BirdeyeConnector

connector = BirdeyeConnector(api_key="YOUR_API_KEY_HERE")
await connector.health_check()
```

---

## جدول ملخص الإعداد

| المنصة | مفتاح API | تسجيل دخول | ملاحظات |
|--------|-----------|-----------|---------|
| Hyperliquid | ❌ غير مطلوب | ❌ غير مطلوب | أفضل نقطة بداية |
| dYdX v4 | ❌ غير مطلوب | ❌ غير مطلوب | قراءة عامة محدودة |
| DeBank | ❌ غير مطلوب | ❌ غير مطلوب | بيانات محافظ فقط |
| Collective2 | ✅ مطلوب | ❌ غير مطلوب | Track records مؤكدة |
| Myfxbook | ❌ غير مطلوب | ✅ مطلوب | يحتاج جلسة دخول |
| ZuluTrade | ✅ مطلوب لبعض النقاط | ❌ غير مطلوب | زلورانك |
| Darwinex | ✅ مطلوب | ❌ غير مطلوب | DARWINs، سمات استثمارية |
| Birdeye | ✅ مطلوب | ❌ غير مطلوب | بيانات DEX، محافظ ذكية |

---

## توصية للبدء الفعلي

للبدء بالاختبار الفعلي بدون تكاليف:

1. ابدأ بـ **Hyperliquid** أو **dYdX** (عامة، لا تحتاج مفتاح)
2. استخدم `fetch_recent_fills()` لجلب الصفقات الحديثة
3. حول `RawFill` إلى `Signal` عبر `normalize_fill_to_signal()`
4. مرر الإشارات عبر `RecommendationEngine` لإصدار التوصيات

## تحذير أمني

لا تضع مفاتيح API مباشرة في الكود. استخدم متغيرات بيئة أو ملف `.env`:

```bash
# .env (لا تُرفع إلى Git)
COLLECTIVE2_API_KEY=your_key_here
DARWINEX_API_KEY=your_key_here
BIRDEYE_API_KEY=your_key_here
```

```python
import os
from copytrade_agg.connectors import Collective2Connector

connector = Collective2Connector(api_key=os.getenv("COLLECTIVE2_API_KEY"))
```

---

**إشعار:** جميع النتائج افتراضية (HYPOTHETICAL_) ولا تشكل نصيحة استثمارية.