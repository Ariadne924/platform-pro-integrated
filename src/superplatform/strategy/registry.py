"""Strategy registry -- same pattern as FactorRegistry."""

import importlib
import pkgutil
from typing import Optional

from superplatform.strategy.base import Strategy


class StrategyRegistry:
    _instance: Optional["StrategyRegistry"] = None

    def __init__(self):
        self._strategies: dict[str, type[Strategy]] = {}
        self._instances: dict[str, Strategy] = {}

    @classmethod
    def get_instance(cls) -> "StrategyRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, cls_or_instance) -> None:
        if isinstance(cls_or_instance, Strategy):
            self._instances[cls_or_instance.name] = cls_or_instance
            self._strategies[cls_or_instance.name] = type(cls_or_instance)
        else:
            self._strategies[cls_or_instance.name] = cls_or_instance

    def unregister(self, name: str) -> None:
        self._strategies.pop(name, None)
        self._instances.pop(name, None)

    def get(self, name: str) -> Strategy:
        if name in self._instances:
            return self._instances[name]
        if name in self._strategies:
            inst = self._strategies[name]()
            self._instances[name] = inst
            return inst
        raise KeyError(f"Strategy '{name}' not registered")

    def list_all(self) -> list[str]:
        return sorted(set(self._strategies.keys()) | set(self._instances.keys()))

    def auto_discover(self, package_path: str = "superplatform.strategy.defs") -> int:
        count = 0
        try:
            package = importlib.import_module(package_path)
            for _, mod_name, _ in pkgutil.walk_packages(
                package.__path__, prefix=package_path + "."
            ):
                importlib.import_module(mod_name)
                count += 1
        except ModuleNotFoundError:
            pass
        return count
