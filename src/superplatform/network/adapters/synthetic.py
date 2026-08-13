"""Synthetic ExchangeAdapter — random-walk OHLCV for testing.

Implements ExchangeAdapter using the same random-walk generator as
SyntheticKLineProvider. All data is generated in-memory — no network.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from superplatform.network.base import ExchangeAdapter, KLineInterval


def _interval_to_timedelta(interval: KLineInterval) -> timedelta:
    """Map KLineInterval to timedelta for bar-count → time-range conversion."""
    _map = {
        KLineInterval.M1: timedelta(minutes=1),
        KLineInterval.M3: timedelta(minutes=3),
        KLineInterval.M5: timedelta(minutes=5),
        KLineInterval.M15: timedelta(minutes=15),
        KLineInterval.M30: timedelta(minutes=30),
        KLineInterval.H1: timedelta(hours=1),
        KLineInterval.H4: timedelta(hours=4),
        KLineInterval.D1: timedelta(days=1),
        KLineInterval.W1: timedelta(weeks=1),
    }
    return _map.get(interval, timedelta(hours=1))


def _generate_ohlcv(
    symbol: str,
    interval: KLineInterval,
    start: datetime,
    end: datetime,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate random-walk OHLCV data."""
    rng = np.random.default_rng(seed + hash(symbol) % 10000)

    freq_map = {
        KLineInterval.M1: timedelta(minutes=1),
        KLineInterval.M5: timedelta(minutes=5),
        KLineInterval.M15: timedelta(minutes=15),
        KLineInterval.H1: timedelta(hours=1),
        KLineInterval.H4: timedelta(hours=4),
        KLineInterval.D1: timedelta(days=1),
    }
    delta = freq_map.get(interval, timedelta(hours=1))

    timestamps = []
    current = start
    while current <= end:
        timestamps.append(current)
        current += delta

    n = len(timestamps)
    if n < 2:
        return pd.DataFrame(columns=[
            "timestamp", "open", "high", "low", "close",
            "volume", "quote_volume", "trades",
            "taker_buy_volume", "taker_buy_quote_volume",
        ])

    base_price = 60000.0
    drift = rng.normal(0.0001, 0.0005, n)
    shocks = rng.normal(0, 0.005, n)
    close = base_price * np.exp(np.cumsum(drift + shocks))

    intraday_noise = rng.normal(0, 0.003, (n, 4))
    high = close * (1 + np.abs(intraday_noise[:, 0]))
    low = close * (1 - np.abs(intraday_noise[:, 1]))
    open_price = close * (1 + intraday_noise[:, 2])

    volume = rng.lognormal(10, 1, n)
    quote_volume = volume * close * 0.5
    trades = rng.integers(100, 10000, n).astype(np.float64)
    taker_buy_vol = volume * rng.uniform(0.3, 0.7, n)

    return pd.DataFrame({
        "timestamp": pd.to_datetime(timestamps, utc=True).as_unit("ns"),
        "open": open_price.astype(np.float64),
        "high": high.astype(np.float64),
        "low": low.astype(np.float64),
        "close": close.astype(np.float64),
        "volume": volume.astype(np.float64),
        "quote_volume": quote_volume.astype(np.float64),
        "trades": trades,
        "taker_buy_volume": taker_buy_vol.astype(np.float64),
        "taker_buy_quote_volume": (taker_buy_vol * close * 0.5).astype(np.float64),
    })


class SyntheticAdapter(ExchangeAdapter):
    """In-memory ExchangeAdapter using random-walk OHLCV.

    Use for testing the live pipeline without a network connection.
    Each fetch_* call generates fresh random data for the requested range.
    """

    def __init__(self, seed: int = 42):
        super().__init__(name="synthetic", rate_limiter=None)
        self._seed = seed
        self._default_start = datetime(2024, 1, 1, tzinfo=UTC)
        self._default_end = datetime(2025, 6, 30, tzinfo=UTC)

    async def fetch_klines(self, symbol, interval, market_type,
                           start=None, end=None, limit=500):
        s = start or self._default_start
        if end:
            e = end
        else:
            # Convert bar count to time range using the interval
            delta = _interval_to_timedelta(interval)
            e = s + delta * limit
        return _generate_ohlcv(symbol, interval, s, e, seed=self._seed)

    async def fetch_trades(self, symbol, market_type,
                           start=None, end=None, limit=1000):
        s = start or self._default_start
        _e = end or (s + timedelta(hours=limit))
        now = pd.Timestamp.now(tz="UTC")
        n = min(limit, 100)
        rng = np.random.default_rng(self._seed + hash(symbol) % 10000)
        prices = rng.uniform(60000, 65000, n)
        return pd.DataFrame({
            "timestamp": [now] * n,
            "price": prices.astype(np.float64),
            "quantity": rng.uniform(0.01, 2.0, n).astype(np.float64),
            "is_buyer_maker": rng.choice([True, False], n),
            "trade_id": np.arange(n, dtype=np.int64),
        })

    async def fetch_order_book(self, symbol, market_type, depth=20):
        price = 65000.0
        rng = np.random.default_rng(self._seed)
        bids_qty = rng.uniform(0.1, 5.0, depth)
        asks_qty = rng.uniform(0.1, 5.0, depth)
        bids = pd.DataFrame({
            "timestamp": pd.Timestamp.now(tz="UTC"),
            "side": "bid",
            "price": (price - np.arange(depth) * 10).astype(np.float64),
            "quantity": bids_qty.astype(np.float64),
        })
        asks = pd.DataFrame({
            "timestamp": pd.Timestamp.now(tz="UTC"),
            "side": "ask",
            "price": (price + np.arange(depth) * 10).astype(np.float64),
            "quantity": asks_qty.astype(np.float64),
        })
        return {"timestamp": pd.Timestamp.now(tz="UTC"), "bids": bids, "asks": asks}

    async def fetch_funding_rate(self, symbol, start=None, end=None, limit=500):
        s = start or self._default_start
        n = limit
        rng = np.random.default_rng(self._seed + hash(symbol) % 10000)
        timestamps = pd.date_range(s, periods=n, freq="8h", tz="UTC")
        return pd.DataFrame({
            "timestamp": timestamps,
            "funding_rate": rng.normal(0.0001, 0.0003, n).astype(np.float64),
            "mark_price": rng.uniform(60000, 65000, n).astype(np.float64),
        })

    async def fetch_open_interest(self, symbol, market_type, period="5m",
                                  start=None, end=None, limit=500):
        s = start or self._default_start
        n = limit
        rng = np.random.default_rng(self._seed + hash(symbol) % 10000)
        timestamps = pd.date_range(s, periods=n, freq="5min", tz="UTC")
        oi = 10000.0 + np.cumsum(rng.normal(0, 50, n))
        return pd.DataFrame({
            "timestamp": timestamps,
            "open_interest": oi.astype(np.float64),
        })

    async def fetch_basis(self, symbol, start=None, end=None):
        s = start or self._default_start
        n = 30
        rng = np.random.default_rng(self._seed)
        timestamps = pd.date_range(s, periods=n, freq="1d", tz="UTC")
        spot = rng.uniform(60000, 65000, n).astype(np.float64)
        basis = rng.normal(0.001, 0.005, n).astype(np.float64)
        return pd.DataFrame({
            "timestamp": timestamps,
            "spot_price": spot,
            "perpetual_price": spot * (1 + basis),
            "basis_pct": basis * 100,
        })

    async def subscribe_klines(self, *a, **kw): pass
    async def subscribe_trades(self, *a, **kw): pass
    async def subscribe_order_book(self, *a, **kw): pass
