"""Additional candle microstructure persistence factors."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


def _price_range(kline):
    # np.nan, not pd.NA: pd.NA upcasts the series to object dtype, which
    # makes rolling().mean()/corr() raise DataError on zero-range bars.
    return (kline["high"] - kline["low"]).replace(0, np.nan)


@factor(
    name="wick_asymmetry",
    category=FactorCategory.MICROSTRUCTURE,
    description="Upper-wick rejection relative to lower-wick rejection",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Wick asymmetry lookback period",
            "min": 2,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for total wick size",
            "min": 0.0,
        },
    },
)
def wick_asymmetry(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    body_high = kline[["open", "close"]].max(axis=1)
    body_low = kline[["open", "close"]].min(axis=1)
    upper = kline["high"] - body_high
    lower = body_low - kline["low"]
    asymmetry = (upper - lower) / (upper + lower + epsilon)
    return _result(kline, asymmetry.rolling(period).mean())


@factor(
    name="range_autocorrelation",
    category=FactorCategory.MICROSTRUCTURE,
    description="Persistence of intrabar range across adjacent observations",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Range autocorrelation lookback period",
            "min": 3,
            "max": 500,
        }
    },
)
def range_autocorrelation(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    range_ratio = (kline["high"] - kline["low"]) / kline["close"]
    return _result(kline, range_ratio.rolling(period).corr(range_ratio.shift(1)))


@factor(
    name="close_location_surprise",
    category=FactorCategory.MICROSTRUCTURE,
    description="Current close location relative to its trailing average",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Close-location baseline period",
            "min": 2,
            "max": 500,
        }
    },
)
def close_location_surprise(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    location = (kline["close"] - kline["low"]) / _price_range(kline)
    # min_periods lets zero-range (NaN) bars be skipped instead of making
    # every trailing window NaN and the whole series non-finite.
    baseline = location.rolling(
        period, min_periods=max(1, period // 2)
    ).mean()
    return _result(kline, location - baseline)


@factor(
    name="candle_direction_autocorrelation",
    category=FactorCategory.MICROSTRUCTURE,
    description="Persistence of bullish or bearish candle direction",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Candle direction autocorrelation period",
            "min": 3,
            "max": 500,
        }
    },
)
def candle_direction_autocorrelation(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    direction = (kline["close"] > kline["open"]).astype(float) - (
        kline["close"] < kline["open"]
    ).astype(float)
    value = direction.rolling(period).corr(direction.shift(1))
    # A constant direction (e.g. every bar bullish) has an undefined
    # autocorrelation; fall back to 0 (no detectable persistence) so the
    # factor stays finite on zero-range / one-sided bars.
    return _result(kline, value.fillna(0.0))
