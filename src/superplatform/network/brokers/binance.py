"""Binance testnet broker — real order execution on Binance USDT-M futures testnet.

Market data is delegated to an internal ``BinanceAdapter`` pointed at the
production public API (testnet prices track production closely). Account,
orders, and fills are managed by the testnet matching engine through a
``UMFutures`` private client (``base_url="https://testnet.binancefuture.com"``,
testnet API keys) — no local matching, funding, or liquidation happens here.

The Runtime does NOT hold AccountState. It queries the broker when it needs
to know positions, and the testnet is the source of truth for fills and trades.

v1 assumptions / limitations:
  - one-way mode accounts (hedge-mode positions log a warning)
  - market orders only (limit orders are rejected)
  - side/reduceOnly decided at execution time from the net position
"""

import asyncio
import math
import time

from binance.error import ClientError

from superplatform.consumption.account import (
    snapshot as account_snapshot,
)
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

_ORDER_STATUS_MAP = {
    "NEW": "open",
    "PARTIALLY_FILLED": "open",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "EXPIRED": "cancelled",
    "REJECTED": "rejected",
}


class BinanceBroker(Broker):
    """Real broker on the Binance USDT-M futures testnet.

    Usage:
        adapter = create_binance_adapter(proxy)
        futures = UMFutures(key, secret, base_url=TESTNET_URL, timeout=10)
        broker = BinanceBroker(adapter, futures, default_leverage=10)

        # Each tick:
        broker.update_prices({"BTCUSDT": 65000})
        await broker.place_order(OrderRequest(...))
        await broker.tick()  # pull account state + equity snapshot
        state = await broker.fetch_account_state()  # cached, cheap
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        futures_client,
        *,
        default_leverage: float = 10,
        symbols: list[str] | None = None,
        recv_window_ms: int = 30_000,
    ):
        super().__init__(name="binance-testnet", rate_limiter=None)
        self._adapter = adapter
        self._client = futures_client
        self._default_leverage = default_leverage
        self._recv_window_ms = recv_window_ms
        self._symbols: set[str] | None = set(symbols) if symbols else None
        # Public (unsigned) SDK calls that must NOT receive `recvWindow`.
        self._public_calls = {self._client.exchange_info}

        # ── Cached state (refreshed once per tick) ──
        self._account: AccountState = AccountState()
        self._last_prices: dict[str, float] = {}
        self._equity: list[EquityPoint] = []

        # ── Session ledger (orders/trades placed by this process) ──
        self._orders: dict[str, Order] = {}
        self._trades: list[Trade] = []
        # order_id → {symbol, binance_order_id} so cancel_order can route.
        self._client_orders: dict[str, dict] = {}

        # ── Caches that must not be refetched every order ──
        self._symbol_info: dict[str, dict] = {}
        self._exchange_info: dict | None = None
        self._leverage_set: set[str] = set()

        # Guard against a scheduler tick and a /state poll racing on
        # self._account / self._equity writes.
        self._sync_lock = asyncio.Lock()

    # ── Market data (delegate to the production adapter) ────────────

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
        """Return the cached account state (refreshed each ``tick``)."""
        return self._account

    # ── Prices ──────────────────────────────────────────────────────

    def update_prices(self, prices: dict[str, float]) -> None:
        """Sync latest market prices (used for notional validation only)."""
        self._last_prices.update(prices)

    @property
    def last_prices(self) -> dict[str, float]:
        return dict(self._last_prices)

    # ── Trading ─────────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> tuple[Order | None, str]:
        """Place a market order on the testnet.

        Returns (order, reject_reason). ``request.side`` is one of
        buy/long/short/sell/close (engine semantics) and is mapped to a
        Binance side + reduceOnly flag at execution time from the net
        position, so a ``close`` always reduces and a fresh-direction order
        never accidentally flips a net position.
        """
        if request.order_type != "market":
            return None, "place_order: only market orders are supported on testnet"
        if request.qty <= 0:
            return None, "place_order: qty must be positive"

        price = self._last_prices.get(request.symbol, 0.0)
        qty = await self._round_qty(request.symbol, request.qty, price)
        if qty is None:
            return None, (
                f"place_order: qty {request.qty} below LOT_SIZE floor "
                f"or MIN_NOTIONAL for {request.symbol}"
            )

        await self._ensure_leverage(request.symbol, request.leverage)

        try:
            binance_side, reduce_only, reason = await self._resolve_side_and_reduce(
                request.side, request.symbol
            )
        except ClientError as exc:
            return None, f"place_order: net-position query failed for {request.symbol}: {exc}"
        if binance_side is None:
            return None, reason

        params: dict = {"quantity": qty}
        if reduce_only:
            params["reduceOnly"] = True

        try:
            resp = await self._request(
                self._client.new_order,
                request.symbol, binance_side, "MARKET", **params,
            )
        except ClientError as exc:
            return None, f"binance rejected {request.side} {request.symbol}: {exc}"

        order = self._resp_to_order(resp, request)
        self._orders[order.order_id] = order
        self._client_orders[order.order_id] = {
            "symbol": request.symbol,
            "binance_order_id": str(resp.get("orderId") or order.order_id),
        }

        if order.status == "filled" and order.filled_qty > 0:
            trade = Trade(
                trade_id=str(resp.get("orderId")) or f"t-{time.time()}",
                order_id=order.order_id,
                symbol=request.symbol,
                side=request.side,
                qty=order.filled_qty,
                price=float(resp.get("avgPrice") or 0.0),
                ts=time.time(),
            )
            self._trades.append(trade)

        logger.info(
            "testnet {} {} {} (reduceOnly={}) -> {}",
            request.symbol, request.side, qty, reduce_only, order.status,
        )
        return order, ""

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order placed by this process. True on success."""
        entry = self._client_orders.get(order_id)
        if entry is None:
            return False
        try:
            await self._request(
                self._client.cancel_order,
                entry["symbol"],
                orderId=entry["binance_order_id"],
            )
        except ClientError as exc:
            logger.warning("cancel_order {} failed: {}", order_id, exc)
            return False
        order = self._orders.get(order_id)
        if order is not None:
            order.status = "cancelled"
            order.updated_ts = time.time()
        return True

    # ── Per-tick maintenance ────────────────────────────────────────

    async def tick(self, funding_rates: dict[str, float] | None = None) -> None:
        """Refresh account state from the testnet and record an equity point.

        The testnet engine handles fills, funding, and liquidation — the
        broker only pulls the resulting account state. Transient errors are
        swallowed: the previously cached state stays visible and the
        scheduler keeps running (``LiveRuntime`` does not catch tick errors).
        """
        async with self._sync_lock:
            try:
                self._account = await self._fetch_account()
            except ClientError as exc:
                logger.warning("testnet account fetch failed: {}", exc)
                return
            self._equity.append(account_snapshot(self._account, time.time()))

    # ── Queries ─────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        return list(self._account.positions.values())

    async def get_orders(self, status: str | None = None) -> list[Order]:
        """Live testnet open orders + this process's order ledger."""
        result: list[Order] = []
        seen: set[str] = set()
        try:
            open_rows = await self._request(self._client.get_orders)
        except ClientError as exc:
            logger.warning("get_orders failed: {}", exc)
            open_rows = []
        for row in open_rows:
            oid = str(row.get("orderId"))
            seen.add(oid)
            result.append(self._resp_to_order(
                row,
                OrderRequest(
                    symbol=row.get("symbol", ""),
                    side="auto",
                    qty=float(row.get("origQty") or 0.0),
                    source="auto",
                ),
            ))
        for order in self._orders.values():
            if order.order_id not in seen:
                seen.add(order.order_id)
                result.append(order)
        if status:
            result = [o for o in result if o.status == status]
        return result

    async def get_trades(self, limit: int = 100) -> list[Trade]:
        """Recent fills: testnet account trades merged with the session ledger.

        v1 deliberately refetches per call so the dashboard shows fresh fills;
        a cache keyed by account-update counter is a future optimization.
        """
        symbols = self._symbols or set(self._last_prices) or {"BTCUSDT"}
        merged: list[Trade] = list(self._trades)
        seen = {t.trade_id for t in merged}
        for symbol in symbols:
            try:
                rows = await self._request(self._client.get_account_trades, symbol)
            except ClientError as exc:
                logger.warning("get_account_trades {} failed: {}", symbol, exc)
                continue
            for row in rows:
                trade = self._row_to_trade(row)
                if trade.trade_id not in seen:
                    seen.add(trade.trade_id)
                    merged.append(trade)
        merged.sort(key=lambda t: t.ts, reverse=True)
        return merged[:limit]

    async def get_equity_curve(self, limit: int = 500) -> list[EquityPoint]:
        return self._equity[-limit:]

    # ── Private helpers ─────────────────────────────────────────────

    async def _request(self, fn, *args, **kwargs):
        """Run one synchronous SDK call in a worker thread.

        ClientError propagates to the caller; no retry — testnet private
        endpoints are kept simple and the scheduler tolerates a failed tick.

        Signed (private) calls carry an explicit ``recvWindow`` so a small
        local clock skew doesn't trip Binance's 5s default timestamp window
        (error -1021). Public calls (``exchange_info``) are left untouched.
        """
        if fn not in self._public_calls:
            kwargs.setdefault("recvWindow", self._recv_window_ms)
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _fetch_account(self) -> AccountState:
        acc = await self._request(self._client.account)
        rows = await self._request(self._client.get_position_risk)
        return self._hydrate(acc, rows)

    @staticmethod
    def _hydrate(acc: dict, rows: list[dict]) -> AccountState:
        """Build an AccountState whose ``equity()`` equals Binance's
        ``totalMarginBalance``.

        Binance's ``totalWalletBalance`` already includes margin locked in
        positions, while local ``equity() = wallet + margin + upnl``. Reporting
        only the non-margin portion as ``wallet_balance`` keeps the identity:
        equity = (totalWalletBalance - ΣinitialMargin) + ΣinitialMargin
               + ΣunRealizedProfit = totalWalletBalance + totalUnrealizedProfit
               = totalMarginBalance.
        """
        total_wallet = float(acc.get("totalWalletBalance") or 0.0)
        positions: dict[str, Position] = {}
        init_margin_sum = 0.0
        for row in rows:
            amt = float(row.get("positionAmt") or 0.0)
            if amt == 0:
                continue
            symbol = row.get("symbol", "")
            side = "long" if amt > 0 else "short"
            position_side = row.get("positionSide") or "BOTH"
            if position_side != "BOTH":
                logger.warning(
                    "hedge-mode position detected for {} (positionSide={}); "
                    "BinanceBroker assumes one-way mode",
                    symbol, position_side,
                )
            margin = float(row.get("positionInitialMargin") or 0.0)
            init_margin_sum += margin
            positions[f"{symbol}:{side}"] = Position(
                symbol=symbol,
                side=side,
                qty=abs(amt),
                entry_price=float(row.get("entryPrice") or 0.0),
                leverage=float(row.get("leverage") or 10.0),
                margin=margin,
                unrealized_pnl=float(row.get("unRealizedProfit") or 0.0),
                mark_price=float(row.get("markPrice") or 0.0),
                liq_price=float(row.get("liquidationPrice") or 0.0),
            )
        return AccountState(wallet_balance=total_wallet - init_margin_sum, positions=positions)

    async def _net_side(self, symbol: str) -> str:
        """Current one-way net position: 'long' / 'short' / 'none'."""
        rows = await self._request(self._client.get_position_risk, symbol=symbol)
        qty = sum(float(row.get("positionAmt") or 0.0) for row in rows)
        if qty > 0:
            return "long"
        if qty < 0:
            return "short"
        return "none"

    async def _resolve_side_and_reduce(
        self, side: str, symbol: str
    ) -> tuple[str | None, bool, str]:
        """Map an engine side to (Binance side, reduceOnly, reject_reason).

        reduceOnly depends on the net position at execution time:
          - buy  → reduceOnly only when the net position is short
          - sell → reduceOnly only when the net position is long
          - close → always reduce; its Binance side is decided by the net
            position (long→SELL, short→BUY), rejected when flat
          - long/short → never reduceOnly (the engine only sends fresh-direction
            orders after a full close, so they always open / increase)
        """
        if side == "buy":
            return "BUY", await self._net_side(symbol) == "short", ""
        if side == "long":
            return "BUY", False, ""
        if side == "short":
            return "SELL", False, ""
        if side == "sell":
            return "SELL", await self._net_side(symbol) == "long", ""
        if side == "close":
            net = await self._net_side(symbol)
            if net == "none":
                return None, False, "close: no open position"
            return ("SELL" if net == "long" else "BUY"), True, ""
        return None, False, f"place_order: unsupported side {side!r}"

    async def _round_qty(self, symbol: str, qty: float, price: float) -> float | None:
        """Floor ``qty`` to the LOT_SIZE step; None if the result is invalid.

        A floored quantity of zero, or one whose notional at the last known
        price falls below MIN_NOTIONAL, cannot be placed.
        """
        info = await self._symbol_info_cached(symbol)
        step = float(info.get("step_size") or 1.0)
        min_notional = info.get("min_notional") or 0.0

        floored = math.floor(qty / step + 1e-12) * step
        if floored <= 0:
            return None
        if price > 0 and floored * price < min_notional:
            return None
        return floored

    async def _symbol_info_cached(self, symbol: str) -> dict:
        """LOT_SIZE / MIN_NOTIONAL filters for a symbol.

        The SDK's ``exchange_info`` takes no symbol argument, so the full
        response is fetched once and indexed by symbol from then on.
        """
        if self._exchange_info is None:
            self._exchange_info = await self._request(self._client.exchange_info)
        if symbol not in self._symbol_info:
            row = next(
                (s for s in self._exchange_info.get("symbols", [])
                 if s.get("symbol") == symbol),
                None,
            )
            info: dict = {"step_size": 1.0, "min_notional": 0.0}
            if row is not None:
                for f in row.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        info["step_size"] = float(f.get("stepSize") or 1.0)
                    elif f.get("filterType") == "MIN_NOTIONAL":
                        info["min_notional"] = float(f.get("notional") or 0.0)
            self._symbol_info[symbol] = info
        return self._symbol_info[symbol]

    async def _ensure_leverage(self, symbol: str, leverage: float) -> None:
        """Set the symbol's leverage once per process.

        ``change_leverage`` failures (margin-type, permissions, "not modified")
        are non-fatal: the testnet keeps its current leverage and the order
        still goes out.
        """
        if symbol in self._leverage_set:
            return
        lev = int(round(leverage)) or self._default_leverage
        try:
            await self._request(self._client.change_leverage, symbol, lev)
        except ClientError as exc:
            logger.warning("change_leverage {} -> {} failed: {}", symbol, lev, exc)
        self._leverage_set.add(symbol)

    @staticmethod
    def _resp_to_order(resp: dict, request: OrderRequest) -> Order:
        """Map a Binance order response (new_order / openOrders) to Order."""
        now = time.time()
        status = _ORDER_STATUS_MAP.get(resp.get("status"), "open")
        return Order(
            order_id=str(resp.get("orderId") or resp.get("clientOrderId") or f"o-{now}"),
            symbol=request.symbol,
            side=request.side,
            qty=float(resp.get("origQty") or request.qty),
            filled_qty=float(resp.get("executedQty") or 0.0),
            limit_price=float(resp["price"]) if resp.get("price") not in (None, "", "0") else None,
            status=status,
            reject_reason=resp.get("rejectReason", "") if status == "rejected" else "",
            source=request.source,
            created_ts=now,
            updated_ts=now,
        )

    @staticmethod
    def _row_to_trade(row: dict) -> Trade:
        """Map a get_account_trades row to Trade.

        Testnet trades are direction-agnostic (BUY/SELL with positionSide); v1
        reports the raw direction — the engine-side (long/short/close) is not
        recoverable from the account-trades endpoint.
        """
        return Trade(
            trade_id=str(row.get("id") or row.get("orderId")),
            order_id=str(row.get("orderId") or ""),
            symbol=row.get("symbol", ""),
            side="buy" if row.get("side") == "BUY" else "sell",
            qty=float(row.get("qty") or 0.0),
            price=float(row.get("price") or 0.0),
            fee=float(row.get("commission") or 0.0),
            ts=float(row.get("time") or 0.0) / 1000.0,
        )
