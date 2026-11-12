"""Import-time sanity helpers.

Imported by `worker` so that a misconfigured deployment fails loudly at the first
request instead of silently producing zero signals.
"""

from __future__ import annotations

from .config import Config

REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def missing_env(env) -> list[str]:
    """Environment variables that are absent (empty string counts as absent)."""
    return [k for k in REQUIRED_ENV if not getattr(env, k, "")]


def config_summary(cfg: Config) -> dict:
    return {
        "venues": list(cfg.enabled_sources),
        "min_independent": cfg.min_independent_traders,
        "entry_tolerance_bps": cfg.entry_tolerance_bps,
        "window_min": cfg.convergence_window_min,
        "min_score": cfg.min_signal_score,
        "watchlist_max": cfg.watchlist_max,
        "dry_run": cfg.dry_run,
    }
