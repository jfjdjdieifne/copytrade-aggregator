"""
اختبارات لوحدة تصنيف المتداولين (classifier.py).
"""

from copytrade_agg.classifier import TraderProfile, TraderClassifier

def test_professional_active_classification():
    """اختبار تصنيف متداول محترف نشط."""
    classifier = TraderClassifier()
    profile = TraderProfile(
        trader_id="test_pro_001",
        platform="collective2",
        market_type="spot",
        pair="BTC-USD",
        direction="BUY",
        entry_price=65000.0,
        total_trades=450,
        winning_trades=270,
        losing_trades=180,
        win_rate=0.60,
        profit_factor=1.85,
        max_drawdown=0.12,
        sharpe_ratio=1.45,
        account_age_days=730,
        last_trade_days_ago=2,
        performance_window_days=270,
        trades_in_window=300,
        win_rate_in_window=0.59,
        data_confirmed=True,
    )
    result = classifier.classify(profile)
    assert result.classification == "PROFESSIONAL_ACTIVE"
    assert result.classification_score >= 70

def test_suspicious_classification():
    """اختبار تصنيف متداول مشبوه (نسبة ربح غير واقعية مع 20+ صفقة)."""
    classifier = TraderClassifier()
    profile = TraderProfile(
        trader_id="test_sus_001",
        platform="zulutrade",
        market_type="spot",
        pair="XAU-USD",
        direction="BUY",
        entry_price=2650.0,
        total_trades=25,  # 20+ صفقة لتفعيل شرط suspicious
        winning_trades=23,
        losing_trades=2,
        win_rate=0.93,  # نسبة مرتفعة جداً
        profit_factor=5.20,
        max_drawdown=0.02,
        account_age_days=45,  # أقل من 6 أشهر
        last_trade_days_ago=1,
        performance_window_days=45,
        trades_in_window=25,
        win_rate_in_window=0.93,
        data_confirmed=False,
    )
    result = classifier.classify(profile)
    assert result.classification == "SUSPICIOUS"

def test_regular_classification():
    """اختبار تصنيف متداول عادي."""
    classifier = TraderClassifier()
    profile = TraderProfile(
        trader_id="test_reg_001",
        platform="dydx",
        market_type="spot",
        pair="ETH-USD",
        direction="SELL",
        entry_price=3400.0,
        total_trades=45,
        winning_trades=23,
        losing_trades=22,
        win_rate=0.51,
        profit_factor=1.10,
        max_drawdown=0.08,
        account_age_days=90,
        last_trade_days_ago=3,
        performance_window_days=90,  # أقل من 6 أشهر
        trades_in_window=30,
        win_rate_in_window=0.50,
        data_confirmed=True,
    )
    result = classifier.classify(profile)
    assert result.classification in ("REGULAR", "PROFESSIONAL_INACTIVE")

def test_deduplicate_by_trader_id():
    """اختبار إزالة التكرار للشخص نفسه عبر منصات متعددة."""
    classifier = TraderClassifier()
    profiles = [
        TraderProfile(
            trader_id="same_trader",
            platform="collective2",
            market_type="spot",
            pair="BTC-USD",
            direction="BUY",
            entry_price=65000.0,
            total_trades=450,
            win_rate=0.60,
            profit_factor=1.85,
            max_drawdown=0.12,
            account_age_days=730,
            last_trade_days_ago=2,
            classification_score=100.0,
            data_confirmed=True,
        ),
        TraderProfile(
            trader_id="same_trader",  # نفس المعرف
            platform="hyperliquid",
            market_type="futures",
            pair="BTC-USD",
            direction="BUY",
            entry_price=65100.0,
            total_trades=400,
            win_rate=0.60,
            profit_factor=1.70,
            max_drawdown=0.14,
            account_age_days=700,
            last_trade_days_ago=1,
            classification_score=95.0,
            data_confirmed=True,
        ),
    ]
    classified = classifier.classify_batch(profiles)
    deduped = classifier.deduplicate_by_trader_id(classified)
    assert len(deduped) == 1  # يجب أن يُحتسب مرة واحدة

def test_get_professional_active():
    """اختبار جلب المتداولين المحترفين النشطين فقط."""
    classifier = TraderClassifier()
    profiles = [
        TraderProfile(
            trader_id="pro_001", platform="c2", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65000.0,
            total_trades=450, win_rate=0.60, profit_factor=1.85,
            max_drawdown=0.12, account_age_days=730, last_trade_days_ago=2,
            classification_score=100.0, classification="PROFESSIONAL_ACTIVE",
        ),
        TraderProfile(
            trader_id="pro_002", platform="c2", market_type="spot",
            pair="BTC-USD", direction="BUY", entry_price=65100.0,
            total_trades=320, win_rate=0.60, profit_factor=1.65,
            max_drawdown=0.15, account_age_days=540, last_trade_days_ago=45,
            classification_score=95.0, classification="PROFESSIONAL_INACTIVE",
        ),
    ]
    classified = classifier.classify_batch(profiles)
    active = classifier.get_professional_active(classified)
    assert len(active) == 1

if __name__ == "__main__":
    test_professional_active_classification()
    test_suspicious_classification()
    test_regular_classification()
    test_deduplicate_by_trader_id()
    test_get_professional_active()
    print("✅ جميع اختبارات classifier نجحت")