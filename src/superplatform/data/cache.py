"""DuckDB-backed data cache for incremental provider fetching.

Follows the same decoupled pattern as live trading: DataCache wraps Store,
CachingProvider wraps any DataProvider, and neither Store nor caching logic
leaks into the Runtime or provider implementations.

Usage:
    store = Store("data/cache.duckdb")
    cache = DataCache(store)
    wrapped = CachingProvider(binance_kline_provider, cache)
    df = await wrapped.fetch("BTCUSDT", DataFrequency.D1, start, end)
    # Second call with overlapping range only fetches the delta.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from superplatform.data.provider_registry import DataProvider
from superplatform.data.schema import DataFrequency
from superplatform.data.store import Store, provider_table

# Data types that are cached. Everything else (e.g. trade / order_book)
# passes through without persistence.
_CACHEABLE_DATA_TYPES = {"kline", "funding_rate", "open_interest", "basis"}


def _freq_str(frequency: DataFrequency) -> str:
    """Normalise frequency to its string value."""
    return str(frequency.value)


def _to_timestamp(dt: datetime | pd.Timestamp | None) -> pd.Timestamp | None:
    """Coerce a datetime-like value to a UTC-aware pd.Timestamp."""
    if dt is None:
        return None
    if isinstance(dt, pd.Timestamp):
        if dt.tz is None:
            return dt.tz_localize("UTC")
        return dt.tz_convert("UTC") if str(dt.tz) != "UTC" else dt
    ts = pd.Timestamp(dt)
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC") if str(ts.tz) != "UTC" else ts


def _has_gap(
    cache_max: pd.Timestamp,
    end_ts: pd.Timestamp,
    cached: dict,
) -> bool:
    """Return True if end_ts extends meaningfully beyond the cached range.

    Uses the median bar interval from the cached segment to determine
    whether end_ts reaches far enough past cache_max to justify a fetch.
    A single-bar overlap at the boundary is not a gap.
    """
    bar_width = cached.get("bar_width")
    if bar_width is None:
        return True  # can't determine — fetch to be safe
    return (end_ts - cache_max) >= bar_width * 1.5


class DataCache:
    """Transparent read-through cache backed by a DuckDB Store.

    Caches every time-series data type (kline, funding_rate, open_interest,
    basis) keyed by its data_type table in the Store.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    async def get_or_fetch(
        self,
        provider: DataProvider,
        symbol: str,
        frequency: DataFrequency | str,
        start: datetime | pd.Timestamp | None,
        end: datetime | pd.Timestamp | None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Return data for the requested range, fetching only missing segments."""
        # Normalise string frequencies (e.g. "1d") to the DataFrequency
        # enum that provider.fetch() expects.
        if isinstance(frequency, str):
            frequency = DataFrequency(frequency)
        freq = _freq_str(frequency)
        if provider.data_type not in _CACHEABLE_DATA_TYPES:
            # Unknown data type — pass through without caching.
            return await provider.fetch(
                symbol, frequency, start=start, end=end, **kwargs
            )
        # One table per provider: pv_<provider_id>. Ensured idempotently so
        # reads/writes and the provider_tables metadata always line up.
        table = provider_table(provider.provider_id)
        self._store.ensure_provider_table(provider.provider_id, provider.data_type)

        start_ts = _to_timestamp(start)
        end_ts = _to_timestamp(end)

        # ── Unbounded start: fetch full and cache ─────────────────
        if start_ts is None:
            df = await provider.fetch(
                symbol, frequency, start=start, end=end, **kwargs
            )
            if not df.empty:
                self._record_internal_gaps(table, df, symbol, freq)
                self._cache(table, df, symbol, freq)
            return df

        cached = self._store.series_range(table, symbol, freq)

        # ── No cached data → full fetch ───────────────────────────
        if cached["count"] == 0:
            df = await provider.fetch(
                symbol, frequency, start=start, end=end, **kwargs
            )
            if not df.empty:
                self._record_internal_gaps(table, df, symbol, freq)
                self._cache(table, df, symbol, freq)
            return df

        cache_min: pd.Timestamp = cached["min_ts"]
        cache_max: pd.Timestamp = cached["max_ts"]

        parts: list[pd.DataFrame] = []

        # ── Segment before cache ──────────────────────────────────
        if start_ts < cache_min:
            before_end = min(end_ts, cache_min) if end_ts else cache_min
            if not self._store.covers_empty_range(
                table, symbol, freq, start_ts, before_end
            ):
                df_before = await provider.fetch(
                    symbol, frequency, start=start_ts, end=before_end, **kwargs
                )
                if not df_before.empty:
                    self._record_internal_gaps(table, df_before, symbol, freq)
                    self._cache(table, df_before, symbol, freq)
                    parts.append(df_before)
                    # The fetched data starts later than requested: the
                    # uncovered prefix is a verified-empty range.
                    covered_min = pd.Timestamp(df_before["timestamp"].min())
                    if covered_min > start_ts:
                        self._store.record_empty_range(
                            table, symbol, freq, start_ts, covered_min
                        )
                else:
                    # The source verified this range is empty; remember it so
                    # future runs skip re-fetching (e.g. archives that never
                    # existed for a symbol).
                    self._store.record_empty_range(
                        table, symbol, freq, start_ts, before_end
                    )

        # ── Cached segment ────────────────────────────────────────
        df_cached = self._store.query_series(
            table, symbol, freq, start=start_ts, end=end_ts
        )
        if not df_cached.empty:
            parts.append(df_cached)

        # ── Segment after cache ───────────────────────────────────
        # Only fetch if the gap between cache_max and end_ts is at least
        # one bar-width.  When end_ts falls inside (or immediately after)
        # the last cached bar, the cached segment already covers everything.
        if end_ts is None:
            after_start = cache_max
        elif _has_gap(cache_max, end_ts, cached):
            after_start = max(start_ts, cache_max)
        else:
            after_start = None

        if after_start is not None:
            if end_ts is not None and self._store.covers_empty_range(
                table, symbol, freq, after_start, end_ts
            ):
                pass  # previously verified empty — skip
            else:
                df_after = await provider.fetch(
                    symbol, frequency, start=after_start, end=end_ts, **kwargs
                )
                if not df_after.empty:
                    self._record_internal_gaps(table, df_after, symbol, freq)
                    self._cache(table, df_after, symbol, freq)
                    parts.append(df_after)
                    # The fetched data ends before the requested end: the
                    # uncovered suffix is a verified-empty range.
                    if end_ts is not None:
                        covered_max = pd.Timestamp(df_after["timestamp"].max())
                        if covered_max < end_ts:
                            self._store.record_empty_range(
                                table, symbol, freq, covered_max, end_ts
                            )
                elif end_ts is not None:
                    self._store.record_empty_range(
                        table, symbol, freq, after_start, end_ts
                    )

        if not parts:
            return pd.DataFrame()

        merged = pd.concat(parts, ignore_index=True)
        # Drop duplicates in case cached and newly-fetched ranges overlap
        # at boundaries (cache_max → after_start).
        if "timestamp" in merged.columns:
            merged = merged.drop_duplicates("timestamp", keep="last")
            merged = merged.sort_values("timestamp").reset_index(drop=True)
        # Strip Store metadata columns so the DataFrame matches the
        # upstream Schema (KLineSchema) — Runtime validates against it.
        for col in ("symbol", "frequency"):
            if col in merged.columns:
                merged = merged.drop(columns=col)
        return merged

    def cache_segment(
        self,
        table: str,
        df: pd.DataFrame,
        symbol: str,
        frequency: str,
    ) -> None:
        """Persist a freshly fetched segment and bookmark its internal holes.

        Same write path as ``get_or_fetch`` uses for newly fetched ranges
        (internal-gap bookmarks + upsert), exposed for backfill's dense
        chunk-coverage path, which bypasses the min/max-based incremental
        logic: min/max span cannot see holes inside the cached range, so a
        chunk lying in an un-fetched hole would wrongly look "cached".
        """
        if df.empty:
            return
        self._record_internal_gaps(table, df, symbol, frequency)
        self._cache(table, df, symbol, frequency)

    def _record_internal_gaps(
        self,
        table: str,
        df: pd.DataFrame,
        symbol: str,
        frequency: str,
    ) -> int:
        """Record internal missing-bar gaps in a fetched segment as verified-empty.

        A segment freshly fetched from the source (e.g. Binance vision
        archives) can have holes in the middle: the source genuinely has no
        bars for those timestamps (missing daily archives, exchange halt).
        The prefix/suffix empty-range logic in ``get_or_fetch`` only covers
        the edges, so these internal holes were never bookmarked — a later
        run would keep re-probing the source for them and the
        validate-report incremental-update section would not reflect them.

        Only runs on data actually fetched this call, and it records what
        the source returned: the same trust model the existing prefix/suffix
        empty-range recording already uses. Returns how many gaps were
        recorded.
        """
        if "timestamp" not in df.columns or len(df) < 2:
            return 0
        ts = pd.to_datetime(df["timestamp"]).sort_values().reset_index(drop=True)
        diffs = ts.diff().dropna()
        if diffs.empty:
            return 0
        bar = diffs.median()
        if pd.isna(bar) or bar.total_seconds() <= 0:
            return 0
        recorded = 0
        for i in diffs.index:
            if diffs.loc[i] > bar * 1.5:
                self._store.record_empty_range(
                    table, symbol, frequency, ts.iloc[i - 1], ts.iloc[i]
                )
                recorded += 1
        return recorded

    def _cache(self, table: str, df: pd.DataFrame, symbol: str, frequency: str) -> None:
        """Write data to DuckDB with symbol + frequency tags."""
        cached = df.copy()
        cached["symbol"] = symbol
        cached["frequency"] = frequency
        self._store.upsert(table, cached)


class CachingProvider(DataProvider):
    """Transparent caching wrapper around any DataProvider.

    Copies provider_id and data_type from the inner provider so the
    registry and pipeline see the same identity. Any other attribute
    (e.g. market_type) is proxied to the inner provider lazily.
    """

    def __init__(self, inner: DataProvider, cache: DataCache) -> None:
        self._inner = inner
        self._cache = cache
        # Copy identity + cadence contract at construction time — matches how
        # concrete providers declare these in __init__. This avoids overriding
        # the base class's plain str class variables with properties, and
        # (critically) leaks the base-class defaults (``exchange = None``,
        # ``market_type = None``, ``available_frequencies = None``) through
        # normal attribute lookup, which would shadow the inner provider's
        # real values via ``__getattr__``.
        self.provider_id = inner.provider_id
        self.data_type = inner.data_type
        self.exchange = inner.exchange
        self.market_type = inner.market_type
        self.available_frequencies = inner.available_frequencies

    def __getattr__(self, name: str) -> Any:
        """Proxy any other attributes (e.g. market_type) to the inner provider."""
        return getattr(self._inner, name)

    async def fetch(
        self,
        symbol: str,
        frequency: DataFrequency,
        start: datetime | None = None,
        end: datetime | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        return await self._cache.get_or_fetch(
            self._inner, symbol, frequency, start=start, end=end, **kwargs
        )
