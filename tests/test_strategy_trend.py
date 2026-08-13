"""Unit tests for the multi-horizon trend-following strategies.

White-box tests of the scoring / hysteresis core plus end-to-end checks of the
registered strategies (delisted-symbol skip, pandera schema, long-only). The
strategies reference fixed-param factor instances (momentum_60d, momentum_120d,
realized_vol_20d) provided by the instance layer. No network or data cache
needed — factor results are hand-built FactorResults.
"""

import math

import numpy as np
import pandas as pd
import pytest

from superplatform.factors.base import FactorCategory, FactorResult
from superplatform.strategy.defs.trend_strategies import _score, _trend_positions
from superplatform.strategy.registry import StrategyRegistry


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n)


def _score_df(scores: list) -> pd.DataFrame:
    return pd.DataFrame({"timestamp": _idx(len(scores)), "score": scores})


def _factor_results(symbol_data: dict) -> dict:
    """symbol_data: {sym: {"m60": seq, "m120": seq, "vol": seq}} → factor_results."""
    n = max(len(v["m60"]) for v in symbol_data.values())
    idx = _idx(n)
    factors = {}
    for fname, key in [
        ("momentum_60d", "m60"),
        ("momentum_120d", "m120"),
        ("realized_vol_20d", "vol"),
    ]:
        factors[fname] = {
            sym: FactorResult(
                name=fname,
                category=FactorCategory.MOMENTUM_REVERSAL,
                values=pd.DataFrame({"timestamp": idx, "value": list(vals[key])}),
                metadata={},
            )
            for sym, vals in symbol_data.items()
        }
    return factors


# ── Volatility normalization ─────────────────────────────────────────


def test_score_normalizes_by_volatility():
    # Identical raw momentum, different realized vol → the lower-vol symbol
    # has the higher (normalized) score.
    fr = _factor_results({
        "A": {"m60": [np.nan, 0.1, 0.1, 0.1], "m120": [np.nan, 0.1, 0.1, 0.1], "vol": [0.5] * 4},
        "B": {"m60": [np.nan, 0.1, 0.1, 0.1], "m120": [np.nan, 0.1, 0.1, 0.1], "vol": [1.0] * 4},
    })
    score_a = _score(fr, "A")["score"].dropna().iloc[0]
    score_b = _score(fr, "B")["score"].dropna().iloc[0]
    assert score_a > score_b
    # Spot-check the exact formula: z = ret / (vol_annual * sqrt(n/365)).
    z60 = 0.1 / (0.5 * math.sqrt(60 / 365))
    z120 = 0.1 / (0.5 * math.sqrt(120 / 365))
    assert score_a == pytest.approx((z60 + z120) / 2)


# ── Hysteresis state machine ─────────────────────────────────────────


def test_hysteresis_holds_position_through_dips():
    # Enters at 0.6, dips to 0.1 (still > exit=0), recovers → no flip.
    pos = _trend_positions(_score_df([0.0, 0.6, 0.6, 0.2, 0.1, 0.6]), "both", 0.4, 0.0)
    assert list(pos) == [0, 1, 1, 1, 1, 1]


def test_exit_on_full_reversal_then_short():
    pos = _trend_positions(_score_df([0.0, 0.6, 0.6, 0.0, -0.2, -0.5, 0.3]), "both", 0.4, 0.0)
    # t0 flat; t1-2 long; t3 score≤0 → flat; t4 flat; t5 ≤-0.4 → short;
    # t6 short exits when score≥0 → flat (0.3 < enter so no re-entry).
    assert list(pos) == [0, 1, 1, 0, 0, -1, 0]


def test_short_side_is_symmetric():
    pos = _trend_positions(_score_df([0.0, -0.6, -0.6, 0.1, 0.6]), "both", 0.4, 0.0)
    assert list(pos) == [0, -1, -1, 0, 1]


def test_warmup_nan_is_flat():
    pos = _trend_positions(_score_df([np.nan, np.nan, 0.6, 0.6]), "both", 0.4, 0.0)
    assert list(pos) == [0, 0, 1, 1]


def test_long_only_never_shorts():
    pos = _trend_positions(_score_df([0.0, 0.6, -0.6, 0.6]), "long", 0.4, 0.0)
    assert list(pos) == [0, 1, 0, 1]
    assert all(p >= 0 for p in pos)


# ── Strategy-level integration (registry) ─────────────────────────────


def test_symbol_missing_from_one_factor_is_skipped():
    fr = _factor_results({
        "A": {"m60": [np.nan, 0.2, 0.2], "m120": [np.nan, 0.2, 0.2], "vol": [0.5] * 3},
        "B": {"m60": [np.nan, 0.2, 0.2], "m120": [np.nan, 0.2, 0.2], "vol": [0.5] * 3},
    })
    del fr["momentum_120d"]["B"]  # delisted symbol: no 120d data

    strat = StrategyRegistry.get_instance().get("multi_horizon_trend")
    sig = strat.generate_signals(fr).positions

    assert "B" not in set(sig["symbol"])
    assert "A" in set(sig["symbol"])


def test_positions_schema_and_values_via_registry():
    fr = _factor_results({
        "A": {"m60": [np.nan, 0.2, 0.3, 0.4], "m120": [np.nan, 0.2, 0.3, 0.4], "vol": [0.5] * 4},
    })
    strat = StrategyRegistry.get_instance().get("multi_horizon_trend")

    signal = strat.generate_signals(fr)

    df = signal.positions
    assert list(df.columns) == ["timestamp", "symbol", "position"]
    assert set(df["position"].unique()) <= {-1, 0, 1}


def test_long_only_strategy_never_shorts():
    fr = _factor_results({
        "A": {"m60": [np.nan, -0.2, -0.2, -0.2], "m120": [np.nan, -0.2, -0.2, -0.2], "vol": [0.5] * 4},
    })
    strat = StrategyRegistry.get_instance().get("multi_horizon_trend_long_only")
    sig = strat.generate_signals(fr).positions
    assert (sig["position"] >= 0).all()
