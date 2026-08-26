"""Strategy listing, configuration CRUD and backtest endpoints."""

import json

import pandas as pd
from fastapi import APIRouter, HTTPException

from superplatform.consumption.base import ConsumerConfig
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import resolve_factor, validate_used_factors_are_instances
from superplatform.runtime.config import Config
from superplatform.runtime.pipeline import OfflineRuntime
from superplatform.runtime.providers import default_provider_for
from superplatform.strategy.registry import StrategyRegistry
from superplatform_web import factor_config as fc
from superplatform_web import state as _state
from superplatform_web.state import (
    config,
    disabled_provider_ids,
    providers,
)

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.get("")
async def list_strategies():
    reg = StrategyRegistry.get_instance()
    return [
        {
            "name": s.name,
            "description": s.description,
            "used_factors": s.used_factors,
        }
        for s in [reg.get(n) for n in reg.list_all()]
    ]


# ── Strategy configuration CRUD (factors.yaml strategies.*) ──────────


@router.get("/refresh")
async def refresh_strategies():
    """Re-run ``auto_discover()`` and report which strategies are new."""
    reg = StrategyRegistry.get_instance()
    before = set(reg.list_all())
    imported = reg.auto_discover()
    after = set(reg.list_all())
    return {
        "imported_modules": imported,
        "before": len(before),
        "after": len(after),
        "new_strategies": sorted(after - before),
    }


@router.get("/{name}/config")
async def get_strategy_config(name: str):
    """Full config entry for one strategy (used_factors etc.)."""
    reg = StrategyRegistry.get_instance()
    try:
        strategy = reg.get(name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"未知策略: {name}") from error

    cfg = fc.get_strategy_config(name)
    schema = [
        {
            "key": "used_factors",
            "label": "使用的因子",
            "type": "list",
            "description": "该策略组合的因子名列表",
            "available": reg and FactorRegistry.get_instance().list_all(),
        }
    ]
    return {
        "name": name,
        "description": strategy.description,
        "config": cfg,
        "schema": schema,
    }


@router.put("/{name}/config")
async def put_strategy_config(name: str, payload: dict):
    """Write one strategy's config back to ``factors.yaml``.

    Body: ``{"used_factors": ["momentum", ...]}``.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再修改策略配置")

    try:
        StrategyRegistry.get_instance().get(name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"未知策略: {name}") from error

    if "used_factors" in payload:
        if not isinstance(payload["used_factors"], list):
            raise HTTPException(status_code=422, detail="'used_factors' 需要列表")
        known = set(FactorRegistry.get_instance().list_all()) | set(
            FactorInstanceRegistry.get_instance().list_all()
        )
        unknown = [f for f in payload["used_factors"] if f not in known]
        if unknown:
            raise HTTPException(status_code=422, detail=f"未知因子: {unknown}")
        try:
            validate_used_factors_are_instances(payload["used_factors"])
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    cfg = fc.set_strategy_config(name, payload)
    _state.reload_config()
    return {"name": name, "config": cfg}


@router.post("/backtest")
async def backtest(data: dict):
    """Run a strategy backtest. Returns signals, trades, equity & metrics.

    Body:
        { "strategy": "momentum_demo", "start": "2021-01-01", "end": "2025-06-30",
          "symbols": ["BTCUSDT","ETHUSDT"] }
    """
    strategy_name = data["strategy"]
    sample_start = data.get("start", config.get("evaluation.sample_start"))
    sample_end = data.get("end", config.get("evaluation.sample_end"))
    symbols = data.get("symbols", ["BTCUSDT", "ETHUSDT"])

    # Build config override — resolve providers from defaults
    temp = Config(config.to_dict())
    strategy_obj = StrategyRegistry.get_instance().get(strategy_name)
    validate_used_factors_are_instances(strategy_obj.used_factors)
    for fn in strategy_obj.used_factors:
        factor = resolve_factor(fn)
        provider_map = {
            dt: default_provider_for(
                factor, dt, config=config, registry=providers,
                disabled=disabled_provider_ids(),
            ).provider_id
            for dt in factor.required_data
        }
        temp._data.setdefault("factors", {})[fn] = {
            "symbols": symbols,
            "providers": provider_map,
            "start": sample_start,
            "end": sample_end,
        }

    runtime = OfflineRuntime(
        temp,
        providers,
        store=_state.store,
    )
    try:
        result = await runtime.run_strategy(
            strategy_name,
            consumer=ConsumerConfig.backtest(),
            sample_start=sample_start,
            sample_end=sample_end,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    bt = result["backtest"]
    signal = result["signal"]

    return {
        "strategy_name": strategy_name,
        "metrics": {
            "sharpe": bt.sharpe,
            "total_return": bt.total_return,
            "annual_return": bt.annual_return,
            "annual_vol": bt.annual_vol,
            "max_drawdown": bt.max_drawdown,
            "win_rate": bt.win_rate,
            "avg_return": bt.avg_return,
            "liquidated_at": (
                bt.liquidated_at.isoformat() if bt.liquidated_at is not None else None
            ),
        },
        "equity": _df_to_records(bt.equity) if not bt.equity.empty else [],
        "trades": _df_to_records(bt.trades) if not bt.trades.empty else [],
        "signals": _df_to_records(signal.positions) if not signal.positions.empty else [],
    }


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))
