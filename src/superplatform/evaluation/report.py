"""Matplotlib plots and deterministic Markdown reporting for evaluations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def _ensure_factor_column(data: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with a stable factor_name column."""
    result = data.copy()
    if "factor_name" not in result.columns:
        result["factor_name"] = "factor"
    return result


def plot_ic_series(
    ic_data: pd.DataFrame,
    output_path: str | Path,
    *,
    timestamp_col: str = "timestamp",
    ic_col: str = "ic",
) -> Path:
    """Save IC time series and cumulative IC as a two-panel PNG."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = _ensure_factor_column(ic_data)
    figure, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    if not data.empty:
        for factor_name, group in data.groupby("factor_name", sort=True):
            group = group.sort_values(timestamp_col)
            axes[0].plot(group[timestamp_col], group[ic_col], label=str(factor_name))
            axes[1].plot(
                group[timestamp_col],
                group[ic_col].fillna(0).cumsum(),
                label=str(factor_name),
            )
        axes[0].axhline(0.0, color="black", linewidth=0.7)
        axes[1].axhline(0.0, color="black", linewidth=0.7)
        axes[0].legend(loc="best")
        axes[1].legend(loc="best")
    axes[0].set_title("Cross-sectional IC")
    axes[1].set_title("Cumulative IC")
    axes[1].set_xlabel("Timestamp (UTC)")
    axes[0].set_ylabel("IC")
    axes[1].set_ylabel("Cumulative IC")
    figure.tight_layout()
    figure.savefig(output, dpi=150)
    plt.close(figure)
    return output


def plot_layer_nav(
    decile_returns: pd.DataFrame,
    output_path: str | Path,
    *,
    timestamp_col: str = "timestamp",
    return_col: str = "mean_return",
) -> Path:
    """Save gross and net cumulative NAV curves for every factor layer."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = _ensure_factor_column(decile_returns)
    figure, axis = plt.subplots(figsize=(12, 6))
    return_columns = [return_col]
    if {"gross_return", "net_return"}.issubset(data.columns):
        return_columns = ["gross_return", "net_return"]
    if not data.empty:
        for (factor_name, quantile), group in data.groupby(
            ["factor_name", "quantile"], sort=True
        ):
            group = group.sort_values(timestamp_col)
            for column in return_columns:
                nav = (
                    1.0
                    + pd.to_numeric(group[column], errors="coerce").fillna(0)
                ).cumprod()
                label = f"{factor_name} Q{int(quantile)} {column.removesuffix('_return')}"
                axis.plot(
                    group[timestamp_col],
                    nav,
                    label=label,
                    linestyle="--" if column == "net_return" else "-",
                )
        axis.legend(loc="best", ncol=2)
    axis.set_title("Layer cumulative gross and net NAV")
    axis.set_xlabel("Timestamp (UTC)")
    axis.set_ylabel("NAV")
    figure.tight_layout()
    figure.savefig(output, dpi=150)
    plt.close(figure)
    return output


def plot_correlation_heatmap(
    matrix: pd.DataFrame,
    output_path: str | Path,
    *,
    title: str = "Factor correlation",
) -> Path:
    """Save a correlation matrix heatmap as PNG."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(8, 7))
    if matrix.empty:
        axis.text(0.5, 0.5, "No correlation data", ha="center", va="center")
        axis.set_axis_off()
    else:
        image = axis.imshow(
            matrix.to_numpy(dtype=float),
            vmin=-1,
            vmax=1,
            cmap="coolwarm",
        )
        labels = [str(label) for label in matrix.index]
        axis.set_xticks(range(len(labels)), labels=labels, rotation=45, ha="right")
        axis.set_yticks(range(len(labels)), labels=labels)
        figure.colorbar(image, ax=axis, label="Correlation")
    axis.set_title(title)
    figure.tight_layout()
    figure.savefig(output, dpi=150)
    plt.close(figure)
    return output


def generate_plots(
    ic_data: pd.DataFrame,
    decile_returns: pd.DataFrame,
    correlations: dict[str, pd.DataFrame],
    output_dir: str | Path,
) -> dict[str, Path]:
    """Generate all stable PNG artifact names for one evaluation run."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "ic_timeseries": plot_ic_series(
            ic_data,
            directory / "ic_timeseries.png",
        ),
        "layer_nav": plot_layer_nav(
            decile_returns,
            directory / "layer_nav.png",
        ),
        "corr_pearson": plot_correlation_heatmap(
            correlations.get("pearson", pd.DataFrame()),
            directory / "corr_pearson.png",
            title="Pearson factor correlation",
        ),
        "corr_spearman": plot_correlation_heatmap(
            correlations.get("spearman", pd.DataFrame()),
            directory / "corr_spearman.png",
            title="Spearman factor correlation",
        ),
    }
    return outputs


def _markdown_mapping(title: str, values: dict[str, Any]) -> list[str]:
    """Render a mapping with deterministic key ordering."""
    lines = [f"## {title}", ""]
    for key in sorted(values):
        value = values[key]
        if isinstance(value, (dict, list, tuple)):
            formatted = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        else:
            formatted = str(value)
        lines.append(f"- {key}: {formatted}")
    lines.append("")
    return lines


def write_evaluation_report(
    *,
    output_path: str | Path,
    methods: dict[str, Any],
    parameters: dict[str, Any],
    sample_statistics: dict[str, Any],
    core_results: dict[str, Any],
    risks: list[str],
    failed_tasks: list[dict[str, Any]],
    sample_filter_statistics: dict[str, Any] | None = None,
) -> Path:
    """Write the deterministic Markdown evaluation report."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Factor Evaluation Report", ""]
    lines.extend(_markdown_mapping("Methods", methods))
    lines.extend(_markdown_mapping("Parameters", parameters))
    lines.extend(_markdown_mapping("Sample Statistics", sample_statistics))
    lines.extend(_markdown_mapping("Core Results", core_results))
    lines.extend(["## 样本过滤统计", ""])
    if sample_filter_statistics:
        lines.extend(
            [
                "| 指标 | 数值 |",
                "| --- | ---: |",
                f"| 过滤前行数 | {sample_filter_statistics.get('input_rows', 'N/A')} |",
                f"| eligibility 通过行数 | {sample_filter_statistics.get('eligible_rows', 'N/A')} |",
                f"| 最终选中行数 | {sample_filter_statistics.get('selected_rows', 'N/A')} |",
                f"| 过滤掉行数 | {sample_filter_statistics.get('filtered_rows', 'N/A')} |",
                "",
                "| eligibility 原因 | 行数 |",
                "| --- | ---: |",
            ]
        )
        for reason, count in sorted(
            sample_filter_statistics.get("reason_counts", {}).items()
        ):
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| unavailable | N/A |")
    lines.append("")
    lines.extend(
        [
            "## Cost Treatment",
            "",
            "- Layer net_return = gross_return - turnover * (fee_bps + slippage_bps) / 10,000.",
            "- Long-short costs include both the top and bottom layer turnover.",
            "",
        ]
    )
    lines.extend(["## Risk Notes", ""])
    lines.extend(f"- {risk}" for risk in risks)
    lines.append("")
    lines.extend(["## Failed Tasks", ""])
    if failed_tasks:
        for task in sorted(
            failed_tasks,
            key=lambda item: str(item.get("task", "")),
        ):
            lines.append(
                f"- {task.get('task', 'unknown')}: "
                f"{task.get('error', 'unknown error')}"
            )
    else:
        lines.append("- None")
    lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")
    return output
