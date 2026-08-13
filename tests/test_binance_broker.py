"""Unit tests for BinanceBroker + build_broker factory (no network).

A FakeFuturesClient stands in for ``binance.um_futures.UMFutures``. The broker
runs every SDK call through ``asyncio.to_thread``, so the fake only needs to be
plain synchronous methods with the same names/signatures as the real client.

Repo convention: no pytest-asyncio — coroutines are driven with ``asyncio.run``.
"""

import asyncio

import pytest
from binance.error import ClientError

from superplatform.data.trading import OrderRequest
from superplatform.network.brokers import BinanceBroker, SimulatedBroker, build_broker
from superplatform.runtime.config import Config


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


# ── Fakes ────────────────────────────────────────────────────────────

class FakeAdapter:
    """Stand-in for the market-data adapter (not exercised in these tests)."""

    name = "fake"


class FakeFuturesClient:
    """In-process stand-in for binance.um_futures.UMFutures.

    Mirrors the private-endpoint surface the broker uses, with a minimal
    one-way-mode ledger so orders update the net position like a real fill.
    """

    def __init__(self, wallet: float = 100_000.0):
        self.base_url = "https://testnet.binancefuture.com"
        self.calls: list[tuple] = []   # (method, args, kwargs)
        self.wallet = wallet
        self._positions: dict[str, dict] = {}
        self._open_orders: list[dict] = []
        self._account_trades: list[dict] = []
        self._next_order_id = 1000
        self._step_size = "0.001"
        self._min_notional = "5"
        self._last_price = 100.0
        self._order_status = "FILLED"
        self.reject_next: ClientError | None = None   # raised by next new_order
        self._fail_account = False

    # ── helpers ──
    def _set_position(self, symbol, amt, *, entry=100.0, mark=100.0, leverage=10):
        self._positions[symbol] = {
            "symbol": symbol, "positionSide": "BOTH", "amt": float(amt),
            "entry": float(entry), "mark": float(mark), "leverage": float(leverage),
        }

    @staticmethod
    def _serialize_pos(p: dict) -> dict:
        amt = p["amt"]
        margin = abs(amt) * p["mark"] / p["leverage"] if p["leverage"] else 0.0
        return {
            "symbol": p["symbol"],
            "positionSide": p["positionSide"],
            "positionAmt": str(amt),
            "entryPrice": str(p["entry"]),
            "markPrice": str(p["mark"]),
            "unRealizedProfit": str(amt * (p["mark"] - p["entry"])),
            "liquidationPrice": "0",
            "leverage": str(p["leverage"]),
            "positionInitialMargin": str(margin),
        }

    # ── SDK surface ──
    def exchange_info(self, symbol=None):
        self.calls.append(("exchange_info", (symbol,), {}))
        syms = [symbol] if symbol else ["BTCUSDT", "ETHUSDT"]
        return {"symbols": [
            {"symbol": s, "filters": [
                {"filterType": "LOT_SIZE", "stepSize": self._step_size, "minQty": self._step_size},
                {"filterType": "MIN_NOTIONAL", "notional": self._min_notional},
            ]}
            for s in syms
        ]}

    def change_leverage(self, symbol, leverage, **kwargs):
        self.calls.append(("change_leverage", (symbol, leverage), {}))
        return {"symbol": symbol, "leverage": str(leverage)}

    def get_position_risk(self, symbol=None, **kwargs):
        self.calls.append(("get_position_risk", (symbol,), {}))
        rows = [self._serialize_pos(p) for p in self._positions.values()]
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        return rows

    def account(self, **kwargs):
        self.calls.append(("account", (), dict(kwargs)))
        if self._fail_account:
            raise ClientError(-1, -1001, "connection reset", None)
        upnl = sum(p["amt"] * (p["mark"] - p["entry"]) for p in self._positions.values())
        return {
            "totalWalletBalance": str(self.wallet),
            "totalUnrealizedProfit": str(upnl),
            "totalMarginBalance": str(self.wallet + upnl),
        }

    def new_order(self, symbol, side, type, **kwargs):
        self.calls.append(("new_order", (symbol, side, type), dict(kwargs)))
        if self.reject_next is not None:
            raise self.reject_next
        qty = float(kwargs.get("quantity") or 0.0)
        reduce_only = bool(kwargs.get("reduceOnly"))
        order_id = self._next_order_id
        self._next_order_id += 1

        if self._order_status == "REJECTED":
            return {
                "orderId": order_id, "symbol": symbol, "side": side, "type": type,
                "status": "REJECTED", "origQty": str(qty), "executedQty": "0",
                "avgPrice": "0", "price": "0", "reduceOnly": reduce_only,
            }

        price = self._last_price
        signed = qty if side == "BUY" else -qty
        old = self._positions.get(symbol)
        old_amt = old["amt"] if old else 0.0
        if reduce_only:
            new_amt = old_amt + signed
            if old_amt * new_amt < 0:        # crossed zero → clamp flat
                new_amt = 0.0
            if new_amt == 0.0:
                self._positions.pop(symbol, None)
            else:
                self._positions[symbol] = {
                    "symbol": symbol, "positionSide": "BOTH", "amt": new_amt,
                    "entry": old["entry"], "mark": price, "leverage": old["leverage"],
                }
        else:
            new_amt = old_amt + signed
            if old is not None and old_amt * new_amt >= 0 and abs(new_amt) > abs(old_amt):
                old_qty, add_qty = abs(old_amt), abs(new_amt) - abs(old_amt)
                entry = (old["entry"] * old_qty + price * add_qty) / (old_qty + add_qty)
            else:
                entry = price
            self._positions[symbol] = {
                "symbol": symbol, "positionSide": "BOTH", "amt": new_amt,
                "entry": entry, "mark": price, "leverage": 10,
            }

        return {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": type,
            "status": self._order_status,
            "origQty": str(qty),
            "executedQty": str(qty),
            "avgPrice": str(price),
            "price": "0",
            "reduceOnly": reduce_only,
        }

    def cancel_order(self, symbol, orderId=None, origClientOrderId=None, **kwargs):
        self.calls.append(("cancel_order", (symbol,), {"orderId": orderId, "origClientOrderId": origClientOrderId}))
        return {"symbol": symbol, "status": "CANCELED", "orderId": orderId}

    def get_orders(self, **kwargs):
        self.calls.append(("get_orders", (), {}))
        return list(self._open_orders)

    def get_account_trades(self, symbol, **kwargs):
        self.calls.append(("get_account_trades", (symbol,), {}))
        return [t for t in self._account_trades if t.get("symbol") == symbol]


def broker():
    fake = FakeFuturesClient()
    b = BinanceBroker(FakeAdapter(), fake, default_leverage=10)
    return b, fake


def req(symbol="BTCUSDT", side="buy", qty=0.1):
    return OrderRequest(symbol=symbol, side=side, qty=qty, source="auto")


# ── Side mapping ─────────────────────────────────────────────────────

def test_side_mapping_buy_and_short():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})

    order, reason = run(b.place_order(req(side="buy", qty=0.5)))
    assert reason == "" and order.status == "filled"
    _, args, kwargs = fake.calls[-1]
    assert args[:2] == ("BTCUSDT", "BUY")
    assert not kwargs.get("reduceOnly")

    order, reason = run(b.place_order(req(side="short", qty=0.3)))
    assert reason == ""
    _, args, kwargs = fake.calls[-1]
    assert args[1] == "SELL"
    assert not kwargs.get("reduceOnly")


def test_close_mapping():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})

    order, reason = run(b.place_order(req(side="close", qty=0.1)))
    assert order is None and "no open position" in reason

    run(b.place_order(req(side="buy", qty=0.5)))          # now net long
    order, reason = run(b.place_order(req(side="close", qty=0.5)))
    assert reason == ""
    _, args, kwargs = fake.calls[-1]
    assert args[1] == "SELL"
    assert kwargs.get("reduceOnly") is True


def test_buy_on_short_is_reduce_only():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})
    fake._set_position("BTCUSDT", -0.5)                   # net short

    order, reason = run(b.place_order(req(side="buy", qty=0.2)))
    assert reason == ""
    _, args, kwargs = fake.calls[-1]
    assert args[1] == "BUY"
    assert kwargs.get("reduceOnly") is True


def test_sell_on_long_is_reduce_only():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})
    fake._set_position("BTCUSDT", 0.5)                    # net long

    order, reason = run(b.place_order(req(side="sell", qty=0.2)))
    assert reason == ""
    _, args, kwargs = fake.calls[-1]
    assert args[1] == "SELL"
    assert kwargs.get("reduceOnly") is True


# ── Order lifecycle ─────────────────────────────────────────────────

def test_rejected_order_status():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})
    fake._order_status = "REJECTED"

    order, reason = run(b.place_order(req(side="buy", qty=0.5)))
    assert order is not None and order.status == "rejected"
    assert order.filled_qty == 0


def test_client_error_rejects_order():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})
    fake.reject_next = ClientError(400, -2019, "Margin is insufficient", None)

    order, reason = run(b.place_order(req(side="buy", qty=0.5)))
    assert order is None and "binance rejected" in reason


def test_cancel_order_routes_and_updates_ledger():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})
    order, _ = run(b.place_order(req(side="buy", qty=0.5)))

    assert run(b.cancel_order(order.order_id)) is True
    assert b._orders[order.order_id].status == "cancelled"
    assert run(b.cancel_order("does-not-exist")) is False


def test_get_orders_merges_testnet_and_ledger():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})
    order, _ = run(b.place_order(req(side="buy", qty=0.5)))

    fake._open_orders.append({
        "orderId": 555, "symbol": "ETHUSDT", "side": "BUY", "type": "MARKET",
        "status": "NEW", "origQty": "0.2", "executedQty": "0.1", "price": "0",
    })

    orders = run(b.get_orders())
    assert any(o.order_id == "555" for o in orders)
    assert any(o.order_id == order.order_id for o in orders)

    open_only = run(b.get_orders(status="open"))
    assert open_only and all(o.status == "open" for o in open_only)


# ── Quantity rounding ───────────────────────────────────────────────

def test_round_qty_floors_to_step_and_rejects_small():
    b, _ = broker()
    b.update_prices({"BTCUSDT": 100.0})

    # 0.123456 floored to a 0.001 step → 0.123
    assert run(b._round_qty("BTCUSDT", 0.123456, 100.0)) == pytest.approx(0.123)
    # below one step → floors to zero → reject
    assert run(b._round_qty("BTCUSDT", 0.0005, 100.0)) is None
    # notional below MIN_NOTIONAL (5 USDT): 0.03 × 100 = 3 < 5 → reject
    assert run(b._round_qty("BTCUSDT", 0.03, 100.0)) is None
    # above MIN_NOTIONAL: 0.06 × 100 = 6 ≥ 5 → ok
    assert run(b._round_qty("BTCUSDT", 0.06, 100.0)) == pytest.approx(0.06)


# ── Hydration & account ─────────────────────────────────────────────

def test_hydrate_equity_equals_total_margin_balance():
    b, fake = broker()
    fake.wallet = 100_000.0
    # 0.5 BTC long, entry 100, mark 110 → upnl +5, margin = 0.5*110/10 = 5.5
    fake._set_position("BTCUSDT", 0.5, entry=100.0, mark=110.0)

    acc = run(b._fetch_account())
    assert acc.positions["BTCUSDT:long"].qty == pytest.approx(0.5)
    assert acc.wallet_balance == pytest.approx(100_000.0 - 5.5)
    assert acc.equity() == pytest.approx(100_000.0 + 5.0)   # == totalMarginBalance

    acc2 = BinanceBroker._hydrate(fake.account(), fake.get_position_risk())
    assert acc2.equity() == pytest.approx(100_000.0 + 5.0)


def test_tick_snapshot_and_cached_account():
    b, fake = broker()
    fake.wallet = 50_000.0

    run(b.tick())
    assert len(b._equity) == 1
    assert b._equity[0].equity == pytest.approx(50_000.0)
    state = run(b.fetch_account_state())
    assert state is b._account
    assert len([c for c in fake.calls if c[0] == "account"]) == 1

    run(b.tick())                                          # second point, refetched
    assert len(b._equity) == 2
    assert len([c for c in fake.calls if c[0] == "account"]) == 2


def test_tick_swallows_client_error():
    b, fake = broker()
    fake._fail_account = True
    run(b.tick())                                          # must not raise
    assert b._equity == []


def test_get_positions_after_tick():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})
    run(b.place_order(req(side="buy", qty=0.5)))
    run(b.tick())

    positions = run(b.get_positions())
    assert len(positions) == 1
    pos = positions[0]
    assert (pos.symbol, pos.side) == ("BTCUSDT", "long")
    assert pos.qty == pytest.approx(0.5)
    assert pos.mark_price == pytest.approx(100.0)


# ── recvWindow injection (clock-skew resilience) ────────────────────

def test_signed_calls_carry_recv_window_public_does_not():
    b, fake = broker()
    b.update_prices({"BTCUSDT": 100.0})

    # signed order call carries recvWindow
    run(b.place_order(req(side="buy", qty=0.5)))
    _, _, order_kwargs = [c for c in fake.calls if c[0] == "new_order"][-1]
    assert order_kwargs.get("recvWindow") == b._recv_window_ms

    # signed account fetch carries recvWindow
    run(b._fetch_account())
    _, _, acc_kwargs = [c for c in fake.calls if c[0] == "account"][-1]
    assert acc_kwargs.get("recvWindow") == b._recv_window_ms

    # public exchange_info must NOT receive recvWindow
    run(b._symbol_info_cached("BTCUSDT"))
    _, _, ei_kwargs = [c for c in fake.calls if c[0] == "exchange_info"][-1]
    assert "recvWindow" not in ei_kwargs


# ── Factory ─────────────────────────────────────────────────────────

def test_factory_simulated_reads_capital():
    cfg = Config({"live": {
        "broker": "simulated",
        "paper": {"initial_capital_usdt": 777.0},
    }})
    b = build_broker(cfg)
    assert isinstance(b, SimulatedBroker)
    assert b._account.wallet_balance == pytest.approx(777.0)


def test_factory_testnet_missing_keys_raises(monkeypatch):
    cfg = Config({"live": {
        "broker": "binance-testnet",
        "binance_testnet": {"api_key_env": "TN_KEY", "api_secret_env": "TN_SECRET"},
    }})
    monkeypatch.delenv("TN_KEY", raising=False)
    monkeypatch.delenv("TN_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="TN_KEY"):
        build_broker(cfg)


def test_factory_testnet_with_keys_builds_binance_broker(monkeypatch):
    cfg = Config({"live": {
        "broker": "binance-testnet",
        "symbols": ["BTCUSDT"],
        "binance_testnet": {
            "base_url": "https://testnet.binancefuture.com",
            "api_key_env": "TN_KEY",
            "api_secret_env": "TN_SECRET",
            "default_leverage": 5,
            "recv_window_ms": 40000,
        },
    }})
    monkeypatch.setenv("TN_KEY", "k")
    monkeypatch.setenv("TN_SECRET", "s")
    ad = FakeAdapter()
    b = build_broker(cfg, adapter=ad)
    assert isinstance(b, BinanceBroker)
    assert b._adapter is ad
    assert b._default_leverage == 5
    assert b._symbols == {"BTCUSDT"}
    assert b._recv_window_ms == 40000
    assert b._client.base_url == "https://testnet.binancefuture.com"


def test_factory_testnet_requires_explicit_symbols(monkeypatch):
    cfg = Config({"live": {
        "broker": "binance-testnet",
        "binance_testnet": {"api_key_env": "TN_KEY", "api_secret_env": "TN_SECRET"},
    }})
    monkeypatch.setenv("TN_KEY", "k")
    monkeypatch.setenv("TN_SECRET", "s")
    with pytest.raises(RuntimeError, match="live.symbols"):
        build_broker(cfg)


def test_factory_unknown_broker_raises():
    cfg = Config({"live": {"broker": "bogus"}})
    with pytest.raises(ValueError):
        build_broker(cfg)
