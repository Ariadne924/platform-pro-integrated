"""Hot-swappable data provider registry.

Data providers encapsulate "where data comes from". They bridge network
layer adapters, local caches, or hybrid sources. Validation is NOT their
responsibility — Runtime calls validators.py separately after fetching.

Registry maps provider_id (e.g. 'binance-kline') to implementations.
Consumers query by data_type, not by source.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import pandas as pd

from superplatform.data.schema import DataFrequency, MarketType


class DataProvider(ABC):
    """Abstract data provider — encapsulates data source, not validation.

    Each provider has:
    - A unique `provider_id` (e.g. 'binance-kline')
    - A `data_type` it serves (e.g. 'kline')
    - An `exchange` name (e.g. 'binance') for automatic resolution
    - An optional `market_type` (spot / perpetual / …); None when the
      data type spans markets (e.g. basis)
    """

    provider_id: str
    data_type: str
    exchange: str | None = None
    market_type: MarketType | None = None
    # DataFrequencies this provider can serve natively. None means "all
    # DataFrequency members" (used by providers whose fetch handles any
    # cadence, e.g. kline).
    available_frequencies: set[DataFrequency] | None = None

    @abstractmethod
    async def fetch(
        self,
        symbol: str,
        frequency: DataFrequency,
        start: datetime | None = None,
        end: datetime | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Fetch data from the underlying source.

        Returns a DataFrame with columns matching the data_type's Schema.
        Validation (schema, UTC, outliers, mixing) is done by Runtime
        via validators.py after this call returns.
        """
        ...


class DataProviderRegistry:
    """Registry of all data providers, keyed by provider_id.

    Usage:
        registry = DataProviderRegistry()
        registry.register(BinanceKLineProvider())
        provider = registry.get("binance-kline")
        df = await provider.fetch("BTCUSDT", DataFrequency.H1)
        # Then: Runtime calls validators.full_validation_report(df, KLineSchema)
    """

    def __init__(self):
        self._providers: dict[str, DataProvider] = {}

    def register(self, provider: DataProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"Provider '{provider.provider_id}' already registered")
        self._providers[provider.provider_id] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def clear(self) -> None:
        """Remove all providers. Used to re-populate a live registry in place."""
        self._providers.clear()

    def get(self, provider_id: str) -> DataProvider:
        if provider_id not in self._providers:
            raise KeyError(f"Provider '{provider_id}' not found")
        return self._providers[provider_id]

    def list_by_data_type(self, data_type: str) -> list[str]:
        """Return provider_ids that serve a given data type."""
        return [
            pid
            for pid, p in self._providers.items()
            if p.data_type == data_type
        ]

    def list_by_market_type(self, market_type: MarketType) -> list[str]:
        """Return provider_ids that serve a given market type."""
        return [
            pid
            for pid, p in self._providers.items()
            if p.market_type == market_type
        ]

    def list_all(self) -> list[str]:
        return sorted(self._providers.keys())

    def __contains__(self, provider_id: str) -> bool:
        return provider_id in self._providers


def resolve_provider_for_data_type(
    exchange: str,
    market: str | MarketType,
    data_type: str,
    registry: DataProviderRegistry,
    *,
    disabled: set[str] | None = None,
    allow_fallback: bool = True,
) -> str:
    """Find a provider id matching (exchange, market, data_type).

    Resolution order when ``allow_fallback`` is true:
    1. Exact match: exchange + market + data_type
    2. Same exchange + data_type (any market)
    3. Any provider serving data_type

    ``disabled`` excludes provider ids from consideration (e.g. providers
    toggled off via the web settings overlay). The web layer passes
    ``disabled_provider_ids()``; the CLI passes nothing (no overlay loaded).
    """
    disabled = disabled or set()
    market_val = market if isinstance(market, str) else market.value
    available = [pid for pid in registry.list_all() if pid not in disabled]

    # Tier 1: exact (exchange, market, data_type)
    for pid in available:
        p = registry.get(pid)
        p_market_val = p.market_type.value if p.market_type else None
        if (
            p.data_type == data_type
            and p.exchange == exchange
            and p_market_val == market_val
        ):
            return pid

    if not allow_fallback:
        raise ValueError(
            f"No exact provider for data_type='{data_type}' exchange='{exchange}' "
            f"market='{market_val}'"
        )

    # Tier 2: same exchange + data_type, any market
    for pid in available:
        p = registry.get(pid)
        if p.data_type == data_type and p.exchange == exchange:
            return pid

    # Tier 3: any provider serving this data_type
    candidates = [
        pid for pid in registry.list_by_data_type(data_type) if pid not in disabled
    ]
    if candidates:
        return candidates[0]

    raise ValueError(
        f"No provider for data_type='{data_type}' exchange='{exchange}' market='{market}'"
        f" — available: {registry.list_all()}"
    )
