"""Offline end-to-end acceptance coverage for the evaluation entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

from superplatform.evaluation.experiment import run_evaluation

REQUIRED_OUTPUTS = {
    "corr_pearson.csv",
    "corr_pearson.png",
    "corr_spearman.csv",
    "corr_spearman.png",
    "decile_returns.csv",
    "evaluated_panel.csv",
    "evaluation.log",
    "evaluation_report.md",
    "failed_tasks.csv",
    "ic_timeseries.csv",
    "ic_timeseries.png",
    "input_panel_snapshot.csv",
    "layer_assignment_log.csv",
    "layer_nav.png",
    "long_short_nav.csv",
    "long_short_returns.csv",
    "qc_result.json",
    "rank_ic_timeseries.csv",
    "resolved_config.yaml",
    "run_manifest.json",
    "stability.csv",
    "turnover.csv",
}


def test_evaluation_entrypoint_completes_offline_and_writes_auditable_outputs(
    tmp_path: Path,
) -> None:
    """The Demo-backed entry point must produce a complete successful run."""
    default_config_path = Path(__file__).parents[1] / "config" / "config.yaml"
    config = yaml.safe_load(default_config_path.read_text(encoding="utf-8"))
    config["output"]["root"] = str(tmp_path / "outputs")
    config["input"]["panel_path"] = str(tmp_path / "missing_panel.csv")
    config["experiment"]["experiment_id"] = "e2e-offline"
    config_path = tmp_path / "e2e_config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )

    output_dir = run_evaluation(
        config_path,
        run_date="e2e",
        demo=True,
    )

    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["failed_tasks"] == []
    assert REQUIRED_OUTPUTS.issubset(
        {path.name for path in output_dir.iterdir() if path.is_file()}
    )
    evaluated_panel = pd.read_csv(output_dir / "evaluated_panel.csv")
    assert {"is_eligible", "eligibility_reason"}.issubset(evaluated_panel.columns)
    assert evaluated_panel["is_eligible"].astype(str).str.lower().eq("true").all()
    assert evaluated_panel["eligibility_reason"].notna().all()

    qc_result = json.loads((output_dir / "qc_result.json").read_text(encoding="utf-8"))
    assert {"status", "factor", "returns", "forward_bias", "preprocessing"}.issubset(
        qc_result
    )
    assert qc_result["status"] == "passed"
    assert qc_result["forward_bias"]["passed"] is True
    for section in ("factor", "returns"):
        assert {
            "rows",
            "missing",
            "duplicate_key_rows",
            "extreme_ratio",
            "timestamp",
        }.issubset(qc_result[section])
        assert {
            "is_utc",
            "timezone",
            "null_count",
            "is_sorted",
        }.issubset(qc_result[section]["timestamp"])
