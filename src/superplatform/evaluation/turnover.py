"""Factor turnover computation.

Turnover = fraction of assets that change layers between consecutive periods.
High turnover implies high trading costs.
"""

import numpy as np
import pandas as pd


def compute_turnover(
    df: pd.DataFrame,
    n_layers: int = 5,
    factor_col: str = "factor_value",
    group_col: str = "timestamp",
    id_col: str = "symbol",
) -> pd.DataFrame:
    """Compute layer turnover between consecutive periods.

    Args:
        df: DataFrame with columns [timestamp, symbol, factor_value].
        n_layers: Number of quantile layers.
        factor_col: Name of factor value column.
        group_col: Period grouping column.
        id_col: Asset identifier column.

    Returns:
        DataFrame with columns: timestamp, turnover (fraction of assets changing layers).
    """
    work = df[[group_col, id_col, factor_col]].copy()
    work[factor_col] = pd.to_numeric(work[factor_col], errors="coerce")
    work = work.dropna(subset=[factor_col])
    if work.empty:
        return pd.DataFrame(columns=["timestamp", "turnover", "n_assets"])

    # Per-period layer via rank-based equal division (same semantics as the
    # backtest's stable rank-cut): layer = floor((rank-1) * layers / size).
    sizes = work.groupby(group_col)[factor_col].transform("size")
    work = work[sizes >= 2]
    if work.empty:
        return pd.DataFrame(columns=["timestamp", "turnover", "n_assets"])
    sizes = work.groupby(group_col)[factor_col].transform("size")
    actual = np.minimum(n_layers, sizes.to_numpy())
    ranks = work.groupby(group_col)[factor_col].rank(method="first")
    work["layer"] = ((ranks - 1) * actual // sizes).astype(int)

    # Pivot to a per-period × per-asset layer matrix and diff adjacent rows.
    pivot = work.pivot(index=group_col, columns=id_col, values="layer").sort_index()
    prev = pivot.shift(1)
    common = pivot.notna() & prev.notna()
    changed = (pivot != prev) & common
    turnover = changed.sum(axis=1) / common.sum(axis=1).replace(0, np.nan)
    result = pd.DataFrame({
        "timestamp": pivot.index,
        "turnover": turnover.to_numpy(),
        "n_assets": common.sum(axis=1).to_numpy(),
    })
    return result.reset_index(drop=True)


def mean_turnover(turnover_df: pd.DataFrame) -> float:
    """Mean turnover over all periods."""
    return turnover_df["turnover"].mean()
