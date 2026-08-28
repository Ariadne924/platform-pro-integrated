"""End-to-end ML research orchestration over the platform factor panel."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from superplatform.consumption.backtest import BacktestResult, backtest
from superplatform.ml.comparison import compare_strategy_returns
from superplatform.ml.models import SUPPORTED_MODELS, WalkForwardConfig, walk_forward_panel
from superplatform.ml.regime import RegimeConfig, detect_market_regime
from superplatform.ml.risk import ScoreConfig, score_research_result, tail_risk_metrics


@dataclass(frozen=True)
class MLResearchConfig:
    target_horizon: int = 1
    top_n: int = 3
    frequency: str = "1d"
    taker_fee_bps: float = 4.0
    slippage_bps: float = 2.0
    models: tuple[str, ...] = SUPPORTED_MODELS
    walk_forward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    score: ScoreConfig = field(default_factory=ScoreConfig)
    reference_symbol: str | None = None

    def validate(self) -> None:
        if self.target_horizon not in {1, 5, 10, 20}:
            raise ValueError("target_horizon must be one of 1, 5, 10, 20")
        if self.top_n < 1:
            raise ValueError("top_n must be positive")
        if self.taker_fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("cost assumptions must be non-negative")
        self.walk_forward.validate()
        self.regime.validate()
        self.score.validate()


def prepare_ml_panel(
    panel: pd.DataFrame,
    *,
    target_horizon: int,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Convert the long factor-evaluation panel into features, target and prices."""
    target_column = f"ret_{target_horizon}"
    required = {"timestamp", "symbol", "factor_name", "factor_value", "close", target_column}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"ML panel is missing required columns: {missing}")
    working = panel.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
    if working["timestamp"].isna().any():
        raise ValueError("ML panel contains invalid timestamps")
    working["symbol"] = working["symbol"].astype(str)
    working["factor_name"] = working["factor_name"].astype(str)
    keys = ["timestamp", "symbol"]
    factor_rows = working.drop_duplicates([*keys, "factor_name"], keep="first")
    features = (
        factor_rows.set_index([*keys, "factor_name"])["factor_value"]
        .unstack("factor_name")
        .sort_index()
    )
    features.columns.name = None
    base = (
        working.sort_values([*keys, "factor_name"])
        .drop_duplicates(keys, keep="first")
        .set_index(keys)[[target_column, "close"]]
        .sort_index()
    )
    common = features.index.intersection(base.index)
    features = features.reindex(common)
    target = pd.to_numeric(base.loc[common, target_column], errors="coerce").rename("target")
    prices = base.loc[common, ["close"]].reset_index()
    return features, target, prices


def _equal_weight_factor_score(features: pd.DataFrame) -> pd.Series:
    def _zscore(group: pd.DataFrame) -> pd.DataFrame:
        means = group.mean()
        stds = group.std(ddof=0).replace(0.0, np.nan)
        normalized = group.sub(means).div(stds)
        # A one-asset cross-section has no rank information. Preserve the
        # direction of its raw features without inventing cross-asset spread.
        if len(group) == 1:
            normalized = np.sign(group).astype(float)
        return normalized

    # Avoid DataFrameGroupBy.apply(include_groups=...), which was only added in
    # pandas 2.2. The project supports pandas >= 2.0.
    normalized = pd.concat(
        [_zscore(group) for _, group in features.groupby(level="timestamp", sort=False)]
    ).sort_index()
    return normalized.mean(axis=1, skipna=True).rename("equal_weight_score")


def _scores_to_signals(scores: pd.Series, top_n: int, name: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    clean = pd.to_numeric(scores, errors="coerce")
    for timestamp, group in clean.groupby(level="timestamp", sort=True):
        values = group.droplevel("timestamp").dropna().sort_values(ascending=False)
        if values.empty:
            continue
        selected = set(values.head(min(top_n, len(values))).index.astype(str))
        for symbol in values.index.astype(str):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "position": 1.0 if symbol in selected else 0.0,
                }
            )
    result = pd.DataFrame(rows, columns=["timestamp", "symbol", "position"])
    result.attrs["strategy_name"] = name
    return result


def _price_data(prices: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        str(symbol): group[["timestamp", "close"]].sort_values("timestamp").reset_index(drop=True)
        for symbol, group in prices.groupby("symbol", sort=True)
    }


def _periods_per_year(frequency: str) -> int:
    return {
        "1m": 365 * 24 * 60,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "1h": 365 * 24,
        "4h": 365 * 6,
        "6h": 365 * 4,
        "8h": 365 * 3,
        "1d": 365,
    }.get(frequency, 365)


def _backtest_payload(result: BacktestResult) -> tuple[dict[str, Any], pd.Series]:
    equity = result.equity.copy()
    equity["timestamp"] = pd.to_datetime(equity["timestamp"], utc=True)
    series = equity.set_index("timestamp")["equity"].pct_change().dropna()
    payload = {
        "metrics": {
            "total_return": float(result.total_return),
            "annual_return": float(result.annual_return),
            "annual_vol": float(result.annual_vol),
            "sharpe": float(result.sharpe),
            "max_drawdown": float(result.max_drawdown),
            "win_rate": float(result.win_rate),
            "avg_return": float(result.avg_return),
        },
        "equity": [
            {"timestamp": row.timestamp.isoformat(), "equity": float(row.equity)}
            for row in equity.itertuples(index=False)
        ],
    }
    return payload, series


def _correlation_metrics(prediction: pd.Series, target: pd.Series) -> dict[str, Any]:
    aligned = pd.concat([prediction.rename("prediction"), target], axis=1).dropna()
    rows: list[dict[str, Any]] = []
    for timestamp, group in aligned.groupby(level="timestamp", sort=True):
        if (
            len(group) < 2
            or group["prediction"].nunique() < 2
            or group["target"].nunique() < 2
        ):
            continue
        ic = group["prediction"].corr(group["target"], method="pearson")
        rank_ic = group["prediction"].corr(group["target"], method="spearman")
        rows.append(
            {
                "timestamp": pd.Timestamp(timestamp).isoformat(),
                "ic": float(ic) if pd.notna(ic) else None,
                "rank_ic": float(rank_ic) if pd.notna(rank_ic) else None,
            }
        )
    ic_values = [row["ic"] for row in rows if row["ic"] is not None]
    rank_values = [row["rank_ic"] for row in rows if row["rank_ic"] is not None]
    return {
        "ic": float(np.mean(ic_values)) if ic_values else None,
        "rank_ic": float(np.mean(rank_values)) if rank_values else None,
        "sample_periods": len(rows),
        "series": rows,
    }


def _slice_return_metrics(returns: pd.Series) -> dict[str, Any]:
    clean = returns.dropna()
    if clean.empty:
        return {"sample_count": 0, "total_return": None, "sharpe": None}
    std = float(clean.std(ddof=1)) if len(clean) > 1 else 0.0
    return {
        "sample_count": len(clean),
        "total_return": float((1.0 + clean).prod() - 1.0),
        "sharpe": float(clean.mean() / std) if std > 0 else None,
    }


def _fold_return_metrics(
    returns: pd.Series,
    folds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fold in folds:
        start, end = pd.Timestamp(fold["test_start"]), pd.Timestamp(fold["test_end"])
        metrics = _slice_return_metrics(returns.loc[start:end])
        metrics.update({"test_start": fold["test_start"], "test_end": fold["test_end"]})
        rows.append(metrics)
    return rows


def _regime_return_metrics(
    returns: pd.Series,
    regime: pd.DataFrame,
) -> dict[str, dict[str, Any]]:
    regime_for_returns = regime["regime"].reindex(returns.index, method="ffill")
    return {
        name: _slice_return_metrics(returns[regime_for_returns.eq(name)])
        for name in ("bull", "bear", "sideways")
    }


def _feature_recommendations(folds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for fold in folds:
        model_weights = fold.get("model_weights", {})
        for weights in model_weights.values():
            for feature, value in weights.items():
                buckets.setdefault(feature, []).append(float(value))
    rows = []
    total_models = max(1, sum(len(fold.get("model_weights", {})) for fold in folds))
    for feature, values in buckets.items():
        array: np.ndarray = np.asarray(values, dtype=float)
        rows.append(
            {
                "feature": feature,
                "mean_weight": float(array.mean()),
                "mean_absolute_weight": float(np.abs(array).mean()),
                "selection_frequency": len(values) / total_models,
                "sign_consistency": float(abs(np.sign(array).mean())),
            }
        )
    return sorted(rows, key=lambda row: row["mean_absolute_weight"], reverse=True)


def run_ml_research(panel: pd.DataFrame, *, config: MLResearchConfig | None = None) -> dict[str, Any]:
    """Run the first complete ML → signals → unified backtest → score slice."""
    config = config or MLResearchConfig()
    config.validate()
    features, target, prices = prepare_ml_panel(
        panel, target_horizon=config.target_horizon
    )
    if features.shape[1] < 1 or len(features.index.get_level_values("symbol").unique()) < 2:
        raise ValueError("ML research requires at least one factor and two symbols")
    walk_forward = walk_forward_panel(
        features,
        target,
        config=config.walk_forward,
        models=config.models,
    )
    ensemble = walk_forward.predictions["ensemble"].dropna()
    if ensemble.empty or not walk_forward.folds:
        raise ValueError("insufficient history for the configured walk-forward run")
    oos_index = ensemble.index
    baseline_score = _equal_weight_factor_score(features).reindex(oos_index)
    strategy_scores = {
        model: walk_forward.predictions[model].dropna()
        for model in config.models
    }
    strategy_scores["ensemble"] = ensemble
    strategy_scores["equal_weight"] = baseline_score.dropna()
    shared_score_frame = pd.concat(
        [scores.rename(name) for name, scores in strategy_scores.items()],
        axis=1,
        join="inner",
    ).dropna()
    if shared_score_frame.empty:
        raise ValueError("strategies do not share an out-of-sample comparison window")
    strategy_scores = {
        name: shared_score_frame[name].rename(name)
        for name in strategy_scores
    }
    prices_by_symbol = _price_data(prices)
    kwargs = {
        "price_data": prices_by_symbol,
        "periods_per_year": _periods_per_year(config.frequency),
        "taker_fee_bps": config.taker_fee_bps,
        "slippage_bps": config.slippage_bps,
    }
    backtest_payloads: dict[str, dict[str, Any]] = {}
    strategy_returns: dict[str, pd.Series] = {}
    for name, scores in strategy_scores.items():
        signals = _scores_to_signals(scores, config.top_n, name)
        result = backtest(signals, **kwargs)
        payload, returns = _backtest_payload(result)
        backtest_payloads[name] = payload
        strategy_returns[name] = returns

    ml_payload = backtest_payloads["ensemble"]
    baseline_payload = backtest_payloads["equal_weight"]
    baseline_returns = strategy_returns["equal_weight"]

    reference = config.reference_symbol or str(prices["symbol"].iloc[0])
    reference_close = (
        prices[prices["symbol"].astype(str).eq(reference)]
        .drop_duplicates("timestamp")
        .set_index("timestamp")["close"]
        .sort_index()
    )
    regime = detect_market_regime(reference_close, config=config.regime)
    model_metrics = {
        name: _correlation_metrics(scores, target.reindex(scores.index))
        for name, scores in strategy_scores.items()
    }
    correlations = model_metrics["ensemble"]
    strategy_evidence: dict[str, dict[str, Any]] = {}
    scorecards: dict[str, dict[str, Any]] = {}
    for name, returns in strategy_returns.items():
        fold_metrics = _fold_return_metrics(returns, walk_forward.folds)
        regime_metrics = _regime_return_metrics(returns, regime)
        tails = tail_risk_metrics(
            returns,
            benchmark_returns=baseline_returns,
            confidence=config.score.confidence,
        )
        correlation = model_metrics[name]
        scorecard = score_research_result(
            strategy_metrics=backtest_payloads[name]["metrics"],
            benchmark_metrics=baseline_payload["metrics"],
            tail_metrics=tails,
            fold_metrics=fold_metrics,
            regime_metrics=regime_metrics,
            ic=correlation["ic"],
            rank_ic=correlation["rank_ic"],
            config=config.score,
        )
        scorecards[name] = scorecard
        strategy_evidence[name] = {
            "backtest": backtest_payloads[name],
            "correlations": correlation,
            "fold_metrics": fold_metrics,
            "regime_performance": regime_metrics,
            "tail_risk": tails,
            "score": scorecard,
        }
    comparison = compare_strategy_returns(
        strategy_returns,
        benchmark_name="equal_weight",
        periods_per_year=_periods_per_year(config.frequency),
        confidence=config.score.confidence,
        scorecards=scorecards,
    )
    comparison["details"] = strategy_evidence
    regime_metrics = strategy_evidence["ensemble"]["regime_performance"]
    fold_metrics = strategy_evidence["ensemble"]["fold_metrics"]
    tails = strategy_evidence["ensemble"]["tail_risk"]
    score = strategy_evidence["ensemble"]["score"]
    latest_regime = regime.iloc[-1]
    return {
        "status": "completed_research_only",
        "protocol_version": "ml-research-v2",
        "config": {
            "target_horizon": config.target_horizon,
            "top_n": config.top_n,
            "frequency": config.frequency,
            "taker_fee_bps": config.taker_fee_bps,
            "slippage_bps": config.slippage_bps,
            "models": list(config.models),
            "walk_forward": asdict(config.walk_forward),
            "regime": asdict(config.regime),
            "score": asdict(config.score),
        },
        "sample": {
            "rows": len(features),
            "periods": int(features.index.get_level_values("timestamp").nunique()),
            "symbols": sorted(features.index.get_level_values("symbol").unique()),
            "factors": list(features.columns),
            "oos_prediction_rows": int(ensemble.notna().sum()),
        },
        "models": model_metrics,
        "strategy_comparison": comparison,
        "folds": walk_forward.folds,
        "fold_metrics": fold_metrics,
        "feature_recommendations": _feature_recommendations(walk_forward.folds),
        "strategy": ml_payload,
        "equal_weight_benchmark": baseline_payload,
        "correlations": correlations,
        "market_regime": {
            "reference_symbol": reference,
            "latest": {
                "timestamp": regime.index[-1].isoformat(),
                "regime": str(latest_regime["regime"]),
                "confidence": float(latest_regime["confidence"]),
            },
            "performance": regime_metrics,
        },
        "tail_risk": tails,
        "score": score,
        "research_note": (
            "Strict walk-forward research output; it is not validated alpha or live-trading approval."
        ),
    }
