"""Pagination tests for BinanceAdapter historical data methods."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from binance.error import ClientError

from superplatform.data.enums import MarketType
from superplatform.network.base import KLineInterval
from superplatform.network.binance import BinanceAdapter


class MockClient:
    """Binance-SDK-like client returning canned data and recording calls."""

    def __init__(self, *, funding=None, open_interest=None, klines=None):
        self.funding = funding or []
        self.open_interest = open_interest or []
        self.klines_data = klines or []
        self.funding_calls = []
        self.open_interest_calls = []
        self.klines_calls = []

    def klines(self, symbol, interval, **kwargs):
        self.klines_calls.append((symbol, interval, dict(kwargs)))
        start_time = kwargs.get("startTime")
        end_time = kwargs.get("endTime")
        limit = kwargs.get("limit", 500)
        filtered = [
            bar for bar in self.klines_data
            if (start_time is None or bar[0] >= start_time)
            and (end_time is None or bar[0] <= end_time)
        ]
        return filtered[:limit]

    def funding_rate(self, symbol, **kwargs):
        self.funding_calls.append((symbol, dict(kwargs)))
        start_time = kwargs.get("startTime")
        end_time = kwargs.get("endTime")
        limit = kwargs.get("limit", 500)
        filtered = [
            row for row in self.funding
            if (start_time is None or row["fundingTime"] >= start_time)
            and (end_time is None or row["fundingTime"] <= end_time)
        ]
        return filtered[:limit]

    def open_interest_hist(self, symbol, period, **kwargs):
        self.open_interest_calls.append((symbol, period, dict(kwargs)))
        limit = kwargs.get("limit", 500)
        return self.open_interest[:limit]


class MockVision:
    """Vision archive client stub returning canned frames and recording calls."""

    def __init__(self, frames, kline_frames=None, funding_frames=None):
        self.frames = list(frames)
        self.kline_frames = list(kline_frames or [])
        self.funding_frames = list(funding_frames or [])
        self.calls = []
        self.kline_calls = []
        self.funding_calls = []

    async def fetch_metrics_range(self, symbol, start, end, *, period="1d"):
        self.calls.append((symbol, start, end, period))
        return self.frames.pop(0) if self.frames else pd.DataFrame(
            columns=["timestamp", "open_interest"]
        )

    async def fetch_klines_range(self, symbol, interval, market_path, start, end):
        self.kline_calls.append((symbol, interval, market_path, start, end))
        return self.kline_frames.pop(0) if self.kline_frames else pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close",
                     "volume", "quote_volume", "trades",
                     "taker_buy_volume", "taker_buy_quote_volume"]
        )

    async def fetch_funding_rate_range(self, symbol, start, end):
        self.funding_calls.append((symbol, start, end))
        return self.funding_frames.pop(0) if self.funding_frames else pd.DataFrame(
            columns=["timestamp", "funding_rate"]
        )


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _bar(timestamp_ms: int, close: float) -> list:
    """One Binance-native kline row (12 fields)."""
    return [
        timestamp_ms, str(close), str(close + 1), str(close - 1), str(close),
        "10", timestamp_ms + 86_400_000, "1000", "5", "500", "500", "0",
    ]


def _kline_frame(rows):
    """rows as (timestamp_ms, close) → an adapter-shaped kline DataFrame."""
    return pd.DataFrame([{
        "timestamp": pd.Timestamp(datetime.fromtimestamp(ts / 1000.0, tz=UTC)),
        "open": float(c), "high": float(c + 1), "low": float(c - 1),
        "close": float(c), "volume": 10.0, "quote_volume": 1000.0,
        "trades": 5.0, "taker_buy_volume": 500.0, "taker_buy_quote_volume": 500.0,
    } for ts, c in rows])


class _RaisingVision:
    """Vision client that fails the test if the kline hybrid path is taken."""

    async def fetch_klines_range(self, *args, **kwargs):
        raise AssertionError("short kline range must stay on REST")


def _funding_frame(timestamps: list, rate: float = 0.0001) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": timestamps,
        "funding_rate": [rate] * len(timestamps),
    })


@pytest.mark.asyncio
async def test_request_retries_rate_limit_then_succeeds():
    """A 418 weight-limit response is retried (retry-after wait), not dropped."""
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=MockClient(),
        vision=MockVision([]),
    )
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ClientError(418, -1003, "way too many", {"retry-after": "1"})
        return "ok"

    result = await adapter._request(flaky)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_request_does_not_retry_non_rate_limit_error():
    """A 400 Invalid-symbol error is a permanent failure — raised immediately."""
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=MockClient(),
        vision=MockVision([]),
    )
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ClientError(400, -1121, "Invalid symbol.", {})

    with pytest.raises(ClientError):
        await adapter._request(bad)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_funding_rate_uses_vision_archives():
    """Funding history comes from vision archives, not the WAF'd REST endpoint."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    step = timedelta(hours=8)
    timestamps = [start + i * step for i in range(6)]
    vision = MockVision(
        [], funding_frames=[_funding_frame(timestamps)]
    )
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=MockClient(),
        vision=vision,
    )

    data = await adapter.fetch_funding_rate(
        "BTCUSDT",
        start=start,
        end=start + 5 * step,
    )

    assert list(data.columns) == ["timestamp", "funding_rate"]
    assert len(data) == 6
    # Vision, not the REST endpoint, is the source.
    assert len(vision.funding_calls) == 1
    assert vision.funding_calls[0][0] == "BTCUSDT"


@pytest.mark.asyncio
async def test_funding_rate_defaults_range_when_omitted():
    """start/end omitted → a wide default range, still served from vision."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    vision = MockVision([], funding_frames=[_funding_frame([start])])
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=MockClient(),
        vision=vision,
    )

    data = await adapter.fetch_funding_rate("BTCUSDT")

    assert len(data) == 1
    assert len(vision.funding_calls) == 1


@pytest.mark.asyncio
async def test_open_interest_merges_rest_recent_with_vision_history():
    """REST serves the recent tail; vision fills the older portion."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    step = timedelta(days=1)
    rest_timestamps = [
        _milliseconds(start + i * step) for i in (3, 4)  # REST covers Jan 4-5
    ]
    futures = MockClient(open_interest=[
        {"symbol": "BTCUSDT", "sumOpenInterest": "123.45",
         "sumOpenInterestValue": "100000", "timestamp": ts}
        for ts in rest_timestamps
    ])
    vision_timestamps = [
        pd.Timestamp(start + i * step).tz_convert("UTC") for i in (0, 1, 2)  # Jan 1-3
    ]
    vision = MockVision([pd.DataFrame({
        "timestamp": vision_timestamps,
        "open_interest": [10.0, 20.0, 30.0],
    })])
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=futures,
        vision=vision,
    )

    data = await adapter.fetch_open_interest(
        "BTCUSDT",
        MarketType.PERPETUAL,
        period="1d",
        start=start,
        end=start + 4 * step,
        limit=2,
    )

    expected = [start + i * step for i in range(5)]
    assert list(data["timestamp"].astype("int64") // 1_000_000) == [
        _milliseconds(ts) for ts in expected
    ]
    # REST called once, no time bounds (the endpoint rejects them).
    assert len(futures.open_interest_calls) == 1
    symbol, period, kwargs = futures.open_interest_calls[0]
    assert symbol == "BTCUSDT"
    assert period == "1d"
    assert kwargs["limit"] == 2
    assert "startTime" not in kwargs and "endTime" not in kwargs
    # Vision asked only for the portion before REST coverage.
    assert len(vision.calls) == 1
    _, v_start, v_end, period = vision.calls[0]
    assert v_start == start
    assert v_end == pd.Timestamp(rest_timestamps[0], unit="ms", tz="UTC")
    assert period == "1d"


@pytest.mark.asyncio
async def test_open_interest_falls_back_to_vision_when_rest_empty():
    """When REST returns nothing, vision must supply the whole range."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    step = timedelta(days=1)
    futures = MockClient(open_interest=[])
    vision_timestamps = [
        pd.Timestamp(start + i * step).tz_convert("UTC") for i in range(3)
    ]
    vision = MockVision([pd.DataFrame({
        "timestamp": vision_timestamps,
        "open_interest": [10.0, 20.0, 30.0],
    })])
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=futures,
        vision=vision,
    )

    data = await adapter.fetch_open_interest(
        "BTCUSDT",
        MarketType.PERPETUAL,
        period="1d",
        start=start,
        end=start + 2 * step,
        limit=500,
    )

    assert len(data) == 3
    assert len(vision.calls) == 1
    assert vision.calls[0][1] == start
    assert vision.calls[0][2] == start + 2 * step


@pytest.mark.asyncio
async def test_basis_paginates_spot_and_perpetual_price_legs():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    step = timedelta(days=1)
    timestamps = [_milliseconds(start + i * step) for i in range(5)]

    def bars(price_offset):
        return [
            _bar(ts, 100 + i + price_offset)
            for i, ts in enumerate(timestamps)
        ]

    spot = MockClient(klines=bars(0))
    futures = MockClient(klines=bars(10))
    adapter = BinanceAdapter(
        spot_client=spot,
        futures_client=futures,
        vision=MockVision([]),
    )

    data = await adapter.fetch_basis(
        "BTCUSDT",
        start=start,
        end=start + 4 * step,
        limit=2,
    )

    assert list(data["timestamp"].astype("int64") // 1_000_000) == timestamps
    assert len(spot.klines_calls) == 3
    assert len(futures.klines_calls) == 3
    assert data["basis_pct"].notna().all()


@pytest.mark.asyncio
async def test_klines_parse_full_binance_columns():
    """Kline responses carry the full 10-column schema (no NaN)."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars = [_bar(_milliseconds(start), 100.0)]
    futures = MockClient(klines=bars)
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=futures,
        vision=MockVision([]),
    )

    data = await adapter.fetch_klines(
        "BTCUSDT", KLineInterval.D1, MarketType.PERPETUAL,
        start=start, end=start,
    )

    assert list(data.columns) == [
        "timestamp", "open", "high", "low", "close",
        "volume", "quote_volume", "trades",
        "taker_buy_volume", "taker_buy_quote_volume",
    ]
    assert data["quote_volume"].iloc[0] == 1000.0
    assert data["trades"].iloc[0] == 5.0
    assert not data[["quote_volume", "trades",
                     "taker_buy_volume", "taker_buy_quote_volume"]].isna().any().any()


@pytest.mark.asyncio
async def test_klines_short_range_stays_on_rest():
    """Ranges needing few REST pages never touch the vision client."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars = [_bar(_milliseconds(start + timedelta(days=i)), 100.0) for i in range(3)]
    futures = MockClient(klines=bars)
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=futures,
        vision=_RaisingVision(),
    )

    data = await adapter.fetch_klines(
        "BTCUSDT", KLineInterval.D1, MarketType.PERPETUAL,
        start=start, end=start + timedelta(days=2),
    )
    assert len(data) == 3
    assert any("startTime" in kw for _, _, kw in futures.klines_calls)


@pytest.mark.asyncio
async def test_klines_long_range_uses_vision_bulk_plus_rest_tail():
    """Long fine-cadence ranges route history to vision, tail stays on REST."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 2, tzinfo=UTC)  # ~33 days of 1m ≈ 47k bars ≫ 5 pages

    vision_df = _kline_frame([(_milliseconds(start), 10.0),
                              (_milliseconds(start + timedelta(days=1)), 11.0)])
    vision = MockVision([], kline_frames=[vision_df])

    rest_bars = [
        _bar(_milliseconds(end - timedelta(days=2)), 20.0),
        _bar(_milliseconds(end - timedelta(days=1)), 21.0),
        _bar(_milliseconds(end), 22.0),
    ]
    futures = MockClient(klines=rest_bars)

    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=futures,
        vision=vision,
    )

    data = await adapter.fetch_klines(
        "BTCUSDT", KLineInterval.M1, MarketType.PERPETUAL,
        start=start, end=end,
    )

    # One vision bulk call over [start, end - 2 days] for the perpetual market.
    assert len(vision.kline_calls) == 1
    sym, interval, market_path, v_start, v_end = vision.kline_calls[0]
    assert sym == "BTCUSDT" and interval == "1m" and market_path == "futures/um"
    assert v_start == pd.Timestamp(start)
    assert v_end == pd.Timestamp(end - timedelta(days=2))

    # The REST tail was requested from vision_end onward.
    assert futures.klines_calls[-1][2]["startTime"] == _milliseconds(end - timedelta(days=2))

    # Merged result: vision bulk bars + REST tail bars, sorted, deduped.
    closes = data["close"].tolist()
    assert closes[:2] == [10.0, 11.0]
    assert closes[-3:] == [20.0, 21.0, 22.0]
    assert data["timestamp"].is_monotonic_increasing


@pytest.mark.asyncio
async def test_fill_vision_klines_gaps_via_rest():
    """Vision bulk holes (missing intraday bars) are filled from REST."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    day1 = _milliseconds(start)
    day2 = _milliseconds(start + timedelta(days=1))
    h = 3600 * 1000  # 1h in ms
    vision_df = _kline_frame([
        (day1, 10.0), (day1 + 4 * h, 11.0), (day1 + 8 * h, 12.0),
        (day1 + 12 * h, 13.0), (day2, 14.0),   # 16:00/20:00 missing
    ])
    # REST serves the missing bars when asked for the hole.
    futures = MockClient(klines=[
        _bar(day1 + 16 * h, 15.0), _bar(day1 + 20 * h, 16.0),
    ])
    adapter = BinanceAdapter(
        spot_client=MockClient(),
        futures_client=futures,
        vision=MockVision([]),
    )

    filled = await adapter._fill_vision_klines_gaps(
        "BTCUSDT", KLineInterval.H4, MarketType.PERPETUAL, vision_df, "4h", 500
    )

    assert len(filled) == 7
    assert 15.0 in filled["close"].tolist()
    assert 16.0 in filled["close"].tolist()
    assert filled["timestamp"].is_monotonic_increasing
