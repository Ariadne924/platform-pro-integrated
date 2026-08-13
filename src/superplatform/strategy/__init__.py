"""Strategy layer -- consumes factors, produces position signals."""

from superplatform.strategy.base import Strategy, StrategySignal, strategy
from superplatform.strategy.registry import StrategyRegistry

__all__ = ["Strategy", "StrategySignal", "strategy", "StrategyRegistry"]
