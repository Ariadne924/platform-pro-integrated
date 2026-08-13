"""因子评级（rating）—— 近 N 天快速打分 + S~D 综合评级 + 评级榜（04 阶段）。

纯函数部分（compute_factor_metrics / grade_of 等）逐行移植自 sim_platform
`app/factors/rating.py`；服务层（RatingService）把源项目 routes_admin 里的
取数/聚合逻辑移植到本平台的 DataProvider + 双文件注册中心上。

与源项目的差异：

* 源项目从 `factor_value` 落库缓存读因子值；本平台无落库因子值历史，
  评级窗口内的因子值按注册实现 + K 线重算（含 lookback warmup），
  与 metrics/六查同一口径；
* 源项目固定 1m 粒度（horizon=60 根 1m bar、bars_per_year=525600）；
  这里按因子 MD 声明频率取 bar 序列，horizon/bars_per_year 均按频率换算
  （1h 因子默认 horizon=24 根 ≈ 1 日，年化 8760）；
* 样本不足/全部标的不可算时返回 status="insufficient" 且 grade=None——
  不给假数字（源项目在聚合层放一个占位 "D"，这里按任务书死规矩改为
  不出级）；横截面/funding/OI/mark_price 依赖因子返回 "not_supported"；
* 结果写 DuckDB 缓存（eval_rating_cache，(factor_id, 数据版本) 键控），
  替代源项目的 10 分钟内存 TTL。

评级阈值依据（源模块 docstring）：经典量价因子 RankIC 0.02~0.05 为可用区间
（|RankIC|>=0.02 有预测力，>=0.05 强预测力；ICIR>=0.3/0.5 稳定性中等/良好；
|Sharpe| 1.0/1.5 信号回测可交易/优秀）。反向因子同样有预测力，故评级一律用
|RankIC| / |ICIR| / |Sharpe|。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from superplatform.evaluation.bias import (
    UNSUPPORTED_INPUTS,
    EvalCacheStore,
    EvalFactorRecord,
    KlineFetcher,
    _json_safe,
    _now_iso,
    bar_delta,
    factor_direction,
    list_factor_records,
    load_factor_record,
)

logger = logging.getLogger("superplatform.evaluation.rating")

# ---------------------------------------------------------------
# 模块级常量（阈值依据见模块 docstring）
# ---------------------------------------------------------------

#: 有效样本点下限，低于此值判定 insufficient（样本不足，指标不可靠）
MIN_SAMPLES: int = 50

#: IC 分块大小（每 240 根 bar 为一块，计算块内 IC）
IC_BLOCK: int = 240

#: 块内最少有效点数（低于此值该块跳过，避免小块 IC 噪声）
IC_BLOCK_MIN: int = 20

#: 单边手续费率（仓位翻转一次 = 一卖一买，双边共扣 2 * FEE_PER_SIDE = 0.001）
FEE_PER_SIDE: float = 0.0005

#: 评级阈值：S 级
GRADE_S_RANK_IC: float = 0.05
GRADE_S_ICIR: float = 0.5
GRADE_S_SHARPE: float = 1.5
#: 评级阈值：A 级
GRADE_A_RANK_IC: float = 0.03
GRADE_A_ICIR: float = 0.3
GRADE_A_SHARPE: float = 1.0
#: 评级阈值：B 级
GRADE_B_RANK_IC: float = 0.02
GRADE_B_SHARPE: float = 0.5
#: 评级阈值：C 级
GRADE_C_RANK_IC: float = 0.01

#: 各频率每年的 bar 数（7×24 市场），信号回测年化用
_BARS_PER_YEAR: dict[str, int] = {
    "1m": 525_600, "5m": 105_120, "15m": 35_040, "30m": 17_520,
    "1h": 8_760, "4h": 2_190, "8h": 1_095, "1d": 365, "1w": 52,
}

#: 聚合时做跨 symbol 均值的数值字段
_RATING_MEAN_KEYS = (
    "ic_mean", "ic_std", "rank_ic_mean", "icir", "ic_positive_ratio",
    "total_return_pct", "annualized_return_pct", "max_drawdown_pct",
    "sharpe", "calmar", "win_rate", "turnover", "avg_hold_bars",
    "coverage", "span_days",
)


def bars_per_year(frequency: str) -> int:
    return _BARS_PER_YEAR.get(str(frequency or "1d"), 365)


# ---------------------------------------------------------------
# 综合评级
# ---------------------------------------------------------------

def grade_of(
    rank_ic_mean: Optional[float],
    icir: Optional[float],
    sharpe: Optional[float],
) -> str:
    """按规则给出综合评级 S/A/B/C/D。

    反向因子同样有预测力，故 RankIC / ICIR 一律取绝对值；
    Sharpe 同样取绝对值：同一信号反转持仓后 Sharpe 恰为原值的相反数
    （每期毛收益取负、换手成本不变），|Sharpe| 即「两个方向取优」的回测水平。
    任一指标缺失（None）按 0 处理，即天然掉级。
    """
    if rank_ic_mean is None:
        return "D"
    a = abs(rank_ic_mean)
    i = abs(icir) if icir is not None and math.isfinite(icir) else 0.0
    s = abs(sharpe) if sharpe is not None and math.isfinite(sharpe) else 0.0
    if a >= GRADE_S_RANK_IC and i >= GRADE_S_ICIR and s >= GRADE_S_SHARPE:
        return "S"
    if a >= GRADE_A_RANK_IC and i >= GRADE_A_ICIR and s >= GRADE_A_SHARPE:
        return "A"
    if a >= GRADE_B_RANK_IC and s >= GRADE_B_SHARPE:
        return "B"
    if a >= GRADE_C_RANK_IC:
        return "C"
    return "D"


# ---------------------------------------------------------------
# 内部：预测力指标（IC 类）
# ---------------------------------------------------------------

def _ic_metrics(factor: pd.Series, close: pd.Series, horizon: int) -> dict[str, Any]:
    """滚动分块 IC：因子值 vs horizon 期前瞻收益，分块计算 Pearson / Spearman。"""
    fwd = close.shift(-horizon) / close - 1.0
    mask = fwd.notna()
    fv = factor[mask]
    rv = fwd[mask]
    ics: list[float] = []
    rics: list[float] = []
    for start in range(0, len(fv), IC_BLOCK):
        f_blk = fv.iloc[start:start + IC_BLOCK]
        r_blk = rv.iloc[start:start + IC_BLOCK]
        if len(f_blk) < IC_BLOCK_MIN or f_blk.nunique() < 2 or r_blk.nunique() < 2:
            continue
        ics.append(float(f_blk.corr(r_blk)))                      # Pearson
        rics.append(float(f_blk.corr(r_blk, method="spearman")))  # Spearman
    if not ics:
        return {
            "ic_mean": None, "ic_std": None, "rank_ic_mean": None,
            "icir": None, "ic_positive_ratio": None, "ic_blocks": 0,
        }
    ic_arr = np.asarray(ics)
    ric_arr = np.asarray(rics)
    ic_mean = float(ic_arr.mean())
    ic_std = float(ic_arr.std(ddof=1)) if len(ic_arr) > 1 else 0.0
    icir = (ic_mean / ic_std) if ic_std > 0 else None
    return {
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "rank_ic_mean": float(ric_arr.mean()),
        "icir": icir,
        "ic_positive_ratio": float((ic_arr > 0).mean()),
        "ic_blocks": len(ics),
    }


# ---------------------------------------------------------------
# 内部：信号回测指标（向量化，±1 仓位）
# ---------------------------------------------------------------

def _signal_backtest(
    factor: pd.Series,
    close: pd.Series,
    bullish_high: bool,
    horizon: int,
    bars_per_year: int,
) -> dict[str, Any]:
    """因子值 > 滚动中位数则按方向持仓（±1），扣换手成本后统计净值指标。"""
    win = horizon * 4
    med = factor.rolling(win, min_periods=max(2, win // 2)).median()
    ok = med.notna()
    if ok.sum() < 2:
        return _empty_backtest()
    # 仓位 ∈ {+1, -1}（无空仓，简化）；方向语义：值大是否看多
    pos = pd.Series(np.where(factor[ok] > med[ok], 1.0, -1.0), index=factor.index[ok])
    if not bullish_high:
        pos = -pos
    ret = close.pct_change()
    strat = pos.shift(1) * ret
    # 仓位翻转一次扣双边费用（首个有效仓位视为建仓，同样扣一次）
    flip = (pos != pos.shift(1)).astype(float)
    flip.iloc[0] = 1.0
    cost = flip * (2.0 * FEE_PER_SIDE)
    net = (strat - cost).dropna()
    if len(net) < 2:
        return _empty_backtest()

    equity = (1.0 + net).cumprod()
    total = float(equity.iloc[-1] - 1.0)
    n = len(net)
    # 年化：复利外推，净值接近清零时兜底为 -100%
    if total <= -0.9999:
        ann = -1.0
    else:
        ann = float((1.0 + total) ** (bars_per_year / n) - 1.0)
    dd = equity / equity.cummax() - 1.0
    max_dd = float(dd.min())
    mu = float(net.mean())
    sigma = float(net.std(ddof=1))
    sharpe = (mu / sigma * math.sqrt(bars_per_year)) if sigma > 0 else None
    calmar = (ann / abs(max_dd)) if max_dd < 0 else None
    trades = int(flip.loc[net.index].sum())
    win_rate = float((net > 0).mean())  # 仓位恒为 ±1，全部 bar 均在仓
    return {
        "total_return_pct": total * 100.0,
        "annualized_return_pct": ann * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
        "sharpe": sharpe,
        "calmar": calmar,
        "win_rate": win_rate,
        "trades": trades,
        "turnover": trades / n if n > 0 else None,
        "avg_hold_bars": (n / trades) if trades > 0 else None,
        "n_bars": n,
    }


def _empty_backtest() -> dict[str, Any]:
    """回测无法计算（样本过少）时的空指标占位。"""
    return {
        "total_return_pct": None, "annualized_return_pct": None,
        "max_drawdown_pct": None, "sharpe": None, "calmar": None,
        "win_rate": None, "trades": 0, "turnover": None,
        "avg_hold_bars": None, "n_bars": 0,
    }


# ---------------------------------------------------------------
# 公开主函数（纯函数）
# ---------------------------------------------------------------

def compute_factor_metrics(
    factor: pd.Series,
    close: pd.Series,
    bullish_high: bool,
    horizon: int = 24,
    bars_per_year: int = 8760,
) -> dict[str, Any]:
    """计算单个标的上某因子的完整评测指标。

    :param factor: 因子值序列（DatetimeIndex，允许含 NaN，覆盖率据此统计）
    :param close: 收盘价序列（DatetimeIndex，与 factor 时间轴大致同域）
    :param bullish_high: 方向语义，True 表示「因子值大看多」
    :param horizon: 前瞻收益期数（bar 数）
    :param bars_per_year: 年化基准（按因子频率，见 bars_per_year()）
    :return: 四组指标 + grade；样本不足时 insufficient=True 且预测力/回测指标为 None
    """
    df = pd.concat({"f": factor, "c": close}, axis=1).sort_index()
    df = df.dropna(subset=["c"])  # 无价格的 bar 无法评估，直接剔除
    f_raw = df["f"]

    # C. 数据质量（基于剔除无价格 bar 后的对齐视图）
    n_total = len(df)
    n_valid = int(f_raw.notna().sum())
    coverage = (n_valid / n_total) if n_total > 0 else 0.0
    if n_total > 0:
        span_days = max(0.0, (df.index[-1] - df.index[0]).total_seconds() / 86400.0)
        last_ts = df.index[-1].isoformat()
    else:
        span_days = 0.0
        last_ts = None
    data_quality = {
        "coverage": coverage,
        "n_samples": n_valid,
        "span_days": span_days,
        "last_ts": last_ts,
    }

    d = df.dropna()
    if len(d) < MIN_SAMPLES or len(d) <= horizon + 5:
        # 样本不足：评级无意义，返回占位
        return {
            "insufficient": True,
            "reason": f"有效样本 {len(d)} < {MIN_SAMPLES}（或不足以覆盖 horizon={horizon}）",
            "ic_mean": None, "ic_std": None, "rank_ic_mean": None,
            "icir": None, "ic_positive_ratio": None, "ic_blocks": 0,
            **_empty_backtest(),
            **data_quality,
            "grade": "D",
        }

    # A. 预测力 + B. 信号回测 + D. 评级
    ic = _ic_metrics(d["f"], d["c"], horizon)
    bt = _signal_backtest(d["f"], d["c"], bullish_high, horizon, bars_per_year)
    grade = grade_of(ic["rank_ic_mean"], ic["icir"], bt["sharpe"])
    return {
        "insufficient": False,
        "reason": None,
        **ic,
        **bt,
        **data_quality,
        "grade": grade,
    }


# ---------------------------------------------------------------
# 服务层：取数 + 重算 + 聚合 + 缓存 + 评级榜
# ---------------------------------------------------------------


class RatingService:
    """因子评级服务（04 服务层，供 CLI 与 05 的 API 映射调用）。

    全部方法返回 JSON 可序列化 dict。非线程安全：同一时间只跑一个
    计算调用（DuckDB 缓存为单写者）。
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
        getter = getattr(config, "get", None)
        self._symbols_override = symbols
        self._settings = dict(getter("bias_control", {}) or {}) if callable(getter) else {}

    # ------------------------------------------------------------------
    # 缓存键：(factor_id, 评级参数, 数据版本)
    # ------------------------------------------------------------------
    def _data_version(self, rec: EvalFactorRecord, symbols: list[str]) -> str:
        """评级窗口的数据版本：kline 缓存表内相关 symbol 的首末日期 + 行数。

        评级窗口是「最近 N 天」，末日期不封顶——新数据到达即失效重算。
        """
        from superplatform.data.store import provider_table

        getter = getattr(self.config, "get", None)
        exchange = getter("defaults.exchange", "binance") if callable(getter) else "binance"
        market = getter("defaults.market", "perpetual") if callable(getter) else "perpetual"
        pid = f"{exchange}-{'perp' if market == 'perpetual' else 'spot'}-kline"
        table = provider_table(pid)
        parts: list[str] = []
        try:
            for row in self.eval_store.series_bounds(table, rec.frequency or "1d"):
                if row["symbol"] in symbols:
                    parts.append(f"{row['symbol']}@{row['s']}~{row['e']}#{row['c']}")
        except Exception:
            logger.warning("评级数据版本键计算失败，退化为空版本", exc_info=True)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _cache_key(self, rec: EvalFactorRecord, symbols: list[str], days: float, horizon: int) -> str:
        raw = json.dumps({
            "factor_id": rec.factor_id,
            "params": rec.params,
            "days": days,
            "horizon": horizon,
            "symbols": sorted(symbols),
            "data": self._data_version(rec, symbols),
        }, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    # ------------------------------------------------------------------
    # 单因子评级
    # ------------------------------------------------------------------
    def rate_factor(
        self,
        factor_id: str,
        days: Optional[float] = None,
        horizon: Optional[int] = None,
        symbols: Optional[list[str]] = None,
        refresh: bool = False,
    ) -> Optional[dict[str, Any]]:
        """单因子评级 payload；因子未注册返回 None。

        返回的 ``status``：``ok``（aggregate.grade 为 S~D）、
        ``insufficient``（样本不足，grade=None，不给假数字）、
        ``not_supported``（横截面/funding/OI/mark_price 依赖）。
        """
        rec = load_factor_record(factor_id, self.config)
        if rec is None:
            return None
        days = float(days if days is not None else self._settings.get("rating_days", 30))
        horizon = int(horizon if horizon is not None else self._settings.get("rating_horizon", 24))
        use_symbols = list(symbols or self._symbols_override or self._default_symbols())
        cache_key = self._cache_key(rec, use_symbols, days, horizon)
        if not refresh:
            raw = self.eval_store.read_payload("eval_rating_cache", factor_id, cache_key)
            if raw is not None:
                payload = json.loads(raw)
                payload["cache_hit"] = True
                return payload
        payload = self._compute_rating(rec, use_symbols, days, horizon)
        try:
            # 写键在计算之后重取：计算期间的增量拉取会改变数据版本，
            # 以写时状态为准，后续读取（同状态）才能命中。
            cache_key = self._cache_key(rec, use_symbols, days, horizon)
            self.eval_store.write_payload(
                "eval_rating_cache", factor_id, cache_key,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            logger.warning("评级缓存写入失败: %s", factor_id, exc_info=True)
        payload["cache_hit"] = False
        return payload

    def _default_symbols(self) -> list[str]:
        getter = getattr(self.config, "get", None)
        if callable(getter):
            return list(getter("data.symbols.perpetual", []) or [])
        return []

    def _compute_rating(
        self,
        rec: EvalFactorRecord,
        symbols: list[str],
        days: float,
        horizon: int,
    ) -> dict[str, Any]:
        """评级计算内核（移植 routes_admin._compute_factor_rating 的聚合逻辑）。

        因子值按注册实现重算：拉取 [end-days-warmup, end] 的 K 线，compute 后
        切片到评级窗口——与 metrics/六查同一重算口径。
        """
        direction_text, bullish_high = factor_direction(self.config, rec)
        frequency = rec.frequency or "1d"
        base: dict[str, Any] = {
            "ok": True,
            "factor_id": rec.factor_id,
            "name": rec.name,
            "status": rec.status,
            "category": rec.category,
            "frequency": frequency,
            "cross_sectional": bool(rec.cross_sectional),
            "direction_text": direction_text,
            "bullish_high": bullish_high,
            "eval_window": {"days": days, "horizon": horizon},
            "notes": [],
        }

        unsupported_inputs = sorted(UNSUPPORTED_INPUTS & set(rec.data_types or []))
        if rec.cross_sectional or unsupported_inputs:
            if rec.cross_sectional:
                base["notes"].append("横截面/多标的因子暂不支持单标的时序评级")
            if unsupported_inputs:
                base["notes"].append(
                    "依赖输入 " + ", ".join(unsupported_inputs) + " 无本地时序数据，暂不支持评级"
                )
            base["status"] = "not_supported"
            base["aggregate"] = {"not_supported": True, "grade": None}
            base["per_symbol"] = []
            return _json_safe(base)

        if rec.compute_fn is None:
            base["notes"].append("因子实现未加载，无法重算评级窗口内的因子值")
            base["status"] = "not_supported"
            base["aggregate"] = {"not_supported": True, "grade": None}
            base["per_symbol"] = []
            return _json_safe(base)

        if not symbols:
            base["notes"].append("标的研究池为空")
            base["status"] = "insufficient"
            base["aggregate"] = {"insufficient": True, "grade": None, "n_symbols": 0}
            base["per_symbol"] = []
            return _json_safe(base)

        now = pd.Timestamp.now(tz="UTC")
        window_start = now - pd.Timedelta(days=days)
        warmup_bars = max(int(rec.lookback_bars or 20) * 3, 240)
        fetch_start = window_start - bar_delta(frequency, warmup_bars)
        bpy = bars_per_year(frequency)

        per_symbol: list[dict[str, Any]] = []
        for sym in symbols:
            kline = self.fetcher.fetch_frame(sym, frequency, fetch_start, now)
            if kline.empty:
                per_symbol.append({
                    "symbol": sym,
                    "insufficient": True,
                    "reason": "K线数据为空",
                    "grade": None,
                })
                continue
            try:
                series = rec.compute_fn(
                    {"kline": kline, "symbol": sym}, dict(rec.params or {})
                )
            except Exception as exc:
                per_symbol.append({
                    "symbol": sym,
                    "insufficient": True,
                    "reason": f"因子重算异常 {type(exc).__name__}: {exc}",
                    "grade": None,
                })
                continue
            f_ser = series[series.index >= window_start] if not series.empty else series
            c_ser = pd.Series(
                pd.to_numeric(kline["close"], errors="coerce").to_numpy(dtype="float64"),
                index=pd.DatetimeIndex(kline.index),
            ).groupby(level=0).last().sort_index()
            c_ser = c_ser[c_ser.index >= window_start]
            m = compute_factor_metrics(
                f_ser, c_ser, bullish_high, horizon=horizon, bars_per_year=bpy
            )
            per_symbol.append({"symbol": sym, **m})

        # 聚合：各数值字段跨 symbol 取均值；样本数、交易次数取合计
        ok_rows = [r for r in per_symbol if not r.get("insufficient")]
        agg: dict[str, Any] = {"n_symbols": len(symbols), "n_symbols_ok": len(ok_rows)}
        for key in _RATING_MEAN_KEYS:
            vals = [r[key] for r in ok_rows if r.get(key) is not None]
            agg[key] = (sum(vals) / len(vals)) if vals else None
        agg["n_samples"] = sum(int(r.get("n_samples") or 0) for r in ok_rows)
        agg["trades"] = sum(int(r.get("trades") or 0) for r in ok_rows)
        agg["last_ts"] = max((r.get("last_ts") for r in ok_rows if r.get("last_ts")), default=None)
        if not ok_rows:
            # 死规矩：样本不足返回 insufficient，不出级、不给假数字
            agg["insufficient"] = True
            agg["grade"] = None
            base["status"] = "insufficient"
            base["notes"].append(
                f"所有标的中有效样本均不足（<{MIN_SAMPLES} 点），无法评级"
            )
        else:
            agg["insufficient"] = False
            agg["grade"] = grade_of(agg["rank_ic_mean"], agg["icir"], agg["sharpe"])
            base["status"] = "ok"
            if len(ok_rows) < len(symbols):
                base["notes"].append(
                    f"{len(symbols) - len(ok_rows)} 个标的样本不足，聚合仅基于 {len(ok_rows)} 个标的"
                )
        base["notes"].append(
            "评级基于 |RankIC| / |ICIR| / |Sharpe|：反向因子同样有预测力，方向仅用于信号回测持仓方向"
        )
        base["notes"].append(
            f"指标按因子声明频率 {frequency} 的 bar 序列计算，年化基准 {bpy} bar/年；"
            "评级窗口内因子值按注册实现 + K 线重算"
        )

        base["aggregate"] = agg
        base["per_symbol"] = per_symbol
        return _json_safe(base)

    # ------------------------------------------------------------------
    # 评级榜
    # ------------------------------------------------------------------
    def leaderboard(
        self,
        ids: Optional[list[str]] = None,
        days: Optional[float] = None,
        horizon: Optional[int] = None,
        refresh: bool = False,
        compute_limit: int = 20,
    ) -> dict[str, Any]:
        """评级榜：无 ids 时只读缓存（未缓存标 not_evaluated，绝不触发重算）；
        ids 子集（≤compute_limit）同步计算并落缓存。

        返回 entries（每因子一行：grade/status/关键指标）+ 汇总计数。
        """
        started = time.perf_counter()
        roster = list_factor_records(self.config)
        if ids:
            wanted = {str(x) for x in ids}
            roster = [r for r in roster if r.factor_id in wanted]
            known = {r.factor_id for r in roster}
            for missing in sorted(wanted - known):
                roster.append(EvalFactorRecord(factor_id=missing, name=missing, status="unknown"))

        entries: list[dict[str, Any]] = []
        computed = 0
        for rec in roster:
            entry = self._leaderboard_entry(rec, None)
            if rec.status == "unknown":
                entry["note"] = "因子未注册"
                entries.append(entry)
                continue
            use_symbols = list(self._symbols_override or self._default_symbols())
            days_v = float(days if days is not None else self._settings.get("rating_days", 30))
            horizon_v = int(horizon if horizon is not None else self._settings.get("rating_horizon", 24))
            cache_key = self._cache_key(rec, use_symbols, days_v, horizon_v)
            raw = None if refresh else self.eval_store.read_payload(
                "eval_rating_cache", rec.factor_id, cache_key
            )
            if raw is not None:
                payload = json.loads(raw)
                payload["cache_hit"] = True
                entry = self._leaderboard_entry(rec, payload)
            elif ids and computed < max(1, int(compute_limit)) and rec.status != "unknown":
                payload = self.rate_factor(
                    rec.factor_id, days=days_v, horizon=horizon_v, refresh=refresh
                )
                computed += 1
                entry = self._leaderboard_entry(rec, payload)
            entries.append(entry)

        def sort_key(e: dict[str, Any]) -> tuple:
            grade_order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
            grade = e.get("grade")
            return (
                grade_order.get(grade, 9) if grade else 9,
                -(abs(e.get("rank_ic_mean")) if e.get("rank_ic_mean") is not None else -1),
                e.get("factor_id") or "",
            )

        entries.sort(key=sort_key)
        return _json_safe({
            "entries": entries,
            "summary": {
                "total": len(entries),
                "rated": sum(1 for e in entries if e.get("grade") is not None),
                "insufficient": sum(1 for e in entries if e.get("rating_status") == "insufficient"),
                "not_supported": sum(1 for e in entries if e.get("rating_status") == "not_supported"),
                "not_evaluated": sum(1 for e in entries if e.get("rating_status") == "not_evaluated"),
                "computed_this_call": computed,
            },
            "eval_window": {
                "days": float(days if days is not None else self._settings.get("rating_days", 30)),
                "horizon": int(horizon if horizon is not None else self._settings.get("rating_horizon", 24)),
            },
            "note": (
                "无 ids 时只读缓存，未缓存因子标 not_evaluated；"
                "ids 子集（≤20）同步计算。评级口径见 rating.py 模块 docstring。"
            ),
            "computed_at": _now_iso(),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })

    @staticmethod
    def _leaderboard_entry(rec: EvalFactorRecord, payload: Optional[dict[str, Any]]) -> dict[str, Any]:
        """评级榜行：payload 为 None 表示未评级（not_evaluated）。"""
        entry: dict[str, Any] = {
            "factor_id": rec.factor_id,
            "name": rec.name,
            "factor_status": rec.status,
            "category": rec.category,
            "frequency": rec.frequency,
        }
        if payload is None:
            entry.update({"rating_status": "not_evaluated", "grade": None})
            return entry
        aggregate = payload.get("aggregate") or {}
        entry.update({
            "rating_status": payload.get("status"),
            "grade": aggregate.get("grade"),
            "rank_ic_mean": aggregate.get("rank_ic_mean"),
            "icir": aggregate.get("icir"),
            "sharpe": aggregate.get("sharpe"),
            "total_return_pct": aggregate.get("total_return_pct"),
            "max_drawdown_pct": aggregate.get("max_drawdown_pct"),
            "win_rate": aggregate.get("win_rate"),
            "n_samples": aggregate.get("n_samples"),
            "n_symbols_ok": aggregate.get("n_symbols_ok"),
            "last_ts": aggregate.get("last_ts"),
            "cache_hit": payload.get("cache_hit"),
        })
        return entry
