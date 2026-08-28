"""Fair, uncertainty-aware comparison for strategy return series.

All candidates are aligned to the same timestamps before metrics are computed.
This prevents a strategy from winning merely because it was evaluated on an
easier or shorter market window.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

from superplatform.ml.risk import tail_risk_metrics


def _clean_returns(values: pd.Series) -> pd.Series:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if clean.index.has_duplicates:
        raise ValueError("strategy return indexes must not contain duplicates")
    return clean.sort_index()


def _finite_or_none(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def _correlation(left: pd.Series, right: pd.Series) -> float | None:
    if left.nunique() < 2 or right.nunique() < 2:
        return None
    return _finite_or_none(float(left.corr(right)))


def _return_metrics(
    returns: pd.Series,
    *,
    periods_per_year: int,
    confidence: float,
) -> dict[str, float | int | None]:
    total_return = float((1.0 + returns).prod() - 1.0)
    annual_return = (
        float((1.0 + total_return) ** (periods_per_year / len(returns)) - 1.0)
        if total_return > -1.0
        else -1.0
    )
    annual_vol = float(returns.std(ddof=1) * np.sqrt(periods_per_year))
    downside = returns.clip(upper=0.0)
    downside_deviation = float(
        np.sqrt(np.square(downside).mean()) * np.sqrt(periods_per_year)
    )
    tails = tail_risk_metrics(returns, confidence=confidence)
    return {
        "sample_count": len(returns),
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_vol": annual_vol,
        "sharpe": annual_return / annual_vol if annual_vol > 0 else None,
        "sortino": annual_return / downside_deviation if downside_deviation > 0 else None,
        "win_rate": float((returns > 0).mean()),
        "max_drawdown": tails.get("max_drawdown"),
        "historical_var": tails.get("historical_var"),
        "expected_shortfall": tails.get("expected_shortfall"),
    }


def _block_bootstrap_difference(
    left: pd.Series,
    right: pd.Series,
    *,
    samples: int,
    seed: int,
) -> dict[str, float | int]:
    """Bootstrap paired total-return differences with contiguous blocks."""
    if samples < 20:
        raise ValueError("bootstrap_samples must be at least 20")
    size = len(left)
    if size < 2:
        point = float((1.0 + left).prod() - (1.0 + right).prod())
        return {
            "samples": 0,
            "block_size": 0,
            "difference": point,
            "ci_low": point,
            "ci_high": point,
            "probability_positive": float(point > 0),
            "probability_negative": float(point < 0),
            "probability_equal": float(point == 0),
        }
    block_size = max(2, min(20, int(np.sqrt(size))))
    blocks_needed = int(np.ceil(size / block_size))
    full_blocks = max(0, blocks_needed - 1)
    remainder = size - full_blocks * block_size
    rng = np.random.default_rng(seed)
    left_values = left.to_numpy(dtype=float)
    right_values = right.to_numpy(dtype=float)
    left_full_growth = np.prod(
        np.lib.stride_tricks.sliding_window_view(1.0 + left_values, block_size), axis=1
    )
    right_full_growth = np.prod(
        np.lib.stride_tricks.sliding_window_view(1.0 + right_values, block_size), axis=1
    )
    left_remainder_growth = np.prod(
        np.lib.stride_tricks.sliding_window_view(1.0 + left_values, remainder), axis=1
    )
    right_remainder_growth = np.prod(
        np.lib.stride_tricks.sliding_window_view(1.0 + right_values, remainder), axis=1
    )
    starts = rng.integers(0, len(left_full_growth), size=(samples, full_blocks))
    remainder_starts = rng.integers(0, len(left_remainder_growth), size=samples)
    left_growth = np.prod(left_full_growth[starts], axis=1) * left_remainder_growth[
        remainder_starts
    ]
    right_growth = np.prod(right_full_growth[starts], axis=1) * right_remainder_growth[
        remainder_starts
    ]
    differences: np.ndarray = left_growth - right_growth
    point = float((1.0 + left).prod() - (1.0 + right).prod())
    return {
        "samples": samples,
        "block_size": block_size,
        "difference": point,
        "ci_low": float(np.quantile(differences, 0.025)),
        "ci_high": float(np.quantile(differences, 0.975)),
        "probability_positive": float((differences > 0).mean()),
        "probability_negative": float((differences < 0).mean()),
        "probability_equal": float((differences == 0).mean()),
    }


def _pareto_front(rows: list[dict[str, Any]]) -> list[str]:
    """Return non-dominated names: more return/Sharpe, less drawdown/ES."""
    frontier: list[str] = []
    for candidate in rows:
        candidate_values = (
            float(candidate.get("total_return") or 0.0),
            float(candidate.get("sharpe") or 0.0),
            -float(candidate.get("max_drawdown") or 0.0),
            -float(candidate.get("expected_shortfall") or 0.0),
        )
        dominated = False
        for other in rows:
            if other is candidate:
                continue
            other_values = (
                float(other.get("total_return") or 0.0),
                float(other.get("sharpe") or 0.0),
                -float(other.get("max_drawdown") or 0.0),
                -float(other.get("expected_shortfall") or 0.0),
            )
            if all(a >= b for a, b in zip(other_values, candidate_values, strict=True)) and any(
                a > b for a, b in zip(other_values, candidate_values, strict=True)
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(str(candidate["name"]))
    return frontier


def compare_strategy_returns(
    strategy_returns: dict[str, pd.Series],
    *,
    benchmark_name: str,
    periods_per_year: int,
    confidence: float = 0.95,
    scorecards: dict[str, dict[str, Any]] | None = None,
    bootstrap_samples: int = 300,
) -> dict[str, Any]:
    """Compare candidates on a shared OOS window with paired uncertainty."""
    if benchmark_name not in strategy_returns:
        raise ValueError("benchmark_name must identify one strategy return series")
    if len(strategy_returns) < 2:
        raise ValueError("at least two strategies are required for comparison")
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be positive")
    clean = {name: _clean_returns(series).rename(name) for name, series in strategy_returns.items()}
    aligned = pd.concat(clean.values(), axis=1, join="inner").dropna()
    if len(aligned) < 2:
        raise ValueError("strategies do not share enough return observations")

    scorecards = scorecards or {}
    rows: list[dict[str, Any]] = []
    for name in sorted(aligned.columns):
        metrics = _return_metrics(
            aligned[name],
            periods_per_year=periods_per_year,
            confidence=confidence,
        )
        scorecard = scorecards.get(name, {})
        rows.append(
            {
                "name": name,
                **metrics,
                "score": scorecard.get("score"),
                "status": scorecard.get("status", "unscored"),
                "gates_failed": list(scorecard.get("gates_failed", [])),
            }
        )

    rows.sort(
        key=lambda row: (
            row["status"] != "eligible",
            -float(row["score"] if row["score"] is not None else -1.0),
            float(row["max_drawdown"] or 0.0),
            -float(row["total_return"] or 0.0),
            str(row["name"]),
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    pairwise: list[dict[str, Any]] = []
    ordered_names = sorted(aligned.columns)
    for pair_index, (left_name, right_name) in enumerate(combinations(ordered_names, 2)):
        left, right = aligned[left_name], aligned[right_name]
        spread = left - right
        spread_std = float(spread.std(ddof=1))
        bootstrap = _block_bootstrap_difference(
            left,
            right,
            samples=bootstrap_samples,
            seed=17_291 + pair_index,
        )
        pairwise.append(
            {
                "left": left_name,
                "right": right_name,
                "correlation": _correlation(left, right),
                "outperformance_rate": float((left > right).mean()),
                "information_ratio": (
                    float(spread.mean() / spread_std * np.sqrt(periods_per_year))
                    if spread_std > 0
                    else None
                ),
                "bootstrap": bootstrap,
            }
        )

    relative_to_benchmark: list[dict[str, Any]] = []
    for row in pairwise:
        if benchmark_name not in {row["left"], row["right"]}:
            continue
        if row["right"] == benchmark_name:
            relative_to_benchmark.append(row)
            continue
        bootstrap = row["bootstrap"]
        relative_to_benchmark.append(
            {
                "left": row["right"],
                "right": benchmark_name,
                "correlation": row["correlation"],
                "outperformance_rate": float(
                    (aligned[row["right"]] > aligned[benchmark_name]).mean()
                ),
                "information_ratio": (
                    -float(row["information_ratio"])
                    if row["information_ratio"] is not None
                    else None
                ),
                "bootstrap": {
                    **bootstrap,
                    "difference": -float(bootstrap["difference"]),
                    "ci_low": -float(bootstrap["ci_high"]),
                    "ci_high": -float(bootstrap["ci_low"]),
                    "probability_positive": float(bootstrap["probability_negative"]),
                    "probability_negative": float(bootstrap["probability_positive"]),
                },
            }
        )
    return {
        "protocol": "shared-window-risk-first-v1",
        "benchmark": benchmark_name,
        "common_window": {
            "start": pd.Timestamp(aligned.index[0]).isoformat(),
            "end": pd.Timestamp(aligned.index[-1]).isoformat(),
            "periods": len(aligned),
        },
        "leaderboard": rows,
        "pareto_front": _pareto_front(rows),
        "relative_to_benchmark": relative_to_benchmark,
        "pairwise": pairwise,
        "correlation_matrix": {
            str(name): {
                str(column): _correlation(aligned[name], aligned[column])
                for column in aligned.columns
            }
            for name in aligned.columns
        },
        "ranking_rule": (
            "eligible first, then risk-first score, lower drawdown, higher total return"
        ),
        "uncertainty": (
            "95% paired moving-block bootstrap interval; descriptive research evidence only"
        ),
    }
