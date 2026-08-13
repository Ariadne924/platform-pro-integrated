"""Demo strategies that generate frequent signals for live pipeline testing."""

import pandas as pd

from superplatform.strategy.base import strategy


@strategy(
    name="momentum_demo",
    description="[Demo] Go long when momentum>0, short when momentum<0 — triggers ~50% of ticks",
    used_factors=["momentum_20d"],
)
def momentum_demo(factor_results, **params):
    """Momentum-following with aggressive weights for visible order flow."""
    frames = []
    for group_key, result in factor_results["momentum_20d"].items():
        fv = result.values
        sig = pd.DataFrame({"timestamp": fv["timestamp"]})
        sig["symbol"] = group_key
        sig["position"] = 0.0
        sig.loc[fv["value"] > 0, "position"] = 0.4    # long 40%
        sig.loc[fv["value"] < 0, "position"] = -0.4   # short 40%
        frames.append(sig)
    return pd.concat(frames, ignore_index=True)
