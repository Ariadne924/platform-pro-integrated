"""RSI factor."""

import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.utils.timestamps import lookback_bars


@factor(
    name="rsi",
    category=FactorCategory.MOMENTUM_REVERSAL,
    description="N 日 RSI（N 由 lookback_days 配置，默认 14）",
    required_data=["kline"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 14,
            "description": "RSI 计算回看天数",
            "min": 1,
            "max": 500,
        }
    },
)
def rsi(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 14))
    delta = kline["close"].diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta).clip(lower=0).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return pd.DataFrame({"timestamp": kline["timestamp"], "value": rsi})
