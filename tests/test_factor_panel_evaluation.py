"""Coverage for evaluation from separate factor and return-label artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

from superplatform.evaluation.experiment import run_evaluation


def _write_separate_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Write a small complete UTC factor panel and matching perpetual labels."""
    timestamps = pd.date_range("2025-01-01", periods=25, freq="D", tz="UTC")
    symbols = [f"S{i:02d}" for i in range(10)]
    factor_rows: list[dict[str, object]] = []
    label_rows: list[dict[str, object]] = []
    for day_index, timestamp in enumerate(timestamps):
        for symbol_index, symbol in enumerate(symbols):
            factor_rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "factor_name": "test_factor",
                    "factor_value": float(symbol_index + day_index / 100.0),
                }
            )
            label_rows.append(
                {
                    "timestamp": timestamp,
                    "available_ts": timestamp,
                    "entry_ts": timestamp + pd.Timedelta(days=1),
                    "exit_ts": timestamp + pd.Timedelta(days=2),
                    "exchange": "binance",
                    "market_type": "perpetual",
                    "settlement_asset": "USDT",
                    "funding_included": True,
                    "is_eligible": True,
                    "symbol": symbol,
                    "ret_1": float((symbol_index - 4.5) / 10_000.0),
                    "ret_5": float((symbol_index - 4.5) / 5_000.0),
                    "ret_10": float((symbol_index - 4.5) / 2_500.0),
                    "ret_20": float((symbol_index - 4.5) / 1_250.0),
                }
            )
    factor_path = tmp_path / "factor_panel.csv"
    labels_path = tmp_path / "evaluation_panel.csv"
    pd.DataFrame(factor_rows).to_csv(factor_path, index=False)
    pd.DataFrame(label_rows).to_csv(labels_path, index=False)
    return factor_path, labels_path


def test_evaluation_merges_separate_factor_and_label_panels(tmp_path: Path) -> None:
    """The evaluation runner must consume the generation artifact without new factors."""
    factor_path, labels_path = _write_separate_inputs(tmp_path)
    default_config_path = Path(__file__).parents[1] / "config" / "config.yaml"
    config = yaml.safe_load(default_config_path.read_text(encoding="utf-8"))
    config["output"]["root"] = str(tmp_path / "outputs")
    config["input"]["factor_panel_path"] = str(factor_path)
    config["input"]["evaluation_panel_path"] = str(labels_path)
    config["input"]["panel_path"] = None
    config["experiment"]["experiment_id"] = "separate-factor-inputs"
    config_path = tmp_path / "evaluation.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )

    output_dir = run_evaluation(config_path, run_date="separate")

    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    qc_result = json.loads((output_dir / "qc_result.json").read_text(encoding="utf-8"))
    evaluated = pd.read_csv(output_dir / "evaluated_panel.csv")
    assert manifest["status"] == "success"
    assert qc_result["input_merge"]["unmatched_factor_rows"] == 0
    assert qc_result["funding_contract"]["passed"] is True
    assert {"factor_name", "factor_value", "ret_1", "available_ts"}.issubset(
        evaluated.columns
    )
    assert len(evaluated) == 250


def test_separate_perpetual_labels_require_funding_declaration(tmp_path: Path) -> None:
    """Perpetual return labels fail closed when the funding audit flag is absent."""
    factor_path, labels_path = _write_separate_inputs(tmp_path)
    labels = pd.read_csv(labels_path)
    labels = labels.drop(columns="funding_included")
    labels.to_csv(labels_path, index=False)

    default_config_path = Path(__file__).parents[1] / "config" / "config.yaml"
    config = yaml.safe_load(default_config_path.read_text(encoding="utf-8"))
    config["output"]["root"] = str(tmp_path / "outputs")
    config["input"]["factor_panel_path"] = str(factor_path)
    config["input"]["evaluation_panel_path"] = str(labels_path)
    config["input"]["panel_path"] = None
    config["experiment"]["experiment_id"] = "funding-contract-failure"
    config_path = tmp_path / "evaluation.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )

    output_dir = run_evaluation(config_path, run_date="funding-failure")

    failures = pd.read_csv(output_dir / "failed_tasks.csv")
    qc_result = json.loads((output_dir / "qc_result.json").read_text(encoding="utf-8"))
    assert failures["task"].tolist() == ["load_input"]
    assert "funding_included" in qc_result["error"]
    assert qc_result["funding_contract"]["passed"] is False
