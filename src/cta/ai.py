"""AI assistant layer — turns the radar into a conversation.

Design constraints that matter:

1. **The model never invents numbers.** It receives a pre-computed JSON fact sheet
   (signals, watchlist, cluster members, outcomes) and is instructed to speak only
   from it. Every figure it can quote was produced by deterministic code we can
   unit-test.
2. **It never decides trades.** No tool gives the model write access to signals,
   thresholds or capital. It reads, explains, answers.
3. **It degrades gracefully.** Workers AI has a 10k neurons/day free allowance; a
   deterministic Arabic narrator answers when the model is unavailable, over
   budget, or times out. The bot never goes silent.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .models import Cluster, Signal

SYSTEM_PROMPT = """أنت "رادار" — مساعد تحليل لنظام يرصد اتفاق المتداولين المحترفين المستقلين.

قواعد صارمة لا تُخالف:
1. استخدم فقط الأرقام الموجودة في FACTS. لا تحسب ولا تخمّن ولا تُكمل أرقاماً ناقصة.
2. إن لم يكن الرقم موجوداً في FACTS، قل بوضوح: "لا توجد بيانات كافية".
3. أنت لا تنفّذ صفقات ولا تنصح بمال حقيقي. أنت تشرح ما رصده النظام.
4. تكلّم بالعربية العامية المفهومة، بإيجاز (٣-٦ أسطر) إلا إن طُلب تفصيل.
5. اذكر دائماً درجة الثقة وعدد المتداولين المستقلين عند الحديث عن إشارة.
6. إن سُئلت عن الأداء، استخدم الأرقام الفعلية فقط واذكر حجم العينة.
7. لا تذكر أبداً أنك نموذج لغوي أو أن هناك "FACTS".
8. في نهاية أي حديث عن توصية، ذكّر بسطر واحد أنه قرار شخصي وليس نصيحة مالية.
"""


# ── fact sheets ──────────────────────────────────────────────────────────────
def signal_facts(signal: Signal) -> dict[str, Any]:
    """Compact, exact representation of a signal for the model."""
    return {
        "coin": signal.coin,
        "side": signal.side,
        "score": signal.score,
        "level": signal.level,
        "entry_center": signal.entry_center,
        "market_price": signal.market_price,
        "slippage_bps": None if signal.slippage_bps is None else round(signal.slippage_bps, 1),
        "spread_bps": round(signal.spread_bps, 1),
        "n_independent": signal.n_independent,
        "sources": signal.sources,
        "stop": None if signal.plan is None else signal.plan.stop,
        "stop_source": None if signal.plan is None else signal.plan.stop_source,
        "target": None if signal.plan is None else signal.plan.target,
        "target_source": None if signal.plan is None else signal.plan.target_source,
        "rr": None if signal.plan is None else signal.plan.rr,
        "breakdown": signal.breakdown,
        "members": [
            {
                "wallet": m.get("wallet", "")[:10],
                "source": m.get("source"),
                "entry": m.get("entry"),
                "quality": m.get("quality"),
                "score": m.get("quality_score"),
                "leverage": m.get("leverage"),
                "opened_min_ago": m.get("age_min"),
            }
            for m in signal.members
        ],
    }


def cluster_facts(cluster: Cluster) -> dict[str, Any]:
    return {
        "coin": cluster.coin,
        "side": cluster.side,
        "n_independent": cluster.n_members,
        "sources": cluster.sources,
        "entry_center": cluster.entry_center,
        "spread_bps": round(cluster.spread_bps, 1),
        "time_span_min": round(cluster.time_span_min, 0),
        "median_leverage": cluster.median_leverage,
        "members": [
            {
                "wallet": m.wallet[:10],
                "source": m.source,
                "entry": m.entry,
                "quality": m.quality,
                "score": m.quality_score,
                "leverage": m.leverage,
            }
            for m in cluster.members
        ],
    }


def build_user_prompt(question: str, facts: dict[str, Any], history: list[dict]) -> str:
    """Assemble the prompt. Facts are injected as JSON, never paraphrased."""
    lines: list[str] = []
    if history:
        lines.append("سياق الحوار السابق (مختصر):")
        for turn in history[-6:]:
            role = "المستخدم" if turn.get("role") == "user" else "أنت"
            lines.append(f"- {role}: {str(turn.get('content', ''))[:300]}")
        lines.append("")
    lines.append("FACTS (المصدر الوحيد للحقائق):")
    lines.append(json.dumps(facts, ensure_ascii=False, default=str))
    lines.append("")
    lines.append(f"سؤال المستخدم: {question}")
    return "\n".join(lines)


# ── deterministic narrator (fallback + guarantees) ───────────────────────────
_AR_SIDE = {"LONG": "شراء", "SHORT": "بيع"}
_AR_ARROW = {"LONG": "🟢", "SHORT": "🔴"}
_AR_LEVEL = {
    "STRONG": "قوية جداً",
    "GOOD": "جيدة",
    "WATCH": "للمراقبة فقط",
    "REJECT": "مرفوضة",
}


def narrate_signal(signal: Signal) -> str:
    """A complete Arabic explanation with zero AI dependency.

    This is the text the user gets even if every model on earth is down.
    """
    side = _AR_SIDE.get(signal.side, signal.side)
    arrow = _AR_ARROW.get(signal.side, "•")
    level = _AR_LEVEL.get(signal.level, signal.level)

    out = [
        f"{arrow} *{signal.coin} — {side}* · إشارة {level} ({signal.score}/100)",
        "",
        f"• متداولون مستقلون: *{signal.n_independent}*",
        f"• المصادر: {', '.join(signal.sources)}",
        f"• وسط الدخول: `{signal.entry_center:g}`",
        f"• تقارب السعر: {signal.spread_bps:.1f} نقطة أساس",
    ]

    if signal.market_price:
        out.append(f"• السعر الحالي: `{signal.market_price:g}`")
    if signal.slippage_bps is not None:
        out.append(f"• الانزلاق: {signal.slippage_bps:.0f} نقطة أساس")

    if signal.plan is not None:
        p = signal.plan
        if p.stop is not None:
            tag = "" if p.stop_source == "trader" else " (محسوب)"
            out.append(f"• الستوب: `{p.stop:g}`{tag}")
        if p.target is not None:
            tag = "" if p.target_source == "trader" else " (محسوب)"
            out.append(f"• الهدف: `{p.target:g}`{tag}")
        if p.rr is not None:
            out.append(f"• العائد/المخاطرة: {p.rr}:1")

    if signal.members:
        out.append("")
        out.append("*المتداولون:*")
        for m in signal.members:
            out.append(
                f"  – `{str(m.get('wallet', ''))[:10]}` · {m.get('source')} · "
                f"{m.get('quality')} {m.get('quality_score', 0)}/100 · "
                f"دخول `{m.get('entry')}`"
            )

    out.append("")
    if signal.breakdown:
        b = signal.breakdown
        out.append(
            "تفصيل الدرجة: إجماع {consensus} · جودة {quality} · "
            "تقارب {tightness} · تزامن {synchrony} · تنوّع {diversity}".format(**b)
        )
    out.append("")
    out.append("⚠️ قرار شخصي — ليست نصيحة مالية · HYPOTHETICAL_")
    return "\n".join(out)


def narrate_no_signal(reason: str = "لا يوجد اتفاق كافٍ بين المتداولين المحترفين حالياً.") -> str:
    return (
        "🔍 لا توجد إشارة الآن\n\n"
        f"{reason}\n\n"
        "النظام يراقب باستمرار وسيرسل لك لحظة تحقق الشروط."
    )


def explain_rejection(clusters: list[Cluster], cfg) -> str:
    """Tell the user *why* nothing fired — silence breeds distrust."""
    if not clusters:
        return narrate_no_signal("لم يصل أي تقارب إلى بوابة القبول.")

    lines = ["🔍 تقاربات لم تجتز البوابة:", ""]
    for c in clusters[:5]:
        problems = []
        if c.n_members < cfg.min_independent_traders:
            problems.append(f"مستقلون {c.n_members}<{cfg.min_independent_traders}")
        if c.spread_bps > cfg.entry_tolerance_bps:
            problems.append(f"انتشار {c.spread_bps:.0f}bps>{cfg.entry_tolerance_bps:.0f}")
        if c.time_span_min > cfg.convergence_window_min:
            problems.append(f"تزامن {c.time_span_min:.0f}د>{cfg.convergence_window_min}")
        if c.median_leverage > cfg.max_median_leverage:
            problems.append(f"رافعة {c.median_leverage:.1f}x>{cfg.max_median_leverage:g}")
        if not problems:
            problems.append("درجة الثقة أقل من العتبة")
        lines.append(f"• {c.coin} {_AR_SIDE.get(c.side, c.side)} — {' · '.join(problems)}")
    return "\n".join(lines)


# ── Workers AI bridge ────────────────────────────────────────────────────────
class AiBudget:
    """Tracks daily neuron use so a chatty day cannot exhaust the free tier."""

    def __init__(self, soft_cap: int = 8_000, model_neurons_per_1k_in: float = 27.0,
                 model_neurons_per_1k_out: float = 205.0):
        self.soft_cap = soft_cap
        self.used = 0.0
        self.per_1k_in = model_neurons_per_1k_in
        self.per_1k_out = model_neurons_per_1k_out

    def estimate(self, prompt_chars: int, max_out_tokens: int = 400) -> float:
        in_tokens = prompt_chars / 3.2
        return (in_tokens / 1000) * self.per_1k_in + (max_out_tokens / 1000) * self.per_1k_out

    def allows(self, prompt_chars: int, max_out_tokens: int = 400) -> bool:
        return self.used + self.estimate(prompt_chars, max_out_tokens) <= self.soft_cap

    def charge(self, prompt_chars: int, out_tokens: int) -> None:
        self.used += self.estimate(prompt_chars, out_tokens)

    def reset(self) -> None:
        self.used = 0.0


async def ask_ai(
    env,
    question: str,
    facts: dict[str, Any],
    history: Optional[list[dict]] = None,
    budget: Optional[AiBudget] = None,
    cfg=None,
) -> Optional[str]:
    """Call Workers AI. Returns None when the model is unavailable — caller falls back."""
    ai = getattr(env, "AI", None)
    if ai is None:
        return None

    prompt = build_user_prompt(question, facts, history or [])

    if budget is not None and not budget.allows(len(prompt)):
        return None

    model = cfg.ai_model if cfg is not None else "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        response = await ai.run(
            model,
            {"messages": messages, "max_tokens": 512, "temperature": 0.3},
        )
    except Exception:
        return None

    text = ""
    if isinstance(response, dict):
        text = response.get("response") or ""
        if not text and isinstance(response.get("result"), dict):
            text = response["result"].get("response", "")
    elif isinstance(response, str):
        text = response

    text = (text or "").strip()
    if not text:
        return None

    if budget is not None:
        budget.charge(len(prompt), out_tokens=max(1, len(text) // 3))
    return text
