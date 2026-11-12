"""Domain models.

The atomic unit is a Position, never a raw fill: one trader's 12 fills that build
a single BTC long must count as *one* opinion, not twelve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc
BPS = 10_000.0  # one basis point = 1/10_000


# ── inputs ───────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Fill:
    """A single trade print from a venue."""

    time_ms: int
    coin: str
    side: str                # BUY | SELL  (as reported; not the position intent)
    price: float
    size: float
    closed_pnl: float = 0.0
    fee: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return abs(self.price * self.size)


@dataclass(slots=True)
class TraderStats:
    """Everything we know about one wallet's track record."""

    wallet: str
    n_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    consistency: float = 0.0          # share of profitable weeks
    concentration: float = 1.0        # biggest win / total wins
    active_days: int = 0
    avg_hold_hours: float = 0.0
    max_leverage: float = 0.0
    account_value_usd: float = 0.0
    source: str = "hyperliquid"
    data_confirmed: bool = False
    score: int = 0
    grade: str = "REJECT"
    reasons: list[str] = field(default_factory=list)

    @property
    def is_proven(self) -> bool:
        return self.grade in ("ELITE", "PROVEN")


@dataclass(slots=True)
class Position:
    """A netted open position for one wallet on one coin.

    `side` is derived from the sign of the signed size, never from the raw
    BUY/SELL label of the triggering fill.
    """

    wallet: str
    source: str
    coin: str
    signed_size: float        # >0 long, <0 short (base units)
    entry: float
    size_usd: float
    leverage: float = 1.0
    opened_at: Optional[datetime] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    quality: str = "WATCH"
    quality_score: int = 0
    mark_price: Optional[float] = None

    @property
    def side(self) -> str:
        return "LONG" if self.signed_size >= 0 else "SHORT"

    @property
    def is_long(self) -> bool:
        return self.signed_size >= 0

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        if self.opened_at is None:
            return 0.0
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.opened_at).total_seconds() / 60.0)


# ── outputs ──────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Cluster:
    """A set of independent proven traders agreeing on coin + side + price + time."""

    coin: str
    side: str
    members: list[Position]
    entry_center: float
    spread_bps: float
    time_span_min: float
    sources: list[str]

    @property
    def n_members(self) -> int:
        return len(self.members)

    @property
    def n_distinct_sources(self) -> int:
        return len({s for s in self.sources})

    @property
    def median_leverage(self) -> float:
        if not self.members:
            return 0.0
        levs = sorted(m.leverage for m in self.members)
        mid = len(levs) // 2
        if len(levs) % 2:
            return levs[mid]
        return (levs[mid - 1] + levs[mid]) / 2.0


@dataclass(slots=True)
class Confidence:
    total: int
    level: str                # STRONG | GOOD | WATCH | REJECT
    breakdown: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def emitted(self) -> bool:
        return self.level in ("STRONG", "GOOD")


@dataclass(slots=True)
class TradePlan:
    entry: float
    stop: Optional[float]
    target: Optional[float]
    rr: Optional[float]
    stop_source: str = "trader"      # trader | atr | none
    target_source: str = "trader"    # trader | atr | none
    risk_pct: float = 1.0
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Signal:
    """A fully-formed, explainable recommendation."""

    coin: str
    side: str
    score: int
    level: str
    entry_center: float
    spread_bps: float
    n_independent: int
    sources: list[str]
    plan: Optional[TradePlan]
    members: list[dict]
    created_at: datetime
    signal_id: str = ""
    market_price: Optional[float] = None
    breakdown: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def slippage_bps(self) -> Optional[float]:
        if self.market_price is None or self.entry_center <= 0:
            return None
        return abs(self.market_price - self.entry_center) / self.entry_center * BPS

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "coin": self.coin,
            "side": self.side,
            "score": self.score,
            "level": self.level,
            "entry_center": round(self.entry_center, 8),
            "spread_bps": round(self.spread_bps, 2),
            "n_independent": self.n_independent,
            "sources": self.sources,
            "market_price": self.market_price,
            "slippage_bps": None if self.slippage_bps is None else round(self.slippage_bps, 2),
            "plan": None if self.plan is None else {
                "entry": self.plan.entry,
                "stop": self.plan.stop,
                "target": self.plan.target,
                "rr": self.plan.rr,
                "stop_source": self.plan.stop_source,
                "target_source": self.plan.target_source,
                "risk_pct": self.plan.risk_pct,
            },
            "members": self.members,
            "breakdown": self.breakdown,
            "reasons": self.reasons,
            "created_at": self.created_at.isoformat(),
        }
