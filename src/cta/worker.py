"""HTTP surface of the Worker.

Routes
------
POST /telegram   Telegram webhook (secret-token protected)
GET  /            short status page, safe to hit in a browser
GET  /health      JSON health for uptime checks
GET  /signals     last signals as JSON
POST /kick        run one cycle now (owner token required)
POST /refresh     rebuild the watchlist now (owner token required)
POST /boot        first-run setup: webhook + watchlist (owner token required)
"""

from __future__ import annotations

import json
from typing import Any

from . import bootstrap  # noqa: F401  (kept for symmetry with local runner)
from .runtime import (
    boot_async,
    handle_telegram_async,
    run_cycle_async,
    run_profile_async,
    track_outcomes_async,
)
from .state import get_signals, get_watchlist, store_from_env

JSON_HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
}


def _json(data: Any, status: int = 200) -> Any:
    """Return a plain tuple: works with workers-py and discourages Response drilling."""
    return (json.dumps(data, ensure_ascii=False, default=str), {"status": status, **JSON_HEADERS})


def _text(body: str, status: int = 200) -> Any:
    return (body, {"status": status, "content-type": "text/plain; charset=utf-8"})


async def _authorised(request, env) -> bool:
    expected = getattr(env, "OWNER_TOKEN", "") or ""
    if not expected:
        return True  # unset token = open (documented; set it in production)
    headers = getattr(request, "headers", {}) or {}
    provided = headers.get("x-owner-token", "") if hasattr(headers, "get") else ""
    return provided == expected


def _path(request) -> str:
    url = getattr(request, "url", "") or ""
    path = url.split("://", 1)[-1]
    idx = path.find("/")
    return (path[idx:] if idx >= 0 else "/").split("?")[0].rstrip("/") or "/"


def _method(request) -> str:
    return (getattr(request, "method", "GET") or "GET").upper()


async def _body_json(request) -> dict:
    try:
        if hasattr(request, "json"):
            data = request.json()
            if hasattr(data, "__await__"):
                data = await data
            return data if isinstance(data, dict) else {}
        raw = getattr(request, "body", "")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _default_fetch(env):
    """Resolve a fetch implementation, preferring one injected for tests.

    `js` only exists inside the Cloudflare runtime, so it is imported lazily: a
    missing binding must degrade to a clear error, never to an import crash that
    takes the whole Worker down before a single route runs.
    """
    try:
        injected = getattr(env, "fetch", None)
    except Exception:
        injected = None
    if callable(injected):
        return injected
    try:
        import js  # noqa: PLC0415 — Cloudflare-only module

        return js.fetch
    except Exception:  # pragma: no cover - only hit outside Cloudflare
        raise RuntimeError(
            "no fetch implementation available: pass fetch=... or run on Cloudflare"
        )


async def handle(request, env, ctx=None, fetch=None):
    """Single entrypoint for every HTTP request."""
    fetcher = fetch or _default_fetch(env)

    path = _path(request)
    method = _method(request)

    try:
        if path == "/telegram" and method == "POST":
            expected_secret = getattr(env, "TELEGRAM_WEBHOOK_SECRET", "") or ""
            if expected_secret:
                headers = getattr(request, "headers", {}) or {}
                got = headers.get("x-telegram-bot-api-secret-token", "") if hasattr(headers, "get") else ""
                if got != expected_secret:
                    return _json({"ok": False, "error": "bad secret"}, 403)
            update = await _body_json(request)
            result = await handle_telegram_async(env, fetcher, update)
            return _json(result)

        if path in ("/", "/status"):
            store = store_from_env(env)
            watchlist = await get_watchlist(store)
            signals = await get_signals(store)
            last = await store.get("last_cycle_at", "—")
            html = (
                "<h1>Cop-Trade Aggregator — Radار</h1>"
                f"<p>watchlist: <b>{len(watchlist)}</b></p>"
                f"<p>signals stored: <b>{len(signals)}</b></p>"
                f"<p>last cycle: <b>{last}</b></p>"
                "<p>HYPOTHETICAL_ — not financial advice.</p>"
            )
            return (html, {"status": 200, "content-type": "text/html; charset=utf-8"})

        if path == "/health":
            store = store_from_env(env)
            watchlist = await get_watchlist(store)
            counters = await store.get("counters", {}) or {}
            return _json(
                {
                    "ok": True,
                    "watchlist": len(watchlist),
                    "cycles": counters.get("cycles", 0),
                    "signals": counters.get("signals", 0),
                    "last_cycle_at": await store.get("last_cycle_at"),
                    "source_health": await store.get("source_health", {}),
                }
            )

        if path == "/signals":
            store = store_from_env(env)
            signals = await get_signals(store)
            return _json({"ok": True, "count": len(signals), "signals": signals[-20:]})

        if path in ("/kick", "/refresh", "/boot", "/track") and method == "POST":
            if not await _authorised(request, env):
                return _json({"ok": False, "error": "unauthorised"}, 401)
            if path == "/kick":
                return _json(await run_cycle_async(env, fetcher))
            if path == "/refresh":
                return _json(await run_profile_async(env, fetcher))
            if path == "/track":
                return _json(await track_outcomes_async(env, fetcher))
            return _json(await boot_async(env, fetcher))

        return _json({"ok": False, "error": f"no route for {method} {path}"}, 404)

    except Exception as exc:  # never leak a stack trace to Telegram or the web
        return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}, 500)


async def scheduled(env, ctx=None, fetch=None):
    """Cron trigger: profile daily, scan every run, track outcomes hourly."""
    fetcher = fetch or _default_fetch(env)

    result = await run_cycle_async(env, fetcher)
    try:
        await track_outcomes_async(env, fetcher)
    except Exception:
        pass

    # Cheap daily maintenance: rebuild the watchlist once a day.
    try:
        store = store_from_env(env)
        last_profile = await store.get("last_profile_at", "")
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date().isoformat()
        if not str(last_profile).startswith(today):
            await run_profile_async(env, fetcher)
            await store.set("last_profile_at", datetime.now(timezone.utc).isoformat())
            result["profiled"] = True
    except Exception:
        pass

    return result
