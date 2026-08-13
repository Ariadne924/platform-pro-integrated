"""探索页聚合 API（sim_platform 形状）：/api/admin/overview + MD/impl 预览 + 页面重定向。

因子/策略清单来自 02 的双文件注册中心；评级/指标在 sim_rating.py（04 适配层）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from superplatform_web import simserve
from superplatform_web.routes.sim_registry import factor_rows, strategy_rows

router = APIRouter(tags=["sim-admin"])


# ── 页面重定向（StaticFiles html=True 只自动解析 index.html）────────────


@router.get("/explorer", include_in_schema=False)
async def explorer_redirect():
    return RedirectResponse(url="/explorer.html", status_code=307)


@router.get("/admin", include_in_schema=False)
async def admin_redirect():
    """兼容旧路径：/admin → /explorer.html。"""
    return RedirectResponse(url="/explorer.html", status_code=307)


# ── /api/admin/overview ─────────────────────────────────────────────


@router.get("/api/admin/overview")
async def overview() -> dict[str, Any]:
    factors = factor_rows()
    strategies = strategy_rows()
    return {
        "ready": {"factors": True, "strategies": True, "pystrategies": False},
        "counts": {
            "factors": len(factors),
            "strategies": len(strategies),
            "pystrategies": 0,
        },
        "factors": factors,
        "strategies": strategies,
        "pystrategies": [],
    }


# ── MD / impl 全文预览 ──────────────────────────────────────────────


def _read_text(path: Any) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


@router.get("/api/admin/factors/{factor_id}/md")
async def get_factor_md(factor_id: str) -> dict[str, Any]:
    reg = simserve.factor_registry()
    rec = reg.get_record(factor_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    return {
        "factor_id": factor_id,
        "name": rec.name,
        "md_path": str(rec.md_path),
        "content": _read_text(rec.md_path),
    }


@router.get("/api/admin/factors/{factor_id}/impl")
async def get_factor_impl(factor_id: str) -> dict[str, Any]:
    reg = simserve.factor_registry()
    rec = reg.get_record(factor_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    return {
        "factor_id": factor_id,
        "name": rec.name,
        "impl_path": str(rec.impl_path),
        "content": _read_text(rec.impl_path),
    }


@router.get("/api/admin/strategies/{strategy_id}/md")
async def get_strategy_md(strategy_id: str) -> dict[str, Any]:
    reg = simserve.strategy_registry()
    rec = reg.get_record(strategy_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")
    return {
        "strategy_id": strategy_id,
        "name": rec.name,
        "md_path": str(rec.md_path),
        "content": _read_text(rec.md_path),
    }


@router.get("/api/admin/strategies/{strategy_id}/impl")
async def get_strategy_impl(strategy_id: str) -> dict[str, Any]:
    reg = simserve.strategy_registry()
    rec = reg.get_record(strategy_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")
    return {
        "strategy_id": strategy_id,
        "name": rec.name,
        "impl_path": str(rec.impl_path),
        "content": _read_text(rec.impl_path),
    }


@router.get("/api/admin/pystrategies/{strategy_id}/source")
async def get_pystrategy_source(strategy_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=404, detail=f"本平台无纯 Python 策略通道: {strategy_id}")
