"""CLI entry point for Superplatform."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from superplatform.consumption.base import ConsumerConfig, Strictness
from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.providers import setup_providers
from superplatform.data.schema import (
    BasisSchema,
    FundingRateSchema,
    KLineSchema,
    OpenInterestSchema,
    OrderBookSchema,
    TradeSchema,
)
from superplatform.data.validators import full_validation_report
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import resolve_factor
from superplatform.runtime.config import Config
from superplatform.runtime.live import LiveRuntime
from superplatform.runtime.pipeline import OfflineRuntime
from superplatform.runtime.providers import default_provider_for
from superplatform.utils.logging import setup_logging

logger = logging.getLogger(__name__)


def _flatten_symbols(raw) -> list[str]:
    """Normalise a config symbols value into a flat list of symbol names."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, str)]
    return [str(raw)]


def _setup_providers(config: Config | None = None):
    """Create and populate a DataProviderRegistry from config.

    Reads proxy and other exchange settings from the config so callers
    don't need to know which exchanges are configured.

    Returns (registry, store) — store may be None if caching is disabled.
    Callers MUST call store.close() when done.
    """
    from superplatform.data.store import Store

    reg = DataProviderRegistry()
    proxy = ""
    store = None
    vision_max_concurrent = None
    if config is not None:
        proxy = _first_exchange_proxy(config)
        if config.get("data.cache.enabled"):
            cache_path = config.get("data.cache.path", "data/cache.duckdb")
            store = Store(cache_path)
        try:
            vision_max_concurrent = int(config.get("data.max_concurrent_requests", 0)) or None
        except (TypeError, ValueError):
            vision_max_concurrent = None
    setup_providers(
        reg, exchange_proxy=proxy, store=store,
        vision_max_concurrent=vision_max_concurrent,
    )
    return reg, store


def _first_exchange_proxy(config: Config) -> str:
    """Return the proxy URL from the first enabled exchange, if any."""
    exchanges = config.get("exchanges") or {}
    for cfg in exchanges.values():
        if cfg.get("enabled", False):
            return cfg.get("proxy", "")
    return ""


def cmd_validate(args) -> None:
    """Validate a data file against a schema."""
    import pandas as pd

    schema_map = {
        "kline": KLineSchema,
        "trade": TradeSchema,
        "orderbook": OrderBookSchema,
        "funding_rate": FundingRateSchema,
        "open_interest": OpenInterestSchema,
        "basis": BasisSchema,
    }
    schema_cls = schema_map[args.schema]
    df = pd.read_parquet(args.input) if args.input.endswith(".parquet") else pd.read_csv(args.input)
    report = full_validation_report(df, schema_cls)

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
        print(f"Report written to {args.output}")
    else:
        print(json.dumps(report, indent=2, default=str))


def cmd_factors_list(args) -> None:
    """List registered factors: decorator/config channel + dual-file channel.

    The dual-file channel (builtin factors/ + imports/factors/) is where the
    factor library scales to thousands, so it is paginated and filterable;
    `ensure_scanned()` applies an mtime incremental diff on every call so
    hot-dropped files show up without a restart.
    """
    from superplatform.factors.dual_registry import DualFactorRegistry
    from superplatform.factors.instance_registry import FactorInstanceRegistry

    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    FactorInstanceRegistry.get_instance().build_from_config(config, registry)
    dual = DualFactorRegistry.get_instance()

    print("== decorator / config 通道 ==")
    print(f"{'Name':<30} {'Kind':<10} {'Category':<25} {'#Sym':<6} {'Data Types'}")
    print("-" * 90)
    for name in registry.list_all():
        if dual.get_record(name) is not None:
            continue  # 双文件通道注册进来的实例在下方专区展示
        f = registry.get(name)
        syms = str(f.required_symbols) if f.required_symbols is not None else "any"
        print(f"{name:<30} {'factory':<10} {f.category.value:<25} {syms:<6} {', '.join(f.required_data)}")
    for name in FactorInstanceRegistry.get_instance().list_all():
        inst = FactorInstanceRegistry.get_instance().get(name)
        syms = str(inst.required_symbols) if inst.required_symbols is not None else "any"
        print(
            f"{name:<30} {'instance':<10} {inst.category.value:<25} {syms:<6} "
            f"{', '.join(inst.required_data)}  (factory={inst.factory_name}, params={inst.params})"
        )

    # ---- 双文件通道（factors/ + imports/factors/），分页/过滤 ----
    dual.ensure_scanned()
    rows = dual.list_factors()
    if args.filter:
        needle = args.filter.lower()
        rows = [
            r for r in rows
            if needle in (r["factor_id"] or "").lower() or needle in r["name"].lower()
        ]
    if args.category:
        rows = [r for r in rows if r["category"] == args.category]
    if args.status:
        rows = [r for r in rows if r["status"] == args.status]
    if args.source != "all":
        rows = [r for r in rows if r["source"] == args.source]

    total = len(rows)
    page_size = max(1, args.page_size)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, args.page), pages)
    chunk = rows[(page - 1) * page_size: page * page_size]

    print()
    print(f"== 双文件通道（factors/ + imports/factors/）== "
          f"共 {total} 个，第 {page}/{pages} 页（每页 {page_size}）")
    print(f"{'Factor ID':<12} {'Name':<24} {'Category':<16} {'Status':<11} "
          f"{'Freq':<6} {'Src':<8} {'Version'}")
    print("-" * 90)
    for r in chunk:
        print(
            f"{(r['factor_id'] or '-'):<12} {r['name']:<24} {(r['category'] or '-'):<16} "
            f"{r['status']:<11} {(r['frequency'] or '-'):<6} {r['source']:<8} "
            f"{r['version'] or '-'}"
        )
        for err in r["validation_errors"]:
            print(f"    -> 规则{err['rule_no']} | 字段[{err['field']}] | {err['message']}")
        if r.get("conflict"):
            print(f"    -> 冲突: {r['conflict']}")


def _dual_factor_defaults_from_args(args) -> dict:
    """CLI --symbols/--start/--end → 双文件因子评估默认覆盖项。"""
    symbols = getattr(args, "symbols", None)
    return {
        "symbols": (
            [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
        ),
        "start": getattr(args, "start", None),
        "end": getattr(args, "end", None),
    }


def cmd_evaluate(args) -> None:
    """Run factor evaluation pipeline."""
    if not args.all and not args.factor:
        raise SystemExit("evaluate requires --factor <name> or --all")
    if args.all and args.factor:
        raise SystemExit("--all and --factor are mutually exclusive")

    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    providers, store = _setup_providers(config)

    try:
        runtime = OfflineRuntime(
            config, providers,
            dual_factor_defaults=_dual_factor_defaults_from_args(args),
        )
        factor_names = None if args.all else args.factor
        results = asyncio.run(runtime.run(factor_names, output_dir=args.output))

        for r in results:
            print(f"\n{'='*60}")
            print(f"Factor: {r.factor_name}")

            icir = r.ic_stats.get("icir", float("nan"))
            icir_s = f"{icir:.4f}" if not (isinstance(icir, float) and icir != icir) else "N/A"
            mean_ic = r.ic_stats.get("mean_ic", float("nan"))
            mean_ic_s = f"{mean_ic:.4f}" if not (isinstance(mean_ic, float) and mean_ic != mean_ic) else "N/A"
            ic_pos = r.ic_stats.get("ic_positive_ratio")
            ic_pos_s = f"{ic_pos:.2%}" if ic_pos is not None and not (isinstance(ic_pos, float) and ic_pos != ic_pos) else "N/A"

            print(f"  ICIR:        {icir_s}")
            print(f"  Mean IC:     {mean_ic_s}")
            print(f"  IC > 0:      {ic_pos_s}")
            print(f"  Layers:      {r.layer_results['layer'].nunique() if not r.layer_results.empty else 0}")
            if not r.turnover_df.empty:
                print(f"  Avg Turnover: {r.turnover_df['turnover'].mean():.4f}")
            print(f"  Forward Bias: {'PASS' if r.forward_bias_passed else 'FAIL'}")
            print(f"  Cost scenarios: {len(r.cost_summary)}")
            print(f"  Report: {args.output}/{r.factor_name}_report.html")

        if getattr(args, "deliver", False):
            _run_deliver(results)

    finally:
        if store:
            store.close()


def _run_deliver(results) -> None:
    import pandas as pd

    from superplatform.evaluation.experiment import ExperimentRunner

    # 前视检查是硬门槛：FAIL 的因子一律不交付。
    failed = [r.factor_name for r in results if not r.forward_bias_passed]
    if failed:
        print(f"Forward Bias FAIL — 不交付: {', '.join(failed)}")
    results = [r for r in results if r.forward_bias_passed]
    if not results:
        print("No deliverable factor passed the forward-bias gate.")
        return

    frames = [r.cross_section for r in results if not r.cross_section.empty]
    if not frames:
        print("No cross-section data to deliver.")
        return

    panel = pd.concat(frames, ignore_index=True)
    if "frequency" not in panel.columns:
        panel["frequency"] = "1d"

    # The evaluation panel contract requires one return column per
    # (timestamp, symbol); factors evaluated at different frequencies
    # produce different ret_1 semantics, so each frequency group is
    # delivered as its own experiment.
    for frequency, group in panel.groupby("frequency", sort=True):
        subdir = str(frequency)
        print(f"Delivering evaluation panel ({frequency}): {len(group)} rows")
        ExperimentRunner(
            "config/config.yaml",
            panel=group,
            output_subdir=subdir,
        ).run()


def cmd_deliver(args) -> None:
    """Run the standalone evaluation deliverable from a panel or config."""
    from superplatform.evaluation.experiment import ExperimentRunner

    output_dir = ExperimentRunner(
        args.config,
        run_date=args.run_date,
        demo=args.demo,
    ).run()
    print(f"Evaluation outputs: {output_dir}")


def cmd_dashboard(args) -> None:
    """Generate factor library dashboard."""
    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    providers, store = _setup_providers(config)

    try:
        runtime = OfflineRuntime(config, providers)
        asyncio.run(runtime.run(output_dir=args.output))
        print(f"Dashboard: {args.output}/dashboard.html")
    finally:
        if store:
            store.close()


def cmd_backtest(args) -> None:
    """Run strategy pipeline: factors → signals → backtest."""
    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    providers, store = _setup_providers(config)

    try:
        strictness = getattr(args, "strictness", None)
        target = getattr(args, "target_exchange", None)
        if strictness is not None or target is not None:
            consumer = ConsumerConfig(
                consumer_id="cli-backtest",
                target_exchange=target or "backtest",
                strictness=Strictness(strictness) if strictness else Strictness.SILENT,
            )
        else:
            consumer = ConsumerConfig.backtest()

        runtime = OfflineRuntime(
            config, providers,
            dual_factor_defaults=_dual_factor_defaults_from_args(args),
        )
        result = asyncio.run(runtime.run_strategy(
            args.strategy, output_dir=args.output, consumer=consumer,
        ))

        bt = result["backtest"]
        print(f"\nStrategy: {args.strategy}")
        print(f"  Sharpe:       {bt.sharpe:.2f}")
        print(f"  Total Return: {bt.total_return:.2%}")
        print(f"  Annual Return:{bt.annual_return:.2%}")
        print(f"  Annual Vol:   {bt.annual_vol:.2%}")
        print(f"  Max Drawdown: {bt.max_drawdown:.2%}")
        print(f"  Win Rate:     {bt.win_rate:.2%}")
        print(f"  Avg Return:   {bt.avg_return:.4%}")
    finally:
        if store:
            store.close()


def _validate_fetch_plan(
    config: Config,
    data_types: list[str] | None = None,
    registry: DataProviderRegistry | None = None,
) -> list[dict]:
    """Derive the fetch plan for validate-report from factor config.

    Returns a list of descriptor dicts (provider_id, data_type, symbol,
    frequency, start, end) — the union of every (data_type, symbol) request
    the configured factors would make, including evaluation_price klines.
    Providers are resolved through ``default_provider_for``, so the plan
    works without per-factor ``providers`` blocks. Pure derivation, no I/O;
    identical requests are deduplicated. The coordinator executing the plan
    dedups again at fetch time.

    ``registry`` is required in production (``_fetch_cache_data`` builds it);
    it is supplied by callers/tests.
    """
    from superplatform.factors.registry import FactorRegistry
    from superplatform.runtime.pipeline import _data_frequency, _parse_frequency

    if registry is None:
        raise ValueError("_validate_fetch_plan requires a provider registry")

    FactorRegistry.get_instance().auto_discover()
    from superplatform.factors.instance_registry import FactorInstanceRegistry
    FactorInstanceRegistry.get_instance().build_from_config(config, FactorRegistry.get_instance())
    wanted = set(data_types) if data_types else None

    plan: list[dict] = []
    seen: set[tuple] = set()
    sample_start = config.get("evaluation.sample_start")
    sample_end = config.get("evaluation.sample_end")
    factor_registry = FactorRegistry.get_instance()
    all_cfgs = {
        **(config.get("factors") or {}),
        **(config.get("factor_instances") or {}),
    }
    for name, cfg in all_cfgs.items():
        if not isinstance(cfg, dict):
            continue
        try:
            factor = resolve_factor(name, factory_registry=factor_registry)
        except KeyError:
            continue
        providers_cfg = cfg.get("providers") or {}
        symbols = _flatten_symbols(cfg.get("symbols", ["S1"]))
        start = cfg.get("start") or sample_start
        end = cfg.get("end") or sample_end

        for data_type in factor.required_data:
            if wanted is not None and data_type not in wanted:
                continue
            try:
                provider_id = default_provider_for(
                    factor, data_type, config=config, registry=registry,
                    factor_providers=providers_cfg,
                ).provider_id
            except ValueError:
                # No provider for this data type under the current defaults —
                # nothing to fetch for it; skip instead of failing the plan.
                logger.warning(
                    "validate-report: no provider for %s.%s, skipping",
                    name, data_type,
                )
                continue
            frequency = _data_frequency(name, cfg, data_type)
            for symbol in symbols:
                key = (provider_id, symbol, frequency.value, str(start), str(end))
                if key in seen:
                    continue
                seen.add(key)
                plan.append({
                    "provider_id": provider_id,
                    "data_type": data_type,
                    "symbol": symbol,
                    "frequency": frequency,
                    "start": start,
                    "end": end,
                })

        # A non-price factor still needs a K-line source for forward returns.
        if "kline" not in factor.required_data and (wanted is None or "kline" in wanted):
            eval_price = cfg.get("evaluation_price")
            eval_freq = (
                eval_price.get("frequency", "1d")
                if isinstance(eval_price, dict)
                else "1d"
            )
            frequency = _parse_frequency(
                eval_freq, f"factors.{name}.evaluation_price"
            )
            try:
                provider_id = default_provider_for(
                    factor, "kline", config=config, registry=registry,
                    factor_providers=providers_cfg,
                ).provider_id
            except ValueError:
                logger.warning(
                    "validate-report: no kline provider for %s, skipping "
                    "evaluation-price fetch",
                    name,
                )
                continue
            for symbol in symbols:
                key = (provider_id, symbol, frequency.value, str(start), str(end))
                if key in seen:
                    continue
                seen.add(key)
                plan.append({
                    "provider_id": provider_id,
                    "data_type": "kline",
                    "symbol": symbol,
                    "frequency": frequency,
                    "start": start,
                    "end": end,
                })
    return plan


def _fetch_cache_data(
    cache_path: str | Path,
    config: Config,
    data_types: list[str] | None = None,
) -> list[tuple]:
    """Fetch the configured dataset into the validation cache.

    Builds a Store at ``cache_path`` and registers providers with the
    caching layer, which writes every fetched range through to DuckDB.
    Then runs the fetch plan concurrently (bounded + deduplicated by
    ``_DataFetchCoordinator``) so a clean environment gets real data
    before the audit.

    Returns a list of (provider_id, data_type, symbol, frequency, rows).
    """
    from superplatform.data.provider_registry import DataProviderRegistry
    from superplatform.data.providers import setup_providers
    from superplatform.data.store import Store
    from superplatform.runtime.pipeline import _DataFetchCoordinator

    store = Store(cache_path)
    try:
        setup_providers(
            reg := DataProviderRegistry(),
            exchange_proxy=_first_exchange_proxy(config),
            store=store,
        )
        try:
            max_concurrent = int(config.get("data.max_concurrent_requests", 16) or 16)
        except (TypeError, ValueError):
            max_concurrent = 16
        if max_concurrent < 1:
            max_concurrent = 16
        coordinator = _DataFetchCoordinator(max_concurrent)
        plan = _validate_fetch_plan(config, data_types, reg)

        async def _run() -> list[tuple]:
            # Launch every plan request as a task; _DataFetchCoordinator
            # bounds in-flight work to max_concurrent and dedups identical
            # requests. as_completed streams each result out as it lands so
            # a long clean-environment fetch shows live progress instead of
            # one silent wall-clock wait.
            pending: list[asyncio.Task] = []
            for req in plan:
                provider = reg.get(req["provider_id"])
                if provider is None:
                    logger.warning(
                        "validate-report: provider %s not registered, skipping",
                        req["provider_id"],
                    )
                    continue

                async def _one(req=req, provider=provider) -> tuple:
                    try:
                        df = await coordinator.fetch(
                            provider,
                            symbol=req["symbol"],
                            frequency=req["frequency"],
                            start=req["start"],
                            end=req["end"],
                        )
                        return req, len(df)
                    except Exception as exc:  # noqa: BLE001 - one symbol must not sink the batch
                        logger.warning(
                            "validate-report: fetch failed %s %s %s: %s: %s",
                            req["provider_id"], req["symbol"], req["frequency"],
                            type(exc).__name__, exc,
                        )
                        return req, None

                pending.append(asyncio.create_task(_one()))

            results: list[tuple] = []
            for done in asyncio.as_completed(pending):
                req, n = await done
                label = (
                    f"{req['provider_id']} · {req['data_type']} · "
                    f"{req['symbol']} · {req['frequency'].value}"
                )
                if n is not None:
                    # ASCII markers: ✓/✗ are not encodable on a GBK console
                    # (the Windows default codepage), which crashes the fetch.
                    print(f"  [ok] {label}: {n} rows")
                    results.append((
                        req["provider_id"], req["data_type"],
                        req["symbol"], req["frequency"].value, n,
                    ))
                else:
                    print(f"  [FAIL] {label}: failed")
            return results

        return asyncio.run(_run())
    finally:
        store.close()


def cmd_validate_report(args) -> None:
    """Generate the G1 data validation report from the cache store."""
    from superplatform.data.validation_report import generate_validation_report
    from superplatform.runtime.config import Config

    cache_path = Path(args.cache)
    # A clean checkout has no cache. Fetch the configured dataset first so
    # the command is self-sufficient; --fetch forces an (incremental)
    # refresh of any missing ranges.
    if args.fetch or not cache_path.exists():
        config = Config.load(
            args.config, "config/exchanges.yaml", "config/factors.yaml"
        )
        print(
            f"Cache '{cache_path}' missing or --fetch given — "
            "fetching the configured dataset first..."
        )
        fetched = _fetch_cache_data(cache_path, config, data_types=args.data_type)
        if fetched:
            print(f"Fetched {len(fetched)} series:")
            for provider_id, data_type, symbol, frequency, rows in fetched:
                print(
                    f"  {provider_id} · {data_type} · {symbol} · "
                    f"{frequency}: {rows} rows"
                )
        else:
            print("No data fetched (check provider config / network).")

    max_missing_pct = getattr(args, "max_missing_pct", None)
    if max_missing_pct is None:
        cfg = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
        try:
            max_missing_pct = float(cfg.get("data.validation.max_missing_pct", 10.0))
        except (TypeError, ValueError):
            max_missing_pct = 10.0

    artifacts = generate_validation_report(
        cache_path,
        args.output,
        data_types=args.data_type,
        outlier_method=args.outlier_method,
        outlier_threshold=args.outlier_threshold,
        max_missing_pct=max_missing_pct,
    )
    print(f"Verdict: {artifacts.verdict}")
    print(f"Report:  {artifacts.markdown_path}")
    print(f"JSON:    {artifacts.json_path}")


def cmd_backfill(args) -> None:
    """Backfill historical kline/funding/OI into the DuckDB cache.

    Vision-archive only (no REST): reuses DataCache/CachingProvider so the
    run is incremental — re-running the same command resumes from the
    cached bookmarks. See superplatform.data.backfill for the design.
    """
    from superplatform.data.backfill import run_backfill, settings_from_config

    config = Config.load(args.config, "config/exchanges.yaml")
    settings = settings_from_config(config, args, proxy=_first_exchange_proxy(config))
    code = run_backfill(settings)
    if code:
        raise SystemExit(code)


def cmd_check(args) -> None:
    """Run forward-bias check for a factor (hard gate: FAIL → exit 1)."""
    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    providers, store = _setup_providers(config)

    try:
        runtime = OfflineRuntime(
            config, providers,
            dual_factor_defaults=_dual_factor_defaults_from_args(args),
        )
        results = asyncio.run(runtime.run([args.factor], output_dir="reports"))

        all_passed = True
        for r in results:
            status = "PASS" if r.forward_bias_passed else "FAIL"
            print(f"{r.factor_name}: Forward Bias — {status}")
            if not r.forward_bias_passed:
                all_passed = False
                print("  This factor has forward-looking bias and MUST be fixed!")
        if not all_passed:
            raise SystemExit(1)
    finally:
        if store:
            store.close()


def _eval_service_context(args):
    """04 评估类子命令的公共装配：config + providers + store + cache 路径。

    Returns (config, providers, store, cache_path)。store 用毕必须 close()。
    """
    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    providers, store = _setup_providers(config)
    cache_path = config.get("data.cache.path", "data/cache.duckdb")
    return config, providers, store, cache_path


def _symbols_arg(args) -> list[str] | None:
    raw = getattr(args, "symbols", None)
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()] or None


def _print_json(payload) -> None:
    # ensure_ascii=True：GBK 控制台下重定向的 JSON 仍是纯 ASCII，可被任何
    # UTF-8 读取方解析（非 ASCII 字符以 \uXXXX 转义，不改变内容）。
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def cmd_rating(args) -> None:
    """Factor rating (S~D) or leaderboard (04: evaluation/rating.py)."""
    from superplatform.evaluation.rating import RatingService

    if not args.leaderboard and not args.factor:
        raise SystemExit("rating requires --factor <id> or --leaderboard")
    if args.leaderboard and args.factor:
        raise SystemExit("--leaderboard and --factor are mutually exclusive")

    config, providers, store, cache_path = _eval_service_context(args)
    service = RatingService(
        config, providers, cache_path=cache_path, store=store,
        symbols=_symbols_arg(args),
    )
    try:
        if args.leaderboard:
            ids = (
                [s.strip() for s in args.ids.split(",") if s.strip()]
                if getattr(args, "ids", None) else None
            )
            payload = service.leaderboard(
                ids=ids, days=args.days, horizon=args.horizon, refresh=args.refresh,
            )
            if args.json:
                _print_json(payload)
            else:
                print(f"评级榜（{payload['eval_window']['days']} 天窗口, "
                      f"horizon={payload['eval_window']['horizon']}）")
                print(f"{'Factor ID':<14} {'Grade':<6} {'Status':<14} {'RankIC':>9} "
                      f"{'ICIR':>8} {'Sharpe':>8} {'Samples':>9}")
                print("-" * 78)
                for e in payload["entries"]:
                    ric = f"{e['rank_ic_mean']:.4f}" if e.get("rank_ic_mean") is not None else "-"
                    icir = f"{e['icir']:.3f}" if e.get("icir") is not None else "-"
                    shp = f"{e['sharpe']:.2f}" if e.get("sharpe") is not None else "-"
                    n = str(e.get("n_samples") or "-")
                    print(f"{e['factor_id']:<14} {str(e.get('grade') or '-'):<6} "
                          f"{e.get('rating_status') or '-':<14} {ric:>9} {icir:>8} {shp:>8} {n:>9}")
                s = payload["summary"]
                print(f"\n共 {s['total']} 个：rated={s['rated']} "
                      f"insufficient={s['insufficient']} not_supported={s['not_supported']} "
                      f"not_evaluated={s['not_evaluated']}（本次计算 {s['computed_this_call']}）")
            return

        payload = service.rate_factor(
            args.factor, days=args.days, horizon=args.horizon, refresh=args.refresh,
        )
        if payload is None:
            raise SystemExit(f"Factor '{args.factor}' not registered "
                             "(双文件与 config 通道均未找到)")
        if args.json:
            _print_json(payload)
        else:
            agg = payload.get("aggregate") or {}
            print(f"Factor: {payload['factor_id']} ({payload.get('name')})  "
                  f"status={payload.get('status')}")
            print(f"  Grade:     {agg.get('grade') or '-'}")
            print(f"  RankIC:    {agg.get('rank_ic_mean')}")
            print(f"  ICIR:      {agg.get('icir')}")
            print(f"  Sharpe:    {agg.get('sharpe')}")
            print(f"  Symbols:   {agg.get('n_symbols_ok')}/{agg.get('n_symbols')} ok, "
                  f"samples={agg.get('n_samples')}")
            for note in payload.get("notes") or []:
                print(f"  note: {note}")
        if payload.get("status") == "insufficient":
            raise SystemExit(2)
    finally:
        if store:
            store.close()


def cmd_metrics(args) -> None:
    """Factor evaluation metrics / qualification summary / correlation matrix (04)."""
    from superplatform.evaluation.factor_metrics import FactorMetricsService

    if not (args.factor or args.qualification_summary or args.correlation_matrix):
        raise SystemExit("metrics requires --factor <id> | --qualification-summary | "
                         "--correlation-matrix")

    config, providers, store, cache_path = _eval_service_context(args)
    service = FactorMetricsService(
        config, providers, cache_path=cache_path, store=store,
        symbols=_symbols_arg(args),
    )
    try:
        if args.qualification_summary:
            payload = service.qualification_summary(refresh=args.refresh)
            _export_payload(args, payload, service.qualification_csv, "qualification")
            if args.json or not args.output:
                _print_json(payload)
            s = payload["summary"]
            print(f"qualification 汇总: total={s['total']} evaluated={s['evaluated']} "
                  f"qualified={s['qualified']} unqualified={s['unqualified']} "
                  f"not_evaluated={s['not_evaluated']}")
            return

        if args.correlation_matrix:
            ids = (
                [s.strip() for s in args.ids.split(",") if s.strip()]
                if getattr(args, "ids", None) else None
            )
            payload = service.correlation_matrix(ids)
            _export_payload(args, payload, service.correlation_csv, "correlation_matrix")
            if args.json or not args.output:
                _print_json(payload)
            print(f"相关性矩阵: {len(payload.get('factor_ids') or [])} 因子 "
                  f"(excluded={len(payload.get('excluded') or [])}, "
                  f"truncated={payload.get('truncated')}, cache_hit={payload.get('cache_hit')})")
            return

        payload = service.factor_metrics(args.factor, force=args.refresh)
        if payload is None:
            raise SystemExit(f"Factor '{args.factor}' not registered "
                             "(双文件与 config 通道均未找到)")
        _export_payload(args, payload, service.metrics_csv, f"{args.factor}_metrics")
        if args.json or not args.output:
            _print_json(payload)
        else:
            print(f"Factor: {payload['factor_id']}  status={payload.get('status')} "
                  f"(cache_hit={payload.get('cache_hit')})")
            print(f"  RankIC:    {payload.get('rank_ic')}")
            print(f"  IC:        {payload.get('ic')}")
            print(f"  ICIR:      {payload.get('icir')}")
            print(f"  Turnover:  {payload.get('turnover')}")
            print(f"  Samples:   {payload.get('sample_count')}")
            q = payload.get("qualification") or {}
            print(f"  Qualified: {q.get('qualified')}  reasons={q.get('reasons')}")
    finally:
        if store:
            store.close()


def _export_payload(args, payload, csv_fn, stem: str) -> None:
    """--output 导出：.csv 走 CSV 转换器，其余按 JSON。"""
    output = getattr(args, "output", None)
    if not output:
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        path.write_text(csv_fn(payload), encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported: {path}")


def cmd_bias_check(args) -> None:
    """六查批次（前视/全样本泄露/多重检验/过拟合/成本/样本外）+ 报告导出 (04)。"""
    from superplatform.evaluation.bias import BiasControlService

    if not args.all and not args.factor:
        raise SystemExit("bias-check requires --factor <id> (repeatable) or --all")

    config, providers, store, cache_path = _eval_service_context(args)
    service = BiasControlService(
        config, providers, cache_path=cache_path, store=store,
        symbols=_symbols_arg(args),
    )
    try:
        if args.all:
            from superplatform.evaluation.bias import list_factor_records

            factor_ids = [r.factor_id for r in list_factor_records(config)]
            if not factor_ids:
                raise SystemExit("无可评测因子（双文件与 config 通道均为空）")
        else:
            factor_ids = list(args.factor)

        run = service.run(args.scope, factor_ids)
        run_id = run["run_id"]

        # 报告导出（md/json/csv 可组合）
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        exported = []
        for fmt in (args.format or ["md", "json"]):
            content, filename = service.report(run_id, fmt)
            path = out_dir / filename
            path.write_text(content, encoding="utf-8")
            exported.append(str(path))

        if args.json:
            data = service.report_data(run_id)
            _print_json(data)
        else:
            print(f"Bias check run: {run_id}  scope={args.scope}")
            print(f"{'Factor ID':<14} {'Overall':<10} {'Look':<7} {'Full':<7} {'MT':<7} "
                  f"{'Overfit':<8} {'Cost':<7} {'OOS'}")
            print("-" * 84)
            for r in run["results"]:
                checks = r.get("checks") or {}
                cells = [checks.get(k, {}).get("status", "-") for k in
                         ("lookahead", "full_sample", "multiple_testing", "overfit", "cost")]
                oos = (r.get("oos") or {}).get("status", "-")
                print(f"{r['factor_id']:<14} {r['overall_status']:<10} "
                      f"{cells[0]:<7} {cells[1]:<7} {cells[2]:<7} {cells[3]:<8} {cells[4]:<7} {oos}")
                if r.get("failure_reason"):
                    print(f"    -> {r['failure_reason']}")
            s = run["summary"]
            print(f"\nSummary: total={s['total']} PASS={s['pass']} FAIL={s['fail']} "
                  f"BLOCKED={s['blocked']} ERROR={s['error']} LOCKED={s['locked']}")
        for path in exported:
            print(f"Report: {path}")

        # 硬门槛语义与 03 check 一致：任一因子 FAIL/ERROR → exit 1
        if any(r["overall_status"] in ("FAIL", "ERROR") for r in run["results"]):
            raise SystemExit(1)
    finally:
        if store:
            store.close()


def cmd_live(args) -> None:
    """Run the live trading pipeline with the configured broker."""
    from superplatform.consumption.base import ConsumerConfig
    from superplatform.network.brokers import build_broker

    config = Config.load("config/default.yaml", "config/exchanges.yaml", "config/factors.yaml")
    # CLI 覆盖项只改内存中的 config，不回写配置文件。
    live_cfg = config.to_dict().setdefault("live", {})
    if getattr(args, "broker", None):
        live_cfg["broker"] = args.broker
    if getattr(args, "interval", None):
        live_cfg["tick_interval_seconds"] = args.interval
    if getattr(args, "symbols", None):
        live_cfg["symbols"] = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # Broker 构建失败（如 binance-testnet 缺 API key）必须报错退出——
    # 绝不静默降级为模拟盘。
    try:
        broker = build_broker(config)
    except RuntimeError as exc:
        raise SystemExit(f"live 启动失败: {exc}")

    providers, cache_store = _setup_providers(config)

    consumer = ConsumerConfig.backtest()
    live = LiveRuntime(config, providers, broker, consumer=consumer)
    live.setup(strategy_name=args.strategy)

    # 跑满 N 个 tick 后自动停止（便于验收与冒烟测试）。
    if getattr(args, "ticks", 0) and args.ticks > 0:
        async def _stop_after_ticks(ctx) -> None:
            if ctx.tick_no >= args.ticks:
                await live.scheduler.stop()
        live.scheduler.register_hook(_stop_after_ticks)

    # Optional: DuckDB persistence for live trading state (separate from cache)
    live_store = None
    if args.store:
        from superplatform.data.store import Store
        live_store = Store(args.store)

    async def run_loop():
        print(f"Live trading started: strategy={args.strategy}, broker={broker.name}")
        if args.ticks > 0:
            print(f"Running {args.ticks} ticks (interval={live.scheduler.interval:.1f}s).\n")
        else:
            print("Press Ctrl+C to stop.\n")

        tick_task = asyncio.create_task(live.start())

        if args.ticks > 0:
            await tick_task
        elif args.duration > 0:
            await asyncio.sleep(args.duration)
            await live.stop()
        else:
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

        state = live.state
        print(f"\nFinal: equity={state.equity():.2f} wallet={state.wallet_balance:.2f}")
        if state.positions:
            print("Positions:")
            for key, pos in sorted(state.positions.items()):
                print(
                    f"  {key}: qty={pos.qty:.6f} entry={pos.entry_price:.2f} "
                    f"mark={pos.mark_price:.2f} upnl={pos.unrealized_pnl:.2f}"
                )
        else:
            print("Positions: (none)")
        orders = await broker.get_orders()
        trades = await broker.get_trades()
        print(f"Orders: {len(orders)}, Trades: {len(trades)}")

        if live_store:
            live_store.upsert_orders(orders)
            live_store.upsert_trades(trades)
            for pt in await broker.get_equity_curve():
                live_store.upsert_equity(pt)
            print(f"State saved to {args.store}")

    try:
        asyncio.run(run_loop())
    except KeyboardInterrupt:
        state = live.state
        print(f"\nInterrupted. Final equity: {state.equity():.2f}")
    finally:
        if live_store:
            live_store.close()
        if cache_store:
            cache_store.close()


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()  # .env (gitignored) → os.environ; existing env vars win

    setup_logging()

    parser = argparse.ArgumentParser(
        prog="superplatform",
        description="Superplatform — Quantitative Trading Research Framework",
    )
    sub = parser.add_subparsers(dest="command")

    # validate
    p_val = sub.add_parser("validate", help="Validate data against schema")
    p_val.add_argument("--input", required=True)
    p_val.add_argument("--schema", required=True,
                       choices=["kline", "trade", "orderbook", "funding_rate",
                                "open_interest", "basis"])
    p_val.add_argument("--output")
    p_val.set_defaults(func=cmd_validate)

    # validate-report (G1 数据校验报告)
    p_vr = sub.add_parser(
        "validate-report",
        help="Generate G1 data validation report from the cache store",
    )
    p_vr.add_argument("--cache", default="data/cache.duckdb",
                      help="DuckDB cache path to audit")
    p_vr.add_argument("--output", default="reports/data_validation_report.md",
                      help="Markdown report path (sidecar .json written alongside)")
    p_vr.add_argument("--config", default="config/default.yaml",
                      help="Config used to derive the fetch plan (default.yaml)")
    p_vr.add_argument("--fetch", action="store_true",
                      help="Fetch the configured dataset into the cache first "
                           "(also done automatically when the cache is missing)")
    p_vr.add_argument("--data-type", action="append", default=None,
                      choices=["kline", "funding_rate", "open_interest", "basis"],
                      help="Only audit providers of these data types (repeatable); "
                           "default all")
    p_vr.add_argument("--outlier-method", default="mad",
                      choices=["mad", "zscore"],
                      help="Outlier detection method")
    p_vr.add_argument("--outlier-threshold", type=float, default=15.0,
                      help="Outlier threshold (deviations); default 15 keeps "
                           "only pathological values — crypto bars routinely "
                           "move several MADs without being data errors")
    p_vr.add_argument("--max-missing-pct", type=float, default=None,
                      help="缺失占比告警阈值(百分数);默认取 config "
                           "data.validation.max_missing_pct (10),超过标 WARN")
    p_vr.set_defaults(func=cmd_validate_report)

    # backfill (G1 数据回填: Binance vision 归档 → DuckDB 缓存, 增量/断点续跑)
    p_bf = sub.add_parser(
        "backfill",
        help="Backfill historical kline/funding/OI into the DuckDB cache "
             "(Binance vision archives, incremental)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
全量回填(40 永续 + BTC/ETH 现货, 2019→now, 1m+1d+funding+OI):
  superplatform backfill --all
  标的取自 config/default.yaml 的 data.symbols(40 个 USDT-M 永续 +
  BTC/ETH 现货); 时间边界取 data.backfill.perpetual_start(2019-09-25,
  币安永续上线;更早请求按空处理不报错)与 data.backfill.spot_start
  (2019-01-01); 数据类型 kline(1m+1d) + funding_rate + open_interest。
  预计量级: 1m 约 1.5 亿行(40 永续 × ~350 万 + 2 现货 × ~400 万),
  归档下载 ~5GB,首次全量数小时;1d/funding/OI 合计仅数十万行,分钟级。
断点续跑: 重复执行同一条命令即可——已覆盖区间按缓存/empty_ranges 书签
  跳过,只补缺口与最新尾部,中断不丢已落库分块。
示例:
  superplatform backfill --symbols BTCUSDT,ETHUSDT --market both
  superplatform backfill --symbols-file symbols.txt --market perpetual
  superplatform backfill --all --data-type kline --kline-frequencies 1d
""",
    )
    p_bf.add_argument("--symbols", default=None,
                      help="逗号分隔标的,如 BTCUSDT,ETHUSDT(也接受 BTC/USDT)")
    p_bf.add_argument("--symbols-file", default=None,
                      help="标的清单文件,每行一个(# 后为注释)")
    p_bf.add_argument("--all", action="store_true",
                      help="全量: config data.symbols 的 40 永续 + BTC/ETH 现货")
    p_bf.add_argument("--market", default="both",
                      choices=["perpetual", "spot", "both"],
                      help="回填哪个市场;both = 同一符号两个市场各一份(分表不混)")
    p_bf.add_argument("--start", default=None,
                      help="UTC 起点(如 2019-09-25);默认按 data.backfill 边界: "
                           "永续 2019-09-25,现货 2019-01-01。永续早于 2019-09-25 "
                           "按空处理不报错")
    p_bf.add_argument("--end", default=None,
                      help="UTC 终点(默认 now;vision 归档 T+1,最新一天下次补)")
    p_bf.add_argument("--data-type", action="append", default=None,
                      choices=["kline", "funding_rate", "open_interest"],
                      help="只回填这些类型(可重复);默认全部")
    p_bf.add_argument("--kline-frequencies", default=None,
                      help="逗号分隔,默认取 config data.backfill.kline_frequencies "
                           "(1m,1d)")
    p_bf.add_argument("--oi-frequency", default=None,
                      help="OI 重采样频率,默认取 config(1d);源归档为 5m")
    p_bf.add_argument("--chunk-months", type=int, default=None,
                      help="sub-daily kline 分块大小(月),默认取 config(1)")
    p_bf.add_argument("--cache", default=None,
                      help="DuckDB 缓存路径(默认 config data.cache.path)")
    p_bf.add_argument("--config", default="config/default.yaml")
    p_bf.set_defaults(func=cmd_backfill)

    # factors
    p_factors = sub.add_parser("factors")
    p_factors_sub = p_factors.add_subparsers(dest="factors_command")
    p_list = p_factors_sub.add_parser("list")
    p_list.add_argument("--config", default="config/default.yaml",
                        help="Config for factory instances (default.yaml)")
    p_list.add_argument("--page", type=int, default=1,
                        help="双文件通道页码（默认 1）")
    p_list.add_argument("--page-size", type=int, default=50,
                        help="双文件通道每页条数（默认 50）")
    p_list.add_argument("--filter", default=None,
                        help="按 factor_id/name 子串过滤（不区分大小写）")
    p_list.add_argument("--category", default=None,
                        help="按 MD category 精确过滤（如 momentum/volatility）")
    p_list.add_argument("--status", default=None,
                        choices=["draft", "active", "deprecated", "invalid", "conflict"],
                        help="按状态过滤；invalid=校验失败，conflict=与内置冲突被压制")
    p_list.add_argument("--source", default="all",
                        choices=["all", "builtin", "imports"],
                        help="按来源过滤：builtin=内置 factors/，imports=imports/factors/")
    p_list.set_defaults(func=cmd_factors_list)

    # evaluate
    p_eval = sub.add_parser("evaluate")
    p_eval.add_argument("--factor", action="append", required=False,
                       help="Factor to evaluate (repeatable); 双文件因子（02 通道）"
                            "直接用 factor_id，如 MOM-001")
    p_eval.add_argument("--all", action="store_true",
                       help="Evaluate all configured factors")
    p_eval.add_argument("--config", default="config/default.yaml")
    p_eval.add_argument("--output", default="reports/")
    p_eval.add_argument("--symbols", default=None,
                       help="逗号分隔标的（仅对无 config 条目的双文件因子生效；"
                            "默认取 data.symbols.perpetual 研究池）")
    p_eval.add_argument("--start", default=None,
                       help="评估起点（仅双文件因子；默认 evaluation.sample_start）")
    p_eval.add_argument("--end", default=None,
                       help="评估终点（仅双文件因子；默认 evaluation.sample_end）")
    p_eval.add_argument("--deliver", action="store_true",
                       help="After evaluation, run the full deliverable pipeline "
                            "(前视 FAIL 的因子不交付)")
    p_eval.set_defaults(func=cmd_evaluate)

    # deliver
    p_deliver = sub.add_parser("deliver", help="Run full evaluation deliverable")
    p_deliver.add_argument("--config", default="config/config.yaml")
    p_deliver.add_argument("--run-date", default=None)
    p_deliver.add_argument("--demo", action="store_true",
                          help="Generate demo data when input panel is missing")
    p_deliver.set_defaults(func=cmd_deliver)

    # dashboard
    p_dash = sub.add_parser("dashboard")
    p_dash.add_argument("--config", default="config/default.yaml")
    p_dash.add_argument("--output", default="reports/")
    p_dash.set_defaults(func=cmd_dashboard)

    # backtest
    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--strategy", required=True,
                      help="策略名；双文件策略（02 通道）直接用 strategy_id，如 DEM-001")
    p_bt.add_argument("--config", default="config/default.yaml")
    p_bt.add_argument("--output", default="reports/")
    p_bt.add_argument("--symbols", default=None,
                      help="逗号分隔标的（仅对双文件策略的信号因子生效；"
                           "默认取 data.symbols.perpetual 研究池）")
    p_bt.add_argument("--start", default=None,
                      help="评估起点（仅双文件策略；默认 evaluation.sample_start）")
    p_bt.add_argument("--end", default=None,
                      help="评估终点（仅双文件策略；默认 evaluation.sample_end）")
    p_bt.add_argument("--strictness", default=None,
                      choices=["strict", "warn", "silent"],
                      help="Provider-broker consistency check policy")
    p_bt.add_argument("--target-exchange", default=None,
                      help="Target exchange for consistency check")
    p_bt.set_defaults(func=cmd_backtest)

    # check
    p_check = sub.add_parser("check")
    p_check.add_argument("--factor", required=True,
                         help="因子名；双文件因子（02 通道）直接用 factor_id")
    p_check.add_argument("--config", default="config/default.yaml")
    p_check.add_argument("--symbols", default=None,
                         help="逗号分隔标的（仅双文件因子；默认 data.symbols.perpetual）")
    p_check.add_argument("--start", default=None,
                         help="评估起点（仅双文件因子；默认 evaluation.sample_start）")
    p_check.add_argument("--end", default=None,
                         help="评估终点（仅双文件因子；默认 evaluation.sample_end）")
    p_check.set_defaults(func=cmd_check)

    # rating (04: 因子评级 S~D + 评级榜)
    p_rating = sub.add_parser(
        "rating",
        help="Factor rating (S~D, 近 N 天快速打分) or leaderboard (04)",
    )
    p_rating.add_argument("--factor", default=None,
                          help="因子 ID；双文件因子（02 通道）直接用 factor_id，如 MOM-001")
    p_rating.add_argument("--leaderboard", action="store_true",
                          help="评级榜：无 --ids 时只读缓存（未评级标 not_evaluated），"
                               "--ids 子集（≤20）同步计算")
    p_rating.add_argument("--ids", default=None,
                          help="逗号分隔因子 ID 子集（仅 --leaderboard；同步计算并落缓存）")
    p_rating.add_argument("--days", type=float, default=None,
                          help="评级窗口天数（默认 bias_control.rating_days=30）")
    p_rating.add_argument("--horizon", type=int, default=None,
                          help="前瞻收益期数，因子频率 bar 数（默认 bias_control.rating_horizon=24）")
    p_rating.add_argument("--symbols", default=None,
                          help="逗号分隔标的（默认 data.symbols.perpetual 研究池）")
    p_rating.add_argument("--refresh", action="store_true",
                          help="跳过缓存读强制重算（按原键覆盖落库）")
    p_rating.add_argument("--json", action="store_true", help="输出 JSON")
    p_rating.add_argument("--config", default="config/default.yaml")
    p_rating.set_defaults(func=cmd_rating)

    # metrics (04: 开发集深度评估指标 + 合格判定 + 相关性矩阵)
    p_metrics = sub.add_parser(
        "metrics",
        help="Factor metrics (IC/RankIC/ICIR/衰减/分层/换手) + qualification (04)",
    )
    p_metrics.add_argument("--factor", default=None,
                           help="因子 ID；双文件因子直接用 factor_id，如 MOM-001")
    p_metrics.add_argument("--qualification-summary", action="store_true",
                           help="全库合格判定汇总（只读缓存；--refresh 限量补算 ≤20 个）")
    p_metrics.add_argument("--correlation-matrix", action="store_true",
                           help="库级因子相关性矩阵（日频网格 Spearman，因子数封顶）")
    p_metrics.add_argument("--ids", default=None,
                           help="逗号分隔因子 ID 子集（仅 --correlation-matrix）")
    p_metrics.add_argument("--symbols", default=None,
                           help="逗号分隔标的（默认 data.symbols.perpetual 研究池）")
    p_metrics.add_argument("--refresh", action="store_true",
                           help="跳过缓存读强制重算（按原键覆盖落库）")
    p_metrics.add_argument("--output", default=None,
                           help="导出路径：.csv 导出 CSV（带 BOM），其余按 JSON")
    p_metrics.add_argument("--json", action="store_true", help="输出 JSON")
    p_metrics.add_argument("--config", default="config/default.yaml")
    p_metrics.set_defaults(func=cmd_metrics)

    # bias-check (04: 六查批次 + 报告导出)
    p_bias = sub.add_parser(
        "bias-check",
        help="偏差控制六查（前视/全样本泄露/多重检验/过拟合/成本/样本外）+ 报告导出 (04)",
    )
    p_bias.add_argument("--factor", action="append", default=None,
                        help="因子 ID（可重复）；多重检验家族 = 本批全部因子")
    p_bias.add_argument("--all", action="store_true",
                        help="全部可评测因子（双文件在册 + config 条目）")
    p_bias.add_argument("--scope", default="development",
                        choices=["development", "locked_oos"],
                        help="development=开发集五查（样本外显示 LOCKED）；"
                             "locked_oos=加跑锁定样本外（每因子仅允许成功一次）")
    p_bias.add_argument("--symbols", default=None,
                        help="逗号分隔标的（默认 data.symbols.perpetual 研究池）")
    p_bias.add_argument("--output", default="reports/",
                        help="报告导出目录（默认 reports/）")
    p_bias.add_argument("--format", action="append", default=None,
                        choices=["md", "json", "csv"],
                        help="报告格式（可重复，默认 md+json）")
    p_bias.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    p_bias.add_argument("--config", default="config/default.yaml")
    p_bias.set_defaults(func=cmd_bias_check)

    # live
    p_live = sub.add_parser(
        "live",
        help="Run live trading with the configured broker "
             "(live.broker: simulated | binance-testnet)",
    )
    p_live.add_argument("--strategy", required=True,
                        help="策略名；双文件策略（02 通道）直接用 strategy_id，如 DEM-001")
    p_live.add_argument("--duration", type=int, default=0,
                        help="Run for N seconds (0=forever)")
    p_live.add_argument("--ticks", type=int, default=0,
                        help="跑满 N 个 tick 打印账户权益/持仓后退出（0=不限制；"
                             "优先于 --duration）")
    p_live.add_argument("--interval", type=float, default=None,
                        help="覆盖 live.tick_interval_seconds（秒，仅本次进程生效）")
    p_live.add_argument("--symbols", default=None,
                        help="逗号分隔标的（覆盖 live.symbols，仅本次进程生效）")
    p_live.add_argument("--broker", default=None,
                        choices=["simulated", "binance-testnet"],
                        help="覆盖 live.broker（仅本次进程生效）；binance-testnet "
                             "缺环境变量 API key 会报错退出，不降级为模拟盘")
    p_live.add_argument("--store", default=None,
                        help="DuckDB path for persistence")
    p_live.set_defaults(func=cmd_live)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()

