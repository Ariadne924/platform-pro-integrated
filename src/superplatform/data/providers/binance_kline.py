"""Binance kline data provider.

Wraps BinanceAdapter as a DataProvider so the offline research pipeline
(OfflineRuntime) can fetch real exchange data through the same interface
it uses for synthetic data.
"""

from datetime import datetime

import pandas as pd

from superplatform.data.provider_registry import DataProvider
from superplatform.data.providers.binance_common import create_binance_adapter
from superplatform.data.schema import DataFrequency, MarketType
from superplatform.network.base import KLineInterval
from superplatform.network.binance import BinanceAdapter
from superplatform.utils.time_utils import to_utc

# ── DataFrequency → KLineInterval mapping ──────────────────────────
_FREQ_TO_INTERVAL: dict[DataFrequency, KLineInterval] = {
    DataFrequency.M1: KLineInterval.M1,
    DataFrequency.M5: KLineInterval.M5,
    DataFrequency.M15: KLineInterval.M15,
    DataFrequency.M30: KLineInterval.M30,
    DataFrequency.H1: KLineInterval.H1,
    DataFrequency.H4: KLineInterval.H4,
    DataFrequency.H8: KLineInterval.H8,
    DataFrequency.D1: KLineInterval.D1,
    DataFrequency.W1: KLineInterval.W1,
}

class BinanceKLineProvider(DataProvider):
    """Real Binance kline data provider.

    Wraps a BinanceAdapter to serve kline data through the DataProvider
    interface. Works with both spot and perpetual markets.

    Usage:
        provider = BinanceKLineProvider(market_type=MarketType.PERPETUAL)
        df = await provider.fetch("BTCUSDT", DataFrequency.H1,
                                  start=datetime(2024,1,1), end=datetime(2024,6,1))
    """

    data_type = "kline"
    exchange = "binance"
    available_frequencies = set(_FREQ_TO_INTERVAL)

    def __init__(
        self,
        market_type: MarketType = MarketType.PERPETUAL,
        provider_id: str = "binance-kline",
        adapter: BinanceAdapter | None = None,
        proxy: str = "",
    ):
        self.market_type = market_type
        self.provider_id = provider_id
        self._adapter = adapter
        self._proxy = proxy

    @property
    def adapter(self) -> BinanceAdapter:
        """Lazy-init the adapter (allows sharing across providers)."""
        if self._adapter is None:
            self._adapter = create_binance_adapter(self._proxy)
        return self._adapter

    async def fetch(
        self,
        symbol: str,
        frequency: DataFrequency,
        start: datetime | None = None,
        end: datetime | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """Fetch kline data from Binance.

        Args:
            symbol: Trading pair, e.g. 'BTCUSDT'.
            frequency: Bar frequency (must map to a KLineInterval).
            start: Start of range (UTC). None → no lower bound.
            end: End of range (UTC). None → fetches up to `limit` bars.
            **kwargs: Passed through (e.g. limit=1000).

        Returns:
            DataFrame with KLineSchema columns.
        """
        interval = _FREQ_TO_INTERVAL.get(frequency)
        if interval is None:
            raise ValueError(
                f"DataFrequency {frequency} cannot be mapped to a KLineInterval. "
                f"Supported: {list(_FREQ_TO_INTERVAL.keys())}"
            )

        limit = kwargs.get("limit", 500)

        if start is not None:
            start = to_utc(start)
        if end is not None:
            end = to_utc(end)

        # The adapter always returns a full-schema DataFrame; validate
        # it here before returning so the contract is consistent.
        df = await self.adapter.fetch_klines(
            symbol=symbol,
            interval=interval,
            market_type=self.market_type,
            start=start,
            end=end,
            limit=limit,
        )

        return df
