"""Crypto-specific factors — funding rate, open interest, basis.

These factors consume data types unique to perpetual futures markets.
They require providers that serve "funding_rate", "open_interest",
or "basis" data (e.g. BinanceAdapter wrapped via DataProvider).
"""

import numpy as np
import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.factors.param_schema import factor_params
from superplatform.utils.timestamps import lookback_bars, median_bar_seconds

# ── Annualized periods per year at common intervals ──────────────
_PERIODS_PER_YEAR = {
    8 * 60 * 60 * 1000: 365 * 3,   # 8-hour funding cycle → ~1095 periods/year
}


def _get_periods_per_year(freq_ms: float) -> float:
    """Estimate periods-per-year for annualization from interval in ms."""
    for known_ms, ppy in _PERIODS_PER_YEAR.items():
        if abs(freq_ms - known_ms) < 1000:
            return ppy
    # Fallback: compute from ms
    return (365.25 * 24 * 3600 * 1000) / freq_ms


@factor(
    name="funding_rate_annualized",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Latest funding rate annualized (%). Positive → longs pay shorts.",
    required_data=["funding_rate"],
    required_symbols=1,
)
def funding_rate_annualized(data, **params):
    """Annualize the latest funding rate per timestamp.

    Uses the per-exchange funding interval (typically 8h for Binance)
    to annualize. This is NOT a rolling average — each bar's funding_rate
    is annualized independently, representing the implied cost of leverage
    at that point in time.
    """
    fr = list(data["funding_rate"].values())[0]
    result = pd.DataFrame({"timestamp": fr["timestamp"].copy()})

    # Estimate funding interval from timestamp spacing
    freq_seconds = median_bar_seconds(fr)
    if freq_seconds > 0:
        ppy = _get_periods_per_year(freq_seconds * 1000)
    else:
        ppy = 365 * 3  # default: assume 8h

    rate = fr["funding_rate"].astype(float)
    result["value"] = rate * ppy * 100  # annualized %
    # Clamp extreme values (exchange errors, early listings)
    result["value"] = result["value"].clip(-500, 500)
    return result


@factor(
    name="oi_change_ratio",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="60-period open interest change ratio — OI momentum",
    required_data=["open_interest"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 10,
            "description": "持仓量变化回看天数（默认 10 天 ≈ 4h 下 60 根）",
            "min": 1,
            "max": 500,
        }
    },
)
def oi_change_ratio(data, **params):
    """Rate of change of open interest over the lookback window.

    Positive → OI expanding (new money entering).
    Negative → OI contracting (positions being closed / liquidated).

    Often used with price direction:
      Price ↑ + OI ↑ → trend confirmation
      Price ↑ + OI ↓ → short covering, trend weakening
    """
    oi = list(data["open_interest"].values())[0]
    period = lookback_bars(oi, params.get("lookback_days", 10))
    result = pd.DataFrame({"timestamp": oi["timestamp"].copy()})

    oi_series = oi["open_interest"].astype(float)
    result["value"] = oi_series.pct_change(period)
    return result


@factor(
    name="basis_latest",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Spot-perpetual basis (%) — premium of perp over spot",
    required_data=["basis"],
    required_symbols=1,
)
def basis_latest(data, **params):
    """Spot-perp basis as a percentage.

    Positive basis → perpetual trades above spot (bullish sentiment,
    leveraged longs paying premium).
    Negative basis → perpetual trades below spot (bearish sentiment).

    This factor is a pass-through of pre-computed basis data.
    """
    basis = list(data["basis"].values())[0]
    result = pd.DataFrame({"timestamp": basis["timestamp"].copy()})
    result["value"] = basis["basis_pct"].astype(float)
    return result


@factor(
    name="funding_rate_zscore",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="30-period funding-rate rolling z-score",
    required_data=["funding_rate"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=30),
)
def funding_rate_zscore(data, **params):
    epsilon = params.get("epsilon", 1e-12)
    fr = list(data["funding_rate"].values())[0]
    period = lookback_bars(fr, params.get("lookback_days", 30))
    rate = fr["funding_rate"].astype(float)
    mean = rate.rolling(period).mean()
    std = rate.rolling(period).std().clip(lower=epsilon)
    result = pd.DataFrame({"timestamp": fr["timestamp"].copy()})
    result["value"] = (rate - mean) / std
    return result


@factor(
    name="oi_price_divergence",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="20-period open-interest return minus price return",
    required_data=["kline", "open_interest"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def oi_price_divergence(data, **params):
    kline = list(data["kline"].values())[0]
    oi = list(data["open_interest"].values())[0]
    left = kline[["timestamp", "close"]].copy()
    right = oi[["timestamp", "open_interest"]].copy()
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True)
    right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True)
    aligned = pd.merge_asof(
        left.sort_values("timestamp"),
        right.sort_values("timestamp"),
        on="timestamp",
        direction="backward",
    )
    period = lookback_bars(aligned, params.get("lookback_days", 20))
    result = pd.DataFrame({"timestamp": aligned["timestamp"]})
    result["value"] = (
        aligned["open_interest"].pct_change(period)
        - aligned["close"].pct_change(period)
    )
    return result


@factor(
    name="basis_zscore",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="30-period basis rolling z-score",
    required_data=["basis"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=30),
)
def basis_zscore(data, **params):
    epsilon = params.get("epsilon", 1e-12)
    basis = list(data["basis"].values())[0]
    period = lookback_bars(basis, params.get("lookback_days", 30))
    value = basis["basis_pct"].astype(float)
    mean = value.rolling(period).mean()
    std = value.rolling(period).std().clip(lower=epsilon)
    result = pd.DataFrame({"timestamp": basis["timestamp"].copy()})
    result["value"] = (value - mean) / std
    return result


@factor(
    name="crypto_gap_risk",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="20-period mean absolute overnight-style open-to-previous-close move",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_gap_risk(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    gap = (kline["open"] / kline["close"].shift(1) - 1.0).abs()
    result = pd.DataFrame({"timestamp": kline["timestamp"].copy()})
    result["value"] = gap.rolling(period).mean()
    return result


@factor(
    name="crypto_tail_risk",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="60-period rolling 95th percentile of absolute returns",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=60, quantile=0.95),
)
def crypto_tail_risk(data, **params):
    quantile = params.get("quantile", 0.95)
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 60))
    absolute_return = kline["close"].pct_change().abs()
    result = pd.DataFrame({"timestamp": kline["timestamp"].copy()})
    result["value"] = absolute_return.rolling(period).quantile(quantile)
    return result


@factor(
    name="crypto_volume_volatility",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="20-period volatility of log-volume changes",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_volume_volatility(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    log_volume_change = np.log(kline["volume"].clip(lower=1e-12)).diff()
    result = pd.DataFrame({"timestamp": kline["timestamp"].copy()})
    result["value"] = log_volume_change.rolling(period).std()
    return result


@factor(
    name="crypto_return_volume_corr",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="20-period rolling correlation between returns and log-volume changes",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_return_volume_corr(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    returns = kline["close"].pct_change()
    log_volume_change = np.log(kline["volume"].clip(lower=1e-12)).diff()
    result = pd.DataFrame({"timestamp": kline["timestamp"].copy()})
    result["value"] = returns.rolling(period).corr(log_volume_change)
    return result


@factor(
    name="crypto_weekend_activity_ratio",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="20-period weekend volume divided by total volume",
    required_data=["kline"],
    required_symbols=1,
    params_schema=factor_params(lookback_days=20),
)
def crypto_weekend_activity_ratio(data, **params):
    kline = list(data["kline"].values())[0]
    period = lookback_bars(kline, params.get("lookback_days", 20))
    timestamps = pd.to_datetime(kline["timestamp"], utc=True)
    weekend_volume = kline["volume"].where(timestamps.dt.dayofweek >= 5, 0.0)
    total_volume = kline["volume"].rolling(period).mean()
    result = pd.DataFrame({"timestamp": kline["timestamp"].copy()})
    result["value"] = weekend_volume.rolling(period).mean() / total_volume
    return result
