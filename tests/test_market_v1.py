from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _KlineStore:
    def query_series(self, table, symbol, frequency, **_kwargs):
        assert table == "pv_binance_perp_kline"
        assert symbol == "BTCUSDT"
        assert frequency == "1m"
        return pd.DataFrame(
            [
                {
                    "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                    "open": 100.0,
                    "high": 103.0,
                    "low": 99.0,
                    "close": 102.0,
                    "volume": 12.5,
                    "quote_volume": 1260.0,
                    "trades": 8,
                    "taker_buy_volume": 7.0,
                    "taker_buy_quote_volume": 710.0,
                }
            ]
        )


def test_kline_api_exposes_explicit_market_and_quality_metadata(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    monkeypatch.setattr(state, "store", _KlineStore())
    monkeypatch.setattr(
        state,
        "resolve_provider_for_data_type",
        lambda exchange, market, data_type, **_kwargs: "binance-perp-kline",
    )

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get(
        "/api/v1/market/klines",
        params={
            "exchange": "binance",
            "market_type": "perpetual",
            "symbol": "BTCUSDT",
            "frequency": "1m",
            "ma": "all",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"] == {
        "exchange": "binance",
        "market_type": "perpetual",
        "symbol": "BTCUSDT",
        "frequency": "1m",
        "source_frequency": "1m",
        "provider_id": "binance-perp-kline",
        "source": "provider_cache",
        "data_layer": "silver",
        "transformations": [
            "utc_normalization",
            "canonical_fields",
            "close_state",
            "quality_flags",
        ],
        "count": 1,
        "has_more": False,
        "quality_summary": {"flagged_bars": 0, "incomplete_bars": 0},
    }
    assert payload["data"] == [
        {
            "open_time": "2026-01-01T00:00:00+00:00",
            "close_time": "2026-01-01T00:01:00+00:00",
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 102.0,
            "volume": 12.5,
            "quote_volume": 1260.0,
            "trade_count": 8,
            "taker_buy_volume": 7.0,
            "taker_buy_quote_volume": 710.0,
            "is_closed": True,
            "quality_flags": [],
        }
    ]
    assert payload["ma"]["MA5"] == [None]


def test_kline_api_builds_four_hour_bars_from_silver_one_minute_data(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    class ResampleStore:
        def query_series(self, _table, _symbol, frequency, **_kwargs):
            assert frequency == "1m"
            return pd.DataFrame(
                [
                    {"timestamp": "2026-01-01T00:00:00Z", "open": 100, "high": 103, "low": 99, "close": 102, "volume": 1, "quote_volume": 101, "trades": 1, "taker_buy_volume": 0.5, "taker_buy_quote_volume": 50},
                    {"timestamp": "2026-01-01T01:00:00Z", "open": 102, "high": 105, "low": 101, "close": 104, "volume": 2, "quote_volume": 205, "trades": 2, "taker_buy_volume": 1.0, "taker_buy_quote_volume": 102},
                    {"timestamp": "2026-01-01T02:00:00Z", "open": 104, "high": 106, "low": 98, "close": 101, "volume": 3, "quote_volume": 307, "trades": 3, "taker_buy_volume": 1.5, "taker_buy_quote_volume": 153},
                    {"timestamp": "2026-01-01T03:00:00Z", "open": 101, "high": 108, "low": 100, "close": 107, "volume": 4, "quote_volume": 415, "trades": 4, "taker_buy_volume": 2.0, "taker_buy_quote_volume": 208},
                ]
            )

    monkeypatch.setattr(state, "store", ResampleStore())
    monkeypatch.setattr(
        state,
        "resolve_provider_for_data_type",
        lambda exchange, market, data_type, **_kwargs: "binance-perp-kline",
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get(
        "/api/v1/market/klines",
        params={
            "exchange": "binance",
            "market_type": "perpetual",
            "symbol": "BTCUSDT",
            "frequency": "4h",
            "layer": "gold",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T04:00:00Z",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["source_frequency"] == "1m"
    assert payload["meta"]["data_layer"] == "gold"
    assert payload["meta"]["transformations"] == [
        "utc_normalization",
        "canonical_fields",
        "close_state",
        "quality_flags",
        "resample:1m->4h",
    ]
    assert payload["data"] == [
        {
            "open_time": "2026-01-01T00:00:00+00:00",
            "close_time": "2026-01-01T04:00:00+00:00",
            "open": 100.0,
            "high": 108.0,
            "low": 98.0,
            "close": 107.0,
            "volume": 10.0,
            "quote_volume": 1028.0,
            "trade_count": 10,
            "taker_buy_volume": 5.0,
            "taker_buy_quote_volume": 513.0,
            "is_closed": True,
            "quality_flags": [],
        }
    ]


def test_gold_bar_inherits_silver_source_quality_flags(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    class InvalidSourceStore:
        def query_series(self, *_args, **_kwargs):
            return pd.DataFrame(
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "open": 100,
                        "high": 99,
                        "low": 98,
                        "close": 98.5,
                        "volume": 1,
                        "quote_volume": 99,
                        "trades": 1,
                        "taker_buy_volume": 0.5,
                        "taker_buy_quote_volume": 50,
                    },
                    {
                        "timestamp": "2026-01-01T01:00:00Z",
                        "open": 98.5,
                        "high": 102,
                        "low": 98,
                        "close": 101,
                        "volume": 1,
                        "quote_volume": 100,
                        "trades": 1,
                        "taker_buy_volume": 0.5,
                        "taker_buy_quote_volume": 50,
                    },
                ]
            )

    monkeypatch.setattr(state, "store", InvalidSourceStore())
    monkeypatch.setattr(
        state,
        "resolve_provider_for_data_type",
        lambda exchange, market, data_type, **_kwargs: "binance-perp-kline",
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get(
        "/api/v1/market/klines",
        params={
            "exchange": "binance",
            "market_type": "perpetual",
            "symbol": "BTCUSDT",
            "frequency": "4h",
            "layer": "gold",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"][0]["quality_flags"] == ["invalid_ohlc"]


def test_kline_api_preserves_missing_values_as_quality_flags(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    class MissingFieldStore:
        def query_series(self, *_args, **_kwargs):
            return pd.DataFrame(
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "volume": 2.0,
                        "quote_volume": None,
                        "trades": None,
                        "taker_buy_volume": None,
                        "taker_buy_quote_volume": None,
                    }
                ]
            )

    monkeypatch.setattr(state, "store", MissingFieldStore())
    monkeypatch.setattr(
        state,
        "resolve_provider_for_data_type",
        lambda exchange, market, data_type, **_kwargs: "binance-perp-kline",
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get(
        "/api/v1/market/klines",
        params={
            "exchange": "binance",
            "market_type": "perpetual",
            "symbol": "BTCUSDT",
            "frequency": "1m",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["quality_summary"]["flagged_bars"] == 1
    assert payload["data"][0]["quality_flags"] == ["missing_field"]
    assert payload["data"][0]["quote_volume"] is None
    assert payload["data"][0]["trade_count"] is None
    assert payload["data"][0]["taker_buy_volume"] is None


def test_versioned_kline_interface_is_registered_on_main_application():
    from superplatform_web.app import app

    assert "/api/v1/market/klines" in app.openapi()["paths"]


def test_v1_kline_interface_disables_provider_fallback(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    fallback_options = []

    def resolve_provider(_exchange, _market, _data_type, *, allow_fallback=None):
        fallback_options.append(allow_fallback)
        return "binance-perp-kline"

    monkeypatch.setattr(state, "store", _KlineStore())
    monkeypatch.setattr(state, "resolve_provider_for_data_type", resolve_provider)
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get(
        "/api/v1/market/klines",
        params={
            "exchange": "binance",
            "market_type": "perpetual",
            "symbol": "BTCUSDT",
            "frequency": "1m",
        },
    )

    assert response.status_code == 200
    assert fallback_options == [False]


def test_weekly_gold_bars_use_stable_monday_boundaries(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    class WindowedStore:
        def query_series(self, *_args, start=None, end=None, **_kwargs):
            frame = pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2026-01-05T00:00:00Z")
                        + pd.Timedelta(days=day),
                        "open": 100 + day,
                        "high": 102 + day,
                        "low": 99 + day,
                        "close": 101 + day,
                        "volume": 1,
                    }
                    for day in range(7)
                ]
            )
            timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            if start is not None:
                frame = frame[timestamps >= start]
                timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            if end is not None:
                frame = frame[timestamps < end]
            return frame.reset_index(drop=True)

    monkeypatch.setattr(state, "store", WindowedStore())
    monkeypatch.setattr(
        state,
        "resolve_provider_for_data_type",
        lambda exchange, market, data_type, **_kwargs: "binance-perp-kline",
    )
    app = FastAPI()
    app.include_router(router)

    def load(start):
        response = TestClient(app).get(
            "/api/v1/market/klines",
            params={
                "exchange": "binance",
                "market_type": "perpetual",
                "symbol": "BTCUSDT",
                "frequency": "1w",
                "layer": "gold",
                "start": start,
                "end": "2026-01-12T00:00:00Z",
            },
        )
        assert response.status_code == 200
        return response.json()["data"]

    from_tuesday = load("2026-01-06T00:00:00Z")
    from_thursday = load("2026-01-08T00:00:00Z")

    assert from_tuesday == from_thursday
    assert from_tuesday[0]["open_time"] == "2026-01-05T00:00:00+00:00"
    assert from_tuesday[0]["open"] == 100.0
    assert from_tuesday[0]["close"] == 107.0


def test_gold_excludes_bar_cut_off_by_historical_end(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    class PartialEndStore:
        def query_series(self, *_args, start=None, end=None, **_kwargs):
            frame = pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2026-01-05T00:00:00Z")
                        + pd.Timedelta(hours=hour),
                        "open": 100 + hour,
                        "high": 102 + hour,
                        "low": 99 + hour,
                        "close": 101 + hour,
                        "volume": 1,
                    }
                    for hour in range(4)
                ]
            )
            timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            if start is not None:
                frame = frame[timestamps >= start]
                timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            if end is not None:
                frame = frame[timestamps < end]
            return frame.reset_index(drop=True)

    monkeypatch.setattr(state, "store", PartialEndStore())
    monkeypatch.setattr(
        state,
        "resolve_provider_for_data_type",
        lambda exchange, market, data_type, **_kwargs: "binance-perp-kline",
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get(
        "/api/v1/market/klines",
        params={
            "exchange": "binance",
            "market_type": "perpetual",
            "symbol": "BTCUSDT",
            "frequency": "4h",
            "layer": "gold",
            "start": "2026-01-05T00:00:00Z",
            "end": "2026-01-05T02:30:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == []


def test_market_page_consumes_gold_kline_interface():
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "/v1/market/klines?" in html
    assert "layer=gold" in html
    assert "ma=all" in html
    assert "function computeClientMA" not in html
    assert 'id="kline-data-meta"' in html


def test_bronze_kline_page_is_bounded_and_keeps_native_rows(monkeypatch):
    from superplatform_web import state
    from superplatform_web.routes.market_v1 import router

    class BronzeStore:
        def query_series(self, *_args, **_kwargs):
            return pd.DataFrame(
                [
                    {"timestamp": "2026-01-01T00:00:00Z", "open": 100.0},
                    {"timestamp": "2026-01-01T00:01:00Z", "open": 101.0},
                ]
            )

    monkeypatch.setattr(state, "store", BronzeStore())
    monkeypatch.setattr(
        state,
        "resolve_provider_for_data_type",
        lambda exchange, market, data_type, **_kwargs: "binance-perp-kline",
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get(
        "/api/v1/market/klines",
        params={
            "exchange": "binance",
            "market_type": "perpetual",
            "symbol": "BTCUSDT",
            "frequency": "1m",
            "layer": "bronze",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["data_layer"] == "bronze"
    assert payload["meta"]["transformations"] == []
    assert payload["meta"]["has_more"] is True
    assert payload["data"] == [
        {"timestamp": "2026-01-01T00:01:00+00:00", "open": 101.0}
    ]
