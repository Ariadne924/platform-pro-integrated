"""Tests for the batch-evaluation background job: progress events + polling routes.

``batch_evaluate`` emits structured progress events through its injected
``progress`` callback; the web layer turns those into a job the frontend polls.
These tests cover the event vocabulary (unit) and the POST/GET route contract
(route), following the same isolated-state pattern as ``test_evaluation_routes``.
"""

import asyncio
import time

import pytest

import superplatform_web.jobs as jobs
import superplatform_web.state as _state
from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.providers.synthetic import SyntheticKLineProvider
from superplatform.factors.registry import FactorRegistry
from superplatform.runtime.config import Config
from superplatform_web.research import batch_evaluate


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path, monkeypatch):
    """Isolate config + provider registry + job registry for each test."""
    monkeypatch.setattr(_state, "_CONFIG_FILES", (
        str(tmp_path / "default.yaml"),
        str(tmp_path / "exchanges.yaml"),
        str(tmp_path / "factors.yaml"),
        str(tmp_path / "settings.yaml"),
    ))
    monkeypatch.setattr("superplatform_web.app._EXPERIMENTS_PATH", tmp_path / "experiments.duckdb")
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()
    jobs._jobs.clear()
    yield
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()
    jobs._jobs.clear()


def _batch_config() -> Config:
    return Config({
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "factors": {
            "momentum": {"symbols": ["S1"], "params": {"lookback_days": 20}},
            "short_term_reversal": {"symbols": ["S1"], "params": {"lookback_days": 5}},
            "rsi": {"symbols": ["S1"], "params": {"lookback_days": 14}},
        },
        "evaluation": {"forward_bias": {"n_cutoffs": 5}},
    })


def _registry() -> DataProviderRegistry:
    reg = DataProviderRegistry()
    reg.register(SyntheticKLineProvider(seed=42))
    return reg


def test_batch_evaluate_emits_full_progress_sequence():
    """batch_evaluate should emit every pipeline stage in order via progress."""
    FactorRegistry.get_instance().auto_discover()
    collected: list[dict] = []

    asyncio.run(batch_evaluate(
        base_config=_batch_config(),
        providers=_registry(),
        factor_names=["momentum", "short_term_reversal", "rsi"],
        symbols=["S1"],
        start="2024-01-01",
        end="2024-06-30",
        progress=collected.append,
    ))

    kinds = [ev["kind"] for ev in collected]
    assert kinds[0] == "batch_start"
    assert kinds[-1] == "batch_done"
    assert collected[0]["factor_count"] == 3

    # Per-factor stages: 3 factors, single group each.
    assert kinds.count("factor_start") == 3
    assert kinds.count("factor_done") == 3
    assert kinds.count("compute") == 3
    assert kinds.count("cross_section") == 3
    assert kinds.count("metrics") == 3
    assert kinds.count("forward_bias") == 3

    # Fetch is deduplicated across factors sharing the same request.
    assert kinds.count("fetch_start") == 1
    assert kinds.count("fetch_done") == 1
    fetch_done = next(ev for ev in collected if ev["kind"] == "fetch_done")
    assert fetch_done["rows"] >= 1
    # Timing + byte estimate ride along so the web layer can show speed.
    assert fetch_done["elapsed"] >= 0
    assert fetch_done["bytes"] > 0

    # Cross-factor stages, in the right relative order.
    assert kinds.count("correlation") == 1
    assert kinds.count("serialize") == 3
    first_factor_done = kinds.index("factor_done")
    assert kinds.index("correlation") > first_factor_done
    assert kinds.index("correlation") < kinds.index("batch_done")

    # forward_bias carries per-group progress indices.
    fb = next(ev for ev in collected if ev["kind"] == "forward_bias")
    assert fb["i"] == 1 and fb["n"] == 1
    # factor_done carries the ICIR string.
    fd = next(ev for ev in collected if ev["kind"] == "factor_done")
    assert isinstance(fd["icir"], str)


def test_format_progress_event_fetch_done_includes_speed():
    from superplatform_web.routes.factors import _format_progress_event

    done = _format_progress_event({
        "kind": "fetch_done",
        "data_type": "kline",
        "symbol": "BTCUSDT",
        "frequency": "1d",
        "rows": 1200,
        "elapsed": 3.0,
        "bytes": 6 * 1024 * 1024,  # 6 MB over 3s → ~2 MB/s
    })
    assert done.startswith("已拉取 BTCUSDT K线（1d）")
    assert "1200 行" in done
    assert "3.0s" in done
    assert "MB/s" in done

    pending = _format_progress_event({
        "kind": "fetch_pending",
        "data_type": "funding_rate",
        "symbol": "ETHUSDT",
        "frequency": "8h",
        "elapsed": 12.3,
    })
    assert pending.startswith("拉取 ETHUSDT 资金费率（8h）")
    assert "12s" in pending


def test_job_download_totals_survive_event_ring_eviction():
    """Cumulative download stats must not shrink when the event ring trims.

    Long multi-symbol batches flood ``events`` with ``fetch_pending``
    heartbeats, pushing the ring past ``MAX_EVENTS`` and evicting the oldest
    ``fetch_done`` rows. ``job.download`` keeps a running total so the panel's
    "已下载" counter is monotonic instead of recomputing over the trimmed ring.
    """
    job = jobs.create_batch_job()
    expected = {"fetches": 0, "rows": 0, "bytes": 0, "elapsed": 0.0}
    for rows, nbytes, elapsed in [(100, 1000, 2.0), (200, 2000, 3.0)]:
        jobs.record_job_event(
            job, "fetch_done", "done",
            {"kind": "fetch_done", "rows": rows, "bytes": nbytes, "elapsed": elapsed},
        )
        expected["fetches"] += 1
        expected["rows"] += rows
        expected["bytes"] += nbytes
        expected["elapsed"] += elapsed

    # Flood the ring past MAX_EVENTS so every fetch_done is evicted, the way
    # a multi-symbol batch's heartbeats do.
    for i in range(jobs.MAX_EVENTS + 10):
        jobs.record_job_event(job, "fetch_pending", f"pending {i}", {"elapsed": 1.0})

    # Ring stays capped and all fetch_done are gone from the window…
    assert len(job.events) == jobs.MAX_EVENTS
    assert all(e["kind"] != "fetch_done" for e in job.events)
    # …but the running totals kept every fetch.
    assert job.download == expected


def test_batch_job_status_reports_download_totals(client):
    """The status payload carries monotonic download totals for the panel."""
    _discover_factors()
    job_id = _start_batch(client, ["momentum"])
    snap = _poll_until_done(client, job_id)
    assert snap["status"] == "done", snap.get("error")
    assert snap["download"]["fetches"] >= 1
    assert snap["download"]["rows"] >= 1
    assert snap["download"]["bytes"] >= 0
    assert snap["download"]["elapsed"] >= 0


def test_batch_job_events_keep_structured_fetch_fields(client):
    """Stored events carry rows/elapsed/bytes so the panel can aggregate speed."""
    _discover_factors()
    job_id = _start_batch(client, ["momentum"])
    snap = _poll_until_done(client, job_id)
    assert snap["status"] == "done", snap.get("error")
    fetch_done = next(ev for ev in snap["events"] if ev["kind"] == "fetch_done")
    assert fetch_done["rows"] >= 1
    assert "elapsed" in fetch_done
    assert "bytes" in fetch_done


@pytest.fixture
def client(_fresh_state):
    from fastapi.testclient import TestClient

    from superplatform_web.app import app

    with TestClient(app) as c:
        yield c


def _discover_factors():
    FactorRegistry.get_instance().auto_discover()
    _state.providers.clear()
    _state.providers.register(SyntheticKLineProvider(seed=42))


def _start_batch(client, factors):
    r = client.post("/api/factors/batch", json={
        "factors": factors,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "start": "2024-01-01",
        "end": "2024-03-31",
        "frequency": "1d",
    })
    assert r.status_code == 200, r.text
    assert "job_id" in r.json()
    return r.json()["job_id"]


def _poll_until_done(client, job_id, timeout: float = 20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/factors/batch/{job_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.2)
    pytest.fail(f"batch job {job_id} did not finish in {timeout}s; last status={last!r}")


def test_batch_job_route_polls_to_done(client):
    """POST starts a job immediately; GET polls to a done status with a result."""
    _discover_factors()
    factors = ["momentum", "short_term_reversal", "rsi"]
    job_id = _start_batch(client, factors)

    # Job is registered and running right away.
    r = client.get(f"/api/factors/batch/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    snap = _poll_until_done(client, job_id)
    assert snap["status"] == "done", snap.get("error")

    result = snap["result"]
    assert result is not None
    assert len(result["results"]) == 3
    assert set(result["correlation"]["labels"]) == set(factors)

    # Progress log is populated with human-readable Chinese steps.
    messages = [ev["message"] for ev in snap["events"]]
    assert messages
    assert any("计算因子相关矩阵" in m for m in messages)
    assert any(m.startswith("评估因子：") for m in messages)
    assert any("完成（ICIR=" in m for m in messages)
    assert messages[-1] == "批量评估完成"


def test_batch_job_status_unknown_returns_404(client):
    r = client.get("/api/factors/batch/nonexistent-job")
    assert r.status_code == 404
