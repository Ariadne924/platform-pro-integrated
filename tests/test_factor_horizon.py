"""Regression tests: the unified ``momentum`` factor treats lookback_days
literally at any input cadence (via ``lookback_bars``).

The momentum family (10d/20d/60d/120d) was deduplicated into one configurable
``momentum`` factor. The old ``momentum_10d`` used a raw row count (``period``),
so on a non-daily buffer it silently meant N *minutes*, not N days. This file
guards the unified factor's day-literal semantics across cadences.
"""

import pandas as pd

from superplatform.factors.registry import FactorRegistry


def _kline(periods: int, freq: str) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range(
            "2024-01-01", periods=periods, freq=freq, tz="UTC",
        ).as_unit("ns"),
        "close": [100.0 + i for i in range(periods)],
    })


FactorRegistry.get_instance().auto_discover()


def _factor(name):
    return FactorRegistry.get_instance().get(name)


def test_momentum_60d_daily_is_literal_60_days():
    res = _factor("momentum").compute(
        {"kline": {"S1": _kline(200, "1D")}}, lookback_days=60
    )
    values = res.values.dropna()
    assert len(values) == 140  # first 60 daily bars are the warm-up window
    assert values["timestamp"].iloc[0] == res.values["timestamp"].iloc[60]


def test_momentum_120d_daily_is_literal_120_days():
    res = _factor("momentum").compute(
        {"kline": {"S1": _kline(200, "1D")}}, lookback_days=120
    )
    values = res.values.dropna()
    assert len(values) == 80
    assert values["timestamp"].iloc[0] == res.values["timestamp"].iloc[120]


def test_momentum_60d_4h_needs_360_bars_not_60_rows():
    # A raw row-count factor would emit 140 non-NaN values on 200 rows; the
    # day-literal window is 60 days × 6 bars/day = 360 bars, so 200 rows of
    # 4h data produce no value at all.
    res = _factor("momentum").compute(
        {"kline": {"S1": _kline(200, "4h")}}, lookback_days=60
    )
    assert res.values["value"].notna().sum() == 0


def test_momentum_default_param_is_lookback_days_20():
    info = _factor("momentum")
    assert info.params_schema["lookback_days"]["default"] == 20
