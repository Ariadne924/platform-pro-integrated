"""Factor base class and registration decorator.

A factor computes a value for each timestamp. It may operate on a single
symbol (momentum, volatility, RSI) or on a group of symbols (pair spread,
basket momentum). That choice is declared by required_symbols.

The config controls which symbols go into each call via the `symbols` field:
  symbols: [S1, S2, S3]           → Runtime calls factor 3 times, one per symbol
  symbols: [[S1, S2], [S3, S4]]   → Runtime calls factor 2 times, each with 2 symbols

Data contract:
  data is dict[str, dict[str, DataFrame]] — data_type → {symbol: DataFrame}
  Single-symbol factors get one key in the inner dict.
  Multi-symbol factors get one key per symbol in their group.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pandas as pd

from superplatform.factors.param_schema import normalize_params_schema


class FactorCategory(StrEnum):
    MOMENTUM_REVERSAL = "momentum_reversal"
    VOLATILITY = "volatility"
    VOLUME_LIQUIDITY = "volume_liquidity"
    MICROSTRUCTURE = "microstructure"
    CRYPTO_SPECIFIC = "crypto_specific"


@dataclass
class FactorResult:
    """Factor computation output.

    values has columns (timestamp, value). The symbol is known by Runtime
    (it's the group key in the config) and attached by Runtime when needed.
    """

    name: str
    category: FactorCategory
    values: pd.DataFrame
    metadata: dict = field(default_factory=dict)


class Factor(ABC):
    """Abstract base class for a factor."""

    name: str
    category: FactorCategory
    description: str = ""
    version: str = "0.1.0"
    required_data: list[str] = field(default_factory=list)
    required_symbols: int | None = None  # None=any, 1=single, 2=pair, etc.
    params_schema: dict[str, dict] = field(default_factory=dict)

    @abstractmethod
    def compute(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        # data_type → {symbol: DataFrame}
        **params: Any,
    ) -> FactorResult:
        ...


def factor(
    name: str,
    category: FactorCategory,
    description: str = "",
    required_data: list[str] | None = None,
    required_symbols: int | None = None,
    params_schema: dict[str, dict] | None = None,
) -> Callable:
    """Decorator to create a Factor from a function.

    Usage:
        # Single-symbol
        @factor("momentum", ..., required_symbols=1)
        def momentum(data, **params):
            kline = list(data["kline"].values())[0]

        # Pairs
        @factor("pair_spread", ..., required_symbols=2)
        def pair_spread(data, **params):
            syms = list(data["kline"].keys())
            s0, s1 = data["kline"][syms[0]], data["kline"][syms[1]]

    ``params_schema`` declares the factor's editable parameters (name → spec
    with type/default/description/min/max/enum). It drives the web config
    form and request-time validation. ``default`` must match the value the
    compute function falls back to via ``params.get(name, default)``.
    """

    def wrapper(fn: Callable) -> type[Factor]:
        cls_name = f"Factor_{name}"

        def compute(self, data, **params):
            result_values = fn(data, **params)
            if isinstance(result_values, pd.DataFrame):
                return FactorResult(
                    name=name,
                    category=category,
                    values=result_values,
                    metadata={"params": params},
                )
            return result_values

        new_cls = type(
            cls_name,
            (Factor,),
            {
                "compute": compute,
                "name": name,
                "category": category,
                "description": description,
                "required_data": required_data or [],
                "required_symbols": required_symbols,
                "params_schema": normalize_params_schema(params_schema or {}),
            },
        )
        from superplatform.factors.registry import FactorRegistry

        FactorRegistry.get_instance().register(new_cls)
        return new_cls

    return wrapper
