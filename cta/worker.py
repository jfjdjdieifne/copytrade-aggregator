"""HTTP surface of the Worker.

Routes
------
POST /telegram   Telegram webhook (secret-token protected)
GET  /            short status page, safe to hit in a browser
GET  /health      JSON health for uptime checks
GET  /signals     last signals as JSON
GET  /dash        full Arabic dashboard with the live state the cron built
GET  /telegram/setup  register the Telegram webhook from a phone browser
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
from .dashboard import render_dashboard
from .telegram import Telegram
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


def _origin(request) -> str:
    """Reconstruct the public base URL from the request itself.

    Cloudflare gives us the full URL the client used, so the Worker can discover
    its own address. That removes the main first-run failure mode: a placeholder
    or mistyped WORKER_URL silently pointing Telegram at a host that does not
    exist, which makes the bot look dead with no error anywhere.
    """
    url = getattr(request, "url", "") or ""
    if "://" in url:
        scheme, _, rest = url.partition("://")
        host = rest.split("/", 1)[0]
        if host:
            return f"{scheme}://{host}"
    return ""


def _configured_base(env) -> str:
    """WORKER_URL if it is real, otherwise "" so callers can fall back."""
    raw = str(getattr(env, "WORKER_URL", "") or "").strip().rstrip("/")
    # The shipped wrangler.toml value contains "<your-subdomain>"; treat any
    # angle bracket as "not configured" rather than registering it with Telegram.
    if not raw or "<" in raw or ">" in raw:
        return ""
    return raw


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


async def _unavailable_fetch(*_args, **_kwargs):
    """Placeholder used when no fetch implementation can be resolved.

    Raising here (instead of at resolve time) is deliberate. Routes such as
    `/health` and `/dash` read only from the Durable Object and need no network
    at all; a failure to find `js.fetch` must not take those down with it. Only
    a venue call — which genuinely needs the network — should fail.
    """
    raise RuntimeError("no fetch implementation available: pass fetch=... or run on Cloudflare")


def _default_fetch(env):
    """Resolve a fetch implementation, preferring one injected for tests.

    `js` only exists inside the Cloudflare runtime, so it is imported lazily and
    never allowed to raise at import time.
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
        return _unavailable_fetch


async def _dashboard_html(store, base_url: str = "") -> str:
    """Render the live dashboard from whatever the last cron cycle stored.

    Everything here comes out of the Durable Object, so this page is a window on
    real state — not a re-run of a scenario. If a cycle has not completed yet the
    page says so rather than showing invented numbers.
    """
    watchlist = await get_watchlist(store)
    signals = await get_signals(store)
    last = await store.get("last_cycle_at")
    health = await store.get("source_health", {}) or {}
    rejections = await store.get("last_rejections", []) or []
    counters = await store.get("counters", {}) or {}

    return render_dashboard(
        signals[-20:],
        watchlist,
        {
            "cycles": counters.get("cycles", 0),
            "clusters": counters.get("clusters", 0),
            "signals": counters.get("signals", 0),
        },
        rejections,
        live=bool(health) and any(health.values()),
        source_health=health,
        last_cycle=last,
        base_url=base_url,
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

        if path in ("/telegram/setup", "/setup"):
            # Deliberately a GET so it can be run from a phone's address bar:
            # there is no easy way to send a POST from mobile Safari/Chrome, and
            # needing one was the main reason first-run setup was painful.
            token = str(getattr(env, "TELEGRAM_BOT_TOKEN", "") or "")
            secret = str(getattr(env, "TELEGRAM_WEBHOOK_SECRET", "") or "")
            # Prefer the configured URL, but fall back to what the browser used.
            base = _configured_base(env) or _origin(request)

            if not token or not base:
                return _json(
                    {
                        "ok": False,
                        "error": "TELEGRAM_BOT_TOKEN is not set",
                        "hint": "Add it in Settings > Variables and Secrets, then reload this page",
                    },
                    400,
                )

            tg = Telegram(fetcher, token)
            hook = await tg.set_webhook(f"{base}/telegram", secret=secret)
            info = await tg.get_webhook_info()
            detail = info.get("result", {}) if info.get("ok") else {}
            return _json(
                {
                    "ok": bool(hook.get("ok")),
                    "registered_url": detail.get("url", ""),
                    "expected_url": f"{base}/telegram",
                    "worker_url": base,
                    "worker_url_source": "WORKER_URL var" if _configured_base(env) else "auto-detected",
                    "dashboard": f"{base}/dash",
                    "secret_registered": bool(secret),
                    "pending_updates": detail.get("pending_update_count", 0),
                    "last_error": detail.get("last_error_message", ""),
                    "telegram_said": hook.get("description", ""),
                }
            )

        if path in ("/", "/status", "/dash"):
            store = store_from_env(env)
            html = await _dashboard_html(store, _configured_base(env) or _origin(request))
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
