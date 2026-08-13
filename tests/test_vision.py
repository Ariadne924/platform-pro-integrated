"""Tests for the Binance vision archive client (data.binance.vision)."""

import io
import zipfile
from datetime import UTC, datetime

import pandas as pd
import pytest
import requests

from superplatform.network.binance.vision import (
    BinanceVisionClient,
    _funding_rate_monthly_url,
    _klines_monthly_url,
    _klines_url,
    _metrics_monthly_url,
    _metrics_url,
    parse_funding_rate_archive,
    parse_kline_archive,
    parse_metrics_archive,
    resample_to_period,
)

_METRICS_CSV = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
    "2024-01-01 00:00:00,BTCUSDT,100.0,1000000.0,1.0,1.0,1.0,1.0\n"
    "2024-01-01 00:05:00,BTCUSDT,110.0,1100000.0,1.0,1.0,1.0,1.0\n"
    "2024-01-01 00:10:00,BTCUSDT,120.0,1200000.0,1.0,1.0,1.0,1.0\n"
    "2024-01-02 00:00:00,BTCUSDT,200.0,2000000.0,1.0,1.0,1.0,1.0\n"
)


def _zip_of(content: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("metrics.csv", content)
    return buffer.getvalue()


def test_metrics_url_format():
    url = _metrics_url("BTCUSDT", datetime(2024, 1, 1, tzinfo=UTC))
    assert url.endswith("/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-01.zip")


def test_metrics_monthly_url_format():
    url = _metrics_monthly_url("BTCUSDT", datetime(2024, 1, 15, tzinfo=UTC))
    assert url.endswith("/data/futures/um/monthly/metrics/BTCUSDT/BTCUSDT-metrics-2024-01.zip")


def test_klines_url_format():
    # Binance vision kline archives live under an interval sub-directory:
    # daily/klines/{symbol}/{interval}/{symbol}-{interval}-{date}.zip
    url = _klines_url("BTCUSDT", "1h", datetime(2024, 1, 1, tzinfo=UTC))
    assert url.endswith(
        "/data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01-01.zip"
    )
    spot = _klines_url("BTCUSDT", "1d", datetime(2024, 1, 1, tzinfo=UTC), "spot")
    assert spot.endswith(
        "/data/spot/daily/klines/BTCUSDT/1d/BTCUSDT-1d-2024-01-01.zip"
    )


def test_klines_monthly_url_format():
    url = _klines_monthly_url("BTCUSDT", "1h", datetime(2024, 1, 15, tzinfo=UTC))
    assert url.endswith(
        "/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"
    )


def _klines_csv(rows) -> str:
    """Rows as (timestamp_ms, close) → a 12-field kline CSV (no header)."""
    lines = []
    for ts, close in rows:
        lines.append(
            f"{ts},{close},{close + 1},{close - 1},{close},"
            f"10,{ts + 3600000},1000,5,500,500,0"
        )
    return "\n".join(lines) + "\n"


def test_parse_kline_archive():
    ts = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    df = parse_kline_archive(_zip_of(_klines_csv([(ts, 100.0), (ts + 60_000, 101.0)])))
    assert list(df.columns) == [
        "timestamp", "open", "high", "low", "close",
        "volume", "quote_volume", "trades",
        "taker_buy_volume", "taker_buy_quote_volume",
    ]
    assert len(df) == 2
    assert df["close"].tolist() == [100.0, 101.0]
    assert df["trades"].tolist() == [5.0, 5.0]
    assert str(df["timestamp"].dtype.tz) == "UTC"


def test_parse_kline_archive_drops_header_row():
    ts = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    header = (
        "open_time,open,high,low,close,volume,close_time,quote_asset_volume,"
        "number_of_trades,taker_buy_base_asset_volume,"
        "taker_buy_quote_asset_volume,ignore\n"
    )
    df = parse_kline_archive(_zip_of(header + _klines_csv([(ts, 100.0)])))
    assert len(df) == 1
    assert df["close"].iloc[0] == 100.0


def test_parse_kline_archive_empty_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("klines.csv", "")
    df = parse_kline_archive(buffer.getvalue())
    assert df.empty


def test_parse_metrics_archive():
    df = parse_metrics_archive(_zip_of(_METRICS_CSV))
    assert list(df.columns) == ["timestamp", "open_interest"]
    assert len(df) == 4
    assert df["open_interest"].tolist() == [100.0, 110.0, 120.0, 200.0]
    assert str(df["timestamp"].dtype.tz) == "UTC"


def test_parse_metrics_archive_empty_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("empty.csv", "")
    df = parse_metrics_archive(buffer.getvalue())
    assert df.empty
    assert list(df.columns) == ["timestamp", "open_interest"]


def test_funding_rate_monthly_url_format():
    url = _funding_rate_monthly_url("BTCUSDT", datetime(2024, 1, 15, tzinfo=UTC))
    assert url.endswith(
        "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    )


def test_parse_funding_rate_archive():
    csv = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1704067200000,8,0.00037409\n"
        "1704103200000,8,0.00010000\n"
    )
    df = parse_funding_rate_archive(_zip_of(csv))
    assert list(df.columns) == ["timestamp", "funding_rate"]
    assert len(df) == 2
    assert df["funding_rate"].tolist() == [0.00037409, 0.0001]
    assert str(df["timestamp"].dtype.tz) == "UTC"


def test_parse_funding_rate_archive_empty_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("empty.csv", "")
    df = parse_funding_rate_archive(buffer.getvalue())
    assert df.empty
    assert list(df.columns) == ["timestamp", "funding_rate"]


def test_resample_to_period_takes_last_value_per_day():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=10, freq="5min", tz="UTC"),
        "open_interest": range(10),
    })
    daily = resample_to_period(df, "1d")
    # One observation per UTC day.
    assert len(daily) == 1
    assert daily["timestamp"].iloc[0] == pd.Timestamp("2024-01-01", tz="UTC")
    # Last 5m observation of the day (index 9 → value 9).
    assert daily["open_interest"].iloc[0] == 9.0


def test_resample_4h_buckets():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=6, freq="5min", tz="UTC"),
        "open_interest": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    })
    buckets = resample_to_period(df, "4h")
    # 00:00-03:55 → one bucket; last value 6.0 (all within first 4h bucket).
    assert len(buckets) == 1
    assert buckets["open_interest"].iloc[0] == 6.0


def test_resample_unsupported_period_raises():
    df = pd.DataFrame(columns=["timestamp", "open_interest"])
    with pytest.raises(ValueError, match="unsupported open-interest period"):
        resample_to_period(df, "7m")


class _FakeSession:
    """Synchronous session stub for the vision client's HTTP layer.

    ``exists_from`` (YYYY-MM-DD) marks the first date whose archive exists;
    earlier dates answer HEAD/GET with 404.  ``archives`` maps date strings
    to archive bytes for downloads.
    """

    def __init__(self, archives: dict | None = None, exists_from: str = "2024-01-01"):
        self.archives = archives or {}
        self.exists_from = exists_from
        self.urls: list[str] = []        # GET (downloads)
        self.head_urls: list[str] = []   # HEAD (probes)

    def _date_of(self, url: str) -> str | None:
        import re
        # Any archive filename: ...-YYYY-MM.zip or ...-YYYY-MM-DD.zip
        # (metrics: {symbol}-metrics-..., klines: {symbol}-{interval}-...).
        match = re.search(r"(\d{4}-\d{2}(?:-\d{2})?)\.zip", url)
        return match.group(1) if match else None

    def _exists(self, url: str) -> bool:
        date = self._date_of(url)
        return date is not None and date >= self.exists_from

    def get(self, url, timeout=None):
        self.urls.append(url)
        date = self._date_of(url)
        if date in self.archives:
            return _FakeResponse(200, self.archives[date])
        return _FakeResponse(404, None)

    def head(self, url, timeout=None):
        self.head_urls.append(url)
        if self._exists(url):
            return _FakeResponse(200, None)
        return _FakeResponse(404, None)


class _FakeResponse:
    def __init__(self, status: int, content: bytes | None):
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


@pytest.mark.asyncio
async def test_vision_client_downloads_daily_archives_and_resamples():
    client = BinanceVisionClient()
    archive = _zip_of(_METRICS_CSV)
    client._session = _FakeSession(
        archives={"2024-01-01": archive, "2024-01-02": archive},
        exists_from="2024-01-01",
    )

    df = await client.fetch_metrics_range(
        "BTCUSDT",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 2, tzinfo=UTC),
        period="1d",
    )
    # Two UTC dates → two archive downloads → two daily observations.
    assert len(client._session.urls) == 2
    assert df["timestamp"].tolist() == [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
    ]
    assert df["open_interest"].tolist() == [120.0, 200.0]


@pytest.mark.asyncio
async def test_vision_client_uses_monthly_archive_for_full_month():
    """A whole month is fetched with one monthly archive, not ~30 dailies."""
    client = BinanceVisionClient()
    archive = _zip_of(_METRICS_CSV)
    session = _FakeSession(
        archives={"2024-01": archive},
        exists_from="2024-01-01",
    )
    client._session = session

    df = await client.fetch_metrics_range(
        "BTCUSDT",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 31, tzinfo=UTC),  # covers all of January
        period="1d",
    )
    # One download: the monthly archive URL.
    assert len(session.urls) == 1
    assert "/monthly/metrics/" in session.urls[0]
    assert df["timestamp"].tolist() == [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
    ]
    assert df["open_interest"].tolist() == [120.0, 200.0]


@pytest.mark.asyncio
async def test_vision_client_monthly_missing_falls_back_to_daily():
    """A missing monthly archive (404) falls back to per-day downloads."""
    client = BinanceVisionClient()
    archive = _zip_of(_METRICS_CSV)
    session = _FakeSession(
        archives={"2024-01-01": archive, "2024-01-02": archive},
        exists_from="2024-01-01",
    )
    client._session = session

    df = await client.fetch_metrics_range(
        "BTCUSDT",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 31, tzinfo=UTC),
        period="1d",
    )
    # The monthly attempt 404s, so all 31 days are downloaded individually.
    assert len(session.urls) == 1 + 31
    assert "/monthly/metrics/" in session.urls[0]
    assert sum("/daily/metrics/" in url for url in session.urls) == 31
    assert df["timestamp"].tolist() == [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
    ]
    assert df["open_interest"].tolist() == [120.0, 200.0]


@pytest.mark.asyncio
async def test_vision_client_uses_monthly_kline_archive_for_full_month():
    """A whole month of klines is one monthly archive, not ~30 dailies."""
    client = BinanceVisionClient()
    ts = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    archive = _zip_of(_klines_csv([(ts, 100.0)]))
    session = _FakeSession(archives={"2024-01": archive}, exists_from="2024-01-01")
    client._session = session

    df = await client.fetch_klines_range(
        "BTCUSDT", "1h", "futures/um",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 31, tzinfo=UTC),
    )
    assert len(session.urls) == 1
    assert "/monthly/klines/" in session.urls[0]
    assert df["close"].tolist() == [100.0]


@pytest.mark.asyncio
async def test_prime_earliest_skips_binary_search():
    """A listed_at hint reduces earliest-discovery to one verification HEAD."""
    client = BinanceVisionClient()
    archive = _zip_of(_METRICS_CSV)
    session = _FakeSession(
        archives={"2024-01-01": archive, "2024-01-02": archive},
        exists_from="2024-01-01",
    )
    client._session = session
    client.prime_earliest("BTCUSDT", datetime(2024, 1, 1, tzinfo=UTC))

    df = await client.fetch_metrics_range(
        "BTCUSDT",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 2, tzinfo=UTC),
        period="1d",
    )
    # One verification HEAD (not the ~17-probe binary search).
    assert len(session.head_urls) == 1
    assert df["timestamp"].tolist() == [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
    ]
    assert df["open_interest"].tolist() == [120.0, 200.0]


_SINGLE_DAY_CSV = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
    "2024-01-02 00:00:00,BTCUSDT,200.0,2000000.0,1.0,1.0,1.0,1.0\n"
    "2024-01-02 00:05:00,BTCUSDT,210.0,2100000.0,1.0,1.0,1.0,1.0\n"
)


@pytest.mark.asyncio
async def test_vision_client_clamps_before_archive_start():
    """Dates before the archive's confirmed start are never requested."""
    client = BinanceVisionClient()
    archive = _zip_of(_SINGLE_DAY_CSV)
    session = _FakeSession(archives={"2024-01-02": archive}, exists_from="2024-01-02")
    client._session = session

    df = await client.fetch_metrics_range(
        "BTCUSDT",
        start=datetime(2024, 1, 1, tzinfo=UTC),  # before archive start
        end=datetime(2024, 1, 3, tzinfo=UTC),
        period="1d",
    )
    # The day before the archive start (01-01) is never requested; the two
    # days at/after it are, and 01-03 (no archive) yields no rows.
    assert len(session.urls) == 2
    assert not any("metrics-2024-01-01.zip" in url for url in session.urls)
    assert df["timestamp"].tolist() == [pd.Timestamp("2024-01-02", tz="UTC")]
    assert df["open_interest"].tolist() == [210.0]


@pytest.mark.asyncio
async def test_vision_uses_requested_end_for_earliest_archive_probe():
    """A historical request must not require a current-day archive."""
    client = BinanceVisionClient()
    archive = _zip_of(_klines_csv([(1704067200000, 100.0)]))
    session = _FakeSession(
        archives={"2024-01-01": archive},
        exists_from="2024-01-01",
    )
    client._session = session

    df = await client.fetch_klines_range(
        "BTCUSDT",
        "5m",
        "futures/um",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 2, tzinfo=UTC),
    )

    assert not df.empty
    assert df["timestamp"].tolist() == [pd.Timestamp("2024-01-01", tz="UTC")]


@pytest.mark.asyncio
async def test_vision_client_empty_when_no_archive_exists():
    """A symbol with no archives at all returns empty without downloads."""
    client = BinanceVisionClient()
    client._session = _FakeSession(archives={}, exists_from="2099-01-01")

    df = await client.fetch_metrics_range(
        "BTCUSDT",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 3, tzinfo=UTC),
        period="1d",
    )
    assert df.empty
    assert client._session.urls == []  # no GET at all


@pytest.mark.asyncio
async def test_vision_client_empty_range():
    client = BinanceVisionClient()
    client._session = _FakeSession(archives={"2024-01-01": _zip_of(_METRICS_CSV)})

    df = await client.fetch_metrics_range(
        "BTCUSDT",
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 1, tzinfo=UTC),
        period="1d",
    )
    assert df.empty
    assert client._session.urls == []
    assert client._session.head_urls == []


@pytest.mark.asyncio
async def test_vision_client_rejects_period_outside_supported():
    client = BinanceVisionClient()
    client._session = _FakeSession(archives={"2024-01-01": _zip_of(_METRICS_CSV)})
    with pytest.raises(ValueError, match="unsupported open-interest period"):
        await client.fetch_metrics_range(
            "BTCUSDT",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 2, tzinfo=UTC),
            period="9m",
        )


def test_vision_client_semaphore_limits_concurrency():
    """The client bounds concurrent downloads with a semaphore."""
    client = BinanceVisionClient(max_concurrent=3)
    assert client._semaphore._value == 3


def test_adapter_wires_vision_concurrency_from_setup():
    """The adapter's shared vision client honors ``vision_max_concurrent``.

    All symbols' archive downloads funnel through this one client, so the
    semaphore here — not the fetch coordinator — caps multi-symbol cold
    fetches. ``setup_providers`` passes ``data.max_concurrent_requests``
    through; the default stays 8 for direct construction.
    """
    from superplatform.data.providers.binance_common import create_binance_adapter

    boosted = create_binance_adapter(vision_max_concurrent=16)
    assert boosted._vision._semaphore._value == 16

    default = create_binance_adapter()
    assert default._vision._semaphore._value == 8
