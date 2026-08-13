"""Live trading state and control endpoints."""

import asyncio

from fastapi import APIRouter, HTTPException

from superplatform.network.brokers import build_broker
from superplatform.runtime.live import LiveRuntime
import superplatform_web.state as _state
from superplatform_web.state import config, live_runtime, providers
from superplatform_web.universe import fetch_tickers, stored_active

router = APIRouter(prefix="/api/live", tags=["live"])


async def _broker_data(broker) -> dict:
    """Extract all broker state in one async pass."""
    positions, orders, trades, curve = await asyncio.gather(
        broker.get_positions(),
        broker.get_orders(),
        broker.get_trades(limit=200),
        broker.get_equity_curve(limit=500),
    )

    return {
        "positions": [{
            "symbol": p.symbol, "side": p.side, "qty": p.qty,
            "entry_price": p.entry_price, "mark_price": p.mark_price,
            "unrealized_pnl": p.unrealized_pnl, "margin": p.margin,
            "leverage": p.leverage,
        } for p in positions],
        "orders": [{
            "order_id": o.order_id, "symbol": o.symbol, "side": o.side,
            "qty": o.qty, "filled_qty": o.filled_qty, "status": o.status,
            "source": o.source,
        } for o in orders],
        "trades": [{
            "trade_id": t.trade_id, "symbol": t.symbol, "side": t.side,
            "qty": t.qty, "price": t.price, "fee": t.fee,
        } for t in trades],
        "equity_curve": [{
            "ts": e.ts, "equity": e.equity, "wallet_balance": e.wallet_balance,
            "margin_used": e.margin_used, "unrealized_pnl": e.unrealized_pnl,
        } for e in curve],
    }


@router.get("/state")
async def live_state():
    global live_runtime
    if live_runtime is None:
        # Configured backend id (e.g. "binance-testnet" / "simulated") so the
        # UI can show the right broker labels before any session starts.
        return {"running": False, "broker": config.get("live.broker", "simulated")}

    sched = live_runtime.scheduler.snapshot()
    state = live_runtime.state

    return {
        "running": True,
        "broker": live_runtime.broker.name,
        "tick": sched["tick_no"],
        "prices": sched["prices"],
        "data_stale": sched["data_stale"],
        "equity": state.equity(),
        "wallet_balance": state.wallet_balance,
        **(await _broker_data(live_runtime.broker)),
    }


@router.post("/start")
async def live_start(data: dict):
    global live_runtime
    if live_runtime is not None:
        return {"error": "Live runtime is already running"}

    strategy_name = data.get("strategy", "momentum_demo")
    symbols = data.get("symbols")
    # Trading a delisted symbol is the failure mode this selection feature
    # exists to prevent: reject unknown symbols up front. Guarded on `active`
    # so an offline session (no tickers, no stored universe) still starts — the
    # broker rejects bad symbols at fill time as the fallback.
    if symbols:
        active = set(await fetch_tickers()) | stored_active()
        if active:
            unknown = sorted(set(symbols) - active)
            if unknown:
                raise HTTPException(status_code=422, detail=f"未知（已下架/未收录）标的：{'、'.join(unknown)}")
    # Reuse the registry's Binance adapter for market data when available
    # (prices on testnet track production closely); the broker itself is
    # chosen by live.broker in config.
    # providers.get raises KeyError for an absent id (registry contract), so
    # probe membership first — the provider may not be registered (offline,
    # provider toggled off, or an empty registry under tests).
    if "binance-perp-kline" in providers:
        adapter = providers.get("binance-perp-kline").adapter
    else:
        adapter = None
    broker = build_broker(config, adapter=adapter, symbols=symbols)

    from superplatform.consumption.base import ConsumerConfig
    live = LiveRuntime(
        config,
        providers,
        broker,
        consumer=ConsumerConfig.backtest(),
        symbols=symbols,
    )
    live.setup(strategy_name=strategy_name)
    live_runtime = live
    _state.live_runtime = live  # so symbol-group writes are rejected mid-session

    # Start the tick loop in background
    asyncio.create_task(live.start())

    return {"status": "started", "strategy": strategy_name, "broker": broker.name, "symbols": symbols}


@router.post("/stop")
async def live_stop():
    global live_runtime
    if live_runtime is None:
        return {"error": "No live runtime running"}
    await live_runtime.stop()
    live_runtime = None
    _state.live_runtime = None
    return {"status": "stopped"}


@router.get("/broker")
async def broker_state():
    """Quick broker state without scheduler info."""
    global live_runtime
    if live_runtime is None:
        return {"running": False}
    return {"running": True, **(await _broker_data(live_runtime.broker))}
