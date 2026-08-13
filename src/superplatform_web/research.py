"""Factor research orchestration shared by Web API endpoints."""

import copy
import json
import math
import time
from typing import Any

import pandas as pd

from superplatform.data.enums import DataFrequency
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import factor_entry, resolve_factor
from superplatform.runtime.config import Config
from superplatform.runtime.pipeline import OfflineRuntime, PipelineResult, ProgressFn
from superplatform.runtime.providers import default_provider_for
from superplatform.utils.logging import logger
from superplatform_web.evaluation_cache import EvaluationResultCache
from superplatform_web.factor_params import normalize_factor_params, parameter_hash, stable_hash
from superplatform_web.state import (
    default_exchange,
    default_market,
    disabled_provider_ids,
    resolve_provider_for_data_type,
)


def _declared_frequencies(provider) -> set[DataFrequency]:
    """Native cadences a provider serves; all DataFrequency members when unset."""
    declared = getattr(provider, "available_frequencies", None)
    return set(declared) if declared else set(DataFrequency)


def _provider_for_data_type(
    providers,
    exchange: str,
    market: str,
    data_type: str,
    resolved_providers: dict | None,
    base_config: Config | None = None,
):
    """Resolve the provider serving ``data_type`` for ``(exchange, market)``.

    ``resolved_providers`` is a data_type→provider_id map built by the run
    callers; otherwise the provider is resolved from the registry. A global
    ``defaults.providers.<data_type>`` override (layer ②) wins over the
    registry derivation. Returns None when no provider serves the data type.
    """
    if resolved_providers is not None:
        provider_id = resolved_providers.get(data_type)
    else:
        overrides = (
            (base_config.get("defaults.providers") if base_config is not None else None)
            or {}
        )
        override_pid = overrides.get(data_type) if isinstance(overrides, dict) else None
        if override_pid:
            provider_id = override_pid
        else:
            try:
                provider_id = resolve_provider_for_data_type(
                    exchange, market, data_type, providers
                )
            except ValueError:
                return None
    if provider_id is None:
        return None
    try:
        return providers.get(provider_id)
    except KeyError:
        return None


def factor_available_frequencies(
    factor,
    providers,
    exchange: str,
    market: str,
    base_config: Config,
    resolved_providers: dict | None = None,
) -> list[str]:
    """Native cadences at which a factor can be evaluated (finest first).

    A factor is available at cadence X iff every required data type and — for
    non-kline factors — the evaluation-price kline source can be served
    natively at X. The evaluation cadence is exactly the intersection of the
    providers' ``available_frequencies``; no resampling is assumed. An
    unresolvable required data type yields [].
    """
    common = set(DataFrequency)
    for data_type in factor.required_data:
        provider = _provider_for_data_type(
            providers, exchange, market, data_type, resolved_providers,
            base_config=base_config,
        )
        if provider is None:
            return []
        common &= _declared_frequencies(provider)
    if "kline" not in factor.required_data:
        # Non-price factors still need a kline source for forward returns.
        eval_price = factor_entry(base_config, factor.name).get("evaluation_price", {})
        ep_provider_id = eval_price.get("provider") if isinstance(eval_price, dict) else None
        if ep_provider_id:
            try:
                provider = providers.get(ep_provider_id)
            except KeyError:
                return []
            if provider is None:
                return []
        else:
            provider = _provider_for_data_type(
                providers, exchange, market, "kline", None, base_config=base_config
            )
            if provider is None:
                return []
        common &= _declared_frequencies(provider)
    return [f.value for f in DataFrequency if f in common]


def _apply_run_cadence(factor_config: dict, frequency: str) -> None:
    """Pin a per-factor config dict to the run-level evaluation cadence.

    ``frequency``/``evaluation_price.frequency`` become the evaluation cadence
    and the per-data-type ``frequencies`` override is dropped so every input —
    not just the evaluation price — is fetched natively at the run cadence.
    """
    factor_config["frequency"] = frequency
    factor_config.pop("frequencies", None)
    ep = factor_config.get("evaluation_price")
    if isinstance(ep, dict):
        ep["frequency"] = frequency


def _validate_run_cadence(
    factor,
    frequency: str,
    providers,
    exchange: str,
    market: str,
    base_config: Config,
    resolved_providers: dict | None,
) -> None:
    """Raise when the run cadence is not natively supported by a factor."""
    available = factor_available_frequencies(
        factor, providers, exchange, market, base_config, resolved_providers
    )
    if frequency not in available:
        supported = "、".join(available) if available else "无（缺少数据源）"
        raise ValueError(
            f"因子 {factor.name} 在频率 {frequency} 下不可用"
            f"（原生数据仅支持：{supported}）。请选择其他频率，或移除该因子。"
        )


async def _run_factor(
    *,
    base_config: Config,
    providers,
    factor_name: str,
    symbols: list[str],
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    params: dict | None = None,
    param_units: dict[str, str | None] | None = None,
    frequency: str | None = None,
) -> PipelineResult:
    """Run one factor's full evaluation pipeline and return the raw result.

    Shared orchestration between ``evaluate_factor`` (metrics) and
    ``factor_series`` (per-symbol factor + kline overlay): resolve the factor,
    pin its data config to the requested window/cadence, and run the runtime.
    """
    data, effective_params = _prepare_factor_run(
        base_config=base_config,
        providers=providers,
        factor_name=factor_name,
        symbols=symbols,
        start=start,
        end=end,
        exchange=exchange,
        market=market,
        params=params,
        param_units=param_units,
        frequency=frequency,
    )
    logger.info(f"factor={factor_name} effective_params={effective_params}")
    return await _execute_factor(data, providers, factor_name)


def _prepare_factor_run(
    *,
    base_config: Config,
    providers,
    factor_name: str,
    symbols: list[str],
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    params: dict | None = None,
    param_units: dict[str, str | None] | None = None,
    frequency: str | None = None,
) -> tuple[dict, dict]:
    """Build the isolated runtime config and normalized factor parameters."""
    exchange = exchange or default_exchange()
    market = market or default_market()

    factor = resolve_factor(factor_name)
    provider_map = {
        data_type: default_provider_for(
            factor, data_type, config=base_config, registry=providers,
            disabled=disabled_provider_ids(),
        ).provider_id
        for data_type in factor.required_data
    }

    data = copy.deepcopy(base_config.to_dict())
    factor_config = data.setdefault("factors", {}).setdefault(factor_name, {})
    factor_config.update(factor_entry(base_config, factor_name))
    effective_params = normalize_factor_params(
        factor,
        factor_config.get("params"),
        params,
        param_units,
    )
    factor_config.update({
        "symbols": symbols,
        "providers": provider_map,
        "start": start,
        "end": end,
        "params": effective_params,
    })
    if frequency is not None:
        _validate_run_cadence(factor, frequency, providers, exchange, market, base_config, provider_map)
        _apply_run_cadence(factor_config, frequency)
    return data, effective_params


async def _execute_factor(data: dict, providers, factor_name: str) -> PipelineResult:
    """Execute an already-normalized one-factor runtime configuration."""
    results = await OfflineRuntime(Config(data), providers).run(
        [factor_name], skip_report=True
    )
    return results[0]


async def evaluate_factor(
    *,
    base_config: Config,
    providers,
    factor_name: str,
    symbols: list[str],
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    params: dict | None = None,
    param_units: dict[str, str | None] | None = None,
    frequency: str | None = None,
    result_cache: EvaluationResultCache | None = None,
) -> dict:
    """Evaluate one factor with an isolated, explicit data configuration.

    Providers are resolved from ``(exchange, market)`` using the configured
    defaults when not specified.  ``params`` deep-merges over the factor's
    config-default parameters.  ``frequency`` pins the evaluation cadence when
    given (overriding the factor's config); otherwise the factor keeps its own
    config cadence.

    Returns a dict with a ``warning`` key when the symbol count is too low
    for meaningful cross-sectional metrics (IC, layers, turnover). The
    evaluation still runs so time-series factor values are returned.
    """
    data, effective_params = _prepare_factor_run(
        base_config=base_config,
        providers=providers,
        factor_name=factor_name,
        symbols=symbols,
        start=start,
        end=end,
        exchange=exchange,
        market=market,
        params=params,
        param_units=param_units,
        frequency=frequency,
    )
    params_hash = parameter_hash(effective_params)
    evaluation_identity = {
        "symbols": symbols,
        "start": start,
        "end": end,
        "frequency": data["factors"][factor_name].get("frequency"),
        "config": data,
    }
    request_hash = stable_hash(evaluation_identity)
    cache_key = f"{factor_name}:params={params_hash}:request={request_hash}"
    run_id = f"factor-{factor_name}-{params_hash[:12]}-{stable_hash(cache_key)[:8]}"

    if result_cache is not None:
        cached = result_cache.get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

    logger.info(
        f"factor={factor_name} effective_params={effective_params} "
        f"params_hash={params_hash} run_id={run_id}"
    )
    result = await _execute_factor(data, providers, factor_name)
    min_symbols = base_config.get("evaluation.ic.min_stocks_per_period", 2)
    output = _json_safe(serialize_evaluation(result))
    output.update({
        "run_id": run_id,
        "params_hash": params_hash,
        "effective_params": effective_params,
        "cache_hit": False,
    })
    if len(symbols) < min_symbols:
        output["warning"] = (
            f"标的数量 ({len(symbols)}) 少于截面分析最低要求 ({min_symbols})。"
            "IC、分层收益、换手率等截面指标已跳过。因子值本身不受影响。"
        )
    if result_cache is not None:
        result_cache.put(cache_key, output)
    return output


async def factor_series(
    *,
    base_config: Config,
    providers,
    factor_name: str,
    symbol: str,
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    params: dict | None = None,
    param_units: dict[str, str | None] | None = None,
    frequency: str | None = None,
) -> dict:
    """Per-symbol factor value + kline series for the K线 + 因子值 overlay chart.

    Runs the evaluation pipeline for a single symbol (cheap: one symbol's data,
    served from cache) and slices the aligned ``cross_section`` — the same
    fetch the metrics come from, so factor values and candles share cadence and
    timestamps. Each row is ``{timestamp, open, high, low, close,
    factor_value}``; NaN warm-up values serialize to ``null``.
    """
    try:
        result = await _run_factor(
            base_config=base_config,
            providers=providers,
            factor_name=factor_name,
            symbols=[symbol],
            start=start,
            end=end,
            exchange=exchange,
            market=market,
            params=params,
            param_units=param_units,
            frequency=frequency,
        )
    except ValueError as error:
        # A symbol with no kline at all in the window (unknown / delisted)
        # makes the pipeline raise "No evaluation data produced". Surface it
        # as an empty series instead of a 422 so the chart can show its empty
        # state; any other ValueError (e.g. a bad cadence) still propagates.
        if "No evaluation data produced" in str(error):
            return {"factor_name": factor_name, "symbol": symbol, "rows": []}
        raise
    cs = result.cross_section
    if cs.empty:
        return {"factor_name": factor_name, "symbol": symbol, "rows": []}
    sub = cs[cs["symbol"] == symbol].sort_values("timestamp")
    cols = ["timestamp"]
    cols += [c for c in ("open", "high", "low", "close") if c in sub.columns]
    cols += ["factor_value"]
    return {
        "factor_name": factor_name,
        "symbol": symbol,
        "rows": _df_to_records(sub[cols]),
    }


async def _run_batch(
    *,
    base_config: Config,
    providers,
    factor_names: list[str],
    symbols: list[str],
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    frequency: str | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list, OfflineRuntime]:
    """Shared multi-factor run: returns (PipelineResults, the runtime).

    The evaluation cadence is a run-level setting: every factor is pinned to
    ``frequency`` (defaulting to "1d"), so all factors in one batch share a
    native evaluation cadence and the stage-3 mixed-frequency conflict cannot
    arise. ``_validate_run_cadence`` rejects a factor that cannot natively
    serve the chosen cadence.
    """
    exchange = exchange or default_exchange()
    market = market or default_market()
    effective_frequency = frequency or DataFrequency.D1.value

    factor_registry = FactorRegistry.get_instance()
    data = copy.deepcopy(base_config.to_dict())

    for factor_name in factor_names:
        factor = resolve_factor(factor_name, factory_registry=factor_registry)
        provider_map = {
            data_type: default_provider_for(
                factor, data_type, config=base_config, registry=providers,
                disabled=disabled_provider_ids(),
            ).provider_id
            for data_type in factor.required_data
        }
        factor_config = data.setdefault("factors", {}).setdefault(factor_name, {})
        factor_config.update(factor_entry(base_config, factor_name))
        factor_config.update({
            "symbols": symbols,
            "providers": provider_map,
            "start": start,
            "end": end,
        })
        _validate_run_cadence(
            factor, effective_frequency, providers, exchange, market, base_config, provider_map
        )
        _apply_run_cadence(factor_config, effective_frequency)

    runtime = OfflineRuntime(Config(data), providers, progress=progress)
    results = await runtime.run(factor_names, skip_report=True)
    return results, runtime


async def batch_evaluate(
    *,
    base_config: Config,
    providers,
    factor_names: list[str],
    symbols: list[str],
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    frequency: str | None = None,
    progress: ProgressFn | None = None,
) -> dict:
    """Evaluate several factors in one run, returning results + correlation."""
    def _emit(event: dict) -> None:
        if progress is not None:
            progress(event)

    results, runtime = await _run_batch(
        base_config=base_config,
        providers=providers,
        factor_names=factor_names,
        symbols=symbols,
        start=start,
        end=end,
        exchange=exchange,
        market=market,
        frequency=frequency,
        progress=progress,
    )

    correlation = runtime.correlation_matrix
    corr_payload = None
    if correlation is not None and not correlation.empty:
        labels = list(correlation.columns)
        corr_payload = {
            "labels": labels,
            "matrix": [
                [float(correlation.loc[a, b]) for b in labels]
                for a in labels
            ],
        }

    serialized = []
    for r in results:
        _emit({"kind": "serialize", "factor": r.factor_name})
        serialized.append(serialize_evaluation(r))
    _emit({"kind": "batch_done"})

    return _json_safe({
        "results": serialized,
        "correlation": corr_payload,
    })


# ── Factory parameter sweep (one shared-fetch run across the grid) ────


async def run_factory_sweep(
    *,
    base_config: Config,
    providers,
    factory_name: str,
    symbols: list[str],
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    frequency: str | None = None,
    combos: list[dict[str, Any]],
) -> dict:
    """Evaluate one factory across a parameter grid in a single shared-fetch pass.

    ``OfflineRuntime.evaluate_grid`` fetches the factory's data and builds the
    price frame once, then per combo computes the factor values + IC stats —
    skipping the per-evaluation pipeline overhead (fetch setup, thread pools,
    validation) that a temp-instance + ``run()`` approach pays N times.
    """
    factory = resolve_factor(factory_name, factory_registry=FactorRegistry.get_instance())
    # Validate every combo against the factory's schema up front so a bad grid
    # point fails fast instead of mid-run.
    normalized = [normalize_factor_params(factory, None, p, None) for p in combos]

    runtime = OfflineRuntime(Config(base_config.to_dict()), providers)
    started = time.perf_counter()
    results = await runtime.evaluate_grid(
        factor_name=factory_name,
        combos=normalized,
        symbols=symbols,
        sample_start=start,
        sample_end=end,
        frequency=frequency,
    )
    elapsed = time.perf_counter() - started

    n = len(combos)
    return {
        "param_names": sorted(set().union(*[p.keys() for p in combos])) if combos else [],
        "results": results,
        "combos": n,
        "elapsed_ms": round(elapsed * 1000, 1),
        "ms_per_combo": round(elapsed * 1000 / n, 1) if n else 0.0,
    }


async def build_batch_panel(
    *,
    base_config: Config,
    providers,
    factor_names: list[str],
    symbols: list[str],
    start: str,
    end: str,
    exchange: str | None = None,
    market: str | None = None,
    frequency: str | None = None,
) -> pd.DataFrame:
    """Evaluate several factors and return their combined evaluation panel.

    Each ``PipelineResult.cross_section`` is already a stage-3-compatible panel
    (timestamp/symbol/factor_value/ret_1..20/entry/exit/available_ts/market
    metadata), so concatenating them yields the input the deliverable pipeline
    consumes. Timestamps are normalised to tz-aware UTC and rows sorted for a
    deterministic export.
    """
    results, _ = await _run_batch(
        base_config=base_config,
        providers=providers,
        factor_names=factor_names,
        symbols=symbols,
        start=start,
        end=end,
        exchange=exchange,
        market=market,
        frequency=frequency,
    )
    frames = [
        r.cross_section
        for r in results
        if r.cross_section is not None and not r.cross_section.empty
    ]
    if not frames:
        raise ValueError("批量评估未产出因子值面板数据")
    panel = pd.concat(frames, ignore_index=True)
    for col in ("timestamp", "available_ts", "entry_ts", "exit_ts"):
        if col in panel.columns:
            panel[col] = pd.to_datetime(panel[col], utc=True)
    return panel.sort_values(["timestamp", "factor_name", "symbol"]).reset_index(drop=True)


def normalize_for_experiment(
    panel: pd.DataFrame, stage3_config: dict, bar_interval: str | None = None
) -> pd.DataFrame:
    """Stamp the deliverable config's market / horizon onto a web-built panel.

    The web panel is built from the web's own defaults (``defaults.exchange`` /
    ``defaults.market``), while ``ExperimentRunner`` filters and validates
    against ``config/config.yaml`` — its ``market`` section and the temporal
    contract. Stamp those values so the pipeline sees a self-consistent panel:
    the market columns are overwritten to match the config, and ``exit_ts`` is
    recomputed as ``entry_ts + return_col × bar_interval`` (the pipeline's
    cross-section builder hardcodes a 1-bar horizon). ``bar_interval``
    overrides the stage-3 config's cadence so the recomputed exit_ts matches
    the run-level evaluation cadence the panel was built at.
    """
    panel = panel.copy()

    market_cfg = stage3_config.get("market") or {}
    exchange = market_cfg.get("exchange")
    market_type = market_cfg.get("market_type")
    settlement_asset = market_cfg.get("settlement_asset")
    if exchange:
        panel["exchange"] = exchange
    if market_type:
        panel["market_type"] = market_type
    if settlement_asset:
        panel["settlement_asset"] = settlement_asset
    if market_type == "perpetual" and "funding_included" not in panel.columns:
        panel["funding_included"] = "funding_rate" in panel.columns

    eval_cfg = stage3_config.get("evaluation") or {}
    return_col = eval_cfg.get("return_col", "ret_1")
    bar_interval = bar_interval if bar_interval is not None else eval_cfg.get("bar_interval")
    if return_col and bar_interval and {"entry_ts", "exit_ts"}.issubset(panel.columns):
        try:
            horizon = int(str(return_col).removeprefix("ret_"))
        except ValueError:
            horizon = 1
        normalized_interval = (
            f"{bar_interval[:-1]}D"
            if str(bar_interval).lower().endswith("d")
            else bar_interval
        )
        interval = pd.Timedelta(normalized_interval)
        panel["exit_ts"] = pd.to_datetime(panel["entry_ts"], utc=True) + interval * horizon

    for col in ("timestamp", "available_ts", "entry_ts", "exit_ts"):
        if col in panel.columns:
            panel[col] = pd.to_datetime(panel[col], utc=True)

    # eligibility_reason is left to ``_prepare_eligibility_audit``, which builds
    # its own source tag from the (present) is_eligible column.
    _validate_panel_return_table(panel, eval_cfg.get("return_col", "ret_1"))
    return panel


def _validate_panel_return_table(panel: pd.DataFrame, return_col: str) -> None:
    """Mirror ExperimentRunner's return-table invariant so we fail fast with a clear message.

    The deliverable requires every (timestamp, symbol) to map to a single realized
    return and execution window. Mixing factors whose K-lines are fetched at
    different frequencies (e.g. 4h funding vs 1d basis) breaks this — at a shared
    timestamp the two rows carry different ret_1 values and the pipeline rejects
    the panel with an opaque error. Detect it here instead.
    """
    if return_col not in panel.columns:
        return
    value_columns = [return_col]
    value_columns.extend(col for col in ("entry_ts", "exit_ts") if col in panel.columns)
    grouped = panel.groupby(["timestamp", "symbol"], dropna=False)[value_columns]
    if not grouped.nunique(dropna=False).gt(1).any(axis=1).any():
        return
    freqs = (
        "、".join(sorted({str(v) for v in panel["frequency"].dropna().unique()}))
        if "frequency" in panel.columns
        else ""
    )
    suffix = f"（当前面板含频率：{freqs}）" if freqs else ""
    raise ValueError(
        "所选因子在相同（时间, 标的）上的前瞻收益不一致" + suffix
        + "。评估管线要求所有因子用同一频率的 K 线计算收益，"
        "请只选择评估频率一致的因子（如同为 4h 或同为 1d）。"
    )


def _json_safe(value):
    """Recursively map non-finite floats to None so the payload is JSON-safe.

    NaN/Inf can leak in from computed statistics (e.g. a forward-bias cutoff
    bucket with no comparable data yields ``max_abs_diff == nan``). JSON has no
    representation for them and starlette encodes responses with
    ``allow_nan=False``, so they must become ``null`` at the serialization
    boundary.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def serialize_evaluation(result) -> dict:
    """Return a stable JSON representation of one factor evaluation."""
    return {
        "factor_name": result.factor_name,
        "ic_stats": {
            "icir": result.ic_stats.get("icir"),
            "mean_ic": result.ic_stats.get("mean_ic"),
            "std_ic": result.ic_stats.get("std_ic"),
            "ic_positive_ratio": result.ic_stats.get("ic_positive_ratio"),
            "t_stat": result.ic_stats.get("ic_ir_tstat"),
        },
        "rank_ic_stats": {
            "rank_icir": result.rank_ic_stats.get("icir"),
            "mean_rank_ic": result.rank_ic_stats.get("mean_ic"),
            "std_rank_ic": result.rank_ic_stats.get("std_ic"),
            "rank_ic_positive_ratio": result.rank_ic_stats.get("ic_positive_ratio"),
        },
        "ic": _df_to_records(result.ic_df),
        "rank_ic": _df_to_records(result.rank_ic_df),
        "ic_decay": _df_to_records(result.ic_decay_df),
        "layers": _df_to_records(result.layer_results),
        "turnover": _df_to_records(result.turnover_df),
        "rolling": _df_to_records(result.rolling_df),
        "cost": _df_to_records(result.cost_summary),
        "forward_bias_passed": result.forward_bias_passed,
        "forward_bias": [
            {
                "factor_name": report.factor_name,
                "passed": report.passed,
                "n_cutoffs": report.n_cutoffs,
                "n_mismatches": report.n_mismatches,
                "max_abs_diff": report.max_abs_diff,
                "details": [dict(detail) for detail in report.details],
            }
            for report in result.forward_bias_reports
        ],
    }


def _df_to_records(data: pd.DataFrame) -> list[dict]:
    if data.empty:
        return []
    return json.loads(data.to_json(orient="records", date_format="iso"))
