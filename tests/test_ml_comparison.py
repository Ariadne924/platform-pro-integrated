from __future__ import annotations

import pandas as pd

from superplatform.ml.comparison import compare_strategy_returns


def _returns() -> dict[str, pd.Series]:
    index = pd.date_range("2024-01-01", periods=24, freq="D", tz="UTC")
    return {
        "steady": pd.Series([0.002] * 24, index=index),
        "volatile": pd.Series(([0.03, -0.025] * 12), index=index),
        "equal_weight": pd.Series([0.001] * 24, index=index),
    }


def test_comparison_uses_one_shared_window_and_risk_first_rank() -> None:
    returns = _returns()
    returns["volatile"] = returns["volatile"].iloc[2:]
    result = compare_strategy_returns(
        returns,
        benchmark_name="equal_weight",
        periods_per_year=365,
        scorecards={
            "steady": {"score": 82.0, "status": "eligible"},
            "volatile": {"score": 90.0, "status": "rejected", "gates_failed": ["var_limit"]},
            "equal_weight": {"score": 70.0, "status": "eligible"},
        },
        bootstrap_samples=40,
    )

    assert result["common_window"]["periods"] == 22
    assert result["leaderboard"][0]["name"] == "steady"
    assert result["leaderboard"][-1]["name"] == "volatile"
    assert len(result["relative_to_benchmark"]) == 2


def test_comparison_bootstrap_is_deterministic_and_exposes_pareto_front() -> None:
    kwargs = {
        "benchmark_name": "equal_weight",
        "periods_per_year": 365,
        "bootstrap_samples": 40,
    }
    first = compare_strategy_returns(_returns(), **kwargs)
    second = compare_strategy_returns(_returns(), **kwargs)

    assert first["pairwise"] == second["pairwise"]
    assert first["pareto_front"]
    assert set(first["correlation_matrix"]) == {"steady", "volatile", "equal_weight"}
