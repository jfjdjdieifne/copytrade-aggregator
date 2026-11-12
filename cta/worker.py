"""HTTP surface of the Worker.

Routes
------
POST /telegram   Telegram webhook (secret-token protected)
GET  /            short status page, safe to hit in a browser
GET  /health      JSON health for uptime checks
GET  /signals     last signals as JSON
GET  /dash        full Arabic dashboard with the live state the cron built
GET  /setup       register the Telegram webhook, rendered as a checklist
GET  /setup.json  the same result as JSON (for scripts)
POST /kick        run one cycle now (owner token required)
POST /refresh     rebuild the watchlist now (owner token required)
POST /boot        first-run setup: webhook + watchlist (owner token required)
"""

from __future__ import annotations

import json
from typing import Any

from . import bootstrap  # noqa: F401  (kept for symmetry with local runner)
from .runtime import (
    ensure_webhook_async,
    self_test_async,
    boot_async,
    handle_telegram_async,
    run_cycle_async,
    run_profile_async,
    track_outcomes_async,
)
from .dashboard import (
    render_dashboard,
    render_selftest,
    render_setup_page,
    render_state_error,
)
from .http_bridge import as_fetcher
from .telegram import Telegram
from .state import KEY_OWNER_CHAT, KEY_PUBLIC_BASE, KEY_TRACE, get_signals, get_watchlist, store_from_env

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


def _origin_from_env(env) -> str:
    """Self-discover the public URL when there is no request (the cron path).

    The origin normally comes from the incoming request, but a cron trigger has
    none. The bindings expose it: the runtime injects `CF_*`/`WORKER_URL`-style
    values depending on version, so try each in turn. Failing all of them, the
    cron simply skips webhook registration — `/setup` still works by hand.
    """
    for name in ("WORKER_URL", "CF_WORKER_URL", "WORKER_PUBLIC_URL"):
        value = str(getattr(env, name, "") or "").strip().rstrip("/")
        if value and "<" not in value and ">" not in value and "://" in value:
            return value
    return ""


def _method(request) -> str:
    return (getattr(request, "method", "GET") or "GET").upper()


async def _body_json(request) -> dict:
    """Read a JSON body from any of the request shapes we may be handed.

    The SDK's `Request` (workers-py `request.py`) exposes `js_object`,
    `async text()`, `async json()`, `async bytes()` — and **no `.body`
    attribute** (only `.body` as a JS ReadableStream property on the underlying
    object). The previous version checked `hasattr(request, "json")` first and
    then fell back to `.body`, so on the real Request it returned `{}`.

    That failure was invisible and destructive: an empty dict means
    `parse_update` returns None, the handler answers `{"handled": false}` with
    HTTP 200, and Telegram — seeing success — **drops the message permanently**.
    The bot looks alive (getMe works, the webhook is registered) but never
    replies to anything.
    """
    # 1. an async json()/text() (test fakes, the SDK, our own wrappers)
    for method in ("json", "text", "bytes"):
        fn = getattr(request, method, None)
        if not callable(fn):
            continue
        try:
            data = fn()
            if hasattr(data, "__await__"):
                data = await data
        except Exception:
            continue
        parsed = _coerce_json(data)
        if parsed is not None:
            return parsed

    # 2. a plain body attribute
    raw = getattr(request, "body", None)
    parsed = _coerce_json(raw)
    if parsed is not None:
        return parsed

    # 3. the underlying JS Request
    js_object = getattr(request, "js_object", None)
    if js_object is not None:
        try:
            text = js_object.text()
            if hasattr(text, "then") or hasattr(text, "__await__"):
                text = await text
            parsed = _coerce_json(text)
            if parsed is not None:
                return parsed
        except Exception:
            pass

    return {}


def _coerce_json(value):
    """Best-effort dict out of whatever a body accessor returned."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    # An already-parsed object proxy: fall back to .to_py() if it offers one.
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        try:
            parsed = to_py()
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


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
    # Prefer the SDK's own fetch: it converts the options dict to a real JS
    # object and wraps the reply in `Response`:
    #
    #     async def fetch(resource, **other_options):
    #         resp = await _pyfetch_patched(resource, **other_options)
    #         return Response(resp.js_response)
    #
    # Note the keyword-only signature. Our callers all use `(url, init_dict)`,
    # so the adapter below normalises the shape; returning a bare `js.fetch`
    # here is not enough, because it needs a converted object rather than a
    # Python dict. See `cta.http_bridge.as_fetcher`.
    try:
        from workers import fetch as sdk_fetch  # noqa: PLC0415 — runtime only

        return sdk_fetch
    except Exception:
        pass

    try:
        import js  # noqa: PLC0415 — Cloudflare-only module
        from js import Object  # noqa: PLC0415
        from pyodide.ffi import to_js  # noqa: PLC0415

        async def _fetch(url, init=None, **options):
            merged = dict(init or {})
            merged.update(options)
            return await js.fetch(url, to_js(merged, dict_converter=Object.fromEntries))

        return _fetch
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
    """Single entrypoint for every HTTP request.

    Everything that can fail is inside the try, including resolving the fetch
    implementation. A failure outside it surfaces as Cloudflare's Error 1101 —
    an anonymous "Worker threw exception" page with no detail — which is what a
    live deployment produced when the entrypoint could not see its bindings.
    """
    try:
        fetcher = as_fetcher(fetch or _default_fetch(env))
        return await _dispatch(request, env, ctx, fetcher)
    except Exception as exc:  # never leak a stack trace to the web
        return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}, 500)


async def _dispatch(request, env, ctx, fetcher):
    """Route one request. All routes live here so one try/except covers them."""
    path = _path(request)
    method = _method(request)

    if True:  # keeps the route bodies at their original indentation
        if path == "/telegram" and method == "POST":
            expected_secret = getattr(env, "TELEGRAM_WEBHOOK_SECRET", "") or ""
            if expected_secret:
                headers = getattr(request, "headers", {}) or {}
                got = headers.get("x-telegram-bot-api-secret-token", "") if hasattr(headers, "get") else ""
                if got != expected_secret:
                    return _json({"ok": False, "error": "bad secret"}, 403)
            update = await _body_json(request)
            if not update:
                # Never answer 200 to a body we could not read: Telegram treats
                # 200 as delivered and drops the message for good. A non-2xx
                # makes it retry, so a bug costs a retry instead of a message.
                return _json(
                    {
                        "ok": False,
                        "error": "empty or unreadable update body",
                        "hint": "the webhook received a request it could not parse",
                    },
                    400,
                )
            try:
                result = await handle_telegram_async(env, fetcher, update)
            except Exception as exc:
                # Same reason: let Telegram retry and keep evidence in the reply.
                import traceback

                return _json(
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        "where": traceback.format_exc().splitlines()[-3:],
                    },
                    500,
                )
            if not result.get("handled"):
                # Handled but ignored (wrong chat, unknown update) is a real
                # answer: 200 is correct there, and the reason is reported.
                return _json(result)
            return _json(result)

        if path in ("/telegram/setup", "/setup", "/setup.json", "/telegram/setup.json"):
            # Deliberately a GET so it can be run from a phone's address bar:
            # there is no easy way to send a POST from mobile Safari/Chrome, and
            # needing one was the main reason first-run setup was painful.
            # Renders a human page rather than JSON: this is the first thing a
            # non-technical operator sees, and a wall of JSON reads as an error.
            token = str(getattr(env, "TELEGRAM_BOT_TOKEN", "") or "")
            secret = str(getattr(env, "TELEGRAM_WEBHOOK_SECRET", "") or "")
            chat_id = str(getattr(env, "TELEGRAM_CHAT_ID", "") or "")
            # Prefer the configured URL, but fall back to what the browser used.
            base = _configured_base(env) or _origin(request)

            if not token or not base:
                missing = "TELEGRAM_BOT_TOKEN" if not token else "الرابط"
                if path.endswith(".json"):
                    return _json(
                        {"ok": False, "error": f"{missing} is not set"}, 400
                    )
                return (
                    render_setup_page(
                        ok=False,
                        worker_url=base,
                        missing=missing,
                        dashboard_url=f"{base}/dash" if base else "",
                    ),
                    {"status": 400, "content-type": "text/html; charset=utf-8"},
                )

            # Remember the URL the browser used. The cron has no request, so
            # this is how webhook registration survives without anyone having
            # to set WORKER_URL by hand. Self-healing, and it cannot go stale
            # because /setup re-records it on every visit.
            try:
                await store_from_env(env).set(KEY_PUBLIC_BASE, base)
            except Exception:
                pass

            tg = Telegram(fetcher, token)
            # Which bot is this token actually for? Without asking, a token from
            # a different bot silently registers a webhook nobody is watching,
            # and "the bot does not answer" has a dozen indistinguishable causes.
            me = await tg.call("getMe", {})
            bot_info = me.get("result", {}) if me.get("ok") else {}
            hook = await tg.set_webhook(f"{base}/telegram", secret=secret)
            info = await tg.get_webhook_info()
            detail = info.get("result", {}) if info.get("ok") else {}

            store = store_from_env(env)
            try:
                owner = str(await store.get(KEY_OWNER_CHAT, "") or "")
            except Exception:
                owner = ""
            try:
                await store.set(KEY_PUBLIC_BASE, base)
            except Exception:
                pass
            try:
                trace = await store.get(KEY_TRACE, []) or []
            except Exception:
                trace = []
            try:
                all_updates = int(detail.get("pending_update_count", 0))
            except (TypeError, ValueError):
                all_updates = 0

            result = {
                "ok": bool(hook.get("ok")),
                "owner_linked": bool(owner or chat_id),
                "owner_chat_id": owner or chat_id,
                "ai_enabled": str(getattr(env, "AI_ENABLED", "true")).lower() != "false",
                "ai_binding": bool(getattr(env, "AI", None) is not None),
                "registered_url": detail.get("url", ""),
                "expected_url": f"{base}/telegram",
                "worker_url": base,
                "worker_url_source": "WORKER_URL var" if _configured_base(env) else "auto-detected",
                "dashboard": f"{base}/dash",
                "secret_registered": bool(secret),
                "chat_id_set": bool(chat_id),
                "pending_updates": all_updates,
                "trace": trace[-8:],
                "last_error": detail.get("last_error_message", ""),
                "telegram_said": hook.get("description", ""),
                "token_ok": bool(me.get("ok")),
                "token_error": "" if me.get("ok") else str(me.get("description", me.get("error", "")))[:200],
                "bot_username": bot_info.get("username", ""),
                "bot_id": bot_info.get("id", ""),
            }

            if path.endswith(".json"):
                return _json(result)
            return (
                render_setup_page(
                    ok=result["ok"],
                    worker_url=base,
                    dashboard_url=result["dashboard"],
                    chat_id_set=result["chat_id_set"],
                    registered_url=result["registered_url"],
                    expected_url=result["expected_url"],
                    telegram_said=result["telegram_said"],
                    last_error=result["last_error"],
                    bot_username=result["bot_username"],
                    token_ok=result["token_ok"],
                    token_error=result["token_error"],
                    pending_updates=result["pending_updates"],
                    trace=result["trace"],
                    owner_linked=result["owner_linked"],
                    ai_binding=result["ai_binding"],
                ),
                {"status": 200 if result["ok"] else 502,
                 "content-type": "text/html; charset=utf-8"},
            )

        if path in ("/selftest", "/self-test", "/diagnose",
                    "/selftest.json", "/self-test.json", "/diagnose.json"):
            report = await self_test_async(env, fetcher)
            if path.endswith(".json") or "format=json" in str(getattr(request, "url", "")):
                return _json(report)
            return (
                render_selftest(report, _configured_base(env) or _origin(request)),
                {"status": 200 if report.get("ok") else 503,
                 "content-type": "text/html; charset=utf-8"},
            )

        if path in ("/", "/status", "/dash"):
            store = store_from_env(env)
            try:
                html = await _dashboard_html(store, _configured_base(env) or _origin(request))
            except Exception as exc:
                # Never print zeros we did not measure. If state cannot be read,
                # say that instead — an empty dashboard is indistinguishable from
                # a broken one, and that ambiguity hid a real bug for a day.
                html = render_state_error(str(exc)[:300], base_url=_configured_base(env) or _origin(request))
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


async def scheduled(env, ctx=None, fetch=None):
    """Cron trigger: profile daily, scan every run, track outcomes hourly."""
    fetcher = as_fetcher(fetch or _default_fetch(env))

    # Keep the Telegram webhook registered. Self-healing: if it was never set
    # (nobody opened /setup) or an old URL is still registered, this fixes it
    # within a minute of the token existing. A wrong URL here is exactly the
    # "the bot does not answer" failure with no visible cause.
    try:
        base = _configured_base(env) or _origin_from_env(env)
        if not base:
            try:
                base = str(await store_from_env(env).get(KEY_PUBLIC_BASE, "") or "")
            except Exception:
                base = ""
        if base:
            await ensure_webhook_async(env, fetcher, base)
    except Exception as exc:
        print(f"[cta] webhook check failed: {type(exc).__name__}: {exc}")

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
