"""Minimal factor quantile backtest and composition turnover analysis.

This module consumes a daily factor panel with one selected forward-return column.
It applies an optional turnover-based fee and slippage approximation, but does not
model portfolio drift, capacity, or market impact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)
DEFAULT_RETURN_COLUMNS = {"ret_1", "ret_5", "ret_10", "ret_20"}
LONG_SHORT_COLUMNS = [
    "timestamp",
    "factor_name",
    "top_quantile",
    "bottom_quantile",
    "top_return",
    "bottom_return",
    "gross_return",
    "net_return",
    "turnover",
    "cost",
    "long_short_return",
    "n_assets_top",
    "n_assets_bottom",
]


@dataclass
class BacktestResult:
    """Container for the four persisted MVP outputs and diagnostic data."""

    assignments: pd.DataFrame
    decile_returns: pd.DataFrame
    long_short_returns: pd.DataFrame
    long_short_nav: pd.DataFrame
    turnover: pd.DataFrame
    logs: pd.DataFrame


def _resolve_factor_column(panel: pd.DataFrame, factor_col: str) -> str:
    """Resolve the requested factor column while supporting the metrics output name."""
    if factor_col in panel.columns:
        return factor_col
    if factor_col == "factor_value" and "factor_value_eval" in panel.columns:
        return "factor_value_eval"
    raise ValueError(f"factor column not found: {factor_col}")


def _validate_panel(
    panel: pd.DataFrame,
    *,
    factor_col: str,
    return_col: str,
) -> tuple[pd.DataFrame, str]:
    """Validate a UTC panel and return a sorted, independent working copy."""
    required = {"timestamp", "symbol", return_col}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"panel is missing required columns: {missing}")
    resolved_factor_col = _resolve_factor_column(panel, factor_col)
    if not pd.api.types.is_datetime64_any_dtype(panel["timestamp"]):
        raise TypeError("panel.timestamp must be a timezone-aware UTC datetime")
    timezone = getattr(panel["timestamp"].dtype, "tz", None)
    if timezone is None or str(timezone).upper() not in {
        "UTC",
        "UTC+00:00",
        "ETC/UTC",
    }:
        raise ValueError(f"panel.timestamp must be UTC, got {timezone}")
    if panel["timestamp"].isna().any():
        raise ValueError("panel.timestamp contains null values")
    if panel[["timestamp", "symbol"]].isna().any().any():
        raise ValueError("panel timestamp and symbol keys cannot be null")
    result = panel.copy()
    if "factor_name" not in result.columns:
        result["factor_name"] = "factor"
    if result["factor_name"].isna().any():
        raise ValueError("panel factor_name keys cannot be null")
    if result.duplicated(["timestamp", "symbol", "factor_name"]).any():
        raise ValueError(
            "panel contains duplicate timestamp/symbol/factor_name keys"
        )
    result = result.sort_values(["timestamp", "factor_name", "symbol"]).reset_index(
        drop=True
    )
    return result, resolved_factor_col


def _select_factor(
    panel: pd.DataFrame,
    factor_name: str | None,
) -> pd.DataFrame:
    """Select one factor or retain all factors represented in the panel."""
    if factor_name is None:
        return panel
    if "factor_name" not in panel.columns:
        raise ValueError("factor_name selection requires a factor_name column")
    selected = panel[panel["factor_name"].eq(factor_name)].copy()
    if selected.empty:
        raise ValueError(f"factor_name not found: {factor_name}")
    return selected


def _layer_count(
    n_assets: int,
    requested_q: int,
    min_assets_per_layer: int,
) -> int:
    """Return the largest feasible layer count, or zero when two layers are impossible."""
    if requested_q < 2:
        raise ValueError("q must be at least 2")
    if min_assets_per_layer < 1:
        raise ValueError("min_assets_per_layer must be at least 1")
    feasible = min(requested_q, n_assets // min_assets_per_layer)
    return feasible if feasible >= 2 else 0


def _assign_group_layers(
    group: pd.DataFrame,
    *,
    factor_col: str,
    q: int,
    min_assets_per_layer: int,
) -> tuple[pd.Series, dict[str, object]]:
    """Assign one date/factor group to layers and return its diagnostic record."""
    values = pd.to_numeric(group[factor_col], errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    valid = values.notna()
    n_assets = int(valid.sum())
    actual_q = _layer_count(n_assets, q, min_assets_per_layer)
    base_log: dict[str, object] = {
        "timestamp": group["timestamp"].iloc[0],
        "factor_name": group["factor_name"].iloc[0],
        "requested_q": q,
        "actual_q": actual_q,
        "n_assets": n_assets,
        "status": "skipped",
        "reason": "",
    }
    layers = pd.Series(pd.NA, index=group.index, dtype="Int64")
    if actual_q == 0:
        base_log["reason"] = "fewer than two feasible groups"
        return layers, base_log

    valid_group = group.loc[valid].copy()
    valid_values = values.loc[valid]
    n_valid = len(valid_values)
    if n_valid <= actual_q:
        # Tiny cross-section: direct rank equal-division is equivalent to
        # qcut without its per-call overhead (dominant on small panels).
        ordered = valid_values.sort_values(kind="mergesort")
        cut_values = (np.arange(n_valid) * actual_q // n_valid).astype(int)
        cut = pd.Series(cut_values, index=ordered.index)
        method = "rank_cut"
    else:
        try:
            with np.errstate(invalid="ignore"):
                cut = pd.qcut(
                    valid_values,
                    q=actual_q,
                    labels=False,
                    duplicates="drop",
                )
            cut = pd.Series(cut, index=valid_group.index)
            if cut.nunique(dropna=True) != actual_q:
                raise ValueError("duplicate quantile edges")
            method = "qcut"
        except (TypeError, ValueError):
            stable = valid_group.assign(_factor_value=valid_values)
            stable = stable.sort_values(["_factor_value", "symbol"], kind="mergesort")
            ranks = pd.Series(
                np.arange(len(stable), dtype=float),
                index=stable.index,
            )
            with np.errstate(invalid="ignore"):
                cut = pd.qcut(ranks, q=actual_q, labels=False)
            cut = pd.Series(cut, index=stable.index).reindex(valid_group.index)
            method = "rank_cut"

    layers.loc[cut.index] = cut.astype("int64") + 1
    status = "ok" if actual_q == q and method == "qcut" else "degraded"
    reason_parts = []
    if actual_q < q:
        reason_parts.append(f"sample supports {actual_q} groups")
    if method == "rank_cut":
        reason_parts.append("duplicate quantile edges handled by stable rank-cut")
    base_log.update(
        {
            "status": status,
            "reason": "; ".join(reason_parts) or "qcut",
            "method": method,
        }
    )
    return layers, base_log


def assign_quantiles(
    panel: pd.DataFrame,
    *,
    factor_col: str = "factor_value",
    q: int = 10,
    min_assets_per_layer: int = 1,
    factor_name: str | None = None,
) -> pd.DataFrame:
    """Assign daily factor observations to integer layers from 1 through Q.

    The returned frame has a nullable integer quantile column. Per-date diagnostics
    are available in result.attrs["layer_log"] and are also emitted through logging.
    """
    working, resolved_factor_col = _validate_panel(
        panel,
        factor_col=factor_col,
        return_col=next(
            (column for column in DEFAULT_RETURN_COLUMNS if column in panel.columns),
            "forward_return",
        ),
    )
    working = _select_factor(working, factor_name)
    keys = ["timestamp", "factor_name"]

    working["_factor_value"] = pd.to_numeric(
        working[resolved_factor_col], errors="coerce"
    ).replace([np.inf, -np.inf], np.nan)
    working["_valid"] = working["_factor_value"].notna().astype(int)

    grouped = working.groupby(keys, sort=True, dropna=False)

    # Fully vectorized layer assignment: per-period rank equal-division
    # (identical to qcut without tied edges, and to the stable rank-cut).
    n_valid = grouped["_valid"].transform("sum")
    actual_q = np.minimum(q, n_valid // min_assets_per_layer)
    feasible = actual_q >= 2

    rank = grouped["_factor_value"].rank(method="first")
    layer = ((rank - 1) * actual_q // n_valid.replace(0, np.nan)) + 1

    quantile = pd.Series(pd.NA, index=working.index, dtype="Int64")
    mask = feasible & working["_factor_value"].notna()
    quantile.loc[mask] = layer[mask].astype("Int64")
    working["quantile"] = quantile

    # Per-date diagnostics from the same vectorized pass.
    log_df = pd.DataFrame(
        {
            "timestamp": grouped["timestamp"].first(),
            "factor_name": grouped["factor_name"].first(),
            "requested_q": q,
            "actual_q": grouped["_valid"].sum().clip(lower=0) // min_assets_per_layer,
            "n_assets": grouped["_valid"].sum(),
            "n_unique": grouped["_factor_value"].nunique(),
        }
    )
    log_df["actual_q"] = np.minimum(q, log_df["actual_q"])
    log_df["status"] = np.where(
        log_df["actual_q"] >= 2,
        np.where(log_df["actual_q"] < q, "degraded", "ok"),
        "skipped",
    )
    log_df["method"] = "rank_cut"
    log_df["reason"] = ""
    log_df.loc[log_df["status"] == "skipped", "reason"] = (
        "fewer than two feasible groups"
    )
    # Tied factor values force the stable rank-cut instead of qcut, which
    # the original implementation reported as a degraded assignment.
    tied = log_df["n_unique"] < log_df["n_assets"]
    log_df.loc[tied & (log_df["status"] == "ok"), "status"] = "degraded"
    log_df.loc[tied & (log_df["status"] != "skipped"), "reason"] = (
        "duplicate quantile edges handled by stable rank-cut"
    )
    logs = log_df.to_dict("records")
    working.attrs["layer_log"] = logs

    # Per-date skip/degrade is expected on small cross-sections; log a
    # single summary instead of one line per group.
    skipped = int((log_df["status"] == "skipped").sum())
    degraded = int((log_df["status"] == "degraded").sum())
    if skipped:
        LOGGER.warning(
            "Layer assignment skipped for %d groups (insufficient data)", skipped
        )
    if degraded:
        LOGGER.info(
            "Layer assignment degraded for %d groups (requested layers reduced)",
            degraded,
        )
    return working


def compute_decile_returns(
    panel: pd.DataFrame,
    *,
    return_col: Literal["ret_1", "ret_5", "ret_10", "ret_20"] = "ret_1",
    factor_col: str = "factor_value",
    q: int = 10,
    min_assets_per_layer: int = 1,
    factor_name: str | None = None,
) -> pd.DataFrame:
    """Compute the mean selected forward return for each daily factor layer.

    Accepts either a raw panel (layers are assigned here) or an already
    quantile-assigned frame (callers such as run_backtest reuse their
    assignment to avoid recomputing and re-logging it).
    """
    if return_col not in DEFAULT_RETURN_COLUMNS:
        raise ValueError(f"return_col must be one of {sorted(DEFAULT_RETURN_COLUMNS)}")
    if "quantile" in panel.columns:
        assigned = panel
    else:
        assigned = assign_quantiles(
            panel,
            factor_col=factor_col,
            q=q,
            min_assets_per_layer=min_assets_per_layer,
            factor_name=factor_name,
        )
    valid_return = pd.to_numeric(assigned[return_col], errors="coerce")
    working = assigned.assign(_selected_return=valid_return).dropna(
        subset=["quantile"]
    )
    # Clear attrs before groupby — pandas deep-copies attrs per group, and
    # the layer_log on the assignments frame is large.  Clear the working
    # copy only; the caller's assigned frame keeps its layer_log.
    working.attrs = {}
    grouped = working.groupby(
        ["timestamp", "factor_name", "quantile"], sort=True
    )
    agg = grouped["_selected_return"].agg(["mean", "count"])
    constituents = grouped.size()
    result = agg.reset_index().rename(
        columns={"mean": "mean_return", "count": "n_assets"}
    )
    result["quantile"] = result["quantile"].astype(int)
    result["gross_return"] = result["mean_return"]
    result["n_constituents"] = constituents.reset_index(drop=True).to_numpy()
    result = result[
        [
            "timestamp",
            "factor_name",
            "quantile",
            "mean_return",
            "gross_return",
            "n_assets",
            "n_constituents",
        ]
    ]
    # Do not attach layer_log / assignments to attrs: downstream groupby
    # iterations deep-copy attrs per group, which would rescan the whole
    # assignments frame for every group.  run_backtest reads the log
    # directly from the assignments object it already holds.
    return result


def _transaction_cost_rate(fee_bps: float, slippage_bps: float) -> float:
    """Validate one-way cost inputs and convert basis points to a return rate."""
    try:
        fee = float(fee_bps)
        slippage = float(slippage_bps)
    except (TypeError, ValueError) as error:
        raise ValueError("fee_bps and slippage_bps must be numeric") from error
    if not np.isfinite(fee) or not np.isfinite(slippage) or fee < 0 or slippage < 0:
        raise ValueError("fee_bps and slippage_bps must be finite and non-negative")
    return (fee + slippage) / 10_000.0


def _apply_turnover_costs(
    decile_returns: pd.DataFrame,
    turnover: pd.DataFrame,
    *,
    cost_rate: float,
) -> pd.DataFrame:
    """Add layer turnover, gross return, and turnover-based net return columns."""
    keys = ["timestamp", "factor_name", "quantile"]
    result = decile_returns.merge(
        turnover[keys + ["turnover"]],
        on=keys,
        how="left",
        validate="one_to_one",
    )
    result["cost"] = result["turnover"].fillna(0.0) * cost_rate
    result["net_return"] = result["gross_return"] - result["cost"]
    return result


def compute_long_short_returns(
    decile_returns: pd.DataFrame,
    *,
    top_quantile: int | None = None,
    bottom_quantile: int = 1,
) -> pd.DataFrame:
    """Compute gross and turnover-cost-adjusted top-minus-bottom returns."""
    result = decile_returns.copy()
    if "gross_return" not in result.columns and "mean_return" in result.columns:
        result["gross_return"] = result["mean_return"]
    required = {"timestamp", "factor_name", "quantile", "gross_return"}
    missing = sorted(required.difference(result.columns))
    if missing:
        raise ValueError(f"decile_returns is missing required columns: {missing}")
    available = result.dropna(subset=["gross_return"])
    if available.empty:
        return pd.DataFrame(columns=LONG_SHORT_COLUMNS)

    # Select the top/bottom row per (timestamp, factor_name) with transforms
    # instead of a per-group loop.
    if top_quantile is not None:
        top_mask = available["quantile"] == top_quantile
    else:
        top_mask = (
            available.groupby(["timestamp", "factor_name"])["quantile"].transform("max")
            == available["quantile"]
        )
    bottom_mask = available["quantile"] == bottom_quantile

    top = available[top_mask]
    bottom = available[bottom_mask]
    merged = top.merge(
        bottom,
        on=["timestamp", "factor_name"],
        suffixes=("_top", "_bottom"),
        how="inner",
    )
    if merged.empty:
        return pd.DataFrame(columns=LONG_SHORT_COLUMNS)

    gross_return = merged["gross_return_top"] - merged["gross_return_bottom"]
    cost_top = (
        merged["cost_top"].fillna(0.0) if "cost_top" in merged.columns else 0.0
    )
    cost_bottom = (
        merged["cost_bottom"].fillna(0.0)
        if "cost_bottom" in merged.columns
        else 0.0
    )
    cost = cost_top + cost_bottom
    turnover_top = (
        merged["turnover_top"] if "turnover_top" in merged.columns else np.nan
    )
    turnover_bottom = (
        merged["turnover_bottom"] if "turnover_bottom" in merged.columns else np.nan
    )
    n_assets_top = (
        merged["n_assets_top"] if "n_assets_top" in merged.columns else np.nan
    )
    n_assets_bottom = (
        merged["n_assets_bottom"] if "n_assets_bottom" in merged.columns else np.nan
    )
    rows = pd.DataFrame(
        {
            "timestamp": merged["timestamp"],
            "factor_name": merged["factor_name"],
            "top_quantile": merged["quantile_top"],
            "bottom_quantile": merged["quantile_bottom"],
            "top_return": merged["gross_return_top"],
            "bottom_return": merged["gross_return_bottom"],
            "gross_return": gross_return,
            "net_return": gross_return - cost,
            "turnover": turnover_top + turnover_bottom,
            "cost": cost,
            "long_short_return": gross_return,
            "n_assets_top": n_assets_top,
            "n_assets_bottom": n_assets_bottom,
        }
    )
    return rows.reset_index(drop=True)


def compute_nav(
    returns: pd.DataFrame,
    *,
    return_col: str = "long_short_return",
    nav_col: str = "nav",
    method: Literal["compound", "simple"] = "compound",
) -> pd.DataFrame:
    """Compute cumulative NAV using compounded simple returns by default."""
    if return_col not in returns.columns:
        raise ValueError(f"returns is missing return column: {return_col}")
    if method not in {"compound", "simple"}:
        raise ValueError("method must be 'compound' or 'simple'")
    result = returns.sort_values(["timestamp", "factor_name"]).copy()
    values = pd.to_numeric(result[return_col], errors="coerce")
    result[nav_col] = (
        (1.0 + values).groupby(result["factor_name"]).cumprod()
        if method == "compound"
        else 1.0 + values.groupby(result["factor_name"]).cumsum()
    )
    return result


def compute_turnover(
    assignments: pd.DataFrame,
    *,
    quantile_col: str = "quantile",
) -> pd.DataFrame:
    """Compute adjacent-date constituent change ratios for every quantile.

    Turnover is one minus overlap divided by the larger of current and previous
    constituent counts. The first observation for each factor/quantile is NaN.
    """
    required = {"timestamp", "symbol", "factor_name", quantile_col}
    missing = sorted(required.difference(assignments.columns))
    if missing:
        raise ValueError(f"assignments is missing required columns: {missing}")
    current = assignments.dropna(subset=[quantile_col]).copy()
    # Clear attrs before groupby — pandas deep-copies attrs per group, and
    # the assignments frame may carry a large layer_log.
    current.attrs = {}
    current[quantile_col] = current[quantile_col].astype(int)
    rows: list[dict[str, object]] = []
    previous: dict[tuple[str, int], set[str]] = {}
    # groupby(sort=True) yields (timestamp, factor_name, quantile) in
    # ascending order, so consecutive observations of the same factor
    # quantile are adjacent — no cross-product scan needed.
    for (timestamp, factor_name, quantile), group in current.groupby(
        ["timestamp", "factor_name", quantile_col], sort=True
    ):
        constituents = set(group["symbol"])
        key = (factor_name, int(quantile))
        previous_constituents = previous.get(key)
        if previous_constituents is None:
            turnover = np.nan
            overlap = 0
            changed = np.nan
            previous_count = 0
        else:
            overlap = len(constituents & previous_constituents)
            denominator = max(len(constituents), len(previous_constituents))
            turnover = (
                1.0 - overlap / denominator if denominator else np.nan
            )
            changed = denominator - overlap
            previous_count = len(previous_constituents)
        rows.append(
            {
                "timestamp": timestamp,
                "factor_name": factor_name,
                "quantile": int(quantile),
                "turnover": turnover,
                "n_current": len(constituents),
                "n_previous": previous_count,
                "overlap": overlap,
                "changed": changed,
            }
        )
        previous[key] = constituents
    return pd.DataFrame(rows)


def write_backtest_outputs(
    result: BacktestResult,
    output_dir: str | Path,
) -> dict[str, Path]:
    """Persist the four required CSV outputs and return their paths."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "decile_returns": directory / "decile_returns.csv",
        "long_short_returns": directory / "long_short_returns.csv",
        "long_short_nav": directory / "long_short_nav.csv",
        "turnover": directory / "turnover.csv",
    }
    result.decile_returns.to_csv(outputs["decile_returns"], index=False)
    result.long_short_returns.to_csv(outputs["long_short_returns"], index=False)
    result.long_short_nav.to_csv(outputs["long_short_nav"], index=False)
    result.turnover.to_csv(outputs["turnover"], index=False)
    return outputs


def run_backtest(
    panel: pd.DataFrame,
    *,
    return_col: Literal["ret_1", "ret_5", "ret_10", "ret_20"] = "ret_1",
    factor_col: str = "factor_value",
    q: int = 10,
    min_assets_per_layer: int = 1,
    factor_name: str | None = None,
    allow_short: bool = True,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    output_dir: str | Path | None = None,
) -> BacktestResult:
    """Run quantile returns, optional top-minus-bottom, NAV, and turnover.

    ``allow_short=False`` is used for spot evaluations: layer returns and turnover
    remain available, while the long-short result files contain stable empty
    schemas instead of an infeasible synthetic short portfolio.
    """
    if return_col not in DEFAULT_RETURN_COLUMNS:
        raise ValueError(f"return_col must be one of {sorted(DEFAULT_RETURN_COLUMNS)}")
    assigned = assign_quantiles(
        panel,
        factor_col=factor_col,
        q=q,
        min_assets_per_layer=min_assets_per_layer,
        factor_name=factor_name,
    )
    turnover = compute_turnover(assigned)
    decile_returns = compute_decile_returns(
        assigned,
        return_col=return_col,
        factor_col=factor_col,
        q=q,
        min_assets_per_layer=min_assets_per_layer,
    )
    decile_returns = _apply_turnover_costs(
        decile_returns,
        turnover,
        cost_rate=_transaction_cost_rate(fee_bps, slippage_bps),
    )
    if allow_short:
        long_short_returns = compute_long_short_returns(decile_returns)
        long_short_nav = compute_nav(
            long_short_returns,
            return_col="gross_return",
        )
        long_short_nav["gross_nav"] = long_short_nav["nav"]
        net_nav = compute_nav(
            long_short_returns,
            return_col="net_return",
            nav_col="net_nav",
        )
        long_short_nav = long_short_nav.merge(
            net_nav[["timestamp", "factor_name", "net_nav"]],
            on=["timestamp", "factor_name"],
            how="left",
            validate="one_to_one",
        )
    else:
        LOGGER.info("Long-short results disabled for a long-only evaluation")
        long_short_returns = pd.DataFrame(columns=LONG_SHORT_COLUMNS)
        long_short_nav = pd.DataFrame(
            columns=[*LONG_SHORT_COLUMNS, "nav", "gross_nav", "net_nav"]
        )
    logs = pd.DataFrame(assigned.attrs.get("layer_log", []))
    result = BacktestResult(
        assignments=assigned,
        decile_returns=decile_returns,
        long_short_returns=long_short_returns,
        long_short_nav=long_short_nav,
        turnover=turnover,
        logs=logs,
    )
    if output_dir is not None:
        write_backtest_outputs(result, output_dir)
    return result


run_layer_backtest = run_backtest
