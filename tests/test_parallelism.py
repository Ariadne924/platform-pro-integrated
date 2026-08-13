"""Concurrency and request-deduplication tests for Runtime data loading."""

import asyncio

import pandas as pd

from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.providers.synthetic import SyntheticKLineProvider
from superplatform.network.adapters.synthetic import SyntheticAdapter
from superplatform.network.brokers.simulated import SimulatedBroker
from superplatform.runtime.config import Config
from superplatform.runtime.live import LiveRuntime
from superplatform.runtime.pipeline import OfflineRuntime
from superplatform.runtime.scheduler import HookContext


class CountingKLineProvider(SyntheticKLineProvider):
    """Synthetic provider that records I/O concurrency."""

    def __init__(self):
        super().__init__(seed=42, provider_id="counting-kline")
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch(self, *args, **kwargs):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return await super().fetch(*args, **kwargs)
        finally:
            self.in_flight -= 1


class DelayedAdapter(SyntheticAdapter):
    """Synthetic market-data adapter that records I/O concurrency."""

    def __init__(self):
        super().__init__(seed=42)
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_klines(self, *args, **kwargs):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return await super().fetch_klines(*args, **kwargs)
        finally:
            self.in_flight -= 1


def test_offline_run_deduplicates_requests_and_respects_limit():
    provider = CountingKLineProvider()
    registry = DataProviderRegistry()
    registry.register(provider)
    config = Config({
        "data": {"max_concurrent_requests": 2},
        "factors": {
            name: {
                "symbols": ["S1", "S2"],
                "providers": {"kline": "counting-kline"},
                "params": {"lookback_days": 20},
            }
            for name in ["momentum", "realized_vol", "volume_ratio"]
        },
        "evaluation": {"forward_bias": {"n_cutoffs": 2}},
    })

    runtime = OfflineRuntime(config, registry)
    results = asyncio.run(runtime.run(skip_report=True))

    assert len(results) == 3
    # Three factors ask for the same two inputs. A run-scoped cache shares
    # each request while each caller gets an independent DataFrame copy.
    assert provider.calls == 2
    assert provider.max_in_flight == 2


def test_live_price_fetches_are_bounded_and_parallel():
    adapter = DelayedAdapter()
    broker = SimulatedBroker(adapter)
    config = Config({
        "data": {
            "max_concurrent_requests": 2,
            "symbols": {"perpetual": ["S1", "S2", "S3"]},
        },
    })
    runtime = LiveRuntime(config, DataProviderRegistry(), broker)
    context = HookContext(tick_no=1, tick_time=0.0, prices={})

    asyncio.run(runtime._hook_data(context))

    assert adapter.max_in_flight == 2
    assert set(runtime._data_buffer) == {"S1", "S2", "S3"}
    assert set(broker.last_prices) == {"S1", "S2", "S3"}


class StaticKLineAdapter:
    """Market-data adapter returning the same kline window on every call.

    Simulates the live case where the tick interval is shorter than the M1 bar
    width, so successive fetches return the same in-progress bar.
    """

    def __init__(self, n: int = 200):
        self.name = "static-kline"
        timestamps = pd.date_range(
            "2026-08-01", periods=n, freq="1min", tz="UTC",
        ).as_unit("ns")
        close = [60000.0 + i for i in range(n)]
        self._frame = pd.DataFrame({
            "timestamp": timestamps,
            "open": close,
            "high": [c * 1.001 for c in close],
            "low": [c * 0.999 for c in close],
            "close": close,
            "volume": [100.0] * n,
            "quote_volume": [6_000_000.0] * n,
            "trades": [float(i) for i in range(n)],
            "taker_buy_volume": [50.0] * n,
            "taker_buy_quote_volume": [3_000_000.0] * n,
        })

    async def fetch_klines(self, symbol, interval, market_type,
                           start=None, end=None, limit=500):
        return self._frame


def test_live_buffer_does_not_accumulate_duplicate_bars():
    """Repeatedly appending the same in-progress bar would produce duplicate
    timestamps — which breaks the strategy panel join (reindex on duplicate
    labels) and grows the buffer without bound. Only newer bars are appended."""
    adapter = StaticKLineAdapter()
    broker = SimulatedBroker(adapter)
    config = Config({"data": {"symbols": {"perpetual": ["S1"]}}})
    runtime = LiveRuntime(config, DataProviderRegistry(), broker)
    context = HookContext(tick_no=1, tick_time=0.0, prices={})

    asyncio.run(runtime._hook_data(context))  # prime: 200 bars
    asyncio.run(runtime._hook_data(context))  # same in-progress bar again

    rows = runtime._data_buffer["S1"]
    assert len(rows) == 200, "repeated in-progress bar must not be appended"
    timestamps = [r["timestamp"] for r in rows]
    assert len(set(timestamps)) == len(timestamps), "buffer timestamps must be unique"
