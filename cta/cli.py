"""Local runner — same code as the Worker, zero Cloudflare required.

    python -m cta.cli doctor      # check config + venue reachability
    python -m cta.cli profile     # build the watchlist from real data
    python -m cta.cli scan        # one monitoring cycle
    python -m cta.cli track       # re-price stored signals
    python -m cta.cli run         # loop forever (cron replacement on a VPS)
    python -m cta.cli chat "..."  # ask the assistant a question offline
    python -m cta.cli demo        # fully offline synthetic demo, no network

The `demo` command exists so the whole pipeline can be exercised on a machine with
no outbound access (including CI), using deterministic fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config
from .runtime import (
    run_cycle_async,
    run_profile_async,
    track_outcomes_async,
)
from .state import KEY_WATCHLIST, store_from_env

UTC = timezone.utc


# ── env object with attribute access (mirrors the Worker's `env`) ────────────
class LocalEnv:
    """Attribute-style env, so runtime code is identical in both worlds."""

    def __init__(self, mapping: dict[str, str], store=None):
        self._mapping = mapping
        self.STATE = store  # may be None -> MemoryStore fallback
        self.AI = None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._mapping.get(name, "")


def load_env_file(path: str = ".env") -> dict[str, str]:
    """Minimal .env reader — no dependency for something this small."""
    data: dict[str, str] = {}
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def make_env() -> LocalEnv:
    merged = dict(os.environ)
    merged.update(load_env_file())
    env = LocalEnv(merged)
    # On a server there is no Durable Object, and `MemoryStore` would erase the
    # watchlist, the de-dup set and the paper account on every restart. SQLite
    # keeps them. A store that cannot be opened must be reported, not silently
    # replaced by one that forgets everything.
    try:
        from .sqlite_store import open_store

        env.STATE = open_store(os.environ.get("CTA_STATE_DB", "state.db"))
    except Exception as exc:  # noqa: BLE001
        print(f"[cta] durable store unavailable ({type(exc).__name__}: {exc}); "
              f"falling back to memory — this run will forget everything")
    return env


# ── real network fetch (stdlib only) ─────────────────────────────────────────
async def _stdlib_fetch(url: str, opts: Optional[dict] = None) -> "_Resp":
    """Threaded urllib fetch: stdlib only, signature compatible with Worker fetch."""
    import urllib.error
    import urllib.request

    opts = opts or {}
    method = opts.get("method", "GET")
    headers = opts.get("headers", {}) or {}
    body = opts.get("body")
    if isinstance(body, str):
        body = body.encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    def _do() -> tuple[int, str]:
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return 0, json.dumps({"error": str(exc)})

    status, text = await asyncio.get_running_loop().run_in_executor(None, _do)
    return _Resp(status, text)


class _Resp:
    """Just enough of a Response for `http_json` and `Telegram`."""

    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    def json(self) -> Any:
        return json.loads(self._text or "null")

    @property
    def text(self) -> str:
        return self._text


# ── commands ─────────────────────────────────────────────────────────────────
async def cmd_doctor(env: LocalEnv, deep: bool = False, wallets: Optional[dict] = None) -> int:
    from .bootstrap import config_summary, missing_env
    from .sources import build_venues

    cfg = Config.from_env(env)
    print("── config ──")
    print(json.dumps(config_summary(cfg), indent=2, ensure_ascii=False))

    missing = missing_env(env)
    if missing:
        print(f"\n⚠️  متغيرات ناقصة: {', '.join(missing)}")
    else:
        print("\n✅ متغيرات تلغرام موجودة")

    print("\n── المنصات ──")
    venues = build_venues(_stdlib_fetch, cfg)
    ok = 0
    for name, venue in venues.items():
        try:
            healthy = await venue.health()
        except Exception as exc:  # noqa: BLE001
            healthy = False
            print(f"  {name}: 🔴 {type(exc).__name__}")
        else:
            print(f"  {name}: {'🟢 متصل' if healthy else '🔴 لا يستجيب'}")
        ok += int(healthy)

    print(f"\n{ok}/{len(venues)} منصة تستجيب")

    if deep:
        # A liveness probe only asks "can I reach you". Every real bug in this
        # project answered "yes" to that and still returned nothing usable.
        from .doctor import probe_all, render

        print("\n── فحص عميق (بيانات حقيقية لمحفظة معروفة) ──")
        report = await probe_all(venues, wallets or {})
        print(render(report))
        return 0 if not report["broken"] else 2

    return 0 if ok else 2


async def cmd_profile(env: LocalEnv) -> int:
    result = await run_profile_async(env, _stdlib_fetch)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


async def cmd_scan(env: LocalEnv) -> int:
    result = await run_cycle_async(env, _stdlib_fetch)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


async def cmd_track(env: LocalEnv) -> int:
    result = await track_outcomes_async(env, _stdlib_fetch)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


async def _heartbeat_targets(env) -> tuple[bool, str, str]:
    """(enabled, token, chat_id) — the operator's channel, or nothing."""
    return (
        True,
        str(getattr(env, "TELEGRAM_BOT_TOKEN", "") or ""),
        str(getattr(env, "TELEGRAM_CHAT_ID", "") or ""),
    )


async def _emit(env, text: str) -> None:
    """Push one message to the operator. A heartbeat that cannot be delivered
    must not take the radar down, and must not be reported as delivered."""
    from .telegram import Telegram

    try:
        _, token, chat_id = await _heartbeat_targets(env)
        if not token or not chat_id:
            print("[heartbeat] لا يوجد TELEGRAM_BOT_TOKEN/CHAT_ID — التقرير للّوج فقط")
            print(text)
            return
        tg = Telegram(_stdlib_fetch, token, chat_id)
        sent = await tg.send(text, chat_id)
        if not sent.get("ok"):
            print(f"[heartbeat] ✗ فشل الإرسال: {sent.get('error', '')[:120]}")
            print(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[heartbeat] ✗ {type(exc).__name__}: {exc}")
        print(text)


async def cmd_run(env: LocalEnv, interval: int = 60) -> int:
    """Long-running loop for a VPS — the Cloudflare cron equivalent."""
    from .heartbeat import (
        KEY_LAST_POSITIONS,
        Pulse,
        boot_message,
        rows_to_positions,
    )

    cfg = Config.from_env(env)
    store = store_from_env(env)
    pulse = Pulse(store=store, interval_min=getattr(cfg, "heartbeat_minutes", 30.0))

    print(f"▶ بدء الحلقة كل {interval} ثانية. Ctrl+C للإيقاف.")
    if getattr(env, "SIM", "") in ("1", "true", "yes"):
        print("  (SIM=1: المصادر مُفبرَكة — نفس خط الأنابيب، لا شبكة)")

    # Announce the boot. Its job is not reassurance — it is a baseline, so the
    # operator can see the next report *move* instead of taking "شغال" on faith.
    if getattr(cfg, "heartbeat_enabled", True):
        await _emit(env, boot_message(
            health=None, watchlist=0, interval_min=pulse.interval_min,
        ))

    while True:
        started = time.monotonic()
        try:
            result = await run_cycle_async(env, _stdlib_fetch)
            print(f"[{datetime.now(UTC):%H:%M:%S}] " + json.dumps(result, ensure_ascii=False))
            await track_outcomes_async(env, _stdlib_fetch)
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"[{datetime.now(UTC):%H:%M:%S}] ✗ {type(exc).__name__}: {exc}")

        pulse.observe(result)

        if getattr(cfg, "heartbeat_enabled", True) and await pulse.due():
            try:
                rows = await store.get(KEY_LAST_POSITIONS, []) or []
                wl = await store.get(KEY_WATCHLIST, []) or []
                text = pulse.render(cfg, rows_to_positions(rows), wl)
            except Exception as exc:  # noqa: BLE001
                text = pulse.render(cfg, None, None)
                print(f"[heartbeat] ✗ تعذّر بناء التقرير: {exc}")
            await _emit(env, text)
            await pulse.mark_reported()

        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, interval - elapsed))


async def cmd_chat(env: LocalEnv, question: str) -> int:
    from .bot import Bot
    from .telegram import Telegram

    store = store_from_env(env)
    tg = Telegram(_stdlib_fetch, "offline", "offline")  # sending disabled
    bot = Bot(tg, store, env=env, cfg=Config.from_env(env))
    answer = await bot._converse(question, "cli")
    print(answer)
    return 0


async def cmd_lab(env: LocalEnv) -> int:
    """The A/B lab: how many traders does an agreement actually need?

    Prints the distribution of agreement sizes the radar has actually seen --
    the honest answer to "is three traders common or a once-a-year event?" --
    next to the paper result of each threshold, running from the same clusters
    and the same prices.
    """
    from .paper import KEY_PAPER, summarize
    from .shadow import KEY_SHADOW, empty_lab, render_lab

    store = store_from_env(env)
    lab = await store.get(KEY_SHADOW, None)
    if not isinstance(lab, dict):
        lab = empty_lab()

    pro = await store.get(KEY_PAPER, None)
    pro_summary = summarize(pro) if isinstance(pro, dict) else None

    print(render_lab(lab, pro_summary))
    return 0


async def cmd_funnel(env: LocalEnv) -> int:
    """Open the funnel: who nearly agreed, and which gate stopped them.

    This is the command to run when the radar is quiet and you cannot tell
    whether that is patience or a misconfiguration. Silence that means "the
    market is calm" and silence that means "this will never fire" look exactly
    alike until you measure the near misses.
    """
    from .config import Config
    from .engine import collect_positions
    from .funnel import diagnose
    from .sources import build_venues
    from .state import get_watchlist, store_from_env

    cfg = Config.from_env(env)
    store = store_from_env(env)
    venues = build_venues(_stdlib_fetch, cfg)

    health = {}
    for name, venue in venues.items():
        try:
            health[name] = bool(await venue.health())
        except Exception:
            health[name] = False

    watchlist = await get_watchlist(store)
    limit = min(cfg.watchlist_max, max(len(watchlist), 25))
    positions = await collect_positions(venues, watchlist, cfg, limit=limit)
    report = diagnose(positions, watchlist, cfg)

    print("── المنصات ──")
    for name, ok in health.items():
        print(f"  {name}: {'🟢' if ok else '🔴'}")

    print()
    print("── القمع ──")
    print(f"  قائمة المراقبة : {report['watchlist']}")
    print(f"  مراكز مرئية    : {report['positions_seen']}")
    print(f"  محافظ بمركز    : {report['wallets_holding']}")
    print(f"  عملات مرئية    : {', '.join(report['coins_seen']) or '—'}")

    print()
    print("── العتبات ──")
    for key, value in report["thresholds"].items():
        print(f"  {key:22} {value}")

    print()
    print("── أقرب الإجماعات ──")
    if not report["near_misses"]:
        print("  ما في ولا مجموعة متداولين على نفس العملة والاتجاه.")
    for m in report["near_misses"]:
        flag = "✅ جاهز" if m["passed"] else "❌"
        print(
            f"  {flag} {m['coin']} {m['side']}: {m['n_independent']} متداول "
            f"({m['n_sources']} مصدر) · انتشار {m['spread_bps']}bps · "
            f"تباعد {m['span_min']}د · عمر {m['age_min']}د"
        )
        for gate in m["failed_gates"]:
            print(f"        ↳ {gate}")

    print()
    print(f"📋 {report['verdict']}")
    return 0


async def cmd_simulate(cycles: int = 12, traders: int = 40, seed: int = 7,
                       quiet: bool = False) -> int:
    """Run the real pipeline against the simulator.

    Exists because the venues are unreachable from a dev machine: without it,
    the first execution of this code would happen on the operator's own server,
    at which point every bug is a surprise at 3am.
    """
    from .simulate import render_report, run_quiet_control, run_simulation

    print("▶ محاكاة: نفس خط الأنابيب الحقيقي، سوق مُفبرك")
    print(f"  {traders} متداول · {cycles} دورة · seed={seed}\n")

    if quiet:
        result = await run_quiet_control(cycles=cycles, n_traders=traders, seed=seed)
        print("\n── شاهد سلبي (بلا توافقات محقونة) ──")
        print(render_report(result))
        print("\n✅ الشاهد السلبي: لا إشارات = النظام لا يخترع إجماعاً")
        return 0

    result = await run_simulation(cycles=cycles, n_traders=traders, seed=seed)
    print(render_report(result))
    return 0


async def cmd_demo() -> int:
    """Fully offline end-to-end run with synthetic venues."""
    from .demo import run_demo

    return await run_demo()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cta", description="Copy-Trade Aggregator v2")
    sub = parser.add_subparsers(dest="cmd")

    doctor_p = sub.add_parser("doctor", help="فحص الإعداد والاتصال")
    doctor_p.add_argument("--deep", action="store_true",
                          help="يسأل كل منصة عن بيانات حقيقية لمحفظة معروفة")
    doctor_p.add_argument("--wallet", action="append", default=[],
                          metavar="المنصة=المحفظة",
                          help="مثال: --wallet hyperliquid=0xabc --wallet dydx=dydx1...")
    sub.add_parser("profile", help="بناء قائمة المراقبة")
    sub.add_parser("scan", help="دورة مراقبة واحدة")
    sub.add_parser("track", help="تقييم الإشارات المخزنة")
    sub.add_parser("demo", help="عرض تجريبي كامل بلا إنترنت")
    sim_p = sub.add_parser("simulate", help="تشغيل خط الأنابيب الحقيقي على محاكي")
    sim_p.add_argument("--cycles", type=int, default=12)
    sim_p.add_argument("--traders", type=int, default=40)
    sim_p.add_argument("--seed", type=int, default=7)
    sim_p.add_argument("--quiet", action="store_true",
                       help="شاهد سلبي: بلا توافقات محقونة، يجب ألّا تصدر إشارات")

    run_p = sub.add_parser("run", help="حلقة مستمرة")
    run_p.add_argument("--interval", type=int, default=60)

    sub.add_parser("funnel", help="ليش ما في إشارات؟ — أقرب الإجماعات والبوابة اللي وقفتها")
    sub.add_parser("lab", help="مختبر الاتفاق: كم متداول لازم؟ مقارنة ≥2/≥3/≥4/≥5 بالورق")

    chat_p = sub.add_parser("chat", help="سؤال للمساعد")
    chat_p.add_argument("question", nargs="+")

    args = parser.parse_args(argv)
    command = args.cmd or "doctor"

    if command == "simulate":
        return asyncio.run(
            cmd_simulate(
                cycles=getattr(args, "cycles", 12),
                traders=getattr(args, "traders", 40),
                seed=getattr(args, "seed", 7),
                quiet=getattr(args, "quiet", False),
            )
        )
    if command == "demo":
        return asyncio.run(cmd_demo())

    env = make_env()

    if command == "doctor":
        return asyncio.run(cmd_doctor(env))
    if command == "profile":
        return asyncio.run(cmd_profile(env))
    if command == "scan":
        return asyncio.run(cmd_scan(env))
    if command == "track":
        return asyncio.run(cmd_track(env))
    if command == "run":
        return asyncio.run(cmd_run(env, args.interval))
    if command == "funnel":
        return asyncio.run(cmd_funnel(env))
    if command == "lab":
        return asyncio.run(cmd_lab(env))
    if command == "chat":
        return asyncio.run(cmd_chat(env, " ".join(args.question)))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
