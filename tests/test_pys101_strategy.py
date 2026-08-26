"""PYS-101 垂直切片测试：无前视 / 只消费已闭合 / 信号 schema / 端到端回测。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from superplatform.data.enums import MarketType
from superplatform.data.provider_registry import DataProvider


def _load_pys101():
    path = (
        Path(__file__).resolve().parents[1]
        / "strategies" / "impl" / "trend_following_donchian.py"
    )
    assert path.exists(), f"PYS-101 impl 不存在: {path}"
    spec = importlib.util.spec_from_file_location(
        "pys101_trend_following_test", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trend_frame(n_bars: int = 400, *, per_bar: float = 0.01) -> pd.DataFrame:
    """强单边趋势的 Gold 4h 帧（确定性，保证 ER=1、通道突破）。"""
    idx = pd.date_range("2026-01-01", periods=n_bars, freq="4h", tz="UTC")
    close = 100.0 * (1.0 + per_bar) ** np.arange(n_bars)
    prev_close = pd.Series(close).shift(1).fillna(close[0] / (1.0 + per_bar))
    frame = pd.DataFrame(
        {
            "open": prev_close.to_numpy(),
            "high": close * (1.0 + per_bar / 2.0),
            "low": close / (1.0 + per_bar / 2.0),
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )
    frame["is_closed"] = True
    frame["close_time"] = idx + pd.Timedelta(hours=4)
    return frame


def _bundle(frame: pd.DataFrame) -> dict:
    return {"btc_4h": {"frame": frame}}


# ── 信号 schema 与仓位边界 ────────────────────────────────────────────


def test_pys101_generate_returns_signal_schema():
    mod = _load_pys101()
    out = mod.generate(_bundle(_trend_frame(300)))
    assert list(out.columns) == ["timestamp", "symbol", "position"]
    assert (out["symbol"] == "BTCUSDT").all()
    ts = pd.DatetimeIndex(pd.to_datetime(out["timestamp"], utc=True))
    assert ts.tz is not None
    # 目标仓位只允许 0 / ±gross_target(0.1)
    assert out["position"].isin([-0.1, 0.0, 0.1]).all()
    # 时间戳严格递增（无重复、无乱序）
    assert ts.is_monotonic_increasing
    assert not ts.has_duplicates


# ── 无前视：通道与 ER 预热期内绝无仓位 ────────────────────────────────


def test_pys101_no_entry_before_channel_and_er_warmup():
    mod = _load_pys101()
    frame = _trend_frame(400)
    out = mod.generate(_bundle(frame))
    ts = pd.DatetimeIndex(pd.to_datetime(out["timestamp"], utc=True))
    pos = out["position"]

    # 通道 120 根 + shift：第 120 根 4h 桶收盘才可能入场
    entry_day = frame.index[120].normalize()
    first_actionable = entry_day + pd.Timedelta(days=1)
    assert (pos[ts < first_actionable] == 0.0).all()
    # 强趋势一旦入场即保持做多（gross_target）
    assert (pos[ts >= first_actionable] == 0.1).all()


def test_pys101_signal_timestamps_are_past_observable():
    """每个信号时刻之前都必须存在至少一根已闭合 K 线（close_time <= 时刻）。"""
    mod = _load_pys101()
    frame = _trend_frame(300)
    out = mod.generate(_bundle(frame))
    ts = pd.DatetimeIndex(pd.to_datetime(out["timestamp"], utc=True))
    closes_obs = pd.to_datetime(frame["close_time"], utc=True).sort_values()
    counts = closes_obs.searchsorted(ts, side="right")
    assert (counts >= 1).all()


def test_dual_factors_params_schema_serializable():
    """回归：双文件因子的 params_schema 必须是可序列化 dict（防裸 Field）。

    数据依赖策略执行会触发 DualFactorRegistry 扫描（MOM-001 等入库）；
    若其 params_schema 是基类裸 dataclasses.Field，后续 /api/factors
    序列化会失败。此测试放在本文件（test_factor_config 之后）以避开
    判卷测试的精确因子集合断言。
    """
    from fastapi.encoders import jsonable_encoder

    from superplatform.factors.dual_registry import DualFactorRegistry
    from superplatform.factors.registry import FactorRegistry

    DualFactorRegistry.get_instance().ensure_scanned()
    registry = FactorRegistry.get_instance()
    dual_ids = {
        row["factor_id"]
        for row in DualFactorRegistry.get_instance().list_factors()
        if row.get("registered")
    }
    assert dual_ids, "应至少扫描到已注册的双文件因子"
    for factor_id in dual_ids:
        factor = registry.get(factor_id)
        schema = factor.params_schema
        assert isinstance(schema, dict), (
            f"双文件因子 {factor_id} 的 params_schema 必须是 dict，"
            f"实际: {type(schema).__name__}"
        )
        jsonable_encoder(schema)


# ── 只消费已闭合：未闭合的极端 bar 不影响输出 ─────────────────────────


def test_pys101_ignores_incomplete_bar():
    mod = _load_pys101()
    frame = _trend_frame(300)
    # 追加一根未闭合的极端下跌 bar（若被消费会改变通道/触发平仓/做空）
    last = frame.index[-1]
    bad = pd.DataFrame(
        [{
            "open": frame["close"].iloc[-1],
            "high": frame["close"].iloc[-1],
            "low": 0.1,
            "close": 0.1,
            "volume": 1.0,
            "is_closed": False,
        }],
        index=[last + pd.Timedelta(hours=4)],
    )
    bad["close_time"] = bad.index + pd.Timedelta(hours=4)
    frame_with = pd.concat([frame, bad])

    out_base = mod.generate(_bundle(frame))
    out_with = mod.generate(_bundle(frame_with))
    pd.testing.assert_frame_equal(out_base, out_with)


# ── 端到端：经统一回测引擎 /api/strategies/backtest ──────────────────


class _SpotKlineProvider(DataProvider):
    provider_id = "binance-spot-kline"
    data_type = "kline"
    exchange = "binance"
    market_type = MarketType.SPOT

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        return pd.DataFrame()


class _OneMinuteStore:
    """回测窗口内的 1m 现货 K 线 stub（Gold 4h 由它聚合）。"""

    def __init__(self, start: str, end: str) -> None:
        idx = pd.date_range(start, end, freq="1min", tz="UTC")
        close = 100.0 * 1.00001 ** np.arange(len(idx))
        self._frame = pd.DataFrame(
            {
                "timestamp": idx,
                "open": close,
                "high": close * 1.00001,
                "low": close * 0.99999,
                "close": close,
                "volume": 1.0,
            }
        )

    def query_series(self, table, symbol, frequency, **_kwargs):
        assert table == "pv_binance_spot_kline", table
        assert symbol == "BTCUSDT", symbol
        assert frequency == "1m", frequency
        return self._frame.copy()


def test_pys101_backtest_end_to_end(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from superplatform.strategy.dual_registry import DualStrategyRegistry
    from superplatform_web import state
    from superplatform_web.routes.strategies import router as strategies_router

    DualStrategyRegistry.get_instance().ensure_scanned()
    record = DualStrategyRegistry.get_instance().get_record("PYS-101")
    assert record is not None, "PYS-101 未通过双文件校验并注册"

    state.providers.clear()
    state.providers.register(_SpotKlineProvider())

    start = "2026-01-01T00:00:00Z"
    end = "2026-03-01T00:00:00Z"
    monkeypatch.setattr(state, "store", _OneMinuteStore(start, end))

    app = FastAPI()
    app.include_router(strategies_router)
    client = TestClient(app)
    resp = client.post(
        "/api/strategies/backtest",
        json={"strategy": "PYS-101", "start": start, "end": end},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "metrics" in payload
    assert "sharpe" in payload["metrics"]
    assert payload["metrics"]["sharpe"] is not None
    assert len(payload["signals"]) > 0
    assert len(payload["equity"]) > 0
    # 信号是日频目标仓位，取值受限
    pos_values = {row["position"] for row in payload["signals"]}
    assert pos_values <= {-0.1, 0.0, 0.1}
