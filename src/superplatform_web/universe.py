"""Universe sync orchestration: fetch + reconcile + persist the symbol pool.

The webapp needs a full-market universe (every tradable USDⓈ-M perpetual) so
cross-sectional factor analysis can run on a meaningful number of symbols,
not just the tiny ``data.symbols.perpetual`` config pool. ``sync_universe``
fetches the exchange's ``exchangeInfo`` snapshot, reconciles it against the
stored ``universe`` table (delisting is primarily absence-based, secondarily
status-based), and persists. ``sync_universe_if_stale`` is the background-safe
entrypoint used by the app lifespan — it never raises.
"""

import logging
import time
from datetime import timedelta

import pandas as pd

import superplatform_web.state as _state

LOGGER = logging.getLogger(__name__)

UNIVERSE_EXCHANGE = "binance"
UNIVERSE_PROVIDER_ID = "binance-perp-kline"
UNIVERSE_STALE_AFTER = timedelta(hours=24)
ACTIVE_STATUSES = {"TRADING", "PENDING_TRADING"}

# 24h-ticker TTL. /fapi/v1/ticker/24hr (aggregate) is weight 40, so group
# resolution must reuse one cached fetch across the whole page render.
_TICKER_TTL_SECONDS = 60.0
_TICKER_CACHE: dict = {"ts": 0.0, "data": {}}


def _get_adapter():
    """Shared binance adapter from the registry; fall back to a fresh one.

    Construction does not hit the network, so the fallback is safe when the
    registry is empty (e.g. Binance providers failed to register). The adapter
    resolves through CachingProvider's ``__getattr__`` proxy.
    """
    reg = _state.providers
    if UNIVERSE_PROVIDER_ID in reg:
        return reg.get(UNIVERSE_PROVIDER_ID).adapter
    from superplatform.data.providers.binance_common import create_binance_adapter

    return create_binance_adapter(_state._first_exchange_proxy())


def prime_vision_from_universe(store=None) -> int:
    """Seed each Binance vision client's earliest-date cache from ``listed_at``.

    ``listed_at`` is the earliest date a symbol's archives can start, so a
    primed client verifies its earliest-archive date with a single HEAD instead
    of the ~17-probe binary search on the next archive fetch (open interest,
    and long kline ranges).  Returns the number of (symbol × client) primings
    applied; 0 when there is no store, no universe, or no Binance provider.
    """
    store = store or _state.store
    if store is None:
        return 0
    df = store.query_universe()
    if df.empty or "listed_at" not in df.columns:
        return 0
    primed = 0
    for pid in _state.providers.list_all():
        provider = _state.providers.get(pid)
        adapter = getattr(provider, "adapter", None)
        vision = getattr(adapter, "_vision", None)
        if vision is None:
            continue
        for _, row in df.iterrows():
            listed = row.get("listed_at")
            if pd.notna(listed):
                vision.prime_earliest(row["symbol"], pd.Timestamp(listed))
                primed += 1
    return primed


def _normalise_ts(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Coerce a column to UTC-aware ns timestamps (mirrors adapter pattern)."""
    if col in df.columns:
        df[col] = pd.DatetimeIndex(pd.to_datetime(df[col], utc=True)).as_unit("ns")
    return df


async def sync_universe(adapter=None, store=None) -> dict:
    """Fetch the universe, reconcile against stored rows, persist.

    Returns dict with counts: ``added`` (new symbols), ``updated`` (changed
    metadata), ``delisted`` (absence- or status-based), ``total``.

    Reconciliation notes:
    - Delisting is primarily absence-based (symbol gone from ``exchangeInfo``),
      secondarily status-based (present but ``status`` no longer active).
    - An active symbol always gets ``delisted_at = None`` — a previously
      delisted symbol that trades again is counted as ``added`` (re-listed).
    - A non-active symbol keeps its FIRST delisting timestamp (carried forward
      across syncs) so repeated refreshes do not keep moving ``delisted_at``.
    """
    from superplatform.network.base import MarketType

    store = store or _state.store
    if store is None:
        return {"synced": False, "error": "store disabled (data.cache.enabled=false)"}
    adapter = adapter or _get_adapter()

    snapshot = await adapter.fetch_universe(MarketType.PERPETUAL)
    now = pd.Timestamp.now(tz="UTC")
    existing = store.query_universe()
    existing_map = (
        {r["symbol"]: r for _, r in existing.iterrows()} if not existing.empty else {}
    )
    snap_map = {r["symbol"]: r for _, r in snapshot.iterrows()}

    rows: list[dict] = []
    added = updated = delisted = 0
    for sym, row in snap_map.items():
        prev = existing_map.get(sym)
        was_delisted = prev is not None and pd.notna(prev.get("delisted_at"))
        if row["status"] in ACTIVE_STATUSES:
            # Active → any prior delisting is cleared (a re-listed symbol is a
            # fresh active listing). Never carry delisted_at forward here —
            # INSERT OR REPLACE BY NAME would otherwise keep the old stamp.
            delisted_at = None
            if prev is None:
                added += 1
            elif was_delisted:
                added += 1  # back from delisting
            elif (
                prev.get("status") != row["status"]
                or prev.get("base_asset") != row["base_asset"]
                or prev.get("quote_asset") != row["quote_asset"]
            ):
                updated += 1
        else:
            # Non-active status → delisted. Keep the FIRST delisting timestamp
            # (carry it forward) so repeated syncs do not keep moving it.
            if was_delisted:
                delisted_at = prev["delisted_at"]
            else:
                delisted_at = now  # status-based delisting
                delisted += 1
        rows.append({**row, "delisted_at": delisted_at, "updated_at": now})

    # absence-based delisting: stored symbol no longer in the exchange snapshot
    for sym, prev in existing_map.items():
        if sym in snap_map or pd.notna(prev.get("delisted_at")):
            continue
        rows.append({
            "exchange": UNIVERSE_EXCHANGE,
            "symbol": sym,
            "contract_type": prev.get("contract_type"),
            "status": prev.get("status"),
            "base_asset": prev.get("base_asset"),
            "quote_asset": prev.get("quote_asset"),
            "listed_at": prev.get("listed_at"),
            "delisted_at": now,
            "updated_at": now,
        })
        delisted += 1

    df = pd.DataFrame(rows)
    if not df.empty:
        df = _normalise_ts(df, "listed_at")
        df = _normalise_ts(df, "delisted_at")
        df = _normalise_ts(df, "updated_at")
        store.upsert_universe(df)
    # Fresh listed_at values prime the vision clients so archive fetches skip
    # most of their earliest-date HEAD probes.
    prime_vision_from_universe(store)
    return {
        "synced": True,
        "added": added,
        "updated": updated,
        "delisted": delisted,
        "total": len(df),
    }


def _is_stale(store) -> bool:
    """True when the stored universe has not been refreshed within the window."""
    df = store.query_universe()
    if df.empty:
        return True
    latest = pd.to_datetime(df["updated_at"], utc=True).max()
    return latest < (pd.Timestamp.now(tz="UTC") - UNIVERSE_STALE_AFTER)


async def sync_universe_if_stale(*, force: bool = False) -> None:
    """Background-safe entrypoint: never raises, skips when store is absent."""
    try:
        if _state.store is None:
            return
        if not force and not _is_stale(_state.store):
            return
        await sync_universe()
    except Exception:
        LOGGER.exception("background universe sync failed")


def _iso(value) -> str | None:
    """ISO-format a timestamp, or None for NaT/None."""
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).isoformat()


# ── 24h ticker (shared by symbol-group resolution + live_start validation) ──


async def fetch_tickers() -> dict[str, float]:
    """Symbol → 24h quote volume, cached for _TICKER_TTL_SECONDS.

    Shared by the symbols route (group resolution / Top-N) and live_start
    (unknown-symbol validation). Network failures resolve to {} and are cached,
    so a transient outage degrades to "no volume data" rather than an error.
    """
    now = time.monotonic()
    if now - _TICKER_CACHE["ts"] < _TICKER_TTL_SECONDS:
        return _TICKER_CACHE["data"]
    from superplatform.network.base import MarketType

    try:
        data = await _get_adapter().fetch_tickers(MarketType.PERPETUAL)
    except Exception:
        LOGGER.exception("fetch_tickers failed; resolving groups without volume")
        data = {}
    _TICKER_CACHE.update(ts=now, data=data)
    return data


def stored_active() -> set[str]:
    """Currently-active symbols from the stored universe (delisted excluded).

    Returns an empty set when the cache store is disabled (offline tests) so
    callers can distinguish "no universe data" from "nothing active" — the
    live_start unknown-symbol guard skips validation when this is empty.
    """
    if _state.store is None:
        return set()
    df = _state.store.query_universe()
    if df.empty:
        return set()
    active = df[df["status"].isin(ACTIVE_STATUSES) & df["delisted_at"].isna()]
    return set(active["symbol"].tolist())
