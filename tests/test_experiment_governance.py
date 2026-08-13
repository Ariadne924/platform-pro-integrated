"""Tests for immutable OOS windows and parameter hash governance."""

import json
from pathlib import Path

import pytest

from superplatform.evaluation.experiment import (
    _assert_oos_is_immutable,
    _experiment_history,
    _parameter_hash,
    _parameter_hash_warnings,
    _resolve_experiment_governance,
)


def _config(*, layers: int = 10) -> dict:
    return {
        "experiment": {
            "experiment_id": "frozen-oos",
            "in_sample": {
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-06-30T00:00:00Z",
            },
            "out_of_sample": {
                "start": "2024-07-01T00:00:00Z",
                "end": "2024-12-31T00:00:00Z",
            },
            "fail_fast_on_hash_change": False,
        },
        "evaluation": {"layers": layers, "return_col": "ret_1"},
    }


def test_oos_window_is_immutable_for_an_existing_experiment(tmp_path: Path) -> None:
    """A saved OOS window cannot be changed or removed under the same ID."""
    governance = _resolve_experiment_governance(_config())
    prior_dir = tmp_path / "prior"
    prior_dir.mkdir()
    (prior_dir / "run_manifest.json").write_text(
        json.dumps({"experiment": governance}),
        encoding="utf-8",
    )
    history = _experiment_history(tmp_path, governance["experiment_id"])
    changed = _resolve_experiment_governance(_config())
    changed["out_of_sample"]["end"] = "2025-01-31T00:00:00+00:00"

    with pytest.raises(ValueError, match="out_of_sample is frozen"):
        _assert_oos_is_immutable(history, changed)


def test_parameter_hash_change_creates_a_governance_warning() -> None:
    """A changed governed parameter is detectable under the same experiment ID."""
    governance = _resolve_experiment_governance(_config())
    previous_hash, _ = _parameter_hash(_config(), governance)
    current_hash, _ = _parameter_hash(_config(layers=5), governance)

    warnings = _parameter_hash_warnings(
        [{"experiment": governance, "params_hash": previous_hash}],
        experiment_id=governance["experiment_id"],
        params_hash=current_hash,
    )

    assert previous_hash != current_hash
    assert warnings[0]["code"] == "params_hash_changed"
    assert warnings[0]["previous_params_hashes"] == [previous_hash]
