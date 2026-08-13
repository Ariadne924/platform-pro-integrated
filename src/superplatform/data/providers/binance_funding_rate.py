"""Binance funding-rate data provider."""

from datetime import datetime

import pandas as pd

from superplatform.data.provider_registry import DataProvider
from superplatform.data.providers.binance_common import create_binance_adapter
from superplatform.data.schema import DataFrequency, MarketType
from superplatform.network.binance import BinanceAdapter
from superplatform.utils.time_utils import to_utc


class BinanceFundingRateProvider(DataProvider):
    """Serve Binance perpetual funding-rate history through DataProvider."""

    data_type = "funding_rate"
    market_type = MarketType.PERPETUAL
    exchange = "binance"
    # Binance settles funding every 8 hours; frequency is accepted for the
    # common contract but never resamples the returned history.
    available_frequencies = {DataFrequency.H8}

    def __init__(
        self,
        provider_id: str = "binance-perp-funding-rate",
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
        """Fetch historical funding-rate observations.

        Binance determines funding observations by its settlement schedule,
        so ``frequency`` is accepted for the common DataProvider contract but
        does not resample the returned history.
        """
        del frequency
        if start is not None:
            start = to_utc(start)
        if end is not None:
            end = to_utc(end)

        return await self.adapter.fetch_funding_rate(
            symbol=symbol,
            start=start,
            end=end,
            limit=kwargs.get("limit", 500),
        )
