"""Offline research pipeline.

Runtime reads config, normalises symbol groups, runs each layer per group,
and passes per-group results to evaluation for cross-sectional analysis.
"""

import asyncio
import concurrent.futures
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from superplatform.consumption.backtest import backtest
from superplatform.consumption.base import ConsumerConfig
from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.schema import (
    BasisSchema,
    DataFrequency,
    FundingRateSchema,
    KLineSchema,
    OpenInterestSchema,
)
from superplatform.data.validators import full_validation_report
from superplatform.evaluation.correlation import factor_correlation_from_dict
from superplatform.evaluation.cost_analysis import CostAssumptions, cost_sensitivity
from superplatform.evaluation.forward_bias import ForwardBiasChecker, ForwardBiasReport
from superplatform.evaluation.ic import compute_ic, compute_ic_decay, compute_icir, compute_rankic
from superplatform.evaluation.rolling import rolling_icir
from superplatform.evaluation.stratification import layer_test
from superplatform.evaluation.turnover import compute_turnover
from superplatform.factors.base import FactorResult
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import (
    factor_entry,
    resolve_factor,
    validate_used_factors_are_instances,
)
from superplatform.runtime.config import Config
from superplatform.runtime.consistency import check_consistency
from superplatform.runtime.providers import default_provider_for
from superplatform.strategy.registry import StrategyRegistry
from superplatform.utils.logging import logger
from superplatform.visualization.reports import FactorDashboard, FactorReport

ProgressFn = Callable[[dict], None]

# While a cold (uncached) request is in flight, re-emit a heartbeat every so
# often so the web panel shows live download time instead of a frozen line.
_FETCH_HEARTBEAT_SECONDS = 5.0


def _empty_schema_frame(schema_cls) -> pd.DataFrame:
    """An empty, schema-shaped DataFrame for a failed per-symbol fetch.

    Downstream layers (validation, factor compute, cross-section build) all
    tolerate an empty frame whose columns match the schema; they crash on a
    column-less one. The frame being empty is what lets ``_build_cross_section``
    skip the symbol instead of emitting bogus rows.
    """
    if schema_cls is None or not getattr(schema_cls, "columns", None):
        return pd.DataFrame()
    data = {
        name: pd.Series(
            dtype=(
                "datetime64[ns]"
                if expected is np.datetime64
                else "bool"
                if expected is np.bool_
                else "object"
                if expected is np.str_
                else "float64"
            )
        )
        for name, expected in schema_cls.columns.items()
    }
    return pd.DataFrame(data)


def _normalise_symbol_groups(raw):
    """Turn config symbols into a list of (group_key, [symbol, ...]) pairs.

    "S1"           → ("S1", ("S1",))
    ["S1","S2"]    → ("S1_S2", ("S1","S2"))
    """
    if isinstance(raw, str):
        return raw, (raw,)
    if isinstance(raw, list) and all(isinstance(s, str) for s in raw):
        return "_".join(raw), tuple(raw)
    return str(raw), (str(raw),)


def _normalise_config_symbols(raw_symbols: list):
    """Return [(group_key, (symbol, ...)), ...] from config symbol list."""
    result = []
    for item in raw_symbols:
        key, group = _normalise_symbol_groups(item)
        result.append((key, group))
    return result


def _compute_group_result(factor, group_key: str, all_group_data, params):
    """Run one group's factor computation on its in-memory inputs.

    Module-level so it can be submitted to a thread pool: the computation is
    pure pandas over already-fetched frames (no network), so groups are safe
    to compute concurrently.
    """
    return group_key, factor.compute(all_group_data[group_key], **params)


_DATA_TYPE_SCHEMAS = {
    "kline": KLineSchema,
    "funding_rate": FundingRateSchema,
    "open_interest": OpenInterestSchema,
    "basis": BasisSchema,
}


def _parse_frequency(value: Any, context: str) -> DataFrequency:
    """Convert a config value into the project's frequency enum."""
    if isinstance(value, DataFrequency):
        return value
    try:
        return DataFrequency(value)
    except ValueError as exc:
        supported = ", ".join(freq.value for freq in DataFrequency)
        raise ValueError(
            f"Unsupported frequency {value!r} for {context}. Supported: {supported}"
        ) from exc


def _data_frequency(factor_name: str, factor_config: dict, data_type: str) -> DataFrequency:
    """Resolve a per-data-type frequency, falling back to the factor default."""
    frequencies = factor_config.get("frequencies", {})
    if not isinstance(frequencies, dict):
        raise ValueError(f"factors.{factor_name}.frequencies must be a mapping")
    value = frequencies.get(data_type, factor_config.get("frequency", DataFrequency.D1.value))
    return _parse_frequency(value, f"factors.{factor_name}.{data_type}")


class _DataFetchCoordinator:
    """Share identical provider requests while bounding in-flight I/O.

    A coordinator belongs to one Runtime invocation.  It deliberately keeps
    no cache beyond that invocation so research runs never reuse stale market
    data.  Callers receive a copy because factor implementations are free to
    add temporary columns to their input frames.
    """

    def __init__(self, max_concurrent_requests: int, progress: ProgressFn | None = None):
        if max_concurrent_requests < 1:
            raise ValueError("data.max_concurrent_requests must be at least 1")
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._tasks: dict[tuple[Any, ...], asyncio.Task[pd.DataFrame]] = {}
        self._progress = progress

    def _emit(self, event: dict) -> None:
        if self._progress is not None:
            self._progress(event)

    async def fetch(
        self,
        provider,
        *,
        symbol: str,
        frequency: DataFrequency,
        start: Any,
        end: Any,
    ) -> pd.DataFrame:
        key = (
            provider.provider_id,
            symbol,
            frequency.value,
            str(start) if start is not None else None,
            str(end) if end is not None else None,
        )
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_limited(
                    provider,
                    symbol=symbol,
                    frequency=frequency,
                    start=start,
                    end=end,
                )
            )
            self._tasks[key] = task

        # Shield the shared request: cancellation of one factor evaluation
        # must not cancel a request still needed by another factor.
        return (await asyncio.shield(task)).copy(deep=True)

    async def _fetch_limited(
        self,
        provider,
        *,
        symbol: str,
        frequency: DataFrequency,
        start: Any,
        end: Any,
    ) -> pd.DataFrame:
        async with self._semaphore:
            self._emit({
                "kind": "fetch_start",
                "provider_id": provider.provider_id,
                "data_type": provider.data_type,
                "symbol": symbol,
                "frequency": frequency.value,
            })
            t0 = time.monotonic()
            task = asyncio.create_task(
                provider.fetch(
                    symbol=symbol,
                    frequency=frequency,
                    start=start,
                    end=end,
                )
            )
            try:
                # Cold (uncached) requests can take many seconds; emit a
                # heartbeat so the panel shows the download is alive.
                while True:
                    done, _ = await asyncio.wait(
                        {task}, timeout=_FETCH_HEARTBEAT_SECONDS
                    )
                    if done:
                        break
                    self._emit({
                        "kind": "fetch_pending",
                        "provider_id": provider.provider_id,
                        "data_type": provider.data_type,
                        "symbol": symbol,
                        "frequency": frequency.value,
                        "elapsed": time.monotonic() - t0,
                    })
                df = task.result()
            finally:
                if not task.done():
                    task.cancel()
            elapsed = time.monotonic() - t0
            self._emit({
                "kind": "fetch_done",
                "provider_id": provider.provider_id,
                "data_type": provider.data_type,
                "symbol": symbol,
                "frequency": frequency.value,
                "rows": len(df),
                "elapsed": elapsed,
                "bytes": int(df.memory_usage(deep=True).sum()),
            })
            return df


@dataclass
class PipelineResult:
    factor_name: str
    per_symbol: dict[str, FactorResult] = field(default_factory=dict)
    cross_section: pd.DataFrame = field(default_factory=pd.DataFrame)
    ic_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    ic_stats: dict = field(default_factory=dict)
    rank_ic_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    rank_ic_stats: dict = field(default_factory=dict)
    ic_decay_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    layer_results: pd.DataFrame = field(default_factory=pd.DataFrame)
    turnover_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    rolling_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    forward_bias_passed: bool = False
    forward_bias_reports: list = field(default_factory=list)
    cost_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    validation_reports: list[dict] = field(default_factory=list)


class OfflineRuntime:
    def __init__(
        self,
        config: Config,
        provider_registry: DataProviderRegistry,
        progress: ProgressFn | None = None,
        *,
        dual_factor_defaults: dict | None = None,
    ):
        self.config = config
        self.providers = provider_registry
        self.factors = FactorRegistry.get_instance()
        self.factors.auto_discover()
        FactorInstanceRegistry.get_instance().build_from_config(self.config, self.factors)
        self.strategies = StrategyRegistry.get_instance()
        self.strategies.auto_discover()
        # 双文件因子（02 通道）无 config 条目时的评估默认覆盖项
        # （symbols / start / end，来自 CLI --symbols/--start/--end）。
        self._dual_factor_defaults = dual_factor_defaults or {}
        # Optional progress callback for long-running web jobs; None keeps the
        # runtime invisible to anything that doesn't want progress reporting.
        self._progress = progress
        # Populated by run() when more than one factor is evaluated.
        self.correlation_matrix: pd.DataFrame | None = None

    def _emit(self, event: dict) -> None:
        if self._progress is not None:
            self._progress(event)

    # ── Factor evaluation ──────────────────────────────────────────

    async def run(
        self,
        factor_names: list[str] | None = None,
        output_dir: str = "reports",
        skip_report: bool = False,
        lightweight: bool = False,
    ) -> list[PipelineResult]:
        if factor_names is None:
            known = set(self.factors.list_all()) | set(
                FactorInstanceRegistry.get_instance().list_all()
            )
            factor_names = [
                n for n in sorted(known)
                if factor_entry(self.config, n)
            ]
        self._emit({"kind": "batch_start", "factor_count": len(factor_names)})
        fetcher = _DataFetchCoordinator(self._max_concurrent_requests(), progress=self._progress)

        # Parallel evaluation for batch runs. The shared fetcher deduplicates
        # equal requests across factors and bounds all provider I/O.
        if len(factor_names) > 1:
            results = list(await asyncio.gather(*[
                self._evaluate_factor(name, output_dir, skip_report, fetcher, lightweight)
                for name in factor_names
            ]))
        else:
            results = [
                await self._evaluate_factor(factor_names[0], output_dir, skip_report, fetcher, lightweight)
            ]

        if results and len(results) > 1:
            self._emit({"kind": "correlation"})
            factor_dfs = {}
            for r in results:
                frames = []
                for sym, fres in r.per_symbol.items():
                    fv = fres.values.copy()
                    fv["symbol"] = sym
                    frames.append(fv)
                if frames:
                    factor_dfs[r.factor_name] = pd.concat(frames, ignore_index=True)
            if factor_dfs:
                self.correlation_matrix = factor_correlation_from_dict(factor_dfs)
                logger.info(
                    f"Factor correlation matrix ({len(self.correlation_matrix)} factors):\n"
                    f"{self.correlation_matrix.to_string()}"
                )

        if not skip_report and results:
            self._generate_dashboard(results, output_dir)
        return results

    def _max_concurrent_requests(self) -> int:
        value = self.config.get("data.max_concurrent_requests", 4)
        try:
            int_value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("data.max_concurrent_requests must be an integer") from exc
        if int_value < 1:
            raise ValueError("data.max_concurrent_requests must be at least 1")
        return int_value

    def _cpu_workers(self) -> int:
        """Threads used for CPU-bound work (factor compute, forward-bias audits).

        ``evaluation.cpu_workers`` config: 0 (default) auto-picks between 2 and
        8 threads; an explicit value pins it. The per-group work is pure pandas
        on in-memory frames — never network — so it parallelises safely.
        """
        value = self.config.get("evaluation.cpu_workers", 0)
        try:
            configured = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("evaluation.cpu_workers must be an integer") from exc
        if configured < 0:
            raise ValueError("evaluation.cpu_workers must be >= 0")
        if configured > 0:
            return configured
        return max(2, min(8, os.cpu_count() or 4))

    def _representative_group(self, groups, all_group_data, factor):
        """Pick the group with the longest reference series for the audit.

        The forward-bias audit tests the factor implementation, so one
        representative group is audited by default; the group with the most
        reference rows exercises the full truncation range. Falls back to the
        first configured group when all inputs are empty.
        """
        first_data_type = factor.required_data[0]
        best = groups[0]
        best_len = -1
        for group_key, group in groups:
            reference = (
                all_group_data.get(group_key, {})
                .get(first_data_type, {})
                .get(group[0])
            )
            length = 0 if reference is None or reference.empty else len(reference)
            if length > best_len:
                best, best_len = (group_key, group), length
        return best

    async def _fetch_factor_data(
        self,
        *,
        factor,
        factor_config: dict,
        providers_cfg: dict[str, str],
        groups: list[tuple[str, tuple[str, ...]]],
        sample_start: Any,
        sample_end: Any,
        fetcher: _DataFetchCoordinator,
        lightweight: bool = False,
    ) -> tuple[
        dict[str, dict[str, dict[str, pd.DataFrame]]],
        dict[str, pd.DataFrame],
        list[dict],
    ]:
        """Load all independent inputs for one factor concurrently.

        ``lightweight`` (factory sweeps) skips the per-symbol schema validation
        reports: the data is the same cached frames for every grid combo, so
        re-validating it N times is pure waste.
        """
        group_data: dict[str, dict[str, dict[str, pd.DataFrame]]] = {
            group_key: {} for group_key, _ in groups
        }
        all_kline: dict[str, pd.DataFrame] = {}
        validation_reports: list[dict] = []
        requests: list[tuple[str, str, str, Any, bool, Any]] = []

        for group_key, group in groups:
            for data_type in factor.required_data:
                provider = default_provider_for(
                    factor, data_type, config=self.config,
                    registry=self.providers, factor_providers=providers_cfg,
                )
                schema_cls = _DATA_TYPE_SCHEMAS.get(data_type)
                if schema_cls is None:
                    raise ValueError(f"No schema registered for data type '{data_type}'")
                frequency = _data_frequency(factor.name, factor_config, data_type)
                group_data[group_key][data_type] = {}
                for symbol in group:
                    requests.append((
                        group_key,
                        data_type,
                        symbol,
                        schema_cls,
                        False,
                        fetcher.fetch(
                            provider,
                            symbol=symbol,
                            frequency=frequency,
                            start=sample_start,
                            end=sample_end,
                        ),
                    ))

            # A non-price factor needs an explicit K-line source to create
            # forward returns for IC, layers, turnover, and cost evaluation.
            if "kline" not in factor.required_data:
                evaluation_price = factor_config.get("evaluation_price")
                if not isinstance(evaluation_price, dict):
                    raise ValueError(
                        f"Factor '{factor.name}' has no kline input. Configure "
                        "evaluation_price.frequency (the provider defaults to "
                        "the resolved kline source)."
                    )
                evaluation_provider_id = evaluation_price.get("provider")
                if evaluation_provider_id:
                    evaluation_provider = self.providers.get(evaluation_provider_id)
                    if evaluation_provider.data_type != "kline":
                        raise ValueError(
                            f"Evaluation provider '{evaluation_provider_id}' must serve 'kline', "
                            f"not '{evaluation_provider.data_type}'"
                        )
                else:
                    evaluation_provider = default_provider_for(
                        factor, "kline", config=self.config,
                        registry=self.providers, factor_providers=providers_cfg,
                    )
                evaluation_frequency = _parse_frequency(
                    evaluation_price.get("frequency", DataFrequency.D1.value),
                    f"factors.{factor.name}.evaluation_price",
                )
                for symbol in group:
                    requests.append((
                        group_key,
                        "evaluation_price",
                        symbol,
                        KLineSchema,
                        True,
                        fetcher.fetch(
                            evaluation_provider,
                            symbol=symbol,
                            frequency=evaluation_frequency,
                            start=sample_start,
                            end=sample_end,
                        ),
                    ))

        async def _safe_fetch(symbol: str, schema_cls, coro: Any) -> pd.DataFrame:
            """A single symbol's fetch failure must not sink the whole batch.

            Delisted symbols (Binance ``-1121 Invalid symbol``) and transient
            network errors occur routinely when evaluating over the full-market
            universe; the evaluation should continue over the symbols it could
            load and log the rest. The symbol is replaced with an empty,
            schema-shaped frame so downstream validation/compute stay happy and
            ``_build_cross_section`` skips it.
            """
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 - see docstring
                # f-string, not %s: this logger is loguru, which does not apply
                # printf-style args — the %s form silently dropped the real
                # exception and made fetch failures undiagnosable.
                logger.warning(
                    f"fetch failed for {symbol}: {type(exc).__name__}: {exc}"
                )
                return _empty_schema_frame(schema_cls)

        frames = await asyncio.gather(
            *(_safe_fetch(request[2], request[3], request[5]) for request in requests)
        )
        for (group_key, data_type, symbol, schema_cls, is_evaluation_price, _), df in zip(
            requests, frames, strict=True
        ):
            if not lightweight:
                validation_reports.append(full_validation_report(df, schema_cls))
            if is_evaluation_price:
                all_kline[group_key] = df
                continue
            group_data[group_key][data_type][symbol] = df
            if data_type == "kline":
                # Preserve existing semantics for multi-symbol factors, where
                # the final symbol in a group is the evaluation-price source.
                all_kline[group_key] = df

        return group_data, all_kline, validation_reports

    def _factor_config_entry(self, name: str) -> dict:
        """Factor config entry, falling back to the dual-file (02) channel.

        Dual-file factors have no config entry: a minimal evaluation config is
        synthesised from the MD record (frequency / lookback), with symbols
        defaulting to the research pool. Returns {} when the name is neither
        configured nor a registered dual-file factor.
        """
        cfg = factor_entry(self.config, name)
        if cfg:
            return cfg
        from superplatform.runtime.dual import dual_factor_entry
        return dual_factor_entry(name, self.config, self._dual_factor_defaults)

    async def _evaluate_factor(
        self,
        name: str,
        output_dir: str,
        skip_report: bool = False,
        fetcher: _DataFetchCoordinator | None = None,
        lightweight: bool = False,
    ) -> PipelineResult:
        """Evaluate one factor.

        ``lightweight`` keeps only the cross-section + IC/RankIC stats and skips
        the heavy metrics (decay/layers/turnover/cost/rolling) and the
        forward-bias audit — enough for a factory parameter sweep.
        """
        cfg = self._factor_config_entry(name)
        if not cfg:
            raise ValueError(f"No config entry for factor '{name}'")

        raw_symbols = cfg.get("symbols", ["S1"])
        providers_cfg = cfg.get("providers", {})
        params = cfg.get("params", {})
        factor = resolve_factor(name)

        groups = _normalise_config_symbols(raw_symbols)

        # Validate
        for key, group in groups:
            if factor.required_symbols is not None and len(group) != factor.required_symbols:
                raise ValueError(
                    f"Factor '{name}' requires {factor.required_symbols} symbols, "
                    f"group '{key}' has {len(group)}: {group}"
                )

        sample_start = cfg.get("start") or self.config.get("evaluation.sample_start")
        sample_end = cfg.get("end") or self.config.get("evaluation.sample_end")
        min_stocks = self.config.get("evaluation.ic.min_stocks_per_period", 2)
        if not lightweight:
            logger.info(f"  {name}: effective_params={params}")

        # Phase 1: fetch every independent input concurrently, then compute
        # groups in configuration order for deterministic factor output.
        self._emit({"kind": "factor_start", "factor": name})
        fetcher = fetcher or _DataFetchCoordinator(self._max_concurrent_requests())
        all_group_data, all_kline, validation_reports = await self._fetch_factor_data(
            factor=factor,
            factor_config=cfg,
            providers_cfg=providers_cfg,
            groups=groups,
            sample_start=sample_start,
            sample_end=sample_end,
            fetcher=fetcher,
            lightweight=lightweight,
        )
        per_group: dict[str, FactorResult] = {}
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._cpu_workers()
        ) as pool:
            compute_futures = [
                loop.run_in_executor(
                    pool, _compute_group_result, factor, group_key, all_group_data, params
                )
                for group_key, _ in groups
            ]
            for coro in asyncio.as_completed(compute_futures):
                group_key, result = await coro
                per_group[group_key] = result
                if not lightweight:
                    # Sweeps evaluate N combos × M groups; per-group logging /
                    # events would flood the log and cost real time.
                    self._emit({"kind": "compute", "factor": name, "group": group_key})
                    logger.info(f"  {group_key}: computed")

            # Phase 2+3: cross-sectional eval DataFrame and all evaluation
            # metrics in one offloaded call. These used to run inline on the
            # event-loop thread, blocking every other factor's compute callbacks
            # ~0.7s per factor — that was the "computed" logs landing in ~1s-
            # spaced batches during a batch run. Off the loop they overlap with
            # sibling factors instead of freezing them.
            def _cross_section_and_metrics(lightweight: bool):
                market_type = self.config.get("defaults.market", "")
                exchange = self.config.get("defaults.exchange", "")
                frequency = str(cfg.get("frequency", DataFrequency.D1.value))
                eval_df = self._build_cross_section(
                    per_group,
                    all_kline,
                    factor_name=name,
                    exchange=exchange,
                    market_type=market_type,
                    settlement_asset="USDT" if market_type == "perpetual" else "",
                    # The temporal contract follows the run cadence unless the
                    # config explicitly pins a bar_interval (default stays "1d"
                    # for direct pipeline runs that never set a frequency).
                    bar_interval=cfg.get("bar_interval") or frequency,
                    frequency=frequency,
                    lightweight=lightweight,
                )
                ic_df = compute_ic(eval_df, forward_return_col="forward_return_t1", min_stocks=min_stocks)
                rank_ic_df = compute_rankic(eval_df, forward_return_col="forward_return_t1", min_stocks=min_stocks) if len(eval_df["symbol"].unique()) > 1 else pd.DataFrame()
                ic_stats = compute_icir(ic_df["ic"]) if not ic_df.empty else {}
                rank_ic_stats = compute_icir(rank_ic_df["rank_ic"]) if not rank_ic_df.empty else {}
                if lightweight:
                    # Sweep needs only the IC stats; everything below is unused.
                    return (eval_df, ic_df, rank_ic_df, ic_stats, rank_ic_stats,
                            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), None, pd.DataFrame())
                # Evaluation — all parameters now read from config
                n_layers = self.config.get("evaluation.layers", 5)
                roll_window = self.config.get("evaluation.rolling_window", 60)
                roll_step = self.config.get("evaluation.rolling_step", 20)
                max_horizon = self.config.get("evaluation.ic_decay.max_horizon", 20)
                ic_decay_cols = [f"forward_return_t{i}" for i in range(1, max_horizon + 1)]
                ic_decay_df = compute_ic_decay(
                    eval_df,
                    forward_return_cols=ic_decay_cols,
                    min_stocks=min_stocks,
                )
                layer_results = layer_test(eval_df, forward_return_col="forward_return_t1", n_layers=n_layers)
                turnover_df = compute_turnover(eval_df, n_layers=n_layers)
                cost_cfg = self.config.get("evaluation.cost") or {}
                cost_assumptions = [CostAssumptions(
                    maker_fee_bps=float(cost_cfg.get("maker_fee_bps", 2.0)),
                    taker_fee_bps=float(cost_cfg.get("taker_fee_bps", 4.0)),
                    slippage_bps=float(cost_cfg.get("slippage_bps", 3.0)),
                )] if cost_cfg else None
                cost_summary = cost_sensitivity(layer_results, turnover_df, cost_assumptions=cost_assumptions)
                rolling_df = rolling_icir(ic_df, window=roll_window, step=roll_step) if not ic_df.empty else pd.DataFrame()
                return (eval_df, ic_df, rank_ic_df, ic_stats, rank_ic_stats,
                        ic_decay_df, layer_results, turnover_df, cost_summary, rolling_df)

            (eval_df, ic_df, rank_ic_df, ic_stats, rank_ic_stats,
             ic_decay_df, layer_results, turnover_df, cost_summary, rolling_df) = (
                await loop.run_in_executor(pool, _cross_section_and_metrics, lightweight)
            )
            self._emit({"kind": "cross_section", "factor": name})
            self._emit({"kind": "metrics", "factor": name})

        # Phase 4: forward-bias check per group (skipped for lightweight sweeps)
        all_passed = True
        forward_bias_reports = []
        if not lightweight:
            checker = ForwardBiasChecker(
                n_cutoffs=self.config.get("evaluation.forward_bias.n_cutoffs", 5)
            )

            # The audit verifies the factor *implementation* is causal — the same
            # code runs on every group, so auditing one representative group (the
            # one with the longest reference series, exercising the full truncation
            # range) catches the same look-ahead as auditing every group at a
            # fraction of the cost. ``all`` keeps the full per-group audit for
            # strict offline gates. The chosen groups are audited concurrently on a
            # thread pool: the work is pure pandas over already-fetched frames, and
            # running it off the event-loop thread lets batch factors interleave.
            audit_mode = str(self.config.get("evaluation.forward_bias.groups", "representative"))
            if audit_mode == "all":
                audit_groups = list(groups)
            elif not groups:
                audit_groups = []
            else:
                audit_groups = [self._representative_group(groups, all_group_data, factor)]

            def _audit_group(group_key, group):
                group_inputs = all_group_data[group_key]
                first_data_type = factor.required_data[0]
                reference_data = group_inputs[first_data_type][group[0]]

                # A group with no data in the evaluation window (a delisted symbol,
                # or one listed after the sample range) has nothing to bias-check —
                # skip it rather than let the checker raise.
                if (
                    reference_data.empty
                    or "timestamp" not in reference_data.columns
                    or reference_data["timestamp"].drop_duplicates().size < checker.n_cutoffs + 2
                ):
                    return group_key, ForwardBiasReport(
                        factor_name=f"{name}/{group_key}",
                        passed=True,
                        n_cutoffs=checker.n_cutoffs,
                        n_mismatches=0,
                        max_abs_diff=0.0,
                        details=[{"note": "skipped: insufficient data for forward-bias check"}],
                    )

                # Reuse the Phase-1 full-sample computation as the checker baseline
                # instead of recomputing it once more inside the audit.
                full_result = per_group.get(group_key)
                baseline = full_result.values if full_result is not None else None

                def compute_fn(reference_df):
                    """Recompute from every real input truncated at the test cutoff."""
                    cutoff = reference_df["timestamp"].max()
                    truncated_data = {
                        data_type: {
                            symbol: data[data["timestamp"] <= cutoff].copy()
                            for symbol, data in per_symbol.items()
                        }
                        for data_type, per_symbol in group_inputs.items()
                    }
                    return factor.compute(truncated_data, **params).values

                report = checker.check(
                    factor_name=f"{name}/{group_key}",
                    compute_fn=compute_fn,
                    data=reference_data,
                    baseline=baseline,
                )
                return group_key, report

            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self._cpu_workers()
            ) as pool:
                audit_futures = [
                    loop.run_in_executor(pool, _audit_group, group_key, group)
                    for group_key, group in audit_groups
                ]
                for i, coro in enumerate(asyncio.as_completed(audit_futures), start=1):
                    group_key, report = await coro
                    self._emit({
                        "kind": "forward_bias",
                        "factor": name,
                        "group": group_key,
                        "i": i,
                        "n": len(audit_groups),
                    })
                    forward_bias_reports.append(report)
                    if not report.passed:
                        all_passed = False

        # Phase 5: report (skip for API calls)
        if not skip_report:
            rpt = FactorReport(
                factor_name=name,
                factor_category=factor.category.value,
                ic_df=ic_df,
                ic_stats=ic_stats,
                ic_decay_df=ic_decay_df,
                layer_results=layer_results,
                turnover_df=turnover_df,
                forward_bias_passed=all_passed,
                cost_summary=cost_summary,
            )
            rpt.to_html(f"{output_dir}/{name}_report.html")

        icir_val = ic_stats.get("icir", float("nan"))
        is_nan = isinstance(icir_val, float) and icir_val != icir_val
        icir_str = "N/A" if is_nan else f"{icir_val:.4f}"
        if not lightweight:
            logger.info(f"  {name}: ICIR={icir_str}, bias={'PASS' if all_passed else 'FAIL'}")
        self._emit({"kind": "factor_done", "factor": name, "icir": icir_str})

        return PipelineResult(
            factor_name=name,
            per_symbol=per_group,
            cross_section=eval_df,
            ic_df=ic_df,
            ic_stats=ic_stats,
            rank_ic_df=rank_ic_df,
            rank_ic_stats=rank_ic_stats,
            ic_decay_df=ic_decay_df,
            layer_results=layer_results,
            turnover_df=turnover_df,
            rolling_df=rolling_df,
            forward_bias_passed=all_passed,
            forward_bias_reports=forward_bias_reports,
            cost_summary=cost_summary,
            validation_reports=validation_reports,
        )

    @staticmethod
    def _build_price_frame(
        all_kline: dict[str, pd.DataFrame],
        *,
        exchange: str = "",
        market_type: str = "",
        settlement_asset: str = "",
        bar_interval: str = "1d",
        frequency: str = "1d",
        lightweight: bool = False,
    ) -> pd.DataFrame:
        """The price side of a cross-section: kline + forward returns + metadata.

        Shared by a factory sweep's grid points — the data does not depend on
        the factor params, so it is built once and merged per combo.
        """
        kframes = []
        for group_key, kline in all_kline.items():
            kline = kline.copy()
            kline["symbol"] = group_key
            kframes.append(kline)
        if not kframes:
            raise ValueError("No evaluation data produced")
        frame = pd.concat(kframes, ignore_index=True)

        # Forward returns per symbol, computed in one pass over the
        # concatenated panel via groupby shift — the vectorized equivalent of
        # calling add_forward_returns on each symbol's sorted frame. (That
        # helper shifts a bare frame, so it can only be used per symbol.)
        horizons = (1,) if lightweight else (1, 5, 10, 20)
        for p in horizons:
            frame[f"forward_return_t{p}"] = (
                frame.groupby("symbol")["close"].shift(-p) / frame["close"] - 1
            )

        # ── Evaluation-panel standard columns (factor_name set by the caller) ──
        frame["exchange"] = exchange
        frame["market_type"] = market_type
        if settlement_asset:
            frame["settlement_asset"] = settlement_asset
        frame["is_eligible"] = True
        frame["frequency"] = frequency

        # Temporal contract.
        normalized = bar_interval.replace("d", "D") if bar_interval.endswith("d") else bar_interval
        interval = pd.Timedelta(normalized)
        frame["available_ts"] = frame["timestamp"]
        frame["entry_ts"] = frame["timestamp"] + interval
        frame["exit_ts"] = frame["entry_ts"] + interval  # horizon = 1

        # Map pipeline forward-return names to evaluation-panel names.
        for horizon in horizons:
            frame[f"ret_{horizon}"] = frame[f"forward_return_t{horizon}"]

        if market_type == "perpetual":
            frame["funding_included"] = "funding_rate" in frame.columns

        return frame

    @staticmethod
    def _build_cross_section(
        per_group: dict[str, FactorResult],
        all_kline: dict[str, pd.DataFrame],
        *,
        factor_name: str = "",
        exchange: str = "",
        market_type: str = "",
        settlement_asset: str = "",
        bar_interval: str = "1d",
        frequency: str = "1d",
        lightweight: bool = False,
    ) -> pd.DataFrame:
        """Build a factor cross-section DataFrame with evaluation-panel columns.

        Merges factor values with the shared price frame (kline + forward
        returns + metadata). ``lightweight`` (factory sweeps) keeps only the t1
        forward return the IC metrics need.
        """
        price = OfflineRuntime._build_price_frame(
            all_kline,
            exchange=exchange,
            market_type=market_type,
            settlement_asset=settlement_asset,
            bar_interval=bar_interval,
            frequency=frequency,
            lightweight=lightweight,
        )

        vframes = []
        for group_key, result in per_group.items():
            fv = result.values.copy()
            fv["symbol"] = group_key
            vframes.append(fv[["timestamp", "symbol", "value"]])
        if not vframes:
            raise ValueError("No evaluation data produced")
        all_v = pd.concat(vframes, ignore_index=True)

        merged = price.merge(all_v, on=["timestamp", "symbol"], how="inner")
        merged = merged.rename(columns={"value": "factor_value"})
        merged["factor_name"] = factor_name

        # A symbol with no data in the evaluation window (e.g. POLUSDT before
        # its 2024 rename from MATIC) drops out of the inner join entirely —
        # the empty-timestamp crash the per-group merge guarded against cannot
        # occur on the concatenated frame.
        if merged.empty:
            raise ValueError("No evaluation data produced")
        return merged

    async def evaluate_grid(
        self,
        *,
        factor_name: str,
        combos: list[dict],
        symbols: list[str],
        sample_start: Any,
        sample_end: Any,
        frequency: str | None = None,
    ) -> list[dict]:
        """Evaluate one factory across a param grid in a single shared-fetch pass.

        Fetches the factory's data and builds the price frame once, then for each
        combo computes the factor values, merges them with the shared price
        frame and returns the IC stats — skipping the per-evaluation pipeline
        overhead (fetch setup, thread pools, validation) that would otherwise be
        paid once per grid point.
        """
        factor = resolve_factor(factor_name)
        cfg = factor_entry(self.config, factor_name) or {}
        providers_cfg = cfg.get("providers", {})
        groups = _normalise_config_symbols(symbols)
        fetcher = _DataFetchCoordinator(self._max_concurrent_requests())
        all_group_data, all_kline, _ = await self._fetch_factor_data(
            factor=factor,
            factor_config={**cfg, "symbols": symbols},
            providers_cfg=providers_cfg,
            groups=groups,
            sample_start=sample_start,
            sample_end=sample_end,
            fetcher=fetcher,
            lightweight=True,
        )
        market_type = self.config.get("defaults.market", "")
        exchange = self.config.get("defaults.exchange", "")
        cadence = str(frequency or DataFrequency.D1.value)
        price = self._build_price_frame(
            all_kline,
            exchange=exchange,
            market_type=market_type,
            settlement_asset="USDT" if market_type == "perpetual" else "",
            bar_interval=cadence,
            frequency=cadence,
            lightweight=True,
        )
        min_stocks = self.config.get("evaluation.ic.min_stocks_per_period", 2)

        results: list[dict] = []
        for params in combos:
            per_group: dict[str, FactorResult] = {}
            for group_key, _group in groups:
                per_group[group_key] = factor.compute(all_group_data[group_key], **params)

            vframes = []
            for group_key, result in per_group.items():
                fv = result.values.copy()
                fv["symbol"] = group_key
                vframes.append(fv[["timestamp", "symbol", "value"]])
            all_v = pd.concat(vframes, ignore_index=True)
            merged = price.merge(all_v, on=["timestamp", "symbol"], how="inner")
            merged = merged.rename(columns={"value": "factor_value"})
            if merged.empty:
                raise ValueError("No evaluation data produced")

            ic_df = compute_ic(merged, forward_return_col="forward_return_t1", min_stocks=min_stocks)
            ic_stats = compute_icir(ic_df["ic"]) if not ic_df.empty else {}
            results.append({
                "params": params,
                "metrics": {
                    "icir": ic_stats.get("icir"),
                    "mean_ic": ic_stats.get("mean_ic"),
                    "std_ic": ic_stats.get("std_ic"),
                    "ic_positive_ratio": ic_stats.get("ic_positive_ratio"),
                    "t_stat": ic_stats.get("ic_ir_tstat"),
                },
            })
        return results

    # ── Strategy pipeline ───────────────────────────────────────────

    async def run_strategy(
        self,
        strategy_name: str,
        output_dir: str = "reports",
        consumer: ConsumerConfig | None = None,
    ) -> dict:
        if consumer is None:
            consumer = ConsumerConfig.backtest()

        from superplatform.runtime.dual import periods_per_year, resolve_strategy_ex

        # decorator 通道优先；未注册的名字回退双文件（02）通道，is_dual 时
        # used_factors 由策略 MD params 推导（因子经 02 协议校验注册，
        # 不走 decorator 通道的实例治理）。
        strategy, used_factors, is_dual = resolve_strategy_ex(strategy_name)
        logger.info(f"Running strategy: {strategy_name} (consumer={consumer.consumer_id})")

        if not is_dual:
            validate_used_factors_are_instances(used_factors)

        # ── Consistency check ─────────────────────────────────────
        factor_to_providers: dict[str, dict[str, str]] = {}
        for factor_name in used_factors:
            cfg = self._factor_config_entry(factor_name)
            if not cfg:
                continue
            factor = resolve_factor(factor_name)
            factor_to_providers[factor_name] = {
                dt: default_provider_for(
                    factor, dt, config=self.config, registry=self.providers,
                ).provider_id
                for dt in factor.required_data
            }

        check_consistency(
            strategy_name=strategy_name,
            consumer=consumer,
            factor_registry=self.factors,
            factor_to_providers=factor_to_providers,
        )
        # ────────────────────────────────────────────────────────────

        all_factor_results: dict[str, dict[str, FactorResult]] = {}
        all_price_data: dict[str, pd.DataFrame] = {}
        fetcher = _DataFetchCoordinator(self._max_concurrent_requests())

        for factor_name in used_factors:
            cfg = self._factor_config_entry(factor_name)
            if not cfg:
                raise ValueError(f"No config for factor '{factor_name}'")
            raw_symbols = cfg.get("symbols", ["S1"])
            providers_cfg = cfg.get("providers", {})
            params = cfg.get("params", {})
            sample_start = cfg.get("start") or self.config.get("evaluation.sample_start")
            sample_end = cfg.get("end") or self.config.get("evaluation.sample_end")
            factor = resolve_factor(factor_name)
            groups = _normalise_config_symbols(raw_symbols)

            all_group_data, price_data, _ = await self._fetch_factor_data(
                factor=factor,
                factor_config=cfg,
                providers_cfg=providers_cfg,
                groups=groups,
                sample_start=sample_start,
                sample_end=sample_end,
                fetcher=fetcher,
            )
            all_price_data.update(price_data)
            per_group = {
                group_key: factor.compute(all_group_data[group_key], **params)
                for group_key, _ in groups
            }

            all_factor_results[factor_name] = per_group
            logger.info(f"  Computed {factor_name} on {len(per_group)} groups")

        signal = strategy.generate_signals(all_factor_results)
        logger.info(f"  Signal rows: {len(signal.positions)}")

        # Label the result (backtest reads signals.attrs["strategy_name"]).
        signal.positions.attrs["strategy_name"] = signal.name
        # Trading costs come from the same evaluation.cost section the factor
        # cost-sensitivity path uses; absent config → zero cost.
        cost_cfg = self.config.get("evaluation.cost") or {}
        # Annualisation follows the bar frequency of the first signal factor
        # (1d → 365, the historical default; dual-file factors may declare
        # sub-daily frequencies in their MD).
        bar_frequency = None
        if used_factors:
            bar_frequency = self._factor_config_entry(used_factors[0]).get("frequency")
        bt = backtest(
            signal.positions,
            all_price_data,
            periods_per_year=periods_per_year(bar_frequency),
            taker_fee_bps=float(cost_cfg.get("taker_fee_bps", 0.0)),
            slippage_bps=float(cost_cfg.get("slippage_bps", 0.0)),
        )
        logger.info(
            f"  Sharpe: {bt.sharpe:.2f}, MaxDD: {bt.max_drawdown:.2%}, "
            f"WinRate: {bt.win_rate:.2%}"
        )
        return {"signal": signal, "backtest": bt, "factor_results": all_factor_results}

    # ── Dashboard ───────────────────────────────────────────────────

    def _generate_dashboard(self, results: list[PipelineResult], output_dir: str) -> None:
        reports = []
        for r in results:
            reports.append(FactorReport(
                factor_name=r.factor_name,
                factor_category=resolve_factor(r.factor_name).category.value,
                ic_df=r.ic_df,
                ic_stats=r.ic_stats,
                ic_decay_df=r.ic_decay_df,
                layer_results=r.layer_results,
                turnover_df=r.turnover_df,
                forward_bias_passed=r.forward_bias_passed,
                cost_summary=r.cost_summary,
            ))
        FactorDashboard(reports).to_html(f"{output_dir}/dashboard.html")
        logger.info(f"Dashboard written to {output_dir}/dashboard.html")
