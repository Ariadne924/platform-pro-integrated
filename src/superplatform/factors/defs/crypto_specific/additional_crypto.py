"""Additional crypto-specific factors derived from 24/7 K-line activity."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


def _kline(data):
    return list(data["kline"].values())[0]


def _result(kline, value):
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": value})


def _weekend_mask(kline):
    return pd.to_datetime(kline["timestamp"], utc=True).dt.dayofweek >= 5


@factor(
    name="crypto_weekend_return",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Average weekend return over the trailing 20 observations",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_weekend_return(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    returns = kline["close"].pct_change()
    # Weekend bars are only ~2/7 of the data — a window of `period` weekend bars
    # never accumulates, so relax min_periods to keep the trailing average finite.
    return _result(kline, returns.where(_weekend_mask(kline)).rolling(period, min_periods=1).mean())


@factor(
    name="crypto_weekend_weekday_volume_ratio",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Weekend volume relative to weekday volume in a trailing window",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_weekend_weekday_volume_ratio(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    weekend = _weekend_mask(kline)
    weekend_volume = kline["volume"].where(weekend)
    weekday_volume = kline["volume"].where(~weekend)
    # Each side only has ~2/7 (weekend) or ~5/7 (weekday) of the bars in any
    # window — a full window never accumulates, so relax min_periods on both.
    return _result(
        kline,
        weekend_volume.rolling(period, min_periods=1).mean()
        / (weekday_volume.rolling(period, min_periods=1).mean() + epsilon),
    )


@factor(
    name="crypto_downside_volume_share",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Share of volume occurring on negative-return bars",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_downside_volume_share(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    returns = kline["close"].pct_change()
    downside_volume = kline["volume"].where(returns < 0, 0.0)
    return _result(
        kline,
        downside_volume.rolling(period).sum()
        / (kline["volume"].rolling(period).sum() + epsilon),
    )


@factor(
    name="crypto_upside_volume_share",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Share of volume occurring on positive-return bars",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_upside_volume_share(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    returns = kline["close"].pct_change()
    upside_volume = kline["volume"].where(returns > 0, 0.0)
    return _result(
        kline,
        upside_volume.rolling(period).sum()
        / (kline["volume"].rolling(period).sum() + epsilon),
    )


@factor(
    name="crypto_return_autocorrelation",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Lag-one autocorrelation of returns over 20 observations",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_return_autocorrelation(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    returns = kline["close"].pct_change()
    return _result(kline, returns.rolling(period).corr(returns.shift(1)))


@factor(
    name="crypto_jump_intensity",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Share of recent returns exceeding a rolling two-standard-deviation threshold",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=60, threshold_window_days=20),
)
def crypto_jump_intensity(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 60))
    threshold_period = lookback_bars(kline, params.get("threshold_window_days", 20))
    returns = kline["close"].pct_change()
    threshold = returns.abs().rolling(threshold_period).mean() + 2 * returns.abs().rolling(
        threshold_period
    ).std()
    jumps = returns.abs().gt(threshold).astype(float)
    return _result(kline, jumps.rolling(period).mean())


@factor(
    name="crypto_volume_concentration",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Herfindahl concentration of volume across the trailing 20 observations",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_volume_concentration(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)

    def concentration(window):
        shares = window / (window.sum() + epsilon)
        return float(np.square(shares).sum())

    value = kline["volume"].rolling(period).apply(concentration, raw=False)
    return _result(kline, value)


@factor(
    name="crypto_price_range_volume_corr",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Correlation between intrabar range and volume changes",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_price_range_volume_corr(data, **params):
    kline = _kline(data)
    period = lookback_bars(kline, params.get("lookback_days", 20))
    range_ratio = (kline["high"] - kline["low"]) / kline["close"]
    volume_change = np.log(kline["volume"].clip(lower=1e-12)).diff()
    return _result(kline, range_ratio.rolling(period).corr(volume_change))
