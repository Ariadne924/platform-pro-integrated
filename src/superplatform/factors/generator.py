"""Generate a standardized factor panel from cached historical market data."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from superplatform.data.provider_registry import DataProvider, DataProviderRegistry
from superplatform.data.schema import MarketType
from superplatform.data.snapshot import DataSnapshot
from superplatform.evaluation.forward_bias import ForwardBiasChecker, ForwardBiasReport
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.instances import instance_metadata
from superplatform.factors.metadata import FACTOR_METADATA, FactorMetadata
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import resolve_factor
from superplatform.runtime.config import Config
from superplatform.runtime.providers import default_provider_for

PANEL_COLUMNS = ["timestamp", "symbol", "factor_name", "factor_value"]
SKIP_COLUMNS = [
    "factor_name",
    "symbol",
    "status",
    "reason",
    "emitted_rows",
    "skipped_rows",
]


@dataclass(frozen=True)
class FactorGenerationResult:
    """Output locations and row counts for one cache-backed generation run."""

    panel_path: Path
    metadata_path: Path
    skipped_path: Path
    log_path: Path
    rows: int
    factors_generated: int
    factors_skipped: int
    snapshot_manifest_path: Path
    forward_bias_path: Path
    manifest_path: Path
    run_id: str
    snapshot_id: str


def _normalize_symbol_groups(raw_symbols: object) -> list[tuple[str, tuple[str, ...]]]:
    """Normalize single-symbol and grouped-symbol configuration."""
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ValueError("factor symbols must be a non-empty list")
    groups: list[tuple[str, tuple[str, ...]]] = []
    for item in raw_symbols:
        if isinstance(item, str):
            group = (item,)
        elif isinstance(item, list) and item and all(isinstance(s, str) for s in item):
            group = tuple(item)
        else:
            raise ValueError(
                "factor symbols must contain strings or non-empty lists of strings"
            )
        groups.append(("_".join(group), group))
    return groups


def _frequency_for(
    factor_name: str,
    factor_config: dict,
    data_type: str,
) -> str:
    """Resolve a data-type-specific native frequency."""
    frequencies = factor_config.get("frequencies", {})
    if not isinstance(frequencies, dict):
        raise ValueError(f"factors.{factor_name}.frequencies must be a mapping")
    value = frequencies.get(data_type, factor_config.get("frequency", "1d"))
    if not isinstance(value, str) or not value:
        raise ValueError(f"factors.{factor_name}.{data_type} frequency must be a string")
    return value


def _load_cached_klines(cache_path: Path, frequency: str) -> dict[str, pd.DataFrame]:
    """Retained only for the deprecated private legacy generator."""
    raise RuntimeError(
        "the legacy kline-only generator is unavailable; "
        "use generate_factor_panel_from_cache"
    )


def _validate_required_fields(
    data: dict[str, dict[str, pd.DataFrame]],
    metadata: FactorMetadata,
) -> str | None:
    """Return a reason when a declared source field is absent or entirely null."""
    for data_type, fields in metadata.required_fields.items():
        series = data.get(data_type, {})
        if not series:
            return f"missing_data_type:{data_type}"
        for symbol, frame in series.items():
            if frame.empty:
                return f"empty_snapshot_series:{data_type}:{symbol}"
            for field in fields:
                if field not in frame.columns:
                    return f"missing_{data_type}_field:{field}"
                if frame[field].isna().all():
                    return f"all_{data_type}_values_missing:{field}"
    return None


def _normalize_output(
    values: pd.DataFrame,
    *,
    factor_name: str,
    symbol: str,
) -> tuple[pd.DataFrame, int]:
    """Convert a factor result to the canonical panel without imputing values."""
    required = {"timestamp", "value"}
    missing = sorted(required.difference(values.columns))
    if missing:
        raise ValueError(f"factor result is missing columns: {missing}")

    result = values[["timestamp", "value"]].copy()
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    result["factor_value"] = pd.to_numeric(result["value"], errors="coerce")
    valid = result["timestamp"].notna() & np.isfinite(result["factor_value"])
    skipped_rows = int((~valid).sum())
    result = result.loc[valid, ["timestamp", "factor_value"]].copy()
    if result["timestamp"].duplicated().any():
        raise ValueError("factor result has duplicate timestamps")
    result = result.sort_values("timestamp", kind="stable").reset_index(drop=True)
    result["symbol"] = symbol
    result["factor_name"] = factor_name
    return result[PANEL_COLUMNS], skipped_rows


def _metadata_markdown(
    factor_names: Iterable[str],
    registry: FactorRegistry,
    generated_names: set[str],
    skipped_names: set[str],
    frequency: str,
) -> str:
    """Render formulas, defaults, fields, and data availability in Markdown."""
    lines = [
        "# Factor Metadata",
        "",
        "## Output Fields",
        "",
        "| Field | Meaning |",
        "| --- | --- |",
        "| timestamp | UTC timestamp of the completed input observation |",
        "| symbol | Exchange symbol used for the computation |",
        "| factor_name | Registered factor identifier |",
        "| factor_value | Numeric factor value; invalid or insufficient-history rows are omitted |",
        "",
        "## Generation Rules",
        "",
        (
            "- Source: local DuckDB unified data snapshot; the default "
            f"generation frequency is `{frequency}`."
        ),
        "- Each value uses the current completed bar and observations with timestamp no later than that bar.",
        "- Missing fields, unavailable input data, invalid values, and insufficient history are skipped and recorded in `skipped_factors.csv`.",
        "- No forward fill, backward fill, interpolation, future labels, or evaluation metrics are used.",
        "",
        "## Factor Definitions",
        "",
    ]
    for name in sorted(factor_names):
        factor = registry.get(name)
        metadata = FACTOR_METADATA.get(name)
        status = (
            "generated"
            if name in generated_names
            else "skipped"
            if name in skipped_names
            else "not_requested"
        )
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Category: `{factor.category.value}`",
                f"- Generation status: `{status}`",
            ]
        )
        if metadata is None:
            lines.extend(["- Definition: unavailable", ""])
            continue
        fields = "; ".join(
            f"{data_type}: {', '.join(columns)}"
            for data_type, columns in metadata.required_fields.items()
        )
        lines.extend(
            [
                f"- Formula: `{metadata.formula}`",
                f"- Default parameters: `{json.dumps(metadata.default_params, sort_keys=True)}`",
                f"- Required fields: `{fields}`",
                f"- Economic meaning: {metadata.economic_meaning}",
                "",
            ]
        )
    return "\n".join(lines)


def generate_factor_panel_from_cache_legacy(
    *,
    cache_path: str | Path = "data/cache.duckdb",
    output_dir: str | Path = "outputs/factors",
    frequency: str = "1d",
    factor_names: list[str] | None = None,
    config_path: str | Path = "config/factors.yaml",
) -> FactorGenerationResult:
    """Generate configured factor values from cached data without evaluation."""
    cache = Path(cache_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    panel_path = destination / "factor_panel.csv"
    metadata_path = destination / "factor_meta.md"
    skipped_path = destination / "skipped_factors.csv"
    log_path = destination / "factor_generation.log"

    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(config_file)
    configured_factors = Config.load(str(config_file)).get("factors", {})
    if not isinstance(configured_factors, dict):
        raise ValueError(f"{config_file}: factors must be a mapping")

    selected_names = factor_names or list(configured_factors)
    unknown = sorted(set(selected_names).difference(registry.list_all()))
    if unknown:
        raise KeyError(f"unknown factors requested: {unknown}")
    unconfigured = sorted(set(selected_names).difference(configured_factors))
    if unconfigured:
        raise ValueError(
            f"factors are not configured in {config_file}: {', '.join(unconfigured)}"
        )

    klines = _load_cached_klines(cache, frequency)
    if not klines:
        raise ValueError(f"no cached kline rows found at frequency {frequency!r}")

    rows: list[pd.DataFrame] = []
    skipped: list[dict[str, object]] = []
    generated_names: set[str] = set()
    skipped_names: set[str] = set()
    log_lines = [
        f"source={cache}",
        f"frequency={frequency}",
        f"symbols={','.join(sorted(klines))}",
    ]

    for name in selected_names:
        factor = registry.get(name)
        metadata = FACTOR_METADATA.get(name)
        factor_config = configured_factors[name]
        if not isinstance(factor_config, dict):
            raise ValueError(f"{config_file}: factors.{name} must be a mapping")
        if metadata is None:
            skipped_names.add(name)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": "",
                    "status": "skipped",
                    "reason": "metadata_missing",
                    "emitted_rows": 0,
                    "skipped_rows": 0,
                }
            )
            continue
        if factor.required_symbols != 1:
            skipped_names.add(name)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": "",
                    "status": "skipped",
                    "reason": "only_single_symbol_factors_supported",
                    "emitted_rows": 0,
                    "skipped_rows": 0,
                }
            )
            continue
        unavailable_data = sorted(set(factor.required_data).difference({"kline"}))
        if unavailable_data:
            skipped_names.add(name)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": "",
                    "status": "skipped",
                    "reason": f"unavailable_cached_data:{','.join(unavailable_data)}",
                    "emitted_rows": 0,
                    "skipped_rows": 0,
                }
            )
            log_lines.append(f"skip factor={name} reason=unavailable_cached_data")
            continue

        configured_symbols = factor_config.get("symbols")
        if (
            not isinstance(configured_symbols, list)
            or not configured_symbols
            or not all(isinstance(symbol, str) for symbol in configured_symbols)
        ):
            skipped_names.add(name)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": "",
                    "status": "skipped",
                    "reason": "invalid_configured_symbols",
                    "emitted_rows": 0,
                    "skipped_rows": 0,
                }
            )
            log_lines.append(f"skip factor={name} reason=invalid_configured_symbols")
            continue

        params = metadata.default_params | factor_config.get("params", {})
        factor_emitted = 0
        for symbol in configured_symbols:
            kline = klines.get(symbol)
            if kline is None:
                skipped.append(
                    {
                        "factor_name": name,
                        "symbol": symbol,
                        "status": "skipped",
                        "reason": "symbol_not_in_kline_cache",
                        "emitted_rows": 0,
                        "skipped_rows": 0,
                    }
                )
                log_lines.append(
                    f"skip factor={name} symbol={symbol} reason=symbol_not_in_kline_cache"
                )
                continue
            field_error = _validate_required_fields(kline, metadata)
            if field_error:
                skipped.append(
                    {
                        "factor_name": name,
                        "symbol": symbol,
                        "status": "skipped",
                        "reason": field_error,
                        "emitted_rows": 0,
                        "skipped_rows": len(kline),
                    }
                )
                log_lines.append(f"skip factor={name} symbol={symbol} reason={field_error}")
                continue
            try:
                values = factor.compute(
                    {"kline": {symbol: kline.copy(deep=True)}},
                    **params,
                ).values
                normalized, skipped_rows = _normalize_output(
                    values,
                    factor_name=name,
                    symbol=symbol,
                )
            except Exception as error:
                skipped.append(
                    {
                        "factor_name": name,
                        "symbol": symbol,
                        "status": "skipped",
                        "reason": f"compute_error:{type(error).__name__}:{error}",
                        "emitted_rows": 0,
                        "skipped_rows": len(kline),
                    }
                )
                log_lines.append(
                    f"skip factor={name} symbol={symbol} reason=compute_error:{type(error).__name__}"
                )
                continue

            rows.append(normalized)
            factor_emitted += len(normalized)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": symbol,
                    "status": "generated",
                    "reason": "ok",
                    "emitted_rows": len(normalized),
                    "skipped_rows": skipped_rows,
                }
            )
        if factor_emitted:
            generated_names.add(name)
            log_lines.append(f"generated factor={name} rows={factor_emitted}")
        else:
            skipped_names.add(name)

    panel = (
        pd.concat(rows, ignore_index=True)
        if rows
        else pd.DataFrame(columns=PANEL_COLUMNS)
    )
    if not panel.empty:
        panel = panel.sort_values(["timestamp", "symbol", "factor_name"]).reset_index(drop=True)
        if panel.duplicated(["timestamp", "symbol", "factor_name"]).any():
            raise ValueError("generated panel has duplicate timestamp/symbol/factor_name keys")
    panel.to_csv(panel_path, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    pd.DataFrame(skipped, columns=SKIP_COLUMNS).to_csv(skipped_path, index=False)
    metadata_path.write_text(
        _metadata_markdown(
            selected_names,
            registry,
            generated_names,
            skipped_names,
            frequency,
        ),
        encoding="utf-8",
    )
    log_lines.extend(
        [
            f"panel_rows={len(panel)}",
            f"factors_generated={len(generated_names)}",
            f"factors_skipped={len(skipped_names)}",
        ]
    )
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return FactorGenerationResult(
        panel_path=panel_path,
        metadata_path=metadata_path,
        skipped_path=skipped_path,
        log_path=log_path,
        rows=len(panel),
        factors_generated=len(generated_names),
        factors_skipped=len(skipped_names),
    )


def _write_json(path: Path, payload: object) -> None:
    """Persist deterministic UTF-8 JSON artifacts."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
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


def _bias_payload(
    report: ForwardBiasReport,
    *,
    factor_name: str,
    group_key: str,
) -> dict[str, object]:
    """Convert a factor-level truncation check to an audit-friendly record."""
    return {
        "factor_name": factor_name,
        "group_key": group_key,
        "passed": report.passed,
        "n_cutoffs": report.n_cutoffs,
        "n_mismatches": report.n_mismatches,
        "max_abs_diff": report.max_abs_diff,
        "details": report.details,
    }


class _StubProvider(DataProvider):
    """Read-only provider identity for offline cache-snapshot resolution.

    The generator never fetches from a provider — it reads the per-provider
    cache tables directly. A stub registry built from the cache's own
    ``provider_tables`` metadata gives ``default_provider_for`` the provider
    identities it needs to resolve which table each factor's data lives in.
    """

    def __init__(
        self,
        provider_id: str,
        data_type: str,
        exchange: str,
        market_type: MarketType | None,
    ) -> None:
        self.provider_id = provider_id
        self.data_type = data_type
        self.exchange = exchange
        self.market_type = market_type

    async def fetch(self, *args, **kwargs):
        raise NotImplementedError(
            "stub provider — the generator reads from the cache snapshot, "
            "never from a live source"
        )


def _stub_exchange(provider_id: str) -> str:
    """First '-' segment is the source family (e.g. binance-perp-kline → binance)."""
    return provider_id.split("-", 1)[0]


def _stub_market_type(provider_id: str) -> MarketType | None:
    """Derive market type from the provider id's perp/spot marker."""
    if "perp" in provider_id:
        return MarketType.PERPETUAL
    if "spot" in provider_id:
        return MarketType.SPOT
    return None


def _stub_registry_from_cache(
    cache_path: str | Path,
) -> DataProviderRegistry:
    """Build a provider registry from the cache's provider_tables metadata.

    Offline-safe: reads only the self-describing metadata table, so provider
    ids match the per-provider cache tables exactly. An injected registry
    (``provider_registry``) takes precedence over this stub.
    """
    reg = DataProviderRegistry()
    try:
        con = duckdb.connect(str(cache_path), read_only=True)
    except (duckdb.Error, OSError):
        return reg
    try:
        df = con.execute(
            "SELECT provider_id, data_type FROM provider_tables"
        ).fetchdf()
    except duckdb.Error:
        df = pd.DataFrame(columns=["provider_id", "data_type"])
    finally:
        con.close()
    for _, row in df.iterrows():
        pid = str(row["provider_id"])
        reg.register(_StubProvider(
            pid,
            str(row["data_type"]),
            _stub_exchange(pid),
            _stub_market_type(pid),
        ))
    return reg


def generate_factor_panel_from_cache(
    *,
    cache_path: str | Path = "data/cache.duckdb",
    output_dir: str | Path = "outputs/factors",
    frequency: str | None = None,
    factor_names: list[str] | None = None,
    config_path: str | Path = "config/factors.yaml",
    start: object = None,
    end: object = None,
    run_id: str | None = None,
    provider_registry: DataProviderRegistry | None = None,
) -> FactorGenerationResult:
    """Generate all configured factor types from one read-only data snapshot.

    The generator intentionally does not build future returns.  It only records
    the cache series used to compute each value, performs an in-process
    truncation check, and emits the canonical factor panel consumed by the
    evaluation pipeline.

    ``provider_registry`` (optional) provides the provider identities for
    ``default_provider_for`` resolution; when omitted, a stub registry is
    derived from the cache's own ``provider_tables`` metadata.
    """
    cache = Path(cache_path)
    provider_registry = provider_registry or _stub_registry_from_cache(cache)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    panel_path = destination / "factor_panel.csv"
    metadata_path = destination / "factor_meta.md"
    skipped_path = destination / "skipped_factors.csv"
    log_path = destination / "factor_generation.log"
    snapshot_manifest_path = destination / "snapshot_manifest.json"
    forward_bias_path = destination / "forward_bias.json"
    manifest_path = destination / "generation_manifest.json"

    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(config_file)
    config = Config.load(str(config_file))
    FactorInstanceRegistry.get_instance().build_from_config(config, registry)
    configured_factors = config.get("factors", {})
    if not isinstance(configured_factors, dict):
        raise ValueError(f"{config_file}: factors must be a mapping")
    configured_instances = config.get("factor_instances", {})
    if not isinstance(configured_instances, dict):
        raise ValueError(f"{config_file}: factor_instances must be a mapping")
    all_factor_configs = {**configured_factors, **configured_instances}

    generation_config = config.get("generation", {})
    if not isinstance(generation_config, dict):
        raise ValueError(f"{config_file}: generation must be a mapping")
    configured_timezone = str(generation_config.get("timezone", "UTC")).upper()
    if configured_timezone not in {"UTC", "ETC/UTC", "UTC+00:00"}:
        raise ValueError("generation.timezone must be UTC")
    if start is None:
        start = generation_config.get("start")
    if end is None:
        end = generation_config.get("end")
    default_frequency = frequency or generation_config.get("frequency", "1d")
    if not isinstance(default_frequency, str) or not default_frequency:
        raise ValueError(f"{config_file}: generation.frequency must be a non-empty string")
    frequency = default_frequency
    forward_bias_config = generation_config.get("forward_bias", {})
    if not isinstance(forward_bias_config, dict):
        raise ValueError(f"{config_file}: generation.forward_bias must be a mapping")
    n_cutoffs = int(forward_bias_config.get("n_cutoffs", 5))
    tolerance = float(forward_bias_config.get("tolerance", 1e-12))
    if n_cutoffs < 1:
        raise ValueError("generation.forward_bias.n_cutoffs must be at least 1")
    if tolerance < 0:
        raise ValueError("generation.forward_bias.tolerance must be non-negative")

    selected_names = factor_names or list(all_factor_configs)
    known = set(registry.list_all()) | set(FactorInstanceRegistry.get_instance().list_all())
    unknown = sorted(set(selected_names).difference(known))
    if unknown:
        raise KeyError(f"unknown factors requested: {unknown}")
    unconfigured = sorted(set(selected_names).difference(all_factor_configs))
    if unconfigured:
        raise ValueError(
            f"factors are not configured in {config_file}: {', '.join(unconfigured)}"
        )

    plans: list[dict[str, object]] = []
    requested_series: list[tuple[str, str, str]] = []
    factor_inputs: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    skipped_names: set[str] = set()
    for name in selected_names:
        factor = resolve_factor(name)
        metadata = FACTOR_METADATA.get(name) or instance_metadata(factor)
        factor_config = all_factor_configs[name]
        if not isinstance(factor_config, dict):
            raise ValueError(f"{config_file}: factors.{name} must be a mapping")
        if metadata is None:
            skipped_names.add(name)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": "",
                    "status": "skipped",
                    "reason": "metadata_missing",
                    "emitted_rows": 0,
                    "skipped_rows": 0,
                }
            )
            continue
        providers_cfg = factor_config.get("providers") or {}
        if not isinstance(providers_cfg, dict):
            raise ValueError(f"factors.{name}.providers must be a mapping")
        try:
            resolved_providers = {
                data_type: default_provider_for(
                    factor, data_type, config=config, registry=provider_registry,
                    factor_providers=providers_cfg,
                ).provider_id
                for data_type in factor.required_data
            }
        except ValueError as error:
            # The required provider isn't registered (e.g. its table is not in
            # this cache) — skip the factor instead of failing the whole run.
            skipped_names.add(name)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": "",
                    "status": "skipped",
                    "reason": f"generation_error:{type(error).__name__}:{error}",
                    "emitted_rows": 0,
                    "skipped_rows": 0,
                }
            )
            continue
        groups = _normalize_symbol_groups(factor_config.get("symbols"))
        for group_key, group in groups:
            if factor.required_symbols is not None and len(group) != factor.required_symbols:
                skipped_names.add(name)
                skipped.append(
                    {
                        "factor_name": name,
                        "symbol": group_key,
                        "status": "skipped",
                        "reason": (
                            f"required_symbols_mismatch:"
                            f"expected={factor.required_symbols}:actual={len(group)}"
                        ),
                        "emitted_rows": 0,
                        "skipped_rows": 0,
                    }
                )
                continue
            for data_type in factor.required_data:
                data_frequency = _frequency_for(name, factor_config, data_type)
                requested_series.extend(
                    (resolved_providers[data_type], symbol, data_frequency)
                    for symbol in group
                )
            evaluation_price: dict[str, object] | None = None
            if "kline" not in factor.required_data:
                raw_evaluation_price = factor_config.get("evaluation_price")
                if not isinstance(raw_evaluation_price, dict):
                    skipped_names.add(name)
                    skipped.append(
                        {
                            "factor_name": name,
                            "symbol": group_key,
                            "status": "skipped",
                            "reason": "evaluation_price_required_for_non_kline_factor",
                            "emitted_rows": 0,
                            "skipped_rows": 0,
                        }
                    )
                    continue
                evaluation_frequency = raw_evaluation_price.get("frequency", frequency)
                if not isinstance(evaluation_frequency, str) or not evaluation_frequency:
                    raise ValueError(
                        f"factors.{name}.evaluation_price.frequency must be a string"
                    )
                explicit_provider = raw_evaluation_price.get("provider")
                if isinstance(explicit_provider, str) and explicit_provider:
                    evaluation_price = {
                        "provider": explicit_provider,
                        "frequency": evaluation_frequency,
                    }
                else:
                    # The forward-return K-line source defaults to the resolved
                    # kline provider (defaults.exchange / defaults.market).
                    evaluation_price = {
                        "provider": default_provider_for(
                            factor, "kline", config=config,
                            registry=provider_registry,
                            factor_providers=providers_cfg,
                        ).provider_id,
                        "frequency": evaluation_frequency,
                    }
                requested_series.extend(
                    (evaluation_price["provider"], symbol, evaluation_frequency)
                    for symbol in group
                )
            raw_params = factor_config.get("params", {})
            if not isinstance(raw_params, dict):
                raise ValueError(f"factors.{name}.params must be a mapping")
            params = metadata.default_params | raw_params
            plans.append(
                {
                    "factor_name": name,
                    "factor": factor,
                    "metadata": metadata,
                    "group_key": group_key,
                    "group": group,
                    "params": params,
                    "providers": resolved_providers,
                    "evaluation_price": evaluation_price,
                }
            )
            factor_inputs.append(
                {
                    "factor_name": name,
                    "group_key": group_key,
                    "symbols": list(group),
                    "inputs": [
                        {
                            "data_type": data_type,
                            "provider": resolved_providers[data_type],
                            "frequency": _frequency_for(
                                name,
                                factor_config,
                                data_type,
                            ),
                        }
                        for data_type in factor.required_data
                    ],
                    "evaluation_price": evaluation_price,
                    "params": params,
                }
            )

    generated_names: set[str] = set()
    rows: list[pd.DataFrame] = []
    bias_reports: list[dict[str, object]] = []
    log_lines = [
        f"source={cache}",
        f"default_frequency={frequency}",
        f"start={start}",
        f"end={end}",
        f"requested_factors={','.join(selected_names)}",
    ]

    with DataSnapshot(cache) as snapshot:
        snapshot_id, snapshot_manifest = snapshot.describe(
            requested_series,
            start=start,
            end=end,
        )
        snapshot_manifest["requested_factor_names"] = sorted(selected_names)
        snapshot_manifest["available_data_types"] = snapshot.available_data_types()
        snapshot_manifest["factor_inputs"] = factor_inputs
        _write_json(snapshot_manifest_path, snapshot_manifest)

        for plan in plans:
            name = str(plan["factor_name"])
            factor = plan["factor"]
            metadata = plan["metadata"]
            group_key = str(plan["group_key"])
            group = plan["group"]
            params = plan["params"]
            assert isinstance(group, tuple)
            assert isinstance(metadata, FactorMetadata)

            data: dict[str, dict[str, pd.DataFrame]] = {}
            try:
                for data_type in factor.required_data:
                    data_frequency = _frequency_for(
                        name,
                        all_factor_configs[name],
                        data_type,
                    )
                    data[data_type] = {
                        symbol: snapshot.load(
                            plan["providers"][data_type],
                            symbol,
                            data_frequency,
                            start=start,
                            end=end,
                        )
                        for symbol in group
                    }
                field_error = _validate_required_fields(data, metadata)
                if field_error:
                    raise ValueError(field_error)

                # Verify the price source exists for non-kline factors without
                # exposing it to factor implementations.
                evaluation_price = plan["evaluation_price"]
                if evaluation_price is not None:
                    price_frequency = str(evaluation_price["frequency"])
                    price_provider = str(evaluation_price["provider"])
                    for symbol in group:
                        price = snapshot.load(
                            price_provider,
                            symbol,
                            price_frequency,
                            start=start,
                            end=end,
                        )
                        if price.empty:
                            raise ValueError(
                                f"empty_evaluation_price:{symbol}:{price_frequency}"
                            )

                values = factor.compute(data, **params).values
                normalized, skipped_rows = _normalize_output(
                    values,
                    factor_name=name,
                    symbol=group_key,
                )

                reference_data = data[factor.required_data[0]][group[0]]
                if reference_data["timestamp"].nunique() < n_cutoffs + 2:
                    report = ForwardBiasReport(
                        factor_name=f"{name}/{group_key}",
                        passed=True,
                        n_cutoffs=n_cutoffs,
                        n_mismatches=0,
                        max_abs_diff=0.0,
                        details=[
                            {
                                "note": (
                                    "skipped: insufficient data for forward-bias "
                                    "truncation check"
                                )
                            }
                        ],
                    )
                else:
                    def compute_truncated(
                        reference: pd.DataFrame,
                        *,
                        original_data=data,
                        factor_impl=factor,
                        factor_params=params,
                    ) -> pd.DataFrame:
                        cutoff = reference["timestamp"].max()
                        truncated_data = {
                            data_type: {
                                symbol: frame.loc[
                                    frame["timestamp"].le(cutoff)
                                ].copy()
                                for symbol, frame in per_symbol.items()
                            }
                            for data_type, per_symbol in original_data.items()
                        }
                        return factor_impl.compute(
                            truncated_data,
                            **factor_params,
                        ).values

                    report = ForwardBiasChecker(
                        n_cutoffs=n_cutoffs,
                        tolerance=tolerance,
                    ).check(
                        factor_name=f"{name}/{group_key}",
                        compute_fn=compute_truncated,
                        data=reference_data,
                        # ``values`` above is the full-sample computation the
                        # checker would otherwise redo as its baseline.
                        baseline=values,
                    )
                bias_reports.append(
                    _bias_payload(
                        report,
                        factor_name=name,
                        group_key=group_key,
                    )
                )
                if not report.passed:
                    raise ValueError("forward_bias_check_failed")

            except Exception as error:
                skipped_names.add(name)
                skipped.append(
                    {
                        "factor_name": name,
                        "symbol": group_key,
                        "status": "skipped",
                        "reason": f"generation_error:{type(error).__name__}:{error}",
                        "emitted_rows": 0,
                        "skipped_rows": 0,
                    }
                )
                log_lines.append(
                    f"skip factor={name} group={group_key} "
                    f"reason={type(error).__name__}:{error}"
                )
                continue

            rows.append(normalized)
            generated_names.add(name)
            skipped.append(
                {
                    "factor_name": name,
                    "symbol": group_key,
                    "status": "generated",
                    "reason": "ok",
                    "emitted_rows": len(normalized),
                    "skipped_rows": skipped_rows,
                }
            )
            log_lines.append(
                f"generated factor={name} group={group_key} rows={len(normalized)}"
            )

    panel = (
        pd.concat(rows, ignore_index=True)
        if rows
        else pd.DataFrame(columns=PANEL_COLUMNS)
    )
    if not panel.empty:
        panel = panel.sort_values(
            ["timestamp", "symbol", "factor_name"],
            kind="stable",
        ).reset_index(drop=True)
        if panel.duplicated(["timestamp", "symbol", "factor_name"]).any():
            raise ValueError("generated panel has duplicate timestamp/symbol/factor_name keys")
    panel.to_csv(panel_path, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    pd.DataFrame(skipped, columns=SKIP_COLUMNS).to_csv(skipped_path, index=False)
    _write_json(forward_bias_path, {"reports": bias_reports})
    metadata_path.write_text(
        _metadata_markdown(
            selected_names,
            registry,
            generated_names,
            skipped_names,
            frequency,
        ),
        encoding="utf-8",
    )

    resolved_run_id = run_id or (
        "factor-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + f"-{snapshot_id[:8]}"
    )
    manifest = {
        "run_id": resolved_run_id,
        "snapshot_id": snapshot_id,
        "status": "success",
        "timezone": "UTC",
        "source_cache": str(cache),
        "config_path": str(config_file),
        "config_sha256": _sha256_file(config_file),
        "snapshot_manifest_sha256": _sha256_file(snapshot_manifest_path),
        "default_frequency": frequency,
        "start": start,
        "end": end,
        "factors_requested": selected_names,
        "factors_generated": sorted(generated_names),
        "factors_skipped": sorted(skipped_names),
        "panel_rows": int(len(panel)),
        "forward_bias_passed": all(report["passed"] for report in bias_reports),
        "artifacts": {
            "panel": str(panel_path),
            "metadata": str(metadata_path),
            "skipped": str(skipped_path),
            "snapshot_manifest": str(snapshot_manifest_path),
            "forward_bias": str(forward_bias_path),
        },
    }
    _write_json(manifest_path, manifest)
    log_lines.extend(
        [
            f"run_id={resolved_run_id}",
            f"snapshot_id={snapshot_id}",
            f"panel_rows={len(panel)}",
            f"factors_generated={len(generated_names)}",
            f"factors_skipped={len(skipped_names)}",
        ]
    )
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    return FactorGenerationResult(
        panel_path=panel_path,
        metadata_path=metadata_path,
        skipped_path=skipped_path,
        log_path=log_path,
        rows=len(panel),
        factors_generated=len(generated_names),
        factors_skipped=len(skipped_names),
        snapshot_manifest_path=snapshot_manifest_path,
        forward_bias_path=forward_bias_path,
        manifest_path=manifest_path,
        run_id=resolved_run_id,
        snapshot_id=snapshot_id,
    )
