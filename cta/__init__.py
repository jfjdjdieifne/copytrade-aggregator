"""cta — Copy-Trade Aggregator v2 ("consensus radar").

Runs as a Cloudflare Python Worker with zero third-party dependencies:
only the Python standard library plus Cloudflare bindings injected via `env`.

Public surface
--------------
classify_trader      : TraderStats -> (grade, score)      — who counts as proven
detect_convergence   : [Position] -> [Cluster]            — agreement detection
score_cluster        : Cluster -> Confidence             — 0..100 confidence
build_plan           : Cluster -> TradePlan              — entry / stop / target / RR
"""

from .config import Config
from .models import (
    Position,
    TradePlan,
    TraderStats,
    Cluster,
    Confidence,
    Signal,
)
from .classifier import classify_trader, equity_from_fills, stats_from_fills
from .convergence import detect_convergence
from .confidence import score_cluster
from .plan import build_plan

__version__ = "2.0.0"

__all__ = [
    "Config",
    "Position",
    "TradePlan",
    "TraderStats",
    "Cluster",
    "Confidence",
    "Signal",
    "classify_trader",
    "equity_from_fills",
    "stats_from_fills",
    "detect_convergence",
    "score_cluster",
    "build_plan",
    "__version__",
]
