"""Unified data schemas for all exchange-agnostic data types.

All timestamps are UTC datetime64[ns]. Spot and perpetual data
carry a `market_type` field and MUST NOT be mixed in analysis.

Each schema is a Pydantic model that validates column names, dtypes,
and null-handling rules after DataFrame construction.
"""

from typing import ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel

from superplatform.data.enums import DataFrequency, MarketType


def _dtype_matches(actual, expected: type) -> bool:
    """Check if a pandas dtype matches an expected numpy type.

    Handles pandas extension types (DatetimeTZDtype, boolean, string)
    that numpy's issubdtype can't process.
    """
    # Datetime: accept any datetime64 or timezone-aware datetime
    if expected is np.datetime64:
        return pd.api.types.is_datetime64_any_dtype(actual)
    # Bool
    if expected is np.bool_:
        return pd.api.types.is_bool_dtype(actual)
    # String
    if expected is np.str_:
        return pd.api.types.is_string_dtype(actual) or pd.api.types.is_object_dtype(actual)
    # Numeric: delegate to numpy
    try:
        return np.issubdtype(actual, expected)
    except TypeError:
        return False


class DataSchema(BaseModel):
    """Base schema for all data types."""

    symbol: str
    market_type: MarketType
    frequency: DataFrequency

    # Column definitions: {column_name: numpy_dtype}
    columns: ClassVar[dict[str, type]] = {}

    @classmethod
    def validate_df(cls, df: pd.DataFrame) -> dict:
        """Check a DataFrame against this schema.

        Returns a dict with keys: 'valid', 'missing_cols', 'extra_cols',
        'dtype_mismatches', 'null_counts', 'duplicate_timestamps'.
        """
        report = {
            "valid": True,
            "missing_cols": [],
            "extra_cols": [],
            "dtype_mismatches": [],
            "null_counts": {},
            "duplicate_timestamps": 0,
        }
        expected = set(cls.columns.keys())
        actual = set(df.columns)
        report["missing_cols"] = sorted(expected - actual)
        report["extra_cols"] = sorted(actual - expected)

        for col, expected_dtype in cls.columns.items():
            if col in df.columns:
                actual_dtype = df[col].dtype
                if not _dtype_matches(actual_dtype, expected_dtype):
                    report["dtype_mismatches"].append(
                        f"{col}: expected {expected_dtype}, got {actual_dtype}"
                    )
                report["null_counts"][col] = int(df[col].isna().sum())

        if "timestamp" in df.columns:
            report["duplicate_timestamps"] = int(df["timestamp"].duplicated().sum())

        report["valid"] = (
            len(report["missing_cols"]) == 0
            and len(report["dtype_mismatches"]) == 0
        )
        return report


class KLineSchema(DataSchema):
    """OHLCV candlestick/kline data."""

    columns: ClassVar[dict[str, type]] = {
        "timestamp": np.datetime64,
        "open": np.float64,
        "high": np.float64,
        "low": np.float64,
        "close": np.float64,
        "volume": np.float64,
        "quote_volume": np.float64,
        "trades": np.float64,  # ccxt unified API never provides trades count
        "taker_buy_volume": np.float64,
        "taker_buy_quote_volume": np.float64,
    }


class TradeSchema(DataSchema):
    """Individual trade / aggTrade data."""

    columns: ClassVar[dict[str, type]] = {
        "timestamp": np.datetime64,
        "price": np.float64,
        "quantity": np.float64,
        "is_buyer_maker": np.bool_,
        "trade_id": np.int64,
    }


class OrderBookSchema(DataSchema):
    """Order book depth snapshot (bids + asks as separate DataFrames per side)."""

    columns: ClassVar[dict[str, type]] = {
        "timestamp": np.datetime64,
        "side": np.str_,
        "price": np.float64,
        "quantity": np.float64,
    }


class FundingRateSchema(DataSchema):
    """Historical funding rates (perpetual only)."""

    columns: ClassVar[dict[str, type]] = {
        "timestamp": np.datetime64,
        "funding_rate": np.float64,
    }


class OpenInterestSchema(DataSchema):
    """Open interest time series."""

    columns: ClassVar[dict[str, type]] = {
        "timestamp": np.datetime64,
        "open_interest": np.float64,
    }


class BasisSchema(DataSchema):
    """Spot-perpetual basis."""

    columns: ClassVar[dict[str, type]] = {
        "timestamp": np.datetime64,
        "spot_price": np.float64,
        "perpetual_price": np.float64,
        "basis_pct": np.float64,
    }
