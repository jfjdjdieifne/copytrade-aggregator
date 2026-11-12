"""
Backtest module for Copy Trade Aggregator.

يختبر إشارات النسخ المجمعة على بيانات historischen (ohlc candles) لمعرفة:
- هل الإشارات ذات الثقة العالية حققت أهدافها؟
- ما هو متوسط الـ R لكل مستوى ثقة؟
- كم مرة بلغ الستوب قبل الهدف؟

هذا module مستقل عن ZONES v1 وبحثه تحليلي فقط (HYPOTHETICAL_).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from .models import AggregatedSignal

UTC = timezone.utc


@dataclass
class Candle:
    """شمعة بسيطة للاختبار التاريخي."""
    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class BacktestResult:
    """نتيجة اختبار إشارة واحدة."""
    signal: AggregatedSignal
    outcome: str           # "TARGET" | "STOP" | "TIMEOUT" | "NO_DATA"
    r_value: Optional[float]   # R المحقق (-1 للستوب، +1 للهدف، 0 للتIMEOUT)
    bars_held: int         # عدد الشموع التي stayed في الصفقة
    exit_price: Optional[float]
    exit_time: Optional[datetime]


def backtest_signal(
    signal: AggregatedSignal,
    candles: list[Candle],
    horizon_days: int = 10,
) -> BacktestResult:
    """
    اختبار إشارة مجمعة واحدة على قائمة شموع.

    المنطق:
    --direction BUY: الهدف إذا وصل السعر إلى target، الستوب إذا وصل إلى stop
    -direction SELL: العكس
    - الأفق الزمني: horizon_days من وقت إنشاء الإشارة
    """
    entry = signal.entry_cluster_center
    stop = signal.stop_consensus
    target = signal.target_consensus
    direction = signal.direction.upper()

    if not candles:
        return BacktestResult(signal, "NO_DATA", None, 0, None, None)

    horizon_end = signal.created_at + timedelta(days=horizon_days)

    # فلترة الشموع ضمن الأفق الزمني
    relevant = [c for c in candles if signal.created_at <= c.start <= horizon_end]
    if not relevant:
        return BacktestResult(signal, "NO_DATA", None, 0, None, None)

    r_denominator = None
    if stop is not None:
        r_denominator = abs(entry - stop)

    for i, c in enumerate(relevant):
        if direction == "BUY":
            # الهدف first (إذا تم تحديده)
            if target is not None and c.high >= target:
                r = (target - entry) / r_denominator if r_denominator else None
                return BacktestResult(signal, "TARGET", r, i + 1, target, c.start)
            # الستوب
            if stop is not None and c.low <= stop:
                r = (stop - entry) / r_denominator if r_denominator else None
                return BacktestResult(signal, "STOP", r, i + 1, stop, c.start)
        else:  # SELL
            if target is not None and c.low <= target:
                r = (entry - target) / r_denominator if r_denominator else None
                return BacktestResult(signal, "TARGET", r, i + 1, target, c.start)
            if stop is not None and c.high >= stop:
                r = (entry - stop) / r_denominator if r_denominator else None
                return BacktestResult(signal, "STOP", r, i + 1, stop, c.start)

    # لم يُصل إلى أي منهما → TIMEOUT
    last_close = relevant[-1].close
    if r_denominator:
        if direction == "BUY":
            r = (last_close - entry) / r_denominator
        else:
            r = (entry - last_close) / r_denominator
    else:
        r = None
    return BacktestResult(signal, "TIMEOUT", r, len(relevant), last_close, relevant[-1].start)


def summarize_backtest(results: list[BacktestResult]) -> dict:
    """
    تلخيص نتائج الاختبار التاريخي لمجموعة إشارات.
    """
    total = len(results)
    if total == 0:
        return {"total": 0}

    target_count = sum(1 for r in results if r.outcome == "TARGET")
    stop_count = sum(1 for r in results if r.outcome == "STOP")
    timeout_count = sum(1 for r in results if r.outcome == "TIMEOUT")
    no_data_count = sum(1 for r in results if r.outcome == "NO_DATA")

    hit_rate = target_count / total if total else 0.0
    stop_rate = stop_count / total if total else 0.0

    r_values = [r.r_value for r in results if r.r_value is not None]
    mean_r = sum(r_values) / len(r_values) if r_values else 0.0

    return {
        "total": total,
        "target": target_count,
        "stop": stop_count,
        "timeout": timeout_count,
        "no_data": no_data_count,
        "hit_rate": round(hit_rate, 4),
        "stop_rate": round(stop_rate, 4),
        "mean_r": round(mean_r, 4),
    }


# ---------------------------------------------------------------------------
# Demo مع بيانات اصطناعية
# ---------------------------------------------------------------------------


def _make_candle(start: datetime, open_p: float, high: float, low: float, close: float) -> Candle:
    return Candle(start=start, open=open_p, high=high, low=low, close=close)


def run_backtest_demo() -> None:
    from .models import Signal, CopyTradeAggregator
    from .connectors import normalize_fill_to_signal, RawFill

    print("=" * 70)
    print("Copy Trade Backtest — Demo (بيانات اصطناعية)")
    print("=" * 70)

    # إنشاء إشارة مجمعة افتراضية
    agg = CopyTradeAggregator(entry_tolerance=0.0020, min_traders=2)
    # ننشئ AggregatedSignal يدوياً للتجربة
    signal = AggregatedSignal(
        pair="EURUSD",
        direction="BUY",
        n_traders=3,
        confidence=0.75,
        entry_cluster_center=1.0850,
        entry_spread=0.0010,
        stop_consensus=1.0820,
        target_consensus=1.0900,
        trader_ids=["T1", "T2", "T3"],
        sources=["hyperliquid", "dydx"],
        created_at=datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC),
    )

    # شموع افتراضية: السعر يصعد نحو الهدف
    base = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
    candles = []
    price = 1.0850
    for i in range(60):  # 60 شمعة = 15 ساعة إذا كانت كل 15 دقيقة
        high = price + 0.0005
        low = price - 0.0003
        close = price + 0.0002
        candles.append(_make_candle(base + timedelta(minutes=15 * i), price, high, low, close))
        price += 0.0001  # صعود تدريجي

    result = backtest_signal(signal, candles, horizon_days=10)
    print(f"\nإشارة: {signal.pair} {signal.direction} @ {signal.entry_cluster_center}")
    print(f"الستوب: {signal.stop_consensus} | الهدف: {signal.target_consensus}")
    print(f"النتيجة: {result.outcome}")
    print(f"القيمة R: {result.r_value}")
    print(f"الشموع المحتفظ بها: {result.bars_held}")
    print(f"سعر الخروج: {result.exit_price}")

    # تلخيص مجموع (トゥ示)
    summary = summarize_backtest([result])
    print(f"\nالتلخيص: {summary}")

    print("\nملاحظة: بيانات افتراضية للاختبار فقط (HYPOTHETICAL_).")


if __name__ == "__main__":
    run_backtest_demo()