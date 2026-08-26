"""PYS-101 趋势跟踪：平台双文件策略实现（移植自 Desktop/王子夫，逻辑不重写）。

原策略输入为 UTC 4 小时 OHLCV；在每根已完成 4h K 线收盘后产生信号，
并汇总为日频目标仓位。本文件在平台内提供薄适配：

- :func:`generate_positions` 是原始核心逻辑的逐字移植（信号与仓位算法）；
- :func:`generate` 是平台双文件策略入口：接收 ``data_dependencies``
  解析出的数据集合（``{dep_id: {"frame": ...}}``），只消费已闭合 K 线，
  转成 ``generate_positions`` 需要的 UTC DatetimeIndex DataFrame，
  输出日频目标仓位（``timestamp/symbol/position``）。

成本、滑点与风控一律由统一回测引擎处理，不写回策略。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# 数据依赖 id：与 PYS-101 MD frontmatter 的 data_dependencies 对齐
DEPENDENCY_ID = "btc_4h"
SYMBOL = "BTCUSDT"

STRATEGY_META: dict[str, Any] = {
    "strategy_id": "PYS-101",
    "name": "trend_following_donchian",
    "version": "2.2.1-platform",
    "timeframe": "4h signal / 1d engine",
    "symbols": ["BTC/USDT"],
    "long_only": False,
    "data_dependencies": {
        "BTC/USDT": {
            "instrument": "spot", "frequency": "4h",
            "fields": ["open", "high", "low", "close", "volume"],
        },
    },
    "params": {
        "channel": 120,
        "er_period": 120,
        "er_long": 0.15,
        "er_short": 0.15,
        "gross_target": 0.10,
    },
}

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def _validate_ohlcv(df: pd.DataFrame, label: str) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{label} 缺少字段: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError(f"{label} 索引必须是 DatetimeIndex")
    out = df.sort_index().copy()
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")
    if out.index.has_duplicates:
        raise ValueError(f"{label} 存在重复时间戳")
    return out


def generate_positions(
    bars4h: pd.DataFrame,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成日频目标仓位（原始核心逻辑逐字移植）。"""
    bars = _validate_ohlcv(bars4h, "bars4h")
    p = dict(STRATEGY_META["params"])
    if params:
        p.update(params)
    channel = int(p["channel"])
    er_period = int(p["er_period"])
    if channel < 2 or er_period < 2:
        raise ValueError("channel 和 er_period 必须至少为 2")
    if not 0 < float(p["gross_target"]) <= 1:
        raise ValueError("gross_target 必须在 (0, 1] 内")

    high, low, close = bars["high"], bars["low"], bars["close"]
    upper = high.rolling(channel).max().shift(1)
    lower = low.rolling(channel).min().shift(1)
    path = close.diff().abs().rolling(er_period).sum()
    er = (close - close.shift(er_period)).abs() / path.replace(0.0, np.nan)

    states = np.zeros(len(bars), dtype=np.int8)
    current = 0
    for i in range(len(bars)):
        c, e = close.iloc[i], er.iloc[i]
        if current == 0 and np.isfinite(e):
            if c > upper.iloc[i] and e >= float(p["er_long"]):
                current = 1
            elif c < lower.iloc[i] and e >= float(p["er_short"]):
                current = -1
        elif current > 0 and c < lower.iloc[i]:
            current = 0
        elif current < 0 and c > upper.iloc[i]:
            current = 0
        states[i] = current

    pos4h = pd.Series(states, index=bars.index, dtype=float, name="pos4h")
    daily = bars.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    daily["ret"] = daily["close"].pct_change()
    pos = (pos4h.resample("1D").last().reindex(daily.index).fillna(0.0)
           * float(p["gross_target"]))
    pos.name = "PYS-101_pos"
    state = pd.DataFrame({
        "efficiency_ratio": er.resample("1D").last().reindex(daily.index),
        "direction": np.sign(pos),
        "in_market": pos.ne(0).astype(float),
    }, index=daily.index)
    return {
        "engine_df": daily,
        "pos": pos,
        "long_only": False,
        "state": state,
        "meta": STRATEGY_META | {
            "effective_params": p,
            "signal_changes": int(pos4h.diff().fillna(pos4h).ne(0).sum()),
        },
    }


def _bars_from_bundle(bundle: dict[str, Any]) -> pd.DataFrame:
    """从数据依赖集合中取出已闭合的 4h K 线帧。"""
    if DEPENDENCY_ID not in bundle:
        raise ValueError(
            f"缺少数据依赖 '{DEPENDENCY_ID}'（请检查策略 data_dependencies 声明）"
        )
    entry = bundle[DEPENDENCY_ID]
    frame = entry["frame"] if isinstance(entry, dict) else entry
    frame = frame.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError(f"依赖 '{DEPENDENCY_ID}' 必须以 UTC DatetimeIndex 为索引")
    # 只消费已闭合 K 线（分层管线已过滤，这里防御性再过滤）
    if "is_closed" in frame.columns:
        frame = frame[frame["is_closed"]]
    return frame[list(REQUIRED_COLUMNS)]


def generate(data_bundle: dict[str, Any], **params: Any) -> pd.DataFrame:
    """平台双文件策略入口：输出日频目标仓位（timestamp/symbol/position）。

    执行对齐：t 日信号在 t+1 日生效（末根 4h 桶收盘时点），与统一回测
    引擎的 ``position.shift(1)`` 语义一致；策略本身不产生任何成本/滑点。
    """
    bars = _bars_from_bundle(data_bundle)
    out = generate_positions(bars, params or None)
    pos = out["pos"]
    signal_ts = pos.index + pd.Timedelta(days=1)
    positions = pd.DataFrame({
        "timestamp": signal_ts,
        "symbol": SYMBOL,
        "position": pos.to_numpy(),
    })
    return positions[["timestamp", "symbol", "position"]]
