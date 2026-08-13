"""Timestamp-spacing helpers shared by factor definitions.

Factor parameters are expressed in wall-clock days (``lookback_days``) so the
same factor definition behaves identically at any evaluation cadence: the day
window is converted to bar counts using the data's actual bar spacing. This
keeps factor authors from hardcoding bar counts that silently change meaning
when the evaluation frequency changes.
"""

from __future__ import annotations

import math

import pandas as pd


def median_bar_seconds(df: pd.DataFrame, ts_col: str = "timestamp") -> float:
    """Median spacing between consecutive timestamps, in seconds.

    Returns 0.0 for frames with fewer than two rows (no observable cadence).
    """
    if df is None or len(df) < 2 or ts_col not in df.columns:
        return 0.0
    diff = pd.to_datetime(df[ts_col], utc=True).diff().dropna()
    if diff.empty:
        return 0.0
    return float(diff.median().total_seconds())


def lookback_bars(
    df: pd.DataFrame, lookback_days: int | float | None, min_bars: int = 1
) -> int:
    """Convert a lookback window in days into bar counts on the data's cadence.

    ``ceil(days × 86400 / median_bar_spacing)`` — at a 1d cadence the bar
    count equals the day count, matching the historical behavior. Falls back
    to ``min_bars`` when the cadence cannot be observed or the window is
    non-positive.
    """
    if lookback_days is None or lookback_days <= 0:
        return min_bars
    spacing = median_bar_seconds(df)
    if spacing <= 0:
        return min_bars
    bars = int(math.ceil(float(lookback_days) * 86400 / spacing))
    return max(bars, min_bars)
