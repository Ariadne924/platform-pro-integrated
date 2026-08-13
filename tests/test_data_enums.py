"""DataFrequency.H8 and provider native-cadence declarations."""

import asyncio
from datetime import datetime

import pandas as pd

from superplatform.data.enums import DataFrequency
from superplatform.data.providers.binance_basis import BinanceBasisProvider
from superplatform.data.providers.binance_funding_rate import BinanceFundingRateProvider
from superplatform.data.providers.binance_kline import _FREQ_TO_INTERVAL, BinanceKLineProvider
from superplatform.data.providers.binance_open_interest import BinanceOpenInterestProvider
from superplatform.data.providers.synthetic import SyntheticKLineProvider
from superplatform.network.base import KLineInterval


def test_data_frequency_h8():
    assert DataFrequency.H8.value == "8h"
    order = list(DataFrequency)
    assert order.index(DataFrequency.H4) < order.index(DataFrequency.H8) < order.index(DataFrequency.D1)


def test_provider_available_frequencies_declarations():
    assert BinanceFundingRateProvider.available_frequencies == {DataFrequency.H8}
    assert BinanceBasisProvider.available_frequencies == {DataFrequency.D1}
    assert BinanceKLineProvider.available_frequencies == set(_FREQ_TO_INTERVAL)
    assert DataFrequency.H8 not in BinanceOpenInterestProvider.available_frequencies
    assert DataFrequency.H4 in BinanceOpenInterestProvider.available_frequencies
    assert DataFrequency.H8 in SyntheticKLineProvider.available_frequencies


def test_binance_kline_maps_h8():
    assert _FREQ_TO_INTERVAL[DataFrequency.H8] == KLineInterval.H8


def test_synthetic_kline_generates_8h_grid():
    provider = SyntheticKLineProvider(seed=1)
    df = asyncio.run(
        provider.fetch(
            "BTCUSDT",
            DataFrequency.H8,
            start=datetime(2024, 1, 1),
            end=datetime(2024, 1, 3),
        )
    )
    diffs = pd.to_datetime(df["timestamp"]).diff().dropna()
    assert not diffs.empty
    assert (diffs == pd.Timedelta(hours=8)).all()
