"""Abstract exchange adapter interface.

All exchange-specific implementations must subclass ExchangeAdapter.
The adapter is responsible for:
- Translating raw API responses into our unified schema primitives
- Handling rate limits (via RateLimiter)
- Reconnection and error recovery
- UTC timestamp normalization

Two modes of operation:
  Offline (pull):  Runtime calls fetch_* → returns DataFrame. Runtime owns the result.
  Live (push):     Runtime owns a DataFrame. Runtime creates a task/thread calling
                   subscribe_* methods. The network layer (implementation detail)
                   updates Runtime's DataFrame in-place. Runtime signals stop.

The network layer does NOT own DataFrames or threads — Runtime does.
The network layer provides the functions that Runtime's threads execute.

Reference design: https://github.com/sammchardy/python-binance
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional

import pandas as pd

from superplatform.data.schema import MarketType

if TYPE_CHECKING:
    from superplatform.network.rate_limiter import RateLimiter


class KLineInterval(Enum):
    """Binance-compatible kline intervals."""
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    MN1 = "1M"


class ExchangeAdapter(ABC):
    """Abstract base for exchange API adapters.

    Each subclass represents one exchange (Binance, OKX, Bybit, etc.).
    The adapter translates exchange-specific API formats into unified pandas DataFrames
    with standardized column names and UTC timestamps.

    Market type note:
        Spot and perpetual MUST NOT be mixed. The adapter enforces that each
        method call specifies exactly one MarketType.
    """

    def __init__(self, name: str, rate_limiter: Optional["RateLimiter"] = None):
        self.name = name
        self._rate_limiter = rate_limiter

    @abstractmethod
    async def fetch_klines(
        self,
        symbol: str,
        interval: KLineInterval,
        market_type: MarketType,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch OHLCV kline/candlestick data.

        Returns DataFrame with columns:
            timestamp (datetime64[ns, UTC]), open, high, low, close, volume,
            quote_volume, trades, taker_buy_volume, taker_buy_quote_volume
        """
        ...

    @abstractmethod
    async def fetch_trades(
        self,
        symbol: str,
        market_type: MarketType,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """Fetch individual trade/aggTrade data.

        Returns DataFrame with columns:
            timestamp (datetime64[ns, UTC]), price, quantity,
            is_buyer_maker, trade_id
        """
        ...

    @abstractmethod
    async def fetch_order_book(
        self,
        symbol: str,
        market_type: MarketType,
        depth: int = 20,
    ) -> dict:
        """Fetch order book depth snapshot.

        Returns dict with keys:
            timestamp (datetime64[ns, UTC]),
            bids (DataFrame: price, quantity),
            asks (DataFrame: price, quantity)
        """
        ...

    @abstractmethod
    async def fetch_funding_rate(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch historical funding rates (perpetual only).

        Returns DataFrame with columns:
            timestamp (datetime64[ns, UTC]), funding_rate, mark_price
        """
        ...

    @abstractmethod
    async def fetch_open_interest(
        self,
        symbol: str,
        market_type: MarketType,
        period: str = "5m",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch open interest history.

        Returns DataFrame with columns:
            timestamp (datetime64[ns, UTC]), open_interest
        """
        ...

    @abstractmethod
    async def fetch_basis(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Fetch spot-perpetual basis (price difference %).

        Returns DataFrame with columns:
            timestamp (datetime64[ns, UTC]), spot_price, perpetual_price, basis_pct
        """
        ...

    # ── Live (push) methods ───────────────────────────────────────────
    # Runtime owns the DataFrame and creates the thread/task. The network
    # layer provides the implementation that runs inside that thread/task,
    # updating Runtime's DataFrame in-place until Runtime signals stop.
    #
    # Thread safety contract:
    #   Runtime creates a threading.Lock alongside the DataFrame, passes
    #   both to subscribe_*. Network layer acquires the lock before writing.
    #   Runtime acquires the lock before reading. If using pure asyncio
    #   (single-threaded), the lock is optional — cooperative scheduling
    #   ensures no concurrent access.

    @abstractmethod
    async def subscribe_klines(
        self,
        symbol: str,
        interval: KLineInterval,
        market_type: MarketType,
        target: pd.DataFrame,
        stop: asyncio.Event,
    ) -> None:
        """Continuously update `target` DataFrame with live klines.

        Runtime creates an empty DataFrame matching KLineSchema, passes it
        to this method inside an asyncio task (or dedicated thread). The
        adapter appends new rows as candles close. Blocks until `stop` is set.

        The adapter does NOT own `target` — it only writes to a DataFrame
        that Runtime passed in. The adapter is responsible for:
        - Translating raw WebSocket messages into KLineSchema columns
        - Appending rows in correct column order
        - WebSocket connection, reconnection, and rate limiting
        """
        ...

    @abstractmethod
    async def subscribe_trades(
        self,
        symbol: str,
        market_type: MarketType,
        target: pd.DataFrame,
        stop: asyncio.Event,
    ) -> None:
        """Continuously update `target` DataFrame with live trades.

        Runtime owns `target`. Adapter writes to it — same contract as
        subscribe_klines.
        """
        ...

    @abstractmethod
    async def subscribe_order_book(
        self,
        symbol: str,
        market_type: MarketType,
        target: dict,  # {"bids": DataFrame, "asks": DataFrame, "timestamp": ...}
        stop: asyncio.Event,
    ) -> None:
        """Continuously update `target` dict with live order book snapshots.

        Runtime owns `target`. Adapter writes to it — same contract as
        subscribe_klines.
        """
