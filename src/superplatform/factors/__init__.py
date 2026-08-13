"""Factor layer — hot-swappable factor computation.

Factors are registered by name and can reference any data provider.
The unified function signature ensures Task 2 can consume factors
without knowing their internals.

Factor categories (per Task 1 requirements):
    momentum_reversal — Momentum, reversal, cross-sectional momentum
    volatility         — Volatility, risk premium, realized/implied vol
    volume_liquidity    — Volume, liquidity, turnover
    microstructure      — Bid-ask spread, order book imbalance, trade flow
    crypto_specific     — Funding rate, open interest, basis
"""

from superplatform.factors.base import Factor, FactorResult, factor
from superplatform.factors.registry import FactorRegistry

__all__ = ["Factor", "FactorResult", "factor", "FactorRegistry"]
