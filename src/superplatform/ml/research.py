"""End-to-end ML research orchestration over the platform factor panel."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from superplatform.consumption.backtest import BacktestResult, backtest
from superplatform.ml.comparison import compare_strategy_returns
from superplatform.ml.models import DEFAULT_MODELS, WalkForwardConfig, walk_forward_panel
from superplatform.ml.portfolio import PortfolioConfig, build_portfolio_signals
from superplatform.ml.regime import RegimeConfig, detect_market_regime
from superplatform.ml.risk import ScoreConfig, score_research_result, tail_risk_metrics
from superplatform.ml.threshold_research import (
    ThresholdResearchConfig,
    run_threshold_research,
)


@dataclass(frozen=True)
class MLResearchConfig:
    research_mode: str = "cross_section"
    allow_short: bool = False
    core_factor: str | None = None
    target_horizon: int = 1
    top_n: int = 3
    frequency: str = "1d"
    taker_fee_bps: float = 4.0
    slippage_bps: float = 2.0
    models: tuple[str, ...] = DEFAULT_MODELS
    walk_forward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    score: ScoreConfig = field(default_factory=ScoreConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    threshold_research: ThresholdResearchConfig = field(
        default_factory=ThresholdResearchConfig
    )
    reference_symbol: str | None = None

    def validate(self) -> None:
        if self.research_mode not in {"single_asset", "cross_section"}:
            raise ValueError("research_mode must be single_asset or cross_section")
        if self.target_horizon not in {1, 5, 10, 20}:
            raise ValueError("target_horizon must be one of 1, 5, 10, 20")
        if self.top_n < 1:
            raise ValueError("top_n must be positive")
        if self.taker_fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("cost assumptions must be non-negative")
        self.walk_forward.validate()
        self.regime.validate()
        self.score.validate()
        self.portfolio.validate()
        self.threshold_research.validate()


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


def _scores_to_signals(
    scores: pd.Series,
    top_n: int,
    name: str,
    *,
    research_mode: str,
    allow_short: bool,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    clean = pd.to_numeric(scores, errors="coerce")
    for timestamp, group in clean.groupby(level="timestamp", sort=True):
        values = group.droplevel("timestamp").dropna().sort_values(ascending=False)
        if values.empty:
            continue
        if research_mode == "single_asset":
            for symbol, value in values.items():
                position = float(np.sign(value)) if allow_short else float(value > 0)
                rows.append(
                    {"timestamp": timestamp, "symbol": str(symbol), "position": position}
                )
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
    if not aligned.empty and aligned.index.get_level_values("symbol").nunique() == 1:
        valid = aligned["prediction"].nunique() >= 2 and aligned["target"].nunique() >= 2
        ic = aligned["prediction"].corr(aligned["target"], method="pearson") if valid else None
        rank_ic = (
            aligned["prediction"].corr(aligned["target"], method="spearman")
            if valid
            else None
        )
        return {
            "ic": float(ic) if ic is not None and pd.notna(ic) else None,
            "rank_ic": float(rank_ic) if rank_ic is not None and pd.notna(rank_ic) else None,
            "sample_periods": len(aligned),
            "series": [],
            "method": "time_series_single_asset",
        }
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
        "method": "cross_sectional",
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


def _feature_recommendations(
    folds: list[dict[str, Any]],
    *,
    core_factor: str | None,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for fold in folds:
        model_weights = fold.get("model_weights", {})
        normalized_models: list[dict[str, float]] = []
        for weights in model_weights.values():
            denominator = sum(abs(float(value)) for value in weights.values())
            if denominator <= 0:
                continue
            normalized_models.append(
                {feature: float(value) / denominator for feature, value in weights.items()}
            )
        features = sorted({feature for weights in normalized_models for feature in weights})
        for feature in features:
            fold_weight = float(
                np.mean([weights.get(feature, 0.0) for weights in normalized_models])
            )
            buckets.setdefault(feature, []).append(fold_weight)
    rows: list[dict[str, Any]] = []
    total_folds = max(1, len(folds))
    for feature, values in buckets.items():
        array: np.ndarray = np.asarray(values, dtype=float)
        mean_weight = float(array.mean())
        selection_frequency = len(values) / total_folds
        sign_consistency = float(abs(np.sign(array).mean()))
        stable_weight = mean_weight * selection_frequency * sign_consistency
        rows.append(
            {
                "feature": feature,
                "role": "core" if feature == core_factor else "recommended",
                "direction": 1 if mean_weight >= 0 else -1,
                "mean_weight": mean_weight,
                "mean_absolute_weight": float(np.abs(array).mean()),
                "selection_frequency": selection_frequency,
                "sign_consistency": sign_consistency,
                "recommendation_score": abs(stable_weight),
                "stable_weight": stable_weight,
                "status": "ml_train_fold_candidate",
            }
        )
    rows.sort(
        key=lambda row: (
            float(row["recommendation_score"]),
            float(row["selection_frequency"]),
        ),
        reverse=True,
    )
    denominator = sum(abs(float(row["stable_weight"])) for row in rows)
    for row in rows:
        row["recommended_weight"] = (
            float(row["stable_weight"]) / denominator if denominator > 0 else 0.0
        )
    return rows


def _existing_signal_scores(
    signals: pd.DataFrame,
    *,
    oos_index: pd.MultiIndex,
) -> pd.Series:
    """Align an existing strategy's target positions to the ML OOS panel.

    Existing strategies may emit sparse rebalance rows.  Positions therefore
    carry forward per symbol, while observations before the first signal stay
    flat.  The aligned position is also the strategy's predictive score for
    Signal IC / Rank IC, so non-ML strategies are not penalised merely because
    they do not expose model probabilities.
    """
    required = {"timestamp", "symbol", "position"}
    missing = sorted(required - set(signals.columns))
    if missing:
        raise ValueError(f"existing strategy signals are missing columns: {missing}")
    frame = signals.loc[:, ["timestamp", "symbol", "position"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str)
    frame["position"] = pd.to_numeric(frame["position"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "symbol", "position"])
    frame = frame.drop_duplicates(["timestamp", "symbol"], keep="last")
    raw = frame.set_index(["timestamp", "symbol"])["position"].sort_index()
    aligned_parts: list[pd.Series] = []
    for symbol in oos_index.get_level_values("symbol").unique():
        target_index = oos_index[oos_index.get_level_values("symbol") == symbol]
        symbol_scores = raw[raw.index.get_level_values("symbol") == str(symbol)]
        timestamps = target_index.get_level_values("timestamp")
        if symbol_scores.empty:
            carried = pd.Series(0.0, index=timestamps)
        else:
            carried = symbol_scores.droplevel("symbol").reindex(
                timestamps, method="ffill"
            ).fillna(0.0)
        carried.index = target_index
        aligned_parts.append(carried)
    if not aligned_parts:
        return pd.Series(dtype=float, name="position")
    return pd.concat(aligned_parts).sort_index().rename("position")


def run_ml_research(
    panel: pd.DataFrame,
    *,
    config: MLResearchConfig | None = None,
    existing_strategy_signals: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """Run the first complete ML → signals → unified backtest → score slice."""
    config = config or MLResearchConfig()
    config.validate()
    features, target, prices = prepare_ml_panel(
        panel, target_horizon=config.target_horizon
    )
    symbol_count = len(features.index.get_level_values("symbol").unique())
    if features.shape[1] < 1:
        raise ValueError("ML research requires at least one factor")
    if config.research_mode == "single_asset" and symbol_count != 1:
        raise ValueError("single_asset research requires exactly one symbol")
    if config.research_mode == "cross_section" and symbol_count < 2:
        raise ValueError("cross_section research requires at least two symbols")
    if config.core_factor is not None and config.core_factor not in features.columns:
        raise ValueError("core_factor must be one of the selected factors")
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
    allocation_baseline_name: str | None = None
    if config.research_mode == "cross_section" and config.portfolio.method != "equal_weight":
        allocation_baseline_name = "ensemble_equal_asset"
        strategy_scores[allocation_baseline_name] = ensemble
    if config.core_factor is not None:
        strategy_scores["core_factor"] = _equal_weight_factor_score(
            features[[config.core_factor]]
        ).reindex(oos_index).dropna()
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
    allocation_rows: list[dict[str, Any]] = []
    allocation_risk_events: list[dict[str, Any]] = []
    for name, scores in strategy_scores.items():
        if name == "ensemble" and config.research_mode == "cross_section":
            allocated = build_portfolio_signals(
                scores,
                prices,
                top_n=config.top_n,
                config=config.portfolio,
                name=name,
            )
            signals = allocated.signals
            allocation_rows = allocated.allocations
            allocation_risk_events = allocated.risk_events
        else:
            signals = _scores_to_signals(
                scores,
                config.top_n,
                name,
                research_mode=config.research_mode,
                allow_short=config.allow_short,
            )
        result = backtest(signals, **kwargs)
        payload, returns = _backtest_payload(result)
        backtest_payloads[name] = payload
        strategy_returns[name] = returns

    existing_errors: dict[str, str] = {}
    existing_score_series: dict[str, pd.Series] = {}
    reserved_names = set(strategy_returns)
    for name, raw_signals in (existing_strategy_signals or {}).items():
        if name in reserved_names:
            existing_errors[name] = "name collides with an ML candidate or baseline"
            continue
        try:
            position_scores = _existing_signal_scores(
                raw_signals,
                oos_index=shared_score_frame.index,
            )
            if position_scores.empty or not bool(position_scores.abs().gt(0).any()):
                raise ValueError("strategy has no non-zero positions in the ML OOS window")
            signals = position_scores.rename("position").reset_index()
            signals.attrs["strategy_name"] = name
            result = backtest(signals, **kwargs)
            payload, returns = _backtest_payload(result)
            backtest_payloads[name] = payload
            strategy_returns[name] = returns
            existing_score_series[name] = position_scores
        except Exception as exc:
            existing_errors[name] = f"{type(exc).__name__}: {exc}"

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
    threshold_candidates: dict[str, dict[str, Any]] = {}
    threshold_skipped: list[str] = []
    if config.threshold_research.enabled:
        candidates: list[tuple[str, pd.Series, bool]] = [
            ("ensemble", strategy_scores["ensemble"], config.allow_short)
        ]
        candidates.extend(
            (
                name,
                scores,
                bool(scores.lt(0.0).any()),
            )
            for name, scores in existing_score_series.items()
        )
        selected_candidates = candidates[: config.threshold_research.max_candidates]
        threshold_skipped = [
            name for name, _, _ in candidates[config.threshold_research.max_candidates :]
        ]
        for name, scores, allow_short in selected_candidates:
            try:
                threshold_candidates[name] = run_threshold_research(
                    scores,
                    price_data=prices_by_symbol,
                    regime=regime["regime"],
                    strategy_name=name,
                    config=config.threshold_research,
                    allow_short=allow_short,
                    periods_per_year=_periods_per_year(config.frequency),
                    taker_fee_bps=config.taker_fee_bps,
                    slippage_bps=config.slippage_bps,
                )
            except Exception as exc:
                threshold_candidates[name] = {
                    "strategy": name,
                    "status": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "surface": [],
                    "stable_regions": [],
                    "recommended_point": None,
                }
    model_metrics = {
        name: _correlation_metrics(scores, target.reindex(scores.index))
        for name, scores in strategy_scores.items()
    }
    model_metrics.update(
        {
            name: {
                **_correlation_metrics(scores, target.reindex(scores.index)),
                "method": "strategy_position_signal",
            }
            for name, scores in existing_score_series.items()
        }
    )
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
        if name == "ensemble" and allocation_risk_events:
            scorecard["status"] = "rejected"
            gates = list(scorecard.get("gates_failed", []))
            if "forced_liquidation" not in gates:
                gates.append("forced_liquidation")
            scorecard["gates_failed"] = gates
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
        candidate_kinds={
            **{model: "trained_model" for model in config.models},
            "ensemble": "derived_ensemble",
            "equal_weight": "non_ml_baseline",
            **(
                {allocation_baseline_name: "allocation_baseline"}
                if allocation_baseline_name is not None
                else {}
            ),
            **(
                {"core_factor": "non_ml_baseline"}
                if config.core_factor is not None
                else {}
            ),
            **{name: "existing_strategy" for name in existing_score_series},
        },
    )
    comparison["candidate_groups"] = {
        "trained_models": list(config.models),
        "derived_ensembles": ["ensemble"],
        "non_ml_baselines": [
            "equal_weight",
            *(["core_factor"] if config.core_factor is not None else []),
        ],
        "allocation_baselines": (
            [allocation_baseline_name] if allocation_baseline_name is not None else []
        ),
        "existing_strategies": list(existing_score_series),
    }
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
            "research_mode": config.research_mode,
            "allow_short": config.allow_short,
            "core_factor": config.core_factor,
            "target_horizon": config.target_horizon,
            "top_n": config.top_n,
            "frequency": config.frequency,
            "taker_fee_bps": config.taker_fee_bps,
            "slippage_bps": config.slippage_bps,
            "models": list(config.models),
            "walk_forward": asdict(config.walk_forward),
            "regime": asdict(config.regime),
            "score": asdict(config.score),
            "portfolio": asdict(config.portfolio),
            "threshold_research": asdict(config.threshold_research),
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
        "existing_strategy_scores": {
            name: strategy_evidence[name] for name in existing_score_series
        },
        "existing_strategy_errors": existing_errors,
        "folds": walk_forward.folds,
        "fold_metrics": fold_metrics,
        "feature_recommendations": _feature_recommendations(
            walk_forward.folds,
            core_factor=config.core_factor,
        ),
        "strategy": ml_payload,
        "equal_weight_benchmark": baseline_payload,
        "asset_allocation": {
            "enabled": config.research_mode == "cross_section",
            "method": config.portfolio.method,
            "constraints": {
                "max_weight": config.portfolio.max_weight,
                "risk_model": config.portfolio.risk_model,
                "risk_lookback_periods": config.portfolio.risk_lookback_periods,
                "ewma_decay": config.portfolio.ewma_decay,
                "evt_threshold_quantile": config.portfolio.evt_threshold_quantile,
                "evt_min_exceedances": config.portfolio.evt_min_exceedances,
                "har_min_days": config.portfolio.har_min_days,
                "var_limit": config.portfolio.var_limit,
                "expected_shortfall_limit": config.portfolio.expected_shortfall_limit,
                "confidence": config.portfolio.confidence,
                "annual_volatility_limit": config.portfolio.annual_volatility_limit,
                "soft_drawdown_limit": config.portfolio.soft_drawdown_limit,
                "delever_drawdown_limit": config.portfolio.delever_drawdown_limit,
                "hard_drawdown_limit": config.portfolio.hard_drawdown_limit,
                "single_period_loss_limit": config.portfolio.single_period_loss_limit,
                "cooldown_periods": config.portfolio.cooldown_periods,
            },
            "latest": allocation_rows[-1] if allocation_rows else None,
            "history": allocation_rows,
            "risk_events": allocation_risk_events,
            "equal_asset_baseline": allocation_baseline_name,
        },
        "threshold_research": {
            "enabled": config.threshold_research.enabled,
            "candidates": threshold_candidates,
            "skipped_candidates": threshold_skipped,
            "note": (
                "Stable regions require neighboring thresholds, rolling windows, "
                "market regimes, and drawdown gates to agree."
            ),
        },
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
