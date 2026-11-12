"""Venue adapters. All keyless, all free, all injectable for tests."""

from __future__ import annotations

from typing import Dict

from .base import Fetcher, HttpError, RateLimiter, http_json
from .dydx import DydxVenue
from .gmx import GmxVenue
from .hyperliquid import HyperliquidVenue

__all__ = [
    "Fetcher",
    "HttpError",
    "RateLimiter",
    "http_json",
    "HyperliquidVenue",
    "DydxVenue",
    "GmxVenue",
    "build_venues",
]


def build_venues(fetch: Fetcher, cfg) -> Dict[str, object]:
    """Instantiate every enabled venue with a shared fetch implementation."""
    venues: Dict[str, object] = {}
    if cfg.source_enabled("hyperliquid"):
        venues["hyperliquid"] = HyperliquidVenue(fetch, cfg)
    if cfg.source_enabled("dydx"):
        venues["dydx"] = DydxVenue(fetch, cfg)
    if cfg.source_enabled("gmx"):
        venues["gmx"] = GmxVenue(fetch, cfg)
    return venues
