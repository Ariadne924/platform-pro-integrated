"""Cross-asset and carry-curve proxy factors."""

import pandas as pd

from superplatform.factors.base import FactorCategory, factor
from superplatform.utils.timestamps import lookback_bars


def _pair_kline(data):
    symbols = list(data["kline"])
    if len(symbols) != 2:
        raise ValueError("cross-asset factors require exactly two kline symbols")
    left = data["kline"][symbols[0]][["timestamp", "close"]].copy()
    right = data["kline"][symbols[1]][["timestamp", "close"]].copy()
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True)
    right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True)
    left = left.sort_values("timestamp").rename(columns={"close": "left_close"})
    right = right.sort_values("timestamp").rename(columns={"close": "right_close"})
    return pd.merge(left, right, on="timestamp", how="inner")


def _pair_result(aligned, value):
    return pd.DataFrame({"timestamp": aligned["timestamp"], "value": value})


def _basis_funding_frame(data):
    basis = list(data["basis"].values())[0][["timestamp", "basis_pct"]].copy()
    funding = list(data["funding_rate"].values())[0][
        ["timestamp", "funding_rate"]
    ].copy()
    basis["timestamp"] = pd.to_datetime(basis["timestamp"], utc=True)
    funding["timestamp"] = pd.to_datetime(funding["timestamp"], utc=True)
    basis = basis.sort_values("timestamp")
    funding = funding.sort_values("timestamp")
    return pd.merge_asof(basis, funding, on="timestamp", direction="backward")


@factor(
    name="cross_asset_relative_momentum",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Relative 20-period momentum of the first asset versus the second",
    required_data=["kline"],
    required_symbols=2,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Relative momentum lookback period",
            "min": 1,
            "max": 500,
        }
    },
)
def cross_asset_relative_momentum(data, **params):
    aligned = _pair_kline(data)
    period = lookback_bars(aligned, params.get("lookback_days", 20))
    value = aligned["left_close"].pct_change(period) - aligned["right_close"].pct_change(period)
    return _pair_result(aligned, value)


@factor(
    name="cross_asset_correlation",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Rolling correlation of returns between two assets",
    required_data=["kline"],
    required_symbols=2,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 30,
            "description": "Cross-asset correlation lookback period",
            "min": 3,
            "max": 500,
        }
    },
)
def cross_asset_correlation(data, **params):
    aligned = _pair_kline(data)
    period = lookback_bars(aligned, params.get("lookback_days", 30))
    left_returns = aligned["left_close"].pct_change()
    right_returns = aligned["right_close"].pct_change()
    return _pair_result(aligned, left_returns.rolling(period).corr(right_returns))


@factor(
    name="cross_asset_beta",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Rolling return beta of the first asset to the second",
    required_data=["kline"],
    required_symbols=2,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 30,
            "description": "Cross-asset beta lookback period",
            "min": 3,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for reference variance",
            "min": 0.0,
        },
    },
)
def cross_asset_beta(data, **params):
    aligned = _pair_kline(data)
    period = lookback_bars(aligned, params.get("lookback_days", 30))
    epsilon = params.get("epsilon", 1.0e-12)
    left_returns = aligned["left_close"].pct_change()
    right_returns = aligned["right_close"].pct_change()
    covariance = left_returns.rolling(period).cov(right_returns)
    variance = right_returns.rolling(period).var()
    return _pair_result(aligned, covariance / (variance + epsilon))


@factor(
    name="cross_asset_volatility_ratio",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Rolling realized volatility of the first asset relative to the second",
    required_data=["kline"],
    required_symbols=2,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 20,
            "description": "Cross-asset volatility lookback period",
            "min": 2,
            "max": 500,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for reference volatility",
            "min": 0.0,
        },
    },
)
def cross_asset_volatility_ratio(data, **params):
    aligned = _pair_kline(data)
    period = lookback_bars(aligned, params.get("lookback_days", 20))
    epsilon = params.get("epsilon", 1.0e-12)
    left_vol = aligned["left_close"].pct_change().rolling(period).std()
    right_vol = aligned["right_close"].pct_change().rolling(period).std()
    return _pair_result(aligned, left_vol / (right_vol + epsilon))


@factor(
    name="basis_funding_divergence",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Daily basis minus trailing annualized funding-rate proxy",
    required_data=["basis", "funding_rate"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 30,
            "description": "Carry divergence smoothing period",
            "min": 2,
            "max": 500,
        }
    },
)
def basis_funding_divergence(data, **params):
    aligned = _basis_funding_frame(data)
    period = lookback_bars(aligned, params.get("lookback_days", 30))
    annualized_funding = aligned["funding_rate"] * 365 * 3 * 100
    return _pair_result(
        aligned,
        aligned["basis_pct"] - annualized_funding.rolling(period).mean(),
    )


@factor(
    name="basis_funding_divergence_zscore",
    category=FactorCategory.CRYPTO_SPECIFIC,
    description="Standardized basis-funding carry divergence proxy",
    required_data=["basis", "funding_rate"],
    required_symbols=1,
    params_schema={
        "lookback_days": {
            "type": "int",
            "default": 60,
            "description": "Carry divergence z-score period",
            "min": 3,
            "max": 1000,
        },
        "epsilon": {
            "type": "float",
            "default": 1.0e-12,
            "description": "Numerical floor for divergence volatility",
            "min": 0.0,
        },
    },
)
def basis_funding_divergence_zscore(data, **params):
    aligned = _basis_funding_frame(data)
    period = lookback_bars(aligned, params.get("lookback_days", 60))
    epsilon = params.get("epsilon", 1.0e-12)
    annualized_funding = aligned["funding_rate"] * 365 * 3 * 100
    divergence = aligned["basis_pct"] - annualized_funding.rolling(period).mean()
    mean = divergence.rolling(period).mean()
    std = divergence.rolling(period).std()
    return _pair_result(aligned, (divergence - mean) / (std + epsilon))
