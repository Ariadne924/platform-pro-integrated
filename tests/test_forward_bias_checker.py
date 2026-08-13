"""Tests for the ForwardBiasChecker fast path (baseline reuse)."""

import numpy as np
import pandas as pd

from superplatform.evaluation.forward_bias import ForwardBiasChecker


def _causal_fn(df: pd.DataFrame) -> pd.DataFrame:
    """Rolling mean of ``close`` over its own past — causal by construction."""
    out = df[["timestamp", "close"]].copy()
    out["value"] = out["close"].rolling(3, min_periods=1).mean()
    return out[["timestamp", "value"]]


def _data(periods: int = 40) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=periods, freq="D"),
        "close": np.arange(periods, dtype=float) + 1.0,
    })


def test_baseline_reuse_skips_full_recompute_and_matches() -> None:
    """Supplying a baseline must skip the redundant full-sample recompute."""
    data = _data()
    checker = ForwardBiasChecker(n_cutoffs=5)
    calls = {"n": 0}

    def counting_fn(df):
        calls["n"] += 1
        return _causal_fn(df)

    calls["n"] = 0
    report_no_baseline = checker.check("factor", counting_fn, data)
    assert calls["n"] == 6  # 1 baseline + 5 truncated recomputes

    calls["n"] = 0
    report_with_baseline = checker.check(
        "factor", counting_fn, data, baseline=_causal_fn(data)
    )
    # Only the truncated recomputes run; the baseline is reused.
    assert calls["n"] == 5

    assert report_no_baseline.passed == report_with_baseline.passed
    assert report_no_baseline.n_mismatches == report_with_baseline.n_mismatches
    assert report_no_baseline.max_abs_diff == report_with_baseline.max_abs_diff
    assert report_no_baseline.details == report_with_baseline.details


def test_baseline_reuse_with_series() -> None:
    """An already-extracted Series can be used as the baseline."""
    data = _data()
    checker = ForwardBiasChecker(n_cutoffs=5)
    baseline = _causal_fn(data).set_index("timestamp")["value"]
    report = checker.check("factor", _causal_fn, data, baseline=baseline)
    assert report.passed
    assert report.n_cutoffs == 5
    assert len(report.details) == 5
