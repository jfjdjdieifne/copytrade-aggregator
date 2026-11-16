"""The new source layer: OKX copy trading and the resting-order pipeline.

Every payload here is copied from the venue's own published documentation, not
invented to make the test pass. Where the documentation does *not* contain a
field we would like (OKX's stop-loss and take-profit prices, for instance) there
is a test asserting we leave it empty — because a test for a field we guessed
would only prove the guess matched itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cta.config import Config
from cta.models import Order, Position
from cta.orders import (
    attach_protective,
    entry_orders,
    executable_now,
    fresh,
    net_orders,
    summarise,
)
from cta.pending import as_positions, detect_pending, reachable
from cta.sources.hyperliquid import HyperliquidVenue
from cta.sources.okx import OKXVenue, coin_from_inst
from cta.sources.dydx import DydxVenue

from .conftest import FakeFetch

UTC = timezone.utc
CFG = Config()


# ── payloads lifted from OKX's documentation ─────────────────────────────────
LEAD_TRADERS = {
    "code": "0",
    "data": [
        {
            "dataVer": "20231129213200",
            "ranks": [
                {
                    "accCopyTraderNum": "3536",
                    "aum": "1509265.3238761567721365",
                    "ccy": "USDT",
                    "copyState": "0",
                    "copyTraderNum": "999",
                    "leadDays": "156",
                    "maxCopyTraderNum": "1000",
                    "nickName": "Crypto to the moon",
                    "pnl": "48805.1105999999972258",
                    "pnlRatio": "1.6898",
                    "pnlRatios": [
                        {"beginTs": "1701187200000", "pnlRatio": "1.6744"},
                        {"beginTs": "1700755200000", "pnlRatio": "1.649"},
                    ],
                    "portLink": "https://static.okx.com/cdn/...",
                    "traderInsts": ["ICP-USDT-SWAP", "MINA-USDT-SWAP"],
                    "uniqueCode": "540D011FDACCB47A",
                    "winRatio": "0.6957",
                }
            ],
            "totalPage": "1",
        }
    ],
    "msg": "",
}

SUBPOS_HISTORY = {
    "code": "0",
    "data": [
        {
            "ccy": "USDT",
            "closeAvgPx": "28385.9",
            "closeTime": "1697709137162",
            "instId": "BTC-USDT-SWAP",
            "instType": "SWAP",
            "lever": "20",
            "margin": "4.245285",
            "mgnMode": "isolated",
            "openAvgPx": "28301.9",
            "openTime": "1697698048031",
            "pnl": "0.252",
            "pnlRatio": "0.05935997229868",
            "posSide": "long",
            "subPos": "3",
            "subPosId": "635126416883355648",
            "uniqueCode": "9A8534AB09862774",
        },
        {
            "ccy": "USDT",
            "closeAvgPx": "29000.1",
            "closeTime": "1697795537162",
            "instId": "ETH-USDT-SWAP",
            "instType": "SWAP",
            "lever": "10",
            "margin": "12.0",
            "mgnMode": "isolated",
            "openAvgPx": "29500.0",
            "openTime": "1697780048031",
            "pnl": "-1.84",
            "pnlRatio": "-0.1533",
            "posSide": "net",
            "subPos": "-5",
            "subPosId": "635126416883355649",
            "uniqueCode": "9A8534AB09862774",
        },
    ],
    "msg": "",
}

CURRENT_SUBPOS = {
    "code": "0",
    "data": [
        {
            "ccy": "USDT",
            "instId": "SOL-USDT-SWAP",
            "instType": "SWAP",
            "lever": "5",
            "margin": "20.0",
            "markPx": "101.4",
            "mgnMode": "isolated",
            "openAvgPx": "98.2",
            "openTime": "1701231120479",
            "posSide": "net",
            "subPos": "12",
            "subPosId": "649945658862370816",
            "uniqueCode": "25CD5A80241D6FE6",
            "upl": "0.2807",
            "uplRatio": "0.0222",
        }
    ],
    "msg": "",
}


# ── OKX: discovery ───────────────────────────────────────────────────────────
async def test_leaderboard_unwraps_nested_ranks():
    """The rows live at data[0].ranks — not at data, as a flat read assumes."""
    fetch = FakeFetch({"public-lead-traders": LEAD_TRADERS})
    venue = OKXVenue(fetch, CFG)
    rows = await venue.leaderboard()
    assert len(rows) == 1
    assert rows[0]["uniqueCode"] == "540D011FDACCB47A"


async def test_parse_leaderboard_row_reports_aum_and_refuses_to_relabel_windows():
    """OKX publishes a 90-day PnL. Calling it 'monthly' would quietly feed the
    ROI gates a number that means something else, so `windows` stays empty."""
    row = OKXVenue.parse_leaderboard_row(LEAD_TRADERS["data"][0]["ranks"][0])
    assert row["wallet"] == "540d011fdaccb47a"
    assert row["account_value"] == pytest.approx(1_509_265.32, rel=1e-6)
    assert row["windows"] == {}
    # The measured 90-day figures are still carried, honestly labelled.
    assert row["pnl_ratio_90d"] == pytest.approx(1.6898)
    assert row["lead_days"] == 156
    assert row["win_ratio"] == pytest.approx(0.6957)


async def test_error_code_is_not_read_as_an_empty_history():
    """OKX reports failure inside a 200 body. Reading it as 'no trades' would
    silently downgrade every trader the moment the venue throttles us."""
    fetch = FakeFetch({"public-lead-traders": {"code": "50011", "data": [], "msg": "rate"}})
    venue = OKXVenue(fetch, CFG)
    assert await venue.leaderboard() == []
    assert await venue.health() is False


# ── OKX: track record ────────────────────────────────────────────────────────
async def test_history_becomes_fills_with_fees_flagged_unknown():
    fetch = FakeFetch({"public-subpositions-history": SUBPOS_HISTORY})
    venue = OKXVenue(fetch, CFG)
    fills = venue.parse_fills(await venue.fills("9A8534AB09862774"))

    assert len(fills) == 2
    first, second = fills                # sorted by close time
    assert first.coin == "BTC"
    assert first.side == "BUY"           # posSide: long
    assert first.closed_pnl == pytest.approx(0.252)
    # Net mode carries the side in the sign of subPos.
    assert second.coin == "ETH"
    assert second.side == "SELL"
    assert second.closed_pnl == pytest.approx(-1.84)
    # The venue publishes no fee, and we do not invent one.
    assert all(f.fee == 0.0 for f in fills)
    assert all(f.raw["fee_unknown"] for f in fills)


def _long_history(n: int = 120, fee_unknown: bool = True) -> list[dict]:
    """`n` closed subpositions spread over ~200 days, mostly wins."""
    rows = []
    start = int((datetime.now(UTC) - timedelta(days=200)).timestamp() * 1000)
    for i in range(n):
        win = i % 3 != 0
        rows.append({
            "ccy": "USDT", "closeAvgPx": "28385.9", "instId": "BTC-USDT-SWAP",
            "instType": "SWAP", "lever": "3", "margin": "40.0",
            "mgnMode": "isolated", "openAvgPx": "28301.9",
            "openTime": str(start + i * 86_400_000 * 1.6),
            "closeTime": str(start + i * 86_400_000 * 1.6 + 3_600_000),
            "pnl": "12.5" if win else "-8.0", "pnlRatio": "0.31",
            "posSide": "long", "subPos": "3", "subPosId": f"id{i}",
            "uniqueCode": "9A8534AB09862774",
        })
    return rows


def test_fee_unknown_lowers_the_grade_and_says_why():
    """A gross-of-fee track record flatters the trader, so the score takes a cut
    and says why — rather than passing an optimistic number off as net."""
    from cta.classifier import stats_from_fills

    venue = OKXVenue(FakeFetch({}), CFG)
    fills = venue.parse_fills(_long_history())
    stats = stats_from_fills("9A8534AB09862774", fills, account_value_usd=50_000.0, source="okx")
    assert stats.fee_unknown is True
    assert any("الرسوم" in r for r in stats.reasons)

    # The same track record with fees reported scores strictly higher: the
    # penalty is real, not decorative.
    with_fees = venue.parse_fills(_long_history())
    for f in with_fees:
        f.raw["fee_unknown"] = False
        f.fee = 1.5
    priced = stats_from_fills("9A8534AB09862774", with_fees,
                              account_value_usd=50_000.0, source="okx")
    assert priced.fee_unknown is False
    assert priced.score > stats.score


# ── OKX: live positions ──────────────────────────────────────────────────────
async def test_positions_derive_side_from_the_signed_size():
    fetch = FakeFetch({"public-current-subpositions": CURRENT_SUBPOS})
    venue = OKXVenue(fetch, CFG)
    positions = venue.parse_positions("25cd5a80241d6fe6", await venue.positions("25cd5a80241d6fe6"))

    assert len(positions) == 1
    pos = positions[0]
    assert pos.coin == "SOL"
    assert pos.side == "LONG"
    assert pos.entry == pytest.approx(98.2)
    assert pos.opened_at is not None
    # The public endpoint publishes no stop or target. Leaving them empty is
    # what makes the plan fall back to an ATR stop *labelled as computed*.
    assert pos.stop is None
    assert pos.target is None


async def test_short_subposition_is_read_as_short():
    row = dict(CURRENT_SUBPOS["data"][0], subPos="-12")
    positions = OKXVenue.parse_positions("x", [row])
    assert positions[0].side == "SHORT"


def test_instrument_ids_normalise_to_one_coin_name():
    assert coin_from_inst("BTC-USDT-SWAP") == "BTC"
    assert coin_from_inst("ETH-USD-241227") == "ETH"
    assert coin_from_inst("") == "UNKNOWN"


async def test_unique_code_is_uppercased_on_the_wire():
    """The watchlist stores lower-cased ids; OKX expects upper-case hex."""
    fetch = FakeFetch({"public-current-subpositions": CURRENT_SUBPOS})
    venue = OKXVenue(fetch, CFG)
    await venue.positions("25cd5a80241d6fe6")
    url = fetch.calls[-1][0]
    assert "uniqueCode=25CD5A80241D6FE6" in url


# ── dYdX: the query parameters that were missing ─────────────────────────────
async def test_dydx_fills_asks_for_one_subaccount():
    """Without address + subaccountNumber the indexer answers with the venue's
    most recent fills, which for any quiet wallet means an empty list that the
    old code reported as 'this trader has no history'."""
    fetch = FakeFetch({"fills": {"fills": [{"market": "BTC-USD"}]}})
    venue = DydxVenue(fetch, CFG)
    rows = await venue.fills("dydx1abc")
    assert rows == [{"market": "BTC-USD"}]
    url = fetch.calls[-1][0]
    assert "address=dydx1abc" in url
    assert "subaccountNumber=0" in url


async def test_dydx_positions_asks_for_one_subaccount():
    fetch = FakeFetch({"perpetualPositions": {"positions": []}})
    venue = DydxVenue(fetch, CFG)
    await venue.positions("dydx1abc")
    url = fetch.calls[-1][0]
    assert "address=dydx1abc" in url
    assert "subaccountNumber=0" in url


async def test_dydx_open_orders_keeps_only_live_orders():
    fetch = FakeFetch({
        "orders": {"orders": [
            {"status": "OPEN", "type": "LIMIT", "market": "BTC-USD"},
            {"status": "UNTRIGGERED", "type": "STOP_MARKET", "market": "BTC-USD"},
            {"status": "FILLED", "type": "LIMIT", "market": "ETH-USD"},
        ]}
    })
    venue = DydxVenue(fetch, CFG)
    rows = await venue.open_orders("dydx1abc")
    assert len(rows) == 2
    assert all(r["status"] in ("OPEN", "UNTRIGGERED") for r in rows)


# ── Hyperliquid: reading intent ──────────────────────────────────────────────
def _order(**kw) -> dict:
    base = {
        "coin": "BTC", "limitPx": "65000.0", "sz": "1.0", "oid": 1,
        "side": "B", "timestamp": 1700000000000, "orderType": "Limit",
        "origSz": "1.0", "reduceOnly": False, "isTrigger": False,
        "triggerPx": "0.0", "triggerCondition": "N/A", "isPositionTpsl": False,
    }
    base.update(kw)
    return base


def test_resting_bid_means_long_and_resting_ask_means_short():
    venue = HyperliquidVenue(FakeFetch({}), CFG)
    bid = venue.parse_open_orders("0xaa", [_order(side="B")])
    ask = venue.parse_open_orders("0xaa", [_order(side="A")])
    assert bid[0].side == "LONG"
    assert ask[0].side == "SHORT"


def test_reduce_only_order_reveals_the_position_it_is_closing():
    """A reduce-only ask can only be exiting a long. Reading 'SELL' as 'SHORT'
    here would put the stop on the wrong side of every trade."""
    venue = HyperliquidVenue(FakeFetch({}), CFG)
    out = venue.parse_open_orders("0xaa", [_order(side="A", reduceOnly=True)])
    assert out[0].side == "LONG"


def test_protective_order_is_classified_against_the_market_not_guessed():
    venue = HyperliquidVenue(FakeFetch({}), CFG)
    prices = {"BTC": 65_000.0}
    rows = [
        # reduce-only ask above the market: closing a long at a profit
        _order(side="A", reduceOnly=True, isTrigger=True, triggerPx="68000", isPositionTpsl=True),
        # reduce-only ask below the market: cutting a long at a loss
        _order(side="A", reduceOnly=True, isTrigger=True, triggerPx="63000", isPositionTpsl=True),
    ]
    out = venue.parse_open_orders("0xaa", rows, prices=prices)
    by_trigger = {o.trigger_px: o for o in out}
    assert by_trigger[68000.0].kind == "tp"
    assert by_trigger[63000.0].kind == "sl"
    # Both protect a long, not a short.
    assert all(o.side == "LONG" for o in out)


def test_protective_order_with_no_reference_price_stays_unclassified():
    """Half a decision is worse than none: choosing 'stop' without knowing where
    the market is would put a level on the wrong side and guarantee the loss."""
    venue = HyperliquidVenue(FakeFetch({}), CFG)
    out = venue.parse_open_orders(
        "0xaa", [_order(side="A", reduceOnly=True, isTrigger=True,
                        triggerPx="68000", isPositionTpsl=True)]
    )
    assert out[0].kind == "entry"
    assert out[0].kind_confidence == "unknown"


def test_short_protective_levels_are_the_mirror_image():
    venue = HyperliquidVenue(FakeFetch({}), CFG)
    prices = {"BTC": 65_000.0}
    out = venue.parse_open_orders(
        "0xaa",
        [_order(side="B", reduceOnly=True, isTrigger=True, triggerPx="62000", isPositionTpsl=True)],
        prices=prices,
    )
    assert out[0].side == "SHORT"
    assert out[0].kind == "tp"


# ── the pending-order pipeline ───────────────────────────────────────────────
def _o(wallet: str, coin: str, side: str, price: float, **kw) -> Order:
    defaults = {
        "source": "hyperliquid",
        "kind": "entry",
        "size": 1.0,
        "size_usd": price,
        "quality": "PROVEN",
        "quality_score": 75,
    }
    defaults.update(kw)
    return Order(wallet=wallet, coin=coin, side=side, price=price, **defaults)


def test_stacked_orders_from_one_trader_count_once():
    """Ten scaled limit orders are one opinion. Counting them as ten is how a
    single wallet fabricates a consensus with itself."""
    orders = [_o("0xa", "BTC", "LONG", 65_000 + i * 5) for i in range(10)]
    netted = net_orders(orders)
    assert len(netted) == 1
    assert netted[0].size == pytest.approx(10.0)


def test_their_stop_and_target_land_on_the_position():
    pos = Position(wallet="0xa", source="hyperliquid", coin="BTC",
                   signed_size=1.0, entry=65_000.0, size_usd=65_000.0)
    orders = [
        _o("0xa", "BTC", "LONG", 68_000.0, kind="tp"),
        _o("0xa", "BTC", "LONG", 63_000.0, kind="sl"),
    ]
    assert attach_protective([pos], orders) == 2
    assert pos.target == pytest.approx(68_000.0)
    assert pos.stop == pytest.approx(63_000.0)


def test_a_level_on_the_wrong_side_is_ignored_not_used():
    """A take-profit below a long's entry is not a take-profit. It may be a
    partial close, a hedge, or a mistake — none of which we should copy."""
    pos = Position(wallet="0xa", source="hyperliquid", coin="BTC",
                   signed_size=1.0, entry=65_000.0, size_usd=65_000.0)
    orders = [_o("0xa", "BTC", "LONG", 60_000.0, kind="tp")]
    assert attach_protective([pos], orders) == 0
    assert pos.target is None


def test_a_bid_far_above_the_market_is_not_actionable():
    """The traders were willing to pay up; the market has already passed them.
    Standing there means filling worse than they would have."""
    pos = Position(wallet="0xa", source="hyperliquid", coin="BTC",
                   signed_size=1.0, entry=70_000.0, size_usd=70_000.0)
    assert reachable(pos, 65_000.0, 50.0) is False
    # At their price, or better, it is still a live opportunity.
    assert reachable(pos, 70_000.0, 50.0) is True


def test_no_reference_price_does_not_become_a_rejection():
    """Absence of a price is not evidence of anything. Refusing to invent a
    verdict is the recurring rule in this codebase, and it applies here too."""
    pos = Position(wallet="0xa", source="hyperliquid", coin="BTC",
                   signed_size=1.0, entry=70_000.0, size_usd=70_000.0)
    assert reachable(pos, None, 50.0) is True
    assert reachable(pos, 0.0, 50.0) is True


def test_abandoned_orders_are_litter_not_intent():
    old = _o("0xa", "BTC", "LONG", 65_000.0, placed_ms=1_600_000_000_000)
    recent = _o("0xb", "BTC", "LONG", 65_000.0,
                placed_ms=int(datetime.now(UTC).timestamp() * 1000) - 60_000)
    assert fresh(old, max_age_min=1440) is False
    assert fresh(recent, max_age_min=1440) is True
    # An order of unknown age is treated as fresh, not silently discarded.
    assert fresh(_o("0xc", "BTC", "LONG", 65_000.0), max_age_min=1440) is True


async def test_two_proven_traders_resting_the_same_bid_become_a_signal():
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    orders = [
        _o("0xaa", "BTC", "LONG", 65_000.0, placed_ms=now_ms - 25 * 60_000, size=1.0),
        _o("0xbb", "BTC", "LONG", 65_010.0, placed_ms=now_ms - 5 * 60_000,
           size=2.7, source="dydx"),
    ]
    clusters = detect_pending(orders, CFG, now=now, prices={"BTC": 65_005.0})
    assert len(clusters) == 1
    assert clusters[0].n_members == 2
    assert clusters[0].n_distinct_sources == 2


async def test_one_trader_alone_is_never_consensus():
    now = datetime.now(UTC)
    recent_ms = int(now.timestamp() * 1000) - 60_000
    orders = [_o("0xaa", "BTC", "LONG", 65_000.0, placed_ms=recent_ms)]
    assert detect_pending(orders, CFG, now=now, prices={"BTC": 65_000.0}) == []


async def test_ungraded_wallets_cannot_join_consensus():
    now = datetime.now(UTC)
    recent_ms = int(now.timestamp() * 1000) - 60_000
    orders = [
        _o("0xaa", "BTC", "LONG", 65_000.0, placed_ms=recent_ms, size=1.0),
        _o("0xbb", "BTC", "LONG", 65_005.0, placed_ms=recent_ms - 20 * 60_000,
           size=2.4, quality="WATCH", quality_score=40),
    ]
    assert detect_pending(orders, CFG, now=now, prices={"BTC": 65_000.0}) == []


async def test_a_bid_the_market_has_left_behind_produces_no_signal():
    now = datetime.now(UTC)
    recent_ms = int(now.timestamp() * 1000) - 60_000
    orders = [
        _o("0xaa", "BTC", "LONG", 80_000.0, placed_ms=recent_ms, size=1.0),
        _o("0xbb", "BTC", "LONG", 80_010.0, placed_ms=recent_ms - 20 * 60_000, size=2.4),
    ]
    assert detect_pending(orders, CFG, now=now, prices={"BTC": 65_000.0}) == []


async def test_identical_order_and_identical_second_is_one_operator():
    """Two resting orders of the same size, placed in the same second, are the
    signature of one operator with two accounts (or a copy bot feeding both).

    Counting them as two independent traders would be the worst possible error
    this layer can make: it manufactures the exact consensus the whole system
    is built to wait for.
    """
    now = datetime.now(UTC)
    same_ms = int(now.timestamp() * 1000) - 60_000
    orders = [
        _o("0xaa", "BTC", "LONG", 65_000.0, placed_ms=same_ms, size=2.0),
        _o("0xbb", "BTC", "LONG", 65_000.0, placed_ms=same_ms, size=2.0),
    ]
    assert detect_pending(orders, CFG, now=now, prices={"BTC": 65_000.0}) == []


def test_entry_filter_and_summary():
    orders = [
        _o("0xa", "BTC", "LONG", 65_000.0),
        _o("0xa", "BTC", "LONG", 68_000.0, kind="tp"),
        _o("0xa", "BTC", "LONG", 63_000.0, kind="sl"),
    ]
    assert len(entry_orders(orders)) == 1
    report = summarise(orders)
    assert report["entry"] == 1 and report["tp"] == 1 and report["sl"] == 1
    assert report["by_source"]["hyperliquid"] == 3


def test_executable_now_treats_a_better_price_as_still_good():
    # A bid below the market: we can only improve on it.
    assert executable_now(64_000.0, 65_000.0, "LONG", 50.0) is True
    # A bid above the market is only acceptable within tolerance.
    assert executable_now(65_100.0, 65_000.0, "LONG", 50.0) is True
    assert executable_now(66_000.0, 65_000.0, "LONG", 50.0) is False


def test_pending_orders_project_into_positions_without_inventing_leverage():
    orders = [_o("0xa", "BTC", "LONG", 65_000.0, size=2.0)]
    positions = as_positions(orders)
    assert len(positions) == 1
    assert positions[0].entry == pytest.approx(65_000.0)
    assert positions[0].is_long
    # A resting order says nothing about leverage, and we do not pretend it does.
    assert positions[0].leverage == 1.0
