"""K线均线（SMA）叠加——复制自 sim_platform `app/indicators.py`（只读可复制源）。

预置均线的窗口以自然日定义，按周期换算成 bar 数后在后端统一计算，
前端只负责渲染返回的数据线（web/index.html 的均线叠加功能）。
属图表展示逻辑，非评估指标。
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

# 预置均线（证券公司参考线）。key/label/color 与前端展示一一对应，
# natural_days 为窗口的自然日长度。
MA_PRESETS: list[dict[str, Any]] = [
    {"key": "MA5", "label": "MA5", "color": "#FF6B6B", "natural_days": 5},
    {"key": "MA10", "label": "MA10", "color": "#FFA94D", "natural_days": 10},
    {"key": "MA20", "label": "MA20", "color": "#FFD43B", "natural_days": 20},
    {"key": "MA30", "label": "MA30", "color": "#51CF66", "natural_days": 30},
    {"key": "MA60", "label": "MA60", "color": "#4DABF7", "natural_days": 60},
    {"key": "MA83", "label": "MA83", "color": "#B197FC", "natural_days": 83},
    {"key": "MA_W", "label": "周线", "color": "#F783AC", "natural_days": 5},   # 周线 ≈ 5 个交易日
    {"key": "MA_M", "label": "月线", "color": "#22B8CF", "natural_days": 20},  # 月线 ≈ 20 个交易日
]

_PRESET_BY_KEY = {p["key"]: p for p in MA_PRESETS}

# 各周期每天的 bar 数（币圈 7x24，自然日即交易日）
BARS_PER_DAY = {
    "1m": 1440,
    "5m": 288,
    "15m": 96,
    "30m": 48,
    "1h": 24,
    "4h": 6,
    "1d": 1,
    "1w": 1 / 7,
}


def ma_window_bars(natural_days: float, period: str) -> int:
    """把自然日窗口按周期换算为 bar 数，最小为 2。"""
    bpd = BARS_PER_DAY.get(period, 1)
    return max(2, round(natural_days * bpd))


def sma(closes: pd.Series, window: int) -> list[Optional[float]]:
    """简单移动平均：前 window-1 个为 None，第 window 个起为前 window 根 close 的算术平均。

    window 非法或大于数据长度时返回全 None；返回列表与 closes 等长。
    """
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n == 0 or window < 1 or window > n:
        return out
    means = closes.rolling(window).mean()
    for i in range(window - 1, n):
        v = means.iloc[i]
        if pd.notna(v):
            out[i] = float(v)
    return out


def ma_lookback_bars(period: str, keys: Optional[list[str]] = None) -> int:
    """所请求均线中最大的窗口 bar 数。"""
    if keys is None:
        presets = MA_PRESETS
    else:
        presets = [_PRESET_BY_KEY[k] for k in keys if k in _PRESET_BY_KEY]
    return max((ma_window_bars(p["natural_days"], period) for p in presets), default=0)


def compute_ma_overlays(
    closes: pd.Series,
    period: str,
    keys: Optional[list[str]] = None,
) -> dict[str, list[Optional[float]]]:
    """按 period 计算预置均线，返回 {key: 与 closes 等长的数据线}。"""
    if keys is None:
        presets = MA_PRESETS
    else:
        presets = [_PRESET_BY_KEY[k] for k in keys if k in _PRESET_BY_KEY]
    return {
        p["key"]: sma(closes, ma_window_bars(p["natural_days"], period))
        for p in presets
    }
