"""Signal Engine — converts StrategySignal weights to discrete OrderRequests.

Pure function: receives target weights + current account state + prices,
returns a list of orders that, when filled, will bring the portfolio
to the target weights.

Placed in the Consumption layer because it consumes StrategySignals and
produces OrderRequests (which feed into the Broker).
"""


from pandera.typing import DataFrame

from superplatform.data.trading import AccountState, OrderRequest
from superplatform.strategy.signal_schema import SignalSchema

# ── Minimum notional to place an order (ignore smaller deltas) ─────
_MIN_ORDER_USDT = 1.0


def generate_orders(
    signals: DataFrame[SignalSchema],
    state: AccountState,
    prices: dict[str, float],
    min_order_usdt: float = _MIN_ORDER_USDT,
) -> list[OrderRequest]:
    """Convert target position weights to market orders.

    For each symbol in the signals:
      1. Compute current total exposure (spot + long - short).
      2. Compute target exposure = target_weight * total_equity.
      3. If |delta| < min_order_usdt, skip.
      4. Generate market orders to close the gap:
         - delta > 0: buy (spot) to increase long exposure
         - delta < 0: first close existing long, then short

    Args:
        signals: Per-symbol target position weights ([-1, 1]).
                 Only the latest row per symbol is used.
        state: Current account state (positions).
        prices: Latest market prices per symbol.
        min_order_usdt: Minimum notional value to place an order.

    Returns:
        List of OrderRequests ready to pass to broker.place_order().
    """
    if signals.empty:
        return []

    # Use only the latest signal per symbol
    latest = (
        signals.sort_values("timestamp")
        .groupby("symbol")
        .last()
        .reset_index()
    )

    total_equity = state.equity()
    if total_equity <= 0:
        return []

    # Cap total gross exposure at 100% — consistent with the backtest engine.
    # `latest` holds one row per symbol; gross covers exactly the rows being
    # traded this tick, so N symbols at weight 1.0 become an equal-weight 1/N
    # book instead of an N× leveraged one.
    gross = latest["position"].abs().sum()
    scale = min(1.0, 1.0 / gross) if gross > 0 else 1.0

    orders: list[OrderRequest] = []

    for _, row in latest.iterrows():
        symbol = row["symbol"]
        target_weight = float(row["position"])
        price = prices.get(symbol, 0.0)
        if price <= 0:
            continue

        target_notional = target_weight * scale * total_equity
        current_notional = _current_exposure(state, symbol, price)
        delta = target_notional - current_notional

        if abs(delta) < min_order_usdt:
            continue

        new_orders = _delta_to_orders(symbol, delta, price, state)
        orders.extend(new_orders)

    return orders


# ── Internal helpers ────────────────────────────────────────────────

def _current_exposure(state: AccountState, symbol: str, price: float) -> float:
    """Total notional exposure to a symbol (spot + long - short)."""
    exposure = 0.0

    spot = state.get_position(symbol, "spot")
    if spot:
        exposure += spot.qty * price

    long_pos = state.get_position(symbol, "long")
    if long_pos:
        exposure += long_pos.qty * price

    short_pos = state.get_position(symbol, "short")
    if short_pos:
        exposure -= short_pos.qty * price  # short = negative exposure

    return exposure


def _delta_to_orders(
    symbol: str, delta: float, price: float, state: AccountState,
) -> list[OrderRequest]:
    """Convert an exposure delta into one or more OrderRequests.

    Splits orders when flipping direction:
      delta < 0, currently long  → sell to close, then short remainder
      delta > 0, currently short → buy to close,  then buy remainder
    """
    orders: list[OrderRequest] = []

    if delta > 0:
        # Need more long exposure. First close any short, then buy.
        short = state.get_position(symbol, "short")
        remaining = delta
        if short and short.qty > 0:
            close_qty = min(short.qty, remaining / price)
            orders.append(OrderRequest(
                symbol=symbol, side="close", qty=close_qty,
                order_type="market", source="auto",
            ))
            remaining -= close_qty * price
        if remaining > 0:
            orders.append(OrderRequest(
                symbol=symbol, side="buy", qty=remaining / price,
                order_type="market", source="auto",
            ))

    elif delta < 0:
        # Need short exposure (or reduce long). First close long/spot, then short remainder.
        abs_delta = abs(delta)
        remaining = abs_delta
        long_pos = state.get_position(symbol, "long")
        spot_pos = state.get_position(symbol, "spot")

        close_pos = long_pos if long_pos else spot_pos
        if close_pos and close_pos.qty > 0:
            close_qty = min(close_pos.qty, remaining / price)
            orders.append(OrderRequest(
                symbol=symbol, side="close", qty=close_qty,
                order_type="market", source="auto",
            ))
            remaining -= close_qty * price
        if remaining > 0:
            orders.append(OrderRequest(
                symbol=symbol, side="short", qty=remaining / price,
                order_type="market", source="auto",
            ))

    return orders


# ── Utility: close all positions ────────────────────────────────────

def close_all_orders(state: AccountState, prices: dict[str, float]) -> list[OrderRequest]:
    """Generate orders to flatten the entire portfolio."""
    orders: list[OrderRequest] = []
    for _key, pos in state.positions.items():
        if pos.qty <= 0:
            continue
        price = prices.get(pos.symbol, 0.0)
        if price <= 0:
            continue
        side = "sell" if pos.side in ("spot", "long") else "buy"
        orders.append(OrderRequest(
            symbol=pos.symbol, side=side, qty=pos.qty,
            order_type="market", source="auto",
        ))
    return orders
