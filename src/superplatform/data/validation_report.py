"""G1 数据校验报告生成器。

审计 DuckDB 缓存存储中每条时间序列,并产出全面详实的 Markdown 报告
(附带同名 JSON 留痕),覆盖 G1 的三个硬性检查项:

- 时区统一: 所有时间戳必须为 UTC-aware
- 可增量更新: 验证缓存增量书签(empty_ranges)与序列覆盖范围
- 缺失与异常显式标记: 缺失区间、异常值逐条列出

此外还覆盖 Schema 契约(列、dtype、空值、重复时间戳)、频率一致性、
现货/永续混用守卫等数据层接口契约。

用法:
    superplatform validate-report --cache data/cache.duckdb --output reports/data_validation_report.md

报告只读打开缓存,不写入任何数据。缓存被其他进程(如开发服务器)占用时
会报错并提示先停止占用进程或改传别的缓存路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from superplatform.data.schema import (
    BasisSchema,
    FundingRateSchema,
    KLineSchema,
    OpenInterestSchema,
    _dtype_matches,
)
from superplatform.data.validators import (
    check_spot_perpetual_mix,
    check_utc,
    detect_missing,
    detect_outliers,
)

# provider data_type -> 契约 Schema (per-provider cache tables share the
# data_type's schema, so the audit is keyed by data_type, not table name).
_SCHEMA_BY_DATA_TYPE: dict[str, type] = {
    "kline": KLineSchema,
    "funding_rate": FundingRateSchema,
    "open_interest": OpenInterestSchema,
    "basis": BasisSchema,
}

# 每个 data_type 参与异常检测的数值列(MAD 方法)
_NUMERIC_COLS: dict[str, list[str]] = {
    "kline": [
        "open", "high", "low", "close",
        "volume", "quote_volume",
        "taker_buy_volume", "taker_buy_quote_volume",
    ],
    "funding_rate": ["funding_rate"],
    "open_interest": ["open_interest"],
    "basis": ["spot_price", "perpetual_price", "basis_pct"],
}

# 频率字符串 -> 预期 bar 宽度,用于频率一致性检查
_FREQ_DELTAS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}

# 存储层附加的元数据列,不参与 Schema 契约校验
_METADATA_COLS = {"symbol", "frequency"}

# 检查项通过情况表的行定义:(key, 显示名)。顺序即展示顺序。
_CHECK_ORDER: list[tuple[str, str]] = [
    ("utc", "时区(UTC)"),
    ("schema", "Schema 契约(列/dtype)"),
    ("freq", "频率一致性"),
    ("dup", "重复时间戳"),
    ("gaps", "缺失区间"),
    ("nulls", "空值"),
    ("outliers", "异常值"),
    ("mix", "现货/永续混用"),
]


def _fmt_delta(delta: timedelta | pd.Timedelta | None) -> str:
    """把 timedelta 统一格式化为人类可读的短字符串(如 '1d' / '1h' / '30m')。

    timedelta.str 与 pd.Timedelta.str 输出不一致('1 day, 0:00:00' vs
    '1 days 00:00:00'),报告里统一用 _fmt_delta 保持一致性。
    """
    if delta is None:
        return "N/A"
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _fmt_ts(ts: pd.Timestamp | None) -> str | None:
    """统一格式化为 ISO 字符串,供 JSON 留痕使用。"""
    if ts is None:
        return None
    return ts.isoformat()


def _json_default(obj: Any) -> Any:
    """JSON 序列化兜底:把 SeriesAudit / 时间对象转成结构化数据。

    - SeriesAudit → to_dict()(结构化,非字符串 repr)
    - pd.Timestamp / pd.Timedelta → ISO 字符串
    - 其余 → str()
    """
    if isinstance(obj, SeriesAudit):
        return obj.to_dict()
    if isinstance(obj, (pd.Timestamp, pd.Timedelta)):
        return obj.isoformat()
    return str(obj)


@dataclass
class SeriesAudit:
    """一条序列的完整校验结果。"""

    provider: str
    data_type: str
    symbol: str
    frequency: str
    row_count: int
    time_start: pd.Timestamp | None = None
    time_end: pd.Timestamp | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    utc: dict[str, Any] = field(default_factory=dict)
    freq_expected: timedelta | None = None
    freq_observed: timedelta | None = None
    freq_consistent: bool | None = None
    duplicate_timestamps: int = 0
    missing_gaps: list[dict[str, Any]] = field(default_factory=list)
    gaps_suppressed_count: int = 0
    expected_bars: int | None = None
    missing_bars: int | None = None
    missing_pct: float | None = None
    null_summary: dict[str, int] = field(default_factory=dict)
    outliers: dict[str, dict[str, Any]] = field(default_factory=dict)
    spot_perpetual: dict[str, Any] = field(default_factory=dict)
    hard_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.hard_failures:
            return "FAIL"
        if self.warnings:
            return "WARN"
        return "PASS"

    def to_dict(self) -> dict[str, Any]:
        """结构化序列化,供 JSON 留痕使用。"""
        return {
            "provider": self.provider,
            "data_type": self.data_type,
            "symbol": self.symbol,
            "frequency": self.frequency,
            "row_count": self.row_count,
            "time_start": _fmt_ts(self.time_start),
            "time_end": _fmt_ts(self.time_end),
            "status": self.status,
            "utc": self.utc,
            "freq_expected": _fmt_delta(self.freq_expected),
            "freq_observed": _fmt_delta(self.freq_observed),
            "freq_consistent": self.freq_consistent,
            "duplicate_timestamps": self.duplicate_timestamps,
            "missing_gaps": self.missing_gaps,
            "gaps_suppressed_count": self.gaps_suppressed_count,
            "expected_bars": self.expected_bars,
            "missing_bars": self.missing_bars,
            "missing_pct": self.missing_pct,
            "null_summary": self.null_summary,
            "outliers": self.outliers,
            "spot_perpetual": self.spot_perpetual,
            "hard_failures": self.hard_failures,
            "warnings": self.warnings,
        }


def _coerce_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """把缺失时区的时间戳解释为 UTC(校验报告只用于展示)。"""
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def audit_series(
    data_type: str,
    df: pd.DataFrame,
    frequency: str,
    *,
    provider: str = "",
    outlier_method: str = "mad",
    outlier_threshold: float = 15.0,
    max_missing_pct: float = 10.0,
) -> SeriesAudit:
    """对一条序列运行完整的校验套件。

    Args:
        data_type: provider data_type(kline / funding_rate / open_interest /
            basis)——决定契约 Schema 与异常检测列。
        df: 该序列的数据(含 symbol/frequency 元数据列)。
        frequency: 声明的频率字符串(如 "1d")。
        provider: provider_id(如 binance-perp-kline),报告按此分组。
        outlier_method: 异常检测方法(mad / zscore)。
        outlier_threshold: 异常阈值(多少个偏差)。
        max_missing_pct: 缺失占比告警阈值(百分数,默认 10,对应 config
            data.validation.max_missing_pct)。超过则在 warnings 里显式标记,
            序列判定为 WARN(不能静默 PASS)。

    Note:
        数字资产序列重尾且带趋势,普通波动(如 5-10 倍 MAD 的单根 K 线
        跳动、牛市资金费率)是真实市场行为。默认 15 个偏差只标记真正病态
        的值,避免"统计上极端但市场正常"的点淹没报告。
    """
    schema_cls = _SCHEMA_BY_DATA_TYPE[data_type]
    audit = SeriesAudit(
        provider=provider or data_type,
        data_type=data_type,
        symbol=str(df["symbol"].iloc[0]) if "symbol" in df.columns and len(df) else "",
        frequency=frequency,
        row_count=len(df),
    )

    # ── 时间范围(UTC 归一后展示)────────────────────────────
    if "timestamp" in df.columns and len(df):
        ts = pd.to_datetime(df["timestamp"])
        audit.time_start = _coerce_utc(ts.min())
        audit.time_end = _coerce_utc(ts.max())

    # ── Schema 契约(去掉存储层元数据列)─────────────────────
    contract_df = df.drop(columns=[c for c in _METADATA_COLS if c in df.columns])
    audit.schema = schema_cls.validate_df(contract_df)
    if audit.schema.get("missing_cols"):
        audit.hard_failures.append(
            "Schema 缺列: " + ", ".join(audit.schema["missing_cols"])
        )
    # int↔float 的数值宽度差异(如 Store 把 trades 存成 BIGINT)不视为违约:
    # 数据语义相同,只是存储宽度不同。真正的 dtype 违约(如字符串/对象/布尔
    # 顶替数值列)才记为 warning。
    real_mismatches = _non_numeric_dtype_mismatches(contract_df, schema_cls)
    audit.schema["dtype_mismatches"] = real_mismatches
    audit.schema["valid"] = (
        len(audit.schema.get("missing_cols", [])) == 0 and not real_mismatches
    )
    if real_mismatches:
        audit.warnings.append(
            "Schema dtype 违约: " + "; ".join(real_mismatches)
        )

    # ── UTC 检查 ────────────────────────────────────────────
    audit.utc = check_utc(df)
    if not audit.utc.get("is_utc"):
        audit.hard_failures.append(
            f"时间戳非 UTC-aware(tz={audit.utc.get('tz')})"
        )

    # ── 重复时间戳 ──────────────────────────────────────────
    if "timestamp" in df.columns and len(df):
        audit.duplicate_timestamps = int(df["timestamp"].duplicated().sum())
        if audit.duplicate_timestamps:
            audit.warnings.append(
                f"{audit.duplicate_timestamps} 个重复时间戳"
            )

    # ── 频率一致性 ──────────────────────────────────────────
    audit.freq_expected = _FREQ_DELTAS.get(frequency)
    if audit.freq_expected is not None and len(df) >= 2 and "timestamp" in df.columns:
        diffs = pd.to_datetime(df["timestamp"]).sort_values().diff().dropna()
        audit.freq_observed = pd.Timedelta(diffs.median())
        # 相对容差: 观测中位 bar 宽必须在期望 ±5% 内。同一频率的常规缺口
        # 不会改变中位数(如每天 1d bar 间夹一个 1h bar,中位数仍为 1d)。
        tolerance = audit.freq_expected * 0.05
        audit.freq_consistent = (
            abs(audit.freq_observed - audit.freq_expected) <= tolerance
        )
        if not audit.freq_consistent:
            audit.warnings.append(
                f"频率不一致: 声明 {_fmt_delta(audit.freq_expected)}, "
                f"观测中位数 {_fmt_delta(audit.freq_observed)}"
            )

    # ── 缺失区间(显式标记)─────────────────────────────────
    gaps = detect_missing(df, freq=audit.freq_expected)
    audit.missing_gaps = gaps.to_dict("records")
    if audit.missing_gaps:
        if audit.freq_consistent is False:
            # 声明频率与观测中位 bar 宽不一致时(如 4h 标签下存 Binance
            # 8h 资金费率),按声明频率算出的"缺失"全是假象:每条真实间隔
            # 都超过 1.5×声明频率。根因由频率一致性警告给出,逐条罗列
            # 几千个间隔只会淹没报告。
            audit.gaps_suppressed_count = len(audit.missing_gaps)
            audit.missing_gaps = []
            audit.warnings.append(
                "缺失区间不逐条罗列:声明频率与观测不一致,"
                f"{audit.gaps_suppressed_count} 个按声明频率检出的间隔均为假缺失"
            )
        else:
            total = sum(
                (pd.Timestamp(g["gap_end"]) - pd.Timestamp(g["gap_start"])).total_seconds()
                for g in audit.missing_gaps
            )
            audit.warnings.append(
                f"{len(audit.missing_gaps)} 个缺失区间, 累计 {total / 3600:.1f} 小时"
            )

    # ── 缺失占比(missing_pct)────────────────────────────────
    # 口径: [time_start, time_end] 内按声明频率应有的 bar 数为分母,
    # 缺位 bar 数为分子。声明频率与观测不一致时缺口全是假象(已被上面
    # 抑制),missing_pct 同样不适用,置 None 显示 N/A。
    if (
        audit.freq_expected is not None
        and audit.freq_consistent is not False
        and "timestamp" in df.columns
        and len(df) >= 2
        and audit.time_start is not None
        and audit.time_end is not None
    ):
        span = audit.time_end - audit.time_start
        # round 防整倍数浮点毛刺(9 天 / 1 天 = 8.999…)。
        span_bars = int(round(span / audit.freq_expected, 6)) + 1
        n_unique = int(df["timestamp"].nunique())
        audit.expected_bars = span_bars
        audit.missing_bars = max(span_bars - n_unique, 0)
        if span_bars > 0:
            audit.missing_pct = audit.missing_bars / span_bars * 100.0
        if audit.missing_pct is not None and audit.missing_pct > max_missing_pct:
            audit.warnings.append(
                f"缺失占比 {audit.missing_pct:.2f}% 超过阈值 {max_missing_pct:g}%"
            )

    # ── 空值汇总 ────────────────────────────────────────────
    audit.null_summary = {col: int(df[col].isna().sum()) for col in df.columns}
    non_meta_nulls = {
        c: n for c, n in audit.null_summary.items()
        if c not in _METADATA_COLS and n > 0
    }
    if non_meta_nulls:
        audit.warnings.append(
            "非元数据列存在空值: "
            + ", ".join(f"{c}={n}" for c, n in non_meta_nulls.items())
        )

    # ── 异常值(MAD / zscore)显式标记────────────────────────
    audit.outliers = _outlier_examples(df, data_type, outlier_method, outlier_threshold)
    if audit.outliers:
        audit.warnings.append(
            f"{sum(o['count'] for o in audit.outliers.values())} 个异常值被标记"
        )

    # ── 现货 / 永续混用守卫 ─────────────────────────────────
    audit.spot_perpetual = check_spot_perpetual_mix(df)
    if audit.spot_perpetual.get("is_mixed"):
        audit.hard_failures.append("同一序列混用了现货与永续数据")

    return audit


def _non_numeric_dtype_mismatches(
    df: pd.DataFrame,
    schema_cls: type,
) -> list[str]:
    """返回真正的 dtype 违约(int↔float 宽度差异除外)。

    Store 把数值列存成固定宽度(BIGINT / DOUBLE),读回后 pandas 会给
    整数列 int64。Schema 期望 float64 时,严格比较会误报。这里只把
    "期望数值类型但实际是非数值类型" 视为违约:
      - 期望 np.float64 / np.int64, 实际也是数值 → 兼容,不报
      - 期望数值,实际是 object/str/bool/datetime → 违约
    """
    mismatches: list[str] = []
    for col, expected in schema_cls.columns.items():
        if col not in df.columns:
            continue
        actual = df[col].dtype
        if _dtype_matches(actual, expected):
            continue
        expected_is_numeric = _is_numeric_dtype(expected)
        actual_is_numeric = _is_numeric_dtype(actual)
        if expected_is_numeric and actual_is_numeric:
            # int64 vs float64 等宽度差异: 语义兼容,不违约
            continue
        mismatches.append(f"{col}: expected {expected}, got {actual}")
    return mismatches


def _is_numeric_dtype(dtype) -> bool:
    """判断 numpy 类型 / pandas dtype 是否数值。"""
    try:
        return bool(np.issubdtype(dtype, np.number))
    except TypeError:
        return False


def _outlier_examples(
    df: pd.DataFrame,
    data_type: str,
    method: str,
    threshold: float,
) -> dict[str, dict[str, Any]]:
    """按列检测异常值并收集示例(时间戳 + 值),供报告展示。"""
    out: dict[str, dict[str, Any]] = {}
    for col in _NUMERIC_COLS.get(data_type, []):
        if col not in df.columns or df[col].dropna().empty:
            continue
        mask = detect_outliers(df[col], method=method, threshold=threshold)
        n = int(mask.sum())
        if n == 0:
            continue
        flagged = df.loc[mask]
        examples: list[dict[str, Any]] = []
        for _, row in flagged.head(5).iterrows():
            examples.append({
                "ts": str(_coerce_utc(row["timestamp"])),
                "value": float(row[col]),
            })
        out[col] = {
            "count": n,
            "pct": round(n / len(df) * 100, 3),
            "method": method,
            "threshold": threshold,
            "examples": examples,
        }
    return out


def _list_series(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    """列出表中所有 (symbol, frequency) 序列对。表不存在返回空。"""
    if not _table_exists(con, table):
        return []
    df = con.execute(
        f"SELECT DISTINCT symbol, frequency FROM {table} ORDER BY 1, 2"
    ).fetchdf()
    return [(str(r.symbol), str(r.frequency)) for r in df.itertuples()]


def _table_row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    if not _table_exists(con, table):
        return 0
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """缓存库可能只建了部分表(老库或部分数据源),缺失的表跳过。"""
    row = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()
    return row is not None


def _provider_tables(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Read the provider_tables metadata: provider_id, data_type, table_name.

    The cache is self-describing: every per-provider cache table is recorded
    here when first written. A cache with no rows has no provider series to
    audit (an empty or pre-provider-schema cache — refetch to populate).
    """
    try:
        df = con.execute(
            "SELECT provider_id, data_type, table_name FROM provider_tables "
            "ORDER BY provider_id"
        ).fetchdf()
    except duckdb.Error:
        return []
    return df.to_dict("records")


def _audit_empty_ranges(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """读取增量更新书签(empty_ranges),证明"已验证为空"的区间被持久化。"""
    try:
        df = con.execute(
            "SELECT data_type, symbol, frequency, start_ts, end_ts "
            "FROM empty_ranges ORDER BY 1, 2, 3, 4"
        ).fetchdf()
    except duckdb.Error:
        # 老库可能没有这张表
        return []
    records: list[dict[str, Any]] = []
    for r in df.itertuples():
        records.append({
            "data_type": str(r.data_type),
            "symbol": str(r.symbol),
            "frequency": str(r.frequency),
            "start_ts": str(r.start_ts),
            "end_ts": str(r.end_ts),
        })
    return records


def _verdict(series_audits: list[SeriesAudit], empty_ranges: list[dict]) -> dict[str, Any]:
    """汇总整体判定。

    硬门槛: 任一序列 FAIL 则整体 FAIL;无 FAIL 但有 WARN 则 WARN。
    """
    fails = [s for s in series_audits if s.status == "FAIL"]
    warns = [s for s in series_audits if s.status == "WARN"]
    if fails:
        verdict = "FAIL"
    elif warns:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "series_total": len(series_audits),
        "series_pass": len(series_audits) - len(fails) - len(warns),
        "series_warn": len(warns),
        "series_fail": len(fails),
        "failed_series": [
            {"provider": s.provider, "symbol": s.symbol, "frequency": s.frequency}
            for s in fails
        ],
        "empty_range_bookmarks": len(empty_ranges),
    }


def _check_outcome(audit: SeriesAudit, key: str) -> str:
    """单条序列在某检查项上的结果: pass / fail / n/a(无法检查)。

    规则:
    - 频率一致性 None(序列过短/频率未知)→ 无法检查,算通过。
    - 缺失区间只算真实缺失;因声明频率与观测不一致被抑制的"假缺失"由
      频率一致性项负责,不在这里重复判负。
    - 现货/永续混用依赖 market_type 列;缓存无此列(该信息在采集层打标,
      不落存储)时整项不适用。
    """
    if key == "utc":
        return "pass" if audit.utc.get("is_utc") is True else "fail"
    if key == "schema":
        return "pass" if audit.schema.get("valid") is True else "fail"
    if key == "freq":
        return "pass" if audit.freq_consistent is not False else "fail"
    if key == "dup":
        return "pass" if audit.duplicate_timestamps == 0 else "fail"
    if key == "gaps":
        return "pass" if not audit.missing_gaps else "fail"
    if key == "nulls":
        non_meta = {
            c: n for c, n in audit.null_summary.items()
            if c not in _METADATA_COLS and n > 0
        }
        return "pass" if not non_meta else "fail"
    if key == "outliers":
        return "pass" if not audit.outliers else "fail"
    if key == "mix":
        sp = audit.spot_perpetual
        if sp.get("error") is not None or sp.get("is_mixed") is None:
            return "n/a"
        return "fail" if sp.get("is_mixed") else "pass"
    raise ValueError(f"未知检查项: {key}")


def _build_checks(audits: list[SeriesAudit]) -> dict[str, Any]:
    """逐检查项聚合通过/失败/不适用计数,写入报告与 JSON 留痕。"""
    checks: dict[str, Any] = {}
    for key, label in _CHECK_ORDER:
        outcomes = [_check_outcome(a, key) for a in audits]
        checks[key] = {
            "label": label,
            "pass": outcomes.count("pass"),
            "fail": outcomes.count("fail"),
            "n_a": outcomes.count("n/a"),
            "total": len(audits),
        }
    return checks


# ── 已知数据源限制 ────────────────────────────────────────────────────
#
# 这些是"源端就没有该数据"的固定说明:校验检出的空值/缺口先经直接访问
# 数据源归档核实,确认为数据源不提供或归档缺失,而非采集/缓存 bug。
# 报告开头固定给出说明,审查者不必猜测"为什么这里还空着"。

_OPEN_INTEREST_GAP_NOTE = (
    "以下缺失区间经直接核实为 Binance 源端即无该时点数据:data.binance.vision "
    "归档中缺失日当日归档不存在,且相邻归档亦未覆盖(每日归档按起始日命名、"
    "尾部跨到次日凌晨,故仅当日归档 404 不代表该日缺失,需连前一归档尾部一并核实),"
    "重拉不会恢复。"
)


def _build_source_notes(series_audits: list[SeriesAudit]) -> list[dict[str, Any]]:
    """根据实际审计结果,组装"已知数据源限制"固定说明。

    只在对应情况确实出现在审计结果中时才给出说明(保持最低噪音):
    - open_interest 存在缺失区间 → 该日期源端每日归档缺失或截断,已核实。
    说明文本固定,具体日期/序列从审计结果动态摘取。
    每个说明带 verified/total 核实比例:该问题的失败总数里有多少经核实是
    源端问题;其他来源(如其他表的空值/缺口)未核实,不算进分子。
    """
    notes: list[dict[str, Any]] = []

    # 缺失区间: 全部序列的缺口 vs 其中 open_interest 缺口(源端核实项)
    all_gaps = [g for a in series_audits for g in a.missing_gaps]
    oi_gaps = [
        (a, g)
        for a in series_audits
        if a.data_type == "open_interest"
        for g in a.missing_gaps
    ]
    if oi_gaps:
        notes.append({
            "key": "open_interest_vision_gap",
            "title": "open_interest 缺失区间:源端归档缺失",
            "verified": len(oi_gaps),
            "total": len(all_gaps),
            "detail": _OPEN_INTEREST_GAP_NOTE,
            "items": [
                f"{a.symbol}·{a.frequency}: {g['gap_start']} → {g['gap_end']}"
                for a, g in oi_gaps
            ],
        })

    return notes


# ── Markdown 渲染 ────────────────────────────────────────────────────


def _md_check_summary(audits: list[SeriesAudit]) -> list[str]:
    """开头一节:逐检查项通过计数,向审查者证明每项校验都跑过、过了多少。

    只给计数,不列符号——未通过的序列详情在第 2 节逐表展开,保持最低噪音。
    """
    if not audits:
        return ["### 检查项通过情况", "", "无缓存序列。", ""]
    checks = _build_checks(audits)
    lines = [
        "### 检查项通过情况",
        "",
        "每项校验在所有序列上的通过计数;未通过的序列详情见「逐表判定与详情」。",
        "",
        "| 检查项 | 通过 |",
        "| --- | ---: |",
    ]
    for key, label in _CHECK_ORDER:
        c = checks[key]
        if c["n_a"] == c["total"]:
            lines.append(f"| {label} | 不适用 |")
        elif c["n_a"]:
            lines.append(
                f"| {label} | {c['pass']}/{c['total'] - c['n_a']} "
                f"(另 {c['n_a']} 条不适用) |"
            )
        else:
            lines.append(f"| {label} | {c['pass']}/{c['total']} |")
    lines.append("")
    return lines


def _md_coverage_table(audits: list[SeriesAudit]) -> list[str]:
    """逐序列覆盖表: 每条序列的 earliest / latest / missing_pct 一览。

    验收口径「每 symbol earliest/latest/missing_pct」落在这张表:PASS 序列
    不展开详情块,但覆盖边界与缺失占比在此全量列出,一条不漏。
    """
    if not audits:
        return []
    lines = [
        "### 逐序列覆盖(earliest / latest / missing_pct)",
        "",
        "每条序列的时间边界与缺失占比;缺失占比口径: [earliest, latest] 内按声明",
        "频率应有的 bar 数为分母。N/A = 频率未知或声明频率与观测不一致(不适用)。",
        "",
        "| Provider | Symbol | 频率 | 行数 | Earliest (UTC) | Latest (UTC) | 缺失% | 判定 |",
        "| --- | --- | --- | ---: | --- | --- | ---: | --- |",
    ]
    for a in audits:
        earliest = (
            a.time_start.strftime("%Y-%m-%d") if a.time_start is not None else "N/A"
        )
        latest = (
            a.time_end.strftime("%Y-%m-%d") if a.time_end is not None else "N/A"
        )
        pct = f"{a.missing_pct:.2f}" if a.missing_pct is not None else "N/A"
        lines.append(
            f"| {a.provider} | {a.symbol} | {a.frequency} | {a.row_count} "
            f"| {earliest} | {latest} | {pct} | {a.status} |"
        )
    lines.append("")
    return lines


def _md_outlier_note(audits: list[SeriesAudit]) -> list[str]:
    """异常检测方法说明:基于 MAD,检出的极端值多为真实市场波动,不代表数据有错。

    数字资产序列重尾且带趋势(大阳线、牛市资金费率等),统计上偏大的值是
    正常市场行为。此说明只在确实有异常值被标出时给出,避免把正常报告弄脏。
    """
    if not any(a.outliers for a in audits):
        return []
    return [
        "**关于异常值**:异常检测基于 MAD(中位数绝对偏差),标记的是偏离中位数"
        "超过阈值的统计极端值(方法与阈值见本报告头)。数字资产序列重尾且带趋势,"
        "此类极端值多为真实市场波动而非数据错误,不单独构成数据质量问题。",
        "",
    ]


def _md_mix_note(audits: list[SeriesAudit]) -> list[str]:
    """现货/永续混用检查的说明:缓存不持久化 market_type,混用防护在网络层。

    该检查在所有序列上都不适用(缓存无 market_type 列)时才给出说明,
    否则检查真跑了就靠结果说话,不啰嗦。
    """
    if not audits:
        return []
    if any(_check_outcome(a, "mix") != "n/a" for a in audits):
        return []
    return [
        "**关于现货/永续混用**:该检查显示「不适用」,因为缓存不持久化 "
        "market_type——混用防护在网络层完成:每条序列绑定单一 provider"
        "(如 `binance-perp-kline`),market_type 在 provider 构造时打标,"
        "同一序列在结构上不可能混入两类市场。",
        "",
    ]


def _md_source_notes(notes: list[dict[str, Any]]) -> list[str]:
    """「已知数据源限制」固定说明:空值/缺口的源端根因,审查者不用再猜。

    每个说明标注 verified/total 核实比例:该问题失败总数里有多少经核实是
    源端问题,未被核实的不算在内。
    """
    if not notes:
        return []
    lines = [
        "### 已知数据源限制",
        "",
        "以下空值/缺口经直接核实,是数据源本身不提供或归档缺失,"
        "而非采集或缓存 bug,重拉不会恢复。括号内为「经核实数/失败总数」。",
        "",
    ]
    for note in notes:
        ratio = ""
        if "verified" in note and "total" in note:
            ratio = f"({note['verified']}/{note['total']} 经核实)"
        lines.append(f"- **{note['title']}**{ratio}: {note['detail']}")
        for item in note.get("items", []):
            lines.append(f"  - {item}")
    lines.append("")
    return lines


def _md_verdict_grouping(table: dict[str, Any]) -> list[str]:
    """每表一段判定分组:只列符号清单,不展开细节。

    规则:
    - 某一判定覆盖全表全部序列时折叠为 `全部`(如全是 WARN → `WARN (N): 全部`)。
    - 通过序列只在清单中出现一次,不展开详情(减噪)。
    """
    audits: list[SeriesAudit] = table["series"]
    lines = [f"### Provider `{table['provider']}` ({len(audits)} 条)", ""]
    if not audits:
        lines.append("该表无缓存序列。")
        lines.append("")
        return lines
    total = len(audits)
    for status, label in (("FAIL", "FAIL"), ("WARN", "WARN"), ("PASS", "PASS")):
        group = [a for a in audits if a.status == status]
        cnt = len(group)
        if cnt == 0:
            continue
        if cnt == total:
            lines.append(f"- `{label}` ({cnt}): 全部")
        else:
            names = "、".join(f"{a.symbol}·{a.frequency}" for a in group)
            lines.append(f"- `{label}` ({cnt}): {names}")
    lines.append("")
    return lines


def _md_series_section(audit: SeriesAudit) -> list[str]:
    """非通过序列的详情。只列实际存在的问题,通过项(UTC/Schema/守卫等)
    不逐条罗列——它们通过了,罗列只是噪音;判定分组已给出通过清单。
    """
    lines = [
        f"### {audit.provider} · {audit.symbol} · {audit.frequency} · `{audit.status}`",
        "",
        f"- **行数**: {audit.row_count}",
    ]
    if audit.time_start is not None:
        lines.append(f"- **时间范围**: {audit.time_start} → {audit.time_end} (UTC)")
    if audit.missing_pct is not None:
        lines.append(
            f"- **缺失占比**: {audit.missing_pct:.2f}% "
            f"(缺 {audit.missing_bars}/{audit.expected_bars} 条)"
        )

    for hf in audit.hard_failures:
        lines.append(f"- **硬性失败**: {hf}")

    if audit.freq_consistent is False:
        lines.append(
            f"- **频率不一致**: 声明 {_fmt_delta(audit.freq_expected)}, "
            f"观测 {_fmt_delta(audit.freq_observed)}"
        )

    if audit.duplicate_timestamps:
        lines.append(f"- **重复时间戳**: {audit.duplicate_timestamps}")

    s = audit.schema
    if s.get("missing_cols") or s.get("dtype_mismatches") or s.get("extra_cols"):
        parts = []
        if s.get("missing_cols"):
            parts.append("缺列: " + ", ".join(s["missing_cols"]))
        if s.get("dtype_mismatches"):
            parts.append("dtype: " + ", ".join(s["dtype_mismatches"]))
        if s.get("extra_cols"):
            parts.append("多余列: " + ", ".join(s["extra_cols"]))
        lines.append("- **Schema**: " + "; ".join(parts))

    # 缺失区间
    if audit.gaps_suppressed_count:
        lines.append(
            f"- **缺失区间**: 按声明频率检出 {audit.gaps_suppressed_count} 个间隔,"
            "因声明频率与观测不一致,均为假缺失,不逐条罗列"
        )
    elif audit.missing_gaps:
        lines.append(f"- **缺失区间** ({len(audit.missing_gaps)}):")
        for g in audit.missing_gaps:
            lines.append(
                f"  - {g['gap_start']} → {g['gap_end']} "
                f"(持续 {g['gap_duration']})"
            )

    non_meta_nulls = {
        c: n for c, n in audit.null_summary.items()
        if c not in _METADATA_COLS and n > 0
    }
    if non_meta_nulls:
        lines.append(
            "- **空值**: " + ", ".join(f"{c}={n}" for c, n in non_meta_nulls.items())
        )

    # 异常值
    if audit.outliers:
        lines.append("- **异常值** (显式标记):")
        for col, info in audit.outliers.items():
            ex = "; ".join(
                f"{e['ts']}={e['value']:.4g}" for e in info["examples"]
            )
            lines.append(
                f"  - {col}: {info['count']} 个 ({info['pct']}%), "
                f"示例: {ex}"
            )

    sp = audit.spot_perpetual
    if sp.get("is_mixed"):
        lines.append(
            "- **现货/永续混用**: " + ", ".join(sp["market_types_present"])
        )

    lines.append("")
    return lines


def _render_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    verdict = report["verdict"]
    lines: list[str] = [
        "# G1 数据校验报告",
        "",
        f"生成时间: {meta['generated_at']} (UTC)",
        f"缓存路径: {meta['cache_path']}",
        f"异常检测: {meta['outlier_method']} / 阈值 {meta['outlier_threshold']}",
        f"缺失占比告警阈值: {meta['max_missing_pct']}%",
        "",
        f"**整体判定: `{verdict['verdict']}`**",
        "",
        "## 1. 摘要",
        "",
        "| Provider | 序列数 | 总行数 | PASS | WARN | FAIL |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for table in report["tables"]:
        lines.append(
            "| {t} | {n} | {rows} | {p} | {w} | {f} |".format(
                t=table["provider"],
                n=table["series_count"],
                rows=table["total_rows"],
                p=table["series_pass"],
                w=table["series_warn"],
                f=table["series_fail"],
            )
        )
    lines.extend(
        [
            "",
            f"- 共 {verdict['series_total']} 条序列: "
            f"{verdict['series_pass']} PASS, "
            f"{verdict['series_warn']} WARN, "
            f"{verdict['series_fail']} FAIL",
            f"- 增量更新书签(empty_ranges): {verdict['empty_range_bookmarks']} 条",
            "",
        ]
    )
    all_audits = [a for t in report["tables"] for a in t["series"]]
    lines.extend(_md_coverage_table(all_audits))
    lines.extend(_md_check_summary(all_audits))
    lines.extend(_md_outlier_note(all_audits))
    lines.extend(_md_mix_note(all_audits))
    lines.extend(_md_source_notes(report.get("source_notes", [])))
    lines.extend(
        [
            "## 2. 逐表判定与详情",
            "",
            "每表先给判定分组(通过/警告/失败清单),再展开**非通过**序列的详情。",
            "判定唯一的表折叠为 `全部`;通过序列不逐条展开,见分组清单。",
            "",
        ]
    )
    for table in report["tables"]:
        lines.extend(_md_verdict_grouping(table))
        issues = [a for a in table["series"] if a.status != "PASS"]
        if issues:
            lines.append("非通过序列详情:")
            lines.append("")
            for audit in issues:
                lines.extend(_md_series_section(audit))

    lines.extend(
        [
            "## 3. 增量更新审计",
            "",
            "缓存通过 `empty_ranges` 表持久化“已验证为空”的时间区间,"
            "后续拉取相同区间时跳过源请求,实现增量更新。",
            "",
        ]
    )
    if report["empty_ranges"]:
        lines.append("| Provider 表 | 标的 | 频率 | 起始 | 结束 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in report["empty_ranges"]:
            lines.append(
                "| {t} | {s} | {f} | {a} | {b} |".format(
                    t=r["data_type"], s=r["symbol"], f=r["frequency"],
                    a=r["start_ts"], b=r["end_ts"],
                )
            )
    else:
        lines.append("缓存中暂无已验证为空的区间记录。")

    lines.extend(
        [
            "",
            "## 4. 结论",
            "",
            "| 指标 | 数值 |",
            "| --- | ---: |",
            f"| 序列总数 | {verdict['series_total']} |",
            f"| PASS | {verdict['series_pass']} |",
            f"| WARN | {verdict['series_warn']} |",
            f"| FAIL | {verdict['series_fail']} |",
            "",
        ]
    )
    if verdict["failed_series"]:
        lines.append("**未通过序列**:")
        for f in verdict["failed_series"]:
            lines.append(
                f"- {f['provider']} · {f['symbol']} · {f['frequency']}"
            )
        lines.append("")
    lines.append(
        "本报告由 `superplatform validate-report` 生成,可从干净环境一键复现。"
    )
    lines.append("")
    return "\n".join(lines)


@dataclass
class ReportArtifacts:
    """生成的报告产物路径。"""

    markdown_path: Path
    json_path: Path
    verdict: str


def generate_validation_report(
    cache_path: str | Path = "data/cache.duckdb",
    output: str | Path = "reports/data_validation_report.md",
    *,
    data_types: list[str] | None = None,
    outlier_method: str = "mad",
    outlier_threshold: float = 15.0,
    max_missing_pct: float = 10.0,
) -> ReportArtifacts:
    """生成 G1 数据校验报告。

    Args:
        cache_path: DuckDB 缓存路径。
        output: Markdown 报告输出路径(同名 .json 留痕并排生成)。
        data_types: 只审计这些 provider data_type(kline/funding_rate/
            open_interest/basis);默认全部。
        outlier_method: 异常检测方法(mad / zscore)。
        outlier_threshold: 异常阈值。
        max_missing_pct: 缺失占比告警阈值(百分数,默认 10,对应 config
            data.validation.max_missing_pct);超过的序列标 WARN。

    Returns:
        ReportArtifacts(markdown_path, json_path, verdict)。

    Raises:
        FileNotFoundError: 缓存文件不存在。
        RuntimeError: 缓存被其他进程占用或无法只读打开。
    """
    cache_path = Path(cache_path)
    if not cache_path.exists():
        raise FileNotFoundError(f"缓存文件不存在: {cache_path}")

    wanted = set(data_types) if data_types else None
    unknown = [t for t in (wanted or []) if t not in _SCHEMA_BY_DATA_TYPE]
    if unknown:
        raise ValueError(f"未知 data_type: {unknown}")

    try:
        con = duckdb.connect(str(cache_path), read_only=True)
        # 与 Store 构造一致: TIMESTAMPTZ 按 UTC 解释,否则 pandas 会
        # 按连接默认时区(如 Asia/Shanghai)读回,导致 UTC 检查误报。
        con.execute("SET TimeZone = 'UTC'")
    except duckdb.IOException as exc:
        raise RuntimeError(
            f"无法只读打开缓存 {cache_path}。它可能正被另一个进程占用"
            "(例如开发服务器)。请先停止占用该文件的进程,"
            "或改传其他缓存路径。\n原始错误: {exc}"
        ) from exc

    try:
        provider_rows = _provider_tables(con)
        if wanted is not None:
            provider_rows = [r for r in provider_rows if r["data_type"] in wanted]

        series_audits: list[SeriesAudit] = []
        table_summaries: list[dict[str, Any]] = []

        for row in provider_rows:
            table_name = row["table_name"]
            provider_id = row["provider_id"]
            data_type = row["data_type"]
            series_list = _list_series(con, table_name)
            audits: list[SeriesAudit] = []
            for symbol, frequency in series_list:
                df = con.execute(
                    f"SELECT * FROM {table_name} "
                    "WHERE symbol = ? AND frequency = ? "
                    "ORDER BY timestamp",
                    [symbol, frequency],
                ).fetchdf()
                audits.append(
                    audit_series(
                        data_type, df, frequency, provider=provider_id,
                        outlier_method=outlier_method,
                        outlier_threshold=outlier_threshold,
                        max_missing_pct=max_missing_pct,
                    )
                )
            series_audits.extend(audits)
            table_summaries.append({
                "provider": provider_id,
                "data_type": data_type,
                "table_name": table_name,
                "series_count": len(audits),
                "total_rows": _table_row_count(con, table_name),
                "series_pass": sum(1 for a in audits if a.status == "PASS"),
                "series_warn": sum(1 for a in audits if a.status == "WARN"),
                "series_fail": sum(1 for a in audits if a.status == "FAIL"),
                "series": audits,
            })

        empty_ranges = _audit_empty_ranges(con)
    finally:
        con.close()

    verdict = _verdict(series_audits, empty_ranges)

    report: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "cache_path": str(cache_path),
            "outlier_method": outlier_method,
            "outlier_threshold": outlier_threshold,
            "max_missing_pct": max_missing_pct,
        },
        "verdict": verdict,
        "checks": _build_checks(series_audits),
        "source_notes": _build_source_notes(series_audits),
        "tables": table_summaries,
        "empty_ranges": empty_ranges,
    }

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    markdown = _render_markdown(report)
    output_path.write_text(markdown, encoding="utf-8")

    json_path = output_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )

    return ReportArtifacts(
        markdown_path=output_path,
        json_path=json_path,
        verdict=verdict["verdict"],
    )
