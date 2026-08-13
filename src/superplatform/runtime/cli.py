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


def cmd_evaluate(args) -> None:
    """Run factor evaluation pipeline."""
    if not args.all and not args.factor:
        raise SystemExit("evaluate requires --factor <name> or --all")
    if args.all and args.factor:
        raise SystemExit("--all and --factor are mutually exclusive")

    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    providers, store = _setup_providers(config)

    try:
        runtime = OfflineRuntime(config, providers)
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

        runtime = OfflineRuntime(config, providers)
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
    """Run forward-bias check for a factor."""
    config = Config.load(args.config, "config/exchanges.yaml", "config/factors.yaml")
    providers, store = _setup_providers(config)

    try:
        runtime = OfflineRuntime(config, providers)
        results = asyncio.run(runtime.run([args.factor], output_dir="reports"))

        for r in results:
            status = "PASS" if r.forward_bias_passed else "FAIL"
            print(f"{r.factor_name}: Forward Bias — {status}")
            if not r.forward_bias_passed:
                print("  This factor has forward-looking bias and MUST be fixed!")
    finally:
        if store:
            store.close()


def cmd_live(args) -> None:
    """Run the live trading pipeline with the configured broker."""
    from superplatform.consumption.base import ConsumerConfig
    from superplatform.network.brokers import build_broker

    config = Config.load("config/default.yaml", "config/exchanges.yaml", "config/factors.yaml")
    providers, cache_store = _setup_providers(config)

    broker = build_broker(config)

    consumer = ConsumerConfig.backtest()
    live = LiveRuntime(config, providers, broker, consumer=consumer)
    live.setup(strategy_name=args.strategy)

    # Optional: DuckDB persistence for live trading state (separate from cache)
    live_store = None
    if args.store:
        from superplatform.data.store import Store
        live_store = Store(args.store)

    async def run_loop():
        print(f"Live trading started: strategy={args.strategy}, broker={broker.name}")
        print("Press Ctrl+C to stop.\n")

        tick_task = asyncio.create_task(live.start())

        if args.duration > 0:
            await asyncio.sleep(args.duration)
            await live.stop()
        else:
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

        state = live.state
        print(f"\nFinal: equity={state.equity():.2f} wallet={state.wallet_balance:.2f}")
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
                       help="Factor to evaluate (repeatable)")
    p_eval.add_argument("--all", action="store_true",
                       help="Evaluate all configured factors")
    p_eval.add_argument("--config", default="config/default.yaml")
    p_eval.add_argument("--output", default="reports/")
    p_eval.add_argument("--deliver", action="store_true",
                       help="After evaluation, run the full deliverable pipeline")
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
    p_bt.add_argument("--strategy", required=True)
    p_bt.add_argument("--config", default="config/default.yaml")
    p_bt.add_argument("--output", default="reports/")
    p_bt.add_argument("--strictness", default=None,
                      choices=["strict", "warn", "silent"],
                      help="Provider-broker consistency check policy")
    p_bt.add_argument("--target-exchange", default=None,
                      help="Target exchange for consistency check")
    p_bt.set_defaults(func=cmd_backtest)

    # check
    p_check = sub.add_parser("check")
    p_check.add_argument("--factor", required=True)
    p_check.add_argument("--config", default="config/default.yaml")
    p_check.set_defaults(func=cmd_check)

    # live
    p_live = sub.add_parser(
        "live",
        help="Run live trading with the configured broker "
             "(live.broker: simulated | binance-testnet)",
    )
    p_live.add_argument("--strategy", required=True,
                        help="Strategy to run")
    p_live.add_argument("--duration", type=int, default=0,
                        help="Run for N seconds (0=forever)")
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

