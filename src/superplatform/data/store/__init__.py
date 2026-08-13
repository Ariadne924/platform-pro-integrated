"""DuckDB-backed persistence store.

Thread-safe single-connection wrapper for storing and querying
time-series data, orders, trades, and equity snapshots.

Usage:
    store = Store("data/cache.duckdb")
    store.ensure_provider_table("binance-perp-kline", "kline")
    store.upsert("pv_binance_perp_kline", df)   # INSERT OR REPLACE
    df = store.query("pv_binance_perp_kline", symbol="BTCUSDT", start=..., end=...)
"""

import threading
from pathlib import Path

import duckdb
import pandas as pd

# ── DDL ─────────────────────────────────────────────────────────────

# Provider cache tables are created lazily per provider (one table per
# provider_id), each carrying the data_type's range columns plus the shared
# (symbol, frequency, timestamp) key columns. See ``ensure_provider_table``.
_DATA_TYPE_DDL: dict[str, str] = {
    "kline": (
        "    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,\n"
        "    volume DOUBLE, quote_volume DOUBLE,\n"
        "    trades BIGINT, taker_buy_volume DOUBLE, taker_buy_quote_volume DOUBLE,\n"
    ),
    "funding_rate": "    funding_rate DOUBLE,\n",
    "open_interest": "    open_interest DOUBLE,\n",
    "basis": "    spot_price DOUBLE, perpetual_price DOUBLE, basis_pct DOUBLE,\n",
}

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS provider_tables (
    provider_id VARCHAR PRIMARY KEY,
    data_type VARCHAR,
    table_name VARCHAR
);

CREATE TABLE IF NOT EXISTS factor_values (
    factor_name VARCHAR,
    symbol VARCHAR,
    frequency VARCHAR,
    timestamp TIMESTAMPTZ,
    value DOUBLE,
    PRIMARY KEY (factor_name, symbol, frequency, timestamp)
);

CREATE TABLE IF NOT EXISTS empty_ranges (
    data_type VARCHAR,
    symbol VARCHAR,
    frequency VARCHAR,
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    PRIMARY KEY (data_type, symbol, frequency, start_ts, end_ts)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id VARCHAR PRIMARY KEY,
    symbol VARCHAR,
    side VARCHAR,
    qty DOUBLE,
    filled_qty DOUBLE DEFAULT 0,
    limit_price DOUBLE,
    status VARCHAR DEFAULT 'open',
    reject_reason VARCHAR DEFAULT '',
    source VARCHAR DEFAULT 'auto',
    created_ts DOUBLE,
    updated_ts DOUBLE
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id VARCHAR PRIMARY KEY,
    order_id VARCHAR,
    symbol VARCHAR,
    side VARCHAR,
    qty DOUBLE,
    price DOUBLE,
    fee DOUBLE DEFAULT 0,
    liquidated BOOLEAN DEFAULT FALSE,
    ts DOUBLE
);

CREATE TABLE IF NOT EXISTS equity (
    ts DOUBLE PRIMARY KEY,
    equity DOUBLE,
    wallet_balance DOUBLE,
    margin_used DOUBLE,
    unrealized_pnl DOUBLE
);

CREATE TABLE IF NOT EXISTS universe (
    exchange VARCHAR,
    symbol VARCHAR,
    contract_type VARCHAR,
    status VARCHAR,
    base_asset VARCHAR,
    quote_asset VARCHAR,
    listed_at TIMESTAMPTZ,
    delisted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    PRIMARY KEY (exchange, symbol)
);
"""

# Migration: add frequency column to tables created before the column
# existed.  ALTER … ADD COLUMN IF NOT EXISTS is a no-op on new tables
# (the CREATE TABLE already has the column) and adds the column with a
# default on tables created by earlier versions.
# Note: provider cache tables are created fresh (one per provider); the
# legacy shared tables (klines, funding_rate, ...) are no longer migrated —
# old caches' shared tables are simply orphaned.
_MIGRATION_DDL = """
ALTER TABLE factor_values ADD COLUMN IF NOT EXISTS frequency VARCHAR DEFAULT '1d';
"""

# Tables that support upsert via INSERT OR REPLACE.
# Provider cache tables (``pv_*``) are additionally accepted by ``upsert``.
_UPSERT_TABLES = {"factor_values", "orders", "trades", "equity", "universe"}

# Non-provider tables with a (symbol, frequency, timestamp) key, accepted by
# the range-query methods alongside ``pv_*`` tables.
_RANGE_TABLES = {"factor_values"}

_PROVIDER_TABLE_PREFIX = "pv_"


def provider_table(provider_id: str) -> str:
    """Map a provider id to its DuckDB cache table name.

    One table per provider: ``binance-perp-kline`` → ``pv_binance_perp_kline``.
    Keeping the table per provider (rather than per data_type) is what keeps
    bars from different sources strictly separate in the cache.
    """
    return _PROVIDER_TABLE_PREFIX + provider_id.replace("-", "_")

_UNIVERSE_COLUMNS = {
    "exchange", "symbol", "contract_type", "status",
    "base_asset", "quote_asset", "listed_at", "delisted_at", "updated_at",
}


class Store:
    """Thread-safe DuckDB persistence wrapper.

    A single DuckDB connection protected by a re-entrant lock.
    All write operations use INSERT OR REPLACE for idempotency.
    """

    def __init__(self, path: str | Path = "data/live.duckdb"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = duckdb.connect(str(self._path))
        self._conn.execute("SET TimeZone = 'UTC'")
        self._init_schema()

    def _init_schema(self) -> None:
        """Execute DDL — idempotent (IF NOT EXISTS) + migration."""
        with self._lock:
            for stmt in _SCHEMA_DDL.split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    self._conn.execute(stmt)
            for stmt in _MIGRATION_DDL.split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    self._conn.execute(stmt)

    def ensure_provider_table(self, provider_id: str, data_type: str) -> str:
        """Lazily create the per-provider cache table; return its name.

        Idempotent: creates the ``pv_*`` table (if missing) with the
        data_type's range columns and records the mapping in
        ``provider_tables`` so readers (snapshot, validation-report) can
        enumerate provider tables without a live registry.
        """
        ddl = _DATA_TYPE_DDL.get(data_type)
        if ddl is None:
            raise ValueError(
                f"no cache schema for data type {data_type!r}; "
                f"supported: {sorted(_DATA_TYPE_DDL)}"
            )
        table = provider_table(provider_id)
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (\n"
                "    symbol VARCHAR,\n"
                "    frequency VARCHAR,\n"
                "    timestamp TIMESTAMPTZ,\n"
                f"{ddl}"
                "    PRIMARY KEY (symbol, frequency, timestamp)\n"
                ")"
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO provider_tables VALUES (?, ?, ?)",
                [provider_id, data_type, table],
            )
        return table

    def _table_exists(self, table: str) -> bool:
        """True when the table exists in the connected database."""
        with self._lock:
            df = self._conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = ? AND table_schema = 'main'",
                [table],
            ).fetchdf()
            return not df.empty

    # ── Write ───────────────────────────────────────────────────────

    def upsert(self, table: str, df: pd.DataFrame) -> int:
        """Insert or replace rows into a table.

        Args:
            table: Table name (must be in _UPSERT_TABLES).
            df: DataFrame whose columns match the table.

        Returns:
            Number of rows written.
        """
        if table not in _UPSERT_TABLES and not table.startswith(_PROVIDER_TABLE_PREFIX):
            raise ValueError(
                f"Table '{table}' not in upsert whitelist: "
                f"{sorted(_UPSERT_TABLES)} or a pv_* provider table"
            )

        if df.empty:
            return 0

        # DuckDB pandas replacement scan — BY NAME matches columns automatically
        with self._lock:
            self._conn.register("_tmp_df", df)
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table} BY NAME SELECT * FROM _tmp_df"
            )
            self._conn.unregister("_tmp_df")
        return len(df)

    def upsert_orders(self, orders) -> int:
        """Persist a list of Order dataclasses."""
        if not orders:
            return 0
        rows = [{
            "order_id": o.order_id, "symbol": o.symbol, "side": o.side,
            "qty": o.qty, "filled_qty": o.filled_qty,
            "limit_price": o.limit_price, "status": o.status,
            "reject_reason": o.reject_reason, "source": o.source,
            "created_ts": o.created_ts, "updated_ts": o.updated_ts,
        } for o in orders]
        return self.upsert("orders", pd.DataFrame(rows))

    def upsert_trades(self, trades) -> int:
        """Persist a list of Trade dataclasses."""
        if not trades:
            return 0
        rows = [{
            "trade_id": t.trade_id, "order_id": t.order_id,
            "symbol": t.symbol, "side": t.side,
            "qty": t.qty, "price": t.price, "fee": t.fee,
            "liquidated": t.liquidated, "ts": t.ts,
        } for t in trades]
        return self.upsert("trades", pd.DataFrame(rows))

    def upsert_equity(self, point) -> int:
        """Persist a single EquityPoint."""
        df = pd.DataFrame([{
            "ts": point.ts, "equity": point.equity,
            "wallet_balance": point.wallet_balance,
            "margin_used": point.margin_used,
            "unrealized_pnl": point.unrealized_pnl,
        }])
        return self.upsert("equity", df)

    def upsert_universe(self, df: pd.DataFrame) -> int:
        """Insert or replace universe rows keyed by (exchange, symbol)."""
        missing = _UNIVERSE_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(f"universe df missing columns: {sorted(missing)}")
        return self.upsert("universe", df)

    def query_universe(self) -> pd.DataFrame:
        """Return every universe row, ordered by symbol."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM universe ORDER BY symbol"
            ).fetchdf()

    # ── Read ────────────────────────────────────────────────────────

    def query(
        self,
        table: str,
        symbol: str | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int = 1000,
        order: str = "ASC",
    ) -> pd.DataFrame:
        """Query rows from a table with optional filters.

        Args:
            table: Table name.
            symbol: Filter by symbol column (if table has one).
            start: Start of timestamp range (unix seconds, inclusive).
            end: End of timestamp range (unix seconds, exclusive).
            limit: Max rows to return.
            order: Sort order for timestamp (ASC or DESC).

        Returns:
            DataFrame with query results.
        """
        where = []
        params: list = []

        if symbol and "symbol" in self._columns(table):
            where.append("symbol = ?")
            params.append(symbol)

        if start is not None:
            ts_col = self._ts_column(table)
            where.append(f"{ts_col} >= ?")
            params.append(pd.Timestamp.fromtimestamp(start, tz="UTC"))

        if end is not None:
            ts_col = self._ts_column(table)
            where.append(f"{ts_col} < ?")
            params.append(pd.Timestamp.fromtimestamp(end, tz="UTC"))

        clause = ""
        if where:
            clause = "WHERE " + " AND ".join(where)

        direction = "DESC" if order.upper() == "DESC" else "ASC"
        ts_col = self._ts_column(table)
        sql = f"SELECT * FROM {table} {clause} ORDER BY {ts_col} {direction} LIMIT {limit}"

        with self._lock:
            return self._conn.execute(sql, params).fetchdf()

    def series_range(
        self, table: str, symbol: str, frequency: str
    ) -> dict:
        """Return the min/max timestamp and row count for cached series.

        Works for any time-series table in ``_RANGE_TABLES``.

        Returns dict with keys: min_ts (pd.Timestamp or None),
        max_ts (pd.Timestamp or None), count (int),
        bar_width (pd.Timedelta or None) — median interval between bars.
        """
        if table not in _RANGE_TABLES and not table.startswith(_PROVIDER_TABLE_PREFIX):
            raise ValueError(
                f"'{table}' is not a range-queryable table: "
                f"{sorted(_RANGE_TABLES)} or a pv_* provider table"
            )
        if not self._table_exists(table):
            return {"min_ts": None, "max_ts": None, "count": 0, "bar_width": None}
        with self._lock:
            df = self._conn.execute(
                f"SELECT MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts, "
                f"COUNT(*) AS cnt "
                f"FROM {table} WHERE symbol = ? AND frequency = ?",
                [symbol, frequency],
            ).fetchdf()
        if df.empty or df["cnt"].iloc[0] == 0:
            return {"min_ts": None, "max_ts": None, "count": 0, "bar_width": None}
        result = {
            "min_ts": df["min_ts"].iloc[0],
            "max_ts": df["max_ts"].iloc[0],
            "count": int(df["cnt"].iloc[0]),
        }
        # Estimate bar width from the cached data
        if result["count"] >= 2:
            bar_width = (result["max_ts"] - result["min_ts"]) / (result["count"] - 1)
            result["bar_width"] = bar_width
        else:
            result["bar_width"] = None
        return result

    def query_series(
        self,
        table: str,
        symbol: str,
        frequency: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        limit: int = 100000,
        order: str = "ASC",
    ) -> pd.DataFrame:
        """Query a time-series table by symbol + frequency with a time range."""
        if table not in _RANGE_TABLES and not table.startswith(_PROVIDER_TABLE_PREFIX):
            raise ValueError(
                f"'{table}' is not a range-queryable table: "
                f"{sorted(_RANGE_TABLES)} or a pv_* provider table"
            )
        if not self._table_exists(table):
            return pd.DataFrame()
        where = ["symbol = ?", "frequency = ?"]
        params: list = [symbol, frequency]

        if start is not None:
            where.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            where.append("timestamp < ?")
            params.append(end)

        direction = "DESC" if order.upper() == "DESC" else "ASC"
        clause = " AND ".join(where)
        sql = (
            f"SELECT * FROM {table} WHERE {clause} "
            f"ORDER BY timestamp {direction} LIMIT {limit}"
        )
        with self._lock:
            return self._conn.execute(sql, params).fetchdf()

    def count_series_range(
        self,
        table: str,
        symbol: str,
        frequency: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> int:
        """Count one series' cached rows within [start, end).

        Backfill's dense chunk-coverage check uses this: min/max span alone
        cannot see holes inside the cached range.
        """
        if table not in _RANGE_TABLES and not table.startswith(_PROVIDER_TABLE_PREFIX):
            raise ValueError(
                f"'{table}' is not a range-queryable table: "
                f"{sorted(_RANGE_TABLES)} or a pv_* provider table"
            )
        if not self._table_exists(table):
            return 0
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE symbol = ? AND frequency = ? "
                "AND timestamp >= ? AND timestamp < ?",
                [symbol, frequency, start, end],
            ).fetchone()
        return int(row[0])

    def empty_ranges_between(
        self,
        data_type: str,
        symbol: str,
        frequency: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Return recorded verified-empty ranges overlapping [start, end)."""
        with self._lock:
            df = self._conn.execute(
                "SELECT start_ts, end_ts FROM empty_ranges "
                "WHERE data_type = ? AND symbol = ? AND frequency = ? "
                "AND start_ts < ? AND end_ts > ?",
                [data_type, symbol, frequency, end, start],
            ).fetchdf()
        return [
            (pd.Timestamp(r.start_ts), pd.Timestamp(r.end_ts))
            for r in df.itertuples()
        ]

    def record_empty_range(
        self,
        data_type: str,
        symbol: str,
        frequency: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> None:
        """Persist a verified-empty time range for a series.

        Lets the cache skip re-fetching ranges that the upstream source has
        confirmed contain no data (e.g. archives that never existed).
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO empty_ranges VALUES (?,?,?,?,?)",
                [data_type, symbol, frequency, start, end],
            )

    def covers_empty_range(
        self,
        data_type: str,
        symbol: str,
        frequency: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> bool:
        """Return True when a recorded empty range fully covers [start, end]."""
        with self._lock:
            df = self._conn.execute(
                "SELECT start_ts, end_ts FROM empty_ranges "
                "WHERE data_type = ? AND symbol = ? AND frequency = ?",
                [data_type, symbol, frequency],
            ).fetchdf()
        for _, row in df.iterrows():
            if row["start_ts"] <= start and row["end_ts"] >= end:
                return True
        return False

    def latest_factor_values(
        self, factor_name: str, n: int = 100
    ) -> pd.DataFrame:
        """Get the most recent factor values."""
        with self._lock:
            sql = (
                "SELECT * FROM factor_values "
                "WHERE factor_name = ? "
                "ORDER BY timestamp DESC LIMIT ?"
            )
            return self._conn.execute(sql, [factor_name, n]).fetchdf()

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _columns(table: str) -> set[str]:
        """Return column names for a known table."""
        if table.startswith(_PROVIDER_TABLE_PREFIX):
            # Provider cache tables always carry symbol + frequency + timestamp.
            return {"symbol", "frequency", "timestamp"}
        cols = {
            "factor_values": {"factor_name", "symbol", "frequency", "timestamp", "value"},
            "orders": {"order_id", "symbol", "side", "qty", "filled_qty",
                       "limit_price", "status", "reject_reason", "source",
                       "created_ts", "updated_ts"},
            "trades": {"trade_id", "order_id", "symbol", "side", "qty",
                       "price", "fee", "liquidated", "ts"},
            "equity": {"ts", "equity", "wallet_balance", "margin_used",
                       "unrealized_pnl"},
            "universe": _UNIVERSE_COLUMNS,
        }
        return cols.get(table, set())

    @staticmethod
    def _ts_column(table: str) -> str:
        """Return the timestamp column name for a table."""
        ts_map = {"klines": "timestamp", "factor_values": "timestamp",
                  "orders": "created_ts", "trades": "ts", "equity": "ts",
                  "universe": "updated_at"}
        return ts_map.get(table, "timestamp")

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        """Close the DuckDB connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
