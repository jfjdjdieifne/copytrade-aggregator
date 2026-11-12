"""Shared fixtures: deterministic fills, positions, a fake venue and a fake env."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cta.models import Fill, Position

UTC = timezone.utc


def make_fills(
    n: int = 260,
    days: int = 420,
    edge: float = 120.0,
    seed: int = 11,
    now: datetime | None = None,
) -> list[Fill]:
    """Deterministic fills with a controlled, realistic edge."""
    now = now or datetime.now(UTC)
    state = seed
    start = int((now - timedelta(days=days)).timestamp() * 1000)
    step = days * 86_400_000 // n
    out: list[Fill] = []
    for i in range(n):
        state = (1103515245 * state + 12345) % (2**31)
        noise = (state / 2**31) - 0.5
        if noise > -0.24:
            pnl = edge * (1.6 + abs(noise) * 0.8)
        else:
            pnl = -edge * (1.0 + abs(noise) * 0.4)
        out.append(
            Fill(
                time_ms=start + i * step,
                coin="BTC",
                side="BUY" if noise > 0 else "SELL",
                price=60_000 + noise * 500,
                size=0.01,
                closed_pnl=pnl,
                fee=1.8,
            )
        )
    return out


def make_position(
    wallet: str = "0xaaa",
    coin: str = "BTC",
    side: str = "LONG",
    entry: float = 65_000.0,
    size_usd: float = 100_000.0,
    minutes_ago: float = 20.0,
    source: str = "hyperliquid",
    quality: str = "PROVEN",
    quality_score: int = 75,
    leverage: float = 3.0,
    stop: float | None = None,
    target: float | None = None,
) -> Position:
    sign = 1 if side.upper() == "LONG" else -1
    return Position(
        wallet=wallet,
        source=source,
        coin=coin,
        signed_size=sign * (size_usd / entry),
        entry=entry,
        size_usd=size_usd,
        leverage=leverage,
        opened_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        stop=stop,
        target=target,
        quality=quality,
        quality_score=quality_score,
    )


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        import json

        return json.dumps(self._payload)


class FakeFetch:
    """Records calls and replays canned responses keyed by URL substring."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, opts: dict | None = None):
        self.calls.append((url, opts or {}))
        for needle, payload in self.routes.items():
            if needle in url:
                if callable(payload):
                    payload = payload()
                if isinstance(payload, FakeResponse):
                    return payload
                return FakeResponse(200, payload)
        return FakeResponse(404, {"error": "no route", "url": url})


class FakeEnv:
    """Attribute-style env mirroring the Worker's binding object."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __getattr__(self, name):  # only called for missing attributes
        if name.startswith("__"):
            raise AttributeError(name)
        return ""


@pytest.fixture
def fills() -> list[Fill]:
    return make_fills()


@pytest.fixture
def three_traders() -> list[Position]:
    return [
        make_position("0xalice", entry=65_000, minutes_ago=20, quality_score=90, quality="ELITE"),
        make_position("0xbob", entry=65_040, minutes_ago=34, source="dydx", quality_score=78),
        make_position("0xcarol", entry=64_980, minutes_ago=48, quality_score=81),
    ]
