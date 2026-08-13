"""Shared Binance client construction for all Binance DataProviders."""

from superplatform.network.binance import BinanceAdapter
from superplatform.network.rate_limiter import RateLimitConfig, RateLimiter

BINANCE_RATE_LIMITS: list[RateLimitConfig] = [
    RateLimitConfig(max_requests=1200, window_seconds=60, weight=1),
    RateLimitConfig(max_requests=50, window_seconds=1, weight=10),
]


def create_binance_adapter(
    proxy: str = "",
    vision_max_concurrent: int | None = None,
) -> BinanceAdapter:
    """Create one Binance adapter with the shared public API limiter.

    ``vision_max_concurrent`` sets the archive-download semaphore on the
    shared ``BinanceVisionClient`` (default 8). Multi-symbol cold fetches
    funnel through it, so this — not the fetch coordinator — is the effective
    parallelism cap for vision archive downloads.
    """
    return BinanceAdapter(
        rate_limiter=RateLimiter("binance", BINANCE_RATE_LIMITS),
        proxy=proxy,
        vision_max_concurrent=vision_max_concurrent,
    )
