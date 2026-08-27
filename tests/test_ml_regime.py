from __future__ import annotations

import numpy as np
import pandas as pd

from superplatform.ml.regime import RegimeConfig, detect_market_regime


def _close() -> pd.Series:
    index = pd.date_range("2024-01-01", periods=150, freq="D", tz="UTC")
    values = np.concatenate(
        [np.linspace(100, 160, 60), np.linspace(160, 90, 45), np.full(45, 90.0)]
    )
    return pd.Series(values, index=index, name="close")


def test_regime_detector_finds_bull_bear_and_sideways() -> None:
    result = detect_market_regime(
        _close(),
        config=RegimeConfig(
            fast_window=5,
            slow_window=15,
            volatility_window=5,
            trend_threshold=0.03,
            bear_drawdown=0.12,
            confirmation_periods=2,
        ),
    )
    assert {"bull", "bear", "sideways"}.issubset(set(result["regime"]))
    assert result["confidence"].between(0, 1).all()


def test_regime_detector_is_causal() -> None:
    close = _close()
    config = RegimeConfig(fast_window=5, slow_window=15, volatility_window=5)
    original = detect_market_regime(close, config=config)
    changed = close.copy()
    changed.iloc[-20:] *= 10
    rerun = detect_market_regime(changed, config=config)

    pd.testing.assert_frame_equal(original.iloc[:-20], rerun.iloc[:-20])


def test_regime_detector_rejects_naive_time() -> None:
    close = _close()
    close.index = close.index.tz_localize(None)
    try:
        detect_market_regime(close)
    except ValueError as exc:
        assert "UTC" in str(exc)
    else:
        raise AssertionError("naive timestamps must be rejected")
