"""Run the real 24/7 loop against the simulator.

This is the closest thing to a live test that exists without a route to the
venues, and it is deliberately *not* a separate code path: it builds the sim
venues, hands them to the same `run_cycle` the cron uses, and lets the real
engine, classifier, convergence gates, plan builder and outcome tracker do
their work.

What it proves, in order:

1. discovery can find and score traders
2. grading separates skill from luck (`elite` up, `lucky`/`rich` down)
3. convergence fires when traders agree and stays silent when they do not
4. pending consensus fires on resting orders, with their own stops and targets
5. the outcome tracker prices signals afterwards, so the idea is *measured*

If any of those silently stops working, this run goes quiet — which is the
point. A simulator that always produces signals would prove nothing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from .config import Config
from .engine import run_cycle
from .simulator import COINS, SimVenue, build_sim_venues
from .state import MemoryStore, remember_signal


async def profile_sim(venues: dict, store, cfg: Config) -> dict:
    """Score every sim trader with the real classifier."""
    from .classifier import stats_from_fills
    from .discovery import build_watchlist

    profiles = []
    for name, venue in venues.items():
        for trader in venue.traders:
            fills = venue.parse_fills(await venue.fills(trader.wallet))
            if not fills:
                continue
            stats = stats_from_fills(
                trader.wallet,
                fills,
                account_value_usd=trader.account_value,
                source=name,
                data_confirmed=True,
                max_leverage=trader.leverage,
            )
            profiles.append(stats)

    watchlist = build_watchlist(profiles, cfg)
    await store.set(
        "watchlist",
        [
            {
                "wallet": p.wallet,
                "source": p.source,
                "grade": p.grade,
                "score": p.score,
                "n_trades": p.n_trades,
                "profit_factor": round(p.profit_factor, 3),
                "max_drawdown": round(p.max_drawdown, 4),
                "win_rate": round(p.win_rate, 3),
            }
            for p in watchlist
        ],
    )
    await store.set("profiles", [
        {"wallet": p.wallet, "source": p.source, "grade": p.grade,
         "score": p.score, "n_trades": p.n_trades,
         "profit_factor": round(p.profit_factor, 3)}
        for p in profiles
    ])
    return {
        "scored": len(profiles),
        "watchlist": len(watchlist),
        "grades": _grade_counts(profiles),
    }


def _grade_counts(profiles) -> dict:
    """Count grades from either TraderStats objects or their stored dicts."""
    out: dict[str, int] = {}
    for p in profiles:
        grade = p.get("grade") if isinstance(p, dict) else getattr(p, "grade", "?")
        out[grade] = out.get(grade, 0) + 1
    return dict(sorted(out.items()))


async def scan_sim(
    venues: dict, store, cfg: Config, order_scan: bool = True
) -> dict:
    """One monitoring cycle over the sim venues, using the real `run_cycle`."""
    report = await run_cycle(venues, store, cfg)
    return report


async def run_simulation(
    cycles: int = 12,
    n_traders: int = 40,
    seed: int = 7,
    inject_every: int = 3,
    resting_every: Optional[int] = 4,
    verbose: bool = True,
) -> dict:
    """Drive the whole loop: profile, then N cycles with injected consensus.

    `inject_every` controls how often traders are made to agree. It is not there
    to manufacture a flattering result — a run with no injections must produce
    no signals, and asserting that is one of the tests.
    """
    cfg = Config(
        enabled_sources=("sim", "sim-beta"),
        watchlist_max=200,
        watchlist_min_grade="PROVEN",
        discovery_max_candidates=200,
        subrequest_limit=10_000,
        orders_enabled=True,
        orders_max_wallets=200,
        min_signal_score=65,
    )
    store = MemoryStore()
    # Events are driven explicitly here, so the venue must not also generate
    # them: two sources of events make a run non-reproducible and its failures
    # impossible to replay.
    venues = build_sim_venues(
        n_traders=n_traders, seed=seed, auto_events=False, fresh=True
    )

    out: dict[str, Any] = {
        "profile": None,
        "cycles": [],
        "signals": [],
        "events": [],
        "final_grades": None,
    }

    out["profile"] = await profile_sim(venues, store, cfg)

    alpha: SimVenue = venues["sim"]
    beta = venues["sim-beta"]

    for cycle in range(1, cycles + 1):
        alpha.advance(30)
        beta.advance(30)

        injected = None
        if inject_every and cycle % inject_every == 0:
            coin = COINS[cycle % len(COINS)]
            side = "LONG" if cycle % 2 else "SHORT"
            resting = bool(resting_every and cycle % resting_every == 0)
            injected = _cross_venue_event(alpha, beta, coin, side, resting)
            out["events"].append(injected)

        report = await scan_sim(venues, store, cfg)

        # `engine.run_cycle` returns the signals but does not persist them —
        # that is the runtime wrapper's job. Calling it directly means doing
        # the persistence here, or every downstream reader (outcomes, the
        # paper account, this report) sees an empty history.
        for sig in report.get("signal_objects") or []:
            await remember_signal(store, sig.to_dict())

        signals = await store.get("signals", []) or []
        new = [s for s in signals if s not in out["signals"]]
        out["signals"] = signals

        entry = {
            "cycle": cycle,
            "injected": (injected or {}).get("coin"),
            "resting": bool((injected or {}).get("resting")),
            "clusters": int(report.get("clusters") or 0),
            "emitted": int(report.get("signals") or 0),
            "total_signals": len(signals),
            "rejections": (report.get("last_rejections") or [])[:3],
        }
        out["cycles"].append(entry)

        if verbose:
            tag = f"{entry['injected'] or '—'} {'(أمر معلّق)' if entry['resting'] else ''}"
            print(
                f"  دورة {cycle:>3}: عناقيد={entry['clusters']} "
                f"مُصدَرة={entry['emitted']} · حقنة={tag}"
            )
            for s in new:
                print(f"           ↳ {s.get('coin')} {s.get('side')} "
                      f"{s.get('score')}/100 ({s.get('level')}) · "
                      f"{s.get('n_independent')} متداول · "
                      f"kind={s.get('kind')}")

        # Signals already reported should not be re-emitted next cycle; the
        # dedupe lives in the engine, so this only mirrors it for display.
        await asyncio.sleep(0)

    out["final_grades"] = _grade_counts(await store.get("profiles", []) or [])
    return out


def _cross_venue_event(alpha: SimVenue, beta: SimVenue, coin: str, side: str,
                       resting: bool) -> dict:
    """Make traders on *both* venues agree — the strongest, rarest signal.

    Cross-venue agreement is what the whole design waits for, so the simulator
    must be able to produce it: two traders on independent platforms taking the
    same side at the same price cannot be one operator with two accounts.
    """
    a_elites = [t.wallet for t in alpha.traders if t.archetype == "elite"][:2]
    b_elites = [t.wallet for t in beta.traders if t.archetype == "elite"][:2]
    if not a_elites or not b_elites:
        a_elites = [t.wallet for t in alpha.traders][:2]
        b_elites = [t.wallet for t in beta.traders][:2]

    ev_a = alpha.inject_cluster(coin, side, members=a_elites, resting=resting)
    ev_b = beta.inject_cluster(coin, side, members=b_elites, resting=resting)
    return {
        "coin": coin, "side": side, "resting": resting,
        "members": ev_a["members"] + ev_b["members"],
    }


async def run_quiet_control(cycles: int = 6, n_traders: int = 40, seed: int = 7) -> dict:
    """The negative control: no injections, so there must be no signals.

    A radar that invents consensus when nothing agreed is worse than useless,
    and it is the failure mode most likely to survive testing — every part
    "works", the output looks plausible, and it is fiction.
    """
    return await run_simulation(
        cycles=cycles, n_traders=n_traders, seed=seed,
        inject_every=0, verbose=False,
    )


def render_report(result: dict) -> str:
    lines = ["", "═" * 72, "تقرير المحاكاة", "═" * 72]
    prof = result["profile"] or {}
    lines.append(
        f"التصنيف: صُنِّف {prof.get('scored', 0)} متداولاً · "
        f"قائمة المراقبة: {prof.get('watchlist', 0)}"
    )
    lines.append(f"توزيع الدرجات: {json.dumps(prof.get('grades', {}), ensure_ascii=False)}")

    total = sum(c["emitted"] for c in result["cycles"])
    pending = sum(
        1 for s in result["signals"] if s.get("kind") == "pending"
    )
    lines.append(f"الدورات: {len(result['cycles'])} · إشارات مُصدَرة: {total}")
    lines.append(f"منها أوامر معلّقة (قبل التنفيذ): {pending}")

    if result["signals"]:
        lines.append("")
        lines.append("أقوى الإشارات:")
        for s in sorted(result["signals"], key=lambda x: x.get("score", 0),
                        reverse=True)[:5]:
            plan = s.get("plan") or {}
            lines.append(
                f"  • {s.get('coin')} {s.get('side')} · {s.get('score')}/100 "
                f"({s.get('level')}) · {s.get('n_independent')} متداول · "
                f"مصادر: {', '.join(s.get('sources') or [])}"
            )
            lines.append(
                f"      دخول {plan.get('entry')} · ستوب {plan.get('stop')} "
                f"({plan.get('stop_source')}) · هدف {plan.get('target')} "
                f"({plan.get('target_source')}) · R:R {plan.get('rr')}"
            )
    lines.append("═" * 72)
    return "\n".join(lines)
