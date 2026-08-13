"""Tests for persisted factor-research experiment snapshots."""

from superplatform_web.experiments import ExperimentStore


def test_experiment_store_round_trip(tmp_path):
    store = ExperimentStore(tmp_path / "experiments.duckdb")
    request = {
        "factor": "momentum",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "provider_id": "synthetic-kline",
        "start": "2021-01-01",
        "end": "2021-12-31",
        "oos_start": "2022-01-01",
        "oos_end": "2022-12-31",
    }
    result = {
        "in_sample": {"ic_stats": {"icir": 0.1}},
        "out_of_sample": {"ic_stats": {"icir": 0.05}},
    }

    experiment_id = store.save(request, result)

    history = store.list()
    detail = store.get(experiment_id)
    assert history[0]["experiment_id"] == experiment_id
    assert history[0]["in_sample"]["icir"] == 0.1
    assert detail == {
        "experiment_id": experiment_id,
        "created_at": detail["created_at"],
        "factor_name": "momentum",
        "request": request,
        **result,
    }
