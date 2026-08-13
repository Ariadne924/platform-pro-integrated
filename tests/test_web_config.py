"""Tests for web config overlay, provider labels, and provider resolution."""

import asyncio
from pathlib import Path

import pytest
import yaml

import superplatform_web.state as _state
from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.providers.synthetic import SyntheticKLineProvider


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path, monkeypatch):
    """Isolate config + provider registry for each test."""
    # Point settings overlay at a temp file so tests never touch the real one.
    monkeypatch.setattr(_state, "_CONFIG_FILES", (
        str(tmp_path / "default.yaml"),
        str(tmp_path / "exchanges.yaml"),
        str(tmp_path / "factors.yaml"),
        str(tmp_path / "settings.yaml"),
    ))
    # Fresh in-memory registry per test.
    if _state.store is not None:
        _state.store.close()
    _state.store = None
    _state.providers.clear()
    _state.providers = DataProviderRegistry()
    yield
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _setup_config_files(tmp_path) -> None:
    _write(tmp_path / "default.yaml", {
        "data": {"cache": {"enabled": True, "path": str(tmp_path / "cache.duckdb")}, "symbols": {"perpetual": ["BTCUSDT"]}},
        "evaluation": {"sample_start": "2021-01-01", "cost": {"maker_fee_bps": 2.0}},
        "exchanges": {"binance": {"enabled": True, "proxy": ""}},
    })
    _write(tmp_path / "exchanges.yaml", {})
    _write(tmp_path / "factors.yaml", {})


def test_config_load_merges_settings_overlay(tmp_path):
    """Settings overlay should deep-merge over the base default.yaml."""
    _setup_config_files(tmp_path)
    _write(tmp_path / "settings.yaml", {
        "evaluation": {"cost": {"maker_fee_bps": 5.0}},
        "exchanges": {"binance": {"proxy": "http://127.0.0.1:7890"}},
    })
    _state.reload_config()
    # Overridden by overlay
    assert _state.config.get("evaluation.cost.maker_fee_bps") == 5.0
    assert _state.config.get("exchanges.binance.proxy") == "http://127.0.0.1:7890"
    # Base value not overridden survives
    assert _state.config.get("evaluation.sample_start") == "2021-01-01"
    assert _state.config.get("data.cache.enabled") is True


def test_config_replace_keeps_object_identity(tmp_path):
    """reload_config must mutate the same Config object (static imports stay valid)."""
    _setup_config_files(tmp_path)
    _state.reload_config()
    original = _state.config
    assert _state.config.get("evaluation.cost.maker_fee_bps") == 2.0

    _write(tmp_path / "settings.yaml", {"evaluation": {"cost": {"maker_fee_bps": 7.0}}})
    _state.reload_config()
    # Same object reference, new value visible through it.
    assert _state.config is original
    assert _state.config.get("evaluation.cost.maker_fee_bps") == 7.0


def test_provider_label():
    """Labels use provider metadata (exchange + market + data_type)."""
    # Register actual providers so provider_label() can read their attrs.
    _state.providers.clear()
    from superplatform.data.providers import setup_providers
    setup_providers(_state.providers)

    assert _state.provider_label("binance-perp-kline") == "Binance 永续 · K线"
    assert _state.provider_label("binance-spot-kline") == "Binance 现货 · K线"
    assert _state.provider_label("binance-perp-funding-rate") == "Binance 永续 · 资金费率"
    assert _state.provider_label("binance-basis") == "Binance · 基差"
    assert _state.provider_label("synthetic-kline") == "合成数据 永续 · K线"


def test_resolve_provider_for_data_type_exact():
    """Exact (exchange, market, data_type) match wins."""
    _state.providers.register(SyntheticKLineProvider(seed=42))
    class _FundingProvider(SyntheticKLineProvider):
        provider_id = "bin-perp-funding"
        data_type = "funding_rate"
        exchange = "binance"
    _state.providers.register(_FundingProvider(seed=1))

    resolved = _state.resolve_provider_for_data_type("binance", "perpetual", "funding_rate")
    assert resolved == "bin-perp-funding"


def test_resolve_provider_for_data_type_fallback_exchange():
    """Same exchange, any market is better than any exchange."""
    _state.providers.register(SyntheticKLineProvider(seed=42))
    class _SpotFunding(SyntheticKLineProvider):
        provider_id = "bin-spot-funding"
        data_type = "funding_rate"
        exchange = "binance"
        market_type = type(SyntheticKLineProvider.market_type)("spot")  # MarketType copy
    _state.providers.register(_SpotFunding(seed=2))

    # Request binance+perpetual funding → no exact match, but binance+spot exists
    resolved = _state.resolve_provider_for_data_type("binance", "perpetual", "funding_rate")
    assert resolved == "bin-spot-funding"


def test_resolve_provider_for_data_type_any():
    """Last-resort fallback: any provider for the data type."""
    _state.providers.register(SyntheticKLineProvider(seed=42))
    class _AnyFunding(SyntheticKLineProvider):
        provider_id = "any-funding"
        data_type = "funding_rate"
        exchange = "other-ex"
    _state.providers.register(_AnyFunding(seed=3))

    resolved = _state.resolve_provider_for_data_type("binance", "perpetual", "funding_rate")
    assert resolved == "any-funding"


def test_resolve_provider_for_data_type_raises():
    """No provider for data_type raises."""
    _state.providers.register(SyntheticKLineProvider(seed=42))
    with pytest.raises(ValueError):
        _state.resolve_provider_for_data_type("binance", "perpetual", "order_book")


def test_reapply_providers_in_place(tmp_path, monkeypatch):
    """reapply_providers must repopulate the SAME registry object."""
    _setup_config_files(tmp_path)
    _state.reload_config()
    original_registry = _state.providers
    original_registry.register(SyntheticKLineProvider(seed=42))
    assert len(original_registry.list_all()) == 1

    _state.reapply_providers()
    # Same object, repopulated (synthetic + possibly binance if network allows).
    assert _state.providers is original_registry
    assert "synthetic-kline" in original_registry.list_all()
    # Cache store created because data.cache.enabled is true.
    assert _state.store is not None
    _state.store = None


def test_batch_evaluate_and_correlation():
    """batch_evaluate runs multiple factors and returns a correlation matrix."""
    from superplatform.factors.registry import FactorRegistry
    from superplatform.runtime.config import Config
    from superplatform_web.research import batch_evaluate

    FactorRegistry.get_instance().auto_discover()
    reg = DataProviderRegistry()
    reg.register(SyntheticKLineProvider(seed=42))

    config = Config({
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "factors": {
            "momentum": {"symbols": ["S1"], "params": {"lookback_days": 20}},
            "short_term_reversal": {"symbols": ["S1"], "params": {"lookback_days": 5}},
            "rsi": {"symbols": ["S1"], "params": {"lookback_days": 14}},
        },
        "evaluation": {"forward_bias": {"n_cutoffs": 5}},
    })

    result = asyncio.run(batch_evaluate(
        base_config=config,
        providers=reg,
        factor_names=["momentum", "short_term_reversal", "rsi"],
        symbols=["S1"],
        start="2024-01-01",
        end="2025-06-30",
    ))

    assert len(result["results"]) == 3
    assert set(result["results"][0].keys()) >= {"factor_name", "ic_stats", "forward_bias", "cost"}
    assert result["correlation"] is not None
    assert set(result["correlation"]["labels"]) == {"momentum", "short_term_reversal", "rsi"}
    assert len(result["correlation"]["matrix"]) == 3


def test_pipeline_exposes_forward_bias_reports():
    """OfflineRuntime should collect per-group forward-bias reports."""
    import asyncio

    from superplatform.data.provider_registry import DataProviderRegistry
    from superplatform.data.providers.synthetic import SyntheticKLineProvider
    from superplatform.factors.registry import FactorRegistry
    from superplatform.runtime.config import Config
    from superplatform.runtime.pipeline import OfflineRuntime

    reg = DataProviderRegistry()
    reg.register(SyntheticKLineProvider(seed=42))
    config = Config({
        "factors": {
            "momentum": {
                "symbols": ["S1"],
                "providers": {"kline": "synthetic-kline"},
                "params": {"lookback_days": 20},
            },
        },
        "evaluation": {"forward_bias": {"n_cutoffs": 5}},
    })
    FactorRegistry.get_instance().auto_discover()

    runtime = OfflineRuntime(config, reg)
    results = asyncio.run(runtime.run(["momentum"], skip_report=True))
    assert len(results[0].forward_bias_reports) >= 1
    report = results[0].forward_bias_reports[0]
    assert report.passed is True
    assert report.n_cutoffs == 5
    assert len(report.details) == 5
