from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_main_navigation_links_to_ml_research_page() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "location.href='/ml.html'" in html
    assert "机器学习" in html


def test_ml_page_uses_versioned_job_api_and_risk_contract() -> None:
    html = (ROOT / "web" / "ml.html").read_text(encoding="utf-8")
    assert "/api/v1/ml/jobs" in html
    assert "Walk-Forward" in html
    assert "牛熊震荡识别" in html
    assert "Expected Shortfall" in html
    assert "右尾奖励" in html
    assert "等权基准" in html
    assert "策略统一排行" in html
    assert "strategy_comparison" in html
