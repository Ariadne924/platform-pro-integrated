"""Asyncio tick scheduler — the heartbeat of the live trading system.

A Scheduler runs a loop at a configurable interval. Each tick, it calls
every registered hook in registration order. Each hook is an async
callable that receives a HookContext with the latest market data.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from superplatform.utils.logging import logger

# ── Hook types ──────────────────────────────────────────────────────

@dataclass
class HookContext:
    """Immutable data passed to each engine hook per tick."""

    tick_no: int
    tick_time: float          # unix timestamp
    prices: dict[str, float]  # latest prices per symbol

    # Additional data engines can attach per-tick info
    extra: dict = field(default_factory=dict)


EngineHook = Callable[[HookContext], Awaitable[None]]


# ── Scheduler ───────────────────────────────────────────────────────

class Scheduler:
    """Asyncio tick scheduler with hook-based plugin architecture.

    Hooks are called in registration order each tick. If a hook raises,
    the exception is caught and logged — it never kills the main loop.

    Usage:
        scheduler = Scheduler(interval=10.0)

        async def factor_tick(ctx): ...
        scheduler.register_hook(factor_tick)

        async def trading_tick(ctx): ...
        scheduler.register_hook(trading_tick)

        await scheduler.run()  # blocks until cancelled
    """

    def __init__(self, interval: float = 10.0):
        self.interval = max(1.0, interval)
        self._hooks: list[EngineHook] = []
        self._tick_no: int = 0
        self._running: bool = False
        self._prices: dict[str, float] = {}
        self._data_stale: bool = False
        self._stale_symbols: list[str] = []
        self._last_tick_duration: float = 0.0

    # ── Hook registration ───────────────────────────────────────────

    def register_hook(self, hook: EngineHook) -> None:
        """Register an async callback. Called in order per tick."""
        self._hooks.append(hook)

    # ── Tick state (for API consumers) ─────────────────────────────

    def snapshot(self) -> dict:
        """Return a read-only view of current scheduler state."""
        return {
            "tick_no": self._tick_no,
            "running": self._running,
            "prices": dict(self._prices),
            "data_stale": self._data_stale,
            "stale_symbols": list(self._stale_symbols),
            "last_tick_duration": self._last_tick_duration,
        }

    # ── Main loop ───────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the tick loop. Blocks until cancelled."""
        self._running = True
        logger.info(
            "Scheduler starting: interval={:.1f}s, hooks={}",
            self.interval, len(self._hooks),
        )

        while self._running:
            t0 = time.monotonic()
            self._tick_no += 1

            try:
                await self._run_one_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler tick {} crashed — skipping", self._tick_no)

            elapsed = time.monotonic() - t0
            self._last_tick_duration = elapsed

            # Compensated sleep — don't drift
            sleep_time = self.interval - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                logger.warning(
                    "Tick {} overran by {:.2f}s (took {:.2f}s, interval {:.2f}s)",
                    self._tick_no, -sleep_time, elapsed, self.interval,
                )

        logger.info("Scheduler stopped after {} ticks", self._tick_no)

    async def stop(self) -> None:
        """Signal the scheduler to stop after the current tick."""
        self._running = False

    async def _run_one_tick(self) -> None:
        """Execute one complete tick: update prices → run all hooks."""
        tick_time = time.time()
        ctx = HookContext(
            tick_no=self._tick_no,
            tick_time=tick_time,
            prices=dict(self._prices),
        )

        error_count = 0
        for i, hook in enumerate(self._hooks):
            try:
                await hook(ctx)
            except Exception:
                error_count += 1
                logger.exception(
                    "Hook {}/{} crashed in tick {}",
                    i + 1, len(self._hooks), self._tick_no,
                )

        # Summary log line
        price_str = ", ".join(
            f"{s}={p:.2f}" for s, p in sorted(self._prices.items())[:5]
        )
        stale_tag = " [STALE]" if self._data_stale else ""
        err_tag = f" errors={error_count}" if error_count else ""
        logger.info(
            "[tick {}] {} | {:.2f}s{}{}",
            self._tick_no, price_str, self._last_tick_duration,
            stale_tag, err_tag,
        )

    # ── Price management ────────────────────────────────────────────

    def update_prices(self, prices: dict[str, float]) -> None:
        """Merge latest prices. Called by the data-fetch hook."""
        self._prices.update(prices)

    def set_stale(self, symbols: list[str]) -> None:
        """Mark data as stale for specific symbols."""
        if symbols:
            self._data_stale = True
            self._stale_symbols = symbols
        else:
            self._data_stale = False
            self._stale_symbols = []
