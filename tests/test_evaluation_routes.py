"""Tests for the web deliverable endpoints: /api/evaluation/panel-export and /run.

``_STAGE3_CONFIG`` is monkeypatched to a temp config whose ``output.root`` keeps
every pipeline artifact inside tmp_path, so the real ``outputs/`` directory is
never written.
"""

import io
import zipfile

import pytest
import yaml

import superplatform_web.routes.evaluation as evaluation_module
import superplatform_web.state as _state
from superplatform_web import factor_config as fc
from superplatform_web.app import app


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
    (tmp_path / "factors.yaml").write_text("", encoding="utf-8")

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
def stage3_config(tmp_path, monkeypatch):
    """A minimal deliverable config pointed at tmp_path outputs; also points the route at it."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "run_date": "auto",
        "runtime": {"random_seed": 42},
        "experiment": {
            "experiment_id": "web-test",
            "in_sample": {"start": None, "end": None},
            "out_of_sample": {"start": None, "end": None},
            "fail_fast_on_hash_change": False,
        },
        "input": {
            "panel_path": "data/evaluation_panel.csv",
            "generate_demo_if_missing": False,
            "require_temporal_metadata": True,
        },
        "output": {"root": str(tmp_path / "outputs")},
        "evaluation": {
            "factor_col": "factor_value",
            "return_col": "ret_1",
            "bar_interval": "1d",
            "layers": 2,
            "min_assets_per_layer": 1,
            "min_assets": 2,
            "sample_start": None,
            "sample_end": None,
        },
        "universe": {"require_eligibility": True, "eligibility_column": "is_eligible"},
        "market": {
            "exchange": "binance",
            "exchange_column": "exchange",
            "market_type": "perpetual",
            "market_column": "market_type",
            "settlement_asset": "USDT",
            "settlement_asset_column": "settlement_asset",
            "allow_short": True,
            "require_funding_included": True,
            "funding_included_column": "funding_included",
        },
        "preprocessing": {
            "winsorize_enabled": True,
            "zscore_enabled": False,
            "winsorize_limits": [0.01, 0.99],
        },
        "stability": {"window_days": 60, "min_periods": 20},
        "correlation": {"min_assets": 2},
        "cost": {"fee_bps": 0.0, "slippage_bps": 0.0},
        "regression_guard": {
            "baseline_path": "regression_baseline.json",
            "thresholds": {
                "mean_ic": 0.0,
                "rankic_mean": 0.0,
                "ic_ir": 0.0,
                "long_short_ann_return": 0.0,
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(evaluation_module, "_STAGE3_CONFIG", config_path)
    return config_path


@pytest.fixture
def client(_isolated_state):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
BODY = {
    "factors": ["momentum"],
    "symbols": SYMBOLS,
    "start": "2024-01-01",
    "end": "2024-06-30",
}


def _discover_factors():
    from superplatform.factors.registry import FactorRegistry

    FactorRegistry.get_instance().auto_discover()


def test_panel_export_returns_csv(client):
    _discover_factors()
    r = client.post("/api/evaluation/panel-export", json=BODY)
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]

    import csv as csv_module

    rows = list(csv_module.DictReader(io.StringIO(r.text)))
    assert rows, "CSV body should have data rows"
    assert {"timestamp", "symbol", "factor_name", "factor_value", "ret_1"} <= set(rows[0])
    # At least one row per symbol across the sample.
    symbols = {row["symbol"] for row in rows}
    assert {"BTCUSDT", "ETHUSDT"} <= symbols


def test_panel_export_unknown_factor_422(client):
    _discover_factors()
    r = client.post("/api/evaluation/panel-export", json={**BODY, "factors": ["ghost_factor"]})
    assert r.status_code == 422, r.text


def test_generate_in_app_reports_returns_overview_and_factor_reports(client):
    r = client.post("/api/evaluation/reports", json={
        **BODY,
        "results": [
            {
                "factor_name": "momentum",
                "ic_stats": {"mean_ic": 0.02, "icir": 0.5, "ic_positive_ratio": 0.6},
                "rank_ic_stats": {"mean_rank_ic": 0.01, "rank_icir": 0.3},
                "forward_bias_passed": True,
                "forward_bias": [],
            },
            {
                "factor_name": "rsi",
                "ic_stats": {"mean_ic": -0.01, "icir": -0.2, "ic_positive_ratio": 0.4},
                "rank_ic_stats": {"mean_rank_ic": -0.02, "rank_icir": -0.1},
                "forward_bias_passed": False,
                "forward_bias": [{"passed": False}],
            },
        ],
        "correlation": {
            "labels": ["momentum", "rsi"],
            "matrix": [[1.0, -0.7], [-0.7, 1.0]],
        },
    })
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "因子横向对比报告" in payload["overview"]["markdown"]
    assert len(payload["factors"]) == 2
    assert payload["factors"][0]["factor_name"] == "momentum"


def test_run_pipeline_returns_zip(client, stage3_config):
    _discover_factors()
    r = client.post("/api/evaluation/run", json=BODY)
    assert r.status_code == 200, r.text
    assert "application/zip" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        names = set(archive.namelist())
        assert "run_manifest.json" in names

        import json

        manifest = json.loads(archive.read("run_manifest.json").decode("utf-8"))
        assert manifest["status"] in {"success", "partial_failure"}
        assert manifest["run_date"]
        assert "evaluated_panel.csv" in names


def test_run_writes_only_under_stage3_output_root(client, stage3_config, tmp_path):
    """Pipeline artifacts land under the config's output.root (tmp_path), never the repo outputs/."""
    _discover_factors()
    r = client.post("/api/evaluation/run", json=BODY)
    assert r.status_code == 200, r.text
    manifests = list((tmp_path / "outputs").glob("*/*/run_manifest.json"))
    assert manifests, f"expected a run manifest under {tmp_path / 'outputs'}"


def test_normalize_rejects_mixed_frequency_panel():
    """A panel with conflicting ret_1 at one (timestamp, symbol) must be rejected up front."""
    import pandas as pd

    from superplatform_web.research import normalize_for_experiment

    cfg = {
        "market": {"exchange": "binance", "market_type": "perpetual", "settlement_asset": "USDT"},
        "evaluation": {"return_col": "ret_1", "bar_interval": "1d"},
    }
    ts = pd.to_datetime(["2024-01-01"] * 4, utc=True)
    panel = pd.DataFrame({
        "timestamp": ts,
        "symbol": ["BTCUSDT"] * 4,
        "factor_name": ["funding_4h", "funding_4h", "basis_1d", "basis_1d"],
        "factor_value": [1.0, 2.0, 3.0, 4.0],
        "frequency": ["4h", "4h", "1d", "1d"],
        # Same (timestamp, symbol) carries two different 1-bar returns.
        "ret_1": [0.01, 0.01, 0.05, 0.05],
        "is_eligible": [True] * 4,
    })
    with pytest.raises(ValueError, match="前瞻收益不一致"):
        normalize_for_experiment(panel, cfg)


def test_normalize_accepts_consistent_frequency_panel():
    """Same ret_1 across factor rows at a shared timestamp must pass."""
    import pandas as pd

    from superplatform_web.research import normalize_for_experiment

    cfg = {
        "market": {"exchange": "binance", "market_type": "perpetual", "settlement_asset": "USDT"},
        "evaluation": {"return_col": "ret_1", "bar_interval": "1d"},
    }
    ts = pd.to_datetime(["2024-01-01"] * 4, utc=True)
    panel = pd.DataFrame({
        "timestamp": ts,
        "symbol": ["BTCUSDT"] * 4,
        "factor_name": ["a", "a", "b", "b"],
        "factor_value": [1.0, 2.0, 3.0, 4.0],
        "frequency": ["1d", "1d", "1d", "1d"],
        "ret_1": [0.01, 0.01, 0.01, 0.01],
        "is_eligible": [True] * 4,
    })
    out = normalize_for_experiment(panel, cfg)
    assert out["exchange"].eq("binance").all()
    assert out["market_type"].eq("perpetual").all()


def test_run_warning_helper():
    from superplatform_web.routes.evaluation import _run_warning

    assert _run_warning({"status": "success", "failed_tasks": []}) is None
    warning = _run_warning({
        "status": "partial_failure",
        "failed_tasks": [
            {"task": "load_input", "error": "ValueError: factor rows disagree on the return"}
        ],
    })
    assert "partial_failure" in warning
    assert "load_input" in warning


def test_panel_export_pins_run_cadence_on_rows(client):
    """A run cadence is stamped on every panel row: frequency + the 1-bar horizon."""
    import pandas as pd

    _discover_factors()
    r = client.post("/api/evaluation/panel-export", json={**BODY, "frequency": "1d"})
    assert r.status_code == 200, r.text

    import csv as csv_module

    rows = list(csv_module.DictReader(io.StringIO(r.text)))
    assert rows, "CSV body should have data rows"
    assert all(row["frequency"] == "1d" for row in rows)
    for row in rows:
        delta = pd.Timestamp(row["exit_ts"]) - pd.Timestamp(row["entry_ts"])
        assert delta == pd.Timedelta("1D"), row


def test_panel_export_rejects_frequency_not_natively_available(client):
    """funding_rate_annualized is 8h-native; requesting 1d must 422 with the hint."""
    from superplatform.data.providers.binance_funding_rate import BinanceFundingRateProvider

    _discover_factors()
    if "binance-perp-funding-rate" not in _state.providers:
        _state.providers.register(BinanceFundingRateProvider())
    r = client.post("/api/evaluation/panel-export", json={
        **BODY,
        "factors": ["funding_rate_annualized"],
        "frequency": "1d",
    })
    assert r.status_code == 422, r.text
    assert "8h" in r.text


def test_normalize_recomputes_exit_ts_with_run_bar_interval():
    """normalize_for_experiment's bar_interval override re-derives exit_ts at that cadence."""
    import pandas as pd

    from superplatform_web.research import normalize_for_experiment

    cfg = {
        "market": {"exchange": "binance", "market_type": "perpetual", "settlement_asset": "USDT"},
        "evaluation": {"return_col": "ret_1", "bar_interval": "1d"},
    }
    panel = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01"], utc=True),
        "symbol": ["BTCUSDT"],
        "factor_name": ["a"],
        "factor_value": [1.0],
        "frequency": ["4h"],
        "ret_1": [0.01],
        "entry_ts": pd.to_datetime(["2024-01-01"], utc=True),
        "exit_ts": pd.to_datetime(["2024-01-01 04:00"], utc=True),
        "is_eligible": [True],
    })
    out = normalize_for_experiment(panel, cfg, bar_interval="4h")
    assert (out["exit_ts"] - out["entry_ts"] == pd.Timedelta("4h")).all()
    # Without the override the config's 1d cadence would win.
    out_default = normalize_for_experiment(panel, cfg)
    assert (out_default["exit_ts"] - out_default["entry_ts"] == pd.Timedelta("1D")).all()


def test_experiment_runner_pins_bar_interval(stage3_config):
    """ExperimentRunner(bar_interval=...) overrides the temporal contract before hashing."""
    from superplatform.evaluation.experiment import ExperimentRunner

    runner = ExperimentRunner(stage3_config, bar_interval="4h")
    assert runner._config["evaluation"]["bar_interval"] == "4h"

    default_runner = ExperimentRunner(stage3_config)
    assert default_runner._config["evaluation"]["bar_interval"] == "1d"
