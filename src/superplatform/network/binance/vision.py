"""Binance historical data archive client — data.binance.vision.

Binance serves full market-data history as daily zip archives on
``data.binance.vision`` (REST endpoints such as ``/futures/data/*`` only
cover recent windows).  This client downloads and parses those archives
and returns DataFrames in the same shapes the REST providers produce —
it never persists anything; callers (adapter → cache) own storage.

Two archive families are supported:
- metrics archives (``open interest`` history, futures/um only)
- kline archives (per-interval OHLCV history, spot or futures/um)

Both share the same month-first range strategy: whole months come from one
monthly archive, boundary months fall back to per-day downloads.
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests

from superplatform.utils.time_utils import to_utc

BASE_URL = "https://data.binance.vision"

# Transient network failures (TUN/proxy resets) occur at a measurable rate
# under sustained load; archive downloads retry like the REST adapter.
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 1.0
_RETRYABLE_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
)
# Archives are small (a few KB); a 10s cap fails fast when the connection
# hangs instead of blocking a worker thread for the TCP timeout.
_DOWNLOAD_TIMEOUT_SECONDS = 10

# 5-minute metrics columns (metrics archives are always 5m granularity).
_METRICS_COLUMNS = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)

# Kline frame columns — the same 10-column shape the REST adapter emits
# (close_time and the trailing ignore field are dropped).
_KLINE_COLUMNS = (
    "timestamp", "open", "high", "low", "close",
    "volume", "quote_volume", "trades",
    "taker_buy_volume", "taker_buy_quote_volume",
)

# Map Binance openInterestHist periods to pandas resample frequencies.
_PERIOD_TO_FREQ = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1d": "1D",
}

def _metrics_url(symbol: str, date: datetime) -> str:
    """Return the daily metrics archive URL for one UTC date."""
    date_str = date.strftime("%Y-%m-%d")
    return (
        f"{BASE_URL}/data/futures/um/daily/metrics/{symbol}/"
        f"{symbol}-metrics-{date_str}.zip"
    )


def _metrics_monthly_url(symbol: str, month: datetime) -> str:
    """Return the monthly metrics archive URL for one UTC month."""
    month_str = month.strftime("%Y-%m")
    return (
        f"{BASE_URL}/data/futures/um/monthly/metrics/{symbol}/"
        f"{symbol}-metrics-{month_str}.zip"
    )


def _klines_url(
    symbol: str, interval: str, date: datetime, market_path: str = "futures/um"
) -> str:
    """Return the daily kline archive URL for one UTC date."""
    date_str = date.strftime("%Y-%m-%d")
    return (
        f"{BASE_URL}/data/{market_path}/daily/klines/{symbol}/{interval}/"
        f"{symbol}-{interval}-{date_str}.zip"
    )


def _klines_monthly_url(
    symbol: str, interval: str, month: datetime, market_path: str = "futures/um"
) -> str:
    """Return the monthly kline archive URL for one UTC month."""
    month_str = month.strftime("%Y-%m")
    return (
        f"{BASE_URL}/data/{market_path}/monthly/klines/{symbol}/{interval}/"
        f"{symbol}-{interval}-{month_str}.zip"
    )


def _funding_rate_monthly_url(symbol: str, month: datetime) -> str:
    """Return the monthly funding-rate archive URL for one UTC month.

    Funding rate is published only as monthly archives on vision (daily
    fundingRate archives do not exist).
    """
    month_str = month.strftime("%Y-%m")
    return (
        f"{BASE_URL}/data/futures/um/monthly/fundingRate/{symbol}/"
        f"{symbol}-fundingRate-{month_str}.zip"
    )


def _last_day_of_month(month: datetime) -> datetime:
    """Return the last UTC calendar day of ``month`` (day of month ignored)."""
    if month.month == 12:
        return datetime(month.year + 1, 1, 1, tzinfo=UTC) - timedelta(days=1)
    return datetime(month.year, month.month + 1, 1, tzinfo=UTC) - timedelta(days=1)


def parse_metrics_archive(content: bytes) -> pd.DataFrame:
    """Parse a metrics zip archive into a 5m (timestamp, open_interest) frame."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if not names:
            return pd.DataFrame(columns=["timestamp", "open_interest"])
        with archive.open(names[0]) as handle:
            try:
                df = pd.read_csv(handle)
            except pd.errors.EmptyDataError:
                return pd.DataFrame(columns=["timestamp", "open_interest"])
    if df.empty or "sum_open_interest" not in df.columns:
        return pd.DataFrame(columns=["timestamp", "open_interest"])
    result = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(df["create_time"], utc=True),
            "open_interest": pd.to_numeric(
                df["sum_open_interest"], errors="coerce"
            ),
        }
    ).dropna()
    return result


def parse_funding_rate_archive(content: bytes) -> pd.DataFrame:
    """Parse a funding-rate zip archive into a (timestamp, funding_rate) frame.

    Monthly fundingRate archives carry columns
    ``calc_time, funding_interval_hours, last_funding_rate`` (no markPrice).
    """
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if not names:
            return pd.DataFrame(columns=["timestamp", "funding_rate"])
        with archive.open(names[0]) as handle:
            try:
                df = pd.read_csv(handle)
            except pd.errors.EmptyDataError:
                return pd.DataFrame(columns=["timestamp", "funding_rate"])
    if df.empty or "calc_time" not in df.columns:
        return pd.DataFrame(columns=["timestamp", "funding_rate"])
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                pd.to_numeric(df["calc_time"], errors="coerce"),
                unit="ms",
                utc=True,
            ),
            "funding_rate": pd.to_numeric(
                df["last_funding_rate"], errors="coerce"
            ),
        }
    ).dropna()


def parse_kline_archive(content: bytes) -> pd.DataFrame:
    """Parse a kline zip archive into the adapter's 10-column kline frame.

    Binance kline archives hold the same 12 fields as the REST array; the
    ``close_time`` and ``ignore`` columns are dropped to match the adapter.
    Some archives include a header row and some don't — a header is detected
    and skipped when present.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if not names:
            return pd.DataFrame(columns=_KLINE_COLUMNS)
        with archive.open(names[0]) as handle:
            try:
                df = pd.read_csv(handle, header=None)
            except pd.errors.EmptyDataError:
                return pd.DataFrame(columns=_KLINE_COLUMNS)
    if df.shape[1] < 12:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    if len(df) == 0:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    # Drop a header row when present (open_time is not numeric).
    if pd.isna(pd.to_numeric(df.iloc[0, 0], errors="coerce")):
        df = df.iloc[1:].reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    # Keep the 10 adapter columns, dropping close_time (index 6) and ignore
    # (index 11) — the same projection the REST adapter applies.
    result = df.iloc[:, [0, 1, 2, 3, 4, 5, 7, 8, 9, 10]].copy()
    result.columns = list(_KLINE_COLUMNS)
    result["timestamp"] = pd.to_datetime(
        pd.to_numeric(result["timestamp"], errors="coerce"), unit="ms", utc=True
    )
    for col in _KLINE_COLUMNS[1:]:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    return result.dropna(subset=["timestamp"]).reset_index(drop=True)


def resample_to_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """Resample 5-minute observations to a Binance openInterestHist period.

    Binance REST aggregates open interest per period bucket using the last
    observation in each bucket (e.g. period=1d returns the day-end value).
    """
    freq = _PERIOD_TO_FREQ.get(period)
    if freq is None:
        raise ValueError(f"unsupported open-interest period: {period}")
    if df.empty:
        return df
    series = df.set_index("timestamp")["open_interest"]
    resampled = series.resample(freq, label="left", closed="left").last()
    return resampled.dropna().reset_index()


class _ArchiveSpec:
    """Which archive family a fetch targets: metrics or klines at an interval.

    Encapsulates the URL layout and parser so the range/clamp logic is shared
    between the two families — metrics archives (open-interest history) and
    kline archives (long-range OHLCV that would cost hundreds of REST pages).
    """

    __slots__ = ("kind", "interval", "market_path")

    def __init__(
        self,
        kind: str,
        interval: str | None = None,
        market_path: str = "futures/um",
    ) -> None:
        self.kind = kind
        self.interval = interval
        self.market_path = market_path

    def url(self, symbol: str, date: datetime) -> str:
        if self.kind == "klines":
            return _klines_url(symbol, self.interval, date, self.market_path)
        return _metrics_url(symbol, date)

    def monthly_url(self, symbol: str, month: datetime) -> str:
        if self.kind == "klines":
            return _klines_monthly_url(
                symbol, self.interval, month, self.market_path
            )
        if self.kind == "fundingRate":
            return _funding_rate_monthly_url(symbol, month)
        return _metrics_monthly_url(symbol, month)

    def parse(self, content: bytes) -> pd.DataFrame:
        if self.kind == "klines":
            return parse_kline_archive(content)
        if self.kind == "fundingRate":
            return parse_funding_rate_archive(content)
        return parse_metrics_archive(content)


class BinanceVisionClient:
    """Download and parse daily archives from data.binance.vision."""

    # Metrics archives do not exist before this date for any USDT-M symbol.
    _EARLIEST_POSSIBLE = datetime(2019, 9, 1, tzinfo=UTC)

    def __init__(self, proxy: str = "", max_concurrent: int = 8) -> None:
        self._proxy = proxy
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session: requests.Session | None = None
        # Earliest confirmed archive date per (symbol, archive family). Kline
        # intervals and metrics do not necessarily begin on the same day.
        self._earliest_by_symbol: dict[tuple[str, str, str | None, str], datetime] = {}
        # Listing-date hints (seeded from the universe table) — verified with
        # one HEAD instead of a full binary search.
        self._earliest_hints: dict[str, datetime] = {}

    @property
    def session(self) -> requests.Session:
        """Create the HTTP session lazily so tests can inject a mock."""
        if self._session is None:
            self._session = requests.Session()
            if self._proxy:
                self._session.proxies = {"http": self._proxy, "https": self._proxy}
        return self._session

    def prime_earliest(self, symbol: str, date: datetime) -> None:
        """Seed the earliest-archive date from a known listing date.

        The universe sync knows when each symbol listed — the earliest date its
        archives can start.  Seeding lets ``_earliest_archive_date`` skip most
        of its HEAD binary search for symbols the universe already covers.
        """
        self._earliest_hints[symbol] = max(to_utc(date), self._EARLIEST_POSSIBLE)

    async def fetch_funding_rate_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch funding-rate history over [start, end) from monthly archives.

        Binance vision publishes funding rate only as monthly archives (daily
        fundingRate archives do not exist), so the range is walked month by
        month and trimmed to [start, end).  Returns columns:
        timestamp, funding_rate.
        """
        spec = _ArchiveSpec("fundingRate")
        start_ts = to_utc(start)
        end_ts = to_utc(end)
        if start_ts >= end_ts:
            return pd.DataFrame(columns=["timestamp", "funding_rate"])

        frames: list[pd.DataFrame] = []
        month = start_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while month < end_ts:
            frame = await self._fetch_month(symbol, month, spec)
            if not frame.empty:
                frames.append(frame)
            if month.month == 12:
                month = datetime(month.year + 1, 1, 1, tzinfo=UTC)
            else:
                month = datetime(month.year, month.month + 1, 1, tzinfo=UTC)

        if not frames:
            return pd.DataFrame(columns=["timestamp", "funding_rate"])
        merged = pd.concat(frames, ignore_index=True)
        merged["timestamp"] = pd.DatetimeIndex(
            pd.to_datetime(merged["timestamp"], utc=True)
        ).as_unit("ns")
        merged = merged[
            (merged["timestamp"] >= start_ts) & (merged["timestamp"] < end_ts)
        ]
        merged = (
            merged.drop_duplicates("timestamp", keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        return merged

    async def fetch_metrics_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        period: str = "1d",
    ) -> pd.DataFrame:
        """Fetch open-interest history over [start, end) from daily archives.

        The request window is clamped to the symbol's confirmed archive
        range: dates before the earliest existing archive are skipped
        without any HTTP request.  Returns columns: timestamp, open_interest.
        """
        spec = _ArchiveSpec("metrics")
        start_ts = to_utc(start)
        end_ts = to_utc(end)
        if start_ts >= end_ts:
            return pd.DataFrame(columns=["timestamp", "open_interest"])

        earliest = await self._earliest_archive_date(
            symbol,
            spec,
            latest_available=end_ts - timedelta(days=1),
        )
        if earliest is None or end_ts <= earliest:
            return pd.DataFrame(columns=["timestamp", "open_interest"])
        first_date = max(start_ts, earliest).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        last_date = end_ts.replace(hour=0, minute=0, second=0, microsecond=0)

        frames = await self._fetch_range(symbol, first_date, last_date, spec)
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return pd.DataFrame(columns=["timestamp", "open_interest"])
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates("timestamp", keep="last")
        merged = merged.sort_values("timestamp").reset_index(drop=True)
        return resample_to_period(merged, period)

    async def fetch_klines_range(
        self,
        symbol: str,
        interval: str,
        market_path: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch kline history over [start, end) from vision archives.

        Same month-first strategy as ``fetch_metrics_range``: whole months from
        one monthly archive, boundary months per-day.  Returns the adapter's
        10-column kline frame at the bar's native interval (no resampling).
        """
        spec = _ArchiveSpec("klines", interval, market_path)
        start_ts = to_utc(start)
        end_ts = to_utc(end)
        if start_ts >= end_ts:
            return pd.DataFrame(columns=_KLINE_COLUMNS)

        earliest = await self._earliest_archive_date(
            symbol,
            spec,
            latest_available=end_ts - timedelta(days=1),
        )
        if earliest is None or end_ts <= earliest:
            return pd.DataFrame(columns=_KLINE_COLUMNS)
        first_date = max(start_ts, earliest).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        last_date = end_ts.replace(hour=0, minute=0, second=0, microsecond=0)

        frames = await self._fetch_range(symbol, first_date, last_date, spec)
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return pd.DataFrame(columns=_KLINE_COLUMNS)
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates("timestamp", keep="last")
        return merged.sort_values("timestamp").reset_index(drop=True)

    async def _fetch_range(
        self,
        symbol: str,
        first_date: datetime,
        last_date: datetime,
        spec: _ArchiveSpec,
    ) -> list[pd.DataFrame]:
        """Download archives covering [first_date, last_date], month-first.

        Whole months come from a single monthly archive (one HTTP request
        instead of ~30); boundary months — the symbol's listing month and the
        current month — fall back to per-day downloads.  A multi-year request
        is thus ~dozens of requests rather than one per day (~1000+).
        """
        frames: list[pd.DataFrame] = []
        month = first_date.replace(day=1)
        while month <= last_date:
            month_end = _last_day_of_month(month)
            if month >= first_date and month_end <= last_date:
                frame = await self._fetch_month(symbol, month, spec)
                if frame.empty:
                    # Monthly archive missing (e.g. listing mid-month) — fall
                    # back to per-day downloads for this month.
                    frames.extend(
                        await self._fetch_days(
                            symbol, max(month, first_date), month_end, spec
                        )
                    )
                else:
                    frames.append(frame)
            else:
                frames.extend(
                    await self._fetch_days(
                        symbol,
                        max(month, first_date),
                        min(month_end, last_date),
                        spec,
                    )
                )
            month = month_end + timedelta(days=1)
        return frames

    async def _fetch_days(
        self,
        symbol: str,
        day_start: datetime,
        day_end: datetime,
        spec: _ArchiveSpec,
    ) -> list[pd.DataFrame]:
        """Download each day's archive in parallel (semaphore-bounded)."""
        dates = [
            day_start + timedelta(days=n)
            for n in range((day_end - day_start).days + 1)
        ]
        results = await asyncio.gather(
            *(self._fetch_one(symbol, date, spec) for date in dates)
        )
        return [frame for frame in results if not frame.empty]

    async def _earliest_archive_date(
        self,
        symbol: str,
        spec: _ArchiveSpec,
        *,
        latest_available: datetime | None = None,
    ) -> datetime | None:
        """Find the first UTC date with an existing archive for ``spec``.

        Binary search over dates in O(log N) HEAD requests, cached per archive
        family for the client's lifetime. A listing-date hint (see
        ``prime_earliest``) is verified with one HEAD instead. Returns None
        when no archive exists.
        """
        cache_key = (symbol, spec.kind, spec.interval, spec.market_path)
        if cache_key in self._earliest_by_symbol:
            return self._earliest_by_symbol[cache_key]

        # A known listing date is the earliest archives can start — confirm it
        # with a single HEAD rather than the full binary search.
        hint = self._earliest_hints.get(symbol)
        if hint is not None and await self._archive_exists(symbol, hint, spec):
            self._earliest_by_symbol[cache_key] = hint
            return hint

        # Daily archives lag by one day (today's file is published T+1). For
        # historical requests, probing today can be outside the requested
        # range and falsely conclude that no archive exists. Probe the latest
        # archive day the request can actually consume instead.
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        low = self._EARLIEST_POSSIBLE
        high = today - timedelta(days=1)
        if latest_available is not None:
            high = min(high, to_utc(latest_available)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        if high < low:
            return None
        found: datetime | None = None

        # Verify the high end exists at all; if not, nothing exists.
        if not await self._archive_exists(symbol, high, spec):
            found = None
        else:
            while low < high:
                mid = low + (high - low) // 2
                if await self._archive_exists(symbol, mid, spec):
                    high = mid
                else:
                    low = mid + timedelta(days=1)
            found = low

        self._earliest_by_symbol[cache_key] = found
        return found

    async def _archive_exists(
        self, symbol: str, date: datetime, spec: _ArchiveSpec
    ) -> bool:
        """Return whether an archive exists for one UTC date."""
        async with self._semaphore:
            return await asyncio.to_thread(self._head_archive, symbol, date, spec)

    def _head_archive(
        self, symbol: str, date: datetime, spec: _ArchiveSpec
    ) -> bool:
        """Synchronously probe one archive URL (runs in a worker thread)."""
        url = spec.url(symbol, date)
        for attempt in range(_MAX_RETRIES):
            try:
                response = self.session.head(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
                return response.status_code == 200
            except _RETRYABLE_ERRORS:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        return False  # pragma: no cover

    async def _fetch_one(
        self, symbol: str, date: datetime, spec: _ArchiveSpec
    ) -> pd.DataFrame:
        """Download and parse one daily archive; 404 yields an empty frame."""
        async with self._semaphore:
            content = await asyncio.to_thread(
                self._download, symbol, date, spec
            )
        if content is None:
            return pd.DataFrame(columns=_KLINE_COLUMNS
                                if spec.kind == "klines"
                                else ["timestamp", "open_interest"])
        return spec.parse(content)

    async def _fetch_month(
        self, symbol: str, month: datetime, spec: _ArchiveSpec
    ) -> pd.DataFrame:
        """Download and parse one monthly archive; 404 yields an empty frame."""
        async with self._semaphore:
            content = await asyncio.to_thread(
                self._download_month, symbol, month, spec
            )
        if content is None:
            return pd.DataFrame(columns=_KLINE_COLUMNS
                                if spec.kind == "klines"
                                else ["timestamp", "open_interest"])
        return spec.parse(content)

    def _download(
        self, symbol: str, date: datetime, spec: _ArchiveSpec
    ) -> bytes | None:
        """Synchronously download one daily archive (worker thread)."""
        return self._download_url(spec.url(symbol, date))

    def _download_month(
        self, symbol: str, month: datetime, spec: _ArchiveSpec
    ) -> bytes | None:
        """Synchronously download one monthly archive (worker thread)."""
        return self._download_url(spec.monthly_url(symbol, month))

    def _download_url(self, url: str) -> bytes | None:
        """Synchronously download one archive URL (runs in a worker thread)."""
        for attempt in range(_MAX_RETRIES):
            try:
                response = self.session.get(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.content
            except _RETRYABLE_ERRORS:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        return None  # pragma: no cover
