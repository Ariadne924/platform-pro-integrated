from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from superplatform.ml.models import WalkForwardConfig
from superplatform.ml.regime import RegimeConfig
from superplatform.ml.research import MLResearchConfig, prepare_ml_panel, run_ml_research


def _research_panel(periods: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    symbols = ("BTC", "ETH", "SOL", "BNB")
    factor_names = ("momentum", "low_volatility", "liquidity")
    rows: list[dict] = []
    closes = {symbol: 100.0 + index * 10 for index, symbol in enumerate(symbols)}
    for time_index, timestamp in enumerate(timestamps):
        raw_scores = {
            symbol: np.sin(time_index / 8.0 + symbol_index) + rng.normal(0, 0.1)
            for symbol_index, symbol in enumerate(symbols)
        }
        future_returns = {
            symbol: 0.006 * score + rng.normal(0, 0.002)
            for symbol, score in raw_scores.items()
        }
        for symbol_index, symbol in enumerate(symbols):
            closes[symbol] *= 1.0 + future_returns[symbol]
            factors = {
                "momentum": raw_scores[symbol],
                "low_volatility": -abs(rng.normal(0.5, 0.2)) + raw_scores[symbol] * 0.2,
                "liquidity": symbol_index / len(symbols) + rng.normal(0, 0.05),
            }
            for factor_name in factor_names:
                rows.append(
                    {
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "factor_name": factor_name,
                        "factor_value": factors[factor_name],
                        "ret_1": future_returns[symbol],
                        "ret_5": future_returns[symbol] * 5,
                        "ret_10": future_returns[symbol] * 10,
                        "ret_20": future_returns[symbol] * 20,
                        "close": closes[symbol],
                    }
                )
    return pd.DataFrame(rows)


def _config() -> MLResearchConfig:
    return MLResearchConfig(
        target_horizon=1,
        core_factor="momentum",
        top_n=2,
        frequency="1d",
        models=("ridge", "elastic_net", "tree_stumps"),
        walk_forward=WalkForwardConfig(
            min_train_periods=30,
            test_periods=15,
            horizon_periods=1,
            embargo_periods=1,
            max_features=3,
        ),
        regime=RegimeConfig(
            fast_window=5,
            slow_window=20,
            volatility_window=5,
            confirmation_periods=2,
        ),
        reference_symbol="BTC",
    )


def test_prepare_ml_panel_pivots_factors_without_losing_utc() -> None:
    features, target, prices = prepare_ml_panel(_research_panel(30), target_horizon=1)
    assert list(features.columns) == ["liquidity", "low_volatility", "momentum"]
    assert features.index.names == ["timestamp", "symbol"]
    assert str(features.index.get_level_values("timestamp").tz) == "UTC"
    assert target.index.equals(features.index)
    assert set(prices.columns) == {"timestamp", "symbol", "close"}


def test_run_ml_research_completes_full_vertical_slice() -> None:
    result = run_ml_research(_research_panel(), config=_config())
    assert result["status"] == "completed_research_only"
    assert result["protocol_version"] == "ml-research-v2"
    assert result["sample"]["factors"] == ["liquidity", "low_volatility", "momentum"]
    assert result["sample"]["oos_prediction_rows"] > 0
    assert result["strategy"]["equity"]
    assert result["equal_weight_benchmark"]["equity"]
    assert result["folds"]
    assert result["feature_recommendations"]
    assert result["market_regime"]["latest"]["regime"] in {
        "bull",
        "bear",
        "sideways",
    }
    assert set(result["market_regime"]["performance"]) == {
        "bull",
        "bear",
        "sideways",
    }
    assert result["score"]["weights"]["upside_bonus"] == 5
    assert result["score"]["score"] <= 100
    comparison = result["strategy_comparison"]
    assert comparison["benchmark"] == "equal_weight"
    assert {row["name"] for row in comparison["leaderboard"]} == {
        "ridge",
        "elastic_net",
        "tree_stumps",
        "ensemble",
        "equal_weight",
        "core_factor",
    }
    assert comparison["relative_to_benchmark"]
    assert comparison["pareto_front"]
    kinds = {row["name"]: row["kind"] for row in comparison["leaderboard"]}
    assert kinds["ridge"] == "trained_model"
    assert kinds["ensemble"] == "derived_ensemble"
    assert kinds["equal_weight"] == "non_ml_baseline"
    assert kinds["core_factor"] == "non_ml_baseline"
    assert {
        row["sample_count"] for row in comparison["leaderboard"]
    } == {comparison["common_window"]["periods"]}
    recommendations = result["feature_recommendations"]
    assert any(row["role"] == "core" for row in recommendations)
    assert np.isclose(sum(abs(row["recommended_weight"]) for row in recommendations), 1.0)


def test_run_ml_research_requires_cross_section() -> None:
    panel = _research_panel(50)
    panel = panel[panel["symbol"].eq("BTC")]
    try:
        run_ml_research(panel, config=_config())
    except ValueError as exc:
        assert "cross_section" in str(exc)
    else:
        raise AssertionError("single-symbol research must be rejected")


def test_single_asset_mode_builds_timing_strategy_and_core_factor_baseline() -> None:
    panel = _research_panel()[lambda frame: frame["symbol"].eq("BTC")]
    result = run_ml_research(
        panel,
        config=replace(_config(), research_mode="single_asset", top_n=1),
    )

    assert result["config"]["research_mode"] == "single_asset"
    assert result["models"]["ensemble"]["method"] == "time_series_single_asset"
    assert "core_factor" in {
        row["name"] for row in result["strategy_comparison"]["leaderboard"]
    }


def test_existing_strategy_is_retested_and_scored_with_signal_ic() -> None:
    panel = _research_panel()
    base = panel.drop_duplicates(["timestamp", "symbol"])[
        ["timestamp", "symbol", "ret_1"]
    ].copy()
    base["position"] = np.where(base["ret_1"] >= 0, 1.0, -1.0)

    result = run_ml_research(
        panel,
        config=_config(),
        existing_strategy_signals={"PYS-101": base[["timestamp", "symbol", "position"]]},
    )

    rows = {
        row["name"]: row for row in result["strategy_comparison"]["leaderboard"]
    }
    assert rows["PYS-101"]["kind"] == "existing_strategy"
    evidence = result["existing_strategy_scores"]["PYS-101"]
    assert evidence["score"]["score"] <= 100
    assert evidence["correlations"]["method"] == "strategy_position_signal"
    assert evidence["correlations"]["ic"] is not None
    assert result["existing_strategy_errors"] == {}


def test_existing_strategy_without_oos_position_is_reported_not_fatal() -> None:
    signals = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2020-01-01", tz="UTC")],
            "symbol": ["BTC"],
            "position": [0.0],
        }
    )
    result = run_ml_research(
        _research_panel(),
        config=_config(),
        existing_strategy_signals={"empty_strategy": signals},
    )
    assert "empty_strategy" in result["existing_strategy_errors"]
    assert "empty_strategy" not in result["existing_strategy_scores"]
