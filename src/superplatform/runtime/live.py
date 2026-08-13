"""LiveRuntime — online pipeline orchestrator.

LiveRuntime holds:
  - Pipeline state: data buffers, factor values, strategy config
  - Account mirror: a local copy of AccountState, synced from the Broker
                    each tick. This is a CACHE for fast queries during
                    tick processing — the Broker is always the source of
                    truth (simulated or real exchange).

Each tick:
  1. Fetch latest prices → broker.update_prices()
  2. Compute factors (windowed from buffered data)
  3. Generate signals → OrderRequests (Signal Engine, future)
  4. Place orders via broker.place_order()
  5. broker.tick() — fills, marks, funding, liquidation, snapshot
  6. Sync local mirror: self._state = await broker.fetch_account_state()
"""

import asyncio

from superplatform.consumption.account import fresh_account
from superplatform.consumption.base import ConsumerConfig
from superplatform.consumption.engine import generate_orders
from superplatform.consumption.risk import RiskLimits
from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.trading import AccountState
from superplatform.factors.base import FactorResult
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import (
    factor_entry,
    resolve_factor,
    validate_used_factors_are_instances,
)
from superplatform.network.broker import Broker
from superplatform.runtime.config import Config
from superplatform.runtime.consistency import check_consistency
from superplatform.runtime.providers import default_provider_for
from superplatform.runtime.scheduler import HookContext, Scheduler
from superplatform.strategy.registry import StrategyRegistry
from superplatform.utils.logging import logger


class LiveRuntime:
    """Online trading runtime.

    The Runtime owns pipeline orchestration and holds a LOCAL MIRROR of
    the account state. The Broker is the source of truth — it represents
    either a virtual exchange (SimulatedBroker) or a real one (BinanceBroker).
    The local mirror is synced each tick for fast query access.

    Usage:
        config = Config.load("config/default.yaml", ...)
        broker = SimulatedBroker(adapter=..., initial_capital=100_000)
        live = LiveRuntime(config, providers, broker)
        live.setup(strategy_name="rsi_mean_reversion")
        await live.start()  # blocks until cancelled
    """

    def __init__(
        self,
        config: Config,
        provider_registry: DataProviderRegistry,
        broker: Broker,
        *,
        consumer: ConsumerConfig | None = None,
        limits: RiskLimits | None = None,
        symbols: list[str] | None = None,
    ):
        self.config = config
        self.providers = provider_registry
        self.broker = broker
        self.consumer = consumer or ConsumerConfig.backtest()
        self.limits = limits or RiskLimits()
        # Per-session symbol override (web live_start) — falls back to
        # config live.symbols → data.symbols.perpetual in _hook_data.
        self._symbols_override = symbols

        # Shared registries (factory layer + instance layer)
        self.factors = FactorRegistry.get_instance()
        self.factors.auto_discover()
        FactorInstanceRegistry.get_instance().build_from_config(self.config, self.factors)
        self.strategies = StrategyRegistry.get_instance()
        self.strategies.auto_discover()

        # Scheduler
        interval = config.get("live.tick_interval_seconds", 10)
        self.scheduler = Scheduler(interval=interval)

        # Account mirror — will be hydrated in setup() from the broker
        initial_capital = config.get("live.paper.initial_capital_usdt", 100_000)
        self._state: AccountState = fresh_account(initial_capital)

        # Pipeline state
        self._factor_results: dict[str, dict[str, FactorResult]] = {}
        #                     factor_name → {symbol: FactorResult}
        self._data_buffer: dict[str, list] = {}
        self._active_strategy_name: str | None = None
        self._data_primed: bool = False  # first tick does a bulk pre-fetch
        self._data_fetch_semaphore = asyncio.Semaphore(
            self._max_concurrent_requests()
        )

    def _max_concurrent_requests(self) -> int:
        value = self.config.get("data.max_concurrent_requests", 4)
        try:
            max_concurrent_requests = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("data.max_concurrent_requests must be an integer") from exc
        if max_concurrent_requests < 1:
            raise ValueError("data.max_concurrent_requests must be at least 1")
        return max_concurrent_requests

    # ── Setup ───────────────────────────────────────────────────────

    def setup(self, strategy_name: str | None = None) -> None:
        """Register hooks on the scheduler in strict execution order."""
        self._active_strategy_name = strategy_name

        if strategy_name:
            strategy = self.strategies.get(strategy_name)
            validate_used_factors_are_instances(strategy.used_factors)

        self.scheduler.register_hook(self._hook_data)
        self.scheduler.register_hook(self._hook_factors)
        self.scheduler.register_hook(self._hook_strategy)
        self.scheduler.register_hook(self._hook_trading)

        if strategy_name and self.consumer.strictness.value != "silent":
            self._run_consistency_check(strategy_name)

        logger.info(
            "LiveRuntime ready: broker={} consumer={} interval={:.1f}s",
            self.broker.name, self.consumer.consumer_id,
            self.scheduler.interval,
        )

    # ── Start / Stop ────────────────────────────────────────────────

    async def start(self) -> None:
        # Hydrate local mirror from broker on first tick
        self._state = await self.broker.fetch_account_state()
        logger.info("Account hydrated: equity={:.2f}", self._state.equity())
        await self.scheduler.run()

    async def stop(self) -> None:
        logger.info("LiveRuntime stopped. Final equity: {:.2f}", self._state.equity())
        await self.scheduler.stop()

    # ── Hook 1: Data ────────────────────────────────────────────────

    async def _hook_data(self, ctx: HookContext) -> None:
        """Fetch latest prices via the broker's market-data adapter.

        On the first tick, pre-fetches a window of historical bars so
        factors have enough lookback data to compute meaningful values.
        Subsequent ticks fetch only the latest bar(s).
        """
        symbols = (
            self._symbols_override
            or self.config.get("live.symbols")
            or self.config.get("data.symbols.perpetual", ["BTCUSDT"])
        )

        from superplatform.network.base import KLineInterval, MarketType

        async def fetch_symbol(sym: str):
            try:
                async with self._data_fetch_semaphore:
                    limit = 2 if self._data_primed else 200
                    df = await self.broker.fetch_klines(
                        symbol=sym,
                        interval=KLineInterval.M1,
                        market_type=MarketType.PERPETUAL,
                        limit=limit,
                    )
                return sym, df, None
            except Exception as exc:
                return sym, None, exc

        # Network fetches are independent. Apply results afterwards so broker
        # and scheduler state updates remain serialized and deterministic.
        fetched = await asyncio.gather(*(fetch_symbol(sym) for sym in symbols))
        failed_symbols = []
        for sym, df, error in fetched:
            if error is not None:
                failed_symbols.append(sym)
                logger.warning("Price fetch failed for {}: {}", sym, error)
                continue

            if not self._data_primed and not df.empty:
                self._data_buffer[sym] = df.to_dict("records")
            elif self._data_primed and not df.empty:
                # The tick interval is shorter than the M1 bar width, so
                # successive fetches return the same in-progress bar. Append
                # only rows newer than the last buffered one — otherwise the
                # buffer accumulates duplicate timestamps, which breaks the
                # strategy panel join (reindex on duplicate labels) and grows
                # the buffer without bound.
                rows = self._data_buffer.setdefault(sym, [])
                last_ts = rows[-1]["timestamp"] if rows else None
                if last_ts is None:
                    rows.extend(df.to_dict("records"))
                else:
                    rows.extend(
                        row for row in df.to_dict("records")
                        if row["timestamp"] > last_ts
                    )

            try:
                if sym in self._data_buffer and self._data_buffer[sym]:
                    price = float(self._data_buffer[sym][-1]["close"])
                elif not df.empty:
                    price = float(df["close"].iloc[-1])
                else:
                    continue
                self.broker.update_prices({sym: price})
                self.scheduler.update_prices({sym: price})
            except Exception:
                failed_symbols.append(sym)
                logger.warning("Price update failed for {}", sym, exc_info=True)

        if failed_symbols:
            self.scheduler.set_stale(failed_symbols)

        if not self._data_primed:
            self._data_primed = True
            logger.info(
                "Data primed: {} symbols, {} bars each",
                len(self._data_buffer),
                min(len(v) for v in self._data_buffer.values()) if self._data_buffer else 0,
            )

    # ── Hook 2: Factors ─────────────────────────────────────────────

    async def _hook_factors(self, ctx: HookContext) -> None:
        """Compute factors from buffered kline data.

        Only the active strategy's ``used_factors`` are computed — every other
        factor is wasted work and, for factors whose ``required_data`` the live
        data hook cannot provide (cross-asset pairs, open interest, ...), a
        per-tick warning that drowns out real errors.
        """
        if not self._data_buffer or not self._active_strategy_name:
            return

        import pandas as pd

        strategy = self.strategies.get(self._active_strategy_name)
        wanted = set(strategy.used_factors)
        known = set(self.factors.list_all()) | set(
            FactorInstanceRegistry.get_instance().list_all()
        )
        for factor_name in sorted(known):
            factor = resolve_factor(factor_name)
            if factor_name not in wanted:
                continue
            if "kline" not in factor.required_data:
                continue
            cfg = factor_entry(self.config, factor_name)
            if not cfg:
                continue

            try:
                kline_data: dict[str, pd.DataFrame] = {}
                for sym, rows in self._data_buffer.items():
                    if rows:
                        kline_data[sym] = pd.DataFrame(rows)
                if not kline_data:
                    continue

                result = factor.compute({"kline": kline_data})
                # Store per-symbol FactorResult for strategy consumption
                for sym in kline_data:
                    self._factor_results.setdefault(factor_name, {})[sym] = result
            except Exception:
                logger.warning("Factor {} failed", factor_name, exc_info=True)

    # ── Hook 3: Strategy → Orders ───────────────────────────────────

    async def _hook_strategy(self, ctx: HookContext) -> None:
        """Generate signals, convert to OrderRequests, place via broker.

        Pipeline: factors → strategy.generate_signals() → Signal Engine →
                  OrderRequests → broker.place_order().
        """
        if not self._active_strategy_name or not self._factor_results:
            return

        strategy = self.strategies.get(self._active_strategy_name)

        # Build factor_results dict in the format Strategy expects:
        #   {factor_name: {symbol: FactorResult}}
        factor_results: dict[str, dict[str, FactorResult]] = {}
        for fn in strategy.used_factors:
            per_sym = self._factor_results.get(fn, {})
            if per_sym:
                factor_results[fn] = per_sym

        if not factor_results:
            return

        try:
            signal = strategy.generate_signals(factor_results)
        except Exception:
            logger.warning("Strategy {} failed", self._active_strategy_name, exc_info=True)
            return

        # Signal Engine: weights → orders
        prices = self.scheduler.snapshot()["prices"]
        orders = generate_orders(
            signals=signal.positions,
            state=self._state,
            prices=prices,
        )

        for req in orders:
            order, reason = await self.broker.place_order(req)
            if order:
                logger.info(
                    "Order placed: {} {} {:.4f} {}",
                    order.symbol, order.side, order.qty, order.status,
                )
            else:
                logger.warning(
                    "Order rejected: {} {} — {}", req.symbol, req.side, reason,
                )

    # ── Hook 4: Trading Engine ─────────────────────────────────────

    async def _hook_trading(self, ctx: HookContext) -> None:
        """Broker tick + sync local mirror."""
        await self.broker.tick()

        # Sync local mirror from broker (source of truth)
        self._state = await self.broker.fetch_account_state()
        ctx.extra["equity"] = self._state.equity()

    # ── Consistency ─────────────────────────────────────────────────

    def _run_consistency_check(self, strategy_name: str) -> None:
        strategy = self.strategies.get(strategy_name)
        factor_to_providers: dict[str, dict[str, str]] = {}
        for fn in strategy.used_factors:
            cfg = factor_entry(self.config, fn)
            if not cfg:
                continue
            factor = resolve_factor(fn)
            factor_to_providers[fn] = {
                dt: default_provider_for(
                    factor, dt, config=self.config, registry=self.providers,
                ).provider_id
                for dt in factor.required_data
            }

        check_consistency(
            strategy_name=strategy_name,
            consumer=self.consumer,
            factor_registry=self.factors,
            factor_to_providers=factor_to_providers,
        )

    # ── Public API ──────────────────────────────────────────────────

    @property
    def state(self) -> AccountState:
        """Current local mirror of account state (fast, no I/O)."""
        return self._state
