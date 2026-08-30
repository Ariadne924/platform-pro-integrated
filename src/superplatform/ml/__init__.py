"""Leakage-controlled machine-learning research primitives."""

from superplatform.ml.comparison import compare_strategy_returns
from superplatform.ml.models import WalkForwardConfig, WalkForwardResult, walk_forward_panel
from superplatform.ml.multifrequency import MultiFrequencyResult, fuse_factor_panels
from superplatform.ml.portfolio import (
    PortfolioConfig,
    allocate_weights,
    build_portfolio_signals,
)
from superplatform.ml.regime import RegimeConfig, detect_market_regime
from superplatform.ml.risk import ScoreConfig, score_research_result, tail_risk_metrics
from superplatform.ml.tail_models import DynamicRiskEstimate, estimate_dynamic_risk

__all__ = [
    "RegimeConfig",
    "DynamicRiskEstimate",
    "PortfolioConfig",
    "MultiFrequencyResult",
    "ScoreConfig",
    "WalkForwardConfig",
    "WalkForwardResult",
    "compare_strategy_returns",
    "allocate_weights",
    "build_portfolio_signals",
    "fuse_factor_panels",
    "detect_market_regime",
    "estimate_dynamic_risk",
    "score_research_result",
    "tail_risk_metrics",
    "walk_forward_panel",
]
