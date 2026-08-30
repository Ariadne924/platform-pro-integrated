from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from superplatform.ml.threshold_research import (
    ThresholdResearchConfig,
    run_threshold_research,
    threshold_positions,
)


def _inputs(periods: int = 240) -> tuple[pd.Series, dict[str, pd.DataFrame], pd.Series]:
    timestamps = pd.date_range("2025-01-01", periods=periods, freq="D", tz="UTC")
    scores = np.sin(np.arange(periods) / 7.0) + 0.15 * np.sin(np.arange(periods) / 2.0)
    realized = np.r_[0.0, np.sign(scores[:-1]) * 0.004]
    close = 100.0 * np.cumprod(1.0 + realized)
    index = pd.MultiIndex.from_arrays(
        [timestamps, np.repeat("BTC", periods)], names=["timestamp", "symbol"]
    )
    score_series = pd.Series(scores, index=index, name="signal")
    price_data = {
        "BTC": pd.DataFrame({"timestamp": timestamps, "close": close})
    }
    regimes = pd.Series(
        np.resize(["bull"] * 30 + ["bear"] * 30 + ["sideways"] * 30, periods),
        index=timestamps,
        name="regime",
    )
    return score_series, price_data, regimes


def _permissive_config() -> ThresholdResearchConfig:
    return ThresholdResearchConfig(
        enabled=True,
        entry_quantiles=(0.55, 0.70, 0.85),
        exit_quantiles=(0.10, 0.25, 0.40),
        rolling_window=30,
        rolling_step=15,
        min_neighbor_count=1,
        min_neighbor_positive_ratio=0.0,
        max_neighbor_return_dispersion=10.0,
        max_drawdown_limit=1.0,
        min_rolling_positive_ratio=0.0,
        min_regime_non_loss_ratio=0.0,
        min_stable_region_size=1,
    )


def test_threshold_positions_apply_entry_exit_hysteresis() -> None:
    timestamps = pd.date_range("2026-01-01", periods=7, freq="D", tz="UTC")
    index = pd.MultiIndex.from_arrays(
        [timestamps, ["BTC"] * 7], names=["timestamp", "symbol"]
    )
    scores = pd.Series([0.0, 0.8, 0.5, 0.1, -0.9, -0.4, -0.1], index=index)

    positions = threshold_positions(
        scores,
        entry_threshold=0.7,
        exit_threshold=0.2,
        allow_short=True,
        strategy_name="test",
    )

    assert positions["position"].tolist() == [0.0, 1.0, 1.0, 0.0, -1.0, -1.0, 0.0]


def test_threshold_surface_runs_cost_aware_backtests_and_finds_regions() -> None:
    scores, prices, regimes = _inputs()
    result = run_threshold_research(
        scores,
        price_data=prices,
        regime=regimes,
        strategy_name="ensemble",
        config=_permissive_config(),
        allow_short=True,
        taker_fee_bps=4.0,
        slippage_bps=2.0,
    )

    assert result["status"] == "completed"
    assert result["surface"]
    assert result["stable_regions"]
    assert result["recommended_point"] is not None
    point = result["surface"][0]
    assert set(point["regime_metrics"]) == {"bull", "bear", "sideways"}
    assert point["rolling_windows"]
    assert point["exit_threshold"] < point["entry_threshold"]
    assert 0.0 <= point["win_rate"] <= 1.0
    assert point["average_turnover"] >= 0.0


def test_transaction_costs_reduce_threshold_surface_return() -> None:
    scores, prices, regimes = _inputs()
    free = run_threshold_research(
        scores,
        price_data=prices,
        regime=regimes,
        strategy_name="ensemble",
        config=_permissive_config(),
        allow_short=True,
    )
    costly = run_threshold_research(
        scores,
        price_data=prices,
        regime=regimes,
        strategy_name="ensemble",
        config=_permissive_config(),
        allow_short=True,
        taker_fee_bps=20.0,
        slippage_bps=10.0,
    )

    free_points = {
        (row["entry_index"], row["exit_index"]): row for row in free["surface"]
    }
    costly_points = {
        (row["entry_index"], row["exit_index"]): row for row in costly["surface"]
    }
    key = next(iter(free_points))
    assert costly_points[key]["total_return"] < free_points[key]["total_return"]


def test_discrete_strategy_signal_is_reported_instead_of_inventing_a_surface() -> None:
    scores, prices, regimes = _inputs()
    discrete = scores.gt(0).astype(float)

    result = run_threshold_research(
        discrete,
        price_data=prices,
        regime=regimes,
        strategy_name="discrete",
        config=ThresholdResearchConfig(enabled=True),
    )

    assert result["status"] == "insufficient_signal_resolution"
    assert result["surface"] == []
    assert result["recommended_point"] is None


def test_future_signal_distribution_cannot_change_calibrated_thresholds() -> None:
    scores, prices, regimes = _inputs()
    config = _permissive_config()
    original = run_threshold_research(
        scores,
        price_data=prices,
        regime=regimes,
        strategy_name="ensemble",
        config=config,
        allow_short=True,
    )
    changed = scores.copy()
    cutoff = pd.Timestamp(original["calibration"]["end"])
    future = changed.index.get_level_values("timestamp") > cutoff
    changed.loc[future] = changed.loc[future] * 100.0 + 50.0
    rerun = run_threshold_research(
        changed,
        price_data=prices,
        regime=regimes,
        strategy_name="ensemble",
        config=config,
        allow_short=True,
    )

    assert rerun["calibration"] == original["calibration"]
    assert rerun["entry_thresholds"] == original["entry_thresholds"]
    assert rerun["exit_thresholds"] == original["exit_thresholds"]


def test_threshold_config_rejects_unsorted_quantiles() -> None:
    with pytest.raises(ValueError, match="unique and increasing"):
        ThresholdResearchConfig(
            enabled=True,
            entry_quantiles=(0.8, 0.6),
        ).validate()
