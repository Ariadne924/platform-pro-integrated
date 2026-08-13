"""Binance open-interest data provider."""

from datetime import datetime

import pandas as pd

from superplatform.data.provider_registry import DataProvider
from superplatform.data.providers.binance_common import create_binance_adapter
from superplatform.data.schema import DataFrequency, MarketType
from superplatform.network.binance import BinanceAdapter
from superplatform.utils.time_utils import to_utc

_OPEN_INTEREST_PERIODS: dict[DataFrequency, str] = {
    DataFrequency.M5: "5m",
    DataFrequency.M15: "15m",
    DataFrequency.M30: "30m",
    DataFrequency.H1: "1h",
    DataFrequency.H4: "4h",
    DataFrequency.D1: "1d",
}

class BinanceOpenInterestProvider(DataProvider):
    """Serve Binance perpetual open-interest history through DataProvider."""

    data_type = "open_interest"
    market_type = MarketType.PERPETUAL
    exchange = "binance"
    available_frequencies = set(_OPEN_INTEREST_PERIODS)

    def __init__(
        self,
        provider_id: str = "binance-perp-open-interest",
        adapter: BinanceAdapter | None = None,
        proxy: str = "",
    ):
        self.provider_id = provider_id
        self._adapter = adapter
        self._proxy = proxy

    @property
    def adapter(self) -> BinanceAdapter:
        """Create the network adapter only when data is first requested."""
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
        """Fetch perpetual open-interest history at a Binance-supported period."""
        period = _OPEN_INTEREST_PERIODS.get(frequency)
        if period is None:
            supported = ", ".join(freq.value for freq in _OPEN_INTEREST_PERIODS)
            raise ValueError(
                f"DataFrequency {frequency.value} is unsupported for Binance open interest. "
                f"Supported: {supported}"
            )

        if start is not None:
            start = to_utc(start)
        if end is not None:
            end = to_utc(end)

        return await self.adapter.fetch_open_interest(
            symbol=symbol,
            market_type=self.market_type,
            period=period,
            start=start,
            end=end,
            limit=kwargs.get("limit", 500),
        )
