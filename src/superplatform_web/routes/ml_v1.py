"""Versioned API for leakage-controlled ML research jobs."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from superplatform.consumption.base import ConsumerConfig
from superplatform.factors.resolve import resolve_factor
from superplatform.ml.models import SUPPORTED_MODELS, WalkForwardConfig
from superplatform.ml.regime import RegimeConfig
from superplatform.ml.research import MLResearchConfig, run_ml_research
from superplatform.ml.risk import ScoreConfig
from superplatform.runtime.config import Config
from superplatform.runtime.dual import resolve_strategy_ex, scan_dual_registries
from superplatform.runtime.pipeline import OfflineRuntime
from superplatform.runtime.providers import default_provider_for
from superplatform.strategy.dual_registry import DualStrategyRegistry
from superplatform.strategy.registry import StrategyRegistry
from superplatform_web import state as web_state
from superplatform_web.ml_jobs import (
    MLJob,
    create_ml_job,
    get_ml_job,
    ml_job_snapshot,
    record_ml_event,
    request_ml_job_cancel,
)
from superplatform_web.research import build_batch_panel

router = APIRouter(prefix="/api/v1/ml", tags=["machine-learning"])


class WalkForwardBody(BaseModel):
    min_train_periods: int = Field(60, ge=20, le=100_000)
    test_periods: int = Field(20, ge=1, le=10_000)
    embargo_periods: int = Field(1, ge=0, le=1_000)
    alpha: float = Field(10.0, ge=0)
    elastic_net_l1_ratio: float = Field(0.3, ge=0, le=1)
    min_feature_coverage: float = Field(0.8, gt=0, le=1)
    max_features: int = Field(80, ge=1, le=2_000)
    max_pairwise_correlation: float = Field(0.95, gt=0, le=1)


class RegimeBody(BaseModel):
    fast_window: int = Field(20, ge=2)
    slow_window: int = Field(60, ge=3)
    volatility_window: int = Field(20, ge=2)
    trend_threshold: float = Field(0.03, gt=0, lt=1)
    bear_drawdown: float = Field(0.15, gt=0, lt=1)
    confirmation_periods: int = Field(3, ge=1)

    @model_validator(mode="after")
    def validate_windows(self) -> RegimeBody:
        if self.slow_window <= self.fast_window:
            raise ValueError("slow_window must be greater than fast_window")
        return self


class RiskBody(BaseModel):
    confidence: float = Field(0.95, gt=0.5, lt=1)
    max_drawdown_limit: float = Field(0.25, gt=0, le=1)
    var_limit: float = Field(0.08, gt=0, le=1)
    expected_shortfall_limit: float = Field(0.12, gt=0, le=1)


class MLJobRequest(BaseModel):
    factors: list[str] = Field(min_length=1, max_length=80)
    symbols: list[str] = Field(min_length=1, max_length=200)
    research_mode: Literal["single_asset", "cross_section"] = "cross_section"
    allow_short: bool = False
    core_factor: str | None = None
    start: str
    end: str
    exchange: str = "binance"
    market_type: str = "perpetual"
    frequency: Literal["1m", "5m", "15m", "1h", "4h", "6h", "8h", "1d"] = "1d"
    target_horizon: Literal[1, 5, 10, 20] = 1
    top_n: int = Field(3, ge=1, le=100)
    models: list[Literal["ridge", "elastic_net", "tree_stumps"]] = Field(
        default_factory=lambda: list(SUPPORTED_MODELS), min_length=1
    )
    existing_strategies: list[str] = Field(default_factory=list, max_length=20)
    taker_fee_bps: float = Field(4.0, ge=0, le=1_000)
    slippage_bps: float = Field(2.0, ge=0, le=1_000)
    reference_symbol: str | None = None
    walk_forward: WalkForwardBody = Field(default_factory=WalkForwardBody)
    regime: RegimeBody = Field(default_factory=RegimeBody)
    risk: RiskBody = Field(default_factory=RiskBody)

    @model_validator(mode="after")
    def validate_request(self) -> MLJobRequest:
        start = pd.Timestamp(self.start)
        end = pd.Timestamp(self.end)
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        else:
            start = start.tz_convert("UTC")
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        else:
            end = end.tz_convert("UTC")
        if start >= end:
            raise ValueError("start must be before end")
        if self.research_mode == "single_asset" and len(set(self.symbols)) != 1:
            raise ValueError("single_asset research requires exactly one symbol")
        if self.research_mode == "cross_section" and len(set(self.symbols)) < 2:
            raise ValueError("cross_section research requires at least two symbols")
        if self.research_mode == "cross_section" and self.top_n > len(set(self.symbols)):
            raise ValueError("top_n cannot exceed the number of unique symbols")
        if len(set(self.factors)) != len(self.factors):
            raise ValueError("factors must not contain duplicates")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must not contain duplicates")
        if len(set(self.existing_strategies)) != len(self.existing_strategies):
            raise ValueError("existing_strategies must not contain duplicates")
        reserved = {*self.models, "ensemble", "equal_weight", "core_factor"}
        collisions = sorted(reserved.intersection(self.existing_strategies))
        if collisions:
            raise ValueError(f"existing strategy names are reserved: {collisions}")
        if self.core_factor is not None and self.core_factor not in self.factors:
            raise ValueError("core_factor must be included in factors")
        return self


def _research_config(body: MLJobRequest) -> MLResearchConfig:
    return MLResearchConfig(
        research_mode=body.research_mode,
        allow_short=body.allow_short,
        core_factor=body.core_factor,
        target_horizon=body.target_horizon,
        top_n=body.top_n,
        frequency=body.frequency,
        taker_fee_bps=body.taker_fee_bps,
        slippage_bps=body.slippage_bps,
        models=tuple(body.models),
        reference_symbol=body.reference_symbol,
        walk_forward=WalkForwardConfig(
            horizon_periods=body.target_horizon,
            **body.walk_forward.model_dump(),
        ),
        regime=RegimeConfig(**body.regime.model_dump()),
        score=ScoreConfig(**body.risk.model_dump()),
    )


async def _load_existing_strategy_signals(
    body: MLJobRequest,
    request: Request,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Run registered strategies only far enough to obtain their signals.

    The ML research engine intentionally re-backtests those signals against
    its own Gold price panel and cost assumptions.  This prevents the existing
    strategy rows from entering the leaderboard with a different data window
    or a cheaper execution model.
    """
    signals: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    for strategy_name in body.existing_strategies:
        try:
            _, used_factors, _ = resolve_strategy_ex(strategy_name)
            temp = Config(request.app.state.config.to_dict())
            temp._data.setdefault("evaluation", {})["cost"] = {
                "taker_fee_bps": body.taker_fee_bps,
                "slippage_bps": body.slippage_bps,
            }
            for factor_name in used_factors:
                factor = resolve_factor(factor_name)
                provider_map = {
                    data_type: default_provider_for(
                        factor,
                        data_type,
                        config=temp,
                        registry=request.app.state.providers,
                    ).provider_id
                    for data_type in factor.required_data
                }
                temp._data.setdefault("factors", {})[factor_name] = {
                    "symbols": body.symbols,
                    "providers": provider_map,
                    "start": body.start,
                    "end": body.end,
                    "frequency": body.frequency,
                }
            runtime = OfflineRuntime(
                temp,
                request.app.state.providers,
                dual_factor_defaults={
                    "symbols": body.symbols,
                    "start": body.start,
                    "end": body.end,
                },
                store=web_state.store,
            )
            result = await runtime.run_strategy(
                strategy_name,
                consumer=ConsumerConfig.backtest(),
                sample_start=body.start,
                sample_end=body.end,
            )
            signals[strategy_name] = result["signal"].positions.copy()
        except Exception as exc:
            errors[strategy_name] = f"{type(exc).__name__}: {exc}"
    return signals, errors


async def _run_job(job: MLJob, body: MLJobRequest, request: Request) -> None:
    try:
        job.status, job.stage = "running", "data"
        record_ml_event(job, "data", "计算 Gold 因子研究面板", progress=0.05)
        panel = await build_batch_panel(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            factor_names=body.factors,
            symbols=body.symbols,
            start=body.start,
            end=body.end,
            exchange=body.exchange,
            market=body.market_type,
            frequency=body.frequency,
        )
        if job.cancel_requested:
            job.status, job.stage = "cancelled", "cancelled"
            record_ml_event(job, "cancelled", "任务已在训练前取消", progress=1.0)
            return
        existing_signals: dict[str, pd.DataFrame] = {}
        existing_errors: dict[str, str] = {}
        if body.existing_strategies:
            job.stage = "existing_strategies"
            record_ml_event(
                job,
                "existing_strategies",
                "生成已有策略信号，准备统一样本外评分",
                progress=0.20,
            )
            existing_signals, existing_errors = await _load_existing_strategy_signals(
                body, request
            )
        job.stage = "training"
        record_ml_event(job, "training", "执行无泄漏 Walk-Forward 训练", progress=0.35)
        result = await asyncio.to_thread(
            run_ml_research,
            panel,
            config=_research_config(body),
            existing_strategy_signals=existing_signals,
        )
        result["existing_strategy_errors"] = {
            **existing_errors,
            **result.get("existing_strategy_errors", {}),
        }
        if job.cancel_requested:
            job.status, job.stage = "cancelled", "cancelled"
            record_ml_event(job, "cancelled", "训练完成后按取消请求丢弃结果", progress=1.0)
            return
        job.result = result
        job.status, job.stage = "done", "complete"
        record_ml_event(job, "done", "训练、自动回测与风险评分完成", progress=1.0)
    except Exception as exc:  # job boundary: expose a stable status instead of task loss
        job.status, job.stage = "error", "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        record_ml_event(job, "error", "机器学习研究任务失败", progress=1.0)


@router.get("/capabilities")
async def ml_capabilities() -> dict[str, Any]:
    return {
        "models": list(SUPPORTED_MODELS),
        "target_horizons": [1, 5, 10, 20],
        "research_modes": ["single_asset", "cross_section"],
        "candidate_groups": {
            "trained_models": list(SUPPORTED_MODELS),
            "derived_ensembles": ["ensemble"],
            "non_ml_baselines": ["equal_weight", "core_factor"],
            "existing_strategies": "registered_strategy_signals",
        },
        "regimes": ["bull", "bear", "sideways"],
        "job_backend": "in_process",
        "gpu_enabled": False,
        "future_plugins": ["lightgbm", "xgboost", "deep_learning_gpu"],
        "comparison_protocol": "shared-window-risk-first-v1",
        "existing_strategy_scoring": True,
        "comparison_metrics": [
            "total_return",
            "sharpe",
            "sortino",
            "max_drawdown",
            "historical_var",
            "expected_shortfall",
            "paired_block_bootstrap",
            "pareto_front",
        ],
        "score_weights": {
            "downside_risk": 45,
            "walk_forward_robustness": 20,
            "relative_performance": 20,
            "ic_rank_ic": 10,
            "upside_bonus": 5,
        },
        "research_only": True,
    }


@router.get("/strategies")
async def ml_scoreable_strategies() -> dict[str, Any]:
    """List registered strategies that can be selected for unified scoring."""
    scan_dual_registries()
    registry = StrategyRegistry.get_instance()
    dual = DualStrategyRegistry.get_instance()
    rows = []
    for name in registry.list_all():
        strategy = registry.get(name)
        record = dual.get_record(name)
        rows.append(
            {
                "name": name,
                "description": strategy.description,
                "source": "dual_file" if record is not None else "decorator",
                "status": record.status if record is not None else "registered",
            }
        )
    return {"strategies": rows, "count": len(rows)}


@router.post("/jobs", status_code=202)
async def submit_ml_job(body: MLJobRequest, request: Request) -> dict[str, Any]:
    payload = body.model_dump(mode="json")
    job, created = create_ml_job(payload)
    if created:
        asyncio.create_task(_run_job(job, body, request))
    response = ml_job_snapshot(job)
    response["reused"] = not created
    return response


@router.get("/jobs/{job_id}")
async def ml_job_status(job_id: str) -> dict[str, Any]:
    job = get_ml_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ML job not found")
    return ml_job_snapshot(job)


@router.delete("/jobs/{job_id}")
async def cancel_ml_job(job_id: str) -> dict[str, Any]:
    job = get_ml_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ML job not found")
    accepted = request_ml_job_cancel(job)
    return {"accepted": accepted, **ml_job_snapshot(job)}
