"""
اختبارات لوحدة محرك التوصيات (engine.py).
"""

from copytrade_agg.classifier import TraderProfile
from copytrade_agg.engine import RecommendationEngine

def test_engine_generates_recommendation():
    """اختبار أن المحرك يولد توصية عند توفر 2 محترفين نشطين."""
    engine = RecommendationEngine(entry_tolerance_pct=1.5, min_confidence=45)

    engine.add_signal(
        trader_id="pro_001", platform="collective2", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65000.0,
        stop_loss=63000.0, take_profit=69000.0, leverage=1.0,
        total_trades=450, winning_trades=270, losing_trades=180,
        win_rate=0.60, profit_factor=1.85, max_drawdown=0.12,
        account_age_days=730, last_trade_days_ago=2,
        performance_window_days=270, trades_in_window=300,
        win_rate_in_window=0.59, data_confirmed=True,
    )
    engine.add_signal(
        trader_id="pro_002", platform="myfxbook", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65200.0,
        stop_loss=63200.0, take_profit=69200.0, leverage=1.0,
        total_trades=320, winning_trades=192, losing_trades=128,
        win_rate=0.60, profit_factor=1.65, max_drawdown=0.15,
        account_age_days=540, last_trade_days_ago=5,
        performance_window_days=240, trades_in_window=200,
        win_rate_in_window=0.58, data_confirmed=True,
    )

    recommendations = engine.generate_recommendations()
    assert len(recommendations) >= 1
    rec = recommendations[0]
    assert rec.pair == "BTC-USD"
    assert rec.direction == "BUY"
    assert rec.market_type == "spot"
    assert rec.signal_emitted is True
    assert rec.confidence_score >= 45

def test_engine_no_recommendation_below_min_confidence():
    """اختبار أن المحرك لا يولد توصية أقل من الحد الأدنى للثقة."""
    engine = RecommendationEngine(entry_tolerance_pct=1.5, min_confidence=90)

    engine.add_signal(
        trader_id="pro_001", platform="collective2", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65000.0,
        total_trades=450, win_rate=0.60, profit_factor=1.85,
        max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
        performance_window_days=270, trades_in_window=300,
        win_rate_in_window=0.59, data_confirmed=True,
    )
    engine.add_signal(
        trader_id="pro_002", platform="myfxbook", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65200.0,
        total_trades=320, winning_trades=192, losing_trades=128,
        win_rate=0.60, profit_factor=1.65, max_drawdown=0.15,
        account_age_days=540, last_trade_days_ago=5,
        performance_window_days=240, trades_in_window=200,
        win_rate_in_window=0.58, data_confirmed=True,
    )

    recommendations = engine.generate_recommendations()
    # قد لا تُطلق الإشارة إذا كانت الثقة أقل من 90
    for rec in recommendations:
        assert rec.confidence_score >= 90

def test_engine_market_type_filter():
    """اختبار تصفية التوصيات حسب نوع السوق."""
    engine = RecommendationEngine(entry_tolerance_pct=1.5, min_confidence=45)

    engine.add_signal(
        trader_id="pro_001", platform="collective2", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65000.0,
        total_trades=450, win_rate=0.60, profit_factor=1.85,
        max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
        performance_window_days=270, trades_in_window=300,
        win_rate_in_window=0.59, data_confirmed=True,
    )
    engine.add_signal(
        trader_id="pro_002", platform="binance", market_type="futures",
        pair="ETH-USD", direction="SELL", entry_price=3400.0,
        total_trades=380, winning_trades=228, losing_trades=152,
        win_rate=0.60, profit_factor=1.70, max_drawdown=0.14,
        account_age_days=650, last_trade_days_ago=1,
        performance_window_days=300, trades_in_window=250,
        win_rate_in_window=0.59, data_confirmed=True,
    )

    recs_spot = engine.generate_recommendations(market_type="spot")
    recs_futures = engine.generate_recommendations(market_type="futures")

    for rec in recs_spot:
        assert rec.market_type == "spot"
    for rec in recs_futures:
        assert rec.market_type == "futures"

def test_statistics():
    """اختبار إحصائيات المحرك."""
    engine = RecommendationEngine(entry_tolerance_pct=1.5, min_confidence=45)

    engine.add_signal(
        trader_id="pro_001", platform="collective2", market_type="spot",
        pair="BTC-USD", direction="BUY", entry_price=65000.0,
        total_trades=450, win_rate=0.60, profit_factor=1.85,
        max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
        data_confirmed=True,
    )

    stats = engine.get_statistics()
    assert stats["total_signals"] == 1
    assert stats["unique_traders"] == 1
    assert "BTC-USD" in stats["pairs"]
    assert "spot" in stats["market_types"]

if __name__ == "__main__":
    test_engine_generates_recommendation()
    test_engine_no_recommendation_below_min_confidence()
    test_engine_market_type_filter()
    test_statistics()
    print("✅ جميع اختبارات engine نجحت")