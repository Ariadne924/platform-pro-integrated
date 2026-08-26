"""策略数据依赖版本化接口（/api/v1/strategies/...）测试。"""

from __future__ import annotations

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.kline_layers import DataLayer
from superplatform.data.provider_registry import (
    DataProvider,
    DataProviderRegistry,
)
from superplatform.strategy.data_dependencies import StrategyDataDependency


class _SpotKlineProvider(DataProvider):
    provider_id = "binance-spot-kline"
    data_type = "kline"
    exchange = "binance"
    market_type = MarketType.SPOT

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        return pd.DataFrame()


class _FundingProvider(DataProvider):
    provider_id = "binance-perp-funding"
    data_type = "funding_rate"
    exchange = "binance"
    market_type = MarketType.PERPETUAL

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        return pd.DataFrame()


class _Store:
    def query_series(self, table, symbol, frequency, **_kwargs):
        assert table == "pv_binance_spot_kline", table
        assert symbol == "BTCUSDT", symbol
        assert frequency == "1m", frequency
        return pd.DataFrame(
            [
                {"timestamp": "2026-01-01T00:00:00Z", "open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0, "volume": 1.0},
                {"timestamp": "2026-01-01T01:00:00Z", "open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "volume": 2.0},
                {"timestamp": "2026-01-01T02:00:00Z", "open": 104.0, "high": 106.0, "low": 98.0, "close": 101.0, "volume": 3.0},
                {"timestamp": "2026-01-01T03:00:00Z", "open": 101.0, "high": 108.0, "low": 100.0, "close": 107.0, "volume": 4.0},
            ]
        )


def _pys101_record() -> object:
    from superplatform.strategy.dual_registry import DualStrategyRecord

    return DualStrategyRecord(
        strategy_id="PYS-101",
        name="trend_following_donchian",
        version="2.2.1",
        status="active",
        symbols=["BTCUSDT"],
        params={},
        max_leverage=1.0,
        risk_limits={},
        md_path=__import__("pathlib").Path("PYS-101.md"),
        impl_path=__import__("pathlib").Path("pys101.py"),
        source="builtin",
        engine_frequency="1d",
        data_dependencies=[
            StrategyDataDependency(
                id="btc_4h",
                exchange="binance",
                market_type=MarketType.SPOT,
                data_type="kline",
                symbol="BTCUSDT",
                frequency=DataFrequency.H4,
                layer=DataLayer.GOLD,
                required_fields=("open", "high", "low", "close", "volume"),
                closed_only=True,
                group="primary",
                align="intersect",
            )
        ],
    )


class _FakeDual:
    def __init__(self, record):
        self._record = record

    def get_record(self, strategy_id):
        return self._record if strategy_id == "PYS-101" else None


def _client(monkeypatch, record=None, with_store=True):
    from superplatform.strategy import dual_registry as dr
    from superplatform_web import state
    from superplatform_web.routes import strategy_data_v1

    if record is None:
        record = _pys101_record()
    monkeypatch.setattr(dr.DualStrategyRegistry, "get_instance", classmethod(lambda cls: _FakeDual(record)))
    monkeypatch.setattr(state, "providers", _providers())
    if with_store:
        monkeypatch.setattr(state, "store", _Store())
    else:
        monkeypatch.setattr(state, "store", None)
    app = FastAPI()
    app.include_router(strategy_data_v1.router)
    return TestClient(app)


def _providers() -> DataProviderRegistry:
    registry = DataProviderRegistry()
    registry.register(_SpotKlineProvider())
    registry.register(_FundingProvider())
    return registry


def test_data_requirements_endpoint_returns_exact_provider(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/v1/strategies/PYS-101/data-requirements")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["strategy_id"] == "PYS-101"
    assert payload["engine_frequency"] == "1d"
    assert len(payload["dependencies"]) == 1
    dep = payload["dependencies"][0]
    assert dep["id"] == "btc_4h"
    assert dep["market_type"] == "spot"
    assert dep["frequency"] == "4h"
    assert dep["layer"] == "gold"
    assert dep["provider_id"] == "binance-spot-kline"
    assert dep["source"] == "provider_cache"
    assert dep["available"] is True
    assert payload["missing"] == []
    assert payload["errors"] == []


def test_data_requirements_unknown_strategy_returns_404(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/v1/strategies/UNKNOWN-999/data-requirements")
    assert resp.status_code == 404


def test_data_requirements_reports_missing_when_no_exact_provider(monkeypatch):
    # perpetual kline 不存在 → 该依赖进入 missing，不静默回退
    record = _pys101_record()
    record.data_dependencies = [
        StrategyDataDependency(
            id="btc_perp",
            exchange="binance",
            market_type=MarketType.PERPETUAL,
            data_type="kline",
            symbol="BTCUSDT",
            frequency=DataFrequency.D1,
            layer=DataLayer.GOLD,
        )
    ]
    client = _client(monkeypatch, record=record)
    resp = client.get("/api/v1/strategies/PYS-101/data-requirements")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dependencies"] == []
    assert len(payload["missing"]) == 1
    assert payload["missing"][0]["id"] == "btc_perp"
    assert payload["missing"][0]["available"] is False


def test_data_requirements_does_not_mislabel_provider_fetch_as_cache(monkeypatch):
    record = _pys101_record()
    record.data_dependencies = [
        StrategyDataDependency(
            id="funding",
            exchange="binance",
            market_type=MarketType.PERPETUAL,
            data_type="funding_rate",
            symbol="BTCUSDT",
            frequency=DataFrequency.D1,
        )
    ]
    client = _client(monkeypatch, record=record)

    response = client.get("/api/v1/strategies/PYS-101/data-requirements")

    assert response.status_code == 200
    dependency = response.json()["dependencies"][0]
    assert dependency["provider_id"] == "binance-perp-funding"
    assert dependency["source"] == "provider_registry"


def test_data_resolve_endpoint_returns_dataset_with_quality_meta(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post(
        "/api/v1/strategies/PYS-101/data/resolve",
        json={"limit": 300, "end": "2026-01-02T00:00:00Z"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["strategy_id"] == "PYS-101"
    datasets = payload["datasets"]
    assert "btc_4h" in datasets
    meta = datasets["btc_4h"]["meta"]
    assert meta["provider_id"] == "binance-spot-kline"
    assert meta["data_layer"] == "gold"
    assert meta["source"] == "provider_cache"
    assert meta["source_frequency"] == "1m"
    assert "quality_summary" in meta
    assert "time_range" in meta
    rows = datasets["btc_4h"]["rows"]
    assert rows and "open_time" in rows[0]
    assert "quality_flags" in rows[0]
    assert "is_closed" in rows[0]


def test_data_resolve_requires_store(monkeypatch):
    client = _client(monkeypatch, with_store=False)
    resp = client.post(
        "/api/v1/strategies/PYS-101/data/resolve", json={}
    )
    assert resp.status_code == 503


def test_data_resolve_no_dependencies_returns_empty(monkeypatch):
    record = _pys101_record()
    record.data_dependencies = []
    client = _client(monkeypatch, record=record)
    resp = client.post("/api/v1/strategies/PYS-101/data/resolve", json={})
    assert resp.status_code == 200
    assert resp.json()["datasets"] == {}
