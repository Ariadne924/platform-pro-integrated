"""Trading data types — shapes used by Account, Matcher, Broker, and Store.

Pure dataclasses with no external dependencies. These are the "data shapes"
that Consumption and Network layers agree on. Persistence (pandera models
for DuckDB) is handled separately in data/store/schemas.py.
"""

from dataclasses import dataclass, field

# ── Order ───────────────────────────────────────────────────────────

@dataclass
class OrderRequest:
    """Incoming order intent — what to place.

    This is the input to Broker.place_order(). It is NOT persisted;
    it gets converted to an Order (which IS persisted) after validation.
    """

    symbol: str
    side: str          # "buy" / "sell" (spot) or "long" / "short" / "close" (perp)
    qty: float
    order_type: str = "market"  # "market" or "limit"
    limit_price: float | None = None
    leverage: float = 1.0
    source: str = "manual"


@dataclass
class Order:
    """Persisted order — has an ID and lifecycle status.

    State machine: open → filled | cancelled | rejected
    Filled orders spawn Trade records.
    """

    order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float = 0.0
    limit_price: float | None = None
    status: str = "open"  # open / filled / cancelled / rejected
    reject_reason: str = ""
    source: str = "auto"
    created_ts: float = 0.0
    updated_ts: float = 0.0


# ── Trade ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    """A completed fill — one execution of (part of) an order.

    `side` semantics:
      spot:   "buy" / "sell"
      perp:   "long" / "short" / "close"
      system: "funding" (funding settlement), "liquidation" (forced close)
    """

    trade_id: str
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float = 0.0
    liquidated: bool = False
    ts: float = 0.0


# ── Position ────────────────────────────────────────────────────────

@dataclass
class Position:
    """A single position — spot holding or perpetual contract.

    spot positions:  side="spot",  leverage=1, margin=cost_basis
    perp positions:  side="long" or "short", isolated margin
    """

    symbol: str
    side: str           # "spot" / "long" / "short"
    qty: float
    entry_price: float
    leverage: float = 1.0
    margin: float = 0.0
    unrealized_pnl: float = 0.0
    mark_price: float = 0.0
    liq_price: float = 0.0  # 0.0 = not applicable (spot)

    def notional(self) -> float:
        return self.qty * self.mark_price if self.mark_price > 0 else self.qty * self.entry_price

    def margin_ratio(self) -> float:
        """Maintenance margin ratio. < mmr → liquidation."""
        n = self.notional()
        if n <= 0:
            return float("inf")
        return (self.margin + self.unrealized_pnl) / n

    def side_sign(self) -> int:
        """+1 for long/spot, -1 for short. Used for PnL direction."""
        return -1 if self.side == "short" else 1


# ── Account ─────────────────────────────────────────────────────────

@dataclass
class AccountState:
    """Snapshot of the full account at one point in time.

    wallet_balance: free USDT not tied up in margin.
    positions:      keyed by "{symbol}:{side}" for O(1) lookup.
    """

    wallet_balance: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    last_funding_settle_ts: float = 0.0

    def equity(self) -> float:
        """Total account value = wallet + margin + unrealized PnL."""
        margin = sum(p.margin for p in self.positions.values())
        upnl = sum(p.unrealized_pnl for p in self.positions.values())
        return self.wallet_balance + margin + upnl

    def margin_used(self) -> float:
        return sum(p.margin for p in self.positions.values())

    def unrealized_pnl_total(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def position_key(self, symbol: str, side: str) -> str:
        return f"{symbol}:{side}"

    def get_position(self, symbol: str, side: str) -> Position | None:
        return self.positions.get(self.position_key(symbol, side))


# ── Equity snapshot ─────────────────────────────────────────────────

@dataclass
class EquityPoint:
    """One row of the equity curve — persisted per tick."""

    ts: float
    equity: float
    wallet_balance: float
    margin_used: float
    unrealized_pnl: float
