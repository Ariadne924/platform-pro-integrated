"""Tests for the dynamic config schema, introspection and per-step evaluation APIs."""

import pytest
import yaml

import superplatform_web.state as _state
from superplatform_web import factor_config as fc
from superplatform_web.app import app
from superplatform_web.config_schema import flatten_values


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point config + factors.yaml + experiments DB at temp files so tests never touch real ones."""
    monkeypatch.setattr(_state, "_CONFIG_FILES", (
        str(tmp_path / "default.yaml"),
        str(tmp_path / "exchanges.yaml"),
        str(tmp_path / "factors.yaml"),
        str(tmp_path / "settings.yaml"),
    ))
    monkeypatch.setattr(fc, "_FACTORS_PATH", tmp_path / "factors.yaml")
    monkeypatch.setattr("superplatform_web.app._EXPERIMENTS_PATH", tmp_path / "experiments.duckdb")

    (tmp_path / "default.yaml").write_text(yaml.safe_dump({
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "evaluation": {
            "sample_start": "2024-01-01",
            "sample_end": "2025-06-30",
            "oos_start": "2025-07-01",
            "oos_end": "2025-12-31",
            "layers": 5,
            "cost": {"maker_fee_bps": 2.0},
            "forward_bias": {"n_cutoffs": 3},
        },
        "data": {"symbols": {"perpetual": ["BTCUSDT"]}},
        "exchanges": {"binance": {"enabled": True, "proxy": ""}},
    }), encoding="utf-8")
    (tmp_path / "exchanges.yaml").write_text("", encoding="utf-8")
    (tmp_path / "factors.yaml").write_text(yaml.safe_dump({
        "factor_instances": {
            "momentum_20d": {"factory": "momentum", "params": {"lookback_days": 20}},
        },
    }), encoding="utf-8")

    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()
    _state.reload_config()
    yield
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()


@pytest.fixture
def client(_isolated_state):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def test_build_schema_types(client):
    body = client.get("/api/config/schema").json()["schema"]
    fields = {f["key"]: f for f in body["fields"]}

    assert fields["defaults.exchange"]["type"] == "str"
    assert fields["defaults.exchange"]["enum"] == ["binance", "synthetic", "okx", "bybit"]
    assert fields["evaluation.layers"]["type"] == "int"
    assert fields["evaluation.layers"]["min"] == 2
    assert fields["evaluation.layers"]["max"] == 10
    assert fields["evaluation.sample_start"]["type"] == "date"
    assert fields["evaluation.cost.maker_fee_bps"]["type"] == "number"


def test_build_schema_editability(client):
    fields = {f["key"]: f for f in client.get("/api/config/schema").json()["schema"]["fields"]}

    assert fields["defaults.exchange"]["editable"] is True
    assert fields["evaluation.layers"]["editable"] is True
    # Governance-locked OOS window must not be editable from the web.
    assert fields["evaluation.oos_start"]["editable"] is False


def test_flatten_values_nested():
    flat = flatten_values({"a": {"b": {"c": 1}}, "d": [1, 2]})
    assert flat == {"a.b.c": 1, "d": [1, 2]}


def test_config_values_roundtrip(client):
    r = client.put("/api/config/values", json={"evaluation.layers": 7})
    assert r.status_code == 200, r.text
    assert r.json()["values"]["evaluation.layers"] == 7
    assert _state.config.get("evaluation.layers") == 7

    # Invalid: unknown key → 422
    assert client.put("/api/config/values", json={"nope.nope": 1}).status_code == 422
    # Invalid: locked key → 422
    assert client.put("/api/config/values", json={"evaluation.oos_start": "2026-01-01"}).status_code == 422
    # Invalid: type mismatch → 422
    assert client.put("/api/config/values", json={"evaluation.layers": "five"}).status_code == 422

    # Reset restores base defaults.
    assert client.delete("/api/config/values").status_code == 200
    assert _state.config.get("evaluation.layers") == 5


def test_introspect_data_types(client):
    body = client.get("/api/introspect/data-types").json()
    dtypes = {d["data_type"] for d in body["data_types"]}
    assert {"kline", "funding_rate", "open_interest"} <= dtypes
    kline = next(d for d in body["data_types"] if d["data_type"] == "kline")
    cols = {c["name"] for c in kline["columns"]}
    assert {"timestamp", "open", "high", "low", "close", "volume"} <= cols


def test_introspect_frequencies_and_exchanges(client):
    freqs = {f["value"] for f in client.get("/api/introspect/frequencies").json()["frequencies"]}
    assert {"1m", "1h", "1d"} <= freqs

    exchanges = client.get("/api/introspect/exchanges").json()["exchanges"]
    assert any(e["name"] == "binance" and e["enabled"] for e in exchanges)


def test_introspect_evaluation_manifest(client):
    body = client.get("/api/introspect/evaluation").json()
    names = {e["name"] for e in body["evaluation"]}
    assert {"compute_ic", "layer_test", "compute_ic_decay", "ForwardBiasChecker"} <= names
    # Every entry carries a doc snippet + param list.
    for entry in body["evaluation"]:
        assert "name" in entry and "kind" in entry


def test_evaluate_manifest(client):
    steps = {s["key"] for s in client.get("/api/evaluate/manifest").json()["steps"]}
    assert {"ic", "layers", "decay", "forward-bias", "cost", "correlation"} == steps


def test_evaluate_ic_step_runs(client):
    """Runs a real single-factor IC step against the synthetic provider."""
    from superplatform.factors.registry import FactorRegistry
    FactorRegistry.get_instance().auto_discover()

    r = client.post("/api/evaluate/ic", json={
        "factor": "momentum",
        "symbols": ["BTCUSDT"],
        "start": "2024-01-01",
        "end": "2025-06-30",
        "params": {"lookback_days": 20},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["factor_name"] == "momentum"
    assert "ic_stats" in body and "rank_ic_stats" in body


def test_evaluate_params_change_run_identity_and_result_cache(client):
    """Distinct UI params must not reuse a stale result-cache entry."""
    from superplatform.factors.registry import FactorRegistry
    FactorRegistry.get_instance().auto_discover()

    base = {
        "factor": "momentum",
        "symbols": ["BTCUSDT"],
        "start": "2024-01-01",
        "end": "2024-04-30",
    }
    fast = client.post("/api/evaluate/ic", json={
        **base,
        "params": {"lookback_days": 5},
    })
    slow = client.post("/api/evaluate/ic", json={
        **base,
        "params": {"lookback_days": 20},
    })
    fast_again = client.post("/api/evaluate/ic", json={
        **base,
        "params": {"lookback_days": 5},
    })

    assert fast.status_code == slow.status_code == fast_again.status_code == 200
    fast_body = fast.json()
    slow_body = slow.json()
    assert fast_body["effective_params"] == {"lookback_days": 5}
    assert slow_body["effective_params"] == {"lookback_days": 20}
    assert fast_body["params_hash"] != slow_body["params_hash"]
    assert fast_body["run_id"] != slow_body["run_id"]
    assert fast_body["cache_hit"] is False
    assert slow_body["cache_hit"] is False
    assert fast_again.json()["cache_hit"] is True


def test_evaluate_default_params_keep_existing_behavior(client):
    """Omitted params resolve to the factor default and share that cache entry."""
    from superplatform.factors.registry import FactorRegistry
    FactorRegistry.get_instance().auto_discover()

    base = {
        "factor": "momentum",
        "symbols": ["BTCUSDT"],
        "start": "2024-01-01",
        "end": "2024-04-30",
    }
    default_run = client.post("/api/evaluate/ic", json=base)
    explicit_default = client.post("/api/evaluate/ic", json={
        **base,
        "params": {"lookback_days": 20},
    })

    assert default_run.status_code == explicit_default.status_code == 200
    default_body = default_run.json()
    explicit_body = explicit_default.json()
    assert default_body["effective_params"] == {"lookback_days": 20}
    assert explicit_body["effective_params"] == {"lookback_days": 20}
    assert default_body["run_id"] == explicit_body["run_id"]
    assert explicit_body["cache_hit"] is True


@pytest.mark.parametrize("params", [
    {"lookbackDays": 5},
    {"lookback_days": "5"},
    {"lookback_days": 0},
])
def test_evaluate_rejects_invalid_or_misnamed_params(client, params):
    from superplatform.factors.registry import FactorRegistry
    FactorRegistry.get_instance().auto_discover()

    r = client.post("/api/evaluate/ic", json={
        "factor": "momentum",
        "symbols": ["BTCUSDT"],
        "start": "2024-01-01",
        "end": "2024-04-30",
        "params": params,
    })
    assert r.status_code == 422


def test_factor_config_crud(client):
    from superplatform.factors.registry import FactorRegistry
    FactorRegistry.get_instance().auto_discover()

    # /api/factors exposes the factor's declared params_schema.
    info = {f["name"]: f for f in client.get("/api/factors").json()}
    assert info["momentum"]["params_schema"]["lookback_days"]["type"] == "int"
    assert info["momentum"]["params_schema"]["lookback_days"]["default"] == 20
    for factor_info in info.values():
        for param_name, spec in factor_info["params_schema"].items():
            assert spec["name"] == param_name
            assert spec["required"] is False
            assert {
                "type", "default", "min", "max", "step", "adjustment_unit",
                "ui_precision", "description", "example",
            } <= set(spec)
            if spec["type"] in {"int", "float", "number"}:
                assert spec["adjustment_unit"] == spec["step"]

    # GET schema for an existing factor — object nodes use `children`,
    # params leaves are typed and editable.
    body = client.get("/api/factors/momentum/config").json()
    assert body["name"] == "momentum"
    schema = {f["key"]: f for f in body["schema"]}
    assert schema["params"]["children"], "params node should have schema-driven children"
    period = next(f for f in schema["params"]["children"] if f["key"] == "params.lookback_days")
    assert period["type"] == "int"
    assert period["default"] == 20
    assert period["editable"] is True
    assert period["min"] == 1 and period["max"] == 500
    assert period["adjustment_unit"] == 1
    assert period["physical_unit"] == "day"
    assert "children" in schema["providers"]  # vocabulary aligned to config schema

    # PUT writes back to factors.yaml (temp file) and reloads config.
    r = client.put("/api/factors/momentum/config", json={
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "providers": {"kline": "synthetic-kline"},
        "params": {"lookback_days": 30},
    })
    assert r.status_code == 200, r.text
    assert _state.config.get("factors.momentum.params.lookback_days") == 30


def test_factor_config_rejects_unaligned_adjustment_value(client):
    """Config persistence uses the same Decimal-backed alignment validation."""
    from superplatform.factors.registry import FactorRegistry
    FactorRegistry.get_instance().auto_discover()

    response = client.put("/api/factors/crypto_tail_risk/config", json={
        "params": {"quantile": 0.955},
    })

    assert response.status_code == 422
    assert "adjustment_unit=0.01" in response.json()["detail"]
    assert "建议使用 0.95 或 0.96" in response.json()["detail"]

    # Unknown factor → 404.
    assert client.put("/api/factors/ghost/config", json={"symbols": []}).status_code == 404

    # Param validation: wrong type → 422, below declared min → 422.
    assert client.put("/api/factors/momentum/config", json={"params": {"lookback_days": "abc"}}).status_code == 422
    assert client.put("/api/factors/momentum/config", json={"params": {"lookback_days": 0}}).status_code == 422


def test_provider_toggle(client):
    # synthetic-kline is always registered; disable then verify resolution skips it.
    r = client.put("/api/data/providers/synthetic-kline", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    listed = {p["provider_id"]: p for p in client.get("/api/data/providers").json()}
    assert listed["synthetic-kline"]["enabled"] is False

    # Re-enable.
    r = client.put("/api/data/providers/synthetic-kline", json={"enabled": True})
    assert r.json()["enabled"] is True
    assert client.put("/api/data/providers/ghost", json={"enabled": False}).status_code == 404
    assert client.put("/api/data/providers/synthetic-kline", json={"enabled": "yes"}).status_code == 422


def test_strategy_config_crud(client):
    # Strategies may only reference instances, not factory factors.
    r = client.put("/api/strategies/momentum_demo/config", json={"used_factors": ["momentum_20d"]})
    assert r.status_code == 200, r.text
    assert _state.config.get("strategies.momentum_demo.used_factors") == ["momentum_20d"]

    # Referencing a factory factor → 422 (strategies must use instances).
    r = client.put("/api/strategies/momentum_demo/config", json={"used_factors": ["momentum"]})
    assert r.status_code == 422

    # Unknown factor in used_factors → 422.
    r = client.put("/api/strategies/momentum_demo/config", json={"used_factors": ["nope"]})
    assert r.status_code == 422


def test_refresh_factors_reloads_registry(client):
    """POST /factors/refresh re-discovers defs and reports the diff."""
    r = client.post("/api/factors/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"imported_modules", "before", "after", "new_factors", "removed_factors"}
    assert isinstance(body["imported_modules"], int)
    assert body["after"] >= body["before"] - len(body["removed_factors"])
    # Momentum factors must be present after a reload.
    from superplatform.factors.registry import FactorRegistry
    assert "momentum" in FactorRegistry.get_instance().list_all()


def test_factor_list_marks_instances_and_factories(client):
    body = client.get("/api/factors").json()
    by_name = {f["name"]: f for f in body}
    assert by_name["momentum"]["kind"] == "factory"
    assert by_name["momentum_20d"]["kind"] == "instance"
    assert by_name["momentum_20d"]["factory"] == "momentum"
    assert by_name["momentum_20d"]["instance_params"] == {"lookback_days": 20}


def test_factor_instance_create_and_delete(client):
    r = client.post("/api/factors/instances", json={
        "name": "momentum_90d",
        "factory": "momentum",
        "params": {"lookback_days": 90},
        "description": "90日动量",
    })
    assert r.status_code == 200, r.text
    assert r.json()["config"]["params"] == {"lookback_days": 90}

    names = {f["name"] for f in client.get("/api/factors").json()}
    assert "momentum_90d" in names

    # Instance name colliding with a factory → 422.
    r = client.post("/api/factors/instances", json={
        "name": "momentum", "factory": "momentum", "params": {"lookback_days": 20},
    })
    assert r.status_code == 422

    # Unknown factory → 422.
    r = client.post("/api/factors/instances", json={
        "name": "x", "factory": "nope", "params": {},
    })
    assert r.status_code == 422

    # Delete works and removes it from the list.
    r = client.delete("/api/factors/instances/momentum_90d")
    assert r.status_code == 200, r.text
    names = {f["name"] for f in client.get("/api/factors").json()}
    assert "momentum_90d" not in names
    assert client.delete("/api/factors/instances/momentum_90d").status_code == 404


def test_sweep_rejects_unknown_param(client):
    r = client.post("/api/factors/sweep", json={
        "factory": "momentum",
        "symbols": ["BTCUSDT"],
        "start": "2024-01-01",
        "end": "2024-03-31",
        "sweep": [{"param": "nope", "from": 1, "to": 10, "step": 1}],
    })
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]

    # Unknown factory → 422.
    r = client.post("/api/factors/sweep", json={
        "factory": "nope",
        "symbols": ["BTCUSDT"],
        "start": "2024-01-01",
        "end": "2024-03-31",
        "sweep": [{"param": "lookback_days", "from": 10, "to": 20, "step": 5}],
    })
    assert r.status_code == 422
