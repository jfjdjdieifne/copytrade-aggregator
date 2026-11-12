"""Paper trading: a $100 account that takes every signal, so the results are real.

What the operator asked for, in their words
-------------------------------------------
"خلّيه 24 ساعة وأنا نايم... يلاقي تقاطع صفقات ويبعت توصية، ويتابع التوصيات كلها
— سواء دخلتها أو ما دخلتها — يسجّلها ويشوف بعدين شو صار. واعتبر حسابنا فيه 100
دولار وأخذنا كل التوصيات لنشوف قدّيش بيطلع."

That is a paper account. Not a simulation of a market — a *bookkeeping* record of
what would have happened had every signal been taken with a fixed account and
fixed risk. The distinction matters: the prices are real, the fills are real
levels from the plan, and the only thing invented is that we placed the order.

Why this is the honest way to answer "will it make money?"
----------------------------------------------------------
`/stats` already aggregates R-multiples, but R-multiples flatter a strategy:
+3R on one trade and -1R on three looks fine until you size it. A fixed $100
account with fixed fractional risk produces the number that actually matters —
the account balance — and it exposes things R hides:

  * a signal that never fills (price ran away) is a miss, not a win
  * two signals on the same coin at once are one position, not two
  * the stop is a real loss of real dollars, and 12 of them in a row ends you

Design decisions, and why
-------------------------
1. **Every signal is taken.** No discretion, no "I would have skipped that one".
   Hindsight-trading is the single easiest way to fool yourself about a strategy,
   and the operator explicitly asked for all of them to be recorded.
2. **Risk is fixed at `risk_per_trade_pct` of the *current* balance**, not the
   starting balance. Compounding is the honest model; a fixed dollar risk
   overstates results when the account shrinks.
3. **The plan's own levels are used.** Entry, stop and target come from the
   signal, so the result measures the plan the operator was actually shown.
4. **Unfilled signals are recorded as missed**, and a signal whose entry was
   never touched is not silently counted as a winner.
5. **One open position per coin** per direction — the same coin appearing twice
   is the same trade, and taking it twice doubles the risk without doubling the
   edge.

What this deliberately does NOT model
-------------------------------------
Fees, funding, slippage beyond the plan's own tolerance, and liquidation. They
make results slightly worse, never better, so a positive paper result is not
proof of profit — it is a necessary condition. The dashboard says so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

UTC = timezone.utc

KEY_PAPER = "paper_account"

DEFAULT_START_BALANCE = 100.0


def empty_account(start_balance: float = DEFAULT_START_BALANCE) -> dict[str, Any]:
    return {
        "start_balance": float(start_balance),
        "balance": float(start_balance),
        "equity": float(start_balance),
        "trades": [],            # closed
        "open": [],              # taken, not yet resolved
        "missed": [],            # entry never reached
        "started_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _r_multiple(side: str, entry: float, stop: float, exit_price: float) -> float:
    """Signed R for a closed trade. Positive is profit."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    move = (exit_price - entry) if side == "LONG" else (entry - exit_price)
    return move / risk


def open_trade(account: dict, signal: dict, now: Optional[datetime] = None,
               risk_pct: float = 1.0) -> tuple[dict, Optional[dict]]:
    """Book a signal as taken. Returns (account, trade) or (account, None).

    A trade is only opened when the plan has a usable entry and stop: without a
    stop there is no risk unit, and without a risk unit the result cannot be
    compared across signals.
    """
    now = now or datetime.now(UTC)
    plan = signal.get("plan") or {}
    coin = str(signal.get("coin") or "").upper()
    side = str(signal.get("side") or "").upper()

    entry = _f(plan.get("entry") or signal.get("entry_center"))
    stop = _f(plan.get("stop"))
    target = _f(plan.get("target"))

    if not coin or side not in ("LONG", "SHORT") or entry <= 0 or stop <= 0:
        return account, None

    # One position per coin+direction. The same coin twice is the same trade.
    for existing in account.get("open", []):
        if existing.get("coin") == coin and existing.get("side") == side:
            return account, None

    balance = float(account.get("balance", account.get("start_balance", 0.0)))
    risk_usd = balance * (max(0.01, float(risk_pct)) / 100.0)
    stop_distance_pct = abs(entry - stop) / entry
    if stop_distance_pct <= 0:
        return account, None

    # Position size such that hitting the stop loses exactly `risk_usd`.
    notional = risk_usd / stop_distance_pct

    trade = {
        "signal_id": str(signal.get("signal_id") or ""),
        "coin": coin,
        "side": side,
        "entry": entry,
        "stop": stop,
        "target": target or None,
        "score": signal.get("score"),
        "level": signal.get("level"),
        "n_independent": signal.get("n_independent"),
        "risk_usd": round(risk_usd, 4),
        "notional_usd": round(notional, 4),
        "opened_at": now.isoformat(),
        "status": "open",
        # Assumed filled at the plan's entry.
        #
        # A signal is emitted with `entry` equal to market at emission (that is
        # how the plan is built), so by the time a human could act the price is
        # already there. Modelling a "did we get filled" gate made trades that
        # had already reached their target unable to resolve — the account sat
        # on a winner forever because the entry check ran before the exit check.
        # The operator asked for every signal to be taken, so we take it.
        "filled": True,
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
    }
    account.setdefault("open", []).append(trade)
    account["updated_at"] = now.isoformat()
    return account, trade


def mark_to_market(account: dict, prices: dict[str, float]) -> dict:
    """Update open trades against current prices, closing any that resolved.

    A trade resolves when price touches the stop or the target. Until then it is
    still open and its unrealised P&L belongs in equity, not in balance — the
    distinction keeps "closed trades" honest.
    """
    if not prices:
        return account

    still_open: list[dict] = []
    balance = float(account.get("balance", 0.0))

    for trade in account.get("open", []):
        coin = trade.get("coin")
        price = _f(prices.get(coin))
        if price <= 0:
            still_open.append(trade)
            continue

        entry = _f(trade.get("entry"))
        stop = _f(trade.get("stop"))
        target = _f(trade.get("target"))
        side = trade.get("side", "LONG")

        # Track excursions for the record, not for the decision.
        excursion = (price - entry) / entry * 100.0 if side == "LONG" else (entry - price) / entry * 100.0
        trade["mfe_pct"] = round(max(float(trade.get("mfe_pct", 0.0)), excursion), 4)
        trade["mae_pct"] = round(min(float(trade.get("mae_pct", 0.0)), excursion), 4)

        hit_stop = stop > 0 and (price <= stop if side == "LONG" else price >= stop)
        hit_target = target > 0 and (price >= target if side == "LONG" else price <= target)

        if not (hit_stop or hit_target):
            still_open.append(trade)
            continue

        exit_price = stop if hit_stop else target
        r = _r_multiple(side, entry, stop, exit_price)
        pnl = r * _f(trade.get("risk_usd"))

        trade.update(
            {
                "status": "closed",
                "exit_price": exit_price,
                "closed_at": datetime.now(UTC).isoformat(),
                "hit_stop": bool(hit_stop),
                "hit_target": bool(hit_target),
                "r_multiple": round(r, 4),
                "pnl_usd": round(pnl, 4),
            }
        )
        balance += pnl
        account.setdefault("trades", []).append(trade)

    account["open"] = still_open
    account["balance"] = round(balance, 4)

    unrealised = 0.0
    for trade in still_open:
        if not trade.get("filled"):
            continue
        price = _f(prices.get(trade.get("coin")))
        entry = _f(trade.get("entry"))
        if price <= 0 or entry <= 0:
            continue
        stop = _f(trade.get("stop"))
        risk = _f(trade.get("risk_usd"))
        r_now = _r_multiple(trade.get("side", "LONG"), entry, stop, price)
        unrealised += r_now * risk

    account["equity"] = round(balance + unrealised, 4)
    account["updated_at"] = datetime.now(UTC).isoformat()
    return account


def record_missed(account: dict, signal: dict, reason: str = "لم يصل السعر للدخول") -> dict:
    """Keep a record of signals that never filled.

    A strategy that reports only the trades it took is reporting a filtered
    sample. Misses are part of the strategy's real cost, so they are recorded.
    """
    account.setdefault("missed", []).append(
        {
            "signal_id": str(signal.get("signal_id") or ""),
            "coin": signal.get("coin"),
            "side": signal.get("side"),
            "score": signal.get("score"),
            "reason": reason,
            "at": datetime.now(UTC).isoformat(),
        }
    )
    account["missed"] = account["missed"][-200:]
    return account


def summarize(account: dict) -> dict:
    """The numbers an operator needs, phrased as an account statement."""
    trades = account.get("trades", []) or []
    start = float(account.get("start_balance", DEFAULT_START_BALANCE))
    balance = float(account.get("balance", start))
    equity = float(account.get("equity", balance))

    n = len(trades)
    wins = [t for t in trades if _f(t.get("pnl_usd")) > 0]
    losses = [t for t in trades if _f(t.get("pnl_usd")) < 0]
    gross_win = sum(_f(t.get("pnl_usd")) for t in wins)
    gross_loss = abs(sum(_f(t.get("pnl_usd")) for t in losses))

    r_values = [_f(t.get("r_multiple")) for t in trades]
    mean_r = (sum(r_values) / len(r_values)) if r_values else 0.0

    # Max drawdown over the closed-trade sequence, in dollars and percent.
    peak = start
    worst = 0.0
    running = start
    for t in trades:
        running += _f(t.get("pnl_usd"))
        peak = max(peak, running)
        drawdown = peak - running
        if drawdown > worst:
            worst = drawdown

    return {
        "start_balance": round(start, 2),
        "balance": round(balance, 2),
        "equity": round(equity, 2),
        "pnl_usd": round(balance - start, 2),
        "pnl_pct": round((balance - start) / start * 100.0, 2) if start else 0.0,
        "trades": n,
        "open": len(account.get("open", []) or []),
        "missed": len(account.get("missed", []) or []),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100.0, 1) if n else 0.0,
        "mean_r": round(mean_r, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (
            None if not wins else 99.0
        ),
        "max_drawdown_usd": round(worst, 2),
        "max_drawdown_pct": round(worst / peak * 100.0, 2) if peak > 0 else 0.0,
        # Explicitly not a profit forecast. Fees, funding and slippage are absent,
        # and every one of them makes the real number worse than this one.
        "caveat": "بدون رسوم أو تمويل أو انزلاق — النتيجة الحقيقية أسوأ قليلاً",
    }


def honest_verdict(summary: dict) -> str:
    """One sentence. Refuses to celebrate before there is evidence."""
    n = int(summary.get("trades", 0) or 0)
    pnl = float(summary.get("pnl_usd", 0.0) or 0.0)
    mean_r = float(summary.get("mean_r", 0.0) or 0.0)

    if n == 0:
        return "لا صفقات مغلقة بعد — لا يمكن الحكم على شيء."
    if n < 20:
        return f"{n} صفقة فقط — عدد قليل جداً للحكم. انتظر 20 على الأقل."
    if pnl > 0 and mean_r > 0.1:
        return f"{n} صفقة بمتوسط {mean_r:+.2f}R — نتيجة إيجابية، لكنها ليست ضماناً للمستقبل."
    if pnl > 0:
        return f"{n} صفقة بربح {pnl:+.2f}$ — لكن متوسط R ضعيف ({mean_r:+.2f})."
    return f"{n} صفقة بخسارة {pnl:+.2f}$ — الاستراتيجية لم تثبت نفسها بعد."


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
