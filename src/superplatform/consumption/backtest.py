"""Simple vectorized backtester.

Takes strategy signals and price data, computes P&L and risk metrics.
The engine caps total gross exposure at 100%: N symbols at full weight become
an equal-weight 1/N book, not an N× leveraged one. Daily losses run through a
hard liquidation floor — once equity reaches ≤ 0 it is frozen at zero and
compounding stops. Costs (taker fee + slippage) are opt-in via taker_fee_bps /
slippage_bps and default to zero.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from pandera.typing import DataFrame

from superplatform.strategy.signal_schema import SignalSchema


@dataclass
class BacktestResult:
    """Output of a backtest run.

    Attributes:
        strategy_name: Which strategy was tested.
        equity: Cumulative P&L curve (DataFrame: timestamp, equity).
        trades: Per-timestamp position changes (DataFrame: timestamp, symbol, position, pnl).
        total_return: Total return over the period.
        annual_return: Annualized return.
        annual_vol: Annualized volatility.
        sharpe: Sharpe ratio (annualized, assuming 0 risk-free rate for crypto).
        max_drawdown: Maximum drawdown as a negative fraction.
        win_rate: Fraction of periods with positive return.
        avg_return: Mean per-period return.
        liquidated_at: First day equity reached ≤ 0 (liquidation), if any.
        liquidation: Human-readable liquidation note when liquidated_at is set.
    """

    strategy_name: str
    equity: pd.DataFrame
    trades: pd.DataFrame
    total_return: float
    annual_return: float
    annual_vol: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    avg_return: float
    liquidated_at: Optional[pd.Timestamp] = None
    liquidation: Optional[str] = None


def backtest(
    signals: DataFrame[SignalSchema],
    price_data: dict[str, pd.DataFrame],
    periods_per_year: int = 365,
    taker_fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> BacktestResult:
    """Run a vectorized backtest.

    Args:
        signals: DataFrame with columns (timestamp, symbol, position).
                 Position is target weight in [-1, 1].
        price_data: Dict of symbol → DataFrame with (timestamp, close).
                    Same symbols as in signals.
        periods_per_year: For annualization (365 for daily crypto).
        taker_fee_bps: Per-trade taker fee in basis points (one-way).
        slippage_bps: Per-trade slippage in basis points (one-way).

    Returns:
        BacktestResult with equity curve and metrics.
    """
    if signals.empty:
        raise ValueError("Signals DataFrame is empty")

    strategy_name = signals.attrs.get("strategy_name", "unknown")

    # Cap total gross exposure at 100%: strategy defs emit raw target weights;
    # the ENGINE scales so Σ|weight| ≤ 1.0 per timestamp. N symbols all at 1.0
    # become a 1/N equal-weight book instead of an N× leveraged one. Symbols
    # absent from price_data are skipped downstream, so they consume no budget.
    traded_mask = signals["symbol"].isin(price_data.keys())
    gross = (
        signals["position"].where(traded_mask, 0.0).abs()
        .groupby(signals["timestamp"]).transform("sum")
    )
    safe = gross.where(gross > 0, 1.0)      # gross == 0 → scale 1 (no div-by-zero)
    scale = (1.0 / safe).clip(upper=1.0)    # Σ|w| ≤ 1 untouched; above 1 scaled to 1
    signals = signals.copy()                # never mutate the caller's frame
    signals["position"] = signals["position"] * scale.to_numpy()

    # One-way trading cost rate (matches evaluation.backtest._transaction_cost_rate)
    rate = (taker_fee_bps + slippage_bps) / 10000.0
    if rate < 0.0:
        raise ValueError("taker_fee_bps and slippage_bps must be non-negative")

    # Compute per-symbol returns, then aggregate
    pnl_frames = []
    cost_frames = []
    for symbol in signals["symbol"].unique():
        if symbol not in price_data:
            continue
        prices = price_data[symbol][["timestamp", "close"]].copy()
        prices["return"] = prices["close"].pct_change()

        sig = signals[signals["symbol"] == symbol].copy()
        sig = sig.sort_values("timestamp")
        # Normalize both to timezone-naive datetime64[ns] for merge
        sig["timestamp"] = pd.to_datetime(sig["timestamp"], utc=True).dt.tz_localize(None)
        p = prices[["timestamp", "return"]].copy()
        p["timestamp"] = pd.to_datetime(p["timestamp"], utc=True).dt.tz_localize(None)

        merged = sig.merge(p, on="timestamp", how="left")
        # Position at time t earns return at time t+1
        merged["pnl"] = merged["position"].shift(1) * merged["return"]
        merged["symbol"] = symbol
        pnl_frames.append(merged[["timestamp", "symbol", "pnl"]])

        if rate > 0.0:
            # Rebalance cost lands on the same row as the pnl it affects:
            # held = weight during the interval ending at t, prev_held = the
            # previous held weight (0 before the series starts, so the initial
            # entry pays its fee on the first real pnl row).
            held = merged["position"].shift(1)
            prev_held = merged["position"].shift(2).fillna(0.0)
            turnover = (held - prev_held).abs().fillna(0.0)
            merged["cost"] = turnover * rate
            cost_frames.append(merged[["timestamp", "symbol", "cost"]])

    if not pnl_frames:
        raise ValueError("No matching symbols between signals and price data")

    pnl = pd.concat(pnl_frames, ignore_index=True)

    # Aggregate P&L across symbols per timestamp
    daily_pnl = pnl.groupby("timestamp")["pnl"].sum().dropna()
    if rate > 0.0:
        cost = pd.concat(cost_frames, ignore_index=True)
        daily_cost = cost.groupby("timestamp")["cost"].sum()
        daily_pnl = daily_pnl.sub(daily_cost.reindex(daily_pnl.index, fill_value=0.0))
    if len(daily_pnl) < 2:
        raise ValueError("Not enough data points for backtest")

    daily_pnl = daily_pnl.sort_index()

    # Hard liquidation floor. If any day's loss pushes equity to ≤ 0 the book
    # is wiped out: floor that day to -100% (equity → exactly 0) and freeze all
    # later days — no further compounding, equity never goes negative.
    equity_curve = (1.0 + daily_pnl).cumprod()
    liquidated_at = None
    if (equity_curve <= 0).any():
        liq_pos = int(np.argmax((equity_curve <= 0).to_numpy()))
        liquidated_at = daily_pnl.index[liq_pos]
        daily_pnl = daily_pnl.copy()
        daily_pnl.iloc[liq_pos] = -1.0
        daily_pnl.iloc[liq_pos + 1:] = 0.0
        equity_curve = (1.0 + daily_pnl).cumprod()

    # Equity curve
    equity = pd.DataFrame({
        "timestamp": daily_pnl.index,
        "equity": equity_curve,
    })

    # Metrics
    total_return = equity["equity"].iloc[-1] - 1
    mean_ret = daily_pnl.mean()
    vol = daily_pnl.std()
    annual_return = (1 + total_return) ** (periods_per_year / len(daily_pnl)) - 1
    annual_vol = vol * np.sqrt(periods_per_year)
    sharpe = annual_return / annual_vol if annual_vol > 0 else 0.0
    win_rate = (daily_pnl > 0).mean()

    # Max drawdown
    peak = equity["equity"].cummax()
    drawdown = (equity["equity"] - peak) / peak
    max_dd = drawdown.min()

    # Per-symbol position timeline for trade visualization
    trades_df = pd.concat(pnl_frames, ignore_index=True) if pnl_frames else pd.DataFrame()

    return BacktestResult(
        strategy_name=strategy_name,
        equity=equity,
        trades=trades_df,
        total_return=total_return,
        annual_return=annual_return,
        annual_vol=annual_vol,
        sharpe=sharpe,
        max_drawdown=max_dd,
        win_rate=win_rate,
        avg_return=mean_ret,
        liquidated_at=liquidated_at,
        liquidation=(
            f"Liquidated on {liquidated_at.date()} — equity reached zero"
            if liquidated_at is not None else None
        ),
    )
