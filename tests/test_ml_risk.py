from __future__ import annotations

import pandas as pd

from superplatform.ml.risk import ScoreConfig, score_research_result, tail_risk_metrics


def test_tail_metrics_separate_downside_and_upside() -> None:
    returns = pd.Series([-0.10, -0.03, 0.01, 0.02, 0.20])
    benchmark = pd.Series([-0.08, -0.02, 0.01, 0.04, 0.10])
    metrics = tail_risk_metrics(returns, benchmark_returns=benchmark, confidence=0.8)

    assert metrics["historical_var"] > 0
    assert metrics["expected_shortfall"] > 0
    assert metrics["risk_var"] >= metrics["historical_var"]
    assert metrics["risk_expected_shortfall"] >= metrics["expected_shortfall"]
    assert metrics["expected_tail_gain"] > 0
    assert metrics["upside_capture"] is not None


def test_upside_bonus_cannot_override_downside_gate() -> None:
    score = score_research_result(
        strategy_metrics={"total_return": 0.50, "sharpe": 3.0},
        benchmark_metrics={"total_return": 0.10},
        tail_metrics={
            "max_drawdown": 0.50,
            "historical_var": 0.02,
            "expected_shortfall": 0.03,
            "upside_capture": 2.0,
            "top_days_positive_contribution": 0.2,
        },
        fold_metrics=[{"sample_count": 10, "total_return": 0.2}],
        regime_metrics={"bull": {"sample_count": 10, "total_return": 0.2}},
        ic=0.10,
        rank_ic=0.10,
        config=ScoreConfig(max_drawdown_limit=0.25),
    )

    assert score["components"]["upside_bonus"] == 5.0
    assert score["status"] == "rejected"
    assert "max_drawdown_limit" in score["gates_failed"]


def test_score_weights_match_agreed_risk_first_contract() -> None:
    score = score_research_result(
        strategy_metrics={"total_return": 0.10, "sharpe": 1.0},
        benchmark_metrics={"total_return": 0.05},
        tail_metrics={
            "max_drawdown": 0.10,
            "historical_var": 0.02,
            "expected_shortfall": 0.03,
            "upside_capture": 0.8,
            "top_days_positive_contribution": 0.3,
        },
        fold_metrics=[{"sample_count": 10, "total_return": 0.02}],
        regime_metrics={"sideways": {"sample_count": 10, "total_return": 0.01}},
        ic=0.03,
        rank_ic=0.04,
    )
    assert score["weights"] == {
        "downside_risk": 45,
        "walk_forward_robustness": 20,
        "relative_performance": 20,
        "ic_rank_ic": 10,
        "upside_bonus": 5,
    }


def test_dynamic_tail_risk_can_reject_when_historical_baseline_looks_safe() -> None:
    score = score_research_result(
        strategy_metrics={"total_return": 0.10, "sharpe": 1.0},
        benchmark_metrics={"total_return": 0.05},
        tail_metrics={
            "max_drawdown": 0.10,
            "historical_var": 0.01,
            "expected_shortfall": 0.02,
            "risk_var": 0.04,
            "risk_expected_shortfall": 0.06,
        },
        fold_metrics=[{"sample_count": 10, "total_return": 0.02}],
        regime_metrics={"sideways": {"sample_count": 10, "total_return": 0.01}},
        ic=0.03,
        rank_ic=0.04,
    )

    assert score["status"] == "rejected"
    assert "var_limit" in score["gates_failed"]
    assert "expected_shortfall_limit" in score["gates_failed"]
