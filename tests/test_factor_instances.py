"""Tests for the factor instance layer (two-layer split).

The factory layer (`FactorRegistry`) holds configurable factors; the instance
layer (`FactorInstanceRegistry`) holds fixed-parameter instances that strategies
reference. An instance's ``compute`` injects its preset so live's no-arg call
evaluates with the preset.
"""

import pandas as pd
import pytest

from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.instances import FactorInstance, instance_metadata, instances_from_config
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import (
    resolve_factor,
    validate_used_factors_are_instances,
)
from superplatform.runtime.config import Config


def _config(*, factor_instances: dict) -> Config:
    return Config({
        "factors": {
            "momentum": {"symbols": ["S1"], "params": {"lookback_days": 20}},
        },
        "factor_instances": factor_instances,
    })


def _kline(n: int = 200) -> dict:
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC").as_unit("ns"),
        "close": range(n),
    })
    return {"kline": {"S1": frame}}


@pytest.fixture(autouse=True)
def _registry():
    fr = FactorRegistry.get_instance()
    fr.auto_discover()
    FactorInstanceRegistry.get_instance().clear()
    yield fr
    FactorInstanceRegistry.get_instance().clear()


def test_instances_from_config_builds_fixed_param_factors():
    fr = FactorRegistry.get_instance()
    built = instances_from_config(_config(factor_instances={
        "momentum_60d": {"factory": "momentum", "params": {"lookback_days": 60}},
    }), fr)
    inst = built["momentum_60d"]
    assert isinstance(inst, FactorInstance)
    assert inst.factory_name == "momentum"
    assert inst.params == {"lookback_days": 60}
    assert inst.category == inst._factory.category
    assert inst.required_data == ["kline"]


def test_instance_compute_injects_preset_without_args():
    # live semantics: compute(data) with NO params evaluates with the preset
    fr = FactorRegistry.get_instance()
    built = instances_from_config(_config(factor_instances={
        "momentum_60d": {"factory": "momentum", "params": {"lookback_days": 60}},
    }), fr)
    inst = built["momentum_60d"]
    values = inst.compute(_kline()).values
    assert values["value"].notna().sum() == 140  # 60-day warm-up


def test_instance_compute_allows_override():
    fr = FactorRegistry.get_instance()
    built = instances_from_config(_config(factor_instances={
        "momentum_60d": {"factory": "momentum", "params": {"lookback_days": 60}},
    }), fr)
    inst = built["momentum_60d"]
    values = inst.compute(_kline(), lookback_days=20).values
    assert values["value"].notna().sum() == 180  # 20-day warm-up


def test_instance_metadata_derived_from_factory():
    fr = FactorRegistry.get_instance()
    built = instances_from_config(_config(factor_instances={
        "momentum_60d": {"factory": "momentum", "params": {"lookback_days": 60}},
    }), fr)
    md = instance_metadata(built["momentum_60d"])
    assert md is not None
    assert md.default_params == {"lookback_days": 60}
    assert md.required_fields == {"kline": ("close",)}
    # Non-instances derive nothing (so FACTOR_METADATA stays authoritative).
    assert instance_metadata(fr.get("momentum")) is None


def test_instances_from_config_rejects_unknown_params():
    fr = FactorRegistry.get_instance()
    with pytest.raises(ValueError, match="未知参数"):
        instances_from_config(_config(factor_instances={
            "bad": {"factory": "momentum", "params": {"nope": 1}},
        }), fr)


def test_instances_from_config_rejects_missing_factory():
    fr = FactorRegistry.get_instance()
    with pytest.raises(ValueError, match="缺少 `factory`"):
        instances_from_config(_config(factor_instances={
            "bad": {"params": {"lookback_days": 20}},
        }), fr)


def test_instances_from_config_rejects_unknown_factory():
    fr = FactorRegistry.get_instance()
    with pytest.raises(ValueError, match="未注册"):
        instances_from_config(_config(factor_instances={
            "bad": {"factory": "does_not_exist", "params": {}},
        }), fr)


def test_registry_build_and_resolve():
    fr = FactorRegistry.get_instance()
    reg = FactorInstanceRegistry.get_instance()
    config = _config(factor_instances={
        "momentum_60d": {"factory": "momentum", "params": {"lookback_days": 60}},
    })
    names = reg.build_from_config(config, fr)
    assert names == ["momentum_60d"]
    assert reg.has("momentum_60d")
    # resolve_factor prefers the instance layer.
    assert isinstance(resolve_factor("momentum_60d"), FactorInstance)
    assert isinstance(resolve_factor("momentum"), type(fr.get("momentum")))


def test_strategy_validation_rejects_factory_factors():
    fr = FactorRegistry.get_instance()
    FactorInstanceRegistry.get_instance().build_from_config(_config(factor_instances={
        "momentum_60d": {"factory": "momentum", "params": {"lookback_days": 60}},
    }), fr)
    # Instance-only passes.
    validate_used_factors_are_instances(["momentum_60d"])
    # Referencing the factory factor raises.
    with pytest.raises(ValueError, match="策略只能引用实例"):
        validate_used_factors_are_instances(["momentum"])


# ── Factory parameter sweep (shared-fetch optimization) ───────────────


def _sweep_config() -> Config:
    return Config({
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "factors": {
            "momentum": {
                "symbols": ["S1", "S2", "S3"],
                "providers": {"kline": "synthetic-kline"},
                "params": {"lookback_days": 20},
            },
        },
        "factor_instances": {
            "momentum_20d": {"factory": "momentum", "params": {"lookback_days": 20}},
        },
        "evaluation": {
            "sample_start": "2021-01-01",
            "sample_end": "2021-12-31",
            "ic": {"min_stocks_per_period": 2},
        },
    })


def test_run_factory_sweep_shared_fetch_returns_metrics():
    import asyncio

    from superplatform.data.provider_registry import DataProviderRegistry
    from superplatform.data.providers.synthetic import SyntheticKLineProvider
    from superplatform_web.research import run_factory_sweep

    fr = FactorRegistry.get_instance()
    fr.auto_discover()
    reg = DataProviderRegistry()
    reg.register(SyntheticKLineProvider(seed=42))
    config = _sweep_config()
    FactorInstanceRegistry.get_instance().build_from_config(config, fr)

    combos = [{"lookback_days": n} for n in (10, 20, 30)]
    payload = asyncio.run(run_factory_sweep(
        base_config=config, providers=reg, factory_name="momentum",
        symbols=["S1", "S2", "S3"], start="2021-01-01", end="2021-12-31",
        combos=combos,
    ))

    assert payload["combos"] == 3
    assert len(payload["results"]) == 3
    assert {r["params"]["lookback_days"] for r in payload["results"]} == {10, 20, 30}
    for r in payload["results"]:
        assert "icir" in r["metrics"] and "mean_ic" in r["metrics"]
    assert payload["elapsed_ms"] > 0
    assert payload["ms_per_combo"] > 0
    # Temp instances are cleaned up after the run.
    assert not any(
        n.startswith("__sweep__")
        for n in FactorInstanceRegistry.get_instance().list_all()
    )
    # Shared fetch: different combos of the same factory do not collide.
    assert payload["param_names"] == ["lookback_days"]
