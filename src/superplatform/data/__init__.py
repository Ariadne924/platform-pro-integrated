"""Data layer — unified data schemas and hot-swappable providers.

This layer abstracts away the data source: whether from Binance, OKX,
local cache, or a hybrid source, the consumer sees the same interface.

Provider naming convention: {source}-{data_type}
Examples: binance-kline, okx-funding-rate, local-cache-kline
"""

from superplatform.data.provider_registry import DataProvider, DataProviderRegistry
from superplatform.data.schema import (
    BasisSchema,
    DataSchema,
    FundingRateSchema,
    KLineSchema,
    OpenInterestSchema,
    OrderBookSchema,
    TradeSchema,
)

__all__ = [
    "DataSchema",
    "KLineSchema",
    "TradeSchema",
    "OrderBookSchema",
    "FundingRateSchema",
    "OpenInterestSchema",
    "BasisSchema",
    "DataProvider",
    "DataProviderRegistry",
]
