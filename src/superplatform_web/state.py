"""Shared mutable state for the web application.

Holds module-level singletons (config, providers, store, live runtime) that
are imported by both ``app.py`` and route modules.  Extracting them into a
separate module avoids the circular import that would otherwise occur when
``app.py`` tries to import routers that themselves import from ``app.py``.

Lifecycle note: ``config`` and ``providers`` are NEVER rebound to a new
object — they are mutated in place.  Several route modules capture the
reference via ``from superplatform_web.state import providers`` at import time;
replacing the object would leave those imports pointing at a stale registry.
"""

from pathlib import Path

from superplatform.data.provider_registry import DataProviderRegistry
from superplatform.data.provider_registry import (
    resolve_provider_for_data_type as _resolve_provider,
)
from superplatform.data.store import Store
from superplatform.runtime.config import Config
from superplatform_web.evaluation_cache import EvaluationResultCache

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # state.py → project root

# ── Singletons (mutated in place, never rebound) ────────────────────
config: Config = Config()
providers: DataProviderRegistry = DataProviderRegistry()
store: Store | None = None
live_runtime = None
evaluation_cache = EvaluationResultCache()

# Runtime settings overlay is loaded LAST so it overrides the base files.
# user_groups.yaml (user-saved factor groups) sits before settings.yaml so it
# counts as a base file (survives a reset) while settings still wins any clash.
_CONFIG_FILES = (
    str(_PROJECT_ROOT / "config" / "default.yaml"),
    str(_PROJECT_ROOT / "config" / "exchanges.yaml"),
    str(_PROJECT_ROOT / "config" / "factors.yaml"),
    str(_PROJECT_ROOT / "config" / "user_groups.yaml"),
    str(_PROJECT_ROOT / "config" / "user_symbol_groups.yaml"),
    str(_PROJECT_ROOT / "config" / "settings.yaml"),
)


def reload_config() -> None:
    """Reload config from YAML files (incl. runtime settings overlay) in place."""
    config.replace(Config.load(*_CONFIG_FILES).to_dict())
    # Evaluation results depend on factor and global evaluation configuration.
    evaluation_cache.clear()


def base_config() -> Config:
    """Config loaded WITHOUT the runtime settings overlay (base defaults).

    The overlay (``config/settings.yaml``) is the last entry of ``_CONFIG_FILES``;
    dropping it yields the values a reset would restore.
    """
    return Config.load(*_CONFIG_FILES[:-1])


def _first_exchange_proxy() -> str:
    exchanges = config.get("exchanges") or {}
    for cfg in exchanges.values():
        if cfg.get("enabled", False):
            return cfg.get("proxy", "")
    return ""


def reapply_providers() -> None:
    """(Re)build the provider registry in place, honouring current config.

    Creates (or closes) the DuckDB data-cache store based on ``data.cache.*``,
    then repopulates ``providers`` so every existing reference sees the new set.
    """
    from superplatform.data.providers import setup_providers

    global store
    if store is not None:
        store.close()
        store = None

    providers.clear()
    if config.get("data.cache.enabled"):
        store = Store(config.get("data.cache.path", "data/cache.duckdb"))
    setup_providers(
        providers,
        exchange_proxy=_first_exchange_proxy(),
        store=store,
        # The vision-archive semaphore is the real parallelism cap for
        # multi-symbol cold fetches; reuse the same knob that bounds the
        # fetch coordinator so the Settings value controls both paths.
        vision_max_concurrent=_data_max_concurrent(),
    )


def _data_max_concurrent() -> int | None:
    """The configured ``data.max_concurrent_requests``, or None when unset."""
    try:
        value = int(config.get("data.max_concurrent_requests", 0))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def live_is_running() -> bool:
    return live_runtime is not None


def disabled_provider_ids() -> set[str]:
    """Provider ids toggled off via the runtime settings overlay.

    Overlay shape: ``data.providers.<provider_id>.enabled: false``
    (set through ``PUT /api/data/providers/{id}``).
    """
    disabled: set[str] = set()
    providers_cfg = config.get("data.providers") or {}
    for pid, cfg in providers_cfg.items():
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            disabled.add(pid)
    return disabled


def default_exchange() -> str:
    """Return the configured default exchange name (e.g. 'binance')."""
    return config.get("defaults.exchange", "binance")


def default_market() -> str:
    """Return the configured default market type (e.g. 'perpetual')."""
    return config.get("defaults.market", "perpetual")


# ── Provider display & resolution ───────────────────────────────────

_EXCHANGE_LABELS = {
    "binance": "Binance",
    "okx": "OKX",
    "bybit": "Bybit",
    "synthetic": "合成数据",
}
_MARKET_LABELS = {"perpetual": "永续", "spot": "现货", "coin_futures": "币本位"}
_DATA_TYPE_LABELS = {
    "kline": "K线",
    "trade": "逐笔成交",
    "funding_rate": "资金费率",
    "open_interest": "持仓量",
    "basis": "基差",
    "order_book": "盘口",
}


def provider_label(provider_id: str) -> str:
    """Human-readable label from provider metadata (exchange + market + data_type)."""
    prov = providers.get(provider_id)
    exchange = prov.exchange
    market = prov.market_type
    data_type = prov.data_type
    exchange_key = exchange or provider_id
    label = _EXCHANGE_LABELS.get(exchange_key, exchange_key)
    if market and market.value in _MARKET_LABELS:
        label += f" {_MARKET_LABELS[market.value]}"
    label += f" · {_DATA_TYPE_LABELS.get(data_type, data_type)}"
    return label


def market_label(market: str) -> str:
    return _MARKET_LABELS.get(market, market)


def exchange_label(exchange: str) -> str:
    return _EXCHANGE_LABELS.get(exchange, exchange)


def resolve_provider_for_data_type(
    exchange: str,
    market: str,
    data_type: str,
    registry: DataProviderRegistry | None = None,
    *,
    disabled: set[str] | None = None,
    allow_fallback: bool = True,
) -> str:
    """Resolve a provider matching (exchange, market, data_type).

    Re-export of the data-layer resolver (``superplatform.data.provider_registry``),
    applying the web provider toggles (``disabled_provider_ids()``) by default.
    Existing positional ``registry`` callers keep working.
    """
    reg = registry if registry is not None else providers
    if disabled is None:
        disabled = disabled_provider_ids()
    return _resolve_provider(
        exchange,
        market,
        data_type,
        reg,
        disabled=disabled,
        allow_fallback=allow_fallback,
    )
