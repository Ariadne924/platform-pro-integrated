"""Consumption layer -- consumes strategy signals.

Sits above strategy layer. Runs backtests, paper trading, or forwards
signals to live execution. Currently implements a simple backtester.
"""

from superplatform.consumption.backtest import BacktestResult, backtest
from superplatform.consumption.base import ConsumerConfig, Strictness

__all__ = ["BacktestResult", "ConsumerConfig", "Strictness", "backtest"]
