"""Risk control — stateless order validation.

Pure function: receives order intent + account state + limits,
returns (approved: bool, reason: str). No side effects.
"""

from dataclasses import dataclass

from superplatform.data.trading import AccountState, OrderRequest, Position


@dataclass
class RiskLimits:
    """Configurable risk limits — read from config/live.yaml."""

    max_leverage: float = 20.0
    max_order_notional: float = 100_000.0
    max_position_notional_per_symbol: float = 100_000.0
    maintenance_margin_rate: float = 0.005
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0


def check_order(
    request: OrderRequest,
    state: AccountState,
    prices: dict[str, float],
    limits: RiskLimits,
) -> tuple[bool, str]:
    """Validate an order against risk limits.

    Returns:
        (True, "") if the order passes all checks.
        (False, "reason") if any check fails.

    Checks (in order):
      1. Quantity must be positive.
      2. Price must be available for the symbol.
      3. Leverage must be within [1, max_leverage].
      4. Single order notional ≤ max_order_notional.
      5. Symbol-level position notional ≤ max_position_notional_per_symbol.
      6. Wallet balance covers margin + estimated fee.
      7. Spot sells cannot exceed current holdings.
    """
    # 1. Positive quantity
    if request.qty <= 0:
        return False, "Quantity must be positive"

    # 2. Price available
    price = prices.get(request.symbol)
    if price is None or price <= 0:
        return False, f"No price available for {request.symbol}"

    # 3. Leverage
    is_spot = request.leverage <= 1.0 and request.side in ("buy", "sell")
    if not is_spot:
        if request.leverage < 1.0 or request.leverage > limits.max_leverage:
            return False, (
                f"Leverage {request.leverage}x outside [1, {limits.max_leverage}]"
            )

    # 4. Order notional
    order_notional = request.qty * price
    if order_notional > limits.max_order_notional:
        return False, (
            f"Order notional {order_notional:.0f} > max {limits.max_order_notional:.0f}"
        )

    # 5. Position notional per symbol
    pos = state.get_position(request.symbol, _resolve_pos_side(request.side))
    current_notional = pos.notional() if pos else 0.0
    is_increasing = _is_position_increasing(request, pos)
    if is_increasing:
        new_notional = current_notional + order_notional
        if new_notional > limits.max_position_notional_per_symbol:
            return False, (
                f"Position notional {new_notional:.0f} > "
                f"max {limits.max_position_notional_per_symbol:.0f} "
                f"for {request.symbol}"
            )

    # 6. Wallet balance
    if is_increasing:
        added_margin = order_notional / request.leverage if request.leverage > 0 else order_notional
        est_fee = order_notional * limits.taker_fee_bps / 10000
        required = added_margin + est_fee
        if required > state.wallet_balance:
            return False, (
                f"Insufficient balance: need {required:.2f}, "
                f"wallet {state.wallet_balance:.2f}"
            )

    # 7. Spot sell
    if request.side == "sell" and is_spot:
        if pos is None or pos.qty < request.qty:
            return False, (
                f"Insufficient spot holding: have {pos.qty if pos else 0}, "
                f"want to sell {request.qty}"
            )

    return True, ""


def _resolve_pos_side(side: str) -> str:
    """Map order side to position side."""
    if side in ("buy", "sell"):
        return "spot"
    return side  # "long", "short"


def _is_position_increasing(request: OrderRequest, pos: Position | None) -> bool:
    """Does this order increase absolute position size?"""
    if pos is None:
        return True
    # Same direction → increasing
    if request.side in ("buy", "long") and pos.side in ("spot", "long"):
        return True
    if request.side in ("sell", "short") and pos.side in ("spot", "short"):
        return True
    # Opposite direction — could be decreasing (closing) or flipping
    # We only block if it's a flip (reversing beyond zero)
    if request.qty > pos.qty:
        return True  # flip: exceeds existing position
    return False  # reducing / closing
