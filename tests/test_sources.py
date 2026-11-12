"""Venue adapters against canned payloads that mirror the real APIs."""

from __future__ import annotations

import pytest

from cta.config import Config
from cta.sources import DydxVenue, GmxVenue, HyperliquidVenue, RateLimiter, build_venues
from cta.sources.base import HttpError, http_json

from .conftest import FakeFetch, FakeResponse

CFG = Config()


# ── http plumbing ────────────────────────────────────────────────────────────
async def test_http_json_returns_payload():
    fetch = FakeFetch({"example.com": {"ok": True}})
    data = await http_json(fetch, "https://example.com/x")
    assert data == {"ok": True}


async def test_http_json_raises_on_4xx():
    fetch = FakeFetch({"example.com": FakeResponse(404, {"error": "nope"})})
    with pytest.raises(HttpError) as exc:
        await http_json(fetch, "https://example.com/x", retries=0)
    assert exc.value.status == 404
    assert exc.value.retryable is False


async def test_http_json_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(503, {"error": "busy"})
        return FakeResponse(200, {"ok": True})

    fetch = FakeFetch({"example.com": flaky})
    data = await http_json(fetch, "https://example.com/x", retries=3, backoff_s=0.0)
    assert data == {"ok": True}
    assert calls["n"] == 3


async def test_rate_limiter_refuses_over_budget():
    clock = {"t": 0.0}
    limiter = RateLimiter(10, clock=lambda: clock["t"])
    assert await limiter.acquire(6) is True
    assert await limiter.acquire(5) is False   # would exceed 10
    clock["t"] = 61.0                          # window rolls over
    assert await limiter.acquire(5) is True


# ── Hyperliquid ──────────────────────────────────────────────────────────────
def test_parse_leaderboard_row_flattens_windows():
    row = {
        "ethAddress": "0xABC",
        "accountValue": "50000",
        "windowPerformances": [
            ["day", {"pnl": "100", "roi": "0.01", "vlm": "100000"}],
            ["week", {"pnl": "800", "roi": "0.05", "vlm": "900000"}],
            ["month", {"pnl": "2500", "roi": "0.12", "vlm": "3000000"}],
        ],
    }
    parsed = HyperliquidVenue.parse_leaderboard_row(row)
    assert parsed["wallet"] == "0xabc"           # lower-cased identity
    assert parsed["account_value"] == 50_000
    assert parsed["windows"]["month"]["roi"] == pytest.approx(12.0)


def test_parse_leaderboard_row_tolerates_missing_windows():
    parsed = HyperliquidVenue.parse_leaderboard_row({"ethAddress": "0xdef"})
    assert parsed["wallet"] == "0xdef"
    assert parsed["windows"] == {}


async def test_positions_are_parsed_and_netted():
    payload = {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "1.5", "entryPx": "65000",
                          "positionValue": "97500", "leverage": {"value": 3}}},
            {"position": {"coin": "ETH", "szi": "-10", "entryPx": "3000",
                          "positionValue": "30000", "leverage": {"value": 2}}},
            {"position": {"coin": "SOL", "szi": "0", "entryPx": "100"}},
        ]
    }
    fetch = FakeFetch({"api.hyperliquid.xyz/info": payload})
    venue = HyperliquidVenue(fetch, CFG)
    raw = await venue.positions("0xABC")
    parsed = venue.parse_positions("0xABC", raw)

    assert len(parsed) == 2                       # flat position skipped
    btc = next(p for p in parsed if p.coin == "BTC")
    eth = next(p for p in parsed if p.coin == "ETH")
    assert btc.side == "LONG" and eth.side == "SHORT"   # from szi sign, not labels
    assert btc.wallet == "0xabc"
    assert btc.leverage == 3


async def test_fills_are_sorted_and_parsed():
    payload = [
        {"time": 2000, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1",
         "closedPnl": "50", "fee": "2"},
        {"time": 1000, "coin": "BTC", "side": "A", "px": "64000", "sz": "0.2",
         "closedPnl": "-30", "fee": "2"},
    ]
    fetch = FakeFetch({"api.hyperliquid.xyz/info": payload})
    venue = HyperliquidVenue(fetch, CFG)
    fills = venue.parse_fills(await venue.fills("0xabc"))
    assert [f.time_ms for f in fills] == [1000, 2000]
    assert fills[0].closed_pnl == -30


async def test_prices_normalise_symbols():
    fetch = FakeFetch({"api.hyperliquid.xyz/info": {"BTC": "65000", "ETH": "3000"}})
    venue = HyperliquidVenue(fetch, CFG)
    prices = await venue.prices()
    assert prices["BTC"] == 65_000


async def test_venue_health_false_when_unreachable():
    fetch = FakeFetch({})           # every call 404s
    venue = HyperliquidVenue(fetch, CFG)
    assert await venue.health() is False


async def test_market_symbol_normalisation():
    assert HyperliquidVenue.parse_positions("0x1", [
        {"position": {"coin": "BTC-USD", "szi": "1", "entryPx": "1"}}
    ])[0].coin == "BTC"


# ── dYdX ─────────────────────────────────────────────────────────────────────
def test_dydx_position_side_from_size():
    rows = [{"market": "BTC-USD", "size": "-2", "entryPrice": "65000", "value": "130000"}]
    parsed = DydxVenue.parse_positions("0xabc", rows)
    assert len(parsed) == 1
    assert parsed[0].side == "SHORT"
    assert parsed[0].coin == "BTC"


def test_dydx_fills_parse_iso_timestamps():
    rows = [{"market": "ETH-USD", "side": "BUY", "price": "3000", "size": "1",
             "createdAt": "2026-01-01T00:00:00Z", "closedPnl": "10", "fee": "0.5"}]
    fills = DydxVenue.parse_fills(rows)
    assert fills[0].time_ms > 0
    assert fills[0].coin == "ETH"


def test_dydx_invalid_timestamp_does_not_raise():
    rows = [{"market": "ETH-USD", "side": "BUY", "price": "3000", "size": "1",
             "createdAt": "not-a-date"}]
    assert DydxVenue.parse_fills(rows)[0].time_ms == 0


# ── GMX ──────────────────────────────────────────────────────────────────────
def test_gmx_fixed_point_prices_are_normalised():
    """GMX sends 30-decimal fixed point on several endpoints."""
    raw = 65_000 * 10 ** 30
    rows = [{"account": "0xabc", "sizeInUsd": str(100_000 * 10 ** 30),
             "entryPrice": str(raw), "isLong": True, "leverage": str(3 * 10 ** 30)}]
    parsed = GmxVenue.parse_positions("0xabc", rows)
    assert len(parsed) == 1
    assert parsed[0].entry == pytest.approx(65_000, rel=1e-6)
    assert parsed[0].side == "LONG"


def test_gmx_short_position_is_negative():
    rows = [{"sizeInUsd": "100000", "entryPrice": "3000", "isLong": False}]
    parsed = GmxVenue.parse_positions("0xabc", rows)
    assert parsed[0].side == "SHORT"


async def test_gmx_failure_returns_empty_not_exception():
    fetch = FakeFetch({})
    venue = GmxVenue(fetch, CFG)
    assert await venue.fills("0xabc") == []
    assert await venue.positions("0xabc") == []


# ── registry ─────────────────────────────────────────────────────────────────
def test_build_venues_respects_enabled_sources():
    cfg = Config(enabled_sources=("hyperliquid",))
    venues = build_venues(FakeFetch({}), cfg)
    assert set(venues) == {"hyperliquid"}


def test_all_venues_expose_the_protocol():
    venues = build_venues(FakeFetch({}), Config())
    for venue in venues.values():
        for method in ("leaderboard", "fills", "positions", "prices", "health"):
            assert callable(getattr(venue, method))
