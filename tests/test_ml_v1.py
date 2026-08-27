from __future__ import annotations

import asyncio
import time

import numpy as np
import pandas as pd
import pytest

import superplatform_web.ml_jobs as ml_jobs
import superplatform_web.routes.ml_v1 as ml_v1


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
def _fresh_jobs(monkeypatch):
    ml_jobs._ml_jobs.clear()

    async def fake_build_batch_panel(**_kwargs):
        await asyncio.sleep(0)
        return _panel()

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
    assert payload["research_only"] is True


def test_ml_job_runs_training_backtest_and_score(client) -> None:
    submitted = client.post("/api/v1/ml/jobs", json=_request())
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    result = _poll(client, job_id)
    assert result["status"] == "done", result.get("error")
    assert result["result"]["strategy"]["equity"]
    assert result["result"]["equal_weight_benchmark"]["equity"]
    assert result["result"]["score"]["weights"]["upside_bonus"] == 5


def test_identical_ml_job_reuses_cached_result(client) -> None:
    first = client.post("/api/v1/ml/jobs", json=_request()).json()
    completed = _poll(client, first["job_id"])
    assert completed["status"] == "done"
    second = client.post("/api/v1/ml/jobs", json=_request())
    assert second.status_code == 202
    assert second.json()["job_id"] == first["job_id"]
    assert second.json()["reused"] is True


def test_ml_job_validation_and_unknown_status(client) -> None:
    invalid = _request()
    invalid["top_n"] = 99
    response = client.post("/api/v1/ml/jobs", json=invalid)
    assert response.status_code == 422
    assert client.get("/api/v1/ml/jobs/unknown").status_code == 404
