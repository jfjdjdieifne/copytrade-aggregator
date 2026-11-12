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
) -> str:
    """Return the full HTML page as a string."""
    banner = (
        '<div class="banner live">🟢 بيانات حيّة من المنصات</div>'
        if live
        else '<div class="banner demo">🟡 عرض تجريبي — هذه البيئة لا تصل لمنصات التداول'
        "<br><small>الأرقام أدناه محسوبة بالكود الحقيقي على بيانات تجريبية، "
        "ولتوليد إشارات حيّة انشر الـ Worker على Cloudflare أو شغّله من جهازك.</small></div>"
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
.banner.live{{background:#0f2417;border:1px solid #1e4d2b;color:#63e6a0}}
.pills{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}}
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
