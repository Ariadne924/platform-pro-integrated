"""Binance spot-perpetual basis data provider."""

from datetime import datetime

import pandas as pd

from superplatform.data.provider_registry import DataProvider
from superplatform.data.providers.binance_common import create_binance_adapter
from superplatform.data.schema import DataFrequency, MarketType
from superplatform.network.binance import BinanceAdapter
from superplatform.utils.time_utils import to_utc


class BinanceBasisProvider(DataProvider):
    """Serve daily spot-perpetual basis computed by BinanceAdapter."""

    data_type = "basis"
    exchange = "binance"
    # Basis spans spot and perpetual, so it matches either market via
    # fallback in provider resolution.
    market_type: MarketType | None = None
    # Basis is computed from aligned daily spot/perpetual klines, so it is
    # natively available only at daily frequency.
    available_frequencies = {DataFrequency.D1}

    def __init__(
        self,
        provider_id: str = "binance-basis",
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
        """Fetch daily basis history.

        The adapter calculates basis from aligned daily spot and perpetual
        klines, therefore other requested frequencies would be misleading.
        """
        if frequency != DataFrequency.D1:
            raise ValueError("Binance basis is currently available only at daily frequency")
        if start is not None:
            start = to_utc(start)
        if end is not None:
            end = to_utc(end)

        return await self.adapter.fetch_basis(
            symbol=symbol,
            start=start,
            end=end,
            limit=kwargs.get("limit", 1000),
        )
