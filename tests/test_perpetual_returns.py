"""Tests for perpetual total-return construction."""

import pandas as pd
import pytest

from superplatform.evaluation.returns import construct_perpetual_returns


def _perpetual_panel() -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-01", periods=21, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "symbol": "BTCUSDT",
            "close": [100.0 * 1.01**index for index in range(len(timestamps))],
            "funding_rate": [0.0] + [0.001] * (len(timestamps) - 1),
        }
    )


def test_perpetual_returns_add_price_and_funding_for_all_horizons() -> None:
    """Each ret_* is the close return plus signed funding over the hold period."""
    result = construct_perpetual_returns(
        _perpetual_panel(),
        market_type="perpetual",
    )

    for horizon in (1, 5, 10, 20):
        assert result.loc[0, f"ret_{horizon}"] == pytest.approx(
            1.01**horizon - 1.0 + horizon * 0.001
        )


def test_perpetual_returns_reject_missing_funding_column() -> None:
    """A perpetual panel cannot use predeclared returns in place of funding data."""
    with pytest.raises(ValueError, match="funding_rate"):
        construct_perpetual_returns(
            _perpetual_panel().drop(columns="funding_rate"),
            market_type="perpetual",
        )


def test_spot_returns_are_not_recomputed_or_require_funding() -> None:
    """Spot inputs retain their supplied return fields and skip funding validation."""
    panel = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-01", tz="UTC")],
            "symbol": ["BTCUSDT"],
            "ret_1": [0.02],
        }
    )
    result = construct_perpetual_returns(panel, market_type="spot")

    pd.testing.assert_frame_equal(result, panel)
