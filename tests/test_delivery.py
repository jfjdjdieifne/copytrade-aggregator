"""Delivery: the one path that must never be conditional on anything else.

This file exists because of a specific bug.

Telegram delivery lived *inside* the paper-account `except` block:

    try:
        ... book the paper trade ...
    except Exception:
        print("paper account failed")
        if not dry_run and tg.configured:
            ... send the message ...

Read that again. A signal was pushed to the operator only when bookkeeping
*failed*. In the normal case — the paper account working exactly as designed —
nothing was ever sent. Every counter still reported the cycle as a success, the
dashboard stayed green, and the operator received silence while the system
believed it had delivered.

Four hundred tests passed with that bug in place, because none of them asked the
only question that mattered: did the message go out?
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cta.runtime import run_cycle_async
from cta.simulator import build_sim_venues
from cta.state import MemoryStore

UTC = timezone.utc


class Resp:
    status = 200

    def __init__(self, data):
        self._data = data

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)


class RecordingFetch:
    """Answers venue calls and records anything addressed to Telegram."""

    def __init__(self, telegram_ok: bool = True):
        self.sent: list[dict] = []
        self.telegram_ok = telegram_ok

    async def __call__(self, url, opts=None):
        if "api.telegram.org" in str(url):
            payload = json.loads((opts or {}).get("body") or "{}")
            self.sent.append(payload)
            return Resp({"ok": self.telegram_ok, "result": {"message_id": 1}})
        return Resp({})


def _env(store: MemoryStore, **kw):
    class Env:
        TELEGRAM_BOT_TOKEN = "token"
        TELEGRAM_CHAT_ID = "1234"
        DRY_RUN = "false"

    env = Env()
    env.STATE = store
    for key, value in kw.items():
        setattr(env, key, value)
    return env


@pytest.fixture
def sim_venues(monkeypatch):
    """Force the runtime to use the simulator, so no network is required."""
    import cta.runtime as runtime

    venues = build_sim_venues(n_traders=30, seed=7, auto_events=True, event_every=2)
    monkeypatch.setattr(runtime, "build_venues", lambda fetch, cfg: venues)
    return venues


async def test_a_signal_is_delivered_when_the_paper_account_works(sim_venues):
    """The regression test for the bug above.

    The paper account succeeds here — that is the *normal* path — and the
    message must still go out. Asserting only on the failure path is what let
    the bug survive four hundred green tests.
    """
    store = MemoryStore()
    fetch = RecordingFetch()

    # Let the simulator build a watchlist and then agree on something.
    delivered_total = 0
    for _ in range(8):
        report = await run_cycle_async(_env(store), fetch)
        delivered_total += int(report.get("delivered") or 0)
        if delivered_total:
            break

    assert report.get("signals", 0) > 0, "no signal was produced to deliver"
    assert delivered_total > 0, (
        "signals were produced and none reached Telegram — delivery is broken"
    )
    assert fetch.sent, "nothing was ever posted to Telegram"


async def test_delivery_survives_a_failing_paper_account(sim_venues, monkeypatch):
    """Bookkeeping is a measurement. If it breaks, the alert must still go out."""
    import cta.runtime as runtime

    import cta.paper as paper

    async def boom(*args, **kwargs):
        raise RuntimeError("paper account exploded")

    # Break the paper account only. Breaking `venue.prices` instead would also
    # disable the scan's slippage and reachability gates, so the run would
    # produce no signals at all and the test would prove nothing about delivery.
    monkeypatch.setattr(paper, "empty_account", boom)
    monkeypatch.setattr(runtime, "_now", lambda: datetime.now(UTC))
    store = MemoryStore()
    fetch = RecordingFetch()

    delivered_total = 0
    for _ in range(8):
        report = await run_cycle_async(_env(store), fetch)
        delivered_total += int(report.get("delivered") or 0)
        if delivered_total:
            break

    # The cycle must still complete, and a signal must still be delivered.
    assert report.get("ok") is True
    assert delivered_total > 0, "a bookkeeping failure took delivery down with it"


async def test_a_failed_send_is_recorded_not_swallowed(sim_venues):
    """Silence is the worst failure mode a monitoring system has. If Telegram
    refuses a message, that refusal must be visible in state."""
    store = MemoryStore()
    fetch = RecordingFetch(telegram_ok=False)

    for _ in range(8):
        report = await run_cycle_async(_env(store), fetch)
        if int(report.get("signals") or 0):
            break

    if not report.get("signals"):
        pytest.skip("no signal produced in this run")

    failures = await store.get("delivery_failures", []) or []
    assert failures, "a rejected send vanished with no trace"
    assert "error" in failures[0]


async def test_dry_run_sends_nothing(sim_venues):
    """`DRY_RUN` must mean it. A test environment that pages the operator is
    not a test environment."""
    store = MemoryStore()
    fetch = RecordingFetch()
    for _ in range(6):
        await run_cycle_async(_env(store, DRY_RUN="true"), fetch)
    assert fetch.sent == []


async def test_the_paper_account_records_every_signal(sim_venues):
    """The measurement exists so the idea can be judged on evidence. A signal
    that is delivered but never booked cannot be evaluated later."""
    from cta.paper import KEY_PAPER

    store = MemoryStore()
    fetch = RecordingFetch()

    total_signals = 0
    for _ in range(8):
        report = await run_cycle_async(_env(store), fetch)
        total_signals += int(report.get("signals") or 0)
        if total_signals:
            break

    if not total_signals:
        pytest.skip("no signal produced in this run")

    account = await store.get(KEY_PAPER)
    assert account is not None
    assert len(account.get("open", [])) + len(account.get("closed", [])) > 0


# ── regressions for what the delivery tests uncovered ───────────────────────
async def test_discovery_sees_every_source_not_a_hardcoded_few(sim_venues):
    """Discovery used to name the sources it was willing to look at.

    `for name in ("dydx", "gmx")` meant a source added later could be built,
    enabled, and reported healthy while remaining invisible to the search.
    Here the simulator is a source nobody hard-coded, and discovery must find
    its traders anyway.
    """
    store = MemoryStore()
    fetch = RecordingFetch()
    for _ in range(5):
        await run_cycle_async(_env(store), fetch)

    watchlist = await store.get("watchlist", []) or []
    sources = {str(w.get("source")) for w in watchlist}
    assert len(watchlist) > 0, "discovery found nobody in a venue full of traders"
    assert "sim" in sources or "sim-beta" in sources


async def test_a_seeded_candidate_keeps_its_account_value():
    """A candidate with no account value is scored against a zero denominator.

    Drawdown is measured relative to account value, so a missing value produced
    an 85% "catastrophic" drawdown for every trader from those venues, and the
    whole source was rejected — while still reporting itself healthy.
    """
    from cta.classifier import classify_trader, stats_from_fills

    venues = build_sim_venues(n_traders=6, seed=7, auto_events=False)
    venue = venues["sim"]
    trader = venue.traders[0]
    fills = venue.parse_fills(await venue.fills(trader.wallet))

    funded = stats_from_fills(
        trader.wallet, fills, account_value_usd=trader.account_value,
        source="sim", data_confirmed=True,
    )
    broke = stats_from_fills(
        trader.wallet, fills, account_value_usd=0.0, source="sim",
        data_confirmed=True,
    )
    classify_trader(funded)
    classify_trader(broke)

    assert broke.max_drawdown >= 0.5, (
        "a zero account value should read as catastrophic, not merely poor"
    )
    assert funded.max_drawdown < broke.max_drawdown
    assert trader.account_value > 0


def test_data_confirmation_is_not_a_hardcoded_source():
    """`data_confirmed` used to be `source == "hyperliquid"`.

    That punished every other source by 15% and barred it from ELITE forever,
    whatever the trader's record. Confirmation is a property of the feed, so
    the venue declares it and the two places that read it must agree.
    """
    from cta.discovery_resume import _venue_confirms_data as resume_rule
    from cta.engine import _venue_confirms_data as engine_rule

    class Venue:
        data_confirmed = True

    class Unverified(Venue):
        data_confirmed = False

    class Plain:  # says nothing
        pass

    assert engine_rule(Venue()) is True
    assert resume_rule(Venue()) is True
    assert engine_rule(Unverified()) is False
    assert resume_rule(Unverified()) is False
    # Silence is not an admission: an unlabelled feed is treated as confirmed,
    # because the alternative silently disables every new source.
    assert engine_rule(Plain()) is True
    assert resume_rule(Plain()) is True


async def test_a_failed_lifecycle_message_is_recorded_too(sim_venues):
    """An exit alert that never arrives is worse than an entry that never
    arrives: the operator is holding a position believing someone is watching
    it. So the lifecycle path must record failures exactly as the raw path
    does — and it did not, at first, because it never went through `_send`.
    """
    store = MemoryStore()
    fetch = RecordingFetch(telegram_ok=False)
    ideas_seen = 0
    for _ in range(10):
        report = await run_cycle_async(_env(store), fetch)
        ideas_seen += int(report.get("ideas_opened") or 0)
        if ideas_seen:
            break

    if not ideas_seen:
        pytest.skip("no idea opened in this run")

    failures = await store.get("delivery_failures", []) or []
    assert failures, "a rejected lifecycle message vanished with no trace"
    assert any(
        ":" in str(f.get("signal_id", "")) for f in failures
    ), f"only raw signals were recorded: {failures}"


async def test_the_failure_recorder_cannot_be_silenced_by_a_bad_clock():
    """The recorder was passing the clock *function* instead of calling it, so
    `now.isoformat()` raised inside the `except: pass` and the failure was lost.

    A component whose only job is to report failures must not have a code path
    where it fails silently.
    """
    from cta.runtime import _send

    class RefusingTelegram:
        async def send(self, text, keyboard=None):
            return {"ok": False, "error": "chat not found"}

    store = MemoryStore()

    # A callable in the clock slot is exactly the mistake that was made.
    delivered = await _send(
        RefusingTelegram(), store, "hello", label="x",
        _now=lambda: datetime.now(UTC),
    )
    assert delivered == 0
    failures = await store.get("delivery_failures", []) or []
    assert failures, "the recorder swallowed its own error"
