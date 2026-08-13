"""Factor instances — fixed-parameter factors derived from factory factors.

Two-layer split:

- **Factory layer** (`FactorRegistry`): configurable factors (`momentum`,
  `high_low_range`, ...) with an editable `params_schema`. Used only for
  exploration (ad-hoc parameter sweeps) and for deriving instances.
- **Instance layer** (`FactorInstanceRegistry`): concrete fixed-parameter
  factors (`momentum_60d` = `momentum` + `{lookback_days: 60}`) that
  strategies, panels and live consume.

An instance is a first-class named factor whose ``compute`` injects its fixed
parameter preset, so callers that pass no params (the live runtime) still
evaluate with the preset.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from superplatform.factors.base import Factor
from superplatform.factors.metadata import FACTOR_METADATA, FactorMetadata
from superplatform.factors.param_schema import normalize_params_schema


class FactorInstance(Factor):
    """A factory factor bound to a fixed parameter preset.

    ``name``/``category``/``description``/``required_data``/
    ``required_symbols``/``params_schema`` are derived from the factory factor.
    ``compute`` merges the preset as a base so live's no-arg call uses the
    preset; callers may override individual params.
    """

    factory_name: str

    def __init__(
        self,
        factory: type[Factor],
        name: str,
        params: dict[str, Any],
        description: str = "",
    ) -> None:
        self._factory_cls = factory
        self._factory = factory()
        self.factory_name = self._factory.name
        self.name = name
        self.category = self._factory.category
        self.description = description or self._factory.description
        self.required_data = list(self._factory.required_data)
        self.required_symbols = self._factory.required_symbols
        self.params_schema = dict(self._factory.params_schema)
        self._params = dict(params)

    @property
    def params(self) -> dict[str, Any]:
        """The fixed parameter preset."""
        return dict(self._params)

    def compute(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        **params: Any,
    ):
        merged = {**self._params, **params}
        return self._factory.compute(data, **merged)


def instance_metadata(instance: Factor) -> FactorMetadata | None:
    """Derive a FactorMetadata for an instance from its factory + preset.

    Stateless — never mutates ``FACTOR_METADATA`` (test-isolation safe).
    Returns ``None`` for non-instances so callers can fall back unchanged.
    """
    if not isinstance(instance, FactorInstance):
        return None
    base = FACTOR_METADATA.get(instance.factory_name)
    if base is None:
        return None
    return FactorMetadata(
        formula=base.formula,
        default_params=dict(instance._params),
        required_fields=base.required_fields,
        economic_meaning=base.economic_meaning,
    )


def instances_from_config(config, factory_registry) -> dict[str, FactorInstance]:
    """Build FactorInstance objects from the config ``factor_instances:`` section.

    Each entry::

        momentum_60d:
          factory: momentum
          params: {lookback_days: 60}
          symbols: *research_pool
          description: 60日动量

    Raises ``ValueError`` with an actionable Chinese message on the first
    malformed entry. Param names are validated against the factory's
    ``params_schema`` so config typos surface here, not at evaluation time.
    """
    raw = config.get("factor_instances") or {}
    if not isinstance(raw, dict):
        raise ValueError("`factor_instances` 需要是一个映射（实例名 → 定义）")
    instances: dict[str, FactorInstance] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"factor instance '{name}' 需要是一个映射（含 factory/params）")
        factory_name = spec.get("factory")
        if not isinstance(factory_name, str) or not factory_name:
            raise ValueError(f"factor instance '{name}' 缺少 `factory`（工厂因子名）")
        try:
            factory = factory_registry.get(factory_name)
        except KeyError:
            raise ValueError(
                f"factor instance '{name}' 引用的工厂因子 '{factory_name}' 未注册"
            ) from None
        params = spec.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"factor instance '{name}' 的 `params` 需要是一个对象")

        schema = normalize_params_schema(factory.params_schema or {})
        unknown = sorted(set(params).difference(schema))
        if unknown:
            raise ValueError(
                f"factor instance '{name}' 的未知参数 {unknown}，"
                f"工厂 '{factory_name}' 可用参数: {sorted(schema)}"
            )

        description = spec.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"factor instance '{name}' 的 `description` 需要是字符串")

        instances[name] = FactorInstance(
            factory=type(factory),
            name=name,
            params=params,
            description=description,
        )
    return instances
