from __future__ import annotations

import numpy as np
import pandas as pd

from superplatform.ml.models import WalkForwardConfig, walk_forward_panel


def _panel(periods: int = 90, symbols: tuple[str, ...] = ("BTC", "ETH", "SOL")):
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    index = pd.MultiIndex.from_product(
        [timestamps, symbols], names=["timestamp", "symbol"]
    )
    x1 = np.linspace(-1.0, 1.0, len(index))
    x2 = np.sin(np.arange(len(index)) / 7.0)
    features = pd.DataFrame({"momentum": x1, "volatility": x2}, index=index)
    target = pd.Series(0.03 * x1 - 0.01 * x2, index=index, name="target")
    return features, target


def test_walk_forward_panel_purges_horizon_and_embargo() -> None:
    features, target = _panel()
    config = WalkForwardConfig(
        min_train_periods=30,
        test_periods=10,
        horizon_periods=3,
        embargo_periods=2,
        max_features=2,
    )
    result = walk_forward_panel(features, target, config=config)

    assert result.folds
    for fold in result.folds:
        train_end = pd.Timestamp(fold["train_end"])
        test_start = pd.Timestamp(fold["test_start"])
        assert (test_start - train_end).days >= 6
        assert fold["purge_periods"] == 3
        assert fold["embargo_periods"] == 2
    assert result.predictions["ensemble"].notna().any()


def test_walk_forward_preprocessing_does_not_see_future_values() -> None:
    features, target = _panel()
    config = WalkForwardConfig(min_train_periods=30, test_periods=10, max_features=2)
    original = walk_forward_panel(features, target, config=config)

    changed = features.copy()
    cutoff = pd.Timestamp(original.folds[0]["test_end"])
    future = changed.index.get_level_values("timestamp") > cutoff
    changed.loc[future, "momentum"] = 1_000_000.0
    rerun = walk_forward_panel(changed, target, config=config)

    first_test = original.predictions.index.get_level_values("timestamp") <= cutoff
    pd.testing.assert_series_equal(
        original.predictions.loc[first_test, "ensemble"],
        rerun.predictions.loc[first_test, "ensemble"],
    )


def test_walk_forward_requires_utc_panel() -> None:
    features, target = _panel(periods=40)
    naive_timestamps = features.index.get_level_values("timestamp").tz_localize(None)
    features.index = pd.MultiIndex.from_arrays(
        [naive_timestamps, features.index.get_level_values("symbol")],
        names=["timestamp", "symbol"],
    )
    target.index = features.index

    try:
        walk_forward_panel(features, target)
    except ValueError as exc:
        assert "UTC" in str(exc)
    else:
        raise AssertionError("naive panel must be rejected")
