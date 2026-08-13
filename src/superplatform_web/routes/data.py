"""Generic data-query endpoints backed by the registered provider registry."""

import json
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request

from superplatform.data.enums import DataFrequency
from superplatform_web.routes.config import load_settings_overlay, write_settings_overlay
from superplatform_web.state import disabled_provider_ids, provider_label

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/providers")
async def list_data_providers(request: Request) -> list[dict]:
    """Describe the data capabilities exposed by the current provider registry."""
    registry = request.app.state.providers
    disabled = disabled_provider_ids()
    result = []
    for provider_id in registry.list_all():
        provider = registry.get(provider_id)
        result.append({
            "provider_id": provider_id,
            "label": provider_label(provider_id),
            "data_type": provider.data_type,
            "market_type": provider.market_type.value if provider.market_type else None,
            "enabled": provider_id not in disabled,
        })
    return result


@router.put("/providers/{provider_id}")
async def set_data_provider(provider_id: str, payload: dict, request: Request):
    """Toggle a provider on/off, persisted to the settings overlay.

    Body: ``{"enabled": bool}``. Disabled providers are skipped by
    ``resolve_provider_for_data_type`` until re-enabled.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再修改数据源")

    registry = request.app.state.providers
    if provider_id not in registry:
        raise HTTPException(status_code=404, detail=f"未知 provider: {provider_id}")

    enabled = payload.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="'enabled' 需要布尔值")

    overlay = load_settings_overlay()
    providers_cfg = overlay.setdefault("data", {}).setdefault("providers", {})
    entry = providers_cfg.setdefault(provider_id, {})
    if enabled is not None:
        entry["enabled"] = enabled
    write_settings_overlay(overlay)
    _state.reload_config()

    current = entry.get("enabled", True)
    return {"provider_id": provider_id, "enabled": bool(current)}


# Universe routes must be registered BEFORE the "/{data_type}" catch-all
# below — Starlette matches in registration order, so a later definition
# would be captured by /{data_type} and 422 on the missing query params.
@router.get("/universe")
async def get_universe(request: Request) -> dict:
    """Return the synced universe, falling back to the config pool when empty."""
    import superplatform_web.state as _state
    from superplatform_web.universe import _iso

    if _state.store is not None:
        df = _state.store.query_universe()
        if not df.empty:
            active = df[
                df["delisted_at"].isna() & (df["exchange"] == "binance")
            ]
            per = {
                r["symbol"]: {
                    "status": r["status"],
                    "listed_at": _iso(r["listed_at"]),
                    "delisted_at": _iso(r["delisted_at"]),
                }
                for _, r in active.iterrows()
            }
            return {
                "exchange": "binance",
                "source": "universe",
                "count": len(per),
                "updated_at": _iso(pd.to_datetime(df["updated_at"], utc=True).max()),
                "symbols": list(per.keys()),
                "per_symbol": per,
            }
    cfg = request.app.state.config.get("data.symbols.perpetual") or []
    return {
        "exchange": "binance",
        "source": "config",
        "count": len(cfg),
        "updated_at": None,
        "symbols": list(cfg),
        "per_symbol": {},
    }


@router.post("/universe/refresh")
async def refresh_universe() -> dict:
    """Force a sync against Binance exchangeInfo."""
    from fastapi.responses import JSONResponse

    from superplatform_web.universe import sync_universe

    try:
        return await sync_universe()
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"synced": False, "error": str(exc)},
        )


@router.get("/{data_type}")
async def query_data(
    data_type: str,
    request: Request,
    provider_id: str,
    symbol: str,
    frequency: DataFrequency = DataFrequency.D1,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=2_000, ge=1, le=5_000),
) -> dict:
    """Fetch one provider-backed dataset in a type-agnostic response envelope."""
    registry = request.app.state.providers
    if provider_id not in registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    provider = registry.get(provider_id)
    if provider.data_type != data_type:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Provider '{provider_id}' serves '{provider.data_type}', "
                f"not '{data_type}'"
            ),
        )

    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")

    data = await provider.fetch(
        symbol=symbol,
        frequency=frequency,
        start=start,
        end=end,
        limit=limit,
    )
    return _data_response(
        data_type=data_type,
        provider_id=provider_id,
        symbol=symbol,
        frequency=frequency,
        start=start,
        end=end,
        data=data,
    )


def _data_response(
    *,
    data_type: str,
    provider_id: str,
    symbol: str,
    frequency: DataFrequency,
    start: datetime | None,
    end: datetime | None,
    data: pd.DataFrame,
) -> dict:
    """Serialize any provider DataFrame while preserving its declared columns."""
    records = json.loads(data.to_json(orient="records", date_format="iso"))
    return {
        "data_type": data_type,
        "provider_id": provider_id,
        "query": {
            "symbol": symbol,
            "frequency": frequency.value,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "limit": len(data),
        },
        "columns": list(data.columns),
        "records": records,
    }
