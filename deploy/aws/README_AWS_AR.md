# النشر على AWS (الخطة المجانية) — 24/7 حقيقي

> بدك تشغّل الرادار على سيرفر AWS تبعل AWS Free / الرصيد المجاني.
> كل الأوامر بالأسفل بتعمل على **Amazon Linux 2023** أو **Ubuntu 22.04+**.

---

## 0. ليش AWS بدل Cloudflare Worker؟

| | Cloudflare Worker (مجاني) | EC2 (الخطة المجانية) |
|---|---|---|
| مدة التنفيذ | **10ms CPU** — ما بيكفي يفك ملف 39MB | ثوانٍ/دقائق، ما في حد |
| طلبات فرعية لكل دورة | **50** — يعني ~40 محفظة بالدورة | ما في حد عملي |
| الذاكرة | 128MB | 1GB (t2.micro / t4g.smaller) |
| تكرار الاستطلاع | كل دقيقة، بس بنافذة دوّارة | كل 30–60 ثانية، **كل المحافظ** |
| التكلفة | 0 | 0 ضمن الخطة المجانية |

النتيجة: على AWS نقدر نراقب **كل المحترفين بالدورة الواحدة**، مش 40 بس،
ونقرأ الأوامر المعلّقة بشكل دوري. هاد بهوّن علينا مشكلة الاكتشاف كلها.

> ⚠️ **تحذير تكلفة بصراحة:** الخطة المجانية ١٢ شهر وبعدها بيبدأ الفوترة،
> والرصيد المجاني بينفذ. راقب الفاتورة من Billing Dashboard، وحط
> **Budget Alarm** على ١ دولار. مش مسؤوليتي تدفع شي ما بتعرف فيه.

---

## 1. افتح السيرفر

- نوع المثيل: `t2.micro` أو `t3.micro` (أو `t4g.small` إن توفر بالمنطقة تبعتك).
- المساحة: 8–16GB كافية (الكود كله نص).
- Security Group: **ما تفتح SSH للعالم** (`.ssh` من عنوانك بس). ما في منافذ تانية مطلوبة —
  إحنا إحنا منخرج للإنترنت، مش حدا بيدخل علينا.

## 2. ثبّت

```bash
sudo dnf update -y || sudo apt update -y
sudo dnf install -y git python3.11 python3.11-pip || sudo apt install -y git python3.11 python3-pip
git clone https://github.com/jfjdjdieifne/copytrade-aggregator.git
cd copytrade-aggregator
python3.11 -m pip install --user -e .
```

## 3. اضبط الإعدادات

```bash
cp .env.example .env
nano .env
```

الأساسيات:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
OWNER_TOKEN=...              # أي نص عشوائي
ENABLED_SOURCES=hyperliquid,okx,dydx,gmx
POLL_INTERVAL_SEC=45
ORDERS_MAX_WALLETS=60
```

## 4. اختبر قبل ما تشغّلو دائم

```bash
# 1) المنطق بلا إنترنت — لازم يطلع إشارة و3 رفض مبرّر
python3.11 -m cta.cli demo

# 2) فحص عميق: كل منصة لازم ترجّع بيانات حقيقية لمحفظة معروفة
python3.11 -m cta.cli doctor --deep \
  --wallet hyperliquid=0x... \
  --wallet dydx=dydx1... \
  --wallet okx=25CD5A80241D6FE6
```

`--deep` هو الفحص اللي ما كان موجود قبل اليوم. بيسأل كل منصة:
«هل رجّعتلي صفوف حقيقية لمحفظة معروفة؟» — منصة بترد فراغ تظهر **🟡**،
مش 🟢. هاد الفرق بين «شغّال» و«شكله شغّال».

## 5. شغّلو كخدمة (systemd)

```bash
sudo ./deploy/aws/install.sh
```

أو يدوي:

```bash
sudo cp deploy/aws/copytrade.service /etc/systemd/system/
sudo sed -i "s|/home/ec2-user|$(pwd)|g" /etc/systemd/system/copytrade.service
sudo systemctl daemon-reload
sudo systemctl enable --now copytrade
sudo systemctl status copytrade
```

أوامر مفيدة:

```bash
sudo journalctl -u copytrade -f        # تابع اللوج لحظياً
sudo systemctl restart copytrade
sudo systemctl stop copytrade
```

## 6. لوحة التحكم من الجوال

اللوحة العربية موجودة (`/dash`) بس على Worker. على السيرفر:

```bash
python3.11 -m cta.preview_server --host 0.0.0.0 --port 8000
```

بعدين افتح `http://<آي-بي-السيرفر>:8000`.
**انتبه:** لازم تفتح المنفذ 8000 بالـ Security Group لعنوانك بس، وإلا بتعرض
السيرفر للعالم. الأفضل: استخدم SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 ec2-user@<ip>
```

وبعدين افتح `http://localhost:8000` على جوالك/كمبيوترك — بلا ما تفتح منفذ للعالم.

---

## 7. النسخ الاحتياطي

كل الحالة بملف واحد (SQLite): `state.db`.
انسخو مرة باليوم لـ S3 إن أردت:

```bash
aws s3 cp state.db s3://<bucket>/copytrade/$(date +%F).db
```

---

## 8. قائمة تحقّق قبل المال الحقيقي

- [ ] `demo` بطلع إشارة + رفض مبرّر
- [ ] `doctor --deep` كل المنصات 🟢 أو ⚪ (مش 🟡 ولا 🔴)
- [ ] ٤ أسابيع على الحساب الورقي، **100+ إشارة** متتبَّعة
- [ ] متوسط R موجب بعد حساب التكاليف
- [ ] إجماع ELITE أفضل من إجماع PROVEN فعلياً، مش بالكلام

إن ما اجتمعت: **ما في مال حقيقي.** هاد مش تشاؤم، هاد ترتيب.
