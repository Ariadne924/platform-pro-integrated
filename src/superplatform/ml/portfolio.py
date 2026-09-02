"""Causal, risk-constrained asset allocation for ML score panels.

The predictor decides which assets are attractive.  This module separately
decides how much capital each selected asset receives using only returns that
were observable before the rebalance timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.optimize import minimize
from scipy.spatial.distance import squareform

from superplatform.ml.tail_models import RISK_MODELS, estimate_dynamic_risk

PORTFOLIO_METHODS = ("equal_weight", "inverse_volatility", "risk_parity", "hrp")


@dataclass(frozen=True)
class PortfolioConfig:
    method: str = "equal_weight"
    lookback_periods: int = 60
    min_history_periods: int = 20
    covariance_shrinkage: float = 0.10
    max_weight: float = 0.50
    confidence: float = 0.95
    var_limit: float = 0.03
    expected_shortfall_limit: float = 0.05
    annual_volatility_limit: float = 0.35
    risk_model: str = "hybrid_fhs_evt"
    risk_lookback_periods: int = 720
    ewma_decay: float = 0.94
    evt_threshold_quantile: float = 0.90
    evt_min_exceedances: int = 20
    har_min_days: int = 30
    soft_drawdown_limit: float = 0.15
    delever_drawdown_limit: float = 0.20
    hard_drawdown_limit: float = 0.25
    single_period_loss_limit: float = 0.10
    cooldown_periods: int = 20
    recovery_steps: tuple[float, ...] = (0.25, 0.50, 0.75, 1.0)

    def validate(self) -> None:
        if self.method not in PORTFOLIO_METHODS:
            raise ValueError(f"unsupported portfolio method: {self.method}")
        if self.lookback_periods < 2:
            raise ValueError("lookback_periods must be at least 2")
        if not 2 <= self.min_history_periods <= self.lookback_periods:
            raise ValueError("min_history_periods must be in [2, lookback_periods]")
        if not 0 <= self.covariance_shrinkage <= 1:
            raise ValueError("covariance_shrinkage must be in [0, 1]")
        if not 0 < self.max_weight <= 1:
            raise ValueError("max_weight must be in (0, 1]")
        if not 0.5 < self.confidence < 1:
            raise ValueError("confidence must be in (0.5, 1)")
        if self.var_limit <= 0 or self.expected_shortfall_limit <= 0:
            raise ValueError("VaR and Expected Shortfall limits must be positive")
        if self.annual_volatility_limit <= 0:
            raise ValueError("annual_volatility_limit must be positive")
        if self.risk_model not in RISK_MODELS:
            raise ValueError(f"unsupported risk model: {self.risk_model}")
        if self.risk_lookback_periods < self.min_history_periods:
            raise ValueError("risk_lookback_periods must cover min_history_periods")
        if not 0 < self.ewma_decay < 1:
            raise ValueError("ewma_decay must be in (0, 1)")
        if not 0.5 < self.evt_threshold_quantile < self.confidence:
            raise ValueError("evt_threshold_quantile must be in (0.5, confidence)")
        if self.evt_min_exceedances < 5:
            raise ValueError("evt_min_exceedances must be at least 5")
        if self.har_min_days < 24:
            raise ValueError("har_min_days must be at least 24")
        if not (
            0 < self.soft_drawdown_limit
            < self.delever_drawdown_limit
            < self.hard_drawdown_limit
            <= 1
        ):
            raise ValueError("drawdown limits must be ordered in (0, 1]")
        if not 0 < self.single_period_loss_limit <= 1:
            raise ValueError("single_period_loss_limit must be in (0, 1]")
        if self.cooldown_periods < 1:
            raise ValueError("cooldown_periods must be positive")
        if not self.recovery_steps or any(
            step <= 0 or step > 1 for step in self.recovery_steps
        ):
            raise ValueError("recovery_steps must contain values in (0, 1]")
        if tuple(sorted(self.recovery_steps)) != self.recovery_steps:
            raise ValueError("recovery_steps must be non-decreasing")


@dataclass
class PortfolioResult:
    signals: pd.DataFrame
    allocations: list[dict[str, Any]]
    risk_events: list[dict[str, Any]]


def _normalize(weights: pd.Series) -> pd.Series:
    clean = weights.astype(float).clip(lower=0.0).replace([np.inf, -np.inf], np.nan)
    clean = clean.fillna(0.0)
    total = float(clean.sum())
    if total <= 0:
        return pd.Series(1.0 / len(clean), index=clean.index, dtype=float)
    return clean / total


def _cap_weights(weights: pd.Series, max_weight: float) -> pd.Series:
    """Project long-only weights onto a capped simplex."""
    clean = _normalize(weights)
    effective_cap = max(float(max_weight), 1.0 / len(clean))
    for _ in range(len(clean) + 2):
        over = clean > effective_cap + 1e-12
        if not bool(over.any()):
            break
        excess = float((clean.loc[over] - effective_cap).sum())
        clean.loc[over] = effective_cap
        under = ~over
        capacity = effective_cap - clean.loc[under]
        capacity_total = float(capacity.clip(lower=0.0).sum())
        if capacity_total <= 0:
            break
        clean.loc[under] += excess * capacity.clip(lower=0.0) / capacity_total
    return _normalize(clean)


def _shrunk_covariance(returns: pd.DataFrame, shrinkage: float) -> pd.DataFrame:
    covariance = returns.cov().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    diagonal = np.diag(np.diag(covariance.to_numpy(dtype=float)))
    values = (1.0 - shrinkage) * covariance.to_numpy(dtype=float) + shrinkage * diagonal
    floor = max(float(np.nanmedian(np.diag(values))) * 1e-8, 1e-12)
    values[np.diag_indices_from(values)] = np.maximum(np.diag(values), floor)
    return pd.DataFrame(values, index=covariance.index, columns=covariance.columns)


def inverse_volatility_weights(
    returns: pd.DataFrame,
    *,
    max_weight: float = 1.0,
) -> pd.Series:
    volatility = returns.std(ddof=1).replace(0.0, np.nan)
    return _cap_weights(1.0 / volatility, max_weight)


def risk_parity_weights(
    returns: pd.DataFrame,
    *,
    covariance_shrinkage: float = 0.10,
    max_weight: float = 1.0,
) -> pd.Series:
    covariance = _shrunk_covariance(returns, covariance_shrinkage)
    matrix = covariance.to_numpy(dtype=float)
    initial = inverse_volatility_weights(returns, max_weight=max_weight).to_numpy()
    cap = max(float(max_weight), 1.0 / len(initial))

    def objective(weights: np.ndarray) -> float:
        variance = float(weights @ matrix @ weights)
        if variance <= 0:
            return 1e6
        contribution = weights * (matrix @ weights) / variance
        target = np.full(len(weights), 1.0 / len(weights))
        return float(np.square(contribution - target).sum())

    fitted = minimize(
        objective,
        initial,
        method="SLSQP",
        bounds=[(0.0, cap)] * len(initial),
        constraints={"type": "eq", "fun": lambda weights: float(weights.sum() - 1.0)},
        options={"maxiter": 300, "ftol": 1e-12},
    )
    values = fitted.x if fitted.success and np.isfinite(fitted.x).all() else initial
    return _cap_weights(pd.Series(values, index=returns.columns), max_weight)


def _cluster_variance(covariance: pd.DataFrame, names: list[str]) -> float:
    sub = covariance.loc[names, names]
    inverse = 1.0 / np.diag(sub.to_numpy(dtype=float))
    weights = inverse / inverse.sum()
    return float(weights @ sub.to_numpy(dtype=float) @ weights)


def hrp_weights(
    returns: pd.DataFrame,
    *,
    covariance_shrinkage: float = 0.10,
    max_weight: float = 1.0,
) -> pd.Series:
    if returns.shape[1] == 1:
        return pd.Series(1.0, index=returns.columns)
    covariance = _shrunk_covariance(returns, covariance_shrinkage)
    correlation = returns.corr().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    correlation_values = correlation.to_numpy(dtype=float, copy=True)
    np.fill_diagonal(correlation_values, 1.0)
    distance = np.sqrt(np.clip((1.0 - correlation_values) / 2.0, 0.0, 1.0))
    order = leaves_list(linkage(squareform(distance, checks=False), method="single"))
    ordered = [str(returns.columns[index]) for index in order]
    weights = pd.Series(1.0, index=ordered, dtype=float)
    clusters: list[list[str]] = [ordered]
    while clusters:
        next_clusters: list[list[str]] = []
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            split = len(cluster) // 2
            left, right = cluster[:split], cluster[split:]
            left_variance = _cluster_variance(covariance, left)
            right_variance = _cluster_variance(covariance, right)
            denominator = left_variance + right_variance
            left_share = right_variance / denominator if denominator > 0 else 0.5
            weights.loc[left] *= left_share
            weights.loc[right] *= 1.0 - left_share
            next_clusters.extend([left, right])
        clusters = next_clusters
    return _cap_weights(weights.reindex(returns.columns), max_weight)


def allocate_weights(returns: pd.DataFrame, config: PortfolioConfig) -> pd.Series:
    config.validate()
    if returns.shape[1] < 1:
        raise ValueError("asset allocation requires at least one asset")
    if config.method == "equal_weight":
        raw = pd.Series(1.0, index=returns.columns, dtype=float)
        return _cap_weights(raw, config.max_weight)
    if config.method == "inverse_volatility":
        return inverse_volatility_weights(returns, max_weight=config.max_weight)
    if config.method == "risk_parity":
        return risk_parity_weights(
            returns,
            covariance_shrinkage=config.covariance_shrinkage,
            max_weight=config.max_weight,
        )
    return hrp_weights(
        returns,
        covariance_shrinkage=config.covariance_shrinkage,
        max_weight=config.max_weight,
    )


def build_portfolio_signals(
    scores: pd.Series,
    prices: pd.DataFrame,
    *,
    top_n: int,
    config: PortfolioConfig,
    name: str,
) -> PortfolioResult:
    """Turn cross-sectional scores into causal, risk-constrained target weights."""
    config.validate()
    if not isinstance(scores.index, pd.MultiIndex):
        raise ValueError("scores must use a timestamp/symbol MultiIndex")
    required = {"timestamp", "symbol", "close"}
    missing = sorted(required - set(prices.columns))
    if missing:
        raise ValueError(f"prices are missing required columns: {missing}")
    close = prices.copy()
    close["timestamp"] = pd.to_datetime(close["timestamp"], utc=True, errors="coerce")
    close = close.pivot_table(index="timestamp", columns="symbol", values="close", aggfunc="last")
    asset_returns = close.sort_index().pct_change()
    rows: list[dict[str, Any]] = []
    allocations: list[dict[str, Any]] = []
    risk_events: list[dict[str, Any]] = []
    clean_scores = pd.to_numeric(scores, errors="coerce")
    previous_weights = pd.Series(dtype=float)
    risk_equity = 1.0
    risk_peak = 1.0
    cooldown_remaining = 0
    recovery_index = len(config.recovery_steps)
    previous_risk_signature: tuple[str, tuple[str, ...]] = ("normal", ())

    for timestamp, group in clean_scores.groupby(level="timestamp", sort=True):
        period_return = 0.0
        if not previous_weights.empty and timestamp in asset_returns.index:
            observed = asset_returns.loc[timestamp].reindex(previous_weights.index)
            period_return = max(
                -1.0,
                float(observed.fillna(0.0).dot(previous_weights)),
            )
            risk_equity *= 1.0 + period_return
            risk_peak = max(risk_peak, risk_equity)
        risk_drawdown = max(0.0, 1.0 - risk_equity / risk_peak)
        hard_reason: str | None = None
        if cooldown_remaining == 0:
            if period_return <= -config.single_period_loss_limit:
                hard_reason = "single_period_loss"
            elif risk_drawdown >= config.hard_drawdown_limit:
                hard_reason = "hard_drawdown"
        if hard_reason is not None:
            cooldown_remaining = config.cooldown_periods
            recovery_index = 0
            risk_events.append(
                {
                    "timestamp": pd.Timestamp(timestamp).isoformat(),
                    "event": "forced_liquidation",
                    "reason": hard_reason,
                    "period_return": period_return,
                    "drawdown": risk_drawdown,
                    "equity": risk_equity,
                    "cooldown_periods": config.cooldown_periods,
                }
            )

        ranked = group.droplevel("timestamp").dropna().sort_values(ascending=False)
        selected = [str(name_) for name_ in ranked.head(min(top_n, len(ranked))).index]
        if not selected:
            continue
        available_history = asset_returns.loc[
            asset_returns.index < timestamp, selected
        ].dropna()
        history = available_history.tail(config.lookback_periods)
        risk_history = available_history.tail(config.risk_lookback_periods)
        fallback = len(history) < config.min_history_periods
        if fallback:
            weights = _cap_weights(
                pd.Series(1.0, index=selected, dtype=float), config.max_weight
            )
        else:
            weights = allocate_weights(history, config)
        portfolio_returns = risk_history.loc[:, weights.index].dot(weights).dropna()
        risk = estimate_dynamic_risk(
            portfolio_returns,
            confidence=config.confidence,
            risk_model=config.risk_model,
            ewma_decay=config.ewma_decay,
            evt_threshold_quantile=config.evt_threshold_quantile,
            evt_min_exceedances=config.evt_min_exceedances,
            har_min_days=config.har_min_days,
        )
        var = risk.selected_var
        expected_shortfall = risk.selected_expected_shortfall
        annualized_volatility = risk.selected_annualized_volatility
        scale = 1.0
        if var > 0:
            scale = min(scale, config.var_limit / var)
        if expected_shortfall > 0:
            scale = min(scale, config.expected_shortfall_limit / expected_shortfall)
        if annualized_volatility > 0:
            scale = min(scale, config.annual_volatility_limit / annualized_volatility)
        active_limits: list[str] = []
        if var > config.var_limit:
            active_limits.append("var_limit")
        if expected_shortfall > config.expected_shortfall_limit:
            active_limits.append("expected_shortfall_limit")
        if annualized_volatility > config.annual_volatility_limit:
            active_limits.append("annual_volatility_limit")
        circuit_state = "normal"
        circuit_scale = 1.0
        if cooldown_remaining > 0:
            circuit_state = "cooldown"
            circuit_scale = 0.0
            cooldown_remaining -= 1
            if cooldown_remaining == 0:
                risk_peak = risk_equity
        elif recovery_index < len(config.recovery_steps):
            circuit_state = "recovery"
            circuit_scale = config.recovery_steps[recovery_index]
            recovery_index += 1
        elif risk_drawdown >= config.delever_drawdown_limit:
            circuit_state = "delever"
            width = config.hard_drawdown_limit - config.delever_drawdown_limit
            circuit_scale = 0.25 + 0.25 * max(
                0.0,
                (config.hard_drawdown_limit - risk_drawdown) / width,
            )
        elif risk_drawdown >= config.soft_drawdown_limit:
            circuit_state = "warning"
            width = config.delever_drawdown_limit - config.soft_drawdown_limit
            circuit_scale = 0.50 + 0.50 * max(
                0.0,
                (config.delever_drawdown_limit - risk_drawdown) / width,
            )
        scale = min(scale, circuit_scale)
        scale = float(np.clip(scale, 0.0, 1.0))
        risk_signature = (circuit_state, tuple(active_limits))
        if risk_signature != previous_risk_signature:
            previous_state, previous_limits = previous_risk_signature
            triggered = circuit_state != "normal" or bool(active_limits)
            action = {
                "warning": "reduce_exposure",
                "delever": "forced_deleveraging",
                "cooldown": "hold_cash",
                "recovery": "staged_recovery",
                "normal": "risk_limit_scaling" if active_limits else "resume_normal",
            }[circuit_state]
            risk_events.append(
                {
                    "timestamp": pd.Timestamp(timestamp).isoformat(),
                    "event": "risk_triggered" if triggered else "risk_recovered",
                    "reason": circuit_state if circuit_state != "normal" else (
                        active_limits[0] if active_limits else "risk_cleared"
                    ),
                    "action": action,
                    "state_from": previous_state,
                    "state_to": circuit_state,
                    "active_limits": active_limits,
                    "previous_active_limits": list(previous_limits),
                    "period_return": period_return,
                    "drawdown": risk_drawdown,
                    "equity": risk_equity,
                    "risk_scale": scale,
                    "circuit_scale": circuit_scale,
                    "observed": {
                        "var": var,
                        "expected_shortfall": expected_shortfall,
                        "annualized_volatility": annualized_volatility,
                    },
                    "thresholds": {
                        "var_limit": config.var_limit,
                        "expected_shortfall_limit": config.expected_shortfall_limit,
                        "annual_volatility_limit": config.annual_volatility_limit,
                        "soft_drawdown_limit": config.soft_drawdown_limit,
                        "delever_drawdown_limit": config.delever_drawdown_limit,
                        "hard_drawdown_limit": config.hard_drawdown_limit,
                        "single_period_loss_limit": config.single_period_loss_limit,
                    },
                }
            )
            previous_risk_signature = risk_signature
        constrained = weights * scale
        previous_weights = constrained.copy()
        for symbol in ranked.index.astype(str):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "position": float(constrained.get(symbol, 0.0)),
                }
            )
        allocations.append(
            {
                "timestamp": pd.Timestamp(timestamp).isoformat(),
                "method": config.method,
                "selected": selected,
                "weights": {symbol: float(constrained[symbol]) for symbol in constrained.index},
                "cash_weight": float(max(0.0, 1.0 - constrained.sum())),
                "risk_model": risk.risk_model,
                "estimated_var": var,
                "estimated_expected_shortfall": expected_shortfall,
                "estimated_annualized_volatility": annualized_volatility,
                "historical_var": risk.historical_var,
                "historical_expected_shortfall": risk.historical_expected_shortfall,
                "filtered_var": risk.filtered_var,
                "filtered_expected_shortfall": risk.filtered_expected_shortfall,
                "evt_var": risk.evt_var,
                "evt_expected_shortfall": risk.evt_expected_shortfall,
                "evt_exceedances": risk.evt_exceedances,
                "historical_annualized_volatility": (
                    risk.historical_annualized_volatility
                ),
                "har_annualized_volatility_forecast": (
                    risk.har_annualized_volatility_forecast
                ),
                "risk_scale": scale,
                "risk_constraint_triggered": scale < 1.0,
                "circuit_breaker_state": circuit_state,
                "circuit_scale": circuit_scale,
                "period_return": period_return,
                "risk_drawdown": risk_drawdown,
                "cooldown_remaining": cooldown_remaining,
                "history_periods": len(history),
                "risk_history_periods": len(risk_history),
                "fallback_equal_weight": fallback,
            }
        )
    signals = pd.DataFrame(rows, columns=["timestamp", "symbol", "position"])
    signals.attrs["strategy_name"] = name
    return PortfolioResult(
        signals=signals,
        allocations=allocations,
        risk_events=risk_events,
    )
