"""Deterministic, dependency-light panel models with time-ordered validation.

The first vertical slice deliberately keeps the baseline models in NumPy so a
fresh platform install can run the research workflow without a GPU stack.
Heavier estimators can implement the same fit/predict boundary later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

import numpy as np
import pandas as pd

SUPPORTED_MODELS = ("ridge", "elastic_net", "tree_stumps")


class TreeStump(TypedDict):
    feature_index: int
    threshold: float
    left_value: float
    right_value: float


@dataclass(frozen=True)
class WalkForwardConfig:
    """Walk-forward settings expressed in unique market timestamps."""

    min_train_periods: int = 60
    test_periods: int = 20
    horizon_periods: int = 1
    embargo_periods: int = 1
    alpha: float = 10.0
    elastic_net_l1_ratio: float = 0.3
    min_feature_coverage: float = 0.8
    max_features: int = 80
    max_pairwise_correlation: float = 0.95

    def validate(self) -> None:
        if self.min_train_periods < 20:
            raise ValueError("min_train_periods must be at least 20")
        if self.test_periods < 1 or self.horizon_periods < 1:
            raise ValueError("test_periods and horizon_periods must be positive")
        if self.embargo_periods < 0 or self.alpha < 0:
            raise ValueError("embargo_periods and alpha must be non-negative")
        if not 0 <= self.elastic_net_l1_ratio <= 1:
            raise ValueError("elastic_net_l1_ratio must be in [0, 1]")
        if not 0 < self.min_feature_coverage <= 1:
            raise ValueError("min_feature_coverage must be in (0, 1]")
        if self.max_features < 1:
            raise ValueError("max_features must be positive")
        if not 0 < self.max_pairwise_correlation <= 1:
            raise ValueError("max_pairwise_correlation must be in (0, 1]")


@dataclass
class WalkForwardResult:
    predictions: pd.DataFrame
    folds: list[dict[str, Any]]


def _fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    penalty = np.eye(x.shape[1], dtype=float) * alpha
    return np.linalg.pinv(x.T @ x + penalty) @ x.T @ y


def _soft_threshold(value: float, penalty: float) -> float:
    if value > penalty:
        return value - penalty
    if value < -penalty:
        return value + penalty
    return 0.0


def _fit_elastic_net(
    x: np.ndarray,
    y: np.ndarray,
    alpha: float,
    l1_ratio: float,
    *,
    max_iter: int = 250,
) -> np.ndarray:
    n_rows, n_features = x.shape
    weights = np.zeros(n_features, dtype=float)
    regularization = alpha / max(1, n_rows)
    energy = np.square(x).mean(axis=0)
    for _ in range(max_iter):
        previous = weights.copy()
        for index in range(n_features):
            residual = y - x @ weights + x[:, index] * weights[index]
            correlation = float(np.dot(x[:, index], residual) / max(1, n_rows))
            denominator = energy[index] + regularization * (1.0 - l1_ratio)
            weights[index] = (
                _soft_threshold(correlation, regularization * l1_ratio) / denominator
                if denominator > 0
                else 0.0
            )
        if float(np.max(np.abs(weights - previous), initial=0.0)) <= 1e-7:
            break
    return weights


def _fit_tree_stumps(
    x: np.ndarray,
    y: np.ndarray,
    *,
    n_estimators: int = 8,
    learning_rate: float = 0.1,
) -> list[TreeStump]:
    predictions: np.ndarray = np.zeros(len(y), dtype=float)
    stumps: list[TreeStump] = []
    for _ in range(n_estimators):
        residual = y - predictions
        best_loss = np.inf
        best: TreeStump | None = None
        for feature_index in range(x.shape[1]):
            feature = x[:, feature_index]
            for threshold in np.unique(np.quantile(feature, (0.3, 0.5, 0.7))):
                left = feature <= threshold
                if not left.any() or left.all():
                    continue
                left_value = float(residual[left].mean())
                right_value = float(residual[~left].mean())
                update = np.where(left, left_value, right_value)
                loss = float(np.mean(np.square(residual - learning_rate * update)))
                if loss < best_loss:
                    best_loss = loss
                    best = {
                        "feature_index": feature_index,
                        "threshold": float(threshold),
                        "left_value": learning_rate * left_value,
                        "right_value": learning_rate * right_value,
                    }
        if best is None:
            break
        feature = x[:, best["feature_index"]]
        predictions += np.where(
            feature <= best["threshold"], best["left_value"], best["right_value"]
        )
        stumps.append(best)
    return stumps


def _predict_tree_stumps(x: np.ndarray, stumps: list[TreeStump]) -> np.ndarray:
    predictions: np.ndarray = np.zeros(len(x), dtype=float)
    for stump in stumps:
        feature = x[:, stump["feature_index"]]
        predictions += np.where(
            feature <= stump["threshold"],
            stump["left_value"],
            stump["right_value"],
        )
    return predictions


def _screen_features(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    config: WalkForwardConfig,
) -> tuple[list[str], dict[str, float]]:
    scores = x_train.corrwith(y_train).abs().replace([np.inf, -np.inf], np.nan).dropna()
    kept: list[str] = []
    for name in scores.sort_values(ascending=False).index:
        if len(kept) >= config.max_features:
            break
        if kept and bool(
            (x_train[kept].corrwith(x_train[name]).abs() >= config.max_pairwise_correlation).any()
        ):
            continue
        kept.append(str(name))
    return kept, {name: float(scores[name]) for name in kept}


def _validate_panel_index(index: pd.Index) -> pd.MultiIndex:
    if not isinstance(index, pd.MultiIndex) or list(index.names) != ["timestamp", "symbol"]:
        raise ValueError("features must use a MultiIndex named timestamp, symbol")
    timestamps = pd.DatetimeIndex(index.get_level_values("timestamp"))
    if timestamps.tz is None or str(timestamps.tz).upper() not in {"UTC", "ETC/UTC"}:
        raise ValueError("feature timestamps must be timezone-aware UTC")
    return index


def walk_forward_panel(
    features: pd.DataFrame,
    target: pd.Series,
    *,
    config: WalkForwardConfig | None = None,
    models: tuple[str, ...] = SUPPORTED_MODELS,
) -> WalkForwardResult:
    """Produce strict out-of-sample predictions for a timestamp/symbol panel."""
    config = config or WalkForwardConfig()
    config.validate()
    _validate_panel_index(features.index)
    if not features.index.equals(target.index):
        target = target.reindex(features.index)
    normalized_models = tuple(dict.fromkeys(models))
    unsupported = sorted(set(normalized_models) - set(SUPPORTED_MODELS))
    if not normalized_models or unsupported:
        raise ValueError(f"unsupported models: {unsupported or list(models)}")

    numeric = features.apply(pd.to_numeric, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    numeric_target = pd.to_numeric(target, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    order = np.lexsort(
        (
            features.index.get_level_values("symbol").astype(str),
            features.index.get_level_values("timestamp"),
        )
    )
    numeric = numeric.iloc[order]
    numeric_target = numeric_target.iloc[order]
    timestamps = pd.DatetimeIndex(
        numeric.index.get_level_values("timestamp").unique()
    ).sort_values()
    prediction_frame = pd.DataFrame(
        np.nan,
        index=numeric.index,
        columns=[*normalized_models, "ensemble"],
        dtype=float,
    )
    folds: list[dict[str, Any]] = []
    first_test = (
        config.min_train_periods + config.horizon_periods + config.embargo_periods
    )

    for test_start in range(first_test, len(timestamps), config.test_periods):
        test_end = min(test_start + config.test_periods, len(timestamps))
        train_end = test_start - config.horizon_periods - config.embargo_periods
        train_times = timestamps[:train_end]
        test_times = timestamps[test_start:test_end]
        train_mask = numeric.index.get_level_values("timestamp").isin(train_times)
        test_mask = numeric.index.get_level_values("timestamp").isin(test_times)
        train_x = numeric.loc[train_mask]
        train_y = numeric_target.loc[train_mask]
        valid_target = train_y.notna()
        train_x, train_y = train_x.loc[valid_target], train_y.loc[valid_target]
        coverage = train_x.notna().mean()
        columns = coverage[coverage >= config.min_feature_coverage].index.tolist()
        if len(train_times) < config.min_train_periods or not columns:
            continue

        means = train_x[columns].mean()
        stds = train_x[columns].std(ddof=0)
        columns = stds[stds > 0].index.tolist()
        if not columns:
            continue
        means, stds = means[columns], stds[columns]
        standardized_train = train_x[columns].fillna(means).sub(means).div(stds)
        columns, screening_scores = _screen_features(
            standardized_train, train_y, config
        )
        if not columns:
            continue
        means, stds = means[columns], stds[columns]
        standardized_train = standardized_train[columns]
        test_x = numeric.loc[test_mask, columns].fillna(means).sub(means).div(stds)
        target_mean = float(train_y.mean())
        x_train = standardized_train.to_numpy()
        y_train = train_y.to_numpy() - target_mean
        x_test = test_x.to_numpy()
        fold_predictions: list[np.ndarray] = []
        model_weights: dict[str, dict[str, float]] = {}

        for model in normalized_models:
            if model == "tree_stumps":
                stumps = _fit_tree_stumps(x_train, y_train)
                predicted = _predict_tree_stumps(x_test, stumps) + target_mean
                importance: np.ndarray = np.zeros(len(columns), dtype=float)
                for stump in stumps:
                    importance[stump["feature_index"]] += abs(
                        stump["right_value"] - stump["left_value"]
                    )
                weights = importance
            elif model == "elastic_net":
                weights = _fit_elastic_net(
                    x_train,
                    y_train,
                    config.alpha,
                    config.elastic_net_l1_ratio,
                )
                predicted = x_test @ weights + target_mean
            else:
                weights = _fit_ridge(x_train, y_train, config.alpha)
                predicted = x_test @ weights + target_mean
            prediction_frame.loc[test_mask, model] = predicted
            fold_predictions.append(predicted)
            model_weights[model] = {
                name: float(value) for name, value in zip(columns, weights, strict=True)
            }

        prediction_frame.loc[test_mask, "ensemble"] = np.mean(
            np.vstack(fold_predictions), axis=0
        )
        folds.append(
            {
                "train_start": train_times[0].isoformat(),
                "train_end": train_times[-1].isoformat(),
                "test_start": test_times[0].isoformat(),
                "test_end": test_times[-1].isoformat(),
                "n_train_periods": len(train_times),
                "n_train_rows": len(train_y),
                "n_test_periods": len(test_times),
                "feature_count": len(columns),
                "features": columns,
                "screening_scores": screening_scores,
                "model_weights": model_weights,
                "purge_periods": config.horizon_periods,
                "embargo_periods": config.embargo_periods,
                "ensemble_rule": "equal_weight_without_test_period_selection",
            }
        )

    return WalkForwardResult(predictions=prediction_frame, folds=folds)
