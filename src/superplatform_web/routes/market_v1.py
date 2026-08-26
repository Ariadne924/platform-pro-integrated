"""Versioned market-data HTTP interface."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.kline_layers import DataLayer, KlineLayerPipeline, KlineQuery
from superplatform_web import state
from superplatform_web.ma_overlays import compute_ma_overlays

router = APIRouter(prefix="/api/v1/market", tags=["market-v1"])

Frequency = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
def _parse_utc(value: str | None) -> pd.Timestamp | None:
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


@router.get("/klines")
async def klines(
    exchange: Annotated[str, Query(min_length=1)],
    market_type: MarketType,
    symbol: Annotated[str, Query(min_length=3)],
    frequency: Frequency,
    layer: DataLayer = DataLayer.SILVER,
    limit: Annotated[int, Query(ge=1, le=5000)] = 300,
    start: str | None = None,
    end: str | None = None,
    ma: str | None = None,
) -> dict[str, Any]:
    """Return Bronze, Silver or Gold klines through one stable interface."""
    if state.store is None:
        raise HTTPException(status_code=503, detail="数据缓存未启用")

    try:
        start_time = _parse_utc(start)
        end_time = _parse_utc(end)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"无效时间参数: {exc}") from exc
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise HTTPException(status_code=422, detail="start 必须早于 end")

    def resolve_exact_provider(exchange: str, market: str, data_type: str) -> str:
        return state.resolve_provider_for_data_type(
            exchange,
            market,
            data_type,
            allow_fallback=False,
        )

    pipeline = KlineLayerPipeline(state.store, resolve_exact_provider)
    try:
        page = pipeline.load(
            KlineQuery(
                exchange=exchange,
                market_type=market_type,
                symbol=symbol,
                frequency=DataFrequency(frequency),
                layer=layer,
                start=start_time,
                end=end_time,
                limit=limit,
            )
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = page.as_dict()
    if ma:
        if layer is DataLayer.BRONZE:
            raise HTTPException(status_code=422, detail="Bronze 层不计算均线")
        ma_keys = None
        if ma.strip().lower() != "all":
            ma_keys = [key.strip() for key in ma.split(",") if key.strip()]
        closes = pd.Series([row["close"] for row in page.data], dtype="float64")
        payload["ma"] = compute_ma_overlays(closes, frequency, ma_keys)
    return payload
