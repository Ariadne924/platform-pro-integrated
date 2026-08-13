"""交易与账户 API（sim_platform 形状）：/api/trading/*。

全部映射到 03 的 LiveRuntime 会话内 broker（默认 SimulatedBroker 本地撮合）。
live 未启动时如实 503——不返回假持仓/假净值。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

import superplatform_web.state as _state
from superplatform_web import simserve

router = APIRouter(prefix="/api/trading", tags=["sim-trading"])


def _live():
    live = _state.live_runtime
    if live is None:
        raise HTTPException(
            status_code=503,
            detail="交易引擎未初始化：live 会话未启动（python run.py 默认自动启动模拟盘，"
                   "或 POST /api/live/start 手动启动）",
        )
    return live


class OrderRequestIn(BaseModel):
    """下单请求体（sim 形状）。"""

    symbol: str = Field(..., description="交易对，如 BTC/USDT")
    side: str = Field(..., description="buy/sell/long/short/close")
    order_type: str = Field(default="market")
    qty: float = Field(..., gt=0)
    price: float | None = Field(None)
    leverage: float | None = Field(None)
    market_type: str | None = Field(None)


def _order_dict(o) -> dict[str, Any]:
    return {
        "order_id": o.order_id,
        "symbol": simserve.ui_symbol(o.symbol),
        "side": o.side,
        "order_type": "limit" if o.limit_price else "market",
        "qty": float(o.qty),
        "filled_qty": float(o.filled_qty),
        "price": float(o.limit_price) if o.limit_price else None,
        "leverage": None,  # Order 记录未存杠杆（下单请求里有），如实 null
        "status": o.status,
        "source": o.source,
        "created_ts": simserve._ts_iso(o.created_ts),
        "reject_reason": o.reject_reason or "",
    }


def _trade_dict(t) -> dict[str, Any]:
    return {
        "trade_id": t.trade_id,
        "order_id": t.order_id,
        "symbol": simserve.ui_symbol(t.symbol),
        "side": t.side,
        "qty": float(t.qty),
        "price": float(t.price),
        "fee": float(t.fee),
        "ts": simserve._ts_iso(t.ts),
        "liquidated": bool(t.liquidated),
    }


@router.post("/orders")
async def create_order(req: OrderRequestIn) -> dict[str, Any]:
    """手动下单（走 live 会话 broker 的真实撮合/风控）。"""
    from superplatform.data.trading import OrderRequest

    live = _live()
    order_req = OrderRequest(
        symbol=simserve.core_symbol(req.symbol),
        side=req.side,
        qty=req.qty,
        order_type=req.order_type,
        limit_price=req.price,
        leverage=req.leverage or 1.0,
        source="manual",
    )
    order, reason = await live.broker.place_order(order_req)
    if order is None:
        return {"ok": False, "reason": reason}
    return {"ok": True, "order_id": order.order_id, "status": order.status}


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str = Path(...)) -> dict[str, Any]:
    live = _live()
    ok = await live.broker.cancel_order(order_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"挂单不存在或已终结: {order_id}")
    return {"ok": True, "order_id": order_id}


@router.get("/orders")
async def list_orders(
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    live = _live()
    orders = await live.broker.get_orders(status=status)
    rows = [_order_dict(o) for o in orders[-limit:]]
    return {"data": rows, "count": len(rows)}


@router.get("/positions")
async def list_positions() -> dict[str, Any]:
    live = _live()
    rows = [
        {
            "symbol": simserve.ui_symbol(p.symbol),
            "category": "spot" if p.side == "spot" else "perp",
            "side": p.side,
            "qty": float(p.qty),
            "entry_price": float(p.entry_price),
            "mark_price": float(p.mark_price),
            "leverage": float(p.leverage),
            "margin": float(p.margin),
            "unrealized_pnl": float(p.unrealized_pnl),
            "liq_price": float(p.liq_price) if p.liq_price else None,
        }
        for p in await live.broker.get_positions()
    ]
    return {"data": rows, "count": len(rows)}


@router.get("/trades")
async def list_trades(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    live = _live()
    rows = [_trade_dict(t) for t in await live.broker.get_trades(limit=limit)]
    return {"data": rows, "count": len(rows)}


@router.get("/equity")
async def equity_curve(limit: int = Query(2000, ge=1, le=10000)) -> dict[str, Any]:
    live = _live()
    curve = await live.broker.get_equity_curve(limit=limit)
    rows = [
        {
            "ts": simserve._ts_iso(e.ts),
            "equity": float(e.equity),
            "wallet_balance": float(e.wallet_balance),
            "margin_used": float(e.margin_used),
            "unrealized_pnl": float(e.unrealized_pnl),
        }
        for e in curve
    ]
    return {"data": rows, "count": len(rows)}


@router.get("/account")
async def account_summary() -> dict[str, Any]:
    live = _live()
    acc = live.state
    return {
        "equity": acc.equity(),
        "wallet_balance": acc.wallet_balance,
        "margin_used": acc.margin_used(),
        "unrealized_pnl": acc.unrealized_pnl_total(),
        "win_rate": None,
        "max_drawdown_pct": None,
    }
