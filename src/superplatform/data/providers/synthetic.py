"""Synthetic data provider for testing and development.

Generates realistic-but-fake OHLCV kline data so the full pipeline
can be tested without a real exchange connection.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from superplatform.data.provider_registry import DataProvider
from superplatform.data.schema import DataFrequency, MarketType
from superplatform.utils.time_utils import to_utc


def _generate_klines(
    symbol: str,
    frequency: DataFrequency,
    start: datetime,
    end: datetime,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic OHLCV data with random walk prices."""
    rng = np.random.default_rng(seed + hash(symbol) % 10000)

    freq_map = {
        DataFrequency.M1: timedelta(minutes=1),
        DataFrequency.M5: timedelta(minutes=5),
        DataFrequency.M15: timedelta(minutes=15),
        DataFrequency.M30: timedelta(minutes=30),
        DataFrequency.H1: timedelta(hours=1),
        DataFrequency.H4: timedelta(hours=4),
        DataFrequency.H8: timedelta(hours=8),
        DataFrequency.D1: timedelta(days=1),
        DataFrequency.W1: timedelta(weeks=1),
    }
    delta = freq_map.get(frequency, timedelta(hours=1))

    timestamps = []
    current = start
    while current <= end:
        timestamps.append(current)
        current += delta

    n = len(timestamps)
    if n < 2:
        raise ValueError(f"Not enough periods between {start} and {end} at {frequency}")

    # Random walk with drift + noise
    base_price = 60000.0
    drift = rng.normal(0.0001, 0.0005, n)
    shocks = rng.normal(0, 0.005, n)
    log_returns = drift + shocks
    log_price = np.log(base_price) + np.cumsum(log_returns)
    close = np.exp(log_price)

    # OHLC from close
    intraday_noise = rng.normal(0, 0.003, (n, 4))
    high = close * (1 + np.abs(intraday_noise[:, 0]))
    low = close * (1 - np.abs(intraday_noise[:, 1]))
    open_price = close * (1 + intraday_noise[:, 2])
    # close is close

    volume = rng.lognormal(10, 1, n)
    quote_volume = volume * close * 0.5
    trades = rng.integers(100, 10000, n)
    taker_buy_vol = volume * rng.uniform(0.3, 0.7, n)

    return pd.DataFrame({
        "timestamp": pd.to_datetime(timestamps, utc=True).as_unit("ns"),
        "open": open_price.astype(np.float64),
        "high": high.astype(np.float64),
        "low": low.astype(np.float64),
        "close": close.astype(np.float64),
        "volume": volume.astype(np.float64),
        "quote_volume": quote_volume.astype(np.float64),
        "trades": trades.astype(np.float64),
        "taker_buy_volume": taker_buy_vol.astype(np.float64),
        "taker_buy_quote_volume": (taker_buy_vol * close * 0.5).astype(np.float64),
    })


class SyntheticKLineProvider(DataProvider):
    """Generates fake kline data for testing."""

    provider_id = "synthetic-kline"
    data_type = "kline"
    market_type = MarketType.PERPETUAL
    exchange = "synthetic"
    available_frequencies = set(
        (
            DataFrequency.M1,
            DataFrequency.M5,
            DataFrequency.M15,
            DataFrequency.M30,
            DataFrequency.H1,
            DataFrequency.H4,
            DataFrequency.H8,
            DataFrequency.D1,
            DataFrequency.W1,
        )
    )

    def __init__(self, seed: int = 42, provider_id: str | None = None):
        self._seed = seed
        if provider_id is not None:
            self.provider_id = provider_id

    async def fetch(
        self,
        symbol: str,
        frequency: DataFrequency,
        start: datetime | None = None,
        end: datetime | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        if start is None:
            start = datetime(2024, 1, 1)
        if end is None:
            end = datetime(2025, 6, 30)
        start = to_utc(start)
        end = to_utc(end)

        return _generate_klines(
            symbol=symbol,
            frequency=frequency,
            start=start,
            end=end,
            seed=self._seed,
        )
