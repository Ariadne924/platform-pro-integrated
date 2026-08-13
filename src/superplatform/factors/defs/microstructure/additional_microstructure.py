"""Additional candle and bar microstructure factors."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


def _price_range(kline):
    # np.nan keeps the column numeric; pd.NA would upcast to object dtype and
    # make downstream rolling/sum calls fail with a pandas DataError.
    return (kline["high"] - kline["low"]).replace(0, np.nan)


@factor(
    name="body_to_range",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-day mean absolute candle body share of range",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def body_to_range(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    body = (kline["close"] - kline["open"]).abs()
    # _price_range is NaN on doji bars (high == low); min_periods=1 keeps the
    # trailing mean finite instead of every window containing a NaN.
    return _result(kline, (body / _price_range(kline)).rolling(period, min_periods=1).mean())


@factor(
    name="upper_wick_ratio",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-day mean upper wick share of bar range",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def upper_wick_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    body_high = kline[["open", "close"]].max(axis=1)
    upper_wick = kline["high"] - body_high
    return _result(kline, (upper_wick / _price_range(kline)).rolling(period, min_periods=1).mean())


@factor(
    name="lower_wick_ratio",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-day mean lower wick share of bar range",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def lower_wick_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    body_low = kline[["open", "close"]].min(axis=1)
    lower_wick = body_low - kline["low"]
    return _result(kline, (lower_wick / _price_range(kline)).rolling(period, min_periods=1).mean())


@factor(
    name="candle_efficiency",
    category=FactorCategory.MICROSTRUCTURE,
    description="Net price displacement divided by cumulative bar range",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def candle_efficiency(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    net_move = kline["close"].diff(period).abs()
    cumulative_range = (kline["high"] - kline["low"]).rolling(period).sum()
    return _result(kline, net_move / (cumulative_range + epsilon))


@factor(
    name="range_expansion",
    category=FactorCategory.MICROSTRUCTURE,
    description="Current bar range relative to its 20-day average range",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def range_expansion(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    range_ratio = (kline["high"] - kline["low"]) / kline["close"]
    return _result(kline, range_ratio / (range_ratio.rolling(period).mean() + epsilon))


@factor(
    name="candle_body_direction",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-day average candle direction",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def candle_body_direction(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    direction = (kline["close"] > kline["open"]).astype(float) - (
        kline["close"] < kline["open"]
    ).astype(float)
    return _result(kline, direction.rolling(period).mean())


@factor(
    name="close_location_dispersion",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-day dispersion of close location within bar ranges",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def close_location_dispersion(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    location = (kline["close"] - kline["low"]) / _price_range(kline)
    return _result(kline, location.rolling(period, min_periods=1).std())


@factor(
    name="range_weighted_return",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-day range-weighted close-to-close return",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def range_weighted_return(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    returns = kline["close"].pct_change()
    weights = (kline["high"] - kline["low"]) / kline["close"]
    return _result(
        kline,
        (returns * weights).rolling(period).sum()
        / (weights.rolling(period).sum() + epsilon),
    )
