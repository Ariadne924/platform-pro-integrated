"""Acceptance tests for stability, correlation, reporting, and time contracts."""

from pathlib import Path

import pandas as pd
import pytest

from superplatform.evaluation.correlation import compute_factor_correlations
from superplatform.evaluation.experiment import (
    _filter_sample,
    _load_panel,
    _resolve_market_contract,
    _validate_temporal_contract,
)
from superplatform.evaluation.report import generate_plots, write_evaluation_report
from superplatform.evaluation.stability import rolling_stability


def _factor_panel(days: int = 4) -> pd.DataFrame:
    """Create a small UTC panel with two same-date factors."""
    timestamps = pd.date_range("2024-01-01", periods=days, tz="UTC")
    rows: list[dict[str, object]] = []
    for timestamp in timestamps:
        for symbol, value in zip(["A", "B", "C"], [1.0, 2.0, 3.0], strict=True):
            rows.extend(
                [
                    {
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "factor_name": "f1",
                        "factor_value": value,
                    },
                    {
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "factor_name": "f2",
                        "factor_value": value * 2.0,
                    },
                ]
            )
    return pd.DataFrame(rows)


def test_rolling_stability_has_mean_and_ir_columns() -> None:
    """Rolling output must expose both requested stability measures."""
    timestamps = pd.date_range("2024-01-01", periods=3, tz="UTC")
    ic_data = pd.DataFrame(
        {
            "timestamp": timestamps,
            "factor_name": "f1",
            "ic": [0.1, 0.2, 0.3],
        }
    )
    result = rolling_stability(ic_data, window_days=60, min_periods=2)
    assert {"rolling_ic_mean", "rolling_ic_ir", "n_periods"}.issubset(result.columns)
    assert result.iloc[-1]["rolling_ic_mean"] == pytest.approx(0.2)


def test_rolling_stability_window_has_at_most_requested_daily_points() -> None:
    """A 60-day inclusive window must contain 60 daily observations, not 61."""
    timestamps = pd.date_range("2024-01-01", periods=61, tz="UTC")
    result = rolling_stability(
        pd.DataFrame(
            {
                "timestamp": timestamps,
                "factor_name": "f1",
                "ic": 0.1,
            }
        ),
        window_days=60,
        min_periods=1,
    )
    assert result.iloc[-1]["n_periods"] == 60


def test_same_date_factor_correlation_is_perfect_for_scaled_factor() -> None:
    """A factor scaled by two should have Pearson and Spearman correlation one."""
    panel = _factor_panel()
    matrices = compute_factor_correlations(panel, min_assets=3)
    assert matrices["pearson"].loc["f1", "f2"] == pytest.approx(1.0)
    assert matrices["spearman"].loc["f1", "f2"] == pytest.approx(1.0)


def test_report_writes_pngs_and_markdown(tmp_path: Path) -> None:
    """Report helpers must create all three visualization classes and Markdown."""
    panel = _factor_panel(days=2)
    ic_data = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, tz="UTC"),
            "factor_name": ["f1", "f1"],
            "ic": [0.1, 0.2],
        }
    )
    deciles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, tz="UTC"),
            "factor_name": ["f1", "f1"],
            "quantile": [1, 1],
            "mean_return": [0.01, 0.02],
        }
    )
    generate_plots(
        ic_data,
        deciles,
        compute_factor_correlations(panel, min_assets=3),
        tmp_path,
    )
    report_path = write_evaluation_report(
        output_path=tmp_path / "evaluation_report.md",
        methods={"ic": "Pearson"},
        parameters={"seed": 42},
        sample_statistics={"rows": 2},
        core_results={"ic_ir": 1.0},
        risks=["costs omitted"],
        failed_tasks=[],
    )
    assert (tmp_path / "ic_timeseries.png").exists()
    assert (tmp_path / "layer_nav.png").exists()
    assert (tmp_path / "corr_pearson.png").exists()
    assert report_path.read_text(encoding="utf-8").find("Risk Notes") >= 0
    assert report_path.read_text(encoding="utf-8").find("Cost Treatment") >= 0


def test_temporal_contract_rejects_entry_before_availability() -> None:
    """A factor cannot be entered before it is available."""
    timestamp = pd.Timestamp("2024-01-01", tz="UTC")
    panel = pd.DataFrame(
        {
            "timestamp": [timestamp],
            "available_ts": [timestamp],
            "entry_ts": [timestamp],
            "exit_ts": [timestamp + pd.Timedelta(days=1)],
        }
    )
    with pytest.raises(ValueError, match="entry_ts"):
        _validate_temporal_contract(panel, require_metadata=True)


def test_temporal_contract_rejects_mismatched_horizon() -> None:
    """The selected return horizon must match the configured bar interval."""
    timestamp = pd.Timestamp("2024-01-01", tz="UTC")
    panel = pd.DataFrame(
        {
            "timestamp": [timestamp],
            "available_ts": [timestamp],
            "entry_ts": [timestamp + pd.Timedelta(days=1)],
            "exit_ts": [timestamp + pd.Timedelta(days=3)],
        }
    )
    with pytest.raises(ValueError, match="exit interval"):
        _validate_temporal_contract(
            panel,
            require_metadata=True,
            return_col="ret_1",
            bar_interval="1d",
        )


def test_load_panel_rejects_naive_timestamp(tmp_path: Path) -> None:
    """CSV timestamps without an explicit timezone must not be treated as UTC."""
    path = tmp_path / "panel.csv"
    pd.DataFrame(
        {
            "timestamp": ["2024-01-01"],
            "symbol": ["A"],
            "factor_name": ["f1"],
            "factor_value": [1.0],
            "ret_1": [0.01],
        }
    ).to_csv(path, index=False)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _load_panel(path)


def test_load_panel_requires_configured_horizon(tmp_path: Path) -> None:
    """A ret_5 run must load ret_5 instead of implicitly falling back to ret_1."""
    path = tmp_path / "panel.csv"
    pd.DataFrame(
        {
            "timestamp": ["2024-01-01T00:00:00Z"],
            "symbol": ["A"],
            "factor_name": ["f1"],
            "factor_value": [1.0],
            "ret_5": [0.05],
        }
    ).to_csv(path, index=False)
    loaded = _load_panel(path, return_col="ret_5")
    assert loaded["ret_5"].tolist() == [0.05]

    with pytest.raises(ValueError, match="configured return column: ret_10"):
        _load_panel(path, return_col="ret_10")


def test_sample_filter_requires_dynamic_eligibility() -> None:
    """Only rows marked eligible may enter the evaluation sample by default."""
    timestamp = pd.Timestamp("2024-01-01", tz="UTC")
    panel = pd.DataFrame(
        {
            "timestamp": [timestamp, timestamp],
            "symbol": ["A", "B"],
            "is_eligible": [True, False],
        }
    )
    result = _filter_sample(panel, {"universe": {"require_eligibility": True}})
    assert result["symbol"].tolist() == ["A"]


def test_market_filter_and_spot_contract_are_long_only() -> None:
    """Market selection must isolate spot rows and prohibit a synthetic short leg."""
    timestamp = pd.Timestamp("2024-01-01", tz="UTC")
    panel = pd.DataFrame(
        {
            "timestamp": [timestamp, timestamp],
            "symbol": ["A", "B"],
            "exchange": ["binance", "other"],
            "market_type": ["spot", "perpetual"],
            "settlement_asset": ["USDT", "USDT"],
        }
    )
    config = {
        "universe": {"require_eligibility": False},
        "market": {
            "exchange": "binance",
            "exchange_column": "exchange",
            "market_type": "spot",
            "market_column": "market_type",
            "settlement_asset": "USDT",
            "settlement_asset_column": "settlement_asset",
            "allow_short": False,
        },
    }
    selected = _filter_sample(panel, config)
    market_type, allow_short = _resolve_market_contract(config, selected)
    assert selected["symbol"].tolist() == ["A"]
    assert market_type == "spot"
    assert not allow_short


def test_perpetual_contract_does_not_require_funding_declaration() -> None:
    """Perpetual funding is now calculated from the input sequence, not declared."""
    market_type, allow_short = _resolve_market_contract(
        {"market": {"market_type": "perpetual", "allow_short": True}},
        pd.DataFrame({"symbol": ["A"]}),
    )
    assert market_type == "perpetual"
    assert allow_short
