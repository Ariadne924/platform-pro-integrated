"""Tests for the consumption-layer strategy backtester and signal engine.

Covers the realism fixes: gross-exposure cap at 100%, trading costs, and the
hard liquidation floor. NOTE: this targets `superplatform.consumption.backtest` /
`superplatform.consumption.engine` — NOT `superplatform.evaluation.backtest` (the
separate factor/decile engine covered by tests/test_backtest.py).
"""

import numpy as np
import pandas as pd
import pytest

from superplatform.consumption.backtest import backtest
from superplatform.consumption.engine import generate_orders
from superplatform.data.trading import AccountState


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n)


def _sig(rows: list[tuple]) -> pd.DataFrame:
    """Rows: (timestamp, symbol, position)."""
    return pd.DataFrame(rows, columns=["timestamp", "symbol", "position"])


def _prices(closes: dict[str, list[float]]) -> dict[str, pd.DataFrame]:
    """Daily close frames keyed by symbol."""
    n = max(len(c) for c in closes.values())
    idx = _dates(n)
    return {sym: pd.DataFrame({"timestamp": idx, "close": c}) for sym, c in closes.items()}


# ── Gross-exposure normalization ─────────────────────────────────────


def test_two_full_weight_symbols_become_equal_weight_not_doubled():
    # Both symbols always at position 1.0, both +10%/day. An equal-weight
    # portfolio returns 10%/day (0.5·0.10 + 0.5·0.10); an unscaled cross-symbol
    # sum would return 20%/day.
    idx = _dates(3)
    signals = _sig([(idx[i], sym, 1.0) for i in range(3) for sym in ("A", "B")])
    prices = _prices({"A": [100, 110, 121], "B": [200, 220, 242]})

    bt = backtest(signals, prices)

    assert bt.liquidated_at is None
    assert bt.total_return == pytest.approx(1.1**2 - 1, abs=1e-9)


def test_gross_below_one_is_not_scaled():
    # momentum_demo-style ±0.4: gross 0.8 < 1 → weights untouched, portfolio
    # returns 0.4·0.10 + 0.4·0.10 = 8%/day.
    idx = _dates(3)
    rows = [(idx[i], sym, 0.4) for i in range(3) for sym in ("A", "B")]
    signals = _sig(rows)
    prices = _prices({"A": [100, 110, 121], "B": [200, 220, 242]})

    bt = backtest(signals, prices)

    assert bt.total_return == pytest.approx(1.08**2 - 1, abs=1e-9)
    assert bt.liquidated_at is None


def test_single_symbol_behaves_identically_to_old_engine():
    # One symbol at 1.0 → scale 1; result matches the pre-normalization math.
    idx = _dates(3)
    signals = _sig([(idx[i], "A", 1.0) for i in range(3)])
    prices = _prices({"A": [100, 110, 121]})

    bt = backtest(signals, prices)

    assert bt.total_return == pytest.approx(1.1**2 - 1, abs=1e-9)


# ── Trading costs ────────────────────────────────────────────────────


def test_rebalance_and_entry_cost_are_charged():
    # Flat prices (gross pnl 0), positions go long → short → short. Entry
    # trades |1−0| = 1 unit; the flip trades |−1−1| = 2 units. rate = 7 bps.
    idx = _dates(3)
    signals = _sig([(idx[0], "A", 1.0), (idx[1], "A", -1.0), (idx[2], "A", -1.0)])
    prices = _prices({"A": [100, 100, 100]})
    rate = (4 + 3) / 10000.0

    bt = backtest(signals, prices, taker_fee_bps=4.0, slippage_bps=3.0)

    assert bt.total_return == pytest.approx((1 - rate) * (1 - 2 * rate) - 1, abs=1e-9)


def test_default_args_are_zero_cost():
    idx = _dates(3)
    signals = _sig([(idx[0], "A", 1.0), (idx[1], "A", -1.0), (idx[2], "A", -1.0)])
    prices = _prices({"A": [100, 100, 100]})

    bt = backtest(signals, prices)

    assert bt.total_return == pytest.approx(0.0, abs=1e-12)


def test_negative_rate_is_rejected():
    idx = _dates(3)
    signals = _sig([(idx[i], "A", 1.0) for i in range(3)])
    prices = _prices({"A": [100, 110, 121]})

    with pytest.raises(ValueError):
        backtest(signals, prices, taker_fee_bps=-1.0, slippage_bps=0.0)


# ── Liquidation floor ────────────────────────────────────────────────


def test_equity_cannot_compound_through_a_wipeout():
    # A 1.0-long that loses 100% in one day (close 100 → 0) → equity hits
    # exactly 0 and freezes there; later days don't compound.
    idx = _dates(4)
    signals = _sig([(idx[i], "A", 1.0) for i in range(4)])
    prices = _prices({"A": [100, 100, 0, 0]})

    bt = backtest(signals, prices)

    assert bt.liquidated_at == pd.Timestamp("2024-01-03")
    assert bt.liquidation is not None
    assert bt.total_return == pytest.approx(-1.0, abs=1e-9)
    assert bt.equity["equity"].iloc[-1] == pytest.approx(0.0, abs=1e-12)
    assert (bt.equity["equity"] >= 0).all()


# ── Signal engine (live path) ────────────────────────────────────────


def test_generate_orders_caps_total_gross_exposure():
    # Two symbols at 1.0 → each sized at half equity, total notional ≤ equity.
    state = AccountState(wallet_balance=1000.0)
    idx = _dates(1)
    signals = _sig([(idx[0], "A", 1.0), (idx[0], "B", 1.0)])
    prices = {"A": 100.0, "B": 200.0}

    orders = generate_orders(signals, state, prices)

    a_qty = sum(o.qty for o in orders if o.symbol == "A")
    b_qty = sum(o.qty for o in orders if o.symbol == "B")
    assert a_qty * prices["A"] == pytest.approx(500.0, abs=1e-9)
    assert b_qty * prices["B"] == pytest.approx(500.0, abs=1e-9)
    assert a_qty * prices["A"] + b_qty * prices["B"] <= 1000.0 + 1e-9


def test_generate_orders_single_symbol_unchanged():
    state = AccountState(wallet_balance=1000.0)
    idx = _dates(1)
    signals = _sig([(idx[0], "A", 1.0)])

    orders = generate_orders(signals, state, {"A": 100.0})

    a_qty = sum(o.qty for o in orders if o.symbol == "A")
    assert a_qty * 100.0 == pytest.approx(1000.0, abs=1e-9)


def test_generate_orders_zero_gross_is_noop():
    # All-flat signals → gross 0 → no target notional, no orders placed.
    state = AccountState(wallet_balance=1000.0)
    idx = _dates(1)
    signals = _sig([(idx[0], "A", 0.0), (idx[0], "B", 0.0)])

    orders = generate_orders(signals, state, {"A": 100.0, "B": 200.0})

    assert orders == []
