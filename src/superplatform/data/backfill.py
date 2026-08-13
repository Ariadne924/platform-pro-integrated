"""历史数据回填 — Binance vision 归档 → DuckDB 增量缓存。

为什么是这个形状:

- 本机直连 fapi/api.binance.com 超时(00 实测),只有 data.binance.vision
  归档可达;而标准 BinanceKLineProvider 的长区间路径(hybrid)固定要
  打 REST 尾段,REST 不通则整段失败。因此回填用 **vision-only 源**:
  三个薄 DataProvider 直接调 BinanceVisionClient,不打任何 REST。
  provider_id 与运行时标准 provider 完全一致(binance-perp-kline 等),
  缓存表(pv_*)与增量书签(empty_ranges)因此与运行时/validate-report
  完全对齐——网络恢复后标准 provider 读同一张表,只补增量。
- 增量与断点续跑复用 DataCache / CachingProvider:已缓存区间不重拉,
  源端已验证为空的区间记入 empty_ranges 永久跳过。sub-daily kline 的
  分块用**稠密覆盖判定**(窗口内行数 + 已验证为空 bar 数 vs 期望 bar 数):
  DataCache 的 min/max 跨度看不到缓存内部空洞(实测首轮回填曾把
  2025-01→2026-07 的 19 个月大洞误判为已覆盖),分块路径因此直调源
  并写穿,不经过 CachingProvider。
- 1m 数据量大(单标的 2019→now 约 350 万行),按月分块、每块落库一次,
  中断后重跑同一条命令即可续跑。**分块按时间倒序**(最新月最先):
  BinanceVisionClient._earliest_archive_date 会把「该请求区间内无归档」
  的结论(None)按 (symbol,kind,interval,market) 缓存——若先跑最早月,
  None 被固化,后续月份全被误判为空。先跑最新月让二分搜索在
  latest_available≈昨天 下定到真实首日,之后早于首日的块自然短路为空。
- 现货归档早于 UM 永续(metrics 下界 2019-09 是为永续写的)。回填客户端
  把 _EARLIEST_POSSIBLE 放宽到 2017,现货 BTC/ETH 才能取到 2019-01。
- 死规矩落实:时间戳全程 UTC-aware(vision 解析即 utc=True,Store 连接
  SET TimeZone='UTC');现货/永续分 provider 分表,结构上不混;永续请求
  起点早于 2019-09-25(币安永续上线)时钳到该日,钳掉的前缀按
  「已验证为空」记录,不报错。
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests

from superplatform.data.cache import CachingProvider, DataCache
from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.provider_registry import DataProvider
from superplatform.data.store import Store, provider_table
from superplatform.network.binance.vision import BinanceVisionClient
from superplatform.utils.time_utils import to_utc

logger = logging.getLogger(__name__)

# 币安 USDⓈ-M 永续上线日;更早的永续请求按空处理(钳位 + empty_range 记录)。
PERPETUAL_LAUNCH = pd.Timestamp("2019-09-25", tz="UTC")
# 现货回填默认起点(config data.backfill.spot_start 可覆盖)。
SPOT_FLOOR = pd.Timestamp("2019-01-01", tz="UTC")

# 与 vision 客户端一致的 kline 帧列(10 列,同 REST 投影)。
_KLINE_COLUMNS = (
    "timestamp", "open", "high", "low", "close",
    "volume", "quote_volume", "trades",
    "taker_buy_volume", "taker_buy_quote_volume",
)

# 数据类型与频率的合法取值(CLI choices 与 plan 构造共用)。
DATA_TYPES = ("kline", "funding_rate", "open_interest")
# DataFrequency.value 与 Binance interval 字符串一致("1m"/"1d"/"8h"…)。
_KLINE_FREQUENCIES = {
    DataFrequency.M1, DataFrequency.M5, DataFrequency.M15, DataFrequency.M30,
    DataFrequency.H1, DataFrequency.H4, DataFrequency.H8,
    DataFrequency.D1, DataFrequency.W1,
}
# 与 providers/binance_open_interest.py 的 period 映射一致(metrics 源为 5m,
# 重采样到请求周期)。
_OI_PERIODS: dict[DataFrequency, str] = {
    DataFrequency.M5: "5m",
    DataFrequency.M15: "15m",
    DataFrequency.M30: "30m",
    DataFrequency.H1: "1h",
    DataFrequency.H4: "4h",
    DataFrequency.D1: "1d",
}
# 已覆盖分块跳过判定用的 bar 宽(仅用于"尾条是否到位"的容差)。
_BAR_WIDTH: dict[DataFrequency, pd.Timedelta] = {
    DataFrequency.M1: pd.Timedelta(minutes=1),
    DataFrequency.M5: pd.Timedelta(minutes=5),
    DataFrequency.M15: pd.Timedelta(minutes=15),
    DataFrequency.M30: pd.Timedelta(minutes=30),
    DataFrequency.H1: pd.Timedelta(hours=1),
    DataFrequency.H4: pd.Timedelta(hours=4),
    DataFrequency.H8: pd.Timedelta(hours=8),
    DataFrequency.D1: pd.Timedelta(days=1),
    DataFrequency.W1: pd.Timedelta(weeks=1),
}


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """归一为 UTC-aware(DuckDB 读回 / 用户输入都可能是 naive)。"""
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


def parse_utc(value: str) -> pd.Timestamp:
    """解析命令行日期为 UTC-aware 时间戳(naive 输入按 UTC 解释)。"""
    return _as_utc(pd.Timestamp(value))


def _parse_kline_archive_auto_unit(content: bytes) -> pd.DataFrame:
    """parse_kline_archive 的单位嗅探版:open_time 按数量级判定 ms/µs/ns。

    实测(2026-08-13,curl 取证):vision 归档时间戳单位不统一——spot 月归档
    自 2025-01 起 open_time 为 **微秒**(1735689600000000),UM 永续月归档
    2026-07 仍为毫秒。基类 parse_kline_archive 固定 unit="ms":微秒值约
    1.7e15 按 ms 解释超出 datetime64[ns] 上界,溢出回绕成垃圾时间戳,
    随后被调用方的区间裁剪全部丢掉——表现为"2025 年起的数据静默消失"。
    这里先嗅探数量级再转换,其余处理(表头探测、列投影)与基类一致。
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
    if df.shape[1] < 12 or len(df) == 0:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    # 有表头则跳过(open_time 不是数值)。
    if pd.isna(pd.to_numeric(df.iloc[0, 0], errors="coerce")):
        df = df.iloc[1:].reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    # 保留与 REST 投影一致的 10 列,丢弃 close_time(6)与 ignore(11)。
    result = df.iloc[:, [0, 1, 2, 3, 4, 5, 7, 8, 9, 10]].copy()
    result.columns = list(_KLINE_COLUMNS)
    raw_ts = pd.to_numeric(result["timestamp"], errors="coerce")
    median = raw_ts.dropna().median()
    # 当前毫秒约 1.8e12(13 位),微秒 1.8e15(16 位),纳秒 1.8e18(19 位)。
    if median >= 1e17:
        unit = "ns"
    elif median >= 1e14:
        unit = "us"
    else:
        unit = "ms"
    result["timestamp"] = pd.to_datetime(raw_ts, unit=unit, utc=True)
    for col in _KLINE_COLUMNS[1:]:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    return result.dropna(subset=["timestamp"]).reset_index(drop=True)


class _BackfillVisionClient(BinanceVisionClient):
    """回填专用 vision 客户端,针对回填实测暴露的三个源端/解析问题加固:

    1. _EARLIEST_POSSIBLE 放宽到 2017:基类下界 2019-09-01 是为 UM 永续
       metrics 写的,而现货 kline 归档 2017 年就有(BTCUSDT spot 1m 自
       2017-08),回填现货 2019-01→now 需要更早的二分搜索下界。
    2. _earliest_archive_date:高端「单日 HEAD 判定有无归档」允许发布滞后
       ——实测 UM 永续日线/指标归档"今天-1"尚无文件(2026-08-12 404,
       2026-08-11 200),基类据此误判「该标的全无归档」并把 None 缓存,
       整条序列随后静默全空。这里单日 404 时向前回退最多 14 天找存在的
       归档日,且只有确定的最早日才写缓存(None 留给下次重试)。
    3. kline 归档解析按数量级嗅探时间戳单位(见
       _parse_kline_archive_auto_unit 的实测说明)。
    4. _download_url:归档服务偶发 5xx(实测 ETHUSDT metrics 单日 503),
       与连接/超时一样重试——基类只重试连接类错误,一次数千文件的 OI
       全量拉取会被单个文件的瞬时 503 整批带走。

    不改 network/ 源码(回填地界之外):以上全部以子类覆盖实现。
    """

    _EARLIEST_POSSIBLE = datetime(2017, 1, 1, tzinfo=UTC)
    # 高端探测回退天数(实测永续日线滞后 ≤11 天,留足余量)。
    _HIGH_PROBE_FALLBACK_DAYS = 14

    async def _earliest_archive_date(self, symbol, spec, *, latest_available=None):
        cache_key = (symbol, spec.kind, spec.interval, spec.market_path)
        cached = self._earliest_by_symbol.get(cache_key)
        if cached is not None:
            return cached

        hint = self._earliest_hints.get(symbol)
        if hint is not None and await self._archive_exists(symbol, hint, spec):
            self._earliest_by_symbol[cache_key] = hint
            return hint

        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        low = self._EARLIEST_POSSIBLE
        high = today - timedelta(days=1)
        if latest_available is not None:
            high = min(high, to_utc(latest_available)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        # 高端单日可能因发布滞后 404:向前回退找最近一个存在的归档日。
        existing = None
        probe = high
        for _ in range(self._HIGH_PROBE_FALLBACK_DAYS + 1):
            if probe < low:
                break
            if await self._archive_exists(symbol, probe, spec):
                existing = probe
                break
            probe -= timedelta(days=1)
        if existing is None:
            return None  # 不缓存 None:归档可能只是尚未发布

        lo, hi = low, existing
        while lo < hi:
            mid = lo + (hi - lo) // 2
            if await self._archive_exists(symbol, mid, spec):
                hi = mid
            else:
                lo = mid + timedelta(days=1)
        self._earliest_by_symbol[cache_key] = lo
        return lo

    async def _fetch_one(self, symbol, date, spec):
        """kline 用单位嗅探解析;metrics/funding 沿用基类(已实测正确)。"""
        if spec.kind != "klines":
            return await super()._fetch_one(symbol, date, spec)
        async with self._semaphore:
            content = await asyncio.to_thread(self._download, symbol, date, spec)
        if content is None:
            return pd.DataFrame(columns=_KLINE_COLUMNS)
        return _parse_kline_archive_auto_unit(content)

    async def _fetch_month(self, symbol, month, spec):
        if spec.kind != "klines":
            return await super()._fetch_month(symbol, month, spec)
        async with self._semaphore:
            content = await asyncio.to_thread(self._download_month, symbol, month, spec)
        if content is None:
            return pd.DataFrame(columns=_KLINE_COLUMNS)
        return _parse_kline_archive_auto_unit(content)

    def _download_url(self, url: str) -> bytes | None:
        """覆盖基类:5xx(归档服务瞬时故障,实测 503)与连接错误一样重试。"""
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=10)
                if response.status_code == 404:
                    return None
                if 500 <= response.status_code < 600 and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.content
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError,
                requests.exceptions.ChunkedEncodingError,
            ):
                if attempt == 2:
                    raise
                time.sleep(1.0 * (attempt + 1))
        return None  # pragma: no cover


class _VisionKLineSource(DataProvider):
    """kline 的 vision-only 回填源(provider_id 与运行时标准 provider 相同)。"""

    data_type = "kline"
    exchange = "binance"
    available_frequencies = set(_KLINE_FREQUENCIES)

    def __init__(self, market_type: MarketType, vision: BinanceVisionClient) -> None:
        self.market_type = market_type
        self.provider_id = (
            "binance-perp-kline"
            if market_type == MarketType.PERPETUAL
            else "binance-spot-kline"
        )
        self._vision = vision

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        interval = frequency.value
        market_path = (
            "futures/um" if self.market_type == MarketType.PERPETUAL else "spot"
        )
        start_ts = _as_utc(pd.Timestamp(start)) if start is not None else None
        end_ts = _as_utc(pd.Timestamp(end)) if end is not None else None
        df = await self._vision.fetch_klines_range(
            symbol, interval, market_path,
            start_ts or SPOT_FLOOR, end_ts or pd.Timestamp.now(tz="UTC"),
        )
        if df.empty:
            return df
        # 归档按自然日/月取整,裁回请求的 [start, end) 半开区间,保证相邻
        # 分块不重叠(缓存 upsert 幂等,重叠无害但浪费)。
        ts = df["timestamp"]
        if start_ts is not None:
            df = df[ts >= start_ts]
            ts = df["timestamp"]
        if end_ts is not None:
            df = df[ts < end_ts]
        return df.reset_index(drop=True)


class _VisionFundingRateSource(DataProvider):
    """funding_rate 的 vision-only 回填源(vision 只有月归档,本就无 REST)。"""

    data_type = "funding_rate"
    exchange = "binance"
    market_type = MarketType.PERPETUAL
    available_frequencies = {DataFrequency.H8}
    provider_id = "binance-perp-funding-rate"

    def __init__(self, vision: BinanceVisionClient) -> None:
        self._vision = vision

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        del frequency  # 结算节奏由币安决定,不重采样(同标准 provider)
        start_ts = _as_utc(pd.Timestamp(start)) if start is not None else PERPETUAL_LAUNCH
        end_ts = _as_utc(pd.Timestamp(end)) if end is not None else pd.Timestamp.now(tz="UTC")
        return await self._vision.fetch_funding_rate_range(symbol, start_ts, end_ts)


class _VisionOpenInterestSource(DataProvider):
    """open_interest 的 vision-only 回填源(metrics 日归档,重采样到请求周期)。"""

    data_type = "open_interest"
    exchange = "binance"
    market_type = MarketType.PERPETUAL
    available_frequencies = set(_OI_PERIODS)
    provider_id = "binance-perp-open-interest"

    def __init__(self, vision: BinanceVisionClient) -> None:
        self._vision = vision

    async def fetch(self, symbol, frequency, start=None, end=None, **kwargs):
        period = _OI_PERIODS.get(frequency)
        if period is None:
            raise ValueError(
                f"open_interest 不支持频率 {frequency.value};"
                f"支持: {sorted(p.value for p in _OI_PERIODS)}"
            )
        start_ts = _as_utc(pd.Timestamp(start)) if start is not None else PERPETUAL_LAUNCH
        end_ts = _as_utc(pd.Timestamp(end)) if end is not None else pd.Timestamp.now(tz="UTC")
        df = await self._vision.fetch_metrics_range(
            symbol, start_ts, end_ts, period=period
        )
        if df.empty:
            return df
        ts = df["timestamp"]
        return df[(ts >= start_ts) & (ts < end_ts)].reset_index(drop=True)


@dataclass
class BackfillSettings:
    """一次回填的全部参数(由 config + CLI 参数构造,见 settings_from_config)。"""

    symbols_perpetual: list[str] = field(default_factory=list)
    symbols_spot: list[str] = field(default_factory=list)
    start_perpetual: pd.Timestamp = PERPETUAL_LAUNCH
    start_spot: pd.Timestamp = SPOT_FLOOR
    # 用户显式请求的起点(钳位前),用于把钳掉的前缀记为已验证为空。
    requested_start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None  # None = 跑的时候取 now
    data_types: list[str] = field(default_factory=lambda: list(DATA_TYPES))
    kline_frequencies: list[DataFrequency] = field(
        default_factory=lambda: [DataFrequency.M1, DataFrequency.D1]
    )
    oi_frequency: DataFrequency = DataFrequency.D1
    cache_path: str = "data/cache.duckdb"
    max_concurrent: int = 8
    chunk_months: int = 1  # 1m 分块大小(月)
    proxy: str = ""        # HTTP 代理(exchanges 配置解析;空=直连)


def normalize_symbol(raw: str) -> str:
    """'BTC/USDT' / 'btcusdt' → 'BTCUSDT'。"""
    return raw.strip().upper().replace("/", "").replace("-", "")


def settings_from_config(config, args, proxy: str = "") -> BackfillSettings:
    """从 Config + argparse 命名空间构造 BackfillSettings。

    config 段(全部可选,有默认):
      data.symbols.perpetual / data.symbols.spot   — --all 的标的全集
      data.backfill.perpetual_start / spot_start   — 数据完整边界
      data.backfill.kline_frequencies / oi_frequency / chunk_months
      data.cache.path, data.max_concurrent_requests
    ``proxy`` 由调用方从 exchanges 配置解析(cli._first_exchange_proxy),
    保持 data 层不感知 exchanges.yaml 的形状。
    """
    symbols_perp = [normalize_symbol(s) for s in (config.get("data.symbols.perpetual") or [])]
    symbols_spot = [normalize_symbol(s) for s in (config.get("data.symbols.spot") or [])]

    market = getattr(args, "market", "both")
    if getattr(args, "all", False):
        chosen_perp = symbols_perp if market in ("perpetual", "both") else []
        chosen_spot = symbols_spot if market in ("spot", "both") else []
    else:
        raw: list[str] = []
        if getattr(args, "symbols", None):
            raw.extend(s for s in str(args.symbols).split(",") if s.strip())
        symbols_file = getattr(args, "symbols_file", None)
        if symbols_file:
            with open(symbols_file, encoding="utf-8") as fh:
                raw.extend(
                    line.split("#")[0].strip()
                    for line in fh
                    if line.split("#")[0].strip()
                )
        if not raw:
            raise SystemExit(
                "backfill 需要 --symbols <列表> 或 --symbols-file <文件> 或 --all"
            )
        symbols = [normalize_symbol(s) for s in raw]
        # 显式列表按 --market 分发:perpetual → 永续表,spot → 现货表,
        # both → 同一符号两个市场各回填一份(分表存放,不混)。
        chosen_perp = symbols if market in ("perpetual", "both") else []
        chosen_spot = symbols if market in ("spot", "both") else []

    perp_floor = parse_utc(config.get("data.backfill.perpetual_start") or "2019-09-25")
    perp_floor = max(perp_floor, PERPETUAL_LAUNCH)
    spot_floor = parse_utc(config.get("data.backfill.spot_start") or "2019-01-01")

    requested = getattr(args, "start", None)
    requested_ts = parse_utc(requested) if requested else None
    # 死规矩:永续早于 2019-09-25 按空处理不报错——钳位,前缀记 empty_range。
    start_perp = max(requested_ts, perp_floor) if requested_ts else perp_floor
    start_spot = max(requested_ts, spot_floor) if requested_ts else spot_floor

    end_raw = getattr(args, "end", None)
    end_ts = parse_utc(end_raw) if end_raw else None
    if end_ts is not None and end_ts <= start_perp and chosen_perp:
        raise SystemExit(f"--end({end_ts}) 必须晚于永续起点({start_perp})")
    if end_ts is not None and end_ts <= start_spot and chosen_spot:
        raise SystemExit(f"--end({end_ts}) 必须晚于现货起点({start_spot})")

    data_types = getattr(args, "data_type", None) or list(DATA_TYPES)
    unknown = [t for t in data_types if t not in DATA_TYPES]
    if unknown:
        raise SystemExit(f"未知 data-type: {unknown};可选: {list(DATA_TYPES)}")

    kline_freqs_raw = (
        getattr(args, "kline_frequencies", None)
        or config.get("data.backfill.kline_frequencies")
        or ["1m", "1d"]
    )
    if isinstance(kline_freqs_raw, str):
        kline_freqs_raw = [s for s in kline_freqs_raw.split(",") if s.strip()]
    kline_freqs = [DataFrequency(str(f).strip()) for f in kline_freqs_raw]
    bad = [f for f in kline_freqs if f not in _KLINE_FREQUENCIES]
    if bad:
        raise SystemExit(f"不支持的 kline 频率: {[f.value for f in bad]}")

    oi_freq = DataFrequency(
        str(getattr(args, "oi_frequency", None)
            or config.get("data.backfill.oi_frequency") or "1d")
    )
    if oi_freq not in _OI_PERIODS:
        raise SystemExit(f"不支持的 OI 频率: {oi_freq.value}")

    try:
        chunk_months = int(
            getattr(args, "chunk_months", None)
            or config.get("data.backfill.chunk_months") or 1
        )
    except (TypeError, ValueError):
        chunk_months = 1
    chunk_months = max(chunk_months, 1)

    try:
        max_concurrent = int(config.get("data.max_concurrent_requests", 8) or 8)
    except (TypeError, ValueError):
        max_concurrent = 8

    return BackfillSettings(
        symbols_perpetual=chosen_perp,
        symbols_spot=chosen_spot,
        start_perpetual=start_perp,
        start_spot=start_spot,
        requested_start=requested_ts,
        end=end_ts,
        data_types=data_types,
        kline_frequencies=kline_freqs,
        oi_frequency=oi_freq,
        cache_path=str(
            getattr(args, "cache", None)
            or config.get("data.cache.path")
            or "data/cache.duckdb"
        ),
        max_concurrent=max_concurrent,
        chunk_months=chunk_months,
        proxy=proxy,
    )


def _add_months(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    """月初对齐的 ts 向后推 N 个月。"""
    total = (ts.year * 12 + (ts.month - 1)) + months
    return pd.Timestamp(
        year=total // 12, month=total % 12 + 1, day=1, tz="UTC"
    )


def _month_chunks_desc(
    start: pd.Timestamp, end: pd.Timestamp, chunk_months: int
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """把 [start, end) 按 chunk_months 个月分块,**时间倒序**(最新块最先)。

    倒序是为了让 vision 客户端的最早归档二分搜索先在「有数据的近端」
    落定真实首日;若先跑最早月,搜索在latest_available过早的请求上得到
    None 并缓存,后续月份会被全部误判为空(见模块 docstring)。
    """
    chunks: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cur_end = end
    m = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if m >= end:  # end 恰在月初:本月整块已完成,从上一个月界开始
        m = _add_months(m, -chunk_months)
    while m > start:
        chunks.append((max(m, start), cur_end))
        cur_end = m
        m = _add_months(m, -chunk_months)
    if cur_end > start:
        chunks.append((start, cur_end))
    return [(s, e) for s, e in chunks if s < e]


def _chunk_coverage(
    store: Store,
    table: str,
    symbol: str,
    freq: DataFrequency,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[int, int]:
    """分块的稠密覆盖度: 返回 (仍缺 bar 数, 期望 bar 数)。

    缺 = 期望 - 窗口内缓存行数 - 窗口内已验证为空的 bar 数。
    min/max 跨度只能证明"两端有数据",看不到缓存区间内部的空洞(实测:
    首轮回填因归档微秒时间戳被按毫秒解析,2025-01→2026-07 的大洞按
    min/max 判定被全部误标"已覆盖",靠 validate-report 的 missing_pct
    才暴露)。稠密判定直接数 bar,与报告的 missing_pct 同口径,重跑自愈。
    empty_ranges 边界按 bar 网格取舍,残留误差 ≤2 bar/块,由调用方容差吸收。
    """
    bar = _BAR_WIDTH.get(freq, pd.Timedelta(days=1))
    expected = max(int(round((end - start) / bar, 6)), 0)
    if expected == 0:
        return 0, 0
    cached = store.count_series_range(table, symbol, freq.value, start, end)
    empty_bars = 0
    for er_start, er_end in store.empty_ranges_between(
        table, symbol, freq.value, start, end
    ):
        er_start = _as_utc(er_start)
        er_end = _as_utc(er_end)
        ov_start = max(er_start, start)
        ov_end = min(er_end, end)
        if ov_end <= ov_start:
            continue
        # (er_start, er_end) 开区间内的 bar 为缺失;空洞越过块左边界时
        # 块首 bar 也在洞内,补 1。
        n = max(int(round((ov_end - ov_start) / bar, 6)) - 1, 0)
        if er_start < start:
            n += 1
        empty_bars += n
    return max(expected - cached - empty_bars, 0), expected


async def _backfill_series(
    source: DataProvider,
    cache: DataCache,
    store: Store,
    symbol: str,
    frequency: DataFrequency,
    start: pd.Timestamp,
    end: pd.Timestamp,
    chunk_months: int,
    log,
) -> tuple[str, int, str | None]:
    """回填单条序列(单 provider+symbol+frequency),返回 (标签, 新拉行数, 错误)。

    - 非分块序列(1d/funding/OI,全区间仅几千行):走 CachingProvider,
      由 DataCache 做 before/after 增量。
    - 分块序列(sub-daily kline):按月倒序分块,每块先做稠密覆盖判定
      (_chunk_coverage),已覆盖跳过;未覆盖**直调源**并写穿缓存——
      CachingProvider 的 min/max 增量逻辑看不到缓存内部空洞,洞里的块
      会被它当成已有数据返回空,所以分块路径不经过它。块内空洞/前后缀
      按"已验证为空"记录,与 DataCache 的书签语义一致。
    单块失败不拖垮整批:记错误后继续,重跑同命令补上。
    """
    label = f"{source.provider_id} · {symbol} · {frequency.value}"
    table = provider_table(source.provider_id)
    store.ensure_provider_table(source.provider_id, source.data_type)

    # 只有 sub-daily kline 需要分块(1m 单标的全历史约 350 万行);
    # funding/OI/日线的全区间帧只有几千行,单次 fetch 即可。
    bar = _BAR_WIDTH.get(frequency)
    chunked = (
        source.data_type == "kline"
        and bar is not None
        and bar < pd.Timedelta(days=1)
        and (end - start) > pd.Timedelta(days=62)
    )

    if not chunked:
        provider = CachingProvider(source, cache)
        try:
            df = await provider.fetch(symbol, frequency, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - 单序列失败不拖垮整批
            error = f"{type(exc).__name__}: {exc}"
            log(f"  [FAIL] {label} · 全区间: {error}")
            return label, 0, error
        log(f"  [ok] {label} · 全区间: {len(df)} rows")
        return label, len(df), None

    total = 0
    error: str | None = None
    for chunk_start, chunk_end in _month_chunks_desc(start, end, chunk_months):
        tag = f"{chunk_start:%Y-%m-%d}~{chunk_end:%Y-%m-%d}"
        missing, expected = _chunk_coverage(
            store, table, symbol, frequency, chunk_start, chunk_end
        )
        # 容差 8 bar:吸收 empty_ranges 边界在 bar 网格上的取舍误差。
        if expected > 0 and missing <= 8:
            log(f"  [skip] {label} · {tag}: 已覆盖(缺 {missing}/{expected})")
            continue
        try:
            df = await source.fetch(
                symbol, frequency, start=chunk_start, end=chunk_end
            )
        except Exception as exc:  # noqa: BLE001 - 单块失败不拖垮整批
            error = f"{type(exc).__name__}: {exc}"
            log(f"  [FAIL] {label} · {tag}: {error}")
            continue
        if df.empty:
            # 源端确认整块无数据(如永续上线前/归档起点前),记录后永跳。
            store.record_empty_range(
                table, symbol, frequency.value, chunk_start, chunk_end
            )
        else:
            cache.cache_segment(table, df, symbol, frequency.value)
            first = _as_utc(df["timestamp"].min())
            last = _as_utc(df["timestamp"].max())
            # 源端确认无数据的首尾段同样记书签(上市月前缀、归档尾部)。
            if first > chunk_start:
                store.record_empty_range(
                    table, symbol, frequency.value, chunk_start, first
                )
            if last < chunk_end - bar:
                store.record_empty_range(
                    table, symbol, frequency.value, last, chunk_end
                )
        total += len(df)
        log(f"  [ok] {label} · {tag}: {len(df)} rows")
    return label, total, error


async def _run(settings: BackfillSettings, log=print) -> int:
    """执行回填计划;返回进程退出码(0=全部成功,1=有失败序列)。"""
    end = settings.end or pd.Timestamp.now(tz="UTC")
    store = Store(settings.cache_path)
    try:
        vision = _BackfillVisionClient(
            settings.proxy,
            max_concurrent=max(settings.max_concurrent, 1),
        )
        cache = DataCache(store)
        sources: dict[tuple[str, str], DataProvider] = {}
        if settings.symbols_perpetual:
            sources[("kline", "perpetual")] = _VisionKLineSource(
                MarketType.PERPETUAL, vision
            )
            sources[("funding_rate", "perpetual")] = _VisionFundingRateSource(vision)
            sources[("open_interest", "perpetual")] = _VisionOpenInterestSource(vision)
        if settings.symbols_spot:
            sources[("kline", "spot")] = _VisionKLineSource(MarketType.SPOT, vision)

        # 钳位前缀记为「已验证为空」:永续早于 2019-09-25(及 config 边界)
        # 的请求不报错,留书签证明这段不存在,增量重跑不再探。
        if settings.requested_start is not None:
            for (data_type, market), source in sources.items():
                if data_type not in settings.data_types:
                    continue
                start = (
                    settings.start_perpetual
                    if market == "perpetual"
                    else settings.start_spot
                )
                if settings.requested_start >= start:
                    continue
                symbols = (
                    settings.symbols_perpetual
                    if market == "perpetual"
                    else settings.symbols_spot
                )
                freqs = (
                    settings.kline_frequencies
                    if data_type == "kline"
                    else [DataFrequency.H8 if data_type == "funding_rate"
                          else settings.oi_frequency]
                )
                table = provider_table(source.provider_id)
                store.ensure_provider_table(source.provider_id, source.data_type)
                for symbol in symbols:
                    for freq in freqs:
                        store.record_empty_range(
                            table, symbol, freq.value,
                            settings.requested_start, start,
                        )
                log(
                    f"  [note] {source.provider_id}: 请求起点 "
                    f"{settings.requested_start:%Y-%m-%d} 早于数据边界 "
                    f"{start:%Y-%m-%d},前缀按已验证为空记录"
                )

        tasks = []
        for (data_type, market), source in sources.items():
            if data_type not in settings.data_types:
                continue
            symbols = (
                settings.symbols_perpetual
                if market == "perpetual"
                else settings.symbols_spot
            )
            start = (
                settings.start_perpetual
                if market == "perpetual"
                else settings.start_spot
            )
            if start >= end:
                continue
            if data_type == "kline":
                freqs = settings.kline_frequencies
            elif data_type == "funding_rate":
                freqs = [DataFrequency.H8]
            else:
                freqs = [settings.oi_frequency]
            for symbol in symbols:
                for freq in freqs:
                    tasks.append(
                        _backfill_series(
                            source, cache, store, symbol, freq, start, end,
                            settings.chunk_months, log,
                        )
                    )

        if not tasks:
            log("没有需要回填的序列(检查 --symbols/--market/--data-type)。")
            return 0

        log(f"回填 {len(tasks)} 条序列 → {settings.cache_path}(end={end})")
        results = await asyncio.gather(*tasks)
        total_rows = sum(r[1] for r in results)
        failures = [(r[0], r[2]) for r in results if r[2] is not None]
        log(f"完成: {total_rows} 行 / {len(results)} 条序列。")
        if failures:
            log(f"有 {len(failures)} 条序列存在失败块(重跑同命令可增量补上):")
            for label, err in failures:
                log(f"  [FAIL] {label}: {err}")
            return 1
        return 0
    finally:
        store.close()


def run_backfill(settings: BackfillSettings, log=print) -> int:
    """同步入口:执行回填并返回退出码。"""
    return asyncio.run(_run(settings, log=log))
