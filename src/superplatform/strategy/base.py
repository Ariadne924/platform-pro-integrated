"""Strategy base class and signal container.

A strategy takes per-symbol factor results and produces per-symbol signals.
Strategy code iterates over symbols to build position decisions.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from pandera.typing import DataFrame

from superplatform.factors.base import FactorResult
from superplatform.strategy.signal_schema import SignalSchema


@dataclass
class StrategySignal:
    """Output of strategy.generate_signals()."""

    name: str
    positions: DataFrame[SignalSchema]
    metadata: dict = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base class for a trading strategy."""

    name: str
    description: str = ""
    used_factors: list[str] = field(default_factory=list)

    @abstractmethod
    def generate_signals(
        self,
        factor_results: dict[str, dict[str, FactorResult]],
        #               factor_name → {symbol: FactorResult}
        **params: Any,
    ) -> StrategySignal:
        ...


def strategy(
    name: str,
    description: str = "",
    used_factors: list[str] | None = None,
) -> "Callable[[Callable], type[Strategy]]":
    """Decorator to create a Strategy from a function."""

    def wrapper(fn) -> type[Strategy]:
        cls_name = f"Strategy_{name}"

        def generate_signals(self, factor_results, **params):
            raw = fn(factor_results, **params)
            if isinstance(raw, pd.DataFrame):
                from pandera.typing import DataFrame as PaDataFrame
                raw = PaDataFrame[SignalSchema](raw)
            if isinstance(raw, DataFrame):
                # Already typed
                pass
            else:
                return raw

            return StrategySignal(
                name=name,
                positions=raw,
                metadata={"params": params, "used_factors": used_factors or []},
            )

        new_cls = type(
            cls_name,
            (Strategy,),
            {
                "generate_signals": generate_signals,
                "name": name,
                "description": description,
                "used_factors": used_factors or [],
            },
        )
        from superplatform.strategy.registry import StrategyRegistry
        StrategyRegistry.get_instance().register(new_cls)
        return new_cls

    return wrapper
