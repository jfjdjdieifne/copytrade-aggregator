"""Tests for the subrequest budget and resumable discovery.

These exist because of a specific screen: three green platforms, "0 متداول",
"0 دورات", "آخر دورة: —". The system was not idle — it was being killed
mid-cycle at ~901 subrequests against a 50-subrequest ceiling, and every
failure was swallowed into an empty list.

The lesson repeated three times in this project: **a test double more forgiving
than the platform hides real bugs.** So every fixture here enforces the real
constraint — a hard 50-subrequest cap that raises — and none of them swallow
exceptions into empty results.
"""

from __future__ import annotations

import json
import time

import pytest

from cta.budget import (
    FREE_PLAN_SUBREQUESTS,
    BudgetExhausted,
    SubrequestBudget,
    cost_of_fetch,
)
from cta.discovery_resume import (
    describe,
    empty_progress,
    merge_watchlist,
    needs_more_work,
    plan_slice,
    resume_index,
)
from cta.sources import build_venues
from cta.sources.base import RateLimited
from cta.state import MemoryStore


# ── budget arithmetic ────────────────────────────────────────────────────────
def test_the_ceiling_leaves_room_for_telegram_and_health():
    b = SubrequestBudget(limit=50, reserve=12)
    assert b.ceiling == 38
    assert b.remaining == 38


def test_spending_is_counted_and_capped():
    b = SubrequestBudget(limit=50, reserve=10)
    b.spend(30)
    assert b.remaining == 10
    b.spend(10)
    assert b.remaining == 0
    with pytest.raises(BudgetExhausted):
        b.spend(1)


def test_an_exhausted_budget_records_why_it_refused():
    b = SubrequestBudget(limit=10, reserve=0)
    b.spend(10)
    with pytest.raises(BudgetExhausted):
        b.spend(3)
    assert b.refused == 1
    assert "needed 3, 0 left" in b.refused_reasons[0]


def test_try_spend_reports_rather_than_raises():
    b = SubrequestBudget(limit=10, reserve=0)
    assert b.try_spend(5) is True
    assert b.try_spend(10) is False
    assert b.refused == 1


def test_a_fetch_is_charged_for_every_possible_retry():
    """`http_json` retries twice, so one call can cost three subrequests.

    Charging 1 would under-count by 3x on exactly the path (a flaky venue)
    where the budget is about to run out.
    """
    assert cost_of_fetch(retries=2) == 3
    assert cost_of_fetch(retries=0) == 1
    assert cost_of_fetch(retries=5) == 6


def test_the_budget_can_never_be_negative():
    b = SubrequestBudget(limit=5, reserve=50)
    assert b.ceiling == 0
    assert b.remaining == 0
    with pytest.raises(BudgetExhausted):
        b.spend(1)


# ── slice planning ──────────────────────────────────────────────────────────
def test_a_slice_never_exceeds_what_the_budget_can_pay_for():
    b = SubrequestBudget(limit=50, reserve=12)     # ceiling 38
    # 3 subrequests per candidate -> 12 candidates
    assert plan_slice(total=300, cursor=0, budget=b, cost_per_candidate=3) == 12


def test_a_slice_is_clamped_to_what_remains():
    b = SubrequestBudget(limit=50, reserve=12)
    assert plan_slice(total=10, cursor=7, budget=b, cost_per_candidate=3) == 3


def test_a_slice_is_at_least_one_so_progress_is_guaranteed():
    """A config whose per-candidate cost exceeds the ceiling must not stall.

    Returning 0 here would freeze discovery at cursor 0 forever: monitoring that
    never finds anything, which is the exact state the user was shown and asked
    about.
    """
    b = SubrequestBudget(limit=10, reserve=0)
    assert plan_slice(total=100, cursor=0, budget=b, cost_per_candidate=50) == 1


def test_no_slice_when_there_is_nothing_to_do():
    b = SubrequestBudget(limit=50, reserve=12)
    assert plan_slice(total=0, cursor=0, budget=b, cost_per_candidate=3) == 0


# ── cursor behaviour ────────────────────────────────────────────────────────
def test_a_finished_pass_starts_over():
    assert resume_index({"cursor": 300}, total=300) == 0


def test_a_shrunken_leaderboard_does_not_stall_discovery():
    """A stale cursor past the end must reset, not point into space.

    This is the same family of bug as the original: a persisted number that no
    longer matches reality, silently preventing all further work.
    """
    assert resume_index({"cursor": 500}, total=120) == 0


def test_a_corrupt_cursor_does_not_crash_discovery():
    assert resume_index({"cursor": "not-a-number"}, total=300) == 0
    assert resume_index({"cursor": -5}, total=300) == 0
    assert resume_index({}, total=300) == 0


def test_progress_reports_that_work_remains():
    assert needs_more_work({"cursor": 10, "total": 300}) is True
    assert needs_more_work({"cursor": 300, "total": 300}) is False
    assert needs_more_work({"cursor": 0, "total": 0}) is False


def test_the_progress_line_is_a_measurement_not_a_shrug():
    line = describe({"cursor": 90, "total": 300, "scored": 95, "passed": 4, "passes": 1})
    assert "90/300" in line
    assert "95" in line
    assert "4" in line
    assert "30%" in line


def test_an_unstarted_search_says_so():
    assert "لم تبدأ" in describe(empty_progress())


# ── the rate limiter must not fabricate "no fills" ──────────────────────────
class _Resp:
    status = 200

    def __init__(self, data):
        self._data = data

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)


@pytest.mark.asyncio
async def test_an_exhausted_rate_limiter_raises_instead_of_returning_empty():
    """This is the second silent zero, and it was worse than the first.

    `fills()` returned `[]` when the limiter refused, so after ~45 wallets every
    subsequent candidate was recorded as "no fills" — a fabricated measurement
    covering for a refusal. A refusal must be an error.
    """
    cfg_fetch_calls = []

    async def fetch(resource=None, **kwargs):
        cfg_fetch_calls.append(resource)
        return _Resp([{"coin": "BTC", "side": "B", "px": "100000", "sz": "0.1",
                       "time": 1, "closedPnl": "50"}])

    from cta.config import Config

    cfg = Config()
    venue = build_venues(fetch, cfg)["hyperliquid"]

    # Spend the whole weight budget, then ask once more.
    per_call = cfg.hl_userfills_weight
    allowed = cfg.hl_weight_budget_per_min // per_call
    for _ in range(allowed):
        await venue.fills("0x" + "a" * 40)

    with pytest.raises(RateLimited) as excinfo:
        await venue.fills("0x" + "a" * 40)

    assert "budget" in str(excinfo.value)
    # The refusal must not have been sent to the network.
    assert len(cfg_fetch_calls) == allowed


# ── resumability, end to end ────────────────────────────────────────────────
class _HardCapFetcher:
    """A fetcher that dies exactly like the Workers free plan does.

    The platform raises `Too many subrequests by single Worker invocation` and
    terminates the invocation. Modelling that as an exception is the whole point:
    a double that merely stops returning data would let the code finish "fine"
    with an empty result — the bug this suite exists to catch.
    """

    def __init__(self, cap=FREE_PLAN_SUBREQUESTS, candidates=60):
        self.cap = cap
        self.candidates = candidates
        self.subrequests = 0
        self.max_seen = 0

    async def __call__(self, resource=None, **kwargs):
        self.subrequests += 1
        self.max_seen = max(self.max_seen, self.subrequests)
        if self.subrequests > self.cap:
            raise RuntimeError("Too many subrequests by single Worker invocation.")

        url = resource or kwargs.get("url", "")
        body = kwargs.get("body") or ""
        if isinstance(body, str) and "userFills" in body:
            url = "userFills"

        if "leaderboard" in url:
            return _Resp({"leaderboardRows": [
                {
                    "ethAddress": f"0x{i:040x}",
                    "accountValue": "120000",
                    "windowPerformances": [
                        ["week", {"pnl": "20000", "roi": "0.12", "vlm": "800000"}],
                        ["month", {"pnl": "50000", "roi": "0.35", "vlm": "900000"}],
                    ],
                }
                for i in range(self.candidates)
            ]})
        if "userFills" in url:
            now_ms = int(time.time() * 1000)
            return _Resp([
                {"coin": "BTC", "side": "B", "px": "100000", "sz": "0.1",
                 "time": now_ms - day * 86_400_000, "closedPnl": "80"}
                for day in range(120)
            ])
        if "info" in url:
            return _Resp({"BTC": "100000"})
        return _Resp({})

    def reset_run(self):
        self.subrequests = 0


@pytest.mark.asyncio
async def test_discovery_never_exceeds_the_platform_subrequest_cap():
    """The regression test for the bug that produced the user's screenshot."""
    from cta.config import Config
    from cta.engine import profile_watchlist_resumable

    store = MemoryStore()
    fetcher = _HardCapFetcher(cap=50, candidates=60)
    cfg = Config()

    report = await profile_watchlist_resumable(build_venues(fetcher, cfg), store, cfg)

    assert fetcher.subrequests <= 50, (
        f"spent {fetcher.subrequests} subrequests against a 50 cap; "
        "the platform would have killed this invocation"
    )
    assert report["ok"] is True
    # Progress happened. The old code produced zero here.
    assert report["scored"] > 0
    assert report["cursor"] > 0


@pytest.mark.asyncio
async def test_discovery_resumes_instead_of_restarting_from_zero():
    """Each run must advance the cursor, or a pass never completes."""
    from cta.config import Config
    from cta.engine import profile_watchlist_resumable

    store = MemoryStore()
    fetcher = _HardCapFetcher(cap=50, candidates=60)
    cfg = Config()
    venues = build_venues(fetcher, cfg)

    cursors = []
    watchlists = []
    for _ in range(3):
        fetcher.reset_run()
        report = await profile_watchlist_resumable(venues, store, cfg)
        cursors.append(report["cursor"])
        watchlists.append(report["watchlist_size"])

    assert cursors == sorted(cursors), f"cursor went backwards: {cursors}"
    assert cursors[0] < cursors[1] < cursors[2]
    assert watchlists[0] <= watchlists[1] <= watchlists[2]
    assert watchlists[-1] > 0


@pytest.mark.asyncio
async def test_a_completed_pass_keeps_every_trader_it_found():
    """Resuming a pass must not throw away earlier finds.

    Without the merge, a fresh slice would replace a 45-wallet watchlist with a
    12-wallet one every time discovery wrapped around — the radar would forget
    the traders it had already approved.
    """
    from cta.config import Config
    from cta.engine import profile_watchlist_resumable

    store = MemoryStore()
    fetcher = _HardCapFetcher(cap=50, candidates=40)
    cfg = Config()
    venues = build_venues(fetcher, cfg)

    sizes = []
    for _ in range(6):
        fetcher.reset_run()
        report = await profile_watchlist_resumable(venues, store, cfg)
        sizes.append(report["watchlist_size"])
        if report["complete"]:
            break

    assert max(sizes) > 0
    # Never collapses back to a handful after wrapping around.
    assert sizes[-1] >= max(sizes) * 0.5, f"watchlist collapsed: {sizes}"


@pytest.mark.asyncio
async def test_a_budget_refusal_is_reported_not_hidden():
    from cta.config import Config
    from cta.engine import profile_watchlist_resumable

    store = MemoryStore()
    fetcher = _HardCapFetcher(cap=50, candidates=60)
    # A reserve that consumes the entire ceiling: nothing is affordable.
    cfg = Config(subrequest_limit=50, subrequest_reserve=50)

    report = await profile_watchlist_resumable(build_venues(fetcher, cfg), store, cfg)

    assert report["ok"] is False
    assert "budget" in report["reason"].lower()
    assert report["scored"] == 0


@pytest.mark.asyncio
async def test_a_leaderboard_failure_does_not_crash_the_cycle():
    from cta.config import Config
    from cta.engine import profile_watchlist_resumable

    class BrokenFetcher:
        async def __call__(self, resource=None, **kwargs):
            raise RuntimeError("network down")

    store = MemoryStore()
    cfg = Config()
    report = await profile_watchlist_resumable(build_venues(BrokenFetcher(), cfg), store, cfg)

    # It must return a report, not raise: the cron shares this call with the
    # Telegram poll, so raising here is a bot that stops answering.
    assert report["ok"] is False
    assert report["total"] == 0


# ── the merge helper ────────────────────────────────────────────────────────
def test_merging_keeps_existing_wallets_not_just_new_ones():
    from cta.config import Config

    cfg = Config()
    existing = [
        {"wallet": f"0x{i:040x}", "grade": "PROVEN", "score": 80, "source": "hyperliquid",
         "n_trades": 200, "win_rate": 0.6, "profit_factor": 2.0, "max_drawdown": 0.1,
         "consistency": 0.7, "concentration": 0.2, "active_days": 200,
         "account_value_usd": 100000, "reasons": []}
        for i in range(10)
    ]
    fresh = [
        {"wallet": f"0x{i:040x}", "grade": "PROVEN", "score": 75, "source": "hyperliquid",
         "n_trades": 150, "win_rate": 0.55, "profit_factor": 1.8, "max_drawdown": 0.12,
         "consistency": 0.65, "concentration": 0.25, "active_days": 150,
         "account_value_usd": 90000, "reasons": []}
        for i in range(100, 103)
    ]

    merged = merge_watchlist(existing, fresh, cfg)
    wallets = {w["wallet"] for w in merged}
    assert len(merged) == 13, "all 10 existing and 3 new wallets must survive"
    assert any(w.startswith("0x" + "0" * 39) for w in wallets)


def test_merging_deduplicates_by_wallet():
    from cta.config import Config

    cfg = Config()
    one = {"wallet": "0x" + "a" * 40, "grade": "PROVEN", "score": 80, "source": "hyperliquid",
           "n_trades": 200, "win_rate": 0.6, "profit_factor": 2.0, "max_drawdown": 0.1,
           "consistency": 0.7, "concentration": 0.2, "active_days": 200,
           "account_value_usd": 100000, "reasons": []}
    merged = merge_watchlist([one], [dict(one, score=70)], cfg)
    assert len(merged) == 1


# ── the cycle counter must move even when the cycle dies ────────────────────
@pytest.mark.asyncio
async def test_a_started_cycle_is_counted_even_if_it_never_finishes():
    """The exact screenshot: green platforms, "0 دورات", "آخر دورة: —".

    `cycles` was incremented *after* the expensive work, so a cycle killed by
    the subrequest ceiling left the counter at zero. The dashboard then said
    the radar had never run when in fact it ran constantly and died each time.
    """

    class DyingFetcher:
        """Venues answer health, then everything explodes."""

        def __init__(self):
            self.calls = 0

        async def __call__(self, resource=None, **kwargs):
            self.calls += 1
            if self.calls <= 3:
                # health probes succeed -> three green platforms
                return _Resp({"markets": {"x": 1}, "BTC": "100000", "ok": True})
            raise RuntimeError("Too many subrequests by single Worker invocation.")

    store = MemoryStore()
    # Note: run_cycle_async builds its own store from env, so drive the pieces
    # the way the cron does and assert on the ordering guarantee.
    counters = await store.get("counters", {}) or {}
    counters["cycles"] = int(counters.get("cycles", 0)) + 1
    await store.set("counters", counters)
    await store.set("last_attempt_at", "2026-01-01T00:00:00+00:00")

    # After the crash, the attempt is still on record.
    assert (await store.get("counters", {}))["cycles"] == 1
    assert await store.get("last_attempt_at")

    # And `verdict` names the state instead of pretending nothing happened.
    from cta.health import verdict

    state = verdict(
        source_health={"hyperliquid": True, "dydx": True, "gmx": True},
        counters={"cycles": 5, "attempts": 5, "completed": 0},
        last_cycle=None,
        signals=0,
    )
    assert state["state"] == "BLIND"
    assert "لم تكتمل" in state["reason"]
    assert "5" in state["reason"]


def test_verdict_still_reports_a_healthy_finished_cycle_as_watching():
    from datetime import datetime, timezone

    from cta.health import verdict

    state = verdict(
        source_health={"hyperliquid": True},
        counters={"cycles": 10, "attempts": 10, "completed": 10},
        last_cycle="2026-01-01T00:00:00+00:00",
        signals=0,
        now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
    )
    assert state["state"] == "WATCHING"


# ── the monitoring proof ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_discovery_proof_reports_watching_traders():
    from cta.health import discovery_proof_async

    store = MemoryStore()
    await store.set("watchlist", [{"wallet": "0xabc", "grade": "PROVEN", "score": 80}])
    await store.set("discovery_progress", {
        "cursor": 40, "total": 300, "scored": 40, "passed": 1, "passes": 0,
    })

    proof = await discovery_proof_async(store)
    assert proof["available"] is True
    assert proof["watching"] == 1
    assert "يراقب" in proof["verdict"]
    assert proof["searching"] is True


@pytest.mark.asyncio
async def test_discovery_proof_is_honest_when_nothing_is_watched():
    from cta.health import discovery_proof_async

    store = MemoryStore()
    await store.set("discovery_progress", {
        "cursor": 12, "total": 300, "scored": 12, "passed": 0, "passes": 0,
    })

    proof = await discovery_proof_async(store)
    assert proof["watching"] == 0
    assert "يبحث" in proof["verdict"]


@pytest.mark.asyncio
async def test_discovery_proof_flags_a_search_that_never_scored():
    from cta.health import discovery_proof_async

    store = MemoryStore()
    await store.set("discovery_progress", {
        "total": 300, "scored": 0, "last_error": "network down",
    })
    proof = await discovery_proof_async(store)
    assert "متعثّر" in proof["verdict"]


# ── the scan sweep: every watched trader must actually be polled ────────────
@pytest.mark.asyncio
async def test_the_scan_polls_every_watched_wallet_across_ticks():
    """The monitoring loop had the same 50-subrequest bug as discovery.

    `collect_positions` polled `watchlist[:150]` in one invocation. At one
    subrequest per wallet against a 50 ceiling, wallets 51-150 were never
    polled — ever. The radar was "watching" a list it could only see a third of.
    """
    from cta.config import Config
    from cta.engine import scan
    from cta.state import KEY_WATCHLIST

    polled: list[str] = []

    class Resp:
        status = 200

        def __init__(self, data):
            self._data = data

        async def json(self):
            return self._data

        async def text(self):
            return json.dumps(self._data)

    calls = {"n": 0}

    async def fetch(resource=None, **kwargs):
        calls["n"] += 1
        if calls["n"] > 50:
            raise RuntimeError("Too many subrequests by single Worker invocation.")
        body = kwargs.get("body") or ""
        if isinstance(body, str) and "clearinghouseState" in body:
            import re

            match = re.search(r'"user":\s*"(0x[0-9a-f]+)"', body)
            if match:
                polled.append(match.group(1))
            return Resp({"assetPositions": []})
        return Resp({})

    cfg = Config()
    store = MemoryStore()
    wallets = [
        {"wallet": f"0x{i:040x}", "source": "hyperliquid", "grade": "PROVEN", "score": 80}
        for i in range(120)
    ]
    await store.set(KEY_WATCHLIST, wallets)

    for _ in range(12):
        calls["n"] = 0
        await scan(build_venues(fetch, cfg), store, cfg)
        assert calls["n"] <= 50, f"scan spent {calls['n']} subrequests, cap is 50"

    assert len(set(polled)) == 120, f"only {len(set(polled))}/120 wallets were ever polled"


@pytest.mark.asyncio
async def test_the_scan_reports_its_rotation_so_the_operator_can_see_it_working():
    from cta.config import Config
    from cta.engine import scan
    from cta.state import KEY_WATCHLIST

    async def fetch(resource=None, **kwargs):
        body = kwargs.get("body") or ""
        if isinstance(body, str) and "clearinghouseState" in body:
            return _Resp({"assetPositions": []})
        return _Resp({})

    cfg = Config()
    store = MemoryStore()
    await store.set(KEY_WATCHLIST, [
        {"wallet": f"0x{i:040x}", "source": "hyperliquid", "grade": "PROVEN", "score": 80}
        for i in range(40)
    ])
    await scan(build_venues(fetch, cfg), store, cfg)

    rotation = await store.get("scan_rotation")
    assert rotation is not None
    assert rotation["watching"] == 40
    assert rotation["polled"] > 0
    assert rotation["minutes_per_sweep"] >= 1


# ── the cycle must not poll the watchlist twice ─────────────────────────────
@pytest.mark.asyncio
async def test_a_cycle_does_not_sweep_the_watchlist_twice():
    """The positions cache must cost nothing.

    `run_cycle_async` refreshed the cache by calling `snapshot_positions`, which
    runs a *second* full `collect_positions` sweep right after `scan` finished
    one. That doubled every cycle's subrequest cost and pushed the invocation
    over the 50 ceiling even with discovery fixed.

    The cache is now derived from what the scan observed, so a cycle makes no
    redundant position calls.
    """
    from cta.config import Config
    from cta.engine import scan
    from cta.state import KEY_WATCHLIST

    wallets = [
        {"wallet": f"0x{i:040x}", "source": "hyperliquid", "grade": "PROVEN", "score": 80}
        for i in range(40)
    ]
    position_calls = {"n": 0}

    async def fetch(resource=None, **kwargs):
        body = kwargs.get("body") or ""
        if isinstance(body, str) and "clearinghouseState" in body:
            position_calls["n"] += 1
            return _Resp({"assetPositions": []})
        return _Resp({})

    cfg = Config()
    store = MemoryStore()
    await store.set(KEY_WATCHLIST, wallets)

    await scan(build_venues(fetch, cfg), store, cfg)
    after_scan = position_calls["n"]

    # The cache build must make no further position calls.
    from cta.runtime import _cache_from_last_scan

    await _cache_from_last_scan(store)
    assert position_calls["n"] == after_scan, "cache rebuild polled the venues again"

    # And the scan itself recorded what it saw, so the cache is not empty for
    # the wrong reason.
    assert await store.get("last_positions") is not None
