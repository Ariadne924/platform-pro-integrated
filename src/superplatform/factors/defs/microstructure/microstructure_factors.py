"""Microstructure proxy factors derived from kline data.

These use OHLCV as proxies for microstructure signals:
  - close_location: where close sits in bar range → order-imbalance proxy

The intra-bar range / close spread proxy previously registered here
(high_low_spread_14) is the same formula as the volatility factor
``high_low_range`` — it was deduplicated into that configurable factor.

For true microstructure factors (order-book imbalance, trade flow, etc.)
the required_data would be ["order_book"] or ["trade"].
"""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


@factor(
    name="close_location",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-period mean of (close - low) / (high - low) — where close sits in bar",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "收盘位置均值回看天数",
            "min": 1,
            "max": 500,
        }
    },
)
def close_location(data, **params):
    """Where the close price falls within the bar's high-low range.

    Value ∈ [0, 1]:
      1 → closed at the high  (buying pressure)
      0 → closed at the low   (selling pressure)

    Averaged over `period` bars to smooth noise.
    """
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))

    hl_range = kline["high"] - kline["low"]
    # Avoid division by zero (flat bars are rare but possible). np.nan keeps the
    # column numeric — pd.NA would upcast to object dtype and break the rolling
    # mean with DataError. min_periods=1 keeps it finite when doji bars recur.
    hl_range = hl_range.replace(0, np.nan)

    location = (kline["close"] - kline["low"]) / hl_range
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = location.rolling(period, min_periods=1).mean()
    return result


@factor(
    name="kline_order_imbalance",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-period volume-weighted close-location imbalance proxy",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def kline_order_imbalance(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    price_range = (kline["high"] - kline["low"]).replace(0, np.nan)
    imbalance = ((2.0 * kline["close"] - kline["high"] - kline["low"]) / price_range)
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = (
        (imbalance * kline["volume"]).rolling(period, min_periods=1).mean()
        / kline["volume"].rolling(period).mean()
    )
    return result


@factor(
    name="wick_imbalance",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-period mean upper-minus-lower wick imbalance",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def wick_imbalance(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    body_high = kline[["open", "close"]].max(axis=1)
    body_low = kline[["open", "close"]].min(axis=1)
    price_range = (kline["high"] - kline["low"]).replace(0, np.nan)
    upper_wick = kline["high"] - body_high
    lower_wick = body_low - kline["low"]
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = ((upper_wick - lower_wick) / price_range).rolling(period, min_periods=1).mean()
    return result


@factor(
    name="intrabar_reversal",
    category=FactorCategory.MICROSTRUCTURE,
    description="20-period signed candle-body share of high-low range",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def intrabar_reversal(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    price_range = (kline["high"] - kline["low"]).replace(0, np.nan)
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = ((kline["close"] - kline["open"]) / price_range).rolling(period, min_periods=1).mean()
    return result
