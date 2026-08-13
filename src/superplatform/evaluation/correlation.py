"""Factor correlation matrices — panel-based (canonical) and dict-based helpers."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

CORRELATION_METHODS = ("pearson", "spearman")


# ── Panel-based (canonical multi-factor) ──────────────────────────────


def _resolve_value_column(panel: pd.DataFrame, value_col: str) -> str:
    """Resolve the requested factor value column."""
    if value_col in panel.columns:
        return value_col
    if value_col == "factor_value" and "factor_value_eval" in panel.columns:
        return "factor_value_eval"
    raise ValueError(f"factor value column not found: {value_col}")


def _validate_corr_panel(
    panel: pd.DataFrame,
    *,
    timestamp_col: str,
    symbol_col: str,
    factor_col: str,
    value_col: str,
) -> str:
    """Validate correlation panel fields and return the resolved value column."""
    required = {timestamp_col, symbol_col, factor_col}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"factor panel is missing required columns: {missing}")
    timestamps = panel[timestamp_col]
    if not pd.api.types.is_datetime64_any_dtype(timestamps):
        raise TypeError("factor panel timestamp must be timezone-aware UTC")
    timezone = getattr(timestamps.dtype, "tz", None)
    if timezone is None or str(timezone).upper() not in {
        "UTC",
        "UTC+00:00",
        "ETC/UTC",
    }:
        raise ValueError(f"factor panel timestamp must be UTC, got {timezone}")
    if panel.duplicated([timestamp_col, symbol_col, factor_col]).any():
        raise ValueError(
            "factor panel contains duplicate timestamp/symbol/factor keys"
        )
    return _resolve_value_column(panel, value_col)


def factor_correlation_matrix(
    panel: pd.DataFrame,
    *,
    method: str = "pearson",
    timestamp_col: str = "timestamp",
    symbol_col: str = "symbol",
    factor_col: str = "factor_name",
    value_col: str = "factor_value",
    min_assets: int = 2,
) -> pd.DataFrame:
    """Compute the mean same-date cross-sectional factor correlation matrix."""
    if method not in CORRELATION_METHODS:
        raise ValueError(f"method must be one of {CORRELATION_METHODS}")
    if min_assets < 2:
        raise ValueError("min_assets must be at least 2")
    resolved_value_col = _validate_corr_panel(
        panel,
        timestamp_col=timestamp_col,
        symbol_col=symbol_col,
        factor_col=factor_col,
        value_col=value_col,
    )
    factor_names = sorted(panel[factor_col].dropna().unique().tolist())
    sums = pd.DataFrame(0.0, index=factor_names, columns=factor_names)
    counts = pd.DataFrame(0, index=factor_names, columns=factor_names, dtype=int)
    for _, group in panel.groupby(timestamp_col, sort=True):
        # Reject undersized cross-sections before pivoting — pivot is the
        # dominant cost here and would be discarded anyway.
        if group[symbol_col].nunique() < min_assets:
            continue
        wide = group.pivot(
            index=symbol_col,
            columns=factor_col,
            values=resolved_value_col,
        ).reindex(columns=factor_names)
        corr = wide.corr(method=method, min_periods=min_assets)
        for left in factor_names:
            for right in factor_names:
                value = corr.loc[left, right]
                if pd.notna(value):
                    sums.loc[left, right] += float(value)
                    counts.loc[left, right] += 1
    matrix = sums.divide(counts.where(counts > 0))
    matrix = matrix.reindex(index=factor_names, columns=factor_names)
    for factor_name in factor_names:
        if counts.loc[factor_name, factor_name] > 0:
            matrix.loc[factor_name, factor_name] = 1.0
    matrix.index.name = factor_col
    matrix.columns.name = factor_col
    return matrix


def compute_factor_correlations(
    panel: pd.DataFrame,
    *,
    methods: Sequence[str] = CORRELATION_METHODS,
    timestamp_col: str = "timestamp",
    symbol_col: str = "symbol",
    factor_col: str = "factor_name",
    value_col: str = "factor_value",
    min_assets: int = 2,
) -> dict[str, pd.DataFrame]:
    """Compute all requested same-date Pearson and Spearman matrices."""
    invalid = sorted(set(methods).difference(CORRELATION_METHODS))
    if invalid:
        raise ValueError(f"unsupported correlation methods: {invalid}")
    return {
        method: factor_correlation_matrix(
            panel,
            method=method,
            timestamp_col=timestamp_col,
            symbol_col=symbol_col,
            factor_col=factor_col,
            value_col=value_col,
            min_assets=min_assets,
        )
        for method in methods
    }


# ── Dict-based (lightweight pipeline helper) ──────────────────────────


def factor_correlation_from_dict(
    factor_values: dict[str, pd.DataFrame],
    value_col: str = "value",
    method: str = "pearson",
) -> pd.DataFrame:
    """Compute pairwise correlation between factors from a dict of DataFrames.

    Each DataFrame must have columns: timestamp, symbol, and the value column.
    Merges all factors on (timestamp, symbol) and computes the correlation.
    """
    merged = None
    for name, fv in factor_values.items():
        fv_sub = fv[["timestamp", "symbol", value_col]].rename(
            columns={value_col: name}
        )
        if merged is None:
            merged = fv_sub
        else:
            merged = merged.merge(fv_sub, on=["timestamp", "symbol"], how="inner")

    if merged is None or len(merged.columns) <= 2:
        return pd.DataFrame()

    factor_cols = [c for c in merged.columns if c not in ("timestamp", "symbol")]
    return merged[factor_cols].corr(method=method)
