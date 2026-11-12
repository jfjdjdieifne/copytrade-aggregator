"""
Public API connectors for copy-trade aggregator.

تغطي هذه الوصلات 8 منصات:
1. Hyperliquid — WebSocket + REST عام (بدون حساب) للصفقات والمحافظ
2. dYdX — REST Indexer عام (بدون حساب) لسجل الصفقات (GET /fills)
3. Collective2 — API Key مطلوب (قوائم المتصدرين وتفاصيل الاستراتيجيات)
4. Myfxbook — جلسة تسجيل دخول مطلوب (حسابات مثبتة، لا تصدير عام للصفقات)
5. ZuluTrade — REST API لللائحة الترتيبية للمتداولين (مفتاح API)
6. Darwinex — API Key مطلوب (DARWINs، السمات الاستثمارية)
7. Birdeye — API Key مطلوب (bds.birdeye.so، محافظ المتداولين على DEX)
8. DeBank — عام/م partially عام (محفظة، محافظ الحيتان، عدد المتابعين)

ملاحظة: هذا ملف هيكلي (boilerplate). الوصلات التي تحتاج مفتاح API
ت require تكوين المفتاح قبل التشغيل الفعلي. الوصلات العامة (Hyperliquid, dYdX)
يمكن اختبارها ضد النقاط الحقيقية.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # allow import without aiohttp installed

UTC = timezone.utc


@dataclass
class RawFill:
    """صفقة خام من منصة عامة."""
    trader: str               # عنوان المحفظة / معرف المتداول
    pair: str                 # مثال: "EURUSD" أو "BTC-USD"
    side: str                 # "BUY" أو "SELL"
    price: float
    size: float
    timestamp: datetime
    platform: str             # "hyperliquid" | "dydx" | ...
    raw: dict                 # البيانات الخام كما وردت من المنصة


class BaseConnector(ABC):
    """واجهة موحدة لكل منصات البيانات العامة."""

    @abstractmethod
    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        """جلب الصفقات الحديثة من المنصة."""
        ...

    @abstractmethod
    async def stream_fills(self, callback) -> None:
        """بث الصفقات اللحظية عبر WebSocket."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """التحقق من أن المنصة متاحة."""
        ...


# ===========================================================================
# Hyperliquid — public WebSocket + REST (no account needed)
# ===========================================================================

class HyperliquidConnector(BaseConnector):
    """
    موصل عام لـ Hyperliquid.
    الوثائق: https://hyperliquid.gitbook.io/hyperliquid-docs
    """

    BASE_URL = "https://api.hyperliquid.xyz"
    WS_URL = "wss://api.hyperliquid.xyz/ws"

    def __init__(self):
        self.platform = "hyperliquid"

    async def health_check(self) -> bool:
        if aiohttp is None:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.BASE_URL}/info",
                    json={"type": "allMids"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        """
        جلب الصفقات الحديثة.
        Hyperliquid يحتاج عنوان مستخدم لجلب fills الخاصة به؛ بدون عنوان،
        لا يمكن جلب fills عامة لكل المتداولين عبر REST.
        تُستخدم WebSocket للمتابعة اللحظية بدلاً من ذلك.
        """
        raise NotImplementedError(
            "Hyperliquid REST /info userFills requires a user address. "
            "Use stream_fills() with a known trader address subscription instead."
        )

    async def stream_fills(self, callback) -> None:
        """
        بث الصفقات اللحظية عبر WebSocket عام.
        القناة: trades (للصفقات العامة) أو userFills (لعنوان معين).
        """
        raise NotImplementedError(
            "Hyperliquid WebSocket stream needs real ws connection testing."
        )


# ===========================================================================
# dYdX v4 Indexer — public REST (no account needed)
# ===========================================================================

class DydxConnector(BaseConnector):
    """
    موصل عام لـ dYdX v4 Indexer.
    الوثائق: https://indexer.dydx.trade/v4
    نقطة النهاية العامة: GET /fills (بلا مصادقة لقراءة عامة محدودة)
    """

    BASE_URL = "https://indexer.dydx.trade/v4"

    def __init__(self):
        self.platform = "dydx"

    async def health_check(self) -> bool:
        if aiohttp is None:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/fills",
                    params={"limit": 1},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        """
        جلب الصفقات الحديثة عبر dYdX v4 Indexer العامة.
        """
        fills: list[RawFill] = []
        if aiohttp is None:
            return fills
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/fills",
                    params={"limit": min(limit, 1000)},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return fills
                    data = await resp.json()
            for item in data.get("fills", [])[:limit]:
                pair = item.get("market", "UNKNOWN")
                side = item.get("side", "BUY").upper()
                price = float(item.get("price", 0) or 0)
                size = float(item.get("size", 0) or 0)
                trader = item.get("trader", "unknown")
                ts = item.get("createdAt") or item.get("createdAt", "")
                timestamp = _parse_iso(ts)
                fills.append(RawFill(
                    trader=trader,
                    pair=pair,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    platform="dydx",
                    raw=item,
                ))
        except Exception:
            pass
        return fills

    async def stream_fills(self, callback) -> None:
        """
        dYdX v4 لا يوفر WebSocket عام للfills في الإصدار العام.
        يُستخدم الاستطلاع (polling) عبر fetch_recent_fills بدلاً من ذلك.
        """
        raise NotImplementedError(
            "dYdX v4 Indexer does not expose a public fill WebSocket. "
            "Use polling via fetch_recent_fills()."
        )


# ===========================================================================
# Collective2 — API key required
# ===========================================================================

class Collective2Connector(BaseConnector):
    """
    موصل Collective2 (يحتاج مفتاح API).
    الوثائق: https://www.collective2.com/api-docs/latest
    """

    BASE_URL = "https://api.collective2.com"

    def __init__(self, api_key: Optional[str] = None):
        self.platform = "collective2"
        self.api_key = api_key

    async def health_check(self) -> bool:
        return self.api_key is not None

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        """
        Collective2 لا ي endpoint عام للصفقات الفردية بدون مفتاح API.
        يُستخدم GetLeaderBoard أو listAllSystems مع مفتاح API.
        """
        if not self.api_key:
            return []
        return []

    async def stream_fills(self, callback) -> None:
        raise NotImplementedError(
            "Collective2 uses polling, not WebSocket. Use fetch_recent_fills()."
        )


# ===========================================================================
# Myfxbook — login session required
# ===========================================================================

class MyfxbookConnector(BaseConnector):
    """
    موصل Myfxbook (يحتاج جلسة تسجيل دخول).
    الوثائق: https://www.myfxbook.com
    """

    BASE_URL = "https://www.myfxbook.com/api"

    def __init__(self, email: Optional[str] = None, password: Optional[str] = None):
        self.platform = "myfxbook"
        self.email = email
        self.password = password
        self.session_id: Optional[str] = None

    async def health_check(self) -> bool:
        return self.session_id is not None or (self.email is not None and self.password is not None)

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        """
        Myfxbook لا يوفر تصدير عام لصفقات النظام بدون جلسة دخول.
        تُستخدم GetAccount أو GetGain بعد تسجيل الدخول.
        """
        if not self.session_id and not (self.email and self.password):
            return []
        return []

    async def stream_fills(self, callback) -> None:
        raise NotImplementedError(
            "Myfxbook uses polling via REST session, not WebSocket."
        )


# ===========================================================================
# ZuluTrade — REST API leaderboard (API key)
# ===========================================================================

class ZuluTradeConnector(BaseConnector):
    """
    موصل ZuluTrade (يحتاج مفتاح API لبعض النقاط).
    الوثائق: https://us.zulutrade.com/restapi-reference
    """

    BASE_URL = "https://www.zulutrade.com"

    def __init__(self, api_key: Optional[str] = None):
        self.platform = "zulutrade"
        self.api_key = api_key

    async def health_check(self) -> bool:
        return True  # العامة 부분 لا تحتاج مفتاح

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        """
        ZuluTrade يوفر لائحة المتصدرين العامة (ZuluRank) لكن ليس صفقات فردية
        عامة لكل متداول بدون مصادقة.
        """
        return []

    async def stream_fills(self, callback) -> None:
        raise NotImplementedError(
            "ZuluTrade uses polling via REST, not WebSocket."
        )


# ===========================================================================
# Darwinex — API key required
# ===========================================================================

class DarwinexConnector(BaseConnector):
    """
    موصل Darwinex (يحتاج مفتاح API).
    الوثائق: https://www.darwinex.com
    """

    BASE_URL = "https://api.darwinex.com"

    def __init__(self, api_key: Optional[str] = None):
        self.platform = "darwinex"
        self.api_key = api_key

    async def health_check(self) -> bool:
        return self.api_key is not None

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        if not self.api_key:
            return []
        return []

    async def stream_fills(self, callback) -> None:
        raise NotImplementedError(
            "Darwinex uses polling via REST API key, not WebSocket."
        )


# ===========================================================================
# Birdeye — API key required
# ===========================================================================

class BirdeyeConnector(BaseConnector):
    """
    موصل Birdeye Data API (يحتاج مفتاح API من bds.birdeye.so).
    الوثائق: https://docs.birdeye.so
    """

    BASE_URL = "https://public-api.birdeye.so"

    def __init__(self, api_key: Optional[str] = None):
        self.platform = "birdeye"
        self.api_key = api_key

    async def health_check(self) -> bool:
        return self.api_key is not None

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        if not self.api_key:
            return []
        return []

    async def stream_fills(self, callback) -> None:
        raise NotImplementedError(
            "Birdeye uses polling via REST API key, not WebSocket."
        )


# ===========================================================================
# DeBank — public portfolio / whale tracking
# ===========================================================================

class DeBankConnector(BaseConnector):
    """
    موصل DeBank (محافظ، محافظ الحيتان، عدد المتابعين).
    الوثائق: https://debank.com
    """

    BASE_URL = "https://pro.openapi.debank.com"

    def __init__(self, api_key: Optional[str] = None):
        self.platform = "debank"
        self.api_key = api_key

    async def health_check(self) -> bool:
        return True

    async def fetch_recent_fills(self, limit: int = 200) -> list[RawFill]:
        """
        DeBank لا يوفر صفقات فردية (fills) مباشرة، بل بيانات محافظ ورصيد.
        تُستخدم لتحديد المحافظ النشطة ثم ربطها بمنصات أخرى.
        """
        return []

    async def stream_fills(self, callback) -> None:
        raise NotImplementedError(
            "DeBank provides portfolio data, not fill streams. Use polling."
        )


# ===========================================================================
# Connector registry: كل المنصات في مكان واحد
# ===========================================================================

ALL_CONNECTORS: dict[str, type[BaseConnector]] = {
    "hyperliquid": HyperliquidConnector,
    "dydx": DydxConnector,
    "collective2": Collective2Connector,
    "myfxbook": MyfxbookConnector,
    "zulutrade": ZuluTradeConnector,
    "darwinex": DarwinexConnector,
    "birdeye": BirdeyeConnector,
    "debank": DeBankConnector,
}


def get_connector(platform: str, **kwargs) -> BaseConnector:
    """إنشاء موصل لمنصة محددة."""
    if platform not in ALL_CONNECTORS:
        raise ValueError(f"Unknown platform: {platform}. Available: {list(ALL_CONNECTORS)}")
    return ALL_CONNECTORS[platform](**kwargs)


def list_platforms() -> list[str]:
    """إرجاع قائمة المنصات المدعومة."""
    return list(ALL_CONNECTORS.keys())


# ===========================================================================
# Helpers
# ===========================================================================

def _parse_iso(ts: str | None) -> datetime:
    """تحويل سلسلة ISO إلى datetime."""
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(UTC)


# ===========================================================================
# Normalizer: تحويل بيانات المنصة الخام إلى Signal الخاص بالمجمع
# ===========================================================================

def normalize_fill_to_signal(raw: RawFill, trader_rating: float = 3.0) -> dict:
    """
    تحويل RawFill إلى dict يمكن استخدامه لبناء Signal.
    trader_rating افتراضي 3.0 (متوسط) ما لم تُوفر تقييم حقيقي من المنصة.
    """
    return {
        "pair": raw.pair,
        "direction": raw.side.upper(),
        "entry": raw.price,
        "stop": None,
        "target": None,
        "trader_id": raw.trader,
        "trader_rating": trader_rating,
        "source": raw.platform,
        "received_at": raw.timestamp,
        "raw_note": f"platform_fill size={raw.size}",
    }


# ===========================================================================
# Self-test: التحقق من أن الهيكلية تعمل بدون اتصال حقيقي
# ===========================================================================

def test_normalizer() -> None:
    raw = RawFill(
        trader="0xabc123",
        pair="BTC-USD",
        side="BUY",
        price=65000.0,
        size=0.1,
        timestamp=datetime.now(UTC),
        platform="hyperliquid",
        raw={"test": True},
    )
    sig = normalize_fill_to_signal(raw, trader_rating=4.5)
    assert sig["pair"] == "BTC-USD"
    assert sig["direction"] == "BUY"
    assert sig["entry"] == 65000.0
    assert sig["trader_rating"] == 4.5
    assert sig["source"] == "hyperliquid"
    print("Normalizer self-test PASSED")


def test_registry() -> None:
    platforms = list_platforms()
    assert "hyperliquid" in platforms
    assert "dydx" in platforms
    assert "collective2" in platforms
    assert "myfxbook" in platforms
    assert "zulutrade" in platforms
    assert "darwinex" in platforms
    assert "birdeye" in platforms
    assert "debank" in platforms
    print(f"Registry self-test PASSED — {len(platforms)} platforms registered")


if __name__ == "__main__":
    test_normalizer()
    test_registry()
    print("Connectors module structure OK (no live API calls made).")