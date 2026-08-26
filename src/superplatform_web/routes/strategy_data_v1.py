"""策略数据依赖版本化接口。

- ``GET  /api/v1/strategies/{strategy_id}/data-requirements``
  返回策略显式声明的数据依赖 + 逐依赖精确 Provider 解析结果；
- ``POST /api/v1/strategies/{strategy_id}/data/resolve``
  一次解析策略的全部数据依赖，返回带血缘元数据（source / provider_id /
  data_layer / quality_flags / time_range）的数据集合与按 group 对齐视图。

两个接口都不破坏旧版 ``/api/strategies`` 与 ``/api/market/klines``。
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException

from superplatform.strategy.data_dependencies import (
    align_dependency_groups,
    fetch_strategy_data,
    resolve_dependency_provider,
)
from superplatform.strategy.dual_registry import DualStrategyRegistry
from superplatform_web import state

router = APIRouter(prefix="/api/v1/strategies", tags=["strategy-data-v1"])


def _get_record_or_404(strategy_id: str) -> Any:
    rec = DualStrategyRegistry.get_instance().get_record(strategy_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"未知策略: {strategy_id}")
    return rec


def _parse_utc(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


def _frame_to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    data = frame.reset_index()
    # 分层管线产出的 kline 帧含 close_time 列，把索引列命名为 open_time，
    # 与 /api/v1/market/klines 的行形状保持一致
    if data.columns[0] == "timestamp" and "close_time" in data.columns:
        data = data.rename(columns={"timestamp": "open_time"})
    return json.loads(data.to_json(orient="records", date_format="iso"))


@router.get("/{strategy_id}/data-requirements")
async def strategy_data_requirements(strategy_id: str) -> dict[str, Any]:
    """返回策略声明的数据依赖与精确 Provider 解析结果。"""
    rec = _get_record_or_404(strategy_id)
    deps = list(getattr(rec, "data_dependencies", None) or [])
    resolved: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for dep in deps:
        entry = dep.as_dict()
        try:
            provider_id = resolve_dependency_provider(
                dep,
                state.providers,
                disabled=state.disabled_provider_ids(),
            )
        except (KeyError, ValueError):
            entry["available"] = False
            missing.append(entry)
            continue
        entry["provider_id"] = provider_id
        entry["source"] = (
            "provider_cache" if dep.data_type == "kline" else "provider_registry"
        )
        entry["available"] = True
        resolved.append(entry)
    return {
        "strategy_id": rec.strategy_id,
        "name": rec.name,
        "engine_frequency": getattr(rec, "engine_frequency", None),
        "dependencies": resolved,
        "missing": missing,
        "errors": [],
    }


@router.post("/{strategy_id}/data/resolve")
async def strategy_data_resolve(
    strategy_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次解析策略的全部数据依赖。

    Body: ``{"start": ISO-8601, "end": ISO-8601, "limit": 1~5000}``。
    """
    rec = _get_record_or_404(strategy_id)
    deps = list(getattr(rec, "data_dependencies", None) or [])
    engine_frequency = getattr(rec, "engine_frequency", None)
    if not deps:
        return {
            "strategy_id": strategy_id,
            "name": rec.name,
            "engine_frequency": engine_frequency,
            "datasets": {},
            "aligned": {},
        }
    if state.store is None:
        raise HTTPException(status_code=503, detail="数据缓存未启用")

    body = payload or {}
    try:
        start = _parse_utc(body.get("start"))
        end = _parse_utc(body.get("end"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"无效时间参数: {exc}") from exc
    if start is not None and end is not None and start >= end:
        raise HTTPException(status_code=422, detail="start 必须早于 end")
    try:
        limit = int(body.get("limit", 300))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="limit 需要整数") from exc
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=422, detail="limit 必须在 1~5000")

    try:
        bundle = await fetch_strategy_data(
            deps,
            store=state.store,
            registry=state.providers,
            disabled=state.disabled_provider_ids(),
            start=start,
            end=end,
            limit=limit,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    datasets = {
        dep_id: {
            "meta": entry["meta"],
            "rows": _frame_to_records(entry["frame"]),
        }
        for dep_id, entry in bundle.items()
    }
    aligned = {
        group: _frame_to_records(frame)
        for group, frame in align_dependency_groups(bundle, deps).items()
        if not frame.empty
    }
    return {
        "strategy_id": strategy_id,
        "name": rec.name,
        "engine_frequency": engine_frequency,
        "datasets": datasets,
        "aligned": aligned,
    }
