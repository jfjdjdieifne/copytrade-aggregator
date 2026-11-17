"""The heartbeat: proof of life that carries evidence.

The operator sees the world through Telegram and nothing else, and asked for
two things that pull against each other: never go silent while I am still
deciding whether this works, and never send words where numbers would do.

These tests protect the second requirement. A message that says "شغال" passes
a naive test and tells the operator nothing; the property that matters is that
the message would look *different* if the system were broken.
"""

from datetime import datetime, timedelta, timezone

from cta.config import Config
from cta.heartbeat import (
    FIRST_REPORT_MIN,
    Pulse,
    boot_message,
    positions_to_rows,
    rows_to_positions,
)
from cta.models import Position

UTC = timezone.utc
CFG = Config()
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def report(**kw):
    base = {
        "ok": True,
        "watchlist": 0,
        "clusters": 0,
        "signals": 0,
        "positions": 0,
        "ideas_opened": 0,
        "ideas_closed": 0,
        "health": {"hyperliquid": True, "okx": True, "dydx": True, "gmx": True},
        "coins": [],
    }
    base.update(kw)
    return base


def pos(wallet, coin, side, entry, when, source="sim"):
    return Position(
        wallet=wallet, source=source, coin=coin,
        signed_size=(1.0 if side == "LONG" else -1.0) * 1000.0,
        entry=entry, size_usd=100_000.0, leverage=3.0,
        opened_at=when, mark_price=entry, quality="ELITE", quality_score=90,
    )


# ── the report carries counts, not claims ────────────────────────────────────

def test_the_report_states_the_numbers_that_move():
    p = Pulse(interval_min=30, now=NOW)
    for i in range(1, 9):
        p.observe(report(watchlist=i * 6, positions=i * 2,
                         coins=["BTC", "ETH", "SOL"]))
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=4))
    assert "8 دورة" in text
    assert "48" in text          # watchlist
    assert "16" in text          # positions visible
    assert "3 عملات" in text
    assert "4/4 تستجيب" in text


def test_the_second_report_shows_what_changed_since_the_first():
    """Motion is the proof. Without the delta the operator compares by memory."""
    p = Pulse(interval_min=30, now=NOW)
    p.observe(report(watchlist=10))
    p.render(CFG, [], [])
    import asyncio

    asyncio.run(p.mark_reported(NOW))
    p.observe(report(watchlist=17))
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=30))
    assert "(+7)" in text


def test_a_rising_signal_count_appears_in_the_report():
    p = Pulse(interval_min=30, now=NOW)
    for _ in range(4):
        p.observe(report(watchlist=20, signals=3))
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=10))
    assert "12" in text


# ── broken states must be named, not smoothed over ───────────────────────────

def test_all_venues_down_is_reported_as_such():
    p = Pulse(interval_min=30, now=NOW)
    p.observe(report(watchlist=30, positions=0,
                     health={"a": False, "b": False, "c": False, "d": False}))
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=30))
    assert "0/4 تستجيب" in text
    assert "⚠️" in text
    assert "ما في ولا منصة تستجيب" in text


def test_a_watchlist_still_empty_after_fifteen_minutes_is_named():
    """Silence about this is how an operator loses a day."""
    p = Pulse(interval_min=30, now=NOW)
    for _ in range(20):
        p.observe(report(watchlist=0, positions=0))
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=20))
    assert "قائمة المراقبة لسه صفر" in text


def test_a_watchlist_that_is_early_is_not_yet_a_warning():
    """Five minutes in, zero is normal. Naming it a fault would cry wolf."""
    p = Pulse(interval_min=30, now=NOW)
    p.observe(report(watchlist=0, positions=0))
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=5))
    assert "قائمة المراقبة لسه صفر" not in text


def test_mostly_failing_cycles_are_reported_not_averaged_away():
    p = Pulse(interval_min=30, now=NOW)
    for i in range(10):
        p.observe(report(watchlist=20) if i % 2 == 0 else {"error": "boom"})
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=30))
    assert "5 نجحت" in text
    assert "5 فشلت" in text
    assert "أكثر من نص الدورات بتفشل" in text


def test_zero_cycles_is_reported_as_a_dead_loop():
    p = Pulse(interval_min=30, now=NOW)
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=30))
    assert "ما صارت ولا دورة" in text


def test_traders_but_no_positions_is_named():
    p = Pulse(interval_min=30, now=NOW)
    for _ in range(15):
        p.observe(report(watchlist=40, positions=0))
    text = p.render(CFG, [], [], now=NOW + timedelta(minutes=30))
    assert "ما نشوف ولا مركز مفتوح" in text


# ── the silence explanation must never be invented ───────────────────────────

def test_unknown_positions_produce_no_explanation_at_all():
    """The funnel's verdict for an empty list is 'we see no positions'.

    That is a lie when the real fault is that the snapshot never arrived, and a
    confident wrong reason is worse than an honest blank.
    """
    p = Pulse(interval_min=30, now=NOW)
    p.observe(report(watchlist=44, positions=9))
    text = p.render(CFG, None, [], now=NOW + timedelta(minutes=30))
    assert "🎯" not in text


def test_known_positions_produce_the_gate_that_blocked_them():
    p = Pulse(interval_min=30, now=NOW)
    p.observe(report(watchlist=44, positions=2, coins=["BTC"]))
    group = [
        pos("w1", "BTC", "LONG", 100.0, NOW - timedelta(hours=9)),
        pos("w2", "BTC", "LONG", 112.0, NOW - timedelta(hours=10), source="sim-beta"),
    ]
    text = p.render(CFG, group, [], now=NOW)
    assert "🎯" in text


def test_a_signal_suppresses_the_explanation():
    """Once something fired, 'why nothing?' is the wrong question."""
    p = Pulse(interval_min=30, now=NOW)
    p.observe(report(watchlist=44, positions=2, signals=1))
    text = p.render(CFG, [], [], now=NOW)
    assert "🎯" not in text


# ── cadence ──────────────────────────────────────────────────────────────────

def test_the_first_report_lands_early():
    """A new operator is watching. Half an hour of silence reads as failure."""
    p = Pulse(store=None, interval_min=30, now=NOW)
    import asyncio

    assert asyncio.run(p.due(NOW + timedelta(minutes=FIRST_REPORT_MIN - 1))) is False
    assert asyncio.run(p.due(NOW + timedelta(minutes=FIRST_REPORT_MIN + 1))) is True


def test_the_cadence_survives_a_restart():
    """Otherwise every restart greets the operator with a first-run report."""
    import asyncio

    from cta.state import MemoryStore

    store = MemoryStore()

    async def scenario():
        first = Pulse(store=store, interval_min=30, now=NOW)
        assert await first.due(NOW + timedelta(minutes=6)) is True
        await first.mark_reported(NOW + timedelta(minutes=6))

        restarted = Pulse(store=store, interval_min=30, now=NOW + timedelta(minutes=8))
        assert await restarted.due(NOW + timedelta(minutes=10)) is False
        assert await restarted.due(NOW + timedelta(minutes=40)) is True

    asyncio.run(scenario())


# ── position round trip ──────────────────────────────────────────────────────

def test_positions_survive_the_store_round_trip():
    live = [pos("w1", "BTC", "LONG", 100.0, NOW),
            pos("w2", "ETH", "SHORT", 2_500.0, NOW - timedelta(hours=3))]
    back = rows_to_positions(positions_to_rows(live))
    assert len(back) == 2
    assert back[0].coin == "BTC" and back[0].side == "LONG"
    assert back[1].coin == "ETH" and back[1].side == "SHORT"
    assert back[0].opened_at == NOW
    assert back[1].opened_at == NOW - timedelta(hours=3)


def test_a_malformed_row_costs_one_position_not_the_whole_report():
    good = positions_to_rows([pos("w1", "BTC", "LONG", 100.0, NOW)])
    rows = good + [{"wallet": "broken", "entry": "not-a-number"}, "junk", None]
    back = rows_to_positions(rows)
    assert len(back) == 1
    assert back[0].wallet == "w1"


def test_garbage_input_never_raises():
    for junk in (None, [], {}, [1, 2], "x"):
        assert rows_to_positions(junk) == []


# ── boot message ─────────────────────────────────────────────────────────────

def test_the_boot_message_gives_a_baseline_to_compare_against():
    text = boot_message(health={"a": True, "b": True, "c": True, "d": True},
                        watchlist=0)
    assert "الرادار اشتغل" in text
    assert "4/4" in text
    assert str(int(FIRST_REPORT_MIN)) in text


def test_the_boot_message_reports_venue_failures_honestly():
    text = boot_message(health={"a": True, "b": False}, watchlist=0)
    assert "1/2" in text
