"""Unit tests for native-cadence factor availability (run-level evaluation cadence)."""


from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.provider_registry import DataProvider, DataProviderRegistry
from superplatform.factors.registry import FactorRegistry
from superplatform.runtime.config import Config
from superplatform_web.research import factor_available_frequencies


class _MockProvider(DataProvider):
    exchange = "synthetic"
    market_type = MarketType.PERPETUAL
    available_frequencies: set[DataFrequency] | None = None

    def __init__(self, provider_id: str = "mock-kline", data_type: str = "kline"):
        self.provider_id = provider_id
        self.data_type = data_type

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _factor(name):
    FactorRegistry.get_instance().auto_discover()
    return FactorRegistry.get_instance().get(name)


def _registry(*providers) -> DataProviderRegistry:
    registry = DataProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return registry


def _kline_provider():
    return _MockProvider()


def _funding_provider():
    provider = _MockProvider(provider_id="mock-funding", data_type="funding_rate")
    provider.available_frequencies = {DataFrequency.H8}
    return provider


def _oi_provider():
    provider = _MockProvider(provider_id="mock-oi", data_type="open_interest")
    provider.available_frequencies = {
        DataFrequency.M5, DataFrequency.M15, DataFrequency.M30,
        DataFrequency.H1, DataFrequency.H4, DataFrequency.D1,
    }
    return provider


def _basis_provider():
    provider = _MockProvider(provider_id="mock-basis", data_type="basis")
    provider.available_frequencies = {DataFrequency.D1}
    return provider


def _all_values() -> list[str]:
    return [f.value for f in DataFrequency]


def test_kline_factor_available_at_all_cadences():
    """A kline factor is available anywhere its kline provider can serve — unset means all."""
    registry = _registry(_kline_provider())
    available = factor_available_frequencies(
        _factor("momentum"), registry, "synthetic", "perpetual", Config()
    )
    assert available == _all_values()


def test_funding_factor_available_only_at_8h():
    """funding_rate_annualized requires funding (8h) AND eval-price kline → only 8h."""
    registry = _registry(_kline_provider(), _funding_provider())
    available = factor_available_frequencies(
        _factor("funding_rate_annualized"), registry, "synthetic", "perpetual", Config()
    )
    assert available == ["8h"]


def test_oi_factor_available_at_oi_native_cadences():
    registry = _registry(_kline_provider(), _oi_provider())
    available = factor_available_frequencies(
        _factor("oi_change_ratio"), registry, "synthetic", "perpetual", Config()
    )
    assert available == ["5m", "15m", "30m", "1h", "4h", "1d"]


def test_basis_factor_available_only_at_1d():
    registry = _registry(_kline_provider(), _basis_provider())
    available = factor_available_frequencies(
        _factor("basis_latest"), registry, "synthetic", "perpetual", Config()
    )
    assert available == ["1d"]


def test_factor_without_provider_is_unavailable():
    """No provider for a required data type → [] (not an all-members assumption)."""
    registry = _registry(_kline_provider())  # no funding provider registered
    available = factor_available_frequencies(
        _factor("funding_rate_annualized"), registry, "synthetic", "perpetual", Config()
    )
    assert available == []


def test_unset_available_frequencies_means_all_members():
    """available_frequencies=None is the 'all cadences' contract."""
    assert _declared(_kline_provider()) == set(DataFrequency)


def test_evaluation_price_provider_config_restricts_availability():
    """A configured evaluation_price.provider that lacks 8h narrows the factor."""
    registry = _registry(_funding_provider())
    # funding provider registered but NO kline provider at all; the factor config
    # names a kline provider that doesn't exist → unresolvable → [].
    base_config = Config({
        "factors": {
            "funding_rate_annualized": {
                "evaluation_price": {"provider": "missing-kline"},
            }
        }
    })
    available = factor_available_frequencies(
        _factor("funding_rate_annualized"), registry, "synthetic", "perpetual", base_config
    )
    assert available == []


def _declared(provider) -> set[DataFrequency]:
    from superplatform_web.research import _declared_frequencies

    return _declared_frequencies(provider)


def test_resolved_providers_map_short_circuits_resolution():
    """resolved_providers (data_type → provider_id) is used verbatim when given."""
    registry = _registry(_funding_provider())
    factor = _factor("funding_rate_annualized")
    # kline resolution is NOT in the map, so the eval-price branch resolves it;
    # funding is in the map. No kline provider → still [] because eval price
    # needs a kline source.
    available = factor_available_frequencies(
        factor, registry, "synthetic", "perpetual", Config(),
        resolved_providers={"funding_rate": "mock-funding"},
    )
    assert available == []


def test_factor_config_schema_frequency_enum_is_native(monkeypatch):
    """The config form's frequency dropdown only offers native cadences (+ saved value)."""
    import types

    import superplatform_web.state as _state
    from superplatform_web.routes.factors import _factor_config_schema

    registry = _registry(_kline_provider(), _funding_provider(), _basis_provider())
    cfg = Config({"defaults": {"exchange": "synthetic", "market": "perpetual"}})
    monkeypatch.setattr(_state, "config", cfg)
    app = types.SimpleNamespace(state=types.SimpleNamespace(providers=registry, config=cfg))
    request = types.SimpleNamespace(app=app)

    schema = _factor_config_schema(_factor("funding_rate_annualized"), {}, request)
    freq = next(f for f in schema if f["key"] == "frequency")
    assert freq["enum"] == ["8h"]

    schema_basis = _factor_config_schema(_factor("basis_latest"), {"frequency": "1d"}, request)
    freq_basis = next(f for f in schema_basis if f["key"] == "frequency")
    assert freq_basis["enum"] == ["1d"]
    # evaluation_price.frequency is constrained the same way.
    ep_field = next(f for f in schema_basis if f["key"] == "evaluation_price")
    ep_freq = next(f for f in ep_field["children"] if f["key"] == "evaluation_price.frequency")
    assert ep_freq["enum"] == ["1d"]

    # A saved non-native value stays selectable so existing configs round-trip.
    schema_legacy = _factor_config_schema(_factor("basis_latest"), {"frequency": "4h"}, request)
    freq_legacy = next(f for f in schema_legacy if f["key"] == "frequency")
    assert freq_legacy["enum"] == ["1d", "4h"]


def test_caching_wrapper_preserves_native_cadence_contract(tmp_path):
    """Regression: CachingProvider must forward the inner provider's cadence contract.

    With the DuckDB cache enabled every provider is CachingProvider-wrapped.
    The base ``DataProvider.available_frequencies = None`` default would shadow
    the inner provider's declaration through normal class-attribute lookup
    (``__getattr__`` is never reached for an attribute that exists), making
    every factor look available at every cadence.
    """
    from superplatform.data.cache import CachingProvider, DataCache
    from superplatform.data.providers.binance_basis import BinanceBasisProvider
    from superplatform.data.providers.binance_funding_rate import BinanceFundingRateProvider
    from superplatform.data.providers.binance_kline import BinanceKLineProvider
    from superplatform.data.schema import MarketType
    from superplatform.data.store import Store

    store = Store(tmp_path / "cache.duckdb")
    try:
        basis = CachingProvider(BinanceBasisProvider(), DataCache(store))
        funding = CachingProvider(BinanceFundingRateProvider(), DataCache(store))
        kline = CachingProvider(
            BinanceKLineProvider(market_type=MarketType.PERPETUAL, provider_id="binance-perp-kline"),
            DataCache(store),
        )
        assert _declared(basis) == {DataFrequency.D1}
        assert _declared(funding) == {DataFrequency.H8}

        registry = _registry(basis, funding, kline)
        assert factor_available_frequencies(
            _factor("basis_latest"), registry, "binance", "perpetual", Config()
        ) == ["1d"]
        assert factor_available_frequencies(
            _factor("funding_rate_annualized"), registry, "binance", "perpetual", Config()
        ) == ["8h"]
    finally:
        store.close()
