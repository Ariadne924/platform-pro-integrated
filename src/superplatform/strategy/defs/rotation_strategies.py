"""Cross-sectional momentum rotation, long-only.

Core idea: in crypto, the more robust and better-documented retail edge is
momentum *within* a pool rather than timing the index. Every month we rank the
research pool by 60-day momentum normalized by realized volatility — so a 60%
move in BTC and a 150% move in a meme coin are comparable z-scores — and hold
the top-N names equal-weight. The book is long-only: on the real 2021–2025
sample, shorting crypto destroys value (violent mean reversion off crash lows,
plus positive funding shorts pay in bull regimes, which the backtest does not
model).

Two guard rails keep it real:

1. Low turnover — the rank set is re-evaluated every ``rebalance_days`` trading
   days (default 21 ≈ monthly), not daily. Daily re-ranking under the 7 bps/side
   cost — the killer that sank equal_weight_combo — turns a ~1%/yr selection
   edge into a ~6%/yr cost drag. Monthly rebalancing keeps the drag near zero
   while preserving most of the selection gain.

2. A deep-bear circuit breaker — when the pool's mean 120-day momentum is below
   ``bear_m120`` (a confirmed broad bear, e.g. most of 2022), the book flattens
   *immediately* and stays out until momentum recovers. The gate is evaluated
   every day, not just at rebalances, so a mid-cycle crash cannot be ridden
   down. It is deliberately deep (a plain index-vs-MA gate that also sits out
   bull-market dips costs more in missed rallies than it saves in drawdown).

Position semantics: the selected set is emitted at weight 1.0; the backtest
engine equal-weights whatever is active (Σ|w| ≤ 100%). Symbols missing from any
factor (delisted / not yet listed) are excluded from the rank each period.

Honest benchmark: on real 2021-01..2025-06 Binance perp data this does NOT beat
an equal-weight buy-and-hold index (Sharpe ≈ 0.73, +450%) — the window is a
secular bull and survivorship inflates the pool — but it is the strongest
factor-based alternative found (~+260% net, Sharpe ≈ 0.5, max DD ≈ -65%),
comfortably ahead of equal_weight_combo (Sharpe ≈ 0.18, max DD ≈ -88%).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from superplatform.strategy.base import strategy

_TOP_N = 10
_REBALANCE_DAYS = 21
_BEAR_M120 = -0.15


def _panel(factor_results, factor_name, symbols):
    """timestamp × symbol panel of one factor's values (outer join on time).

    Symbols with different listing dates produce NaN for rows before they
    exist; warm-up rows and delisted periods are excluded from ranking via
    NaN.
    """
    series = {}
    stamps = []
    for s in symbols:
        df = factor_results[factor_name][s].values[["timestamp", "value"]]
        series[s] = df
        stamps.append(df["timestamp"].to_numpy())
    grid = pd.Index(sorted(np.unique(np.concatenate(stamps))))
    panel = pd.DataFrame(index=grid)
    for s in symbols:
        panel[s] = series[s].set_index("timestamp")["value"].reindex(grid)
    return panel


def _build_scores(factor_results, symbols):
    """z60 panel (vol-normalized 60d momentum) + pool mean 120d momentum."""
    m60 = _panel(factor_results, "momentum_60d", symbols)
    m120 = _panel(factor_results, "momentum_120d", symbols)
    vol = _panel(factor_results, "realized_vol_20d", symbols)
    # realized_vol_20d is annualized (×√365); scale to the 60-day horizon.
    vol60 = vol * np.sqrt(60.0 / 365.0)
    z60 = m60 / vol60.replace(0, np.nan)
    mean_m120 = m120.mean(axis=1, skipna=True)
    return z60, mean_m120


def _positions(z60, mean_m120, top_n, rebalance_days, bear_m120):
    """Monthly rebalanced top-N selection + daily deep-bear gate → positions."""
    n = len(z60)
    pos = pd.DataFrame(0.0, index=z60.index, columns=z60.columns)
    ranks = z60.rank(axis=1, ascending=False)
    selected = (ranks <= top_n) & z60.notna()
    for i in range(0, n, rebalance_days):
        j = min(i + rebalance_days, n)
        pos.iloc[i:j] = selected.iloc[i].astype(float).to_numpy()
    # Deep-bear circuit breaker acts every day, not just at rebalances.
    pos.loc[mean_m120 < bear_m120] = 0.0
    return pos


def _signals(z60, pos):
    frames = []
    for s in z60.columns:
        valid = z60[s].notna()
        frames.append(pd.DataFrame({
            "timestamp": z60.index[valid],
            "symbol": s,
            "position": pos[s][valid].to_numpy(),
        }))
    return pd.concat(frames, ignore_index=True)


# Restored (2026-08): the factor instance layer provides momentum_60d /
# momentum_120d / realized_vol_20d instances, so the rotation strategy's
# dual-horizon factors are expressible again.
@strategy(
    name="momentum_rotation",
    description="long-only 截面动量轮动：月度按波动率归一的 60d 动量持有 top-10，均值 120d 动量<-15% 即时空仓",
    used_factors=["momentum_60d", "momentum_120d", "realized_vol_20d"],
)
def momentum_rotation(factor_results, **params):
    top_n = int(params.get("top_n", _TOP_N))
    rebalance_days = int(params.get("rebalance_days", _REBALANCE_DAYS))
    bear_m120 = float(params.get("bear_m120", _BEAR_M120))

    symbols = [
        s for s in factor_results["momentum_60d"]
        if s in factor_results["momentum_120d"]
        and s in factor_results["realized_vol_20d"]
    ]
    z60, mean_m120 = _build_scores(factor_results, symbols)
    pos = _positions(z60, mean_m120, top_n, rebalance_days, bear_m120)
    return _signals(z60, pos)
