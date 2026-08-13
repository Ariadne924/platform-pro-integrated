"""Factor instance registry — the concrete fixed-parameter factor layer.

Separate from ``FactorRegistry`` (the configurable factory layer). Strategies
and production evaluation consume instances by name; factory factors are used
only for exploration and instance derivation.
"""

from __future__ import annotations

from superplatform.factors.instances import FactorInstance, instances_from_config
from superplatform.factors.registry import FactorRegistry


class FactorInstanceRegistry:
    """Global registry of factor instances keyed by instance name."""

    _instance: FactorInstanceRegistry | None = None

    def __init__(self) -> None:
        self._instances: dict[str, FactorInstance] = {}

    @classmethod
    def get_instance(cls) -> FactorInstanceRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, instance: FactorInstance) -> None:
        self._instances[instance.name] = instance

    def unregister(self, name: str) -> None:
        self._instances.pop(name, None)

    def get(self, name: str) -> FactorInstance:
        try:
            return self._instances[name]
        except KeyError:
            raise KeyError(f"Factor instance '{name}' not registered") from None

    def has(self, name: str) -> bool:
        return name in self._instances

    def list_all(self) -> list[str]:
        return sorted(self._instances)

    def clear(self) -> None:
        self._instances.clear()

    def build_from_config(self, config, factory_registry: FactorRegistry) -> list[str]:
        """(Re)build instances from config ``factor_instances:``; idempotent.

        Clears the registry, then rebuilds from the config section. Call after
        ``factory_registry.auto_discover()`` / ``reload()`` and after config
        reloads so instance edits take effect.
        """
        self.clear()
        for _name, instance in instances_from_config(config, factory_registry).items():
            self.register(instance)
        return self.list_all()
