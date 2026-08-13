"""因子库偏差控制 API（sim_platform 形状）。

六查结果来自 04 的 BiasControlService（simserve 的 run manager 串行执行、
逐因子落库 eval_bias_* + 本层 data/web_bias_results.json 留档）；检查状态登记/
人工解封是本层的真实簿记（data/web_factor_check.json），不是指标。
04 未就位时启动批次如实 503；从未跑过检查的因子如实 NOT_CHECKED，绝不标假 PASS。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Path as FsPath, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from superplatform_web import simserve

router = APIRouter(tags=["sim-bias"])

_CHECK_KEYS = ("lookahead", "full_sample", "multiple_testing", "overfit", "cost")


# ── 行/概览组装 ─────────────────────────────────────────────────────


def _factor_row(fid: Optional[str], meta: dict[str, Any]) -> dict[str, Any]:
    stored = simserve.run_manager().stored_result(fid) if fid else None
    checks = (stored or {}).get("checks") or {}
    oos = (stored or {}).get("out_of_sample") or {}
    row: dict[str, Any] = {
        "factor_id": fid,
        "library": meta.get("source"),
        "source": meta.get("source"),
        "category": meta.get("category"),
        "overall_status": (stored or {}).get("overall_status") or "NOT_CHECKED",
        "checks": checks,
        "out_of_sample": oos,
        "oos": oos,
        "run_id": (stored or {}).get("run_id"),
        "checked_at": (stored or {}).get("checked_at"),
        "failure_reason": (stored or {}).get("failure_reason"),
    }
    return row


def _all_rows() -> list[dict[str, Any]]:
    reg = simserve.factor_registry()
    rows = []
    for item in reg.list_factors():
        rows.append(_factor_row(item.get("factor_id"), item))
    return rows


def _windows() -> tuple[Any, Any]:
    """config bias_control 段的开发集/样本外窗口（04 定义；缺省如实 None）。"""
    import superplatform_web.state as _state

    dev = _state.config.get("bias_control.development_window")
    oos = _state.config.get("bias_control.out_of_sample_window")
    return dev, oos


@router.get("/api/admin/bias-control/overview")
async def bias_control_overview() -> dict[str, Any]:
    rows = _all_rows()
    state = simserve.check_state()
    reg = simserve.factor_registry()
    records = {r["factor_id"]: reg.get_record(r["factor_id"])
               for r in reg.list_factors() if r.get("factor_id")}
    statuses = state.list_statuses(records)

    pass_n = sum(1 for r in rows if r["overall_status"] == "PASS")
    fail_n = sum(1 for r in rows if r["overall_status"] in ("FAIL", "ERROR"))
    checked_n = sum(1 for s in statuses.values() if s.get("status") == "checked")
    latest = simserve.run_manager().latest()
    latest_dict = latest.to_dict() if latest else None
    if latest_dict is None:
        latest_dict = _latest_run_04()
    dev, oos = _windows()
    return {
        "available": True,
        "summary": {
            "total_factors": len(rows),
            "checked_count": checked_n,
            "pass_count": pass_n,
            "fail_count": fail_n,
            "not_checked_count": len(rows) - pass_n - fail_n,
            "last_run_at": (latest.finished_at if latest else None)
            or (latest_dict or {}).get("finished_at"),
            "development_window": dev,
            "oos_window": oos,
        },
        "factors": rows,
        "latest_run": latest_dict,
    }


def _latest_run_04() -> Optional[dict[str, Any]]:
    """04 落库的最近批次（eval_bias_runs）——UI 批次与 04 共享 run_id，
    进程重启后概览仍能展示最近一次运行。"""
    try:
        _, _, bias = simserve.services04()
        run_id = simserve.call04(bias.eval_store.latest_run_id)
        if not run_id:
            return None
        data = simserve.call04(bias.report_data, run_id)
    except Exception:
        return None
    if not data:
        return None
    run = data.get("run") or {}
    factors = data.get("factors") or []
    status_map = {"finished": "PASS", "running": "RUNNING"}
    any_fail = any(
        str(f.get("overall_status", "")).upper() in ("FAIL", "ERROR") for f in factors
    )
    status = status_map.get(str(run.get("status")), str(run.get("status") or "UNKNOWN"))
    if status == "PASS" and any_fail:
        status = "FAIL"
    return {
        "run_id": run.get("run_id") or run_id,
        "scope": run.get("scope"),
        "status": status,
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "total": len(factors),
        "progress": {
            "completed": len(factors),
            "total": len(factors),
            "passed": sum(1 for f in factors if str(f.get("overall_status", "")).upper() == "PASS"),
            "failed": sum(1 for f in factors if str(f.get("overall_status", "")).upper() in ("FAIL", "ERROR")),
            "not_checked": 0,
            "current_factor": None,
            "latest_error": None,
        },
    }


@router.get("/api/admin/bias-control/factors")
async def bias_control_factors() -> dict[str, Any]:
    return {"available": True, "factors": _all_rows()}


@router.get("/api/admin/bias-control/factors/{factor_id}")
async def bias_control_factor_detail(factor_id: str = FsPath(...)) -> dict[str, Any]:
    """单因子六查详情（最近一次 bias-check 运行的留档结果）。"""
    reg = simserve.factor_registry()
    if reg.get_record(factor_id) is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    stored = simserve.run_manager().stored_result(factor_id)
    if stored is None:
        # 前端对 404 有设计的降级行为（toast 提示 + 回落列表行），如实 404
        raise HTTPException(status_code=404, detail=f"因子尚未运行偏差检查: {factor_id}")
    detail = dict(stored.get("raw") or {})
    detail.update({
        "factor_id": factor_id,
        "overall_status": stored.get("overall_status"),
        "checks": stored.get("checks") or {},
        "out_of_sample": stored.get("out_of_sample") or {},
        "oos": stored.get("out_of_sample") or {},
        "failure_reason": stored.get("failure_reason"),
        "checked_at": stored.get("checked_at"),
        "scope": stored.get("scope"),
    })
    # 前端详情抽屉按顶层键读各检查块（checks 容器之外的别名兜底）
    for key in _CHECK_KEYS:
        block = (stored.get("checks") or {}).get(key)
        if block is not None:
            detail.setdefault(key, block)
    return detail


# ── 批次（run）───────────────────────────────────────────────────────


class LookaheadCheckRequest(BaseModel):
    scope: Literal["development", "development_failed", "locked_oos", "all"] = "development"
    factor_ids: list[str] = Field(default_factory=list)


@router.post("/api/admin/bias-control/lookahead-check", status_code=202)
async def start_bias_control_check(payload: LookaheadCheckRequest) -> dict[str, Any]:
    """异步启动偏差检查批次（后台线程串行调 04 bias-check CLI）。"""
    reg = simserve.factor_registry()
    factor_ids = list(payload.factor_ids)
    if not factor_ids:
        rows = reg.list_factors()
        if payload.scope == "development":
            factor_ids = [r["factor_id"] for r in rows
                          if r.get("factor_id") and r.get("status") == "active"]
        else:
            factor_ids = [r["factor_id"] for r in rows if r.get("factor_id")]
    if not factor_ids:
        raise HTTPException(status_code=400, detail="没有可检查的因子")
    try:
        run = simserve.run_manager().start(payload.scope, factor_ids)
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"run": run.to_dict()}


@router.get("/api/admin/bias-control/runs/{run_id}")
async def bias_control_run_status(run_id: str = FsPath(...)) -> dict[str, Any]:
    run = simserve.run_manager().get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行批次不存在: {run_id}")
    return run.to_dict()


@router.post("/api/admin/bias-control/runs/{run_id}/cancel", status_code=202)
async def cancel_bias_control_run(run_id: str = FsPath(...)) -> dict[str, Any]:
    try:
        run = simserve.run_manager().cancel(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"运行批次不存在: {run_id}")
    return {"run": run.to_dict()}


@router.get("/api/admin/bias-control/runs/{run_id}/report")
async def bias_control_run_report(
    run_id: str = FsPath(...),
    format: Literal["json", "csv", "md"] = Query("json"),
) -> Response:
    """导出批次检查报告（json/csv/md）。

    UI 发起的批次与 04 的 run_id 共享并逐因子落库（eval_bias_runs/results），
    报告由 04 BiasControlService.report 生成——进程重启后仍可导出。
    """
    try:
        _, _, bias = simserve.services04()
        content, filename = simserve.call04(bias.report, run_id, format)
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"运行批次不存在或无结果: {run_id} ({exc})")
    media = {
        "json": "application/json; charset=utf-8",
        "csv": "text/csv; charset=utf-8",
        "md": "text/markdown; charset=utf-8",
    }[format]
    return Response(
        content=content, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── 检查状态登记（attest/uncheck/状态查询）────────────────────────────


@router.get("/api/factors/check-status")
async def all_check_status() -> dict[str, Any]:
    reg = simserve.factor_registry()
    records = {r["factor_id"]: reg.get_record(r["factor_id"])
               for r in reg.list_factors() if r.get("factor_id")}
    statuses = simserve.check_state().list_statuses(records)
    return {"statuses": statuses, "count": len(statuses)}


@router.get("/api/factors/check-overrides")
async def all_check_overrides() -> dict[str, Any]:
    return {"overrides": simserve.check_state().list_overrides()}


@router.get("/api/factors/{factor_id}/check-status")
async def factor_check_status(factor_id: str = FsPath(...)) -> dict[str, Any]:
    rec = simserve.factor_registry().get_record(factor_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    return simserve.check_state().get_status(factor_id, rec)


class AttestRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


@router.post("/api/factors/{factor_id}/attest")
async def attest_factor(payload: AttestRequest, factor_id: str = FsPath(...)) -> dict[str, Any]:
    rec = simserve.factor_registry().get_record(factor_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    status = simserve.check_state().attest(factor_id, payload.name, rec)
    return {"ok": True, "status": status}


@router.post("/api/factors/{factor_id}/uncheck")
async def uncheck_factor(factor_id: str = FsPath(...)) -> dict[str, Any]:
    rec = simserve.factor_registry().get_record(factor_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    status = simserve.check_state().uncheck(factor_id, rec)
    return {"ok": True, "status": status}


@router.post("/api/factors/{factor_id}/run-check", status_code=202)
async def run_factor_check(factor_id: str = FsPath(...)) -> dict[str, Any]:
    """后台自动偏差检查（开发集六查，04 bias-check CLI 串行执行）。"""
    if simserve.factor_registry().get_record(factor_id) is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    try:
        run = simserve.run_manager().start("development", [factor_id])
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "ok": True,
        "run": run.to_dict(),
        "message": "自动偏差检查已在后台启动（04 bias-check，六查），完成后自动标记「已检查·自动」；"
                   "可稍后刷新检查状态查看。",
    }


# ── 人工解封 ────────────────────────────────────────────────────────


class OverrideRequest(BaseModel):
    check_name: str = Field(..., description="overall/lookahead/full_sample/multiple_testing/overfit/cost/out_of_sample/qualification")
    operator: str = Field(..., min_length=1, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=200)


class OverrideRevokeRequest(BaseModel):
    check_name: str


def _original_status(factor_id: str, check_name: str) -> str:
    """该检查项当前的真实状态（解封前提：FAIL/BLOCKED/ERROR）。"""
    stored = simserve.run_manager().stored_result(factor_id)
    if stored is None:
        raise HTTPException(status_code=400, detail=f"因子无检查结果，无可解封项: {factor_id}")
    if check_name == "overall":
        status = stored.get("overall_status")
    elif check_name in ("out_of_sample", "oos"):
        block = stored.get("out_of_sample") or {}
        status = block.get("status") if isinstance(block, dict) else block
    elif check_name == "qualification":
        # 合格判定人工通过：现状来自 04 metrics 的 qualification_state
        try:
            _, metrics, _ = simserve.services04()
            state = simserve.call04(metrics.qualification_state, factor_id)
        except simserve.Service04Unavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if state is None:
            raise HTTPException(status_code=400, detail=f"因子未评估，无合格判定可解封: {factor_id}")
        return "FAIL" if state is False else "PASS"
    else:
        block = (stored.get("checks") or {}).get(check_name) or {}
        status = block.get("status") if isinstance(block, dict) else block
    return str(status or "NOT_CHECKED").upper()


@router.post("/api/factors/{factor_id}/override")
async def override_check(payload: OverrideRequest, factor_id: str = FsPath(...)) -> dict[str, Any]:
    if simserve.factor_registry().get_record(factor_id) is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    original = _original_status(factor_id, payload.check_name)
    if original not in ("FAIL", "BLOCKED", "ERROR"):
        raise HTTPException(
            status_code=400,
            detail=f"仅 FAIL/BLOCKED/ERROR 可人工解封，当前状态: {original}",
        )
    result = simserve.check_state().set_override(
        factor_id, payload.check_name, payload.operator, payload.reason, original
    )
    return {"ok": True, "override": result}


@router.post("/api/factors/{factor_id}/override/revoke")
async def revoke_check_override(payload: OverrideRevokeRequest, factor_id: str = FsPath(...)) -> dict[str, Any]:
    existed = simserve.check_state().revoke_override(factor_id, payload.check_name)
    if not existed:
        raise HTTPException(status_code=400, detail=f"无人工解封记录: {factor_id}/{payload.check_name}")
    return {"ok": True, "override": {"revoked": payload.check_name}}


# ── 离线自查包 ──────────────────────────────────────────────────────


@router.get("/api/factors/{factor_id}/self-check-package")
async def self_check_package(factor_id: str = FsPath(...)) -> StreamingResponse:
    """离线自查包 zip：因子 MD + impl + 自查说明（真实文件打包）。"""
    rec = simserve.factor_registry().get_record(factor_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        wrote = []
        for arc, p in ((Path(rec.md_path).name, rec.md_path),
                       (f"impl/{Path(rec.impl_path).name}", rec.impl_path)):
            try:
                zf.write(p, arc)
                wrote.append(arc)
            except OSError:
                pass
        if not wrote:
            raise HTTPException(status_code=409, detail=f"因子文件缺失，无法打包: {factor_id}")
        zf.writestr("README.txt", (
            f"自查包: {factor_id}\n\n"
            "在本仓库根目录执行：\n"
            f"  superplatform check --factor {factor_id}        # 前视检查（03）\n"
            f"  superplatform bias-check --factor {factor_id} --scope development  # 六查（04）\n"
            f"  superplatform metrics --factor {factor_id} --json                # 评估指标（04）\n"
            f"  superplatform rating --factor {factor_id} --json                 # 评级（04）\n"
        ))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="self_check_{factor_id}.zip"'},
    )
