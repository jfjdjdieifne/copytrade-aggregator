"""The assistant: routing, formatting, AI fallback, and the no-invention rule."""

from __future__ import annotations

import json

import pytest

from cta.ai import (
    AiBudget,
    ask_ai,
    build_user_prompt,
    explain_rejection,
    narrate_no_signal,
    narrate_signal,
    signal_facts,
)
from cta.bot import Bot
from cta.config import Config
from cta.models import Cluster, Signal, TradePlan
from cta.state import KEY_STATS, KEY_WATCHLIST, MemoryStore
from cta.telegram import Telegram, _chunks, parse_update

from .conftest import FakeEnv, FakeFetch, make_position


def _signal(**over) -> Signal:
    defaults = dict(
        coin="BTC",
        side="LONG",
        score=82,
        level="STRONG",
        entry_center=65_000.0,
        spread_bps=9.2,
        n_independent=3,
        sources=["hyperliquid", "dydx"],
        plan=TradePlan(entry=65_010, stop=63_800, target=67_600, rr=2.09),
        members=[
            {"wallet": "0xalice1234", "source": "hyperliquid", "entry": 65_000,
             "quality": "ELITE", "quality_score": 90, "age_min": 20, "leverage": 3},
        ],
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        signal_id="abc123",
        market_price=65_030.0,
        breakdown={"consensus": 24, "quality": 30, "tightness": 16,
                   "synchrony": 7, "diversity": 5},
        reasons=["3 متداول مستقل"],
    )
    defaults.update(over)
    return Signal(**defaults)


# ── formatting ───────────────────────────────────────────────────────────────
def test_narration_contains_every_material_number():
    text = narrate_signal(_signal())
    assert "BTC" in text and "82/100" in text
    assert "3" in text                      # independent count
    assert "63,800" not in text             # no thousands separators injected
    assert "63800" in text
    assert "HYPOTHETICAL_" in text


def test_narration_marks_computed_levels():
    sig = _signal(
        plan=TradePlan(entry=65_010, stop=63_800, target=67_600, rr=2.09,
                       stop_source="atr", target_source="atr")
    )
    text = narrate_signal(sig)
    assert "محسوب" in text


def test_narration_handles_missing_plan():
    text = narrate_signal(_signal(plan=None))
    assert "BTC" in text
    assert "الستوب" not in text


def test_no_signal_message_is_explicit():
    assert "لا توجد إشارة" in narrate_no_signal()


def test_rejection_explanation_names_the_gate():
    cluster = Cluster(
        coin="ETH", side="LONG", members=[make_position("0xa"), make_position("0xb")],
        entry_center=3_000.0, spread_bps=180.0, time_span_min=20.0,
        sources=["hyperliquid"],
    )
    text = explain_rejection([cluster], Config())
    assert "ETH" in text and "انتشار" in text


# ── prompt discipline ────────────────────────────────────────────────────────
def test_prompt_embeds_facts_as_json_not_prose():
    facts = {"signals": [], "watchlist_size": 3}
    prompt = build_user_prompt("كيف الحال", facts, [])
    assert json.dumps(facts, ensure_ascii=False) in prompt
    assert "كيف الحال" in prompt


def test_prompt_includes_short_history_only():
    history = [{"role": "user", "content": f"q{i}"} for i in range(20)]
    prompt = build_user_prompt("x", {}, history)
    assert "q19" in prompt
    assert "q0" not in prompt


def test_signal_facts_expose_exact_numbers():
    facts = signal_facts(_signal())
    assert facts["score"] == 82
    assert facts["stop"] == 63_800
    assert facts["n_independent"] == 3


def test_budget_blocks_when_exhausted():
    budget = AiBudget(soft_cap=100)
    assert budget.allows(10) is True
    budget.used = 99
    assert budget.allows(10_000, max_out_tokens=600) is False


def test_budget_estimate_is_positive():
    assert AiBudget().estimate(1_000) > 0


# ── AI bridge ────────────────────────────────────────────────────────────────
class _FakeAI:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        return self.reply


async def test_ask_ai_returns_model_text():
    env = FakeEnv(AI=_FakeAI({"response": "إجابة"}))
    out = await ask_ai(env, "سؤال", {}, [], cfg=Config())
    assert out == "إجابة"


async def test_ask_ai_returns_none_without_binding():
    assert await ask_ai(FakeEnv(), "q", {}, [], cfg=Config()) is None


async def test_ask_ai_swallows_model_failure():
    class Boom:
        async def run(self, *a, **k):
            raise RuntimeError("model down")

    assert await ask_ai(FakeEnv(AI=Boom()), "q", {}, [], cfg=Config()) is None


async def test_ask_ai_respects_budget():
    env = FakeEnv(AI=_FakeAI({"response": "x"}))
    budget = AiBudget(soft_cap=0)
    assert await ask_ai(env, "q", {}, [], budget=budget, cfg=Config()) is None


async def test_ask_ai_charges_budget_on_success():
    env = FakeEnv(AI=_FakeAI({"response": "نص"}))
    budget = AiBudget(soft_cap=100_000)
    await ask_ai(env, "سؤال", {"a": 1}, [], budget=budget, cfg=Config())
    assert budget.used > 0


# ── telegram transport ───────────────────────────────────────────────────────
def test_update_parsing_message():
    update = {"message": {"chat": {"id": 42}, "from": {"username": "mo"},
                          "text": "/latest"}}
    parsed = parse_update(update)
    assert parsed["chat_id"] == "42"
    assert parsed["kind"] == "message"


def test_update_parsing_callback():
    update = {"callback_query": {"id": "cb1", "data": "stats",
                                 "message": {"chat": {"id": 9}}, "from": {"id": 1}}}
    parsed = parse_update(update)
    assert parsed["kind"] == "callback"
    assert parsed["text"] == "stats"


def test_update_parsing_ignores_garbage():
    assert parse_update({}) is None
    assert parse_update("nope") is None


def test_long_messages_are_chunked_on_line_boundaries():
    text = "\n".join(["line"] * 2_000)
    chunks = _chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)


async def test_telegram_send_without_token_is_a_no_op():
    tg = Telegram(FakeFetch({}), "")
    result = await tg.send("hi")
    assert result["ok"] is False


async def test_telegram_send_posts_to_api():
    fetch = FakeFetch({"api.telegram.org": {"ok": True, "result": {}}})
    tg = Telegram(fetch, "token", "123")
    assert (await tg.send("hello"))["ok"] is True
    url, opts = fetch.calls[0]
    assert "sendMessage" in url
    assert json.loads(opts["body"])["chat_id"] == "123"


# ── bot routing ──────────────────────────────────────────────────────────────
@pytest.fixture
def bot():
    store = MemoryStore()
    tg = Telegram(FakeFetch({"api.telegram.org": {"ok": True}}), "token", "1")
    return Bot(tg, store, env=FakeEnv(), cfg=Config())


async def test_help_command(bot):
    text, keyboard = await bot._route_command("/help", "1")
    assert "رادار" in text
    assert keyboard


async def test_unknown_command_is_answered(bot):
    text, _ = await bot._route_command("/nonsense", "1")
    assert "غير معروف" in text


async def test_latest_with_no_signals(bot):
    text, _ = await bot.cmd_latest()
    assert "لا توجد إشارة" in text


async def test_command_with_bot_suffix_is_parsed(bot):
    text, _ = await bot._route_command("/health@MyRadarBot", "1")
    assert "حالة النظام" in text


async def test_stats_reports_insufficient_sample(bot):
    await bot.store.set(KEY_STATS, {"evaluated": 3})
    text, _ = await bot.cmd_stats()
    assert "العينة صغيرة" in text


async def test_stats_judges_negative_results_honestly(bot):
    await bot.store.set(KEY_STATS, {"evaluated": 50, "mean_r": -0.4})
    text, _ = await bot.cmd_stats()
    assert "سلبية" in text


async def test_watchlist_reports_empty_state(bot):
    text, _ = await bot.cmd_watchlist()
    assert "فارغة" in text


async def test_watchlist_shows_counts(bot):
    await bot.store.set(KEY_WATCHLIST, [
        {"wallet": "0xaaaa", "grade": "ELITE", "score": 90, "n_trades": 300, "profit_factor": 2.1},
        {"wallet": "0xbbbb", "grade": "PROVEN", "score": 70, "n_trades": 150, "profit_factor": 1.6},
    ])
    text, _ = await bot.cmd_watchlist()
    assert "ELITE: 1" in text
    assert "PROVEN: 1" in text


async def test_free_text_without_ai_falls_back(bot):
    await bot.store.set(KEY_WATCHLIST, [{"wallet": "0xa", "grade": "ELITE"}])
    reply = await bot._converse("كيف حالة النظام؟", "1")
    assert "يعمل" in reply or "مراقبة" in reply


async def test_free_text_about_signals_uses_narrator(bot):
    await bot.store.set("signals", [])
    reply = await bot._converse("في أي إشارة؟", "1")
    assert "لا توجد إشارة" in reply


async def test_chat_memory_is_persisted(bot):
    await bot._converse("سؤال أول", "1")
    history = await bot.store.get("memory:1", [])
    assert any(t["content"] == "سؤال أول" for t in history)


async def test_handle_update_message_returns_handled(bot):
    update = {"message": {"chat": {"id": 5}, "text": "/help"}}
    result = await bot.handle_update(update)
    assert result["handled"] is True
