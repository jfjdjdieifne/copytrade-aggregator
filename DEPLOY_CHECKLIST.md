# قائمة النشر — من صفر إلى نظام يعمل على تلغرام

كل خطوة لها **علامة نجاح**. لا تنتقل قبل أن تتحقق.

---

## المرحلة 0 — التحقق المحلي (10 دقائق، بلا حساب ولا شبكة)

```bash
cd copytrade-aggregator
pip install -e ".[dev]"
pytest -q
```

✅ **النجاح:** `277 passed`

**تحقّق بصري (لا يحتاج كمبيوتر):**
```bash
python -m cta.preview_server --host 0.0.0.0 --port 8000
```
افتح المنفذ من الجوال → يجب أن ترى لوحة عربية فيها إشارة BTC واضحة، وشريط 🟡 يقول «عرض تجريبي».


```bash
python -m cta.cli demo
```

✅ **النجاح:** يظهر
- إشارة `BTC شراء` بدرجة ≥ 65 و`متداولون مستقلون: 3`
- `محفظة متحوّطة (ETH): ✅ رُفض`
- `دخول قديم 6 ساعات (SOL): ✅ رُفض`
- `انتشار 2.4% (ARB): ✅ رُفض`
- `النتيجة: ✅ نجح`

❌ **إن فشل:** لا تنشر. المشكلة في المنطق لا في الإعداد.

---

## المرحلة 1 — بوت تلغرام (5 دقائق)

1. افتح `@BotFather` → `/newbot` → اختر اسماً → انسخ **التوكن**.
2. أرسل أي رسالة لبوتك الجديد (مهم، بدونه لا يمكنه مراسلتك).
3. افتح `@userinfobot` → انسخ **Id** الخاص بك.

✅ **النجاح:** لديك رقمان: توكن يشبه `123456:ABC-DEF...` و chat_id يشبه `123456789`.

---

## المرحلة 2 — اختبار حقيقي من جهازك (5 دقائق)

هذه أهم خطوة: تثبت أن المنصات تستجيب فعلاً **قبل** إضاعة وقت على Cloudflare.

```bash
cp .env.example .env
```

املأ في `.env`:
```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

```bash
python -m cta.cli doctor
```

✅ **النجاح:** `hyperliquid: 🟢 متصل` و`3/3` أو `1/3` على الأقل (Hyperliquid هو الأهم).

❌ **إن كانت كلها 🔴:** شبكتك تحجبها. جرّب VPN، أو انشر على Cloudflare مباشرة (شبكتهم تصل).

```bash
python -m cta.cli profile
```

✅ **النجاح:** JSON فيه `"watchlist": <رقم أكبر من 0>` و`"elite": <رقم>`.

❌ **إن كان `watchlist: 0`:**
- طبيعي في البداية إن كانت الفلاتر صارمة.
- جرّب تخفيفها مؤقتاً في `.env`:
  ```env
  WATCHLIST_MIN_GRADE=WATCH
  DISCOVERY_MAX_CANDIDATES=500
  ```

```bash
python -m cta.cli scan
```

✅ **النجاح:** `"ok": true` مع `"watchlist"` و`"clusters"`. وصول إشارة على تلغرام مكسب إضافي (قد يستغرق ساعات — التقارب نادر بطبيعته).

---

## المرحلة 3 — النشر على Cloudflare (10 دقائق)

```bash
npm install -g wrangler
wrangler login          # يفتح المتصفح
```

```bash
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_CHAT_ID
wrangler secret put TELEGRAM_WEBHOOK_SECRET    # أي نص عشوائي طويل
wrangler secret put OWNER_TOKEN                # أي نص عشوائي طويل
```

عدّل `wrangler.toml`:
```toml
[vars]
WORKER_URL = "https://copytrade-radar.<نطاقك>.workers.dev"
```

```bash
wrangler deploy
```

✅ **النجاح:** رسالة فيها رابط `https://copytrade-radar....workers.dev`.

❌ **إن ظهر `python` غير مدعوم:** تأكد أن حسابك على خطة تسمح بـ Python Workers، وأن `compatibility_date` في الملف حديث.

---

## المرحلة 4 — التهيئة الأولى

```bash
curl -X POST https://<رابطك>/boot -H "x-owner-token: <OWNER_TOKEN>"
```

✅ **النجاح:** `{"ok": true, "watchlist": <رقم>, "steps": [...]}`

```bash
curl https://<رابطك>/health
```

✅ **النجاح:**
```json
{"ok": true, "watchlist": 40, "source_health": {"hyperliquid": true, ...}}
```

---

## المرحلة 5 — اختبار البوت

أرسل `/start` لبوتك على تلغرام.

✅ **النجاح:** تظهر القائمة مع أزرار.

أرسل `/health`.

✅ **النجاح:** يظهر عدد المراقَبين وآخر دورة.

أرسل: `لماذا لم تصل إشارة؟`

✅ **النجاح:** يشرح أسباب الرفض بأرقام حقيقية (وليس رداً عاماً).

❌ **إن لم يرد البوت:** نفّذ `/boot` مرة أخرى، أو:
```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```
تحقق أن `url` صحيح و`last_error_message` فارغ.

---

## المرحلة 6 — أول 48 ساعة (مراقبة، لا تدخّل)

```bash
curl https://<رابطك>/health     # كل عدة ساعات
```

| ما تراقبه | المتوقع | ماذا يعني إن اختلف |
|---|---|---|
| `watchlist` | 30–150 | 0 = الفلاتر شديدة أو الشبكة |
| `source_health.hyperliquid` | true | false = المنصة تحجب أو تغيّر API |
| `signals` | 0–10 في أول يومين | 0 طبيعي. >20 يعني البوابات مرتخية |
| `/stats` | `evaluated: 0` ثم يزيد | لا يُقيَّم شيء قبل مرور ساعة |

**قاعدة ذهبية:** لا تغيّر عتبة قبل 20 إشارة مُقيَّمة. تغييرها قبل ذلك يجعلك تعاير على ضجيج.

---

## المرحلة 7 — بعد أسبوع: أول حكم حقيقي

```bash
curl -X POST https://<رابطك>/track -H "x-owner-token: <OWNER_TOKEN>"
```

ثم `/stats` في تلغرام.

| النتيجة | الحكم |
|---|---|
| `mean_r > +0.15` وعينة ≥ 20 | النظام يضيف قيمة — استمر وراقب السحب |
| `mean_r` بين −0.05 و +0.15 | محايد — راجع `ENTRY_TOLERANCE_BPS` و`MIN_SIGNAL_SCORE` |
| `mean_r < −0.05` | **أوقف الإرسال** (`MIN_SIGNAL_SCORE=90`) وأعد المعايرة |

هذه اللحظة هي التي تفصل مشروعاً حقيقياً عن واجهة جميلة.

---

## أخطاء شائعة وحلولها

| العرض | السبب المرجّح | الحل |
|---|---|---|
| `watchlist: 0` | فلاتر الاكتشاف صارمة | `WATCHLIST_MIN_GRADE=WATCH`, `DISCOVERY_MAX_CANDIDATES=500` |
| `watchlist > 0` لكن `signals: 0` دائماً | لا تقارب فعلي (طبيعي) أو الشبكة | راقب `/why` |
| `source_health` كلها false | شبكة/حجب | جرّب من Cloudflare لا من جهازك |
| البوت يرد لكن بلا AI | نفدت neurons أو النموذج غير متاح | طبيعي — البديل الحتمي يعمل |
| إشارات مكررة | `signal_id` غير مستقر | تحقق من `seen_signal_ids` في الحالة |
| `wrangler deploy` يفشل | `compatibility_date` قديم | حدّثه في `wrangler.toml` |

---

## ما لا يجب فعله

- ❌ **لا ترفع `.env`** — الأسرار في `wrangler secret` فقط.
- ❌ **لا تخفّض `MIN_INDEPENDENT_TRADERS` إلى 1** — هذا يلغي سبب وجود النظام.
- ❌ **لا تُلغِ `MIN_ELITE_OR_PROVEN`** — ستستقبل إشارات من محافظ لم تُثبت شيئاً.
- ❌ **لا تتدخل بصفقات بمال حقيقي** قبل المرحلة 7 بنتيجة إيجابية.
