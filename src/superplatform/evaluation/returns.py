"""Forward total-return construction for perpetual evaluation panels."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

RETURN_HORIZONS = (1, 5, 10, 20)


def _forward_funding_sum(values: pd.Series, horizon: int) -> pd.Series:
    """Sum signed funding from the next bar through the holding-period end."""
    shifted = values.shift(-1)
    return shifted.iloc[::-1].rolling(horizon, min_periods=horizon).sum().iloc[::-1]


def construct_perpetual_returns(
    panel: pd.DataFrame,
    *,
    market_type: str | None,
    price_col: str = "close",
    funding_col: str = "funding_rate",
    horizons: Sequence[int] = RETURN_HORIZONS,
) -> pd.DataFrame:
    """Build perpetual ``ret_*`` columns from close prices and signed funding.

    ``funding_col`` must be aligned to the price bars and expressed as a signed
    simple return for the position. Funding observations from the next bar through
    the holding-period end are added to close-to-close price returns.
    """
    if str(market_type).lower() != "perpetual":
        return panel.copy()

    normalized_horizons = tuple(int(horizon) for horizon in horizons)
    if not normalized_horizons or any(horizon < 1 for horizon in normalized_horizons):
        raise ValueError("horizons must contain positive integers")

    required = {"timestamp", "symbol", price_col, funding_col}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"perpetual panel is missing return construction columns: {missing}")

    keys = ["timestamp", "symbol"]
    market_data = panel[keys + [price_col, funding_col]].copy()
    conflicts = (
        market_data.groupby(keys, dropna=False)[[price_col, funding_col]]
        .nunique(dropna=False)
        .gt(1)
        .any(axis=1)
    )
    if conflicts.any():
        raise ValueError("factor rows disagree on perpetual price or funding inputs")
    market_data = market_data.drop_duplicates(keys).sort_values(
        ["symbol", "timestamp"]
    )
    market_data[price_col] = pd.to_numeric(market_data[price_col], errors="coerce")
    market_data[funding_col] = pd.to_numeric(market_data[funding_col], errors="coerce")
    if market_data[[price_col, funding_col]].isna().any().any():
        raise ValueError("perpetual price and funding inputs must be complete numeric values")
    if not np.isfinite(market_data[[price_col, funding_col]].to_numpy()).all():
        raise ValueError("perpetual price and funding inputs must be finite")
    if (market_data[price_col] <= 0).any():
        raise ValueError("perpetual close prices must be positive")

    grouped = market_data.groupby("symbol", sort=False)
    return_columns: list[str] = []
    for horizon in normalized_horizons:
        return_col = f"ret_{horizon}"
        price_return = grouped[price_col].transform(
            lambda values, period=horizon: values.shift(-period) / values - 1.0
        )
        funding_return = grouped[funding_col].transform(
            lambda values, period=horizon: _forward_funding_sum(values, period)
        )
        market_data[return_col] = price_return + funding_return
        return_columns.append(return_col)

    result = panel.drop(columns=return_columns, errors="ignore")
    return result.merge(
        market_data[keys + return_columns],
        on=keys,
        how="left",
        validate="many_to_one",
    )
