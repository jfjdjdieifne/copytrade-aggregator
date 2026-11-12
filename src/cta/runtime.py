"""Runtime glue shared by the Worker entrypoint and the local CLI.

Both call `run_cycle_async` / `handle_telegram_async`, which means the exact code
exercised by pytest is the code that runs in production. There is no second
implementation to drift.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import outputs
from .ai import narrate_signal
from .bot import Bot
from .config import Config
from .engine import run_cycle
from .outcomes import evaluate, summarize
from .sources import build_venues
from .state import (
    KEY_STATS,
    append_outcome,
    get_signals,
    get_watchlist,
    remember_signal,
    store_from_env,
)
from .telegram import Telegram, signal_keyboard

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


async def run_cycle_async(env, fetch, cfg: Optional[Config] = None) -> dict:
    """One full radar cycle. Returns a JSON-serialisable report."""
    cfg = cfg or Config.from_env(env)
    store = store_from_env(env)
    venues = build_venues(fetch, cfg)

    health = {}
    for name, venue in venues.items():
        try:
            health[name] = bool(await venue.health())
        except Exception:
            health[name] = False
    await store.set("source_health", health)

    report = await run_cycle(venues, store, cfg, now=_now())

    signals = report.pop("signal_objects", []) or []
    tg = Telegram(fetch, getattr(env, "TELEGRAM_BOT_TOKEN", ""), getattr(env, "TELEGRAM_CHAT_ID", ""))

    delivered = 0
    for sig in signals:
        payload = sig.to_dict()
        await remember_signal(store, payload)
        if not cfg.dry_run and tg.configured:
            keyboard = signal_keyboard(sig.signal_id)
            result = await tg.send(narrate_signal(sig), keyboard=keyboard)
            if result.get("ok"):
                delivered += 1

    # Keep the positions cache fresh so /positions answers instantly.
    try:
        watchlist = await get_watchlist(store)
        if watchlist:
            positions = await outputs.snapshot_positions(venues, watchlist, cfg)
            await store.set("positions_cache", positions)
    except Exception:
        pass

    await store.set("last_cycle_at", _now().isoformat())
    counters = await store.get("counters", {}) or {}
    counters["cycles"] = int(counters.get("cycles", 0)) + 1
    counters["clusters"] = int(counters.get("clusters", 0)) + report.get("clusters", 0)
    counters["signals"] = int(counters.get("signals", 0)) + len(signals)
    await store.set("counters", counters)

    return {
        "ok": True,
        "profile": report.get("profiled"),
        "watchlist": report.get("watchlist_size", 0),
        "clusters": report.get("clusters", 0),
        "signals": len(signals),
        "delivered": delivered,
        "source_health": health,
    }


async def run_profile_async(env, fetch, cfg: Optional[Config] = None) -> dict:
    """Rebuild the watchlist from scratch (the expensive, daily job)."""
    from .engine import profile_watchlist

    cfg = cfg or Config.from_env(env)
    store = store_from_env(env)
    venues = build_venues(fetch, cfg)
    watchlist = await profile_watchlist(venues, store, cfg)

    elite = sum(1 for w in watchlist if w.get("grade") == "ELITE")
    return {
        "ok": True,
        "watchlist": len(watchlist),
        "elite": elite,
        "proven": len(watchlist) - elite,
    }


async def handle_telegram_async(env, fetch, update: dict) -> dict:
    """Route one Telegram update."""
    cfg = Config.from_env(env)
    store = store_from_env(env)
    tg = Telegram(fetch, getattr(env, "TELEGRAM_BOT_TOKEN", ""), getattr(env, "TELEGRAM_CHAT_ID", ""))
    bot = Bot(tg, store, env=env, cfg=cfg)
    return await bot.handle_update(update)


async def track_outcomes_async(env, fetch, cfg: Optional[Config] = None) -> dict:
    """Re-price stored signals and refresh the stats the bot reports."""
    cfg = cfg or Config.from_env(env)
    store = store_from_env(env)
    venues = build_venues(fetch, cfg)

    prices: dict[str, float] = {}
    hl = venues.get("hyperliquid")
    if hl is not None:
        try:
            prices = await hl.prices()
        except Exception:
            prices = {}

    signals = await get_signals(store)
    if not signals or not prices:
        return {"ok": True, "evaluated": 0}

    for sig in signals:
        price = prices.get(sig.get("coin", ""))
        if not price:
            continue
        outcome = evaluate(sig, price, now=_now())
        if outcome.get("r_multiple") is not None or outcome.get("age_min", 0) >= 60:
            await append_outcome(store, outcome)

    all_outcomes = await store.get("outcomes", []) or []
    stats = summarize(all_outcomes)
    await store.set(KEY_STATS, stats)
    return {"ok": True, **stats}


async def boot_async(env, fetch) -> dict:
    """First-run setup: webhook registration + watchlist if empty."""
    cfg = Config.from_env(env)
    store = store_from_env(env)
    tg = Telegram(fetch, getattr(env, "TELEGRAM_BOT_TOKEN", ""), getattr(env, "TELEGRAM_CHAT_ID", ""))

    result: dict[str, Any] = {"ok": True, "steps": []}

    watchlist = await get_watchlist(store)
    if not watchlist:
        profile = await run_profile_async(env, fetch, cfg)
        result["steps"].append({"profile": profile})
        result["watchlist"] = profile.get("watchlist", 0)
    else:
        result["watchlist"] = len(watchlist)

    if tg.configured:
        try:
            # The secret must be registered with Telegram *and* checked on the
            # way in. Passing it here but not in `worker` would silently 403
            # every update; checking it there but not passing it here would make
            # the bot look dead. Both sides read the same env var.
            secret = str(getattr(env, "TELEGRAM_WEBHOOK_SECRET", "") or "")
            webhook = await tg.set_webhook(f"{env.WORKER_URL}/telegram", secret=secret)
            result["steps"].append(
                {"webhook": webhook.get("ok", False), "secret": bool(secret)}
            )
            if not webhook.get("ok"):
                result["steps"].append(
                    {"webhook_error": str(webhook.get("description", ""))[:150]}
                )
        except Exception as exc:
            result["steps"].append({"webhook_error": str(exc)[:100]})

    return result
