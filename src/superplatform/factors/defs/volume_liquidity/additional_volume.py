"""Additional volume and liquidity factors."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


@factor(
    name="volume_momentum",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-day volume growth",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def volume_momentum(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    # Explicit ratio with epsilon: pct_change(period) hits +Inf when the lagged
    # volume is zero, punching holes in the factor series.
    prev = kline["volume"].shift(period)
    return _result(kline, (kline["volume"] - prev) / (prev + epsilon))


@factor(
    name="volume_regime_ratio",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="Short average volume relative to long average volume",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(short_window_days=20, long_window_days=60),
)
def volume_regime_ratio(data, **params):
    kline = _kline(data)
    short_period = lookback_bars(kline, params.get("short_window_days", 20))
    long_period = lookback_bars(kline, params.get("long_window_days", 60))
    epsilon = params.get("epsilon", 1e-12)
    short = kline["volume"].rolling(short_period).mean()
    long = kline["volume"].rolling(long_period).mean()
    return _result(kline, short / (long + epsilon))


@factor(
    name="volume_shock",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="Current volume relative to its rolling median",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def volume_shock(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    return _result(kline, kline["volume"] / (kline["volume"].rolling(period).median() + epsilon))


@factor(
    name="obv_momentum",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-day change in on-balance volume normalized by traded volume",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def obv_momentum(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    returns = kline["close"].pct_change()
    signed_volume = kline["volume"].where(returns > 0, -kline["volume"].where(returns < 0, 0.0))
    obv = signed_volume.cumsum()
    return _result(kline, obv.diff(period) / (kline["volume"].rolling(period).sum() + epsilon))


@factor(
    name="accumulation_distribution",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-day accumulation-distribution flow normalized by volume",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def accumulation_distribution(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    # np.nan keeps the column numeric; pd.NA would upcast to object dtype and
    # make downstream rolling/sum calls fail with a pandas DataError.
    price_range = (kline["high"] - kline["low"]).replace(0, np.nan)
    money_flow_multiplier = (
        (2 * kline["close"] - kline["high"] - kline["low"]) / price_range
    ).fillna(0.0)  # doji bars (high == low) contribute no directional flow
    flow = money_flow_multiplier * kline["volume"]
    return _result(
        kline,
        flow.rolling(period).sum() / (kline["volume"].rolling(period).sum() + epsilon),
    )


@factor(
    name="money_flow_index",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="14-day volume-weighted buying and selling pressure index",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=14),
)
def money_flow_index(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 14))
    epsilon = params.get("epsilon", 1e-12)
    typical_price = (kline["high"] + kline["low"] + kline["close"]) / 3
    raw_flow = typical_price * kline["volume"]
    direction = typical_price.diff()
    positive = raw_flow.where(direction > 0, 0.0).rolling(period).sum()
    negative = raw_flow.where(direction < 0, 0.0).rolling(period).sum()
    money_ratio = positive / (negative + epsilon)
    return _result(kline, 100 - 100 / (1 + money_ratio))


@factor(
    name="vwap_deviation",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="Close deviation from the 20-day volume-weighted average price",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def vwap_deviation(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    typical_price = (kline["high"] + kline["low"] + kline["close"]) / 3
    vwap = (typical_price * kline["volume"]).rolling(period).sum() / (
        kline["volume"].rolling(period).sum() + epsilon
    )
    return _result(kline, kline["close"] / vwap - 1)


@factor(
    name="volume_weighted_return",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-day volume-weighted close-to-close return",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def volume_weighted_return(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    returns = kline["close"].pct_change()
    return _result(
        kline,
        (returns * kline["volume"]).rolling(period).sum()
        / (kline["volume"].rolling(period).sum() + epsilon),
    )
