"""Binance market-data proxies for MVRV-like valuation.

These factors are deliberately named ``proxy``: Binance klines do not contain
on-chain realized capitalization.  Rolling quote-volume VWAP is used as an
observable approximation of aggregate acquisition cost, so the proxy ratio is
current price divided by that trailing cost basis.
"""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.utils.timestamps import lookback_bars

_LOOKBACK_SCHEMA = {
    "lookback_days": {
        "type": "int",
        "default": 365,
        "description": "Trailing Binance trading history used as the cost-basis proxy",
        "min": 2,
        "max": 3650,
    }
}


def _inputs(data, lookback_days: int) -> tuple[pd.DataFrame, int, pd.Series, pd.Series]:
    kline = list(data["kline"].values())[0].sort_values("timestamp", kind="stable")
    period = lookback_bars(kline, lookback_days)
    close = pd.to_numeric(kline["close"], errors="coerce").where(lambda x: x > 0)

    # Prefer the exchange-reported quote volume. Fall back to base volume *
    # close for compatible historical files where quote_volume is unavailable.
    if "quote_volume" in kline.columns:
        quote_volume = pd.to_numeric(kline["quote_volume"], errors="coerce")
    else:
        base_volume = pd.to_numeric(kline["volume"], errors="coerce")
        quote_volume = base_volume * close
    quote_volume = quote_volume.where(quote_volume >= 0)
    return kline, period, close, quote_volume


def _proxy_ratio(
    close: pd.Series,
    quote_volume: pd.Series,
    period: int,
) -> tuple[pd.Series, pd.Series]:
    # base volume ~= quote volume / close. This makes rolling VWAP equal to
    # sum(quote volume) / sum(base volume) while using Binance schema fields.
    base_volume = quote_volume / close
    min_periods = period
    rolling_quote = quote_volume.rolling(period, min_periods=min_periods).sum()
    rolling_base = base_volume.rolling(period, min_periods=min_periods).sum()
    cost_basis = rolling_quote / rolling_base.replace(0, np.nan)
    ratio = close / cost_basis.replace(0, np.nan)
    return ratio, cost_basis


@factor(
    name="binance_mvrv_proxy",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Price / trailing Binance quote-volume VWAP; not on-chain MVRV",
    required_data=["kline"],
    required_symbols=1,
    params_schema=_LOOKBACK_SCHEMA,
)
def binance_mvrv_proxy(data, **params):
    kline, period, close, quote_volume = _inputs(
        data, params.get("lookback_days", 365)
    )
    ratio, _ = _proxy_ratio(close, quote_volume, period)
    return pd.DataFrame({"timestamp": kline["timestamp"].copy(), "value": ratio})


@factor(
    name="binance_realized_price_proxy",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Trailing Binance quote-volume VWAP used as a realized-price proxy",
    required_data=["kline"],
    required_symbols=1,
    params_schema=_LOOKBACK_SCHEMA,
)
def binance_realized_price_proxy(data, **params):
    kline, period, close, quote_volume = _inputs(
        data, params.get("lookback_days", 365)
    )
    _, cost_basis = _proxy_ratio(close, quote_volume, period)
    return pd.DataFrame({"timestamp": kline["timestamp"].copy(), "value": cost_basis})


@factor(
    name="binance_mvrv_proxy_change",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Change in the Binance MVRV proxy over a configurable horizon",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        **_LOOKBACK_SCHEMA,
        "change_days": {
            "type": "int",
            "default": 30,
            "description": "Calendar-day horizon for proxy percentage change",
            "min": 1,
            "max": 1000,
        },
    },
)
def binance_mvrv_proxy_change(data, **params):
    kline, period, close, quote_volume = _inputs(
        data, params.get("lookback_days", 365)
    )
    change_period = lookback_bars(kline, params.get("change_days", 30))
    ratio, _ = _proxy_ratio(close, quote_volume, period)
    value = ratio.pct_change(change_period, fill_method=None)
    return pd.DataFrame({"timestamp": kline["timestamp"].copy(), "value": value})


@factor(
    name="binance_mvrv_proxy_zscore",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Trailing standardized Binance MVRV proxy",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        **_LOOKBACK_SCHEMA,
        "zscore_days": {
            "type": "int",
            "default": 730,
            "description": "Calendar-day window used to standardize the proxy",
            "min": 2,
            "max": 3650,
        },
    },
)
def binance_mvrv_proxy_zscore(data, **params):
    kline, period, close, quote_volume = _inputs(
        data, params.get("lookback_days", 365)
    )
    zscore_period = lookback_bars(kline, params.get("zscore_days", 730))
    ratio, _ = _proxy_ratio(close, quote_volume, period)
    mean = ratio.rolling(zscore_period, min_periods=zscore_period).mean()
    std = ratio.rolling(zscore_period, min_periods=zscore_period).std(ddof=1)
    value = (ratio - mean) / std.replace(0, np.nan)
    return pd.DataFrame({"timestamp": kline["timestamp"].copy(), "value": value})
