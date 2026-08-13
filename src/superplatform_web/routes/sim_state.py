"""聚合状态 API（sim_platform 形状）：GET /api/state（前端 30s 轮询主入口）。

账户/持仓来自 03 的 LiveRuntime（run.py 默认自动启动模拟盘会话；
未启动则 account=None，前端如实显示「--」）；行情快照来自 01 的 DuckDB 缓存；
策略摘要来自 02 的双文件注册中心。本平台无独立信号引擎/CUDA 后端/后台回填
守护进程，对应字段如实置 null/false。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

import superplatform_web.state as _state
from superplatform_web import simserve
from superplatform_web.routes.sim_market import tickers as _tickers_impl

router = APIRouter(prefix="/api/state", tags=["sim-state"])


@router.get("")
async def state() -> dict[str, Any]:
    """一次返回账户 + 持仓 + 行情 + 策略摘要 + 调度器状态。"""
    live = _state.live_runtime

    # 调度器状态（live 未跑则如实报停止）
    if live is not None:
        snap = live.scheduler.snapshot()
        scheduler = {
            "running": bool(snap.get("running")),
            "tick_count": snap.get("tick_no", 0),
            "current_tick_ts": None,
            "last_prices": {simserve.ui_symbol(k): v for k, v in (snap.get("prices") or {}).items()},
            "data_stale": snap.get("data_stale", False),
            "stale_symbols": [simserve.ui_symbol(s) for s in snap.get("stale_symbols") or []],
            "last_tick_duration_sec": snap.get("last_tick_duration"),
            "server_time_utc": simserve.utcnow_iso(),
        }
    else:
        scheduler = {
            "running": False,
            "tick_count": 0,
            "current_tick_ts": None,
            "last_prices": {},
            "data_stale": False,
            "stale_symbols": [],
            "server_time_utc": simserve.utcnow_iso(),
        }

    # 账户快照 + 持仓（03 LiveRuntime 的 AccountState 本地镜像）
    account_summary: dict[str, Any] | None = None
    positions: list[dict[str, Any]] = []
    if live is not None:
        acc = live.state
        account_summary = {
            "equity": acc.equity(),
            "wallet_balance": acc.wallet_balance,
            "margin_used": acc.margin_used(),
            "unrealized_pnl": acc.unrealized_pnl_total(),
            # 模拟盘会话未统计胜率/回撤——如实置 null，前端显示「--」
            "win_rate": None,
            "max_drawdown_pct": None,
        }
        for (key, pos) in acc.positions.items():
            category = "spot" if pos.side == "spot" else "perp"
            positions.append({
                "symbol": simserve.ui_symbol(pos.symbol),
                "category": category,
                "side": pos.side,
                "qty": float(pos.qty),
                "entry_price": float(pos.entry_price),
                "leverage": float(pos.leverage),
                "margin": float(pos.margin),
                "unrealized_pnl": float(pos.unrealized_pnl),
                "liq_price": float(pos.liq_price) if pos.liq_price else None,
            })

    # 行情快照（复用 /api/market/tickers 的同一实现，01 缓存）
    tickers: dict[str, Any] = {}
    if simserve.get_store() is not None:
        tickers = (await _tickers_impl())["symbols"]

    # 策略摘要（02 双文件注册中心；增量扫描=热插拔）
    strategy_summary: dict[str, Any] | None = None
    try:
        items = simserve.strategy_registry().list_strategies()
        strategy_summary = {
            "count": len(items),
            "active": sum(1 for x in items if x.get("status") == "active"),
            "invalid": sum(1 for x in items if x.get("status") == "invalid"),
        }
    except Exception:
        strategy_summary = None

    return {
        "time_utc": simserve.utcnow_iso(),
        "scheduler": scheduler,
        "account": account_summary,
        "positions": positions,
        "tickers": tickers,
        # 本平台无双文件因子的常驻最新值计算（评估按需触发），如实空 dict
        "factor_values": {},
        # 无独立信号引擎（信号由策略产出）
        "signal_status": None,
        "strategy_summary": strategy_summary,
        # 无 CUDA 数值后端
        "compute": {
            "requested": None, "device": None, "available": False,
            "backend": None, "name": None, "reason": "本平台未启用 CUDA 数值后端",
        },
        # 回填由 01 的 CLI/工具执行（superplatform backfill），无常驻守护进程
        "backfill": {"running": False, "error": None},
    }
