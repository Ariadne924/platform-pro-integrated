"""Forward return computation.

Adds forward-looking return columns to a price DataFrame.
Used by the evaluation layer for IC computation, layer tests, etc.

IMPORTANT: These columns contain FUTURE information. They must only be
used for evaluation (correlating factor_t with return_{t+n}), never as
input to factor computation — that would be forward-looking bias.
"""

import pandas as pd


def add_forward_returns(
    df: pd.DataFrame,
    price_col: str = "close",
    periods: list[int] | None = None,
    pct: bool = True,
) -> pd.DataFrame:
    """Add forward return columns to a price DataFrame.

    Args:
        df: DataFrame with a price column, sorted by timestamp ascending.
        price_col: Name of the price column.
        periods: List of forward periods, e.g. [1, 5, 10, 20].
                 Defaults to [1, 5, 10, 20].
        pct: If True, compute percentage returns. If False, absolute differences.

    Returns:
        DataFrame with added columns: forward_return_t1, forward_return_t5, ...
    """
    if periods is None:
        periods = [1, 5, 10, 20]

    result = df.copy()
    for p in periods:
        col_name = f"forward_return_t{p}"
        if pct:
            result[col_name] = result[price_col].shift(-p) / result[price_col] - 1
        else:
            result[col_name] = result[price_col].shift(-p) - result[price_col]

    return result
