"""Minimal cross-sectional factor metrics.

The public entry point is evaluate_factor, which merges a factor table and
forward-return table, applies optional per-period preprocessing, and returns
both the evaluated panel and an auditable summary dictionary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from superplatform.evaluation.qc import check_forward_bias, run_qc

FACTOR_REQUIRED_COLUMNS = {"timestamp", "symbol", "factor_name"}
RETURN_REQUIRED_COLUMNS = {"timestamp", "symbol"}


def _require_columns(df: pd.DataFrame, required: set[str], label: str) -> None:
    """Raise a descriptive error when required columns are missing."""
    missing = sorted(required.difference(df.columns))
    if missing:
        raise ValueError(f"{label} is missing required columns: {missing}")


def _validate_utc(df: pd.DataFrame, label: str) -> None:
    """Require a non-null, timezone-aware UTC timestamp column."""
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        raise TypeError(f"{label}.timestamp must be a timezone-aware datetime")
    timezone = getattr(df["timestamp"].dtype, "tz", None)
    if timezone is None or str(timezone).upper() not in {
        "UTC",
        "UTC+00:00",
        "ETC/UTC",
    }:
        raise ValueError(f"{label}.timestamp must be UTC, got {timezone}")
    if df["timestamp"].isna().any():
        raise ValueError(f"{label}.timestamp contains null values")


def _validate_keys(df: pd.DataFrame, keys: Sequence[str], label: str) -> None:
    """Require unique, non-null key columns."""
    if df[list(keys)].isna().any().any():
        raise ValueError(f"{label} contains null key values")
    if df.duplicated(list(keys)).any():
        raise ValueError(f"{label} contains duplicate keys: {list(keys)}")


def _validate_limits(limits: tuple[float, float]) -> tuple[float, float]:
    """Validate quantile bounds represented as lower and upper quantiles."""
    lower, upper = limits
    if not 0 <= lower < upper <= 1:
        raise ValueError("limits must satisfy 0 <= lower < upper <= 1")
    return float(lower), float(upper)


def winsorize(
    series: pd.Series,
    *,
    limits: tuple[float, float] = (0.01, 0.99),
) -> pd.Series:
    """Clip finite values to configured lower and upper quantiles.

    NaN and infinite values are preserved as missing values. The input Series is not
    mutated and its index is retained.
    """
    lower_q, upper_q = _validate_limits(limits)
    result = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    finite = result.dropna()
    if finite.empty:
        return result
    lower = finite.quantile(lower_q)
    upper = finite.quantile(upper_q)
    return result.clip(lower=lower, upper=upper)


def zscore(series: pd.Series) -> pd.Series:
    """Standardize a numeric Series using mean and sample standard deviation.

    A constant finite Series is mapped to zeros. Missing and non-numeric values remain
    missing so they can be excluded explicitly during cross-sectional evaluation.
    """
    result = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    finite = result.dropna()
    if finite.empty:
        return result.astype(float)
    std = finite.std(ddof=1)
    if not np.isfinite(std) or std == 0:
        result.loc[finite.index] = 0.0
        return result.astype(float)
    result.loc[finite.index] = (finite - finite.mean()) / std
    return result.astype(float)


def _preprocess_by_period(
    panel: pd.DataFrame,
    *,
    factor_col: str,
    timestamp_col: str,
    factor_name_col: str,
    winsorize_enabled: bool,
    zscore_enabled: bool,
    winsorize_limits: tuple[float, float],
) -> pd.DataFrame:
    """Apply optional winsorization and z-score within each date cross-section."""
    result = panel.copy()
    result["factor_value_raw"] = pd.to_numeric(
        result[factor_col], errors="coerce"
    ).replace([np.inf, -np.inf], np.nan)
    result["factor_value_eval"] = result["factor_value_raw"]
    result["is_outlier"] = False

    group_keys = [timestamp_col, factor_name_col]
    for _, index in result.groupby(group_keys, sort=False, dropna=False).groups.items():
        values = result.loc[index, "factor_value_raw"]
        if winsorize_enabled:
            processed = winsorize(values, limits=winsorize_limits)
            changed = (
                processed.notna()
                & values.notna()
                & ~np.isclose(
                    processed.to_numpy(),
                    values.to_numpy(),
                    equal_nan=True,
                )
            )
            result.loc[index, "is_outlier"] = changed
            values = processed
        if zscore_enabled:
            values = zscore(values)
        result.loc[index, "factor_value_eval"] = values
    return result


def _merge_inputs(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    *,
    factor_col: str,
    return_col: str,
) -> pd.DataFrame:
    """Validate and left-merge factor observations with forward returns."""
    _require_columns(factor_df, FACTOR_REQUIRED_COLUMNS | {factor_col}, "factor_df")
    _require_columns(returns_df, RETURN_REQUIRED_COLUMNS | {return_col}, "returns_df")
    _validate_utc(factor_df, "factor_df")
    _validate_utc(returns_df, "returns_df")
    _validate_keys(factor_df, ("timestamp", "symbol", "factor_name"), "factor_df")
    _validate_keys(returns_df, ("timestamp", "symbol"), "returns_df")
    return (
        factor_df.merge(
            returns_df,
            on=["timestamp", "symbol"],
            how="left",
            validate="many_to_one",
            indicator=True,
            suffixes=("_factor", "_return"),
        )
        .sort_values(["timestamp", "symbol", "factor_name"])
        .reset_index(drop=True)
    )


def _validate_merged_panel(
    panel: pd.DataFrame,
    *,
    factor_col: str,
    return_col: str,
) -> None:
    """Validate a pre-merged panel before calculating cross-sectional metrics."""
    _require_columns(
        panel,
        FACTOR_REQUIRED_COLUMNS | {factor_col, return_col},
        "panel",
    )
    _validate_utc(panel, "panel")
    _validate_keys(panel, ("timestamp", "symbol", "factor_name"), "panel")


def preprocess_factor_panel(
    panel: pd.DataFrame,
    *,
    factor_col: str = "factor_value",
    return_col: str = "forward_return",
    winsorize_enabled: bool = False,
    zscore_enabled: bool = False,
    winsorize_limits: tuple[float, float] = (0.01, 0.99),
) -> pd.DataFrame:
    """Validate and preprocess a pre-merged factor/forward-return panel.

    The raw factor is preserved in ``factor_value_raw`` and the evaluated factor
    is written to ``factor_value_eval``.  Processing is performed independently
    for every UTC timestamp and factor name, so no information crosses dates.
    """
    _validate_merged_panel(
        panel,
        factor_col=factor_col,
        return_col=return_col,
    )
    _validate_limits(winsorize_limits)
    return _preprocess_by_period(
        panel,
        factor_col=factor_col,
        timestamp_col="timestamp",
        factor_name_col="factor_name",
        winsorize_enabled=winsorize_enabled,
        zscore_enabled=zscore_enabled,
        winsorize_limits=winsorize_limits,
    )


def _pearson(x, y) -> float:
    """Pearson correlation of two 1-D arrays; NaN on zero variance.

    numpy implementation of the standard formula — pandas Series.corr has a
    per-call overhead that dominates on panels with thousands of tiny
    cross-sections.
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        x = x - x.mean()
        y = y - y.mean()
        denom = np.sqrt(np.dot(x, x) * np.dot(y, y))
        if denom == 0:
            return float("nan")
        return float(np.dot(x, y) / denom)


def _cross_sectional_correlation(
    panel: pd.DataFrame,
    *,
    factor_col: str,
    return_col: str,
    timestamp_col: str,
    method: str,
    min_assets: int,
) -> pd.DataFrame:
    """Compute one correlation value per timestamp and factor name."""
    work = panel[[timestamp_col, "factor_name", factor_col, return_col]].copy()
    work[factor_col] = pd.to_numeric(work[factor_col], errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    work[return_col] = pd.to_numeric(work[return_col], errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )

    results: list[dict[str, Any]] = []
    for (timestamp, factor_name), group in work.groupby(
        [timestamp_col, "factor_name"], sort=True
    ):
        factor = group[factor_col]
        returns = group[return_col]
        mask = factor.notna() & returns.notna()
        if mask.sum() < min_assets:
            continue
        factor_values = factor[mask].to_numpy()
        return_values = returns[mask].to_numpy()
        if (
            np.unique(factor_values).size < 2
            or np.unique(return_values).size < 2
        ):
            correlation = np.nan
        elif method == "pearson":
            correlation = _pearson(factor_values, return_values)
        else:
            correlation = _pearson(
                stats.rankdata(factor_values),
                stats.rankdata(return_values),
            )
        results.append(
            {
                timestamp_col: timestamp,
                "factor_name": factor_name,
                "ic" if method == "pearson" else "rank_ic": correlation,
                "n_assets": int(mask.sum()),
            }
        )
    value_name = "ic" if method == "pearson" else "rank_ic"
    return pd.DataFrame(
        results,
        columns=[timestamp_col, "factor_name", value_name, "n_assets"],
    )


def compute_ic(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame | None = None,
    *,
    factor_col: str = "factor_value",
    return_col: str = "forward_return",
    timestamp_col: str = "timestamp",
    min_assets: int = 2,
) -> pd.DataFrame:
    """Compute cross-sectional Pearson IC for each timestamp and factor."""
    panel = (
        _merge_inputs(
            factor_df,
            returns_df,
            factor_col=factor_col,
            return_col=return_col,
        )
        if returns_df is not None
        else factor_df
    )
    if returns_df is None:
        _validate_merged_panel(
            panel,
            factor_col=factor_col,
            return_col=return_col,
        )
    if timestamp_col not in panel.columns:
        raise ValueError(f"missing timestamp column: {timestamp_col}")
    value_col = "factor_value_eval" if "factor_value_eval" in panel else factor_col
    return _cross_sectional_correlation(
        panel,
        factor_col=value_col,
        return_col=return_col,
        timestamp_col=timestamp_col,
        method="pearson",
        min_assets=min_assets,
    )


def compute_rank_ic(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame | None = None,
    *,
    factor_col: str = "factor_value",
    return_col: str = "forward_return",
    timestamp_col: str = "timestamp",
    min_assets: int = 2,
) -> pd.DataFrame:
    """Compute cross-sectional Spearman RankIC for each timestamp and factor."""
    panel = (
        _merge_inputs(
            factor_df,
            returns_df,
            factor_col=factor_col,
            return_col=return_col,
        )
        if returns_df is not None
        else factor_df
    )
    if returns_df is None:
        _validate_merged_panel(
            panel,
            factor_col=factor_col,
            return_col=return_col,
        )
    if timestamp_col not in panel.columns:
        raise ValueError(f"missing timestamp column: {timestamp_col}")
    value_col = "factor_value_eval" if "factor_value_eval" in panel else factor_col
    return _cross_sectional_correlation(
        panel,
        factor_col=value_col,
        return_col=return_col,
        timestamp_col=timestamp_col,
        method="spearman",
        min_assets=min_assets,
    )


def compute_ic_ir(
    ic_data: pd.Series | pd.DataFrame,
    *,
    value_col: str = "ic",
    zero_std_tolerance: float = 1e-12,
) -> dict[str, float]:
    """Compute time-series mean, sample standard deviation, and IC_IR.

    Standard deviations at or below zero_std_tolerance are treated as zero to
    prevent floating-point noise from producing an artificial infinite IC_IR.
    """
    if zero_std_tolerance < 0:
        raise ValueError("zero_std_tolerance must be non-negative")
    values = ic_data[value_col] if isinstance(ic_data, pd.DataFrame) else ic_data
    values = pd.to_numeric(values, errors="coerce").dropna()
    count = int(len(values))
    mean = float(values.mean()) if count else float("nan")
    std = float(values.std(ddof=1)) if count >= 2 else float("nan")
    if np.isfinite(std) and std <= zero_std_tolerance:
        std = 0.0
    ic_ir = float(mean / std) if np.isfinite(std) and std > 0 else float("nan")
    return {
        "mean_ic": mean,
        "std_ic": std,
        "ic_ir": ic_ir,
        "ic_positive_ratio": float((values > 0).mean()) if count else float("nan"),
        "n_periods": float(count),
    }


def evaluate_factor(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    *,
    factor_col: str = "factor_value",
    return_col: str = "forward_return",
    winsorize_enabled: bool = False,
    zscore_enabled: bool = False,
    winsorize_limits: tuple[float, float] = (0.01, 0.99),
    min_assets: int = 2,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Merge, preprocess, and evaluate a factor table against forward returns."""
    panel = _merge_inputs(
        factor_df,
        returns_df,
        factor_col=factor_col,
        return_col=return_col,
    )
    qc = run_qc(
        factor_df,
        returns_df,
        factor_value_col=factor_col,
        return_value_col=return_col,
        winsorize_limits=winsorize_limits,
    )
    panel = preprocess_factor_panel(
        panel,
        factor_col=factor_col,
        return_col=return_col,
        winsorize_enabled=winsorize_enabled,
        zscore_enabled=zscore_enabled,
        winsorize_limits=winsorize_limits,
    )
    ic = compute_ic(
        panel,
        factor_col="factor_value_eval",
        return_col=return_col,
        min_assets=min_assets,
    )
    rank_ic = compute_rank_ic(
        panel,
        factor_col="factor_value_eval",
        return_col=return_col,
        min_assets=min_assets,
    )
    summary = {
        "rows": int(len(panel)),
        "factors": sorted(panel["factor_name"].dropna().unique().tolist()),
        "qc": qc,
        "forward_bias": check_forward_bias(factor_df, returns_df),
        "ic": ic,
        "rank_ic": rank_ic,
        "ic_ir": compute_ic_ir(ic),
        "rank_ic_ir": compute_ic_ir(rank_ic, value_col="rank_ic"),
    }
    return panel, summary
