"""Stress coverage for explainable evaluation behavior under adverse inputs."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from superplatform.evaluation.backtest import BacktestResult, run_backtest
from superplatform.evaluation.qc import run_qc


def _panel(*, days: int = 2, symbols: int = 10) -> pd.DataFrame:
    """Create a deterministic UTC panel suitable for layer backtests."""
    rows: list[dict[str, object]] = []
    for timestamp in pd.date_range("2024-01-01", periods=days, freq="D", tz="UTC"):
        for index in range(symbols):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": f"S{index:02d}",
                    "factor_name": "stress_factor",
                    "factor_value": float(index),
                    "ret_1": float(index + 1) / 100.0,
                }
            )
    return pd.DataFrame(rows)


def _assert_explainable_layer_log(result: BacktestResult) -> None:
    """Every stress run must retain an inspectable layer-assignment explanation."""
    assert not result.logs.empty
    assert {"timestamp", "status", "reason", "n_assets", "actual_q"}.issubset(
        result.logs.columns
    )


def test_sparse_cross_section_degrades_with_a_logged_explanation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A one-asset date is skipped rather than crashing the entire backtest."""
    panel = pd.concat(
        [
            _panel(days=1),
            _panel(days=1, symbols=1).assign(
                timestamp=pd.Timestamp("2024-01-02", tz="UTC")
            ),
        ],
        ignore_index=True,
    )

    with caplog.at_level(logging.WARNING, logger="superplatform.evaluation.backtest"):
        result = run_backtest(panel, q=10)

    sparse_log = result.logs[result.logs["timestamp"] == pd.Timestamp("2024-01-02", tz="UTC")]
    assert sparse_log.iloc[0]["status"] == "skipped"
    assert sparse_log.iloc[0]["reason"] == "fewer than two feasible groups"
    assert not result.decile_returns.empty
    assert any("Layer assignment skipped" in record.message for record in caplog.records)


def test_extreme_return_shock_is_reported_by_qc_and_retains_finite_outputs() -> None:
    """One huge forward return remains visible in QC without breaking outputs."""
    panel = _panel()
    shock_timestamp = pd.Timestamp("2024-01-02", tz="UTC")
    panel.loc[
        (panel["timestamp"] == shock_timestamp) & (panel["symbol"] == "S09"),
        "ret_1",
    ] = 1_000.0
    returns = panel[["timestamp", "symbol", "ret_1"]]

    result = run_backtest(panel, q=2)
    qc_result = run_qc(panel, returns, return_value_col="ret_1")

    _assert_explainable_layer_log(result)
    assert qc_result["returns"]["extreme_ratio"] > 0.0
    assert np.isfinite(result.decile_returns["gross_return"]).all()
    assert result.long_short_returns["gross_return"].max() > 100.0


def test_random_thirty_percent_missing_returns_remain_explainable() -> None:
    """Randomly missing returns reduce valid assets but do not abort the run."""
    panel = _panel(days=3)
    mask = np.random.default_rng(42).random(len(panel)) < 0.30
    panel.loc[mask, "ret_1"] = np.nan
    returns = panel[["timestamp", "symbol", "ret_1"]]

    result = run_backtest(panel, q=2)
    qc_result = run_qc(panel, returns, return_value_col="ret_1")

    _assert_explainable_layer_log(result)
    assert qc_result["returns"]["missing"]["ret_1"] == int(mask.sum())
    assert (result.decile_returns["n_assets"] < result.decile_returns["n_constituents"]).any()
    assert result.decile_returns["gross_return"].notna().any()


def test_tied_factor_values_use_rank_cut_with_a_logged_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Large tied cross-sections use the stable fallback instead of failing qcut."""
    panel = _panel(days=1)
    panel["factor_value"] = 1.0

    with caplog.at_level(logging.INFO, logger="superplatform.evaluation.backtest"):
        result = run_backtest(panel, q=10)

    _assert_explainable_layer_log(result)
    assert result.logs.loc[0, "method"] == "rank_cut"
    assert "duplicate quantile edges" in result.logs.loc[0, "reason"]
    assert result.decile_returns["quantile"].nunique() == 10
    assert any("Layer assignment degraded" in record.message for record in caplog.records)
