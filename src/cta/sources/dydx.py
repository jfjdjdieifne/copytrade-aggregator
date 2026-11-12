"""dYdX v4 Indexer adapter — second independent venue.

dYdX does not publish a public follower leaderboard we can rely on, so discovery
here works differently from Hyperliquid: we accept an operator-supplied seed list
of addresses (env `DYDX_SEED_WALLETS`) *or* discover active traders from recent
market trades. Both paths end at the same place — a wallet whose fills we can
score and whose positions we can poll.

Keeping a second venue matters even at small scale: two traders on *different*
platforms agreeing is a far stronger signal than two traders on the same one.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import Fill, Position
from .base import Fetcher, RateLimiter, http_json


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coin(market: str) -> str:
    m = (market or "").upper()
    for suffix in ("-USD", "USDT", "USD"):
        if m.endswith(suffix):
            m = m[: -len(suffix)]
    return m.split("-")[0] or "UNKNOWN"


class DydxVenue:
    name = "dydx"

    def __init__(self, fetch: Fetcher, cfg, limiter: Optional[RateLimiter] = None):
        self.fetch = fetch
        self.cfg = cfg
        self.limiter = limiter or RateLimiter(300)
        self.seed_wallets = tuple(
            w.strip().lower() for w in getattr(cfg, "dydx_seed_wallets", ()) or ()
            if w.strip()
        )

    # ── discovery ───────────────────────────────────────────────────────────
    async def leaderboard(self) -> list[dict]:
        """Return candidate wallets.

        Source order: explicit seeds, then the most active addresses seen in recent
        perpetual-market trades.
        """
        rows: list[dict] = [{"wallet": w, "account_value": 0.0, "windows": {}} for w in self.seed_wallets]
        seen = {r["wallet"] for r in rows}

        markets = await self._perp_markets()
        for market in markets[:4]:
            for wallet in await self._recent_traders(market):
                if wallet not in seen:
                    seen.add(wallet)
                    rows.append({"wallet": wallet, "account_value": 0.0, "windows": {}})
        return rows

    async def _perp_markets(self) -> list[str]:
        try:
            data = await http_json(self.fetch, f"{self.cfg.dydx_base_url}/perpetualMarkets")
        except Exception:
            return []
        markets = data.get("markets", {}) if isinstance(data, dict) else {}
        ranked = sorted(
            markets.items(),
            key=lambda kv: _f(kv[1].get("volume24H") if isinstance(kv[1], dict) else 0),
            reverse=True,
        )
        return [name for name, _ in ranked]

    async def _recent_traders(self, market: str, limit: int = 100) -> list[str]:
        try:
            data = await http_json(
                self.fetch,
                f"{self.cfg.dydx_base_url}/trades/perpetualMarket/{market}",
                method="GET",
            )
        except Exception:
            return []
        trades = data.get("trades", []) if isinstance(data, dict) else []
        out: list[str] = []
        for t in trades[: limit * 4]:
            addr = (t.get("subaccount") or {}).get("address") or t.get("address")
            if addr:
                out.append(str(addr).lower())
            if len(out) >= limit:
                break
        return out

    # ── fills ───────────────────────────────────────────────────────────────
    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]:
        if not await self.limiter.acquire(1):
            return []
        data = await http_json(
            self.fetch,
            f"{self.cfg.dydx_base_url}/fills",
            method="GET",
            headers={"X-Requested-With": "cta-radar"},
        )
        rows = data.get("fills", []) if isinstance(data, dict) else []
        mine = [r for r in rows if str(r.get("subaccount") or r.get("address") or "").lower() == wallet.lower()]
        return (mine or rows)[:limit]

    @staticmethod
    def parse_fills(rows: list[dict]) -> list[Fill]:
        out: list[Fill] = []
        for r in rows:
            price = _f(r.get("price"))
            size = abs(_f(r.get("size")))
            if price <= 0:
                continue
            created = r.get("createdAt") or ""
            out.append(
                Fill(
                    time_ms=_iso_to_ms(created),
                    coin=_coin(r.get("market", "")),
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

    # ── positions ───────────────────────────────────────────────────────────
    async def positions(self, wallet: str) -> list[dict]:
        if not await self.limiter.acquire(1):
            return []
        data = await http_json(
            self.fetch,
            f"{self.cfg.dydx_base_url}/perpetualPositions",
            method="GET",
            headers={"X-Requested-With": "cta-radar"},
        )
        rows = data.get("positions", []) if isinstance(data, dict) else []
        return [r for r in rows if str(r.get("subaccount") or "").lower() == wallet.lower()] or rows

    @staticmethod
    def parse_positions(wallet: str, rows: list[dict]) -> list[Position]:
        out: list[Position] = []
        for r in rows:
            signed = _f(r.get("size"))
            if signed == 0:
                continue
            entry = _f(r.get("entryPrice"))
            if entry <= 0:
                continue
            out.append(
                Position(
                    wallet=wallet.lower(),
                    source="dydx",
                    coin=_coin(r.get("market", "")),
                    signed_size=signed,
                    entry=entry,
                    size_usd=abs(_f(r.get("value"))) or abs(signed * entry),
                    leverage=1.0,
                    mark_price=_f(r.get("unrealizedPnl")) or None,
                )
            )
        return out

    async def prices(self) -> dict[str, float]:
        try:
            data = await http_json(self.fetch, f"{self.cfg.dydx_base_url}/perpetualMarkets")
        except Exception:
            return {}
        markets = data.get("markets", {}) if isinstance(data, dict) else {}
        out: dict[str, float] = {}
        for name, m in markets.items():
            if isinstance(m, dict):
                px = _f(m.get("oraclePrice")) or _f(m.get("price"))
                if px > 0:
                    out[_coin(name)] = px
        return out

    async def health(self) -> bool:
        try:
            data = await http_json(
                self.fetch, f"{self.cfg.dydx_base_url}/perpetualMarkets", retries=0
            )
            return isinstance(data, dict) and bool(data.get("markets"))
        except Exception:
            return False


def _iso_to_ms(value: str) -> int:
    """ISO-8601 -> epoch ms without third-party parsing."""
    from datetime import datetime, timezone

    if not value:
        return 0
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0
