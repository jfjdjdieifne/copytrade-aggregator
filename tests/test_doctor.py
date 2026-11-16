"""The deep probe — the check that would have caught every bug we shipped.

Every one of these tests encodes a real failure this repository has had:
a venue that answered "I'm here" while returning nothing anybody could use.
"""

from __future__ import annotations

import pytest

from cta.config import Config
from cta.doctor import EMPTY, ERROR, OK, UNSUPPORTED, probe_all, probe_venue, render
from cta.models import Fill, Position

CFG = Config()


class StubVenue:
    """A venue with configurable answers, so each check can be isolated."""

    def __init__(
        self,
        name="stub",
        prices=None,
        positions=None,
        fills=None,
        orders=None,
        leaderboard=None,
        has_orders=True,
        raises=False,
    ):
        self.name = name
        self._prices = prices if prices is not None else {"BTC": 65_000.0}
        self._positions = positions if positions is not None else []
        self._fills = fills if fills is not None else []
        self._orders = orders if orders is not None else []
        self._leaderboard = leaderboard if leaderboard is not None else []
        self._raises = raises
        self._has_orders = has_orders

    async def health(self):
        return not self._raises

    async def prices(self):
        return self._prices

    async def leaderboard(self):
        return self._leaderboard

    @staticmethod
    def parse_leaderboard_row(row):
        return {"wallet": (row.get("uniqueCode") or "").lower(),
                "account_value": 10_000.0, "windows": {}}

    async def positions(self, wallet):
        return self._positions

    @staticmethod
    def parse_positions(wallet, rows):
        return [
            Position(wallet=wallet, source="stub", coin="BTC",
                     signed_size=1.0, entry=65_000.0, size_usd=65_000.0)
            for _ in rows
        ]

    async def fills(self, wallet, limit=2000):
        return self._fills

    @staticmethod
    def parse_fills(rows):
        return [
            Fill(time_ms=1, coin="BTC", side="BUY", price=65_000.0, size=1.0)
            for _ in rows
        ]

    # NOTE: no `open_orders` here. `StubVenue` models a venue that publishes no
    # resting orders — which is OKX — because `hasattr` is exactly the check the
    # doctor performs, and an attribute set to None would still answer "yes".


class OrdersStub(StubVenue):
    """The same venue, on a platform that does publish resting orders."""

    async def open_orders(self, wallet):
        return self._orders

    @staticmethod
    def parse_open_orders(wallet, rows, prices=None):
        return []


async def test_a_venue_that_answers_but_returns_nothing_is_suspect_not_healthy():
    """The dYdX bug exactly: reachable, HTTP 200, zero usable rows. Reporting
    that as healthy is what let the radar watch nothing for months."""
    report = await probe_venue(StubVenue(name="dydx"), wallet="dydx1abc")
    assert report["checks"]["health"]["status"] == OK
    assert report["checks"]["positions"]["status"] == EMPTY
    assert report["checks"]["fills"]["status"] == EMPTY
    assert report["verdict"] == EMPTY


async def test_a_venue_returning_parsed_rows_is_ok():
    report = await probe_venue(
        StubVenue(name="hl", positions=[{}], fills=[{}, {}],
                  leaderboard=[{"uniqueCode": "AAA"}]),
        wallet="0xabc",
    )
    assert report["checks"]["positions"]["status"] == OK
    assert report["checks"]["fills"]["status"] == OK
    assert report["verdict"] == OK


async def test_a_failing_call_is_reported_broken():
    class Boom(StubVenue):
        async def positions(self, wallet):
            raise RuntimeError("connection reset")

    report = await probe_venue(Boom(name="gmx"), wallet="0xabc")
    assert report["checks"]["positions"]["status"] == ERROR
    assert "connection reset" in report["checks"]["positions"]["error"]
    assert report["verdict"] == ERROR


async def test_a_venue_without_an_order_endpoint_says_so():
    """OKX publishes no resting orders. Reporting that as 'zero orders' looks
    like a bug and invites someone to 'fix' it by inventing one."""
    report = await probe_venue(StubVenue(name="okx"), wallet="ABC123")
    assert report["checks"]["orders"]["status"] == UNSUPPORTED
    assert "لا تنشر أوامر معلقة" in report["checks"]["orders"]["note"]


async def test_zero_resting_orders_is_a_success_not_a_failure():
    """Unlike fills, an empty order book is normal. Treating it as EMPTY would
    flood the report with false alarms about healthy wallets."""
    report = await probe_venue(
        OrdersStub(name="hl", orders=[], leaderboard=[{"uniqueCode": "AAA"}]),
        wallet="0xabc",
    )
    assert report["checks"]["orders"]["status"] == OK


async def test_leaderboard_rows_that_cannot_be_attributed_count_as_empty():
    """Rows with no identifier cannot be watched. Counting them as discovery
    would inflate the candidate list with people we could never poll."""
    report = await probe_venue(
        StubVenue(name="okx", leaderboard=[{"nickName": "anon"}, {"nickName": "anon2"}]),
        wallet="ABC123",
    )
    assert report["checks"]["leaderboard"]["status"] == EMPTY
    assert report["checks"]["leaderboard"]["count"] == 2


async def test_probe_all_groups_venues_by_verdict():
    venues = {
        "good": StubVenue(name="good", positions=[{}], fills=[{}],
                          leaderboard=[{"uniqueCode": "ABC"}]),
        "quiet": StubVenue(name="quiet"),
        "dead": StubVenue(name="dead", raises=False),
    }

    class Dead(StubVenue):
        async def prices(self):
            raise RuntimeError("dns")

    venues["dead"] = Dead(name="dead")
    report = await probe_all(venues, {n: "0xabc" for n in venues})
    assert "good" in report["healthy"]
    assert "quiet" in report["suspect"]
    assert "dead" in report["broken"]


async def test_render_makes_the_problem_impossible_to_miss():
    report = await probe_all({"quiet": StubVenue(name="quiet")}, {"quiet": "0xabc"})
    text = render(report)
    assert "🟡" in text
    assert "1/1 سليم" not in text          # it must not claim health
    assert "يسأل السؤال الخطأ" in text
