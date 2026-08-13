"""Multi-symbol evaluations must survive symbols that produce no data.

Two distinct failure modes break a kline-factor batch over the full-market
universe unless handled:

1. A symbol whose fetch *raises* (e.g. Binance ``-1121 Invalid symbol`` for a
   delisted contract). The pipeline isolates the failure into an empty,
   schema-shaped frame and logs it instead of aborting the whole batch.
2. A symbol that fetches an *empty* frame because it has no data in the chosen
   window (e.g. POLUSDT before its 2024 rename from MATIC). The empty merge is
   skipped in ``_build_cross_section`` and the forward-bias gate is skipped.

Both used to crash the entire evaluation with ``KeyError('timestamp')`` /
``TypeError`` on the empty frame, which is exactly why selecting a kline factor
over many symbols appeared to "only cover a few".
"""

import asyncio

import pandas as pd
import pytest

from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.providers.synthetic import SyntheticKLineProvider
from superplatform.data.schema import KLineSchema
from superplatform.data.validators import full_validation_report
from superplatform.factors.base import FactorCategory, FactorResult
from superplatform.factors.registry import FactorRegistry
from superplatform.runtime.config import Config
from superplatform.runtime.pipeline import OfflineRuntime

# ── Providers that mimic the two failure modes ────────────────────────

class _EmptySymbolProvider(SyntheticKLineProvider):
    """Returns a schema-shaped empty frame for configured symbols (no data)."""

    def __init__(self, empty_symbols=()):
        super().__init__(seed=42)
        self.empty_symbols = set(empty_symbols)

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        if symbol in self.empty_symbols:
            cols = list(KLineSchema.columns.keys())
            return pd.DataFrame({c: pd.Series(dtype="datetime64[ns]" if c == "timestamp" else "float64") for c in cols})
        return await super().fetch(symbol, frequency, start, end, **kwargs)


class _RaisingProvider(SyntheticKLineProvider):
    """Raises for configured symbols (delisted / network failure)."""

    def __init__(self, failing_symbols=()):
        super().__init__(seed=42)
        self.failing_symbols = set(failing_symbols)

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        if symbol in self.failing_symbols:
            raise RuntimeError(f"fetch failed for {symbol}")
        return await super().fetch(symbol, frequency, start, end, **kwargs)


# ── Unit: validators tolerate a column-less empty frame ───────────────

def test_full_validation_report_tolerates_columnless_frame():
    report = full_validation_report(pd.DataFrame(), KLineSchema)
    assert report["row_count"] == 0
    assert report["utc_check"]["error"] == "missing column: timestamp"
    assert report["time_range"] == {"start": None, "end": None}


# ── Unit: _build_cross_section skips a group with no overlapping data ─

def _kline_df(start="2024-01-01", n=10):
    ts = pd.date_range(start, periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open": [1.0] * n,
        "high": [1.0] * n,
        "low": [1.0] * n,
        "close": [1.0] * n,
        "volume": [100.0] * n,
    })


def test_build_cross_section_skips_empty_group():
    empty_kline = _kline_df()[:0]
    # Factor output on the empty (but schema-typed) kline keeps datetime64 dtype.
    empty_fv = pd.DataFrame({"timestamp": empty_kline["timestamp"], "value": pd.Series(dtype="float64")})
    good_kline = _kline_df()
    good_fv = pd.DataFrame({"timestamp": good_kline["timestamp"], "value": [0.1] * len(good_kline)})
    per_group = {
        "EMPTY": FactorResult(name="momentum", category=FactorCategory.MOMENTUM_REVERSAL, values=empty_fv),
        "S1": FactorResult(name="momentum", category=FactorCategory.MOMENTUM_REVERSAL, values=good_fv),
    }
    df = OfflineRuntime._build_cross_section(
        per_group,
        {"EMPTY": empty_kline, "S1": good_kline},
        factor_name="momentum",
        exchange="synthetic",
        market_type="perpetual",
        bar_interval="1d",
        frequency="1d",
    )
    assert set(df["symbol"].unique()) == {"S1"}
    assert "ret_1" in df.columns


# ── Integration: full batch over mixed good/bad symbols ──────────────

def _batch_config(symbols):
    return Config({
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "factors": {"momentum": {
            "symbols": symbols,
            "providers": {"kline": "synthetic-kline"},
            "params": {"lookback_days": 20},
        }},
        "evaluation": {"forward_bias": {"n_cutoffs": 5}},
    })


def _run(symbols, provider):
    FactorRegistry.get_instance().auto_discover()
    reg = DataProviderRegistry()
    reg.register(provider)
    runtime = OfflineRuntime(_batch_config(symbols), reg)
    return asyncio.run(runtime.run(["momentum"], skip_report=True))


def test_batch_survives_symbol_with_no_data_in_window():
    results = _run(
        ["S1", "S2", "EMPTY"],
        _EmptySymbolProvider(empty_symbols=("EMPTY",)),
    )
    xs = results[0].cross_section
    assert set(xs["symbol"].unique()) == {"S1", "S2"}
    # Forward-bias gate still passes overall (the empty group is skipped, not failed).
    assert results[0].forward_bias_passed is True


def test_batch_survives_symbol_whose_fetch_raises():
    results = _run(
        ["S1", "S2", "BROKEN"],
        _RaisingProvider(failing_symbols=("BROKEN",)),
    )
    xs = results[0].cross_section
    assert set(xs["symbol"].unique()) == {"S1", "S2"}
    assert results[0].forward_bias_passed is True


def test_batch_all_symbols_empty_raises_clear_error():
    # Every group empty → no cross-section can be built; must raise a clear error,
    # not a confusing pandas TypeError/KeyError.
    with pytest.raises(ValueError, match="No evaluation data produced"):
        _run(["EMPTY1", "EMPTY2"], _EmptySymbolProvider(empty_symbols=("EMPTY1", "EMPTY2")))
