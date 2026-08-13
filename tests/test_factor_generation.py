"""Tests for cache-backed factor-panel generation."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from superplatform.data.snapshot import DataSnapshot
from superplatform.data.store import Store, provider_table
from superplatform.factors.generator import generate_factor_panel_from_cache


def _cached_klines(
    *,
    symbol: str = "TESTUSDT",
    frequency: str = "1d",
    periods: int = 25,
    pandas_frequency: str = "D",
) -> pd.DataFrame:
    timestamps = pd.date_range(
        "2024-01-01",
        periods=periods,
        freq=pandas_frequency,
        tz="UTC",
    )
    close = pd.Series(np.arange(100, 100 + periods), dtype=float)
    return pd.DataFrame(
        {
            "symbol": symbol,
            "frequency": frequency,
            "timestamp": timestamps,
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000.0,
            "quote_volume": pd.NA,
            "trades": pd.NA,
            "taker_buy_volume": pd.NA,
            "taker_buy_quote_volume": pd.NA,
        }
    )


def _cached_funding(*, periods: int = 30) -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "symbol": "TESTUSDT",
            "frequency": "4h",
            "timestamp": timestamps,
            "funding_rate": np.linspace(0.00001, 0.00004, periods),
        }
    )


def _cached_open_interest(*, periods: int = 30) -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "symbol": "TESTUSDT",
            "frequency": "4h",
            "timestamp": timestamps,
            "open_interest": np.linspace(1_000.0, 1_300.0, periods),
        }
    )


def test_generate_factor_panel_from_cached_klines(tmp_path: Path) -> None:
    """The generator follows configured factors and emits finite UTC values."""
    cache_path = tmp_path / "cache.duckdb"
    config_path = tmp_path / "factors.yaml"
    config_path.write_text(
        """
factors:
  momentum:
    symbols: [TESTUSDT]
    providers:
      kline: test-kline
    frequency: 1d
    params:
      lookback_days: 20
  funding_rate_annualized:
    symbols: [TESTUSDT]
    providers:
      funding_rate: test-funding-rate
    frequency: 4h
    evaluation_price:
      provider: test-kline
      frequency: 4h
""".lstrip(),
        encoding="utf-8",
    )
    store = Store(cache_path)
    try:
        store.ensure_provider_table("test-kline", "kline")
        store.upsert(provider_table("test-kline"), _cached_klines())
    finally:
        store.close()

    result = generate_factor_panel_from_cache(
        cache_path=cache_path,
        output_dir=tmp_path / "factors",
        config_path=config_path,
    )

    panel = pd.read_csv(result.panel_path)
    panel["timestamp"] = pd.to_datetime(panel["timestamp"], utc=True)
    skipped = pd.read_csv(result.skipped_path)
    metadata = result.metadata_path.read_text(encoding="utf-8")
    assert panel.columns.tolist() == [
        "timestamp",
        "symbol",
        "factor_name",
        "factor_value",
    ]
    assert len(panel) == 5
    assert panel["timestamp"].dt.tz is not None
    assert not panel.duplicated(["timestamp", "symbol", "factor_name"]).any()
    assert panel["factor_value"].notna().all()
    # funding_rate_annualized needs a funding-rate provider not in this cache.
    assert skipped.loc[
        skipped["factor_name"].eq("funding_rate_annualized"),
        "reason",
    ].iloc[0].startswith(
        "generation_error:ValueError:Provider 'test-funding-rate'"
    )
    assert "close_t / close_{t-lookback_days} - 1" in metadata


def test_generate_factor_panel_from_multitype_snapshot(tmp_path: Path) -> None:
    """Funding and OI factors use cached non-kline data plus evaluation prices."""
    cache_path = tmp_path / "cache.duckdb"
    config_path = tmp_path / "factors.yaml"
    config_path.write_text(
        """
generation:
  frequency: 4h
  forward_bias:
    n_cutoffs: 3
    tolerance: 1.0e-12
factors:
  funding_rate_annualized:
    symbols: [TESTUSDT]
    providers:
      funding_rate: test-funding
    frequency: 4h
    evaluation_price:
      provider: test-kline
      frequency: 4h
  oi_price_divergence:
    symbols: [TESTUSDT]
    providers:
      kline: test-kline
      open_interest: test-open-interest
    frequencies:
      kline: 4h
      open_interest: 4h
    params:
      # 1 day at 4h = 6 bars; the 30-row cache gives non-NaN output.
      lookback_days: 1
""".lstrip(),
        encoding="utf-8",
    )
    store = Store(cache_path)
    try:
        store.ensure_provider_table("test-kline", "kline")
        store.ensure_provider_table("test-funding", "funding_rate")
        store.ensure_provider_table("test-open-interest", "open_interest")
        store.upsert(
            provider_table("test-kline"),
            _cached_klines(
                frequency="4h",
                periods=30,
                pandas_frequency="4h",
            ),
        )
        store.upsert(provider_table("test-funding"), _cached_funding())
        store.upsert(
            provider_table("test-open-interest"), _cached_open_interest()
        )
    finally:
        store.close()

    result = generate_factor_panel_from_cache(
        cache_path=cache_path,
        output_dir=tmp_path / "factors",
        config_path=config_path,
        run_id="multitype-test",
    )

    panel = pd.read_csv(result.panel_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    snapshot = json.loads(result.snapshot_manifest_path.read_text(encoding="utf-8"))
    bias = json.loads(result.forward_bias_path.read_text(encoding="utf-8"))

    assert set(panel["factor_name"]) == {
        "funding_rate_annualized",
        "oi_price_divergence",
    }
    assert set(panel["symbol"]) == {"TESTUSDT"}
    assert panel["factor_value"].notna().all()
    assert panel["timestamp"].str.endswith("Z").all()
    assert result.run_id == "multitype-test"
    assert manifest["snapshot_id"] == result.snapshot_id
    assert manifest["forward_bias_passed"] is True
    assert {
        (series["data_type"], series["frequency"])
        for series in snapshot["series"]
    } == {
        ("funding_rate", "4h"),
        ("kline", "4h"),
        ("open_interest", "4h"),
    }
    assert len(bias["reports"]) == 2
    assert all(report["passed"] for report in bias["reports"])


def test_generate_factor_panel_for_cross_asset_pair(tmp_path: Path) -> None:
    """A two-symbol factor uses one ordered group and keeps its group key."""
    cache_path = tmp_path / "cache.duckdb"
    config_path = tmp_path / "factors.yaml"
    config_path.write_text(
        """
generation:
  frequency: 1d
  forward_bias:
    n_cutoffs: 3
    tolerance: 1.0e-12
factors:
  cross_asset_relative_momentum:
    symbols: [[TESTUSDT, PEERUSDT]]
    providers:
      kline: test-kline
    frequency: 1d
    params:
      lookback_days: 5
""".lstrip(),
        encoding="utf-8",
    )
    peer = _cached_klines(symbol="PEERUSDT", periods=30)
    peer["close"] = np.linspace(80.0, 150.0, len(peer))
    store = Store(cache_path)
    try:
        store.ensure_provider_table("test-kline", "kline")
        store.upsert(provider_table("test-kline"), _cached_klines(periods=30))
        store.upsert(provider_table("test-kline"), peer)
    finally:
        store.close()

    result = generate_factor_panel_from_cache(
        cache_path=cache_path,
        output_dir=tmp_path / "factors",
        config_path=config_path,
    )

    panel = pd.read_csv(result.panel_path)
    bias = json.loads(result.forward_bias_path.read_text(encoding="utf-8"))

    assert set(panel["factor_name"]) == {"cross_asset_relative_momentum"}
    assert set(panel["symbol"]) == {"TESTUSDT_PEERUSDT"}
    assert panel["factor_value"].notna().all()
    assert bias["reports"][0]["passed"] is True


def test_snapshot_id_depends_only_on_requested_normalized_series(
    tmp_path: Path,
) -> None:
    """Unrelated cache writes must not change a requested input snapshot ID."""
    cache_path = tmp_path / "cache.duckdb"
    store = Store(cache_path)
    try:
        store.ensure_provider_table("test-kline", "kline")
        store.upsert(
            provider_table("test-kline"),
            _cached_klines(symbol="TESTUSDT"),
        )
        store.upsert(
            provider_table("test-kline"),
            _cached_klines(symbol="OTHERUSDT"),
        )
    finally:
        store.close()

    request = [("test-kline", "TESTUSDT", "1d")]
    with DataSnapshot(cache_path) as snapshot:
        before, manifest = snapshot.describe(request)

    store = Store(cache_path)
    try:
        store.upsert(
            provider_table("test-kline"),
            _cached_klines(symbol="UNRELATEDUSDT", periods=30),
        )
    finally:
        store.close()

    with DataSnapshot(cache_path) as snapshot:
        after, updated_manifest = snapshot.describe(request)

    assert before == after
    assert manifest["source_cache_sha256"] != updated_manifest["source_cache_sha256"]
