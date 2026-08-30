"""Causal alignment of Gold factor panels sampled at different frequencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

FREQUENCY_DURATION = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "6h": pd.Timedelta(hours=6),
    "8h": pd.Timedelta(hours=8),
    "1d": pd.Timedelta(days=1),
}


@dataclass
class MultiFrequencyResult:
    panel: pd.DataFrame
    metadata: dict[str, Any]


def _normalize(panel: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "symbol", "factor_name", "factor_value", "close"}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"factor panel is missing required columns: {missing}")
    frame = panel.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if frame["timestamp"].isna().any():
        raise ValueError("factor panel contains invalid timestamps")
    frame["symbol"] = frame["symbol"].astype(str)
    frame["factor_name"] = frame["factor_name"].astype(str)
    return frame.sort_values(["symbol", "timestamp", "factor_name"])


def fuse_factor_panels(
    panels: dict[str, pd.DataFrame],
    *,
    base_frequency: str,
) -> MultiFrequencyResult:
    """Align every feature backward onto the base decision timestamps.

    Labels and prices always come from the base-frequency panel.  A slower or
    faster feature is joined with ``direction='backward'`` and a finite
    tolerance, so a row can never see a feature timestamp later than itself.
    """
    if base_frequency not in panels:
        raise ValueError("base_frequency must be present in panels")
    if base_frequency not in FREQUENCY_DURATION:
        raise ValueError(f"unsupported base frequency: {base_frequency}")
    normalized = {frequency: _normalize(panel) for frequency, panel in panels.items()}
    unsupported = sorted(set(normalized) - set(FREQUENCY_DURATION))
    if unsupported:
        raise ValueError(f"unsupported feature frequencies: {unsupported}")

    base = normalized[base_frequency]
    keys = ["timestamp", "symbol"]
    label_columns = [name for name in base.columns if name.startswith("ret_")]
    base_rows = (
        base.drop_duplicates(keys, keep="first")[[*keys, "close", *label_columns]]
        .sort_values(["symbol", "timestamp"])
        .reset_index(drop=True)
    )
    merged = base_rows.copy()
    feature_sources: dict[str, str] = {}
    staleness: dict[str, str] = {}

    for frequency, frame in normalized.items():
        pivot = (
            frame.drop_duplicates([*keys, "factor_name"], keep="last")
            .set_index([*keys, "factor_name"])["factor_value"]
            .unstack("factor_name")
            .reset_index()
            .sort_values(["symbol", "timestamp"])
        )
        factor_columns = [name for name in pivot.columns if name not in keys]
        renamed = {
            name: name if frequency == base_frequency else f"{name}@{frequency}"
            for name in factor_columns
        }
        feature_sources.update({output: frequency for output in renamed.values()})
        source_timestamp = f"__source_timestamp_{frequency}"
        pivot[source_timestamp] = pivot["timestamp"]
        pivot = pivot.rename(columns=renamed)
        tolerance = max(
            FREQUENCY_DURATION[base_frequency],
            FREQUENCY_DURATION[frequency] * 2,
        )
        staleness[frequency] = str(tolerance)
        pieces: list[pd.DataFrame] = []
        for symbol, left in merged.groupby("symbol", sort=True):
            right = pivot[pivot["symbol"].eq(symbol)].drop(columns="symbol")
            left_sorted = left.sort_values("timestamp")
            if right.empty:
                joined = left_sorted.copy()
                for name in renamed.values():
                    joined[name] = pd.NA
                joined[source_timestamp] = pd.NaT
            else:
                joined = pd.merge_asof(
                    left_sorted,
                    right.sort_values("timestamp"),
                    on="timestamp",
                    direction="backward",
                    tolerance=tolerance,
                    allow_exact_matches=True,
                )
            pieces.append(joined)
        merged = pd.concat(pieces, ignore_index=True).sort_values(["symbol", "timestamp"])

    source_columns = [name for name in merged.columns if name.startswith("__source_timestamp_")]
    feature_columns = [name for name in feature_sources if name in merged.columns]
    long = merged.melt(
        id_vars=[*keys, "close", *label_columns],
        value_vars=feature_columns,
        var_name="factor_name",
        value_name="factor_value",
    ).dropna(subset=["factor_value"])
    violations = 0
    for source_column in source_columns:
        source = pd.to_datetime(merged[source_column], utc=True, errors="coerce")
        violations += int((source > merged["timestamp"]).fillna(False).sum())
    if violations:
        raise ValueError("multi-frequency alignment produced future feature timestamps")
    return MultiFrequencyResult(
        panel=long.sort_values(["timestamp", "symbol", "factor_name"]).reset_index(drop=True),
        metadata={
            "enabled": len(normalized) > 1,
            "base_frequency": base_frequency,
            "feature_frequencies": list(normalized),
            "feature_sources": feature_sources,
            "staleness_tolerance": staleness,
            "causal_join": "backward_asof",
            "future_timestamp_violations": violations,
            "rows": len(long),
        },
    )
