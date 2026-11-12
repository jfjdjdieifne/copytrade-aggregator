# ARCHITECTURE.md — نسخ الصفقات المجمّع (بنية المشروع)

**المشروع:** copytrade-aggregator (جانبي، منفصل تماماً عن ZONES v1 المجمد)
**التاريخ:** 2026-11-12
**الفرع:** arena/f75dc3c4-trading-bot
**الإصدار:** 1.0.0

## نظرة عامة

إطار عمل لجمع إشارات نسخ التداول من متداولين محترفين عبر منصات متعددة، وتجميع الإشارات المتفقة على نفس الزوج والاتجاه مع مستويات دخول متقاربة، وإصدار إشارة مجمعة مع درجة ثقة من 100 نقطة.

جميع النتائج افتراضية (HYPOTHETICAL_) ولا تشكل نصيحة استثمارية. النظام لا ينفذ صفقات تلقائياً.

## هيكل الملفات (النهائي)

```
copytrade-aggregator/
├── README.md                 # توثيق المشروع الرئيسي
├── LICENSE                   # ترخيص MIT
├── requirements.txt          # تبعيات التشغيل
├── requirements-dev.txt      # تبعيات التطوير
├── setup.py                  # إعدادات التثبيت
├── pyproject.toml            # إعدادات نظام البناء
├── .gitignore                # ملفات تُتجاهل في Git
├── DECLARED_CHOICES_COPYTRADE_AGG.md   # قرارات التصميم والمواصفات
├── ARCHITECTURE.md           # هذا الملف (هندسة المشروع)
├── docs/PLATFORM_SETUP.md    # دليل إعداد المنصات
├── src/
│   └── copytrade_agg/        # الحزمة الرئيسية (مستقلة عن ZONES v1)
│       ├── __init__.py
│       ├── models.py         # Signal, AggregatedSignal, CopyTradeAggregator
│       ├── classifier.py     # TraderClassifier (تصنيف 4-way)
│       ├── scorer.py         # ConfidenceScorer (الثقة من 100 نقطة)
│       ├── engine.py         # RecommendationEngine (محرك التوصيات)
│       ├── aggregator.py     # CopyTradeEngine (محرك التجميع البسيط)
│       ├── connectors.py     # وصلات 8 منصات + normalizer
│       ├── telegram.py       # TelegramNotifier (إشعارات تليجرام + /details)
│       └── backtest.py       # اختبار التاريخي للإشارات
├── tests/                    # اختبارات الوحدة (مُقترَح)
│   ├── test_classifier.py
│   ├── test_scorer.py
│   ├── test_engine.py
│   └── test_connectors.py
├── scripts/                  # سكريبتات التشغيل
│   └── run_copytrade_agg.py  # سكريبت التشغيل التجريبي
└── examples/                 # أمثلة عملية
    └── practical_test.py     # اختبار عملي كامل للنظام
```

## مسار البيانات (Data Flow)

```
[منصة عامة]  →  RawFill  →  normalize_fill_to_signal()  →  Signal
   ↓                                                              ↓
[منصة أخرى]  →  RawFill  →  normalize_fill_to_signal()  →  Signal
   ↓                                                              ↓
   ----------------------------------------------------------------
                              ↓
                    CopyTradeAggregator.aggregate()  (أو RecommendationEngine)
                              ↓
                    [تجميع حسب (الزوج، الاتجاه، نوع السوق)]
                              ↓
                    [تصنيف المتداولين: PROFESSIONAL_ACTIVE/INACTIVE/REGULAR/SUSPICIOUS]
                              ↓
                    [إزالة التكرار: الشخص نفسه يُحسب مرة واحدة]
                              ↓
                    [تجميع مستويات الدخول ضمن 1.5% tolerance]
                              ↓
                    [فحص الحد الأدنى: 2 محترفين نشطين مستقلين]
                              ↓
                    [حساب درجة الثقة 0-100]
                              ↓
                    AggregatedSignal / Recommendation (الإشارة المجمعة)
                              ↓
                    TelegramNotifier (إرسال لتليجرام + أمر /details)
```

## المكونات التقنية

### 1. models.py — نموذج البيانات
- `Signal`: إشارة واحدة من متداول واحد (زوج، اتجاه، دخول، ستوب، هدف، تقييم، مصدر)
- `AggregatedSignal`: إشارة مجمعة (وسط الدخول، انتشار، عدد المتداولين، ثقة)
- `CopyTradeAggregator`: منطق التجميع العنقودي لمستويات الدخول

### 2. classifier.py — تصنيف المتداولين
- `TraderProfile`: ملف متداول مع إحصائيات (صفقات، ربح، سحب، عمر حساب، نافذة أداء، إلخ)
- `TraderClassifier`: تصنيف إلى 4 فئات:
  - `PROFESSIONAL_ACTIVE`: محترف نشط (score ≥ 70، آخر صفقة ≤ 30 يوم، بيانات مؤكدة)
  - `PROFESSIONAL_INACTIVE`: محترف غير نشط (score ≥ 50)
  - `REGULAR`: متداول عادي (score ≥ 30)
  - `SUSPICIOUS`: مشبوه (نسبة ربح > 90% مع 20+ صفقة، أو سحب > 50% مع 50+ صفقة)
- `deduplicate_by_trader_id()`: إزالة التكرار للشخص نفسه عبر منصات متعددة

### 3. scorer.py — نظام الثقة من 100 نقطة
- `ConfidenceScorer`: يحسب درجة الثقة بناءً على:
  - 40 نقطة: عدد المحترفين النشطين المستقلين (8 نقاط لكل متداول، max 5)
  - 25 نقطة: متوسط جودة المتداولين (من التصنيف)
  - 20 نقطة: قرب مستويات الدخول ضمن 1.5% tolerance
  - 15 نقطة: جودة مصدر البيانات (مؤكدة vs عامة)
- `ConfidenceResult`: نتيجة مع تفاصيل كل متداول، وسط الدخول، انتشار الدخول، сигнал أُطلق أم لا

### 4. engine.py — محرك التوصيات
- `RecommendationEngine`: يربط المنصات بالمصنف والم계数، يصدر توصيات مجمعة
- `Recommendation`: نتيجة التوصية مع جميع التفاصيل للتيليجرام
- يدعم `market_type` (spot/futures) بشكل منفصل
- لا يخترع ستوب/هدف/رافعة — يعرض الخطة كما وردت من المصدر

### 5. connectors.py — وصلات المنصات (8 منصات)
- `BaseConnector`: واجهة موحدة لجميع الوصلات
- `HyperliquidConnector`: عام (REST + WebSocket)
- `DydxConnector`: عام (REST Indexer v4)
- `Collective2Connector`: يحتاج API Key
- `MyfxbookConnector`: يحتاج تسجيل دخول
- `ZuluTradeConnector`: يحتاج API Key لبعض النقاط
- `DarwinexConnector`: يحتاج API Key
- `BirdeyeConnector`: يحتاج API Key من bds.birdeye.so
- `DeBankConnector`: عام/جزئي
- `normalize_fill_to_signal()`: محول البيانات الخام إلى Signal

### 6. telegram.py — إشعارات تليجرام
- `TelegramNotifier`: إرسال توصيات مجمعة، دعم أمر `/details`
- `TelegramConfig`: إعدادات البوت (توكن، معرف دردشة)
- رسالة تحتوي على: درجة الثقة، عدد المحترفين، وسط الدخول، السعر الحالي، جدول مقارنة
- أمر `/details <المعرف>`: صفحة تفصيلية لكل متداول (إحصائيات، مصدر، حداثة البيانات)

### 7. backtest.py — اختبار التاريخي
- `BacktestResult`: نتيجة اختبار إشارة واحدة (TARGET/STOP/TIMEOUT/NO_DATA، قيمة R)
- `backtest_signal()`: اختبار إشارة واحدة على بيانات شموع تاريخية
- `summarize_backtest()`: تلخيص نتائج مجموعة إشارات (hit rate، stop rate، mean R)

## المنصات المدعومة

### الفئة الأولى: منصات عامة (بدون حساب)
| المنصة | نوع API | الحالة |
|--------|---------|--------|
| Hyperliquid | REST + WebSocket عام | ✅ مضاف |
| dYdX v4 Indexer | REST عام | ✅ مضاف |
| DeBank | عام (محافظ، حيتان) | ✅ مضاف |

### الفئة الثانية: تحتاج API Key
| المنصة | نوع الوصول | الحالة |
|--------|-----------|--------|
| Collective2 | API Key | ⚠️需要 إعداد |
| Myfxbook | تسجيل دخول | ⚠️需要 إعداد |
| ZuluTrade | API Key لبعض النقاط | ⚠️需要 إعداد |
| Darwinex | API Key | ⚠️需要 إعداد |
| Birdeye | API Key من bds.birdeye.so | ⚠️需要 إعداد |

## قواعد التجميع (من DECLARED_CHOICES)

- المجموعة الأساسية: (الزوج، الاتجاه، نوع السوق) يجب أن تتطابق تماماً
- تحمّل الدخول: 1.5% افتراضياً (مت configurable لكل زوج)
- الحد الأدنى من المتداولين: 2 محترفين نشطين مستقلين
- درجة الثقة: 40×المتداولين + 25×الجودة + 20×قرب الدخول + 15×جودة المصدر
- الحد الأدنى للثقة لإصدار الإشارة: 45/100 (مت configurable)
- الستوب والهدف: متوسط ما ورد من المتداولين (لا اختراع)

## ما لا يثبته هذا المشروع

- جميع النتائج افتراضية (HYPOTHETICAL_)
- لا يثبت الربحية أو divisive edge
- لم يتم ربط أي منصة فعلياً بعد (بلا API keys حقيقية)
- لا يوجد ضمان أن الإشارات المجمعة ستحقق أهدافها في السوق الحقيقي
- لا ينفذ صفقات تلقائياً — كل توصية تتطلب قراراً يدوياً

## الخطوات التالية (NOT DONE)

1. اختبار وصّالات Hyperliquid/dYdX ضد نقاط نهاية حقيقية (يحتاج إنترنت خارجي)
2. إضافة منصات إضافية (Drift, GMX, Bybit Copy Trading, Bitget, إلخ)
3. جلب تقييمات المتداولين الحقيقية من المنصات (بدلاً من القيم الافتراضية)
4. اختبار تاريخي (backtest) على بيانات فوركس/كريبتو حقيقية
5. بناء واجهة عرض للإشارات المجمعة اللحظية
6. إضافة تنبيهات عندما تتجاوز درجة الثقة عتبة معينة
7. كتابة اختبارات وحدة في tests/

## متطلبات النظام

- Python 3.8+
- aiohttp (للاتصال الفعلي بالمنصات)
- pytest (لاختبارات التطوير)

## التثبيت

```bash
pip install -e .
```

أو للتطوير:

```bash
pip install -r requirements-dev.txt
```

---

**إشعار:** هذا البرنامج للأغراض التعليمية والتجريبية فقط. لا يشكل نصيحة استثمارية، ولا يضمن أي ربح.