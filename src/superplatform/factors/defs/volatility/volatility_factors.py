"""Volatility factor definitions."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


@factor(
    name="realized_vol",
    category=FactorCategory.VOLATILITY,
    description="N 日年化已实现波动率（N 由 lookback_days 配置，默认 20）",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "波动率计算回看天数",
            "min": 1,
            "max": 500,
        },
        **factor_params(annualization=365),
    },
)
def realized_vol(data, **params):
    annualization = params.get("annualization", 365)
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    returns = kline["close"].pct_change()
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = returns.rolling(period).std() * (annualization**0.5)
    return result


@factor(
    name="high_low_range",
    category=FactorCategory.VOLATILITY,
    description="Mean intrabar range relative to close over a configurable calendar-day lookback",
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
def high_low_range(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    daily_range = (kline["high"] - kline["low"]) / kline["close"]
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = daily_range.rolling(period).mean()
    return result


@factor(
    name="downside_vol",
    category=FactorCategory.VOLATILITY,
    description="20-period annualized downside volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20, annualization=365),
)
def downside_vol(data, **params):
    annualization = params.get("annualization", 365)
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    downside_return = kline["close"].pct_change().clip(upper=0)
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = (
        downside_return.pow(2).rolling(period).mean().pow(0.5) * annualization**0.5
    )
    return result


@factor(
    name="parkinson_vol",
    category=FactorCategory.VOLATILITY,
    description="20-period annualized Parkinson high-low volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20, annualization=365),
)
def parkinson_vol(data, **params):
    annualization = params.get("annualization", 365)
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    squared_log_range = np.log(kline["high"] / kline["low"]).pow(2)
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = (
        (squared_log_range.rolling(period).mean() / (4.0 * 0.6931471805599453)).pow(0.5)
        * annualization**0.5
    )
    return result


@factor(
    name="volatility_of_volatility",
    category=FactorCategory.VOLATILITY,
    description="60-period standard deviation of 20-period realized volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(vol_window_days=20, window_days=60),
)
def volatility_of_volatility(data, **params):
    kline = list(data["kline"].values())[0]
    vol_period = lookback_bars(kline, params.get("vol_window_days", 20))
    window = lookback_bars(kline, params.get("window_days", 60))
    realized_vol = kline["close"].pct_change().rolling(vol_period).std()
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = realized_vol.rolling(window).std()
    return result
