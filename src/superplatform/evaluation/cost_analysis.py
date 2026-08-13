"""Cost sensitivity analysis.

Assesses how transaction costs (fees + slippage) affect factor performance.
Explicit cost assumptions are required per Task 1.
"""

from dataclasses import dataclass

import pandas as pd


@dataclass
class CostAssumptions:
    """Explicit trading cost assumptions.

    Attributes:
        maker_fee_bps: Maker fee in basis points (e.g. 2.0 = 2 bps = 0.02%).
        taker_fee_bps: Taker fee in basis points.
        slippage_bps: Estimated slippage per trade in basis points.
        min_commission_usd: Minimum commission per trade in USD.
    """

    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 4.0
    slippage_bps: float = 3.0
    min_commission_usd: float = 0.0


def cost_sensitivity(
    layer_results: pd.DataFrame,
    turnover_df: pd.DataFrame,
    cost_assumptions: list[CostAssumptions] | None = None,
) -> pd.DataFrame:
    """Assess net returns under different cost assumptions.

    Returns empty DataFrame if layer_results or turnover_df is empty
    (e.g. single-symbol evaluation with no cross-section).
    """
    if layer_results.empty or turnover_df.empty:
        return pd.DataFrame()

    if cost_assumptions is None:
        cost_assumptions = [
            CostAssumptions(maker_fee_bps=0, taker_fee_bps=0, slippage_bps=0),
            CostAssumptions(maker_fee_bps=2, taker_fee_bps=4, slippage_bps=3),
            CostAssumptions(maker_fee_bps=2, taker_fee_bps=4, slippage_bps=5),
            CostAssumptions(maker_fee_bps=5, taker_fee_bps=7, slippage_bps=10),
        ]

    # Gross top-minus-bottom spread per timestamp. Vectorized in place of the
    # former groupby.apply(lambda ...) which ran one Python callable per
    # timestamp and dominated runtime on long panels.
    max_layer = layer_results.groupby("timestamp")["layer"].transform("max")
    min_layer = layer_results.groupby("timestamp")["layer"].transform("min")
    gross = (
        layer_results.loc[layer_results["layer"].eq(max_layer)]
        .groupby("timestamp")["mean_return"].mean()
        - layer_results.loc[layer_results["layer"].eq(min_layer)]
        .groupby("timestamp")["mean_return"].mean()
    )

    mean_turn = turnover_df["turnover"].mean() if len(turnover_df) > 0 else 1.0

    results = []
    for i, cost in enumerate(cost_assumptions):
        round_trip_bps = cost.taker_fee_bps * 2 + cost.slippage_bps * 2
        annual_cost_bps = round_trip_bps * mean_turn * 252  # assume daily data

        # Per-period cost
        period_cost_bps = round_trip_bps * mean_turn

        results.append({
            "scenario": f"scenario_{i}",
            "maker_fee_bps": cost.maker_fee_bps,
            "taker_fee_bps": cost.taker_fee_bps,
            "slippage_bps": cost.slippage_bps,
            "gross_spread_mean": gross.mean(),
            "round_trip_bps": round_trip_bps,
            "period_cost_bps": period_cost_bps,
            "net_spread_mean": gross.mean() - period_cost_bps / 10000,
            "annual_cost_bps_est": annual_cost_bps,
        })

    return pd.DataFrame(results)
