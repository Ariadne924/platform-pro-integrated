"""因子评级 / 评级榜 / 评估指标 / 合格判定 / 相关性矩阵（sim 形状 → 04 服务）。

唯一数据来源是 04 的服务层（RatingService / FactorMetricsService，见
simserve.services04）；本模块只做形状映射，不重算任何指标。
04 服务未就位时如实 503（前端显示「加载失败」横幅，不展示假数据）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Path, Query, Response

from superplatform_web import simserve

router = APIRouter(tags=["sim-rating"])


async def _to_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)


# ── 评级 ────────────────────────────────────────────────────────────


def _shape_rating(factor_id: str, raw: Any, days: float) -> dict[str, Any]:
    """04 rate_factor payload → sim 评级 payload。

    04 的 payload["status"] 是评级状态（ok/insufficient/not_supported），
    sim 前端的 res.status 徽章要因子注册状态（active/draft）——这里恢复；
    评级状态经 aggregate.insufficient/not_supported 如实传递。
    """
    rec = simserve.factor_registry().get_record(factor_id)
    if not isinstance(raw, dict):
        raw = {}
    agg = raw.get("aggregate") if isinstance(raw.get("aggregate"), dict) else raw
    rating_status = raw.get("status")
    if rating_status == "insufficient":
        agg.setdefault("insufficient", True)
    elif rating_status == "not_supported":
        agg.setdefault("not_supported", True)
    return {
        "ok": True,
        "factor_id": factor_id,
        "name": raw.get("name") or (rec.name if rec else None),
        "status": raw.get("factor_status") or (rec.status if rec else None),
        "category": raw.get("category") or (rec.category if rec else None),
        "frequency": raw.get("frequency") or (rec.frequency if rec else None),
        "cross_sectional": bool(raw.get("cross_sectional", False)),
        "direction_text": raw.get("direction_text"),
        "bullish_high": raw.get("bullish_high", True),
        "last_error": raw.get("last_error"),
        "validation_errors": [],
        "eval_window": raw.get("eval_window") or {"days": days, "horizon": raw.get("horizon")},
        "notes": raw.get("notes") or [],
        "aggregate": agg,
        "per_symbol": raw.get("per_symbol") or [],
    }


def _rating_payload(factor_id: str, days: float, horizon: int, refresh: bool) -> Optional[dict[str, Any]]:
    rating, _, _ = simserve.services04()
    raw = simserve.call04(
        rating.rate_factor, factor_id, days=days, horizon=horizon, refresh=refresh
    )
    if raw is None:
        return None
    return _shape_rating(factor_id, raw, days)


@router.get("/api/admin/factors/{factor_id}/rating")
async def get_factor_rating(
    factor_id: str = Path(...),
    days: float = Query(30, ge=0.02, le=365),
    horizon: int = Query(60, ge=5, le=1440),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """因子评级（S~D）：04 RatingService 的真实输出；样本不足/不支持如实标记。"""
    if simserve.factor_registry().get_record(factor_id) is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    try:
        payload = await _to_thread(_rating_payload, factor_id, days, horizon, refresh)
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if payload is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    return payload


# ── 评级榜 ──────────────────────────────────────────────────────────

#: 04 leaderboard entry → sim 前端契约的字段映射
_LEADERBOARD_EXTRA_KEYS = (
    "ic_mean", "ic_positive_ratio", "annualized_return_pct", "calmar",
    "trades", "turnover", "avg_hold_bars", "coverage", "n_symbols",
)


def _leaderboard_entry(e04: dict[str, Any]) -> dict[str, Any]:
    rating_status = e04.get("rating_status")
    computed = rating_status not in (None, "not_evaluated")
    entry: dict[str, Any] = {
        "factor_id": e04.get("factor_id"),
        "name": e04.get("name"),
        "category": e04.get("category"),
        "status": e04.get("factor_status"),
        "cross_sectional": bool(e04.get("cross_sectional")),
        "direction_text": e04.get("direction_text"),
        "grade": e04.get("grade"),
        "computed": computed,
        "not_supported": rating_status == "not_supported",
        "insufficient": rating_status == "insufficient",
        "error": e04.get("error"),
        "rank_ic_mean": e04.get("rank_ic_mean"),
        "icir": e04.get("icir"),
        "sharpe": e04.get("sharpe"),
        "total_return_pct": e04.get("total_return_pct"),
        "max_drawdown_pct": e04.get("max_drawdown_pct"),
        "win_rate": e04.get("win_rate"),
    }
    for key in _LEADERBOARD_EXTRA_KEYS:
        entry[key] = e04.get(key)
    return entry


def _leaderboard(ids: Optional[list[str]], days: float, horizon: int) -> dict[str, Any]:
    rating, _, _ = simserve.services04()
    raw = simserve.call04(
        rating.leaderboard, ids=ids or None, days=days, horizon=horizon,
        refresh=False, compute_limit=20,
    )
    entries = [_leaderboard_entry(e) for e in raw.get("entries") or []]
    return {
        "ok": True,
        "days": (raw.get("eval_window") or {}).get("days", days),
        "horizon": (raw.get("eval_window") or {}).get("horizon", horizon),
        "entries": entries,
        "computed_count": sum(1 for e in entries if e["computed"]),
        "total_count": len(entries),
        "summary04": raw.get("summary"),
        "note": raw.get("note"),
    }


@router.get("/api/admin/factors/ratings/leaderboard")
async def get_ratings_leaderboard(
    ids: str = Query("", description="逗号分隔因子 ID 子集（≤20 同步计算）；空=只读缓存"),
    days: float = Query(30, ge=0.02, le=365),
    horizon: int = Query(60, ge=5, le=1440),
) -> dict[str, Any]:
    id_list = [s.strip() for s in ids.split(",") if s.strip()]
    if len(id_list) > 20:
        raise HTTPException(status_code=400, detail=f"ids 数量 {len(id_list)} 超过上限 20，请分批请求")
    try:
        return await _to_thread(_leaderboard, id_list, days, horizon)
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── 评估指标（metrics）───────────────────────────────────────────────


def _metrics_payload(factor_id: str, refresh: bool) -> Optional[dict[str, Any]]:
    _, metrics, _ = simserve.services04()
    return simserve.call04(metrics.factor_metrics, factor_id, force=refresh)


@router.get("/api/admin/bias-control/factors/{factor_id}/metrics")
async def bias_control_factor_metrics(
    factor_id: str = Path(...),
    format: str = Query("json"),
    refresh: bool = Query(False),
):
    """单因子评估指标（IC/RankIC/ICIR/衰减/分层/滚动/换手/合格判定）——04 服务。"""
    if simserve.factor_registry().get_record(factor_id) is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    try:
        payload = await _to_thread(_metrics_payload, factor_id, refresh)
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if payload is None:
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    if format == "csv":
        _, metrics, _ = simserve.services04()
        return Response(
            content=metrics.metrics_csv(payload),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="factor_metrics_{factor_id}.csv"'},
        )
    return payload


# ── 合格判定 / 相关性矩阵 ────────────────────────────────────────────


def _qualification_payload(refresh: bool) -> dict[str, Any]:
    _, metrics, _ = simserve.services04()
    return simserve.call04(metrics.qualification_summary, refresh=refresh, refresh_limit=20)


@router.get("/api/admin/bias-control/qualification")
async def bias_control_qualification(
    format: str = Query("json"),
    refresh: bool = Query(False),
):
    """全库合格判定汇总（六项阈值，只读缓存；refresh=1 限量补算）——04 服务。"""
    try:
        payload = await _to_thread(_qualification_payload, refresh)
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if format == "csv":
        _, metrics, _ = simserve.services04()
        return Response(
            content=metrics.qualification_csv(payload),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="factor_qualification.csv"'},
        )
    return payload


def _correlation_payload() -> dict[str, Any]:
    """相关性矩阵：默认因子集 = 有指标缓存（已评估）的因子。

    sim 的口径是「factor_value 落库的全部因子」；本平台无落库因子值，
    对应物是 eval_metrics_cache 里的已评估因子。全库（99 个，多为 decorator
    示例因子）逐因子重算 + 网格化首算要数十分钟，不适合交互端点；
    未评估因子进 excluded 并如实标注原因。
    """
    _, metrics, _ = simserve.services04()
    try:
        cached = simserve.call04(metrics.eval_store.all_cached, "eval_metrics_cache")
        ids = sorted({str(r["factor_id"]) for r in cached})
    except Exception:
        ids = []
    if not ids:
        # 一个都没评估过时，如实返回空矩阵（前端显示空态，不算错误）
        return {
            "factor_ids": [], "matrix": [], "sample_counts": [],
            "excluded": [], "computed_at": None, "cache_hit": False,
            "note": "尚无已评估因子（eval_metrics_cache 为空），矩阵为空；"
                    "先在探索页计算因子指标后再查看",
        }
    payload = simserve.call04(metrics.correlation_matrix, ids)
    evaluated = set(ids)
    roster = simserve.factor_registry().list_factors()
    for row in roster:
        fid = row.get("factor_id")
        if fid and fid not in evaluated:
            payload.setdefault("excluded", []).append(
                {"factor_id": fid, "reason": "未评估（无指标缓存），未纳入矩阵"}
            )
    payload["note"] = "矩阵因子集 = 已评估（有指标缓存）的因子；未评估因子列在 excluded"
    return payload


@router.get("/api/admin/bias-control/correlation-matrix")
async def bias_control_correlation_matrix(format: str = Query("json")):
    """库级因子相关性矩阵（日频 Spearman，04 服务；因子数封顶见其配置）。"""
    try:
        payload = await _to_thread(_correlation_payload)
    except simserve.Service04Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if format == "csv":
        _, metrics, _ = simserve.services04()
        return Response(
            content=metrics.correlation_csv(payload),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="factor_correlation_matrix.csv"'},
        )
    return payload
