"""End-to-end pipeline coverage for funding rate, OI, and basis factors."""

import asyncio

import numpy as np
import pandas as pd

from superplatform.data.enums import DataFrequency
from superplatform.data.provider_registry import DataProvider, DataProviderRegistry
from superplatform.data.providers.synthetic import SyntheticKLineProvider
from superplatform.runtime.config import Config
from superplatform.runtime.pipeline import OfflineRuntime


class SyntheticSeriesProvider(DataProvider):
    """Generate schema-correct non-kline data and record requested frequency."""

    def __init__(self, provider_id: str, data_type: str, available_frequencies=None):
        self.provider_id = provider_id
        self.data_type = data_type
        self.frequencies: list[DataFrequency] = []
        self.available_frequencies = available_frequencies

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        del kwargs
        self.frequencies.append(frequency)
        start_ts = pd.Timestamp(start or "2024-01-01", tz="UTC")
        end_ts = pd.Timestamp(end or "2024-06-30", tz="UTC")
        pandas_frequency = {
            DataFrequency.H8: "8h",
            DataFrequency.H4: "4h",
            DataFrequency.D1: "1D",
        }[frequency]
        timestamps = pd.date_range(start_ts, end_ts, freq=pandas_frequency)
        symbol_scale = 1.0 if symbol == "S1" else 2.0
        index = np.arange(len(timestamps), dtype=float)

        if self.data_type == "funding_rate":
            return pd.DataFrame({
                "timestamp": timestamps,
                "funding_rate": (index + 1.0) * symbol_scale * 0.000001,
            })
        if self.data_type == "open_interest":
            return pd.DataFrame({
                "timestamp": timestamps,
                "open_interest": 1_000.0 + index * symbol_scale,
            })
        if self.data_type == "basis":
            return pd.DataFrame({
                "timestamp": timestamps,
                "spot_price": 40_000.0 + index,
                "perpetual_price": 40_000.0 + index + symbol_scale,
                "basis_pct": (index + 1.0) * symbol_scale * 0.001,
            })
        raise AssertionError(f"Unsupported data type: {self.data_type}")


def test_pipeline_evaluates_non_kline_factors_with_correct_schemas_and_frequencies():
    registry = DataProviderRegistry()
    funding = SyntheticSeriesProvider("test-funding", "funding_rate")
    open_interest = SyntheticSeriesProvider("test-oi", "open_interest")
    basis = SyntheticSeriesProvider("test-basis", "basis")
    registry.register(funding)
    registry.register(open_interest)
    registry.register(basis)
    registry.register(SyntheticKLineProvider(seed=42, provider_id="test-evaluation-kline"))

    config = Config({
        "evaluation": {
            "sample_start": "2024-01-01",
            "sample_end": "2024-05-31",
            "forward_bias": {"n_cutoffs": 3},
        },
        "factors": {
            "funding_rate_annualized": {
                "symbols": ["S1", "S2"],
                "providers": {"funding_rate": "test-funding"},
                "frequency": "4h",
                "evaluation_price": {
                    "provider": "test-evaluation-kline",
                    "frequency": "4h",
                },
            },
            "oi_change_ratio": {
                "symbols": ["S1", "S2"],
                "providers": {"open_interest": "test-oi"},
                "frequencies": {"open_interest": "4h"},
                "evaluation_price": {
                    "provider": "test-evaluation-kline",
                    "frequency": "4h",
                },
                "params": {"lookback_days": 10},
            },
            "basis_latest": {
                "symbols": ["S1", "S2"],
                "providers": {"basis": "test-basis"},
                "frequency": "1d",
                "evaluation_price": {
                    "provider": "test-evaluation-kline",
                    "frequency": "1d",
                },
            },
        },
    })

    results = asyncio.run(OfflineRuntime(config, registry).run(skip_report=True))

    assert [result.factor_name for result in results] == [
        "basis_latest",
        "funding_rate_annualized",
        "oi_change_ratio",
    ]
    for result in results:
        assert result.forward_bias_passed
        assert not result.ic_df.empty
        assert all(report["schema_validation"]["valid"] for report in result.validation_reports)
        assert all(
            factor_result.values["value"].notna().any()
            for factor_result in result.per_symbol.values()
        )

    assert funding.frequencies == [DataFrequency.H4, DataFrequency.H4]
    assert open_interest.frequencies == [DataFrequency.H4, DataFrequency.H4]
    assert basis.frequencies == [DataFrequency.D1, DataFrequency.D1]


def test_funding_factor_at_8h_run_cadence():
    """A run cadence of 8h fetches funding natively at 8h and builds an 8h panel."""
    registry = DataProviderRegistry()
    funding = SyntheticSeriesProvider(
        "test-funding", "funding_rate", available_frequencies={DataFrequency.H8}
    )
    registry.register(funding)
    registry.register(SyntheticKLineProvider(seed=42, provider_id="test-evaluation-kline"))

    config = Config({
        "evaluation": {
            "sample_start": "2024-01-01",
            "sample_end": "2024-05-31",
            "forward_bias": {"n_cutoffs": 3},
        },
        "factors": {
            "funding_rate_annualized": {
                "symbols": ["S1", "S2"],
                "providers": {"funding_rate": "test-funding"},
                "frequency": "8h",
                "evaluation_price": {
                    "provider": "test-evaluation-kline",
                    "frequency": "8h",
                },
            },
        },
    })

    results = asyncio.run(OfflineRuntime(config, registry).run(skip_report=True))
    result = results[0]
    assert result.factor_name == "funding_rate_annualized"
    assert result.forward_bias_passed

    cross = result.cross_section
    assert not cross.empty
    assert (cross["frequency"] == "8h").all()
    # 1-bar horizon at the run cadence: exit_ts − entry_ts == 8h.
    assert (cross["exit_ts"] - cross["entry_ts"] == pd.Timedelta("8h")).all()
    # Both symbols fetched funding natively at 8h.
    assert funding.frequencies == [DataFrequency.H8, DataFrequency.H8]

    # Annualization: funding_rate 1e-6 × 1095 periods/year × 100 == 0.1095%.
    s1 = cross[cross["symbol"] == "S1"].sort_values("timestamp")
    assert abs(float(s1["factor_value"].iloc[0]) - 0.1095) < 1e-9
