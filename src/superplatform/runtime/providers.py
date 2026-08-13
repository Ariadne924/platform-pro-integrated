"""Unified default-provider resolution shared by the CLI and web paths.

This is the single place that decides "which provider serves this factor's
data_type". The three-layer precedence makes ``defaults.exchange`` a global
data-source switch while still allowing per-factor and per-data-type pinning:

    ① per-factor ``providers`` block (config/factors.yaml, now optional)
    ② ``defaults.providers.<data_type>`` — a global override map, primarily
       for non-exchange / vendor sources that have no natural
       (exchange, market) home, and for deliberate per-data-type deviations.
    ③ derived from ``defaults.exchange`` + ``defaults.market`` against the
       provider registry (the "exchange implies providers" step).

Both the offline pipeline and the web research path resolve through here, so
switching ``defaults.exchange`` changes every factor's source consistently.
"""

from __future__ import annotations

import logging
from typing import Any

from superplatform.data.provider_registry import (
    DataProvider,
    DataProviderRegistry,
    resolve_provider_for_data_type,
)

logger = logging.getLogger(__name__)


def default_provider_for(
    factor: Any,
    data_type: str,
    *,
    config: Any,
    registry: DataProviderRegistry,
    factor_providers: dict[str, str] | None = None,
    disabled: set[str] | None = None,
) -> DataProvider:
    """Resolve the provider that serves ``data_type`` for ``factor``.

    Args:
        factor: The factor object (uses ``.name`` for error messages).
        data_type: A provider data type (``kline``, ``funding_rate``, ...).
        config: A runtime ``Config`` or a plain dict. ``defaults`` is read
            one level deep, so both work.
        registry: The provider registry to resolve against.
        factor_providers: The factor's explicit ``providers`` map (layer ①).
        disabled: Provider ids excluded from the derived tier (layer ③). The
            web passes ``disabled_provider_ids()``; the CLI passes nothing.

    Returns:
        The resolved DataProvider. Callers use ``.provider_id``.
    """
    name = getattr(factor, "name", str(factor))

    # ① per-factor explicit override — beats everything.
    pid = (factor_providers or {}).get(data_type)
    if pid:
        return _require(registry, pid, data_type, name)

    defaults = _defaults(config)
    exchange = defaults.get("exchange", "binance")
    market = defaults.get("market", "perpetual")

    # ② global unified override (defaults.providers.<data_type>).
    overrides = defaults.get("providers")
    if isinstance(overrides, dict):
        override_pid = overrides.get(data_type)
        if override_pid:
            try:
                derived_pid = _derive_id(exchange, market, data_type, registry, disabled)
            except ValueError:
                derived_pid = None
            if derived_pid == override_pid:
                logger.warning(
                    "defaults.providers.%s = %s is redundant — it equals the "
                    "derived default for exchange=%s market=%s; consider removing it",
                    data_type, override_pid, exchange, market,
                )
            return _require(registry, override_pid, data_type, name)

    # ③ derive from defaults.exchange + defaults.market.
    return _require(
        registry,
        _derive_id(exchange, market, data_type, registry, disabled),
        data_type,
        name,
    )


def _defaults(config: Any) -> dict:
    """Return the ``defaults`` section of a Config or plain dict (or {})."""
    if config is None:
        return {}
    section = config.get("defaults")
    return section if isinstance(section, dict) else {}


def _derive_id(
    exchange: str,
    market: str,
    data_type: str,
    registry: DataProviderRegistry,
    disabled: set[str] | None,
) -> str:
    """Resolve the provider id derived from ``(exchange, market, data_type)``."""
    return resolve_provider_for_data_type(
        exchange, market, data_type, registry, disabled=disabled
    )


def _require(
    registry: DataProviderRegistry,
    provider_id: str,
    data_type: str,
    factor_name: str,
) -> DataProvider:
    """Look up a provider id and enforce that it serves ``data_type``."""
    if provider_id not in registry:
        raise ValueError(
            f"Provider '{provider_id}' for factor '{factor_name}' data_type "
            f"'{data_type}' is not registered — available: {registry.list_all()}"
        )
    provider = registry.get(provider_id)
    if provider.data_type != data_type:
        raise ValueError(
            f"Provider '{provider_id}' serves '{provider.data_type}', not "
            f"required data type '{data_type}'"
        )
    return provider
