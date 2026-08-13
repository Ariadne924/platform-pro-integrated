"""Broker factory — resolve the configured broker implementation.

Picks a broker from ``live.broker``:

  - ``simulated`` (default): local matching via ``SimulatedBroker``, backed by
    a synthetic or caller-supplied adapter.
  - ``binance-testnet``: real orders on the Binance USDT-M futures testnet via
    ``BinanceBroker`` + a ``UMFutures`` client with testnet API keys read from
    environment variables (never from config files).
"""

import os

from binance.um_futures import UMFutures

from superplatform.network.adapters.synthetic import SyntheticAdapter
from superplatform.network.brokers.binance import BinanceBroker
from superplatform.network.brokers.simulated import SimulatedBroker
from superplatform.runtime.config import Config

TESTNET_BASE_URL = "https://testnet.binancefuture.com"
_REQUEST_TIMEOUT_SECONDS = 10


def _first_exchange_proxy(config: Config) -> str:
    """Proxy URL from the first enabled exchange, if any."""
    exchanges = config.get("exchanges") or {}
    for cfg in exchanges.values():
        if cfg.get("enabled", False):
            return cfg.get("proxy", "")
    return ""


def build_broker(config: Config, adapter=None, symbols: list[str] | None = None):
    """Build the broker configured under ``live.broker``.

    ``adapter`` may be supplied by the caller (e.g. the web provider registry's
    Binance adapter); when omitted a synthetic adapter is used for the
    simulated broker and a fresh Binance adapter for binance-testnet.

    ``symbols`` is a per-session override (web ``live_start``) for the testnet
    subscription list; when omitted the configured ``live.symbols`` is used.
    The simulated broker does not read symbols here — the runtime's data hook
    is what restricts market data to the session selection.

    Raises RuntimeError when binance-testnet is configured but the required
    API-key environment variables are not set — a loud failure beats silently
    connecting to a testnet with empty credentials.
    """
    kind = config.get("live.broker", "simulated")
    proxy = _first_exchange_proxy(config)

    if kind == "simulated":
        initial_capital = config.get("live.paper.initial_capital_usdt", 100_000.0)
        sim_adapter = adapter or SyntheticAdapter(seed=42)
        return SimulatedBroker(adapter=sim_adapter, initial_capital=initial_capital)

    if kind == "binance-testnet":
        from superplatform.data.providers.binance_common import create_binance_adapter

        tn = config.get("live.binance_testnet") or {}
        key_env = tn.get("api_key_env", "BINANCE_TESTNET_API_KEY")
        secret_env = tn.get("api_secret_env", "BINANCE_TESTNET_API_SECRET")
        api_key = os.environ.get(key_env) or ""
        api_secret = os.environ.get(secret_env) or ""
        missing = [name for name, value in ((key_env, api_key), (secret_env, api_secret)) if not value]
        if missing:
            raise RuntimeError(
                "binance-testnet requires API keys in environment variables: "
                + ", ".join(missing)
                + " (set them, e.g. via export / $env:)"
            )

        symbols = symbols or config.get("live.symbols") or []
        if not symbols:
            raise RuntimeError(
                "binance-testnet requires live.symbols to be listed explicitly "
                "(e.g. [\"BTCUSDT\"]): the research pool data.symbols.perpetual "
                "contains delisted symbols the testnet cannot fill"
            )

        client_kwargs: dict = {
            "base_url": tn.get("base_url", TESTNET_BASE_URL),
            "timeout": _REQUEST_TIMEOUT_SECONDS,
        }
        if proxy:
            client_kwargs["proxies"] = {"http": proxy, "https": proxy}
        futures = UMFutures(key=api_key, secret=api_secret, **client_kwargs)

        binance_adapter = adapter or create_binance_adapter(proxy)
        return BinanceBroker(
            binance_adapter,
            futures,
            default_leverage=tn.get("default_leverage", 10),
            symbols=symbols,
            recv_window_ms=tn.get("recv_window_ms", 30_000),
        )

    raise ValueError(
        f"live.broker={kind!r} is not a supported broker "
        "(choose 'simulated' or 'binance-testnet')"
    )
