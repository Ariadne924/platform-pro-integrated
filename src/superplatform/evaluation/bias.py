"""偏差控制六查 + 批次报告（04 阶段）。

算法移植自 sim_platform `app/bias_checkers.py` 与 `app/bias_control.py`，
数据面接 exchangia 分层内核（DataProvider.fetch → 01 的 DuckDB 缓存），
因子面接 02 的双文件注册中心（DualFactorRegistry）与 decorator/config 通道。

与源项目的对应/差异（语义一致处不逐条列）：

* 源项目检查器从 `factor_value` 在线缓存表读因子历史值；本平台没有落库的
  因子值历史（03 的离线评估在内存中计算），因此所有检查统一走
  「按注册实现 + K 线重算」路径（即源项目落库为空时的重算回退口径），
  recomputed 标志恒为 True，如实标注；
* 源项目的频率只有 1m/1d 两档，这里泛化为按因子 MD 声明频率取 bar 宽
  （1m/5m/15m/30m/1h/4h/8h/1d/1w）；源配置里 `*_1m_*` 的键在这里叫
  `*_intraday_*`（语义 = 非 1d 因子的 bar 粒度参数）；
* 取数从 `store.range_klines` 换成 provider 拉取（KlineFetcher，
  同步包装异步 fetch，进程内单事件循环）；横截面帧（cross）未移植——
  双文件协议无横截面声明、config 多标的因子（required_symbols>1）
  在记录加载时标 cross_sectional=True，逐标的历史重算对它不成立，
  各检查如实 BLOCKED，不给假数字；
* 偏差判定语义按任务书拍的板：lookahead/full_sample/overfit 的 PASS 是
  真判定；multiple_testing/cost/out_of_sample 的 PASS 仅表示「可计算」，
  显著性以 `significant_after_correction` 为准（payload 带 note）。

样本外一次性锁定：配置了 out_of_sample_window 后，locked_oos 每个因子
只允许成功执行一次（eval_oos_lock 表持久化），重复执行拒绝。
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
import tokenize
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from superplatform.data.schema import DataFrequency
from superplatform.runtime.providers import default_provider_for

logger = logging.getLogger("superplatform.evaluation.bias")

PASS = "PASS"
FAIL = "FAIL"
NOT_CHECKED = "NOT_CHECKED"
RUNNING = "RUNNING"
BLOCKED = "BLOCKED"
ERROR = "ERROR"
LOCKED = "LOCKED"

#: 开发集五查（样本外 out_of_sample 单列，合称六查）
CHECK_KEYS = ("lookahead", "full_sample", "multiple_testing", "overfit", "cost")

#: full_sample 静态扫描前剔除的 token 类型（注释与字符串字面量）：
#:  docstring/注释里的 “fit(”“bfill” 等字样不代表真实调用，扫原文会误杀；
#:  tokenize 失败回退原文（宁误杀不漏杀）。
_SCAN_STRIP_TOKEN_TYPES = {tokenize.COMMENT, tokenize.STRING}
if hasattr(tokenize, "FSTRING_MIDDLE"):
    _SCAN_STRIP_TOKEN_TYPES.add(tokenize.FSTRING_MIDDLE)

#: 因子频率 → 单根 bar 宽（bar 时间戳为开盘时间，边界比较用）
FREQ_DELTAS: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "8h": pd.Timedelta(hours=8),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(days=7),
}

#: 评级/检查不支持的输入数据类型（无本地连续时序，无法逐标的重算评测）
UNSUPPORTED_INPUTS = {"funding_rate", "open_interest", "mark_price"}

#: funding / OI 辅助数据的原生拉取频率（01 回填口径）
_AUX_FREQUENCY = {"funding_rate": DataFrequency.H8, "open_interest": DataFrequency.D1}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    """递归把 numpy/pandas 值转成 JSON 可序列化的 Python 原生值。"""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return _number(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").isoformat().replace("+00:00", "Z")
    if value is None:
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _scanable_source(source: str) -> str:
    """剔除注释与字符串字面量后的待扫描源码（tokenize 失败回退原文）。"""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        kept = [
            (tok.type, tok.string)
            for tok in tokens
            if tok.type not in _SCAN_STRIP_TOKEN_TYPES
        ]
        return tokenize.untokenize(kept)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return source


def bar_delta(frequency: str, bars: int) -> pd.Timedelta:
    """按因子频率给 bars 根 bar 的时长；未知频率按 1d（与历史默认一致）。"""
    return FREQ_DELTAS.get(str(frequency or "1d"), pd.Timedelta(days=1)) * bars


def is_daily(frequency: str) -> bool:
    return str(frequency or "1d") in ("1d", "1w")


# ----------------------------------------------------------------------
# 因子记录适配：双文件通道（02）+ decorator/config 通道 → 统一评测记录
# ----------------------------------------------------------------------


@dataclass
class EvalFactorRecord:
    """偏差检查/指标/评级共用的因子评测记录。

    compute_fn 遵循移植口径：``compute_fn(data, params) -> pd.Series``，
    data 含 ``kline``（DataFrame，UTC DatetimeIndex，保留 timestamp 列）、
    ``symbol``，按需含 ``funding`` / ``oi``（同索引对齐的 DataFrame）。
    """

    factor_id: str
    name: str
    category: str = ""
    status: str = "active"
    frequency: str = "1d"
    lookback_bars: int = 20
    inputs: list[str] = field(default_factory=list)      # MD 原始 inputs（列级）
    data_types: list[str] = field(default_factory=list)  # 归一后的数据类型
    params: dict[str, Any] = field(default_factory=dict)
    impl_path: Optional[str] = None
    source: str = "dual"                                 # dual / config
    cross_sectional: bool = False                        # 需多标的同调用输入
    compute_fn: Optional[Callable[[dict, dict], pd.Series]] = None


def _result_to_series(result: Any, kline: pd.DataFrame) -> pd.Series:
    """把冻结接口的返回值（FactorResult/DataFrame/Series）归一成 UTC 时序。"""
    from superplatform.factors.base import FactorResult

    if isinstance(result, FactorResult):
        result = result.values
    if isinstance(result, pd.Series):
        series = result
    elif isinstance(result, pd.DataFrame):
        if {"timestamp", "value"}.issubset(result.columns):
            series = pd.Series(
                pd.to_numeric(result["value"], errors="coerce").to_numpy(dtype="float64"),
                index=pd.to_datetime(result["timestamp"], utc=True),
            )
        elif len(result.columns) == 1:
            series = pd.to_numeric(result.iloc[:, 0], errors="coerce")
        else:
            raise ValueError(f"因子返回 DataFrame 无法归一（列: {list(result.columns)}）")
    else:
        series = pd.Series(result, index=kline.index)
    if not isinstance(series.index, pd.DatetimeIndex):
        if len(series) == len(kline):
            series = pd.Series(series.to_numpy(dtype="float64"), index=kline.index)
        else:
            raise ValueError("因子返回序列无时间索引且长度与 K 线不一致")
    if series.index.tz is None:
        series.index = series.index.tz_localize("UTC")
    return pd.to_numeric(series, errors="coerce").groupby(level=0).last().sort_index()


def _dual_record(factor_id: str) -> Optional[EvalFactorRecord]:
    """从 02 的双文件注册中心构造评测记录（未注册返回 None）。"""
    from superplatform.factors.dual_registry import _INPUT_DATA_TYPE, DualFactorRegistry

    dual = DualFactorRegistry.get_instance()
    dual.ensure_scanned()
    rec = dual.get_record(factor_id)
    if rec is None:
        return None
    base_params = dict(rec.params)
    raw_fn = rec.compute_fn

    def compute_fn(data: dict, params: dict) -> pd.Series:
        symbol = data["symbol"]
        payload: dict[str, dict[str, pd.DataFrame]] = {"kline": {symbol: data["kline"]}}
        if "funding" in data:
            payload["funding_rate"] = {symbol: data["funding"]}
        if "oi" in data:
            payload["open_interest"] = {symbol: data["oi"]}
        merged = {**base_params, **(params or {})}
        return _result_to_series(raw_fn(payload, **merged), data["kline"])

    return EvalFactorRecord(
        factor_id=rec.factor_id,
        name=rec.name,
        category=rec.category,
        status=rec.status,
        frequency=rec.frequency or "1d",
        lookback_bars=int(rec.lookback_bars or 20),
        inputs=list(rec.inputs or []),
        data_types=sorted({_INPUT_DATA_TYPE.get(x, "kline") for x in (rec.inputs or [])}),
        params=base_params,
        impl_path=str(rec.impl_path),
        source="dual",
        cross_sectional=False,
        compute_fn=compute_fn,
    )


def _config_record(factor_id: str, config: Any) -> Optional[EvalFactorRecord]:
    """从 decorator/config 通道构造评测记录（无 config 条目返回 None）。

    config 因子没有实现文件路径（decorator 注册），full_sample 静态扫描
    对它如实 BLOCKED；多标的因子（required_symbols>1）标 cross_sectional。
    """
    from superplatform.factors.instance_registry import FactorInstanceRegistry
    from superplatform.factors.registry import FactorRegistry
    from superplatform.factors.resolve import factor_entry, resolve_factor

    entry = factor_entry(config, factor_id)
    if not entry:
        return None
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    FactorInstanceRegistry.get_instance().build_from_config(config, registry)
    try:
        factor = resolve_factor(factor_id, factory_registry=registry)
    except KeyError:
        return None
    params = dict(entry.get("params") or {})
    required = list(getattr(factor, "required_data", None) or ["kline"])
    required_symbols = getattr(factor, "required_symbols", None)
    lookback = 20
    for key in ("window", "lookback", "period", "n"):
        value = params.get(key)
        if isinstance(value, (int, float)) and value > 0:
            lookback = int(value) + 1
            break

    def compute_fn(data: dict, params_override: dict) -> pd.Series:
        symbol = data["symbol"]
        payload: dict[str, dict[str, pd.DataFrame]] = {"kline": {symbol: data["kline"]}}
        if "funding" in data:
            payload["funding_rate"] = {symbol: data["funding"]}
        if "oi" in data:
            payload["open_interest"] = {symbol: data["oi"]}
        merged = {**params, **(params_override or {})}
        return _result_to_series(factor.compute(payload, **merged), data["kline"])

    return EvalFactorRecord(
        factor_id=factor_id,
        name=factor_id,
        category=str(getattr(factor, "category", "")),
        status="active",
        frequency=str(entry.get("frequency", "1d")),
        lookback_bars=lookback,
        inputs=list(required),
        data_types=list(required),
        params=params,
        impl_path=None,
        source="config",
        cross_sectional=bool(required_symbols and required_symbols > 1),
        compute_fn=compute_fn,
    )


def load_factor_record(factor_id: str, config: Any) -> Optional[EvalFactorRecord]:
    """加载评测记录：双文件通道优先，回退 decorator/config 通道。"""
    rec = _dual_record(factor_id)
    if rec is not None:
        return rec
    return _config_record(factor_id, config)


def list_factor_records(config: Any) -> list[EvalFactorRecord]:
    """全量可评测因子记录：双文件在册因子 + 有 config 条目的因子/实例。"""
    from superplatform.factors.dual_registry import DualFactorRegistry

    records: dict[str, EvalFactorRecord] = {}
    dual = DualFactorRegistry.get_instance()
    dual.ensure_scanned()
    for row in dual.list_factors():
        if row.get("registered") and row.get("factor_id"):
            rec = _dual_record(row["factor_id"])
            if rec is not None:
                records[rec.factor_id] = rec
    for section in ("factors", "factor_instances"):
        for name in (config.get(section) or {}):
            if name in records:
                continue
            rec = _config_record(str(name), config)
            if rec is not None:
                records[rec.factor_id] = rec
    return [records[key] for key in sorted(records)]


def factor_direction(config: Any, rec: EvalFactorRecord) -> tuple[str, bool]:
    """因子方向语义：解析双文件 MD 的 output.direction 文本。

    返回 (方向原文, bullish_high)。识别不到时默认 True（值大看多）——
    评级一律用 |Sharpe|，方向只影响信号回测的持仓符号，不影响评级结论。
    """
    text = ""
    try:
        if rec.source == "dual":
            from superplatform.factors import protocol
            from superplatform.factors.dual_registry import DualFactorRegistry

            dual_rec = DualFactorRegistry.get_instance().get_record(rec.factor_id)
            if dual_rec is not None:
                meta = protocol.parse_md(dual_rec.md_path).meta or {}
                output = meta.get("output") or {}
                text = str(output.get("direction", "") or "")
    except Exception:  # noqa: BLE001 - 方向文本解析失败不致命
        text = ""
    lowered = text.lower()
    bearish = ("看空" in text and "越大越看多" not in text) or "bearish" in lowered
    bullish = ("看多" in text) or "bullish" in lowered
    bullish_high = True
    if bearish and not bullish:
        bullish_high = False
    return text, bullish_high


# ----------------------------------------------------------------------
# 数据取数：provider → 同步 K 线/辅助数据帧（进程内单事件循环）
# ----------------------------------------------------------------------


class KlineFetcher:
    """把异步 DataProvider.fetch 包装成同步取数，供纯同步的检查器使用。

    内部持有一个专用事件循环（lazy 创建）。非线程安全：评估类调用方
    （CLI / 05 的 asyncio.to_thread 工作线程）应串行使用——DuckDB 缓存
    本来就是单写者。
    """

    def __init__(self, providers: Any, config: Any, store: Any = None) -> None:
        self.providers = providers
        self.config = config
        self.store = store  # 01 的 Store（可选，用于 latest_kline_ts 快速查询）
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _run(self, coro: Any) -> Any:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        if self._loop is not None:
            self._loop.close()
            self._loop = None

    def _provider(self, rec_name: str, data_type: str) -> Any:
        return default_provider_for(
            rec_name, data_type, config=self.config, registry=self.providers,
        )

    def fetch_frame(
        self,
        symbol: str,
        frequency: str,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
        data_type: str = "kline",
    ) -> pd.DataFrame:
        """拉取一个 (symbol, frequency) 的数据帧，UTC DatetimeIndex。

        拉取失败（无效标的/源端无数据/网络异常）返回空帧——与 03 pipeline
        的 _safe_fetch 同语义：单标的失败不拖垮整批。
        """
        try:
            provider = self._provider("evaluation", data_type)
            freq = DataFrequency(frequency)
            df = self._run(provider.fetch(symbol, freq, start, end))
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            logger.warning(
                "fetch failed for %s %s %s: %s: %s",
                data_type, symbol, frequency, type(exc).__name__, exc,
            )
            return pd.DataFrame()
        if df is None or df.empty or "timestamp" not in df.columns:
            return pd.DataFrame()
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        df = df.set_index("timestamp", drop=False)
        return df

    def latest_kline_ts(self, symbol: str, frequency: str = "1d") -> Optional[pd.Timestamp]:
        """缓存库内该标的最新 K 线时间戳（无缓存/查询失败返回 None）。"""
        if self.store is None:
            return None
        try:
            from superplatform.data.store import provider_table

            provider = self._provider("evaluation", "kline")
            info = self.store.series_range(provider_table(provider.provider_id), symbol, frequency)
            ts = info.get("max_ts")
            if ts is None:
                return None
            ts = pd.Timestamp(ts)
            return ts if ts.tzinfo is not None else ts.tz_localize("UTC")
        except Exception:  # noqa: BLE001 - 兜底窗口用，失败回退 None
            return None


# ----------------------------------------------------------------------
# 评估结果缓存（DuckDB，按 (factor_id, 数据版本) 键控）
# ----------------------------------------------------------------------


class EvalCacheStore:
    """评估/评级/偏差检查结果的 DuckDB 缓存。

    与 01 的数据缓存同库（默认 data/cache.duckdb）但表独立；自持连接
    （同进程多连接是 DuckDB 支持用法，跨进程并发仍受单写者锁约束——
    评估类进程不要与 live 并发）。
    """

    def __init__(self, path: str | Path = "data/cache.duckdb") -> None:
        import duckdb

        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self._path))
        self._conn.execute("SET TimeZone = 'UTC'")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_metrics_cache (
                factor_id   VARCHAR NOT NULL,
                cache_key   VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                computed_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (factor_id, cache_key)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_rating_cache (
                factor_id   VARCHAR NOT NULL,
                cache_key   VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                computed_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (factor_id, cache_key)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_corr_cache (
                cache_key   VARCHAR PRIMARY KEY,
                payload_json VARCHAR NOT NULL,
                computed_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_bias_runs (
                run_id      VARCHAR PRIMARY KEY,
                scope       VARCHAR NOT NULL,
                status      VARCHAR NOT NULL,
                factor_ids_json VARCHAR NOT NULL,
                started_at  TIMESTAMPTZ,
                finished_at TIMESTAMPTZ
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_bias_results (
                run_id      VARCHAR NOT NULL,
                factor_id   VARCHAR NOT NULL,
                overall_status VARCHAR NOT NULL,
                checks_json VARCHAR NOT NULL,
                oos_json    VARCHAR NOT NULL,
                failure_reason VARCHAR,
                evidence_path VARCHAR,
                checked_at  TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (run_id, factor_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_oos_lock (
                factor_id   VARCHAR PRIMARY KEY,
                run_id      VARCHAR NOT NULL,
                ran_at      TIMESTAMPTZ NOT NULL
            )
            """
        )

    # -- 通用键值缓存读写 ------------------------------------------------
    def read_payload(self, table: str, factor_id: str, cache_key: str) -> Optional[str]:
        rows = self._conn.execute(
            f"SELECT payload_json FROM {table} WHERE factor_id = ? AND cache_key = ?",
            [factor_id, cache_key],
        ).fetchdf()
        if rows.empty:
            return None
        return str(rows.iloc[0]["payload_json"])

    def write_payload(self, table: str, factor_id: str, cache_key: str, payload_json: str) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO {table} (factor_id, cache_key, payload_json, computed_at) "
            "VALUES (?, ?, ?, ?)",
            [factor_id, cache_key, payload_json, datetime.now(timezone.utc)],
        )

    def read_corr(self, cache_key: str) -> Optional[str]:
        rows = self._conn.execute(
            "SELECT payload_json FROM eval_corr_cache WHERE cache_key = ?", [cache_key],
        ).fetchdf()
        return None if rows.empty else str(rows.iloc[0]["payload_json"])

    def write_corr(self, cache_key: str, payload_json: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO eval_corr_cache (cache_key, payload_json, computed_at) "
            "VALUES (?, ?, ?)",
            [cache_key, payload_json, datetime.now(timezone.utc)],
        )

    def all_cached(self, table: str) -> list[dict[str, str]]:
        rows = self._conn.execute(
            f"SELECT factor_id, cache_key, payload_json FROM {table}"
        ).fetchdf()
        return rows.to_dict("records") if not rows.empty else []

    # -- 数据版本 --------------------------------------------------------
    def series_bounds(self, table: str, frequency: str) -> list[dict[str, Any]]:
        """provider 缓存表内某频率逐 symbol 的覆盖范围（表不存在返回 []）。"""
        exists = self._conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = ? AND table_schema = 'main'",
            [table],
        ).fetchdf()
        if exists.empty:
            return []
        rows = self._conn.execute(
            f"SELECT symbol, CAST(min(timestamp) AS DATE) AS s, "
            f"CAST(max(timestamp) AS DATE) AS e, count(*) AS c "
            f"FROM {table} WHERE frequency = ? GROUP BY symbol ORDER BY symbol",
            [frequency],
        ).fetchdf()
        return rows.to_dict("records") if not rows.empty else []

    # -- 偏差检查批次 ----------------------------------------------------
    def save_run(self, run: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO eval_bias_runs "
            "(run_id, scope, status, factor_ids_json, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                run["run_id"], run["scope"], run["status"], run["factor_ids_json"],
                run.get("started_at"), run.get("finished_at"),
            ],
        )

    def save_result(self, run_id: str, result: dict[str, Any]) -> None:
        import json as _json

        self._conn.execute(
            "INSERT OR REPLACE INTO eval_bias_results "
            "(run_id, factor_id, overall_status, checks_json, oos_json, "
            " failure_reason, evidence_path, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                result["factor_id"],
                result.get("overall_status", NOT_CHECKED),
                _json.dumps(_json_safe(result.get("checks") or {}), ensure_ascii=False),
                _json.dumps(_json_safe(result.get("oos") or {}), ensure_ascii=False),
                result.get("failure_reason"),
                result.get("evidence_path"),
                datetime.now(timezone.utc),
            ],
        )

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM eval_bias_runs WHERE run_id = ?", [run_id],
        ).fetchdf()
        if rows.empty:
            return None
        return rows.iloc[0].to_dict()

    def run_results(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM eval_bias_results WHERE run_id = ? ORDER BY factor_id",
            [run_id],
        ).fetchdf()
        return rows.to_dict("records") if not rows.empty else []

    def latest_run_id(self) -> Optional[str]:
        rows = self._conn.execute(
            "SELECT run_id FROM eval_bias_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchdf()
        return None if rows.empty else str(rows.iloc[0]["run_id"])

    # -- 样本外一次性锁 ----------------------------------------------------
    def oos_locked(self, factor_id: str) -> bool:
        rows = self._conn.execute(
            "SELECT 1 FROM eval_oos_lock WHERE factor_id = ?", [factor_id],
        ).fetchdf()
        return not rows.empty

    def oos_lock(self, factor_id: str, run_id: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO eval_oos_lock (factor_id, run_id, ran_at) VALUES (?, ?, ?)",
            [factor_id, run_id, datetime.now(timezone.utc)],
        )

    def close(self) -> None:
        self._conn.close()


# ----------------------------------------------------------------------
# 偏差检查器（移植 BiasCheckRunner）
# ----------------------------------------------------------------------


class BiasCheckRunner:
    """对单个或一组因子执行偏差检查（只读数据，不写库）。

    帧/收盘价缓存按 (symbol, frequency, 列集) 记覆盖区间，重叠窗口内存
    切片，跨因子复用；因子序列缓存是因子级的，逐因子 reset_series_cache()。
    """

    def __init__(
        self,
        fetcher: KlineFetcher,
        config: Any,
        symbols: Optional[list[str]] = None,
    ) -> None:
        self.fetcher = fetcher
        self.config = config
        cfg_get = getattr(config, "get", None)
        self.settings = dict(cfg_get("bias_control", {}) or {}) if callable(cfg_get) else {}
        self.symbols = list(symbols) if symbols else list(
            cfg_get("data.symbols.perpetual", []) or [] if callable(cfg_get) else []
        )
        self._close_cache: dict[tuple[Any, ...], pd.Series] = {}
        self._frame_cache: dict[tuple[Any, ...], tuple[Any, Any, pd.DataFrame]] = {}
        self._series_cache: dict[tuple[Any, ...], tuple[Any, Any, pd.Series, bool]] = {}

    # ------------------------------------------------------------------
    # 基础配置
    # ------------------------------------------------------------------
    def _setting(self, name: str, default: Any = None) -> Any:
        return self.settings.get(name, default)

    def _factor_symbols(self, factor_id: str) -> list[str]:
        """评测标的集：本平台无落库因子值，直接取配置的研究池/调用方覆盖。"""
        return list(self.symbols)

    @staticmethod
    def _parse_time(value: Any) -> Optional[pd.Timestamp]:
        if value is None or value == "":
            return None
        try:
            ts = pd.Timestamp(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(ts):
            return None
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts

    def _window(self, name: str) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
        value = self.settings.get(name)
        if value is None and name == "development_window":
            value = self.settings.get("dev_window")
        if value is None and name == "out_of_sample_window":
            value = self.settings.get("oos_window") or self.settings.get("locked_oos_window")
        if isinstance(value, dict):
            return (
                self._parse_time(value.get("start") or value.get("from") or value.get("begin")),
                self._parse_time(value.get("end") or value.get("to") or value.get("finish")),
            )
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return self._parse_time(value[0]), self._parse_time(value[1])
        return None, None

    @staticmethod
    def _window_payload(
        start: Optional[pd.Timestamp], end: Optional[pd.Timestamp]
    ) -> Optional[dict[str, str]]:
        if start is None or end is None:
            return None
        return {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        }

    def _bar_delta(self, rec: EvalFactorRecord, bars: int) -> pd.Timedelta:
        return bar_delta(getattr(rec, "frequency", "1d"), bars)

    # ------------------------------------------------------------------
    # K 线帧 / 收盘价 / 辅助数据（移植 _kline_frame/_close_series/_load_auxiliary）
    # ------------------------------------------------------------------
    def _kline_frame(
        self,
        symbol: str,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
        period: str,
        columns: Optional[tuple[str, ...]] = None,
    ) -> pd.DataFrame:
        """拉取 K 线帧并按 (symbol, period) 缓存覆盖区间，重叠窗口切片。

        列裁剪只影响内部收盘价路径的内存占用（provider 层总是全列返回），
        列集仍在缓存键内，与源项目同口径，防缺列命中。
        """
        key = (symbol, period, tuple(sorted(columns)) if columns else None)
        entry = self._frame_cache.get(key)
        req_start, req_end = start, end
        if entry is not None:
            c_start, c_end, frame = entry
            covered = (
                (c_start is None or (start is not None and c_start <= start))
                and (c_end is None or (end is not None and end <= c_end))
            )
            if covered:
                return self._slice_frame(frame, start, end)
            start = None if (c_start is None or start is None) else min(c_start, start)
            end = None if (c_end is None or end is None) else max(c_end, end)
        frame = self.fetcher.fetch_frame(symbol, period, start, end)
        self._frame_cache[key] = (start, end, frame)
        return self._slice_frame(frame, req_start, req_end)

    @staticmethod
    def _slice_frame(
        frame: pd.DataFrame,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
    ) -> pd.DataFrame:
        if frame.empty:
            return frame
        sliced = frame
        if start is not None:
            sliced = sliced[sliced.index >= start]
        if end is not None:
            sliced = sliced[sliced.index <= end]
        return sliced

    @staticmethod
    def _slice_series(
        series: pd.Series,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
    ) -> pd.Series:
        if series.empty:
            return series
        sliced = series
        if start is not None:
            sliced = sliced[sliced.index >= start]
        if end is not None:
            sliced = sliced[sliced.index <= end]
        return sliced

    def _close_series(
        self,
        symbol: str,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
        period: str,
    ) -> pd.Series:
        key = (symbol, str(start), str(end), period)
        cached = self._close_cache.get(key)
        if cached is None:
            klines = self._kline_frame(symbol, start, end, period, columns=("close",))
            if klines.empty or "close" not in klines.columns:
                cached = pd.Series(dtype=float)
            else:
                cached = pd.Series(
                    pd.to_numeric(klines["close"], errors="coerce").to_numpy(dtype="float64"),
                    index=pd.DatetimeIndex(klines.index),
                ).groupby(level=0).last().sort_index()
            self._close_cache[key] = cached
        return cached

    def _load_auxiliary(
        self,
        data_type: str,
        column: str,
        symbol: str,
        index: pd.Index,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """funding/OI 辅助序列：按原生频率拉取，reindex+ffill 到 K 线索引。"""
        freq = _AUX_FREQUENCY[data_type]
        df = self.fetcher.fetch_frame(symbol, freq.value, None, end, data_type=data_type)
        if df.empty or column not in df.columns:
            return pd.DataFrame({column: np.nan}, index=index)
        series = pd.Series(
            pd.to_numeric(df[column], errors="coerce").to_numpy(dtype="float64"),
            index=pd.DatetimeIndex(df.index),
        ).groupby(level=0).last().sort_index()
        target = pd.DatetimeIndex(index)
        if target.tz is None:
            series.index = series.index.tz_localize(None)
        combined = series.reindex(target.union(series.index)).ffill()
        return combined.reindex(target).to_frame(column)

    # ------------------------------------------------------------------
    # 因子值重算（源项目落库为空时的重算回退路径，本平台统一走这里）
    # ------------------------------------------------------------------
    def _recompute_prefix(
        self,
        rec: EvalFactorRecord,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.Series:
        period = rec.frequency if rec.frequency in FREQ_DELTAS else "1d"
        kline = self._kline_frame(symbol, start, end, period)
        if kline.empty:
            return pd.Series(dtype=float)
        if getattr(rec, "cross_sectional", False):
            raise ValueError("多标的/横截面因子不支持逐标的历史重算")
        if rec.compute_fn is None:
            raise ValueError("因子实现未加载，无法重算")
        data: dict[str, Any] = {"kline": kline, "symbol": symbol}
        data_types = set(getattr(rec, "data_types", []) or [])
        if "funding_rate" in data_types:
            data["funding"] = self._load_auxiliary(
                "funding_rate", "funding_rate", symbol, kline.index, end
            )
        if "open_interest" in data_types:
            data["oi"] = self._load_auxiliary(
                "open_interest", "open_interest", symbol, kline.index, end
            )
        return rec.compute_fn(data, dict(getattr(rec, "params", {}) or {}))

    def _fallback_window(
        self,
        symbol: str,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        """未配置窗口时的兜底：以库内最新 K 线为终点的最近 N 天。"""
        if end is None:
            end = self.fetcher.latest_kline_ts(symbol)
            if end is None:
                end = pd.Timestamp.now(tz="UTC")
        if start is None:
            days = int(self._setting("default_window_days", 365))
            start = end - pd.Timedelta(days=days)
        return start, end

    def _series_for_window(
        self,
        rec: EvalFactorRecord,
        symbol: str,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
        min_samples: int,
    ) -> tuple[pd.Series, pd.Series, bool]:
        """返回 (factor, close, recomputed=True)：统一按注册实现重算。"""
        period = rec.frequency if rec.frequency in FREQ_DELTAS else "1d"
        if start is None or end is None:
            start, end = self._fallback_window(symbol, start, end)
        warmup = max(int(getattr(rec, "lookback_bars", 20) or 20) * 3, 240)
        recompute_start = start - self._bar_delta(rec, warmup)
        recomputed = self._recompute_prefix(rec, symbol, recompute_start, end)
        if not recomputed.empty:
            recomputed = recomputed[recomputed.index >= start]
        close = self._close_series(symbol, start, end, period)
        return recomputed, close, True

    def _series_cached(
        self,
        rec: EvalFactorRecord,
        symbol: str,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
        min_samples: int,
    ) -> tuple[pd.Series, pd.Series, bool]:
        """_series_for_window 的因子级覆盖缓存包装（切片口径一致）。

        数值恒等依据：因果因子逐点只依赖历史，更长前缀（更足 warmup）
        算出的重叠区数值一致。lookahead() 不得走此缓存（它靠截断 vs
        扩展重算的差异检测非因果，走缓存会真空 PASS）。
        """
        period = rec.frequency if rec.frequency in FREQ_DELTAS else "1d"
        if start is None or end is None:
            start, end = self._fallback_window(symbol, start, end)
        req_start, req_end = start, end
        key = (str(rec.factor_id), symbol, period)
        entry = self._series_cache.get(key)
        if entry is not None:
            cov_start, cov_end, cached, cached_recomputed = entry
            if cov_start <= start and cov_end >= end:
                close = self._close_series(symbol, req_start, req_end, period)
                return self._slice_series(cached, start, end), close, cached_recomputed
            start, end = min(cov_start, start), max(cov_end, end)
        factor, _union_close, recomputed = self._series_for_window(
            rec, symbol, start, end, min_samples
        )
        if not factor.empty:
            self._series_cache[key] = (start, end, factor, recomputed)
        close = self._close_series(symbol, req_start, req_end, period)
        return self._slice_series(factor, req_start, req_end), close, recomputed

    def reset_series_cache(self) -> None:
        """只清因子序列缓存（因子级），保留帧/收盘价缓存跨因子复用。"""
        self._series_cache.clear()

    # ------------------------------------------------------------------
    # 前视偏差 / 全样本泄露
    # ------------------------------------------------------------------
    def _cutoffs(self, rec: EvalFactorRecord) -> list[pd.Timestamp]:
        configured = self.settings.get("lookahead_cutoffs") or []
        result = [ts for ts in (self._parse_time(value) for value in configured) if ts is not None]
        if result:
            return sorted(set(result))
        # 未配置截断点：按库内 K 线覆盖范围的 50%/70%/85% 取点
        starts: list[pd.Timestamp] = []
        ends: list[pd.Timestamp] = []
        for symbol in self.symbols:
            try:
                raw_end = self.fetcher.latest_kline_ts(symbol)
            except (AttributeError, TypeError, ValueError):
                continue
            if raw_end is not None:
                ends.append(raw_end)
        if not ends:
            return []
        end = max(ends)
        dev_start, dev_end = self._window("development_window")
        start = dev_start or (end - pd.Timedelta(days=int(self._setting("default_window_days", 365))))
        if start >= end:
            return []
        return [start + (end - start) * ratio for ratio in (0.50, 0.70, 0.85)]

    def lookahead(self, rec: EvalFactorRecord) -> dict[str, Any]:
        factor_id = str(rec.factor_id)
        cutoffs = self._cutoffs(rec)
        if not cutoffs:
            return {
                "status": BLOCKED,
                "checked_at": _now_iso(),
                "cutoffs": [],
                "compared_count": 0,
                "max_abs_diff": None,
                "tolerance": self._setting("lookahead_abs_tolerance", 1e-8),
                "failure_reason": "没有可用截断点（未配置且无法从数据覆盖推导）",
                "method": "截断未来数据后重算并与扩展重算值比较",
            }

        symbols = self._factor_symbols(factor_id)
        window_bars = int(
            self._setting("lookahead_window_bars", max(int(rec.lookback_bars or 20) * 3, 240))
        )
        min_samples = int(self._setting("lookahead_min_samples", 30))
        abs_tolerance = float(self._setting("lookahead_abs_tolerance", 1e-8))
        rel_tolerance = float(self._setting("lookahead_rel_tolerance", 1e-6))
        probe_bars = max(1, int(self._setting("lookahead_probe_bars", min(window_bars, 240))))
        cutoff_rows: list[dict[str, Any]] = []
        cutoff_scales: list[float] = []
        errors: list[str] = []
        any_fail = False
        total_compared = 0
        global_max_diff: Optional[float] = None

        for cutoff in cutoffs:
            start = cutoff - self._bar_delta(rec, window_bars)
            compared = 0
            max_diff: Optional[float] = None
            cutoff_error: Optional[str] = None
            boundary_change = False
            magnitude_samples: list[pd.Series] = []
            for symbol in symbols:
                try:
                    truncated = self._recompute_prefix(rec, symbol, start, cutoff)
                    expanded = self._recompute_prefix(
                        rec, symbol, start, cutoff + self._bar_delta(rec, probe_bars)
                    )
                    if truncated.empty and expanded.empty:
                        continue
                    joined = pd.concat(
                        {"truncated": truncated, "expanded": expanded}, axis=1, sort=False
                    )
                    # 只比较在截断视图中已完结的 bar：截断点落在某根 bar 内部时，
                    # 未扩展序列里该 bar 是未聚合完的 partial bar，与扩展序列的
                    # 完整 bar 比较会产生边界伪差。
                    joined = joined[joined.index + self._bar_delta(rec, 1) <= cutoff]
                    both_missing = joined["truncated"].isna() & joined["expanded"].isna()
                    joined = joined[~both_missing]
                    if joined.empty:
                        continue
                    one_missing = joined["truncated"].isna() ^ joined["expanded"].isna()
                    if bool(one_missing.any()):
                        boundary_change = True
                    comparable = joined[~one_missing]
                    if not comparable.empty:
                        diff = (comparable["truncated"] - comparable["expanded"]).abs()
                        current_max = _number(diff.max())
                        if current_max is not None:
                            max_diff = current_max if max_diff is None else max(max_diff, current_max)
                        magnitude_samples.append(comparable["truncated"].abs())
                        magnitude_samples.append(comparable["expanded"].abs())
                    compared += len(joined)
                except Exception as error:
                    cutoff_error = f"{symbol}: {type(error).__name__}: {error}"
                    errors.append(f"{cutoff.isoformat()} {cutoff_error}")

            # 相对容差按被比较序列的实际量级（逐点 |值| 的中位数）缩放。
            scale = 0.0
            if magnitude_samples:
                median_magnitude = _number(pd.concat(magnitude_samples).median())
                if median_magnitude is not None:
                    scale = median_magnitude
            cutoff_scales.append(scale)
            if max_diff is not None:
                tolerance = abs_tolerance + rel_tolerance * scale
            else:
                tolerance = abs_tolerance
            passed = (
                cutoff_error is None
                and compared >= min_samples
                and (max_diff is not None and max_diff <= tolerance)
            )
            if max_diff is not None:
                global_max_diff = max_diff if global_max_diff is None else max(global_max_diff, max_diff)
            total_compared += compared
            any_fail = any_fail or boundary_change or (max_diff is not None and max_diff > tolerance)
            cutoff_rows.append({
                "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
                "compared_count": compared,
                "max_abs_diff": max_diff,
                "tolerance": tolerance,
                "status": (
                    FAIL
                    if boundary_change or (max_diff is not None and max_diff > tolerance)
                    else PASS if passed else BLOCKED
                ),
                "failure_reason": cutoff_error or (
                    "截断后新增未来数据导致历史值出现或消失" if boundary_change
                    else "样本数不足" if compared < min_samples else None
                ),
            })

        if any_fail:
            status = FAIL
            reason = "截断后历史因子值发生超过容差的变化"
        elif errors or total_compared < min_samples * len(cutoffs):
            status = BLOCKED
            reason = "截断重算存在异常或有效比较样本不足"
        else:
            status = PASS
            reason = None
        representative_scale = float(np.median(cutoff_scales)) if cutoff_scales else 0.0
        return {
            "status": status,
            "checked_at": _now_iso(),
            "cutoffs": cutoff_rows,
            "compared_count": total_compared,
            "max_abs_diff": global_max_diff,
            "tolerance": abs_tolerance + rel_tolerance * representative_scale,
            "failure_reason": reason,
            "method": "截断未来数据后重算，并与同一截断点之前的扩展重算值逐点比较",
        }

    def full_sample(self, rec: EvalFactorRecord, lookahead: dict[str, Any]) -> dict[str, Any]:
        if not rec.impl_path:
            return {
                "status": BLOCKED if lookahead.get("status") != FAIL else FAIL,
                "uses_full_sample_standardization": None,
                "uses_future_fill": None,
                "uses_future_fields": None,
                "historical_values_changed": lookahead.get("status") == FAIL,
                "description": "实现源码不可定位（decorator/config 通道因子），静态扫描不可用",
                "evidence_path": "",
                "failure_reason": None if lookahead.get("status") == FAIL else "无法读取实现源码：无 impl 路径",
            }
        try:
            source = Path(rec.impl_path).read_text(encoding="utf-8")
        except Exception as error:
            return {
                "status": BLOCKED,
                "uses_full_sample_standardization": None,
                "uses_future_fill": None,
                "uses_future_fields": None,
                "historical_values_changed": None,
                "description": f"无法读取实现源码：{error}",
                "evidence_path": str(rec.impl_path),
                "failure_reason": f"无法读取实现源码：{error}",
            }

        patterns = {
            "uses_full_sample_standardization": (
                r"StandardScaler|MinMaxScaler|RobustScaler|fit_transform|\.fit\s*\("
            ),
            "uses_future_fill": r"\bbfill\b|backfill|fillna\s*\([^\n]*method\s*=\s*['\"]bfill",
            "uses_future_fields": r"\b(future|lead|forward_return|fwd_return)\b|\.shift\s*\(\s*-",
        }
        scan_source = _scanable_source(source)
        violations = {
            key: bool(re.search(pattern, scan_source, re.IGNORECASE))
            for key, pattern in patterns.items()
        }
        if any(violations.values()) or lookahead.get("status") == FAIL:
            status = FAIL
            reason = "源码出现未来填充/未来字段/全样本拟合模式，或截断重算发现历史值变化"
        elif lookahead.get("status") != PASS:
            status = BLOCKED
            reason = "前缀重算未完成，无法确认历史值未受未来数据影响"
        else:
            status = PASS
            reason = None
        return {
            "status": status,
            "uses_full_sample_standardization": violations["uses_full_sample_standardization"],
            "uses_future_fill": violations["uses_future_fill"],
            "uses_future_fields": violations["uses_future_fields"],
            "historical_values_changed": lookahead.get("status") == FAIL,
            "description": "实现源码静态扫描 + 前视截断重算一致性检查",
            "evidence_path": str(rec.impl_path),
            "violations": [key for key, value in violations.items() if value],
            "failure_reason": reason,
        }

    # ------------------------------------------------------------------
    # 多重检验
    # ------------------------------------------------------------------
    def _p_value(self, rec: EvalFactorRecord) -> tuple[Optional[float], int]:
        horizon = int(self._setting("multiple_testing_horizon", 24))
        start, end = self._window("development_window")
        # 与 factor_metrics 同一口径：非 1d 因子评估窗口封顶最近 N 根 bar
        if not is_daily(getattr(rec, "frequency", "1d")) and start is not None and end is not None:
            max_bars = int(self._setting("multiple_testing_intraday_max_bars", 250000))
            cap_start = end - bar_delta(rec.frequency, max_bars)
            if cap_start > start:
                start = cap_start
        values: list[float] = []
        samples = 0
        for symbol in self._factor_symbols(str(rec.factor_id)):
            factor, close, _ = self._series_cached(
                rec,
                symbol,
                start=start,
                end=end,
                min_samples=int(self._setting("multiple_testing_min_samples", 50)),
            )
            if factor.empty or close.empty:
                continue
            frame = pd.concat({"factor": factor, "close": close}, axis=1, sort=False).dropna()
            if len(frame) <= horizon + 5:
                continue
            fwd = frame["close"].shift(-horizon) / frame["close"] - 1.0
            pair = pd.concat(
                {"factor": frame["factor"], "fwd": fwd}, axis=1, sort=False
            ).dropna()
            if len(pair) < int(self._setting("multiple_testing_min_samples", 50)):
                continue
            # 常数序列（零方差）除零发 RuntimeWarning，NaN 由 isfinite 丢弃，
            # 只压警告不改数值。
            with np.errstate(invalid="ignore", divide="ignore"):
                corr = pair["factor"].rank().corr(pair["fwd"].rank())
            if corr is None or not math.isfinite(float(corr)):
                continue
            n = len(pair)
            z = abs(float(corr)) * math.sqrt(max(n - 2, 1) / max(1.0 - float(corr) ** 2, 1e-12))
            values.append(float(math.erfc(z / math.sqrt(2.0))))
            samples += n
        if not values:
            return None, samples
        # 多标的聚合采用保守 Bonferroni，避免取最小 p-value 后过度乐观。
        return min(1.0, min(values) * len(values)), samples

    @staticmethod
    def _bh_adjust(p_values: dict[str, float]) -> dict[str, float]:
        ordered = sorted(p_values.items(), key=lambda item: item[1])
        total = len(ordered)
        adjusted: dict[str, float] = {}
        running = 1.0
        for index in range(total - 1, -1, -1):
            factor_id, p_value = ordered[index]
            rank = index + 1
            running = min(running, p_value * total / rank)
            adjusted[factor_id] = min(1.0, running)
        return adjusted

    def prepare_multiple_testing(
        self, records: list[EvalFactorRecord], family: Optional[str] = None
    ) -> dict[str, dict[str, Any]]:
        """对统一因子族计算近似双侧 p-value 并做 Benjamini-Hochberg FDR。"""
        p_values: dict[str, float] = {}
        sample_counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        for rec in records:
            # 序列缓存是因子级的：不逐因子清理会把全家族序列同时留在内存。
            self.reset_series_cache()
            try:
                p_value, samples = self._p_value(rec)
            except Exception as exc:
                p_value, samples = None, 0
                errors[str(rec.factor_id)] = f"{type(exc).__name__}: {exc}"
            sample_counts[str(rec.factor_id)] = samples
            if p_value is not None:
                p_values[str(rec.factor_id)] = p_value
        adjusted = self._bh_adjust(p_values)
        alpha = float(self._setting("multiple_testing_alpha", 0.05))
        family = family or str(self._setting("multiple_testing_family", "development_factor_family"))
        exploratory = bool(self._setting("exploratory_candidate_pool", False))
        family_size = len(p_values)
        result: dict[str, dict[str, Any]] = {}
        for rec in records:
            factor_id = str(rec.factor_id)
            p_value = p_values.get(factor_id)
            adjusted_p = adjusted.get(factor_id)
            status = PASS if p_value is not None and adjusted_p is not None else BLOCKED
            significant = adjusted_p is not None and adjusted_p <= alpha
            if status == PASS:
                failure_reason = None
            elif factor_id in errors:
                failure_reason = f"p-value 计算异常：{errors[factor_id]}"
            else:
                failure_reason = "开发集有效样本不足，无法计算 p-value"
            result[factor_id] = {
                "status": status,
                "p_value": p_value,
                "adjusted_p_value": adjusted_p,
                "method": "Benjamini-Hochberg FDR",
                "family": family,
                "family_size": family_size,
                "alpha": alpha,
                "exploratory": exploratory,
                "significant_after_correction": significant,
                "sample_count": sample_counts.get(factor_id, 0),
                "failure_reason": failure_reason,
            }
            if status == PASS:
                # PASS 只代表「p-value 算得出来」，显著性结论在
                # significant_after_correction；附 note 供调用方分层展示。
                result[factor_id]["note"] = (
                    "PASS 仅表示 p-value 可计算，不代表校正后显著；显著性以 "
                    f"significant_after_correction 为准（当前：{'是' if significant else '否'}，"
                    f"校正后 p-value={adjusted_p}，alpha={alpha}）。"
                )
        return result

    # ------------------------------------------------------------------
    # 参数过拟合 / 成本假设 / 样本外
    # ------------------------------------------------------------------
    def overfit(self, rec: EvalFactorRecord) -> dict[str, Any]:
        parameter_search = self.settings.get("parameter_search", {}) or {}
        metadata = (
            parameter_search.get(str(rec.factor_id), {})
            if isinstance(parameter_search, dict)
            else {}
        )
        params = dict(getattr(rec, "params", {}) or {})
        if metadata:
            frozen = metadata.get("frozen")
            retuned = metadata.get("retuned_on_oos", metadata.get("oos_retuned"))
            if frozen is True and retuned is not True:
                status = PASS
                reason = None
            elif frozen is False or retuned is True:
                status = FAIL
                reason = "参数未冻结或在样本外重新调参"
            else:
                status = BLOCKED
                reason = "参数搜索记录缺少 frozen 或 retuned_on_oos 字段"
            return {
                "status": status,
                "search_space": metadata.get("search_space", params),
                "tried_count": metadata.get("tried_count", metadata.get("attempt_count")),
                "best_dev_params": metadata.get("best_dev_params", params),
                "validation_result": metadata.get("validation_result"),
                "frozen": frozen,
                "retuned_on_oos": retuned,
                "failure_reason": reason,
            }

        if params:
            return {
                "status": BLOCKED,
                "search_space": {"fixed_params": params},
                "tried_count": 1,
                "best_dev_params": params,
                "validation_result": {"reported": False},
                "frozen": None,
                "retuned_on_oos": None,
                "failure_reason": "只有固定参数值，缺少显式参数冻结和样本外未调参证据",
                "method": "需要 bias_control.parameter_search 提供 frozen=true 且 retuned_on_oos=false",
            }
        return {
            "status": BLOCKED,
            "search_space": None,
            "tried_count": None,
            "best_dev_params": None,
            "validation_result": None,
            "frozen": None,
            "retuned_on_oos": None,
            "failure_reason": "因子没有参数或参数搜索元数据",
        }

    def cost(self, rec: EvalFactorRecord) -> dict[str, Any]:
        scenarios = self.settings.get("cost_scenarios_bps", [0, 2, 5, 10])
        scenarios = [float(value) for value in scenarios]
        fee_maker_bps = float(self._setting("fee_maker_bps", 2))
        fee_taker_bps = float(self._setting("fee_taker_bps", 5))
        execution_side = str(self._setting("cost_execution_side", "taker")).lower()
        execution_fee_bps = fee_maker_bps if execution_side == "maker" else fee_taker_bps
        slippage_bps = float(self._setting("slippage_bps", 0))
        funding_cost_bps = float(self._setting("funding_cost_bps", 0))
        horizon = int(self._setting("cost_horizon_bars", 24))
        window = max(2, horizon * 4)
        gross_values: list[float] = []
        turnover_values: list[float] = []
        nets: dict[float, list[float]] = {bps: [] for bps in scenarios}
        sample_count = 0
        errors: list[str] = []
        dev_start, dev_end = self._window("development_window")
        for symbol in self._factor_symbols(str(rec.factor_id)):
            try:
                factor, close, _ = self._series_cached(
                    rec,
                    symbol,
                    start=dev_start,
                    end=dev_end,
                    min_samples=int(self._setting("cost_min_samples", 50)),
                )
                frame = pd.concat({"factor": factor, "close": close}, axis=1, sort=False).dropna()
                if len(frame) < max(50, window):
                    continue
                median = frame["factor"].rolling(window, min_periods=max(2, window // 2)).median()
                position = pd.Series(np.where(frame["factor"] > median, 1.0, -1.0), index=frame.index)
                ret = frame["close"].pct_change()
                gross = position.shift(1) * ret
                flip = position.ne(position.shift(1)).astype(float)
                flip.iloc[0] = 1.0
                valid = pd.concat(
                    {"gross": gross, "flip": flip, "position": position.shift(1)},
                    axis=1,
                    sort=False,
                ).dropna()
                if valid.empty:
                    continue
                sample_count += len(valid)
                gross_values.append(float((1.0 + valid["gross"]).prod() - 1.0))
                turnover_values.append(float(valid["flip"].mean()))
                for bps in scenarios:
                    # bps 是压力增量；配置的手续费/滑点/资金费始终计入净收益。
                    execution_bps = bps + execution_fee_bps + slippage_bps
                    transaction_cost = valid["flip"] * (2.0 * execution_bps / 10000.0)
                    funding_cost = valid["position"].abs() * funding_cost_bps / 10000.0
                    net = valid["gross"] - transaction_cost - funding_cost
                    nets[bps].append(float((1.0 + net).prod() - 1.0))
            except Exception as error:
                errors.append(f"{symbol}: {type(error).__name__}: {error}")

        if sample_count < int(self._setting("cost_min_samples", 50)) or not gross_values:
            return {
                "status": BLOCKED,
                "fee_maker_bps": fee_maker_bps,
                "fee_taker_bps": fee_taker_bps,
                "execution_side": execution_side,
                "slippage_bps": slippage_bps,
                "funding_cost_bps": funding_cost_bps,
                "portfolio_turnover": None,
                "gross_return": None,
                "net_return": None,
                "sensitivity": [],
                "failure_reason": "开发集成本敏感性有效样本不足",
            }

        sensitivity = []
        for bps in scenarios:
            values = nets[bps]
            net_return = float(np.mean(values)) if values else None
            sensitivity.append({
                "cost_bps": bps,
                "gross_return": float(np.mean(gross_values)),
                "net_return": net_return,
                "turnover": float(np.mean(turnover_values)) if turnover_values else None,
                "status": PASS if net_return is not None and math.isfinite(net_return) else BLOCKED,
            })
        # 所有敏感性档位都算不出有效净收益时，顶层不能仅凭「无异常」判 PASS。
        all_blocked = bool(sensitivity) and all(
            item["status"] == BLOCKED for item in sensitivity
        )
        result = {
            "status": BLOCKED if errors or all_blocked else PASS,
            "fee_maker_bps": fee_maker_bps,
            "fee_taker_bps": fee_taker_bps,
            "execution_side": execution_side,
            "slippage_bps": slippage_bps,
            "funding_cost_bps": funding_cost_bps,
            "portfolio_turnover": float(np.mean(turnover_values)) if turnover_values else None,
            "gross_return": float(np.mean(gross_values)) if gross_values else None,
            "net_return": sensitivity[-1]["net_return"] if sensitivity else None,
            "sensitivity": sensitivity,
            "sample_count": sample_count,
            "failure_reason": "; ".join(errors) if errors else (
                "全部成本档位的净收益均不是有限值" if all_blocked else None
            ),
            "method": "按成本档位重算信号净收益；成本档位单位为 bps/单边",
        }
        if result["status"] == PASS:
            # PASS 只代表「净收益算得出来」，盈亏结论看 net_return 符号。
            net = result["net_return"]
            sign = "正" if (net is not None and net > 0) else "负" if (net is not None and net < 0) else "零/缺失"
            result["note"] = (
                "PASS 仅表示各成本档位净收益可计算，不代表扣成本后仍盈利；盈亏以 "
                f"net_return 字段为准（当前最高成本档 net_return={net}，符号：{sign}），"
                "逐档位明细见 sensitivity。"
            )
        return result

    def out_of_sample(self, rec: EvalFactorRecord, run_id: Optional[str] = None) -> dict[str, Any]:
        """锁定样本外窗口（独立于开发集，只允许成功运行一次——由服务层把关）。"""
        dev_start, dev_end = self._window("development_window")
        oos_start, oos_end = self._window("out_of_sample_window")
        base: dict[str, Any] = {
            "status": BLOCKED,
            "development_window": self._window_payload(dev_start, dev_end),
            "oos_window": self._window_payload(oos_start, oos_end),
            "locked": bool(oos_start is not None and oos_end is not None),
            "has_run": True,
            "can_run_once": False,
            "run_id": run_id,
            "data_hash": self.settings.get("data_hash"),
            "code_hash": self.settings.get("code_hash"),
            "config_hash": self.settings.get("config_hash"),
            "complete": False,
            "sample_count": 0,
            "gross_return": None,
            "net_return": None,
            "failure_reason": None,
        }
        if oos_start is None or oos_end is None or oos_start >= oos_end:
            base["failure_reason"] = "未配置有效的锁定样本外区间"
            return base

        min_samples = int(self._setting("oos_min_samples", 50))
        cost_bps = float(self._setting("oos_cost_bps", self._setting("fee_taker_bps", 5)))
        signal_window = max(2, int(self._setting("oos_signal_window", 24)))
        period = rec.frequency if rec.frequency in FREQ_DELTAS else "1d"
        gross_values: list[float] = []
        net_values: list[float] = []
        sample_count = 0
        errors: list[str] = []
        for symbol in self._factor_symbols(str(rec.factor_id)):
            try:
                factor, _close, _recomputed = self._series_for_window(
                    rec, symbol, start=oos_start, end=oos_end, min_samples=min_samples,
                )
                close = self._close_series(symbol, oos_start, oos_end, period)
                frame = pd.concat({"factor": factor, "close": close}, axis=1, sort=False).dropna()
                if len(frame) < min_samples:
                    continue
                median = frame["factor"].rolling(
                    signal_window, min_periods=max(2, signal_window // 2)
                ).median()
                position = pd.Series(
                    np.where(frame["factor"] > median, 1.0, -1.0), index=frame.index
                )
                returns = frame["close"].pct_change()
                turnover = position.ne(position.shift(1)).astype(float)
                turnover.iloc[0] = 1.0
                valid = pd.concat(
                    {"gross": position.shift(1) * returns, "turnover": turnover}, axis=1
                ).dropna()
                if valid.empty:
                    continue
                net = valid["gross"] - valid["turnover"] * (2.0 * cost_bps / 10000.0)
                gross_values.append(float((1.0 + valid["gross"]).prod() - 1.0))
                net_values.append(float((1.0 + net).prod() - 1.0))
                sample_count += len(valid)
            except Exception as error:
                errors.append(f"{symbol}: {type(error).__name__}: {error}")

        base["sample_count"] = sample_count
        base["gross_return"] = float(np.mean(gross_values)) if gross_values else None
        base["net_return"] = float(np.mean(net_values)) if net_values else None
        if errors:
            base["failure_reason"] = "; ".join(errors)
        elif sample_count < min_samples or not net_values:
            base["failure_reason"] = "锁定样本外有效样本不足，无法完成独立运行"
        else:
            base["status"] = PASS
            base["complete"] = True
            # PASS 只代表「锁定样本外跑完了一次」，盈亏结论看 net_return 符号。
            net = base["net_return"]
            sign = "正" if (net is not None and net > 0) else "负" if (net is not None and net < 0) else "零/缺失"
            base["note"] = (
                "PASS 仅表示锁定样本外独立运行完成且样本充足，不代表样本外盈利；盈亏以 "
                f"net_return 字段为准（当前 net_return={net}，符号：{sign}）。"
            )
        return base

    # ------------------------------------------------------------------
    # 单因子六查汇总
    # ------------------------------------------------------------------
    def check_factor(
        self,
        rec: EvalFactorRecord,
        multiple_testing: Optional[dict[str, Any]] = None,
        scope: str = "development",
    ) -> dict[str, Any]:
        def run_check(name: str, fn: Any) -> dict[str, Any]:
            try:
                return fn()
            except Exception as error:
                return {
                    "status": ERROR,
                    "checked_at": _now_iso(),
                    "failure_reason": f"{name}执行异常：{type(error).__name__}: {error}",
                }

        lookahead = run_check("前视检查", lambda: self.lookahead(rec))
        full_sample = run_check("全样本泄露检查", lambda: self.full_sample(rec, lookahead))
        multiple = multiple_testing or {
            "status": BLOCKED,
            "failure_reason": "未准备多重检验家族数据",
        }
        overfit = run_check("参数过拟合检查", lambda: self.overfit(rec))
        cost = run_check("成本敏感性检查", lambda: self.cost(rec))
        checks = {
            "lookahead": lookahead,
            "full_sample": full_sample,
            "multiple_testing": multiple,
            "overfit": overfit,
            "cost": cost,
        }
        oos_window = self._window("out_of_sample_window")
        oos_locked = (
            oos_window[0] is not None
            and oos_window[1] is not None
            and oos_window[0] < oos_window[1]
        )
        if scope == "locked_oos":
            oos = self.out_of_sample(rec)
        else:
            oos = {
                "status": LOCKED if oos_locked else NOT_CHECKED,
                "development_window": self._window_payload(*self._window("development_window")),
                "oos_window": self._window_payload(*oos_window),
                "locked": oos_locked,
                "has_run": False,
                "can_run_once": oos_locked,
                "complete": False,
                "failure_reason": None if oos_locked else "未配置有效的锁定样本外区间",
            }
        statuses = [item.get("status", NOT_CHECKED) for item in checks.values()]
        if scope == "locked_oos":
            statuses.append(oos.get("status", NOT_CHECKED))
        elif not oos_locked:
            # 未配置样本外窗口时，开发集检查不能声称完整偏差控制门槛通过。
            statuses.append(NOT_CHECKED)
        if FAIL in statuses:
            overall = FAIL
        elif ERROR in statuses:
            overall = ERROR
        elif BLOCKED in statuses:
            overall = BLOCKED
        elif RUNNING in statuses:
            overall = RUNNING
        elif NOT_CHECKED in statuses:
            overall = NOT_CHECKED
        else:
            overall = PASS
        reasons = [
            item.get("failure_reason") for item in checks.values() if item.get("failure_reason")
        ]
        if oos.get("failure_reason"):
            reasons.append(oos["failure_reason"])
        return {
            "factor_id": str(rec.factor_id),
            "overall_status": overall,
            "checks": checks,
            "oos": oos,
            "failure_reason": "; ".join(str(reason) for reason in reasons) or None,
            "evidence_path": str(getattr(rec, "impl_path", "") or ""),
            "checked_at": _now_iso(),
            "scope": scope,
        }


# ----------------------------------------------------------------------
# 偏差检查批次服务（移植 BiasControlService 的运行与报告导出；
# CLI 一次性同步执行，不移植源项目的后台线程/暂停恢复）
# ----------------------------------------------------------------------


class BiasControlService:
    """六查批次执行 + 报告导出（同步执行，结果落 eval_bias_* 表）。

    样本外一次性锁定：scope=locked_oos 时，已在 eval_oos_lock 在册的因子
    拒绝重复执行（结果如实标 LOCKED + 拒绝原因）。
    """

    def __init__(
        self,
        config: Any,
        providers: Any = None,
        cache_path: str | Path = "data/cache.duckdb",
        store: Any = None,
        symbols: Optional[list[str]] = None,
        fetcher: Optional[KlineFetcher] = None,
        eval_store: Optional[EvalCacheStore] = None,
    ) -> None:
        self.config = config
        self.fetcher = fetcher or KlineFetcher(providers, config, store=store)
        self.eval_store = eval_store or EvalCacheStore(cache_path)
        self.symbols = symbols

    def _new_runner(self) -> BiasCheckRunner:
        return BiasCheckRunner(self.fetcher, self.config, symbols=self.symbols)

    def run(
        self,
        scope: str,
        factor_ids: list[str],
        run_id: Optional[str] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, Any]:
        """同步执行一个六查批次，返回 run 摘要（结果逐因子落库）。

        scope: development（开发集五查，样本外显示 LOCKED）或
               locked_oos（五查 + 锁定样本外，每因子只允许成功一次）。
        """
        run_id = run_id or uuid.uuid4().hex[:12]
        import json as _json

        run: dict[str, Any] = {
            "run_id": run_id,
            "scope": scope,
            "status": RUNNING,
            "factor_ids_json": _json.dumps(factor_ids, ensure_ascii=False),
            "started_at": datetime.now(timezone.utc),
            "finished_at": None,
        }
        self.eval_store.save_run(run)

        records: list[EvalFactorRecord] = []
        missing: list[str] = []
        for factor_id in factor_ids:
            rec = load_factor_record(factor_id, self.config)
            if rec is None:
                missing.append(factor_id)
            else:
                records.append(rec)

        runner = self._new_runner()
        # 多重检验按家族一次性准备（BH 校正跨整个批次因子族）
        mt_context = runner.prepare_multiple_testing(records) if records else {}

        results: list[dict[str, Any]] = []
        total = len(records)
        for index, rec in enumerate(records, 1):
            if on_progress is not None:
                on_progress(index, total, rec.factor_id)
            if scope == "locked_oos" and self.eval_store.oos_locked(rec.factor_id):
                result = {
                    "factor_id": rec.factor_id,
                    "overall_status": LOCKED,
                    "checks": {},
                    "oos": {
                        "status": LOCKED,
                        "locked": True,
                        "has_run": True,
                        "can_run_once": False,
                        "complete": False,
                        "failure_reason": "锁定样本外已运行过一次，一次性锁定不允许重复执行",
                    },
                    "failure_reason": "锁定样本外已运行过一次，一次性锁定不允许重复执行",
                    "evidence_path": str(rec.impl_path or ""),
                    "checked_at": _now_iso(),
                    "scope": scope,
                }
            else:
                runner.reset_series_cache()
                result = runner.check_factor(
                    rec, multiple_testing=mt_context.get(rec.factor_id), scope=scope
                )
                if scope == "locked_oos" and result.get("oos", {}).get("complete"):
                    self.eval_store.oos_lock(rec.factor_id, run_id)
            self.eval_store.save_result(run_id, result)
            results.append(result)

        for factor_id in missing:
            result = {
                "factor_id": factor_id,
                "overall_status": BLOCKED,
                "checks": {},
                "oos": {},
                "failure_reason": "因子未注册（双文件与 config 通道均未找到）",
                "evidence_path": "",
                "checked_at": _now_iso(),
                "scope": scope,
            }
            self.eval_store.save_result(run_id, result)
            results.append(result)

        run["status"] = "finished"
        run["finished_at"] = datetime.now(timezone.utc)
        self.eval_store.save_run(run)
        return {
            **run,
            "factor_ids": factor_ids,
            "summary": {
                "total": len(results),
                "pass": sum(1 for r in results if r["overall_status"] == PASS),
                "fail": sum(1 for r in results if r["overall_status"] == FAIL),
                "blocked": sum(1 for r in results if r["overall_status"] == BLOCKED),
                "error": sum(1 for r in results if r["overall_status"] == ERROR),
                "locked": sum(1 for r in results if r["overall_status"] == LOCKED),
            },
            "results": results,
        }

    # ------------------------------------------------------------------
    # 报告导出（json / csv / md）
    # ------------------------------------------------------------------
    def report_data(self, run_id: str) -> Optional[dict[str, Any]]:
        import json as _json

        run = self.eval_store.get_run(run_id)
        if run is None:
            return None
        factors = []
        for row in self.eval_store.run_results(run_id):
            factors.append({
                "factor_id": row["factor_id"],
                "overall_status": row.get("overall_status") or NOT_CHECKED,
                "checks": _json.loads(row.get("checks_json") or "{}"),
                "oos": _json.loads(row.get("oos_json") or "{}"),
                "failure_reason": row.get("failure_reason"),
                "evidence_path": row.get("evidence_path"),
                "checked_at": _json_safe(row.get("checked_at")),
            })
        return {"run": _json_safe(run), "factors": factors}

    def report(self, run_id: str, output_format: str) -> tuple[str, str]:
        """返回 (内容, 文件名)。CSV 带 BOM（与项目其他报告一致）。"""
        import json as _json

        data = self.report_data(run_id)
        if data is None:
            raise KeyError(run_id)
        if output_format == "json":
            return (
                _json.dumps(_json_safe(data), ensure_ascii=False, indent=2),
                f"bias_control_{run_id}.json",
            )
        if output_format == "csv":
            import csv

            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow([
                "factor_id", "overall_status",
                *CHECK_KEYS, "oos_status", "checked_at", "failure_reason",
            ])
            for row in data["factors"]:
                writer.writerow([
                    row.get("factor_id"),
                    row.get("overall_status"),
                    *[
                        row.get("checks", {}).get(key, {}).get("status", NOT_CHECKED)
                        for key in CHECK_KEYS
                    ],
                    row.get("oos", {}).get("status", NOT_CHECKED),
                    row.get("checked_at"),
                    row.get("failure_reason"),
                ])
            return "﻿" + buffer.getvalue(), f"bias_control_{run_id}.csv"
        if output_format == "md":
            run = data["run"]
            lines = [
                f"# 偏差控制检查报告：{run_id}",
                "",
                f"- 状态：{run.get('status')}",
                f"- 范围：{run.get('scope')}",
                f"- 开始时间（UTC）：{_json_safe(run.get('started_at')) or '—'}",
                f"- 完成时间（UTC）：{_json_safe(run.get('finished_at')) or '—'}",
                "",
                "| 因子 ID | 总体状态 | 前视偏差 | 全样本泄露 | 多重检验 | 参数过拟合 | 成本假设 | 样本外 | 失败原因 |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for row in data["factors"]:
                checks = row.get("checks", {})
                statuses = [
                    checks.get(key, {}).get("status", NOT_CHECKED) for key in CHECK_KEYS
                ]
                lines.append(
                    "| " + " | ".join([
                        str(row.get("factor_id") or "—"),
                        str(row.get("overall_status") or NOT_CHECKED),
                        *statuses,
                        str(row.get("oos", {}).get("status", NOT_CHECKED)),
                        str(row.get("failure_reason") or "—"),
                    ]) + " |"
                )
            return "\n".join(lines) + "\n", f"bias_control_{run_id}.md"
        raise ValueError(f"不支持的报告格式: {output_format}")
