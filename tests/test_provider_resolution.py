"""Default-provider resolution tests (superplatform/runtime/providers.py)."""

from __future__ import annotations

import logging

import pytest

from superplatform.data.provider_registry import (
    DataProvider,
    DataProviderRegistry,
    resolve_provider_for_data_type,
)
from superplatform.data.schema import MarketType
from superplatform.runtime.config import Config
from superplatform.runtime.providers import default_provider_for


class _StubFactor:
    name = "stub"


class _StubProvider(DataProvider):
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


def _registry() -> DataProviderRegistry:
    reg = DataProviderRegistry()
    reg.register(_StubProvider(
        "binance-perp-kline", "kline", "binance", MarketType.PERPETUAL,
    ))
    reg.register(_StubProvider(
        "okx-perp-kline", "kline", "okx", MarketType.PERPETUAL,
    ))
    reg.register(_StubProvider(
        "binance-perp-funding-rate", "funding_rate", "binance", MarketType.PERPETUAL,
    ))
    reg.register(_StubProvider(
        "kaiko-perp-funding-rate", "funding_rate", "kaiko", MarketType.PERPETUAL,
    ))
    return reg


def _config(defaults: dict) -> Config:
    return Config({"defaults": defaults})


def test_exchange_implies_providers() -> None:
    """③ the derived tier: defaults.exchange + market pick the provider."""
    reg = _registry()
    factor = _StubFactor()
    provider = default_provider_for(
        factor, "kline",
        config=_config({"exchange": "binance", "market": "perpetual"}),
        registry=reg,
    )
    assert provider.provider_id == "binance-perp-kline"

    provider = default_provider_for(
        factor, "kline",
        config=_config({"exchange": "okx", "market": "perpetual"}),
        registry=reg,
    )
    assert provider.provider_id == "okx-perp-kline"


def test_defaults_providers_override_wins() -> None:
    """② a global defaults.providers entry beats the exchange derivation."""
    reg = _registry()
    factor = _StubFactor()
    cfg = _config({
        "exchange": "binance",
        "market": "perpetual",
        "providers": {"funding_rate": "kaiko-perp-funding-rate"},
    })
    provider = default_provider_for(factor, "funding_rate", config=cfg, registry=reg)
    assert provider.provider_id == "kaiko-perp-funding-rate"

    # Other data types still derive from the exchange.
    provider = default_provider_for(factor, "kline", config=cfg, registry=reg)
    assert provider.provider_id == "binance-perp-kline"


def test_factor_level_providers_beat_defaults() -> None:
    """① a per-factor providers block beats both defaults layers."""
    reg = _registry()
    factor = _StubFactor()
    cfg = _config({
        "exchange": "okx",
        "market": "perpetual",
        "providers": {"kline": "okx-perp-kline"},
    })
    provider = default_provider_for(
        factor, "kline", config=cfg, registry=reg,
        factor_providers={"kline": "binance-perp-kline"},
    )
    assert provider.provider_id == "binance-perp-kline"


def test_redundant_defaults_provider_warns(caplog) -> None:
    """A defaults.providers entry equal to the derivation logs a warning."""
    reg = _registry()
    factor = _StubFactor()
    cfg = _config({
        "exchange": "binance",
        "market": "perpetual",
        "providers": {"kline": "binance-perp-kline"},
    })
    with caplog.at_level(logging.WARNING, logger="superplatform.runtime.providers"):
        provider = default_provider_for(factor, "kline", config=cfg, registry=reg)
    assert provider.provider_id == "binance-perp-kline"
    assert any("redundant" in record.message for record in caplog.records)


def test_disabled_filters_derived_tier() -> None:
    """disabled excludes providers only from the derived tier (③)."""
    reg = _registry()
    factor = _StubFactor()
    cfg = _config({"exchange": "binance", "market": "perpetual"})

    # Disabling the derived provider falls through to the any-provider tier.
    provider = default_provider_for(
        factor, "kline", config=cfg, registry=reg,
        disabled={"binance-perp-kline"},
    )
    assert provider.provider_id == "okx-perp-kline"

    # Disabling every kline provider makes the derivation unresolvable.
    with pytest.raises(ValueError):
        default_provider_for(
            factor, "kline", config=cfg, registry=reg,
            disabled={"binance-perp-kline", "okx-perp-kline"},
        )

    # An explicit per-factor pin is honored even when disabled (deliberate).
    provider = default_provider_for(
        factor, "kline", config=cfg, registry=reg,
        factor_providers={"kline": "binance-perp-kline"},
        disabled={"binance-perp-kline"},
    )
    assert provider.provider_id == "binance-perp-kline"


def test_plain_dict_config() -> None:
    """config may be a plain dict, not just a Config."""
    reg = _registry()
    factor = _StubFactor()
    provider = default_provider_for(
        factor, "kline",
        config={"defaults": {"exchange": "okx", "market": "perpetual"}},
        registry=reg,
    )
    assert provider.provider_id == "okx-perp-kline"


def test_unresolvable_raises() -> None:
    reg = _registry()
    factor = _StubFactor()
    cfg = _config({"exchange": "binance", "market": "perpetual"})
    with pytest.raises(ValueError):
        default_provider_for(factor, "trade", config=cfg, registry=reg)


def test_resolve_provider_data_layer() -> None:
    """The data-layer resolver honors disabled and the 3-tier fallback."""
    reg = _registry()
    assert resolve_provider_for_data_type(
        "binance", "perpetual", "kline", reg
    ) == "binance-perp-kline"
    # Same exchange, any market (basis has market_type None).
    assert resolve_provider_for_data_type(
        "binance", "perpetual", "funding_rate", reg
    ) == "binance-perp-funding-rate"
    # Any provider when the exchange has none.
    assert resolve_provider_for_data_type(
        "kaiko", "perpetual", "kline", reg
    ) == "binance-perp-kline"
    with pytest.raises(ValueError, match="No exact provider"):
        resolve_provider_for_data_type(
            "binance",
            "spot",
            "kline",
            reg,
            allow_fallback=False,
        )
    with pytest.raises(ValueError):
        resolve_provider_for_data_type(
            "binance", "perpetual", "trade", reg
        )
