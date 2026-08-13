"""G1 数据校验报告 —— validate-report 命令与报告生成器测试。

覆盖:
- 干净序列 → PASS
- 含缺失区间 / 异常值 / 重复时间戳 / 非 UTC 的序列 → 被显式标记
- Markdown 报告与 JSON 留痕产物
- CLI 入口调用
- 缓存不存在 / 被占用时的报错
"""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from superplatform.data.provider_registry import DataProvider, DataProviderRegistry
from superplatform.data.schema import MarketType
from superplatform.data.store import Store, provider_table
from superplatform.data.validation_report import (
    audit_series,
    generate_validation_report,
)

UTC = "UTC"


class _StubProvider(DataProvider):
    def __init__(
        self,
        provider_id: str,
        data_type: str,
        market_type: MarketType | None,
    ) -> None:
        self.provider_id = provider_id
        self.data_type = data_type
        self.exchange = "binance"
        self.market_type = market_type

    async def fetch(self, *args, **kwargs):
        raise NotImplementedError


def _stub_registry() -> DataProviderRegistry:
    """The binance/perpetual provider set used by fetch-plan tests."""
    reg = DataProviderRegistry()
    reg.register(_StubProvider("binance-perp-kline", "kline", MarketType.PERPETUAL))
    reg.register(_StubProvider(
        "binance-perp-funding-rate", "funding_rate", MarketType.PERPETUAL,
    ))
    reg.register(_StubProvider(
        "binance-perp-open-interest", "open_interest", MarketType.PERPETUAL,
    ))
    reg.register(_StubProvider("binance-basis", "basis", None))
    return reg


def _kline_df(
    *,
    n: int = 10,
    freq: str = "1d",
    start: str = "2024-01-01",
    tz: str | None = UTC,
    drop: list[int] | None = None,
    duplicates: int = 0,
    outlier_at: list[int] | None = None,
) -> pd.DataFrame:
    """构造一张 kline 测试帧,可注入各种数据缺陷。"""
    pd_freq = {"1d": "D", "1h": "h"}.get(freq, freq)
    ts = pd.date_range(start, periods=n, freq=pd_freq, tz=tz)
    close = [100.0 + i for i in range(n)]
    if outlier_at:
        for i in outlier_at:
            close[i] = 1e7  # 明显异常
    df = pd.DataFrame({
        "timestamp": ts,
        "open": [99.0] * n,
        "high": [101.0] * n,
        "low": [98.0] * n,
        "close": close,
        "volume": [1000.0] * n,
        "quote_volume": [1e5] * n,
        "trades": [10] * n,
        "taker_buy_volume": [500.0] * n,
        "taker_buy_quote_volume": [5e4] * n,
    })
    if drop:
        df = df.drop(index=drop).reset_index(drop=True)
    if duplicates:
        tail = df.iloc[[0]].copy()
        df = pd.concat([df, tail], ignore_index=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["symbol"] = "BTCUSDT"
    df["frequency"] = "1d"
    return df


def _funding_df(*, n: int = 10, start: str = "2023-01-01") -> pd.DataFrame:
    """构造 funding_rate 测试帧(不含 mark_price,列已从 schema 移除)。"""
    df = pd.DataFrame({
        "timestamp": pd.date_range(start, periods=n, freq="8h", tz=UTC),
        "funding_rate": [0.0001] * n,
    })
    df["symbol"] = "BTCUSDT"
    df["frequency"] = "8h"
    return df


def _oi_df(*, n: int = 6, start: str = "2023-12-16", drop: list[int] | None = None) -> pd.DataFrame:
    """构造 open_interest 测试帧,可注入缺失日(如掉 12-18)。"""
    df = pd.DataFrame({
        "timestamp": pd.date_range(start, periods=n, freq="D", tz=UTC),
        "open_interest": [100.0 + i for i in range(n)],
    })
    if drop:
        df = df.drop(index=drop).reset_index(drop=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["symbol"] = "NEARUSDT"
    df["frequency"] = "1d"
    return df


def _build_cache(
    tmp_path,
    *,
    tables=("kline",),
    with_empty_ranges=False,
    filename="cache.duckdb",
) -> str:
    """写一个测试用 DuckDB 缓存(per-provider 表),返回路径。"""
    path = tmp_path / filename
    store = Store(path)
    try:
        frames = {
            "kline": ("binance-perp-kline", _kline_df()),
            "funding_rate": ("binance-perp-funding-rate", _funding_df()),
            "open_interest": ("binance-perp-open-interest", _oi_df()),
        }
        for data_type in tables:
            pid, df = frames[data_type]
            store.ensure_provider_table(pid, data_type)
            store.upsert(provider_table(pid), df)
        if with_empty_ranges:
            store.record_empty_range(
                provider_table("binance-perp-kline"), "BTCUSDT", "1d",
                pd.Timestamp("2020-01-01", tz="UTC"),
                pd.Timestamp("2020-12-31", tz="UTC"),
            )
    finally:
        store.close()
    return str(path)


# ── 单元: audit_series ────────────────────────────────────────────────


def test_clean_series_passes():
    audit = audit_series("kline", _kline_df(), "1d")
    assert audit.status == "PASS"
    assert audit.utc["is_utc"] is True
    assert audit.missing_gaps == []
    assert audit.outliers == {}
    assert audit.duplicate_timestamps == 0


def test_missing_gaps_are_marked():
    audit = audit_series("kline", _kline_df(drop=[5, 6]), "1d")
    assert audit.status == "WARN"
    assert len(audit.missing_gaps) >= 1
    assert "gap_start" in audit.missing_gaps[0]


def test_outliers_are_marked():
    audit = audit_series("kline", _kline_df(outlier_at=[3]), "1d")
    assert audit.status == "WARN"
    assert "close" in audit.outliers
    assert audit.outliers["close"]["count"] == 1


def test_market_normal_move_not_flagged_at_default():
    """约 8 倍 MAD 的跳变(数字资产里的普通波动)在默认阈值 15 下不标记。

    默认阈值调大的动机:重尾/带趋势的序列里,统计上偏大的单根 K 线是
    真实市场行为而非数据错误,不应把报告淹没在"异常值"里。只有病态值
    (如 1e7 的坏数)才应被标记。
    """
    df = _kline_df()
    # close 基线 100..109,median≈104.5,MAD≈2.5;+30 约等于 8 个 MAD
    df.loc[3, "close"] = 134.5
    audit = audit_series("kline", df, "1d")
    assert audit.outliers == {}
    assert audit.status == "PASS"


def test_duplicate_timestamps_warn():
    audit = audit_series("kline", _kline_df(duplicates=1), "1d")
    assert audit.status == "WARN"
    assert audit.duplicate_timestamps >= 1


def test_non_utc_is_hard_failure():
    audit = audit_series("kline", _kline_df(tz=None), "1d")
    assert audit.status == "FAIL"
    assert any("非 UTC" in f for f in audit.hard_failures)


def test_frequency_mismatch_warns():
    # 声明 1d,但实际是 1h 频率
    df = _kline_df(freq="1h")
    audit = audit_series("kline", df, "1d")
    assert audit.status == "WARN"
    assert audit.freq_consistent is False


def test_frequency_mismatch_suppresses_spurious_gap_noise():
    # 4h 标签下存 8h 数据(Binance 资金费率天然 8h,被按请求频率标成 4h):
    # 每条真实间隔都超 1.5×4h,若逐条罗列会产生几千条假"缺失区间"。
    df = _kline_df(freq="8h", n=20)
    audit = audit_series("kline", df, "4h")
    assert audit.status == "WARN"
    assert audit.freq_consistent is False
    assert audit.missing_gaps == []          # 假缺失被清除
    assert audit.gaps_suppressed_count == 19  # 但计数保留可追踪
    assert any("假缺失" in w for w in audit.warnings)


# ── 集成: generate_validation_report 产物 ────────────────────────────


def test_generate_report_produces_markdown_and_json(tmp_path):
    cache = _build_cache(tmp_path, with_empty_ranges=True)
    md_path = tmp_path / "data_validation_report.md"

    artifacts = generate_validation_report(cache, md_path)

    assert artifacts.verdict == "PASS"
    assert md_path.exists()
    json_path = md_path.with_suffix(".json")
    assert json_path.exists()

    md = md_path.read_text(encoding="utf-8")
    assert "G1 数据校验报告" in md
    # 唯一序列且通过 → 判定折叠为「全部」,不逐条展开(减噪)
    assert "`PASS` (1): 全部" in md
    assert "增量更新" in md
    assert "empty_ranges" in md or "已验证为空" in md

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["verdict"]["verdict"] == "PASS"
    assert data["verdict"]["series_total"] == 1
    # empty_ranges 现在以 provider 表名作键
    assert data["empty_ranges"][0]["data_type"] == "pv_binance_perp_kline"


def test_report_groups_by_verdict_and_expands_only_nonpassing(tmp_path):
    """判定分组:WARN 序列列清单并展开详情;通过序列只进分组,不展开。"""
    path = tmp_path / "cache.duckdb"
    store = Store(path)
    store.ensure_provider_table("binance-perp-kline", "kline")
    good = _kline_df().assign(symbol="BTCUSDT")  # 干净
    bad = _kline_df(drop=[5]).assign(symbol="ETHUSDT")  # 含缺失
    store.upsert(provider_table("binance-perp-kline"), good)
    store.upsert(provider_table("binance-perp-kline"), bad)
    store.close()

    md_path = tmp_path / "r.md"
    artifacts = generate_validation_report(path, md_path)
    assert artifacts.verdict == "WARN"
    md = md_path.read_text(encoding="utf-8")
    # 分组:两条序列不同判定 → 各列符号
    assert "- `PASS` (1): BTCUSDT·1d" in md
    assert "- `WARN` (1): ETHUSDT·1d" in md
    # 非通过序列展开详情
    assert "### binance-perp-kline · ETHUSDT · 1d · `WARN`" in md
    assert "缺失区间" in md
    # 通过序列不展开详情块
    assert "### binance-perp-kline · BTCUSDT · 1d" not in md
    # 开头检查项通过计数:UTC 全过,缺失区间 1/2(ETH 含缺口)
    assert "| 时区(UTC) | 2/2 |" in md
    assert "| 缺失区间 | 1/2 |" in md


def test_report_check_summary_lists_pass_counts(tmp_path):
    """报告开头有逐检查项通过计数,证明每项校验都跑过、过了多少。"""
    cache = _build_cache(tmp_path, with_empty_ranges=True)
    md_path = tmp_path / "cs.md"
    generate_validation_report(cache, md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "### 检查项通过情况" in md
    assert "| 时区(UTC) | 1/1 |" in md
    assert "| Schema 契约(列/dtype) | 1/1 |" in md
    assert "| 缺失区间 | 1/1 |" in md
    # 缓存无 market_type 列 → 整项不适用,如实标注而非虚报全过
    assert "| 现货/永续混用 | 不适用 |" in md

    data = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["checks"]["utc"] == {"label": "时区(UTC)", "pass": 1, "fail": 0, "n_a": 0, "total": 1}
    assert data["checks"]["mix"]["n_a"] == 1


def test_report_source_note_open_interest_gap(tmp_path):
    """open_interest 缺失区间 → 报告固定给出"源端归档缺失"说明,列具体日期。"""
    path = tmp_path / "cache.duckdb"
    store = Store(path)
    store.ensure_provider_table("binance-perp-open-interest", "open_interest")
    # 掉第 2 行(2023-12-18):detect_missing 应标记一个 1d 缺口
    store.upsert(
        provider_table("binance-perp-open-interest"), _oi_df(drop=[2])
    )
    store.close()

    md_path = tmp_path / "r.md"
    generate_validation_report(path, md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "open_interest 缺失区间:源端归档缺失" in md
    assert "1/1 经核实" in md
    assert "data.binance.vision" in md
    assert "NEARUSDT·1d" in md
    # 缺口渲染为 gap_start → gap_end:掉 12-18,边界是 12-17 → 12-19
    assert "2023-12-17" in md
    assert "2023-12-19" in md

    data = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
    keys = [n["key"] for n in data["source_notes"]]
    assert keys == ["open_interest_vision_gap"]
    assert data["source_notes"][0]["verified"] == 1
    assert data["source_notes"][0]["total"] == 1
    item = data["source_notes"][0]["items"][0]
    assert "NEARUSDT·1d" in item
    assert "2023-12-17" in item and "2023-12-19" in item


def test_report_source_note_ratio_counts_only_verified(tmp_path):
    """核实比例只算经核实的源端问题:其他 provider 的缺口不计入分子。"""
    path = tmp_path / "cache.duckdb"
    store = Store(path)
    store.ensure_provider_table("binance-perp-kline", "kline")
    store.ensure_provider_table("binance-perp-open-interest", "open_interest")
    # kline: 有缺口(未核实 → 进分母不进分子)
    store.upsert(provider_table("binance-perp-kline"), _kline_df(drop=[5]))
    # open_interest: 缺口(核实项)
    store.upsert(
        provider_table("binance-perp-open-interest"), _oi_df(drop=[2])
    )
    store.close()

    md_path = tmp_path / "r.md"
    generate_validation_report(path, md_path)
    md = md_path.read_text(encoding="utf-8")
    # 缺口 1/2:kline 缺口未核实,只算 open_interest 的核实项
    assert "1/2 经核实" in md
    assert md.count("1/2 经核实") == 1

    data = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
    notes = {n["key"]: n for n in data["source_notes"]}
    assert notes["open_interest_vision_gap"]["verified"] == 1
    assert notes["open_interest_vision_gap"]["total"] == 2


def test_report_outlier_note_appears_only_when_flagged(tmp_path):
    """异常值说明:基于 MAD 的方法论提示只在有异常值被标出时出现。"""
    # 干净缓存 → 无异常值 → 不出方法论说明
    clean_md = tmp_path / "clean.md"
    generate_validation_report(_build_cache(tmp_path), clean_md)
    assert "关于异常值" not in clean_md.read_text(encoding="utf-8")

    # 含异常值的缓存 → 出说明,且点明 MAD 与"不代表数据错误"
    path = tmp_path / "outlier_cache.duckdb"
    store = Store(path)
    store.ensure_provider_table("binance-perp-kline", "kline")
    store.upsert(
        provider_table("binance-perp-kline"),
        _kline_df(outlier_at=[3]),  # close=1e7,明显异常
    )
    store.close()

    md_path = tmp_path / "r.md"
    generate_validation_report(path, md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "**关于异常值**" in md
    assert "MAD" in md
    assert "真实市场波动" in md


def test_report_mix_note_explains_not_applicable(tmp_path):
    """现货/永续混用显示不适用时,给出结构上禁止混用的说明。"""
    cache = _build_cache(tmp_path)  # 缓存无 market_type 列 → 混用检查不适用
    md_path = tmp_path / "r.md"
    generate_validation_report(cache, md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "| 现货/永续混用 | 不适用 |" in md
    assert "**关于现货/永续混用**" in md
    assert "不持久化 market_type" in md
    assert "结构上不可能混入两类市场" in md


def test_generate_report_flags_bad_series(tmp_path):
    # 插入一条含缺口 + 重复时间戳的序列
    path = tmp_path / "cache.duckdb"
    store = Store(path)
    store.ensure_provider_table("binance-perp-kline", "kline")
    store.upsert(
        provider_table("binance-perp-kline"),
        _kline_df(drop=[5], duplicates=1),
    )
    store.close()

    artifacts = generate_validation_report(path, tmp_path / "r.md")
    assert artifacts.verdict == "WARN"
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "缺失区间" in md
    assert "重复时间戳" in md


def test_missing_cache_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        generate_validation_report(tmp_path / "nope.duckdb", tmp_path / "r.md")


def test_unknown_data_type_rejected(tmp_path):
    cache = _build_cache(tmp_path)
    with pytest.raises(ValueError, match="未知 data_type"):
        generate_validation_report(cache, tmp_path / "r.md", data_types=["nope"])


# ── CLI 入口 ──────────────────────────────────────────────────────────


def test_cli_validate_report(tmp_path, monkeypatch):
    from superplatform.runtime.cli import cmd_validate_report

    cache = _build_cache(tmp_path)
    out = tmp_path / "cli_report.md"

    args = _simple_args(cache, out)
    cmd_validate_report(args)
    assert out.exists()
    assert out.with_suffix(".json").exists()
    assert "G1 数据校验报告" in out.read_text(encoding="utf-8")


# ── 干净环境自取数据 ─────────────────────────────────────────────────


def _simple_args(cache, out, *, fetch=False, config="config/default.yaml"):
    class _Args:
        pass

    args = _Args()
    args.cache = str(cache)
    args.output = str(out)
    args.data_type = None
    args.outlier_method = "mad"
    args.outlier_threshold = 15.0
    args.fetch = fetch
    args.config = config
    return args


def test_fetch_plan_derives_requests_from_config():
    from superplatform.runtime.cli import _validate_fetch_plan

    config = {
        "defaults": {"exchange": "binance", "market": "perpetual"},
        "factors": {
            # Real discovered factors; providers resolve from the defaults.
            "momentum": {
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "frequency": "1d",
            },
            "funding_rate_annualized": {
                "symbols": ["BTCUSDT"],
                "frequencies": {"funding_rate": "8h"},
                "evaluation_price": {"frequency": "8h"},
            },
        },
        "evaluation": {"sample_start": "2021-01-01", "sample_end": "2025-06-30"},
    }
    plan = _validate_fetch_plan(config, registry=_stub_registry())
    keys = {(p["provider_id"], p["symbol"], p["frequency"].value) for p in plan}
    assert ("binance-perp-kline", "BTCUSDT", "1d") in keys
    assert ("binance-perp-kline", "ETHUSDT", "1d") in keys
    assert ("binance-perp-funding-rate", "BTCUSDT", "8h") in keys
    # evaluation_price kline (8h) is a distinct request from momentum's 1d kline
    assert ("binance-perp-kline", "BTCUSDT", "8h") in keys
    assert len(plan) == 4


def test_fetch_plan_respects_data_type_filter():
    from superplatform.runtime.cli import _validate_fetch_plan

    config = {
        "defaults": {"exchange": "binance", "market": "perpetual"},
        "factors": {
            "oi_change_ratio": {
                "symbols": ["BTCUSDT"],
                "frequencies": {"open_interest": "4h"},
                "evaluation_price": {"frequency": "4h"},
            },
        },
        "evaluation": {"sample_start": "2021-01-01", "sample_end": "2025-06-30"},
    }
    plan = _validate_fetch_plan(
        config, data_types=["open_interest"], registry=_stub_registry()
    )
    assert plan, "filter should keep at least the requested data type"
    assert all(p["data_type"] == "open_interest" for p in plan)
    assert all(p["frequency"].value == "4h" for p in plan)


def test_cli_auto_fetches_when_cache_missing(tmp_path, monkeypatch):
    """Missing cache must trigger a fetch, then still produce a report."""
    from superplatform.data.store import Store
    from superplatform.runtime import cli

    calls: dict = {}

    def fake_fetch(cache_path, config, data_types=None):
        calls["cache"] = str(cache_path)
        calls["config"] = config
        store = Store(cache_path)  # 与真实路径一致:建库,含全部表
        store.close()
        return []

    monkeypatch.setattr(cli, "_fetch_cache_data", fake_fetch)

    out = tmp_path / "auto.md"
    cache = tmp_path / "missing.duckdb"
    args = _simple_args(cache, out)
    args.fetch = False

    cli.cmd_validate_report(args)
    assert calls["cache"] == str(cache)
    assert out.exists()
    assert out.with_suffix(".json").exists()


def test_cli_fetch_flag_forces_fetch_even_when_cache_exists(tmp_path, monkeypatch):
    from superplatform.runtime import cli

    cache = _build_cache(tmp_path)
    calls: dict = {}

    def fake_fetch(cache_path, config, data_types=None):
        calls["cache"] = str(cache_path)
        return []

    monkeypatch.setattr(cli, "_fetch_cache_data", fake_fetch)

    out = tmp_path / "force.md"
    args = _simple_args(cache, out, fetch=True)
    cli.cmd_validate_report(args)
    assert calls["cache"] == str(cache)
