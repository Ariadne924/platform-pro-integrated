"""Additional momentum and trend factors."""

import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


@factor(
    name="momentum_skip",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="20-day momentum measured after skipping the most recent 5 days",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(skip_days=5, lookback_days=20),
)
def momentum_skip(data, **params):
    kline = _kline(data)
    skip = lookback_bars(kline, params.get("skip_days", 5))
    lookback = lookback_bars(kline, params.get("lookback_days", 20))
    close = kline["close"]
    return _result(kline, close.shift(skip) / close.shift(skip + lookback) - 1)


@factor(
    name="momentum_consistency",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Balance of up and down return observations over 20 days",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def momentum_consistency(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    returns = kline["close"].pct_change()
    value = returns.gt(0).rolling(period).mean() - returns.lt(0).rolling(period).mean()
    return _result(kline, value)


@factor(
    name="trend_strength",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Absolute cumulative return divided by cumulative absolute return",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def trend_strength(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    returns = kline["close"].pct_change()
    value = returns.rolling(period).sum().abs() / (
        returns.abs().rolling(period).sum() + epsilon
    )
    return _result(kline, value)


@factor(
    name="moving_average_distance",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Distance of close from its 20-day simple moving average",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def moving_average_distance(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    average = kline["close"].rolling(period).mean()
    return _result(kline, kline["close"] / average - 1)


@factor(
    name="moving_average_crossover",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Short moving average relative to long moving average",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(short_window_days=10, long_window_days=30),
)
def moving_average_crossover(data, **params):
    kline = _kline(data)
    short_period = lookback_bars(kline, params.get("short_window_days", 10))
    long_period = lookback_bars(kline, params.get("long_window_days", 30))
    close = kline["close"]
    value = close.rolling(short_period).mean() / close.rolling(long_period).mean() - 1
    return _result(kline, value)


@factor(
    name="donchian_position",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="Close location inside the trailing 20-day channel",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def donchian_position(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    close = kline["close"]
    high = close.rolling(period).max()
    low = close.rolling(period).min()
    return _result(kline, (close - low) / (high - low).replace(0, pd.NA))
