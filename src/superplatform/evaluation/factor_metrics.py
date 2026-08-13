"""因子评估指标：IC / RankIC / ICIR、IC 衰减、分层测试、滚动稳定性、换手率、
库级相关性矩阵与合格判定（04 阶段，指标的唯一权威实现——CLI/页面只调用本模块，不自算）。

算法移植自 sim_platform `app/factor_metrics.py`，适配点：

* 数据源从源项目 Store 换成本平台 DataProvider（经 bias.KlineFetcher），
  因子值一律按注册实现 + K 线重算（本平台无落库因子值历史）；
* 评估粒度取因子自身频率：1d/1w 因子按日频评估开发集全窗口；其余频率
  （intraday，如 1h）按自身 bar 评估，窗口封顶 metrics_intraday_max_bars
  根（取开发集内最近一段），防止分钟级全窗口重算超限；
* 指标口径（与源一致）：逐 symbol 时序计算、跨 symbol 均值汇总；
  IC=Pearson、RankIC=Spearman、ICIR=滚动 RankIC 均值/标准差、
  换手=1-相邻期因子排名 Spearman 相关、分层按因子值分位数分组、
  衰减=多 horizon 的 RankIC/IC；样本不足如实 BLOCKED（insufficient_samples），
  不给假数字；
* 相关性矩阵：日频网格逐 symbol Spearman 再跨 symbol 均值；因子数封顶
  metrics_corr_max_factors（规模约束：不做在线 O(n²) 全库矩阵），
  超出按 factor_id 排序截断并在 truncated 字段如实标注；串行重算
  （源项目的 spawn 子进程池未移植——本平台因子库当前规模下串行足够，
  批量提速靠跨因子共享 K 线帧缓存与 DuckDB 结果缓存的增量重算）；
* 结果带参数指纹 + 数据版本键写 DuckDB 缓存（eval_metrics_cache /
  eval_corr_cache），命中直接返回，force=True 强制重算并按原键覆盖。

合格判定（qualification gating）移植自源项目对 tools/batch_factor_report_numba.py
的移植版：六项全过才算合格——|RankIC| >= ic_min、p_value < p_max、
|ICIR| >= icir_min、分层单调（组均收益严格单调，或 |组序-组均收益 Spearman| > 0.6）、
|多空差| >= long_short_min、decay_ratio >= decay_min_ratio（不可算时跳过该项）。
派生字段口径：

* p_value：主 horizon 的跨 symbol 均值 RankIC + 总样本数 n 做 t 近似，
  t = |r|·sqrt((n-2)/(1-r²))，大样本下退化为标准正态，双侧 p = erfc(t/√2)
  （不引 scipy）。r 是跨 symbol 均值而 n 是合计样本数，p 值偏乐观，
  仅用于合格判定阈值，不作严格统计推断；
* decay_ratio：ic_decay 中 horizon = 5×主 horizon（缺失时退化为第二个
  horizon）的 RankIC 与主 horizon RankIC 的比值。主 horizon |RankIC| <= 1e-6
  或任一端缺失时返回 None（判定时跳过）；两端反号视为完全衰减，返回 0.0。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from superplatform.evaluation.bias import (
    BLOCKED,
    BiasCheckRunner,
    EvalCacheStore,
    EvalFactorRecord,
    KlineFetcher,
    _json_safe,
    _now_iso,
    _number,
    bar_delta,
    is_daily,
    list_factor_records,
    load_factor_record,
)

logger = logging.getLogger("superplatform.evaluation.factor_metrics")

# 指标计算状态（指标本身不做通过判定）
OK = "OK"
PARTIAL = "PARTIAL"
ERROR = "ERROR"

# 汇总行在 CSV 里的 symbol 占位
_ALL = "ALL"

# 相关性矩阵两两有效样本下限（低于此值的格点留空，不给误导性数字）
_CORR_MIN_PAIR_SAMPLES = 30

# 相关性矩阵构建口径版本号（进入缓存键）：口径变化时递增让旧缓存自然失效
_CORR_MATRIX_REVISION = 1

# 合格判定默认阈值（config bias_control.qualification_rules 缺失时兜底；
# 口径移植自源项目 config.yaml 的 default 规则集）
_DEFAULT_QUALIFICATION_RULES: dict[str, Any] = {
    "library": {},
    "default": {"ic_min": 0.025, "icir_min": 0.5, "p_max": 0.05,
                "decay_min_ratio": 0.5, "long_short_min": 0.001},
    "sensitivity_base": "default",
    "sensitivity": {
        "ic_min": [0.005, 0.01, 0.015, 0.02, 0.025],
        "icir_min": [0.1, 0.2, 0.3, 0.4, 0.5],
        "p_max": [0.01, 0.03, 0.05, 0.07, 0.1],
        "decay_min_ratio": [0.1, 0.2, 0.3, 0.4, 0.5],
        "long_short_min": [0.0002, 0.0005, 0.001, 0.002, 0.005],
    },
}

# 合格判定真实生效的五个阈值键
_RULE_KEYS = ("ic_min", "icir_min", "p_max", "decay_min_ratio", "long_short_min")


def _spearman(left: pd.Series, right: pd.Series) -> Optional[float]:
    """Spearman 相关的安全封装：常数序列 / 非有限值一律返回 None。"""
    if len(left) < 3:
        return None
    try:
        # 常数序列（零方差）除零发 RuntimeWarning，其 NaN 结果本就是设计口径
        # （下方 _number 归为 None），只压警告不改数值。
        with np.errstate(invalid="ignore", divide="ignore"):
            value = left.rank().corr(right.rank())
    except Exception:
        return None
    return _number(value)


def _pearson(left: pd.Series, right: pd.Series) -> Optional[float]:
    if len(left) < 3:
        return None
    try:
        with np.errstate(invalid="ignore", divide="ignore"):
            value = left.corr(right)
    except Exception:
        return None
    return _number(value)


def _resample_daily_series(series: pd.Series) -> pd.Series:
    """sub-daily 序列重采样到日频网格：取每日最后一个非空值。"""
    if series.empty:
        return series
    return series.resample("1D").last().dropna()


class FactorMetricsCalculator(BiasCheckRunner):
    """单因子评估指标与库级相关性矩阵的真实计算。

    复用 BiasCheckRunner 的 K 线帧缓存（按 symbol+period 覆盖区间缓存，
    重叠窗口内存切片），避免逐因子/逐 symbol 重复拉取。
    """

    # ------------------------------------------------------------------
    # 参数与窗口
    # ------------------------------------------------------------------
    def _grid(self, rec: EvalFactorRecord) -> str:
        return "1d" if is_daily(getattr(rec, "frequency", "1d")) else "intraday"

    def _metrics_params(self, grid: str) -> dict[str, Any]:
        """按评估粒度取 horizon/滚动窗口等参数（单位：因子频率的 bar）。"""
        if grid == "intraday":
            horizons = self._setting("metrics_horizons_intraday", [1, 6, 24, 72, 168])
            window = int(self._setting("metrics_rolling_window_bars_intraday", 720))
            step = int(self._setting("metrics_rolling_step_bars_intraday", 168))
        else:
            horizons = self._setting("metrics_horizons_1d", [1, 5, 10, 20, 60])
            window = int(self._setting("metrics_rolling_window_bars", 120))
            step = int(self._setting("metrics_rolling_step_bars", 20))
        horizons = sorted({int(h) for h in horizons if int(h) > 0}) or [1]
        return {
            "horizons": horizons,
            "rolling_window": max(10, window),
            "rolling_step": max(1, step),
            "quantiles": max(2, int(self._setting("metrics_quantiles", 5))),
            "min_samples": max(30, int(self._setting("metrics_min_samples", 120))),
        }

    def _eval_window(self) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
        """评估窗口：取 bias_control.development_window，与多重检验/成本检查一致。"""
        start, end = self._window("development_window")
        if start is None or end is None or start >= end:
            symbols = self.symbols or ["BTCUSDT"]
            start, end = self._fallback_window(symbols[0], None, None)
        return start, end

    # ------------------------------------------------------------------
    # 因子值序列：按注册实现 + K 线重算（因子级覆盖缓存）
    # ------------------------------------------------------------------
    def _recompute_cached(
        self,
        rec: EvalFactorRecord,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        grid: str,
    ) -> pd.Series:
        """按 (factor_id, symbol, period) 覆盖缓存的重算：覆盖切片/并集重算。

        warmup 规则：1d 用 max(metrics_warmup_bars, lookback*3) 天（固定值使
        所有因子共享同一 K 线缓存覆盖区间），intraday 用 max(lookback*3, 240)
        根 bar；并集重算时 warmup 从并集起点按同一规则再往前推。
        只缓存非空序列；异常由调用方按原路径捕获，缓存不写入。
        """
        period = rec.frequency if rec.frequency else "1d"
        key = (str(rec.factor_id), symbol, period)
        req_start, req_end = start, end
        entry = self._series_cache.get(key)
        if entry is not None:
            cov_start, cov_end, cached = entry[0], entry[1], entry[2]
            if cov_start <= start and cov_end >= end:
                return self._slice_series(cached, start, end)
            start, end = min(cov_start, start), max(cov_end, end)
        lookback = int(getattr(rec, "lookback_bars", 20) or 20)
        if grid == "1d":
            warmup_days = int(self._setting("metrics_warmup_bars", 400))
            warmup_start = start - pd.Timedelta(days=max(warmup_days, lookback * 3))
        else:
            warmup_start = start - bar_delta(rec.frequency, max(lookback * 3, 240))
        series = self._recompute_prefix(rec, symbol, warmup_start, end)
        if not series.empty:
            series = series[series.index >= start]
        if not series.empty:
            self._series_cache[key] = (start, end, series, True)
        return self._slice_series(series, req_start, req_end)

    def _series_map(
        self,
        rec: EvalFactorRecord,
        grid: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        min_samples: int,
        symbols: Optional[list[str]] = None,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """返回 {symbol: {series, close, source}} 与告警列表（全部重算口径）。"""
        factor_id = str(rec.factor_id)
        out: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        period = rec.frequency if rec.frequency else "1d"
        for symbol in (symbols if symbols is not None else self._factor_symbols(factor_id)):
            series = pd.Series(dtype=float)
            error: Optional[str] = None
            try:
                series = self._recompute_cached(rec, symbol, start, end, grid)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                series = pd.Series(dtype=float)
            valid = len(series.dropna())
            if valid < min_samples:
                reason = error or f"有效样本 {valid} < 最小要求 {min_samples}"
                warnings.append(f"{symbol}: {reason}")
                continue
            close = self._close_series(symbol, start, end, period)
            out[symbol] = {
                "series": series, "close": close,
                "source": "recomputed", "sample_count": valid,
            }
        return out, warnings

    # ------------------------------------------------------------------
    # 单 symbol 指标计算
    # ------------------------------------------------------------------
    def _symbol_metrics(
        self,
        frame: pd.DataFrame,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """frame 列为 factor/close（已对齐去空），返回该 symbol 的全部指标。"""
        horizons: list[int] = params["horizons"]
        primary = horizons[0]
        factor = frame["factor"]
        close = frame["close"]
        fwd = {h: close.shift(-h) / close - 1.0 for h in horizons}

        # IC 衰减（同时给出各 horizon 的 IC / RankIC）
        decay: list[dict[str, Any]] = []
        for h in horizons:
            pair = pd.concat({"factor": factor, "fwd": fwd[h]}, axis=1).dropna()
            decay.append({
                "horizon": h,
                "ic": _pearson(pair["factor"], pair["fwd"]),
                "rank_ic": _spearman(pair["factor"], pair["fwd"]),
                "sample_count": int(len(pair)),
            })

        # 分层测试（主 horizon）：按因子值排名的分位数分组
        quantile: dict[str, Any] = {"buckets": [], "monotonicity": None, "long_short": None}
        pair = pd.concat({"factor": factor, "fwd": fwd[primary]}, axis=1).dropna()
        q = params["quantiles"]
        if len(pair) >= q * 2:
            bucket = pd.qcut(pair["factor"].rank(method="first"), q, labels=False)
            grouped = pair.groupby(bucket)["fwd"]
            means = grouped.mean()
            counts = grouped.size()
            quantile["buckets"] = [
                {
                    "bucket": int(b) + 1,
                    "mean_return": _number(means.get(b)),
                    "sample_count": int(counts.get(b, 0)),
                }
                for b in range(q)
            ]
            valid_means = means.dropna()
            if len(valid_means) >= 3:
                quantile["monotonicity"] = _spearman(
                    pd.Series(valid_means.index.to_numpy(dtype="float64"), index=valid_means.index),
                    valid_means,
                )
            top = _number(means.get(q - 1))
            bottom = _number(means.get(0))
            if top is not None and bottom is not None:
                quantile["long_short"] = top - bottom

        # 滚动稳定性：滚动窗口 RankIC 序列（主 horizon）
        window = params["rolling_window"]
        step = params["rolling_step"]
        roll_frame = pd.concat({"factor": factor, "fwd": fwd[primary]}, axis=1).dropna()
        rolling_values: list[dict[str, Any]] = []
        if len(roll_frame) >= window:
            for offset in range(0, len(roll_frame) - window + 1, step):
                sliced = roll_frame.iloc[offset:offset + window]
                value = _spearman(sliced["factor"], sliced["fwd"])
                if value is not None:
                    rolling_values.append({
                        "start": sliced.index[0].isoformat().replace("+00:00", "Z"),
                        "rank_ic": value,
                    })
        series_values = [item["rank_ic"] for item in rolling_values]
        mean = _number(np.mean(series_values)) if series_values else None
        std = _number(np.std(series_values, ddof=1)) if len(series_values) > 1 else None
        rolling = {
            "mean": mean,
            "std": std,
            "positive_ratio": _number(np.mean([v > 0 for v in series_values])) if series_values else None,
            "count": len(series_values),
            "values": rolling_values,
        }

        # 换手率：1 - 相邻两期因子排名的 Spearman 相关
        consecutive = pd.concat({"now": factor, "prev": factor.shift(1)}, axis=1).dropna()
        rank_auto = _spearman(consecutive["now"], consecutive["prev"])
        turnover = (1.0 - rank_auto) if rank_auto is not None else None

        icir = None
        if mean is not None and std is not None and std > 1e-12:
            icir = mean / std
        return {
            "ic": decay[0]["ic"],
            "rank_ic": decay[0]["rank_ic"],
            "icir": _number(icir),
            "turnover": _number(turnover),
            "ic_decay": decay,
            "quantile": quantile,
            "rolling": rolling,
        }

    # ------------------------------------------------------------------
    # 单因子指标（跨 symbol 汇总）
    # ------------------------------------------------------------------
    def factor_metrics(self, rec: EvalFactorRecord) -> dict[str, Any]:
        started = time.perf_counter()
        factor_id = str(rec.factor_id)
        grid = self._grid(rec)
        start, end = self._eval_window()
        warnings: list[str] = []
        eval_start = start
        if grid == "intraday":
            max_bars = int(self._setting("metrics_intraday_max_bars", 250000))
            cap_start = end - bar_delta(rec.frequency, max_bars)
            if cap_start > start:
                eval_start = cap_start
                warnings.append(
                    f"{rec.frequency} 因子评估窗口封顶 {max_bars} 根 bar，实际取 {eval_start.isoformat()} 起"
                )
        params = self._metrics_params(grid)
        min_samples = max(
            params["min_samples"],
            params["horizons"][-1] + params["rolling_window"] + 2 * params["rolling_step"],
        )
        symbols = self._factor_symbols(factor_id)
        series_map, series_warnings = self._series_map(
            rec, grid, eval_start, end, min_samples, symbols=symbols
        )
        warnings.extend(series_warnings)

        symbol_rows: list[dict[str, Any]] = []
        for symbol, item in sorted(series_map.items()):
            frame = pd.concat(
                {"factor": item["series"], "close": item["close"]}, axis=1, sort=False
            ).dropna()
            if len(frame) < min_samples:
                warnings.append(f"{symbol}: 对齐后有效样本 {len(frame)} < {min_samples}")
                continue
            try:
                metrics = self._symbol_metrics(frame, params)
            except Exception as exc:
                warnings.append(f"{symbol}: 指标计算异常 {type(exc).__name__}: {exc}")
                continue
            if metrics["rank_ic"] is None:
                warnings.append(f"{symbol}: 因子值在窗口内无有效波动，RankIC 不可算")
                continue
            metrics["symbol"] = symbol
            metrics["source"] = item["source"]
            metrics["sample_count"] = int(len(frame))
            symbol_rows.append(metrics)

        base: dict[str, Any] = {
            "factor_id": factor_id,
            "frequency": grid,
            "factor_frequency": rec.frequency,
            "window": {
                "start": eval_start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
            "horizons": params["horizons"],
            "primary_horizon": params["horizons"][0],
            "quantiles": params["quantiles"],
            "rolling_window_bars": params["rolling_window"],
            "rolling_step_bars": params["rolling_step"],
            "computed_at": _now_iso(),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "warnings": warnings,
            "method": (
                "逐 symbol 时间序列计算，跨 symbol 取均值汇总；IC=Pearson、RankIC=Spearman，"
                "ICIR=滚动 RankIC 均值/标准差，换手=1-相邻期因子排名相关；"
                "因子值按注册实现 + K 线全窗口重算（本平台无落库因子值历史）"
            ),
        }

        if not symbol_rows:
            base.update({
                "status": BLOCKED,
                "insufficient_samples": True,
                "failure_reason": "全部 symbol 有效样本不足或因子值无波动，无法计算评估指标",
                "sample_count": 0,
                "ic": None, "rank_ic": None, "icir": None, "turnover": None,
                "ic_decay": [], "quantile_test": {"buckets": [], "monotonicity": None, "long_short": None},
                "rolling": {"mean": None, "std": None, "positive_ratio": None, "count": 0},
                "symbols": [],
            })
            return _json_safe(base)

        def avg(key: str) -> Optional[float]:
            values = [row[key] for row in symbol_rows if row.get(key) is not None]
            return _number(np.mean(values)) if values else None

        # IC 衰减汇总：逐 horizon 跨 symbol 均值
        decay_summary: list[dict[str, Any]] = []
        for index, horizon in enumerate(params["horizons"]):
            ics = [row["ic_decay"][index]["ic"] for row in symbol_rows if row["ic_decay"][index]["ic"] is not None]
            rics = [row["ic_decay"][index]["rank_ic"] for row in symbol_rows if row["ic_decay"][index]["rank_ic"] is not None]
            decay_summary.append({
                "horizon": horizon,
                "ic": _number(np.mean(ics)) if ics else None,
                "rank_ic": _number(np.mean(rics)) if rics else None,
                "sample_count": int(sum(row["ic_decay"][index]["sample_count"] for row in symbol_rows)),
            })

        # 分层汇总：各组平均收益按 symbol 等权平均，避免高波动标的主导
        q = params["quantiles"]
        bucket_rows: list[dict[str, Any]] = []
        for b in range(q):
            means = [
                row["quantile"]["buckets"][b]["mean_return"]
                for row in symbol_rows
                if len(row["quantile"]["buckets"]) > b and row["quantile"]["buckets"][b]["mean_return"] is not None
            ]
            bucket_rows.append({
                "bucket": b + 1,
                "mean_return": _number(np.mean(means)) if means else None,
                "sample_count": int(sum(
                    row["quantile"]["buckets"][b]["sample_count"]
                    for row in symbol_rows if len(row["quantile"]["buckets"]) > b
                )),
            })
        mean_values = pd.Series(
            [row["mean_return"] for row in bucket_rows], dtype="float64"
        ).dropna()
        monotonicity = None
        if len(mean_values) >= 3:
            monotonicity = _spearman(
                pd.Series(np.arange(len(mean_values), dtype="float64")), mean_values.reset_index(drop=True)
            )
        top = bucket_rows[-1]["mean_return"] if bucket_rows else None
        bottom = bucket_rows[0]["mean_return"] if bucket_rows else None
        long_short = (top - bottom) if (top is not None and bottom is not None) else None
        long_shorts = [row["quantile"]["long_short"] for row in symbol_rows if row["quantile"]["long_short"] is not None]

        # 滚动稳定性汇总：跨 symbol 汇总全部窗口 RankIC 值
        pooled = [
            item["rank_ic"] for row in symbol_rows for item in row["rolling"]["values"]
        ]
        roll_mean = _number(np.mean(pooled)) if pooled else None
        roll_std = _number(np.std(pooled, ddof=1)) if len(pooled) > 1 else None
        icir = None
        if roll_mean is not None and roll_std is not None and roll_std > 1e-12:
            icir = roll_mean / roll_std

        base.update({
            "status": OK if len(symbol_rows) == len(symbols) else PARTIAL,
            "insufficient_samples": False,
            "failure_reason": None,
            "sample_count": int(sum(row["sample_count"] for row in symbol_rows)),
            "ic": avg("ic"),
            "rank_ic": avg("rank_ic"),
            "icir": _number(icir),
            "turnover": avg("turnover"),
            "ic_decay": decay_summary,
            "quantile_test": {
                "horizon": params["horizons"][0],
                "buckets": bucket_rows,
                "monotonicity": monotonicity,
                "long_short": _number(long_short),
                "long_short_symbol_mean": _number(np.mean(long_shorts)) if long_shorts else None,
            },
            "rolling": {
                "window_bars": params["rolling_window"],
                "step_bars": params["rolling_step"],
                "mean": roll_mean,
                "std": roll_std,
                "positive_ratio": _number(np.mean([v > 0 for v in pooled])) if pooled else None,
                "count": len(pooled),
            },
            "symbols": [
                {**row, "rolling": {**row["rolling"]}} for row in symbol_rows
            ],
        })
        return _json_safe(base)

    # ------------------------------------------------------------------
    # 库级因子相关性矩阵（日频网格，Spearman，因子数封顶）
    # ------------------------------------------------------------------
    def correlation_matrix(
        self, records: list[EvalFactorRecord]
    ) -> dict[str, Any]:
        started = time.perf_counter()
        start, end = self._eval_window()
        params = self._metrics_params("1d")
        min_samples = params["min_samples"]

        max_factors = int(self._setting("metrics_corr_max_factors", 200))
        truncated = False
        if len(records) > max_factors:
            records = sorted(records, key=lambda r: str(r.factor_id))[:max_factors]
            truncated = True

        series_map: dict[str, dict[str, pd.Series]] = {}
        excluded: list[dict[str, str]] = []
        for rec in records:
            factor_id = str(rec.factor_id)
            grid = self._grid(rec)
            eval_start = start
            if grid == "intraday":
                max_bars = int(self._setting("metrics_intraday_max_bars", 250000))
                cap_start = end - bar_delta(rec.frequency, max_bars)
                if cap_start > start:
                    eval_start = cap_start
            per_factor: dict[str, pd.Series] = {}
            counts: list[str] = []
            for symbol in self._factor_symbols(factor_id):
                try:
                    series = self._recompute_cached(rec, symbol, eval_start, end, grid)
                except Exception as exc:
                    counts.append(f"{symbol}: 重算异常 {type(exc).__name__}: {exc}")
                    continue
                if grid == "intraday":
                    series = _resample_daily_series(series)
                valid = len(series.dropna())
                if valid >= min_samples:
                    per_factor[symbol] = series
                else:
                    counts.append(f"{symbol}: 有效样本 {valid} < 最小要求 {min_samples}")
            if per_factor:
                series_map[factor_id] = per_factor
            else:
                excluded.append({
                    "factor_id": factor_id,
                    "reason": "; ".join(counts[:2]) or "有效样本不足",
                })
            # 序列缓存是因子级的，逐因子清理控制内存
            self.reset_series_cache()

        factor_ids = sorted(series_map)
        symbols = sorted({symbol for item in series_map.values() for symbol in item})
        size = len(factor_ids)
        matrices: list[np.ndarray] = []
        count_mats: list[np.ndarray] = []
        sample_count = 0
        for symbol in symbols:
            wide = pd.DataFrame({
                fid: series_map[fid][symbol] for fid in factor_ids if symbol in series_map[fid]
            })
            wide = wide.reindex(columns=factor_ids)
            if wide.dropna(how="all").empty:
                continue
            sample_count += int((wide.notna().sum(axis=1) >= 2).sum())
            corr = wide.corr(method="spearman", min_periods=_CORR_MIN_PAIR_SAMPLES)
            notna = wide.notna().astype("float64")
            matrices.append(corr.to_numpy(dtype="float64"))
            count_mats.append((notna.T @ notna).to_numpy(dtype="float64"))

        if matrices:
            stacked = np.stack(matrices)
            valid = np.isfinite(stacked)
            summed = np.nansum(np.where(valid, stacked, 0.0), axis=0)
            denom = valid.sum(axis=0)
            matrix = np.where(denom > 0, summed / np.maximum(denom, 1), np.nan)
            counts = np.stack(count_mats).sum(axis=0)
            np.fill_diagonal(matrix, 1.0)
        else:
            matrix = np.full((size, size), np.nan)
            counts = np.zeros((size, size))

        matrix_rows = [
            [_number(matrix[i][j]) for j in range(size)]
            for i in range(size)
        ]
        count_rows = [
            [int(counts[i][j]) for j in range(size)]
            for i in range(size)
        ]
        return _json_safe({
            "factor_ids": factor_ids,
            "matrix": matrix_rows,
            "sample_counts": count_rows,
            "excluded": excluded,
            "truncated": truncated,
            "max_factors": max_factors,
            "frequency": "1d",
            "window": {
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
            "sample_count": int(sample_count),
            "computed_at": _now_iso(),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "method": (
                "日频网格逐 symbol 计算因子间 Spearman 相关，再跨 symbol 取均值；"
                "因子值按注册实现重算（1d 因子日频全窗口，intraday 因子在 "
                "metrics_intraday_max_bars 封顶窗口按自身频率重算并重采样到日频网格）；"
                "因子数封顶 metrics_corr_max_factors"
            ),
        })


class FactorMetricsService:
    """评估指标的缓存、注册表查询、合格判定与 CSV 导出服务（04 服务层）。

    供 CLI 与 05 的 API 映射调用；全部方法返回 JSON 可序列化 dict。
    非线程安全：同一时间只跑一个计算调用（DuckDB 缓存为单写者）。
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
        self._version_cache: tuple[float, str] = (0.0, "")

    # ------------------------------------------------------------------
    # 缓存键：参数指纹 + 数据版本
    # ------------------------------------------------------------------
    def _settings(self) -> dict[str, Any]:
        getter = getattr(self.config, "get", None)
        value = getter("bias_control", {}) if callable(getter) else {}
        return dict(value or {}) if isinstance(value, dict) else {}

    def _cache_enabled(self) -> bool:
        return bool(self._settings().get("metrics_cache_enabled", True))

    def _data_version(self, records: Optional[list[EvalFactorRecord]] = None) -> str:
        """数据版本键：因子消费的 provider 缓存表逐 symbol 首末日期 + 行数。

        日级粒度——同一交易日内结果不变；回填改变历史覆盖（首末日期/行数
        变化）会自动失效旧缓存。末日期按开发集窗口右端封顶：窗口外的新行情
        不应让全库缓存集体失效。带进程内 60s TTL（max/min 聚合走 zone map
        很快，但缓存命中路径不应每调都扫库）。
        """
        now = time.monotonic()
        cached_at, cached_value = self._version_cache
        if cached_value and now - cached_at < 60:
            return cached_value
        settings = self._settings()
        win = settings.get("development_window") or settings.get("dev_window") or {}
        cap = str(win.get("end") or "")[:10] if isinstance(win, dict) else ""

        def _clip(d: Any) -> Any:
            # ISO 日期字符串可直接按字典序比较；无窗口右端则不封顶
            if cap and d and str(d) > cap:
                return cap
            return d

        from superplatform.data.store import provider_table

        # 涉及的 (provider 表, 频率)：kline 按各因子频率 + 必要的辅助表
        tables: set[tuple[str, str]] = set()
        freqs = {"1d"}
        data_types = {"kline"}
        for rec in records or []:
            freqs.add(rec.frequency if rec.frequency else "1d")
            data_types.update(rec.data_types or [])
        provider_ids = {"kline": "binance-perp-kline"}
        getter = getattr(self.config, "get", None)
        exchange = getter("defaults.exchange", "binance") if callable(getter) else "binance"
        market = getter("defaults.market", "perpetual") if callable(getter) else "perpetual"
        provider_ids = {
            "kline": f"{exchange}-{'perp' if market == 'perpetual' else 'spot'}-kline",
            "funding_rate": f"{exchange}-perp-funding-rate",
            "open_interest": f"{exchange}-perp-open-interest",
        }
        for dt in sorted(data_types):
            pid = provider_ids.get(dt)
            if pid is None:
                continue
            if dt == "kline":
                for freq in sorted(freqs):
                    tables.add((provider_table(pid), freq))
            else:
                tables.add((provider_table(pid), {"funding_rate": "8h", "open_interest": "1d"}[dt]))

        parts: list[str] = []
        try:
            for table, freq in sorted(tables):
                for row in self.eval_store.series_bounds(table, freq):
                    parts.append(
                        f"{table}:{freq}:{row['symbol']}@{row['s']}~{_clip(row['e'])}#{row['c']}"
                    )
        except Exception:
            logger.warning("数据版本键计算失败，退化为空版本", exc_info=True)
        version = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
        self._version_cache = (now, version)
        return version

    def _cache_key(self, scope: dict[str, Any], records: Optional[list[EvalFactorRecord]] = None) -> str:
        settings = self._settings()
        fingerprint = {
            "h1d": settings.get("metrics_horizons_1d", [1, 5, 10, 20, 60]),
            "hi": settings.get("metrics_horizons_intraday", [1, 6, 24, 72, 168]),
            "rw": settings.get("metrics_rolling_window_bars", 120),
            "rs": settings.get("metrics_rolling_step_bars", 20),
            "rwi": settings.get("metrics_rolling_window_bars_intraday", 720),
            "rsi": settings.get("metrics_rolling_step_bars_intraday", 168),
            "q": settings.get("metrics_quantiles", 5),
            "min": settings.get("metrics_min_samples", 120),
            "maxi": settings.get("metrics_intraday_max_bars", 250000),
            "warmup": settings.get("metrics_warmup_bars", 400),
            "win": settings.get("development_window") or settings.get("dev_window"),
            "data": self._data_version(records),
            "symbols": list(self.symbols) if self.symbols else None,
            "scope": scope,
        }
        raw = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _new_calculator(self) -> FactorMetricsCalculator:
        # 每次计算新建计算器：干净的 K 线缓存，避免串扰
        return FactorMetricsCalculator(self.fetcher, self.config, symbols=self.symbols)

    def _load_record(self, factor_id: str) -> Optional[EvalFactorRecord]:
        return load_factor_record(factor_id, self.config)

    # ------------------------------------------------------------------
    # 单因子指标
    # ------------------------------------------------------------------
    def factor_metrics(self, factor_id: str, force: bool = False) -> Optional[dict[str, Any]]:
        """单因子评估指标。force=True 跳过缓存读强制重算（按原键覆盖落库）。"""
        rec = self._load_record(factor_id)
        if rec is None:
            return None
        return self._factor_metrics_with(rec, self._new_calculator(), force=force)

    def _factor_metrics_with(
        self, rec: EvalFactorRecord, calculator: FactorMetricsCalculator, force: bool = False
    ) -> dict[str, Any]:
        """factor_metrics 的计算内核：缓存查询 → 计算 → 写库 → 派生字段。"""
        factor_id = str(rec.factor_id)
        cache_key = self._cache_key({"factor_id": factor_id, "params": rec.params}, [rec])
        if self._cache_enabled() and not force:
            payload = self._cached_metrics(factor_id, cache_key)
            if payload is not None:
                return payload
        try:
            payload = calculator.factor_metrics(rec)
        except Exception as exc:
            logger.exception("因子评估指标计算失败: %s", factor_id)
            payload = {
                "factor_id": factor_id,
                "status": ERROR,
                "insufficient_samples": None,
                "failure_reason": f"指标计算异常：{type(exc).__name__}: {exc}",
                "computed_at": _now_iso(),
                "cache_hit": False,
            }
            self._attach_qualification(payload, rec)
            return payload
        if self._cache_enabled() and payload.get("status") != ERROR:
            try:
                # 写键在计算之后重取：计算期间的增量拉取会改变数据版本，
                # 以写时状态为准，后续读取（同状态）才能命中。
                cache_key = self._cache_key(
                    {"factor_id": factor_id, "params": rec.params}, [rec]
                )
                self.eval_store.write_payload(
                    "eval_metrics_cache",
                    factor_id,
                    cache_key,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
            except Exception:
                logger.warning("评估指标缓存写入失败: %s", factor_id, exc_info=True)
        payload["cache_hit"] = False
        self._attach_qualification(payload, rec)
        return payload

    def factor_metrics_many(
        self,
        factor_ids: list[str],
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, Optional[dict[str, Any]]]:
        """批量评估：复用同一个计算器逐因子计算（K 线帧缓存跨因子共享）。

        因子序列缓存是因子级的，每因子算完 reset_series_cache() 清掉以控制
        内存。返回 {factor_id: payload}，未注册因子为 None。
        """
        calculator = self._new_calculator()
        results: dict[str, Optional[dict[str, Any]]] = {}
        total = len(factor_ids)
        for index, factor_id in enumerate(factor_ids, 1):
            if on_progress is not None:
                on_progress(index, total, factor_id)
            rec = self._load_record(factor_id)
            if rec is None:
                results[factor_id] = None
                continue
            try:
                results[factor_id] = self._factor_metrics_with(rec, calculator)
            finally:
                calculator.reset_series_cache()
        return results

    def _decode_cached_payload(self, factor_id: str, raw: str) -> dict[str, Any]:
        payload = json.loads(raw)
        payload["cache_hit"] = True
        rec = self._load_record(factor_id)
        self._attach_qualification(payload, rec)
        return payload

    def _cached_metrics(self, factor_id: str, cache_key: Optional[str] = None) -> Optional[dict[str, Any]]:
        """只读缓存查询：命中返回（补齐派生字段后的）payload，未命中返回 None。

        绝不触发重算——qualification 汇总只消费已缓存的 metrics，
        未缓存因子由调用方标 not_evaluated。
        """
        if not self._cache_enabled():
            return None
        if cache_key is None:
            rec = self._load_record(factor_id)
            scope: dict[str, Any] = {"factor_id": factor_id}
            if rec is not None:
                scope["params"] = rec.params
            cache_key = self._cache_key(scope, [rec] if rec is not None else None)
        raw = self.eval_store.read_payload("eval_metrics_cache", factor_id, cache_key)
        if raw is None:
            return None
        return self._decode_cached_payload(factor_id, raw)

    # ------------------------------------------------------------------
    # 合格判定（qualification gating）
    # ------------------------------------------------------------------
    def _qualification_rules(self) -> dict[str, Any]:
        """读取 config 的 bias_control.qualification_rules，缺失键用默认阈值补齐。"""
        raw = self._settings().get("qualification_rules")
        merged: dict[str, Any] = {
            "library": {k: dict(v) for k, v in _DEFAULT_QUALIFICATION_RULES["library"].items()},
            "default": dict(_DEFAULT_QUALIFICATION_RULES["default"]),
            "sensitivity_base": _DEFAULT_QUALIFICATION_RULES["sensitivity_base"],
            "sensitivity": {k: list(v) for k, v in _DEFAULT_QUALIFICATION_RULES["sensitivity"].items()},
        }
        if isinstance(raw, dict):
            default = raw.get("default")
            if isinstance(default, dict):
                merged["default"].update({k: default[k] for k in _RULE_KEYS if k in default})
            library = raw.get("library")
            if isinstance(library, dict):
                for prefix, rules in library.items():
                    if not isinstance(rules, dict):
                        continue
                    # 未知前缀以 default 规则为底补齐缺失键，防 KeyError
                    base = merged["library"].get(str(prefix)) or merged["default"]
                    merged["library"][str(prefix)] = {**base, **{k: rules[k] for k in _RULE_KEYS if k in rules}}
            if raw.get("sensitivity_base"):
                merged["sensitivity_base"] = str(raw["sensitivity_base"])
            sensitivity = raw.get("sensitivity")
            if isinstance(sensitivity, dict):
                for key, values in sensitivity.items():
                    if isinstance(values, (list, tuple)) and values:
                        merged["sensitivity"][str(key)] = [float(v) for v in values]
        return merged

    def _rules_for(self, factor_id: Optional[str], name: Optional[str] = None) -> tuple[str, dict[str, Any]]:
        """按前缀匹配库特化阈值；返回 (库标签, 阈值字典)。先匹配先生效。"""
        rules_cfg = self._qualification_rules()

        def normalize(value: Optional[str]) -> str:
            return str(value or "").lower().replace("-", "_")

        candidates = [normalize(name), normalize(factor_id)]
        for prefix, rules in rules_cfg["library"].items():
            key = normalize(prefix)
            if any(candidate.startswith(key) for candidate in candidates if candidate):
                return str(prefix), dict(rules)
        return "default", dict(rules_cfg["default"])

    @staticmethod
    def _p_value_from(rank_ic: Optional[float], sample_count: Optional[int]) -> Optional[float]:
        """RankIC 显著性的 t 近似双侧 p 值（口径见模块 docstring，不引 scipy）。"""
        if rank_ic is None or sample_count is None or sample_count < 10:
            return None
        r = abs(float(rank_ic))
        if r >= 1.0:
            return 0.0
        t = r * math.sqrt((sample_count - 2) / (1.0 - r * r))
        # 大样本下 t 分布退化为标准正态：双侧 p = erfc(t/√2)
        return _number(math.erfc(t / math.sqrt(2.0)))

    @staticmethod
    def _decay_ratio_from(ic_decay: Any, primary_horizon: Optional[int]) -> Optional[float]:
        """5×主horizon 与主 horizon 的 RankIC 比值（口径与防护见模块 docstring）。"""
        rows = [row for row in (ic_decay or []) if isinstance(row, dict)]
        if not rows:
            return None
        first = rows[0]
        second = None
        if primary_horizon is not None:
            target = int(primary_horizon) * 5
            second = next((row for row in rows if row.get("horizon") == target), None)
        if second is None and len(rows) > 1:
            second = rows[1]
        if second is None:
            return None
        ic1 = first.get("rank_ic")
        ic5 = second.get("rank_ic")
        if ic1 is None or abs(ic1) <= 1e-6 or ic5 is None:
            return None
        if ic1 * ic5 < 0:
            # 反号：因子方向随 horizon 翻转，视为完全衰减
            return 0.0
        return _number(abs(ic5 / ic1))

    @staticmethod
    def _is_monotonic_from(quantile_test: Any) -> bool:
        """分层单调性：组均收益严格单调（递增或递减）→ True；否则
        |组序-组均收益 Spearman| > 0.6 → True（作用于跨 symbol 汇总结果）。"""
        buckets = (quantile_test or {}).get("buckets") or []
        means = [row.get("mean_return") for row in buckets if row.get("mean_return") is not None]
        if len(means) < 3:
            return False
        if all(means[i] <= means[i + 1] for i in range(len(means) - 1)):
            return True
        if all(means[i] >= means[i + 1] for i in range(len(means) - 1)):
            return True
        monotonicity = (quantile_test or {}).get("monotonicity")
        return monotonicity is not None and abs(monotonicity) > 0.6

    def evaluate_qualification(
        self,
        payload: dict[str, Any],
        rules: Optional[dict[str, Any]] = None,
        factor_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """对单个 metrics payload 做合格判定（六项全过才合格，收集全部原因）。"""
        if rules is None:
            _, rules = self._rules_for(payload.get("factor_id"), factor_name)
        status = payload.get("status")
        rank_ic = payload.get("rank_ic")
        p_value = payload.get("p_value")
        if p_value is None:
            p_value = self._p_value_from(rank_ic, payload.get("sample_count"))
        decay_ratio = payload.get("decay_ratio")
        if decay_ratio is None:
            decay_ratio = self._decay_ratio_from(payload.get("ic_decay"), payload.get("primary_horizon"))
        is_monotonic = self._is_monotonic_from(payload.get("quantile_test"))
        long_short = (payload.get("quantile_test") or {}).get("long_short")

        reasons: list[str] = []
        if status not in (OK, PARTIAL) or rank_ic is None:
            reasons.append(f"指标不可用（status={status}）")
        else:
            if abs(rank_ic) < rules["ic_min"]:
                reasons.append(f"IC({abs(rank_ic):.4f}) < {rules['ic_min']}")
            # 源逻辑 p 缺失按 1 处理 → 不显著
            if p_value is None or p_value >= rules.get("p_max", 0.05):
                reasons.append(f"p值({p_value if p_value is not None else 1:.4f}) >= {rules.get('p_max', 0.05)}")
            icir = payload.get("icir")
            if icir is None or abs(icir) < rules["icir_min"]:
                reasons.append(f"ICIR({icir if icir is not None else 0:.4f}) < {rules['icir_min']}")
            if not is_monotonic:
                reasons.append("分层不单调")
            # 源逻辑多空差缺失按 0 处理 → 过小
            if long_short is None or abs(long_short) < rules.get("long_short_min", 0.001):
                reasons.append(f"多空差({long_short if long_short is not None else 0:.4f})过小")
            # 源逻辑 decay_ratio 为 NaN 时跳过衰减检查
            if decay_ratio is not None and decay_ratio < rules["decay_min_ratio"]:
                reasons.append(f"衰减过快 ({decay_ratio:.4f} < {rules['decay_min_ratio']})")
        return {
            "qualified": not reasons,
            "reasons": reasons or ["通过"],
            "rules_used": dict(rules),
            "p_value": p_value,
            "decay_ratio": decay_ratio,
            "is_monotonic": is_monotonic,
        }

    def _attach_qualification(self, payload: dict[str, Any], rec: Any) -> None:
        """在返回前补齐派生字段：p_value / decay_ratio / qualification 块。

        派生字段由指标结果确定性推出，不写缓存——阈值调整即时生效。
        """
        factor_name = str(getattr(rec, "name", "") or "") if rec is not None else ""
        if "p_value" not in payload:
            payload["p_value"] = self._p_value_from(payload.get("rank_ic"), payload.get("sample_count"))
        if "decay_ratio" not in payload:
            payload["decay_ratio"] = self._decay_ratio_from(
                payload.get("ic_decay"), payload.get("primary_horizon")
            )
        payload["qualification"] = self.evaluate_qualification(payload, factor_name=factor_name)

    def qualification_state(self, factor_id: str) -> Optional[bool]:
        """只读缓存的合格状态：True 合格 / False 不合格 / None 未评估。"""
        payload = self._cached_metrics(factor_id)
        if payload is None:
            return None
        qualification = payload.get("qualification")
        if not isinstance(qualification, dict):
            qualification = self.evaluate_qualification(payload)
        return bool(qualification.get("qualified"))

    # ------------------------------------------------------------------
    # 全库合格判定汇总（只消费缓存，不现场重算；refresh 限量补算）
    # ------------------------------------------------------------------
    def _factor_roster(self) -> list[dict[str, Any]]:
        """全库因子名册（factor_id/name/status/registered）。"""
        roster: list[dict[str, Any]] = []
        try:
            from superplatform.factors.dual_registry import DualFactorRegistry

            dual = DualFactorRegistry.get_instance()
            dual.ensure_scanned()
            for row in dual.list_factors():
                roster.append({
                    "factor_id": str(row["factor_id"]) if row.get("factor_id") else str(row.get("name") or ""),
                    "name": str(row.get("name") or ""),
                    "status": row.get("status"),
                    "registered": bool(row.get("registered")),
                    "validation_errors": row.get("validation_errors") or [],
                })
        except Exception:
            logger.warning("双文件名册读取失败", exc_info=True)
        seen = {row["factor_id"] for row in roster}
        getter = getattr(self.config, "get", None)
        if callable(getter):
            for section in ("factors", "factor_instances"):
                for name in (getter(section) or {}):
                    if str(name) not in seen:
                        roster.append({
                            "factor_id": str(name), "name": str(name),
                            "status": None, "registered": True, "validation_errors": [],
                        })
        if not roster:
            rows = self.eval_store.all_cached("eval_metrics_cache")
            for value in sorted({str(r["factor_id"]) for r in rows}):
                roster.append({
                    "factor_id": value, "name": "", "status": None,
                    "registered": True, "validation_errors": [],
                })
        return roster

    def _qualification_entry(self, roster_row: dict[str, Any], payload: Optional[dict[str, Any]]) -> dict[str, Any]:
        """汇总条目：payload 为 None 表示未缓存（not_evaluated，绝不触发重算）。"""
        entry: dict[str, Any] = {
            "factor_id": roster_row["factor_id"],
            "name": roster_row.get("name") or "",
            "factor_status": roster_row.get("status"),
            # 成功导入判定：注册表校验是否通过（与指标合格判定正交）
            "registered": bool(roster_row.get("registered", True)),
            "import_errors": [
                str(e.get("message", e)) if isinstance(e, dict) else str(e)
                for e in (roster_row.get("validation_errors") or [])
            ],
        }
        if payload is None:
            entry.update({"evaluation": "not_evaluated", "qualified": None})
            return entry
        _, rules = self._rules_for(entry["factor_id"], entry["name"])
        qualification = self.evaluate_qualification(payload, rules=rules)
        quantile = payload.get("quantile_test") or {}
        entry.update({
            "evaluation": "evaluated",
            "qualified": qualification["qualified"],
            "reasons": qualification["reasons"],
            "rules_used": qualification["rules_used"],
            "rank_ic": payload.get("rank_ic"),
            "p_value": qualification["p_value"],
            "icir": payload.get("icir"),
            "decay_ratio": qualification["decay_ratio"],
            "is_monotonic": qualification["is_monotonic"],
            "long_short": quantile.get("long_short"),
            "turnover": payload.get("turnover"),
            "metrics_status": payload.get("status"),
            "metrics_computed_at": payload.get("computed_at"),
        })
        return entry

    def qualification_summary(self, refresh: bool = False, refresh_limit: int = 20) -> dict[str, Any]:
        """全库合格判定汇总：默认只读缓存；refresh=True 时为最多 refresh_limit 个
        未缓存因子同步补算（经 factor_metrics 计算并落缓存）。"""
        started = time.perf_counter()
        # 缓存批量预载（一次取全表）：逐因子查询在因子多时线性放大
        cache_map: dict[tuple[str, str], str] = {}
        if self._cache_enabled():
            try:
                cache_map = {
                    (str(r["factor_id"]), str(r["cache_key"])): str(r["payload_json"])
                    for r in self.eval_store.all_cached("eval_metrics_cache")
                }
            except Exception:
                logger.warning("评估指标缓存批量预载失败，退化为逐因子查询", exc_info=True)
        entries: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for roster_row in self._factor_roster():
            factor_id = roster_row["factor_id"]
            payload: Optional[dict[str, Any]] = None
            rec = self._load_record(factor_id)
            scope: dict[str, Any] = {"factor_id": factor_id}
            if rec is not None:
                scope["params"] = rec.params
            raw = cache_map.get((factor_id, self._cache_key(scope, [rec] if rec is not None else None)))
            if raw is not None:
                payload = self._decode_cached_payload(factor_id, raw)
            if payload is None:
                missing.append(roster_row)
            entries.append(self._qualification_entry(roster_row, payload))
        refreshed: list[str] = []
        if refresh and missing:
            for roster_row in missing[: max(1, int(refresh_limit))]:
                payload = self.factor_metrics(roster_row["factor_id"])
                if payload is None:
                    continue
                refreshed.append(roster_row["factor_id"])
                for index, entry in enumerate(entries):
                    if entry["factor_id"] == roster_row["factor_id"]:
                        entries[index] = self._qualification_entry(roster_row, payload)
                        break
        evaluated = [entry for entry in entries if entry["evaluation"] == "evaluated"]
        qualified = [entry for entry in evaluated if entry["qualified"]]
        return _json_safe({
            "cache_only": True,
            "note": (
                "只消费已缓存的评估指标，不现场重算；未缓存因子标 not_evaluated。"
                "refresh=True 时对最多 20 个未缓存因子同步补算。"
                "逐因子按库特化阈值判定（见 rules_used）。"
            ),
            "summary": {
                "total": len(entries),
                "evaluated": len(evaluated),
                "qualified": len(qualified),
                "unqualified": len(evaluated) - len(qualified),
                "not_evaluated": len(entries) - len(evaluated),
                "refreshed": len(refreshed),
            },
            "rules": self._qualification_rules(),
            "refreshed_ids": refreshed,
            "entries": entries,
            "computed_at": _now_iso(),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })

    # ------------------------------------------------------------------
    # 相关性矩阵（服务层：注册表枚举 + 缓存）
    # ------------------------------------------------------------------
    def correlation_matrix(self, factor_ids: Optional[list[str]] = None) -> dict[str, Any]:
        if factor_ids:
            records = []
            excluded: list[dict[str, str]] = []
            for factor_id in factor_ids:
                rec = self._load_record(factor_id)
                if rec is None:
                    excluded.append({"factor_id": factor_id, "reason": "因子未注册"})
                else:
                    records.append(rec)
        else:
            records = list_factor_records(self.config)
            excluded = []
        cache_key = self._cache_key(
            {"correlation": sorted(str(r.factor_id) for r in records),
             "matrix_rev": _CORR_MATRIX_REVISION},
            records,
        )
        if self._cache_enabled():
            raw = self.eval_store.read_corr(cache_key)
            if raw is not None:
                payload = json.loads(raw)
                payload["cache_hit"] = True
                return payload
        payload = self._new_calculator().correlation_matrix(records)
        payload["excluded"] = excluded + payload.get("excluded", [])
        if self._cache_enabled():
            try:
                self.eval_store.write_corr(
                    cache_key, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
            except Exception:
                logger.warning("相关性矩阵缓存写入失败", exc_info=True)
        payload["cache_hit"] = False
        return payload

    # ------------------------------------------------------------------
    # CSV 导出（带 BOM，与偏差控制报告一致）
    # ------------------------------------------------------------------
    @staticmethod
    def metrics_csv(payload: dict[str, Any]) -> str:
        """长表格式：section,metric,horizon,bucket,symbol,value,sample_count。"""
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["section", "metric", "horizon", "bucket", "symbol", "value", "sample_count"])
        window = payload.get("window") or {}
        meta = {
            "factor_id": payload.get("factor_id"),
            "status": payload.get("status"),
            "frequency": payload.get("frequency"),
            "window_start": window.get("start"),
            "window_end": window.get("end"),
            "sample_count": payload.get("sample_count"),
            "elapsed_ms": payload.get("elapsed_ms"),
            "computed_at": payload.get("computed_at"),
            "failure_reason": payload.get("failure_reason"),
        }
        for key, value in meta.items():
            writer.writerow(["meta", key, "", "", "", value, ""])
        primary = payload.get("primary_horizon")
        sample_count = payload.get("sample_count")
        for key in ("ic", "rank_ic", "icir", "turnover"):
            writer.writerow(["summary", key, primary if key in ("ic", "rank_ic") else "", "", _ALL, payload.get(key), sample_count])
        # 合格判定派生字段（t 近似 p 值、5×主horizon/主horizon 衰减比、六项判定结果）
        writer.writerow(["summary", "p_value", primary, "", _ALL, payload.get("p_value"), sample_count])
        writer.writerow(["summary", "decay_ratio", "", "", _ALL, payload.get("decay_ratio"), sample_count])
        qualification = payload.get("qualification") or {}
        writer.writerow(["summary", "qualified", "", "", _ALL, qualification.get("qualified"), sample_count])
        writer.writerow(["summary", "qualification_reasons", "", "", _ALL, "; ".join(qualification.get("reasons") or []), sample_count])
        for row in payload.get("ic_decay") or []:
            writer.writerow(["ic_decay", "ic", row.get("horizon"), "", _ALL, row.get("ic"), row.get("sample_count")])
            writer.writerow(["ic_decay", "rank_ic", row.get("horizon"), "", _ALL, row.get("rank_ic"), row.get("sample_count")])
        quantile = payload.get("quantile_test") or {}
        for row in quantile.get("buckets") or []:
            writer.writerow(["quantile", "mean_return", quantile.get("horizon"), row.get("bucket"), _ALL, row.get("mean_return"), row.get("sample_count")])
        writer.writerow(["quantile", "monotonicity", quantile.get("horizon"), "", _ALL, quantile.get("monotonicity"), sample_count])
        writer.writerow(["quantile", "long_short", quantile.get("horizon"), "", _ALL, quantile.get("long_short"), sample_count])
        rolling = payload.get("rolling") or {}
        for key in ("mean", "std", "positive_ratio", "count"):
            writer.writerow(["rolling", key, "", "", _ALL, rolling.get(key), rolling.get("count")])
        for symbol_row in payload.get("symbols") or []:
            symbol = symbol_row.get("symbol")
            writer.writerow(["symbol", "ic", primary, "", symbol, symbol_row.get("ic"), symbol_row.get("sample_count")])
            writer.writerow(["symbol", "rank_ic", primary, "", symbol, symbol_row.get("rank_ic"), symbol_row.get("sample_count")])
            writer.writerow(["symbol", "turnover", "", "", symbol, symbol_row.get("turnover"), symbol_row.get("sample_count")])
            symbol_rolling = symbol_row.get("rolling") or {}
            for key in ("mean", "std", "positive_ratio", "count"):
                writer.writerow(["symbol_rolling", key, "", "", symbol, symbol_rolling.get(key), symbol_rolling.get("count")])
        return "﻿" + buffer.getvalue()

    @staticmethod
    def qualification_csv(payload: dict[str, Any]) -> str:
        """合格判定汇总 CSV：每因子一行。"""
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "factor_id", "name", "evaluation", "qualified", "reasons",
            "rank_ic", "p_value", "icir", "decay_ratio", "is_monotonic",
            "long_short", "turnover", "metrics_status", "metrics_computed_at",
        ])
        for entry in payload.get("entries") or []:
            writer.writerow([
                entry.get("factor_id"), entry.get("name"), entry.get("evaluation"),
                entry.get("qualified"), "; ".join(entry.get("reasons") or []),
                entry.get("rank_ic"), entry.get("p_value"), entry.get("icir"),
                entry.get("decay_ratio"), entry.get("is_monotonic"),
                entry.get("long_short"), entry.get("turnover"),
                entry.get("metrics_status"), entry.get("metrics_computed_at"),
            ])
        return "﻿" + buffer.getvalue()

    @staticmethod
    def correlation_csv(payload: dict[str, Any]) -> str:
        """矩阵格式：首列 factor_id，首行因子 ID 表头。"""
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        factor_ids = payload.get("factor_ids") or []
        writer.writerow(["factor_id", *factor_ids])
        matrix = payload.get("matrix") or []
        for index, factor_id in enumerate(factor_ids):
            row = matrix[index] if index < len(matrix) else []
            writer.writerow([factor_id, *["" if value is None else value for value in row]])
        return "﻿" + buffer.getvalue()
