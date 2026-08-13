"""Unit tests for the minimal factor metrics and QC layer."""

import numpy as np
import pandas as pd
import pytest

from superplatform.evaluation.metrics import (
    compute_ic,
    compute_ic_ir,
    compute_rank_ic,
    evaluate_factor,
)
from superplatform.evaluation.qc import check_forward_bias, run_qc


def _tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build deterministic UTC factor and return tables."""
    timestamps = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    rows = []
    returns = []
    for timestamp in timestamps:
        for symbol, value in zip(["A", "B", "C"], [1.0, 2.0, 3.0], strict=True):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "factor_name": "f1",
                    "factor_value": value,
                    "available_ts": timestamp,
                }
            )
            returns.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "forward_return": value * 0.01,
                    "entry_ts": timestamp + pd.Timedelta(minutes=1),
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(returns)


def test_cross_sectional_ic_is_perfect_positive() -> None:
    """Pearson IC should be one for a perfectly aligned cross-section."""
    factors, returns = _tables()
    result = compute_ic(factors, returns)
    assert len(result) == 3
    assert np.allclose(result["ic"], 1.0)
    assert result["n_assets"].tolist() == [3, 3, 3]


def test_cross_sectional_rank_ic_is_perfect_positive() -> None:
    """Spearman RankIC should be one for a monotonic cross-section."""
    factors, returns = _tables()
    result = compute_rank_ic(factors, returns)
    assert np.allclose(result["rank_ic"], 1.0)


def test_ic_ir_uses_time_series_sample_standard_deviation() -> None:
    """IC_IR should equal the mean divided by ddof=1 standard deviation."""
    data = pd.Series([0.1, 0.2, 0.3])
    expected = data.mean() / data.std(ddof=1)
    result = compute_ic_ir(data)
    assert result["n_periods"] == 3
    assert result["ic_ir"] == pytest.approx(expected)


def test_ic_ir_zero_standard_deviation_is_nan() -> None:
    """A zero-variance IC series must not produce an infinite IC_IR."""
    result = compute_ic_ir(pd.Series([0.1, 0.1, 0.1]))
    assert result["std_ic"] == 0.0
    assert np.isnan(result["ic_ir"])


def test_winsorize_and_zscore_are_configurable() -> None:
    """Preprocessing switches should add transformed values without losing raw data."""
    factors, returns = _tables()
    factors.loc[factors.index[-1], "factor_value"] = 100.0
    panel, summary = evaluate_factor(
        factors,
        returns,
        winsorize_enabled=True,
        zscore_enabled=True,
        winsorize_limits=(0.0, 0.8),
    )
    assert {"factor_value_raw", "factor_value_eval", "is_outlier"}.issubset(panel.columns)
    assert panel["factor_value_raw"].max() == 100.0
    assert panel["is_outlier"].any()
    assert "qc" in summary


def test_qc_reports_missing_duplicates_and_extreme_ratio() -> None:
    """QC should expose missing values, duplicate key rows, and extreme ratio."""
    factors, returns = _tables()
    factors.loc[0, "factor_value"] = np.nan
    factors = pd.concat([factors, factors.iloc[[1]]], ignore_index=True)
    report = run_qc(factors, returns, winsorize_limits=(0.0, 0.8))
    assert report["factor"]["missing"]["factor_value"] == 1
    assert report["factor"]["duplicate_key_rows"] == 2
    assert 0 <= report["factor"]["extreme_ratio"] <= 1


def test_forward_bias_check_passes_with_post_close_entry() -> None:
    """Availability checks should pass when entry follows factor availability."""
    factors, returns = _tables()
    report = check_forward_bias(factors, returns)
    assert report["passed"]
    assert report["availability_check"]["violations"] == 0


def test_forward_bias_check_rejects_entry_before_factor_availability() -> None:
    """An entry before the factor is available must fail the hard check."""
    factors, returns = _tables()
    returns["entry_ts"] = returns["timestamp"] - pd.Timedelta(minutes=1)
    report = check_forward_bias(factors, returns)
    assert not report["passed"]
    assert report["availability_check"]["violations"] == len(returns)


def test_duplicate_keys_are_rejected_before_metric_computation() -> None:
    """Metric computation must not silently create a many-to-many merge."""
    factors, returns = _tables()
    factors = pd.concat([factors, factors.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate keys"):
        compute_ic(factors, returns)


def test_non_utc_timestamps_are_rejected() -> None:
    """Naive timestamps violate the UTC input contract."""
    factors, returns = _tables()
    factors["timestamp"] = factors["timestamp"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="UTC"):
        compute_ic(factors, returns)


def test_premerged_metric_panel_still_validates_utc() -> None:
    """Direct metric calls cannot bypass the UTC contract."""
    factors, returns = _tables()
    panel = factors.merge(returns, on=["timestamp", "symbol"])
    panel["timestamp"] = panel["timestamp"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="UTC"):
        compute_ic(panel)
