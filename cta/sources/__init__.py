"""Venue adapters. All keyless, all free, all injectable for tests."""

from __future__ import annotations

from typing import Dict

from .base import Fetcher, HttpError, RateLimiter, http_json
from .dydx import DydxVenue
from .gmx import GmxVenue
from .hyperliquid import HyperliquidVenue
from .okx import OKXVenue

try:  # dev/test only — the simulator lives in the package root
    from ..simulator import build_sim_venues as build_sim_venues
except Exception:  # pragma: no cover
    build_sim_venues = None  # type: ignore[assignment,misc]

__all__ = [
    "Fetcher",
    "HttpError",
    "RateLimiter",
    "http_json",
    "HyperliquidVenue",
    "OKXVenue",
    "DydxVenue",
    "GmxVenue",
    "build_venues",
    "build_sim_venues",
]


def build_venues(fetch: Fetcher, cfg) -> Dict[str, object]:
    """Instantiate every enabled venue with a shared fetch implementation."""
    venues: Dict[str, object] = {}
    if cfg.source_enabled("hyperliquid"):
        venues["hyperliquid"] = HyperliquidVenue(fetch, cfg)
    if cfg.source_enabled("okx"):
        venues["okx"] = OKXVenue(fetch, cfg)
    if cfg.source_enabled("dydx"):
        venues["dydx"] = DydxVenue(fetch, cfg)
    if cfg.source_enabled("gmx"):
        venues["gmx"] = GmxVenue(fetch, cfg)

    # A fabricated venue, used only when it is named explicitly in
    # ENABLED_SOURCES. It exists so the real loop — discovery, grading,
    # clustering, planning, delivery — can be run and watched end to end
    # without a route to any exchange.
    if build_sim_venues is not None and (
        cfg.source_enabled("sim") or cfg.source_enabled("sim-beta")
    ):
        venues.update(build_sim_venues())
    return venues
