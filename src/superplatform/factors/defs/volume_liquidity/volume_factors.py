"""Volume and liquidity factor definitions."""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars


@factor(
    name="volume_ratio",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="Current volume / 20-day average volume",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "均量回看天数",
            "min": 1,
            "max": 500,
        }
    },
)
def volume_ratio(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    avg_vol = kline["volume"].rolling(period).mean()
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = kline["volume"] / avg_vol
    return result


@factor(
    name="turnover",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-day mean turnover (volume / close)",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "换手率均值回看天数",
            "min": 1,
            "max": 500,
        }
    },
)
def turnover(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    daily_turnover = kline["volume"] / kline["close"]
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = daily_turnover.rolling(period).mean()
    return result


@factor(
    name="dollar_volume",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-period mean close-times-volume trading notional proxy",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def dollar_volume(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = (kline["close"] * kline["volume"]).rolling(period).mean()
    return result


@factor(
    name="volume_price_pressure",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-period volume-weighted close-location pressure",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def volume_price_pressure(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    price_range = (kline["high"] - kline["low"]).replace(0, np.nan)
    pressure = ((kline["close"] - kline["open"]) / price_range) * kline["volume"]
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = pressure.rolling(period, min_periods=1).mean() / kline["volume"].rolling(period).mean()
    return result


@factor(
    name="amihud_illiquidity",
    category=FactorCategory.VOLUME_LIQUIDITY,
    description="20-period mean absolute return per close-times-volume notional proxy",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def amihud_illiquidity(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1e-12)
    notional_proxy = (kline["close"] * kline["volume"]).clip(lower=epsilon)
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = (kline["close"].pct_change().abs() / notional_proxy).rolling(period).mean()
    return result
