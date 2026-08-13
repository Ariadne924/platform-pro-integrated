"""(Placeholder for momentum/reversal factor definitions.)

# Single-symbol factor — each factor runs on one symbol at a time:
#
# from superplatform.factors.base import factor, FactorCategory
# import pandas as pd
# from superplatform.utils.timestamps import lookback_bars
#
# @factor(
#     name="momentum",
#     category=FactorCategory.MOMENTUM_REVERSAL,
#     description="N-day price momentum (N from lookback_days)",
#     required_data=["kline"],
# )
# def momentum(data, **params):
#     \"\"\"Compute N-day momentum for one symbol.\"\"\"
#     period = lookback_bars(data["kline"], params.get("lookback_days", 20))
#     kline = data["kline"]  # one symbol's OHLCV data
#     result = pd.DataFrame({"timestamp": kline["timestamp"]})
#     result["value"] = kline["close"] / kline["close"].shift(period) - 1
#     return result
"""
