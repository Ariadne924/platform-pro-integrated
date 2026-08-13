"""Rolling stability analysis.

Computes IC/IR over rolling windows to assess factor performance stability.
"""

import pandas as pd

from superplatform.evaluation.ic import compute_ic, compute_icir


def rolling_stability(
    df: pd.DataFrame,
    window: int = 60,
    step: int = 20,
    factor_col: str = "factor_value",
    forward_return_col: str = "forward_return",
    group_col: str = "timestamp",
) -> pd.DataFrame:
    """Compute IC over rolling windows.

    Args:
        df: DataFrame with factor values and forward returns.
        window: Rolling window size in periods.
        step: Step size between windows in periods.
        factor_col: Factor value column.
        forward_return_col: Forward return column.
        group_col: Period grouping column.

    Returns:
        DataFrame with columns: window_start, window_end, mean_ic, icir, ic_positive_ratio.
    """
    unique_ts = sorted(df[group_col].unique())
    results = []

    for start_idx in range(0, len(unique_ts) - window, step):
        end_idx = start_idx + window
        window_ts = unique_ts[start_idx:end_idx]
        window_df = df[df[group_col].isin(window_ts)]

        ic_df = compute_ic(
            window_df,
            factor_col=factor_col,
            forward_return_col=forward_return_col,
            group_col=group_col,
        )
        if ic_df.empty:
            continue

        stats = compute_icir(ic_df["ic"])
        results.append({
            "window_start": window_ts[0],
            "window_end": window_ts[-1],
            **stats,
        })

    return pd.DataFrame(results)


def rolling_icir(
    ic_df: pd.DataFrame,
    window: int = 60,
    step: int = 20,
) -> pd.DataFrame:
    """Compute rolling ICIR directly from an already-evaluated IC time series."""
    if "timestamp" not in ic_df or "ic" not in ic_df:
        raise ValueError("ic_df must contain timestamp and ic columns")

    ordered = ic_df[["timestamp", "ic"]].dropna().sort_values("timestamp").reset_index(drop=True)
    results = []
    for start_idx in range(0, len(ordered) - window + 1, step):
        values = ordered.iloc[start_idx:start_idx + window]
        stats = compute_icir(values["ic"])
        results.append({
            "window_start": values["timestamp"].iloc[0],
            "window_end": values["timestamp"].iloc[-1],
            **stats,
        })
    return pd.DataFrame(results)
