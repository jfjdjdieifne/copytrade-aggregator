"""A/B lab: how many traders does an agreement actually need?

The operator asked the only question that matters here, and asked it in the
right order: *does convergence of three or more even happen, or is it a
once-a-year event?* Then: *is five better than three?*

Neither can be answered by reasoning, and neither can be answered by my
simulator — its traders are invented, so how often they agree is a property of
my imagination, not of the market. Both can only be answered by measuring, on
live venues, over days.

So this module is an instrument, not a strategy. Every cycle it does two
things:

1. **Counts.** For every cluster the radar finds, it records how many
   independent traders were in it, with no quality filter applied. The
   resulting histogram *is* the answer to "is three common?" — read off the
   bucket sizes after a day of running.

2. **Bets.** It runs several paper accounts side by side, each starting from
   the same balance and each entering on a different rule:

       n2  two or more traders agree    (any quality)
       n3  three or more                (any quality)
       n4  four or more                 (any quality)
       n5  five or more                 (any quality)

   Every account uses the *same* plan builder, the same risk percentage and
   the same prices, so a difference in outcome is attributable to the
   threshold and nothing else.

The `pro` bucket is deliberately NOT here. The real system already paper-trades
its own signals in `cta.paper`; comparing against that account is the point of
printing them together, but duplicating its entry rule in two places is how
two implementations quietly drift apart.

Nothing here can affect a cycle, a signal, or a message. It observes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

UTC = timezone.utc

KEY_SHADOW = "shadow_lab"

# Thresholds tracked in parallel. Two is the configured default; three is the
# operator's proposal; four and five bracket the "more is better" claim so it
# can be falsified rather than assumed.
DEFAULT_THRESHOLDS: tuple[int, ...] = (2, 3, 4, 5)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN guard


# ── lab state ────────────────────────────────────────────────────────────────

def empty_lab(
    thresholds=DEFAULT_THRESHOLDS,
    start_balance: float = 100.0,
    risk_pct: float = 2.0,
) -> dict:
    """One lab: a size histogram plus one paper account per threshold."""
    from .paper import empty_account

    return {
        "started_at": datetime.now(UTC).isoformat(),
        "start_balance": float(start_balance),
        "risk_pct": float(risk_pct),
        "thresholds": [int(t) for t in thresholds],
        # Cluster sizes, counted with no quality filter. This is the number the
        # operator actually asked for: is three traders common or rare?
        "sizes": {},
        # Total clusters observed, so "no size-5 cluster yet" can be told apart
        # from "no clusters at all".
        "clusters_seen": 0,
        "accounts": {
            f"n{t}": empty_account(start_balance) for t in thresholds
        },
    }


def _bucket_for(threshold: int) -> str:
    return f"n{threshold}"


# ── observation ──────────────────────────────────────────────────────────────

def observe_clusters(
    lab: dict,
    clusters: list,
    cfg: Any = None,
    prices: Optional[dict] = None,
    now: Optional[datetime] = None,
    n_positions: int = 0,
    n_coins: int = 0,
) -> dict:
    """Record every cluster's size, and bet on each threshold that is met.

    `clusters` are the raw clusters from `engine.scan`, before any quality
    gate — that is the whole point: the histogram must describe what the market
    does, not what we currently allow ourselves to trade.
    """
    now = now or datetime.now(UTC)

    from .paper import open_trade
    from .plan import build_plan

    lab.setdefault("sizes", {})
    lab.setdefault("accounts", {})
    # Ingredients for the chance baseline. Without them "three traders agreed"
    # cannot be told apart from "three traders had to agree sooner or later".
    lab["cycles"] = _int(lab.get("cycles")) + 1
    lab["position_sum"] = _int(lab.get("position_sum")) + int(n_positions or 0)
    lab["coin_sum"] = _int(lab.get("coin_sum")) + int(n_coins or 0)

    risk_pct = _f(lab.get("risk_pct"), 2.0)

    for cluster in clusters or []:
        try:
            n = int(getattr(cluster, "n_members", 0) or 0)
        except Exception:
            continue
        if n <= 0:
            continue

        lab["clusters_seen"] = _int(lab.get("clusters_seen")) + 1
        key = str(n)
        lab["sizes"][key] = _int(lab["sizes"].get(key)) + 1

        coin = str(getattr(cluster, "coin", "") or "").upper()
        side = str(getattr(cluster, "side", "") or "").upper()
        if not coin or side not in ("LONG", "SHORT"):
            continue

        for threshold in lab.get("thresholds") or DEFAULT_THRESHOLDS:
            threshold = int(threshold)
            if n < threshold:
                continue

            bucket = _bucket_for(threshold)
            account = lab["accounts"].get(bucket)
            if not isinstance(account, dict):
                continue

            try:
                plan = build_plan(
                    cluster, cfg,
                    atr_hint=None,
                    market_price=(prices or {}).get(coin),
                )
                if plan is None:
                    continue
                signal = {
                    "coin": coin,
                    "side": side,
                    "signal_id": f"{bucket}:{coin}:{side}:{now.isoformat()}",
                    "plan": {
                        "entry": _f(getattr(plan, "entry", 0.0)),
                        "stop": _f(getattr(plan, "stop", 0.0)),
                        "target": _f(getattr(plan, "target", 0.0)),
                    },
                    "n_independent": n,
                }
                lab["accounts"][bucket], _ = open_trade(
                    account, signal, risk_pct=risk_pct, now=now
                )
            except Exception:
                # One bad cluster must not cost us the whole measurement.
                continue

    return lab


def mark_all(lab: dict, prices: Optional[dict]) -> dict:
    """Mark every account. A bucket that cannot be marked keeps its last state."""
    from .paper import mark_to_market

    for bucket, account in (lab.get("accounts") or {}).items():
        if not isinstance(account, dict):
            continue
        try:
            lab["accounts"][bucket] = mark_to_market(account, prices or {})
        except Exception:
            continue
    return lab


# ── reporting ────────────────────────────────────────────────────────────────

def size_histogram(lab: dict) -> dict[int, int]:
    out: dict[int, int] = {}
    for key, count in (lab.get("sizes") or {}).items():
        try:
            out[int(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return dict(sorted(out.items()))


def chance_baseline(positions: int, coins: int, max_k: int = 6) -> dict[int, float]:
    """How many agreements of each size pure chance would produce.

    Scatter `positions` traders uniformly over `coins` x 2 directions and count
    how many buckets end up holding k traders. That is the number to compare
    against: if the market shows three-trader agreements about as often as
    shuffling a deck would, then "three traders agree" carries no information,
    and raising the threshold is not a filter -- it is a slower coin flip.

    Poisson is the right approximation: many traders, each independently
    unlikely to land in any particular bucket.
    """
    if positions <= 0 or coins <= 0:
        return {}
    buckets = coins * 2
    lam = positions / buckets
    if lam <= 0:
        return {}

    from math import exp, factorial

    out: dict[int, float] = {}
    tail = 0.0
    for k in range(max_k, -1, -1):
        tail += exp(-lam) * (lam ** k) / factorial(k)
        out[k] = buckets * tail
    return out


def summarize_lab(lab: dict) -> dict:
    """Per-threshold results plus the size distribution.

    `n` is carried on every row on purpose. A 60% win rate on two trades is
    noise, and presenting it next to a 60% win rate on ninety trades without
    saying so is how an operator talks themselves into a threshold.
    """
    from .paper import summarize

    rows = []
    for threshold in lab.get("thresholds") or DEFAULT_THRESHOLDS:
        bucket = _bucket_for(threshold)
        account = (lab.get("accounts") or {}).get(bucket)
        if not isinstance(account, dict):
            continue
        summary = summarize(account)
        # `paper.summarize` names these `trades` and `mean_r`, and returns
        # `win_rate` already as a percentage. Reading them under the wrong names
        # produced a silent zero-filled table; multiplying an already-percent
        # win rate by 100 printed "2500%".
        n_trades = _int(summary.get("trades"))
        rows.append({
            "bucket": bucket,
            "min_traders": int(threshold),
            "balance": round(_f(summary.get("balance")), 2),
            "pnl_pct": round(_f(summary.get("pnl_pct")), 2),
            "trades": n_trades,
            "wins": _int(summary.get("wins")),
            "win_rate": round(_f(summary.get("win_rate")), 1),
            "total_r": round(_f(summary.get("mean_r")) * n_trades, 2),
            "avg_r": round(_f(summary.get("mean_r")), 3),
            "open": _int(summary.get("open")),
        })

    cycles = _int(lab.get("cycles"))
    avg_positions = _int(lab.get("position_sum")) / cycles if cycles else 0
    avg_coins = _int(lab.get("coin_sum")) / cycles if cycles else 0

    return {
        "clusters_seen": _int(lab.get("clusters_seen")),
        "cycles": cycles,
        "avg_positions": round(avg_positions, 1),
        "avg_coins": round(avg_coins, 1),
        "chance": chance_baseline(int(avg_positions), int(avg_coins)),
        "sizes": size_histogram(lab),
        "start_balance": round(_f(lab.get("start_balance"), 100.0), 2),
        "rows": rows,
    }


def render_lab(lab: dict, pro_summary: Optional[dict] = None) -> str:
    """Arabic report. Numbers first, verdict last, never the other way round."""
    if not isinstance(lab, dict):
        return "🧪 *مختبر الاتفاق*\n\nما عندنا بيانات لسه — شغّل دورات أول."

    summary = summarize_lab(lab)
    lines: list[str] = ["🧪 *مختبر الاتفاق*", ""]

    total = summary["clusters_seen"]
    sizes = summary["sizes"]

    if not total:
        lines.append("ما شفنا ولا عنقود لسه — بَدّي وقت أطول.")
        return "\n".join(lines)

    lines.append(f"عناقيد مرصودة: {total}")
    lines.append("")
    lines.append("*كم متداول اجتمع؟*")
    for n, count in sizes.items():
        share = count / total * 100 if total else 0.0
        bar = "█" * max(1, round(share / 5)) if share >= 5 else "▏"
        lines.append(f"  {n} متداول: {count:4d}  ({share:4.1f}%) {bar}")

    # Observed versus chance. This is the row that decides whether convergence
    # means anything at all: if the market agrees about as often as a shuffle
    # would, the threshold is not filtering signal, it is just making the bell
    # ring less often.
    cycles = summary["cycles"]
    chance = summary.get("chance") or {}
    if cycles and chance:
        lines.append("")
        lines.append(
            f"*مقارنة بالصدفة* (وسطي {summary['avg_positions']:.0f} مركز على "
            f"{summary['avg_coins']:.0f} عملة)"
        )
        lines.append("```")
        lines.append("        شاهدنا/دورة   الصدفة تتوقع")
        for k in sorted(sizes):
            observed = sizes[k] / cycles
            expected = chance.get(k)
            if expected is None:
                continue
            verdict = "" if observed >= expected * 1.5 else "  ← قريب من الصدفة"
            lines.append(
                f"  ≥{k}      {observed:8.2f}      {expected:8.2f}{verdict}"
            )
        lines.append("```")

    # The sentence the operator is waiting for.
    at_least_3 = sum(c for n, c in sizes.items() if n >= 3)
    at_least_5 = sum(c for n, c in sizes.items() if n >= 5)
    lines.append("")
    if total:
        lines.append(
            f"٣ أو أكثر: {at_least_3} مرة ({at_least_3/total*100:.0f}% من العناقيد) · "
            f"٥ أو أكثر: {at_least_5} مرة ({at_least_5/total*100:.0f}%)"
        )

    lines.append("")
    lines.append("*النتائج (كل حساب بدأ بـ $%.0f)*" % summary["start_balance"])
    lines.append("```")
    lines.append("  القاعدة         صفقات   ربح%   نجاح%   مجموع R")
    for row in summary["rows"]:
        # A win rate computed over zero trades is not 0%, it is unknown.
        # Printing "0.0%" there reads as "lost everything".
        wr = f"{row['win_rate']:5.1f}%" if row["trades"] else "     —"
        lines.append(
            f"  ≥{row['min_traders']} متداول      {row['trades']:5d}  "
            f"{row['pnl_pct']:+6.1f}%  {wr}  {row['total_r']:+7.2f}R"
        )
    if pro_summary:
        n_pro = _int(pro_summary.get("trades"))
        wr_pro = (f"{_f(pro_summary.get('win_rate')):5.1f}%" if n_pro
                  else "     —")
        lines.append(
            f"  النظام(محترفين)  {n_pro:5d}  "
            f"{_f(pro_summary.get('pnl_pct')):+6.1f}%  {wr_pro}  "
            f"{_f(pro_summary.get('mean_r')) * n_pro:+7.2f}R"
        )
    lines.append("```")

    thin = [r for r in summary["rows"] if r["trades"] < 10]
    if thin:
        names = "، ".join(f"≥{r['min_traders']}" for r in thin)
        lines.append("")
        lines.append(
            f"_⚠️ عيّنة صغيرة ({names}): أقل من ١٠ صفقات — النسبة هون ضجيج، مو نتيجة._"
        )

    return "\n".join(lines)
