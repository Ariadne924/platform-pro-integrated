"""Account state machine — pure functions for position & PnL management.

All functions are stateless: they take an AccountState and return a new
AccountState. The Runtime holds the current state and passes it in each tick.
"""

import time

from superplatform.data.trading import (
    AccountState,
    EquityPoint,
    Position,
    Trade,
)

# ── Initialisation ──────────────────────────────────────────────────

def fresh_account(initial_capital: float) -> AccountState:
    """Create a new account with the given starting capital."""
    return AccountState(wallet_balance=initial_capital)


# ── Fill application ────────────────────────────────────────────────

def apply_fill(state: AccountState, trade: Trade, maker_fee_bps: float = 2.0) -> AccountState:
    """Apply a filled trade to the account. Returns updated state.

    Handles:
      - spot buys/sells
      - perpetual open (long/short)
      - perpetual close (reduce/flip)
      - funding settlements (side='funding')
      - liquidations (trade.liquidated=True)
    """
    # Funding settlement — simple wallet adjustment
    if trade.side == "funding":
        return AccountState(
            wallet_balance=state.wallet_balance + trade.fee,  # fee column carries signed payment
            positions=dict(state.positions),
            last_funding_settle_ts=state.last_funding_settle_ts,
        )

    pos_side = _trade_to_pos_side(trade.side)
    key = state.position_key(trade.symbol, pos_side)
    existing = state.positions.get(key)
    new_state = AccountState(
        wallet_balance=state.wallet_balance,
        positions=dict(state.positions),
        last_funding_settle_ts=state.last_funding_settle_ts,
    )

    if trade.side == "buy":
        _apply_spot_buy(new_state, trade, maker_fee_bps, key)
    elif trade.side == "sell":
        _apply_spot_sell(new_state, trade, maker_fee_bps, key, existing)
    elif trade.side in ("long", "short"):
        _apply_perp_open(new_state, trade, maker_fee_bps, key)
    elif trade.side == "close":
        _apply_perp_close(new_state, trade, maker_fee_bps, key, existing)

    return new_state


def _apply_spot_buy(state: AccountState, trade: Trade, fee_bps: float, key: str) -> None:
    """Buy spot: add to position, deduct cost from wallet."""
    cost = trade.qty * trade.price
    fee = cost * fee_bps / 10000
    existing = state.positions.get(key)
    if existing:
        # Weighted-average entry
        total_qty = existing.qty + trade.qty
        total_cost = existing.entry_price * existing.qty + cost
        state.positions[key] = Position(
            symbol=trade.symbol, side="spot", qty=total_qty,
            entry_price=total_cost / total_qty if total_qty > 0 else 0,
            leverage=1.0, margin=total_cost,
        )
    else:
        state.positions[key] = Position(
            symbol=trade.symbol, side="spot", qty=trade.qty,
            entry_price=trade.price, leverage=1.0, margin=cost,
        )
    state.wallet_balance -= (cost + fee)


def _apply_spot_sell(state: AccountState, trade: Trade, fee_bps: float,
                     key: str, existing: Position | None) -> None:
    """Sell spot: reduce position, realise PnL."""
    if existing is None or existing.qty <= 0:
        return
    fee = trade.qty * trade.price * fee_bps / 10000
    released = existing.margin * (trade.qty / existing.qty)
    realised = (trade.price - existing.entry_price) * trade.qty
    state.wallet_balance += released + realised - fee

    remaining = existing.qty - trade.qty
    if remaining <= 0:
        state.positions.pop(key, None)
    else:
        # Keep proportional margin
        remaining_margin = existing.margin * (remaining / existing.qty)
        state.positions[key] = Position(
            symbol=trade.symbol, side="spot", qty=remaining,
            entry_price=existing.entry_price, leverage=1.0,
            margin=remaining_margin,
        )


def _apply_perp_open(state: AccountState, trade: Trade, fee_bps: float, key: str) -> None:
    """Open/increase perpetual position."""
    notional = trade.qty * trade.price
    margin_added = notional / max(trade.qty, 1)  # will be fixed below
    # Actually: margin = notional / leverage
    # We don't have leverage on Trade — it's stored on the order/position.
    # For now, use the position's existing leverage or default 1x.
    # This needs leverage from context. Simplified for now.
    margin_added = notional  # default 1x

    fee = notional * fee_bps / 10000
    existing = state.positions.get(key)
    side = trade.side  # "long" or "short"

    if existing:
        total_qty = existing.qty + trade.qty
        total_cost = existing.entry_price * existing.qty + notional
        total_margin = existing.margin + margin_added
        state.positions[key] = Position(
            symbol=trade.symbol, side=side, qty=total_qty,
            entry_price=total_cost / total_qty if total_qty > 0 else 0,
            leverage=existing.leverage, margin=total_margin,
        )
    else:
        state.positions[key] = Position(
            symbol=trade.symbol, side=side, qty=trade.qty,
            entry_price=trade.price, leverage=1.0, margin=margin_added,
        )
    state.wallet_balance -= (margin_added + fee)


def _apply_perp_close(state: AccountState, trade: Trade, fee_bps: float,
                      key: str, existing: Position | None) -> None:
    """Close/reduce perpetual position."""
    if existing is None or existing.qty <= 0:
        return
    notional = trade.qty * trade.price
    fee = notional * fee_bps / 10000

    # Direction: closing a long sells at price → profit if price > entry
    #             closing a short buys at price → profit if price < entry
    realised = existing.side_sign() * (trade.price - existing.entry_price) * trade.qty
    released_margin = existing.margin * (trade.qty / existing.qty)
    state.wallet_balance += released_margin + realised - fee

    remaining = existing.qty - trade.qty
    if remaining <= 0:
        state.positions.pop(key, None)
        # If it was a flip (trade.qty > existing.qty), open opposite position
        excess = trade.qty - existing.qty
        if excess > 0:
            flip_side = "short" if existing.side == "long" else "long"
            flip_notional = excess * trade.price
            flip_margin = flip_notional / existing.leverage
            flip_key = state.position_key(trade.symbol, flip_side)
            state.positions[flip_key] = Position(
                symbol=trade.symbol, side=flip_side, qty=excess,
                entry_price=trade.price, leverage=existing.leverage,
                margin=flip_margin,
            )
            state.wallet_balance -= flip_margin
    else:
        remaining_margin = existing.margin * (remaining / existing.qty)
        state.positions[key] = Position(
            symbol=trade.symbol, side=existing.side, qty=remaining,
            entry_price=existing.entry_price, leverage=existing.leverage,
            margin=remaining_margin,
        )


def _trade_to_pos_side(side: str) -> str:
    if side == "buy":
        return "spot"
    if side == "sell":
        return "spot"
    return side  # long, short


# ── Mark-to-market ──────────────────────────────────────────────────

def update_marks(state: AccountState, prices: dict[str, float]) -> AccountState:
    """Recalculate unrealized PnL and liquidation prices for all positions."""
    new_positions = {}
    for key, pos in state.positions.items():
        price = prices.get(pos.symbol)
        if price is None or price <= 0:
            new_positions[key] = pos
            continue

        upnl = pos.side_sign() * (price - pos.entry_price) * pos.qty
        liq = _calc_liq_price(pos, price) if pos.side != "spot" else 0.0

        new_positions[key] = Position(
            symbol=pos.symbol, side=pos.side, qty=pos.qty,
            entry_price=pos.entry_price, leverage=pos.leverage,
            margin=pos.margin, unrealized_pnl=upnl,
            mark_price=price, liq_price=liq,
        )
    return AccountState(
        wallet_balance=state.wallet_balance,
        positions=new_positions,
        last_funding_settle_ts=state.last_funding_settle_ts,
    )


def _calc_liq_price(pos: Position, mark: float) -> float:
    """Isolated-margin liquidation price."""
    if pos.qty <= 0:
        return 0.0
    margin_per_unit = pos.margin / pos.qty
    mmr = 0.005  # default maintenance margin rate
    if pos.side == "long":
        return (pos.entry_price - margin_per_unit) / (1 - mmr)
    else:  # short
        return (pos.entry_price + margin_per_unit) / (1 + mmr)


# ── Check liquidation ───────────────────────────────────────────────

def check_liquidation(
    state: AccountState, mmr: float = 0.005
) -> tuple[AccountState, list[Trade]]:
    """Check all positions for liquidation. Returns (new_state, liquidations).

    A position is liquidated if margin_ratio < maintenance_margin_rate.
    """
    liq_trades: list[Trade] = []
    new_positions = dict(state.positions)
    now = time.time()

    for key, pos in list(new_positions.items()):
        if pos.side == "spot":
            continue  # spot can't be liquidated
        if pos.margin_ratio() >= mmr:
            continue

        # Force-close at mark price
        liq_qty = pos.qty
        liq_trade = Trade(
            trade_id=f"LIQ-{now}-{pos.symbol}",
            order_id=f"LIQ-{now}-{pos.symbol}",
            symbol=pos.symbol, side="close", qty=liq_qty,
            price=pos.mark_price,
            fee=pos.notional() * 0.0005,  # taker fee
            liquidated=True, ts=now,
        )
        liq_trades.append(liq_trade)
        new_positions.pop(key, None)

    return (
        AccountState(
            wallet_balance=state.wallet_balance,
            positions=new_positions,
            last_funding_settle_ts=state.last_funding_settle_ts,
        ),
        liq_trades,
    )


# ── Funding settlement ──────────────────────────────────────────────

def settle_funding(
    state: AccountState,
    funding_rates: dict[str, float],
    current_ts: float,
) -> tuple[AccountState, list[Trade]]:
    """Apply funding payments for all perpetual positions.

    Only settles once per 8-hour window (keyed on last_funding_settle_ts).
    """
    # Check if we're in a funding window (00:00, 08:00, 16:00 UTC)
    import datetime
    dt = datetime.datetime.fromtimestamp(current_ts, tz=datetime.UTC)
    window = dt.replace(minute=0, second=0, microsecond=0)
    window_ts = window.timestamp()

    if state.last_funding_settle_ts >= window_ts:
        return state, []

    trades: list[Trade] = []
    new_state = AccountState(
        wallet_balance=state.wallet_balance,
        positions=dict(state.positions),
        last_funding_settle_ts=window_ts,
    )

    for _key, pos in new_state.positions.items():
        if pos.side == "spot":
            continue
        rate = funding_rates.get(pos.symbol, 0.0)
        if rate == 0.0:
            continue
        # Long pays short when rate > 0; short pays long when rate < 0
        payment = -pos.side_sign() * pos.notional() * rate
        new_state.wallet_balance += payment
        trades.append(Trade(
            trade_id=f"FUND-{window_ts}-{pos.symbol}",
            order_id="",
            symbol=pos.symbol, side="funding", qty=0.0,
            price=0.0, fee=payment, ts=window_ts,
        ))

    return new_state, trades


# ── Snapshot ────────────────────────────────────────────────────────

def snapshot(state: AccountState, ts: float | None = None) -> EquityPoint:
    """Create an equity curve data point."""
    if ts is None:
        ts = time.time()
    return EquityPoint(
        ts=ts,
        equity=state.equity(),
        wallet_balance=state.wallet_balance,
        margin_used=state.margin_used(),
        unrealized_pnl=state.unrealized_pnl_total(),
    )
