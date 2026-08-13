"""Additional volume and liquidity regime factors."""

import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


@factor(
    name="dollar_volume_momentum",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-period growth in traded dollar volume",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Dollar-volume momentum lookback period",
            "min": 1,
            "max": 500,
        }
    },
)
def dollar_volume_momentum(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    dollar_volume = kline["close"] * kline["volume"]
    return _result(kline, dollar_volume.pct_change(period))


@factor(
    name="liquidity_regime_ratio",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="Short traded-dollar-volume regime relative to its long regime",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "short_window_days": {
            "type": "int",
            "default": 20,
            "description": "Short liquidity window",
            "min": 2,
            "max": 500,
        },
        "long_window_days": {
            "type": "int",
            "default": 60,
            "description": "Long liquidity window",
            "min": 3,
            "max": 1000,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for long dollar volume",
            "min": 0.0,
        },
    },
)
def liquidity_regime_ratio(data, **params):
    kline = _kline(data)
    short_period = lookback_bars(kline, params.get("short_window_days", 20))
    long_period = lookback_bars(kline, params.get("long_window_days", 60))
    epsilon = params.get("epsilon", 1.0e-12)
    dollar_volume = kline["close"] * kline["volume"]
    short = dollar_volume.rolling(short_period).mean()
    long = dollar_volume.rolling(long_period).mean()
    return _result(kline, short / (long + epsilon))


@factor(
    name="amihud_illiquidity_change",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="Change in Amihud-style illiquidity versus the previous window",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Illiquidity lookback period",
            "min": 2,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for traded dollar volume",
            "min": 0.0,
        },
    },
)
def amihud_illiquidity_change(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    dollar_volume = (kline["close"] * kline["volume"]).clip(lower=epsilon)
    illiquidity = (kline["close"].pct_change().abs() / dollar_volume).rolling(period).mean()
    return _result(kline, illiquidity / (illiquidity.shift(period) + epsilon) - 1.0)


@factor(
    name="turnover_volatility",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="Coefficient of variation of volume-to-price turnover",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Turnover volatility lookback period",
            "min": 2,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for mean turnover",
            "min": 0.0,
        },
    },
)
def turnover_volatility(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    turnover = kline["volume"] / kline["close"].clip(lower=epsilon)
    return _result(kline, turnover.rolling(period).std() / (turnover.rolling(period).mean() + epsilon))
