"""Deep health check — prove each venue returns real data, not just a 200.

Why this exists
---------------
Every serious defect this project has shipped was the same defect wearing a
different hat:

    a venue call fails, or is asked the wrong question
    -> the adapter returns [] or a page belonging to somebody else
    -> the exception is swallowed
    -> "healthy" is reported
    -> the radar watches nothing and says so in no visible way

dYdX called its indexer without `address`, got the venue-wide feed, filtered it
by a field that does not exist, and reported every wallet as having no history.
GMX pointed at a host that does not exist. A liveness probe would have called
both of those "fine", because a liveness probe only asks *can I reach you*, not
*did you answer the question I asked*.

So this module asks the second question, per venue, with a real wallet:

    leaderboard  — did we get rows, and do they carry an identifier?
    positions    — did we get at least one position for a known active wallet?
    fills        — did we get history, with a plausible size?
    orders       — does this venue publish resting orders at all?
    prices       — can we resolve a live reference price?

A venue that returns zero rows where it should return some is reported
`EMPTY`, not `OK`. A venue with no order endpoint is reported `UNSUPPORTED`,
which is a fact rather than a failure — OKX publishes no resting orders, and
saying so is better than a zero that looks like a bug.
"""

from __future__ import annotations

import time
from typing import Any, Optional

# Statuses
OK = "OK"
EMPTY = "EMPTY"        # reachable, answered, but returned nothing
UNSUPPORTED = "UNSUPPORTED"   # the venue genuinely has no such endpoint
ERROR = "ERROR"        # the call failed


async def _timed(coro) -> tuple[Any, Optional[str], float]:
    started = time.monotonic()
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001 — the whole point is to report it
        return None, f"{type(exc).__name__}: {exc}"[:200], time.monotonic() - started
    return result, None, time.monotonic() - started


def _verdict(rows: Optional[list], error: Optional[str]) -> str:
    if error:
        return ERROR
    if rows is None:
        return ERROR
    return OK if rows else EMPTY


async def probe_venue(venue, wallet: Optional[str] = None) -> dict:
    """Ask one venue the questions that actually matter.

    `wallet` is a wallet known to be active on that venue. Without one we can
    only prove reachability, and that is exactly the proof that let the dYdX
    bug survive: `/v4/perpetualMarkets` answered fine for months while
    `/v4/perpetualPositions` returned nothing usable.
    """
    name = getattr(venue, "name", "?")
    report: dict[str, Any] = {
        "venue": name,
        "wallet_probed": wallet or "",
        "checks": {},
    }

    # ── reachability (cheap, but not sufficient on its own) ─────────────────
    healthy, err, took = await _timed(venue.health())
    report["checks"]["health"] = {
        "status": OK if healthy else (ERROR if err else EMPTY),
        "ms": round(took * 1000),
        "error": err,
    }

    # ── prices: needed for the slippage gate to mean anything ───────────────
    prices, err, took = await _timed(venue.prices())
    report["checks"]["prices"] = {
        "status": _verdict(list(prices.items()) if prices else [], err),
        "ms": round(took * 1000),
        "count": len(prices or {}),
        "error": err,
    }

    # ── leaderboard: can discovery find anyone at all? ──────────────────────
    if hasattr(venue, "leaderboard"):
        rows, err, took = await _timed(venue.leaderboard())
        if err and "NotImplemented" in err:
            status = UNSUPPORTED
        else:
            status = _verdict(rows, err)
        identified = None
        if status == OK and hasattr(venue, "parse_leaderboard_row"):
            parsed = [venue.parse_leaderboard_row(r) for r in rows or []]
            identified = sum(1 for p in parsed if p.get("wallet"))
            # Rows we cannot attribute to anyone are useless for discovery:
            # we would have candidates we could never poll.
            if identified == 0:
                status = EMPTY
        entry = {
            "status": status,
            "ms": round(took * 1000),
            "count": len(rows or []),
            "error": err,
        }
        if identified is not None:
            entry["identified"] = identified
        report["checks"]["leaderboard"] = entry

    if not wallet:
        report["verdict"] = report["checks"]["health"]["status"]
        return report

    # ── the four per-wallet questions ───────────────────────────────────────
    positions, err, took = await _timed(venue.positions(wallet))
    parsed_positions = []
    if positions is not None and hasattr(venue, "parse_positions"):
        try:
            parsed_positions = venue.parse_positions(wallet, positions)
        except Exception as exc:  # noqa: BLE001
            err = err or f"parse_positions: {type(exc).__name__}: {exc}"[:200]
    report["checks"]["positions"] = {
        "status": _verdict(parsed_positions, err),
        "ms": round(took * 1000),
        "raw_rows": len(positions or []),
        "parsed": len(parsed_positions),
        "error": err,
    }

    fills, err, took = await _timed(venue.fills(wallet, limit=200))
    parsed_fills = []
    if fills is not None and hasattr(venue, "parse_fills"):
        try:
            parsed_fills = venue.parse_fills(fills)
        except Exception as exc:  # noqa: BLE001
            err = err or f"parse_fills: {type(exc).__name__}: {exc}"[:200]
    report["checks"]["fills"] = {
        "status": _verdict(parsed_fills, err),
        "ms": round(took * 1000),
        "raw_rows": len(fills or []),
        "parsed": len(parsed_fills),
        "error": err,
    }

    # Resting orders: the only place intent is visible, and the only place a
    # "no" is an honest answer rather than a defect.
    if hasattr(venue, "open_orders") and hasattr(venue, "parse_open_orders"):
        orders, err, took = await _timed(venue.open_orders(wallet))
        parsed_orders = []
        if orders is not None:
            try:
                parsed_orders = venue.parse_open_orders(wallet, orders)
            except Exception as exc:  # noqa: BLE001
                err = err or f"parse_open_orders: {type(exc).__name__}: {exc}"[:200]
        report["checks"]["orders"] = {
            # Zero resting orders is normal for most wallets, so an empty list
            # is a success here, not a failure — the opposite of `fills`.
            "status": OK if not err else ERROR,
            "ms": round(took * 1000),
            "parsed": len(parsed_orders),
            "error": err,
        }
    else:
        report["checks"]["orders"] = {
            "status": UNSUPPORTED,
            "note": "هذه المنصة لا تنشر أوامر معلقة — مصدر النية هو Hyperliquid وdYdX",
        }

    statuses = [c.get("status") for c in report["checks"].values()]
    if ERROR in statuses:
        report["verdict"] = ERROR
    elif EMPTY in statuses:
        report["verdict"] = EMPTY
    else:
        report["verdict"] = OK
    return report


async def probe_all(venues: dict, wallets: Optional[dict] = None) -> dict:
    """Probe every venue. `wallets` maps venue name -> a known-active wallet."""
    wallets = wallets or {}
    reports = {}
    for name, venue in venues.items():
        reports[name] = await probe_venue(venue, wallets.get(name))
    return {
        "venues": reports,
        "healthy": [n for n, r in reports.items() if r["verdict"] == OK],
        "suspect": [n for n, r in reports.items() if r["verdict"] == EMPTY],
        "broken": [n for n, r in reports.items() if r["verdict"] == ERROR],
    }


def render(report: dict) -> str:
    """Arabic one-screen summary. The point is that a problem is unmissable."""
    icon = {OK: "🟢", EMPTY: "🟡", UNSUPPORTED: "⚪", ERROR: "🔴"}
    lines = []
    for name, r in report["venues"].items():
        verdict = r["verdict"]
        lines.append(
            f"{icon.get(verdict, '?')} {name}"
            + (f"  (محفظة: {r['wallet_probed'][:18]})" if r["wallet_probed"] else "")
        )
        for check, info in r["checks"].items():
            status = info.get("status", "?")
            detail = f"{info.get('ms', 0)}ms"
            if check in ("positions", "fills"):
                detail += f" · خام {info.get('raw_rows', 0)} → مُحلَّل {info.get('parsed', 0)}"
            elif check == "leaderboard":
                detail += f" · {info.get('count', 0)} صف"
                if "identified" in info:
                    detail += f" · {info['identified']} قابل للنسب"
            elif check == "prices":
                detail += f" · {info.get('count', 0)} سعر"
            elif check == "orders":
                detail += f" · {info.get('parsed', 0)} أمر"
            if info.get("error"):
                detail += f" · ❌ {info['error']}"
            if info.get("note"):
                detail += f" · {info['note']}"
            lines.append(f"    {icon.get(status, '?')} {check}: {status}  ({detail})")

    summary = report["venues"]
    lines.append("")
    lines.append(
        f"الخلاصة: {len(report['healthy'])}/{len(summary)} سليم · "
        f"{len(report['suspect'])} يُرجِع فراغاً · {len(report['broken'])} معطوب"
    )
    if report["suspect"]:
        lines.append(
            "🟡 منصة تُرجِع فراغاً تعني أن الكود يعمل لكنه يسأل السؤال الخطأ "
            "(أو لا يسأل أحداً). لا تعتمدها قبل الإصلاح."
        )
    if report["broken"]:
        lines.append("🔴 منصة معطوبة: الاستدعاء نفسه يفشل.")
    return "\n".join(lines)
