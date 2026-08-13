"""Data providers package.

Each provider module implements DataProvider for a specific source + data type.
Call setup_providers() to populate a DataProviderRegistry with available providers.
"""

from __future__ import annotations

import logging

from superplatform.data.cache import CachingProvider, DataCache
from superplatform.data.provider_registry import DataProvider, DataProviderRegistry
from superplatform.data.providers.binance_basis import BinanceBasisProvider
from superplatform.data.providers.binance_common import create_binance_adapter
from superplatform.data.providers.binance_funding_rate import BinanceFundingRateProvider
from superplatform.data.providers.binance_kline import BinanceKLineProvider
from superplatform.data.providers.binance_open_interest import BinanceOpenInterestProvider
from superplatform.data.providers.synthetic import SyntheticKLineProvider
from superplatform.data.schema import MarketType
from superplatform.data.store import Store

_logger = logging.getLogger(__name__)


def setup_providers(
    registry: DataProviderRegistry,
    exchange_proxy: str = "",
    store: Store | None = None,
    vision_max_concurrent: int | None = None,
) -> None:
    """Register all available data providers on the given registry.

    Args:
        registry: The DataProviderRegistry to populate.
        exchange_proxy: Optional HTTP proxy for exchange connections
                        (e.g. 'http://127.0.0.1:7890'). Used by all exchange
                        adapters. Also overridable via HTTPS_PROXY env var.
        store: Optional DuckDB Store. When provided, every registered
               provider is wrapped with CachingProvider for transparent
               incremental fetching + local persistence.
        vision_max_concurrent: Archive-download semaphore for the shared
               Binance vision client (default 8). Mirrors
               ``data.max_concurrent_requests``.

    Factors and strategies reference providers by ID in config/factors.yaml;
    changing a provider ID there switches data sources without touching
    factor/strategy code.
    """
    cache: DataCache | None = None
    if store is not None:
        cache = DataCache(store)

    # Synthetic (testing / offline development)
    _register(registry, SyntheticKLineProvider(seed=42), cache)

    # Binance (real exchange data)
    try:
        # One adapter owns both ccxt clients and one limiter accounts for all
        # Binance endpoints, including the two legs of basis requests.
        adapter = create_binance_adapter(
            exchange_proxy, vision_max_concurrent=vision_max_concurrent
        )
        _register(registry, BinanceKLineProvider(
            market_type=MarketType.PERPETUAL,
            provider_id="binance-perp-kline",
            adapter=adapter,
            proxy=exchange_proxy,
        ), cache)
        _register(registry, BinanceKLineProvider(
            market_type=MarketType.SPOT,
            provider_id="binance-spot-kline",
            adapter=adapter,
            proxy=exchange_proxy,
        ), cache)
        _register(registry, BinanceFundingRateProvider(
            adapter=adapter, proxy=exchange_proxy,
        ), cache)
        _register(registry, BinanceOpenInterestProvider(
            adapter=adapter, proxy=exchange_proxy,
        ), cache)
        _register(registry, BinanceBasisProvider(
            adapter=adapter, proxy=exchange_proxy,
        ), cache)
    except Exception:
        _logger.warning(
            "Binance providers not registered (network / ccxt unavailable). "
            "They will be available once the adapter can connect.",
            exc_info=True,
        )


def _register(
    registry: DataProviderRegistry,
    provider: DataProvider,
    cache: DataCache | None,
) -> None:
    """Register a provider, optionally wrapping it with CachingProvider."""
    if cache is not None:
        provider = CachingProvider(provider, cache)
    registry.register(provider)
