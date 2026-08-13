"""Unit tests for the long-only cross-sectional momentum rotation strategy.

White-box tests of the top-N selection / monthly rebalance / deep-bear gate
core plus end-to-end checks of the registered strategy (schema, long-only,
missing-symbol skip, param overrides). The strategy references fixed-param
factor instances (momentum_60d, momentum_120d, realized_vol_20d) provided by
the instance layer. No network or data cache needed.
"""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, FactorResult
from superplatform.strategy.defs.rotation_strategies import _positions
from superplatform.strategy.registry import StrategyRegistry


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n)


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


def _z60(values: dict) -> pd.DataFrame:
    return pd.DataFrame(values, index=_idx(len(next(iter(values.values())))))


def test_positions_selects_top_n_and_holds():
    z60 = _z60({"A": [0.3] * 30, "B": [0.2] * 30, "C": [0.1] * 30})
    mean_m120 = pd.Series(0.3, index=z60.index)
    pos = _positions(z60, mean_m120, top_n=2, rebalance_days=30, bear_m120=-0.15)
    assert (pos["A"] == 1.0).all()
    assert (pos["B"] == 1.0).all()
    assert (pos["C"] == 0.0).all()


def test_positions_rebalances_when_rank_leadership_changes():
    z60 = _z60({"A": [0.3] * 10 + [0.1] * 20, "B": [0.1] * 10 + [0.3] * 20})
    mean_m120 = pd.Series(0.3, index=z60.index)
    pos = _positions(z60, mean_m120, top_n=1, rebalance_days=10, bear_m120=-0.15)
    assert list(pos["A"][:10]) == [1.0] * 10
    assert list(pos["A"][10:]) == [0.0] * 20
    assert list(pos["B"][:10]) == [0.0] * 10
    assert list(pos["B"][10:]) == [1.0] * 20


def test_positions_bear_gate_flattens_mid_cycle_immediately():
    # A selected at day 0; pool mean m120 turns deeply negative at day 15 →
    # the gate must flatten the SAME day, not at the next rebalance.
    z60 = _z60({"A": [0.3] * 30, "B": [0.1] * 30})
    mean_m120 = pd.Series([0.3] * 15 + [-0.5] * 15, index=z60.index)
    pos = _positions(z60, mean_m120, top_n=1, rebalance_days=30, bear_m120=-0.15)
    assert list(pos["A"][:15]) == [1.0] * 15
    assert list(pos["A"][15:]) == [0.0] * 15


def test_positions_warmup_nan_excluded_from_selection():
    z60 = _z60({
        "A": [0.3] * 30,
        "B": [0.2] * 30,
        "C": [np.nan] * 8 + [0.1] * 22,
    })
    mean_m120 = pd.Series(0.3, index=z60.index)
    pos = _positions(z60, mean_m120, top_n=2, rebalance_days=30, bear_m120=-0.15)
    assert (pos["A"] == 1.0).all()
    assert (pos["B"] == 1.0).all()
    assert (pos["C"] == 0.0).all()  # never cracks top-2


def test_positions_returns_zero_for_empty_pool():
    z60 = pd.DataFrame(np.nan, index=_idx(30), columns=["A", "B"])
    mean_m120 = pd.Series(0.3, index=z60.index)
    pos = _positions(z60, mean_m120, top_n=2, rebalance_days=10, bear_m120=-0.15)
    assert (pos == 0.0).all().all()


# ── Strategy-level integration (registry) ─────────────────────────────


def test_selects_top_n_and_holds():
    fr = _factor_results({
        "A": {"m60": [0.3] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
        "B": {"m60": [0.2] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
        "C": {"m60": [0.1] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
    })
    strat = StrategyRegistry.get_instance().get("momentum_rotation")
    sig = strat.generate_signals(fr, top_n=2).positions

    pos_a = sig[sig["symbol"] == "A"].set_index("timestamp")["position"]
    pos_b = sig[sig["symbol"] == "B"].set_index("timestamp")["position"]
    pos_c = sig[sig["symbol"] == "C"].set_index("timestamp")["position"]
    assert (pos_a == 1.0).all() and (pos_b == 1.0).all() and (pos_c == 0.0).all()


def test_rebalances_when_rank_leadership_changes():
    m60_a = [0.3] * 10 + [0.1] * 20
    m60_b = [0.1] * 10 + [0.3] * 20
    fr = _factor_results({
        "A": {"m60": m60_a, "m120": [0.3] * 30, "vol": [0.5] * 30},
        "B": {"m60": m60_b, "m120": [0.3] * 30, "vol": [0.5] * 30},
    })
    strat = StrategyRegistry.get_instance().get("momentum_rotation")
    sig = strat.generate_signals(fr, rebalance_days=10, top_n=1).positions

    pos_a = sig[sig["symbol"] == "A"]["position"].to_numpy()
    pos_b = sig[sig["symbol"] == "B"]["position"].to_numpy()
    assert list(pos_a[:10]) == [1.0] * 10
    assert list(pos_a[10:]) == [0.0] * 20
    assert list(pos_b[:10]) == [0.0] * 10
    assert list(pos_b[10:]) == [1.0] * 20


def test_bear_gate_flattens_mid_cycle_immediately():
    m120 = [0.3] * 15 + [-0.5] * 15
    fr = _factor_results({
        "A": {"m60": [0.3] * 30, "m120": m120, "vol": [0.5] * 30},
        "B": {"m60": [0.1] * 30, "m120": m120, "vol": [0.5] * 30},
    })
    strat = StrategyRegistry.get_instance().get("momentum_rotation")
    sig = strat.generate_signals(fr, rebalance_days=20, top_n=1).positions

    pos_a = sig[sig["symbol"] == "A"]["position"].to_numpy()
    assert list(pos_a[:15]) == [1.0] * 15
    assert list(pos_a[15:]) == [0.0] * 15


def test_warmup_nan_excluded_from_selection():
    fr = _factor_results({
        "A": {"m60": [0.3] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
        "B": {"m60": [0.2] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
        "C": {"m60": [np.nan] * 8 + [0.1] * 22, "m120": [0.3] * 30, "vol": [0.5] * 30},
    })
    strat = StrategyRegistry.get_instance().get("momentum_rotation")
    sig = strat.generate_signals(fr, top_n=2).positions

    sig_c = sig[sig["symbol"] == "C"]
    assert len(sig_c) == 22
    assert (sig_c["position"] == 0.0).all()


def test_long_only_never_shorts():
    fr = _factor_results({
        "A": {"m60": [-0.3] * 30, "m120": [-0.3] * 30, "vol": [0.5] * 30},
        "B": {"m60": [-0.1] * 30, "m120": [-0.3] * 30, "vol": [0.5] * 30},
    })
    strat = StrategyRegistry.get_instance().get("momentum_rotation")
    sig = strat.generate_signals(fr, top_n=1).positions
    assert (sig["position"] >= 0).all()
    assert set(sig["position"].unique()) <= {0.0, 1.0}


def test_symbol_missing_from_one_factor_is_skipped():
    fr = _factor_results({
        "A": {"m60": [0.3] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
        "B": {"m60": [0.2] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
    })
    del fr["momentum_120d"]["B"]  # delisted symbol: no 120d data

    strat = StrategyRegistry.get_instance().get("momentum_rotation")
    sig = strat.generate_signals(fr).positions
    assert "B" not in set(sig["symbol"])
    assert "A" in set(sig["symbol"])


def test_positions_schema_via_registry():
    fr = _factor_results({
        "A": {"m60": [0.3] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
    })
    strat = StrategyRegistry.get_instance().get("momentum_rotation")
    signal = strat.generate_signals(fr)
    df = signal.positions
    assert list(df.columns) == ["timestamp", "symbol", "position"]


def test_params_are_overrideable():
    fr = _factor_results({
        "A": {"m60": [0.3] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
        "B": {"m60": [0.2] * 30, "m120": [0.3] * 30, "vol": [0.5] * 30},
    })
    strat = StrategyRegistry.get_instance().get("momentum_rotation")

    sig = strat.generate_signals(fr, top_n=1).positions
    assert (sig[sig["symbol"] == "A"]["position"] == 1.0).all()
    assert (sig[sig["symbol"] == "B"]["position"] == 0.0).all()

    pos = strat.generate_signals(_factor_results({
        "A": {"m60": [0.3] * 30, "m120": [0.05] * 30, "vol": [0.5] * 30},
    }), bear_m120=-10.0).positions
    assert (pos["position"] == 1.0).all()
    pos = strat.generate_signals(_factor_results({
        "A": {"m60": [0.3] * 30, "m120": [-0.2] * 30, "vol": [0.5] * 30},
    }), bear_m120=0.0).positions
    assert (pos["position"] == 0.0).all()
