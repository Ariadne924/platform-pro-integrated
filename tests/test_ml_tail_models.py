from __future__ import annotations

import numpy as np
import pandas as pd

from superplatform.ml.tail_models import estimate_dynamic_risk


def test_hybrid_tail_model_keeps_historical_baseline_and_uses_conservative_tail() -> None:
    rng = np.random.default_rng(42)
    returns = pd.Series(
        rng.standard_t(df=4, size=2_000) * 0.01,
        index=pd.date_range("2024-01-01", periods=2_000, freq="h", tz="UTC"),
    )

    estimate = estimate_dynamic_risk(returns, evt_min_exceedances=20)

    assert estimate.historical_var > 0
    assert estimate.filtered_var > 0
    assert estimate.evt_var is not None
    assert estimate.evt_expected_shortfall is not None
    assert estimate.evt_exceedances >= 20
    assert estimate.selected_var >= max(
        estimate.historical_var,
        estimate.filtered_var,
        estimate.evt_var,
    )
    assert estimate.selected_expected_shortfall >= max(
        estimate.historical_expected_shortfall,
        estimate.filtered_expected_shortfall,
        estimate.evt_expected_shortfall,
    )


def test_fhs_reacts_to_a_recent_volatility_shock() -> None:
    rng = np.random.default_rng(7)
    calm = rng.normal(0.0, 0.002, 800)
    shock = rng.normal(0.0, 0.04, 80)
    index = pd.date_range("2025-01-01", periods=880, freq="h", tz="UTC")

    before = estimate_dynamic_risk(pd.Series(calm, index=index[:800]))
    after = estimate_dynamic_risk(pd.Series(np.r_[calm, shock], index=index))

    assert after.filtered_var > before.filtered_var * 3
    assert after.filtered_expected_shortfall > before.filtered_expected_shortfall * 3


def test_har_rv_forecast_is_available_after_enough_intraday_history() -> None:
    rng = np.random.default_rng(3)
    periods = 24 * 60
    returns = pd.Series(
        rng.normal(0.0, 0.005, periods),
        index=pd.date_range("2025-01-01", periods=periods, freq="h", tz="UTC"),
    )

    estimate = estimate_dynamic_risk(returns, har_min_days=30)

    assert estimate.har_annualized_volatility_forecast is not None
    assert estimate.har_annualized_volatility_forecast > 0
    assert estimate.selected_annualized_volatility >= (
        estimate.har_annualized_volatility_forecast
    )


def test_evt_gracefully_falls_back_when_tail_sample_is_too_short() -> None:
    returns = pd.Series(
        [0.01, -0.02, 0.005, -0.01, 0.003],
        index=pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC"),
    )

    estimate = estimate_dynamic_risk(returns, evt_min_exceedances=5)

    assert estimate.evt_var is None
    assert estimate.evt_expected_shortfall is None
    assert estimate.selected_var >= estimate.historical_var
