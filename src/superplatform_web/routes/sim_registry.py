"""因子/策略注册中心 API（sim_platform 形状，02 双文件热插拔的 HTTP 映射）。

路径说明（web/API_DIFF.md A 类）：sim 源页的 `GET /api/factors`、`GET /api/strategies`、
`DELETE /api/factors/{id}` 与 exchangia 既有冻结路由撞名，前端已改到
`/api/registry/*`；响应形状与 sim 一致。其余路径（/api/factors/{id}/series、
/api/factors/export、/api/strategies/reload、/api/pystrategies*）无冲突，原样保留。
"""

from __future__ import annotations

import io
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Path as FsPath, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from superplatform_web import simserve

router = APIRouter(tags=["sim-registry"])

_PROJECT_ROOT = simserve.PROJECT_ROOT


# ── 行形状组装（sim list_factors/list_strategies 同构）────────────────


def factor_rows() -> list[dict[str, Any]]:
    """02 双文件因子清单（ensure_scanned 已在 accessor 内做=热插拔）。"""
    reg = simserve.factor_registry()
    rows = []
    for row in reg.list_factors():
        row = dict(row)
        fid = row.get("factor_id")
        rec = reg.get_record(fid) if fid else None
        # sim 形状里有而双文件注册中心没有的字段：如实补空值
        row.setdefault("last_computed", None)
        row.setdefault("latest_values", {})
        row.setdefault("last_error", None)
        row["params"] = dict(rec.params) if rec else None
        row["inputs"] = list(rec.inputs) if rec else None
        row["cross_sectional"] = False  # 02 协议暂未引入横截面标记位
        rows.append(row)
    return rows


def strategy_rows() -> list[dict[str, Any]]:
    reg = simserve.strategy_registry()
    rows = []
    for row in reg.list_strategies():
        row = dict(row)
        sid = row.get("strategy_id")
        rec = reg.get_record(sid) if sid else None
        row.setdefault("last_run", None)
        row.setdefault("last_orders", None)
        row.setdefault("last_rejected", None)
        row.setdefault("last_error", None)
        row["params"] = dict(rec.params) if rec else None
        row["risk_limits"] = dict(rec.risk_limits) if rec else None
        rows.append(row)
    return rows


# ── /api/registry/*（避让 exchangia 冻结路由的新路径）─────────────────


@router.get("/api/registry/factors")
async def list_factors() -> dict[str, Any]:
    """全部双文件因子注册状态（含 invalid/conflict 行）。"""
    items = factor_rows()
    return {"factors": items, "count": len(items)}


@router.get("/api/registry/strategies")
async def list_strategies() -> dict[str, Any]:
    items = strategy_rows()
    return {"strategies": items, "count": len(items)}


@router.delete("/api/registry/factors/{factor_id}")
async def delete_factor(factor_id: str = FsPath(..., description="因子 ID，如 MOM-001")) -> dict[str, Any]:
    """下架因子（热拔）：MD+impl 移入 imports/factor_trash/（可恢复），注册表即时离册。

    仅允许下架 source=imports 的因子：builtin 的 factors/ 是源码树（02 地界），
    UI 删除内置因子会改源码目录，如实拒绝。
    """
    reg = simserve.factor_registry()
    rec = reg.get_record(factor_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    if rec.source != "imports":
        raise HTTPException(
            status_code=403,
            detail=f"内置因子（factors/ 源码树）不从 UI 下架: {factor_id}；"
                   "如需下架请移动文件或联系维护者",
        )

    trash_dir = _PROJECT_ROOT / "imports" / "factor_trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved_to: dict[str, str] = {}
    notes: list[str] = []
    for label, raw in (("md", rec.md_path), ("impl", rec.impl_path)):
        src = Path(raw) if raw else None
        if src is None or not src.exists():
            notes.append(f"{label} 文件缺失，已跳过移动: {src}")
            continue
        target = trash_dir / f"{src.stem}__{stamp}{src.suffix}"
        shutil.move(str(src), str(target))
        moved_to[label] = str(target)

    reg.check_and_reload()  # mtime 快照发现 .md 消失后增量注销
    if reg.get_record(factor_id) is not None:
        raise HTTPException(status_code=500, detail=f"因子离册失败: {factor_id}")
    return {"deleted": True, "factor_id": factor_id, "moved_to": moved_to, "notes": notes}


# ── sim 原路径（无冲突部分）──────────────────────────────────────────


@router.post("/api/strategies/reload")
async def reload_strategies() -> dict[str, Any]:
    """强制全量重新扫描 strategies/ 与 imports/strategies/。"""
    summary = simserve.strategy_registry().force_reload()
    return {"ok": True, "summary": summary}


@router.get("/api/factors/{factor_id}/series")
async def factor_series(
    factor_id: str = FsPath(...),
    symbol: str = Query(...),
    limit: int = Query(300, ge=1, le=5000),
) -> dict[str, Any]:
    """因子在指定标的上的最近时序（调 02 注册的 compute 真实计算）。

    本平台不做常驻因子值落库；按需计算，数据取因子声明频率的缓存 K 线。
    """
    core = simserve.core_symbol(symbol)
    rows = simserve.compute_factor_series(factor_id, core, limit)
    return {"factor_id": factor_id, "symbol": symbol, "limit": limit,
            "count": len(rows), "data": rows}


class FactorExportRequest(BaseModel):
    ids: list[str] = []


@router.post("/api/factors/export")
async def export_factors(req: FactorExportRequest) -> StreamingResponse:
    """导出因子注册信息为 Excel（列与 sim 一致；latest_* 列本平台无常驻值，留空）。"""
    from openpyxl import Workbook

    items = factor_rows()
    if req.ids:
        wanted = set(req.ids)
        items = [f for f in items if f.get("factor_id") in wanted]

    headers = [
        "factor_id", "name", "category", "status", "version", "frequency",
        "lookback_bars", "author", "last_computed", "source",
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "factors"
    ws.append(headers)
    for f in items:
        ws.append([f.get(k) for k in headers])
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = "factors_export_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── 纯 Python 策略通道：本平台没有，如实空/404 ─────────────────────────


@router.get("/api/pystrategies")
async def list_py_strategies() -> dict[str, Any]:
    """本平台只有双文件（MD+impl）策略通道，纯 Python 策略通道不存在——如实空列表。"""
    return {"strategies": [], "count": 0}


@router.get("/api/pystrategies/{strategy_id}")
async def get_py_strategy(strategy_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=404, detail=f"本平台无纯 Python 策略通道: {strategy_id}")


@router.get("/api/pystrategies/{strategy_id}/source")
async def get_py_strategy_source(strategy_id: str) -> dict[str, str]:
    raise HTTPException(status_code=404, detail=f"本平台无纯 Python 策略通道: {strategy_id}")


@router.get("/api/pystrategies/{strategy_id}/description")
async def get_py_strategy_description(strategy_id: str) -> dict[str, str]:
    raise HTTPException(status_code=404, detail=f"本平台无纯 Python 策略通道: {strategy_id}")


@router.post("/api/pystrategies/reload")
async def reload_py_strategies() -> dict[str, Any]:
    return {"ok": True, "summary": {"note": "本平台无纯 Python 策略通道，无需重载"}}
