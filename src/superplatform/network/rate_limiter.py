"""Rate limiter for exchange API calls.

Supports token-bucket and sliding-window strategies.
Each exchange adapter gets its own limiter instance configured
to the exchange's documented limits.
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimitConfig:
    """Configuration for a rate limit rule.

    Attributes:
        max_requests: Maximum number of requests allowed in the window.
        window_seconds: Duration of the rolling window in seconds.
        weight: Weight of each request (some exchanges assign different weights).
    """
    max_requests: int
    window_seconds: float
    weight: int = 1


@dataclass
class RateLimiter:
    """Token-bucket + sliding-window rate limiter.

    Usage:
        limiter = RateLimiter("binance", [
            RateLimitConfig(max_requests=1200, window_seconds=60, weight=1),
            RateLimitConfig(max_requests=50, window_seconds=1, weight=10),
        ])
        async with limiter:
            await exchange.fetch_klines(...)
    """

    name: str
    limits: list[RateLimitConfig] = field(default_factory=list)
    _windows: dict[int, deque] = field(default_factory=dict, init=False)

    def __post_init__(self):
        self._windows = {i: deque() for i in range(len(self.limits))}

    def _prune(self, idx: int, now: float) -> None:
        cfg = self.limits[idx]
        window = self._windows[idx]
        cutoff = now - cfg.window_seconds
        while window and window[0] < cutoff:
            window.popleft()

    def _count(self, idx: int) -> int:
        return sum(self.limits[idx].weight for _ in self._windows[idx])

    async def acquire(self) -> None:
        """Wait until all rate limits allow a request."""
        while True:
            now = time.monotonic()
            blocked = False
            for idx, cfg in enumerate(self.limits):
                self._prune(idx, now)
                if self._count(idx) + cfg.weight > cfg.max_requests:
                    blocked = True
            if not blocked:
                break
            await asyncio.sleep(0.05)

        now = time.monotonic()
        for idx in range(len(self.limits)):
            self._windows[idx].append(now)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        pass  # tokens already consumed in acquire()
