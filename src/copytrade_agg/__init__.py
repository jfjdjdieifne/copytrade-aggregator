"""
copytrade-aggregator — نسخ الصفقات المجمّع (مستقل تماماً عن ZONES v1)

إطار عمل لجمع إشارات نسخ التداول من متداولين محترفين عبر منصات متعددة،
وتجميع الإشارات المتفقة على نفس الزوج والاتجاه مع مستويات دخول متقاربة،
وإصدار إشارة مجمعة مع درجة ثقة من 100 نقطة.

المكونات:
- models: نموذج البيانات (Signal, AggregatedSignal, CopyTradeAggregator)
- classifier: تصنيف المتداولين (4-way: PROFESSIONAL_ACTIVE/INACTIVE/REGULAR/SUSPICIOUS)
- scorer: نظام الثقة من 100 نقطة
- engine: محرك التوصيات المجمعة (RecommendationEngine)
- aggregator: محرك التجميع البسيط (CopyTradeEngine)
- connectors: وصلات المنصات (8 منصات)
- telegram: إشعارات التيليجرام مع أمر /details
- backtest: اختبار التاريخي للإشارات

جميع النتائج افتراضية (HYPOTHETICAL_) ولا تشكل نصيحة استثمارية.
"""

from .models import Signal, AggregatedSignal, CopyTradeAggregator
from .classifier import TraderClassifier, TraderProfile
from .scorer import ConfidenceScorer, ConfidenceResult
from .engine import RecommendationEngine, Recommendation
from .aggregator import CopyTradeEngine, EngineConfig, AggregationResult
from .connectors import (
    RawFill,
    BaseConnector,
    HyperliquidConnector,
    DydxConnector,
    Collective2Connector,
    MyfxbookConnector,
    ZuluTradeConnector,
    DarwinexConnector,
    BirdeyeConnector,
    DeBankConnector,
    ALL_CONNECTORS,
    get_connector,
    list_platforms,
    normalize_fill_to_signal,
)
from .telegram import TelegramNotifier, TelegramConfig
from .backtest import BacktestResult, backtest_signal, summarize_backtest

__version__ = "1.0.0"
__all__ = [
    "Signal",
    "AggregatedSignal",
    "CopyTradeAggregator",
    "TraderProfile",
    "TraderClassifier",
    "ConfidenceScorer",
    "ConfidenceResult",
    "RecommendationEngine",
    "Recommendation",
    "CopyTradeEngine",
    "EngineConfig",
    "AggregationResult",
    "RawFill",
    "BaseConnector",
    "HyperliquidConnector",
    "DydxConnector",
    "Collective2Connector",
    "MyfxbookConnector",
    "ZuluTradeConnector",
    "DarwinexConnector",
    "BirdeyeConnector",
    "DeBankConnector",
    "ALL_CONNECTORS",
    "get_connector",
    "list_platforms",
    "normalize_fill_to_signal",
    "TelegramNotifier",
    "TelegramConfig",
    "BacktestResult",
    "backtest_signal",
    "summarize_backtest",
]
