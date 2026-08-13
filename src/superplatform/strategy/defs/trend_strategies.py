"""Multi-horizon time-series trend-following strategies.

Core idea (a classic CTA approach): a symbol is in a trend only when its recent
returns at multiple horizons are strong *after* normalizing by its own realized
volatility — i.e. the move is large relative to the noise. We use 60-day and
120-day momentum; dividing each by its horizon-scaled volatility turns a raw
return into a z-score, so the same threshold is stationary across a pool whose
volatility ranges from BTC (~60%/yr) to meme coins (~150%/yr).

A hysteresis state machine keeps turnover low: we only enter at ±enter σ and
only exit when the trend has fully reversed back to 0.

REAL-DATA NOTE (2026-08): on real 2021-01..2025-06 Binance perp data this
design does NOT work — multi_horizon_trend nets ≈ -46% (Sharpe ≈ -0.2) and even
the long-only variant nets ≈ -33%. The short side is the main value destroyer:
crypto's sharp crash reversals run over shorts, and the 60/120-day lookbacks lag
tops badly. This module is kept as a documented negative result; the working
long-only cross-sectional alternative is ``momentum_rotation`` (rotation_
strategies.py). The P&L also does not include funding, which makes the backtest
optimistic about shorts.
"""

import numpy as np
import pandas as pd

from superplatform.strategy.base import strategy

# Entry / exit thresholds in volatility-normalized score units (σ).
_ENTER_SIGMA = 0.4
_EXIT_SIGMA = 0.0


def _score(factor_results, symbol):
    """Vol-normalized composite trend score (σ units) for one symbol.

    Returns a DataFrame (timestamp, score) aligned to momentum_60d's grid.
    score = mean(z60, z120), where zN is the N-day return divided by the
    corresponding N-day volatility derived from the annualized 20d realized vol.
    """
    m60 = factor_results["momentum_60d"][symbol].values
    m120 = factor_results["momentum_120d"][symbol].values
    vol = factor_results["realized_vol_20d"][symbol].values

    df = m60[["timestamp", "value"]].rename(columns={"value": "m60"})
    df = df.merge(
        m120[["timestamp", "value"]].rename(columns={"value": "m120"}),
        on="timestamp", how="left",
    )
    df = df.merge(
        vol[["timestamp", "value"]].rename(columns={"value": "vol"}),
        on="timestamp", how="left",
    )
    # realized_vol_20d is annualized (×√365); scale to the horizon's vol.
    vol60 = df["vol"] * np.sqrt(60.0 / 365.0)
    vol120 = df["vol"] * np.sqrt(120.0 / 365.0)
    z60 = df["m60"] / vol60.replace(0, np.nan)
    z120 = df["m120"] / vol120.replace(0, np.nan)
    out = df[["timestamp"]].copy()
    out["score"] = (z60 + z120) / 2.0
    return out


def _trend_positions(score_df, direction, enter, exit):
    """Hysteresis state machine → per-day position in {-1, 0, 1}.

    flat → long when score ≥ enter; flat → short when score ≤ -enter (both
    directions only); long exits when score ≤ exit; short exits when score
    ≥ -exit. NaN (warm-up / missing data) is treated as 0 → flat.
    """
    positions = np.zeros(len(score_df))
    state = 0
    scores = score_df["score"].fillna(0.0).to_numpy()
    for i, s in enumerate(scores):
        if state == 0:
            if s >= enter:
                state = 1 if direction in ("both", "long") else -1
            elif direction == "both" and s <= -enter:
                state = -1
        elif state == 1:
            if s <= exit:
                state = 0
        elif state == -1:
            if s >= -exit:
                state = 0
        positions[i] = state
    return positions


def _trend_signals(factor_results, direction, enter, exit):
    frames = []
    for symbol in factor_results["momentum_60d"]:
        # Skip symbols missing from any factor (delisted / no data).
        if symbol not in factor_results["momentum_120d"]:
            continue
        if symbol not in factor_results["realized_vol_20d"]:
            continue
        score_df = _score(factor_results, symbol)
        out = score_df[["timestamp"]].copy()
        out["symbol"] = symbol
        out["position"] = _trend_positions(score_df, direction, enter, exit)
        frames.append(out)
    return pd.concat(frames, ignore_index=True)


# Restored (2026-08): the factor instance layer provides fixed-param instances
# momentum_60d / momentum_120d / realized_vol_20d, so the dual-horizon design
# is expressible again (strategies reference instances only).
@strategy(
    name="multi_horizon_trend",
    description="60/120日双动量趋势跟踪（波动率归一+迟滞带），双向做多做空",
    used_factors=["momentum_60d", "momentum_120d", "realized_vol_20d"],
)
def multi_horizon_trend(factor_results, **params):
    return _trend_signals(
        factor_results,
        direction="both",
        enter=params.get("enter", _ENTER_SIGMA),
        exit=params.get("exit", _EXIT_SIGMA),
    )


@strategy(
    name="multi_horizon_trend_long_only",
    description="60/120日双动量趋势跟踪，但只做多（对照：量化做空贡献）",
    used_factors=["momentum_60d", "momentum_120d", "realized_vol_20d"],
)
def multi_horizon_trend_long_only(factor_results, **params):
    return _trend_signals(
        factor_results,
        direction="long",
        enter=params.get("enter", _ENTER_SIGMA),
        exit=params.get("exit", _EXIT_SIGMA),
    )
