"""Versioned API for leakage-controlled ML research jobs."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from superplatform.ml.models import SUPPORTED_MODELS, WalkForwardConfig
from superplatform.ml.regime import RegimeConfig
from superplatform.ml.research import MLResearchConfig, run_ml_research
from superplatform.ml.risk import ScoreConfig
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
    symbols: list[str] = Field(min_length=2, max_length=200)
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
        if self.top_n > len(set(self.symbols)):
            raise ValueError("top_n cannot exceed the number of unique symbols")
        if len(set(self.factors)) != len(self.factors):
            raise ValueError("factors must not contain duplicates")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must not contain duplicates")
        return self


def _research_config(body: MLJobRequest) -> MLResearchConfig:
    return MLResearchConfig(
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
        job.stage = "training"
        record_ml_event(job, "training", "执行无泄漏 Walk-Forward 训练", progress=0.35)
        result = await asyncio.to_thread(
            run_ml_research,
            panel,
            config=_research_config(body),
        )
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
        "regimes": ["bull", "bear", "sideways"],
        "job_backend": "in_process",
        "gpu_enabled": False,
        "future_plugins": ["lightgbm", "xgboost", "deep_learning_gpu"],
        "comparison_protocol": "shared-window-risk-first-v1",
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
