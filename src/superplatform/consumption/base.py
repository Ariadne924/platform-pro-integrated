"""Consumer base class — the downstream endpoint that consumes StrategySignals.

A Consumer is anything that takes position signals and does something with them:
  - Backtest (vectorized P&L)
  - Paper trading (simulated fills)
  - Live execution (real exchange orders)

Each Consumer declares its identity so the Runtime can verify that the data
pipeline's upstream sources are consistent with the Consumer's target exchange.
"""

from dataclasses import dataclass
from enum import StrEnum


class Strictness(StrEnum):
    """How strictly to enforce data-source / execution-target consistency."""

    STRICT = "strict"  # Mismatch → raise error, refuse to run
    WARN = "warn"      # Mismatch → log warning, continue
    SILENT = "silent"  # Don't check at all


@dataclass
class ConsumerConfig:
    """Identity and policy for a signal consumer.

    Attributes:
        consumer_id: Unique identifier, e.g. 'backtest', 'binance-perp-broker'.
            The segment before the first '-' is conventionally the exchange name.
        target_exchange: Which exchange orders are sent to (or 'backtest' for
            paper/offline consumers that don't execute on any real exchange).
        strictness: How to handle provider-consumer exchange mismatches.
    """

    consumer_id: str
    target_exchange: str
    strictness: Strictness = Strictness.WARN

    @classmethod
    def backtest(cls) -> "ConsumerConfig":
        """Default config for offline backtesting — no exchange involved."""
        return cls(
            consumer_id="backtest",
            target_exchange="backtest",
            strictness=Strictness.SILENT,
        )

    def __hash__(self) -> int:
        return hash(self.consumer_id)
