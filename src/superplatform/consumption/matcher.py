"""Order matcher — pure-function order placement and fill simulation.

The Matcher does NOT hold state. It receives AccountState, OrderRequest,
and current prices, and returns the resulting Order + optional Trade.
State updates are done by the caller (Runtime / SimulatedBroker).
"""

import time
import uuid

from superplatform.consumption.risk import RiskLimits, check_order
from superplatform.data.trading import (
    AccountState,
    Order,
    OrderRequest,
    Trade,
)


def place_order(
    request: OrderRequest,
    state: AccountState,
    prices: dict[str, float],
    limits: RiskLimits,
) -> tuple[Order | None, Trade | None, str]:
    """Validate and place an order.

    Returns:
        (order, trade, reject_reason)
        - On success: order populated, trade may be non-None for market fills.
        - On rejection: order is None, reject_reason explains why.

    Market orders: fill immediately at last price (taker fee).
    Limit orders:  open with status='open', filled later on price cross (maker fee).
    """
    # Risk check
    ok, reason = check_order(request, state, prices, limits)
    if not ok:
        return None, None, reason

    now = time.time()
    order_id = f"ORD-{uuid.uuid4().hex[:12]}"
    price = prices[request.symbol]
    fee_bps = limits.taker_fee_bps if request.order_type == "market" else limits.maker_fee_bps

    order = Order(
        order_id=order_id,
        symbol=request.symbol,
        side=request.side,
        qty=request.qty,
        limit_price=request.limit_price,
        status="open",
        source=request.source,
        created_ts=now,
        updated_ts=now,
    )

    trade = None
    if request.order_type == "market":
        # Fill immediately at last price
        trade = Trade(
            trade_id=f"TRD-{uuid.uuid4().hex[:12]}",
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            price=price,
            fee=request.qty * price * fee_bps / 10000,
            ts=now,
        )
        order.filled_qty = request.qty
        order.status = "filled"
        order.updated_ts = now

    elif request.order_type == "limit":
        limit_price = request.limit_price or 0
        # Check if limit price has already been crossed
        crossed = _is_crossed(request.side, limit_price, price)
        if crossed:
            # Fill at limit price (maker)
            trade = Trade(
                trade_id=f"TRD-{uuid.uuid4().hex[:12]}",
                order_id=order_id,
                symbol=request.symbol,
                side=request.side,
                qty=request.qty,
                price=limit_price,
                fee=request.qty * limit_price * fee_bps / 10000,
                ts=now,
            )
            order.filled_qty = request.qty
            order.status = "filled"
            order.updated_ts = now

    return order, trade, ""


def check_limit_orders(
    open_orders: list[Order],
    state: AccountState,
    prices: dict[str, float],
    limits: RiskLimits,
) -> tuple[list[Order], list[Trade]]:
    """Check all open limit orders for fills at current prices.

    Returns:
        (updated_orders, new_trades) — orders whose status may have changed,
        and trades for any fills that occurred.
    """
    updated: list[Order] = []
    trades: list[Trade] = []
    now = time.time()
    maker_fee = limits.maker_fee_bps

    for order in open_orders:
        if order.status != "open":
            updated.append(order)
            continue

        price = prices.get(order.symbol)
        if price is None:
            updated.append(order)
            continue

        limit_price = order.limit_price or 0
        crossed = _is_crossed(order.side, limit_price, price)

        # Also cancel close orders whose position no longer exists
        if order.side == "close":
            pos = state.get_position(order.symbol, "long") or state.get_position(order.symbol, "short")
            if pos is None or pos.qty <= 0:
                order.status = "cancelled"
                order.updated_ts = now
                updated.append(order)
                continue

        if crossed:
            fill_qty = min(order.qty - order.filled_qty, order.qty)
            trade = Trade(
                trade_id=f"TRD-{uuid.uuid4().hex[:12]}",
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                qty=fill_qty,
                price=limit_price,
                fee=fill_qty * limit_price * maker_fee / 10000,
                ts=now,
            )
            trades.append(trade)
            order.filled_qty += fill_qty
            order.status = "filled"
            order.updated_ts = now

        updated.append(order)

    return updated, trades


def _is_crossed(side: str, limit: float, current: float) -> bool:
    """Check if limit price is crossed by current market price.

    Buy/long:  limit >= current  (willing to pay limit, market is cheaper)
    Sell/short: limit <= current  (willing to sell at limit, market is higher)
    """
    if side in ("buy", "long"):
        return limit >= current
    if side in ("sell", "short", "close"):
        return limit <= current
    return False
