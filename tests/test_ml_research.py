from __future__ import annotations

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


def test_run_ml_research_requires_cross_section() -> None:
    panel = _research_panel(50)
    panel = panel[panel["symbol"].eq("BTC")]
    try:
        run_ml_research(panel, config=_config())
    except ValueError as exc:
        assert "two symbols" in str(exc)
    else:
        raise AssertionError("single-symbol research must be rejected")
