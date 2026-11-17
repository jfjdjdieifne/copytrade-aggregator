"""The trade lifecycle: one idea, followed from entry to exit.

Why this exists
---------------
The radar had an entry detector and nothing else. It found a consensus, sent a
message, and forgot about it. Measured over 150 cycles that produced 91 alerts
for 10 distinct ideas — 21 of them for a single BTC short — while the operator
was left holding a position with no idea whether the traders who created it
were still in it.

An alert is not a trade. A trade has a middle and an end, and the end is where
the money is decided. So the unit of delivery here is not the signal, it is the
*idea*: one thesis on one coin in one direction, tracked until the people who
created it leave.

Three rules shape everything below.

1. One idea at a time per (coin, side). A second consensus on the same idea is
   not a new trade, it is confirmation. It updates the idea; it does not page
   the operator again.

2. An outage must never look like an exit. If we cannot see a wallet, that is
   ignorance, not information. A venue going down during a pullback would
   otherwise trigger "the pros are leaving" on every open idea at once — the
   single most expensive false signal this system could produce.

3. Exits are reported, not commanded. Whether the traders' leaving carries
   information is an empirical question this simulator cannot settle: in a
   random walk it does not. So the message states what happened and what the
   position is worth, and the operator decides.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from .models import Signal

UTC = timezone.utc

KEY_IDEAS = "open_ideas"
KEY_LEDGER = "idea_ledger"

# A member is treated as still in while they hold at least this fraction of the
# exposure they had when the idea opened.
STILL_IN_FRACTION = 0.5
# Fraction of *visible* members who must have left before we call an exit.
DEFAULT_EXIT_FRACTION = 0.5


def _member_size(member: dict) -> float:
    try:
        return abs(float(member.get("size_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:g}"


class Idea:
    """One trade thesis, from the first consensus to the last man out."""

    def __init__(self, signal: Signal, now: datetime) -> None:
        self.coin = signal.coin
        self.side = signal.side
        self.key = f"{signal.coin}:{signal.side}"
        self.entry = signal.plan.entry if signal.plan else signal.entry_center
        self.stop = signal.plan.stop if signal.plan else None
        self.target = signal.plan.target if signal.plan else None
        self.opened_at = now
        self.updated_at = now

        # wallet -> size at the moment they joined
        self.members: dict[str, float] = {}
        self.sources: set[str] = set(signal.sources or [])
        # wallet -> venue. Needed to tell "this trader closed" apart from "we
        # could not see this venue this cycle".
        self.member_sources: dict[str, str] = {}
        self.qualities: dict[str, str] = {}
        self.signal_ids: list[str] = []
        self.peak_score = int(signal.score or 0)
        self.entries: dict[str, float] = {}

        self.absorb(signal, now)

        self.best_price = self.entry or 0.0
        self.worst_price = self.entry or 0.0
        # Wallets we have positively observed holding exposure since the idea
        # opened. A member we have never seen cannot be declared gone: on the
        # cycle an idea opens, the exposure map may simply not have been polled
        # yet, and reading that as "they all left" produced an ENTER and an
        # EXIT in the same breath.
        self.seen: set[str] = set()
        self.exited_at: Optional[datetime] = None
        self.exit_price: Optional[float] = None
        self.exit_reason: Optional[str] = None

    # ── growth ──────────────────────────────────────────────────────────────
    def absorb(self, signal: Signal, now: datetime) -> None:
        """A later consensus on the same idea: confirmation, not a new trade."""
        self.updated_at = now
        if signal.signal_id and signal.signal_id not in self.signal_ids:
            self.signal_ids.append(signal.signal_id)
        self.sources.update(signal.sources or [])
        self.peak_score = max(self.peak_score, int(signal.score or 0))
        for m in signal.members or []:
            wallet = str(m.get("wallet") or "")
            if not wallet:
                continue
            size = _member_size(m)
            # Keep the largest size seen: a member adding to a position is
            # still in, and shrinking later is what we are watching for.
            self.members[wallet] = max(self.members.get(wallet, 0.0), size)
            source = str(m.get("source") or "")
            if source:
                self.member_sources[wallet] = source
            self.qualities[wallet] = str(m.get("quality") or "WATCH")
            try:
                self.entries[wallet] = float(m.get("entry") or 0.0)
            except (TypeError, ValueError):
                pass
        # A later, tighter consensus may offer a better level. Never move the
        # stop further from entry — that would silently increase risk.
        if signal.plan is not None:
            if self.stop is None or _closer(signal.plan.stop, self.stop, self.entry, self.side):
                self.stop = signal.plan.stop
            if self.target is None:
                self.target = signal.plan.target

    def observe_price(self, price: float) -> None:
        if price <= 0:
            return
        if self.best_price <= 0:
            self.best_price = price
        if self.worst_price <= 0:
            self.worst_price = price
        if self.side == "LONG":
            self.best_price = max(self.best_price, price)
            self.worst_price = min(self.worst_price, price)
        else:
            self.best_price = min(self.best_price, price)
            self.worst_price = max(self.worst_price, price)

    # ── measurement ─────────────────────────────────────────────────────────
    def r_of(self, price: Optional[float]) -> Optional[float]:
        """Unrealised result in R (multiples of the risk taken)."""
        if price is None or not self.entry or not self.stop:
            return None
        risk = abs(self.entry - self.stop)
        if risk <= 0:
            return None
        if self.side == "LONG":
            return (price - self.entry) / risk
        return (self.entry - price) / risk

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "coin": self.coin,
            "side": self.side,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "opened_at": self.opened_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "members": self.members,
            "member_sources": self.member_sources,
            "qualities": self.qualities,
            "entries": self.entries,
            "sources": sorted(self.sources),
            "signal_ids": self.signal_ids,
            "peak_score": self.peak_score,
            "best_price": self.best_price,
            "worst_price": self.worst_price,
            "seen": sorted(self.seen),
            "exited_at": self.exited_at.isoformat() if self.exited_at else None,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["Idea"]:
        if not isinstance(raw, dict) or not raw.get("coin"):
            return None
        obj = cls.__new__(cls)
        obj.coin = str(raw.get("coin") or "")
        obj.side = str(raw.get("side") or "")
        obj.key = str(raw.get("key") or f"{obj.coin}:{obj.side}")
        obj.entry = raw.get("entry")
        obj.stop = raw.get("stop")
        obj.target = raw.get("target")
        obj.opened_at = _parse_dt(raw.get("opened_at"))
        obj.updated_at = _parse_dt(raw.get("updated_at")) or obj.opened_at
        obj.members = dict(raw.get("members") or {})
        obj.member_sources = dict(raw.get("member_sources") or {})
        obj.qualities = dict(raw.get("qualities") or {})
        obj.entries = dict(raw.get("entries") or {})
        obj.sources = set(raw.get("sources") or [])
        obj.signal_ids = list(raw.get("signal_ids") or [])
        obj.peak_score = int(raw.get("peak_score") or 0)
        obj.best_price = float(raw.get("best_price") or 0.0)
        obj.worst_price = float(raw.get("worst_price") or 0.0)
        obj.seen = set(raw.get("seen") or [])
        obj.exited_at = _parse_dt(raw.get("exited_at"))
        obj.exit_price = raw.get("exit_price")
        obj.exit_reason = raw.get("exit_reason")
        return obj


def _closer(candidate: Optional[float], current: Optional[float],
            entry: Optional[float], side: str) -> bool:
    """Is `candidate` a tighter stop than `current` for this direction?"""
    if candidate is None or entry is None:
        return False
    if current is None:
        return True
    distance_new = abs(entry - candidate)
    distance_old = abs(entry - current)
    return distance_new < distance_old


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# ── reconciliation ──────────────────────────────────────────────────────────
def exposure_map(positions: Iterable[Any]) -> dict[tuple[str, str], float]:
    """(wallet, coin) -> absolute USD exposure, as observed this cycle."""
    out: dict[tuple[str, str], float] = {}
    for p in positions:
        wallet = str(getattr(p, "wallet", "") or "")
        coin = str(getattr(p, "coin", "") or "")
        if not wallet or not coin:
            continue
        try:
            size = abs(float(getattr(p, "size_usd", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
        out[(wallet, coin)] = out.get((wallet, coin), 0.0) + size
    return out


def reconcile(
    ideas: dict[str, Idea],
    signals: Sequence[Signal],
    exposure: dict[tuple[str, str], float],
    visible_sources: set[str],
    prices: dict[str, float],
    now: datetime,
    exit_fraction: float = DEFAULT_EXIT_FRACTION,
    max_age_h: float = 72.0,
) -> list[dict[str, Any]]:
    """Fold this cycle's observations into the live ideas.

    Returns the events worth delivering. Everything else — a confirmed idea, a
    quiet cycle — returns nothing, which is the whole point: the operator is
    paged when something changes, not every time the radar looks.
    """
    events: list[dict[str, Any]] = []

    # 1. Absorb every signal into its idea.
    for sig in signals:
        key = f"{sig.coin}:{sig.side}"
        price = prices.get(sig.coin)
        existing = ideas.get(key)
        if existing is None or existing.exited_at is not None:
            idea = Idea(sig, now)
            ideas[key] = idea
            if price:
                idea.observe_price(price)
            events.append({
                "kind": "enter",
                "idea": idea,
                "signal": sig,
                "n": len(idea.members),
            })
        else:
            before = len(existing.members)
            existing.absorb(sig, now)
            if price:
                existing.observe_price(price)
            if len(existing.members) > before:
                events.append({
                    "kind": "strengthen",
                    "idea": existing,
                    "signal": sig,
                    "n": len(existing.members),
                    "joined": len(existing.members) - before,
                })

    # 2. Check every open idea for members leaving, or for old age.
    for key, idea in list(ideas.items()):
        if idea.exited_at is not None:
            continue
        price = prices.get(idea.coin)
        if price:
            idea.observe_price(price)

        still_in, gone, unseen = _count_members(
            idea, exposure, visible_sources
        )
        total_seen = still_in + gone
        if total_seen == 0:
            # Nobody visible. That is an outage or a blind spot — never an
            # exit. Rule 2 above.
            _expire_if_old(idea, now, events, max_age_h)
            continue

        left_fraction = gone / total_seen
        if gone >= 1 and left_fraction >= exit_fraction:
            idea.exited_at = now
            idea.exit_price = price
            idea.exit_reason = "members_left"
            events.append({
                "kind": "exit",
                "idea": idea,
                "left": gone,
                "remaining": still_in,
                "unseen": unseen,
                "r": idea.r_of(price),
            })
            continue

        _expire_if_old(idea, now, events, max_age_h)

    return events


def _count_members(
    idea: Idea,
    exposure: dict[tuple[str, str], float],
    visible_sources: set[str],
) -> tuple[int, int, int]:
    """Split the idea's members into (still in, gone, unseen).

    `unseen` is the critical category. A wallet whose venue did not report this
    cycle is unknown, not absent — counting it as absent would turn any venue
    outage into a mass exit alert.
    """
    still_in = 0
    gone = 0
    unseen = 0
    for wallet, size_at_join in idea.members.items():
        source = _source_of(idea, wallet)
        if visible_sources and source and source not in visible_sources:
            unseen += 1
            continue
        current = exposure.get((wallet, idea.coin))
        if current is None:
            # No row at all. Only a wallet we have already watched can be said
            # to have left; otherwise this is a cycle where we simply have not
            # looked yet, and calling it an exit would fire an ENTER and an
            # EXIT in the same breath.
            if wallet in idea.seen:
                gone += 1
            else:
                unseen += 1
            continue
        idea.seen.add(wallet)
        threshold = max(size_at_join, 1.0) * STILL_IN_FRACTION
        if current >= threshold:
            still_in += 1
        else:
            gone += 1
    return still_in, gone, unseen


def _source_of(idea: Idea, wallet: str) -> Optional[str]:
    """Which venue a member belongs to, if we recorded one."""
    return idea.member_sources.get(wallet)


def _expire_if_old(idea: Idea, now: datetime,
                   events: list[dict[str, Any]], max_age_h: float) -> None:
    age = now - idea.opened_at
    if max_age_h and age > timedelta(hours=max_age_h):
        idea.exited_at = now
        idea.exit_reason = "expired"
        events.append({
            "kind": "expired",
            "idea": idea,
            "age_h": age.total_seconds() / 3600.0,
            "r": idea.r_of(idea.exit_price),
        })


# ── ledger ──────────────────────────────────────────────────────────────────
def ledger_entry(idea: Idea, exit_price: Optional[float],
                 now: datetime) -> dict[str, Any]:
    """One closed idea, attributed.

    Attribution is the point: without a record of *which* sources, grades and
    conditions produced a result, the system can only have opinions. After a
    hundred of these it can have evidence.
    """
    r = idea.r_of(exit_price)
    peak_r = idea.r_of(idea.best_price)
    grades = list(idea.qualities.values())
    return {
        "key": idea.key,
        "coin": idea.coin,
        "side": idea.side,
        "opened_at": idea.opened_at.isoformat(),
        "closed_at": now.isoformat(),
        "held_min": round((now - idea.opened_at).total_seconds() / 60.0, 1),
        "sources": sorted(idea.sources),
        "n_members": len(idea.members),
        "n_sources": len(idea.sources),
        "peak_score": idea.peak_score,
        "grades": grades,
        "entry": idea.entry,
        "stop": idea.stop,
        "target": idea.target,
        "exit_price": exit_price,
        "exit_reason": idea.exit_reason,
        "r": None if r is None else round(r, 3),
        "peak_r": None if peak_r is None else round(peak_r, 3),
        "signal_ids": idea.signal_ids,
    }


async def load_ideas(store) -> dict[str, Idea]:
    raw = await store.get(KEY_IDEAS, []) or []
    out: dict[str, Idea] = {}
    if isinstance(raw, list):
        for item in raw:
            idea = Idea.from_dict(item)
            if idea is not None:
                out[idea.key] = idea
    return out


async def save_ideas(store, ideas: dict[str, Idea], cap: int = 100) -> None:
    payload = [i.to_dict() for i in ideas.values()]
    # Keep closed ideas briefly so a restart does not resurrect them as new.
    await store.set(KEY_IDEAS, payload[-cap:])


async def record_close(store, idea: Idea, exit_price: Optional[float],
                       now: datetime, cap: int = 500) -> dict[str, Any]:
    entry = ledger_entry(idea, exit_price, now)
    await store.append(KEY_LEDGER, entry, cap=cap)
    return entry


def summarize_ledger(entries: Sequence[dict]) -> dict[str, Any]:
    """What has actually worked, sliced by the things we chose on.

    Deliberately small-sample-honest: it reports counts next to every average,
    because an average over three trades is an anecdote with a decimal point.
    """
    scored = [e for e in entries if isinstance(e, dict) and e.get("r") is not None]
    if not scored:
        return {"n": 0, "note": "لا توجد صفقات مغلقة بعد"}

    def stats(rows: list[dict]) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        rs = [float(r["r"]) for r in rows]
        wins = [r for r in rs if r > 0]
        return {
            "n": len(rs),
            "avg_r": round(sum(rs) / len(rs), 3),
            "win_rate": round(len(wins) / len(rs), 3),
            "total_r": round(sum(rs), 2),
        }

    by_source: dict[str, list[dict]] = {}
    by_coin: dict[str, list[dict]] = {}
    for e in scored:
        for s in (e.get("sources") or ["?"]):
            by_source.setdefault(s, []).append(e)
        by_coin.setdefault(str(e.get("coin")), []).append(e)

    return {
        "n": len(scored),
        "all": stats(scored),
        "by_source": {k: stats(v) for k, v in sorted(by_source.items())},
        "by_coin": {k: stats(v) for k, v in sorted(by_coin.items())},
        "peak_r_avg": round(
            sum(float(e["peak_r"]) for e in scored if e.get("peak_r") is not None)
            / max(1, sum(1 for e in scored if e.get("peak_r") is not None)), 3),
    }
