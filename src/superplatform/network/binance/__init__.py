"""Binance exchange adapter — REST.

Implements ExchangeAdapter for Binance spot and USDⓈ-M perpetual futures
using the official Binance SDKs (binance-connector / binance-futures-connector).
Public market-data endpoints only — no trading/private endpoints are called.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import requests
from binance.error import ClientError
from binance.spot import Spot
from binance.um_futures import UMFutures

from superplatform.network.base import ExchangeAdapter, KLineInterval, MarketType
from superplatform.network.binance.vision import BinanceVisionClient
from superplatform.network.rate_limiter import RateLimiter
from superplatform.utils.time_utils import to_utc

LOGGER = logging.getLogger(__name__)

# ── K-line interval mapping ──────────────────────────────────────────
_INTERVAL_TO_TIMEFRAME: dict[KLineInterval, str] = {
    KLineInterval.M1: "1m",
    KLineInterval.M3: "3m",
    KLineInterval.M5: "5m",
    KLineInterval.M15: "15m",
    KLineInterval.M30: "30m",
    KLineInterval.H1: "1h",
    KLineInterval.H2: "2h",
    KLineInterval.H4: "4h",
    KLineInterval.H6: "6h",
    KLineInterval.H8: "8h",
    KLineInterval.H12: "12h",
    KLineInterval.D1: "1d",
    KLineInterval.D3: "3d",
    KLineInterval.W1: "1w",
    KLineInterval.MN1: "1M",
}

# Binance per-request limits
_MAX_KLINES_PER_REQUEST = 1500
_MAX_RECORDS_PER_REQUEST = 1000
# When a bounded kline range would need more than this many REST pages, serve
# the historical bulk from monthly vision archives (which bypass the REST rate
# limiter) and keep only the recent tail on REST.
_KLINE_VISION_MIN_PAGES = 5
# Vision kline archives lag by one day; the REST tail must cover that gap plus
# a bar-width of safety. Overlapping boundary bars are deduplicated on merge
# (REST wins, being fresher).
_KLINE_VISION_TAIL_DAYS = 2

# Vision archive path prefix per market type.
_MARKET_TO_VISION_PATH = {
    MarketType.PERPETUAL: "futures/um",
    MarketType.SPOT: "spot",
}
# Every REST call must fail fast when the connection hangs; the SDKs default
# to no timeout, which would block a worker thread for the TCP timeout.
_REQUEST_TIMEOUT_SECONDS = 10

# Transient network failures (TUN/proxy resets, RST) occur at a measurable
# rate under sustained concurrent load; the SDKs do not retry.
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 1.0

# Requests exceptions that warrant a retry: connection aborted/reset by the
# peer, timeouts, TLS handshake failures, and response streams cut mid-body.
_RETRYABLE_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
)

# Binance returns 418/429 (weight limit exceeded) when a burst trips the
# per-minute budget, and the AWS WAF in front of some endpoints (e.g.
# fundingRate) returns 403 under concurrent load. None of these are
# permanent failures — retry after the server-suggested delay instead of
# dropping the whole series from a clean-environment fetch.
_RATE_LIMIT_STATUSES = (403, 418, 429)
_RATE_LIMIT_RETRIES = 6
_RATE_LIMIT_BACKOFF_SECONDS = 5.0  # fallback when no Retry-After header
# A 418 ban's Retry-After can be ~30-50 min; respecting it would hang a
# clean-environment fetch. Cap the per-attempt wait so the reproduction
# retries briefly, then fails the series for a later re-run.
_RATE_LIMIT_MAX_WAIT_SECONDS = 60.0


def _retry_after_seconds(error: ClientError) -> float:
    """Server-suggested wait (seconds) from a rate-limit error's headers."""
    headers = getattr(error, "header", None) or {}
    try:
        wait = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        wait = _RATE_LIMIT_BACKOFF_SECONDS
    return min(wait, _RATE_LIMIT_MAX_WAIT_SECONDS)


def _make_clients(proxy: str = "") -> tuple[Spot, UMFutures]:
    """Create spot and USDⓈ-M perpetual futures SDK clients."""
    config: dict = {"timeout": _REQUEST_TIMEOUT_SECONDS}
    if proxy:
        config["proxies"] = {"http": proxy, "https": proxy}
    spot = Spot(**config)
    futures = UMFutures(**config)
    return spot, futures


def _to_ms(value: datetime | None) -> int | None:
    """Convert a datetime to Unix milliseconds, or None."""
    if value is None:
        return None
    return int(to_utc(value).timestamp() * 1000)


def _bar_width_days(timeframe: str) -> float:
    """Approximate one bar's width in days for a Binance interval string."""
    num = int(timeframe[:-1]) if timeframe[:-1] else 1
    unit = timeframe[-1]
    if unit == "m":
        return num / 1440.0
    if unit == "h":
        return num / 24.0
    if unit == "w":
        return num * 7.0
    if unit == "M":
        return num * 30.0
    return float(num)  # days


class BinanceAdapter(ExchangeAdapter):
    """Binance exchange adapter (spot + USDⓈ-M perpetual).

    Built on the official Binance SDKs.  Public market-data endpoints only.
    """

    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        *,
        proxy: str = "",
        spot_client: Spot | None = None,
        futures_client: UMFutures | None = None,
        vision: BinanceVisionClient | None = None,
        vision_max_concurrent: int | None = None,
    ):
        super().__init__("binance", rate_limiter)
        if spot_client and futures_client:
            self._spot = spot_client
            self._futures = futures_client
        else:
            self._spot, self._futures = _make_clients(proxy)
        if vision is not None:
            self._vision = vision
        else:
            # Every symbol's archive history (OI metrics, long-range klines)
            # shares this one client, so its semaphore — not the fetch
            # coordinator — is the real parallelism cap for multi-symbol
            # cold fetches. Wire it to ``data.max_concurrent_requests``.
            self._vision = BinanceVisionClient(
                proxy, max_concurrent=vision_max_concurrent or 8
            )

    # ── helpers ─────────────────────────────────────────────────────

    def _client(self, market_type: MarketType):
        """Return the SDK client for the given market type."""
        if market_type == MarketType.PERPETUAL:
            return self._futures
        if market_type == MarketType.COIN_FUTURES:
            raise ValueError("COIN_FUTURES market type is not yet supported for Binance")
        return self._spot

    async def _rate_limit(self) -> None:
        """Acquire rate limiter token before each API call."""
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire()

    async def _request(self, fn, *args, **kwargs):
        """Run one sync SDK call with retries on transient network errors.

        Sustained concurrent load through a TUN/proxy connection shows a
        small but real rate of peer-initiated resets; retrying those is
        required for long-running multi-factor fetches to complete.
        """
        # Separate attempt budgets per failure class: network errors get
        # _MAX_RETRIES with short backoff; rate-limit blocks get
        # _RATE_LIMIT_RETRIES with the server-suggested wait.
        net_attempts = 0
        rate_attempts = 0
        while True:
            await self._rate_limit()
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except ClientError as error:
                # Binance weight limit (418/429) or the AWS WAF block (403)
                # under concurrent load: wait out the retry-after and retry.
                if error.status_code not in _RATE_LIMIT_STATUSES:
                    raise
                rate_attempts += 1
                if rate_attempts >= _RATE_LIMIT_RETRIES:
                    raise
                wait = _retry_after_seconds(error)
                LOGGER.warning(
                    "rate-limited (%s), waiting %.0fs before retry "
                    "(attempt %d/%d)",
                    error.status_code, wait, rate_attempts, _RATE_LIMIT_RETRIES,
                )
                await asyncio.sleep(wait)
            except _RETRYABLE_ERRORS as error:
                net_attempts += 1
                if net_attempts >= _MAX_RETRIES:
                    raise
                LOGGER.warning(
                    "transient network error (attempt %d/%d): %s",
                    net_attempts,
                    _MAX_RETRIES,
                    error,
                )
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * net_attempts)
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _to_utc_timestamp(ms: int | float) -> pd.Timestamp:
        """Convert a Unix-milliseconds timestamp to UTC-aware pd.Timestamp."""
        return pd.Timestamp(datetime.fromtimestamp(ms / 1000.0, tz=UTC))

    @staticmethod
    def _klines_to_df(raw: list) -> pd.DataFrame:
        """Convert a Binance klines response to a KLineSchema DataFrame.

        Binance returns 12 fields per bar:
            [openTime, open, high, low, close, volume, closeTime,
             quoteVolume, numberOfTrades, takerBuyBaseVolume,
             takerBuyQuoteVolume, ignore]
        """
        columns = [
            "timestamp", "open", "high", "low", "close",
            "volume", "quote_volume", "trades",
            "taker_buy_volume", "taker_buy_quote_volume",
        ]
        if not raw:
            return pd.DataFrame(columns=columns)
        rows = [
            {
                "timestamp": pd.Timestamp(
                    datetime.fromtimestamp(bar[0] / 1000.0, tz=UTC)
                ),
                "open": float(bar[1]),
                "high": float(bar[2]),
                "low": float(bar[3]),
                "close": float(bar[4]),
                "volume": float(bar[5]),
                "quote_volume": float(bar[7]),
                "trades": float(bar[8]),
                "taker_buy_volume": float(bar[9]),
                "taker_buy_quote_volume": float(bar[10]),
            }
            for bar in raw
        ]
        df = pd.DataFrame(rows, columns=columns)
        df["timestamp"] = pd.DatetimeIndex(
            pd.to_datetime(df["timestamp"], utc=True)
        ).as_unit("ns")
        return df

    # ── fetch_* implementations ─────────────────────────────────────

    async def fetch_klines(
        self,
        symbol: str,
        interval: KLineInterval,
        market_type: MarketType,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch OHLCV kline data, paginating across the requested range.

        Long bounded ranges (more than ``_KLINE_VISION_MIN_PAGES`` REST pages)
        are served from monthly vision archives for the historical bulk, with
        only the recent tail fetched via REST — the same dual-source pattern as
        open interest.  Short and unbounded ranges keep the pure-REST path.
        """
        timeframe = _INTERVAL_TO_TIMEFRAME.get(interval, "1h")
        batch_limit = min(limit, _MAX_KLINES_PER_REQUEST)

        if start is not None and end is not None:
            bar_width = _bar_width_days(timeframe)
            n_days = (to_utc(end) - to_utc(start)).total_seconds() / 86400.0
            est_pages = n_days / (bar_width * batch_limit) if bar_width > 0 else 1.0
            if est_pages > _KLINE_VISION_MIN_PAGES:
                return await self._fetch_klines_hybrid(
                    symbol, interval, market_type, start, end, timeframe, batch_limit
                )

        return await self._fetch_klines_rest(
            symbol, interval, market_type, start, end, batch_limit
        )

    async def _fetch_klines_rest(
        self,
        symbol: str,
        interval: KLineInterval,
        market_type: MarketType,
        start: datetime | None,
        end: datetime | None,
        batch_limit: int,
    ) -> pd.DataFrame:
        """Page through REST klines (the original pure-REST path)."""
        client = self._client(market_type)
        timeframe = _INTERVAL_TO_TIMEFRAME.get(interval, "1h")
        start_ms = _to_ms(start)
        end_ms = _to_ms(end)

        all_bars: list = []
        since_ms = start_ms
        while True:
            raw = await self._request(
                client.klines,
                symbol,
                timeframe,
                startTime=since_ms,
                endTime=end_ms,
                limit=batch_limit,
            )
            if not raw:
                break
            all_bars.extend(raw)
            if len(raw) < batch_limit:
                break
            since_ms = raw[-1][0] + 1
            if end_ms is not None and since_ms >= end_ms:
                break

        return self._klines_to_df(all_bars)

    async def _fetch_klines_hybrid(
        self,
        symbol: str,
        interval: KLineInterval,
        market_type: MarketType,
        start: datetime,
        end: datetime,
        timeframe: str,
        batch_limit: int,
    ) -> pd.DataFrame:
        """Vision monthly archives for the bulk, REST for the recent tail."""
        market_path = _MARKET_TO_VISION_PATH.get(market_type, "futures/um")
        start_ts = to_utc(start)
        end_ts = to_utc(end)
        vision_end = end_ts - timedelta(days=_KLINE_VISION_TAIL_DAYS)

        frames: list[pd.DataFrame] = []
        if start_ts < vision_end:
            hist = await self._vision.fetch_klines_range(
                symbol, timeframe, market_path, start_ts, vision_end
            )
            if not hist.empty:
                hist = await self._fill_vision_klines_gaps(
                    symbol, interval, market_type, hist, timeframe, batch_limit
                )
                frames.append(hist)
        rest = await self._fetch_klines_rest(
            symbol, interval, market_type, vision_end, end, batch_limit
        )
        if not rest.empty:
            frames.append(rest)
        if not frames:
            return pd.DataFrame(columns=[
                "timestamp", "open", "high", "low", "close",
                "volume", "quote_volume", "trades",
                "taker_buy_volume", "taker_buy_quote_volume",
            ])
        merged = pd.concat(frames, ignore_index=True)
        merged["timestamp"] = pd.DatetimeIndex(
            pd.to_datetime(merged["timestamp"], utc=True)
        ).as_unit("ns")
        return self._filter_and_deduplicate_history(merged, start, end)

    async def _fill_vision_klines_gaps(
        self,
        symbol: str,
        interval: KLineInterval,
        market_type: MarketType,
        hist: pd.DataFrame,
        timeframe: str,
        batch_limit: int,
    ) -> pd.DataFrame:
        """Fill internal gaps in the vision kline bulk via REST.

        Vision 4h/8h archives are sometimes missing bars that the REST
        endpoint still serves — e.g. Binance maintenance windows where the
        daily bar exists but intraday bars are absent from the archive. The
        hybrid fetch only REST-covers the recent tail, so these holes would
        otherwise show up as spurious gaps. Detect them against the expected
        bar width and fill each from REST; genuine source gaps (REST returns
        nothing) are left untouched.
        """
        bar = timedelta(days=_bar_width_days(timeframe))
        if len(hist) < 2:
            return hist
        ts = pd.to_datetime(hist["timestamp"]).sort_values().reset_index(drop=True)
        diffs = ts.diff()
        holes = [
            (ts.iloc[i - 1], ts.iloc[i])
            for i in diffs.index
            if diffs.iloc[i] > bar * 1.5
        ]
        if not holes:
            return hist
        frames: list[pd.DataFrame] = [hist]
        for prev, nxt in holes:
            filled = await self._fetch_klines_rest(
                symbol, interval, market_type, prev + bar, nxt, batch_limit
            )
            if not filled.empty:
                frames.append(filled)
        if len(frames) == 1:
            return hist
        merged = pd.concat(frames, ignore_index=True)
        merged["timestamp"] = pd.DatetimeIndex(
            pd.to_datetime(merged["timestamp"], utc=True)
        ).as_unit("ns")
        return (
            merged.drop_duplicates("timestamp", keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    async def fetch_trades(
        self,
        symbol: str,
        market_type: MarketType,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """Fetch historical aggregated trades (TradeSchema columns)."""
        client = self._client(market_type)
        batch_limit = min(limit, _MAX_RECORDS_PER_REQUEST)

        all_rows: list = []
        since_ms = _to_ms(start)
        end_ms = _to_ms(end)
        while True:
            raw = await self._request(
                client.agg_trades,
                symbol,
                startTime=since_ms,
                endTime=end_ms,
                limit=batch_limit,
            )
            if not raw:
                break
            all_rows.extend(raw)
            if len(raw) < batch_limit:
                break
            since_ms = int(max(row["T"] for row in raw)) + 1
            if end_ms is not None and since_ms >= end_ms:
                break

        if not all_rows:
            return pd.DataFrame(columns=[
                "timestamp", "price", "quantity", "is_buyer_maker", "trade_id",
            ])
        rows = [
            {
                "timestamp": self._to_utc_timestamp(row["T"]),
                "price": float(row["p"]),
                "quantity": float(row["q"]),
                "is_buyer_maker": bool(row["m"]),
                "trade_id": int(row["a"]),
            }
            for row in all_rows
        ]
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.DatetimeIndex(
            pd.to_datetime(df["timestamp"], utc=True)
        ).as_unit("ns")
        return df

    async def fetch_order_book(
        self,
        symbol: str,
        market_type: MarketType,
        depth: int = 20,
    ) -> dict:
        """Fetch order book depth snapshot.

        Returns dict with keys: timestamp, bids (DataFrame), asks (DataFrame).
        """
        client = self._client(market_type)

        raw = await self._request(client.depth, symbol, limit=depth)

        now = pd.Timestamp.now(tz="UTC")

        def _side_df(rows: list[list], side: str) -> pd.DataFrame:
            if not rows:
                return pd.DataFrame(columns=["timestamp", "side", "price", "quantity"])
            df = pd.DataFrame(rows, columns=["price", "quantity"])
            df["timestamp"] = now
            df["side"] = side
            df["price"] = df["price"].astype(np.float64)
            df["quantity"] = df["quantity"].astype(np.float64)
            return df[["timestamp", "side", "price", "quantity"]]

        return {
            "timestamp": now,
            "bids": _side_df(raw.get("bids", []), "bid"),
            "asks": _side_df(raw.get("asks", []), "ask"),
        }

    async def fetch_funding_rate(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch historical funding rates (perpetual only) from vision archives.

        Vision funding archives (data.binance.vision) avoid the fundingRate
        REST endpoint, whose AWS WAF returns 403 when it is hit with
        concurrent/volume requests during a multi-symbol fetch.  Monthly
        archives publish T+1, so the very latest rates are not included —
        fine for historical research ranges.  Returns columns:
        timestamp, funding_rate.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        start_ts = (
            to_utc(start)
            if start is not None
            else datetime(2019, 9, 1, tzinfo=UTC)
        )
        end_ts = to_utc(end) if end is not None else datetime.now(UTC)
        return await self._vision.fetch_funding_rate_range(
            symbol, start_ts, end_ts
        )

    async def fetch_open_interest(
        self,
        symbol: str,
        market_type: MarketType,
        period: str = "5m",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch open interest history across the full requested range.

        Combines two sources:
        1. The REST ``/futures/data/openInterestHist`` endpoint, which only
           serves the most recent window (it rejects any ``startTime``).
        2. The ``data.binance.vision`` daily archives, which hold the full
           history back to ~2020.

        The REST leg supplies the recent tail; the vision leg fills any
        portion of the requested range that precedes REST coverage.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")

        # ── REST leg: most recent batch (no since/until — the endpoint
        # rejects any historical bound, both startTime and endTime) ──
        try:
            rest_df = await self._fetch_open_interest_recent(
                symbol, market_type, period, limit=min(limit, 500)
            )
        except Exception:
            # REST unreachable (network) — the vision archive covers the
            # full history, so fall back to it entirely.
            LOGGER.warning(
                "open-interest REST leg failed for %s; falling back to vision archives",
                symbol,
                exc_info=True,
            )
            rest_df = pd.DataFrame(columns=["timestamp", "open_interest"])
        rest_min = rest_df["timestamp"].min() if not rest_df.empty else None

        # ── Vision leg: full history from the daily archives ──────────────
        def _vision(start_ts, end_ts):
            return self._vision.fetch_metrics_range(
                symbol, start=start_ts, end=end_ts, period=period
            )

        if rest_df.empty:
            # REST unavailable (network / rate limit) — fall back to archive.
            if start is None or end is None:
                return pd.DataFrame(columns=["timestamp", "open_interest"])
            merged = await _vision(start, end)
        elif end is not None and rest_min is not None and end < rest_min:
            # Requested range lies entirely before REST coverage — archive only.
            merged = await _vision(start, end)
        else:
            frames: list[pd.DataFrame] = []
            if start is not None and rest_min is not None and start < rest_min:
                frames.append(await _vision(start, rest_min))
            frames.append(rest_df)
            merged = pd.concat(frames, ignore_index=True)
            merged["timestamp"] = pd.DatetimeIndex(
                pd.to_datetime(merged["timestamp"], utc=True)
            ).as_unit("ns")
        return self._filter_and_deduplicate_history(merged, start, end)

    async def _fetch_open_interest_recent(
        self,
        symbol: str,
        market_type: MarketType,
        period: str,
        *,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch the most recent open-interest observations via REST."""
        client = self._client(market_type)

        raw = await self._request(
            client.open_interest_hist,
            symbol,
            period,
            limit=limit,
        )
        if not raw:
            return pd.DataFrame(columns=["timestamp", "open_interest"])
        rows = [
            {
                "timestamp": self._to_utc_timestamp(row["timestamp"]),
                "open_interest": float(row["sumOpenInterest"]),
            }
            for row in raw
        ]
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.DatetimeIndex(
            pd.to_datetime(df["timestamp"], utc=True)
        ).as_unit("ns")
        return df

    async def fetch_basis(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """Fetch spot-perpetual basis (price difference %).

        Fetches every daily spot and perpetual kline page in parallel, then computes:
            basis_pct = (perpetual_close - spot_close) / spot_close * 100

        Returns DataFrame with columns: timestamp, spot_price,
        perpetual_price, basis_pct.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")

        # Reuse kline pagination so both price legs cover the same requested
        # interval. The two independent markets are still fetched in parallel.
        spot_df, perp_df = await asyncio.gather(
            self.fetch_klines(
                symbol=symbol,
                interval=KLineInterval.D1,
                market_type=MarketType.SPOT,
                start=start,
                end=end,
                limit=limit,
            ),
            self.fetch_klines(
                symbol=symbol,
                interval=KLineInterval.D1,
                market_type=MarketType.PERPETUAL,
                start=start,
                end=end,
                limit=limit,
            ),
        )

        if spot_df.empty or perp_df.empty:
            return pd.DataFrame(columns=[
                "timestamp", "spot_price", "perpetual_price", "basis_pct",
            ])

        spot_df = spot_df[["timestamp", "close"]].rename(
            columns={"close": "spot_price"}
        )
        perp_df = perp_df[["timestamp", "close"]].rename(
            columns={"close": "perpetual_price"}
        )

        merged = spot_df.merge(perp_df, on="timestamp", how="inner")
        if merged.empty:
            return pd.DataFrame(columns=[
                "timestamp", "spot_price", "perpetual_price", "basis_pct",
            ])

        merged["basis_pct"] = (
            (merged["perpetual_price"] - merged["spot_price"])
            / merged["spot_price"]
            * 100.0
        )
        return merged

    async def fetch_universe(
        self, market_type: MarketType = MarketType.PERPETUAL
    ) -> pd.DataFrame:
        """Return the full USDⓈ-M perpetual universe from /fapi/v1/exchangeInfo.

        Every ``contractType == PERPETUAL`` and ``quoteAsset == USDT`` row is
        returned with its raw ``status`` (including ``CLOSE``-status symbols) —
        the caller decides delisting, and keeping the status column is what
        lets a re-added symbol be distinguished from a permanently delisted one.

        Returns DataFrame with columns: exchange, symbol, contract_type,
        status, base_asset, quote_asset, listed_at (UTC-aware).
        """
        client = self._client(market_type)  # PERPETUAL → self._futures
        raw = await self._request(client.exchange_info)
        rows = []
        for s in raw.get("symbols", []):
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
                continue
            onboard_ms = s.get("onboardDate")
            rows.append({
                "exchange": "binance",
                "symbol": s["symbol"],
                "contract_type": s.get("contractType"),
                "status": s.get("status"),
                "base_asset": s.get("baseAsset"),
                "quote_asset": s.get("quoteAsset"),
                "listed_at": self._to_utc_timestamp(onboard_ms) if onboard_ms else pd.NaT,
            })
        columns = [
            "exchange", "symbol", "contract_type", "status",
            "base_asset", "quote_asset", "listed_at",
        ]
        if not rows:
            return pd.DataFrame(columns=columns)
        df = pd.DataFrame(rows, columns=columns)
        df["listed_at"] = pd.DatetimeIndex(
            pd.to_datetime(df["listed_at"], utc=True)
        ).as_unit("ns")
        return df

    async def fetch_tickers(
        self, market_type: MarketType = MarketType.PERPETUAL
    ) -> dict[str, float]:
        """Return symbol → 24h quote volume (USDT) from /fapi/v1/ticker/24hr.

        Called without a ``symbol`` argument the endpoint returns the aggregate
        array (weight 40), so callers should cache this result for a minute or
        two rather than hitting it per request.
        """
        client = self._client(market_type)  # PERPETUAL → self._futures
        raw = await self._request(client.ticker_24hr_price_change)
        if not isinstance(raw, list):
            return {}
        return {item["symbol"]: float(item["quoteVolume"]) for item in raw}

    @staticmethod
    def _filter_and_deduplicate_history(
        df: pd.DataFrame,
        start: datetime | None,
        end: datetime | None,
    ) -> pd.DataFrame:
        """Keep one sorted observation per timestamp inside the requested bounds."""
        if start is not None:
            df = df[df["timestamp"] >= pd.Timestamp(to_utc(start))]
        if end is not None:
            df = df[df["timestamp"] <= pd.Timestamp(to_utc(end))]
        return (
            df.sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )

    # ── subscribe_* (stubs) ──────────────────────────────────────────
    # WebSocket streaming is not yet implemented. These stubs allow the
    # class to be instantiated for REST-only usage. They will be replaced
    # with real WebSocket implementations when the live data pipeline is
    # built (part of LiveRuntime / BinanceBroker work).

    async def subscribe_klines(
        self,
        symbol: str,
        interval: KLineInterval,
        market_type: MarketType,
        target: pd.DataFrame,
        stop: asyncio.Event,
    ) -> None:
        raise NotImplementedError("subscribe_klines: WebSocket streaming not yet implemented")

    async def subscribe_trades(
        self,
        symbol: str,
        market_type: MarketType,
        target: pd.DataFrame,
        stop: asyncio.Event,
    ) -> None:
        raise NotImplementedError("subscribe_trades: WebSocket streaming not yet implemented")

    async def subscribe_order_book(
        self,
        symbol: str,
        market_type: MarketType,
        target: dict,
        stop: asyncio.Event,
    ) -> None:
        raise NotImplementedError("subscribe_order_book: WebSocket streaming not yet implemented")
