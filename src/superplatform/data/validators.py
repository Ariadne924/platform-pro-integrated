"""Data validation — enforcing the interface contract between layers.

Validators do NOT verify data correctness (that's the network layer's job —
it must faithfully translate exchange data). Validators enforce the contract
that every layer above depends on:

- Schema compliance: columns exist with correct names and dtypes
- UTC enforcement: all timestamps are timezone-aware UTC
- Gap detection: missing periods in regular-interval time series
- Outlier marking: extreme values flagged (MAD or Z-score method)
- Spot/perpetual guard: the two market types never appear in the same DataFrame

These checks catch bugs in network layer implementations before they
silently corrupt factor computations and backtest results.

Validation is called by Runtime after fetching/receiving data, before
passing it to the factor layer.
"""

from datetime import timedelta

import pandas as pd


def check_utc(df: pd.DataFrame, ts_col: str = "timestamp") -> dict:
    """Verify timestamp column is timezone-aware UTC.

    Returns:
        dict with keys: is_utc, tz, sample_values
    """
    report = {"is_utc": False, "tz": None, "sample_values": []}
    if ts_col not in df.columns:
        # A fetch that produced no columns (e.g. a failed symbol that the
        # pipeline isolated into an empty frame) cannot be UTC-checked.
        report["error"] = f"missing column: {ts_col}"
        return report
    ts = df[ts_col]
    if hasattr(ts.dtype, "tz") and ts.dtype.tz is not None:
        report["tz"] = str(ts.dtype.tz)
        report["is_utc"] = str(ts.dtype.tz).upper() in ("UTC", "UTCN", "ETC/UTC")
    report["sample_values"] = ts.head(3).astype(str).tolist()
    return report


def detect_missing(
    df: pd.DataFrame,
    ts_col: str = "timestamp",
    freq: timedelta | None = None,
) -> pd.DataFrame:
    """Find missing timestamps in a regular-interval time series.

    Args:
        df: DataFrame with a timestamp column.
        ts_col: Name of the timestamp column.
        freq: Expected frequency. If None, inferred from the median gap.

    Returns:
        DataFrame of missing periods with columns: gap_start, gap_end, gap_duration.
    """
    if df.empty:
        return pd.DataFrame(columns=["gap_start", "gap_end", "gap_duration"])

    ts = df[ts_col].dropna().sort_values().drop_duplicates()
    if len(ts) < 2:
        return pd.DataFrame(columns=["gap_start", "gap_end", "gap_duration"])

    if freq is None:
        freq = ts.diff().median()

    gaps = ts.diff() > freq * 1.5
    gap_indices = gaps[gaps].index
    return pd.DataFrame({
        "gap_start": ts.loc[gap_indices - 1].values if len(gap_indices) > 0 else [],
        "gap_end": ts.loc[gap_indices].values if len(gap_indices) > 0 else [],
        "gap_duration": (ts.loc[gap_indices].values - ts.loc[gap_indices - 1].values)
        if len(gap_indices) > 0 else [],
    })


def detect_outliers(
    series: pd.Series,
    method: str = "mad",
    threshold: float = 15.0,
) -> pd.Series:
    """Mark outliers in a numeric series.

    Args:
        series: Numeric data.
        method: 'mad' (Median Absolute Deviation) or 'zscore'.
        threshold: Number of deviations above which a point is an outlier.

    Note on the default threshold:
        数字资产序列重尾且带趋势——普通波动(如 5-10 倍 MAD 的单根 K 线
        跳动、牛市 0.1%-0.3% 的资金费率)是真实市场行为而非数据错误。
        默认 15 个偏差只标记真正病态的值(坏数、尺度错误),避免逐条
        罗列"统计上极端但市场正常"的点淹没 G1 报告。可用 --outlier-threshold
        调松/调紧。

    Returns:
        Boolean Series, True = outlier.
    """
    clean = series.dropna()
    if method == "mad":
        median = clean.median()
        mad = (clean - median).abs().median()
        if mad == 0:
            return pd.Series(False, index=series.index)
        z = 0.6745 * (clean - median) / mad
        return series.index.isin(clean.index[z.abs() > threshold])
    elif method == "zscore":
        mean = clean.mean()
        std = clean.std()
        if std == 0:
            return pd.Series(False, index=series.index)
        z = (clean - mean) / std
        return series.index.isin(clean.index[z.abs() > threshold])
    else:
        raise ValueError(f"Unknown outlier method: {method}")


def check_spot_perpetual_mix(df: pd.DataFrame) -> dict:
    """Check that spot and perpetual data are not mixed.

    Returns:
        dict with: is_mixed, market_types_present, symbol_market_pairs
    """
    if "market_type" not in df.columns:
        return {"is_mixed": False, "error": "No market_type column"}
    types = df["market_type"].unique()
    return {
        "is_mixed": len(types) > 1,
        "market_types_present": [str(t) for t in types],
    }


def full_validation_report(
    df: pd.DataFrame,
    schema_cls,
    ts_col: str = "timestamp",
    freq: timedelta | None = None,
) -> dict:
    """Run the complete validation suite and return a structured report.

    This is the single entry point for G1 data validation.
    """
    report = {
        "schema_validation": schema_cls.validate_df(df) if schema_cls else {},
        "utc_check": check_utc(df, ts_col),
        "missing_gaps": detect_missing(df, ts_col, freq).to_dict("records"),
        "null_summary": {col: int(df[col].isna().sum()) for col in df.columns},
        "spot_perpetual_check": check_spot_perpetual_mix(df),
        "row_count": len(df),
        "time_range": {
            "start": str(df[ts_col].min()) if ts_col in df.columns and len(df) else None,
            "end": str(df[ts_col].max()) if ts_col in df.columns and len(df) else None,
        },
    }
    return report
