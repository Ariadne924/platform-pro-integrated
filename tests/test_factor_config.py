"""Runtime-factor configuration coverage tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from superplatform.data.provider_registry import DataProvider, DataProviderRegistry
from superplatform.data.schema import MarketType
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.instances import instance_metadata
from superplatform.factors.metadata import FACTOR_METADATA
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import resolve_factor
from superplatform.runtime.config import Config
from superplatform.runtime.providers import default_provider_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BATCH_FACTOR_NAMES = {
    "momentum",
    "momentum_vol_adjusted",
    "trend_tstat",
    "upside_downside_vol_ratio",
    "realized_vol_change",
    "return_tail_to_median_ratio",
    "dollar_volume_momentum",
    "liquidity_regime_ratio",
    "amihud_illiquidity_change",
    "turnover_volatility",
    "wick_asymmetry",
    "range_autocorrelation",
    "close_location_surprise",
    "candle_direction_autocorrelation",
    "cross_asset_relative_momentum",
    "cross_asset_correlation",
    "cross_asset_beta",
    "cross_asset_volatility_ratio",
    "basis_funding_divergence",
    "basis_funding_divergence_zscore",
}

DEDUPLICATED_FACTOR_NAMES = {
    "momentum_10d", "momentum_20d", "momentum_60d", "momentum_120d",
    "breakout_distance_20", "drawdown_from_high_60", "high_low_spread_14",
}


class _StubProvider(DataProvider):
    """Minimal provider for resolution tests (no network)."""

    def __init__(
        self,
        provider_id: str,
        data_type: str,
        exchange: str,
        market_type: MarketType | None,
    ) -> None:
        self.provider_id = provider_id
        self.data_type = data_type
        self.exchange = exchange
        self.market_type = market_type

    async def fetch(self, *args, **kwargs):
        raise NotImplementedError


def _default_registry() -> DataProviderRegistry:
    """The binance/perpetual provider set the default resolution must match."""
    reg = DataProviderRegistry()
    reg.register(_StubProvider(
        "binance-perp-kline", "kline", "binance", MarketType.PERPETUAL,
    ))
    reg.register(_StubProvider(
        "binance-perp-funding-rate", "funding_rate", "binance", MarketType.PERPETUAL,
    ))
    reg.register(_StubProvider(
        "binance-perp-open-interest", "open_interest", "binance", MarketType.PERPETUAL,
    ))
    # basis spans markets — market_type is None, matched by the same-exchange tier.
    reg.register(_StubProvider("binance-basis", "basis", "binance", None))
    return reg


def test_all_discovered_factors_have_complete_runtime_configuration() -> None:
    """Every auto-discovered factor resolves to a provider through the defaults.

    Per-factor ``providers`` blocks are now optional; the standard Runtime
    must resolve every required data type for every factor via the
    ``defaults.exchange``/``defaults.market`` derivation against the registry.
    """
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    factors_config = Config.load(str(PROJECT_ROOT / "config/factors.yaml"))
    factors = factors_config.get("factors")
    instances = factors_config.get("factor_instances") or {}

    assert isinstance(factors, dict)
    # Factory layer: registry == factors == metadata (instances kept separate).
    assert set(registry.list_all()) == set(factors)
    assert set(registry.list_all()) == set(FACTOR_METADATA)
    assert set(FACTOR_METADATA).isdisjoint(instances)

    # Instance layer: every configured instance resolves, references a factory,
    # and derives metadata; instance names live in a distinct namespace.
    FactorInstanceRegistry.get_instance().build_from_config(factors_config, registry)
    assert set(FactorInstanceRegistry.get_instance().list_all()) == set(instances)
    for name in FactorInstanceRegistry.get_instance().list_all():
        inst = FactorInstanceRegistry.get_instance().get(name)
        assert inst.factory_name in registry.list_all()
        assert isinstance(resolve_factor(name), type(inst))
        md = instance_metadata(inst)
        assert md is not None
        assert md.default_params == instances[name].get("params")

    stub_registry = _default_registry()
    for name in registry.list_all():
        factor = registry.get(name)
        config = factors[name]
        assert isinstance(config.get("symbols"), list)
        assert config["symbols"]
        for group in config["symbols"]:
            if isinstance(group, str):
                symbols = (group,)
            else:
                assert isinstance(group, list)
                assert group
                assert all(isinstance(symbol, str) for symbol in group)
                symbols = tuple(group)
            if factor.required_symbols is not None:
                assert len(symbols) == factor.required_symbols
        for data_type in factor.required_data:
            provider = default_provider_for(
                factor, data_type, config=factors_config, registry=stub_registry,
            )
            assert provider.data_type == data_type

        if "kline" not in factor.required_data:
            evaluation_price = config.get("evaluation_price")
            assert isinstance(evaluation_price, dict)
            assert evaluation_price.get("frequency")
            # The forward-return K-line source defaults to the resolved kline.
            provider = default_provider_for(
                factor, "kline", config=factors_config, registry=stub_registry,
            )
            assert provider.data_type == "kline"


def test_expanded_factor_batch_has_all_requested_coverage() -> None:
    """The new batch is registered, configured, and described for generation."""
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    factors = Config.load(str(PROJECT_ROOT / "config/factors.yaml")).get("factors")

    assert BATCH_FACTOR_NAMES <= set(registry.list_all())
    assert BATCH_FACTOR_NAMES <= set(factors)
    assert BATCH_FACTOR_NAMES <= set(FACTOR_METADATA)


def test_parameter_only_factor_variants_are_not_registered() -> None:
    """Period presets must not be hardcoded factory factors; they may exist only
    as config-derived instances in the separate instance layer."""
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    cfg = Config.load(str(PROJECT_ROOT / "config/factors.yaml"))
    configured = cfg.get("factors")

    assert not DEDUPLICATED_FACTOR_NAMES.intersection(registry.list_all())
    assert not DEDUPLICATED_FACTOR_NAMES.intersection(configured)
    # Legacy preset names may reappear only as instances referencing a factory.
    FactorInstanceRegistry.get_instance().build_from_config(cfg, registry)
    for name in DEDUPLICATED_FACTOR_NAMES:
        if FactorInstanceRegistry.get_instance().has(name):
            inst = FactorInstanceRegistry.get_instance().get(name)
            assert inst.factory_name in registry.list_all()


def test_all_kline_factors_compute_on_valid_bars() -> None:
    """All registered K-line factors satisfy the standard output contract."""
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    timestamps = pd.date_range("2024-01-01", periods=160, freq="D", tz="UTC")
    close = pd.Series(np.linspace(100, 140, len(timestamps)))
    kline = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close * 0.998,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": np.linspace(1_000, 3_000, len(timestamps)),
        }
    )

    for name in registry.list_all():
        factor = registry.get(name)
        if factor.required_data != ["kline"]:
            continue
        factor_data = {"kline": {"TESTUSDT": kline.copy()}}
        if factor.required_symbols == 2:
            peer = kline.copy()
            peer["close"] = pd.Series(np.linspace(80, 170, len(peer)))
            factor_data["kline"]["PEERUSDT"] = peer
        result = factor.compute(
            factor_data,
            **FACTOR_METADATA[name].default_params,
        ).values
        assert result.columns.tolist() == ["timestamp", "value"], name
        assert result["timestamp"].equals(kline["timestamp"]), name


def test_all_kline_factors_tolerate_zero_range_and_zero_volume_bars() -> None:
    """Zero-range (high==low) and zero-volume bars must never crash a factor or
    leave it entirely non-finite.

    Guards three past defects: weekend factors that came out all-NaN (rolling
    with default min_periods), ``pd.NA`` zero-guards that upcast to object dtype
    (DataError), and ``pct_change`` producing +Inf on zero lagged volume.
    """
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    # ~3.3y of daily bars: the 365d lookback + 730d zscore window needs ~1095
    # bars before binance_mvrv_proxy_zscore emits a value — shorter frames make
    # it all-NaN purely by warm-up, not by the zero-range/zero-volume injections.
    timestamps = pd.date_range("2024-01-01", periods=1200, freq="D", tz="UTC")
    close = pd.Series(np.linspace(100, 140, len(timestamps)))
    kline = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close * 0.998,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": np.linspace(1_000, 3_000, len(timestamps)),
        }
    )
    # Every 17th bar is a doji (high == low); every 13th has no volume.
    kline.loc[kline.index[::17], "high"] = kline.loc[kline.index[::17], "close"]
    kline.loc[kline.index[::17], "low"] = kline.loc[kline.index[::17], "close"]
    kline.loc[kline.index[::13], "volume"] = 0.0

    for name in registry.list_all():
        factor = registry.get(name)
        if factor.required_data != ["kline"]:
            continue
        factor_data = {"kline": {"TESTUSDT": kline.copy()}}
        if factor.required_symbols == 2:
            peer = kline.copy()
            peer["close"] = pd.Series(np.linspace(80, 170, len(peer)))
            factor_data["kline"]["PEERUSDT"] = peer
        result = factor.compute(
            factor_data,
            **FACTOR_METADATA[name].default_params,
        ).values
        assert result.columns.tolist() == ["timestamp", "value"], name
        assert result["timestamp"].equals(kline["timestamp"]), name
        numeric = pd.to_numeric(result["value"], errors="coerce").to_numpy(dtype=float)
        assert np.isfinite(numeric).any(), (
            f"{name} produced no finite values on zero-range/zero-volume bars — "
            "check min_periods / pd.NA / division-by-zero handling"
        )

def test_basis_funding_proxy_factors_compute_on_native_frequencies() -> None:
    """Term-structure proxies align daily basis with prior 8-hour funding."""
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    basis_timestamps = pd.date_range("2024-01-01", periods=180, freq="D", tz="UTC")
    funding_timestamps = pd.date_range(
        "2024-01-01",
        periods=180 * 3,
        freq="8h",
        tz="UTC",
    )
    basis = pd.DataFrame(
        {
            "timestamp": basis_timestamps,
            "basis_pct": np.linspace(-0.5, 1.5, len(basis_timestamps)),
        }
    )
    funding = pd.DataFrame(
        {
            "timestamp": funding_timestamps,
            "funding_rate": np.linspace(-0.0001, 0.0002, len(funding_timestamps)),
        }
    )
    data = {
        "basis": {"TESTUSDT": basis},
        "funding_rate": {"TESTUSDT": funding},
    }

    for name in {
        "basis_funding_divergence",
        "basis_funding_divergence_zscore",
    }:
        result = registry.get(name).compute(
            data,
            **FACTOR_METADATA[name].default_params,
        ).values
        assert result.columns.tolist() == ["timestamp", "value"]
        assert result["timestamp"].equals(basis["timestamp"])
        assert result["value"].notna().any()
