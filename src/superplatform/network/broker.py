"""Broker abstract base class.

A Broker is an ExchangeAdapter PLUS account and trading capabilities.
It has two concrete implementations:
  - SimulatedBroker: local matching + synthetic account
  - BinanceBroker (future): real exchange via ccxt private APIs

The Broker is the SINGLE point of contact between Runtime and the
outside world. Runtime does NOT call ExchangeAdapter directly — it
always goes through the Broker, which can be simulated or real.
"""

from abc import abstractmethod

from superplatform.data.trading import AccountState, Order, OrderRequest
from superplatform.network.base import ExchangeAdapter


class Broker(ExchangeAdapter):
    """Full broker — market data (inherited) + account + trading.

    Market data methods (fetch_klines, fetch_trades, ...) are inherited
    from ExchangeAdapter and work the same way regardless of whether the
    broker is simulated or real.

    Account and trading methods are NEW — they are NOT on ExchangeAdapter
    because they are not purely read-only market data.
    """

    # ── Account (pull) ──────────────────────────────────────────────

    @abstractmethod
    async def fetch_account_state(self) -> AccountState:
        """Return the full current account snapshot.

        SimulatedBroker: reads internal Account.
        Real broker:     calls ccxt.fetch_balance() + fetch_positions().
        """
        ...

    # ── Prices (shared state) ────────────────────────────────────────

    @abstractmethod
    def update_prices(self, prices: dict[str, float]) -> None:
        """Sync latest market prices into the broker.

        Called by the scheduler each tick BEFORE any trading logic runs.
        Prices are used by the Matcher for fills and by Account for
        mark-to-market PnL.
        """
        ...

    # ── Trading (push) ──────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> tuple[Order | None, str]:
        """Place a new order.

        Returns (order, reject_reason).
        On success: order is populated, reject_reason is "".
        On rejection: order is None, reject_reason explains why.

        SimulatedBroker: validated + matched locally, fill applied immediately
                         for market orders.
        Real broker:     sent to exchange via ccxt.create_order().
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""
        ...

    # ── Tick maintenance ────────────────────────────────────────────

    @abstractmethod
    async def tick(self, funding_rates: dict[str, float] | None = None) -> None:
        """Run per-tick account maintenance.

        Order: limit fills → mark-to-market → funding → liquidation → snapshot.
        Called once per tick by Runtime AFTER any new orders are placed.
        """
        ...

    # ── Queries ──────────────────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list:
        """Return current positions for display/API."""
        ...

    @abstractmethod
    async def get_orders(self, status: str | None = None) -> list[Order]:
        """Return orders, optionally filtered by status."""
        ...

    @abstractmethod
    async def get_trades(self, limit: int = 100) -> list:
        """Return recent trade history."""
        ...

    @abstractmethod
    async def get_equity_curve(self, limit: int = 500) -> list:
        """Return recent equity snapshots."""
        ...
