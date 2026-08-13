"""Tests for regression baseline creation and threshold alerts."""

from pathlib import Path

from superplatform.evaluation.experiment import _run_regression_guard


def _metrics(*, mean_ic: float = 0.1) -> dict[str, float | None]:
    return {
        "mean_ic": mean_ic,
        "rankic_mean": 0.2,
        "ic_ir": 1.5,
        "long_short_ann_return": 0.3,
    }


def test_regression_guard_creates_a_baseline_snapshot(tmp_path: Path) -> None:
    """The first successful run freezes its key metric snapshot."""
    baseline_path = tmp_path / "regression_baseline.json"
    result = _run_regression_guard(
        _metrics(),
        baseline_path=baseline_path,
        thresholds={metric: 0.0 for metric in _metrics()},
    )

    assert result["status"] == "baseline_created"
    assert result["alerts"] == []
    assert baseline_path.exists()
    assert '"mean_ic": 0.1' in baseline_path.read_text(encoding="utf-8")


def test_regression_guard_alerts_when_metric_delta_exceeds_threshold(
    tmp_path: Path,
) -> None:
    """A later run preserves the baseline and reports threshold breaches."""
    baseline_path = tmp_path / "regression_baseline.json"
    thresholds = {
        "mean_ic": 0.01,
        "rankic_mean": 0.01,
        "ic_ir": 0.1,
        "long_short_ann_return": 0.1,
    }
    _run_regression_guard(
        _metrics(),
        baseline_path=baseline_path,
        thresholds=thresholds,
    )

    result = _run_regression_guard(
        _metrics(mean_ic=0.07),
        baseline_path=baseline_path,
        thresholds=thresholds,
    )

    assert result["status"] == "alerts"
    assert result["alerts"] == [
        {
            "metric": "mean_ic",
            "baseline": 0.1,
            "current": 0.07,
            "delta": -0.03,
            "threshold": 0.01,
        }
    ]
    assert '"mean_ic": 0.1' in baseline_path.read_text(encoding="utf-8")
