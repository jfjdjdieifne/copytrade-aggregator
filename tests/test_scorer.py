"""
اختبارات لوحدة نظام الثقة (scorer.py).
"""

from copytrade_agg.classifier import TraderProfile
from copytrade_agg.scorer import ConfidenceScorer, ConfidenceResult

def test_confidence_score_range():
    """اختبار أن درجة الثقة ضمن المدى 0-100."""
    scorer = ConfidenceScorer(entry_tolerance_pct=1.5)
    profiles = [
        TraderProfile(
            trader_id=f"pro_{i}", platform="collective2", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65000.0 + i * 10,
            total_trades=450, win_rate=0.60, profit_factor=1.85,
            max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
            performance_window_days=270, trades_in_window=300,
            win_rate_in_window=0.59, data_confirmed=True,
        )
        for i in range(3)
    ]
    result = scorer.score(profiles, "BTC-USD", "BUY", "spot")
    assert 0 <= result.total_score <= 100
    assert result.confidence_level in ("VERY_HIGH", "HIGH", "MEDIUM", "LOW", "INSUFFICIENT")

def test_min_professionals_required():
    """اختبار أن الإشارة لا تُطلق مع أقل من 2 محترفين نشطين."""
    scorer = ConfidenceScorer(entry_tolerance_pct=1.5)
    profiles = [
        TraderProfile(
            trader_id="pro_001", platform="collective2", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65000.0,
            total_trades=450, win_rate=0.60, profit_factor=1.85,
            max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
            performance_window_days=270, trades_in_window=300,
            win_rate_in_window=0.59, data_confirmed=True,
        ),
    ]
    result = scorer.score(profiles, "BTC-USD", "BUY", "spot")
    assert result.signal_emitted is False  #need min 2

def test_two_professionals_emit_signal():
    """اختبار أن الإشارة تُطلق مع 2 محترفين نشطين."""
    scorer = ConfidenceScorer(entry_tolerance_pct=1.5)
    profiles = [
        TraderProfile(
            trader_id=f"pro_{i}", platform="collective2", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65000.0 + i * 5,
            total_trades=450, win_rate=0.60, profit_factor=1.85,
            max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
            performance_window_days=270, trades_in_window=300,
            win_rate_in_window=0.59, data_confirmed=True,
        )
        for i in range(2)
    ]
    result = scorer.score(profiles, "BTC-USD", "BUY", "spot")
    assert result.signal_emitted is True
    assert result.n_professional_active == 2

def test_entry_spread_within_tolerance():
    """اختبار أن انتشار الدخول ضمن 1.5% لا يؤثر سلباً على الثقة."""
    scorer = ConfidenceScorer(entry_tolerance_pct=1.5)
    profiles = [
        TraderProfile(
            trader_id=f"pro_{i}", platform="collective2", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65000.0 + i * 10,  # انتشار 20 نقطة = 0.03%
            total_trades=450, win_rate=0.60, profit_factor=1.85,
            max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
            performance_window_days=270, trades_in_window=300,
            win_rate_in_window=0.59, data_confirmed=True,
        )
        for i in range(3)
    ]
    result = scorer.score(profiles, "BTC-USD", "BUY", "spot")
    assert result.entry_spread_pct < 1.5  # ضمن التolerance

def test_market_type_separation():
    """اختبار أن Spot و Futures لا يُخلطان."""
    scorer = ConfidenceScorer(entry_tolerance_pct=1.5)
    profiles_spot = [
        TraderProfile(
            trader_id="pro_001", platform="collective2", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65000.0,
            total_trades=450, win_rate=0.60, profit_factor=1.85,
            max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
            performance_window_days=270, trades_in_window=300,
            win_rate_in_window=0.59, data_confirmed=True,
        ),
    ]
    profiles_futures = [
        TraderProfile(
            trader_id="pro_001", platform="hyperliquid", market_type="futures",
            pair="BTC-USD", direction="BUY", entry_price=65000.0,
            total_trades=450, win_rate=0.60, profit_factor=1.85,
            max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
            performance_window_days=270, trades_in_window=300,
            win_rate_in_window=0.59, data_confirmed=True,
        ),
    ]
    result_spot = scorer.score(profiles_spot, "BTC-USD", "BUY", "spot")
    result_futures = scorer.score(profiles_futures, "BTC-USD", "BUY", "futures")
    assert result_spot.market_types == ["spot"]
    assert result_futures.market_types == ["futures"]

if __name__ == "__main__":
    test_confidence_score_range()
    test_min_professionals_required()
    test_two_professionals_emit_signal()
    test_entry_spread_within_tolerance()
    test_market_type_separation()
    print("✅ جميع اختبارات scorer نجحت")