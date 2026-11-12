"""Seeds: watching real traders without waiting for a 39 MB download.

The failure these tests guard against
------------------------------------
`stats-data.hyperliquid.xyz/Mainnet/leaderboard` is ~39 MB of ~47,000 wallets.
A Workers free-plan invocation gets 10 ms of CPU and 128 MB of memory. The
download and `JSON.parse` cannot fit, so the invocation dies — after the cheap
health probes have written "three green platforms" and before the discovery
cursor is saved.

The dashboard then reported green platforms, climbing cycle counts, and a search
that had never started, indefinitely. Every prior fix assumed discovery was
reaching the leaderboard, so every prior fix missed.

The lesson: discovery must not *depend* on the big file for the radar to watch
somebody. Seeds are one subrequest per wallet to poll, which fits.
"""

from __future__ import annotations

import json

import pytest

from cta.config import Config
from cta.seeds import describe, merge_seeds, normalise, parse_addresses, seeds_from_config
from cta.state import MemoryStore

GOOD = "0x85ecf584f25db6f146718b86d493e33c5af72052"
OTHER = "0x1f2b7cd4a9d05e4e0dd2a4e7a1c1b0a1c1d1e1f1"


# ── address parsing ─────────────────────────────────────────────────────────
def test_a_valid_address_is_normalised_to_lowercase():
    assert normalise(GOOD.upper().replace("0X", "0x")) == GOOD


def test_a_bare_hex_string_gains_its_prefix():
    assert normalise(GOOD[2:]) == GOOD


def test_junk_is_rejected_rather_than_stored():
    for junk in ("", "hello", "0x123", "not-an-address", None, "0x" + "z" * 40):
        assert normalise(junk) is None


def test_people_paste_addresses_in_many_shapes():
    """A rejected paste reads as a bug in the radar, so accept all of these."""
    raw = f"{GOOD}, {OTHER}\n{GOOD};  {OTHER}"
    assert parse_addresses(raw) == [GOOD, OTHER]


def test_an_explorer_url_is_accepted():
    url = f"https://app.hyperliquid.xyz/explorer/address/{GOOD}"
    assert parse_addresses(url) == [GOOD]


def test_duplicates_collapse():
    assert parse_addresses(f"{GOOD} {GOOD} {GOOD}") == [GOOD]


# ── configuration ───────────────────────────────────────────────────────────
def test_seeds_come_from_config_with_either_separator():
    assert seeds_from_config(Config(seed_wallets=f"{GOOD},{OTHER}")) == [GOOD, OTHER]
    assert seeds_from_config(Config(seed_wallets=f"{GOOD} {OTHER}")) == [GOOD, OTHER]


def test_an_empty_config_means_no_seeds():
    assert seeds_from_config(Config()) == []


def test_merging_preserves_order_and_drops_duplicates():
    merged = merge_seeds([GOOD], [OTHER, GOOD])
    assert merged == [GOOD, OTHER]


def test_merging_is_capped_so_a_paste_cannot_bankrupt_a_tick():
    many = [f"0x{i:040x}" for i in range(500)]
    assert len(merge_seeds([], many, limit=50)) == 50


def test_the_seed_line_is_readable():
    assert "لا محافظ" in describe([])
    assert "2" in describe([GOOD, OTHER])


# ── the behaviour that matters ──────────────────────────────────────────────
class _Resp:
    status = 200

    def __init__(self, data):
        self._data = data

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)


@pytest.mark.asyncio
async def test_the_radar_watches_seeds_when_the_leaderboard_is_impossible():
    """The exact production failure, reproduced and fixed.

    The leaderboard raises the way a 39 MB download on a 10 ms CPU budget does.
    Before seeds existed, this produced an empty watchlist forever. Now the
    radar watches the seeds and reports the leaderboard failure alongside them.
    """
    import time as _time

    from cta.engine import profile_watchlist_resumable
    from cta.sources import build_venues

    async def fetch(resource=None, **kwargs):
        url = resource or kwargs.get("url", "")
        body = kwargs.get("body") or ""
        if isinstance(body, str) and "userFills" in body:
            url = "userFills"
        if "leaderboard" in str(url):
            raise MemoryError("Worker exceeded its memory limit")
        if "userFills" in str(url):
            now_ms = int(_time.time() * 1000)
            return _Resp([
                {"coin": "BTC", "side": "B", "px": "100000", "sz": "0.1",
                 "time": now_ms - day * 86_400_000, "closedPnl": "95"}
                for day in range(150)
            ])
        return _Resp({})

    cfg = Config(seed_wallets=f"{GOOD}, {OTHER}")
    store = MemoryStore()
    report = await profile_watchlist_resumable(build_venues(fetch, cfg), store, cfg)

    assert report["ok"] is True, "a failed leaderboard must not fail the cycle"
    assert report["scored"] == 2
    assert report["watchlist_size"] == 2

    watchlist = await store.get("watchlist", [])
    assert len(watchlist) == 2
    assert {w["wallet"] for w in watchlist} == {GOOD, OTHER}

    # The failure is still recorded — fixed is not the same as hidden.
    assert "leaderboard" in str(report["progress"].get("last_error", ""))


@pytest.mark.asyncio
async def test_seeds_are_persisted_so_the_next_run_can_use_them():
    import time as _time

    from cta.engine import profile_watchlist_resumable
    from cta.sources import build_venues

    async def fetch(resource=None, **kwargs):
        body = kwargs.get("body") or ""
        if isinstance(body, str) and "userFills" in body:
            now_ms = int(_time.time() * 1000)
            return _Resp([
                {"coin": "BTC", "side": "B", "px": "100000", "sz": "0.1",
                 "time": now_ms - day * 86_400_000, "closedPnl": "95"}
                for day in range(150)
            ])
        raise RuntimeError("leaderboard unavailable")

    cfg = Config(seed_wallets=GOOD)
    store = MemoryStore()
    await profile_watchlist_resumable(build_venues(fetch, cfg), store, cfg)

    stored = await store.get("seed_wallets", [])
    assert GOOD in stored


# ── the chat commands ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_add_accepts_an_address_and_persists_it():
    from cta.bot import Bot
    from cta.telegram import Telegram

    sent: list[dict] = []

    async def fetch(resource=None, **kwargs):
        sent.append(json.loads(kwargs.get("body") or "{}"))
        return _Resp({"ok": True, "result": {"message_id": 1}})

    class Env:
        TELEGRAM_BOT_TOKEN = "1:x"
        TELEGRAM_CHAT_ID = "555"
        DRY_RUN = "true"

    store = MemoryStore()
    bot = Bot(Telegram(fetch, "1:x", "555"), store, env=Env())

    reply = await bot.cmd_add(GOOD)
    assert "أُضيف" in reply
    assert GOOD in (await store.get("seed_wallets", []))


@pytest.mark.asyncio
async def test_add_explains_itself_when_the_address_is_unusable():
    from cta.bot import Bot
    from cta.telegram import Telegram

    async def fetch(resource=None, **kwargs):
        return _Resp({"ok": True, "result": {}})

    class Env:
        TELEGRAM_BOT_TOKEN = "1:x"
        TELEGRAM_CHAT_ID = "555"
        DRY_RUN = "true"

    bot = Bot(Telegram(fetch, "1:x", "555"), MemoryStore(), env=Env())
    reply = await bot.cmd_add("hello")
    assert "لم أجد" in reply
    assert "/add 0x" in reply, "must show the correct form"


@pytest.mark.asyncio
async def test_remove_takes_a_wallet_back_out():
    from cta.bot import Bot
    from cta.telegram import Telegram

    async def fetch(resource=None, **kwargs):
        return _Resp({"ok": True, "result": {}})

    class Env:
        TELEGRAM_BOT_TOKEN = "1:x"
        TELEGRAM_CHAT_ID = "555"
        DRY_RUN = "true"

    store = MemoryStore()
    await store.set("seed_wallets", [GOOD, OTHER])
    bot = Bot(Telegram(fetch, "1:x", "555"), store, env=Env())

    reply = await bot.cmd_remove(GOOD)
    assert "أُزيل" in reply
    assert await store.get("seed_wallets") == [OTHER]


@pytest.mark.asyncio
async def test_seeds_command_tells_the_operator_what_to_do_when_empty():
    from cta.bot import Bot
    from cta.telegram import Telegram

    async def fetch(resource=None, **kwargs):
        return _Resp({"ok": True, "result": {}})

    class Env:
        TELEGRAM_BOT_TOKEN = "1:x"
        TELEGRAM_CHAT_ID = "555"
        DRY_RUN = "true"

    bot = Bot(Telegram(fetch, "1:x", "555"), MemoryStore(), env=Env())
    reply = await bot.cmd_seeds()
    assert "/add" in reply
    # The reason this matters must be visible, not buried in a commit message.
    assert "الخطة المجانية" in reply or "القائمة العامة" in reply
