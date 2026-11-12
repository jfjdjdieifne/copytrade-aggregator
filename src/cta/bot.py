"""Telegram brain — routes commands, runs the assistant, never invents numbers.

Flow for any message:
    1. slash command  -> deterministic handler (always works, no AI)
    2. callback query -> deterministic handler
    3. free text      -> AI with a fact sheet; falls back to deterministic narration

The ordering matters: the things you rely on daily must not depend on a model
being available, in budget, or in a good mood.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .ai import (  # noqa: F401
    AiBudget,
    ask_ai,
    explain_rejection,
    narrate_no_signal,
    narrate_signal,
    signal_facts,
)
from .config import Config
from .state import (
    KEY_STATS,
    KEY_WATCHLIST,
    Store,
    get_chat_history,
    get_signals,
    record_chat,
)
from .telegram import HELP_TEXT, Telegram, main_keyboard, parse_update, signal_keyboard

UTC = timezone.utc

_AR_SIDE = {"LONG": "شراء", "SHORT": "بيع"}


class Bot:
    """Stateless-per-request handler; all continuity lives in the Store."""

    def __init__(self, tg: Telegram, store: Store, env=None, cfg: Optional[Config] = None):
        self.tg = tg
        self.store = store
        self.env = env
        self.cfg = cfg or Config()
        self.budget = AiBudget(soft_cap=self.cfg.ai_daily_neuron_soft_cap)

    # ── entry point ─────────────────────────────────────────────────────────
    async def handle_update(self, update: dict) -> dict:
        parsed = parse_update(update)
        if parsed is None:
            return {"handled": False}

        chat_id = parsed["chat_id"]
        text = parsed["text"]

        if parsed["kind"] == "callback":
            await self.tg.answer_callback(parsed["callback_id"])
            reply = await self._route_callback(text, chat_id)
            if reply:
                await self.tg.send(reply, chat_id, keyboard=main_keyboard())
            return {"handled": True, "kind": "callback"}

        if not text:
            await self.tg.send(
                "أرسل نصاً أو استخدم /help لعرض الأوامر.", chat_id, keyboard=main_keyboard()
            )
            return {"handled": True, "kind": "empty"}

        if text.startswith("/"):
            reply, keyboard = await self._route_command(text, chat_id)
            await self.tg.send(reply, chat_id, keyboard=keyboard or main_keyboard())
            return {"handled": True, "kind": "command"}

        await self.tg.typing(chat_id)
        reply = await self._converse(text, chat_id)
        await self.tg.send(reply, chat_id, keyboard=main_keyboard())
        return {"handled": True, "kind": "chat"}

    # ── commands ────────────────────────────────────────────────────────────
    async def _route_command(self, text: str, chat_id: str) -> tuple[str, Optional[list]]:
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("start", "help"):
            return HELP_TEXT, main_keyboard()
        if cmd == "latest":
            return await self.cmd_latest()
        if cmd == "positions":
            return await self.cmd_positions()
        if cmd == "watchlist":
            return await self.cmd_watchlist()
        if cmd == "stats":
            return await self.cmd_stats()
        if cmd == "why":
            return await self.cmd_why(arg)
        if cmd == "health":
            return await self.cmd_health(), None
        if cmd == "details":
            return await self.cmd_details(arg), None

        return (
            f"أمر غير معروف: `{cmd}`\nاستخدم /help لعرض الأوامر المتاحة.",
            main_keyboard(),
        )

    async def _route_callback(self, data: str, chat_id: str) -> Optional[str]:
        if not data:
            return None
        if data.startswith("det:"):
            return await self.cmd_details(data.split(":", 1)[1])
        if data.startswith("why:"):
            sig = await self._find_signal(data.split(":", 1)[1])
            return self._explain(sig) if sig else "الإشارة غير موجودة في السجل."
        if data == "positions":
            return await self.cmd_positions()
        if data == "latest":
            return await self.cmd_latest()
        if data == "watchlist":
            return await self.cmd_watchlist()
        if data == "stats":
            return await self.cmd_stats()
        if data == "health":
            return await self.cmd_health()
        return None

    # ── command implementations ─────────────────────────────────────────────
    async def cmd_latest(self) -> tuple[str, Optional[list]]:
        signals = await get_signals(self.store)
        if not signals:
            return narrate_no_signal("لم تُرصد أي إشارة منذ آخر تشغيل."), main_keyboard()

        latest = signals[-1]
        text = self._render_signal(latest)
        return text, signal_keyboard(latest.get("signal_id", ""))

    async def cmd_positions(self) -> tuple[str, Optional[list]]:
        positions = await self.store.get("positions_cache", []) or []
        if not positions:
            return (
                "📈 لا توجد مراكز مُخزّنة بعد.\n"
                "المراكز تُحدَّث كل دقيقة بعد أول دورة مراقبة.",
                main_keyboard(),
            )

        lines = ["📈 *المراكز الحالية (المتداولون المراقَبون)*", ""]
        by_coin: dict[str, list[dict]] = {}
        for p in positions:
            by_coin.setdefault(p.get("coin", "?"), []).append(p)

        for coin in sorted(by_coin):
            longs = [p for p in by_coin[coin] if p.get("side") == "LONG"]
            shorts = [p for p in by_coin[coin] if p.get("side") == "SHORT"]
            lines.append(
                f"*{coin}* — 🟢 {len(longs)} شراء · 🔴 {len(shorts)} بيع"
            )
            for p in sorted(by_coin[coin], key=lambda x: x.get("quality_score", 0), reverse=True)[:4]:
                arrow = "🟢" if p.get("side") == "LONG" else "🔴"
                lines.append(
                    f"  {arrow} `{str(p.get('wallet', ''))[:10]}` · {p.get('source')} · "
                    f"{p.get('quality')} {p.get('quality_score', 0)} · دخول `{p.get('entry')}`"
                )
        lines.append("")
        lines.append("⚠️ المراكز بيانات عامة من المنصات، وليست توصيات.")
        return "\n".join(lines), main_keyboard()

    async def cmd_watchlist(self) -> tuple[str, Optional[list]]:
        watchlist = await self.store.get(KEY_WATCHLIST, []) or []
        if not watchlist:
            return (
                "👥 قائمة المراقبة فارغة.\n"
                "شغّل دورة التقييم: `curl -X POST <worker>/refresh`",
                main_keyboard(),
            )

        elite = [w for w in watchlist if w.get("grade") == "ELITE"]
        proven = [w for w in watchlist if w.get("grade") == "PROVEN"]

        lines = [
            f"👥 *قائمة المراقبة* — {len(watchlist)} متداول",
            f"ELITE: {len(elite)} · PROVEN: {len(proven)}",
            "",
        ]
        for w in watchlist[:12]:
            lines.append(
                f"• `{str(w.get('wallet', ''))[:10]}` · {w.get('grade')} {w.get('score', 0)}/100 · "
                f"{w.get('n_trades', 0)} صفقة · PF {w.get('profit_factor', 0)}"
            )
        if len(watchlist) > 12:
            lines.append(f"... و{len(watchlist) - 12} غيرهم")
        return "\n".join(lines), main_keyboard()

    async def cmd_stats(self) -> tuple[str, Optional[list]]:
        stats = await self.store.get(KEY_STATS, {}) or {}
        signals = await get_signals(self.store)

        lines = ["📉 *أداء النظام*", ""]
        lines.append(f"• إشارات مُرسلة: {len(signals)}")

        if stats.get("evaluated"):
            n = stats["evaluated"]
            lines.append(f"• إشارات مُقيَّمة: {n}")
            lines.append(f"• نسبة نجاح الهدف: {stats.get('hit_target', 0)}/{n}")
            lines.append(f"• نسبة ضرب الستوب: {stats.get('hit_stop', 0)}/{n}")
            mean_r = stats.get("mean_r")
            if mean_r is not None:
                lines.append(f"• متوسط R: {mean_r:+.2f}")
            lines.append("")
            lines.append(self._verdict(stats))
        else:
            lines.append("")
            lines.append(
                "⏳ لا توجد إشارات مُقيَّمة بعد.\n"
                "التقييم يبدأ بعد أن تمر 24 ساعة على الإشارة الأولى — "
                "لأن الحكم على إشارة قبل انتهاء أفقها تضليل."
            )

        counters = await self.store.get("counters", {}) or {}
        if counters:
            lines.append("")
            lines.append(
                f"دورات: {counters.get('cycles', 0)} · تقاربات: {counters.get('clusters', 0)} · "
                f"إشارات: {counters.get('signals', 0)}"
            )
        return "\n".join(lines), main_keyboard()

    @staticmethod
    def _verdict(stats: dict) -> str:
        """Honest, non-promotional read on whether the radar is working."""
        n = stats.get("evaluated", 0)
        mean_r = stats.get("mean_r")
        if n < 20:
            return f"⚠️ العينة صغيرة ({n}). لا يمكن الحكم قبل 20 إشارة مُقيَّمة على الأقل."
        if mean_r is None:
            return "⚠️ بيانات R غير مكتملة."
        if mean_r > 0.15:
            return f"✅ النتائج إيجابية (متوسط R {mean_r:+.2f}) — لكن راقب السحب الأقصى."
        if mean_r > -0.05:
            return f"➖ النتائج محايدة (متوسط R {mean_r:+.2f}) — النظام لا يضيف قيمة واضحة بعد."
        return f"🔴 النتائج سلبية (متوسط R {mean_r:+.2f}) — راجع الأوزان أو أوقف الإرسال."

    async def cmd_why(self, arg: str = "") -> tuple[str, Optional[list]]:
        if arg:
            sig = await self._find_signal(arg)
            if sig:
                return self._explain(sig), None

        rejected = await self.store.get("last_rejections", []) or []
        if not rejected:
            return "لا توجد تقاربات مرفوضة في آخر دورة.", main_keyboard()

        lines = ["❓ *لماذا لم تُرسل إشارة؟*", ""]
        for r in rejected[:6]:
            lines.append(
                f"• {r.get('coin')} {_AR_SIDE.get(r.get('side', ''), r.get('side'))} — "
                f"{r.get('reason')}"
            )
        lines.append("")
        lines.append("هذا مهم: النظام يصمت لأسباب، لا لأنه معطّل.")
        return "\n".join(lines), main_keyboard()

    async def cmd_health(self) -> str:
        counters = await self.store.get("counters", {}) or {}
        watchlist = await self.store.get(KEY_WATCHLIST, []) or []
        signals = await get_signals(self.store)
        last = await self.store.get("last_cycle_at", "—")
        health = await self.store.get("source_health", {}) or {}

        lines = [
            "⚙️ *حالة النظام*",
            "",
            f"• قائمة المراقبة: {len(watchlist)} متداول",
            f"• إشارات مُخزّنة: {len(signals)}",
            f"• آخر دورة: {last}",
            f"• دورات مُنفَّذة: {counters.get('cycles', 0)}",
            "",
            "*المنصات:*",
        ]
        for name, ok in (health or {}).items():
            lines.append(f"  {name}: {'🟢 متصل' if ok else '🔴 لا يستجيب'}")

        budget = self.budget
        lines.append("")
        lines.append(f"*ميزانية الذكاء:* {budget.used:.0f}/{budget.soft_cap} neuron")
        return "\n".join(lines)

    async def cmd_details(self, signal_id: str = "") -> str:
        signals = await get_signals(self.store)
        if not signals:
            return "لا توجد إشارات مُخزّنة."
        sig = await self._find_signal(signal_id) or signals[-1]

        lines = [
            f"📊 *تفاصيل إشارة {sig.get('coin')} {_AR_SIDE.get(sig.get('side', ''), sig.get('side'))}*",
            "",
        ]
        for m in sig.get("members", []):
            lines.append(
                f"• `{str(m.get('wallet'))[:12]}`"
                f"\n   المنصة: {m.get('source')} · التقييم: {m.get('quality')} {m.get('quality_score')}/100"
                f"\n   الدخول: `{m.get('entry')}` · الرافعة: {m.get('leverage')}x"
                f"\n   منذ: {m.get('age_min')} دقيقة"
            )
        lines.append("")
        lines.append("*تفصيل الدرجة:*")
        for k, v in (sig.get("breakdown") or {}).items():
            label = {
                "consensus": "قوة الإجماع",
                "quality": "جودة المتداولين",
                "tightness": "تقارب السعر",
                "synchrony": "تزامن الدخول",
                "diversity": "تنوّع المصادر",
            }.get(k, k)
            lines.append(f"  {label}: {v}")

        lines.append("")
        lines.append("⚠️ HYPOTHETICAL_ — قرار شخصي، ليست نصيحة مالية.")
        return "\n".join(lines)

    # ── AI conversation ─────────────────────────────────────────────────────
    async def _converse(self, question: str, chat_id: str) -> str:
        facts = await self._facts()
        history = await get_chat_history(self.store, chat_id, self.cfg.ai_max_history)

        if self.cfg.ai_enabled:
            answer = await ask_ai(
                self.env, question, facts, history, budget=self.budget, cfg=self.cfg
            )
            if answer:
                await record_chat(self.store, chat_id, "user", question)
                await record_chat(self.store, chat_id, "assistant", answer)
                return answer

        # Deterministic fallback — always available, never wrong about numbers.
        await record_chat(self.store, chat_id, "user", question)
        fallback = self._deterministic_answer(question, facts)
        await record_chat(self.store, chat_id, "assistant", fallback)
        return fallback

    def _deterministic_answer(self, question: str, facts: dict) -> str:
        """Keyword-routed answers built from the same facts the model would see."""
        q = question.lower()
        signals = facts.get("signals", [])

        if any(k in q for k in ("إشار", "توصي", "فرص", "signal")):
            if not signals:
                return narrate_no_signal()
            sig = signals[-1]
            return self._render_signal(sig)

        if any(k in q for k in ("مركز", "مراكز", "position")):
            return (
                "📈 اطلب /positions لعرض كل المراكز الحالية للمتداولين المراقَبين."
            )

        if any(k in q for k in ("أداء", "نتيج", "ربح", "خسر", "stats")):
            stats = facts.get("stats", {})
            if not stats.get("evaluated"):
                return (
                    "📉 لا توجد نتائج مُقيَّمة بعد.\n"
                    "التقييم يحتاج 24 ساعة على الأقل بعد أول إشارة."
                )
            return (
                f"📉 مُقيَّم: {stats.get('evaluated')} إشارة · "
                f"أهداف: {stats.get('hit_target', 0)} · ستوبات: {stats.get('hit_stop', 0)} · "
                f"متوسط R: {stats.get('mean_r', 0):+.2f}\n\n{self._verdict(stats)}"
            )

        if any(k in q for k in ("متداول", "محترف", "قائمة", "watchlist")):
            wl = facts.get("watchlist", [])
            return (
                f"👥 نراقب {len(wl)} متداولاً محترفاً.\n"
                f"اطلب /watchlist لعرض القائمة كاملة."
            )

        if any(k in q for k in ("لماذا", "رفض", "why")):
            rejected = facts.get("rejections", [])
            if not rejected:
                return "لا توجد تقاربات مرفوضة مسجّلة في آخر دورة."
            return (
                "❓ أسباب الرفض في آخر دورة:\n"
                + "\n".join(f"• {r.get('coin')}: {r.get('reason')}" for r in rejected[:5])
            )

        if any(k in q for k in ("حال", "health", "يعمل", "شغّال")):
            return (
                f"⚙️ النظام يعمل · مراقبة {len(facts.get('watchlist', []))} متداولاً · "
                f"{len(signals)} إشارة مُخزّنة."
            )

        return (
            "🤖 أستطيع مساعدتك في:\n"
            "• شرح آخر إشارة وسبب درجتها\n"
            "• لماذا لم تصل إشارة\n"
            "• أداء النظام حتى الآن\n"
            "• حالة المتداولين المراقَبين\n\n"
            "اكتب سؤالك أو استخدم /help"
        )

    async def _facts(self) -> dict[str, Any]:
        """The single source of truth handed to the model."""
        signals = await get_signals(self.store)
        watchlist = await self.store.get(KEY_WATCHLIST, []) or []
        stats = await self.store.get(KEY_STATS, {}) or {}
        rejections = await self.store.get("last_rejections", []) or []

        return {
            "signals": signals[-5:],
            "watchlist_size": len(watchlist),
            "watchlist": watchlist[:20],
            "stats": stats,
            "rejections": rejections[:6],
            "now": datetime.now(UTC).isoformat(),
            "config": {
                "min_independent_traders": self.cfg.min_independent_traders,
                "entry_tolerance_bps": self.cfg.entry_tolerance_bps,
                "convergence_window_min": self.cfg.convergence_window_min,
                "min_signal_score": self.cfg.min_signal_score,
            },
        }

    async def _find_signal(self, signal_id: str) -> Optional[dict]:
        if not signal_id:
            return None
        signals = await get_signals(self.store)
        for s in reversed(signals):
            if s.get("signal_id") == signal_id:
                return s
        return None

    @staticmethod
    def _render_signal(sig: dict) -> str:
        """Render a stored signal dict exactly like a live one."""
        side = sig.get("side", "")
        arrow = "🟢" if side == "LONG" else "🔴"
        plan = sig.get("plan") or {}
        lines = [
            f"{arrow} *{sig.get('coin')} — {_AR_SIDE.get(side, side)}* · "
            f"{sig.get('level')} ({sig.get('score')}/100)",
            "",
            f"• متداولون مستقلون: *{sig.get('n_independent')}*",
            f"• المصادر: {', '.join(sig.get('sources', []))}",
            f"• وسط الدخول: `{sig.get('entry_center')}`",
            f"• تقارب السعر: {sig.get('spread_bps')} نقطة أساس",
        ]
        if sig.get("market_price"):
            lines.append(f"• السعر الحالي: `{sig.get('market_price')}`")
        if plan.get("stop") is not None:
            tag = "" if plan.get("stop_source") == "trader" else " (محسوب)"
            lines.append(f"• الستوب: `{plan.get('stop')}`{tag}")
        if plan.get("target") is not None:
            tag = "" if plan.get("target_source") == "trader" else " (محسوب)"
            lines.append(f"• الهدف: `{plan.get('target')}`{tag}")
        if plan.get("rr"):
            lines.append(f"• العائد/المخاطرة: {plan.get('rr')}:1")

        lines.append("")
        lines.append("⚠️ HYPOTHETICAL_ — قرار شخصي، ليست نصيحة مالية.")
        return "\n".join(lines)

    @staticmethod
    def _explain(sig: dict) -> str:
        lines = [
            f"❓ *لماذا {sig.get('score')}/100؟*",
            "",
        ]
        for k, v in (sig.get("breakdown") or {}).items():
            lines.append(f"• {k}: {v}")
        lines.append("")
        for r in (sig.get("reasons") or [])[:8]:
            lines.append(f"– {r}")
        return "\n".join(lines)
