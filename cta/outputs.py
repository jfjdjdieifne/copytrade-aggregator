"""Read-only projections of engine state for display surfaces (bot, JSON API)."""

from __future__ import annotations

from typing import Optional, Sequence

from .config import Config


async def snapshot_positions(
    venues: dict, watchlist: Sequence[dict], cfg: Optional[Config] = None
) -> list[dict]:
    """Flat, JSON-safe list of watched positions for /positions and the API."""
    from .engine import collect_positions

    cfg = cfg or Config()
    positions = await collect_positions(venues, watchlist, cfg)
    out: list[dict] = []
    for p in positions:
        out.append(
            {
                "wallet": p.wallet,
                "source": p.source,
                "coin": p.coin,
                "side": p.side,
                "entry": p.entry,
                "size_usd": round(p.size_usd, 2),
                "leverage": p.leverage,
                "quality": p.quality,
                "quality_score": p.quality_score,
            }
        )
    return out


def positions_by_coin(positions: Sequence[dict]) -> dict[str, dict[str, int]]:
    """Counts per coin: {BTC: {LONG: 3, SHORT: 1}}."""
    out: dict[str, dict[str, int]] = {}
    for p in positions:
        coin = p.get("coin", "?")
        side = p.get("side", "LONG")
        out.setdefault(coin, {"LONG": 0, "SHORT": 0})
        out[coin][side] = out[coin].get(side, 0) + 1
    return out
