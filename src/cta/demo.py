"""Deterministic offline demo — proves the whole pipeline with zero network.

Two scenarios are injected through the same venue interface the real adapters use:

  Scenario A : three proven, independent traders open BTC long within 18 minutes,
               within 30 bps of each other  -> a signal MUST be emitted
  Scenario B : those traders hold a hedged wallet, a stale entry, and a wild
               spread                   -> nothing may be emitted

If the demo prints exactly one signal and a clean set of rejection reasons, the
brains work. No API, no keys, no internet.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .classifier import classify_trader, stats_from_fills
from .config import Config
from .engine import build_signal
from .convergence import detect_convergence
from .models import Fill, Position
from .ai import narrate_signal, explain_rejection

UTC = timezone.utc
NOW = datetime.now(UTC)


def _synthetic_fills(
    wallet: str,
    edge: float,
    n: int = 260,
    days: int = 420,
    win_rate: float = 0.74,
    loss_mult: float = 1.0,
) -> list[Fill]:
    """Reproducible pseudo-random fills with a controlled edge.

    Deterministic by construction: a linear congruential generator seeded from the
    wallet string, so every run prints identical numbers.

    `win_rate` and `loss_mult` widen the generator beyond "one very good trader":
    the defaults reproduce the original distribution exactly, while shifting them
    down lets a scenario roster span ELITE / PROVEN / WATCH / REJECT instead of
    grading every wallet identically.
    """
    seed = sum(ord(c) for c in wallet) or 7
    state = seed
    start = int((NOW - timedelta(days=days)).timestamp() * 1000)
    step = days * 86_400_000 // n

    fills: list[Fill] = []
    for i in range(n):
        state = (1103515245 * state + 12345) % (2 ** 31)
        noise = (state / 2 ** 31) - 0.5          # -0.5 .. 0.5

        # A tradable edge: ~62% wins worth ~2x the average loss. Deterministic, and
        # it produces the profit factor / drawdown a real proven trader would show.
        if noise > 0.5 - win_rate:
            pnl = edge * (1.6 + abs(noise) * 0.8)
        else:
            pnl = -edge * (1.0 + abs(noise) * 0.4) * loss_mult

        fills.append(
            Fill(
                time_ms=start + i * step,
                coin="BTC",
                side="BUY" if noise > 0 else "SELL",
                price=60_000 + noise * 400,
                size=0.01,
                closed_pnl=pnl,
                fee=1.8,
            )
        )
    return fills


def _grade(wallet: str, edge: float) -> tuple[str, int]:
    stats = stats_from_fills(wallet, _synthetic_fills(wallet, edge), account_value_usd=80_000)
    classify_trader(stats)
    return stats.grade, stats.score


class FakeVenue:
    """Implements the venue protocol from canned data."""

    def __init__(self, name: str, positions: list[dict], fills: dict[str, list[Fill]],
                 prices: dict[str, float], leaderboard: Optional[list[dict]] = None):
        self.name = name
        self._positions = positions
        self._fills = fills
        self._prices = prices
        self._leaderboard = leaderboard or []

    async def health(self) -> bool:
        return True

    async def leaderboard(self) -> list[dict]:
        return self._leaderboard

    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]:
        return [f.__dict__ if hasattr(f, "__dict__") else f for f in self._fills.get(wallet, [])]

    async def positions(self, wallet: str) -> list[dict]:
        rows = []
        for row in self._positions:
            if row["wallet"] != wallet:
                continue
            rows.append(
                {
                    "position": {
                        "coin": row["coin"],
                        "szi": row["signed_size"],
                        "entryPx": row["entry"],
                        "positionValue": row["size_usd"],
                        "leverage": {"value": row.get("leverage", 2)},
                    }
                }
            )
        return rows

    async def prices(self) -> dict[str, float]:
        return self._prices

    @staticmethod
    def parse_fills(rows: list[dict]) -> list[Fill]:
        out = []
        for r in rows:
            out.append(
                Fill(
                    time_ms=int(r["time_ms"]),
                    coin=r["coin"],
                    side=r["side"],
                    price=float(r["price"]),
                    size=float(r["size"]),
                    closed_pnl=float(r.get("closed_pnl", 0.0)),
                    fee=float(r.get("fee", 0.0)),
                )
            )
        return out

    @staticmethod
    def parse_positions(wallet: str, rows: list[dict]) -> list[Position]:
        out = []
        for item in rows:
            p = item["position"]
            out.append(
                Position(
                    wallet=wallet,
                    source="demo",
                    coin=p["coin"],
                    signed_size=p["szi"],
                    entry=p["entryPx"],
                    size_usd=p["positionValue"],
                    leverage=(p.get("leverage") or {}).get("value", 2),
                    opened_at=p.get("opened_at"),
                )
            )
        return out


def _scenario_signal() -> tuple[list[Position], dict[str, object]]:
    """Three independent traders agreeing on BTC long, tight in price and time."""
    specs = [
        ("0xalice_demo_wallet_01", 65000.0, 20, "hyperliquid", 1.05, 92),
        ("0xbob_demo_wallet_02", 65040.0, 34, "dydx", 0.85, 78),
        ("0xcarol_demo_wallet_03", 64980.0, 48, "hyperliquid", 0.9, 81),
    ]
    positions = []
    for wallet, entry, minutes, source, edge, _score in specs:
        grade, score = _grade(wallet, 140.0 * edge)
        positions.append(
            Position(
                wallet=wallet,
                source=source,
                coin="BTC",
                signed_size=120_000.0 / entry,
                entry=entry,
                size_usd=120_000.0,
                leverage=3.0,
                opened_at=NOW - timedelta(minutes=minutes),
                quality=grade,
                quality_score=score,
                stop=entry - 1_200.0,
                target=entry + 2_600.0,
            )
        )
    return positions, {}


def _scenario_rejections() -> list[Position]:
    """Cases that must be filtered: hedge, stale entry, wide spread."""
    out = [
        # Hedged: long and short of equal size on the same coin -> no opinion
        Position("0xhedged_demo_wallet_04", "hyperliquid", "ETH", 8.0, 3000.0, 90_000.0,
                 opened_at=NOW - timedelta(minutes=25), quality="PROVEN", quality_score=80),
        Position("0xhedged_demo_wallet_04", "dydx", "ETH", -8.0, 3000.0, 90_000.0,
                 opened_at=NOW - timedelta(minutes=25), quality="PROVEN", quality_score=80),
        # Stale: agreement 6 hours after the fact
        Position("0xstale_demo_wallet_05", "hyperliquid", "SOL", 55.0, 150.0, 60_000.0,
                 opened_at=NOW - timedelta(minutes=360), quality="PROVEN", quality_score=70),
        Position("0xstale_demo_wallet_06", "dydx", "SOL", 55.1, 150.0, 60_000.0,
                 opened_at=NOW - timedelta(minutes=380), quality="PROVEN", quality_score=72),
        # Wide: 2.4% apart -> not an agreement
        Position("0xwide_demo_wallet_07", "hyperliquid", "ARB", 1.2, 1.20, 40_000.0,
                 opened_at=NOW - timedelta(minutes=15), quality="ELITE", quality_score=88),
        Position("0xwide_demo_wallet_08", "hyperliquid", "ARB", 1.2, 1.229, 40_000.0,
                 opened_at=NOW - timedelta(minutes=15), quality="ELITE", quality_score=88),
    ]
    return out


async def run_demo() -> int:
    cfg = Config()
    print("=" * 72)
    print("Copy-Trade Aggregator v2 — عرض تجريبي كامل (بلا إنترنت، بلا مفاتيح)")
    print("=" * 72)

    positions, _ = _scenario_signal()
    print("\n① السيناريو الإيجابي: ثلاثة متداولين محترفين مستقلين، BTC شراء")
    print("   " + " · ".join(
        f"{p.wallet[:12]} {p.quality} {p.quality_score} @ {p.entry:g} ({p.source})"
        for p in positions
    ))

    clusters = detect_convergence(positions, cfg, now=NOW)
    print(f"\n   تقاربات اجتازت البوابة: {len(clusters)}")

    emitted = 0
    for cluster in clusters:
        signal = build_signal(cluster, cfg, market_price=65030.0, now=NOW)
        if signal is None:
            continue
        emitted += 1
        print("\n" + "─" * 72)
        print(narrate_signal(signal))
        print("─" * 72)
        print(f"\n   JSON: {json.dumps(signal.to_dict(), ensure_ascii=False, default=str)[:400]}...")

    print("\n\n② السيناريو السلبي: تحوط · قدم · انتشار واسع")
    rejected = _scenario_rejections()
    all_positions = positions + rejected
    accepted_ids = {id(p) for c in clusters for p in c.members}
    rejected_clusters = detect_convergence(all_positions, cfg, now=NOW)
    rejected_only = [c for c in rejected_clusters if not any(id(m) in accepted_ids for m in c.members)]

    print(f"   تقاربات مرفوضة: {len(rejected_only)}")
    if rejected_only:
        print("\n" + explain_rejection(rejected_only, cfg))
    else:
        print("   (جميع الحالات السلبية فُلتِرت قبل تكوين تقارب — هذا هو المطلوب)")

    # Verify the negative cases individually for a clearer report.
    print("\n   تحقق فردي:")
    for label, subset in (
        ("محفظة متحوّطة (ETH)", rejected[:2]),
        ("دخول قديم 6 ساعات (SOL)", rejected[2:4]),
        ("انتشار 2.4% (ARB)", rejected[4:6]),
    ):
        found = detect_convergence(subset, cfg, now=NOW)
        status = "✅ رُفض" if not found else "❌ مرّ (خطأ)"
        print(f"     {label}: {status}")

    print("\n" + "=" * 72)
    verdict = "✅ نجح" if emitted >= 1 and not rejected_only else "❌ يحتاج مراجعة"
    print(f"النتيجة: {verdict} — إشارات مُصدرة: {emitted}")
    print("=" * 72)
    return 0 if emitted >= 1 and not rejected_only else 1
