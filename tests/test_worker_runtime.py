"""The production entry points: HTTP routing and the cycle orchestration.

These are the paths that actually run on Cloudflare, so they get exercised with
fake request objects rather than trusted by inspection.
"""

from __future__ import annotations

import json

from cta.config import Config
from cta.runtime import (
    handle_telegram_async,
    run_cycle_async,
    run_profile_async,
    track_outcomes_async,
)
from cta.state import MemoryStore
from cta.worker import handle, scheduled

from .conftest import FakeEnv, FakeFetch


class FakeRequest:
    def __init__(self, url: str = "https://radar.workers.dev/health",
                 method: str = "GET", payload=None, headers=None):
        self.url = url
        self.method = method
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def _body(result) -> dict:
    text, headers = result
    assert "application/json" in headers["content-type"]
    return json.loads(text)


def _env(**kw) -> FakeEnv:
    env = FakeEnv(**kw)
    env.STATE = MemoryStore()
    return env


# ── routing ──────────────────────────────────────────────────────────────────
async def test_health_route():
    env = _env()
    result = await handle(FakeRequest("https://x.dev/health"), env, fetch=FakeFetch({}))
    body = _body(result)
    assert body["ok"] is True
    assert body["watchlist"] == 0


async def test_root_route_returns_html():
    env = _env()
    body, headers = await handle(FakeRequest("https://x.dev/"), env, fetch=FakeFetch({}))
    assert "text/html" in headers["content-type"]
    assert "HYPOTHETICAL" in body


async def test_signals_route_lists_stored_signals():
    env = _env()
    await env.STATE.set("signals", [{"signal_id": "a"}, {"signal_id": "b"}])
    body = _body(await handle(FakeRequest("https://x.dev/signals"), env, fetch=FakeFetch({})))
    assert body["count"] == 2


async def test_unknown_route_returns_404():
    env = _env()
    result = await handle(FakeRequest("https://x.dev/nope"), env, fetch=FakeFetch({}))
    assert result[1]["status"] == 404


async def test_owner_token_is_enforced():
    env = _env(OWNER_TOKEN="secret")
    request = FakeRequest("https://x.dev/kick", method="POST", headers={})
    result = await handle(request, env, fetch=FakeFetch({}))
    assert result[1]["status"] == 401


async def test_owner_token_accepted_when_correct():
    env = _env(OWNER_TOKEN="secret")
    request = FakeRequest("https://x.dev/boot", method="POST",
                          headers={"x-owner-token": "secret"})
    result = await handle(request, env, fetch=FakeFetch({}))
    assert result[1]["status"] == 200


async def test_telegram_webhook_rejects_bad_secret():
    env = _env(TELEGRAM_WEBHOOK_SECRET="s3cret", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    request = FakeRequest("https://x.dev/telegram", method="POST",
                          payload={"message": {"chat": {"id": 1}, "text": "/help"}},
                          headers={"x-telegram-bot-api-secret-token": "wrong"})
    result = await handle(request, env, fetch=FakeFetch({}))
    assert result[1]["status"] == 403


async def test_telegram_webhook_accepts_good_secret():
    env = _env(TELEGRAM_WEBHOOK_SECRET="s3cret", TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
    request = FakeRequest("https://x.dev/telegram", method="POST",
                          payload={"message": {"chat": {"id": 1}, "text": "/help"}},
                          headers={"x-telegram-bot-api-secret-token": "s3cret"})
    result = await handle(request, env, fetch=FakeFetch({"api.telegram.org": {"ok": True}}))
    assert result[1]["status"] == 200


async def test_route_errors_do_not_leak_stack_traces():
    class ExplodingStore:
        async def get(self, *a, **k):
            raise RuntimeError("boom")

        async def set(self, *a, **k):
            raise RuntimeError("boom")

        async def append(self, *a, **k):
            raise RuntimeError("boom")

    env = _env()
    env.STATE = ExplodingStore()

    result = await handle(FakeRequest("https://x.dev/health"), env, fetch=FakeFetch({}))
    body = _body(result)
    assert result[1]["status"] == 500
    assert "boom" in body["error"]
    assert "Traceback" not in body["error"]


# ── runtime cycles ───────────────────────────────────────────────────────────
def _hl_routes(positions_payload=None, fills_payload=None, prices=None, leaderboard=None):
    return {
        "/info": lambda: _info_payload(positions_payload, fills_payload, prices),
        "leaderboard": leaderboard or {"leaderboardRows": []},
    }


def _info_payload(positions_payload, fills_payload, prices):
    return positions_payload or fills_payload or prices or {"BTC": "65000"}


async def test_run_cycle_reports_health_and_counts():
    env = _env(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    env.STATE = MemoryStore()
    fetch = FakeFetch({
        "leaderboard": {"leaderboardRows": []},
        "/info": {"BTC": "65000"},
    })
    report = await run_cycle_async(env, fetch, Config())
    assert report["ok"] is True
    assert "source_health" in report
    assert report["signals"] == 0


async def test_run_cycle_never_raises_on_venue_outage():
    env = _env()
    fetch = FakeFetch({})  # every venue 404s
    report = await run_cycle_async(env, fetch, Config())
    assert report["ok"] is True
    assert all(v is False for v in report["source_health"].values())


async def test_run_profile_with_no_leaderboard_produces_empty_watchlist():
    env = _env()
    fetch = FakeFetch({"leaderboard": {"leaderboardRows": []}})
    result = await run_profile_async(env, fetch, Config())
    assert result["ok"] is True
    assert result["watchlist"] == 0


async def test_track_outcomes_without_signals_is_safe():
    env = _env()
    fetch = FakeFetch({"/info": {"BTC": "65000"}})
    result = await track_outcomes_async(env, fetch, Config())
    assert result["ok"] is True
    assert result["evaluated"] == 0


async def test_track_outcomes_scores_a_stored_signal():
    env = _env()
    store = env.STATE
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    await store.set("signals", [{
        "signal_id": "s1", "coin": "BTC", "side": "LONG", "entry_center": 65_000,
        "created_at": (now - timedelta(hours=6)).isoformat(),
        "plan": {"entry": 65_000, "stop": 63_000, "target": 67_000},
    }])
    fetch = FakeFetch({"/info": {"BTC": "68000"}})

    result = await track_outcomes_async(env, fetch, Config())
    assert result["evaluated"] == 1
    assert result["hit_target"] == 1

    stats = await store.get("stats")
    assert stats["evaluated"] == 1


async def test_telegram_handler_runs_a_command():
    env = _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
    fetch = FakeFetch({"api.telegram.org": {"ok": True}})
    result = await handle_telegram_async(
        env, fetch, {"message": {"chat": {"id": 1}, "text": "/help"}}
    )
    assert result["handled"] is True


async def test_scheduled_runs_a_cycle():
    env = _env()
    fetch = FakeFetch({"leaderboard": {"leaderboardRows": []}, "/info": {"BTC": "65000"}})
    result = await scheduled(env, fetch=fetch)
    assert result["ok"] is True


# ── ai integration through the bot ───────────────────────────────────────────
async def test_bot_uses_ai_when_available():
    from cta.bot import Bot
    from cta.telegram import Telegram

    class FakeAI:
        async def run(self, model, payload):
            return {"response": "إجابة من النموذج"}

    env = _env(AI=FakeAI())
    store = MemoryStore()
    bot = Bot(Telegram(FakeFetch({}), "t", "1"), store, env=env, cfg=Config())
    answer = await bot._converse("ما رأيك؟", "1")
    assert answer == "إجابة من النموذج"


async def test_bot_falls_back_when_ai_disabled():
    from cta.bot import Bot
    from cta.telegram import Telegram

    class FakeAI:
        async def run(self, model, payload):  # pragma: no cover - must not be called
            raise AssertionError("AI should not be called when disabled")

    env = _env(AI=FakeAI())
    bot = Bot(Telegram(FakeFetch({}), "t", "1"), MemoryStore(), env=env,
              cfg=Config(ai_enabled=False))
    answer = await bot._converse("كيف الحال؟", "1")
    assert isinstance(answer, str) and answer


async def test_dash_route_renders_live_dashboard():
    env = _env()
    await env.STATE.set("watchlist", [
        {"wallet": "0xabc", "source": "hyperliquid", "grade": "ELITE", "score": 92,
         "n_trades": 300, "profit_factor": 2.4, "max_drawdown": 0.08},
    ])
    await env.STATE.set("source_health", {"hyperliquid": True})
    await env.STATE.set("last_cycle_at", "2026-11-12T10:00:00+00:00")

    body, headers = await handle(FakeRequest("https://x.dev/dash"), env, fetch=FakeFetch({}))

    assert "text/html" in headers["content-type"]
    assert 'dir="rtl"' in body
    assert "0xabc" in body
    assert "2026-11-12" in body
    # A reachable venue must flip the banner to live, not demo.
    assert "بيانات حيّة" in body


async def test_dash_before_first_cycle_says_nothing_happened():
    """Empty state is honest: no invented numbers, still a usable page."""
    body, _ = await handle(FakeRequest("https://x.dev/dash"), _env(), fetch=FakeFetch({}))
    assert "عرض تجريبي" in body or "لا توجد إشارات" in body
    assert "HYPOTHETICAL_" in body


async def test_dash_shows_why_clusters_were_rejected():
    env = _env()
    await env.STATE.set("last_rejections", [
        {"coin": "SOL", "side": "LONG", "reason": "انتشار واسع 2.4%"},
    ])
    body, _ = await handle(FakeRequest("https://x.dev/dash"), env, fetch=FakeFetch({}))
    assert "SOL" in body
    assert "انتشار واسع" in body


# ── entrypoint ──────────────────────────────────────────────────────────────
# entry.py is the only file Cloudflare actually invokes. It must expose the
# class-based handler (current runtime) and still delegate to cta.worker.
async def test_entry_default_class_delegates_fetch():
    import entry

    env = _env()
    called = {}

    class Recorder:
        async def fetch(self, request, env=None, ctx=None):
            called["ok"] = True
            return await handle(request, env or self.env, ctx)

    assert hasattr(entry, "Default")
    rec = Recorder()
    rec.env = env
    await rec.fetch(FakeRequest("https://x.dev/health"), env)
    assert called["ok"] is True


async def test_entry_scheduled_is_exposed():
    """The cron trigger resolves `scheduled`; a missing one silently stops scans."""
    import entry

    assert hasattr(entry, "Default")
    assert hasattr(entry.Default, "scheduled")
    assert callable(entry.on_scheduled)


async def test_entry_class_fetch_uses_self_env_when_not_passed():
    """Cloudflare calls `fetch(request)` only; env comes from the instance."""
    import entry

    env = _env()
    worker = entry.Default(None, env)
    resp = await worker.fetch(FakeRequest("https://x.dev/health"))
    # fetch returns a servable Response now, not the internal tuple.
    assert resp.status == 200
    assert "application/json" in resp.headers["content-type"]
    assert json.loads(resp.body)["ok"] is True


# ── telegram setup (first-run from a phone) ─────────────────────────────────
class _TGFetch:
    """Records Telegram API calls so setup can be asserted without a network."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[str] = []

    async def __call__(self, url, init=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)

        class R:
            # Telegram.call reads `resp.json()`; a fake must match that contract,
            # not a convenient one of its own.
            status = 200

            def __init__(self, payload):
                self._payload = payload

            async def json(self):
                return self._payload

        if method == "setWebhook":
            desc = "Webhook was set" if self.ok else "Wrong URL"
            return R({"ok": self.ok, "result": self.ok, "description": desc})
        if method == "getWebhookInfo":
            return R({"ok": True, "result": {"url": "https://w.dev/telegram",
                                             "pending_update_count": 0}})
        return R({"ok": True, "result": {}})


async def test_telegram_setup_registers_webhook_with_secret():
    """Regression: the secret was checked on the way in but never registered."""
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    env.WORKER_URL = "https://w.dev"
    env.TELEGRAM_WEBHOOK_SECRET = "s3cret"
    fetcher = _TGFetch()

    body = _body(await handle(FakeRequest("https://w.dev/setup.json"), env, fetch=fetcher))

    assert body["ok"] is True
    assert "setWebhook" in fetcher.calls
    assert body["expected_url"] == "https://w.dev/telegram"
    assert body["secret_registered"] is True


async def test_telegram_setup_refuses_without_token():
    env = _env()
    result = await handle(FakeRequest("https://w.dev/setup.json"), env, fetch=_TGFetch())
    body = _body(result)
    assert body["ok"] is False
    assert "TELEGRAM_BOT_TOKEN" in body["error"]


async def test_telegram_setup_reports_a_rejected_url():
    """A wrong WORKER_URL must be visible, not silent."""
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    env.WORKER_URL = "https://wrong.example"
    body = _body(await handle(FakeRequest("https://w.dev/setup.json"), env, fetch=_TGFetch(ok=False)))
    assert body["ok"] is False
    assert body["last_error"] == "" or isinstance(body["last_error"], str)


async def test_stranger_chats_are_refused():
    """Only the owner chat may burn the shared AI allowance."""
    from cta.bot import Bot
    from cta.state import MemoryStore

    sent: list[str] = []

    class TG:
        configured = True

        async def send(self, text, chat_id=None, **_kw):
            sent.append(text)
            return {"ok": True}

        async def typing(self, chat_id=None):
            return None

        async def answer_callback(self, *_a, **_k):
            return None

    env = _env()
    env.TELEGRAM_CHAT_ID = "111"
    bot = Bot(TG(), MemoryStore(), env=env)

    result = await bot.handle_update({"message": {"chat": {"id": 999}, "text": "/help"}})
    assert result["handled"] is False
    assert "خاص" in sent[-1]

    ok = await bot.handle_update({"message": {"chat": {"id": 111}, "text": "/help"}})
    assert ok["handled"] is True


def test_package_is_importable_from_entrypoint_directory():
    """The one deployment invariant that broke the live build.

    Cloudflare puts only the entrypoint's own directory on sys.path, and Workers
    Builds' automatic `pip install .` also uploads a copy at build/lib/. If the
    package ever moves back under src/ or python_modules/, the deployed Worker
    fails at validation with [code: 10021] ModuleNotFoundError: No module named
    'cta'. Assert the layout rather than trusting it.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    assert (root / "entry.py").is_file()
    assert (root / "cta" / "__init__.py").is_file()
    assert not (root / "python_modules" / "cta").exists()
    assert not (root / "src" / "cta").exists()


# ── deployment invariants (learned from live Cloudflare failures) ───────────
def test_durable_object_class_is_resolvable_from_entrypoint():
    """Cloudflare looks up `class_name` inside the entrypoint module.

    `wrangler.toml` says class_name = "RadarState". If entry.py does not bind
    that name, deployment fails: the Worker registers no Durable Object class
    and the STATE binding cannot be resolved. The implementation living in
    cta.durable_object is not enough — it must be importable from entry.
    """
    import entry

    assert hasattr(entry, "RadarState")
    assert entry.RadarState.__name__ == "RadarState"


def test_wrangler_declares_the_sdk_flag_and_the_do_class():
    """The two config lines that each broke a live deploy."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = (root / "wrangler.toml").read_text(encoding="utf-8")

    # Without this the runtime looks for a `workers` package that only
    # pywrangler vendors, and validation dies with ModuleNotFoundError.
    assert "disable_python_external_sdk" in cfg
    assert "python_workers" in cfg
    # Every class_name referenced must be exported by entry.py.
    import entry

    for line in cfg.splitlines():
        if line.strip().startswith("class_name"):
            cls = line.split("=", 1)[1].strip().strip('"')
            assert hasattr(entry, cls), f"{cls} is declared but not exported by entry.py"


def test_durable_object_init_matches_cloudflare_signature():
    """The runtime passes DurableObjectState only; a required extra arg is a bug."""
    import inspect

    from cta.durable_object import RadarState

    params = list(inspect.signature(RadarState.__init__).parameters)
    assert params[0] == "self"
    required = [
        p for p in list(inspect.signature(RadarState.__init__).parameters.values())[2:]
        if p.default is inspect.Parameter.empty
    ]
    assert required == [], "RadarState.__init__ must not require arguments beyond ctx"


# ── the Worker discovers its own URL ────────────────────────────────────────
async def test_setup_derives_worker_url_from_the_request():
    """No WORKER_URL var needed: the request already carries the real host.

    This is the fix for the placeholder value shipped in wrangler.toml — pointing
    Telegram at "<your-subdomain>" made the bot silently dead with no error.
    """
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    env.WORKER_URL = "https://copytrade-aggregator.<your-subdomain>.workers.dev"
    fetcher = _TGFetch()

    result = await handle(
        FakeRequest("https://copytrade-aggregator.abc.workers.dev/setup.json"),
        env,
        fetch=fetcher,
    )
    body = _body(result)

    assert body["expected_url"] == "https://copytrade-aggregator.abc.workers.dev/telegram"
    assert body["worker_url_source"] == "auto-detected"
    assert body["dashboard"] == "https://copytrade-aggregator.abc.workers.dev/dash"


async def test_setup_prefers_a_real_worker_url_var():
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    env.WORKER_URL = "https://radar.example.com"
    body = _body(await handle(FakeRequest("https://x.dev/setup.json"), env, fetch=_TGFetch()))
    assert body["worker_url"] == "https://radar.example.com"
    assert body["worker_url_source"] == "WORKER_URL var"


async def test_short_setup_alias_works():
    """Typing /setup on a phone keyboard is easier than /telegram/setup."""
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    body = _body(await handle(FakeRequest("https://w.dev/setup.json"), env, fetch=_TGFetch()))
    assert body["ok"] is True
    assert body["expected_url"] == "https://w.dev/telegram"


async def test_placeholder_url_is_never_registered():
    """A value containing < or > must be treated as unset."""
    from cta.worker import _configured_base

    class E:
        WORKER_URL = "https://copytrade-aggregator.<your-subdomain>.workers.dev"

    assert _configured_base(E()) == ""


async def test_dash_shows_its_own_url_and_setup_link():
    """The dashboard answers "what is my Worker URL?" without the dashboard UI."""
    env = _env()
    body, _ = await handle(
        FakeRequest("https://copytrade-aggregator.abc.workers.dev/dash"), env, fetch=FakeFetch({})
    )
    assert "https://copytrade-aggregator.abc.workers.dev" in body
    assert "/telegram/setup" in body


# ── /setup renders a page a human can act on ────────────────────────────────
async def test_setup_page_is_html_not_json():
    """Regression: raw JSON on /setup left the operator not knowing what to do."""
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    body, headers = await handle(FakeRequest("https://w.dev/setup"), env, fetch=_TGFetch())

    assert "text/html" in headers["content-type"]
    assert 'dir="rtl"' in body
    assert "<pre>" not in body.split("<details>")[0]  # JSON must not be the first thing
    # Each step states its own condition.
    assert "/help" in body
    assert "تم الربط" in body


async def test_setup_page_explains_a_missing_token():
    body, headers = await handle(FakeRequest("https://w.dev/setup"), _env(), fetch=_TGFetch())
    assert "TELEGRAM_BOT_TOKEN" in body
    assert headers["status"] == 400
    assert "Settings" in body  # tells the user where to fix it


async def test_setup_page_surfaces_telegram_rejection():
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    env.WORKER_URL = "https://wrong.example"
    body, _ = await handle(FakeRequest("https://w.dev/setup"), env, fetch=_TGFetch(ok=False))
    assert "لم يكتمل" in body


async def test_setup_json_route_still_available_for_scripts():
    env = _env()
    env.TELEGRAM_BOT_TOKEN = "123:abc"
    body = _body(await handle(FakeRequest("https://w.dev/setup.json"), env, fetch=_TGFetch()))
    assert body["ok"] is True
    assert "worker_url" in body


# ── entrypoint binding + crash safety (Error 1101) ──────────────────────────
def test_entrypoint_declares_init_so_the_sdk_can_bind_env():
    """Reproduces the live Error 1101.

    The SDK's `_wrap_class` patches the constructor to
    `wrapped_init(self, *args)` and passes the bindings as args[1]:
        if len(args) > 1: args[1] = _EnvWrapper(args[1])
    A subclass with no __init__ never accepts that second argument, so
    construction raises TypeError and Cloudflare reports Error 1101 with no
    detail. Declaring __init__(self, ctx, env) is what fixes it.
    """
    import inspect

    import entry

    params = list(inspect.signature(entry.Default.__init__).parameters)
    assert params == ["self", "ctx", "env"]


def test_entrypoint_binds_env_from_the_second_argument():
    import entry

    class Ctx:
        pass

    class Env:
        STATE = None
        WORKER_URL = "https://x.dev"

    inst = entry.Default(Ctx(), Env())
    assert inst.env is not None
    assert inst.ctx is not None


async def test_handle_never_raises_even_when_the_store_explodes():
    """A crash must become a JSON error, not a 1101 blank page."""

    class Exploding:
        async def get(self, *_a, **_k):
            raise RuntimeError("durable object is unhappy")

        async def set(self, *_a, **_k):
            raise RuntimeError("durable object is unhappy")

        async def append(self, *_a, **_k):
            raise RuntimeError("durable object is unhappy")

    env = _env()
    env.STATE = Exploding()
    result = await handle(FakeRequest("https://w.dev/health"), env, fetch=FakeFetch({}))
    body = _body(result)
    assert body["ok"] is False
    assert "unhappy" in body["error"]
    assert result[1]["status"] == 500


def test_store_falls_back_when_binding_is_an_opaque_object():
    """A stub-shaped object that cannot serve RPC must not take the Worker down."""
    from cta.state import MemoryStore, store_from_env

    class Opaque:
        """Answers every attribute, like a JS proxy, but defines no methods."""

    store = store_from_env(FakeEnv(STATE=Opaque()))
    assert isinstance(store, MemoryStore)


# ── http bridge: tuples must become servable Responses ──────────────────────
def test_bridge_converts_the_tuple_shape_every_route_returns():
    """cta.worker returns (body, headers). The runtime can only serve a Response.

    A Python tuple is not servable: the runtime converts results with
    python_to_rpc, which raises on tuples, and a dict of str+int is not a JS
    Response. The platform then has nothing to send and answers Error 1101.
    """
    from cta.http_bridge import to_response

    resp = to_response(('{"ok": true}', {"status": 200, "content-type": "application/json"}))
    assert resp.status == 200
    assert resp.headers["content-type"] == "application/json"
    assert json.loads(resp.body) == {"ok": True}


def test_bridge_keeps_the_status_the_route_chose():
    from cta.http_bridge import to_response

    assert to_response(("nope", {"status": 403})).status == 403
    assert to_response(("boom", {"status": 500})).status == 500


def test_bridge_accepts_status_as_a_bare_second_value():
    from cta.http_bridge import to_response

    assert to_response(("gone", 404)).status == 404


def test_bridge_defaults_a_content_type():
    """A Response without content-type makes browsers guess; be explicit."""
    from cta.http_bridge import to_response

    assert "text/plain" in to_response(("hi", {"status": 200})).headers["content-type"]


def test_bridge_clamps_statuses_the_fetch_spec_rejects():
    """Response only allows 200-599; 0 or 999 would raise on the platform."""
    from cta.http_bridge import to_response

    assert to_response(("x", {"status": 0})).status == 200
    assert to_response(("x", {"status": 999})).status == 200
    assert to_response(("x", {"status": "not-a-number"})).status == 200


def test_bridge_never_raises_on_odd_bodies():
    """The last thing between us and a blank 1101 page must not fail itself."""
    from cta.http_bridge import to_response

    assert to_response(None).status == 200
    assert "abc" in to_response(b"abc").body
    assert to_response({"a": 1}).body == '{"a": 1}'

    class Weird:
        def __repr__(self):
            return "<weird>"

    assert to_response(Weird()).status == 200


def test_bridge_passes_an_already_built_response_through():
    from cta.http_bridge import to_response

    once = to_response(("hello", {"status": 200}))
    assert to_response(once) is once


def test_crash_response_is_json_500_not_a_blank_page():
    from cta.http_bridge import crash_response

    resp = crash_response(RuntimeError("durable object is unhappy"))
    assert resp.status == 500
    payload = json.loads(resp.body)
    assert "RuntimeError" in payload["error"]
    assert payload["ok"] is False


async def test_every_route_return_value_is_servable():
    """The invariant that was missing: no route may return something unservable."""
    from cta.http_bridge import to_response

    paths = ["/", "/health", "/dash", "/setup", "/setup.json", "/signals", "/nope"]
    env = _env()

    for path in paths:
        raw = await handle(FakeRequest(f"https://w.dev{path}"), env, fetch=FakeFetch({}))
        resp = to_response(raw)
        assert isinstance(resp.status, int), path
        assert 200 <= resp.status <= 599, path
        assert resp.headers.get("content-type"), path
        assert isinstance(resp.body, str), path


async def test_entry_fetch_returns_a_servable_response_for_every_route():
    """End-to-end through the entrypoint the runtime instantiates."""
    import entry

    env = _env()
    worker = entry.Default(None, env)
    for path in ["/", "/health", "/dash"]:
        resp = await worker.fetch(FakeRequest(f"https://w.dev{path}"))
        assert resp.status == 200, path
        assert resp.body, path


async def test_legacy_on_fetch_converts_too():
    """on_fetch is a real dispatch path on older compatibility dates."""
    import entry

    resp = await entry.on_fetch(FakeRequest("https://w.dev/health"), _env())
    assert resp.status == 200
    assert json.loads(resp.body)["ok"] is True


def test_bridge_matches_the_real_sdk_response_signature(monkeypatch):
    """Pin the call against `workers.Response`, verified from the SDK source:

        def __init__(self, body=None, status=None, status_text="", headers=None,
                     web_socket=None)
        # body must be str | bytes | FormData | an accepted BufferSource
        # _to_js_headers accepts a dict
        # the fetch spec rejects a status outside 200-599

    The sandbox has no Pyodide, so no test can construct the real object; this
    stands in for it and fails on any argument the runtime would reject.
    """
    import cta.http_bridge as bridge

    built = []

    class StrictResponse:
        def __init__(self, body=None, status=None, status_text="", headers=None,
                     web_socket=None):
            if not isinstance(body, (str, bytes)) and body is not None:
                raise TypeError(f"Unsupported type in Response: {type(body).__name__}")
            if status is not None and not (200 <= int(status) <= 599):
                raise ValueError(f"Invalid status {status}")
            if headers is not None and not isinstance(headers, dict):
                raise TypeError("Received unexpected type for headers argument")
            self.body = body
            self.status = status or 200
            self.headers = dict(headers or {})
            built.append(self)

    monkeypatch.setattr(bridge, "_Response", StrictResponse)

    from cta.http_bridge import crash_response, to_response

    cases = [
        ('{"ok": true}', {"status": 200, "content-type": "application/json"}),
        ("<html></html>", {"status": 200, "content-type": "text/html; charset=utf-8"}),
        ("denied", {"status": 403, "content-type": "text/plain"}),
        ({"nested": {"a": 1}}, {"status": 200}),
        (None, {"status": 200}),
        ("x", {"status": 0}),          # must be clamped before it reaches Response
        ("x", {"status": 999}),
    ]
    for body, headers in cases:
        resp = to_response((body, headers))
        assert isinstance(resp, StrictResponse), (body, headers)
        assert 200 <= resp.status <= 599

    crashed = crash_response(RuntimeError("boom"))
    assert isinstance(crashed, StrictResponse)
    assert crashed.status == 500
    assert len(built) == len(cases) + 1
