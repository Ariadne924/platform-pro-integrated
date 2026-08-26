"""Bronze, Silver and Gold kline processing.

The module has one public interface: :class:`KlineLayerPipeline.load`.

Bronze
    Provider-native cached rows.  This is the current ingestion seam; raw
    archive files remain owned by the collectors.
Silver
    Native-frequency bars normalized to UTC with explicit close state and
    record-level quality flags.
Gold
    Research-ready bars derived from Silver at the requested cadence.

Keeping these transformations outside the HTTP route makes the same contract
available to the dashboard, factor runtime and independent strategies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, cast

import pandas as pd

from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.store import provider_table


class DataLayer(StrEnum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


_FREQUENCY_DELTA = {
    DataFrequency.M1: timedelta(minutes=1),
    DataFrequency.M5: timedelta(minutes=5),
    DataFrequency.M15: timedelta(minutes=15),
    DataFrequency.M30: timedelta(minutes=30),
    DataFrequency.H1: timedelta(hours=1),
    DataFrequency.H4: timedelta(hours=4),
    DataFrequency.D1: timedelta(days=1),
    DataFrequency.W1: timedelta(weeks=1),
}

_GOLD_SOURCE = {
    DataFrequency.M1: (DataFrequency.M1, None, 1),
    DataFrequency.M5: (DataFrequency.M1, "5min", 5),
    DataFrequency.M15: (DataFrequency.M1, "15min", 15),
    DataFrequency.M30: (DataFrequency.M1, "30min", 30),
    DataFrequency.H1: (DataFrequency.M1, "1h", 60),
    DataFrequency.H4: (DataFrequency.M1, "4h", 240),
    DataFrequency.D1: (DataFrequency.D1, None, 1),
    DataFrequency.W1: (DataFrequency.D1, "W-MON", 7),
}

_NATIVE_FREQUENCIES = {DataFrequency.M1, DataFrequency.D1}
_SUM_COLUMNS = (
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
)


@dataclass(frozen=True)
class KlineQuery:
    exchange: str
    market_type: MarketType
    symbol: str
    frequency: DataFrequency
    layer: DataLayer = DataLayer.SILVER
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    limit: int = 300


@dataclass(frozen=True)
class KlinePage:
    meta: dict[str, Any]
    data: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {"meta": self.meta, "data": self.data}


class KlineLayerPipeline:
    """Load a kline page through the requested data layer."""

    def __init__(
        self,
        store: Any,
        resolve_provider: Callable[[str, str, str], str],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._resolve_provider = resolve_provider
        self._now = now or (lambda: datetime.now(timezone.utc))

    def load(self, query: KlineQuery) -> KlinePage:
        provider_id = self._resolve_provider(
            query.exchange.lower(), query.market_type.value, "kline"
        )
        source_frequency, resample_rule, multiplier = self._source_plan(query)
        # 有 start 锚点时按 ASC 向前分页；只有 end（或都没有）时按 DESC
        # 取最近 limit 根（文档契约：省略时读取最近数据）
        order = "ASC" if query.start is not None else "DESC"
        frame = self._store.query_series(
            provider_table(provider_id),
            _core_symbol(query.symbol),
            source_frequency.value,
            start=_aligned_start(query.start, resample_rule),
            end=query.end,
            limit=(query.limit + 1) * multiplier,
            order=order,
        )
        frame = _ordered(frame)

        if query.layer is DataLayer.BRONZE:
            frame, has_more = _bounded_frame(frame, query.limit, order)
            data = _bronze_records(frame)
            return KlinePage(
                meta=self._meta(
                    query,
                    provider_id,
                    source_frequency,
                    len(data),
                    has_more,
                    0,
                    0,
                ),
                data=data,
            )

        if query.layer is DataLayer.GOLD:
            frame = _attach_source_quality_flags(
                frame,
                frequency=source_frequency,
                now=self._now(),
            )
            frame = _aggregate(frame, resample_rule)
            frame = _complete_before_end(frame, query.end, query.frequency)

        frame, has_more = _bounded_frame(frame, query.limit, order)

        records = _silver_records(
            frame,
            frequency=query.frequency,
            now=self._now(),
        )
        flagged = sum(bool(row["quality_flags"]) for row in records)
        incomplete = sum(not row["is_closed"] for row in records)
        return KlinePage(
            meta=self._meta(
                query,
                provider_id,
                source_frequency,
                len(records),
                has_more,
                flagged,
                incomplete,
            ),
            data=records,
        )

    @staticmethod
    def _source_plan(
        query: KlineQuery,
    ) -> tuple[DataFrequency, str | None, int]:
        if query.frequency not in _FREQUENCY_DELTA:
            raise ValueError(f"不支持的 K 线周期: {query.frequency}")
        if query.layer in {DataLayer.BRONZE, DataLayer.SILVER}:
            if query.frequency not in _NATIVE_FREQUENCIES:
                raise ValueError(
                    f"{query.layer.value} 层只提供原生周期 1m/1d；"
                    "派生周期请使用 gold 层"
                )
            return query.frequency, None, 1
        return _GOLD_SOURCE[query.frequency]

    @staticmethod
    def _meta(
        query: KlineQuery,
        provider_id: str,
        source_frequency: DataFrequency,
        count: int,
        has_more: bool,
        flagged: int,
        incomplete: int,
    ) -> dict[str, Any]:
        transformations: list[str] = []
        if query.layer in {DataLayer.SILVER, DataLayer.GOLD}:
            transformations.extend(
                [
                    "utc_normalization",
                    "canonical_fields",
                    "close_state",
                    "quality_flags",
                ]
            )
        if query.layer is DataLayer.GOLD and source_frequency != query.frequency:
            transformations.append(
                f"resample:{source_frequency}->{query.frequency}"
            )
        return {
            "exchange": query.exchange.lower(),
            "market_type": query.market_type.value,
            "symbol": _core_symbol(query.symbol),
            "frequency": query.frequency.value,
            "source_frequency": source_frequency.value,
            "provider_id": provider_id,
            "source": "provider_cache",
            "data_layer": query.layer.value,
            "transformations": transformations,
            "count": count,
            "has_more": has_more,
            "quality_summary": {
                "flagged_bars": flagged,
                "incomplete_bars": incomplete,
            },
        }


def validate_kline_frequency(
    frequency: DataFrequency,
    layer: DataLayer | None,
) -> str | None:
    """校验 (frequency, layer) 组合是否可被分层管线服务。

    返回 None 表示可服务；否则返回人类可读的错误描述。供策略数据依赖
    声明校验复用，避免声明了管线无法服务的组合（注册时即拦截）。
    """
    if frequency not in _FREQUENCY_DELTA:
        return f"不支持的 K 线周期: {frequency}"
    if layer in {DataLayer.BRONZE, DataLayer.SILVER} and frequency not in _NATIVE_FREQUENCIES:
        return f"{layer.value} 层只提供原生周期 1m/1d；派生周期请使用 gold 层"
    return None


def _core_symbol(symbol: str) -> str:
    return symbol.replace("/", "").strip().upper()


def _ordered(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True)
    return result.sort_values("timestamp").reset_index(drop=True)


def _bounded_frame(
    frame: pd.DataFrame,
    limit: int,
    order: str,
) -> tuple[pd.DataFrame, bool]:
    has_more = len(frame) > limit
    if not has_more:
        return frame, False
    page = frame.head(limit) if order == "ASC" else frame.tail(limit)
    return page.reset_index(drop=True), True


def _aligned_start(
    start: pd.Timestamp | None,
    resample_rule: str | None,
) -> pd.Timestamp | None:
    if start is None or resample_rule is None:
        return start
    if resample_rule == "W-MON":
        return start.normalize() - pd.Timedelta(days=start.weekday())
    return start.floor(resample_rule)


def _aggregate(frame: pd.DataFrame, rule: str | None) -> pd.DataFrame:
    if frame.empty or rule is None:
        return frame
    indexed = frame.set_index("timestamp")
    aggregations: dict[str, Any] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    for column in _SUM_COLUMNS:
        if column in frame:
            aggregations[column] = lambda values: values.sum(min_count=1)
    if "source_quality_flags" in frame:
        aggregations["source_quality_flags"] = _combine_quality_flags
    grouper = (
        pd.Grouper(freq=rule, label="left", closed="left")
        if rule == "W-MON"
        else pd.Grouper(freq=rule)
    )
    return (
        indexed.groupby(grouper)
        .agg(aggregations)
        .dropna(subset=["close"])
        .reset_index()
    )


def _complete_before_end(
    frame: pd.DataFrame,
    end: pd.Timestamp | None,
    frequency: DataFrequency,
) -> pd.DataFrame:
    if frame.empty or end is None:
        return frame
    closes = pd.to_datetime(frame["timestamp"], utc=True) + _FREQUENCY_DELTA[frequency]
    return frame.loc[closes <= end].reset_index(drop=True)


def _attach_source_quality_flags(
    frame: pd.DataFrame,
    *,
    frequency: DataFrequency,
    now: datetime,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    delta = _FREQUENCY_DELTA[frequency]
    result = frame.copy()
    result["source_quality_flags"] = [
        _quality_flags(
            row,
            is_closed=pd.Timestamp(row.timestamp).to_pydatetime() + delta <= now,
        )
        for row in frame.itertuples(index=False)
    ]
    return result


def _combine_quality_flags(values: pd.Series) -> list[str]:
    return list(dict.fromkeys(flag for flags in values for flag in flags))


def _bronze_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        row = dict(raw)
        row["timestamp"] = _utc_iso(row["timestamp"])
        records.append({key: _json_scalar(value) for key, value in row.items()})
    return records


def _silver_records(
    frame: pd.DataFrame,
    *,
    frequency: DataFrequency,
    now: datetime,
) -> list[dict[str, Any]]:
    delta = _FREQUENCY_DELTA[frequency]
    records: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        open_time = pd.Timestamp(row.timestamp)
        close_time = open_time.to_pydatetime() + delta
        is_closed = close_time <= now
        flags = _quality_flags(row, is_closed=is_closed)
        records.append(
            {
                "open_time": _utc_iso(open_time),
                "close_time": close_time.isoformat(),
                "open": _optional_float(getattr(row, "open", None)),
                "high": _optional_float(getattr(row, "high", None)),
                "low": _optional_float(getattr(row, "low", None)),
                "close": _optional_float(getattr(row, "close", None)),
                "volume": _optional_float(getattr(row, "volume", None)),
                "quote_volume": _optional_float(getattr(row, "quote_volume", None)),
                "trade_count": _optional_int(getattr(row, "trades", None)),
                "taker_buy_volume": _optional_float(
                    getattr(row, "taker_buy_volume", None)
                ),
                "taker_buy_quote_volume": _optional_float(
                    getattr(row, "taker_buy_quote_volume", None)
                ),
                "is_closed": is_closed,
                "quality_flags": flags,
            }
        )
    return records


def _quality_flags(row: Any, *, is_closed: bool) -> list[str]:
    inherited = getattr(row, "source_quality_flags", None)
    flags = list(dict.fromkeys(inherited)) if isinstance(inherited, list) else []
    price_values = [
        getattr(row, "open", None),
        getattr(row, "high", None),
        getattr(row, "low", None),
        getattr(row, "close", None),
    ]
    optional_values = [
        getattr(row, "quote_volume", None),
        getattr(row, "trades", None),
        getattr(row, "taker_buy_volume", None),
        getattr(row, "taker_buy_quote_volume", None),
    ]
    if any(_missing(value) for value in price_values + optional_values):
        flags.append("missing_field")
    if not any(_missing(value) for value in price_values):
        open_, high, low, close = (
            float(cast(Any, value)) for value in price_values
        )
        if not (low <= open_ <= high and low <= close <= high):
            flags.append("invalid_ohlc")
        if any(value <= 0 for value in (open_, high, low, close)):
            flags.append("non_positive_price")
    volume = getattr(row, "volume", None)
    if not _missing(volume) and float(cast(Any, volume)) < 0:
        flags.append("negative_value")
    if not is_closed:
        flags.append("incomplete")
    return list(dict.fromkeys(flags))


def _missing(value: Any) -> bool:
    return value is None or bool(pd.isna(value))


def _optional_float(value: Any) -> float | None:
    return None if _missing(value) else float(value)


def _optional_int(value: Any) -> int | None:
    return None if _missing(value) else int(value)


def _utc_iso(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()


def _json_scalar(value: Any) -> Any:
    if _missing(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value
