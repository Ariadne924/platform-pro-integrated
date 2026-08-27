"""Two-tail diagnostics and risk-first research scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ScoreConfig:
    confidence: float = 0.95
    max_drawdown_limit: float = 0.25
    var_limit: float = 0.08
    expected_shortfall_limit: float = 0.12

    def validate(self) -> None:
        if not 0.5 < self.confidence < 1:
            raise ValueError("confidence must be in (0.5, 1)")
        for value in (
            self.max_drawdown_limit,
            self.var_limit,
            self.expected_shortfall_limit,
        ):
            if value <= 0:
                raise ValueError("risk limits must be positive")


def _finite_returns(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()


def tail_risk_metrics(
    returns: pd.Series,
    *,
    benchmark_returns: pd.Series | None = None,
    confidence: float = 0.95,
) -> dict[str, float | int | None]:
    """Return separate loss-tail and upside-opportunity diagnostics."""
    if not 0.5 < confidence < 1:
        raise ValueError("confidence must be in (0.5, 1)")
    clean = _finite_returns(returns)
    if clean.empty:
        return {"sample_count": 0}
    lower_quantile = float(clean.quantile(1.0 - confidence))
    upper_quantile = float(clean.quantile(confidence))
    lower_tail = clean[clean <= lower_quantile]
    upper_tail = clean[clean >= upper_quantile]
    equity = (1.0 + clean).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    metrics: dict[str, float | int | None] = {
        "sample_count": int(len(clean)),
        "historical_var": max(0.0, -lower_quantile),
        "expected_shortfall": max(0.0, -float(lower_tail.mean())),
        "max_drawdown": abs(float(drawdown.min())),
        "upper_quantile": upper_quantile,
        "expected_tail_gain": float(upper_tail.mean()),
        "var_breach_count": int((clean < lower_quantile).sum()),
        "var_breach_rate": float((clean < lower_quantile).mean()),
    }
    top_count = max(1, int(np.ceil(len(clean) * (1.0 - confidence))))
    top_return = float(clean.nlargest(top_count).sum())
    total_positive = float(clean.clip(lower=0.0).sum())
    metrics["top_days_positive_contribution"] = (
        top_return / total_positive if total_positive > 0 else None
    )

    if benchmark_returns is not None:
        aligned = pd.concat(
            [clean.rename("strategy"), _finite_returns(benchmark_returns).rename("benchmark")],
            axis=1,
            join="inner",
        ).dropna()
        benchmark_up = aligned["benchmark"].clip(lower=0.0)
        strategy_on_up = aligned.loc[benchmark_up > 0, "strategy"].clip(lower=0.0)
        denominator = float(benchmark_up.sum())
        metrics["upside_capture"] = (
            float(strategy_on_up.sum()) / denominator if denominator > 0 else None
        )
        up_periods = benchmark_up > 0
        metrics["missed_upside_rate"] = (
            float((aligned.loc[up_periods, "strategy"] <= 0).mean())
            if bool(up_periods.any())
            else None
        )
    return metrics


def _ratio_score(value: float, limit: float, points: float) -> float:
    return float(points * np.clip(1.0 - value / limit, 0.0, 1.0))


def score_research_result(
    *,
    strategy_metrics: dict[str, Any],
    benchmark_metrics: dict[str, Any],
    tail_metrics: dict[str, Any],
    fold_metrics: list[dict[str, Any]],
    regime_metrics: dict[str, dict[str, Any]],
    ic: float | None,
    rank_ic: float | None,
    config: ScoreConfig | None = None,
) -> dict[str, Any]:
    """Score strict OOS evidence with non-compensating safety gates."""
    config = config or ScoreConfig()
    config.validate()
    total_return = float(strategy_metrics.get("total_return") or 0.0)
    benchmark_return = float(benchmark_metrics.get("total_return") or 0.0)
    max_drawdown = float(tail_metrics.get("max_drawdown") or 0.0)
    var = float(tail_metrics.get("historical_var") or 0.0)
    expected_shortfall = float(tail_metrics.get("expected_shortfall") or 0.0)

    safety = (
        _ratio_score(max_drawdown, config.max_drawdown_limit, 20.0)
        + _ratio_score(var, config.var_limit, 12.5)
        + _ratio_score(expected_shortfall, config.expected_shortfall_limit, 12.5)
    )
    valid_folds = [row for row in fold_metrics if row.get("sample_count", 0)]
    positive_fold_ratio = (
        float(np.mean([float(row.get("total_return", 0.0)) > 0 for row in valid_folds]))
        if valid_folds
        else 0.0
    )
    fold_returns: np.ndarray = np.asarray(
        [float(row.get("total_return", 0.0)) for row in valid_folds], dtype=float
    )
    dispersion = float(np.std(fold_returns)) if len(fold_returns) > 1 else 1.0
    regime_rows = [row for row in regime_metrics.values() if row.get("sample_count", 0)]
    regime_non_loss_ratio = (
        float(np.mean([float(row.get("total_return", 0.0)) >= 0 for row in regime_rows]))
        if regime_rows
        else 0.0
    )
    robustness = (
        10.0 * positive_fold_ratio
        + 5.0 * float(np.clip(1.0 - dispersion / 0.20, 0.0, 1.0))
        + 5.0 * regime_non_loss_ratio
    )

    excess = total_return - benchmark_return
    excess_score = 10.0 * float(np.clip((excess + 0.05) / 0.15, 0.0, 1.0))
    sharpe = float(strategy_metrics.get("sharpe") or 0.0)
    performance = excess_score + 10.0 * float(np.clip(sharpe / 2.0, 0.0, 1.0))
    ic_score = 5.0 * float(np.clip(abs(float(ic or 0.0)) / 0.05, 0.0, 1.0))
    rank_ic_score = 5.0 * float(
        np.clip(abs(float(rank_ic or 0.0)) / 0.05, 0.0, 1.0)
    )
    upside_capture = tail_metrics.get("upside_capture")
    concentration = tail_metrics.get("top_days_positive_contribution")
    upside = 0.0
    if upside_capture is not None:
        upside = 5.0 * float(np.clip(float(upside_capture), 0.0, 1.0))
        if concentration is not None and float(concentration) > 0.5:
            upside *= float(np.clip(1.0 - (float(concentration) - 0.5), 0.0, 1.0))

    gates: list[str] = []
    if total_return < 0:
        gates.append("negative_oos_net_return")
    if max_drawdown > config.max_drawdown_limit:
        gates.append("max_drawdown_limit")
    if var > config.var_limit:
        gates.append("var_limit")
    if expected_shortfall > config.expected_shortfall_limit:
        gates.append("expected_shortfall_limit")
    components = {
        "downside_risk": round(safety, 4),
        "walk_forward_robustness": round(robustness, 4),
        "relative_performance": round(performance, 4),
        "ic_rank_ic": round(ic_score + rank_ic_score, 4),
        "upside_bonus": round(upside, 4),
    }
    raw_score = float(sum(components.values()))
    return {
        "score": round(min(100.0, raw_score), 2),
        "status": "rejected" if gates else "eligible",
        "gates_failed": gates,
        "components": components,
        "weights": {
            "downside_risk": 45,
            "walk_forward_robustness": 20,
            "relative_performance": 20,
            "ic_rank_ic": 10,
            "upside_bonus": 5,
        },
        "note": "Upside bonus never overrides a failed downside-risk gate.",
    }
