"""行情 API（sim_platform 形状）：GET /api/market/klines、/api/market/tickers。

取数走 01 的 DuckDB 缓存（superplatform_web.state.store），period 聚合用
pandas resample（图表展示层聚合，非评估指标）；均线叠加见 ma_overlays.py。
符号映射：UI 传 BTC/USDT，数据层查 BTCUSDT。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from superplatform.data.enums import MarketType
from superplatform.data.store import provider_table
from superplatform_web import simserve, state
from superplatform_web.ma_overlays import compute_ma_overlays, ma_lookback_bars

router = APIRouter(prefix="/api/market", tags=["sim-market"])

# 各周期的分钟数（自动降采样与均线回溯共用）
_PERIOD_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440, "1w": 10080,
}

# period → (缓存基础频率, pandas resample 规则)；None = 无需聚合
_PERIOD_SOURCE = {
    "1m": ("1m", None),
    "5m": ("1m", "5min"),
    "15m": ("1m", "15min"),
    "30m": ("1m", "30min"),
    "1h": ("1m", "1h"),
    "4h": ("1m", "4h"),
    "1d": ("1d", None),
    "1w": ("1d", "7D"),
}

_OHLCV_AGG = {
    "open": "first", "high": "max", "low": "min", "close": "last",
    "volume": "sum", "quote_volume": "sum", "trades": "sum",
    "taker_buy_volume": "sum",
}


def _parse_iso(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _auto_period(start: datetime | None, end: datetime | None, requested: str, limit: int) -> str:
    """按时间范围与 limit 自动选择聚合周期（移植 sim routes_market 逻辑）。"""
    if end is None:
        end = datetime.now(timezone.utc)
    if start is None:
        return requested
    minutes = max(1, int((end - start).total_seconds() / 60))
    if minutes <= limit:
        return requested
    requested_min = _PERIOD_MINUTES.get(requested, 1)
    needed = minutes / limit
    for p, m in sorted(_PERIOD_MINUTES.items(), key=lambda x: x[1]):
        if m >= max(needed, requested_min):
            return p
    return "1w"


def _fetch_ohlcv(store, table: str, symbol: str, base_freq: str,
                 start: datetime | None, end: datetime | None, limit: int) -> pd.DataFrame:
    df = store.query_series(
        table, symbol, base_freq,
        start=pd.Timestamp(start) if start else None,
        end=pd.Timestamp(end) if end else None,
        limit=limit, order="ASC",
    )
    return df


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df
    ts = pd.to_datetime(df["timestamp"], utc=True)
    g = df.set_index(ts).groupby(pd.Grouper(freq=rule))
    agg = {k: v for k, v in _OHLCV_AGG.items() if k in df.columns}
    out = g.agg(agg).dropna(subset=["close"]).reset_index(names="timestamp")
    return out


@router.get("/klines")
async def klines(
    symbol: str = Query(..., description="交易对，如 BTC/USDT"),
    limit: int = Query(300, ge=1, le=5000),
    start: str | None = Query(None),
    end: str | None = Query(None),
    period: str = Query("1m"),
    ma: str | None = Query(None),
) -> dict[str, Any]:
    """K 线数据（形状与 sim /api/market/klines 一致）。

    - 不传 start/end：最近 limit 根 1m；
    - 传 start/end：按 period 聚合，超出 limit 自动提升周期降采样；
    - ma=all 或逗号分隔 key：返回均线叠加（取数向前回溯窗口，前缀只算不返回）。
    """
    store = simserve.get_store()
    if store is None:
        raise HTTPException(status_code=503, detail="数据缓存未启用（data.cache.enabled=false）")
    valid_periods = set(_PERIOD_MINUTES)
    if period not in valid_periods:
        return {"error": f"无效 period，支持: {', '.join(sorted(valid_periods))}"}

    core = simserve.core_symbol(symbol)
    table = simserve.kline_table()

    ma_keys: list[str] | None = None
    if ma and ma.strip().lower() != "all":
        ma_keys = [k.strip() for k in ma.split(",") if k.strip()]

    effective_period = period
    prefix = 0
    if start is None and end is None:
        # 兼容旧行为：最近 limit 根（DESC 取最新 N 根再翻回时间正序）
        base_freq, rule = _PERIOD_SOURCE["1m"]
        if ma:
            prefix = ma_lookback_bars(effective_period, ma_keys)
        df = store.query_series(table, core, base_freq, limit=limit + prefix, order="DESC")
        df = df.sort_values("timestamp").reset_index(drop=True)
        prefix = max(0, len(df) - limit)
        df_data = df.iloc[prefix:]
    else:
        start_dt = _parse_iso(start) if start else None
        end_dt = _parse_iso(end) if end else None
        chosen = _auto_period(start_dt, end_dt, period, limit)
        effective_period = chosen
        base_freq, rule = _PERIOD_SOURCE[chosen]
        fetch_start = start_dt
        if ma and start_dt is not None:
            lookback = ma_lookback_bars(chosen, ma_keys)
            fetch_start = start_dt - timedelta(minutes=_PERIOD_MINUTES[chosen] * lookback)
        # 估算行数给个硬上限（1m 拉 7 年也只发生在 ma 回溯外的极端输入）
        est = 2_000_000 if base_freq == "1m" else 200_000
        df_full = _fetch_ohlcv(store, table, core, base_freq, fetch_start, end_dt, est)
        df_full = df_full.sort_values("timestamp").reset_index(drop=True)
        if rule:
            df_full = _resample(df_full, rule)
        if start_dt is not None and not df_full.empty:
            start_ts = pd.Timestamp(start_dt)
            ts = pd.to_datetime(df_full["timestamp"], utc=True)
            df_prefix = df_full[ts < start_ts]
            df_data = df_full[ts >= start_ts]
        else:
            df_prefix = df_full.iloc[0:0]
            df_data = df_full
        if len(df_data) > limit:
            keep: np.ndarray = np.linspace(0, len(df_data) - 1, limit).astype(int)
            df_data = df_data.iloc[keep]
        df = pd.concat([df_prefix, df_data]).reset_index(drop=True)
        prefix = len(df_prefix)

    if df_data.empty:
        return {"symbol": symbol, "period": effective_period, "limit": limit,
                "count": 0, "data": [], "ma": {}}

    ma_data: dict[str, Any] = {}
    if ma:
        overlays = compute_ma_overlays(df["close"].reset_index(drop=True), effective_period, ma_keys)
        ma_data = {k: v[prefix:] for k, v in overlays.items()}

    records = []
    for row in df_data.itertuples(index=False):
        records.append({
            "ts": simserve._ts_iso(row.timestamp),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "quote_volume": _f(getattr(row, "quote_volume", None)),
            "trades": _i(getattr(row, "trades", None)),
            "taker_buy_volume": _f(getattr(row, "taker_buy_volume", None)),
            "vwap": None,  # 01 缓存未存 vwap 列，如实返回 null
        })
    return {"symbol": symbol, "period": effective_period, "limit": limit,
            "count": len(records), "data": records, "ma": ma_data}


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        f = float(v)
        return int(f) if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _exact_market_table(
    exchange: str,
    market_type: MarketType,
    data_type: str,
) -> str:
    provider_id = state.resolve_provider_for_data_type(
        exchange.lower(),
        market_type.value,
        data_type,
        allow_fallback=False,
    )
    return provider_table(provider_id)


def _optional_market_table(
    exchange: str,
    market_type: MarketType,
    data_type: str,
) -> str | None:
    try:
        return _exact_market_table(exchange, market_type, data_type)
    except ValueError:
        return None


@router.get("/tickers")
async def tickers(
    exchange: str | None = None,
    market_type: MarketType | None = None,
) -> dict[str, Any]:
    """各 symbol 最新行情快照：最新价、24h 涨跌、最新 funding、最新 OI。

    标的清单 = 研究池中缓存确有 1m K 线的标的（真实覆盖，不凭空列）。
    """
    store = simserve.get_store()
    if store is None:
        raise HTTPException(status_code=503, detail="数据缓存未启用（data.cache.enabled=false）")

    if (exchange is None) != (market_type is None):
        raise HTTPException(
            status_code=422,
            detail="exchange 与 market_type 必须同时提供",
        )

    if exchange is not None and market_type is not None:
        try:
            kline_table = _exact_market_table(exchange, market_type, "kline")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        funding_table = _optional_market_table(exchange, market_type, "funding_rate")
        oi_table = _optional_market_table(exchange, market_type, "open_interest")
        candidates = state.config.get(f"data.symbols.{market_type.value}") or []
        symbols = []
        for raw_symbol in candidates:
            core = simserve.core_symbol(str(raw_symbol))
            try:
                info = store.series_range(kline_table, core, "1m")
            except Exception:
                continue
            if info.get("count", 0) > 0:
                symbols.append(core)
    else:
        kline_table = simserve.kline_table()
        funding_table = simserve.funding_table()
        oi_table = simserve.oi_table()
        symbols = simserve.cached_kline_symbols("1m")

    result: dict[str, Any] = {}
    for core in symbols:
        row: dict[str, Any] = {"symbol": simserve.ui_symbol(core)}
        recent = store.query_series(
            kline_table, core, "1m", limit=1441, order="DESC"
        )
        if not recent.empty:
            recent = recent.sort_values("timestamp")
            row["last_price"] = float(recent["close"].iloc[-1])
            row["last_ts"] = simserve._ts_iso(recent["timestamp"].iloc[-1])
            # 24h 涨跌：最新 close vs 约 1440 根 1m 前的 close（sim 同口径）
            if len(recent) > 1:
                base = float(recent["close"].iloc[0])
                row["change_24h_pct"] = (row["last_price"] / base - 1.0) * 100.0 if base else None
            else:
                row["change_24h_pct"] = None
        else:
            row.update({"last_price": None, "last_ts": None, "change_24h_pct": None})

        fr = (
            store.query_series(funding_table, core, "8h", limit=1, order="DESC")
            if funding_table is not None
            else pd.DataFrame()
        )
        row["funding_rate"] = float(fr["funding_rate"].iloc[0]) if not fr.empty else None
        oi = (
            store.query_series(oi_table, core, "1d", limit=1, order="DESC")
            if oi_table is not None
            else pd.DataFrame()
        )
        row["open_interest"] = float(oi["open_interest"].iloc[0]) if not oi.empty else None
        result[simserve.ui_symbol(core)] = row

    payload: dict[str, Any] = {"symbols": result, "count": len(result)}
    if exchange is not None and market_type is not None:
        payload.update(
            {
                "exchange": exchange.lower(),
                "market_type": market_type.value,
            }
        )
    return payload
