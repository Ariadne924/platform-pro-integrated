"""策略数据依赖：声明解析 / 校验 / 精确 Provider 解析 / 取数对齐。

策略通过 MD frontmatter 的 ``data_dependencies`` 显式声明数据需求，本模块
负责把声明解析为 :class:`StrategyDataDependency`，并按依赖逐项精确解析
Provider（``allow_fallback=False``，禁止静默跨市场回退）后取数返回带血缘
元数据的集合（bundle）：

- ``kline`` 走 Bronze→Silver→Gold 分层管线（:class:`KlineLayerPipeline`），
  只消费已闭合（``is_closed``）K 线，缺失值不填零；
- 其余 ``data_type``（funding_rate / open_interest / ...）走 Provider fetch；
- 一切时间均为 UTC；
- 多资产对齐仅允许严格时间交集（``intersect``）或显式的过去向对齐
  （``past``，ffill），并支持按信号时刻做 as-of 价格对齐（回测 price_data）。

对外主要接口：
- ``parse_data_dependencies(meta)``       声明解析 + 校验
- ``resolve_dependency_provider(dep, ...)`` 精确解析 Provider（无 fallback）
- ``fetch_strategy_data(deps, ...)``       一次解析多数据依赖集合（async）
- ``align_dependency_groups(bundle, deps)`` 按 group 对齐
- ``build_price_data(bundle, deps, times)`` 按信号时刻构建回测价格（as-of）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.kline_layers import (
    DataLayer,
    KlineLayerPipeline,
    KlineQuery,
    _core_symbol,
)
from superplatform.data.provider_registry import (
    DataProviderRegistry,
    resolve_provider_for_data_type,
)

# 允许的 data_type（与 Provider 注册中心保持一致）
KNOWN_DATA_TYPES = (
    "kline",
    "funding_rate",
    "open_interest",
    "basis",
    "mark_price",
    "trade",
    "order_book",
)

# 对齐规则
ALIGN_INTERSECT = "intersect"
ALIGN_PAST = "past"
ALIGN_RULES = (ALIGN_INTERSECT, ALIGN_PAST)
DEFAULT_GROUP = "primary"

# kline 依赖允许声明的字段
KLINE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_volume",
)

# kline 取数默认页大小（回测需覆盖多年，远大于交互接口的 5000 上限）
DEFAULT_KLINE_LIMIT = 200_000


@dataclass(frozen=True)
class StrategyDataDependency:
    """单个数据依赖声明。"""

    id: str
    exchange: str
    market_type: MarketType
    data_type: str
    symbol: str
    frequency: DataFrequency
    layer: DataLayer | None = None
    required_fields: tuple[str, ...] = ()
    closed_only: bool = True
    group: str = DEFAULT_GROUP
    align: str = ALIGN_INTERSECT

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "exchange": self.exchange.lower(),
            "market_type": self.market_type.value,
            "data_type": self.data_type,
            "symbol": _core_symbol(self.symbol),
            "frequency": self.frequency.value,
            "layer": self.layer.value if self.layer else None,
            "required_fields": list(self.required_fields),
            "closed_only": self.closed_only,
            "group": self.group,
            "align": self.align,
        }


# -------------------------------------------------------------------
# 声明解析与校验
# -------------------------------------------------------------------


def parse_data_dependencies(
    meta: dict[str, Any],
) -> tuple[list[StrategyDataDependency], list[str]]:
    """从策略 frontmatter 解析 data_dependencies。

    返回 ``(deps, errors)``；errors 非空表示存在声明不合规的依赖，调用方
    据此决定是否拒绝注册（策略协议规则 11）。
    """
    raw = meta.get("data_dependencies")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["data_dependencies 必须是一个列表"]
    deps: list[StrategyDataDependency] = []
    errors: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"data_dependencies[{i}] 必须是对象")
            continue
        issue = _validate_dep(item, i)
        if issue:
            errors.append(issue)
            continue
        deps.append(_build_dep(item))
    return deps, errors


def _validate_dep(item: dict[str, Any], index: int) -> str | None:
    label = f"data_dependencies[{index}]"
    for key in ("id", "exchange", "data_type", "symbol", "frequency"):
        if key not in item or not str(item[key]).strip():
            return f"{label}.{key} 缺失或为空"
    market = item.get("market_type")
    if market is None or market not in {m.value for m in MarketType}:
        return f"{label}.market_type 必须是 {[m.value for m in MarketType]}"
    if item["data_type"] not in KNOWN_DATA_TYPES:
        return f"{label}.data_type 不支持: {item['data_type']}"
    try:
        DataFrequency(item["frequency"])
    except ValueError:
        return f"{label}.frequency 必须是 {[f.value for f in DataFrequency]}"
    if item.get("layer") is not None:
        try:
            DataLayer(item["layer"])
        except ValueError:
            return f"{label}.layer 必须是 bronze/silver/gold"
    if item.get("align") is not None and item["align"] not in ALIGN_RULES:
        return f"{label}.align 必须是 {list(ALIGN_RULES)}"
    fields = item.get("required_fields", [])
    if fields:
        if not isinstance(fields, list):
            return f"{label}.required_fields 需要列表"
        if item["data_type"] == "kline" and any(
            f not in KLINE_FIELDS for f in fields
        ):
            return f"{label}.required_fields 含不支持的字段"
    return None


def _build_dep(item: dict[str, Any]) -> StrategyDataDependency:
    return StrategyDataDependency(
        id=str(item["id"]),
        exchange=str(item["exchange"]),
        market_type=MarketType(item["market_type"]),
        data_type=str(item["data_type"]),
        symbol=str(item["symbol"]),
        frequency=DataFrequency(item["frequency"]),
        layer=DataLayer(item["layer"]) if item.get("layer") else None,
        required_fields=tuple(str(f) for f in item.get("required_fields", [])),
        closed_only=bool(item.get("closed_only", True)),
        group=str(item.get("group", DEFAULT_GROUP)),
        align=str(item.get("align", ALIGN_INTERSECT)),
    )


# -------------------------------------------------------------------
# 精确 Provider 解析（禁止静默 fallback）
# -------------------------------------------------------------------


def resolve_dependency_provider(
    dep: StrategyDataDependency,
    registry: DataProviderRegistry,
    *,
    disabled: set[str] | None = None,
) -> str:
    """按 exchange + market_type + data_type 精确解析 Provider。

    找不到精确匹配时抛 ``ValueError``，绝不回退到任意 Provider。
    """
    return resolve_provider_for_data_type(
        dep.exchange.lower(),
        dep.market_type.value,
        dep.data_type,
        registry,
        disabled=disabled,
        allow_fallback=False,
    )


def _exact_resolver(
    registry: DataProviderRegistry,
    disabled: set[str] | None,
) -> Any:
    def resolve(exchange: str, market: str, data_type: str) -> str:
        return resolve_provider_for_data_type(
            exchange, market, data_type, registry,
            disabled=disabled, allow_fallback=False,
        )

    return resolve


# -------------------------------------------------------------------
# 取数
# -------------------------------------------------------------------


async def fetch_strategy_data(
    deps: list[StrategyDataDependency],
    *,
    store: Any,
    registry: DataProviderRegistry,
    disabled: set[str] | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    limit: int = DEFAULT_KLINE_LIMIT,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """一次解析全部数据依赖。

    返回 ``{dep_id: {"meta": {...}, "frame": DataFrame}}``。kline 依赖的
    frame 以 UTC DatetimeIndex 为索引、只含已闭合行（``closed_only``）；
    meta 含 source / provider_id / data_layer / quality_flags / time_range。
    """
    now = now or datetime.now(timezone.utc)
    bundle: dict[str, dict[str, Any]] = {}
    resolver = _exact_resolver(registry, disabled)
    for dep in deps:
        provider_id = resolve_dependency_provider(dep, registry, disabled=disabled)
        if dep.data_type == "kline":
            pipeline = KlineLayerPipeline(store, resolver, now=lambda: now)
            page = pipeline.load(
                KlineQuery(
                    exchange=dep.exchange,
                    market_type=dep.market_type,
                    symbol=dep.symbol,
                    frequency=dep.frequency,
                    layer=dep.layer or DataLayer.SILVER,
                    start=start,
                    end=end,
                    limit=limit,
                )
            )
            frame = page_to_frame(page, closed_only=dep.closed_only)
            meta = dict(page.meta)
            meta["time_range"] = _time_range(frame)
            bundle[dep.id] = {"meta": meta, "frame": frame}
        else:
            provider = registry.get(provider_id)
            frame = await provider.fetch(
                symbol=_core_symbol(dep.symbol),
                frequency=dep.frequency,
                start=start,
                end=end,
                limit=limit,
            )
            frame = _normalize_non_kline_frame(frame)
            bundle[dep.id] = {
                "meta": {
                    "data_type": dep.data_type,
                    "provider_id": provider_id,
                    "source": "provider_fetch",
                    "exchange": dep.exchange.lower(),
                    "market_type": dep.market_type.value,
                    "symbol": _core_symbol(dep.symbol),
                    "frequency": dep.frequency.value,
                    "count": len(frame),
                    "time_range": _time_range(frame),
                },
                "frame": frame,
            }
    return bundle


def page_to_frame(
    page: Any,
    *,
    closed_only: bool = True,
) -> pd.DataFrame:
    """把 KlinePage 记录转成以 UTC open_time 为索引的 DataFrame。

    只保留已闭合行（``closed_only``）；缺失值原样保留，不填零。
    """
    rows = [
        row for row in page.data if not closed_only or row.get("is_closed", True)
    ]
    if not rows:
        return pd.DataFrame(index=pd.DatetimeIndex([], name="timestamp", tz="UTC"))
    frame = pd.DataFrame(rows)
    if "open_time" in frame.columns:
        frame.index = pd.DatetimeIndex(
            pd.to_datetime(frame.pop("open_time"), utc=True)
        )
        frame.index.name = "timestamp"
    elif "timestamp" in frame.columns:
        # Bronze 层记录用原生 timestamp 键；同样归一为 UTC DatetimeIndex
        frame.index = pd.DatetimeIndex(
            pd.to_datetime(frame.pop("timestamp"), utc=True)
        )
        frame.index.name = "timestamp"
    return frame.sort_index()


def _normalize_non_kline_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if not isinstance(result.index, pd.DatetimeIndex):
        if "timestamp" in result.columns:
            result.index = pd.DatetimeIndex(
                pd.to_datetime(result["timestamp"], utc=True)
            )
            result.index.name = "timestamp"
    return result


def _time_range(frame: pd.DataFrame) -> dict[str, str | None]:
    if frame.empty:
        return {"start": None, "end": None}
    return {
        "start": frame.index.min().isoformat(),
        "end": frame.index.max().isoformat(),
    }


# -------------------------------------------------------------------
# 对齐与回测价格
# -------------------------------------------------------------------


def align_dependency_groups(
    bundle: dict[str, dict[str, Any]],
    deps: list[StrategyDataDependency],
) -> dict[str, pd.DataFrame]:
    """按 group 把同一对齐域内的依赖合并为一个对齐后的 DataFrame。

    对齐后列名以 ``{dep_id}.{column}`` 前缀区分来源。
    """
    groups: dict[str, dict[str, pd.DataFrame]] = {}
    for dep in deps:
        groups.setdefault(dep.group, {})[dep.id] = bundle[dep.id]["frame"]
    result: dict[str, pd.DataFrame] = {}
    for group, frames in groups.items():
        align = next(
            (d.align for d in deps if d.group == group), ALIGN_INTERSECT
        )
        result[group] = align_dependency_frames(frames, align)
    return result


def align_dependency_frames(
    frames: dict[str, pd.DataFrame],
    align: str = ALIGN_INTERSECT,
) -> pd.DataFrame:
    """把同组多个依赖的 frame 对齐到一个公共索引。

    ``intersect``：多资产严格时间交集（缺失整体剔除）；
    ``past``：过去向对齐（union 索引 + ffill，只回填已发生的值）。
    """
    if not frames:
        return pd.DataFrame()
    if align == ALIGN_PAST:
        index = pd.DatetimeIndex(
            sorted(set().union(*(set(f.index) for f in frames.values())))
        )
        parts: dict[str, pd.DataFrame] = {}
        for dep_id, frame in frames.items():
            merged = frame.reindex(index).ffill()
            merged.columns = [f"{dep_id}.{c}" for c in merged.columns]
            parts[dep_id] = merged
        return pd.concat(list(parts.values()), axis=1)
    index = frames[next(iter(frames))].index
    for frame in frames.values():
        index = index.intersection(frame.index)
    parts = {}
    for dep_id, frame in frames.items():
        subset = frame.loc[index].copy()
        subset.columns = [f"{dep_id}.{c}" for c in subset.columns]
        parts[dep_id] = subset
    return pd.concat(list(parts.values()), axis=1)


def build_price_data(
    bundle: dict[str, dict[str, Any]],
    deps: list[StrategyDataDependency],
    signal_times: pd.DatetimeIndex,
) -> dict[str, pd.DataFrame]:
    """按信号时刻构建回测 price_data（每 symbol 一个 close 序列）。

    对齐规则为显式的过去向 as-of：每个信号时刻取 ``close_time <= 时刻``
    的最近一根已闭合 K 线 close，杜绝用尚未闭合的桶（前视）。
    """
    price_data: dict[str, pd.DataFrame] = {}
    for dep in deps:
        if dep.data_type != "kline":
            continue
        frame = bundle[dep.id]["frame"]
        if frame.empty or "close" not in frame.columns or "close_time" not in frame.columns:
            continue
        symbol = _core_symbol(dep.symbol)
        closes = pd.Series(
            frame["close"].to_numpy(dtype="float64"),
            index=pd.DatetimeIndex(pd.to_datetime(frame["close_time"], utc=True)),
        )
        closes = closes[~closes.isna()].sort_index()
        times = signal_times.sort_values()
        aligned = (
            closes.reindex(closes.index.union(times)).ffill().reindex(times)
        )
        price_data[symbol] = pd.DataFrame(
            {"timestamp": times, "close": aligned.to_numpy()}
        )
    return price_data
