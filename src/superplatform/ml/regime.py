"""Causal, explainable bull/bear/sideways market-regime detection."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RegimeConfig:
    fast_window: int = 20
    slow_window: int = 60
    volatility_window: int = 20
    trend_threshold: float = 0.03
    bear_drawdown: float = 0.15
    confirmation_periods: int = 3

    def validate(self) -> None:
        if self.fast_window < 2 or self.slow_window <= self.fast_window:
            raise ValueError("slow_window must be greater than fast_window >= 2")
        if self.volatility_window < 2 or self.confirmation_periods < 1:
            raise ValueError("volatility_window and confirmation_periods must be positive")
        if self.trend_threshold <= 0 or not 0 < self.bear_drawdown < 1:
            raise ValueError("trend_threshold and bear_drawdown must be in valid ranges")


def _confirmed_state(raw: pd.Series, confirmation_periods: int) -> pd.Series:
    state = "sideways"
    candidate = state
    count = 0
    output: list[str] = []
    for value in raw.astype(str):
        if value == state:
            candidate, count = state, 0
        elif value == candidate:
            count += 1
        else:
            candidate, count = value, 1
        if count >= confirmation_periods:
            state, count = candidate, 0
        output.append(state)
    return pd.Series(output, index=raw.index, dtype="string")


def detect_market_regime(
    close: pd.Series,
    *,
    config: RegimeConfig | None = None,
) -> pd.DataFrame:
    """Classify each timestamp using only information available at that time."""
    config = config or RegimeConfig()
    config.validate()
    if not isinstance(close.index, pd.DatetimeIndex):
        raise TypeError("close must use a DatetimeIndex")
    if close.index.tz is None or str(close.index.tz).upper() not in {"UTC", "ETC/UTC"}:
        raise ValueError("close index must be timezone-aware UTC")
    values = pd.to_numeric(close, errors="coerce").sort_index()
    if values.index.has_duplicates:
        raise ValueError("close index must not contain duplicates")

    fast = values.rolling(config.fast_window, min_periods=config.fast_window).mean()
    slow = values.rolling(config.slow_window, min_periods=config.slow_window).mean()
    trend = values.div(slow).sub(1.0)
    drawdown = values.div(values.cummax()).sub(1.0)
    volatility = values.pct_change().rolling(
        config.volatility_window,
        min_periods=config.volatility_window,
    ).std(ddof=1)

    raw = pd.Series("sideways", index=values.index, dtype="string")
    bear = (trend <= -config.trend_threshold) | (drawdown <= -config.bear_drawdown)
    bull = (
        (trend >= config.trend_threshold)
        & (fast > slow)
        & ~bear
    )
    raw.loc[bear.fillna(False)] = "bear"
    raw.loc[bull.fillna(False)] = "bull"
    state = _confirmed_state(raw, config.confirmation_periods)
    confidence = np.maximum(
        trend.abs().div(config.trend_threshold),
        drawdown.abs().div(config.bear_drawdown),
    ).clip(0.0, 1.0)
    confidence = confidence.fillna(0.0)
    confidence = confidence.where(state.ne("sideways"), confidence * 0.5)
    return pd.DataFrame(
        {
            "close": values,
            "fast_ma": fast,
            "slow_ma": slow,
            "trend": trend,
            "drawdown": drawdown,
            "volatility": volatility,
            "raw_regime": raw,
            "regime": state,
            "confidence": confidence.astype(float),
        },
        index=values.index,
    )
