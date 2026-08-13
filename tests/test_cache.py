"""Tests for DuckDB-backed data caching and incremental fetch."""

import asyncio
from datetime import datetime, timedelta

import pandas as pd

from superplatform.data.cache import CachingProvider, DataCache
from superplatform.data.provider_registry import (
    DataProvider,
    DataProviderRegistry,
    resolve_provider_for_data_type,
)
from superplatform.data.schema import DataFrequency, MarketType
from superplatform.data.store import Store, provider_table


class _CountingProvider(DataProvider):
    """Provider that counts how many times fetch() is called."""

    def __init__(self, base_price: float = 100.0):
        self.provider_id = "test-kline"
        self.data_type = "kline"
        self._base_price = base_price
        self.fetch_count = 0
        self._seed = 42

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        self.fetch_count += 1
        if start is None:
            start = datetime(2024, 1, 1)
        if end is None:
            end = datetime(2024, 2, 1)

        freq_map = {
            "1d": timedelta(days=1),
            "1h": timedelta(hours=1),
        }
        delta = freq_map.get(frequency.value if hasattr(frequency, "value") else str(frequency), timedelta(days=1))

        timestamps = []
        current = pd.Timestamp(start).tz_localize("UTC") if pd.Timestamp(start).tz is None else pd.Timestamp(start)
        end_ts = pd.Timestamp(end).tz_localize("UTC") if pd.Timestamp(end).tz is None else pd.Timestamp(end)
        while current < end_ts:
            timestamps.append(current)
            current += delta

        import numpy as np
        rng = np.random.default_rng(self._seed + len(timestamps))
        n = len(timestamps)
        returns = rng.normal(0.0001, 0.01, n)
        close = self._base_price * (1 + returns).cumprod()

        return pd.DataFrame({
            "timestamp": timestamps,
            "open": close * 0.999,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": rng.lognormal(10, 1, n).astype(float),
            "quote_volume": rng.lognormal(15, 1, n).astype(float),
            "trades": rng.integers(100, 1000, n).astype(float),
            "taker_buy_volume": rng.lognormal(9, 1, n).astype(float),
            "taker_buy_quote_volume": rng.lognormal(14, 1, n).astype(float),
        })


class _GappedProvider(_CountingProvider):
    """Daily kline provider that drops one middle day (2024-01-04)."""

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        df = await super().fetch(symbol, frequency, start=start, end=end, **kwargs)
        return df[df["timestamp"] != pd.Timestamp("2024-01-04", tz="UTC")]


class _BinanceStyleProvider(_CountingProvider):
    """Mirrors real Binance providers: exchange/market_type declared as class
    attributes (or in __init__), not as plain instance attrs on the wrapper."""

    exchange = "binance"
    market_type = MarketType.PERPETUAL

    def __init__(self, base_price: float = 100.0):
        super().__init__(base_price=base_price)
        self.provider_id = "binance-perp-kline"


class _NonKlineProvider(DataProvider):
    """Funding-rate provider — generates data across a requested range."""

    def __init__(self):
        self.provider_id = "test-funding-rate"
        self.data_type = "funding_rate"
        self.fetch_count = 0

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        self.fetch_count += 1
        if start is None:
            start = datetime(2024, 1, 1)
        if end is None:
            end = datetime(2024, 1, 10)
        timestamps = []
        current = pd.Timestamp(start).tz_localize("UTC")
        end_ts = pd.Timestamp(end).tz_localize("UTC")
        while current < end_ts:
            timestamps.append(current)
            current += timedelta(hours=8)
        return pd.DataFrame({
            "timestamp": timestamps,
            "funding_rate": [0.0001] * len(timestamps),
        })


class TestDataCache:
    """Verify DataCache correctly caches, deduplicates, and fetches deltas."""

    def test_first_fetch_writes_to_store(self):
        """First fetch should call provider and persist to Store."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        df = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))

        assert not df.empty
        assert provider.fetch_count == 1
        # Verify data landed in Store (one table per provider)
        cached_range = store.series_range(
            provider_table("test-kline"), "BTCUSDT", "1d"
        )
        assert cached_range["count"] > 0
        store.close()

    def test_second_fetch_reads_from_cache(self):
        """Second fetch with same range should NOT call provider again."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        # First fetch
        df1 = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        assert provider.fetch_count == 1

        # Second fetch — same range, should hit cache
        df2 = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        assert provider.fetch_count == 1  # unchanged
        assert len(df2) == len(df1)
        store.close()

    def test_incremental_fetch_only_delta(self):
        """Extending the end date should fetch only the new segment."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        # First: 2024-01-01 → 2024-01-05
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 5),
        ))
        assert provider.fetch_count == 1

        # Second: 2024-01-01 → 2024-01-10 (extends end)
        df = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        # Should have fetched the delta (count=2) and returned cached+new
        assert provider.fetch_count == 2
        # Total rows should cover 1-1 to 1-9 (9 days, end is exclusive)
        assert len(df) >= 8
        store.close()

    def test_different_frequencies_do_not_interfere(self):
        """1h and 1d klines for the same symbol should be cached independently."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        # Fetch 1d data
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 5),
        ))
        daily_count = provider.fetch_count

        # Fetch 1h data — should be a cache miss (different frequency)
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.H1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 2),
        ))
        assert provider.fetch_count == daily_count + 1

        # Re-fetch 1d — should still hit cache
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 5),
        ))
        assert provider.fetch_count == daily_count + 1  # unchanged for 1d
        store.close()

    def test_non_kline_provider_is_cached(self):
        """Non-kline providers are cached like kline data."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _NonKlineProvider()
        wrapped = CachingProvider(provider, cache)

        df1 = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.H4,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 3),
        ))
        assert not df1.empty
        assert provider.fetch_count == 1

        df2 = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.H4,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 3),
        ))
        # Second fetch with the same range hits the cache.
        assert provider.fetch_count == 1
        assert len(df2) == len(df1)

        # Data actually landed in the per-provider funding-rate table.
        cached = store.series_range(
            provider_table("test-funding-rate"), "BTCUSDT", "4h"
        )
        assert cached["count"] > 0
        store.close()

    def test_prepend_before_cached_range(self):
        """Fetching data before the cached range should fetch the prefix."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        # Cache 2024-01-10 → 2024-01-20
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 10), end=datetime(2024, 1, 20),
        ))
        assert provider.fetch_count == 1

        # Now fetch 2024-01-01 → 2024-01-20 (prepend + cached)
        df = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 20),
        ))
        # Two more: one for [1-1, 1-10) and one for cached range via Store
        # Actually just one: the segment before cache_min
        assert provider.fetch_count == 2
        assert len(df) >= 18  # ~19 days (end is exclusive)
        store.close()

    def test_unbounded_start_fetches_and_caches(self):
        """start=None should fetch full and cache for future queries."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        df = asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1, start=None, end=None,
        ))
        assert not df.empty
        assert provider.fetch_count == 1

        # Data should be cached (check Store)
        cached = store.series_range(
            provider_table("test-kline"), "BTCUSDT", "1d"
        )
        assert cached["count"] > 0
        store.close()

    def test_empty_range_is_remembered_and_skipped(self):
        """A verified-empty segment is not re-fetched on later runs."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        # Cache 2024-01-10 → 2024-01-20, then ask for 2024-01-01 → 2024-01-20.
        # The provider returns only data >= 2024-01-10, so the prefix fetch
        # comes back empty and is recorded as an empty range.
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 10), end=datetime(2024, 1, 20),
        ))
        assert provider.fetch_count == 1

        class _RangeLimitedProvider(_CountingProvider):
            async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
                self.fetch_count += 1
                if start is not None:
                    ts = pd.Timestamp(start)
                    if ts.tzinfo is None:
                        ts = ts.tz_localize("UTC")
                    if ts < pd.Timestamp("2024-01-10", tz="UTC"):
                        return pd.DataFrame(columns=[
                            "timestamp", "open", "high", "low", "close",
                            "volume", "quote_volume", "trades",
                            "taker_buy_volume", "taker_buy_quote_volume",
                        ])
                return await super().fetch(symbol, frequency, start=start, end=end, **kwargs)

        limited = _RangeLimitedProvider()
        wrapped2 = CachingProvider(limited, cache)

        # First extended query: prefix comes back empty → recorded.
        asyncio.run(wrapped2.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 20),
        ))
        assert limited.fetch_count == 1

        # Second identical query: prefix is skipped from the empty-range
        # record; only the cached segment is read.
        asyncio.run(wrapped2.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 20),
        ))
        assert limited.fetch_count == 1  # unchanged — no re-fetch

        assert store.covers_empty_range(
            provider_table("test-kline"), "BTCUSDT", "1d",
            pd.Timestamp("2024-01-01", tz="UTC"),
            pd.Timestamp("2024-01-10", tz="UTC"),
        )
        store.close()

    def test_internal_gaps_recorded_as_empty_ranges(self):
        """A fetched segment with a hole in the middle is bookmarked empty.

        Mirrors Binance vision archives that skip missing daily files: the
        gap is inside the range, not at an edge, so only the internal-gap
        recording catches it.
        """
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _GappedProvider()  # no bar on 2024-01-04
        wrapped = CachingProvider(provider, cache)

        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        assert provider.fetch_count == 1

        # The gap between 01-03 and 01-05 is recorded as verified-empty.
        assert store.covers_empty_range(
            provider_table("test-kline"), "BTCUSDT", "1d",
            pd.Timestamp("2024-01-04", tz="UTC"),
            pd.Timestamp("2024-01-05", tz="UTC"),
        )
        # A range outside the gap is not covered.
        assert not store.covers_empty_range(
            provider_table("test-kline"), "BTCUSDT", "1d",
            pd.Timestamp("2024-01-02", tz="UTC"),
            pd.Timestamp("2024-01-03", tz="UTC"),
        )

        # Second identical fetch hits the cache; no re-fetch, gap still known.
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        assert provider.fetch_count == 1
        store.close()

    def test_different_symbols_isolated(self):
        """BTC and ETH data should not interfere."""
        store = Store(":memory:")
        cache = DataCache(store)
        provider = _CountingProvider()
        wrapped = CachingProvider(provider, cache)

        # Fetch BTC
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        btc_fetches = provider.fetch_count

        # Fetch ETH — different symbol, cache miss
        asyncio.run(wrapped.fetch(
            "ETHUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        assert provider.fetch_count == btc_fetches + 1

        # Re-fetch BTC — still cached
        asyncio.run(wrapped.fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 10),
        ))
        assert provider.fetch_count == btc_fetches + 1  # unchanged
        store.close()

    def test_provider_table_naming(self):
        """provider_table sanitizes provider ids into DuckDB-safe names."""
        assert provider_table("binance-perp-kline") == "pv_binance_perp_kline"
        assert provider_table("binance-perp-funding-rate") == "pv_binance_perp_funding_rate"
        assert provider_table("synthetic-kline") == "pv_synthetic_kline"

    def test_ensure_provider_table_records_metadata(self, tmp_path):
        """ensure_provider_table is idempotent and self-describing."""
        path = tmp_path / "cache.duckdb"
        store = Store(path)
        table = store.ensure_provider_table("binance-perp-kline", "kline")
        assert table == "pv_binance_perp_kline"
        assert store.ensure_provider_table("binance-perp-kline", "kline") == table
        store.close()

        from superplatform.data.snapshot import DataSnapshot
        with DataSnapshot(path) as snapshot:
            rows = snapshot.provider_series()
            assert len(rows) == 1
            assert rows[0]["provider_id"] == "binance-perp-kline"
            assert rows[0]["data_type"] == "kline"
            assert rows[0]["table_name"] == "pv_binance_perp_kline"

    def test_two_providers_share_no_table(self):
        """Same data_type + symbol + freq from two providers stay separate."""
        store = Store(":memory:")
        cache = DataCache(store)

        p1 = _CountingProvider(base_price=100.0)
        p1.provider_id = "provider-one"
        p2 = _CountingProvider(base_price=200.0)
        p2.provider_id = "provider-two"

        asyncio.run(CachingProvider(p1, cache).fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 5),
        ))
        asyncio.run(CachingProvider(p2, cache).fetch(
            "BTCUSDT", DataFrequency.D1,
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 5),
        ))

        t1, t2 = provider_table("provider-one"), provider_table("provider-two")
        assert t1 != t2
        assert store.series_range(t1, "BTCUSDT", "1d")["count"] > 0
        assert store.series_range(t2, "BTCUSDT", "1d")["count"] > 0

        q1 = store.query_series(t1, "BTCUSDT", "1d")
        q2 = store.query_series(t2, "BTCUSDT", "1d")
        # The two providers' bars are physically separate — not shared rows.
        assert not q1["close"].equals(q2["close"])
        store.close()

    def test_wrapper_preserves_exchange_and_market_type(self):
        """CachingProvider must not shadow exchange/market_type with the base
        class's None defaults (regression: __getattr__ never fires because the
        base class declares them as class attributes)."""
        store = Store(":memory:")
        cache = DataCache(store)
        wrapped = CachingProvider(_BinanceStyleProvider(), cache)

        assert wrapped.exchange == "binance"
        assert wrapped.market_type == MarketType.PERPETUAL
        assert wrapped.data_type == "kline"
        store.close()

    def test_wrapped_provider_resolves_over_fallback(self):
        """A registry holding wrapped providers must resolve the exact
        (exchange, market, data_type) match — not fall through to an
        arbitrary same-data-type provider."""
        store = Store(":memory:")
        cache = DataCache(store)
        reg = DataProviderRegistry()

        synthetic = _CountingProvider()
        synthetic.provider_id = "synthetic-kline"
        reg.register(CachingProvider(synthetic, cache))  # registered first

        reg.register(CachingProvider(_BinanceStyleProvider(), cache))

        assert resolve_provider_for_data_type(
            "binance", "perpetual", "kline", reg
        ) == "binance-perp-kline"
        store.close()
