"""Hyperliquid adapter — the backbone venue.

Public, keyless, and unusually generous:
  * leaderboard : one GET, ~47k wallets with window PnL/ROI/volume
  * positions   : POST /info clearinghouseState   — weight 2  (the cheap poll)
  * fills       : POST /info userFills            — weight ~20 (the expensive one)

The weight asymmetry is the core engineering fact of this project: poll positions
for everyone, fetch fills only for wallets that actually moved.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import Fill, Position
from .base import Fetcher, RateLimited, RateLimiter, http_json


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coin_from_market(market: str) -> str:
    """Normalise venue symbols so the same asset compares across venues."""
    m = (market or "").upper().replace("-USD", "").replace("USDT", "").replace("USD", "")
    for sep in ("-", "/", "_"):
        if sep in m:
            m = m.split(sep)[0]
    return m or "UNKNOWN"


class HyperliquidVenue:
    name = "hyperliquid"

    def __init__(
        self,
        fetch: Fetcher,
        cfg,
        limiter: Optional[RateLimiter] = None,
    ):
        self.fetch = fetch
        self.cfg = cfg
        self.limiter = limiter or RateLimiter(cfg.hl_weight_budget_per_min)

    # ── discovery ───────────────────────────────────────────────────────────
    async def leaderboard(self) -> list[dict]:
        """Raw leaderboard rows (do not trust the headline numbers downstream)."""
        data = await http_json(self.fetch, self.cfg.hl_stats_url, method="GET")
        if isinstance(data, dict):
            return data.get("leaderboardRows", []) or []
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def parse_leaderboard_row(row: dict) -> dict:
        """Flatten one leaderboard row into the fields discovery cares about."""
        windows = {}
        for pair in row.get("windowPerformances", []) or []:
            try:
                key, stats = pair[0], pair[1]
            except (TypeError, IndexError):
                continue
            windows[key] = {
                "pnl": _f(stats.get("pnl")),
                "roi": _f(stats.get("roi")) * 100.0,   # fraction -> percent
                "vlm": _f(stats.get("vlm")),
            }
        return {
            "wallet": (row.get("ethAddress") or "").lower(),
            "account_value": _f(row.get("accountValue")),
            "windows": windows,
        }

    # ── positions: the cheap, frequent call ─────────────────────────────────
    async def positions(self, wallet: str) -> list[dict]:
        if not await self.limiter.acquire(self.cfg.hl_clearinghouse_weight):
            return []
        data = await http_json(
            self.fetch,
            f"{self.cfg.hl_base_url}/info",
            method="POST",
            payload={"type": "clearinghouseState", "user": wallet},
        )
        return list(data.get("assetPositions", []) or []) if isinstance(data, dict) else []

    @staticmethod
    def parse_positions(wallet: str, raw_positions: list[dict]) -> list[Position]:
        """Turn clearinghouseState into netted Position objects.

        Side comes from the sign of `szi`; the raw label is never trusted.
        """
        out: list[Position] = []
        for item in raw_positions:
            pos = item.get("position", item) or {}
            signed = _f(pos.get("szi"))
            if signed == 0:
                continue
            entry = _f(pos.get("entryPx"))
            if entry <= 0:
                continue
            size_usd = abs(_f(pos.get("positionValue"))) or abs(signed * entry)

            lev = 1.0
            leverage = pos.get("leverage") or {}
            if isinstance(leverage, dict):
                lev = _f(leverage.get("value"), 1.0) or 1.0
            elif leverage:
                lev = _f(leverage, 1.0) or 1.0

            out.append(
                Position(
                    wallet=wallet.lower(),
                    source="hyperliquid",
                    coin=_coin_from_market(pos.get("coin", "")),
                    signed_size=signed,
                    entry=entry,
                    size_usd=size_usd,
                    leverage=lev,
                    stop=_f(pos.get("stopLoss")) or None,
                    mark_price=_f(pos.get("markPx")) or None,
                )
            )
        return out

    # ── fills: the expensive call, only when something moved ────────────────
    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]:
        if not await self.limiter.acquire(self.cfg.hl_userfills_weight):
            # Returning [] here is what made the radar report "0 متداول" while
            # everything looked healthy: after ~45 wallets the limiter refused
            # every call and each refusal was recorded as "this wallet has no
            # trades". A refusal is now an error the caller must handle.
            raise RateLimited(self.limiter.refusal(self.cfg.hl_userfills_weight))
        data = await http_json(
            self.fetch,
            f"{self.cfg.hl_base_url}/info",
            method="POST",
            payload={"type": "userFills", "user": wallet},
        )
        rows = data if isinstance(data, list) else data.get("fills", []) if isinstance(data, dict) else []
        return rows[:limit]

    @staticmethod
    def parse_fills(rows: list[dict]) -> list[Fill]:
        out: list[Fill] = []
        for r in rows:
            price = _f(r.get("px")) or _f(r.get("price"))
            size = abs(_f(r.get("sz")) or _f(r.get("size")))
            if price <= 0:
                continue
            out.append(
                Fill(
                    time_ms=int(_f(r.get("time"))),
                    coin=_coin_from_market(r.get("coin", "")),
                    side=(r.get("side") or "BUY").upper(),
                    price=price,
                    size=size,
                    closed_pnl=_f(r.get("closedPnl")),
                    fee=_f(r.get("fee")),
                    raw=r,
                )
            )
        out.sort(key=lambda f: f.time_ms)
        return out

    # ── prices ──────────────────────────────────────────────────────────────
    async def prices(self) -> dict[str, float]:
        data = await http_json(
            self.fetch,
            f"{self.cfg.hl_base_url}/info",
            method="POST",
            payload={"type": "allMids"},
        )
        if not isinstance(data, dict):
            return {}
        return {_coin_from_market(k): _f(v) for k, v in data.items() if _f(v) > 0}

    # ── health ──────────────────────────────────────────────────────────────
    async def health(self) -> bool:
        try:
            data = await http_json(
                self.fetch,
                f"{self.cfg.hl_base_url}/info",
                method="POST",
                payload={"type": "allMids"},
                retries=0,
            )
            return isinstance(data, dict) and len(data) > 0
        except Exception:
            return False
