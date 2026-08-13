"""End-to-end pipeline test using synthetic data."""

import asyncio

import pandas as pd
import pytest

from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.providers.synthetic import SyntheticKLineProvider
from superplatform.factors.registry import FactorRegistry
from superplatform.runtime.config import Config
from superplatform.runtime.pipeline import OfflineRuntime


@pytest.fixture
def registry():
    reg = DataProviderRegistry()
    reg.register(SyntheticKLineProvider(seed=42))
    return reg


@pytest.fixture
def config():
    return Config({
        "factors": {
            "momentum": {
                "symbols": ["S1", "S2", "S3", "S4", "S5"],
                "providers": {"kline": "synthetic-kline"},
                "params": {"lookback_days": 20},
            },
            "realized_vol": {
                "symbols": ["S1", "S2", "S3", "S4", "S5"],
                "providers": {"kline": "synthetic-kline"},
                "params": {"lookback_days": 20},
            },
            "volume_ratio": {
                "symbols": ["S1", "S2", "S3", "S4", "S5"],
                "providers": {"kline": "synthetic-kline"},
                "params": {"lookback_days": 20},
            },
        },
        "evaluation": {
            "forward_bias": {"n_cutoffs": 5},
        },
    })


@pytest.fixture(autouse=True)
def _register_factors():
    """Ensure factors are registered before each test."""
    FactorRegistry.get_instance().auto_discover()
    yield


def test_single_factor_pipeline(config, registry):
    """Test that a single factor runs end-to-end."""
    runtime = OfflineRuntime(config, registry)
    results = asyncio.run(runtime.run(["momentum"]))

    assert len(results) == 1
    r = results[0]
    assert r.factor_name == "momentum"

    # IC should be computed
    assert not r.ic_df.empty
    assert "ic" in r.ic_df.columns

    # ICIR stats should be present
    assert "icir" in r.ic_stats

    # Layer test should have 5 layers
    layers = r.layer_results["layer"].unique()
    assert len(layers) == 5

    # Turnover should be between 0 and 1
    assert 0 <= r.turnover_df["turnover"].mean() <= 1

    # Rolling stability should be derived from the evaluated IC series.
    assert not r.rolling_df.empty
    assert {"window_start", "window_end", "icir"}.issubset(r.rolling_df.columns)

    # Forward-bias check must pass for a properly-implemented factor
    assert r.forward_bias_passed, (
        f"Forward-bias check failed! Details: {r.forward_bias_passed}"
    )


def test_multi_factor_pipeline(config, registry):
    """Test that multiple factors run together."""
    runtime = OfflineRuntime(config, registry)
    results = asyncio.run(runtime.run(["momentum", "realized_vol", "volume_ratio"]))

    assert len(results) == 3

    for r in results:
        assert not r.ic_df.empty
        assert not r.layer_results.empty
        assert r.forward_bias_passed, f"Forward-bias check failed for {r.factor_name}"


def test_factor_output_shape(config, registry):
    """Test that factor output has expected structure."""
    runtime = OfflineRuntime(config, registry)
    results = asyncio.run(runtime.run(["momentum"]))

    # Factor runs per symbol; check one symbol's result
    per_symbol = results[0].per_symbol
    assert len(per_symbol) == 5  # 5 symbols in config
    for _symbol, result in per_symbol.items():
        fv = result.values
        assert "timestamp" in fv.columns
        assert "value" in fv.columns
        assert len(fv) > 100  # ~1.5 years of daily data


def test_forward_bias_default_audits_representative_group(config, registry):
    """Default audit granularity is one representative group per factor.

    The forward-bias audit verifies the factor implementation, so by default
    only the group with the longest reference series is audited — all symbols
    are still computed (per_group is fully populated), but the audit runs once.
    """
    runtime = OfflineRuntime(config, registry)
    results = asyncio.run(runtime.run(["momentum"], skip_report=True))

    per_group = results[0].per_symbol
    assert len(per_group) == 5  # all 5 symbols still computed
    assert len(results[0].forward_bias_reports) == 1
    assert results[0].forward_bias_reports[0].passed


def test_forward_bias_groups_all_audits_every_group(config, registry):
    """groups=all keeps the strict per-group audit."""
    full_config = Config({
        **config.to_dict(),
        "evaluation": {
            "forward_bias": {"n_cutoffs": 5, "groups": "all"},
        },
    })
    runtime = OfflineRuntime(full_config, registry)
    results = asyncio.run(runtime.run(["momentum"], skip_report=True))
    assert len(results[0].forward_bias_reports) == 5
    assert all(r.passed for r in results[0].forward_bias_reports)


def test_validation_reports(config, registry):
    """Test that validation reports are generated."""
    runtime = OfflineRuntime(config, registry)
    results = asyncio.run(runtime.run(["momentum"]))

    assert len(results[0].validation_reports) > 0
    for report in results[0].validation_reports:
        assert "schema_validation" in report
        assert "utc_check" in report
        assert "spot_perpetual_check" in report
        assert report["utc_check"]["is_utc"], f"UTC check failed: {report['utc_check']}"


def test_strategy_backtest_respects_factor_date_range(config, registry):
    """Strategy data must honor the factor-level date range used by the web API."""
    strategy_config = Config({
        "factors": {
            "momentum": {
                "symbols": ["S1", "S2"],
                "providers": {"kline": "synthetic-kline"},
                "params": {"lookback_days": 20},
                "start": "2021-01-01",
                "end": "2021-03-31",
            },
        },
        "factor_instances": {
            "momentum_20d": {"factory": "momentum", "params": {"lookback_days": 20}},
        },
        "evaluation": {
            "sample_start": "2024-01-01",
            "sample_end": "2024-12-31",
        },
    })
    runtime = OfflineRuntime(strategy_config, registry)

    result = asyncio.run(runtime.run_strategy("momentum_demo"))

    trades = result["backtest"].trades
    assert set(trades["symbol"]) == {"S1", "S2"}
    assert trades["timestamp"].max() <= pd.Timestamp("2021-03-31")
