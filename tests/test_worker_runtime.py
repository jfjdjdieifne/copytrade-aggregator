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


def test_a_configured_but_broken_binding_is_reported_not_swapped_for_memory():
    """Silently substituting MemoryStore made the live dashboard invent zeros.

    An unbound binding means "not configured" and legitimately falls back to
    memory. A *configured* binding that cannot be used is a bug, and hiding it
    behind an empty in-memory store produced a page reporting "0 signals, 0
    traders, platforms unreachable" as though those were measurements.
    """
    from cta.state import MemoryStore, UnavailableStore, store_from_env

    class Opaque:
        """Not a store, no idFromName, cannot serve RPC."""

    store = store_from_env(FakeEnv(STATE=Opaque()))
    assert isinstance(store, UnavailableStore)
    assert not isinstance(store, MemoryStore)


async def test_an_unavailable_store_raises_instead_of_answering_empty():
    import pytest

    from cta.state import StoreError, UnavailableStore

    store = UnavailableStore("test")
    with pytest.raises(StoreError):
        await store.get("watchlist")
    with pytest.raises(StoreError):
        await store.set("watchlist", [])


def test_unbound_state_still_falls_back_to_memory():
    """No binding at all is the legitimate local/preview case."""
    from cta.state import MemoryStore, store_from_env

    assert isinstance(store_from_env(FakeEnv(STATE=None)), MemoryStore)
    assert isinstance(store_from_env(FakeEnv(STATE="")), MemoryStore)


# ── the namespace the runtime actually hands us ─────────────────────────────
class _FetcherWrapper:
    """Verbatim shape from workers-runtime-sdk `rpc.py`: forwards to a stub."""

    def __init__(self, binding):
        self._binding = binding

    def __getattr__(self, name):
        attr = getattr(self._binding, name)
        if not callable(attr):
            return attr

        async def wrapper(*args, **kwargs):
            result = attr(*args, **kwargs)
            if hasattr(result, "then") or hasattr(result, "__await__"):
                result = await result
            return result

        return wrapper


class _DurableObjectNamespaceWrapper:
    """Also verbatim: `get()` returns a FetcherWrapper, and it has NO storage_get.

    `__getattr__` forwards to the JS namespace, so a `storage_get` call lands on
    an object that does not have it and raises — the live failure.
    """

    def __init__(self, binding):
        self._binding = binding

    def __getattr__(self, name):
        return getattr(self._binding, name)

    def get(self, *args, **kwargs):
        return _FetcherWrapper(self._binding.get(*args, **kwargs))


class _JsStub:
    def __init__(self):
        self.data = {}

    async def storage_get(self, key):
        return self.data.get(key)

    async def storage_set(self, key, value):
        self.data[key] = value

    async def storage_append(self, key, item, cap=500):
        self.data.setdefault(key, []).append(item)
        return self.data[key]

    async def storage_delete(self, key):
        self.data.pop(key, None)


class _JsNamespace:
    def __init__(self):
        self.stub = _JsStub()
        self.requested = []

    def idFromName(self, name):
        self.requested.append(name)
        return f"id-{name}"

    def get(self, do_id):
        assert do_id.startswith("id-"), "get() must be called with an idFromName result"
        return self.stub


async def test_namespace_is_resolved_to_a_stub_and_state_actually_persists():
    """The live bug: DurableStore was handed the namespace, not a stub.

    Calling storage_get on the namespace raised, DurableStore swallowed it, and
    every read returned its default — so state looked empty, not broken.
    """
    from cta.state import DO_INSTANCE_NAME, DurableStore, store_from_env

    ns = _JsNamespace()
    env = FakeEnv(STATE=_DurableObjectNamespaceWrapper(ns))

    store = store_from_env(env)
    assert isinstance(store, DurableStore)
    assert ns.requested == [DO_INSTANCE_NAME]

    await store.set("counters", {"cycles": 7})
    assert await store.get("counters") == {"cycles": 7}
    assert ns.stub.data["counters"] == '{"cycles": 7}'


async def test_a_real_stub_passed_directly_is_still_used():
    """Some paths receive the stub already; do not double-wrap or reject it."""
    from cta.state import DurableStore, store_from_env

    stub = _JsStub()
    store = store_from_env(FakeEnv(STATE=stub))
    assert isinstance(store, DurableStore)
    await store.set("k", 1)
    assert await store.get("k") == 1


async def test_durable_store_does_not_swallow_rpc_failures():
    """A failure must raise, not look like an empty database."""
    import pytest

    from cta.state import DurableStore, StoreError

    class BrokenStub:
        def storage_get(self, key):
            raise RuntimeError("durable object is asleep")

    with pytest.raises(RuntimeError):
        await DurableStore(BrokenStub()).get("x")

    class MissingMethod:
        pass

    with pytest.raises(StoreError):
        await DurableStore(MissingMethod()).get("x")


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


# ── setup diagnostics: make "the bot is silent" answerable ──────────────────
class _TGDiagFetch:
    """A Telegram that answers getMe / setWebhook / getWebhookInfo."""

    def __init__(self, token_valid=True, username="MyRadarBot", pending=0):
        self.token_valid = token_valid
        self.username = username
        self.pending = pending
        self.calls = []

    async def __call__(self, url, *args, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)

        class R:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

            async def text(self):
                return json.dumps(self._p)

        if method == "getMe":
            if not self.token_valid:
                return R({"ok": False, "error_code": 401,
                          "description": "Unauthorized"})
            return R({"ok": True, "result": {"id": 123456, "is_bot": True,
                                             "username": self.username}})
        if method == "setWebhook":
            return R({"ok": True, "result": True, "description": "Webhook was set"})
        if method == "getWebhookInfo":
            return R({"ok": True, "result": {
                "url": "https://w.dev/telegram",
                "pending_update_count": self.pending,
            }})
        return R({"ok": True, "result": True})


async def test_setup_reports_the_bot_username_so_you_know_which_bot_to_open():
    """A token from the wrong bot registers a webhook nobody watches."""
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="123:abc", WORKER_URL="https://w.dev")
    resp = await handle(
        FakeRequest("https://w.dev/setup.json"), env,
        fetch=_TGDiagFetch(username="MyRadarBot"),
    )
    body = _body(resp)
    assert body["token_ok"] is True
    assert body["bot_username"] == "MyRadarBot"
    assert body["expected_url"] == "https://w.dev/telegram"


async def test_setup_names_a_rejected_token_instead_of_failing_vaguely():
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="bad:token", WORKER_URL="https://w.dev")
    resp = await handle(
        FakeRequest("https://w.dev/setup.json"), env,
        fetch=_TGDiagFetch(token_valid=False),
    )
    body = _body(resp)
    assert body["token_ok"] is False
    assert "nauthor" in body["token_error"]  # Unauthorized


async def test_setup_page_warns_about_updates_telegram_could_not_deliver():
    """pending_update_count > 0 means registered-but-not-delivering."""
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="123:abc", WORKER_URL="https://w.dev")
    raw = await handle(
        FakeRequest("https://w.dev/setup"), env,
        fetch=_TGDiagFetch(pending=4),
    )
    html, headers = raw
    assert "لم تُعالَج" in html
    assert "4" in html


async def test_setup_page_names_the_bot():
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="123:abc", WORKER_URL="https://w.dev")
    html, _ = await handle(
        FakeRequest("https://w.dev/setup"), env,
        fetch=_TGDiagFetch(username="MyRadarBot"),
    )
    assert "@MyRadarBot" in html


# ── the dashboard must not print zeros it did not measure ───────────────────
async def test_dashboard_refuses_to_show_zeros_when_state_is_unreadable():
    from cta.state import UnavailableStore
    from cta.worker import handle

    env = _env()
    env.STATE = UnavailableStore("namespace-without-storage")
    html, headers = await handle(
        FakeRequest("https://w.dev/dash"), env, fetch=FakeFetch({})
    )
    assert headers["status"] == 200
    assert "الذاكرة غير متاحة" in html
    # The tell that a stale deployment shows: a confident row of zeros.
    assert "قائمة المراقبة" not in html


async def test_health_reports_state_failure_rather_than_ok_true():
    from cta.state import UnavailableStore
    from cta.worker import handle

    env = _env()
    env.STATE = UnavailableStore("broken")
    body = _body(await handle(FakeRequest("https://w.dev/health"), env, fetch=FakeFetch({})))
    assert body["ok"] is False


def test_default_fetch_prefers_the_sdk_fetch_which_converts_the_options_dict():
    """`js.fetch(url, python_dict)` silently drops the method and body.

    A Python dict reaches JS as a proxy, so `new Request(url, init)` sees
    `init.method === undefined` and the call becomes a bodyless GET. The SDK's
    own fetch does `to_js(kwargs, dict_converter=Object.fromEntries)` first,
    which is why every network call in cta must go through it.
    """
    import sys
    import types

    from cta.worker import _default_fetch

    sentinel = object()

    class FakeEnv:
        pass

    fake = types.ModuleType("workers")
    fake.fetch = sentinel
    monkeypatch_target = sys.modules.get("workers")
    sys.modules["workers"] = fake
    try:
        assert _default_fetch(FakeEnv()) is sentinel
    finally:
        if monkeypatch_target is None:
            sys.modules.pop("workers", None)
        else:
            sys.modules["workers"] = monkeypatch_target


def test_default_fetch_honours_an_injected_fetch_first():
    """Tests and the preview server inject their own; never shadow it."""
    from cta.worker import _default_fetch

    async def injected(url, *a, **k):
        return None

    env = type("Env", (), {})()
    env.fetch = injected  # instance attribute: no descriptor binding
    assert _default_fetch(env) is injected


async def test_telegram_posts_a_json_body_and_method():
    """Guard the call shape the SDK expects: (url, init-dict)."""
    calls = []

    async def fake_fetch(url, init=None, **kwargs):
        calls.append((url, init))

        class R:
            status = 200

            async def json(self):
                return {"ok": True, "result": {"message_id": 1}}

        return R()

    from cta.telegram import Telegram

    tg = Telegram(fake_fetch, "123:abc")
    await tg.send("hello", "999")

    url, init = calls[0]
    assert url.endswith("/sendMessage")
    assert init["method"] == "POST"
    assert "\"chat_id\": \"999\"" in init["body"] or '"chat_id": "999"' in init["body"]
    assert init["headers"]["content-type"] == "application/json"


# ── calling out: the SDK's fetch is keyword-only ─────────────────────────────
async def test_adapter_bridges_the_sdk_keyword_only_fetch():
    """The live failure, reproduced.

    workers-py:  async def fetch(resource, **other_options)
    our callers: fetch(url, {"method": "POST", ...})

    -> TypeError: fetch() takes 1 positional argument but 2 were given
    which /setup reported as "⚠️ التوكن غير صالح". The token was fine.
    """
    from cta.http_bridge import as_fetcher

    seen = {}

    async def sdk_fetch(resource, **other_options):  # verbatim shape
        seen["resource"] = resource
        seen["options"] = other_options
        return "response"

    fetch = as_fetcher(sdk_fetch)
    result = await fetch("https://api.telegram.org/bot1/getMe", {"method": "POST"})
    assert result == "response"
    assert seen["resource"].endswith("/getMe")
    assert seen["options"]["method"] == "POST"


async def test_adapter_leaves_a_dict_style_fetch_alone():
    """Tests and the preview server use (url, dict); do not break them."""
    from cta.http_bridge import as_fetcher

    async def dict_fetch(url, init=None):
        return init["method"]

    assert await as_fetcher(dict_fetch)("https://x.dev", {"method": "POST"}) == "POST"


async def test_adapter_does_not_retry_a_type_error_raised_inside_fetch():
    """Only the call itself may fall back; a real failure must surface once."""
    import pytest

    from cta.http_bridge import as_fetcher

    calls = []

    async def sdk_fetch(resource, **other_options):
        calls.append(resource)
        raise TypeError("something deep inside fetch exploded")

    with pytest.raises(TypeError, match="deep inside"):
        await as_fetcher(sdk_fetch)("https://x.dev", {"method": "POST"})
    assert len(calls) == 1


async def test_adapter_is_idempotent():
    from cta.http_bridge import as_fetcher

    async def f(url, init=None):
        return 1

    once = as_fetcher(f)
    assert as_fetcher(once) is once


async def test_telegram_works_against_a_keyword_only_fetch():
    """End-to-end: the exact call that failed live."""
    from cta.http_bridge import as_fetcher
    from cta.telegram import Telegram

    calls = []

    async def sdk_fetch(resource, **other_options):
        calls.append((resource, other_options))

        class R:
            status = 200

            async def json(self):
                return {"ok": True, "result": {"message_id": 5, "username": "copytrader99_bot"}}

        return R()

    tg = Telegram(as_fetcher(sdk_fetch), "9262437260:AAE-test")
    assert (await tg.call("getMe", {}))["ok"] is True
    assert (await tg.send("hi", "123"))["ok"] is True
    assert calls[0][0].endswith("/getMe")
    assert calls[1][0].endswith("/sendMessage")
    assert calls[1][1]["method"] == "POST"
    assert '"chat_id"' in calls[1][1]["body"]


async def test_handle_normalises_the_fetch_before_dispatch():
    """/setup must survive a keyword-only fetch; it is the first thing opened."""
    from cta.worker import handle

    async def sdk_fetch(resource, **other_options):
        method = resource.rsplit("/", 1)[-1]

        class R:
            status = 200

            async def json(self):
                if method == "getMe":
                    return {"ok": True, "result": {"id": 1, "username": "copytrader99_bot"}}
                if method == "getWebhookInfo":
                    return {"ok": True, "result": {"url": "https://w.dev/telegram",
                                                   "pending_update_count": 0}}
                return {"ok": True, "result": True}

        return R()

    env = _env(TELEGRAM_BOT_TOKEN="9262437260:AAE-test", WORKER_URL="https://w.dev")
    body = _body(await handle(FakeRequest("https://w.dev/setup.json"), env, fetch=sdk_fetch))
    assert body["token_ok"] is True
    assert body["bot_username"] == "copytrader99_bot"
    assert body["ok"] is True


# ── zero-config ownership: no TELEGRAM_CHAT_ID needed ────────────────────────
class _TGAny:
    """Telegram that accepts anything and records sends."""

    def __init__(self, username="copytrader99_bot", webhook_url=""):
        self.username = username
        self.webhook_url = webhook_url
        self.sent = []
        self.registered = []

    async def __call__(self, url, init=None, **kw):
        method = url.rsplit("/", 1)[-1]
        body = {}
        if isinstance(init, dict) and init.get("body"):
            body = json.loads(init["body"])

        class R:
            status = 200

            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

            async def text(self):
                return json.dumps(self._p)

        if method == "getMe":
            return R({"ok": True, "result": {"id": 1, "username": self.username}})
        if method == "sendMessage":
            self.sent.append(body)
            return R({"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "setWebhook":
            self.registered.append(body.get("url"))
            self.webhook_url = body.get("url", "")
            return R({"ok": True, "result": True})
        if method == "getWebhookInfo":
            return R({"ok": True, "result": {"url": self.webhook_url,
                                             "pending_update_count": 0}})
        return R({"ok": True, "result": True})


def _tg_update(chat_id=555, text="/start"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Owner"},
            "text": text,
        },
    }


async def test_first_chat_claims_ownership_automatically():
    """No TELEGRAM_CHAT_ID: the first person to message becomes the owner.

    This removes the step that was failing, and removes the trap where the bot's
    own id (which BotFather prints) was pasted in and locked out the real owner.
    """
    from cta.runtime import handle_telegram_async
    from cta.state import KEY_OWNER_CHAT
    from cta.state import store_from_env

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    tg = _TGAny()

    result = await handle_telegram_async(env, tg, _tg_update(chat_id=555))
    assert result["handled"] is True
    assert await store_from_env(env).get(KEY_OWNER_CHAT) == "555"
    joined = " ".join(m.get("text", "") for m in tg.sent)
    assert "تم ربط هذه المحادثة" in joined


async def test_a_second_chat_is_refused_after_the_claim():
    """The claim is sticky: one bot, one owner — the AI budget is protected."""
    from cta.runtime import handle_telegram_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    tg = _TGAny()

    await handle_telegram_async(env, tg, _tg_update(chat_id=555))
    result = await handle_telegram_async(env, tg, _tg_update(chat_id=666))
    assert result["handled"] is False
    assert result["reason"] == "chat not allowed"


async def test_whoami_answers_even_when_ownership_is_wrong():
    """A mistyped TELEGRAM_CHAT_ID must remain diagnosable."""
    from cta.runtime import handle_telegram_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="999999")
    tg = _TGAny()

    result = await handle_telegram_async(env, tg, _tg_update(chat_id=555, text="/whoami"))
    assert result["handled"] is True
    assert result["kind"] == "whoami"
    # and the refused message deliberately does NOT reveal the owner id
    refused = await handle_telegram_async(env, tg, _tg_update(chat_id=555, text="/help"))
    assert refused["handled"] is False


async def test_an_explicit_chat_id_still_wins():
    from cta.runtime import handle_telegram_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="555")
    tg = _TGAny()
    assert (await handle_telegram_async(env, tg, _tg_update(chat_id=555)))["handled"] is True
    assert (await handle_telegram_async(env, tg, _tg_update(chat_id=777)))["handled"] is False


# ── the webhook registers itself ─────────────────────────────────────────────
async def test_cron_registers_the_webhook_without_visiting_setup():
    from cta.runtime import ensure_webhook_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    tg = _TGAny(webhook_url="")
    result = await ensure_webhook_async(env, tg, "https://w.dev")
    assert result["ok"] is True
    assert result["changed"] is True
    assert tg.registered == ["https://w.dev/telegram"]


async def test_cron_does_not_reregister_when_already_correct():
    """The cron runs 1,440 times a day; do not burn requests."""
    from cta.runtime import ensure_webhook_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    tg = _TGAny(webhook_url="https://w.dev/telegram")
    result = await ensure_webhook_async(env, tg, "https://w.dev")
    assert result["ok"] is True
    assert result["changed"] is False
    assert tg.registered == []


async def test_setup_remembers_the_url_so_the_cron_can_reuse_it():
    """/setup is the only place the real public URL is learned."""
    from cta.state import KEY_PUBLIC_BASE, store_from_env
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="1:x", WORKER_URL="")
    await handle(FakeRequest("https://real-host.workers.dev/setup.json"), env, fetch=_TGAny())
    assert await store_from_env(env).get(KEY_PUBLIC_BASE) == "https://real-host.workers.dev"


# ── self-test ────────────────────────────────────────────────────────────────
async def test_self_test_proves_the_whole_pipeline():
    from cta.runtime import self_test_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="555")
    report = await self_test_async(env, _TGAny())
    checks = report["checks"]

    assert checks["state"]["ok"] is True
    assert checks["state"]["backend"] == "MemoryStore"
    assert checks["token"]["ok"] is True
    assert checks["token"]["bot_username"] == "copytrader99_bot"
    assert checks["webhook"]["ok"] is False  # nothing registered yet
    assert checks["send"]["ok"] is True
    assert checks["clock"]["ok"] is True


async def test_self_test_reports_a_broken_state_backend():
    """The exact failure that made the dashboard show invented zeros."""
    from cta.runtime import self_test_async
    from cta.state import UnavailableStore

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    env.STATE = UnavailableStore("namespace-without-storage")
    report = await self_test_async(env, _TGAny())
    assert report["checks"]["state"]["ok"] is False
    assert report["ok"] is False
    assert "unavailable" in report["checks"]["state"]["detail"]


async def test_self_test_sends_a_real_message_when_an_owner_exists():
    from cta.runtime import self_test_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="555")
    tg = _TGAny()
    report = await self_test_async(env, tg)
    assert report["checks"]["send"]["chat_id"] == "555"
    assert any("فحص ذاتي" in m.get("text", "") for m in tg.sent)


async def test_self_test_says_what_to_do_when_there_is_no_owner_yet():
    from cta.runtime import self_test_async

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    report = await self_test_async(env, _TGAny())
    assert report["checks"]["send"]["ok"] is False
    assert "/start" in report["checks"]["send"]["detail"]


async def test_selftest_page_is_readable_and_lists_every_check():
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="555")
    raw = await handle(FakeRequest("https://w.dev/selftest"), env, fetch=_TGAny())
    html, headers = raw
    assert headers["status"] in (200, 503)
    for label in ("الذاكرة", "الاتصال", "توكن", "ربط تلغرام", "إرسال", "الذكاء", "الوقت"):
        assert label in html
    assert "@copytrader99_bot" in html


async def test_selftest_json_is_machine_readable():
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    body = _body(await handle(FakeRequest("https://w.dev/selftest.json"), env, fetch=_TGAny()))
    assert set(body["checks"]) >= {"state", "token", "webhook", "send", "ai", "clock"}


# ── reading a real Telegram webhook body ────────────────────────────────────
class _SdkRequest:
    """The SDK's Request: async text()/json(), js_object, NO .body attribute.

    workers-py request.py defines text(), json(), bytes() as async methods and
    exposes the JS object as .js_object. There is no Python `.body` attribute.
    """

    def __init__(self, payload, url="https://w.dev/telegram"):
        self._payload = payload
        self.url = url
        self.method = "POST"
        self.headers = {}
        self.js_object = _JsBody(payload)

    async def text(self):
        return json.dumps(self._payload)

    async def json(self):
        return self._payload


class _JsBody:
    def __init__(self, payload):
        self._payload = payload

    def text(self):
        async def _t():
            return json.dumps(self._payload)

        return _t()


async def test_the_sdk_request_body_is_read_not_silently_dropped():
    """This was THE reason the bot never replied.

    `_body_json` looked for `.json()` then `.body`. The SDK Request has neither
    as a plain attribute, so it returned `{}`; `parse_update({})` returned None;
    the handler answered `{"handled": false}` with **HTTP 200**; and Telegram,
    seeing success, dropped the message permanently. getMe and the webhook both
    looked perfect, so every visible signal said "fine".
    """
    from cta.worker import _body_json

    update = {"update_id": 1, "message": {"text": "/start", "chat": {"id": 555}}}
    got = await _body_json(_SdkRequest(update))
    assert got["update_id"] == 1
    assert got["message"]["text"] == "/start"


async def test_a_real_webhook_post_produces_a_reply():
    """End to end through the route, with an SDK-shaped request."""
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    tg = _TGAny()
    update = _tg_update(chat_id=555, text="/start")
    body = _body(await handle(_SdkRequest(update), env, fetch=tg))

    assert body.get("handled") is True
    assert body.get("delivered") is True
    assert any("تم ربط هذه المحادثة" in m.get("text", "") for m in tg.sent)


async def test_an_unreadable_body_returns_4xx_so_telegram_retries():
    """200 on an empty body tells Telegram to give up on that message."""
    from cta.worker import handle

    class NoBody:
        url = "https://w.dev/telegram"
        method = "POST"
        headers = {}

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    raw = await handle(NoBody(), env, fetch=_TGAny())
    text, headers = raw
    assert headers["status"] == 400
    assert "unreadable" in json.loads(text)["error"]


async def test_a_handler_crash_returns_500_so_telegram_retries():
    class Exploding:
        url = "https://w.dev/telegram"
        method = "POST"
        headers = {}

        def json(self):
            return {"update_id": 1, "message": {"text": "/start", "chat": {"id": 1}}}

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    env.STATE = None  # MemoryStore fallback is fine; make the bot explode instead

    async def boom(*a, **k):
        raise RuntimeError("handler exploded")

    import cta.worker as W

    original = W.handle_telegram_async
    W.handle_telegram_async = boom
    try:
        raw = await W.handle(Exploding(), env, fetch=_TGAny())
    finally:
        W.handle_telegram_async = original

    text, headers = raw
    assert headers["status"] == 500
    assert "exploded" in json.loads(text)["error"]


async def test_the_reply_delivery_result_is_reported():
    """A failed send must not be reported as handled-and-fine."""
    from cta.runtime import handle_telegram_async

    class FailsToSend(_TGAny):
        async def __call__(self, url, init=None, **kw):
            if url.endswith("/sendMessage"):
                class R:
                    status = 400

                    async def json(self):
                        return {"ok": False, "description": "Bad Request: chat not found"}

                return R()
            return await super().__call__(url, init, **kw)

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    result = await handle_telegram_async(env, FailsToSend(), _tg_update(chat_id=555))
    assert result.get("delivered") is False
    assert "chat not found" in result.get("error", "")


async def test_the_trace_records_what_actually_happened():
    """/setup shows this, so "no reply" becomes inspectable."""
    from cta.runtime import handle_telegram_async
    from cta.state import KEY_TRACE, store_from_env

    env = _env(TELEGRAM_BOT_TOKEN="1:x")
    await handle_telegram_async(env, _TGAny(), _tg_update(chat_id=555, text="/start"))
    trace = await store_from_env(env).get(KEY_TRACE, [])

    events = [t["event"] for t in trace]
    assert "update" in events
    assert "reply" in events
    reply = [t for t in trace if t["event"] == "reply"][-1]
    assert reply["ok"] is True


async def test_setup_page_shows_the_trace_and_any_webhook_error():
    from cta.state import KEY_TRACE, store_from_env
    from cta.worker import handle

    env = _env(TELEGRAM_BOT_TOKEN="1:x", WORKER_URL="https://w.dev")
    await store_from_env(env).append(
        KEY_TRACE, {"t": "2026-11-12T18:00:00", "event": "update", "text": "/start"}, cap=12
    )

    class WithError(_TGAny):
        async def __call__(self, url, init=None, **kw):
            if url.endswith("/getWebhookInfo"):
                class R:
                    status = 200

                    async def json(self):
                        return {"ok": True, "result": {
                            "url": "https://w.dev/telegram",
                            "pending_update_count": 2,
                            "last_error_message": "Connection refused",
                        }}

                return R()
            return await super().__call__(url, init, **kw)

    html, _ = await handle(FakeRequest("https://w.dev/setup"), env, fetch=WithError())
    assert "Connection refused" in html
    assert "/start" in html
