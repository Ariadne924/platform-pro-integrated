"""Signal entry/exit threshold surfaces and robustness diagnostics.

The module treats a model prediction or strategy score as a continuous signal.
For every entry/exit threshold pair it causally builds positions, re-runs the
shared cost-aware backtester, and checks whether performance survives nearby
parameters, rolling windows, and bull/bear/sideways regimes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from superplatform.consumption.backtest import BacktestResult, backtest


@dataclass(frozen=True)
class ThresholdResearchConfig:
    enabled: bool = False
    entry_quantiles: tuple[float, ...] = (0.55, 0.65, 0.75, 0.85, 0.95)
    exit_quantiles: tuple[float, ...] = (0.05, 0.15, 0.25, 0.35, 0.45)
    rolling_window: int = 60
    rolling_step: int = 20
    calibration_fraction: float = 0.25
    min_calibration_periods: int = 20
    min_unique_signal_values: int = 5
    min_neighbor_count: int = 2
    min_neighbor_positive_ratio: float = 0.67
    max_neighbor_return_dispersion: float = 0.20
    max_drawdown_limit: float = 0.25
    min_rolling_positive_ratio: float = 0.60
    min_regime_non_loss_ratio: float = 0.50
    min_stable_region_size: int = 2
    max_candidates: int = 5

    def validate(self) -> None:
        for name, values in (
            ("entry_quantiles", self.entry_quantiles),
            ("exit_quantiles", self.exit_quantiles),
        ):
            if not values or any(value <= 0 or value >= 1 for value in values):
                raise ValueError(f"{name} must contain quantiles in (0, 1)")
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be unique and increasing")
        if max(self.exit_quantiles) >= max(self.entry_quantiles):
            raise ValueError("entry quantiles must extend above exit quantiles")
        if self.rolling_window < 10 or self.rolling_step < 1:
            raise ValueError("rolling_window must be >= 10 and rolling_step positive")
        if not 0.10 <= self.calibration_fraction <= 0.50:
            raise ValueError("calibration_fraction must be in [0.10, 0.50]")
        if self.min_calibration_periods < 10:
            raise ValueError("min_calibration_periods must be at least 10")
        if self.min_unique_signal_values < 3:
            raise ValueError("min_unique_signal_values must be at least 3")
        if self.min_neighbor_count < 1 or self.min_stable_region_size < 1:
            raise ValueError("neighbor and stable-region sizes must be positive")
        for name, value in (
            ("min_neighbor_positive_ratio", self.min_neighbor_positive_ratio),
            ("min_rolling_positive_ratio", self.min_rolling_positive_ratio),
            ("min_regime_non_loss_ratio", self.min_regime_non_loss_ratio),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.max_neighbor_return_dispersion <= 0:
            raise ValueError("max_neighbor_return_dispersion must be positive")
        if not 0 < self.max_drawdown_limit <= 1:
            raise ValueError("max_drawdown_limit must be in (0, 1]")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be positive")


def _clean_scores(scores: pd.Series) -> pd.Series:
    if not isinstance(scores.index, pd.MultiIndex):
        raise ValueError("threshold research scores need a timestamp/symbol MultiIndex")
    if list(scores.index.names) != ["timestamp", "symbol"]:
        raise ValueError("score index names must be timestamp and symbol")
    frame = pd.to_numeric(scores, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    timestamps = pd.to_datetime(
        frame.index.get_level_values("timestamp"), utc=True, errors="coerce"
    )
    valid = ~pd.isna(timestamps)
    frame = frame[valid]
    frame.index = pd.MultiIndex.from_arrays(
        [timestamps[valid], frame.index.get_level_values("symbol")[valid].astype(str)],
        names=["timestamp", "symbol"],
    )
    if frame.index.has_duplicates:
        frame = frame.groupby(level=["timestamp", "symbol"]).last()
    return frame.sort_index().astype(float)


def threshold_positions(
    scores: pd.Series,
    *,
    entry_threshold: float,
    exit_threshold: float,
    allow_short: bool,
    strategy_name: str,
) -> pd.DataFrame:
    """Convert a continuous score into stateful entry/exit target positions."""
    if entry_threshold <= 0 or exit_threshold < 0:
        raise ValueError("entry must be positive and exit must be non-negative")
    if exit_threshold >= entry_threshold:
        raise ValueError("exit_threshold must be below entry_threshold")
    clean = _clean_scores(scores)
    rows: list[dict[str, Any]] = []
    for symbol, group in clean.groupby(level="symbol", sort=True):
        state = 0.0
        values = group.droplevel("symbol")
        for timestamp, value in values.items():
            if state == 0.0:
                if value >= entry_threshold:
                    state = 1.0
                elif allow_short and value <= -entry_threshold:
                    state = -1.0
            elif state > 0.0 and value <= exit_threshold:
                state = 0.0
            elif state < 0.0 and value >= -exit_threshold:
                state = 0.0
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": str(symbol),
                    "position": state,
                }
            )
    result = pd.DataFrame(rows, columns=["timestamp", "symbol", "position"])
    result.attrs["strategy_name"] = strategy_name
    return result


def _returns_from_backtest(result: BacktestResult) -> pd.Series:
    equity = result.equity.copy()
    equity["timestamp"] = pd.to_datetime(equity["timestamp"], utc=True)
    return equity.set_index("timestamp")["equity"].pct_change().dropna()


def _drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = (1.0 + returns).cumprod()
    return abs(float(equity.div(equity.cummax()).sub(1.0).min()))


def _position_turnover(signals: pd.DataFrame) -> tuple[float, float, pd.Series]:
    parts: list[pd.Series] = []
    for _, group in signals.sort_values("timestamp").groupby("symbol", sort=True):
        positions = group.set_index("timestamp")["position"].astype(float)
        changes = positions.diff().abs()
        if not changes.empty:
            changes.iloc[0] = abs(float(positions.iloc[0]))
        parts.append(changes)
    if not parts:
        return 0.0, 0.0, pd.Series(dtype=float)
    by_period = pd.concat(parts, axis=1).fillna(0.0).sum(axis=1).sort_index()
    return float(by_period.mean()), float(by_period.sum()), by_period


def _rolling_metrics(
    returns: pd.Series,
    *,
    window: int,
    step: int,
) -> tuple[list[dict[str, Any]], float, float, float]:
    if len(returns) < 10:
        return [], 0.0, 0.0, 1.0
    effective_window = min(window, len(returns))
    starts = list(range(0, len(returns) - effective_window + 1, step))
    final_start = len(returns) - effective_window
    if final_start not in starts:
        starts.append(final_start)
    rows: list[dict[str, Any]] = []
    for start in starts:
        sample = returns.iloc[start : start + effective_window]
        rows.append(
            {
                "start": sample.index[0].isoformat(),
                "end": sample.index[-1].isoformat(),
                "sample_count": len(sample),
                "total_return": float((1.0 + sample).prod() - 1.0),
                "max_drawdown": _drawdown(sample),
                "win_rate": float((sample > 0).mean()),
            }
        )
    total_returns = np.asarray([row["total_return"] for row in rows], dtype=float)
    positive_ratio = float((total_returns > 0).mean()) if len(total_returns) else 0.0
    dispersion = float(np.std(total_returns)) if len(total_returns) > 1 else 0.0
    worst_drawdown = max((float(row["max_drawdown"]) for row in rows), default=1.0)
    return rows, positive_ratio, dispersion, worst_drawdown


def _regime_metrics(
    returns: pd.Series,
    turnover: pd.Series,
    regime: pd.Series,
) -> tuple[dict[str, dict[str, Any]], float]:
    aligned = pd.concat(
        [returns.rename("return"), turnover.rename("turnover"), regime.rename("regime")],
        axis=1,
        join="inner",
    ).dropna(subset=["return", "regime"])
    rows: dict[str, dict[str, Any]] = {}
    for name in ("bull", "bear", "sideways"):
        sample = aligned[aligned["regime"].astype(str).eq(name)]
        values = sample["return"].astype(float)
        rows[name] = {
            "sample_count": len(values),
            "total_return": float((1.0 + values).prod() - 1.0) if len(values) else 0.0,
            "max_drawdown": _drawdown(values),
            "win_rate": float((values > 0).mean()) if len(values) else 0.0,
            "average_turnover": (
                float(sample["turnover"].fillna(0.0).mean()) if len(sample) else 0.0
            ),
        }
    active = [row for row in rows.values() if row["sample_count"] > 0]
    non_loss_ratio = (
        float(np.mean([float(row["total_return"]) >= 0.0 for row in active]))
        if active
        else 0.0
    )
    return rows, non_loss_ratio


def _diagnostic_score(point: dict[str, Any], config: ThresholdResearchConfig) -> float:
    total_return = float(point["total_return"])
    max_drawdown = float(point["max_drawdown"])
    average_turnover = float(point["average_turnover"])
    win_rate = float(point["win_rate"])
    return_component = 25.0 * float(np.clip((total_return + 0.05) / 0.25, 0.0, 1.0))
    drawdown_component = 20.0 * float(
        np.clip(1.0 - max_drawdown / config.max_drawdown_limit, 0.0, 1.0)
    )
    turnover_component = 10.0 * float(np.clip(1.0 - average_turnover, 0.0, 1.0))
    return float(
        return_component
        + drawdown_component
        + turnover_component
        + 10.0 * np.clip(win_rate, 0.0, 1.0)
        + 20.0 * float(point["rolling_positive_ratio"])
        + 15.0 * float(point["regime_non_loss_ratio"])
    )


def _stable_regions(
    points: list[dict[str, Any]],
    *,
    config: ThresholdResearchConfig,
) -> list[dict[str, Any]]:
    by_coordinate = {
        (int(point["entry_index"]), int(point["exit_index"])): point
        for point in points
    }
    preliminary: set[tuple[int, int]] = set()
    for coordinate, point in by_coordinate.items():
        entry_index, exit_index = coordinate
        neighbors = [
            candidate
            for (other_entry, other_exit), candidate in by_coordinate.items()
            if (other_entry, other_exit) != coordinate
            and abs(other_entry - entry_index) <= 1
            and abs(other_exit - exit_index) <= 1
        ]
        neighbor_returns = np.asarray(
            [float(candidate["total_return"]) for candidate in neighbors], dtype=float
        )
        positive_ratio = (
            float((neighbor_returns > 0.0).mean()) if len(neighbor_returns) else 0.0
        )
        dispersion = (
            float(np.std(neighbor_returns)) if len(neighbor_returns) > 1 else 0.0
        )
        point["neighbor_count"] = len(neighbors)
        point["neighbor_positive_ratio"] = positive_ratio
        point["neighbor_return_dispersion"] = dispersion
        point["diagnostic_score"] = round(_diagnostic_score(point, config), 4)
        if (
            float(point["total_return"]) > 0.0
            and float(point["max_drawdown"]) <= config.max_drawdown_limit
            and float(point["rolling_positive_ratio"])
            >= config.min_rolling_positive_ratio
            and float(point["regime_non_loss_ratio"])
            >= config.min_regime_non_loss_ratio
            and len(neighbors) >= config.min_neighbor_count
            and positive_ratio >= config.min_neighbor_positive_ratio
            and dispersion <= config.max_neighbor_return_dispersion
        ):
            preliminary.add(coordinate)

    regions: list[dict[str, Any]] = []
    remaining = set(preliminary)
    stable_coordinates: set[tuple[int, int]] = set()
    while remaining:
        seed = remaining.pop()
        component = {seed}
        queue = [seed]
        while queue:
            entry_index, exit_index = queue.pop()
            adjacent = {
                (entry_index - 1, exit_index),
                (entry_index + 1, exit_index),
                (entry_index, exit_index - 1),
                (entry_index, exit_index + 1),
            }.intersection(remaining)
            remaining.difference_update(adjacent)
            component.update(adjacent)
            queue.extend(adjacent)
        if len(component) < config.min_stable_region_size:
            continue
        stable_coordinates.update(component)
        members = [by_coordinate[coordinate] for coordinate in sorted(component)]
        best = max(members, key=lambda point: float(point["diagnostic_score"]))
        regions.append(
            {
                "size": len(members),
                "entry_threshold_min": min(float(row["entry_threshold"]) for row in members),
                "entry_threshold_max": max(float(row["entry_threshold"]) for row in members),
                "exit_threshold_min": min(float(row["exit_threshold"]) for row in members),
                "exit_threshold_max": max(float(row["exit_threshold"]) for row in members),
                "average_return": float(
                    np.mean([float(row["total_return"]) for row in members])
                ),
                "worst_drawdown": max(float(row["max_drawdown"]) for row in members),
                "best_point": {
                    "entry_threshold": best["entry_threshold"],
                    "exit_threshold": best["exit_threshold"],
                    "diagnostic_score": best["diagnostic_score"],
                },
            }
        )
    for coordinate, point in by_coordinate.items():
        point["stable"] = coordinate in stable_coordinates
    return sorted(regions, key=lambda row: (-int(row["size"]), -float(row["average_return"])))


def run_threshold_research(
    scores: pd.Series,
    *,
    price_data: dict[str, pd.DataFrame],
    regime: pd.Series,
    strategy_name: str,
    config: ThresholdResearchConfig | None = None,
    allow_short: bool = False,
    periods_per_year: int = 365,
    taker_fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> dict[str, Any]:
    """Run a cost-aware 2-D threshold surface and identify stable regions."""
    config = config or ThresholdResearchConfig(enabled=True)
    config.validate()
    clean = _clean_scores(scores)
    timestamps = clean.index.get_level_values("timestamp").unique().sort_values()
    min_total_periods = config.min_calibration_periods + 10
    if len(timestamps) < min_total_periods:
        return {
            "strategy": strategy_name,
            "enabled": config.enabled,
            "status": "insufficient_history",
            "message": (
                "Threshold research needs a separate calibration window and at least "
                "10 later evaluation periods."
            ),
            "signal_rows": len(clean),
            "unique_absolute_signal_values": 0,
            "entry_thresholds": [],
            "exit_thresholds": [],
            "surface": [],
            "stable_regions": [],
            "recommended_point": None,
        }
    calibration_periods = max(
        config.min_calibration_periods,
        int(np.ceil(len(timestamps) * config.calibration_fraction)),
    )
    calibration_periods = min(calibration_periods, len(timestamps) - 10)
    calibration_cutoff = timestamps[calibration_periods - 1]
    calibration = clean[
        clean.index.get_level_values("timestamp") <= calibration_cutoff
    ]
    evaluation = clean[
        clean.index.get_level_values("timestamp") > calibration_cutoff
    ]
    non_zero = calibration.abs()[calibration.abs() > 0.0]
    unique_values = int(non_zero.round(12).nunique())
    base = {
        "strategy": strategy_name,
        "enabled": config.enabled,
        "signal_rows": len(clean),
        "unique_absolute_signal_values": unique_values,
        "calibration": {
            "periods": calibration_periods,
            "end": pd.Timestamp(calibration_cutoff).isoformat(),
            "fraction": config.calibration_fraction,
        },
        "evaluation_periods": int(
            evaluation.index.get_level_values("timestamp").nunique()
        ),
    }
    if clean.empty or len(non_zero) < 10 or unique_values < config.min_unique_signal_values:
        return {
            **base,
            "status": "insufficient_signal_resolution",
            "message": (
                "The strategy exposes too few continuous signal levels for a reliable "
                "entry/exit threshold surface."
            ),
            "entry_thresholds": [],
            "exit_thresholds": [],
            "surface": [],
            "stable_regions": [],
            "recommended_point": None,
        }

    entry_thresholds = sorted(
        {
            float(non_zero.quantile(quantile))
            for quantile in config.entry_quantiles
        }
    )
    exit_thresholds = sorted(
        {
            max(0.0, float(non_zero.quantile(quantile)))
            for quantile in config.exit_quantiles
        }
    )
    surface: list[dict[str, Any]] = []
    for entry_index, entry_threshold in enumerate(entry_thresholds):
        for exit_index, exit_threshold in enumerate(exit_thresholds):
            if exit_threshold >= entry_threshold:
                continue
            signals = threshold_positions(
                evaluation,
                entry_threshold=entry_threshold,
                exit_threshold=exit_threshold,
                allow_short=allow_short,
                strategy_name=f"{strategy_name}:threshold",
            )
            result = backtest(
                signals,
                price_data=price_data,
                periods_per_year=periods_per_year,
                taker_fee_bps=taker_fee_bps,
                slippage_bps=slippage_bps,
            )
            returns = _returns_from_backtest(result)
            average_turnover, total_turnover, turnover = _position_turnover(signals)
            rolling, rolling_positive, rolling_dispersion, rolling_worst_drawdown = (
                _rolling_metrics(
                    returns,
                    window=config.rolling_window,
                    step=config.rolling_step,
                )
            )
            regime_rows, regime_non_loss = _regime_metrics(returns, turnover, regime)
            surface.append(
                {
                    "entry_index": entry_index,
                    "exit_index": exit_index,
                    "entry_threshold": entry_threshold,
                    "exit_threshold": exit_threshold,
                    "total_return": float(result.total_return),
                    "annual_return": float(result.annual_return),
                    "sharpe": float(result.sharpe),
                    "max_drawdown": abs(float(result.max_drawdown)),
                    "win_rate": float(result.win_rate),
                    "average_turnover": average_turnover,
                    "total_turnover": total_turnover,
                    "rolling_positive_ratio": rolling_positive,
                    "rolling_return_dispersion": rolling_dispersion,
                    "rolling_worst_drawdown": rolling_worst_drawdown,
                    "rolling_windows": rolling,
                    "regime_non_loss_ratio": regime_non_loss,
                    "regime_metrics": regime_rows,
                }
            )
    regions = _stable_regions(surface, config=config)
    recommended = None
    stable_points = [point for point in surface if point.get("stable")]
    if stable_points:
        best = max(stable_points, key=lambda point: float(point["diagnostic_score"]))
        recommended = {
            key: best[key]
            for key in (
                "entry_threshold",
                "exit_threshold",
                "total_return",
                "max_drawdown",
                "win_rate",
                "average_turnover",
                "rolling_positive_ratio",
                "regime_non_loss_ratio",
                "diagnostic_score",
            )
        }
    return {
        **base,
        "status": "completed",
        "message": (
            "Stable regions are contiguous parameter areas that pass neighborhood, "
            "rolling-window, regime, and drawdown gates."
        ),
        "entry_thresholds": entry_thresholds,
        "exit_thresholds": exit_thresholds,
        "surface": surface,
        "stable_regions": regions,
        "recommended_point": recommended,
        "cost_assumptions": {
            "taker_fee_bps": taker_fee_bps,
            "slippage_bps": slippage_bps,
        },
        "stability_contract": {
            "min_neighbor_positive_ratio": config.min_neighbor_positive_ratio,
            "max_neighbor_return_dispersion": config.max_neighbor_return_dispersion,
            "max_drawdown_limit": config.max_drawdown_limit,
            "min_rolling_positive_ratio": config.min_rolling_positive_ratio,
            "min_regime_non_loss_ratio": config.min_regime_non_loss_ratio,
            "min_stable_region_size": config.min_stable_region_size,
        },
    }
