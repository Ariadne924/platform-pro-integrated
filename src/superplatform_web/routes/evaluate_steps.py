"""Per-step factor evaluation endpoints.

The full evaluation pipeline returns every metric in one flat payload;
these endpoints let the frontend request only the steps it needs (e.g.
IC + forward-bias without layers or cost), so the /factors page can offer
a check-box of evaluation steps driven by ``GET /api/evaluate/manifest``.

Each single-factor step runs the shared ``evaluate_factor`` orchestration
and slices the result; ``correlation`` uses the multi-factor batch path
which additionally computes the pairwise correlation matrix.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from superplatform_web.research import batch_evaluate, evaluate_factor, factor_series

router = APIRouter(prefix="/api/evaluate", tags=["evaluate"])


class SingleFactorStepRequest(BaseModel):
    factor: str
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
    params: dict[str, Any] | None = None
    param_units: dict[str, str | None] | None = None
    frequency: str | None = None


class SeriesRequest(BaseModel):
    """One symbol's factor-value + kline series for the overlay chart."""
    factor: str
    symbol: str
    start: str
    end: str
    params: dict[str, Any] | None = None
    param_units: dict[str, str | None] | None = None
    frequency: str | None = None


class CorrelationRequest(BaseModel):
    factors: list[str] = Field(min_length=2)
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
    frequency: str | None = None


# step name → {title, slice keys of the full evaluation dict}
_STEPS: dict[str, dict[str, Any]] = {
    "ic": {
        "title": "IC / RankIC / ICIR",
        "description": "截面 Pearson IC、Spearman RankIC、ICIR 及时序",
        "slices": ["ic_stats", "rank_ic_stats", "ic", "rank_ic"],
    },
    "decay": {
        "title": "IC 衰减",
        "description": "从 t+1 到 t+N 逐期 IC，观察因子预测力半衰期",
        "slices": ["ic_decay"],
    },
    "layers": {
        "title": "分层测试 + 换手率",
        "description": "分位数分层收益、多空组合、层间换手率",
        "slices": ["layer_results", "turnover"],
    },
    "cost": {
        "title": "成本敏感性",
        "description": "不同 fee / slippage 假设下的净收益",
        "slices": ["cost"],
    },
    "forward-bias": {
        "title": "前视偏差检查",
        "description": "渐进截断数据重算因子值，验证历史值不变（硬门槛）",
        "slices": ["forward_bias_passed", "forward_bias"],
    },
    "correlation": {
        "title": "多因子相关矩阵",
        "description": "同期横截面 Pearson / Spearman 相关矩阵，识别冗余因子",
        "slices": ["correlation"],
    },
}


def _step_result(evaluation: dict, steps: list[str]) -> dict:
    """Pick the output keys belonging to the requested step names."""
    result: dict[str, Any] = {}
    for step in steps:
        for key in _STEPS[step]["slices"]:
            if key in evaluation:
                result[key] = evaluation[key]
    return result


async def _run_steps(data: dict, request, steps: list[str]) -> dict:
    unknown = [s for s in steps if s not in _STEPS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知评估步骤: {unknown}")

    output: dict[str, Any] = {}
    try:
        if "correlation" in steps:
            corr_req = CorrelationRequest(**data)
            batch = await batch_evaluate(
                base_config=request.app.state.config,
                providers=request.app.state.providers,
                factor_names=corr_req.factors,
                symbols=corr_req.symbols,
                start=corr_req.start,
                end=corr_req.end,
                frequency=corr_req.frequency,
            )
            for factor_result in batch["results"]:
                output.setdefault("results", []).append(
                    {
                        k: factor_result[k]
                        for k in ("factor_name", "ic_stats", "rank_ic_stats")
                    }
                )
            output["correlation"] = batch["correlation"]

        single_steps = [s for s in steps if s != "correlation"]
        if single_steps:
            req = SingleFactorStepRequest(**data)
            evaluation = await evaluate_factor(
                base_config=request.app.state.config,
                providers=request.app.state.providers,
                factor_name=req.factor,
                symbols=req.symbols,
                start=req.start,
                end=req.end,
                params=req.params,
                param_units=req.param_units,
                frequency=req.frequency,
                result_cache=request.app.state.evaluation_cache,
            )
            output["factor_name"] = evaluation.get("factor_name")
            for key in ("run_id", "params_hash", "effective_params", "cache_hit"):
                if key in evaluation:
                    output[key] = evaluation[key]
            output.update(_step_result(evaluation, single_steps))
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return output


@router.get("/manifest")
async def evaluate_manifest():
    """Every available evaluation step with its parameter schema."""
    return {
        "steps": [
            {
                "key": key,
                "title": meta["title"],
                "description": meta["description"],
                "params": {
                    "factor": "str",
                    "symbols": "list[str]",
                    "start": "date",
                    "end": "date",
                    "params": "dict | None",
                    "param_units": "dict[str, str | None] | None",
                } if key != "correlation" else {
                    "factors": "list[str] (min 2)",
                    "symbols": "list[str]",
                    "start": "date",
                    "end": "date",
                },
                "outputs": meta["slices"],
            }
            for key, meta in _STEPS.items()
        ]
    }


@router.post("/ic")
async def run_ic(data: SingleFactorStepRequest, request: Request):
    return await _run_steps(data.model_dump(), request, ["ic"])


@router.post("/decay")
async def run_decay(data: SingleFactorStepRequest, request: Request):
    return await _run_steps(data.model_dump(), request, ["decay"])


@router.post("/layers")
async def run_layers(data: SingleFactorStepRequest, request: Request):
    return await _run_steps(data.model_dump(), request, ["layers"])


@router.post("/cost")
async def run_cost(data: SingleFactorStepRequest, request: Request):
    return await _run_steps(data.model_dump(), request, ["cost"])


@router.post("/forward-bias")
async def run_forward_bias(data: SingleFactorStepRequest, request: Request):
    return await _run_steps(data.model_dump(), request, ["forward-bias"])


@router.post("/correlation")
async def run_correlation(data: CorrelationRequest, request: Request):
    return await _run_steps(data.model_dump(), request, ["correlation"])


@router.post("/series")
async def run_series(data: SeriesRequest, request: Request):
    """Per-symbol factor value + kline series, aligned to one cadence."""
    try:
        return await factor_series(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            factor_name=data.factor,
            symbol=data.symbol,
            start=data.start,
            end=data.end,
            params=data.params,
            param_units=data.param_units,
            frequency=data.frequency,
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
