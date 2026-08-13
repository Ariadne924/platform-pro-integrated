"""Typed signal schema using pandera.

Replaces raw DataFrame columns with static types:
  sig["position"]  → pandera validates at runtime, mypy checks statically
  sig["positino"]   → mypy: "column does not exist in SignalSchema"
"""

import pandera.pandas as pa
from pandera.typing import Series


class SignalSchema(pa.DataFrameModel):
    """Trading signal: per-timestamp per-symbol position decision."""

    timestamp: pa.Timestamp = pa.Field(coerce=True)  # coerce: tz-aware → tz-naive
    symbol: str
    position: Series[float] = pa.Field(default=0.0)
