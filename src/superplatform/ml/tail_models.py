"""Dynamic downside-risk estimates for high-frequency research.

Historical VaR remains available as an audit baseline.  Position sizing can
instead use volatility-filtered historical simulation (FHS), a peaks-over-
threshold EVT correction, and a HAR-style realized-volatility forecast.
Every estimate is causal: callers pass only returns known at the decision time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

RISK_MODELS = ("historical", "filtered_historical", "hybrid_fhs_evt")


@dataclass(frozen=True)
class DynamicRiskEstimate:
    risk_model: str
    sample_count: int
    historical_var: float
    historical_expected_shortfall: float
    filtered_var: float
    filtered_expected_shortfall: float
    evt_var: float | None
    evt_expected_shortfall: float | None
    historical_annualized_volatility: float
    har_annualized_volatility_forecast: float | None
    selected_var: float
    selected_expected_shortfall: float
    selected_annualized_volatility: float
    evt_exceedances: int


def _clean_returns(returns: pd.Series) -> pd.Series:
    clean = pd.to_numeric(returns, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if isinstance(clean.index, pd.DatetimeIndex):
        clean = clean.sort_index()
    return clean.astype(float)


def _periods_per_year(index: pd.Index) -> float:
    if not isinstance(index, pd.DatetimeIndex) or len(index) < 2:
        return 365.25
    gaps = index.to_series().diff().dropna().dt.total_seconds()
    median_gap = float(gaps.median()) if not gaps.empty else 86_400.0
    if not np.isfinite(median_gap) or median_gap <= 0:
        median_gap = 86_400.0
    return float((365.25 * 86_400.0) / median_gap)


def _historical_tail(clean: pd.Series, confidence: float) -> tuple[float, float]:
    if clean.empty:
        return 0.0, 0.0
    quantile = float(clean.quantile(1.0 - confidence))
    tail = clean[clean <= quantile]
    var = max(0.0, -quantile)
    expected_shortfall = max(0.0, -float(tail.mean())) if not tail.empty else var
    return var, expected_shortfall


def _filtered_tail(
    clean: pd.Series,
    *,
    confidence: float,
    ewma_decay: float,
) -> tuple[float, float, np.ndarray, float]:
    """Return FHS VaR/ES, standardized losses and next-period volatility."""
    values = clean.to_numpy(dtype=float)
    if len(values) < 3:
        var, expected_shortfall = _historical_tail(clean, confidence)
        sigma = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        return var, expected_shortfall, np.asarray([], dtype=float), sigma

    seed = values[: min(20, len(values))]
    variance = max(float(np.var(seed, ddof=1)), 1e-12)
    standardized: list[float] = []
    for index, value in enumerate(values):
        sigma = float(np.sqrt(max(variance, 1e-12)))
        if index:
            standardized.append(float(value / sigma))
        variance = ewma_decay * variance + (1.0 - ewma_decay) * float(value**2)
    forecast_sigma = float(np.sqrt(max(variance, 1e-12)))
    residuals = np.asarray(standardized, dtype=float)
    residuals = residuals[np.isfinite(residuals)]
    if not len(residuals):
        var, expected_shortfall = _historical_tail(clean, confidence)
        return var, expected_shortfall, np.asarray([], dtype=float), forecast_sigma

    losses = -residuals
    threshold = float(np.quantile(losses, confidence))
    tail = losses[losses >= threshold]
    filtered_var = max(0.0, threshold * forecast_sigma)
    filtered_es = max(
        filtered_var,
        float(np.mean(tail) * forecast_sigma) if len(tail) else filtered_var,
    )
    return filtered_var, filtered_es, losses, forecast_sigma


def _evt_tail(
    standardized_losses: np.ndarray,
    *,
    forecast_sigma: float,
    confidence: float,
    threshold_quantile: float,
    min_exceedances: int,
) -> tuple[float | None, float | None, int]:
    """Fast method-of-moments POT/GPD estimate for the standardized loss tail."""
    losses = standardized_losses[np.isfinite(standardized_losses)]
    if len(losses) < min_exceedances:
        return None, None, 0
    threshold = float(np.quantile(losses, threshold_quantile))
    excesses = losses[losses > threshold] - threshold
    if len(excesses) < min_exceedances:
        return None, None, int(len(excesses))
    mean_excess = float(np.mean(excesses))
    variance_excess = float(np.var(excesses, ddof=1))
    if not np.isfinite(mean_excess) or mean_excess <= 0:
        return None, None, int(len(excesses))

    if not np.isfinite(variance_excess) or variance_excess <= 1e-15:
        shape = 0.0
    else:
        shape = 0.5 * (1.0 - mean_excess**2 / variance_excess)
        shape = float(np.clip(shape, -0.25, 0.49))
    scale = max(mean_excess * (1.0 - shape), 1e-12)
    exceedance_probability = float(len(excesses) / len(losses))
    tail_probability = 1.0 - confidence
    if tail_probability <= 0 or exceedance_probability <= 0:
        return None, None, int(len(excesses))
    ratio = exceedance_probability / tail_probability
    if abs(shape) < 1e-8:
        evt_quantile = threshold + scale * np.log(ratio)
    else:
        evt_quantile = threshold + scale / shape * (ratio**shape - 1.0)
    if not np.isfinite(evt_quantile):
        return None, None, int(len(excesses))
    conditional_scale = scale + shape * (evt_quantile - threshold)
    evt_es = evt_quantile + conditional_scale / (1.0 - shape)
    return (
        max(0.0, float(evt_quantile * forecast_sigma)),
        max(0.0, float(evt_es * forecast_sigma)),
        int(len(excesses)),
    )


def _har_volatility_forecast(
    clean: pd.Series,
    *,
    min_days: int,
) -> float | None:
    """Forecast annualized realized volatility with daily/weekly/monthly HAR terms."""
    if not isinstance(clean.index, pd.DatetimeIndex):
        return None
    daily_variance = clean.pow(2).groupby(clean.index.floor("D")).sum()
    daily_rv = np.sqrt(daily_variance).replace([np.inf, -np.inf], np.nan).dropna()
    if len(daily_rv) < max(min_days, 24):
        return None
    log_rv = np.log(daily_rv.clip(lower=1e-10))
    rows: list[list[float]] = []
    targets: list[float] = []
    for index in range(21, len(log_rv) - 1):
        rows.append(
            [
                1.0,
                float(log_rv.iloc[index]),
                float(log_rv.iloc[index - 4 : index + 1].mean()),
                float(log_rv.iloc[index - 21 : index + 1].mean()),
            ]
        )
        targets.append(float(log_rv.iloc[index + 1]))
    if len(rows) < 2:
        return None
    design = np.asarray(rows, dtype=float)
    response = np.asarray(targets, dtype=float)
    coefficients = np.linalg.lstsq(design, response, rcond=None)[0]
    latest = np.asarray(
        [
            1.0,
            float(log_rv.iloc[-1]),
            float(log_rv.iloc[-5:].mean()),
            float(log_rv.iloc[-22:].mean()),
        ],
        dtype=float,
    )
    forecast = float(np.exp(np.clip(latest @ coefficients, -20.0, 5.0)))
    annualized = forecast * np.sqrt(365.25)
    return float(annualized) if np.isfinite(annualized) else None


def estimate_dynamic_risk(
    returns: pd.Series,
    *,
    confidence: float = 0.95,
    risk_model: str = "hybrid_fhs_evt",
    ewma_decay: float = 0.94,
    evt_threshold_quantile: float = 0.90,
    evt_min_exceedances: int = 20,
    har_min_days: int = 30,
) -> DynamicRiskEstimate:
    """Estimate causal short-horizon loss tails and annualized volatility."""
    if risk_model not in RISK_MODELS:
        raise ValueError(f"unsupported risk model: {risk_model}")
    if not 0.5 < confidence < 1:
        raise ValueError("confidence must be in (0.5, 1)")
    if not 0 < ewma_decay < 1:
        raise ValueError("ewma_decay must be in (0, 1)")
    if not 0.5 < evt_threshold_quantile < confidence:
        raise ValueError("evt_threshold_quantile must be in (0.5, confidence)")
    if evt_min_exceedances < 5:
        raise ValueError("evt_min_exceedances must be at least 5")
    if har_min_days < 24:
        raise ValueError("har_min_days must be at least 24")

    clean = _clean_returns(returns)
    historical_var, historical_es = _historical_tail(clean, confidence)
    filtered_var, filtered_es, losses, forecast_sigma = _filtered_tail(
        clean,
        confidence=confidence,
        ewma_decay=ewma_decay,
    )
    evt_var, evt_es, evt_exceedances = _evt_tail(
        losses,
        forecast_sigma=forecast_sigma,
        confidence=confidence,
        threshold_quantile=evt_threshold_quantile,
        min_exceedances=evt_min_exceedances,
    )
    historical_volatility = (
        float(clean.std(ddof=1) * np.sqrt(_periods_per_year(clean.index)))
        if len(clean) > 1
        else 0.0
    )
    har_volatility = _har_volatility_forecast(clean, min_days=har_min_days)

    if risk_model == "historical":
        selected_var = historical_var
        selected_es = historical_es
        selected_volatility = historical_volatility
    elif risk_model == "filtered_historical":
        selected_var = filtered_var
        selected_es = filtered_es
        selected_volatility = max(historical_volatility, har_volatility or 0.0)
    else:
        selected_var = max(historical_var, filtered_var, evt_var or 0.0)
        selected_es = max(historical_es, filtered_es, evt_es or 0.0)
        selected_volatility = max(historical_volatility, har_volatility or 0.0)

    return DynamicRiskEstimate(
        risk_model=risk_model,
        sample_count=int(len(clean)),
        historical_var=historical_var,
        historical_expected_shortfall=historical_es,
        filtered_var=filtered_var,
        filtered_expected_shortfall=filtered_es,
        evt_var=evt_var,
        evt_expected_shortfall=evt_es,
        historical_annualized_volatility=historical_volatility,
        har_annualized_volatility_forecast=har_volatility,
        selected_var=selected_var,
        selected_expected_shortfall=selected_es,
        selected_annualized_volatility=selected_volatility,
        evt_exceedances=evt_exceedances,
    )
