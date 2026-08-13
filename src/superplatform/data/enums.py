"""Shared enumerations used across Network, Data, and upper layers.

Defined in a leaf module with zero dependencies so any layer can import
them without creating import cycles or layering violations.
"""

from enum import StrEnum


class MarketType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"  # USDⓈ-M perpetual futures
    COIN_FUTURES = "coin_futures"  # COIN-M futures


class DataFrequency(StrEnum):
    TICK = "tick"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    H8 = "8h"
    D1 = "1d"
    W1 = "1w"
