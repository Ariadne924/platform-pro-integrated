from __future__ import annotations

import asyncio
import importlib
import time

import numpy as np
import pandas as pd
import pytest

import superplatform_web.ml_jobs as ml_jobs
import superplatform_web.routes.ml_v1 as ml_v1

web_app_module = importlib.import_module("superplatform_web.app")


def _panel(periods: int = 100) -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    symbols = ("BTC", "ETH", "SOL")
    rows: list[dict] = []
    close = {symbol: 100.0 for symbol in symbols}
    for index, timestamp in enumerate(timestamps):
        for symbol_index, symbol in enumerate(symbols):
            signal = np.sin(index / 9.0 + symbol_index)
            forward_return = 0.004 * signal
            close[symbol] *= 1.0 + forward_return
            for factor, value in {
                "momentum": signal,
                "liquidity": symbol_index / 3 + signal * 0.1,
            }.items():
                rows.append(
                    {
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "factor_name": factor,
                        "factor_value": value,
                        "ret_1": forward_return,
                        "ret_5": forward_return * 5,
                        "ret_10": forward_return * 10,
                        "ret_20": forward_return * 20,
                        "close": close[symbol],
                    }
                )
    return pd.DataFrame(rows)


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch, tmp_path):
    ml_jobs._ml_jobs.clear()
    monkeypatch.setattr(
        web_app_module,
        "_EXPERIMENTS_PATH",
        tmp_path / "research_experiments.duckdb",
    )

    async def fake_build_batch_panel(**kwargs):
        await asyncio.sleep(0)
        panel = _panel()
        return panel[panel["symbol"].isin(kwargs["symbols"])]

    monkeypatch.setattr(ml_v1, "build_batch_panel", fake_build_batch_panel)
    yield
    ml_jobs._ml_jobs.clear()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from superplatform_web.app import app

    with TestClient(app) as test_client:
        yield test_client


def _request() -> dict:
    return {
        "factors": ["momentum", "liquidity"],
        "symbols": ["BTC", "ETH", "SOL"],
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-04-30T00:00:00Z",
        "exchange": "synthetic",
        "market_type": "perpetual",
        "frequency": "1d",
        "target_horizon": 1,
        "top_n": 2,
        "walk_forward": {
            "min_train_periods": 30,
            "test_periods": 15,
            "embargo_periods": 1,
            "max_features": 2,
        },
        "regime": {
            "fast_window": 5,
            "slow_window": 20,
            "volatility_window": 5,
            "confirmation_periods": 2,
        },
    }


def _poll(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    latest = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/ml/jobs/{job_id}")
        assert response.status_code == 200
        latest = response.json()
        if latest["status"] in {"done", "error", "cancelled"}:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"ML job did not finish: {latest}")


def test_ml_capabilities_expose_risk_first_contract(client) -> None:
    response = client.get("/api/v1/ml/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["regimes"] == ["bull", "bear", "sideways"]
    assert payload["score_weights"]["downside_risk"] == 45
    assert payload["score_weights"]["upside_bonus"] == 5
    assert payload["comparison_protocol"] == "shared-window-risk-first-v1"
    assert payload["candidate_groups"]["trained_models"] == [
        "ridge",
        "elastic_net",
        "tree_stumps",
    ]
    details = {row["name"]: row for row in payload["model_details"]}
    assert {"lightgbm", "xgboost"}.issubset(details)
    assert payload["portfolio"]["causal_covariance"] is True
    assert payload["multi_frequency"]["future_timestamp_audit"] is True
    assert "paired_block_bootstrap" in payload["comparison_metrics"]
    assert payload["research_only"] is True


def test_ml_lists_scoreable_registered_strategies(client) -> None:
    response = client.get("/api/v1/ml/strategies")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(payload["strategies"])
    assert all(row["name"] for row in payload["strategies"])


def test_ml_coverage_reports_local_cache_without_fetching(client, monkeypatch) -> None:
    class FakeStore:
        def series_range(self, table, symbol, frequency):
            assert table == "pv_synthetic_perp_kline"
            assert symbol == "BTC"
            assert frequency == "1d"
            return {
                "min_ts": pd.Timestamp("2024-01-01", tz="UTC"),
                "max_ts": pd.Timestamp("2024-04-29", tz="UTC"),
                "count": 120,
                "bar_width": pd.Timedelta(days=1),
            }

        def count_series_range(self, table, symbol, frequency, start, end):
            del table, symbol, frequency, start, end
            return 120

    with monkeypatch.context() as patch:
        patch.setattr(ml_v1.web_state, "store", FakeStore())
        patch.setattr(
            ml_v1.web_state,
            "resolve_provider_for_data_type",
            lambda exchange, market, data_type: f"{exchange}-{market}-{data_type}",
        )
        response = client.post(
            "/api/v1/ml/coverage",
            json={
                "symbols": ["BTC"],
                "frequencies": ["1d"],
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-04-30T00:00:00Z",
                "exchange": "synthetic",
                "market_type": "perp",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["rows"][0]["coverage_ratio"] == 1.0
    assert payload["missing"] == []


def test_ml_job_runs_training_backtest_and_score(client) -> None:
    submitted = client.post("/api/v1/ml/jobs", json=_request())
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    result = _poll(client, job_id)
    assert result["status"] == "done", result.get("error")
    assert result["result"]["strategy"]["equity"]
    assert result["result"]["equal_weight_benchmark"]["equity"]
    assert result["result"]["score"]["weights"]["upside_bonus"] == 5
    assert len(result["result"]["strategy_comparison"]["leaderboard"]) == 5
    assert result["experiment_id"]
    stored = client.get(f"/api/v1/ml/experiments/{result['experiment_id']}")
    assert stored.status_code == 200
    assert stored.json()["result"]["score"]["score"] <= 100


def test_identical_ml_job_reuses_cached_result(client) -> None:
    first = client.post("/api/v1/ml/jobs", json=_request()).json()
    completed = _poll(client, first["job_id"])
    assert completed["status"] == "done"
    second = client.post("/api/v1/ml/jobs", json=_request())
    assert second.status_code == 202
    assert second.json()["job_id"] == first["job_id"]
    assert second.json()["reused"] is True


def test_single_asset_job_recommends_core_and_companion_factor_weights(client) -> None:
    request = _request()
    request.update(
        {
            "symbols": ["BTC"],
            "research_mode": "single_asset",
            "core_factor": "momentum",
            "top_n": 1,
        }
    )
    submitted = client.post("/api/v1/ml/jobs", json=request)
    assert submitted.status_code == 202, submitted.text
    completed = _poll(client, submitted.json()["job_id"])
    assert completed["status"] == "done", completed.get("error")
    result = completed["result"]
    assert result["config"]["research_mode"] == "single_asset"
    assert any(row["role"] == "core" for row in result["feature_recommendations"])
    assert sum(abs(row["recommended_weight"]) for row in result["feature_recommendations"]) == pytest.approx(1.0)


def test_ml_job_can_score_registered_strategy_signals(client, monkeypatch) -> None:
    async def fake_existing_signals(body, request):
        del request
        timestamps = pd.date_range("2024-01-01", periods=100, freq="D", tz="UTC")
        rows = [
            {"timestamp": ts, "symbol": symbol, "position": 1.0}
            for ts in timestamps
            for symbol in body.symbols
        ]
        return {"PYS-101": pd.DataFrame(rows)}, {}

    monkeypatch.setattr(ml_v1, "_load_existing_strategy_signals", fake_existing_signals)
    request = _request()
    request["existing_strategies"] = ["PYS-101"]
    submitted = client.post("/api/v1/ml/jobs", json=request)
    assert submitted.status_code == 202, submitted.text
    completed = _poll(client, submitted.json()["job_id"])
    assert completed["status"] == "done", completed.get("error")
    result = completed["result"]
    assert "PYS-101" in result["existing_strategy_scores"]
    kinds = {
        row["name"]: row["kind"]
        for row in result["strategy_comparison"]["leaderboard"]
    }
    assert kinds["PYS-101"] == "existing_strategy"


def test_ml_job_validation_and_unknown_status(client) -> None:
    invalid = _request()
    invalid["top_n"] = 99
    response = client.post("/api/v1/ml/jobs", json=invalid)
    assert response.status_code == 422
    assert client.get("/api/v1/ml/jobs/unknown").status_code == 404

    reserved = _request()
    reserved["existing_strategies"] = ["ensemble"]
    assert client.post("/api/v1/ml/jobs", json=reserved).status_code == 422


def test_ml_job_supports_multifrequency_and_risk_parity(client) -> None:
    request = _request()
    request["feature_frequencies"] = ["4h", "1d"]
    request["portfolio"] = {
        "method": "risk_parity",
        "lookback_periods": 30,
        "min_history_periods": 15,
        "max_weight": 0.60,
    }
    submitted = client.post("/api/v1/ml/jobs", json=request)
    assert submitted.status_code == 202, submitted.text
    completed = _poll(client, submitted.json()["job_id"])
    assert completed["status"] == "done", completed.get("error")
    result = completed["result"]
    assert result["multi_frequency"]["causal_join"] == "backward_asof"
    assert result["asset_allocation"]["method"] == "risk_parity"
    assert result["asset_allocation"]["latest"]["weights"]
    history = client.get("/api/v1/ml/experiments").json()
    assert history["count"] == 1
