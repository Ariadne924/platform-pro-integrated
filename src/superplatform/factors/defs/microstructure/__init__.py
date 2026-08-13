"""Microstructure proxy factor definitions.

These factors use readily-available kline data as proxies for true
microstructure signals (which would require trade/order-book data).
"""

from superplatform.factors.defs.microstructure.microstructure_factors import (
    close_location,
)

__all__ = [
    "close_location",
]
