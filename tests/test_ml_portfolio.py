from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from superplatform.ml.portfolio import (
    PortfolioConfig,
    allocate_weights,
    build_portfolio_signals,
)


def _returns(periods: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return pd.DataFrame(
        {
            "BTC": rng.normal(0.001, 0.010, periods),
            "ETH": rng.normal(0.001, 0.020, periods),
            "SOL": rng.normal(0.001, 0.035, periods),
        },
        index=pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC"),
    )


def test_allocation_methods_produce_capped_long_only_weights() -> None:
    returns = _returns()
    for method in ("equal_weight", "inverse_volatility", "risk_parity", "hrp"):
        weights = allocate_weights(
            returns,
            PortfolioConfig(method=method, max_weight=0.45),
        )
        assert weights.sum() == pytest.approx(1.0)
        assert (weights >= 0).all()
        assert weights.max() <= 0.45 + 1e-9


def test_risk_constraint_scales_positions_instead_of_only_scoring_afterward() -> None:
    returns = _returns(90)
    prices = (1.0 + returns).cumprod().mul(100).rename_axis("timestamp").reset_index()
    prices = prices.melt(id_vars="timestamp", var_name="symbol", value_name="close")
    timestamps = pd.DatetimeIndex(sorted(prices["timestamp"].unique()))
    index = pd.MultiIndex.from_product(
        [timestamps, ["BTC", "ETH", "SOL"]], names=["timestamp", "symbol"]
    )
    scores = pd.Series(np.tile([3.0, 2.0, 1.0], len(timestamps)), index=index)
    result = build_portfolio_signals(
        scores,
        prices,
        top_n=3,
        config=PortfolioConfig(
            method="risk_parity",
            lookback_periods=40,
            min_history_periods=20,
            var_limit=0.001,
            expected_shortfall_limit=0.0015,
        ),
        name="ensemble",
    )
    constrained = [row for row in result.allocations if row["risk_constraint_triggered"]]
    assert constrained
    assert all(sum(row["weights"].values()) < 1.0 for row in constrained)
    latest = result.allocations[-1]
    assert latest["risk_model"] == "hybrid_fhs_evt"
    assert latest["filtered_var"] >= 0
    assert latest["historical_var"] >= 0
    assert latest["risk_history_periods"] >= latest["history_periods"]


def test_annualized_volatility_cap_can_reduce_exposure() -> None:
    returns = _returns(90)
    prices = (1.0 + returns).cumprod().mul(100).rename_axis("timestamp").reset_index()
    prices = prices.melt(id_vars="timestamp", var_name="symbol", value_name="close")
    timestamps = pd.DatetimeIndex(sorted(prices["timestamp"].unique()))
    index = pd.MultiIndex.from_product(
        [timestamps, ["BTC", "ETH", "SOL"]], names=["timestamp", "symbol"]
    )
    scores = pd.Series(np.tile([3.0, 2.0, 1.0], len(timestamps)), index=index)
    result = build_portfolio_signals(
        scores,
        prices,
        top_n=3,
        config=PortfolioConfig(
            method="risk_parity",
            lookback_periods=40,
            min_history_periods=20,
            var_limit=1.0,
            expected_shortfall_limit=1.0,
            annual_volatility_limit=0.05,
        ),
        name="ensemble",
    )
    constrained = [row for row in result.allocations if row["risk_constraint_triggered"]]
    assert constrained
    assert all(row["estimated_annualized_volatility"] > 0.05 for row in constrained)


def test_hard_risk_line_forces_cash_then_staged_recovery() -> None:
    returns = _returns(90).mul(0.1)
    returns.iloc[50] = -0.20
    prices = (1.0 + returns).cumprod().mul(100).rename_axis("timestamp").reset_index()
    prices = prices.melt(id_vars="timestamp", var_name="symbol", value_name="close")
    timestamps = pd.DatetimeIndex(sorted(prices["timestamp"].unique()))
    index = pd.MultiIndex.from_product(
        [timestamps, ["BTC", "ETH", "SOL"]], names=["timestamp", "symbol"]
    )
    scores = pd.Series(np.tile([3.0, 2.0, 1.0], len(timestamps)), index=index)
    result = build_portfolio_signals(
        scores,
        prices,
        top_n=3,
        config=PortfolioConfig(
            method="equal_weight",
            lookback_periods=30,
            min_history_periods=10,
            var_limit=1.0,
            expected_shortfall_limit=1.0,
            annual_volatility_limit=3.0,
            single_period_loss_limit=0.10,
            cooldown_periods=3,
        ),
        name="ensemble",
    )
    liquidation = next(
        event for event in result.risk_events if event["event"] == "forced_liquidation"
    )
    assert liquidation["reason"] == "single_period_loss"
    event_timestamp = liquidation["timestamp"]
    event_row = next(row for row in result.allocations if row["timestamp"] == event_timestamp)
    assert event_row["circuit_breaker_state"] == "cooldown"
    assert sum(event_row["weights"].values()) == pytest.approx(0.0)
    recovery = [
        row for row in result.allocations if row["circuit_breaker_state"] == "recovery"
    ]
    assert [row["circuit_scale"] for row in recovery[:4]] == [0.25, 0.5, 0.75, 1.0]

    audit_events = [
        event for event in result.risk_events if event["event"] == "risk_triggered"
    ]
    assert any(event["action"] == "hold_cash" for event in audit_events)
    assert any(event["action"] == "staged_recovery" for event in audit_events)
    assert all("observed" in event and "thresholds" in event for event in audit_events)


def test_risk_limit_transitions_are_recorded_without_logging_every_bar() -> None:
    returns = _returns(90)
    prices = (1.0 + returns).cumprod().mul(100).rename_axis("timestamp").reset_index()
    prices = prices.melt(id_vars="timestamp", var_name="symbol", value_name="close")
    timestamps = pd.DatetimeIndex(sorted(prices["timestamp"].unique()))
    index = pd.MultiIndex.from_product(
        [timestamps, ["BTC", "ETH", "SOL"]], names=["timestamp", "symbol"]
    )
    scores = pd.Series(np.tile([3.0, 2.0, 1.0], len(timestamps)), index=index)
    result = build_portfolio_signals(
        scores,
        prices,
        top_n=3,
        config=PortfolioConfig(
            method="equal_weight",
            lookback_periods=30,
            min_history_periods=10,
            var_limit=0.001,
            expected_shortfall_limit=0.0015,
            annual_volatility_limit=0.05,
        ),
        name="ensemble",
    )
    transitions = [
        event
        for event in result.risk_events
        if event["event"] in {"risk_triggered", "risk_recovered"}
    ]
    assert transitions
    assert len(transitions) < len(result.allocations)
    first = transitions[0]
    assert first["active_limits"]
    assert first["risk_scale"] < 1.0
    assert first["observed"]["var"] >= 0.0
    assert first["thresholds"]["var_limit"] == pytest.approx(0.001)


def test_portfolio_history_is_strictly_causal() -> None:
    returns = _returns(80)
    prices = (1.0 + returns).cumprod().mul(100).rename_axis("timestamp").reset_index()
    prices = prices.melt(id_vars="timestamp", var_name="symbol", value_name="close")
    timestamps = pd.DatetimeIndex(sorted(prices["timestamp"].unique()))
    index = pd.MultiIndex.from_product(
        [timestamps, ["BTC", "ETH", "SOL"]], names=["timestamp", "symbol"]
    )
    scores = pd.Series(np.tile([3.0, 2.0, 1.0], len(timestamps)), index=index)
    config = PortfolioConfig(
        method="hrp", lookback_periods=30, min_history_periods=15, max_weight=0.7
    )
    original = build_portfolio_signals(scores, prices, top_n=3, config=config, name="a")
    cutoff = timestamps[55]
    changed = prices.copy()
    changed.loc[changed["timestamp"] > cutoff, "close"] *= 50.0
    rerun = build_portfolio_signals(scores, changed, top_n=3, config=config, name="b")
    before_original = [row for row in original.allocations if pd.Timestamp(row["timestamp"]) <= cutoff]
    before_rerun = [row for row in rerun.allocations if pd.Timestamp(row["timestamp"]) <= cutoff]
    assert before_original == before_rerun
