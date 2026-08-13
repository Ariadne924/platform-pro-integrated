"""Additional momentum factors for the expanded factor batch."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


@factor(
    name="momentum_vol_adjusted",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="20-day momentum scaled by realized return volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Momentum and volatility lookback period",
            "min": 2,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for volatility",
            "min": 0.0,
        },
    },
)
def momentum_vol_adjusted(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    returns = kline["close"].pct_change()
    volatility = returns.rolling(period).std() * np.sqrt(period)
    value = kline["close"].pct_change(period) / (volatility + epsilon)
    return _result(kline, value)


@factor(
    name="trend_tstat",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Rolling return mean divided by return standard error",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Trend test lookback period",
            "min": 3,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for return volatility",
            "min": 0.0,
        },
    },
)
def trend_tstat(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    returns = kline["close"].pct_change()
    mean = returns.rolling(period).mean()
    std_error = returns.rolling(period).std() / np.sqrt(period)
    return _result(kline, mean / (std_error + epsilon))
