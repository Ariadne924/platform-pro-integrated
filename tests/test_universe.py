"""Tests for the full-market symbol universe: adapter, store, sync, routes.

Covers: BinanceAdapter.fetch_universe filtering, the Store universe table,
the absence- and status-based delisting reconciliation, and the
GET/POST /api/data/universe route contract (config fallback + refresh).
"""

import pandas as pd
import pytest
import yaml

import superplatform_web.state as _state
from superplatform.data.store import Store
from superplatform.network.binance import BinanceAdapter


def _universe_frame(symbols, status="TRADING"):
    """A fetch_universe-shaped DataFrame over the given symbols."""
    return pd.DataFrame([{
        "exchange": "binance",
        "symbol": s,
        "contract_type": "PERPETUAL",
        "status": status,
        "base_asset": s,
        "quote_asset": "USDT",
        "listed_at": pd.Timestamp("2020-01-01", tz="UTC"),
    } for s in symbols])


class FakeFutures:
    """UMFutures-like client returning a canned exchangeInfo payload."""

    def __init__(self, payload):
        self._payload = payload

    def exchange_info(self):
        return self._payload


class FakeSpot:
    pass


class FakeUniverseAdapter:
    """Fake adapter returning canned fetch_universe frames, one per call."""

    def __init__(self, frames):
        self._frames = list(frames)

    async def fetch_universe(self, market_type):
        return self._frames.pop(0)


def _exchange_info_payload():
    return {"symbols": [
        {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
         "status": "TRADING", "baseAsset": "BTC", "onboardDate": 1700000000000},
        {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
         "status": "TRADING", "baseAsset": "ETH", "onboardDate": 1500000000000},
        # Perpetual but non-USDT quote → filtered out.
        {"symbol": "XRPUSDC", "contractType": "PERPETUAL", "quoteAsset": "USDC",
         "status": "TRADING", "baseAsset": "XRP", "onboardDate": 1600000000000},
        # USDT but a delivery contract, not perpetual → filtered out.
        {"symbol": "SOLUSDT", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT",
         "status": "TRADING", "baseAsset": "SOL", "onboardDate": 1650000000000},
        # CLOSE status is kept so the sync layer can mark it delisted.
        {"symbol": "DOGEUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
         "status": "CLOSE", "baseAsset": "DOGE", "onboardDate": 1550000000000},
        # No onboardDate → listed_at must be NaT, not an error.
        {"symbol": "LTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
         "status": "TRADING", "baseAsset": "LTC"},
    ]}


@pytest.mark.asyncio
async def test_fetch_universe_filters_perpetual_usdt():
    adapter = BinanceAdapter(
        spot_client=FakeSpot(), futures_client=FakeFutures(_exchange_info_payload())
    )
    df = await adapter.fetch_universe()

    assert set(df["symbol"]) == {"BTCUSDT", "ETHUSDT", "DOGEUSDT", "LTCUSDT"}
    assert set(df["status"]) == {"TRADING", "CLOSE"}
    assert df["listed_at"].dt.tz is not None
    assert df.loc[df["symbol"] == "LTCUSDT", "listed_at"].isna().iloc[0]


def test_store_universe_roundtrip(tmp_path):
    store = Store(tmp_path / "cache.duckdb")
    try:
        df = pd.DataFrame([{
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "contract_type": "PERPETUAL",
            "status": "TRADING",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "listed_at": pd.Timestamp("2023-01-01", tz="UTC"),
            "delisted_at": pd.NaT,
            "updated_at": pd.Timestamp("2024-01-01", tz="UTC"),
        }])
        assert store.upsert_universe(df) == 1
        out = store.query_universe()
        assert len(out) == 1
        assert out["symbol"].iloc[0] == "BTCUSDT"
        assert out["updated_at"].iloc[0].tz is not None
        assert out["listed_at"].iloc[0].tz is not None
        # Idempotent re-upsert on the (exchange, symbol) PK.
        store.upsert_universe(df)
        assert len(store.query_universe()) == 1
    finally:
        store.close()


def test_prime_vision_from_universe_seeds_adapter(tmp_path):
    """listed_at primes each Binance adapter's vision earliest-date cache."""
    from superplatform.data.providers.binance_open_interest import (
        BinanceOpenInterestProvider,
    )
    from superplatform_web.universe import prime_vision_from_universe

    store = Store(tmp_path / "cache.duckdb")
    try:
        df = pd.DataFrame([{
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "contract_type": "PERPETUAL",
            "status": "TRADING",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "listed_at": pd.Timestamp("2020-01-01", tz="UTC"),
            "delisted_at": pd.NaT,
            "updated_at": pd.Timestamp("2024-01-01", tz="UTC"),
        }])
        store.upsert_universe(df)

        provider = BinanceOpenInterestProvider()
        _state.providers.register(provider)

        assert prime_vision_from_universe(store) == 1
        hints = provider.adapter._vision._earliest_hints
        assert pd.Timestamp(hints["BTCUSDT"]) == pd.Timestamp("2020-01-01", tz="UTC")
    finally:
        store.close()
        _state.providers.clear()


@pytest.mark.asyncio
async def test_sync_adds_and_marks_delisted(tmp_path):
    from superplatform_web.universe import sync_universe

    store = Store(tmp_path / "cache.duckdb")
    try:
        adapter = FakeUniverseAdapter([
            _universe_frame(["A", "B", "C"]),
            _universe_frame(["A", "B"]),       # C disappears → absence-delisted
            _universe_frame(["A", "B", "C"]),  # C returns → re-listed
        ])

        r1 = await sync_universe(adapter=adapter, store=store)
        assert r1 == {"synced": True, "added": 3, "updated": 0, "delisted": 0, "total": 3}

        r2 = await sync_universe(adapter=adapter, store=store)
        assert r2["added"] == 0 and r2["delisted"] == 1 and r2["total"] == 3
        out = store.query_universe()
        c_row = out[out["symbol"] == "C"]
        assert c_row["delisted_at"].notna().iloc[0]

        # Re-adding C as active counts as a fresh listing and clears the stamp.
        r3 = await sync_universe(adapter=adapter, store=store)
        assert r3["added"] == 1 and r3["delisted"] == 0
        out = store.query_universe()
        c_row = out[out["symbol"] == "C"]
        assert c_row["delisted_at"].isna().iloc[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_sync_status_based_delisting(tmp_path):
    from superplatform_web.universe import sync_universe

    store = Store(tmp_path / "cache.duckdb")
    try:
        # D stays in the snapshot but its status flips to CLOSE → delisted
        # without ever going absent.
        adapter = FakeUniverseAdapter([
            _universe_frame(["D"], status="CLOSE"),
        ])
        r = await sync_universe(adapter=adapter, store=store)
        # Not an active addition — the row is tracked only to mark it delisted.
        assert r["added"] == 0 and r["delisted"] == 1
        out = store.query_universe()
        d_row = out[out["symbol"] == "D"]
        assert d_row["delisted_at"].notna().iloc[0]
    finally:
        store.close()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point _state at tmp YAML files and reset providers / store / jobs."""
    monkeypatch.setattr(_state, "_CONFIG_FILES", (
        str(tmp_path / "default.yaml"),
        str(tmp_path / "exchanges.yaml"),
        str(tmp_path / "factors.yaml"),
        str(tmp_path / "settings.yaml"),
    ))
    monkeypatch.setattr("superplatform_web.app._EXPERIMENTS_PATH", tmp_path / "experiments.duckdb")
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()
    yield tmp_path
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()


def _write_config(tmp_path, **data_overrides):
    cfg = {
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "data": {"symbols": {"perpetual": ["A", "B"]}},
    }
    if data_overrides:
        cfg["data"].update(data_overrides)
    (tmp_path / "default.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    for name in ("exchanges", "factors", "settings"):
        (tmp_path / f"{name}.yaml").write_text("", encoding="utf-8")


def test_get_universe_config_fallback(isolated_state):
    """Without a cache store the route falls back to the config symbol pool."""
    tmp_path = isolated_state
    _write_config(tmp_path)

    from fastapi.testclient import TestClient

    from superplatform_web.app import app

    with TestClient(app) as client:
        r = client.get("/api/data/universe")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "config"
        assert body["symbols"] == ["A", "B"]
        assert body["count"] == 2
        assert body["updated_at"] is None


def test_refresh_universe_counts(isolated_state, monkeypatch):
    """POST /universe/refresh syncs through the adapter and returns counts."""
    tmp_path = isolated_state
    _write_config(tmp_path, cache={
        "enabled": True,
        "path": str(tmp_path / "cache.duckdb"),
    })

    import superplatform_web.universe as universe_mod

    adapter = FakeUniverseAdapter([_universe_frame(["A", "B", "C"])])
    monkeypatch.setattr(universe_mod, "_get_adapter", lambda: adapter)
    # Keep the lifespan's fire-and-forget sync out of the assertion.
    monkeypatch.setattr(universe_mod, "_is_stale", lambda store: False)

    from fastapi.testclient import TestClient

    from superplatform_web.app import app

    with TestClient(app) as client:
        r = client.post("/api/data/universe/refresh")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["synced"] is True
        assert body["added"] == 3
        assert body["updated"] == 0
        assert body["delisted"] == 0
        assert body["total"] == 3

        # The refreshed universe is now served from the store.
        listing = client.get("/api/data/universe").json()
        assert listing["source"] == "universe"
        assert listing["symbols"] == ["A", "B", "C"]
