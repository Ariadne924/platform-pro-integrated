"""Experiment runner — governance, regression guard, and full deliverable orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from superplatform.evaluation.backtest import BacktestResult, run_backtest
from superplatform.evaluation.correlation import compute_factor_correlations
from superplatform.evaluation.metrics import (
    compute_ic,
    compute_ic_ir,
    compute_rank_ic,
    preprocess_factor_panel,
)
from superplatform.evaluation.qc import check_forward_bias, run_qc
from superplatform.evaluation.report import generate_plots, write_evaluation_report
from superplatform.evaluation.returns import construct_perpetual_returns
from superplatform.evaluation.stability import rolling_stability

LOGGER = logging.getLogger("evaluation")
GOVERNED_PARAMETER_PATHS = (
    "evaluation.return_col",
    "evaluation.bar_interval",
    "evaluation.layers",
    "evaluation.min_assets_per_layer",
    "evaluation.min_assets",
    "market.exchange",
    "market.market_type",
    "market.settlement_asset",
    "market.allow_short",
    "preprocessing.winsorize_enabled",
    "preprocessing.zscore_enabled",
    "preprocessing.winsorize_limits",
    "stability.window_days",
    "stability.min_periods",
    "correlation.min_assets",
    "cost.fee_bps",
    "cost.slippage_bps",
    "universe.require_eligibility",
    "universe.eligibility_column",
    "input.require_temporal_metadata",
)
REGRESSION_METRICS = (
    "mean_ic",
    "rankic_mean",
    "ic_ir",
    "long_short_ann_return",
)


# ── Config helpers ────────────────────────────────────────────────────


def _load_config(config_path: Path) -> dict[str, Any]:
    """Load a YAML configuration and return an empty-safe mapping."""
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError("configuration root must be a mapping")
    return config


def _nested_get(config: dict[str, Any], path: str, default: Any) -> Any:
    """Read a dotted configuration key."""
    value: Any = config
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


# ── Experiment governance ─────────────────────────────────────────────


def _normalize_experiment_window(
    value: Any,
    *,
    label: str,
) -> dict[str, str | None]:
    """Validate and canonicalize an optional inclusive UTC experiment window."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"experiment.{label} must be a mapping")
    start = value.get("start")
    end = value.get("end")
    if (start is None) != (end is None):
        raise ValueError(f"experiment.{label} requires both start and end")
    if start is None:
        return {"start": None, "end": None}
    try:
        normalized_start = pd.Timestamp(start, tz="UTC")
        normalized_end = pd.Timestamp(end, tz="UTC")
    except (TypeError, ValueError) as error:
        raise ValueError(f"experiment.{label} dates must be valid timestamps") from error
    if normalized_start > normalized_end:
        raise ValueError(f"experiment.{label}.start must be on or before end")
    return {
        "start": normalized_start.isoformat(),
        "end": normalized_end.isoformat(),
    }


def _resolve_experiment_governance(config: dict[str, Any]) -> dict[str, Any]:
    """Validate frozen experiment metadata without mutating the loaded config."""
    raw = _nested_get(config, "experiment", {})
    if not isinstance(raw, dict):
        raise ValueError("experiment must be a mapping")
    experiment_id = str(raw.get("experiment_id", "")).strip()
    if not experiment_id:
        raise ValueError("experiment.experiment_id must be non-empty")
    fail_fast = raw.get("fail_fast_on_hash_change", False)
    if not isinstance(fail_fast, bool):
        raise ValueError("experiment.fail_fast_on_hash_change must be boolean")
    in_sample = _normalize_experiment_window(raw.get("in_sample"), label="in_sample")
    out_of_sample = _normalize_experiment_window(
        raw.get("out_of_sample"),
        label="out_of_sample",
    )
    if (
        in_sample["end"] is not None
        and out_of_sample["start"] is not None
        and pd.Timestamp(in_sample["end"]) >= pd.Timestamp(out_of_sample["start"])
    ):
        raise ValueError("in_sample must end before out_of_sample starts")
    return {
        "experiment_id": experiment_id,
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "fail_fast_on_hash_change": fail_fast,
    }


def _json_safe(value: Any) -> Any:
    """Convert audit payload values into strict JSON-compatible primitives."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    if isinstance(value, (datetime, pd.Timestamp, pd.Timedelta, Path)):
        return str(value)
    if value is pd.NA:
        return None
    return value


def _parameter_hash(
    config: dict[str, Any],
    governance: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Hash the stable subset of parameters that defines an experiment run."""
    parameters = {
        path: _nested_get(config, path, None)
        for path in GOVERNED_PARAMETER_PATHS
    }
    parameters["experiment.in_sample"] = governance["in_sample"]
    parameters["experiment.out_of_sample"] = governance["out_of_sample"]
    normalized = _json_safe(parameters)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), normalized


def _experiment_history(output_root: Path, experiment_id: str) -> list[dict[str, Any]]:
    """Load readable manifests for one experiment ID from prior run directories."""
    history: list[dict[str, Any]] = []
    if not output_root.exists():
        return history
    for path in sorted(output_root.glob("*/run_manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        experiment = manifest.get("experiment")
        if isinstance(experiment, dict) and experiment.get("experiment_id") == experiment_id:
            history.append(manifest)
    return history


def _assert_oos_is_immutable(
    history: list[dict[str, Any]],
    governance: dict[str, Any],
) -> None:
    """Reject attempts to change or remove a previously frozen OOS window."""
    frozen_windows = {
        json.dumps(
            manifest["experiment"]["out_of_sample"],
            sort_keys=True,
            separators=(",", ":"),
        )
        for manifest in history
        if isinstance(manifest.get("experiment"), dict)
        and isinstance(manifest["experiment"].get("out_of_sample"), dict)
        and manifest["experiment"]["out_of_sample"].get("start") is not None
    }
    if len(frozen_windows) > 1:
        raise ValueError("historical manifests disagree on the frozen out_of_sample window")
    if frozen_windows:
        frozen_window = next(iter(frozen_windows))
        current_window = json.dumps(
            governance["out_of_sample"],
            sort_keys=True,
            separators=(",", ":"),
        )
        if current_window != frozen_window:
            raise ValueError(
                "out_of_sample is frozen for this experiment_id; use a new experiment_id"
            )


def _parameter_hash_warnings(
    history: list[dict[str, Any]],
    *,
    experiment_id: str,
    params_hash: str,
) -> list[dict[str, Any]]:
    """Describe prior parameter hashes that differ from the current run."""
    previous_hashes = sorted(
        {
            str(manifest["params_hash"])
            for manifest in history
            if isinstance(manifest.get("params_hash"), str)
            and manifest["params_hash"] != params_hash
        }
    )
    if not previous_hashes:
        return []
    return [
        {
            "code": "params_hash_changed",
            "experiment_id": experiment_id,
            "current_params_hash": params_hash,
            "previous_params_hashes": previous_hashes,
        }
    ]


# ── Regression guard ──────────────────────────────────────────────────


def _periods_per_year(bar_interval: str) -> float:
    """Convert the configured bar interval to its annual observation count."""
    normalized_interval = (
        f"{bar_interval[:-1]}D"
        if bar_interval.lower().endswith("d")
        else bar_interval
    )
    interval = pd.Timedelta(normalized_interval)
    if interval <= pd.Timedelta(0):
        raise ValueError("evaluation.bar_interval must be positive")
    return float(pd.Timedelta(days=365) / interval)


def _safe_float(value: Any) -> float | None:
    """Convert a scalar to a JSON-friendly finite float."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _regression_metrics(
    ic_data: pd.DataFrame,
    rank_ic_data: pd.DataFrame,
    long_short_returns: pd.DataFrame,
    *,
    bar_interval: str,
) -> dict[str, float | None]:
    """Snapshot existing evaluation outputs into scalar regression guard metrics."""
    mean_ic = _safe_float(ic_data["ic"].mean()) if "ic" in ic_data else None
    rankic_mean = (
        _safe_float(rank_ic_data["rank_ic"].mean())
        if "rank_ic" in rank_ic_data
        else None
    )
    ic_ir = _safe_float(compute_ic_ir(ic_data)["ic_ir"]) if not ic_data.empty else None
    long_short_ann_return: float | None = None
    if not long_short_returns.empty:
        return_col = (
            "net_return"
            if "net_return" in long_short_returns.columns
            else "long_short_return"
        )
        annualized: list[float] = []
        for _, group in long_short_returns.groupby("factor_name", sort=True):
            values = pd.to_numeric(group[return_col], errors="coerce").dropna()
            if values.empty or (values <= -1.0).any():
                continue
            cumulative = float((1.0 + values.to_numpy(dtype=float)).prod())
            annualized_return = cumulative ** (
                _periods_per_year(bar_interval) / len(values)
            ) - 1.0
            if np.isfinite(annualized_return):
                annualized.append(float(annualized_return))
        long_short_ann_return = _safe_float(np.mean(annualized)) if annualized else None
    return {
        "mean_ic": mean_ic,
        "rankic_mean": rankic_mean,
        "ic_ir": ic_ir,
        "long_short_ann_return": long_short_ann_return,
    }


def _resolve_regression_guard_config(
    config: dict[str, Any],
    *,
    output_root: Path,
) -> tuple[Path, dict[str, float]]:
    """Validate the baseline path and absolute-delta thresholds."""
    raw = _nested_get(config, "regression_guard", {})
    if not isinstance(raw, dict):
        raise ValueError("regression_guard must be a mapping")
    baseline_path = Path(raw.get("baseline_path", "regression_baseline.json"))
    if not baseline_path.is_absolute():
        baseline_path = output_root / baseline_path
    raw_thresholds = raw.get("thresholds", {})
    if not isinstance(raw_thresholds, dict):
        raise ValueError("regression_guard.thresholds must be a mapping")
    thresholds: dict[str, float] = {}
    for metric in REGRESSION_METRICS:
        try:
            threshold = float(raw_thresholds.get(metric, 0.0))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"regression_guard.thresholds.{metric} must be numeric"
            ) from error
        if not np.isfinite(threshold) or threshold < 0:
            raise ValueError(
                f"regression_guard.thresholds.{metric} must be finite and non-negative"
            )
        thresholds[metric] = threshold
    return baseline_path, thresholds


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write stable sorted JSON using UTF-8."""
    path.write_text(
        json.dumps(
            _json_safe(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    """Return a content hash for an audit artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_regression_guard(
    metrics: dict[str, float | None],
    *,
    baseline_path: Path,
    thresholds: dict[str, float],
) -> dict[str, Any]:
    """Create a baseline once, then emit alerts for threshold-exceeding deltas."""
    if not baseline_path.exists():
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline = {"metrics": metrics}
        _write_json(baseline_path, baseline)
        return {
            "status": "baseline_created",
            "baseline_path": str(baseline_path),
            "metrics": metrics,
            "alerts": [],
        }
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read regression baseline: {baseline_path}") from error
    baseline_metrics = baseline.get("metrics")
    if not isinstance(baseline_metrics, dict):
        raise ValueError("regression baseline is missing a metrics mapping")

    alerts: list[dict[str, Any]] = []
    for metric in REGRESSION_METRICS:
        current = metrics.get(metric)
        previous = baseline_metrics.get(metric)
        if current is None or previous is None:
            continue
        delta = float(current) - float(previous)
        if abs(delta) > thresholds[metric]:
            alerts.append(
                {
                    "metric": metric,
                    "baseline": float(previous),
                    "current": float(current),
                    "delta": delta,
                    "threshold": thresholds[metric],
                }
            )
    return {
        "status": "alerts" if alerts else "passed",
        "baseline_path": str(baseline_path),
        "metrics": metrics,
        "alerts": alerts,
    }


# ── Panel loading and validation ──────────────────────────────────────


def _parse_utc_column(series: pd.Series, label: str) -> pd.Series:
    """Parse a timestamp column only when the source explicitly carries UTC."""
    parsed = pd.to_datetime(series, errors="raise")
    timezone = getattr(parsed.dtype, "tz", None)
    if timezone is None:
        raise ValueError(f"{label} must be timezone-aware UTC; naive timestamps are rejected")
    if str(timezone).upper() not in {"UTC", "UTC+00:00", "ETC/UTC"}:
        raise ValueError(f"{label} must use UTC, got {timezone}")
    if parsed.isna().any():
        raise ValueError(f"{label} contains null timestamps")
    return parsed


def _validate_temporal_contract(
    panel: pd.DataFrame,
    *,
    require_metadata: bool,
    return_col: str | None = None,
    bar_interval: str | None = None,
) -> None:
    """Enforce availability ordering and the selected future-horizon interval."""
    temporal_columns = ("available_ts", "entry_ts", "exit_ts")
    missing = [column for column in temporal_columns if column not in panel.columns]
    if require_metadata and missing:
        raise ValueError(
            "input panel is missing temporal contract columns: "
            + ", ".join(missing)
        )
    if missing:
        return
    for column in temporal_columns:
        panel[column] = _parse_utc_column(panel[column], f"panel.{column}")
    if (panel["available_ts"] < panel["timestamp"]).any():
        raise ValueError("factor available_ts cannot precede timestamp")
    if (panel["entry_ts"] <= panel["available_ts"]).any():
        raise ValueError("entry_ts must be after factor availability")
    if (panel["exit_ts"] <= panel["entry_ts"]).any():
        raise ValueError("exit_ts must be after entry_ts")
    if return_col is not None and bar_interval is not None:
        try:
            horizon = int(return_col.removeprefix("ret_"))
        except ValueError as error:
            raise ValueError(
                "return_col must use ret_<bars> when horizon validation is enabled"
            ) from error
        normalized_interval = (
            f"{bar_interval[:-1]}D"
            if bar_interval.lower().endswith("d")
            else bar_interval
        )
        expected_interval = pd.Timedelta(normalized_interval) * horizon
        actual_interval = panel["exit_ts"] - panel["entry_ts"]
        if not actual_interval.eq(expected_interval).all():
            raise ValueError(
                f"{return_col} exit interval must equal "
                f"{horizon} x {bar_interval}"
            )


def _load_panel(
    path: Path,
    *,
    return_col: str = "ret_1",
    require_return_col: bool = True,
) -> pd.DataFrame:
    """Read a panel and require the return column selected by the run config.

    ``forward_return`` remains a compatibility alias for the default ``ret_1``
    horizon.  Non-default horizons must be present explicitly so a run cannot
    silently evaluate one horizon while the configuration names another.
    """
    if path.suffix.lower() == ".parquet":
        panel = pd.read_parquet(path)
    elif path.suffix.lower() in {".csv", ".txt"}:
        panel = pd.read_csv(path)
    else:
        raise ValueError(f"unsupported panel format: {path.suffix}")
    required = {"timestamp", "symbol"}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"input panel is missing required columns: {missing}")
    if "factor_value" not in panel.columns and "factor_value_eval" not in panel.columns:
        raise ValueError("input panel must contain factor_value or factor_value_eval")
    panel["timestamp"] = _parse_utc_column(panel["timestamp"], "panel.timestamp")
    for column in ("available_ts", "entry_ts", "exit_ts"):
        if column in panel.columns:
            panel[column] = _parse_utc_column(panel[column], f"panel.{column}")
    if "factor_name" not in panel.columns:
        panel["factor_name"] = "factor"
    if require_return_col and return_col not in panel.columns:
        if return_col == "ret_1" and "forward_return" in panel.columns:
            panel[return_col] = panel["forward_return"]
        else:
            raise ValueError(
                f"input panel must contain the configured return column: {return_col}"
            )
    return panel.sort_values(["timestamp", "factor_name", "symbol"]).reset_index(drop=True)


def _load_factor_panel(path: Path) -> pd.DataFrame:
    """Load the factor-generation artifact without deriving factor values."""
    if path.suffix.lower() == ".parquet":
        factor_panel = pd.read_parquet(path)
    elif path.suffix.lower() in {".csv", ".txt"}:
        factor_panel = pd.read_csv(path)
    else:
        raise ValueError(f"unsupported factor panel format: {path.suffix}")

    required = {"timestamp", "symbol", "factor_name", "factor_value"}
    missing = sorted(required.difference(factor_panel.columns))
    if missing:
        raise ValueError(f"factor panel is missing required columns: {missing}")
    factor_panel["timestamp"] = _parse_utc_column(
        factor_panel["timestamp"],
        "factor_panel.timestamp",
    )
    if factor_panel[["timestamp", "symbol", "factor_name"]].isna().any().any():
        raise ValueError("factor panel contains null key values")
    if factor_panel.duplicated(["timestamp", "symbol", "factor_name"]).any():
        raise ValueError(
            "factor panel contains duplicate keys: "
            "['timestamp', 'symbol', 'factor_name']"
        )
    factor_panel["factor_value"] = pd.to_numeric(
        factor_panel["factor_value"],
        errors="coerce",
    )
    factor_panel.loc[
        ~np.isfinite(factor_panel["factor_value"].to_numpy(dtype=float)),
        "factor_value",
    ] = np.nan
    return factor_panel.sort_values(
        ["timestamp", "factor_name", "symbol"]
    ).reset_index(drop=True)


def _load_evaluation_panel(path: Path) -> pd.DataFrame:
    """Load return labels and the temporal contract for factor evaluation."""
    if path.suffix.lower() == ".parquet":
        evaluation_panel = pd.read_parquet(path)
    elif path.suffix.lower() in {".csv", ".txt"}:
        evaluation_panel = pd.read_csv(path)
    else:
        raise ValueError(f"unsupported evaluation panel format: {path.suffix}")

    required = {
        "timestamp",
        "available_ts",
        "entry_ts",
        "exit_ts",
        "exchange",
        "market_type",
        "settlement_asset",
        "is_eligible",
        "symbol",
        "ret_1",
        "ret_5",
        "ret_10",
        "ret_20",
    }
    missing = sorted(required.difference(evaluation_panel.columns))
    if missing:
        raise ValueError(f"evaluation panel is missing required columns: {missing}")
    for column in ("timestamp", "available_ts", "entry_ts", "exit_ts"):
        evaluation_panel[column] = _parse_utc_column(
            evaluation_panel[column],
            f"evaluation_panel.{column}",
        )
    if evaluation_panel[["timestamp", "symbol"]].isna().any().any():
        raise ValueError("evaluation panel contains null key values")
    if evaluation_panel.duplicated(["timestamp", "symbol"]).any():
        raise ValueError(
            "evaluation panel contains duplicate keys: ['timestamp', 'symbol']"
        )
    return evaluation_panel.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def _merge_factor_and_evaluation_panels(
    factor_panel: pd.DataFrame,
    evaluation_panel: pd.DataFrame,
) -> pd.DataFrame:
    """Attach labels and temporal metadata to generated factor observations."""
    return factor_panel.merge(
        evaluation_panel,
        on=["timestamp", "symbol"],
        how="left",
        validate="many_to_one",
        indicator=True,
    )


def _validate_funding_contract(
    panel: pd.DataFrame,
    *,
    market_type: str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed when perpetual labels lack an auditable funding declaration."""
    require_funding = bool(
        _nested_get(config, "market.require_funding_included", True)
    )
    funding_column = str(
        _nested_get(config, "market.funding_included_column", "funding_included")
    )
    result = {
        "required": bool(market_type == "perpetual" and require_funding),
        "column": funding_column,
        "passed": True,
        "checked_rows": int(len(panel)),
        "true_rows": None,
        "false_rows": None,
        "null_rows": None,
        "message": None,
    }
    if market_type != "perpetual" or not require_funding:
        result["message"] = "funding declaration not required for this market contract"
        return result
    if funding_column not in panel.columns:
        result.update(
            {
                "passed": False,
                "message": f"missing required perpetual funding column: {funding_column}",
            }
        )
        return result

    included = _coerce_boolean_flag(panel[funding_column])
    null_rows = int(panel[funding_column].isna().sum())
    false_rows = int((~included & panel[funding_column].notna()).sum())
    true_rows = int(included.sum())
    result.update(
        {
            "passed": null_rows == 0 and false_rows == 0,
            "true_rows": true_rows,
            "false_rows": false_rows,
            "null_rows": null_rows,
        }
    )
    if not result["passed"]:
        result["message"] = (
            "perpetual return labels must explicitly declare funding included "
            f"(false_rows={false_rows}, null_rows={null_rows})"
        )
    return result


# ── Demo data ─────────────────────────────────────────────────────────


def _make_demo_panel(
    seed: int,
    *,
    return_col: str = "ret_1",
    days: int = 180,
    n_symbols: int = 20,
) -> pd.DataFrame:
    """Create a deterministic multi-factor panel for a no-input smoke run."""
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2024-01-01", periods=days, freq="D", tz="UTC")
    symbols = [f"DEMO{i:02d}" for i in range(n_symbols)]
    horizon_days = {"ret_1": 1, "ret_5": 5, "ret_10": 10, "ret_20": 20}.get(
        return_col
    )
    if horizon_days is None:
        raise ValueError("return_col must be ret_1, ret_5, ret_10, or ret_20")
    rows: list[dict[str, object]] = []
    symbol_effect = np.linspace(-1.0, 1.0, n_symbols)
    for timestamp in timestamps:
        base = rng.normal(0.0, 1.0, n_symbols)
        factor_values = {
            "demo_momentum": base + symbol_effect,
            "demo_reversal": -base + rng.normal(0.0, 0.25, n_symbols),
        }
        for factor_name, values in factor_values.items():
            for symbol_index, (symbol, factor_value) in enumerate(zip(
                symbols,
                values,
                strict=True,
            )):
                rows.append(
                    {
                        "timestamp": timestamp,
                        "available_ts": timestamp,
                        "entry_ts": timestamp + pd.Timedelta(days=1),
                        "exit_ts": timestamp + pd.Timedelta(days=horizon_days + 1),
                        "symbol": symbol,
                        "factor_name": factor_name,
                        "factor_value": float(factor_value),
                        "is_eligible": True,
                        "exchange": "binance",
                        "market_type": "perpetual",
                        "settlement_asset": "USDT",
                        "funding_included": True,
                        "close": float(
                            100.0
                            + symbol_index * 5.0
                            + (timestamp - timestamps[0]).days
                            * (0.2 + symbol_index * 0.01)
                        ),
                        "funding_rate": 0.0001
                        if (timestamp - timestamps[0]).days % 8 == 0
                        else 0.0,
                    }
                )
    return pd.DataFrame(rows)


# ── Sample filtering and eligibility ──────────────────────────────────


def _coerce_boolean_flag(series: pd.Series) -> pd.Series:
    """Normalize a boolean-like input flag while treating nulls as false."""
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False).astype(bool)
    return series.astype(str).str.lower().isin({"true", "1"})


def _prepare_eligibility_audit(
    panel: pd.DataFrame,
    config: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Add canonical eligibility fields and summarize existing filter inputs."""
    result = panel.copy()
    eligibility_col = _nested_get(
        config,
        "universe.eligibility_column",
        "is_eligible",
    )
    require_eligibility = bool(
        _nested_get(config, "universe.require_eligibility", True)
    )
    if eligibility_col in result.columns:
        eligibility = _coerce_boolean_flag(result[eligibility_col])
        source_tag = np.where(
            eligibility,
            f"input:{eligibility_col}=true",
            f"input:{eligibility_col}=false",
        )
        existing_reason = (
            result["eligibility_reason"].astype("string")
            if "eligibility_reason" in result.columns
            else pd.Series(pd.NA, index=result.index, dtype="string")
        )
        has_reason = existing_reason.notna() & existing_reason.str.strip().ne("")
        result["is_eligible"] = eligibility
        result["eligibility_reason"] = pd.Series(source_tag, index=result.index)
        result.loc[has_reason, "eligibility_reason"] = (
            result.loc[has_reason, "eligibility_reason"]
            + ";"
            + existing_reason.loc[has_reason]
        )
    elif require_eligibility:
        result["is_eligible"] = False
        result["eligibility_reason"] = f"missing:{eligibility_col}"
    else:
        result["is_eligible"] = True
        result["eligibility_reason"] = "eligibility_filter_disabled"

    reason_counts = {
        str(reason): int(count)
        for reason, count in result["eligibility_reason"].value_counts(
            dropna=False
        ).items()
    }
    statistics = {
        "input_rows": int(len(result)),
        "eligible_rows": int(result["is_eligible"].sum()),
        "selected_rows": None,
        "filtered_rows": None,
        "reason_counts": reason_counts,
    }
    return result, statistics


def _filter_sample(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """Apply time, market, and dynamic-eligibility filters to the input panel."""
    start = _nested_get(config, "evaluation.sample_start", None)
    end = _nested_get(config, "evaluation.sample_end", None)
    result = panel
    if start is not None:
        result = result[result["timestamp"] >= pd.to_datetime(start, utc=True)]
    if end is not None:
        result = result[result["timestamp"] <= pd.to_datetime(end, utc=True)]
    if _nested_get(config, "universe.require_eligibility", True):
        eligibility_col = _nested_get(
            config,
            "universe.eligibility_column",
            "is_eligible",
        )
        if eligibility_col not in result.columns:
            raise ValueError(f"input panel is missing eligibility column: {eligibility_col}")
        result = result[_coerce_boolean_flag(result[eligibility_col])]
    market_type = _nested_get(config, "market.market_type", None)
    if market_type is not None:
        market_col = _nested_get(config, "market.market_column", "market_type")
        if market_col not in result.columns:
            raise ValueError(f"input panel is missing market column: {market_col}")
        expected_market = str(market_type).lower()
        result = result[
            result[market_col].astype(str).str.lower().eq(expected_market)
        ]
    for config_key, column_key, default_column in (
        ("market.exchange", "market.exchange_column", "exchange"),
        ("market.settlement_asset", "market.settlement_asset_column", "settlement_asset"),
    ):
        expected_value = _nested_get(config, config_key, None)
        if expected_value is None:
            continue
        column = _nested_get(config, column_key, default_column)
        if column not in result.columns:
            raise ValueError(f"input panel is missing market column: {column}")
        result = result[
            result[column].astype(str).str.lower().eq(str(expected_value).lower())
        ]
    if result.empty:
        raise ValueError("sample filter produced an empty panel")
    return result.reset_index(drop=True)


def _resolve_market_contract(config: dict[str, Any], panel: pd.DataFrame) -> tuple[str | None, bool]:
    """Validate market-specific rules and return market type plus short permission."""
    configured_market = _nested_get(config, "market.market_type", None)
    if configured_market is None:
        return None, bool(_nested_get(config, "market.allow_short", True))
    market_type = str(configured_market).lower()
    if market_type not in {"spot", "perpetual"}:
        raise ValueError("market.market_type must be 'spot' or 'perpetual'")
    allow_short = bool(_nested_get(config, "market.allow_short", market_type != "spot"))
    if market_type == "spot" and allow_short:
        raise ValueError("spot evaluation cannot enable long-short results")
    return market_type, allow_short


def _build_return_table(panel: pd.DataFrame, return_col: str) -> pd.DataFrame:
    """Extract a unique return table and reject cross-factor return conflicts."""
    required = {"timestamp", "symbol", return_col}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"evaluation panel is missing return fields: {missing}")
    value_columns = [return_col]
    value_columns.extend(
        column for column in ("entry_ts", "exit_ts") if column in panel.columns
    )
    grouped = panel.groupby(["timestamp", "symbol"], dropna=False)[value_columns]
    conflicts = grouped.nunique(dropna=False).gt(1).any(axis=1)
    if conflicts.any():
        raise ValueError("factor rows disagree on the return or execution timestamps")
    returns = panel[["timestamp", "symbol", *value_columns]].drop_duplicates()
    if returns.duplicated(["timestamp", "symbol"]).any():
        raise ValueError("return table contains duplicate timestamp/symbol keys")
    return returns.reset_index(drop=True)


def _winsorize_limits(config: dict[str, Any]) -> tuple[float, float]:
    """Read and validate the two quantile boundaries for factor preprocessing."""
    raw_limits = _nested_get(config, "preprocessing.winsorize_limits", (0.01, 0.99))
    if not isinstance(raw_limits, (list, tuple)) or len(raw_limits) != 2:
        raise ValueError("preprocessing.winsorize_limits must contain two values")
    limits = float(raw_limits[0]), float(raw_limits[1])
    if not 0 <= limits[0] < limits[1] <= 1:
        raise ValueError("preprocessing.winsorize_limits must satisfy 0 <= low < high <= 1")
    return limits


# ── Logging ───────────────────────────────────────────────────────────


def _configure_logging(log_path: Path) -> None:
    """Configure console and file logging for one run."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


def _record_task_failure(
    failures: list[dict[str, Any]],
    task: str,
    error: Exception,
) -> None:
    """Log and retain a task failure for the final report."""
    LOGGER.exception("Task failed: %s", task)
    failures.append({"task": task, "error": f"{type(error).__name__}: {error}"})


# ── ExperimentRunner ──────────────────────────────────────────────────


class ExperimentRunner:
    """Orchestrate a full evaluation deliverable from a YAML config.

    Parameters
    ----------
    config_path:
        Path to the evaluation YAML configuration file.
    run_date:
        Override the output run date (YYYYMMDD).  Defaults to the config
        value or the current UTC date.
    demo:
        Generate deterministic demo data when the input panel is missing.
    project_root:
        Root directory for resolving relative paths.  Defaults to the
        repository root (two levels above this module).
    """

    def __init__(
        self,
        config_path: str | Path = "config/config.yaml",
        *,
        run_date: str | None = None,
        demo: bool = False,
        project_root: Path | None = None,
        panel: pd.DataFrame | None = None,
        output_subdir: str | None = None,
        bar_interval: str | None = None,
    ) -> None:
        self._config_file = Path(config_path)
        self._demo = demo
        self._panel = panel
        self._project_root = project_root or Path(__file__).resolve().parent.parent.parent.parent
        if not self._config_file.is_absolute():
            self._config_file = self._project_root / self._config_file
        self._config = _load_config(self._config_file)
        if bar_interval is not None:
            # Pin the temporal contract to the run cadence before hashing so
            # the delivered horizon, validation, and governance hash all
            # follow the cadence the web panel was built at.
            self._config.setdefault("evaluation", {})["bar_interval"] = bar_interval
        self._governance = _resolve_experiment_governance(self._config)
        self._params_hash, self._hashed_parameters = _parameter_hash(
            self._config, self._governance
        )
        self._seed = int(_nested_get(self._config, "runtime.random_seed", 42))  # noqa: RUF048
        self._return_col = _nested_get(self._config, "evaluation.return_col", "ret_1")
        self._factor_col = _nested_get(self._config, "evaluation.factor_col", "factor_value")
        self._configured_market_type = str(
            _nested_get(self._config, "market.market_type", "")
        ).lower()

        configured_date = _nested_get(self._config, "run_date", None)
        resolved_date = run_date or configured_date
        if not resolved_date or str(resolved_date).lower() == "auto":
            resolved_date = datetime.now(timezone.utc).strftime("%Y%m%d")  # noqa: UP017
        self._resolved_date = str(resolved_date)

        self._output_root = self._project_root / _nested_get(
            self._config, "output.root", "outputs"
        )
        self._output_dir = self._output_root / self._resolved_date
        if output_subdir:
            self._output_dir = self._output_dir / output_subdir
        # Regression baseline is scoped to the output directory so that
        # distinct experiments (e.g. different factor frequencies) keep
        # independent baselines.
        self._regression_baseline_path, self._regression_thresholds = (
            _resolve_regression_guard_config(
                self._config,
                output_root=self._output_dir,
            )
        )
        self.manifest: dict[str, Any] = {}

    # ── Main entry point ──────────────────────────────────────────

    def run(self) -> Path:
        """Execute all evaluation tasks and return the output directory."""
        output_dir = self._output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        _configure_logging(output_dir / "evaluation.log")
        LOGGER.info(
            "Starting evaluation run: date=%s seed=%s",
            self._resolved_date,
            self._seed,
        )
        np.random.seed(self._seed)

        (output_dir / "resolved_config.yaml").write_text(
            yaml.safe_dump(self._config, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )

        history = _experiment_history(self._output_root, self._governance["experiment_id"])
        _assert_oos_is_immutable(history, self._governance)
        governance_warnings = _parameter_hash_warnings(
            history,
            experiment_id=self._governance["experiment_id"],
            params_hash=self._params_hash,
        )
        for warning in governance_warnings:
            LOGGER.warning(
                "Experiment parameter hash changed: experiment_id=%s previous=%s current=%s",
                warning["experiment_id"],
                ",".join(warning["previous_params_hashes"]),
                warning["current_params_hash"],
            )

        if governance_warnings and self._governance["fail_fast_on_hash_change"]:
            return self._fail_governance(output_dir, governance_warnings)

        return self._run_pipeline(output_dir, governance_warnings)

    # ── Internal pipeline ─────────────────────────────────────────

    def _fail_governance(self, output_dir: Path, governance_warnings: list) -> Path:
        failure = {
            "task": "experiment_governance",
            "error": "params_hash changed for an existing experiment_id",
        }
        pd.DataFrame([failure]).to_csv(output_dir / "failed_tasks.csv", index=False)
        manifest = {
            "run_date": self._resolved_date,
            "random_seed": self._seed,
            "status": "governance_failure",
            "failed_tasks": [failure],
            "output_dir": str(output_dir),
            "config_sha256": _sha256_file(output_dir / "resolved_config.yaml"),
            "input_snapshot_sha256": None,
            "evaluated_panel_sha256": None,
            "qc_result_sha256": None,
            "experiment": self._governance,
            "params_hash": self._params_hash,
            "hashed_parameters": self._hashed_parameters,
            "governance_warnings": governance_warnings,
        }
        _write_json(output_dir / "run_manifest.json", manifest)
        self.manifest = manifest
        LOGGER.error("Stopped before evaluation because experiment governance failed")
        return output_dir

    def _run_pipeline(self, output_dir: Path, governance_warnings: list) -> Path:
        failures: list[dict[str, Any]] = []
        qc_result: dict[str, Any] = {"status": "not_run"}
        sample_filter_statistics: dict[str, Any] = {"status": "not_run"}
        market_type: str | None = None
        allow_short = True
        effective_factor_col = self._factor_col
        return_col = self._return_col

        legacy_panel_value = _nested_get(self._config, "input.panel_path", None)
        factor_panel_value = _nested_get(
            self._config,
            "input.factor_panel_path",
            "outputs/factors/factor_panel.csv",
        )
        evaluation_panel_value = _nested_get(
            self._config,
            "input.evaluation_panel_path",
            "data/evaluation_panel.csv",
        )
        separate_input_mode = not legacy_panel_value
        panel_path = (
            self._project_root / legacy_panel_value if legacy_panel_value else None
        )
        factor_panel_path = self._project_root / factor_panel_value
        evaluation_panel_path = self._project_root / evaluation_panel_value
        qc_result["input_sources"] = {
            "mode": (
                "factor_and_evaluation_files"
                if separate_input_mode
                else "legacy_merged_panel"
            ),
            "factor_panel_path": str(factor_panel_path),
            "factor_panel_exists": factor_panel_path.exists(),
            "evaluation_panel_path": str(evaluation_panel_path),
            "evaluation_panel_exists": evaluation_panel_path.exists(),
            "legacy_panel_path": str(panel_path) if panel_path is not None else None,
            "legacy_panel_exists": panel_path.exists() if panel_path is not None else None,
        }
        try:
            if self._panel is not None:
                panel = self._panel.copy()
                input_mode = "pipeline"
                LOGGER.info("Using panel from pipeline: rows=%d", len(panel))
            elif separate_input_mode:
                factor_panel = _load_factor_panel(factor_panel_path)
                evaluation_panel = _load_evaluation_panel(evaluation_panel_path)
                panel = _merge_factor_and_evaluation_panels(
                    factor_panel,
                    evaluation_panel,
                )
                input_mode = "factor_and_evaluation_files"
                merge_audit = {
                    "factor_rows": int(len(factor_panel)),
                    "evaluation_rows": int(len(evaluation_panel)),
                    "merged_rows": int(len(panel)),
                    "unmatched_factor_rows": int(panel["_merge"].ne("both").sum()),
                }
                qc_result["input_merge"] = merge_audit
                if merge_audit["unmatched_factor_rows"]:
                    raise ValueError(
                        "factor observations have no matching evaluation label rows: "
                        f"{merge_audit['unmatched_factor_rows']}"
                    )
                panel = panel.drop(columns="_merge")
                LOGGER.info(
                    "Loaded factor panel and evaluation panel: factors=%s labels=%s "
                    "merged=%d",
                    factor_panel_path,
                    evaluation_panel_path,
                    len(panel),
                )
            elif panel_path is not None and panel_path.exists():
                panel = _load_panel(
                    panel_path,
                    return_col=return_col,
                    require_return_col=self._configured_market_type != "perpetual",
                )
                input_mode = "file"
                LOGGER.info("Loaded panel: path=%s rows=%d", panel_path, len(panel))
            elif self._demo:
                panel = _make_demo_panel(self._seed, return_col=return_col)
                input_mode = "deterministic_demo"
                LOGGER.warning("Input panel missing; generated deterministic demo data")
            else:
                raise FileNotFoundError(panel_path)
            _validate_temporal_contract(
                panel,
                require_metadata=bool(
                    _nested_get(self._config, "input.require_temporal_metadata", True)
                ),
                return_col=return_col,
                bar_interval=_nested_get(self._config, "evaluation.bar_interval", None),
            )
            panel, sample_filter_statistics = _prepare_eligibility_audit(panel, self._config)
            panel = _filter_sample(panel, self._config)
            sample_filter_statistics["selected_rows"] = int(len(panel))
            sample_filter_statistics["filtered_rows"] = (
                sample_filter_statistics["input_rows"]
                - sample_filter_statistics["selected_rows"]
            )
            market_type, allow_short = _resolve_market_contract(self._config, panel)
            if input_mode == "factor_and_evaluation_files":
                funding_qc = _validate_funding_contract(
                    panel,
                    market_type=market_type,
                    config=self._config,
                )
                qc_result["funding_contract"] = funding_qc
                if not funding_qc["passed"]:
                    raise ValueError(str(funding_qc["message"]))
            # Only construct perpetual returns when the panel doesn't already
            # carry the configured return column (e.g. panel was injected from
            # the pipeline which pre-computes forward returns from kline data).
            if return_col not in panel.columns or panel[return_col].isna().all():
                panel = construct_perpetual_returns(panel, market_type=market_type)
            else:
                LOGGER.info(
                    "Skipping perpetual-return construction: %s already present (%d non-null)",
                    return_col,
                    int(panel[return_col].notna().sum()),
                )
            panel.to_csv(output_dir / "input_panel_snapshot.csv", index=False)
            returns = _build_return_table(panel, return_col)
            winsorize_enabled = bool(
                _nested_get(self._config, "preprocessing.winsorize_enabled", False)
            )
            zscore_enabled = bool(
                _nested_get(self._config, "preprocessing.zscore_enabled", False)
            )
            winsorize_limits = _winsorize_limits(self._config)
            input_qc = qc_result
            qc_result = run_qc(
                panel,
                returns,
                factor_value_col=self._factor_col,
                return_value_col=return_col,
                winsorize_limits=winsorize_limits,
            )
            qc_result.update(input_qc)
            factor_timing = panel.drop(columns=["entry_ts", "exit_ts"], errors="ignore")
            qc_result["forward_bias"] = check_forward_bias(factor_timing, returns)
            if not qc_result["forward_bias"]["passed"]:
                raise ValueError("forward-bias check failed; see qc_result.json")
            panel = preprocess_factor_panel(
                panel,
                factor_col=self._factor_col,
                return_col=return_col,
                winsorize_enabled=winsorize_enabled,
                zscore_enabled=zscore_enabled,
                winsorize_limits=winsorize_limits,
            )
            effective_factor_col = "factor_value_eval"
            qc_result["preprocessing"] = {
                "winsorize_enabled": winsorize_enabled,
                "zscore_enabled": zscore_enabled,
                "winsorize_limits": winsorize_limits,
                "outlier_rows": int(panel["is_outlier"].sum()),
            }
            qc_result["status"] = "passed"
            _write_json(output_dir / "qc_result.json", qc_result)
            panel.to_csv(output_dir / "evaluated_panel.csv", index=False)
        except Exception as error:
            _record_task_failure(failures, "load_input", error)
            qc_result["status"] = "failed"
            qc_result["error"] = f"{type(error).__name__}: {error}"
            sample_filter_statistics = {
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
            _write_json(output_dir / "qc_result.json", qc_result)
            panel = pd.DataFrame()
            input_mode = "unavailable"

        # ── Evaluation tasks (each independently fault-tolerant) ──

        backtest_result = BacktestResult(
            assignments=pd.DataFrame(),
            decile_returns=pd.DataFrame(),
            long_short_returns=pd.DataFrame(),
            long_short_nav=pd.DataFrame(),
            turnover=pd.DataFrame(),
            logs=pd.DataFrame(),
        )
        ic_data = pd.DataFrame(columns=["timestamp", "factor_name", "ic", "n_assets"])
        rank_ic_data = pd.DataFrame(
            columns=["timestamp", "factor_name", "rank_ic", "n_assets"]
        )
        stability_data = pd.DataFrame()
        correlations: dict[str, pd.DataFrame] = {
            "pearson": pd.DataFrame(),
            "spearman": pd.DataFrame(),
        }
        regression_guard: dict[str, Any] = {"status": "not_run", "alerts": []}

        if not panel.empty:
            try:
                backtest_result = run_backtest(
                    panel,
                    return_col=return_col,
                    factor_col=effective_factor_col,
                    q=int(_nested_get(self._config, "evaluation.layers", 10)),  # noqa: RUF048
                    min_assets_per_layer=int(
                        _nested_get(self._config, "evaluation.min_assets_per_layer", 1)
                    ),
                    allow_short=allow_short,
                    fee_bps=_nested_get(self._config, "cost.fee_bps", 0.0),
                    slippage_bps=_nested_get(self._config, "cost.slippage_bps", 0.0),
                    output_dir=output_dir,
                )
                backtest_result.logs.to_csv(
                    output_dir / "layer_assignment_log.csv", index=False
                )
                LOGGER.info("Completed backtest task")
            except Exception as error:
                _record_task_failure(failures, "backtest", error)

            try:
                metric_panel = panel.copy()
                metric_panel["forward_return"] = metric_panel[return_col]
                ic_data = compute_ic(
                    metric_panel,
                    factor_col=effective_factor_col,
                    return_col="forward_return",
                    min_assets=int(_nested_get(self._config, "evaluation.min_assets", 5)),
                )
                rank_ic_data = compute_rank_ic(
                    metric_panel,
                    factor_col=effective_factor_col,
                    return_col="forward_return",
                    min_assets=int(_nested_get(self._config, "evaluation.min_assets", 5)),
                )
                ic_data.to_csv(output_dir / "ic_timeseries.csv", index=False)
                rank_ic_data.to_csv(output_dir / "rank_ic_timeseries.csv", index=False)
                LOGGER.info("Completed IC and RankIC task")
            except Exception as error:
                _record_task_failure(failures, "ic_metrics", error)

            try:
                stability_data = rolling_stability(
                    ic_data,
                    window_days=int(
                        _nested_get(self._config, "stability.window_days", 60)
                    ),
                    min_periods=int(
                        _nested_get(self._config, "stability.min_periods", 20)
                    ),
                )
                stability_data.to_csv(output_dir / "stability.csv", index=False)
                LOGGER.info("Completed stability task")
            except Exception as error:
                _record_task_failure(failures, "stability", error)

            try:
                correlations = compute_factor_correlations(
                    panel,
                    methods=("pearson", "spearman"),
                    value_col=effective_factor_col,
                    min_assets=int(
                        _nested_get(self._config, "correlation.min_assets", 5)
                    ),
                )
                correlations["pearson"].to_csv(output_dir / "corr_pearson.csv")
                correlations["spearman"].to_csv(output_dir / "corr_spearman.csv")
                LOGGER.info("Completed factor correlation task")
            except Exception as error:
                _record_task_failure(failures, "correlation", error)

        # Ensure all expected CSVs exist even when tasks were skipped.
        for filename, data in (
            ("stability.csv", stability_data),
            ("corr_pearson.csv", correlations["pearson"]),
            ("corr_spearman.csv", correlations["spearman"]),
        ):
            if not (output_dir / filename).exists():
                data.to_csv(output_dir / filename)

        try:
            generate_plots(
                ic_data,
                backtest_result.decile_returns,
                correlations,
                output_dir,
            )
            LOGGER.info("Completed plotting task")
        except Exception as error:
            _record_task_failure(failures, "plots", error)

        # ── Summaries ──────────────────────────────────────────────

        ic_summary: dict[str, Any] = {}
        if not ic_data.empty:
            for factor_name, group in ic_data.groupby("factor_name", sort=True):
                ic_summary[str(factor_name)] = compute_ic_ir(group)["ic_ir"]
        long_short_summary: dict[str, Any] = {}
        if not backtest_result.long_short_returns.empty:
            for factor_name, group in backtest_result.long_short_returns.groupby(
                "factor_name",
                sort=True,
            ):
                long_short_summary[str(factor_name)] = {
                    "gross_mean_return": _safe_float(group["gross_return"].mean()),
                    "net_mean_return": _safe_float(group["net_return"].mean()),
                    "gross_final_nav": _safe_float(
                        backtest_result.long_short_nav[
                            backtest_result.long_short_nav["factor_name"] == factor_name
                        ]["gross_nav"].iloc[-1]
                    ),
                    "net_final_nav": _safe_float(
                        backtest_result.long_short_nav[
                            backtest_result.long_short_nav["factor_name"] == factor_name
                        ]["net_nav"].iloc[-1]
                    ),
                }
        sample_statistics = {
            "input_mode": input_mode,
            "rows": int(len(panel)),
            "symbols": int(panel["symbol"].nunique()) if not panel.empty else 0,
            "factors": int(panel["factor_name"].nunique()) if not panel.empty else 0,
            "start": str(panel["timestamp"].min()) if not panel.empty else None,
            "end": str(panel["timestamp"].max()) if not panel.empty else None,
        }

        # ── Report ─────────────────────────────────────────────────

        methods = {
            "ic": "daily cross-sectional Pearson correlation",
            "rank_ic": "daily cross-sectional Spearman correlation",
            "stability": "UTC calendar-time rolling window",
            "correlation": "same-date cross-sectional mean matrix",
            "nav": "simple-return cumulative product",
            "preprocessing": "cross-sectional winsorize and optional z-score",
            "perpetual_return_contract": (
                "separate return labels require funding_included=true; "
                "legacy merged panels construct returns from close plus aligned "
                "funding"
            ),
        }
        parameters = {
            "random_seed": self._seed,
            "return_col": return_col,
            "factor_col": effective_factor_col,
            "layers": int(_nested_get(self._config, "evaluation.layers", 10)),
            "market_type": market_type,
            "exchange": _nested_get(self._config, "market.exchange", None),
            "settlement_asset": _nested_get(self._config, "market.settlement_asset", None),
            "allow_short": allow_short,
            "fee_bps": _nested_get(self._config, "cost.fee_bps", 0.0),
            "slippage_bps": _nested_get(self._config, "cost.slippage_bps", 0.0),
            "stability_window_days": int(
                _nested_get(self._config, "stability.window_days", 60)
            ),
            "run_date": self._resolved_date,
        }
        risks = [
            "未来函数：输入面板必须保证因子时点早于未来收益，主流程不会推断缺失的执行语义。",
            "demo 数据仅用于验收流程，不代表真实市场研究结论。",
            "幸存者偏差：动态标的池和上市/退市规则会造成样本选择偏差，当前面板不自动回溯历史成分。",
            "时区错位：bar close、下一根 bar open 和交易所事件时间若未统一，收益标签会错位。",
            "停牌/缺失：停牌、缺 bar 和异步数据可能让有效截面或未来收益不完整。",
            "极值污染：极端因子值会改变分层边界、IC_IR 与净值路径，需结合原始标记复核。",
            "样本稀疏：有效资产数不足时会跳过或降层，跨日结果不可直接视作同一组合。",
            "重复键和 merge 错配会造成多重计权；当前 QC 将其作为硬失败。",
            "流动性过滤会改变有效截面和分层收益，当前主流程依赖输入面板完成流动性筛选。",
            "永续双文件输入必须以 funding_included=true 审计收益标签；旧单文件输入"
            "使用 close 与按 bar 对齐的 signed funding_rate 构造收益。",
            "净收益只按换手近似扣除手续费和滑点，未覆盖容量、冲击成本和融资约束。",
            "滚动指标存在重叠窗口，统计量不可视为独立样本；不同 horizon 也存在收益重叠。",
            "极端行情下相关性、换手和分层净值可能失稳，应单独做压力区间检验。",
            "参数与 OOS：冻结区间必须在外部配置管理，不能用本报告结果反复调参。",
            "分位点重复时使用稳定 rank-cut，实际层数和成分应以 layer_assignment_log.csv 为准。",
        ]
        try:
            write_evaluation_report(
                output_path=output_dir / "evaluation_report.md",
                methods=methods,
                parameters=parameters,
                sample_statistics=sample_statistics,
                core_results={
                    "ic_ir_by_factor": ic_summary,
                    "long_short_by_factor": long_short_summary,
                    "qc_status": qc_result.get("status"),
                    "forward_bias_passed": qc_result.get("forward_bias", {}).get("passed"),
                    "funding_contract": qc_result.get("funding_contract"),
                    "input_merge": qc_result.get("input_merge"),
                },
                risks=risks,
                failed_tasks=failures,
                sample_filter_statistics=sample_filter_statistics,
            )
            LOGGER.info("Completed Markdown report task")
        except Exception as error:
            _record_task_failure(failures, "report", error)

        # ── Regression guard ───────────────────────────────────────

        if not failures:
            try:
                regression_guard = _run_regression_guard(
                    _regression_metrics(
                        ic_data,
                        rank_ic_data,
                        backtest_result.long_short_returns,
                        bar_interval=str(
                            _nested_get(self._config, "evaluation.bar_interval", "1d")
                        ),
                    ),
                    baseline_path=self._regression_baseline_path,
                    thresholds=self._regression_thresholds,
                )
                for alert in regression_guard["alerts"]:
                    LOGGER.warning(
                        "Regression guard alert: metric=%s baseline=%s current=%s "
                        "delta=%s threshold=%s",
                        alert["metric"],
                        alert["baseline"],
                        alert["current"],
                        alert["delta"],
                        alert["threshold"],
                    )
            except Exception as error:
                _record_task_failure(failures, "regression_guard", error)
                regression_guard = {
                    "status": "failed",
                    "alerts": [],
                    "error": f"{type(error).__name__}: {error}",
                }

        # ── Finalise manifest ──────────────────────────────────────

        pd.DataFrame(failures, columns=["task", "error"]).to_csv(
            output_dir / "failed_tasks.csv",
            index=False,
        )
        manifest = {
            "run_date": self._resolved_date,
            "random_seed": self._seed,
            "status": "success" if not failures else "partial_failure",
            "failed_tasks": failures,
            "output_dir": str(output_dir),
            "config_sha256": _sha256_file(output_dir / "resolved_config.yaml"),
            "input_snapshot_sha256": (
                _sha256_file(output_dir / "input_panel_snapshot.csv")
                if (output_dir / "input_panel_snapshot.csv").exists()
                else None
            ),
            "evaluated_panel_sha256": (
                _sha256_file(output_dir / "evaluated_panel.csv")
                if (output_dir / "evaluated_panel.csv").exists()
                else None
            ),
            "qc_result_sha256": _sha256_file(output_dir / "qc_result.json"),
            "experiment": self._governance,
            "params_hash": self._params_hash,
            "hashed_parameters": self._hashed_parameters,
            "governance_warnings": governance_warnings,
            "regression_guard": regression_guard,
        }
        _write_json(output_dir / "run_manifest.json", manifest)
        self.manifest = manifest
        LOGGER.info("Finished evaluation run: status=%s", manifest["status"])
        return output_dir


# ── Convenience function (backward-compatible with run_evaluation.py) ──


def run_evaluation(
    config_path: str | Path = "config/config.yaml",
    *,
    run_date: str | None = None,
    demo: bool = False,
    panel: pd.DataFrame | None = None,
) -> Path:
    """Run all evaluation tasks and return the output directory."""
    runner = ExperimentRunner(config_path, run_date=run_date, demo=demo, panel=panel)
    return runner.run()
