"""Stratification / layer test.

Bucket assets into quantiles by factor value each period,
compute equal-weighted forward returns per bucket, and report:
- Mean return per layer
- Top-minus-bottom spread
- Monotonicity check
"""

import numpy as np
import pandas as pd


def layer_test(
    df: pd.DataFrame,
    n_layers: int = 5,
    factor_col: str = "factor_value",
    forward_return_col: str = "forward_return",
    group_col: str = "timestamp",
    weighting: str = "equal",
) -> pd.DataFrame:
    """Cross-sectional stratification test.

    Each period, assets are sorted by factor_value and divided into
    n_layers equal-sized groups. Returns are computed per layer.

    Args:
        df: DataFrame with columns [timestamp, symbol, factor_value, forward_return].
        n_layers: Number of quantile buckets (default 5).
        factor_col: Name of factor value column.
        forward_return_col: Name of forward return column.
        group_col: Period grouping column.
        weighting: 'equal' or 'value_weighted'.

    Returns:
        DataFrame with columns: timestamp, layer, mean_return, n_assets
    """
    work = df[[group_col, factor_col, forward_return_col]].copy()
    work[factor_col] = pd.to_numeric(work[factor_col], errors="coerce")
    work[forward_return_col] = pd.to_numeric(work[forward_return_col], errors="coerce")
    work = work.dropna(subset=[factor_col, forward_return_col])
    if work.empty:
        return pd.DataFrame(columns=[
            "timestamp", "layer", "mean_return", "n_assets",
        ])

    # Per-period layer via rank-based equal division (same semantics as the
    # backtest's stable rank-cut): layer = floor((rank-1) * layers / size).
    sizes = work.groupby(group_col)[factor_col].transform("size")
    work = work[sizes >= 2]
    if work.empty:
        return pd.DataFrame(columns=[
            "timestamp", "layer", "mean_return", "n_assets",
        ])
    sizes = work.groupby(group_col)[factor_col].transform("size")
    actual = np.minimum(n_layers, sizes.to_numpy())
    ranks = work.groupby(group_col)[factor_col].rank(method="first")
    work["layer"] = ((ranks - 1) * actual // sizes).astype(int)

    if weighting == "equal":
        grouped = work.groupby([group_col, "layer"])[forward_return_col].agg(
            ["mean", "count"]
        )
        result = grouped.reset_index().rename(
            columns={"mean": "mean_return", "count": "n_assets"}
        )
        return result[[group_col, "layer", "mean_return", "n_assets"]]

    # Value-weighted by market cap when available, else equal weights.
    if "market_cap" in df.columns:
        weights = pd.to_numeric(work["market_cap"], errors="coerce").fillna(0.0)
        work["_weighted_return"] = work[forward_return_col] * weights
        grouped = work.groupby([group_col, "layer"]).agg(
            weighted_return=("_weighted_return", "sum"),
            weight_sum=("market_cap", "sum"),
            n_assets=(factor_col, "count"),
        )
        grouped["mean_return"] = (
            grouped["weighted_return"] / grouped["weight_sum"].replace(0, np.nan)
        )
        result = grouped.reset_index()
        return result[[group_col, "layer", "mean_return", "n_assets"]]

    return pd.DataFrame(columns=[
        "timestamp", "layer", "mean_return", "n_assets",
    ])


def layer_summary(layer_results: pd.DataFrame) -> dict:
    """Compute summary statistics from layer test results.

    Returns dict with:
        layer_means: mean return per layer
        spread: top - bottom layer mean
        monotonicity: whether returns increase monotonically with layer
        t_stat: t-stat of top-minus-bottom spread
    """
    layer_means = layer_results.groupby("layer")["mean_return"].mean()
    spread = layer_means.iloc[-1] - layer_means.iloc[0] if len(layer_means) > 1 else np.nan

    # Monotonicity: returns increase with layer rank
    diffs = layer_means.diff().dropna()
    monotonic = (diffs >= 0).all() or (diffs <= 0).all()

    # T-stat of spread
    if len(layer_results) > 0:
        top = layer_results[layer_results["layer"] == layer_results["layer"].max()]["mean_return"]
        bottom = layer_results[layer_results["layer"] == layer_results["layer"].min()]["mean_return"]
        spread_series = top.values - bottom.values[:len(top)]
        t_stat = spread_series.mean() / spread_series.std() if spread_series.std() > 0 else np.nan
    else:
        t_stat = np.nan

    return {
        "layer_means": layer_means.to_dict(),
        "spread": spread,
        "monotonicity": monotonic,
        "t_stat": t_stat,
    }
