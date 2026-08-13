"""Unit tests for the bar-spacing helpers used by lookback_days params."""

import pandas as pd

from superplatform.utils.timestamps import lookback_bars, median_bar_seconds


def test_median_bar_seconds():
    df = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=5, freq="4h", tz="UTC")})
    assert median_bar_seconds(df) == 4 * 3600
    assert median_bar_seconds(pd.DataFrame({"timestamp": []})) == 0.0
    assert median_bar_seconds(pd.DataFrame({"timestamp": [pd.Timestamp("2024-01-01", tz="UTC")]})) == 0.0


def test_lookback_bars_daily_matches_days():
    df = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=30, freq="1D")})
    assert lookback_bars(df, 20) == 20
    assert lookback_bars(df, 5) == 5


def test_lookback_bars_converts_days_to_bars_on_cadence():
    df_4h = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=200, freq="4h")})
    assert lookback_bars(df_4h, 10) == 60  # 10 days × 6 bars/day
    df_8h = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=200, freq="8h")})
    assert lookback_bars(df_8h, 20) == 60  # 20 days × 3 bars/day
    # Partial day still rounds up so the window is at least as long.
    df_1m = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=10_000, freq="1min")})
    assert lookback_bars(df_1m, 1) == 1440


def test_lookback_bars_degenerate_inputs():
    df = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=10, freq="1D")})
    assert lookback_bars(df, None) == 1
    assert lookback_bars(df, 0) == 1
    assert lookback_bars(df, -5) == 1
    assert lookback_bars(pd.DataFrame({"timestamp": []}), 20) == 1
