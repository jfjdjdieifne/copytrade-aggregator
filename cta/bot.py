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
    KEY_OWNER_CHAT,
    KEY_TRACE,
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


def _short_trace(exc: Exception) -> str:
    """Type, message and the last frame — enough to identify, short enough to read."""
    import traceback

    frames = traceback.format_exc().splitlines()
    tail = frames[-3:] if len(frames) > 3 else frames
    return f"{type(exc).__name__}: {exc} | " + " / ".join(t.strip() for t in tail)


def _escape(text: str) -> str:
    """Strip Markdown control characters so a message can never fail to parse."""
    for ch in ("*", "_", "`", "[", "]"):
        text = text.replace(ch, "")
    return text


def parse_update_is_whoami(text) -> bool:
    """`/whoami` must work even when ownership is misconfigured."""
    if not text or not text.startswith("/"):
        return False
    return text.split()[0].lower().lstrip("/").split("@")[0] == "whoami"


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
        """Never let an exception escape: silence is the worst possible answer."""
        try:
            return await self._handle(update)
        except Exception as exc:
            detail = _short_trace(exc)
            chat_id = ""
            try:
                chat_id = str((parse_update(update) or {}).get("chat_id", ""))
            except Exception:
                pass
            await self._note("crash", {"chat_id": chat_id, "error": detail[:200]})
            if chat_id:
                try:
                    await self.tg.send(
                        "⚠️ *خطأ غير متوقع*\n\n"
                        f"`{_escape(str(exc))[:300]}`\n\n"
                        "أرسل /trace لعرض آخر الأحداث.",
                        chat_id,
                    )
                except Exception:
                    pass
            return {"handled": False, "error": detail[:300]}

    async def _handle(self, update: dict) -> dict:
        parsed = parse_update(update)
        if parsed is None:
            return {"handled": False}

        chat_id = parsed["chat_id"]
        text = parsed["text"]

        # Anyone who finds the bot can talk to it and burn the shared Workers AI
        # allowance (10k neurons/day on the free plan). When the owner's chat is
        # known, everyone else gets a polite refusal instead of a conversation.
        await self._note("update", {"chat_id": chat_id, "kind": parsed["kind"],
                                    "text": (text or "")[:60]})

        if parse_update_is_whoami(text):
            info = await self.tg.call("getMe", {})
            me = info.get("result", {}) if info.get("ok") else {}
            await self.tg.send(
                "🪪 معلومات الربط\n"
                f"رقم محادثتك: `{chat_id}`\n"
                f"البوت: @{me.get('username', '?')}\n"
                "ضع رقم المحادثة في TELEGRAM_CHAT_ID إن أردت تثبيت المالك.",
                chat_id,
            )
            return {"handled": True, "kind": "whoami", "chat_id": chat_id}

        allowed, claimed = await self._is_owner(chat_id)
        if not allowed:
            await self.tg.send(
                "هذا البوت خاص. غير مصرّح لهذه المحادثة.", chat_id
            )
            await self._note("refused", {"chat_id": chat_id})
            return {"handled": False, "reason": "chat not allowed", "chat_id": chat_id}
        if claimed:
            await self.tg.send(
                "✅ تم ربط هذه المحادثة بصاحب البوت.\n"
                f"رقم محادثتك: `{chat_id}`\n"
                "من الآن أنت الوحيد الذي يستطيع استخدامه.\n"
                "أرسل /help لعرض الأوامر.",
                chat_id,
            )

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
            # A command that raises used to produce silence: the exception
            # escaped the handler, the Worker answered 500, and Telegram showed
            # the user nothing. That is indistinguishable from a dead bot.
            # Explain it in the chat instead, where the operator already is.
            try:
                reply, keyboard = await self._route_command(text, chat_id)
            except Exception as exc:
                detail = _short_trace(exc)
                await self._note("command_error", {"chat_id": chat_id,
                                                   "text": (text or "")[:40],
                                                   "error": detail[:200]})
                await self.tg.send(
                    "⚠️ *تعذّر تنفيذ هذا الأمر*\n\n"
                    f"`{_escape(str(exc))[:300]}`\n\n"
                    "_هذا خطأ في البرنامج وليس في استخدامك. أرسل /trace أو افتح_ /setup",
                    chat_id,
                )
                return {"handled": True, "kind": "command", "delivered": True,
                        "error": detail[:300]}
            sent = await self.tg.send(reply, chat_id, keyboard=keyboard or main_keyboard())
            await self._note("reply", {"chat_id": chat_id, "ok": bool(sent.get("ok")),
                                       "error": str(sent.get("error", ""))[:200],
                                       "bytes": len(reply or "")})
            if not sent.get("ok"):
                # The send failed. Surface it instead of pretending success.
                return {"handled": True, "kind": "command",
                        "delivered": False, "error": sent.get("error", "")[:200]}
            return {"handled": True, "kind": "command", "delivered": True}

        await self.tg.typing(chat_id)
        reply = await self._converse(text, chat_id)
        sent = await self.tg.send(reply, chat_id, keyboard=main_keyboard())
        await self._note("reply", {"chat_id": chat_id, "ok": bool(sent.get("ok")),
                                   "error": str(sent.get("error", ""))[:200],
                                   "bytes": len(reply or "")})
        return {"handled": True, "kind": "chat", "delivered": bool(sent.get("ok"))}

    async def _note(self, event: str, detail: dict) -> None:
        """Append to a small ring buffer so /selftest can show what happened.

        Every failure in this project so far was silent: a 200 to Telegram, a
        swallowed store error, a wrong-looking token. Recording the last few
        events turns "the bot does not answer" into something inspectable.
        """
        try:
            entry = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "event": event}
            entry.update(detail)
            await self.store.append(KEY_TRACE, entry, cap=12)
        except Exception:
            pass

    async def _is_owner(self, chat_id: str) -> tuple[bool, bool]:
        """Decide whether this chat may use the bot. Returns (allowed, claimed).

        `TELEGRAM_CHAT_ID` used to be a required manual step, and getting it
        wrong is a trap: set it to the *bot's own* id (a very natural mistake,
        since BotFather prints that id) and the bot silently refuses its real
        owner with no way out.

        So ownership is now learned instead of configured:
          * an explicit `TELEGRAM_CHAT_ID` still wins when it is set;
          * otherwise the first chat to message the bot claims ownership, and
            the claim is persisted in Durable Object state, so it survives
            redeploys and cron runs;
          * `/whoami` always answers, so a mistyped variable is diagnosable.

        The claim is deliberately sticky: one bot, one owner.
        """
        env_owner = str(getattr(self.env, "TELEGRAM_CHAT_ID", "") or "").strip()
        try:
            stored = str(await self.store.get(KEY_OWNER_CHAT, "") or "").strip()
        except Exception:
            # A broken store must not turn every message into "unexpected error".
            # Ownership is a convenience; answering is the product. Degrade to
            # the configured chat (or open, if unset) and do not claim, because
            # a claim that cannot be persisted would not survive the next call.
            if env_owner:
                return str(chat_id) == env_owner, False
            return True, False

        if env_owner:
            return str(chat_id) == env_owner, False
        if stored:
            return str(chat_id) == stored, False

        # Nothing configured and nobody has claimed it yet: take this chat.
        try:
            await self.store.set(KEY_OWNER_CHAT, str(chat_id))
        except Exception:
            return True, False
        return True, True

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
        if cmd in ("prove", "verify"):
            return await self.cmd_prove(), None
        if cmd == "trace":
            return await self.cmd_trace(), None
        if cmd == "details":
            return await self.cmd_details(arg), None
        if cmd in ("add", "track", "watch"):
            return await self.cmd_add(arg), main_keyboard()
        if cmd in ("remove", "untrack", "unwatch"):
            return await self.cmd_remove(arg), main_keyboard()
        if cmd == "seeds":
            return await self.cmd_seeds(), main_keyboard()
        if cmd in ("paper", "account", "money", "profits"):
            return await self.cmd_paper()
        if cmd in ("resetpaper", "reset"):
            return await self.cmd_reset_paper(), main_keyboard()

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
        if data == "seeds":
            return await self.cmd_seeds()
        if data == "paper":
            text, _ = await self.cmd_paper()
            return text
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

    async def cmd_add(self, raw: str) -> str:
        """Add wallets to watch, regardless of whether discovery has run.

        This exists because discovery depends on a ~39 MB leaderboard that the
        free plan cannot always parse. Without a way to add a wallet by hand, a
        failed leaderboard means a permanently empty radar with no remedy from
        the chat — which is where the operator actually is.
        """
        from .seeds import merge_seeds, parse_addresses

        found = parse_addresses(raw)
        if not found:
            return (
                "⚠️ *لم أجد عنوان محفظة صالحاً*\n\n"
                "أرسل الأمر مع العنوان، مثال:\n"
                "`/add 0x85ecf584f25db6f146718b86d493e33c5af72052`\n\n"
                "يمكنك لصق عدة عناوين مفصولة بمسافة أو فاصلة."
            )

        existing = await self.store.get("seed_wallets", []) or []
        merged = merge_seeds(existing, found)
        added = [w for w in merged if w not in set(existing)]
        await self.store.set("seed_wallets", merged)

        lines = [f"✅ *أُضيف {len(added)} متداول*" if added else "ℹ️ *موجود مسبقاً*", ""]
        for wallet in added[:10] or found[:10]:
            lines.append(f"• `{wallet[:8]}…{wallet[-4:]}`")
        lines += [
            "",
            f"المجموع: {len(merged)} محفظة",
            "_ستُقيَّم تلقائياً في الدورة القادمة (خلال دقيقة)._",
            "_إن لم تجتز شروط الجودة ستُستبعد — الإضافة ليست موافقة._",
        ]
        return "\n".join(lines)

    async def cmd_remove(self, raw: str) -> str:
        from .seeds import normalise, parse_addresses

        found = parse_addresses(raw) or ([normalise(raw)] if normalise(raw) else [])
        if not found:
            return "⚠️ أرسل العنوان مع الأمر: `/remove 0x...`"

        existing = await self.store.get("seed_wallets", []) or []
        keep = [w for w in existing if w not in set(found)]
        removed = len(existing) - len(keep)
        await self.store.set("seed_wallets", keep)

        if not removed:
            return "ℹ️ لم أجد هذا العنوان في المحافظ المبدئية."
        return (
            f"✅ *أُزيل {removed} محفظة*\n"
            f"المتبقي: {len(keep)}\n\n"
            "_إن كان المتداول مُصنَّفاً مسبقاً سيبقى في قائمة المراقبة "
            "حتى إعادة التقييم._"
        )

    async def cmd_seeds(self) -> str:
        """Show exactly which wallets we watch before any search has run."""
        from .seeds import describe

        seeds = await self.store.get("seed_wallets", []) or []
        progress = await self.store.get("discovery_progress", {}) or {}
        watchlist = await self.store.get("watchlist", []) or []

        lines = [
            "🌱 *المحافظ المبدئية*",
            "",
            f"• {describe(seeds)}",
            f"• قائمة المراقبة: {len(watchlist)} متداول",
        ]
        if progress.get("leaderboard_rows"):
            lines.append(f"• المتداولون من القائمة العامة: {progress['leaderboard_rows']}")
        elif progress.get("last_error"):
            lines.append("• القائمة العامة: تعذّرت — نعتمد على المحافظ المبدئية")
        else:
            lines.append("• القائمة العامة: لم تُقرأ بعد")

        if seeds:
            lines += ["", "*العناوين:*"]
            for wallet in seeds[:15]:
                lines.append(f"• `{wallet[:8]}…{wallet[-4:]}`")
            if len(seeds) > 15:
                lines.append(f"... و{len(seeds) - 15} غيرها")
        else:
            lines += [
                "",
                "لا محافظ مبدئية. أضف واحدة بـ:",
                "`/add 0x...`",
                "",
                "_هذا مهم: القائمة العامة كبيرة وقد لا تُقرأ على الخطة المجانية، "
                "فبدون محافظ مبدئية قد لا يُراقَب أحد._",
            ]
        return "\n".join(lines)

    async def cmd_watchlist(self) -> tuple[str, Optional[list]]:
        watchlist = await self.store.get(KEY_WATCHLIST, []) or []
        if not watchlist:
            from .discovery_resume import describe, load_progress

            progress = await load_progress(self.store)
            search = describe(progress)
            if progress.get("total"):
                # The search is running: say so, with numbers. "Empty" and
                # "still looking" are different states and the operator needs
                # to tell them apart from a phone.
                return (
                    "👥 *قائمة المراقبة قيد البناء*\n"
                    f"{search}\n\n"
                    "كل دقيقة تُفحص دفعة جديدة تلقائياً.\n"
                    "استخدم /health لمتابعة التقدّم.",
                    main_keyboard(),
                )
            return (
                "👥 *لم يبدأ البحث بعد*\n"
                "ستبدأ أول دفعة تلقائياً خلال دقيقة.\n"
                "تحقّق من /health — سترى عدد المحافظ المفحوصة.",
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
            # A bare address prefix is unreadable: every Hyperliquid wallet starts
            # 0x00 or 0x1f, so twelve rows all looked like `0x00000000`. Show the
            # head *and* tail, which is how traders actually recognise a wallet.
            wallet = str(w.get("wallet", ""))
            short = f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 12 else wallet
            lines.append(
                f"• `{short}` · {w.get('grade')} {w.get('score', 0)}/100 · "
                f"{w.get('n_trades', 0)} صفقة · PF {w.get('profit_factor', 0)}"
            )
        if len(watchlist) > 12:
            lines.append(f"... و{len(watchlist) - 12} غيرهم")
        return "\n".join(lines), main_keyboard()

    async def cmd_paper(self) -> tuple[str, Optional[list]]:
        """The $100 account: what taking every signal would actually have made.

        This is the answer to "will it make money", phrased as a balance rather
        than a feeling. It is not a forecast: fees, funding and slippage are
        absent and every one of them makes the real number worse.
        """
        from .paper import KEY_PAPER, empty_account, honest_verdict, summarize

        account = await self.store.get(KEY_PAPER, None)
        if not isinstance(account, dict):
            account = empty_account(self.cfg.paper_start_balance)

        s = summarize(account)

        lines = [
            "💰 *حساب تجريبي — كل التوصيات*",
            "",
            f"• البداية: ${s['start_balance']:.2f}",
            f"• الرصيد الحالي: *${s['balance']:.2f}*",
            f"• حقوق الملكية: ${s['equity']:.2f}",
            f"• الربح/الخسارة: {s['pnl_usd']:+.2f}$ ({s['pnl_pct']:+.1f}%)",
            "",
            f"• صفقات مغلقة: {s['trades']} (ربح {s['wins']} · خسارة {s['losses']})",
            f"• صفقات مفتوحة: {s['open']}",
            f"• إشارات لم تُنفَّذ: {s['missed']}",
        ]
        if s["trades"]:
            lines += [
                "",
                f"• نسبة النجاح: {s['win_rate']}%",
                f"• متوسط R: {s['mean_r']:+.2f}",
                f"• معامل الربح: {s['profit_factor']}",
                f"• أقصى تراجع: ${s['max_drawdown_usd']} ({s['max_drawdown_pct']}%)",
            ]

        open_trades = account.get("open", []) or []
        if open_trades:
            lines += ["", "*مفتوحة الآن:*"]
            for t in open_trades[:6]:
                lines.append(
                    f"  {t.get('coin')} {t.get('side')} · دخول {t.get('entry')} · "
                    f"وقف {t.get('stop')}"
                )

        lines += [
            "",
            f"_{honest_verdict(s)}_",
            "",
            f"⚠️ {s['caveat']}",
        ]
        return "\n".join(lines), main_keyboard()

    async def cmd_reset_paper(self) -> str:
        """Start the $100 account over, deliberately and on the record."""
        from .paper import KEY_PAPER, empty_account

        account = empty_account(self.cfg.paper_start_balance)
        await self.store.set(KEY_PAPER, account)
        return (
            "♻️ *أُعيد ضبط الحساب التجريبي*\n"
            f"الرصيد: ${self.cfg.paper_start_balance:.2f}\n"
            "المخاطرة لكل صفقة: "
            f"{self.cfg.paper_risk_pct}%\n\n"
            "_الصفقات السابقة مُسحت. التوصيات القادمة ستُسجَّل من جديد._"
        )

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

    async def cmd_prove(self) -> str:
        """Prove the detector works, on demand, in the chat.

        Waiting days to discover the radar is blind is the worst trade in this
        system. This runs a synthetic convergence through the real engine and
        reports what it produced.
        """
        from .health import self_check_async

        report = await self_check_async(self.env, None)
        if not report.get("ok"):
            detail = report.get("error") or report.get("note", "")
            return (
                "⚠️ *الكشف لا يعمل*\n\n"
                f"`{_escape(str(detail))[:300]}`\n\n"
                "هذا خلل في البرنامج، وليس سوقاً هادئاً. أرسل /trace أيضاً."
            )

        sample = report.get("sample") or {}
        lines = [
            "✅ *الكشف يعمل*",
            "",
            "مرّرت متداولَين اصطناعيين عبر المحرّك الحقيقي، فأنتج:",
            "",
            f"• {sample.get('coin')} — {sample.get('side')}",
            f"• الدرجة: {sample.get('score')}/100 ({sample.get('level')})",
            f"• من {sample.get('n_independent')} متداول مستقل",
        ]
        if sample.get("entry"):
            lines.append(f"• دخول {sample.get('entry')} · وقف {sample.get('stop')} · هدف {sample.get('target')}")

        # Two independent questions, and they fail independently: "would a
        # signal be seen?" and "is anyone actually being watched?". A working
        # detector over an empty watchlist watches nobody, which is exactly the
        # state the operator was in when they asked.
        from .health import discovery_proof_async

        discovery = await discovery_proof_async(self.store)
        lines += ["", "*هل نراقب أحداً فعلاً؟*"]
        if discovery.get("available"):
            lines.append(f"• {discovery.get('verdict')}")
            if discovery.get("candidates"):
                lines.append(f"• {discovery.get('line')}")
            if discovery.get("watching"):
                lines.append("• الدورة القادمة ستفحص مراكز هؤلاء المتداولين")
            else:
                lines.append("• لا متداولين بعد — البحث يعمل لكنه لم يُكمل دفعة")
        else:
            lines.append(f"• تعذّرت القراءة: {discovery.get('reason', '')}")

        lines += [
            "",
            "_أي سكوت بعد اليوم يعني سوقاً هادئاً، لا نظاماً معطّلاً._",
        ]
        return "\n".join(lines)

    async def cmd_trace(self) -> str:
        """Show the last few things that happened, from inside the chat.

        `/setup` already renders this, but a phone without a browser open is a
        worse place to debug from than the chat itself.
        """
        try:
            trace = await self.store.get(KEY_TRACE, []) or []
        except Exception as exc:
            return f"⚠️ تعذّر قراءة السجل: `{_escape(str(exc))[:200]}`"

        if not trace:
            return (
                "🧪 *السجل فاضي*\n\n"
                "لا توجد أحداث مسجّلة. إن كنت أرسلت أوامر ولم تُسجّل، "
                "فالرسائل لا تصل إلى الـ Worker أصلاً."
            )

        lines = ["🧪 *آخر الأحداث*", ""]
        for item in trace[-8:]:
            stamp = str(item.get("t", ""))[-8:]
            event = item.get("event", "?")
            extra = item.get("error") or item.get("text") or ""
            if event == "update":
                extra = f"{item.get('text', '')}"
            elif event == "reply":
                extra = "تم الإرسال" if item.get("ok") else f"فشل: {item.get('error', '')}"
            lines.append(f"`{stamp}` {event} {_escape(str(extra))[:60]}")
        return "\n".join(lines)

    async def cmd_health(self) -> str:
        counters = await self.store.get("counters", {}) or {}
        watchlist = await self.store.get(KEY_WATCHLIST, []) or []
        signals = await get_signals(self.store)
        last = await self.store.get("last_cycle_at", "—")
        health = await self.store.get("source_health", {}) or {}

        from .discovery_resume import describe, load_progress, needs_more_work

        progress = await load_progress(self.store)
        search = describe(progress)
        searching = needs_more_work(progress)

        from .seeds import describe as describe_seeds

        seeds = await self.store.get("seed_wallets", []) or []
        rows = int(progress.get("leaderboard_rows", 0) or 0)

        lines = [
            "⚙️ *حالة النظام*",
            "",
            f"• قائمة المراقبة: {len(watchlist)} متداول",
            f"• إشارات مُخزّنة: {len(signals)}",
            f"• آخر دورة: {last}",
            f"• دورات مُنفَّذة: {counters.get('cycles', 0)}",
            "",
            "*مصدر المتداولين:*",
            f"  {describe_seeds(seeds)}",
        ]

        # The leaderboard is a ~39 MB file that the free plan cannot always
        # parse. Say which source actually produced the watchlist, because
        # "0 traders" has two completely different causes.
        if rows:
            lines.append(f"  القائمة العامة: {rows} متداول قرئت")
        elif progress.get("last_error") and "leaderboard" in str(progress.get("last_error", "")):
            lines.append("  القائمة العامة: تعذّرت — نعتمد على المحافظ المبدئية")
        elif progress.get("total"):
            lines.append("  القائمة العامة: لم تُقرأ بعد")

        lines += [
            "",
            "*البحث عن متداولين:*",
            f"  {search}",
        ]
        if searching:
            lines.append("  ⏳ البحث جارٍ — يتقدّم خطوة كل دقيقة")
        elif progress.get("passes"):
            lines.append("  ✅ اكتملت دورة بحث كاملة")
        else:
            lines.append("  ⚠️ لم يكتمل بحث بعد")

        rotation = await self.store.get("scan_rotation", {}) or {}
        if rotation.get("watching"):
            lines.append("")
            lines.append("*دورة الفحص:*")
            lines.append(f"  يفحص {rotation.get('polled')} من {rotation.get('watching')} كل دقيقة")
            lines.append(f"  كل متداول يُفحص مرة كل {rotation.get('minutes_per_sweep', 1)} دقيقة")

        lines.append("")
        lines.append("*المنصات:*")
        for name, ok in (health or {}).items():
            lines.append(f"  {name}: {'🟢 متصل' if ok else '🔴 لا يستجيب'}")

        # A green platform with an empty watchlist is the exact screen that made
        # the operator think the system was idle when it was actually being
        # killed mid-cycle. Say which one it is.
        if health and not watchlist:
            lines.append("")
            lines.append("⚠️ المنصات متصلة لكن القائمة فارغة — البحث لم يُكمل بعد")

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
