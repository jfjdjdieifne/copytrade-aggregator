"""Configuration — every knob in one place, validated once at cold start.

Values come from environment variables (Workers `vars` / `wrangler.toml`), so the
same code runs identically under pytest and inside the Worker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Optional

# ── grade ordering (higher is better) ────────────────────────────────────────
GRADE_ORDER = {"REJECT": 0, "WATCH": 1, "PROVEN": 2, "ELITE": 3}


def _read(env, key: str):
    """Read a setting from either a mapping or an attribute-style env object.

    Cloudflare passes `env` as a binding object that is not a dict and not always
    iterable, while local runs use os.environ / a plain dict. Supporting both here
    means every caller can keep passing whatever it has.

    Environment variables are conventionally UPPER_CASE while field names are
    snake_case, so both spellings are tried — trying only the field name silently
    ignored every override, which is exactly the kind of bug that makes a
    configured deployment behave like its defaults.
    """
    candidates = (key.upper(), key)
    try:
        for candidate in candidates:
            if candidate in env:            # dict-like (os.environ, Mapping)
                value = env[candidate]
                if value not in ("", None):
                    return value
    except TypeError:
        pass                                # not iterable -> fall through

    for candidate in candidates:
        value = getattr(env, candidate, None)
        if value not in ("", None):
            return value
    return None


def _as_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _as_list(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(raw).split(",") if p.strip())


@dataclass(slots=True)
class Config:
    """Runtime settings. Defaults are safe for a free-tier deployment."""

    # ── venues ──────────────────────────────────────────────────────────────
    hl_base_url: str = "https://api.hyperliquid.xyz"
    hl_stats_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    dydx_base_url: str = "https://indexer.dydx.trade/v4"
    gmx_base_url: str = "https://arbitrum.gmxapi.io/v1"
    enabled_sources: tuple[str, ...] = ("hyperliquid", "dydx", "gmx")
    # dYdX and GMX expose no follower leaderboard, so they need seed wallets.
    # A single high-quality seed on a *second* venue raises signal independence
    # more than ten extra wallets on Hyperliquid.
    dydx_seed_wallets: tuple[str, ...] = ()
    gmx_seed_wallets: tuple[str, ...] = ()

    # ── discovery ───────────────────────────────────────────────────────────
    min_account_value_usd: float = 10_000.0
    min_window_volume_ratio: float = 5.0   # week volume >= 5x account value
    max_month_roi_pct: float = 120.0       # above this = lottery ticket
    min_month_roi_pct: float = 3.0
    discovery_max_candidates: int = 300
    watchlist_max: int = 150
    watchlist_min_grade: str = "PROVEN"

    # ── polling / rate limits (Hyperliquid: 1200 weight per minute per IP) ──
    hl_weight_budget_per_min: int = 900     # 25% safety margin
    hl_clearinghouse_weight: int = 2
    hl_userfills_weight: int = 20
    poll_interval_sec: int = 60
    max_fills_per_profile: int = 2000

    # ── convergence gates ───────────────────────────────────────────────────
    min_independent_traders: int = 2
    min_elite_or_proven: int = 2
    convergence_window_min: int = 90
    entry_tolerance_bps: float = 50.0       # 0.50% price agreement
    max_slippage_bps: float = 50.0          # current price vs cluster entry
    max_median_leverage: float = 10.0
    hedge_ratio_threshold: float = 0.12     # |net| / gross below this = hedged
    correlation_dupe_threshold: float = 0.60

    # ── signal emission ─────────────────────────────────────────────────────
    min_signal_score: int = 65
    signal_ttl_min: int = 240
    risk_per_trade_pct: float = 1.0

    # ── ai ──────────────────────────────────────────────────────────────────
    ai_enabled: bool = True
    ai_model: str = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    ai_max_history: int = 8
    ai_daily_neuron_soft_cap: int = 8_000   # leave headroom under 10k free
    ai_timeout_ms: int = 25_000

    # ── ops ─────────────────────────────────────────────────────────────────
    state_actor_name: str = "radar-state"
    dry_run: bool = False
    log_level: str = "INFO"
    extra: dict[str, Any] = field(default_factory=dict)

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, Any]] = None) -> "Config":
        """Build from a mapping (usually the Worker `env` or os.environ).

        Unknown string keys are ignored, so adding a var never breaks a boot.
        """
        env = env if env is not None else os.environ
        cfg = cls()

        for f in fields(cls):
            if f.name == "extra":
                continue
            raw = _read(env, f.name)
            if raw is None:
                continue
            current = getattr(cfg, f.name)
            try:
                if isinstance(current, bool):
                    setattr(cfg, f.name, _as_bool(raw))
                elif isinstance(current, int):
                    setattr(cfg, f.name, int(float(raw)))
                elif isinstance(current, float):
                    setattr(cfg, f.name, float(raw))
                elif isinstance(current, tuple):
                    setattr(cfg, f.name, _as_list(raw))
                else:
                    setattr(cfg, f.name, str(raw))
            except (TypeError, ValueError):
                # A malformed override must never take the service down.
                continue

        return cfg

    # ── derived helpers ─────────────────────────────────────────────────────
    @property
    def watchlist_min_rank(self) -> int:
        return GRADE_ORDER.get(self.watchlist_min_grade.upper(), 2)

    def source_enabled(self, name: str) -> bool:
        return name in self.enabled_sources

    def max_weight_per_min(self) -> int:
        return self.hl_weight_budget_per_min
