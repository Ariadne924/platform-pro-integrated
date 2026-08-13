"""Unified factor-name resolution across the two layers.

- ``resolve_factor(name)``: instance layer first, factory layer second — the
  single seam the pipeline / live / research / generator use to turn a factor
  name into a compute-able ``Factor``.
- ``factor_entry(config, name)``: the config entry for a factor name, reading
  either the ``factors:`` (factory) or ``factor_instances:`` (instance)
  section.

Keeping both here means downstream code touches only this module instead of
re-implementing the two-layer lookup everywhere.
"""

from __future__ import annotations

from typing import Any

from superplatform.factors.base import Factor
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.registry import FactorRegistry


def resolve_factor(
    name: str,
    factory_registry: FactorRegistry | None = None,
    instance_registry: FactorInstanceRegistry | None = None,
) -> Factor:
    """Resolve a factor name to a compute-able Factor (instance first)."""
    instance_registry = instance_registry or FactorInstanceRegistry.get_instance()
    if instance_registry.has(name):
        return instance_registry.get(name)
    factory_registry = factory_registry or FactorRegistry.get_instance()
    return factory_registry.get(name)


def factor_entry(config, name: str) -> dict[str, Any]:
    """Config entry for a factor name (factory or instance section).

    An instance inherits its factory's base entry (symbols / providers /
    start / end / frequency) and overrides with its own fields, so a config
    instance typically only needs ``factory`` + ``params``.
    """
    entry = config.get(f"factors.{name}")
    if entry:
        return entry
    instance = config.get(f"factor_instances.{name}")
    if instance is None:
        return {}
    factory_name = instance.get("factory")
    factory_entry = config.get(f"factors.{factory_name}") if factory_name else None
    return {**(factory_entry or {}), **instance}


def validate_used_factors_are_instances(
    used_factors: list[str],
    factory_registry: FactorRegistry | None = None,
    instance_registry: FactorInstanceRegistry | None = None,
) -> None:
    """Enforce that strategies reference instances only, never factory factors.

    Raises ``ValueError`` with an actionable Chinese message when a used factor
    is a configurable factory (not a configured instance).
    """
    instance_registry = instance_registry or FactorInstanceRegistry.get_instance()
    factory_registry = factory_registry or FactorRegistry.get_instance()
    factories = set(factory_registry.list_all())
    for name in used_factors:
        if name in factories and not instance_registry.has(name):
            raise ValueError(
                f"策略只能引用实例：'{name}' 是工厂因子（参数可配置），"
                f"请先在 factor_instances 中把它保存为实例"
            )
