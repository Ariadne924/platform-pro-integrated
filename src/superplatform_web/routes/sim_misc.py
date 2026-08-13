"""其余 sim 形状 API：信号（无服务，如实 503）、清洗配置（无服务，如实 503）、
文件上传（02 热插拔真实落地 imports/）、回测（策略回测映射 03 run_strategy）。

signals/cleaning：superplatform 没有 sim 的独立信号引擎与数据清洗管道
（信号由策略产出、清洗未移植），全部端点如实 503，不返回假空数据。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

import superplatform_web.state as _state
from superplatform_web import simserve

router = APIRouter(tags=["sim-misc"])

# ── 信号规则：无独立信号引擎，全部 503 ────────────────────────────────

_SIGNALS_UNAVAILABLE = "本平台无独立信号引擎（交易信号由双文件策略直接产出），信号规则 API 不可用"


@router.get("/api/signals/rules")
async def list_rules() -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_SIGNALS_UNAVAILABLE)


@router.post("/api/signals/rules")
async def create_rule() -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_SIGNALS_UNAVAILABLE)


@router.get("/api/signals/rules/{rule_id}")
async def get_rule(rule_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_SIGNALS_UNAVAILABLE)


@router.put("/api/signals/rules/{rule_id}")
async def update_rule(rule_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_SIGNALS_UNAVAILABLE)


@router.delete("/api/signals/rules/{rule_id}")
async def delete_rule(rule_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_SIGNALS_UNAVAILABLE)


@router.get("/api/signals/status")
async def signal_status() -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_SIGNALS_UNAVAILABLE)


@router.post("/api/signals/toggle")
async def toggle_signals() -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_SIGNALS_UNAVAILABLE)


# ── 数据清洗配置：无清洗管道，503 ─────────────────────────────────────

_CLEANING_UNAVAILABLE = "本平台无数据清洗管道（sim 的 cleaning 未移植），清洗配置 API 不可用"


@router.get("/api/cleaning/config")
async def get_cleaning_config() -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_CLEANING_UNAVAILABLE)


@router.post("/api/cleaning/config")
async def update_cleaning_config() -> dict[str, Any]:
    raise HTTPException(status_code=503, detail=_CLEANING_UNAVAILABLE)


# ── 文件上传：落 imports/（02 热插拔扫描目录）并触发增量重扫 ──────────

_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-.]{1,120}$")

_KIND_CONFIG: dict[str, dict[str, Any]] = {
    "factor": {
        "ext": ".md",
        "dir": simserve.PROJECT_ROOT / "imports" / "factors",
        "label": "因子文档",
    },
    "strategy_md": {
        "ext": ".md",
        "dir": simserve.PROJECT_ROOT / "imports" / "strategies",
        "label": "双文件制策略文档",
    },
    # strategy_py（纯 Python 策略）本平台无此通道，上传时明确拒绝
}


def _trigger_reload(kind: str) -> None:
    try:
        if kind == "factor":
            simserve.factor_registry().check_and_reload()
        elif kind == "strategy_md":
            simserve.strategy_registry().check_and_reload()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("上传后热重载失败: %s", exc)


def _validation_readback(kind: str, md_target: Path) -> dict[str, Any]:
    """热重载后从注册表读回该 MD 的校验结果（02 协议校验的真实结论）。"""
    reg = simserve.factor_registry() if kind == "factor" else simserve.strategy_registry()
    rows = reg.list_factors() if kind == "factor" else reg.list_strategies()
    wanted = str(md_target.resolve())
    for row in rows:
        row_md = row.get("md_path")
        if not row_md:
            continue
        try:
            matched = str(Path(str(row_md)).resolve()) == wanted
        except OSError:
            matched = str(row_md) == str(md_target)
        if matched:
            return {
                "registered": bool(row.get("registered")),
                "factor_id": row.get("factor_id") or row.get("strategy_id"),
                "status": row.get("status"),
                "validation_errors": row.get("validation_errors") or [],
            }
    return {"registered": False, "factor_id": None,
            "validation_errors": [{"message": "热重载后未在注册表找到该文件"}]}


@router.post("/api/upload/{kind}")
async def upload_file(
    kind: str,
    file: UploadFile = File(...),
    impl: Optional[UploadFile] = File(None),
) -> dict[str, Any]:
    """上传因子/策略文档到 imports/（02 热插拔目录），返回协议校验结果。

    kind: factor（可成对传 impl=.py）/ strategy_md；strategy_py 无通道，拒绝。
    """
    if kind == "strategy_py":
        raise HTTPException(status_code=400, detail="本平台无纯 Python 策略通道，请上传双文件制策略（strategy_md）")
    if kind not in _KIND_CONFIG:
        raise HTTPException(status_code=400, detail=f"无效上传类型: {kind}，支持: factor / strategy_md")

    cfg = _KIND_CONFIG[kind]
    expected_ext = cfg["ext"]
    label = cfg["label"]

    filename = file.filename or ""
    if not filename or not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="文件名非法（仅允许字母、数字、下划线、短横线、点，长度 1-120）")
    if not filename.lower().endswith(expected_ext):
        raise HTTPException(status_code=400, detail=f"{label} 仅支持 {expected_ext} 文件，收到: {filename}")

    content = await file.read()
    if len(content) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件过大，上限 1MB")

    impl_safe_name: Optional[str] = None
    impl_content: Optional[bytes] = None
    if impl is not None:
        impl_filename = impl.filename or ""
        if not impl_filename or not _SAFE_FILENAME_RE.match(impl_filename):
            raise HTTPException(status_code=400, detail="实现文件名非法（仅允许字母、数字、下划线、短横线、点，长度 1-120）")
        if not impl_filename.lower().endswith(".py"):
            raise HTTPException(status_code=400, detail=f"实现文件仅支持 .py，收到: {impl_filename}")
        impl_safe_name = Path(impl_filename).name
        impl_content = await impl.read()
        if len(impl_content) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="实现文件过大，上限 1MB")

    target_dir: Path = cfg["dir"]
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    target = target_dir / safe_name
    target.write_bytes(content)

    impl_path: Optional[Path] = None
    if impl_safe_name is not None and impl_content is not None:
        impl_dir = target_dir / "impl"
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_path = impl_dir / impl_safe_name
        impl_path.write_bytes(impl_content)

    _trigger_reload(kind)

    result: dict[str, Any] = {
        "ok": True,
        "kind": kind,
        "filename": safe_name,
        "path": str(target),
        "size": len(content),
        "message": f"{label} {safe_name} 上传成功，已触发热重载",
    }
    if impl_path is not None:
        result["impl_filename"] = impl_safe_name
        result["impl_path"] = str(impl_path)
    result["validation"] = _validation_readback(kind, target)
    return result


# ── 回测：策略回测映射 03 run_strategy；因子回测无对应服务，如实 501 ────


class StrategyBacktestRequest(BaseModel):
    strategy_id: str = Field(...)
    strategy_kind: str = Field(..., description="md / py")
    buy_time: str = Field(...)
    sell_time: str = Field(...)
    amount: float = Field(..., gt=0)


class FactorBacktestRequest(BaseModel):
    factor_ids: list[str] = Field(..., min_length=1)
    symbol: str = Field(...)
    buy_time: str = Field(...)
    sell_time: str = Field(...)
    amount: float = Field(..., gt=0)
    signal_method: str = Field("direction")
    zscore_window: int = Field(1440, ge=10)
    entry_threshold: float = Field(2.0, ge=0)


@router.post("/api/backtest/factor")
async def backtest_factor(req: FactorBacktestRequest) -> dict[str, Any]:
    """本平台无 sim 的单标的因子买卖回测引擎（那是 sim 的 BacktestEngine）。

    因子效果请用探索页评级/metrics（04 服务）或 CLI `superplatform evaluate`；
    不在路由里新写一套回测逻辑凑形状。
    """
    raise HTTPException(
        status_code=501,
        detail="本平台无因子买卖回测引擎：因子效果请用探索页「评级/指标」（04 服务）"
               "或 CLI `superplatform evaluate --factor <id>`；策略回测用 /api/backtest/strategy",
    )


@router.post("/api/backtest/strategy")
async def backtest_strategy(req: StrategyBacktestRequest) -> dict[str, Any]:
    """策略回测 → 03 的 OfflineRuntime.run_strategy（向量化真实回测）。

    字段映射说明：amount 只用于把归一化净值换算成 USDT 口径展示；
    窗口 buy_time/sell_time 经 dual_factor_defaults 传给 03 运行时。
    本平台回测是权重调仓模型，无逐笔成交价——positions/transactions
    明细如实返回空数组（前端据此隐藏明细表），汇总数字全部真实。
    """
    if req.strategy_kind != "md":
        raise HTTPException(status_code=400, detail="本平台无双文件制外的策略通道（strategy_kind 仅支持 md）")

    from superplatform.consumption.base import ConsumerConfig
    from superplatform.runtime.pipeline import OfflineRuntime

    start = req.buy_time.strip().replace(" ", "T")
    end = req.sell_time.strip().replace(" ", "T")
    runtime = OfflineRuntime(
        _state.config,
        _state.providers,
        dual_factor_defaults={"start": start, "end": end},
    )
    try:
        result = await runtime.run_strategy(
            req.strategy_id, output_dir="reports", consumer=ConsumerConfig.backtest()
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {req.strategy_id}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"回测异常: {type(exc).__name__}: {exc}")

    bt = result["backtest"]
    equity = bt.equity["equity"]
    final_mult = float(equity.iloc[-1]) if len(equity) else 1.0
    total_pnl = req.amount * (final_mult - 1.0)
    try:
        hold_minutes = _parse_minutes(req.buy_time, req.sell_time)
    except Exception:
        hold_minutes = 0
    max_float = float(equity.max()) if len(equity) else 1.0
    min_float = float(equity.min()) if len(equity) else 1.0

    payload = {
        "name": bt.strategy_name,
        "kind": "strategy",
        "buy_time": req.buy_time,
        "sell_time": req.sell_time,
        "amount": req.amount,
        "direction": "权重调仓（多/空/平）",
        "positions": [],
        "transactions": [],
        "total_pnl": total_pnl,
        "total_return_pct": bt.total_return * 100.0,
        "final_equity": req.amount * final_mult,
        "hold_minutes": hold_minutes,
        "max_floating_profit": req.amount * (max_float - 1.0),
        "max_floating_loss": req.amount * (min_float - 1.0),
        "annualized_return": bt.annual_return * 100.0,
        "win_rate": bt.win_rate,
        "factor_stats": None,
        "strategy_signals": None,
        "error": None,
    }
    txt = _build_txt(req, bt, payload)
    return {
        "ok": True,
        "error": None,
        "result": payload,
        "txt_filename": f"backtest_{req.strategy_id}.txt",
        "txt_content": txt,
    }


def _parse_minutes(buy_time: str, sell_time: str) -> int:
    from datetime import datetime

    def _p(s: str) -> datetime:
        s = s.strip().replace(" ", "T")
        return datetime.fromisoformat(s)

    return int((_p(sell_time) - _p(buy_time)).total_seconds() // 60)


def _build_txt(req: StrategyBacktestRequest, bt, payload: dict[str, Any]) -> str:
    """把 03 的 BacktestResult 排版成 txt 报告（纯格式映射，不重算）。"""
    lines = [
        "========== 策略回测报告 ==========",
        f"策略: {bt.strategy_name}",
        f"区间: {req.buy_time} → {req.sell_time} (UTC)",
        f"初始资金: {req.amount:.2f} USDT",
        "",
        f"总收益率: {bt.total_return:.2%}",
        f"年化收益: {bt.annual_return:.2%}",
        f"年化波动: {bt.annual_vol:.2%}",
        f"夏普: {bt.sharpe:.2f}",
        f"最大回撤: {bt.max_drawdown:.2%}",
        f"胜率(按 bar): {bt.win_rate:.2%}",
        f"bar 均收益: {bt.avg_return:.4%}",
        f"期末资金: {payload['final_equity']:.2f} USDT",
    ]
    if bt.liquidation:
        lines.append(f"爆仓: {bt.liquidation}")
    return "\n".join(lines) + "\n"
