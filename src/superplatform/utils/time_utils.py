"""Time handling utilities.

All timestamps in the system MUST be UTC datetime64[ns].
This module provides conversion and validation helpers.
"""

from datetime import UTC, datetime

import pandas as pd


def to_utc(ts: pd.Series | datetime | str | int | float) -> pd.Timestamp | datetime:
    """Convert a timestamp to timezone-aware UTC.

    Handles:
    - Unix timestamps (int/float, seconds or milliseconds)
    - ISO 8601 strings
    - pandas Timestamps
    - Python datetimes
    """
    if isinstance(ts, pd.Series):
        return ts.apply(to_utc)
    if isinstance(ts, (int, float)):
        # Auto-detect seconds vs milliseconds
        if ts > 1e12:
            ts = ts / 1000.0
        dt = datetime.fromtimestamp(ts, tz=UTC)
        return pd.Timestamp(dt)
    if isinstance(ts, str):
        t = pd.Timestamp(ts)
        if t.tz is None:
            return t.tz_localize("UTC")
        return t.tz_convert("UTC")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    if isinstance(ts, pd.Timestamp):
        if ts.tz is None:
            return ts.tz_localize("UTC")
        return ts.tz_convert("UTC")
    raise TypeError(f"Cannot convert {type(ts)} to UTC")


def utc_now() -> pd.Timestamp:
    """Return current time as UTC-aware Timestamp."""
    return pd.Timestamp.now(tz="UTC")


def check_utc_series(ts: pd.Series) -> bool:
    """Verify a timestamp Series is timezone-aware UTC."""
    if hasattr(ts.dtype, "tz") and ts.dtype.tz is not None:
        return str(ts.dtype.tz).upper() in ("UTC", "UTCN", "ETC/UTC")
    return False
