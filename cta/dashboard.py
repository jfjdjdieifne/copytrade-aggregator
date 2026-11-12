"""Self-contained HTML dashboard — one file, no build step, no CDN.

Served by `cta.preview_server` locally and available from the Worker at `/dash`.
Renders whatever state the radar holds; when a running engine has live data it
shows live data, otherwise it shows the demo snapshot and says so explicitly.

Everything is inline (CSS + JS) so it works on a locked-down mobile browser with
no outbound requests at all.
"""

from __future__ import annotations

import html
import json
from typing import Optional

_AR_SIDE = {"LONG": "شراء", "SHORT": "بيع"}

# Maximum points each scoring component can contribute (35+30+20+10+5 = 100).
_SCORE_MAX = {
    "consensus": 35,
    "quality": 30,
    "tightness": 20,
    "synchrony": 10,
    "diversity": 5,
}
_LEVEL_COLOR = {
    "STRONG": "#16c784",
    "GOOD": "#2f9e44",
    "WATCH": "#f59f00",
    "REJECT": "#e03131",
}
_AR_LEVEL = {
    "STRONG": "قوية جداً",
    "GOOD": "جيدة",
    "WATCH": "للمراقبة",
    "REJECT": "مرفوضة",
}


def _fmt(value: Optional[float], digits: int = 6) -> str:
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(num) >= 1_000:
        return f"{num:,.2f}"
    return f"{num:.{digits}g}"


def _signal_card(sig: dict) -> str:
    side = sig.get("side", "LONG")
    level = sig.get("level", "WATCH")
    color = _LEVEL_COLOR.get(level, "#868e96")
    plan = sig.get("plan") or {}
    arrow = "🟢" if side == "LONG" else "🔴"

    members = "".join(
        f"""
        <div class="member">
          <span class="w">{html.escape(str(m.get('wallet', ''))[:10])}</span>
          <span class="tag">{html.escape(str(m.get('source', '')))}</span>
          <span class="grade">{html.escape(str(m.get('quality', '')))} {m.get('quality_score', 0)}</span>
          <span class="entry">{_fmt(m.get('entry'))}</span>
          <span class="age">{int(m.get('age_min') or 0)}د</span>
        </div>"""
        for m in sig.get("members", [])
    )

    breakdown = sig.get("breakdown") or {}
    labels = {
        "consensus": "إجماع",
        "quality": "جودة",
        "tightness": "تقارب",
        "synchrony": "تزامن",
        "diversity": "تنوّع",
    }

    def _pct(key: str, value: float) -> float:
        """Bar width as a fraction of that component's own maximum."""
        top = _SCORE_MAX.get(key)
        if not top:
            return 0.0
        return max(0.0, min(100.0, value / top * 100))

    bars = "".join(
        f'<div class="bar"><span>{html.escape(str(labels.get(k, k)))}</span>'
        f'<div class="track"><i style="width:{_pct(k, v):.0f}%"></i></div>'
        f"<b>{html.escape(str(v))}</b></div>"
        for k, v in breakdown.items()
    )

    return f"""
    <article class="card" style="--accent:{color}">
      <header>
        <div>
          <h2>{arrow} {html.escape(str(sig.get('coin')))} — {_AR_SIDE.get(side, side)}</h2>
          <p class="sub">{html.escape(str(sig.get('n_independent')))} متداول مستقل ·
             {html.escape(', '.join(sig.get('sources', [])))}</p>
        </div>
        <div class="score"><b>{sig.get('score')}</b><span>/100</span>
          <em>{_AR_LEVEL.get(level, level)}</em></div>
      </header>

      <div class="grid">
        <div><label>وسط الدخول</label><strong>{_fmt(sig.get('entry_center'))}</strong></div>
        <div><label>السعر الحالي</label><strong>{_fmt(sig.get('market_price'))}</strong></div>
        <div><label>الستوب</label><strong>{_fmt(plan.get('stop'))}
          {'' if plan.get('stop_source') == 'trader' else '<i class="warn">محسوب</i>'}</strong></div>
        <div><label>الهدف</label><strong>{_fmt(plan.get('target'))}</strong></div>
        <div><label>R:R</label><strong>{plan.get('rr') or '—'}</strong></div>
        <div><label>تقارب السعر</label><strong>{round(sig.get('spread_bps') or 0, 1)} bps</strong></div>
      </div>

      <div class="members">{members}</div>
      <div class="bars">{bars}</div>
      <footer>HYPOTHETICAL_ · تحليل آلي وليس نصيحة مالية</footer>
    </article>"""


def render_dashboard(
    signals: list[dict],
    watchlist: list[dict],
    stats: dict,
    rejections: list[dict],
    *,
    live: bool = False,
    source_health: Optional[dict] = None,
    last_cycle: str = "—",
    base_url: str = "",
) -> str:
    """Return the full HTML page as a string."""
    banner = (
        '<div class="banner live">🟢 بيانات حيّة من المنصات</div>'
        if live
        else '<div class="banner demo">🟡 عرض تجريبي — هذه البيئة لا تصل لمنصات التداول'
        "<br><small>الأرقام أدناه محسوبة بالكود الحقيقي على بيانات تجريبية، "
        "ولتوليد إشارات حيّة انشر الـ Worker على Cloudflare أو شغّله من جهازك.</small></div>"
    )

    # On a live deployment, show the addresses that matter instead of making the
    # user hunt through the Cloudflare dashboard for their Worker URL.
    links = (
        f'<div class="links">'
        f'<div><label>رابط الـ Worker</label><code>{html.escape(base_url)}</code></div>'
        f'<div><label>ربط تلغرام</label>'
        f'<code>{html.escape(base_url)}/telegram/setup</code></div>'
        f"</div>"
        if base_url
        else ""
    )

    health = source_health or {}
    health_html = "".join(
        f'<span class="pill {"ok" if ok else "bad"}">{html.escape(str(name))} '
        f'{"🟢" if ok else "🔴"}</span>'
        for name, ok in health.items()
    ) or '<span class="pill bad">لا اتصال بالمنصات</span>'

    cards = "".join(_signal_card(s) for s in signals) or (
        '<div class="empty">لا توجد إشارات بعد — وهذا متوقع: '
        "النظام ينتظر توافق متداولين محترفين مستقلين، وهو حدث نادر.</div>"
    )

    elite = sum(1 for w in watchlist if w.get("grade") == "ELITE")
    proven = sum(1 for w in watchlist if w.get("grade") == "PROVEN")

    watch_rows = "".join(
        f"""<tr><td><code>{html.escape(str(w.get('wallet', ''))[:12])}</code></td>
        <td>{html.escape(str(w.get('source', '')))}</td>
        <td><span class="g {str(w.get('grade', '')).lower()}">{html.escape(str(w.get('grade', '')))}</span></td>
        <td>{w.get('score', 0)}</td><td>{w.get('n_trades', 0)}</td>
        <td>{w.get('profit_factor', 0)}</td>
        <td>{round((w.get('max_drawdown') or 0) * 100, 1)}%</td></tr>"""
        for w in watchlist[:25]
    ) or '<tr><td colspan="7" class="muted">القائمة ستُبنى عند أول تشغيل متصل</td></tr>'

    rej_html = "".join(
        f'<li><b>{html.escape(str(r.get("coin")))}</b> {_AR_SIDE.get(r.get("side", ""), "")} — '
        f'{html.escape(str(r.get("reason")))}</li>'
        for r in rejections
    ) or "<li class='muted'>لا تقاربات مرفوضة في آخر دورة</li>"

    stats_json = json.dumps(
        {"signals": len(signals), "watchlist": len(watchlist), "stats": stats},
        ensure_ascii=False,
    )

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<title>رادار إجماع المحترفين</title>
<style>
:root{{--bg:#0b0f14;--card:#141a22;--line:#1f2731;--txt:#e8eef6;--dim:#8b97a6;--accent:#2f9e44}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
body{{margin:0;background:var(--bg);color:var(--txt);
 font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
 padding:14px;padding-bottom:60px}}
h1{{font-size:19px;margin:0 0 4px;font-weight:700}}
.muted{{color:var(--dim)}}
header.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:12px}}
.banner{{border-radius:12px;padding:11px 13px;font-size:13px;margin-bottom:14px;line-height:1.6}}
.banner.demo{{background:#2a2113;border:1px solid #5c4a1d;color:#f2cf72}}
.card.bad{{background:#2a1313;border:1px solid #6b2525;color:#ffb4b4;padding:12px;border-radius:10px;margin:12px 0}}
.banner.live{{background:#0f2417;border:1px solid #1e4d2b;color:#63e6a0}}
.pills{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}}
.links{{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:10px 12px;margin-bottom:14px;display:flex;flex-direction:column;gap:8px}}
.links label{{display:block;font-size:10px;color:var(--dim);margin-bottom:2px}}
.links code{{font-size:11px;word-break:break-all;color:#8ad8ff}}
.pill{{font-size:11px;padding:4px 9px;border-radius:99px;background:#1a2129;border:1px solid var(--line)}}
.pill.ok{{color:#63e6a0}} .pill.bad{{color:#ff8787}}
.kpis{{display:grid;grid-template-columns:repeat(2,1fr);gap:9px;margin-bottom:16px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px}}
.kpi b{{display:block;font-size:22px;line-height:1.2}}
.kpi span{{font-size:11px;color:var(--dim)}}
.card{{background:var(--card);border:1px solid var(--line);border-right:3px solid var(--accent);
 border-radius:14px;padding:14px;margin-bottom:14px}}
.card header{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}
.card h2{{font-size:16px;margin:0}}
.sub{{margin:2px 0 0;font-size:12px;color:var(--dim)}}
.score{{text-align:center;flex:0 0 auto}}
.score b{{font-size:26px;color:var(--accent);display:block;line-height:1}}
.score span{{font-size:11px;color:var(--dim)}}
.score em{{display:block;font-size:10px;font-style:normal;color:var(--accent);margin-top:2px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0;
 padding:11px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}}
.grid label{{display:block;font-size:10px;color:var(--dim);margin-bottom:2px}}
.grid strong{{font-size:13px;font-weight:600;display:block;direction:ltr;text-align:right}}
.warn{{font-size:9px;color:#f2cf72;font-style:normal;background:#2a2113;
 border-radius:4px;padding:0 3px;margin-right:3px}}
.members{{display:flex;flex-direction:column;gap:5px;margin-bottom:11px}}
.member{{display:grid;grid-template-columns:1fr auto auto auto auto;gap:7px;align-items:center;
 font-size:11px;background:#0f141a;border-radius:8px;padding:6px 8px}}
.member .w{{font-family:ui-monospace,monospace;color:#8ad8ff;min-width:0;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.member .tag{{color:var(--dim);font-size:10px}}
.member .grade{{color:#63e6a0;font-size:10px}}
.member .entry{{direction:ltr;font-family:ui-monospace,monospace}}
.member .age{{color:var(--dim);font-size:10px}}
.bars{{display:flex;flex-direction:column;gap:5px}}
.bar{{display:grid;grid-template-columns:52px 1fr 22px;gap:7px;align-items:center;font-size:10px;
 color:var(--dim)}}
.bar .track{{height:5px;background:#0f141a;border-radius:99px;overflow:hidden}}
.bar .track i{{display:block;height:100%;background:var(--accent);border-radius:99px}}
.bar b{{color:var(--txt);font-size:10px;text-align:left}}
.card footer{{margin-top:11px;padding-top:9px;border-top:1px solid var(--line);
 font-size:10px;color:var(--dim)}}
h3{{font-size:14px;margin:20px 0 9px;color:var(--dim);font-weight:600}}
.tablewrap{{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:12px;
 border:1px solid var(--line);margin-bottom:4px}}
table{{width:100%;min-width:540px;border-collapse:collapse;font-size:11px;
 background:var(--card)}}
table tr:last-child td{{border-bottom:none}}
th,td{{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line)}}
th{{color:var(--dim);font-weight:600;font-size:10px}}
code{{font-family:ui-monospace,monospace;color:#8ad8ff;font-size:10px}}
.g.elite{{color:#63e6a0}} .g.proven{{color:#8ad8ff}} .g.watch{{color:#f2cf72}}
.g.reject{{color:#ff8787}}
ul{{list-style:none;padding:0;margin:0;font-size:12px}}
ul li{{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:9px 11px;margin-bottom:6px}}
.empty{{background:var(--card);border:1px dashed var(--line);border-radius:14px;
 padding:22px;text-align:center;color:var(--dim);font-size:13px}}
footer.bottom{{margin-top:24px;font-size:10px;color:var(--dim);text-align:center;line-height:1.9}}
</style>
</head>
<body>

<header class="top">
  <div>
    <h1>🎯 رادار إجماع المحترفين</h1>
    <p class="muted" style="margin:0;font-size:11px">Copy-Trade Aggregator v2</p>
  </div>
</header>

{banner}
{links}
<div class="pills">{health_html}</div>

<div class="kpis">
  <div class="kpi"><b>{len(signals)}</b><span>إشارات</span></div>
  <div class="kpi"><b>{len(watchlist)}</b><span>متداول مراقَب</span></div>
  <div class="kpi"><b>{elite}</b><span>ELITE</span></div>
  <div class="kpi"><b>{proven}</b><span>PROVEN</span></div>
</div>

<h3>الإشارات</h3>
{cards}

<h3>لماذا لم تُرسل إشارات أخرى</h3>
<ul>{rej_html}</ul>

<h3>قائمة المراقبة</h3>
<div class="tablewrap"><table>
<thead><tr><th>المحفظة</th><th>المنصة</th><th>التقييم</th><th>نقاط</th>
<th>صفقات</th><th>PF</th><th>سحب</th></tr></thead>
<tbody>{watch_rows}</tbody>
</table></div>

<footer class="bottom">
  آخر دورة: {html.escape(last_cycle or '—')}<br>
  HYPOTHETICAL_ — تحليل آلي، ليس نصيحة مالية. لا تنفيذ صفقات تلقائياً.
  <details style="margin-top:6px">
    <summary style="cursor:pointer">تفاصيل تقنية</summary>
    <span style="direction:ltr;display:block;margin-top:6px">{html.escape(stats_json or '')}</span>
  </details>
</footer>

</body>
</html>"""


_SETUP_CSS = """
:root{--bg:#0b0f14;--card:#141a22;--line:#1f2731;--txt:#e8eef6;--dim:#8b97a6;
 --ok:#16c784;--bad:#e03131;--warn:#f59f00}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);
 font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
 padding:16px;padding-bottom:60px}
h1{font-size:20px;margin:0 0 14px}
.hero{border-radius:16px;padding:16px;margin-bottom:16px;font-size:15px;line-height:1.7}
.hero.good{background:#0f2417;border:1px solid #1e4d2b;color:#63e6a0}
.hero.bad{background:#2a1414;border:1px solid #5c1d1d;color:#ff8787}
.hero b{font-size:17px;display:block;margin-bottom:6px}
ol,ul{list-style:none;padding:0;margin:0}
.step{background:var(--card);border:1px solid var(--line);border-radius:14px;
 padding:13px 15px;margin-bottom:10px;display:flex;gap:11px;align-items:flex-start}
.step .mark{flex:0 0 24px;height:24px;border-radius:50%;display:grid;place-items:center;
 font-size:13px;font-weight:700}
.step.done .mark{background:#0f2417;color:var(--ok);border:1px solid #1e4d2b}
.step.todo .mark{background:#2a2113;color:var(--warn);border:1px solid #5c4a1d}
.step .body{min-width:0;flex:1}
.step .t{font-size:14px;font-weight:600;margin-bottom:3px}
.step .d{font-size:12px;color:var(--dim);line-height:1.6}
.step code{display:block;background:#0f141a;border-radius:7px;padding:7px 9px;
 margin-top:6px;font-size:11px;color:#8ad8ff;word-break:break-all;direction:ltr}
a{color:#8ad8ff}
.note{font-size:11px;color:var(--dim);text-align:center;margin-top:22px;line-height:1.9}
details{margin-top:14px;font-size:11px;color:var(--dim)}
summary{cursor:pointer}
pre{background:#0f141a;border-radius:8px;padding:10px;overflow-x:auto;
 direction:ltr;text-align:left;font-size:10px}
"""


def _open_instruction(bot_username: str) -> str:
    who = "@" + html.escape(bot_username) if bot_username else "اسم بوتك"
    return f"افتح تلغرام وابحث عن <b>{who}</b>، ثم أرسل:"


def render_setup_page(
    *,
    ok: bool,
    worker_url: str = "",
    dashboard_url: str = "",
    chat_id_set: bool = True,
    registered_url: str = "",
    expected_url: str = "",
    telegram_said: str = "",
    last_error: str = "",
    missing: str = "",
    bot_username: str = "",
    token_ok: bool = True,
    token_error: str = "",
    pending_updates: int = 0,
    owner_linked: bool = False,
    ai_binding: bool = True,
    trace: list | None = None,
) -> str:
    """Human-readable first-run page.

    `/setup` used to return raw JSON, which reads as an error to anyone who is
    not a developer — the operator was left staring at a wall of braces not
    knowing whether it worked. This renders the same facts as a checklist where
    every step shows its own state, so "what do I do now" is answered on screen.
    """
    if missing:
        hero = (
            '<div class="hero bad"><b>⚠️ ناقص إعداد</b>'
            f"لم أجد <code>{html.escape(missing)}</code>. "
            "أضفه في Settings ← Variables and Secrets ثم أعد النشر، "
            "وبعدها أعد تحميل هذه الصفحة.</div>"
        )
    elif not token_ok:
        hero = (
            '<div class="hero bad"><b>⚠️ التوكن غير صالح</b>'
            f"تلغرام رفض التوكن: <code>{html.escape(token_error or 'unauthorized')}</code><br>"
            "تأكد أنك نسخت <b>التوكن كاملاً</b> من @BotFather (يبدأ بأرقام ثم نقطتين). "
            "إن كنت ألغيت التوكن سابقاً (Revoke) فالتوكن القديم لم يعد يعمل — "
            "خُذ الجديد من @BotFather ← /mybots ← الاختيار ← API Token.</div>"
        )
    elif ok:
        who = f"@{html.escape(bot_username)}" if bot_username else "بوتك"
        hero = (
            '<div class="hero good"><b>✅ تم الربط بنجاح</b>'
            f"التوكن صالح وهو لبوت <b>{who}</b>. تلغرام صار يعرف عنوان الـ Worker. "
            f"الخطوة الوحيدة الباقية: افتح <b>{who}</b> في تلغرام وأرسل <b>/help</b>.</div>"
        )
    else:
        hero = (
            '<div class="hero bad"><b>⚠️ لم يكتمل الربط</b>'
            "تلغرام رفض التسجيل. السبب مذكور بالأسفل — غالباً التوكن غير صحيح "
            "أو الـ Worker غير منشور.</div>"
        )

    def step(state: str, title: str, desc: str, code: str = "") -> str:
        mark = "✓" if state == "done" else "!"
        code_html = f"<code>{html.escape(code)}</code>" if code else ""
        return (
            f'<li class="step {state}"><span class="mark">{mark}</span>'
            f'<div class="body"><div class="t">{title}</div>'
            f'<div class="d">{desc}</div>{code_html}</div></li>'
        )

    steps = [
        step(
            "done",
            "الـ Worker يعمل",
            "هذه الصفحة نفسها دليل: الخادم استجاب لطلبك.",
        ),
        step(
            "done" if owner_linked else "todo",
            "مالك البوت",
            "مربوط." if owner_linked else
            "يُربط تلقائياً: افتح بوتك وأرسل <b>/start</b> — أول محادثة تصبح المالكة. "
            "لا تحتاج أي إعداد يدوي.",
            "",
        ),
        step(
            "done" if ok else "todo",
            "ربط تلغرام (webhook)",
            "مسجَّل عند تلغرام." if ok else
            "لم يكتمل. راجع الخطأ بالأسفل ثم أعد تحميل الصفحة.",
            registered_url or expected_url or "",
        ),
        step(
            "todo",
            "جرّب البوت — الآن",
            _open_instruction(bot_username),
            "/help",
        ),
    ]

    if last_error:
        hero += (
            '<div class="hero bad" style="margin-top:10px"><b>⚠️ تلغرام يقول إن الوصول فشل</b>'
            f"آخر خطأ سجّله تلغرام عند محاولة تسليم رسالة إلى هذا الـ Worker: "
            f"<code>{html.escape(last_error)}</code><br>"
            "هذا يعني أن الربط مسجَّل لكن الرسائل ترتد. أعد النشر ثم أعد تحميل الصفحة.</div>"
        )

    if trace:
        rows = "".join(
            f"<div class=\"tl\"><span class=\"tt\">{html.escape(str(t.get('t',''))[-8:])}</span>"
            f"<span class=\"te\">{html.escape(str(t.get('event','')))}</span>"
            f"<span class=\"td\">{html.escape(str(t.get('error','') or t.get('text','') or ''))[:90]}</span></div>"
            for t in reversed(trace[-6:])
        )
        hero += (
            '<details style="margin-top:10px"><summary>آخر الرسائل التي وصلت الـ Worker</summary>'
            f'<div class="traces">{rows}</div>'
            "<p style=\"font-size:12px;color:#8fa3b8\">"
            "إن كانت هذه القائمة فاضية تماماً فمعناه أن تلغرام لا يصل إلى هنا أصلاً. "
            "وإن كانت فيها سطور فمعناه أن الرسائل تصل والرد هو الذي يفشل — وهذا يوضّح أين المشكلة.</p>"
            "</details>"
        )

    if pending_updates and ok:
        hero += (
            '<div class="hero bad" style="margin-top:10px"><b>⚠️ رسائل لم تُعالَج</b>'
            f"تلغرام يقول إن عنده <b>{pending_updates}</b> تحديثاً لم يستلمه الـ Worker. "
            "هذا يعني أن الربط مسجَّل لكن الرسائل لا تصل. أعد تحميل هذه الصفحة، "
            "وإن بقي الرقم ثابتاً فأرسل لي هذا الرقم.</div>"
        )

    diag = ""
    if last_error or telegram_said or not ok:
        diag = (
            "<details><summary>تفاصيل تقنية</summary><pre>"
            f"expected : {html.escape(expected_url)}\n"
            f"registered: {html.escape(registered_url)}\n"
            f"telegram : {html.escape(telegram_said)}\n"
            f"error    : {html.escape(last_error)}"
            "</pre></details>"
        )

    dash = (
        f'<li class="step todo"><span class="mark">→</span><div class="body">'
        f'<div class="t">اللوحة الحيّة</div>'
        f'<div class="d">حالة النظام والمتداولون المراقَبون:</div>'
        f"<code>{html.escape(dashboard_url)}</code></div></li>"
        if dashboard_url
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<title>تهيئة تلغرام — رادار الإجماع</title>
<style>{_SETUP_CSS}
.traces{{margin-top:8px}}
.tl{{display:flex;gap:8px;padding:6px 8px;border-bottom:1px solid #232c36;font-size:12px;align-items:center}}
.tt{{color:#63e6a0;direction:ltr;font-variant-numeric:tabular-nums}}
.te{{color:#f2cf72;min-width:64px}}
.td{{color:#8fa3b8;flex:1;word-break:break-all}}
</style>
</head>
<body>
<h1>🔗 تهيئة تلغرام</h1>
{hero}
<ul>{"".join(steps)}</ul>
<ul>{dash}</ul>
{diag}
<div class="note">
  رابط الـ Worker:<br>
  <code style="font-size:11px">{html.escape(worker_url)}</code><br>
  HYPOTHETICAL_ — تحليل آلي، ليس نصيحة مالية.
</div>
</body>
</html>"""


def render_state_error(reason: str, base_url: str = "") -> str:
    """Shown instead of the dashboard when state cannot be read.

    The alternative — rendering an empty dashboard — is what made a broken
    Durable Object look like a quiet market for a whole deployment: every number
    was zero, and zero is a plausible reading of "no signal yet".
    """
    safe = html.escape(reason or "state backend unavailable")
    setup = (
        f'<p><a class="btn" href="{html.escape(base_url)}/setup">افتح الإعداد</a></p>'
        if base_url
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>الذاكرة غير متاحة</title>
<style>{_SETUP_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>🔴 الذاكرة غير متاحة</h1>
  <p>الموقع يعمل، لكنه <strong>لا يستطيع قراءة حالته المحفوظة</strong> — لذلك لن
  يعرض أرقاماً. الأصفار التي كانت تُعرض سابقاً لم تكن قياساً، كانت غياب بيانات.</p>
  <div class="card bad">
    <strong>السبب:</strong>
    <code style="direction:ltr;display:block;margin-top:8px">{safe}</code>
  </div>
  <h2>ما معنى هذا</h2>
  <p>الـ Worker يصل إلى الإنترنت ويعمل، لكن ربط <code>STATE</code> (Durable Object)
  لا يصل إليه بشكل قابل للاستخدام. هذا خلل في الكود أو في الربط، وليس في حسابك.</p>
  {setup}
  <p class="foot">HYPOTHETICAL_ — تحليل آلي، ليس نصيحة مالية.</p>
</div>
</body>
</html>"""


def render_selftest(report: dict, base_url: str = "") -> str:
    """One screen that answers "is it actually working?" without waiting.

    The old flow was: deploy, send /start, and hope. When nothing arrived there
    was nothing to inspect, and each guess cost a redeploy. This renders the
    platform's own answers — every row is a value the platform returned, not an
    inference — so a passing page means the pipeline works end to end.
    """
    checks = report.get("checks", {}) or {}

    titles = {
        "state": "الذاكرة (حفظ وقراءة)",
        "fetch": "الاتصال بالإنترنت",
        "token": "صلاحية توكن البوت",
        "webhook": "ربط تلغرام بالـ Worker",
        "send": "إرسال رسالة فعلية",
        "ai": "الذكاء الاصطناعي",
        "clock": "الوقت",
    }
    hints = {
        "state": "لو فشلت، اللوحة ستعرض أصفاراً غير حقيقية — هذا أهم فحص.",
        "fetch": "لو فشلت، لا شيء يصل للمنصات ولا لتلغرام.",
        "token": "لو فشلت، خذ توكناً جديداً من @BotFather.",
        "webhook": "لو فشلت، افتح /setup مرة واحدة وسيُصلح نفسه.",
        "send": "لو فشلت، أرسل /start للبوت أولاً ليصبح مالكاً.",
        "ai": "بدونه يشرح النظام بجُمل جاهزة بدل صياغة حرة — لا يعطّل شيئاً.",
        "clock": "للمطابقة مع سجلات كلاودفلير.",
    }

    rows = []
    for key in ("state", "fetch", "token", "webhook", "send", "ai", "clock"):
        c = checks.get(key, {})
        ok = c.get("ok")
        mark = "✅" if ok else "❌"
        detail = c.get("detail", "") or ""
        extra = ""
        if key == "token" and c.get("bot_username"):
            extra = f"@{c['bot_username']}"
        elif key == "webhook":
            extra = c.get("url", "") or "غير مسجّل"
            if c.get("pending"):
                extra += f" · {c['pending']} معلّقة"
        elif key == "send" and c.get("chat_id"):
            extra = c.get("chat_id", "")
        elif key == "ai" and c.get("model"):
            extra = c.get("model", "")
        elif key == "state":
            extra = c.get("backend", "")
        elif key == "clock":
            extra = c.get("utc", "")

        rows.append(
            f'<div class="row {("ok" if ok else "no")}">'
            f'<span class="mark">{mark}</span>'
            f'<div><div class="t">{html.escape(titles[key])}</div>'
            f'<div class="d">{html.escape(extra)}</div>'
            + (f'<div class="h">{html.escape(detail)}</div>' if detail else "")
            + (f'<div class="hint">{html.escape(hints[key])}</div>' if not ok else "")
            + "</div></div>"
        )

    all_ok = report.get("ok")
    hero = (
        '<div class="hero good"><b>✅ كل شيء يعمل</b>'
        "الفحوصات السبعة نجحت. أرسل /help إلى بوتك وسيرد عليك.</div>"
        if all_ok
        else '<div class="hero bad"><b>⚠️ في شيء لا يعمل</b>'
        "السبب مكتوب تحت كل فحص فاشل. أعد تحميل الصفحة بعد أي إصلاح.</div>"
    )

    dash = (
        f'<p><a class="btn" href="{html.escape(base_url)}/dash">اللوحة الحيّة</a> '
        f'<a class="btn" href="{html.escape(base_url)}/setup">إعداد تلغرام</a></p>'
        if base_url
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<title>فحص النظام</title>
<style>{_SETUP_CSS}
.row{{display:flex;gap:10px;padding:12px 14px;border-radius:10px;margin:8px 0;
background:#141a21;border:1px solid #232c36}}
.row.ok{{border-color:#1e4d2b}} .row.no{{border-color:#6b2525}}
.row .mark{{font-size:18px}} .row .t{{font-weight:700;margin-bottom:2px}}
.row .d{{font-size:12px;color:#8fa3b8;direction:ltr;text-align:right;word-break:break-all}}
.row .h{{font-size:12px;color:#ffb4b4;margin-top:6px;direction:ltr;text-align:right}}
.row .hint{{font-size:12px;color:#f2cf72;margin-top:6px}}
</style>
</head>
<body>
<h1>🧪 فحص النظام</h1>
{hero}
{''.join(rows)}
{dash}
<p class="foot">HYPOTHETICAL_ — تحليل آلي، ليس نصيحة مالية.</p>
</body>
</html>"""
