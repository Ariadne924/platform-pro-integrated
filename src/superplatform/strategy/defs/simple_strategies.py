"""Simple example strategies."""

import pandas as pd

from superplatform.strategy.base import strategy


@strategy(
    name="momentum_long_only",
    description="Long if momentum > 0, else flat.",
    used_factors=["momentum_20d"],
)
def momentum_long_only(factor_results, **params):
    threshold = params.get("threshold", 0.0)
    frames = []

    for group_key, result in factor_results["momentum_20d"].items():
        fv = result.values
        sig = pd.DataFrame({"timestamp": fv["timestamp"]})
        sig["symbol"] = group_key
        sig["position"] = (fv["value"] > threshold).astype(float)
        frames.append(sig)

    return pd.concat(frames, ignore_index=True)


@strategy(
    name="equal_weight_combo",
    description="Average of momentum and reversal, long if composite > 0.",
    used_factors=["momentum_20d", "short_term_reversal_5d"],
)
def equal_weight_combo(factor_results, **params):
    threshold = params.get("threshold", 0.0)
    frames = []

    momentum = factor_results["momentum_20d"]
    reversal = factor_results["short_term_reversal_5d"]

    for group_key in momentum:
        if group_key not in reversal:
            continue
        m = momentum[group_key].values
        r = reversal[group_key].values

        merged = m[["timestamp"]].copy()
        merged["composite"] = (m["value"].fillna(0) + r["value"].fillna(0)) / 2
        merged["symbol"] = group_key
        merged["position"] = (merged["composite"] > threshold).astype(float)
        frames.append(merged[["timestamp", "symbol", "position"]])

    return pd.concat(frames, ignore_index=True)
