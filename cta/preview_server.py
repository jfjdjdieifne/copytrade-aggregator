"""Local preview server — lets you open the radar in any browser, no install.

Why this exists: the radar's real job needs outbound access to venue APIs, which a
sandboxed host may block. But the *logic* — classification, convergence,
confidence, plans, the dashboard — is pure and can be demonstrated anywhere. This
server renders that logic against a deterministic scenario so the system can be
inspected from a phone.

    python -m cta.preview_server --port 8000 --host 0.0.0.0

Routes
    /            dashboard (Arabic, mobile-first)
    /api/signals JSON of the same data
    /api/health  liveness + mode
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .ai import narrate_signal
from .classifier import stats_from_fills
from .config import Config
from .convergence import detect_convergence
from .engine import build_signal
from .demo import _scenario_rejections, _synthetic_fills, NOW
from .models import Position
from .dashboard import render_dashboard

UTC = timezone.utc
CFG = Config()


def build_snapshot() -> dict[str, Any]:
    """Deterministic scenario rendered through the real production code path."""
    specs = [
        ("0x7a3f9c2b1d4e", 65_000.0, 18, "hyperliquid", 1.05, "ELITE"),
        ("0xb81e4d7a9f02", 65_040.0, 34, "dydx", 0.92, "PROVEN"),
        ("0xc92a5f1b8e73", 64_980.0, 47, "hyperliquid", 0.97, "PROVEN"),
    ]

    positions: list[Position] = []
    watchlist: list[dict] = []
    for wallet, entry, minutes, source, edge, _grade in specs:
        stats = stats_from_fills(
            wallet, _synthetic_fills(wallet, 140.0 * edge), account_value_usd=95_000
        )
        positions.append(
            Position(
                wallet=wallet,
                source=source,
                coin="BTC",
                signed_size=140_000.0 / entry,
                entry=entry,
                size_usd=140_000.0,
                leverage=3.0,
                opened_at=NOW - timedelta(minutes=minutes),
                stop=entry - 1_350.0,
                target=entry + 2_900.0,
                quality=stats.grade,
                quality_score=stats.score,
            )
        )
        watchlist.append(
            {
                "wallet": wallet,
                "source": source,
                "grade": stats.grade,
                "score": stats.score,
                "n_trades": stats.n_trades,
                "profit_factor": round(stats.profit_factor, 2),
                "max_drawdown": stats.max_drawdown,
            }
        )

    # Extra names so the roster spans every grade the classifier can emit. A
    # watchlist where everybody is ELITE demonstrates nothing; these profiles are
    # tuned to land in ELITE / PROVEN / WATCH / REJECT respectively.
    roster = [
        ("0x4d1f8a2c6b90", "gmx", 0.74, 1.0, 260, 420),
        ("0x9e2b7c4d1f38", "hyperliquid", 0.74, 1.0, 90, 200),
        ("0x2f8c5a9e3d61", "dydx", 0.74, 2.8, 70, 120),
        ("0x6b0d3e8f2a57", "hyperliquid", 0.52, 2.4, 260, 420),
    ]
    for wallet, source, win_rate, loss_mult, n, days in roster:
        st = stats_from_fills(
            wallet,
            _synthetic_fills(
                wallet, 140.0, n=n, days=days, win_rate=win_rate, loss_mult=loss_mult
            ),
            account_value_usd=60_000,
        )
        watchlist.append(
            {
                "wallet": wallet,
                "source": source,
                "grade": st.grade,
                "score": st.score,
                "n_trades": st.n_trades,
                "profit_factor": round(st.profit_factor, 2),
                "max_drawdown": st.max_drawdown,
            }
        )

    clusters = detect_convergence(positions, CFG, now=NOW)
    signals = []
    for cluster in clusters:
        sig = build_signal(cluster, CFG, market_price=65_030.0, now=NOW)
        if sig is not None:
            signals.append(sig.to_dict())

    rejected_raw = _scenario_rejections()
    rejections = []
    labels = {
        "ETH": "محفظة متحوّطة (شراء وبيع متساويان = لا رأي)",
        "SOL": "دخول قديم 6 ساعات — الاتفاق لم يعد حياً",
        "ARB": "انتشار 2.4% — ليس اتفاقاً بل صفقتان مختلفتان",
    }
    for coin, reason in labels.items():
        hit = detect_convergence([p for p in rejected_raw if p.coin == coin], CFG, now=NOW)
        if not hit:
            rejections.append({"coin": coin, "side": "LONG", "reason": reason})

    return {
        "signals": signals,
        "watchlist": watchlist,
        "rejections": rejections,
        "stats": {
            "evaluated": 0,
            "note": "التقييم يبدأ بعد 24 ساعة من أول إشارة حقيقية",
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "cta-preview/2.0"
    snapshot: dict[str, Any] = {}
    mode: str = "demo"

    def log_message(self, fmt: str, *args) -> None:  # keep the console readable
        sys.stderr.write("")

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        # Allow embedding behind the sandbox preview proxy.
        self.send_header("x-frame-options", "ALLOWALL")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path in ("/", "/index.html", "/dash"):
            snap = self.snapshot
            page = render_dashboard(
                snap["signals"],
                snap["watchlist"],
                snap["stats"],
                snap["rejections"],
                live=False,
                source_health={
                    "hyperliquid": False,
                    "dydx": False,
                    "gmx": False,
                },
                last_cycle=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            )
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/signals":
            body = json.dumps(self.snapshot, ensure_ascii=False, default=str).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return

        if path == "/api/health":
            body = json.dumps(
                {
                    "ok": True,
                    "mode": self.mode,
                    "signals": len(self.snapshot.get("signals", [])),
                    "watchlist": len(self.snapshot.get("watchlist", [])),
                    "network_isolation": True,
                    "note": "venue APIs are unreachable from this host; "
                            "run on Cloudflare or a machine with outbound access for live data",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return

        if path == "/narrative":
            lines = [narrate_signal_dict(s) for s in self.snapshot.get("signals", [])]
            self._send(
                ("\n\n" + "─" * 60 + "\n\n").join(lines).encode("utf-8"),
                "text/plain; charset=utf-8",
            )
            return

        self._send(b'{"error":"not found"}', "application/json", 404)


def narrate_signal_dict(sig: dict) -> str:
    """Reconstruct the Arabic narrative from a stored signal dict."""
    from .models import Signal, TradePlan

    plan = sig.get("plan") or {}
    model = Signal(
        coin=sig["coin"],
        side=sig["side"],
        score=sig["score"],
        level=sig["level"],
        entry_center=sig["entry_center"],
        spread_bps=sig["spread_bps"],
        n_independent=sig["n_independent"],
        sources=sig["sources"],
        plan=TradePlan(
            entry=plan.get("entry", 0),
            stop=plan.get("stop"),
            target=plan.get("target"),
            rr=plan.get("rr"),
            stop_source=plan.get("stop_source", "trader"),
            target_source=plan.get("target_source", "trader"),
        ),
        members=sig.get("members", []),
        created_at=datetime.now(UTC),
        market_price=sig.get("market_price"),
        breakdown=sig.get("breakdown", {}),
    )
    return narrate_signal(model)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Copy-Trade Aggregator local preview")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    Handler.snapshot = build_snapshot()
    Handler.mode = "demo"

    server = ThreadingHTTPServer((args.host, args.port), Handler)

    print("=" * 62)
    print("  🎯  رادار إجماع المحترفين — معاينة")
    print("=" * 62)
    print(f"  إشارات: {len(Handler.snapshot['signals'])}")
    print(f"  مراقَبون: {len(Handler.snapshot['watchlist'])}")
    print(f"  مرفوضة: {len(Handler.snapshot['rejections'])}")
    print(f"\n  http://{args.host}:{args.port}")
    print("=" * 62)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nإيقاف.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
