"""Rolling stability metrics for daily factor IC time series."""

from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_stability(
    ic_data: pd.DataFrame,
    *,
    timestamp_col: str = "timestamp",
    factor_col: str = "factor_name",
    ic_col: str = "ic",
    window_days: int = 60,
    min_periods: int = 20,
) -> pd.DataFrame:
    """Compute rolling IC mean and IC_IR for each factor.

    The window is a UTC calendar-time window ending at each observation. IC_IR uses
    the sample standard deviation and is NaN when the window has insufficient data or
    zero variance.
    """
    required = {timestamp_col, factor_col, ic_col}
    missing = sorted(required.difference(ic_data.columns))
    if missing:
        raise ValueError(f"ic_data is missing required columns: {missing}")
    if window_days < 1 or min_periods < 1:
        raise ValueError("window_days and min_periods must be positive")
    if ic_data.empty:
        # No IC observations (e.g. cross-sections below min_assets): there is
        # nothing to roll over, so return the stable empty schema.
        return pd.DataFrame(columns=[
            timestamp_col,
            factor_col,
            "window_start",
            "window_end",
            "rolling_mean_ic",
            "rolling_ic_mean",
            "rolling_ic_ir",
            "n_periods",
        ])
    timestamps = ic_data[timestamp_col]
    if not pd.api.types.is_datetime64_any_dtype(timestamps):
        raise TypeError("ic_data timestamp must be a timezone-aware UTC datetime")
    timezone = getattr(timestamps.dtype, "tz", None)
    if timezone is None or str(timezone).upper() not in {
        "UTC",
        "UTC+00:00",
        "ETC/UTC",
    }:
        raise ValueError(f"ic_data timestamp must be UTC, got {timezone}")

    work = ic_data[[timestamp_col, factor_col, ic_col]].copy()
    work[ic_col] = pd.to_numeric(work[ic_col], errors="coerce")
    work = work.replace([np.inf, -np.inf], np.nan).dropna(subset=[timestamp_col])
    work = work.sort_values([factor_col, timestamp_col])
    rows: list[dict[str, object]] = []
    # The current observation is included, so N daily points span N - 1 days.
    window = pd.Timedelta(days=window_days - 1)
    for factor_name, group in work.groupby(factor_col, sort=True, dropna=False):
        group = group.dropna(subset=[ic_col]).sort_values(timestamp_col)
        for timestamp in group[timestamp_col].drop_duplicates():
            start = timestamp - window
            values = group.loc[
                group[timestamp_col].between(start, timestamp),
                ic_col,
            ].dropna()
            count = int(len(values))
            mean_ic = float(values.mean()) if count >= min_periods else np.nan
            std_ic = float(values.std(ddof=1)) if count >= min_periods + 1 else np.nan
            ic_ir = (
                float(mean_ic / std_ic)
                if np.isfinite(mean_ic) and np.isfinite(std_ic) and std_ic > 0
                else np.nan
            )
            rows.append(
                {
                    timestamp_col: timestamp,
                    factor_col: factor_name,
                    "window_start": start,
                    "window_end": timestamp,
                    "rolling_mean_ic": mean_ic,
                    "rolling_ic_mean": mean_ic,
                    "rolling_ic_ir": ic_ir,
                    "n_periods": count,
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            timestamp_col,
            factor_col,
            "window_start",
            "window_end",
            "rolling_mean_ic",
            "rolling_ic_mean",
            "rolling_ic_ir",
            "n_periods",
        ],
    )


def compute_rolling_stability(
    ic_data: pd.DataFrame,
    *,
    window_days: int = 60,
    min_periods: int = 20,
) -> pd.DataFrame:
    """Compatibility wrapper for rolling_stability with standard column names."""
    return rolling_stability(
        ic_data,
        window_days=window_days,
        min_periods=min_periods,
    )
