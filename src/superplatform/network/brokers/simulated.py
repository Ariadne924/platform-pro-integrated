"""Simulated broker — a virtual exchange with local order matching.

Market data is delegated to an internal ExchangeAdapter (real or synthetic).
Account, positions, and orders are managed entirely internally — just like
a real exchange manages its own state.

The Runtime does NOT hold AccountState. It queries the broker when it needs
to know positions, and the broker is the source of truth for fills and trades.
"""

import time

from superplatform.consumption.account import (
    apply_fill,
    check_liquidation,
    fresh_account,
    settle_funding,
    update_marks,
)
from superplatform.consumption.account import (
    snapshot as account_snapshot,
)
from superplatform.consumption.matcher import check_limit_orders as match_limit_orders
from superplatform.consumption.matcher import place_order as match_order
from superplatform.consumption.risk import RiskLimits
from superplatform.data.trading import (
    AccountState,
    EquityPoint,
    Order,
    OrderRequest,
    Position,
    Trade,
)
from superplatform.network.base import ExchangeAdapter
from superplatform.network.broker import Broker
from superplatform.utils.logging import logger


class SimulatedBroker(Broker):
    """Local simulated broker — a self-contained virtual exchange.

    Manages its own account state, order book, and trade history. The
    Runtime calls place_order() and tick() — the broker handles everything
    internally, just as a real exchange would.

    Usage:
        adapter = BinanceAdapter(...)
        broker = SimulatedBroker(adapter, initial_capital=100_000)

        # Each tick:
        broker.update_prices({"BTCUSDT": 65000})
        await broker.place_order(OrderRequest(...))
        await broker.tick()  # marks, funding, liquidation, snapshot
        state = await broker.fetch_account_state()  # for monitoring
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        initial_capital: float = 100_000,
        limits: RiskLimits | None = None,
    ):
        super().__init__(name=f"simulated-{adapter.name}", rate_limiter=None)
        self._adapter = adapter
        self._limits = limits or RiskLimits()

        # ── Internal state (the "exchange server") ──
        self._account: AccountState = fresh_account(initial_capital)
        self._last_prices: dict[str, float] = {}
        self._open_orders: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._equity: list[EquityPoint] = []

    # ── Market data (delegate to adapter) ───────────────────────────

    async def fetch_klines(self, symbol, interval, market_type,
                           start=None, end=None, limit=500):
        return await self._adapter.fetch_klines(
            symbol, interval, market_type, start, end, limit)

    async def fetch_trades(self, symbol, market_type,
                           start=None, end=None, limit=1000):
        return await self._adapter.fetch_trades(
            symbol, market_type, start, end, limit)

    async def fetch_order_book(self, symbol, market_type, depth=20):
        return await self._adapter.fetch_order_book(symbol, market_type, depth)

    async def fetch_funding_rate(self, symbol, start=None, end=None, limit=500):
        return await self._adapter.fetch_funding_rate(symbol, start, end, limit)

    async def fetch_open_interest(self, symbol, market_type, period="5m",
                                  start=None, end=None, limit=500):
        return await self._adapter.fetch_open_interest(
            symbol, market_type, period, start, end, limit)

    async def fetch_basis(self, symbol, start=None, end=None):
        return await self._adapter.fetch_basis(symbol, start, end)

    async def subscribe_klines(self, symbol, interval, market_type, target, stop):
        return await self._adapter.subscribe_klines(
            symbol, interval, market_type, target, stop)

    async def subscribe_trades(self, symbol, market_type, target, stop):
        return await self._adapter.subscribe_trades(symbol, market_type, target, stop)

    async def subscribe_order_book(self, symbol, market_type, target, stop):
        return await self._adapter.subscribe_order_book(
            symbol, market_type, target, stop)

    # ── Account ─────────────────────────────────────────────────────

    async def fetch_account_state(self) -> AccountState:
        """Return a snapshot of the internal account state."""
        return self._account

    # ── Prices ──────────────────────────────────────────────────────

    def update_prices(self, prices: dict[str, float]) -> None:
        """Sync latest market prices. Called at the start of each tick."""
        self._last_prices.update(prices)

    @property
    def last_prices(self) -> dict[str, float]:
        return dict(self._last_prices)

    # ── Trading ─────────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> tuple[Order | None, str]:
        """Place an order. Returns (order, reject_reason).

        Market orders fill immediately against the last price.
        Limit orders are stored and checked each tick for price crossing.
        Fills are applied to the internal account immediately.
        """
        order, trade, reason = match_order(
            request=request,
            state=self._account,
            prices=self._last_prices,
            limits=self._limits,
        )

        if order is None:
            return None, reason

        self._open_orders[order.order_id] = order

        if trade:
            self._account = apply_fill(
                self._account, trade,
                self._limits.maker_fee_bps,
            )
            self._trades.append(trade)
            self._open_orders.pop(trade.order_id, None)
            logger.debug(
                "Filled: {} {} {:.4f} @ {:.2f}",
                trade.symbol, trade.side, trade.qty, trade.price,
            )

        return order, ""

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open limit order."""
        order = self._open_orders.get(order_id)
        if order is None or order.status != "open":
            return False
        order.status = "cancelled"
        order.updated_ts = time.time()
        self._open_orders.pop(order_id, None)
        return True

    # ── Per-tick maintenance ────────────────────────────────────────

    async def tick(self, funding_rates: dict[str, float] | None = None) -> None:
        """Run one tick's worth of account maintenance.

        Order:
          1. Check limit orders for fills
          2. Mark-to-market all positions
          3. Settle funding (perpetual only)
          4. Check liquidations
          5. Record equity snapshot

        Called once per tick by the Runtime AFTER any new orders.
        """
        now = time.time()
        prices = self._last_prices
        if not prices:
            return

        # 1. Limit orders
        orders = list(self._open_orders.values())
        updated, fills = match_limit_orders(
            open_orders=orders,
            state=self._account,
            prices=prices,
            limits=self._limits,
        )
        self._open_orders = {o.order_id: o for o in updated if o.status == "open"}
        for trade in fills:
            self._account = apply_fill(self._account, trade, self._limits.maker_fee_bps)
            self._trades.append(trade)
            logger.debug(
                "Limit fill: {} {} {:.4f} @ {:.2f}",
                trade.symbol, trade.side, trade.qty, trade.price,
            )

        # 2. Mark-to-market
        self._account = update_marks(self._account, prices)

        # 3. Funding
        rates = funding_rates or {}
        self._account, funding_trades = settle_funding(self._account, rates, now)
        for t in funding_trades:
            self._account = apply_fill(self._account, t, 0)
            self._trades.append(t)

        # 4. Liquidation
        self._account, liq_trades = check_liquidation(self._account)
        for t in liq_trades:
            self._account = apply_fill(self._account, t, self._limits.taker_fee_bps)
            self._trades.append(t)
            logger.warning(
                "LIQUIDATION: {} {:.4f} @ {:.2f}",
                t.symbol, t.qty, t.price,
            )

        # 5. Snapshot
        pt = account_snapshot(self._account, now)
        self._equity.append(pt)

    # ── Queries ─────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        return list(self._account.positions.values())

    async def get_orders(self, status: str | None = None) -> list[Order]:
        result = list(self._open_orders.values())
        # Add filled/cancelled from trades
        seen = {o.order_id for o in result}
        for t in reversed(self._trades):
            if t.order_id and t.order_id not in seen:
                seen.add(t.order_id)
                result.append(Order(
                    order_id=t.order_id, symbol=t.symbol, side=t.side,
                    qty=t.qty, filled_qty=t.qty, status="filled",
                    created_ts=t.ts, updated_ts=t.ts,
                ))
        if status:
            result = [o for o in result if o.status == status]
        return result

    async def get_trades(self, limit: int = 100) -> list[Trade]:
        return self._trades[-limit:]

    async def get_equity_curve(self, limit: int = 500) -> list[EquityPoint]:
        return self._equity[-limit:]
