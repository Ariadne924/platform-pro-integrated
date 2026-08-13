"""Tests for eligibility provenance and report filtering statistics."""

from pathlib import Path

import pandas as pd

from superplatform.evaluation.experiment import _prepare_eligibility_audit
from superplatform.evaluation.report import write_evaluation_report


def test_eligibility_audit_adds_source_and_reason_fields() -> None:
    """Eligibility values are canonicalized without changing the source decision."""
    panel = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "is_eligible": [True, False],
            "eligibility_reason": ["liquidity_pass", None],
        }
    )

    audited, statistics = _prepare_eligibility_audit(
        panel,
        {"universe": {"require_eligibility": True}},
    )

    assert audited["is_eligible"].tolist() == [True, False]
    assert audited["eligibility_reason"].tolist() == [
        "input:is_eligible=true;liquidity_pass",
        "input:is_eligible=false",
    ]
    assert statistics["input_rows"] == 2
    assert statistics["eligible_rows"] == 1


def test_sample_filter_statistics_render_consistently(tmp_path: Path) -> None:
    """Report counts and reason counts are persisted in the sample table."""
    report_path = write_evaluation_report(
        output_path=tmp_path / "evaluation_report.md",
        methods={},
        parameters={},
        sample_statistics={},
        core_results={},
        risks=[],
        failed_tasks=[],
        sample_filter_statistics={
            "input_rows": 10,
            "eligible_rows": 7,
            "selected_rows": 6,
            "filtered_rows": 4,
            "reason_counts": {
                "input:is_eligible=true": 7,
                "input:is_eligible=false": 3,
            },
        },
    )

    report = report_path.read_text(encoding="utf-8")
    assert "## 样本过滤统计" in report
    assert "| 过滤前行数 | 10 |" in report
    assert "| eligibility 通过行数 | 7 |" in report
    assert "| 最终选中行数 | 6 |" in report
    assert "| 过滤掉行数 | 4 |" in report
    assert "| input:is_eligible=true | 7 |" in report
