"""GMX v2 (Arbitrum) adapter — third independent venue.

GMX's public API exposes positions and trade history for any wallet without a key.
Its numbers are 30-decimal fixed point on newer endpoints, so every parse goes
through `_scaled()` which normalises both plain floats and fixed-point strings.

A note on reliability: `/trades` intermittently answers 500 after a long timeout.
Every call therefore goes through bounded retries, and a failed call returns an
empty list rather than poisoning the run — GMX is a *bonus* venue, never a hard
dependency.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import Fill, Position
from .base import Fetcher, RateLimiter, http_json

GMX_DECIMALS = 30


def _scaled(value: Any, decimals: int = 0, default: float = 0.0) -> float:
    """Read either a plain number or a fixed-point integer/string."""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return default
    if decimals and abs(raw) > 10 ** (decimals - 1):
        return raw / (10 ** decimals)
    return raw


def _coin(symbol: str) -> str:
    s = (symbol or "").upper()
    for sep in ("/", "-", "_"):
        if sep in s:
            s = s.split(sep)[0]
    for suffix in ("USDT", "USDC", "USD"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s or "UNKNOWN"


class GmxVenue:
    name = "gmx"

    def __init__(self, fetch: Fetcher, cfg, limiter: Optional[RateLimiter] = None):
        self.fetch = fetch
        self.cfg = cfg
        self.limiter = limiter or RateLimiter(300)
        self.seed_wallets = tuple(
            w.strip().lower() for w in getattr(cfg, "gmx_seed_wallets", ()) or ()
            if w.strip()
        )

    # ── discovery ───────────────────────────────────────────────────────────
    async def leaderboard(self) -> list[dict]:
        """GMX has no public follower leaderboard; discovery is seed-based.

        We still return the same row shape so the pipeline is venue-agnostic.
        """
        return [
            {"wallet": w, "account_value": 0.0, "windows": {}} for w in self.seed_wallets
        ]

    # ── positions ───────────────────────────────────────────────────────────
    async def positions(self, wallet: str) -> list[dict]:
        if not await self.limiter.acquire(1):
            return []
        try:
            data = await http_json(
                self.fetch,
                f"{self.cfg.gmx_base_url}/positions",
                method="GET",
                headers={"X-Requested-With": "cta-radar"},
            )
        except Exception:
            return []
        rows = data.get("positions", []) if isinstance(data, dict) else (data or [])
        if not isinstance(rows, list):
            return []
        return [r for r in rows if str(r.get("account") or "").lower() == wallet.lower()] or (
            rows if wallet.lower() in str(rows)[:2000].lower() else []
        )

    @staticmethod
    def parse_positions(wallet: str, rows: list[dict]) -> list[Position]:
        out: list[Position] = []
        for r in rows:
            is_long = r.get("isLong")
            if is_long is None:
                size_usd = _scaled(r.get("sizeInUsd"), GMX_DECIMALS)
                signed = size_usd
            else:
                size_usd = _scaled(r.get("sizeInUsd"), GMX_DECIMALS) or _scaled(r.get("sizeInTokens"), GMX_DECIMALS)
                signed = size_usd if is_long else -size_usd

            entry = _scaled(r.get("entryPrice"), GMX_DECIMALS)
            if entry <= 0 or signed == 0:
                continue

            lev_raw = r.get("leverage")
            lev = _scaled(lev_raw, GMX_DECIMALS) if lev_raw is not None else 1.0
            if lev > 500:  # some endpoints send leverage * 10^4 as basis points
                lev = lev / 10_000.0
            lev = lev or 1.0

            market = (r.get("marketAddress") or "")
            symbol = r.get("market") or r.get("symbol") or market
            coin = _coin(symbol if isinstance(symbol, str) else "")

            out.append(
                Position(
                    wallet=wallet.lower(),
                    source="gmx",
                    coin=coin,
                    signed_size=signed / entry if entry > 0 else signed,
                    entry=entry,
                    size_usd=abs(size_usd),
                    leverage=min(lev, 200.0),
                )
            )
        return out

    # ── fills ───────────────────────────────────────────────────────────────
    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]:
        if not await self.limiter.acquire(1):
            return []
        try:
            data = await http_json(
                self.fetch,
                f"{self.cfg.gmx_base_url}/trades/search",
                method="GET",
                headers={"X-Requested-With": "cta-radar"},
                retries=1,
            )
        except Exception:
            return []
        rows = data.get("trades", []) if isinstance(data, dict) else (data or [])
        if not isinstance(rows, list):
            return []
        mine = [r for r in rows if str(r.get("account") or "").lower() == wallet.lower()]
        return (mine or [])[:limit]

    @staticmethod
    def parse_fills(rows: list[dict]) -> list[Fill]:
        out: list[Fill] = []
        for r in rows:
            price = _scaled(r.get("executionPrice"), GMX_DECIMALS)
            size = abs(_scaled(r.get("sizeDeltaUsd"), GMX_DECIMALS))
            if price <= 0:
                continue
            ts = r.get("timestamp")
            time_ms = int(_scaled(ts) * (1 if _scaled(ts) > 1e12 else 1000)) if ts else 0
            out.append(
                Fill(
                    time_ms=time_ms,
                    coin=_coin(r.get("marketAddress", "") or r.get("market", "")),
                    side="BUY" if r.get("isLong") in (True, "true", 1) else "SELL",
                    price=price,
                    size=size / price if price else 0.0,
                    closed_pnl=_scaled(r.get("pnlUsd"), GMX_DECIMALS),
                    fee=_scaled(r.get("feeUsd"), GMX_DECIMALS),
                    raw=r,
                )
            )
        out.sort(key=lambda f: f.time_ms)
        return out

    async def prices(self) -> dict[str, float]:
        try:
            data = await http_json(
                self.fetch, f"{self.cfg.gmx_base_url}/prices/tickers", retries=1
            )
        except Exception:
            return {}
        rows = data if isinstance(data, list) else data.get("tickers", []) if isinstance(data, dict) else []
        out: dict[str, float] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            coin = _coin(r.get("tokenSymbol") or r.get("symbol") or "")
            px = _scaled(r.get("minPrice") or r.get("price"), GMX_DECIMALS)
            if coin and px > 0:
                out.setdefault(coin, px)
        return out

    async def health(self) -> bool:
        try:
            data = await http_json(self.fetch, f"{self.cfg.gmx_base_url}/markets/info", retries=0)
            return bool(data)
        except Exception:
            return False
