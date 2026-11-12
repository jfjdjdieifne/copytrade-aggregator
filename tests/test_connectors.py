"""
اختبارات لوحدة وصلات المنصات (connectors.py).
"""

from copytrade_agg.connectors import (
    RawFill, BaseConnector,
    HyperliquidConnector, DydxConnector,
    Collective2Connector, MyfxbookConnector,
    ZuluTradeConnector, DarwinexConnector,
    BirdeyeConnector, DeBankConnector,
    ALL_CONNECTORS, get_connector, list_platforms,
    normalize_fill_to_signal,
)
from datetime import datetime, timezone

def test_list_platforms():
    """اختبار أن جميع المنصات الـ 8 مسجلة."""
    platforms = list_platforms()
    assert len(platforms) == 8
    expected = ["hyperliquid", "dydx", "collective2", "myfxbook",
                "zulutrade", "darwinex", "birdeye", "debank"]
    for p in expected:
        assert p in platforms

def test_get_connector():
    """اختبار إنشاء موصل لكل منصة."""
    for platform in list_platforms():
        connector = get_connector(platform)
        assert connector.platform == platform

def test_get_connector_invalid():
    """اختبار أن منصة غير معروفة ترفع خطأ."""
    try:
        get_connector("unknown_platform")
        assert False, "يجب أن يرفع خطأ لمنصة غير معروفة"
    except ValueError:
        pass

def test_normalize_fill_to_signal():
    """اختبار تحويل RawFill إلى Signal."""
    raw = RawFill(
        trader="0xabc123",
        pair="BTC-USD",
        side="BUY",
        price=65000.0,
        size=0.1,
        timestamp=datetime.now(timezone.utc),
        platform="hyperliquid",
        raw={"test": True},
    )
    signal = normalize_fill_to_signal(raw, trader_rating=4.5)
    assert signal["pair"] == "BTC-USD"
    assert signal["direction"] == "BUY"
    assert signal["entry"] == 65000.0
    assert signal["trader_id"] == "0xabc123"
    assert signal["source"] == "hyperliquid"

def test_all_connectors_have_required_methods():
    """اختبار أن جميع الموصلات تطبق واجهة BaseConnector."""
    for platform, connector_class in ALL_CONNECTORS.items():
        connector = connector_class()
        assert hasattr(connector, "fetch_recent_fills")
        assert hasattr(connector, "stream_fills")
        assert hasattr(connector, "health_check")
        assert hasattr(connector, "platform")

def test_public_connectors_no_api_key():
    """اختبار أن المنصات العامة لا تحتاج API Key (لا تحتوي على خاصية api_key)."""
    hl = HyperliquidConnector()
    dx = DydxConnector()

    # Hyperliquid و dYdX لا تحتوي على خاصية api_key على الإطلاق
    assert not hasattr(hl, "api_key")
    assert not hasattr(dx, "api_key")

def test_api_key_connectors_require_key():
    """اختبار أن المنصات التي تحتاج API Key تُظهر أنها غير فعالة بدون مفتاح."""
    c2 = Collective2Connector(api_key=None)
    dw = DarwinexConnector(api_key=None)
    bd = BirdeyeConnector(api_key=None)

    assert c2.api_key is None
    assert dw.api_key is None
    assert bd.api_key is None

if __name__ == "__main__":
    test_list_platforms()
    test_get_connector()
    test_get_connector_invalid()
    test_normalize_fill_to_signal()
    test_all_connectors_have_required_methods()
    test_public_connectors_no_api_key()
    test_api_key_connectors_require_key()
    print("✅ جميع اختبارات connectors نجحت")