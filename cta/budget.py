"""Subrequest budgeting — the reason this project kept reporting zero traders.

The constraint nobody designed for
----------------------------------
A Cloudflare Worker invocation on the free plan may make **50 subrequests**
(outbound `fetch` calls) and then dies with ``Too many subrequests by single
Worker invocation``. It may make 1440 cron invocations per day.

The original ``profile_watchlist`` did this in a single invocation::

    1 leaderboard fetch + 300 candidates x venue.fills()

and every ``http_json`` retries twice, so the worst case is 1 + 300*3 = 901
subrequests — **18x the ceiling**, before a single position is polled. The
exception landed inside ``except Exception: continue``, so the watchlist came
out empty and the screen said "0 متداول" with three green platforms.

That is the worst possible failure: the system was *not* idle, it was being
killed mid-cycle, and it reported the kill as "no candidates found".

Design principles that follow
-----------------------------
1. **Never spend a request you cannot afford.** Check the budget *before* the
   call, using a worst-case cost that includes retries.
2. **Persist progress every invocation.** A cycle that is killed halfway must
   leave a resume point, or the next invocation starts from zero forever and
   never finishes — an infinite loop that looks like idling.
3. **Record why work was skipped.** "Skipped for budget" and "found nothing"
   must never look the same, because only one of them is a bug.
4. **Never let a caller silently continue past an exhausted budget.** Return a
   countable reason, not an empty list.

The budget is per-invocation, so ``ResumableProfiler`` spans invocations: each
cron run scores as many candidates as it can afford, persists the rest to the
Durable Object, and the next run continues where the last one stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# The Workers free-plan ceiling. Paid plans allow 10,000. Kept as a constant
# rather than a config default because exceeding it is not a tuning choice: the
# invocation is terminated by the platform, and the code never finds out.
FREE_PLAN_SUBREQUESTS = 50

# Headroom reserved for work that is not discovery: the health probes, the
# Telegram poll (getUpdates + any sendMessage), and the position snapshot.
# Without this the profiler happily eats the whole budget and the bot which
# shares the invocation cannot reply.
DEFAULT_RESERVE = 12


class BudgetExhausted(Exception):
    """Raised when a call would exceed the invocation's subrequest ceiling.

    Deliberately an exception rather than a `False` return: a boolean is easy to
    ignore in a loop that already has `except Exception: continue`, which is
    exactly how the 901-subrequest bug became an empty watchlist nobody noticed.
    """


@dataclass
class SubrequestBudget:
    """Counts outbound fetches against a hard ceiling.

    Costs are *pessimistic*: a call that may retry twice is charged three
    times. Optimistic accounting is how you exceed a hard limit while your
    own bookkeeping says you are fine.

    Example
    -------
    >>> b = SubrequestBudget(limit=50, reserve=10)
    >>> b.affordable(3)
    True
    >>> b.spend(3)
    >>> b.remaining
    37
    """

    limit: int = FREE_PLAN_SUBREQUESTS
    reserve: int = DEFAULT_RESERVE
    spent: int = 0
    refused: int = 0
    refused_reasons: list[str] = field(default_factory=list)

    @property
    def ceiling(self) -> int:
        """The most this component may spend, after the shared reserve."""
        return max(0, self.limit - self.reserve)

    @property
    def remaining(self) -> int:
        return max(0, self.ceiling - self.spent)

    def affordable(self, cost: int = 1) -> bool:
        """True when `cost` more requests fit. Does not spend."""
        return self.spent + max(0, cost) <= self.ceiling

    def spend(self, cost: int = 1) -> None:
        """Charge the budget, or raise `BudgetExhausted`."""
        cost = max(0, cost)
        if not self.affordable(cost):
            self.refused += 1
            reason = f"needed {cost}, {self.remaining} left"
            if len(self.refused_reasons) < 8:
                self.refused_reasons.append(reason)
            raise BudgetExhausted(reason)
        self.spent += cost

    def try_spend(self, cost: int = 1) -> bool:
        """Charge the budget, returning False instead of raising.

        For call sites that genuinely can skip the work (an optional snapshot),
        as opposed to call sites whose silence would be a lie (discovery).
        """
        try:
            self.spend(cost)
            return True
        except BudgetExhausted:
            return False

    def report(self) -> dict[str, Any]:
        """A JSON-safe summary for `/prove`, `/health` and the trace."""
        return {
            "limit": self.limit,
            "reserve": self.reserve,
            "ceiling": self.ceiling,
            "spent": self.spent,
            "remaining": self.remaining,
            "refused": self.refused,
            "refused_reasons": list(self.refused_reasons),
        }


def cost_of_fetch(retries: int = 2) -> int:
    """Worst-case subrequests for one `http_json` call.

    `http_json` uses `for attempt in range(retries + 1)`, so a call that fails
    and is retried costs `retries + 1`. Charging only 1 would under-count by 3x
    on exactly the path (a flaky venue) where the budget matters most.
    """
    return max(1, int(retries) + 1)
