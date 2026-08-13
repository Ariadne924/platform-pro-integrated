"""Quality checks for the minimal factor evaluation pipeline.

The checks are side-effect free and return structured dictionaries suitable for
persistence alongside metric output and audit metadata.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

FACTOR_KEY = ("timestamp", "symbol", "factor_name")
RETURN_KEY = ("timestamp", "symbol")


def _utc_status(df: pd.DataFrame, column: str) -> tuple[bool, str | None, int]:
    """Return UTC validity, timezone name, and null timestamp count."""
    if column not in df.columns:
        return False, None, len(df)
    series = df[column]
    timezone = getattr(series.dtype, "tz", None)
    timezone_name = str(timezone) if timezone is not None else None
    is_utc = timezone is not None and str(timezone).upper() in {
        "UTC",
        "UTC+00:00",
        "ETC/UTC",
    }
    null_count = int(series.isna().sum())
    return bool(is_utc and null_count == 0), timezone_name, null_count


def _missing_stats(df: pd.DataFrame) -> dict[str, int]:
    """Count missing values for every column in a DataFrame."""
    return {column: int(count) for column, count in df.isna().sum().items()}


def _duplicate_count(df: pd.DataFrame, keys: Sequence[str]) -> int:
    """Count rows participating in duplicate key groups."""
    missing_keys = [key for key in keys if key not in df.columns]
    if missing_keys or df.empty:
        return 0
    return int(df.duplicated(list(keys), keep=False).sum())


def _extreme_ratio(
    df: pd.DataFrame,
    value_col: str,
    lower_quantile: float,
    upper_quantile: float,
) -> float:
    """Measure the fraction of finite values outside configured global quantiles."""
    if value_col not in df.columns:
        return float("nan")
    values = pd.to_numeric(df[value_col], errors="coerce")
    values = values[np.isfinite(values)]
    if values.empty:
        return float("nan")
    lower = float(values.quantile(lower_quantile))
    upper = float(values.quantile(upper_quantile))
    return float(((values < lower) | (values > upper)).mean())


def run_qc(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    *,
    factor_value_col: str = "factor_value",
    return_value_col: str = "forward_return",
    winsorize_limits: tuple[float, float] = (0.01, 0.99),
) -> dict[str, Any]:
    """Return missing, duplicate-key, timestamp, and extreme-value statistics.

    Duplicate counts include every row in a duplicated key group, making accidental
    many-to-many merges easy to audit.
    """
    factor_utc, factor_tz, factor_null_ts = _utc_status(factor_df, "timestamp")
    return_utc, return_tz, return_null_ts = _utc_status(returns_df, "timestamp")
    lower, upper = winsorize_limits
    if not 0 <= lower < upper <= 1:
        raise ValueError("winsorize_limits must satisfy 0 <= lower < upper <= 1")

    factor_sorted = False
    returns_sorted = False
    if "timestamp" in factor_df.columns and len(factor_df) > 1:
        factor_sorted = bool(factor_df["timestamp"].is_monotonic_increasing)
    if "timestamp" in returns_df.columns and len(returns_df) > 1:
        returns_sorted = bool(returns_df["timestamp"].is_monotonic_increasing)

    return {
        "factor": {
            "rows": int(len(factor_df)),
            "missing": _missing_stats(factor_df),
            "duplicate_key_rows": _duplicate_count(factor_df, FACTOR_KEY),
            "extreme_ratio": _extreme_ratio(
                factor_df, factor_value_col, lower, upper
            ),
            "timestamp": {
                "is_utc": factor_utc,
                "timezone": factor_tz,
                "null_count": factor_null_ts,
                "is_sorted": factor_sorted,
            },
        },
        "returns": {
            "rows": int(len(returns_df)),
            "missing": _missing_stats(returns_df),
            "duplicate_key_rows": _duplicate_count(returns_df, RETURN_KEY),
            "extreme_ratio": _extreme_ratio(
                returns_df, return_value_col, lower, upper
            ),
            "timestamp": {
                "is_utc": return_utc,
                "timezone": return_tz,
                "null_count": return_null_ts,
                "is_sorted": returns_sorted,
            },
        },
    }


def _validate_timestamp_columns(
    df: pd.DataFrame,
    columns: Sequence[str],
    label: str,
) -> list[str]:
    """Validate required timestamp fields and return human-readable errors."""
    errors: list[str] = []
    for column in columns:
        if column not in df.columns:
            errors.append(f"{label} missing timestamp column: {column}")
            continue
        valid, timezone, null_count = _utc_status(df, column)
        if timezone is None:
            errors.append(f"{label}.{column} must be timezone-aware UTC")
        elif not valid and null_count == 0:
            errors.append(f"{label}.{column} must use UTC, got {timezone}")
        if null_count:
            errors.append(f"{label}.{column} contains {null_count} null timestamps")
        try:
            df[column].sort_values()
        except (TypeError, ValueError) as exc:
            errors.append(f"{label}.{column} is not sortable: {exc}")
    return errors


def check_forward_bias(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    *,
    factor_timestamp_col: str = "timestamp",
    return_timestamp_col: str = "timestamp",
    factor_available_col: str = "available_ts",
    entry_timestamp_col: str = "entry_ts",
    exit_timestamp_col: str = "exit_ts",
    factor_keys: Sequence[str] = FACTOR_KEY,
    return_keys: Sequence[str] = RETURN_KEY,
) -> dict[str, Any]:
    """Check UTC timestamps and post-merge availability ordering.

    When available and entry fields exist, every merged row must satisfy factor
    timestamp <= available time <= entry time. If these fields are absent, the
    report marks availability as unverifiable rather than silently passing.
    """
    timestamp_errors = _validate_timestamp_columns(
        factor_df, [factor_timestamp_col], "factor"
    )
    timestamp_errors.extend(
        _validate_timestamp_columns(returns_df, [return_timestamp_col], "returns")
    )
    for frame, column, label in (
        (factor_df, factor_available_col, "factor"),
        (returns_df, entry_timestamp_col, "returns"),
        (returns_df, exit_timestamp_col, "returns"),
    ):
        if column in frame.columns:
            timestamp_errors.extend(_validate_timestamp_columns(frame, [column], label))

    key_errors: list[str] = []
    missing_factor_keys = [key for key in factor_keys if key not in factor_df.columns]
    missing_return_keys = [key for key in return_keys if key not in returns_df.columns]
    if missing_factor_keys:
        key_errors.append(f"factor missing key columns: {missing_factor_keys}")
    if missing_return_keys:
        key_errors.append(f"returns missing key columns: {missing_return_keys}")
    if not key_errors:
        factor_duplicate_rows = _duplicate_count(factor_df, factor_keys)
        return_duplicate_rows = _duplicate_count(returns_df, return_keys)
        if factor_duplicate_rows:
            key_errors.append(f"factor has {factor_duplicate_rows} duplicate key rows")
        if return_duplicate_rows:
            key_errors.append(f"returns has {return_duplicate_rows} duplicate key rows")

    merged_rows = 0
    unmatched_factor_rows = 0
    merge_error: str | None = None
    merged: pd.DataFrame | None = None
    if not key_errors:
        try:
            merged = factor_df.merge(
                returns_df,
                on=["timestamp", "symbol"],
                how="left",
                indicator=True,
                validate="many_to_one",
                suffixes=("_factor", "_return"),
            )
            merged_rows = int(len(merged))
            unmatched_factor_rows = int((merged["_merge"] != "both").sum())
        except (KeyError, pd.errors.MergeError, ValueError) as exc:
            merge_error = str(exc)

    availability = {
        "verifiable": False,
        "passed": False,
        "checked_rows": 0,
        "violations": 0,
        "message": "available_ts and entry_ts are required to verify ordering",
    }
    exit_check = {
        "verifiable": False,
        "passed": True,
        "checked_rows": 0,
        "violations": 0,
        "message": "exit_ts is optional for this compatibility check",
    }
    if (
        merged is not None
        and factor_available_col in merged.columns
        and entry_timestamp_col in merged.columns
    ):
        factor_ts = merged[factor_timestamp_col]
        available_ts = merged[factor_available_col]
        entry_ts = merged[entry_timestamp_col]
        matched = merged["_merge"].eq("both")
        ordering = factor_ts.le(available_ts) & available_ts.le(entry_ts)
        violations = int((matched & ~ordering).sum())
        checked_rows = int(matched.sum())
        availability = {
            "verifiable": True,
            "passed": violations == 0 and unmatched_factor_rows == 0,
            "checked_rows": checked_rows,
            "violations": violations,
            "message": None,
        }
        if exit_timestamp_col in merged.columns:
            exit_ts = merged[exit_timestamp_col]
            exit_violations = int((matched & ~entry_ts.lt(exit_ts)).sum())
            exit_check = {
                "verifiable": True,
                "passed": exit_violations == 0 and unmatched_factor_rows == 0,
                "checked_rows": checked_rows,
                "violations": exit_violations,
                "message": None,
            }

    passed = (
        not timestamp_errors
        and not key_errors
        and merge_error is None
        and unmatched_factor_rows == 0
        and availability["passed"]
        and exit_check["passed"]
    )
    return {
        "passed": bool(passed),
        "timestamp_check": {"passed": not timestamp_errors, "errors": timestamp_errors},
        "key_check": {"passed": not key_errors, "errors": key_errors},
        "merge_check": {
            "passed": merge_error is None and unmatched_factor_rows == 0,
            "merged_rows": merged_rows,
            "unmatched_factor_rows": unmatched_factor_rows,
            "error": merge_error,
        },
        "availability_check": availability,
        "exit_check": exit_check,
    }
