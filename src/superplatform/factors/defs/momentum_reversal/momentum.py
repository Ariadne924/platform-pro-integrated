"""Momentum and reversal factor definitions."""

import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


@factor(
    name="momentum",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Price momentum over a configurable calendar-day lookback",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "回看天数",
            "min": 1,
            "max": 500,
        }
    },
)
def momentum(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = kline["close"] / kline["close"].shift(period) - 1
    return result


@factor(
    name="short_term_reversal",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="5-day reversal: negative of 5-day return",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 5,
            "description": "回看天数",
            "min": 1,
            "max": 500,
        }
    },
)
def short_term_reversal(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 5))
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = -(kline["close"] / kline["close"].shift(period) - 1)
    return result


@factor(
    name="momentum_acceleration",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="20-period momentum less 5-period momentum",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(long_window_days=20, short_window_days=5),
)
def momentum_acceleration(data, **params):
    kline = list(data["kline"].values())[0]
    long_period = lookback_bars(kline, params.get("long_window_days", 20))
    short_period = lookback_bars(kline, params.get("short_window_days", 5))
    close = kline["close"]
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = close.pct_change(long_period) - close.pct_change(short_period)
    return result


@factor(
    name="breakout_distance",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Distance from the current close to the trailing calendar-day high",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "回看天数",
            "min": 1,
            "max": 500,
        }
    },
)
def breakout_distance(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    trailing_high = kline["close"].rolling(period).max()
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = kline["close"] / trailing_high - 1.0
    return result
