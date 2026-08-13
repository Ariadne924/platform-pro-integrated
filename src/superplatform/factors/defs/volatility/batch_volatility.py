"""Additional volatility and tail-shape factors."""

import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


@factor(
    name="upside_downside_vol_ratio",
    category=FactorCategory.VOLATILITY,
    description="Upside semivolatility relative to downside semivolatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Semivolatility lookback period",
            "min": 2,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for downside semivolatility",
            "min": 0.0,
        },
    },
)
def upside_downside_vol_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    returns = kline["close"].pct_change()
    upside = returns.clip(lower=0).pow(2).rolling(period).mean().pow(0.5)
    downside = (-returns.clip(upper=0)).pow(2).rolling(period).mean().pow(0.5)
    return _result(kline, upside / (downside + epsilon))


@factor(
    name="realized_vol_change",
    category=FactorCategory.VOLATILITY,
    description="Change in realized volatility versus the previous lookback window",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Realized volatility lookback period",
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
def realized_vol_change(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    returns = kline["close"].pct_change()
    volatility = returns.rolling(period).std()
    return _result(kline, volatility / (volatility.shift(period) + epsilon) - 1.0)


@factor(
    name="return_tail_to_median_ratio",
    category=FactorCategory.VOLATILITY,
    description="Rolling upper-tail absolute return relative to its median",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 60,
            "description": "Tail-shape lookback period",
            "min": 5,
            "max": 1000,
        },
        "quantile": {
            "type": "float",
            "default": 0.95,
            "description": "Absolute-return tail quantile",
            "min": 0.5,
            "max": 0.999,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for the median",
            "min": 0.0,
        },
    },
)
def return_tail_to_median_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 60))
    quantile = params.get("quantile", 0.95)
    epsilon = params.get("epsilon", 1.0e-12)
    absolute_returns = kline["close"].pct_change().abs()
    tail = absolute_returns.rolling(period).quantile(quantile)
    median = absolute_returns.rolling(period).median()
    return _result(kline, tail / (median + epsilon))
