"""Lightweight in-app research reports built from serialized factor results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _number(value: Any, *, percent: bool = False, digits: int = 3) -> str:
    """Format an optional metric for a compact Markdown report."""
    if not isinstance(value, (int, float)):
        return "N/A"
    if percent:
        return f"{value * 100:.{digits}f}%"
    return f"{value:.{digits}f}"


def _report_scope(context: dict[str, Any]) -> list[str]:
    return [
        f"- 因子：{', '.join(context['factors'])}",
        f"- 标的：{', '.join(context['symbols'])}",
        f"- 样本：{context['start']} 至 {context['end']}",
        f"- 评估频率：{context['frequency']}",
        "",
    ]


def _bias_status(result: dict[str, Any]) -> str:
    passed = result.get("forward_bias_passed")
    if passed is True:
        return "通过"
    if passed is False:
        return "未通过"
    return "未执行或无结果"


def build_factor_report(
    result: dict[str, Any],
    context: dict[str, Any],
    *,
    generated_at: str,
) -> dict[str, str]:
    """Build the report for one evaluated factor."""
    factor_name = str(result.get("factor_name", "unknown_factor"))
    ic = result.get("ic_stats") or {}
    rank_ic = result.get("rank_ic_stats") or {}
    warning = result.get("warning")
    lines = [
        f"# 因子研究报告 · {factor_name}",
        "",
        f"生成时间：{generated_at}",
        "",
        "## 评估范围",
        "",
        *_report_scope({**context, "factors": [factor_name]}),
        "## 核心结果",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| IC 均值 | {_number(ic.get('mean_ic'), percent=True)} |",
        f"| ICIR | {_number(ic.get('icir'))} |",
        f"| IC 正值占比 | {_number(ic.get('ic_positive_ratio'), percent=True, digits=1)} |",
        f"| IC t 值 | {_number(ic.get('t_stat'))} |",
        f"| RankIC 均值 | {_number(rank_ic.get('mean_rank_ic'), percent=True)} |",
        f"| RankICIR | {_number(rank_ic.get('rank_icir'))} |",
        "",
        "## 数据质量",
        "",
        f"- 前视偏差检查：{_bias_status(result)}",
    ]
    bias_reports = result.get("forward_bias") or []
    if bias_reports:
        failed = sum(1 for report in bias_reports if not report.get("passed"))
        lines.append(f"- 前视偏差明细：{len(bias_reports)} 组检查，{failed} 组未通过")
    if warning:
        lines.extend(["", "## 运行提示", "", f"- {warning}"])
    lines.extend(
        [
            "",
            "## 后续检查",
            "",
            "- 结合 IC 稳定性、分层收益与换手率判断信号是否可交易。",
            "- 与相关因子进行横向比较，避免重复暴露。",
            "- 该页面为研究工作流摘要，不替代样本外验证或风险审查。",
            "",
        ]
    )
    return {"factor_name": factor_name, "markdown": "\n".join(lines)}


def _correlation_highlights(correlation: dict[str, Any] | None) -> list[str]:
    if not correlation:
        return ["- 本次未产出相关矩阵。"]
    labels = correlation.get("labels") or []
    matrix = correlation.get("matrix") or []
    pairs: list[tuple[float, str, str, float]] = []
    for left, name in enumerate(labels):
        if left >= len(matrix):
            continue
        row = matrix[left]
        if not isinstance(row, list):
            continue
        for right in range(left + 1, min(len(labels), len(row))):
            value = row[right]
            if isinstance(value, (int, float)):
                pairs.append((abs(value), str(name), str(labels[right]), float(value)))
    if not pairs:
        return ["- 本次未产出可用的相关性数据。"]
    pairs.sort(reverse=True)
    return [
        f"- {left} / {right}：{value:.3f}"
        for _, left, right, value in pairs[:5]
    ]


def build_batch_reports(
    *,
    context: dict[str, Any],
    results: list[dict[str, Any]],
    correlation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Generate one cross-factor report and one report per factor."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    factor_reports = [
        build_factor_report(result, context, generated_at=generated_at)
        for result in results
    ]
    ranked = sorted(
        results,
        key=lambda result: (
            (result.get("ic_stats") or {}).get("icir") is not None,
            (result.get("ic_stats") or {}).get("icir") or float("-inf"),
        ),
        reverse=True,
    )
    lines = [
        "# 因子横向对比报告",
        "",
        f"生成时间：{generated_at}",
        "",
        "## 评估范围",
        "",
        *_report_scope(context),
        "## 指标排名",
        "",
        "| 因子 | IC 均值 | ICIR | RankIC 均值 | 前视偏差 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for result in ranked:
        ic = result.get("ic_stats") or {}
        rank_ic = result.get("rank_ic_stats") or {}
        lines.append(
            "| {name} | {mean_ic} | {icir} | {mean_rank_ic} | {bias} |".format(
                name=result.get("factor_name", "unknown_factor"),
                mean_ic=_number(ic.get("mean_ic"), percent=True),
                icir=_number(ic.get("icir")),
                mean_rank_ic=_number(rank_ic.get("mean_rank_ic"), percent=True),
                bias=_bias_status(result),
            )
        )
    lines.extend(
        [
            "",
            "## 高相关组合",
            "",
            *_correlation_highlights(correlation),
            "",
            "## 建议",
            "",
            "- 优先复核 ICIR 稳定、前视偏差通过且与已有信号低相关的因子。",
            "- 对高度相关的因子保留代表项，或在组合层面做正交化处理。",
            "- 继续使用样本内外实验和完整评估管线验证候选因子。",
            "",
        ]
    )
    return {
        "generated_at": generated_at,
        "overview": {"markdown": "\n".join(lines)},
        "factors": factor_reports,
    }
