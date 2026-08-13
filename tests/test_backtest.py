"""Unit tests for quantile returns, long-short spread, NAV, and turnover."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from superplatform.evaluation.backtest import (
    assign_quantiles,
    compute_decile_returns,
    compute_long_short_returns,
    compute_turnover,
    run_backtest,
)


def _panel(
    *,
    days: int = 2,
    symbols: list[str] | None = None,
    factors_by_day: list[list[float]] | None = None,
) -> pd.DataFrame:
    """Build a deterministic UTC daily factor panel."""
    symbols = symbols or [f"S{i}" for i in range(1, 11)]
    timestamps = pd.date_range("2024-01-01", periods=days, freq="D", tz="UTC")
    if factors_by_day is None:
        factors_by_day = [list(np.arange(1, len(symbols) + 1, dtype=float))] * days
    rows: list[dict[str, object]] = []
    for timestamp, factors in zip(timestamps, factors_by_day, strict=True):
        for symbol, factor in zip(symbols, factors, strict=True):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "factor_name": "f1",
                    "factor_value": factor,
                    "ret_1": factor / 100.0,
                    "ret_5": factor / 200.0,
                    "ret_10": factor / 300.0,
                }
            )
    return pd.DataFrame(rows)


def test_assign_quantiles_creates_ordered_deciles() -> None:
    """The lowest and highest factor observations must be Q1 and Q10."""
    assigned = assign_quantiles(_panel(days=1), q=10)
    by_symbol = assigned.set_index("symbol")["quantile"]
    assert by_symbol["S1"] == 1
    assert by_symbol["S10"] == 10
    assert assigned["quantile"].nunique() == 10


def test_long_short_is_top_minus_bottom_and_horizon_is_selectable() -> None:
    """The spread must equal the selected horizon's top return minus bottom return."""
    panel = _panel(days=1)
    deciles = compute_decile_returns(panel, return_col="ret_5", q=2)
    spread = compute_long_short_returns(deciles)
    assert spread.loc[0, "top_return"] == pytest.approx(0.04)
    assert spread.loc[0, "bottom_return"] == pytest.approx(0.015)
    assert spread.loc[0, "gross_return"] == pytest.approx(0.025)
    assert spread.loc[0, "net_return"] == pytest.approx(0.025)
    assert spread.loc[0, "long_short_return"] == pytest.approx(0.025)


def test_turnover_is_composition_change_ratio() -> None:
    """Replacing one of two constituents should produce 50 percent turnover."""
    panel = _panel(
        symbols=["A", "B", "C", "D"],
        factors_by_day=[
            [1.0, 2.0, 3.0, 4.0],
            [1.0, 4.0, 2.0, 3.0],
        ],
    )
    assigned = assign_quantiles(panel, q=2)
    turnover = compute_turnover(assigned)
    first_day = turnover.iloc[0]
    second_day_q1 = turnover[
        (turnover["timestamp"] == pd.Timestamp("2024-01-02", tz="UTC"))
        & (turnover["quantile"] == 1)
    ].iloc[0]
    assert np.isnan(first_day["turnover"])
    assert second_day_q1["overlap"] == 1
    assert second_day_q1["turnover"] == 0.5


def test_small_sample_and_duplicate_values_are_logged_and_degraded() -> None:
    """Small tied cross-sections should use rank-cut rather than fail."""
    panel = _panel(
        days=1,
        symbols=["A", "B", "C"],
        factors_by_day=[[1.0, 1.0, 1.0]],
    )
    assigned = assign_quantiles(panel, q=10)
    logs = pd.DataFrame(assigned.attrs["layer_log"])
    assert assigned["quantile"].notna().all()
    assert assigned["quantile"].nunique() == 3
    assert logs.loc[0, "method"] == "rank_cut"
    assert logs.loc[0, "actual_q"] == 3


def test_run_backtest_writes_required_outputs_and_compounds_nav(
    tmp_path: Path,
) -> None:
    """The orchestration function should persist all requested CSV artifacts."""
    result = run_backtest(_panel(days=2), q=2, output_dir=tmp_path)
    for filename in (
        "decile_returns.csv",
        "long_short_returns.csv",
        "long_short_nav.csv",
        "turnover.csv",
    ):
        assert (tmp_path / filename).exists()
    assert result.long_short_nav["nav"].tolist() == [1.05, 1.1025]


def test_spot_backtest_keeps_layers_but_disables_long_short() -> None:
    """A long-only market may retain layers and turnover but not a short spread."""
    result = run_backtest(_panel(days=2), q=2, allow_short=False)
    assert not result.decile_returns.empty
    assert not result.turnover.empty
    assert result.long_short_returns.empty
    assert result.long_short_nav.empty


def test_turnover_costs_reduce_layer_and_long_short_net_returns() -> None:
    """Costs equal turnover times fee plus slippage for both long-short legs."""
    panel = _panel(
        symbols=["A", "B", "C", "D"],
        factors_by_day=[
            [1.0, 2.0, 3.0, 4.0],
            [1.0, 4.0, 2.0, 3.0],
        ],
    )
    panel["ret_1"] = 0.1

    result = run_backtest(
        panel,
        q=2,
        fee_bps=10.0,
        slippage_bps=10.0,
    )
    second_day = pd.Timestamp("2024-01-02", tz="UTC")
    q1 = result.decile_returns[
        (result.decile_returns["timestamp"] == second_day)
        & (result.decile_returns["quantile"] == 1)
    ].iloc[0]
    spread = result.long_short_returns[
        result.long_short_returns["timestamp"] == second_day
    ].iloc[0]

    assert q1["gross_return"] == pytest.approx(0.1)
    assert q1["cost"] == pytest.approx(0.001)
    assert q1["net_return"] == pytest.approx(0.099)
    assert spread["gross_return"] == pytest.approx(0.0)
    assert spread["cost"] == pytest.approx(0.002)
    assert spread["net_return"] == pytest.approx(-0.002)


def test_backtest_rejects_duplicate_factor_keys() -> None:
    """Direct backtest calls must not double-count duplicate factor observations."""
    panel = _panel(days=1)
    duplicated = pd.concat([panel, panel.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate timestamp/symbol/factor_name"):
        run_backtest(duplicated, q=2)
