"""Additional range and return distribution volatility factors."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


def _true_range(kline):
    previous_close = kline["close"].shift(1)
    return pd.concat(
        [
            kline["high"] - kline["low"],
            (kline["high"] - previous_close).abs(),
            (kline["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


@factor(
    name="atr_ratio",
    category=FactorCategory.VOLATILITY,
    description="14-day average true range normalized by close",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=14),
)
def atr_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 14))
    return _result(kline, _true_range(kline).rolling(period).mean() / kline["close"])


@factor(
    name="true_range_ratio",
    category=FactorCategory.VOLATILITY,
    description="20-day mean true range relative to previous close",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def true_range_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    previous_close = kline["close"].shift(1)
    return _result(kline, (_true_range(kline) / previous_close).rolling(period).mean())


@factor(
    name="garman_klass_vol",
    category=FactorCategory.VOLATILITY,
    description="20-day annualized Garman-Klass OHLC volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20, annualization=365),
)
def garman_klass_vol(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    annualization = params.get("annualization", 365)
    log_hl = np.log(kline["high"] / kline["low"])
    log_co = np.log(kline["close"] / kline["open"])
    variance = (
        0.5 * log_hl.pow(2) - (2 * np.log(2) - 1) * log_co.pow(2)
    ).rolling(period).mean()
    return _result(kline, variance.clip(lower=0).pow(0.5) * annualization**0.5)


@factor(
    name="rogers_satchell_vol",
    category=FactorCategory.VOLATILITY,
    description="20-day annualized Rogers-Satchell OHLC volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20, annualization=365),
)
def rogers_satchell_vol(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    annualization = params.get("annualization", 365)
    high_component = np.log(kline["high"] / kline["close"]) * np.log(
        kline["high"] / kline["open"]
    )
    low_component = np.log(kline["low"] / kline["close"]) * np.log(
        kline["low"] / kline["open"]
    )
    variance = (high_component + low_component).rolling(period).mean()
    return _result(kline, variance.clip(lower=0).pow(0.5) * annualization**0.5)


@factor(
    name="return_skewness",
    category=FactorCategory.VOLATILITY,
    description="60-day rolling skewness of close-to-close returns",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=60),
)
def return_skewness(data, **params):
    kline = _kline(data)
    returns = kline["close"].pct_change()
    return _result(kline, returns.rolling(lookback_bars(kline, params.get("lookback_days", 60))).skew())


@factor(
    name="return_kurtosis",
    category=FactorCategory.VOLATILITY,
    description="60-day rolling excess kurtosis of returns",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=60),
)
def return_kurtosis(data, **params):
    kline = _kline(data)
    returns = kline["close"].pct_change()
    return _result(kline, returns.rolling(lookback_bars(kline, params.get("lookback_days", 60))).kurt())


@factor(
    name="range_realized_vol_ratio",
    category=FactorCategory.VOLATILITY,
    description="Average intrabar range divided by close-to-close realized volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def range_realized_vol_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    range_ratio = (kline["high"] - kline["low"]) / kline["close"]
    realized = kline["close"].pct_change().rolling(period).std()
    return _result(kline, range_ratio.rolling(period).mean() / (realized + epsilon))


@factor(
    name="volatility_regime_ratio",
    category=FactorCategory.VOLATILITY,
    description="Short realized volatility relative to long realized volatility",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(short_window_days=20, long_window_days=60),
)
def volatility_regime_ratio(data, **params):
    kline = _kline(data)
    short_period = lookback_bars(kline, params.get("short_window_days", 20))
    long_period = lookback_bars(kline, params.get("long_window_days", 60))
    epsilon = params.get("epsilon", 1e-12)
    returns = kline["close"].pct_change()
    value = returns.rolling(short_period).std() / (
        returns.rolling(long_period).std() + epsilon
    )
    return _result(kline, value)
