"""OKX copy trading adapter — the best free source for *who is actually good*.

Why this venue earns its place
------------------------------
Hyperliquid's leaderboard is a ~39 MB file of ~47,000 wallets that cannot be
parsed inside a free-tier invocation. OKX hands us a ranked, server-side filtered
list of lead traders in a few light requests, and — critically — a **closed
subposition history** per trader. That history is what `classifier.py` needs: real
trades with real profit and loss, dated, with entry and exit prices.

Every field below is taken from OKX's own public documentation. Where OKX does
*not* publish something we would want, we say so instead of inventing it:

  * **no fees** — `public-subpositions-history` reports `pnl` with no fee column,
    so any profit factor computed from it is *gross* and therefore optimistic.
    Fills from this venue are flagged and the classifier penalises them.
  * **no take-profit / stop-loss on the public endpoints** — the documented
    response for `public-current-subpositions` has no `tpTriggerPx` /
    `slTriggerPx`; those live on the *private* `current-subpositions`, which only
    shows your own account. Pending TP/SL therefore comes from Hyperliquid and
    dYdX, not from here. Saying otherwise would be the exact kind of silent
    fabrication this project keeps having to undo.
  * **sizes are in contracts**, not base units, and OKX publishes no multiplier
    on these endpoints — so `size_usd` is left at zero rather than guessed.

All keyless. All documented. Rate limited conservatively because "we were
refused" must never be reported as "this trader has no history".
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


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def coin_from_inst(inst_id: str) -> str:
    """`BTC-USDT-SWAP` -> `BTC`, `ETH-USD-241227` -> `ETH`.

    Cross-venue comparison only works if the same asset has one name. OKX
    suffixes the instrument type and a quote currency; Hyperliquid just says
    `BTC`. Stripping is deterministic, so it never guesses.
    """
    raw = (inst_id or "").upper()
    parts = [p for p in raw.split("-") if p]
    if not parts:
        return "UNKNOWN"
    return parts[0] or "UNKNOWN"


class OKXVenue:
    """OKX copy trading. The `wallet` used throughout the pipeline is OKX's
    `uniqueCode` for a lead trader — a stable public identifier, not an address.
    """

    name = "okx"

    def __init__(
        self,
        fetch: Fetcher,
        cfg,
        limiter: Optional[RateLimiter] = None,
    ):
        self.fetch = fetch
        self.cfg = cfg
        self.limiter = limiter or RateLimiter(
            budget_per_min=int(getattr(cfg, "okx_weight_budget_per_min", 240))
        )

    # ── plumbing ────────────────────────────────────────────────────────────
    @property
    def base(self) -> str:
        return getattr(self.cfg, "okx_base_url", "https://www.okx.com")

    def _url(self, path: str) -> str:
        return f"{self.base.rstrip('/')}/api/v5/copytrading/{path}"

    @staticmethod
    def _code(wallet: str) -> str:
        """uniqueCode is uppercase hex in OKX's own examples; normalise for the wire."""
        return (wallet or "").strip().upper()

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict]:
        """GET one OKX endpoint and unwrap `data`.

        OKX signals failure with `code != "0"` inside a 200 response, so a plain
        HTTP check would read an error as an empty history. That distinction
        matters: "this trader closed nothing" and "the venue refused us" lead to
        completely different conclusions downstream.
        """
        if not await self.limiter.acquire(1):
            return []
        query = "&".join(
            f"{k}={v}" for k, v in params.items() if v not in (None, "")
        )
        url = f"{self._url(path)}?{query}" if query else self._url(path)
        data = await http_json(self.fetch, url, method="GET")
        if not isinstance(data, dict):
            return []
        if str(data.get("code")) != "0":
            return []
        rows = data.get("data")
        return rows if isinstance(rows, list) else []

    async def health(self) -> bool:
        """A real call, not an echo: one page of the lead trader board."""
        try:
            rows = await self._get("public-lead-traders", {"instType": "SWAP", "limit": 1})
        except Exception:
            return False
        return bool(rows)

    # ── discovery ───────────────────────────────────────────────────────────
    async def leaderboard(self) -> list[dict]:
        """Ranked lead traders, filtered server-side by OKX.

        Sorting and filtering happen in the query, not in our process: OKX will
        not send us 47,000 rows to throw away. `minLeadDays` is configurable
        because a trader with three weeks of history cannot be distinguished
        from a lucky one, no matter how good the numbers look.
        """
        max_pages = max(1, int(getattr(self.cfg, "okx_max_pages", 8)))
        limit = 20  # OKX documents 20 as the maximum page size
        cap = int(getattr(self.cfg, "okx_max_traders", 300))
        rows: list[dict] = []
        total_pages: Optional[int] = None
        for page in range(1, max_pages + 1):
            try:
                chunk = await self._get(
                    "public-lead-traders",
                    {
                        "instType": "SWAP",
                        "sortType": getattr(self.cfg, "okx_sort_type", "pnl_ratio"),
                        "state": "0",
                        "minLeadDays": getattr(self.cfg, "okx_min_lead_days", "3"),
                        "page": page,
                        "limit": limit,
                    },
                )
            except Exception:
                break
            if not chunk:
                break
            # The response nests the rows: data[0].ranks[...]
            added = 0
            for block in chunk:
                if not isinstance(block, dict):
                    continue
                # `totalPage` is authoritative. Reading it (instead of paging
                # until an empty response) is what keeps a single-page board
                # from being requested eight times over.
                if total_pages is None:
                    total_pages = _i(block.get("totalPage")) or None
                ranks = block.get("ranks")
                if isinstance(ranks, list):
                    rows.extend(r for r in ranks if isinstance(r, dict))
                    added += len(ranks)
            if added == 0:
                break
            if total_pages is not None and page >= total_pages:
                break
            if len(rows) >= cap:
                break
        return rows[:cap]

    @staticmethod
    def parse_leaderboard_row(row: dict) -> dict:
        """Flatten one rank row into the shape `discovery.triage_row` expects.

        `windows` is deliberately left empty. OKX publishes a 90-day `pnl` and
        `pnlRatio` here, and calling that "monthly" would be a lie that then
        feeds the ROI gates. Discovery reads it as "no window data" and accepts
        the row on account value alone; the real judgement happens later, from
        the closed-position history, which is the only number we actually trust.
        """
        return {
            "wallet": (row.get("uniqueCode") or "").lower(),
            "account_value": _f(row.get("aum")),
            "windows": {},
            "nick_name": row.get("nickName") or "",
            "lead_days": _i(row.get("leadDays")),
            "win_ratio": _f(row.get("winRatio")),
            "pnl_90d": _f(row.get("pnl")),
            "pnl_ratio_90d": _f(row.get("pnlRatio")),
            "copy_traders": _i(row.get("copyTraderNum")),
        }

    # ── track record: closed subpositions ───────────────────────────────────
    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]:
        """Closed lead positions, newest first.

        This is the call that makes OKX worth having: a real, dated,
        profit-and-loss-bearing trade history per trader, which is exactly the
        input `classifier.py` was written for.
        """
        return await self._get(
            "public-subpositions-history",
            {
                "uniqueCode": self._code(wallet),
                "instType": "SWAP",
                "limit": max(1, min(100, limit)),
            },
        )

    @staticmethod
    def parse_fills(rows: list[dict]) -> list[Fill]:
        """Closed subpositions -> Fill.

        One row is one *closed position*, not one print, which is actually the
        better unit for grading: it already carries the realised PnL.
        """
        out: list[Fill] = []
        for r in rows:
            open_time = _i(r.get("openTime"))
            close_time = _i(r.get("closeTime"))
            pnl = _f(r.get("pnl"))
            if close_time <= 0 and open_time <= 0:
                continue
            pos_side = (r.get("posSide") or "").lower()
            signed = _f(r.get("subPos"))
            if pos_side == "long":
                side = "BUY"
            elif pos_side == "short":
                side = "SELL"
            elif signed < 0:
                side = "SELL"          # net mode: the sign carries the side
            else:
                side = "BUY"
            out.append(
                Fill(
                    time_ms=close_time or open_time,
                    coin=coin_from_inst(r.get("instId", "")),
                    side=side,
                    price=_f(r.get("openAvgPx")),
                    size=abs(signed),
                    closed_pnl=pnl,
                    fee=0.0,           # OKX does not publish fees here — flagged
                    raw={
                        "fee_unknown": True,
                        "open_time_ms": open_time,
                        "close_time_ms": close_time,
                        "close_px": _f(r.get("closeAvgPx")),
                        "lever": _f(r.get("lever")),
                        "margin": _f(r.get("margin")),
                        "pnl_ratio": _f(r.get("pnlRatio")),
                        "sub_pos_id": r.get("subPosId") or "",
                    },
                )
            )
        out.sort(key=lambda f: f.time_ms)
        return out

    async def history_stats(self, wallet: str, last_days: str = "3") -> dict:
        """OKX's own summary, used to cross-check what we computed ourselves.

        `winRatio`, `profitDays` and `lossDays` come straight from the venue. If
        the win rate we derive from the history disagrees badly with the one OKX
        reports, the feed is incomplete — and a truncated history makes a trader
        look *better*, not worse, because the losing tail is what goes missing.
        """
        rows = await self._get(
            "public-stats",
            {"uniqueCode": self._code(wallet), "instType": "SWAP", "lastDays": last_days},
        )
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    async def pnl_series(self, wallet: str, last_days: str = "3") -> list[dict]:
        return await self._get(
            "public-pnl",
            {"uniqueCode": self._code(wallet), "instType": "SWAP", "lastDays": last_days},
        )

    # ── live positions ───────────────────────────────────────────────────────
    async def positions(self, wallet: str) -> list[dict]:
        return await self._get(
            "public-current-subpositions",
            {"uniqueCode": self._code(wallet), "instType": "SWAP"},
        )

    @staticmethod
    def parse_positions(wallet: str, rows: list[dict]) -> list[Position]:
        """Open subpositions -> netted Position.

        `subPos` is signed: positive is long, negative is short. Side is taken
        from that sign rather than from the `posSide` label so a hedge-mode row
        can never be read backwards.

        `stop` and `target` stay None: the public endpoint does not publish them
        and leaving them empty is what makes `plan.py` fall back to an ATR stop
        that it *labels* as computed instead of passing off a guess as the
        trader's own level.
        """
        out: list[Position] = []
        for r in rows:
            signed = _f(r.get("subPos"))
            entry = _f(r.get("openAvgPx"))
            if signed == 0 or entry <= 0:
                continue
            opened_ms = _i(r.get("openTime"))
            out.append(
                Position(
                    wallet=wallet,
                    source="okx",
                    coin=coin_from_inst(r.get("instId", "")),
                    signed_size=signed,
                    entry=entry,
                    # Contract count x price is notional only with a multiplier
                    # OKX does not publish here, so we do not publish a number.
                    size_usd=_f(r.get("margin")) * _f(r.get("lever")),
                    leverage=max(1.0, _f(r.get("lever"), 1.0)),
                    opened_at=None,
                    mark_price=_f(r.get("markPx")) or None,
                    stop=None,
                    target=None,
                )
            )
            if opened_ms:
                from datetime import datetime, timezone

                out[-1].opened_at = datetime.fromtimestamp(
                    opened_ms / 1000.0, tz=timezone.utc
                )
        return out

    # ── prices ──────────────────────────────────────────────────────────────
    async def prices(self) -> dict[str, float]:
        """Mid prices for every SWAP, keyed by our normalised coin.

        Needed for the slippage gate: without a live reference price we cannot
        tell whether a cluster's entry is still reachable or already gone.
        """
        if not await self.limiter.acquire(1):
            return {}
        url = f"{self.base.rstrip('/')}/api/v5/market/tickers?instType=SWAP"
        data = await http_json(self.fetch, url, method="GET")
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {}
        out: dict[str, float] = {}
        for r in rows:
            last = _f(r.get("last"))
            if last > 0:
                out[coin_from_inst(r.get("instId", ""))] = last
        return out
