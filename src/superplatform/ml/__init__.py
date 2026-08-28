"""Leakage-controlled machine-learning research primitives."""

from superplatform.ml.comparison import compare_strategy_returns
from superplatform.ml.models import WalkForwardConfig, WalkForwardResult, walk_forward_panel
from superplatform.ml.regime import RegimeConfig, detect_market_regime
from superplatform.ml.risk import ScoreConfig, score_research_result, tail_risk_metrics

__all__ = [
    "RegimeConfig",
    "ScoreConfig",
    "WalkForwardConfig",
    "WalkForwardResult",
    "compare_strategy_returns",
    "detect_market_regime",
    "score_research_result",
    "tail_risk_metrics",
    "walk_forward_panel",
]
