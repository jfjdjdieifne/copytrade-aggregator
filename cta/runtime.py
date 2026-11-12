"""Runtime glue shared by the Worker entrypoint and the local CLI.

Both call `run_cycle_async` / `handle_telegram_async`, which means the exact code
exercised by pytest is the code that runs in production. There is no second
implementation to drift.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .ai import narrate_signal
from .bot import Bot
from .config import Config
from .engine import run_cycle
from .http_bridge import as_fetcher
from .outcomes import evaluate, summarize
from .sources import build_venues
from .state import (
    KEY_OWNER_CHAT,
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
    fetch = as_fetcher(fetch)
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

    # Record the attempt BEFORE the expensive work, and increment the cycle
    # counter here rather than after `run_cycle`.
    #
    # This ordering is the whole diagnosis of the user's screenshot. The old
    # code wrote `source_health` (three green platforms), then ran the cycle,
    # then incremented `cycles` and wrote `last_cycle_at`. When the cycle was
    # killed by the 50-subrequest ceiling, the counter never moved — so the
    # dashboard showed "0 دورات" and "آخر دورة: —" next to green platforms, and
    # `verdict()` had nothing to read. The system was working hard and reporting
    # that it had never started.
    #
    # A cycle that begins is a fact worth recording even if it does not finish.
    counters = await store.get("counters", {}) or {}
    counters["cycles"] = int(counters.get("cycles", 0)) + 1
    counters["attempts"] = int(counters.get("attempts", 0)) + 1
    await store.set("counters", counters)
    await store.set("last_attempt_at", _now().isoformat())

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

    # The positions cache must NOT re-poll the watchlist.
    #
    # This block used to call `snapshot_positions`, which runs `collect_positions`
    # over every watched wallet — a second full sweep, immediately after `scan`
    # had already done one. That doubled the subrequest cost of every cycle and
    # pushed an already-over-budget invocation further over the 50 ceiling.
    #
    # `scan` now records what it actually measured, so the cache is derived from
    # those observations instead of costing anything.
    try:
        await store.set("positions_cache", await _cache_from_last_scan(store))
    except Exception:
        pass

    await store.set("last_cycle_at", _now().isoformat())
    counters = await store.get("counters", {}) or {}
    # `cycles` was already incremented when the attempt started; only the
    # outcome counters are added here.
    counters["completed"] = int(counters.get("completed", 0)) + 1
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


async def _cache_from_last_scan(store) -> list[dict]:
    """Build the `/positions` cache from the positions the scan just observed.

    Reads persisted state only: no venue calls, no subrequests. If the scan has
    not run yet the cache is empty, which is honest — there is nothing observed
    to show.
    """
    observed = await store.get("last_positions", []) or []
    out: list[dict] = []
    for p in observed:
        if not isinstance(p, dict):
            continue
        out.append(
            {
                "wallet": p.get("wallet"),
                "source": p.get("source"),
                "coin": p.get("coin"),
                "side": p.get("side"),
                "entry": p.get("entry"),
                "size_usd": round(float(p.get("size_usd", 0) or 0), 2),
                "leverage": p.get("leverage"),
                "quality": p.get("quality"),
                "quality_score": p.get("quality_score"),
            }
        )
    return out


async def run_profile_async(env, fetch, cfg: Optional[Config] = None) -> dict:
    """Advance discovery by one affordable slice.

    This used to rebuild the whole watchlist in a single invocation, which
    attempted ~901 subrequests against a 50-subrequest ceiling and died. It now
    scores what the budget allows, persists a cursor, and resumes next run, so
    discovery progresses across many invocations instead of failing once.
    """
    from .engine import profile_watchlist_resumable

    cfg = cfg or Config.from_env(env)
    store = store_from_env(env)
    venues = build_venues(fetch, cfg)

    try:
        report = await profile_watchlist_resumable(venues, store, cfg)
    except Exception as exc:
        # Discovery must never take the whole invocation down: the cron shares
        # this call with the Telegram poll, so an uncaught error here is a bot
        # that stops answering. Report the failure instead of dying.
        report = {
            "ok": False,
            "scored": 0,
            "passed": 0,
            "total": 0,
            "cursor": 0,
            "complete": False,
            "reason": f"{type(exc).__name__}: {exc}"[:160],
            "budget": {},
        }
    watchlist = await get_watchlist(store)

    elite = sum(1 for w in watchlist if w.get("grade") == "ELITE")
    proven = sum(1 for w in watchlist if w.get("grade") == "PROVEN")

    return {
        "ok": bool(report.get("ok")),
        "watchlist": len(watchlist),
        "elite": elite,
        "proven": proven,
        "discovery": {
            "scored_this_run": report.get("scored", 0),
            "passed_this_run": report.get("passed", 0),
            "total_candidates": report.get("total", 0),
            "cursor": report.get("cursor", 0),
            "complete": report.get("complete", False),
            "reason": report.get("reason", ""),
            "failures": report.get("failures", []),
        },
        "budget": report.get("budget", {}),
    }


async def handle_telegram_async(env, fetch, update: dict) -> dict:
    """Route one Telegram update."""
    fetch = as_fetcher(fetch)
    cfg = Config.from_env(env)
    store = store_from_env(env)
    tg = Telegram(fetch, getattr(env, "TELEGRAM_BOT_TOKEN", ""), getattr(env, "TELEGRAM_CHAT_ID", ""))
    bot = Bot(tg, store, env=env, cfg=cfg)
    return await bot.handle_update(update)


async def ensure_webhook_async(env, fetch, base: str = "") -> dict:
    """Register the Telegram webhook without anyone clicking /setup.

    Setup used to require a visit to `/setup`, which is easy to skip and leaves
    the bot silently dead: Telegram has no webhook, so `/start` goes nowhere and
    the operator has no signal at all. The cron now does it, so a deployment
    that has a token becomes live on its own within a minute.

    It is deliberately cheap and idempotent: `getWebhookInfo` first, and only
    `setWebhook` when it disagrees. That keeps the free-tier request budget
    intact and avoids re-registering on every run.
    """
    fetch = as_fetcher(fetch)
    token = str(getattr(env, "TELEGRAM_BOT_TOKEN", "") or "")
    if not token or not base:
        return {"ok": False, "reason": "no token or base url"}

    tg = Telegram(fetch, token)
    info = await tg.get_webhook_info()
    detail = info.get("result", {}) if info.get("ok") else {}
    current = str(detail.get("url", "") or "")
    expected = f"{base.rstrip('/')}/telegram"
    if current == expected:
        return {"ok": True, "changed": False, "url": current}

    result = await tg.set_webhook(expected)
    return {
        "ok": bool(result.get("ok")),
        "changed": True,
        "url": expected,
        "telegram_said": result.get("description", ""),
    }


async def self_test_async(env, fetch) -> dict:
    """Ask the platform seven questions and report the answers.

    This exists because "nothing arrived on Telegram" has many indistinguishable
    causes, and waiting for a message cannot tell them apart. Each check below
    is one of the failure modes actually seen in this project:

      1. state      — the Durable Object was handed over as a namespace, so
                      every read/write raised and the dashboard showed zeros
      2. fetch      — the SDK's fetch is keyword-only: our (url, dict) call
                      raised TypeError and was reported as a bad token
      3. token      — getMe proves the token and names the bot
      4. webhook    — Telegram's own view of the registered URL
      5. send       — an actual sendMessage to the owner chat
      6. ai         — whether Workers AI is bound (narration vs deterministic)
      7. clock      — UTC time, so logs can be matched to this page

    Nothing here is inferred; every field is a result the platform returned.
    """
    fetch = as_fetcher(fetch)
    out: dict = {"ok": True, "checks": {}}

    # 1. state
    store = store_from_env(env)
    probe = f"selftest:{datetime.now(UTC).timestamp()}"
    try:
        await store.set(probe, {"t": 1})
        round_trip = await store.get(probe, None)
        await store.delete(probe)
        out["checks"]["state"] = {
            "ok": round_trip == {"t": 1},
            "backend": type(store).__name__,
            "detail": "" if round_trip == {"t": 1} else "write succeeded but read returned nothing",
        }
    except Exception as exc:
        out["checks"]["state"] = {
            "ok": False,
            "backend": type(store).__name__,
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }

    # 2. fetch reachability (Telegram's getMe is the cheapest real call)
    token = str(getattr(env, "TELEGRAM_BOT_TOKEN", "") or "")
    tg = Telegram(fetch, token)
    if not token:
        out["checks"]["fetch"] = {"ok": False, "detail": "no TELEGRAM_BOT_TOKEN to call"}
        out["checks"]["token"] = {"ok": False, "detail": "missing"}
        out["checks"]["webhook"] = {"ok": False, "detail": "missing token"}
        out["checks"]["send"] = {"ok": False, "detail": "missing token"}
    else:
        me = await tg.call("getMe", {})
        out["checks"]["fetch"] = {
            "ok": "error" not in me,
            "detail": str(me.get("error", ""))[:200],
        }

        # 3. token
        info = me.get("result", {}) if me.get("ok") else {}
        out["checks"]["token"] = {
            "ok": bool(me.get("ok")),
            "bot_username": info.get("username", ""),
            "bot_id": info.get("id", ""),
            "detail": "" if me.get("ok") else str(me.get("description", me.get("error", "")))[:200],
        }

        # 4. webhook
        hook = await tg.get_webhook_info()
        detail = hook.get("result", {}) if hook.get("ok") else {}
        out["checks"]["webhook"] = {
            "ok": bool(detail.get("url")) and not detail.get("last_error_message"),
            "url": detail.get("url", ""),
            "pending": detail.get("pending_update_count", 0),
            "last_error": detail.get("last_error_message", ""),
        }

        # 5. an actual send, to the owner chat if we know it
        target = str(getattr(env, "TELEGRAM_CHAT_ID", "") or "")
        if not target:
            try:
                target = str(await store.get(KEY_OWNER_CHAT, "") or "")
            except Exception:
                target = ""
        if target:
            sent = await tg.send(
                "🧪 فحص ذاتي من الرادار — إذا وصلتك هذه الرسالة فالإرسال يعمل.",
                target,
            )
            out["checks"]["send"] = {
                "ok": bool(sent.get("ok")),
                "chat_id": target,
                "detail": "" if sent.get("ok") else str(sent.get("error", sent.get("description", "")))[:200],
            }
        else:
            out["checks"]["send"] = {
                "ok": False,
                "chat_id": "",
                "detail": "لا يوجد مالك بعد — أرسل /start للبوت لتصبح المالك، ثم أعد الفحص",
            }

    # 6. ai
    out["checks"]["ai"] = {
        "ok": getattr(env, "AI", None) is not None,
        "model": str(getattr(env, "AI_MODEL", "") or ""),
        "detail": "" if getattr(env, "AI", None) is not None else "no AI binding; narrated deterministically",
    }

    # 7. clock
    out["checks"]["clock"] = {"ok": True, "utc": datetime.now(UTC).isoformat()}

    out["ok"] = all(c.get("ok") for c in out["checks"].values())
    return out


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
