"""RSI-based mean reversion strategies."""

import pandas as pd

from superplatform.strategy.base import strategy


@strategy(
    name="rsi_mean_reversion",
    description="RSI 超卖买入，超买卖出",
    used_factors=["rsi_14"],
)
def rsi_mean_reversion(factor_results, **params):
    oversold = params.get("oversold", 30)
    overbought = params.get("overbought", 70)
    frames = []

    for group_key, result in factor_results["rsi_14"].items():
        fv = result.values
        sig = pd.DataFrame({"timestamp": fv["timestamp"]})
        sig["symbol"] = group_key
        sig["position"] = 0.0
        sig.loc[fv["value"] < oversold, "position"] = 1.0
        sig.loc[fv["value"] > overbought, "position"] = -1.0
        frames.append(sig)

    return pd.concat(frames, ignore_index=True)
