"""Read-only, reproducible views over the historical research cache.

The cache tables use the same primary key contract:
``symbol + frequency + timestamp``.  This module exposes that contract without
leaking DuckDB SQL into factor code and records a content-addressed manifest
for every set of series used by a generation run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from superplatform.data.store import provider_table


def _utc_timestamp(value: Any, label: str) -> pd.Timestamp | None:
    """Parse a timestamp and reject naive values."""
    if value is None:
        return None
    parsed = pd.Timestamp(value)
    if parsed.tz is None:
        raise ValueError(f"{label} must be timezone-aware UTC")
    parsed = parsed.tz_convert("UTC")
    return parsed


def normalize_snapshot_frame(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    """Return a sorted frame with a strict, timezone-aware UTC timestamp."""
    result = frame.copy()
    if "timestamp" not in result.columns:
        raise ValueError(f"{label} is missing timestamp")
    timestamps = pd.to_datetime(result["timestamp"], errors="raise")
    if timestamps.dt.tz is None:
        raise ValueError(f"{label}.timestamp must be timezone-aware UTC")
    result["timestamp"] = timestamps.dt.tz_convert("UTC")
    if result["timestamp"].isna().any():
        raise ValueError(f"{label}.timestamp contains null values")
    result = (
        result.drop_duplicates("timestamp", keep="last")
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )
    return result


def _frame_digest(frame: pd.DataFrame) -> str:
    """Hash normalized schema and values without relying on index state."""
    if frame.empty:
        payload = {
            "columns": list(frame.columns),
            "dtypes": [str(dtype) for dtype in frame.dtypes],
            "values": "",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    values = pd.util.hash_pandas_object(frame, index=False).to_numpy(dtype=np.uint64)
    schema = json.dumps(
        {
            "columns": list(frame.columns),
            "dtypes": [str(dtype) for dtype in frame.dtypes],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(schema + b"\0" + values.tobytes()).hexdigest()


class DataSnapshot:
    """Read-only snapshot access to all cached research data types."""

    def __init__(self, cache_path: str | Path):
        self.path = Path(cache_path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._connection = duckdb.connect(str(self.path), read_only=True)
        self._connection.execute("SET TimeZone = 'UTC'")
        self._tables = {
            name for name, in self._connection.execute("SHOW TABLES").fetchall()
        }

    def __enter__(self) -> DataSnapshot:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def provider_series(self) -> list[dict[str, Any]]:
        """Read the provider_tables metadata (provider_id, data_type, table)."""
        if "provider_tables" not in self._tables:
            return []
        df = self._connection.execute(
            "SELECT provider_id, data_type, table_name FROM provider_tables "
            "ORDER BY provider_id"
        ).fetchdf()
        return df.to_dict("records")

    def available_data_types(self) -> list[str]:
        """Return data types available in this cache (from provider metadata)."""
        rows = self.provider_series()
        return sorted({r["data_type"] for r in rows})

    def load(
        self,
        provider_id: str,
        symbol: str,
        frequency: str,
        *,
        start: Any = None,
        end: Any = None,
    ) -> pd.DataFrame:
        """Load one canonical input series from a provider's cache table.

        ``provider_id`` selects the per-provider table (``pv_<id>``). The
        table must already exist — i.e. the provider has been written to this
        cache. Returns rows without the cache metadata columns.
        """
        table = provider_table(provider_id)
        if table not in self._tables:
            raise ValueError(
                f"snapshot has no table for provider {provider_id!r}"
            )

        clauses = ["symbol = ?", "frequency = ?"]
        parameters: list[Any] = [symbol, frequency]
        start_ts = _utc_timestamp(start, f"{provider_id}.start")
        end_ts = _utc_timestamp(end, f"{provider_id}.end")
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            parameters.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp < ?")
            parameters.append(end_ts)
        query = (
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp ASC"
        )
        frame = self._connection.execute(query, parameters).fetchdf()
        frame = normalize_snapshot_frame(
            frame.drop(columns=["symbol", "frequency"], errors="ignore"),
            label=f"{provider_id}/{symbol}/{frequency}",
        )
        return frame

    def describe(
        self,
        requests: Iterable[tuple[str, str, str]],
        *,
        start: Any = None,
        end: Any = None,
    ) -> tuple[str, dict[str, Any]]:
        """Return a stable snapshot id and a manifest for requested series.

        ``requests`` are ``(provider_id, symbol, frequency)`` tuples; the
        manifest also records each series' data_type from provider metadata.
        """
        provider_types = {
            r["provider_id"]: r["data_type"] for r in self.provider_series()
        }
        series: list[dict[str, Any]] = []
        for provider_id, symbol, frequency in sorted(set(requests)):
            data_type = provider_types.get(provider_id, "")
            try:
                frame = self.load(
                    provider_id,
                    symbol,
                    frequency,
                    start=start,
                    end=end,
                )
            except (ValueError, duckdb.Error) as error:
                series.append(
                    {
                        "provider": provider_id,
                        "data_type": data_type,
                        "symbol": symbol,
                        "frequency": frequency,
                        "rows": 0,
                        "start": None,
                        "end": None,
                        "content_sha256": None,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                continue
            series.append(
                {
                    "provider": provider_id,
                    "data_type": data_type,
                    "symbol": symbol,
                    "frequency": frequency,
                    "rows": int(len(frame)),
                    "start": (
                        frame["timestamp"].min().isoformat()
                        if not frame.empty
                        else None
                    ),
                    "end": (
                        frame["timestamp"].max().isoformat()
                        if not frame.empty
                        else None
                    ),
                    "content_sha256": _frame_digest(frame),
                }
            )
        payload: dict[str, Any] = {
            "schema_version": "1",
            "timezone": "UTC",
            "source_cache": str(self.path),
            "source_cache_sha256": _sha256_file(self.path),
            "start": _utc_timestamp(start, "snapshot.start").isoformat()
            if start is not None
            else None,
            "end": _utc_timestamp(end, "snapshot.end").isoformat()
            if end is not None
            else None,
            "series": series,
        }
        # The ID is based only on the requested normalized series. Unrelated
        # tables or rows added to the same cache must not change this snapshot.
        id_payload = {
            key: payload[key]
            for key in ("schema_version", "timezone", "start", "end", "series")
        }
        serialized = json.dumps(
            id_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        payload["snapshot_id"] = snapshot_id
        return snapshot_id, payload


def _sha256_file(path: Path) -> str:
    """Hash a cache file in bounded-size chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
