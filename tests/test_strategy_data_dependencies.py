"""策略数据依赖核心服务测试：声明解析 / 精确 Provider 解析 / 取数对齐。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pandas as pd
import pytest

from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.kline_layers import DataLayer
from superplatform.data.provider_registry import (
    DataProvider,
    DataProviderRegistry,
)
from superplatform.strategy.data_dependencies import (
    StrategyDataDependency,
    align_dependency_groups,
    build_price_data,
    dependency_instrument_id,
    fetch_strategy_data,
    parse_data_dependencies,
    resolve_dependency_provider,
)


class _SpotKlineProvider(DataProvider):
    provider_id = "binance-spot-kline"
    data_type = "kline"
    exchange = "binance"
    market_type = MarketType.SPOT

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        return pd.DataFrame()


class _FundingProvider(DataProvider):
    provider_id = "binance-funding-rate"
    data_type = "funding_rate"
    exchange = "binance"
    market_type = MarketType.PERPETUAL

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        return pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-01-01", tz="UTC"),
                    "funding_rate": 0.0001,
                },
                {
                    "timestamp": pd.Timestamp("2026-01-02", tz="UTC"),
                    "funding_rate": 0.0002,
                },
            ]
        )


class _Store:
    """最小 stub：query_series 返回构造的 1m 现货 K 线。"""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def query_series(self, table, symbol, frequency, **_kwargs):
        assert table == "pv_binance_spot_kline", table
        assert symbol == "BTCUSDT", symbol
        assert frequency == "1m", frequency
        return self._frame.copy()


def _registry() -> DataProviderRegistry:
    registry = DataProviderRegistry()
    registry.register(_SpotKlineProvider())
    registry.register(_FundingProvider())
    return registry


def _dep(**overrides) -> StrategyDataDependency:
    values = {
        "id": "btc_1m",
        "exchange": "binance",
        "market_type": MarketType.SPOT,
        "data_type": "kline",
        "symbol": "BTCUSDT",
        "frequency": DataFrequency.M1,
        "layer": DataLayer.SILVER,
    }
    values.update(overrides)
    return StrategyDataDependency(**values)


# ── 声明解析 ──────────────────────────────────────────────────────────


def test_parse_data_dependencies_ok():
    meta = {
        "data_dependencies": [
            {
                "id": "btc_4h",
                "exchange": "binance",
                "market_type": "spot",
                "data_type": "kline",
                "symbol": "BTCUSDT",
                "frequency": "4h",
                "layer": "gold",
                "required_fields": ["open", "high", "low", "close", "volume"],
                "closed_only": True,
                "group": "primary",
                "align": "intersect",
            }
        ]
    }
    deps, errors = parse_data_dependencies(meta)
    assert errors == []
    assert len(deps) == 1
    dep = deps[0]
    assert dep.id == "btc_4h"
    assert dep.market_type is MarketType.SPOT
    assert dep.frequency is DataFrequency.H4
    assert dep.layer is DataLayer.GOLD
    assert dep.required_fields == ("open", "high", "low", "close", "volume")
    assert dep.closed_only is True
    assert dep.align == "intersect"


def test_parse_data_dependencies_absent_returns_empty():
    deps, errors = parse_data_dependencies({"symbols": ["BTCUSDT"]})
    assert deps == []
    assert errors == []


def test_parse_data_dependencies_rejects_bad_frequency():
    meta = {
        "data_dependencies": [
            {
                "id": "btc_4h",
                "exchange": "binance",
                "market_type": "spot",
                "data_type": "kline",
                "symbol": "BTCUSDT",
                "frequency": "4x",
            }
        ]
    }
    deps, errors = parse_data_dependencies(meta)
    assert deps == []
    assert any("frequency" in e for e in errors)


def test_parse_data_dependencies_rejects_bad_market_type():
    meta = {
        "data_dependencies": [
            {
                "id": "btc_4h",
                "exchange": "binance",
                "market_type": "futures",
                "data_type": "kline",
                "symbol": "BTCUSDT",
                "frequency": "4h",
            }
        ]
    }
    _deps, errors = parse_data_dependencies(meta)
    assert any("market_type" in e for e in errors)


def test_parse_data_dependencies_rejects_unsupported_kline_frequency():
    # 8h 是合法 DataFrequency，但分层管线不服务 → 注册时即拦截
    meta = {
        "data_dependencies": [
            {
                "id": "btc_8h", "exchange": "binance", "market_type": "spot",
                "data_type": "kline", "symbol": "BTCUSDT", "frequency": "8h",
                "layer": "gold",
            }
        ]
    }
    deps, errors = parse_data_dependencies(meta)
    assert deps == []
    assert any("8h" in e or "周期" in e for e in errors)


def test_parse_data_dependencies_rejects_non_native_layer_frequency():
    # 4h 派生周期配 silver 层不可服务
    meta = {
        "data_dependencies": [
            {
                "id": "btc_4h", "exchange": "binance", "market_type": "spot",
                "data_type": "kline", "symbol": "BTCUSDT", "frequency": "4h",
                "layer": "silver",
            }
        ]
    }
    deps, errors = parse_data_dependencies(meta)
    assert deps == []
    assert any("原生" in e for e in errors)


def test_parse_data_dependencies_rejects_duplicate_id():
    meta = {
        "data_dependencies": [
            {
                "id": "btc_4h", "exchange": "binance", "market_type": "spot",
                "data_type": "kline", "symbol": "BTCUSDT", "frequency": "4h",
                "layer": "gold",
            },
            {
                "id": "btc_4h", "exchange": "binance", "market_type": "spot",
                "data_type": "kline", "symbol": "ETHUSDT", "frequency": "4h",
                "layer": "gold",
            },
        ]
    }
    deps, errors = parse_data_dependencies(meta)
    assert len(deps) == 1
    assert any("重复" in e for e in errors)


def test_parse_data_dependencies_rejects_mixed_alignment_in_one_group():
    base = {
        "exchange": "binance",
        "market_type": "spot",
        "data_type": "kline",
        "frequency": "1d",
        "layer": "gold",
        "group": "pair",
    }
    meta = {
        "data_dependencies": [
            base | {"id": "btc", "symbol": "BTCUSDT", "align": "intersect"},
            base | {"id": "eth", "symbol": "ETHUSDT", "align": "past"},
        ]
    }

    _deps, errors = parse_data_dependencies(meta)

    assert any("pair" in error and "align" in error for error in errors)


# ── 精确 Provider 解析（禁止静默 fallback） ───────────────────────────


def test_resolve_dependency_provider_exact_match():
    registry = _registry()
    dep = _dep(frequency=DataFrequency.H4, layer=DataLayer.GOLD)
    assert resolve_dependency_provider(dep, registry) == "binance-spot-kline"


def test_resolve_dependency_provider_rejects_cross_market_fallback():
    registry = _registry()
    # 只有 spot kline；perpetual kline 不存在 → 必须报错，不得回退
    dep = _dep(
        id="btc_perp_1d",
        market_type=MarketType.PERPETUAL,
        frequency=DataFrequency.D1,
        layer=DataLayer.GOLD,
    )
    with pytest.raises(ValueError, match="No exact provider"):
        resolve_dependency_provider(dep, registry)


def test_resolve_dependency_provider_funding_rate():
    registry = _registry()
    dep = StrategyDataDependency(
        id="funding",
        exchange="binance",
        market_type=MarketType.PERPETUAL,
        data_type="funding_rate",
        symbol="BTCUSDT",
        frequency=DataFrequency.D1,
    )
    assert resolve_dependency_provider(dep, registry) == "binance-funding-rate"


# ── 取数（kline 走分层管线，只消费已闭合，UTC） ──────────────────────


def _one_minute_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0, "volume": 1.0},
            {"timestamp": "2026-01-01T00:01:00Z", "open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "volume": 2.0},
            {"timestamp": "2026-01-01T00:02:00Z", "open": 104.0, "high": 106.0, "low": 98.0, "close": 101.0, "volume": 3.0},
        ]
    )


def test_fetch_strategy_data_kline_closed_only_utc_with_meta():
    now = datetime(2026, 1, 1, 0, 1, 30, tzinfo=timezone.utc)  # 00:00 已闭合，00:01/00:02 未闭合
    bundle = asyncio.run(
        fetch_strategy_data(
            [_dep()],
            store=_Store(_one_minute_frame()),
            registry=_registry(),
            now=now,
        )
    )
    entry = bundle["btc_1m"]
    meta = entry["meta"]
    assert meta["provider_id"] == "binance-spot-kline"
    assert meta["source"] == "provider_cache"
    assert meta["data_layer"] == "silver"
    assert meta["source_frequency"] == "1m"
    assert meta["frequency"] == "1m"
    assert meta["quality_summary"]["incomplete_bars"] == 2
    assert "time_range" in meta
    assert meta["time_range"]["start"] == "2026-01-01T00:00:00+00:00"

    frame = entry["frame"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.tz is not None
    assert frame.index.name == "timestamp"
    # closed_only：只留下已闭合的 00:00 桶
    assert list(frame.index) == [pd.Timestamp("2026-01-01T00:00:00", tz="UTC")]
    assert {"open", "high", "low", "close", "volume"}.issubset(frame.columns)


def test_fetch_strategy_data_funding_rate_uses_provider():
    dep = StrategyDataDependency(
        id="funding",
        exchange="binance",
        market_type=MarketType.PERPETUAL,
        data_type="funding_rate",
        symbol="BTCUSDT",
        frequency=DataFrequency.D1,
    )
    bundle = asyncio.run(
        fetch_strategy_data([dep], store=_Store(pd.DataFrame()), registry=_registry())
    )
    entry = bundle["funding"]
    assert entry["meta"]["provider_id"] == "binance-funding-rate"
    assert "funding_rate" in entry["frame"].columns


def test_fetch_strategy_data_rejects_missing_required_fields():
    dep = StrategyDataDependency(
        id="funding",
        exchange="binance",
        market_type=MarketType.PERPETUAL,
        data_type="funding_rate",
        symbol="BTCUSDT",
        frequency=DataFrequency.D1,
        required_fields=("funding_rate", "mark_price"),
    )

    with pytest.raises(ValueError, match="mark_price"):
        asyncio.run(
            fetch_strategy_data(
                [dep], store=_Store(pd.DataFrame()), registry=_registry()
            )
        )


def test_fetch_strategy_data_normalizes_naive_non_kline_index_to_utc():
    class NaiveFundingProvider(_FundingProvider):
        async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
            return pd.DataFrame(
                {"funding_rate": [0.0001]},
                index=pd.DatetimeIndex(["2026-01-01"]),
            )

    registry = DataProviderRegistry()
    registry.register(_SpotKlineProvider())
    registry.register(NaiveFundingProvider())
    dep = StrategyDataDependency(
        id="funding",
        exchange="binance",
        market_type=MarketType.PERPETUAL,
        data_type="funding_rate",
        symbol="BTCUSDT",
        frequency=DataFrequency.D1,
        required_fields=("funding_rate",),
    )

    bundle = asyncio.run(
        fetch_strategy_data([dep], store=_Store(pd.DataFrame()), registry=registry)
    )

    assert str(bundle["funding"]["frame"].index.tz) == "UTC"


def test_fetch_strategy_data_gold_4h_from_1m():
    # 4h 由 1m 聚合，固定桶边界；now 足够大让全部闭合
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    dep = _dep(frequency=DataFrequency.H4, layer=DataLayer.GOLD)
    bundle = asyncio.run(
        fetch_strategy_data([dep], store=_Store(_one_minute_frame()), registry=_registry(), now=now)
    )
    frame = bundle["btc_1m"]["frame"]
    assert frame.index[0] == pd.Timestamp("2026-01-01T00:00:00", tz="UTC")
    assert frame["close"].iloc[0] == 101.0  # 4h 内最后一根 1m 的 close
    assert bundle["btc_1m"]["meta"]["transformations"][-1] == "resample:1m->4h"


class _RecordingStore(_Store):
    def __init__(self, frame: pd.DataFrame) -> None:
        super().__init__(frame)
        self.last_order: str | None = None

    def query_series(self, table, symbol, frequency, **_kwargs):
        self.last_order = _kwargs.get("order")
        return super().query_series(table, symbol, frequency, **_kwargs)


def test_fetch_strategy_data_end_only_reads_recent_bars():
    # 只给 end（无 start）时应按 DESC 取最近数据，而不是返回最旧的一批
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    store = _RecordingStore(_one_minute_frame())
    dep = _dep()
    asyncio.run(
        fetch_strategy_data(
            [dep], store=store, registry=_registry(), now=now,
            end=pd.Timestamp("2026-01-01T00:03:00Z"),
        )
    )
    assert store.last_order == "DESC"


# ── 对齐 ──────────────────────────────────────────────────────────────


def _aligned_bundle():
    a = pd.DataFrame(
        {"close": [1.0, 2.0, 3.0]},
        index=pd.DatetimeIndex(
            ["2026-01-01", "2026-01-02", "2026-01-03"], tz="UTC"
        ),
    )
    b = pd.DataFrame(
        {"close": [10.0, 30.0]},
        index=pd.DatetimeIndex(["2026-01-01", "2026-01-03"], tz="UTC"),
    )
    bundle = {"a": {"frame": a}, "b": {"frame": b}}
    deps = [
        StrategyDataDependency(
            id="a", exchange="binance", market_type=MarketType.SPOT,
            data_type="kline", symbol="BTCUSDT", frequency=DataFrequency.D1,
            layer=DataLayer.GOLD, group="g", align="intersect",
        ),
        StrategyDataDependency(
            id="b", exchange="binance", market_type=MarketType.SPOT,
            data_type="kline", symbol="ETHUSDT", frequency=DataFrequency.D1,
            layer=DataLayer.GOLD, group="g", align="intersect",
        ),
    ]
    return bundle, deps


def test_align_dependency_groups_intersect():
    bundle, deps = _aligned_bundle()
    aligned = align_dependency_groups(bundle, deps)
    group = aligned["g"]
    # 严格交集：只保留两序列都有的日期
    assert list(group.index) == [
        pd.Timestamp("2026-01-01", tz="UTC"),
        pd.Timestamp("2026-01-03", tz="UTC"),
    ]
    assert "a.close" in group.columns and "b.close" in group.columns


def test_build_price_data_asof_no_lookahead():
    # 信号时刻只能看到 close_time <= 时刻 的已闭合 close；
    # 若按 open_time 对齐会把尚未闭合的 4h 桶误当成已知价格（前视）。
    frame = pd.DataFrame(
        {
            "open_time": ["2025-12-31T20:00:00Z", "2026-01-01T20:00:00Z"],
            "close_time": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
            "close": [100.0, 110.0],
        }
    )
    frame = frame.set_index(pd.DatetimeIndex(pd.to_datetime(frame.pop("open_time"), utc=True)))
    bundle = {"btc_4h": {"frame": frame}}
    deps = [
        StrategyDataDependency(
            id="btc_4h", exchange="binance", market_type=MarketType.SPOT,
            data_type="kline", symbol="BTCUSDT", frequency=DataFrequency.H4,
            layer=DataLayer.GOLD,
        )
    ]
    signal_times = pd.DatetimeIndex(
        ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], tz="UTC"
    )
    price_data = build_price_data(bundle, deps, signal_times)
    closes = price_data["BTCUSDT"]["close"].tolist()
    assert closes == [100.0, 110.0]


def test_build_price_data_keeps_spot_and_perpetual_instruments_separate():
    times = pd.DatetimeIndex(["2026-01-02T00:00:00Z"], tz="UTC")
    frame = pd.DataFrame(
        {"close_time": times, "close": [100.0]},
        index=pd.DatetimeIndex(["2026-01-01T00:00:00Z"], tz="UTC"),
    )
    spot = _dep(id="spot_btc", frequency=DataFrequency.D1, layer=DataLayer.GOLD)
    perp = _dep(
        id="perp_btc",
        market_type=MarketType.PERPETUAL,
        frequency=DataFrequency.D1,
        layer=DataLayer.GOLD,
    )
    bundle = {
        "spot_btc": {"frame": frame.copy()},
        "perp_btc": {"frame": frame.assign(close=101.0)},
    }

    price_data = build_price_data(bundle, [spot, perp], times)

    assert set(price_data) == {
        dependency_instrument_id(spot),
        dependency_instrument_id(perp),
    }
    assert price_data[dependency_instrument_id(spot)]["close"].tolist() == [100.0]
    assert price_data[dependency_instrument_id(perp)]["close"].tolist() == [101.0]
