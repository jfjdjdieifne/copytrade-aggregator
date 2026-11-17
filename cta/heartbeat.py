"""Proof of life that carries evidence.

The operator asked for two things that pull in opposite directions: do not go
silent while I am still deciding whether this works, and do not send me words
when you could send me numbers.

"البوت شغال" is not proof of life. It looks identical whether the radar is
scoring forty thousand wallets or died on its first request. The only honest
proof of life is a message whose contents would look *different* if the system
were broken — and the cheapest such message is a list of counts.

So every heartbeat reports things that move:

    uptime, cycles attempted and succeeded, traders discovered and graded,
    venues answering, positions visible, coins covered, signals emitted

and — when there are no signals — *which gate stopped the closest group*.

That last line is the reason this module exists. "Zero signals" is ambiguous:
it means either a quiet market or a radar that can never fire, and the
difference is a day of the operator's life. `cta.funnel` already answers it;
the heartbeat carries the answer so nobody has to open a terminal.

The module is deliberately read-only with respect to the radar. It observes
cycle reports and renders them. It decides nothing and it never blocks a cycle.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

UTC = timezone.utc

# The first report lands early on purpose. A new operator is watching for any
# sign of life, and thirty minutes of silence after a fresh deploy is thirty
# minutes spent wondering whether the SSH key was wrong.
FIRST_REPORT_MIN = 5.0
DEFAULT_REPORT_MIN = 30.0

KEY_HEARTBEAT = "heartbeat"
# The radar's own snapshot of the last positions it saw, written by the cycle
# so the heartbeat can explain silence without re-polling any venue.
KEY_LAST_POSITIONS = "last_positions"

# Capped: this is a diagnostic snapshot, not a database. Four hundred open
# positions is already far beyond any realistic consensus search.
MAX_STORED_POSITIONS = 400


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plural(n: int, word: str) -> str:
    return f"{n} {word}"


# ── position snapshot ────────────────────────────────────────────────────────
# Positions live as `Position` dataclasses inside a cycle and as JSON in the
# store. The heartbeat reads the JSON back, so both shapes have to survive the
# round trip. `opened_at` is the only field that cannot be stored as-is.

def positions_to_rows(positions: list) -> list[dict]:
    """Serialise live positions for the store."""
    from dataclasses import is_dataclass

    rows = []
    for p in positions[:MAX_STORED_POSITIONS]:
        if not is_dataclass(p):
            continue
        row = asdict(p)
        opened = row.get("opened_at")
        if isinstance(opened, datetime):
            row["opened_at"] = opened.isoformat()
        rows.append(row)
    return rows


def rows_to_positions(rows: Any) -> list:
    """Rebuild positions from the store. Unreadable rows are dropped, not fatal."""
    from .models import Position

    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            opened_raw = row.get("opened_at")
            opened = None
            if opened_raw:
                try:
                    opened = datetime.fromisoformat(
                        str(opened_raw).replace("Z", "+00:00")
                    )
                except ValueError:
                    opened = None
            out.append(
                Position(
                    wallet=str(row.get("wallet", "")),
                    source=str(row.get("source", "")),
                    coin=str(row.get("coin", "")),
                    signed_size=float(row.get("signed_size") or 0.0),
                    entry=float(row.get("entry") or 0.0),
                    size_usd=float(row.get("size_usd") or 0.0),
                    leverage=float(row.get("leverage") or 1.0),
                    opened_at=opened,
                    stop=row.get("stop"),
                    target=row.get("target"),
                    quality=str(row.get("quality", "WATCH")),
                    quality_score=_int(row.get("quality_score")),
                    mark_price=row.get("mark_price"),
                )
            )
        except (TypeError, ValueError):
            # One malformed row must not cost us the whole explanation.
            continue
    return out


# ── the pulse ────────────────────────────────────────────────────────────────

class Pulse:
    """Accumulates what each cycle did, and says it once it is worth saying."""

    def __init__(
        self,
        store=None,
        interval_min: float = DEFAULT_REPORT_MIN,
        now: Optional[datetime] = None,
    ) -> None:
        self.store = store
        self.interval_min = max(1.0, float(interval_min or DEFAULT_REPORT_MIN))
        self.started_at = now or datetime.now(UTC)
        self.cycles = 0
        self.failed = 0
        self.signals = 0
        self.clusters = 0
        self.ideas_opened = 0
        self.ideas_closed = 0
        self.watchlist = 0
        self.positions_seen = 0
        self.coins: set[str] = set()
        self.health: dict[str, bool] = {}
        self.reports_sent = 0
        # Counts as of the last report, so the message can show what moved
        # rather than only where things stand.
        self._baseline: dict[str, int] = {}

    # ── observe ──────────────────────────────────────────────────────────────
    def observe(self, report: Optional[dict]) -> None:
        report = report or {}
        self.cycles += 1
        if report.get("error"):
            self.failed += 1
        self.signals += _int(report.get("signals"))
        self.clusters += _int(report.get("clusters"))
        self.ideas_opened += _int(report.get("ideas_opened"))
        self.ideas_closed += _int(report.get("ideas_closed"))
        watchlist = _int(report.get("watchlist"))
        if watchlist:
            self.watchlist = watchlist
        positions = _int(report.get("positions"))
        if positions:
            self.positions_seen = positions
        for coin in report.get("coins") or []:
            if coin:
                self.coins.add(str(coin))
        health = report.get("health")
        if isinstance(health, dict) and health:
            self.health = {str(k): bool(v) for k, v in health.items()}

    # ── timing ───────────────────────────────────────────────────────────────
    async def _last_report_at(self) -> Optional[datetime]:
        if self.store is None:
            return None
        try:
            raw = await self.store.get(KEY_HEARTBEAT, None)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        stamp = raw.get("last_report_at")
        if not stamp:
            return None
        try:
            return datetime.fromisoformat(str(stamp))
        except ValueError:
            return None

    async def due(self, now: Optional[datetime] = None) -> bool:
        """Report early the first time, then on a fixed cadence.

        The interval is measured from the last *successful* report, persisted in
        the store, so restarting the service does not reset the clock and flood
        the operator with first-run greetings.
        """
        now = now or datetime.now(UTC)
        last = await self._last_report_at()
        if last is None:
            elapsed = (now - self.started_at).total_seconds() / 60.0
            return elapsed >= FIRST_REPORT_MIN
        return (now - last).total_seconds() / 60.0 >= self.interval_min

    async def mark_reported(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(UTC)
        self.reports_sent += 1
        self._baseline = {
            "watchlist": self.watchlist,
            "signals": self.signals,
            "cycles": self.cycles,
        }
        if self.store is None:
            return
        try:
            await self.store.set(
                KEY_HEARTBEAT,
                {
                    "last_report_at": now.isoformat(),
                    "reports_sent": self.reports_sent,
                },
            )
        except Exception:
            # A heartbeat that cannot remember itself still reports next time.
            pass

    # ── render ───────────────────────────────────────────────────────────────
    def _delta(self, key: str, value: int) -> str:
        """`(+7)` since the last report — motion is the proof that it works."""
        base = self._baseline.get(key)
        if base is None:
            return ""
        diff = value - base
        if diff == 0:
            return ""
        return f"  (+{diff})" if diff > 0 else f"  ({diff})"

    def _uptime(self, now: datetime) -> str:
        span = now - self.started_at
        total = int(max(0, span.total_seconds()))
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        if hours:
            return f"{hours}س {minutes}د"
        return f"{minutes}د"

    def render(
        self,
        cfg: Any = None,
        positions: Optional[list] = None,
        watchlist: Optional[list] = None,
        now: Optional[datetime] = None,
    ) -> str:
        now = now or datetime.now(UTC)
        lines: list[str] = []

        lines.append(f"📡 *تقرير {int(self.interval_min)} دقيقة* — {now:%H:%M}")

        # 1. Is it running? Cycles attempted vs cycles that raised.
        ok = self.cycles - self.failed
        if self.failed:
            lines.append(
                f"⏱ شغال من {self._uptime(now)} · {self.cycles} دورة "
                f"({ok} نجحت · {self.failed} فشلت)"
            )
        else:
            lines.append(f"⏱ شغال من {self._uptime(now)} · {self.cycles} دورة (كلها نجحت)")

        # 2. Has it found anyone? This is the number that proves discovery works.
        lines.append(
            f"👁 المتداولون المراقَبون: {self.watchlist}"
            f"{self._delta('watchlist', self.watchlist)}"
        )

        # 3. Is it seeing the market right now?
        n_coins = len(self.coins)
        if n_coins == 0:
            coin_note = ""
        elif n_coins == 1 or n_coins >= 11:
            coin_note = f" على {n_coins} عملة"
        else:
            coin_note = f" على {n_coins} عملات"
        lines.append(f"📊 مراكز مرئية الآن: {self.positions_seen}{coin_note}")

        # 4. Are the venues answering?
        if self.health:
            up = sum(1 for v in self.health.values() if v)
            total = len(self.health)
            flag = "" if up == total else " ⚠️"
            lines.append(f"🌐 المنصات: {up}/{total} تستجيب{flag}")

        # 5. Signals — and, when there are none, why.
        lines.append(
            f"🔔 الإشارات: {self.signals}{self._delta('signals', self.signals)}"
        )
        if self.ideas_closed:
            lines.append(
                f"🚪 أفكار: {self.ideas_opened} افتتحت · {self.ideas_closed} أُغلقت"
            )

        # 6. The line that makes silence interpretable.
        why = self._explain(positions, watchlist, cfg, now)
        if why:
            lines.append("")
            lines.append(why)

        # 7. Say it plainly when the numbers mean something is wrong.
        warnings = self._warnings(now)
        if warnings:
            lines.append("")
            lines.extend(warnings)

        return "\n".join(lines)

    def _explain(
        self,
        positions: Optional[list],
        watchlist: Optional[list],
        cfg: Any,
        now: datetime,
    ) -> str:
        """Why no signal? Delegates to the funnel, which already knows.

        Refuses to answer when the positions are unknown. The funnel's verdict
        for an empty list is "we see no open positions", which is a lie when the
        real problem is that the snapshot never arrived — and a confident wrong
        reason sends the operator chasing the wrong fault.
        """
        if self.signals:
            return ""
        if cfg is None or positions is None:
            return ""
        try:
            from .funnel import diagnose

            report = diagnose(positions, watchlist or [], cfg, now=now)
        except Exception:
            # An explanation is a nicety. Losing it must not lose the report.
            return ""
        verdict = str(report.get("verdict") or "").strip()
        if not verdict:
            return ""
        return f"🎯 {verdict}"

    def _warnings(self, now: datetime) -> list[str]:
        """Numbers that mean 'broken', stated as such.

        A heartbeat that only ever says things are fine is decoration. These are
        the states the operator cannot see from outside and would otherwise
        discover a day late.
        """
        out: list[str] = []
        elapsed_min = (now - self.started_at).total_seconds() / 60.0

        if self.cycles == 0:
            out.append("⚠️ ما صارت ولا دورة — الحلقة ما تشتغل")

        if self.health and not any(self.health.values()):
            out.append("⚠️ ما في ولا منصة تستجيب — تأكد من الشبكة (المنفذ ٤٤٣)")

        if self.watchlist == 0 and elapsed_min >= 15:
            out.append(
                "⚠️ قائمة المراقبة لسه صفر بعد "
                f"{int(elapsed_min)} دقيقة — البحث ما لاقى متداولين يجتازون التقييم"
            )

        if self.cycles >= 10 and self.positions_seen == 0 and self.watchlist:
            out.append(
                "⚠️ في متداولون مراقَبون بس ما نشوف ولا مركز مفتوح — "
                "تأكد إنو المنصات ترجّع مراكز"
            )

        if self.failed and self.cycles and self.failed * 2 >= self.cycles:
            out.append("⚠️ أكثر من نص الدورات بتفشل — شوف اللوج")

        return out


# ── boot message ─────────────────────────────────────────────────────────────

def boot_message(
    health: Optional[dict] = None,
    watchlist: int = 0,
    interval_min: float = DEFAULT_REPORT_MIN,
) -> str:
    """The first thing the operator receives.

    Its job is not to be reassuring. It is to give the operator a baseline to
    compare every later report against, so a change is visible.
    """
    lines = ["🚀 *الرادار اشتغل*", ""]

    if health:
        up = sum(1 for v in health.values() if v)
        total = len(health)
        lines.append(f"🌐 المنصات: {up}/{total} تستجيب")
    else:
        lines.append("🌐 المنصات: لم تُفحص بعد")

    if watchlist:
        lines.append(f"👁 المتداولون المراقَبون: {watchlist}")
    else:
        lines.append("👁 المتداولون: ٠ هلأ — البحث بدأ يقيّم")

    lines.append("")
    lines.append(f"⏱ أول تقرير بعد {int(FIRST_REPORT_MIN)} دقائق، ثم كل {int(interval_min)}")
    lines.append("_الأرقام بتزيد كل تقرير — هادا دليل إنو عم يشتغل._")

    return "\n".join(lines)


def _age(stamp: Optional[datetime], now: datetime) -> Optional[timedelta]:
    if stamp is None:
        return None
    return now - stamp
