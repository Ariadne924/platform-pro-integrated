"""Tests for Binance non-kline DataProvider wrappers."""

from datetime import UTC, datetime

import pandas as pd
import pytest

from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.providers import setup_providers
from superplatform.data.providers.binance_basis import BinanceBasisProvider
from superplatform.data.providers.binance_funding_rate import BinanceFundingRateProvider
from superplatform.data.providers.binance_open_interest import BinanceOpenInterestProvider


class StubBinanceAdapter:
    def __init__(self):
        self.calls = []

    async def fetch_funding_rate(self, **kwargs):
        self.calls.append(("funding_rate", kwargs))
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=2, freq="8h", tz="UTC"),
                "funding_rate": [0.0001, 0.0002],
            }
        )

    async def fetch_open_interest(self, **kwargs):
        self.calls.append(("open_interest", kwargs))
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=2, freq="1D", tz="UTC"),
                "open_interest": [1_000.0, 1_100.0],
            }
        )

    async def fetch_basis(self, **kwargs):
        self.calls.append(("basis", kwargs))
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=2, freq="1D", tz="UTC"),
                "spot_price": [40_000.0, 40_100.0],
                "perpetual_price": [40_020.0, 40_130.0],
                "basis_pct": [0.05, 0.075],
            }
        )


@pytest.mark.asyncio
async def test_funding_rate_provider_delegates_and_normalizes_bounds():
    adapter = StubBinanceAdapter()
    provider = BinanceFundingRateProvider(adapter=adapter)

    data = await provider.fetch(
        "BTCUSDT",
        DataFrequency.H1,
        start=datetime(2024, 1, 1),
        end=datetime(2024, 1, 2),
        limit=100,
    )

    name, kwargs = adapter.calls[0]
    assert name == "funding_rate"
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["start"].tzinfo == UTC
    assert kwargs["end"].tzinfo == UTC
    assert kwargs["limit"] == 100
    assert list(data.columns) == ["timestamp", "funding_rate"]


@pytest.mark.asyncio
async def test_open_interest_provider_maps_frequency_and_market_type():
    adapter = StubBinanceAdapter()
    provider = BinanceOpenInterestProvider(adapter=adapter)

    data = await provider.fetch("BTCUSDT", DataFrequency.D1, limit=250)

    name, kwargs = adapter.calls[0]
    assert name == "open_interest"
    assert kwargs["market_type"] == MarketType.PERPETUAL
    assert kwargs["period"] == "1d"
    assert kwargs["limit"] == 250
    assert list(data.columns) == ["timestamp", "open_interest"]


@pytest.mark.asyncio
async def test_open_interest_provider_rejects_unsupported_frequency():
    provider = BinanceOpenInterestProvider(adapter=StubBinanceAdapter())

    with pytest.raises(ValueError, match="unsupported"):
        await provider.fetch("BTCUSDT", DataFrequency.M1)


@pytest.mark.asyncio
async def test_basis_provider_requires_daily_frequency_and_delegates():
    adapter = StubBinanceAdapter()
    provider = BinanceBasisProvider(adapter=adapter)

    data = await provider.fetch("BTCUSDT", DataFrequency.D1)

    assert adapter.calls == [(
        "basis",
        {"symbol": "BTCUSDT", "start": None, "end": None, "limit": 1000},
    )]
    assert list(data.columns) == [
        "timestamp",
        "spot_price",
        "perpetual_price",
        "basis_pct",
    ]

    with pytest.raises(ValueError, match="daily"):
        await provider.fetch("BTCUSDT", DataFrequency.H1)


def test_setup_providers_registers_all_binance_data_types():
    registry = DataProviderRegistry()
    setup_providers(registry)

    assert {
        "binance-perp-funding-rate",
        "binance-perp-open-interest",
        "binance-basis",
    }.issubset(registry.list_all())
    assert registry.list_by_data_type("funding_rate") == ["binance-perp-funding-rate"]
    assert registry.list_by_data_type("open_interest") == ["binance-perp-open-interest"]
    assert registry.list_by_data_type("basis") == ["binance-basis"]


def test_setup_providers_shares_one_binance_adapter_and_limiter():
    registry = DataProviderRegistry()
    setup_providers(registry)

    provider_ids = [
        "binance-perp-kline",
        "binance-spot-kline",
        "binance-perp-funding-rate",
        "binance-perp-open-interest",
        "binance-basis",
    ]
    adapters = [registry.get(provider_id).adapter for provider_id in provider_ids]

    assert len({id(adapter) for adapter in adapters}) == 1
    assert len({id(adapter._rate_limiter) for adapter in adapters}) == 1
