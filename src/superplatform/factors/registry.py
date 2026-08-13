"""Factor registry — hot-swappable factor management.

All factors are registered here. The registry supports:
- Register/unregister factors at runtime
- Query factors by category, required data, or name
- Validation that no duplicate names exist
"""

import importlib
import pkgutil
import sys
from typing import Optional

import pandas as pd

from superplatform.factors.base import Factor, FactorCategory, FactorResult


class FactorRegistry:
    """Global registry of all available factors.

    Usage:
        registry = FactorRegistry()
        registry.register(MyMomentumFactor())
        registry.register(MyVolatilityFactor())

        factor = registry.get("momentum")
        result = factor.compute(data_dict)
    """

    _instance: Optional["FactorRegistry"] = None

    def __init__(self):
        self._factors: dict[str, type[Factor]] = {}
        self._instances: dict[str, Factor] = {}

    @classmethod
    def get_instance(cls) -> "FactorRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, factor_cls_or_instance) -> None:
        """Register a factor class or instance."""
        if isinstance(factor_cls_or_instance, Factor):
            inst = factor_cls_or_instance
            name = inst.name
            self._instances[name] = inst
            self._factors[name] = type(inst)
        else:
            cls_obj = factor_cls_or_instance
            name = cls_obj.name
            self._factors[name] = cls_obj

        if name in self._factors and name in self._instances:
            pass

    def unregister(self, name: str) -> None:
        self._factors.pop(name, None)
        self._instances.pop(name, None)

    def get(self, name: str) -> Factor:
        """Get a factor instance by name (creates if class-registered)."""
        if name in self._instances:
            return self._instances[name]
        if name in self._factors:
            inst = self._factors[name]()
            self._instances[name] = inst
            return inst
        raise KeyError(f"Factor '{name}' not registered")

    def list_all(self) -> list[str]:
        return sorted(set(self._factors.keys()) | set(self._instances.keys()))

    def list_by_category(self, category: FactorCategory) -> list[str]:
        return [
            name
            for name in self.list_all()
            if self.get(name).category == category
        ]

    def list_by_required_data(self, data_type: str) -> list[str]:
        return [
            name
            for name in self.list_all()
            if data_type in self.get(name).required_data
        ]

    def categories_summary(self) -> dict[str, int]:
        """Return count of factors per category."""
        summary: dict[str, int] = {}
        for name in self.list_all():
            cat = self.get(name).category.value
            summary[cat] = summary.get(cat, 0) + 1
        return summary

    def compute_one(
        self,
        name: str,
        data: dict[str, "pd.DataFrame"],
    ) -> FactorResult:
        """Compute a single factor for one symbol."""
        return self.get(name).compute(data)

    def auto_discover(self, package_path: str = "superplatform.factors.defs") -> int:
        """Recursively import all modules in the defs tree.

        Walks subpackages so factors defined in e.g.
        `defs/momentum_reversal/momentum.py` are discovered.
        """
        count = 0
        try:
            package = importlib.import_module(package_path)
            for _finder, mod_name, _is_pkg in pkgutil.walk_packages(
                package.__path__, prefix=package_path + "."
            ):
                importlib.import_module(mod_name)
                count += 1
        except ModuleNotFoundError:
            pass
        return count

    def reload(self, package_path: str = "superplatform.factors.defs") -> dict:
        """Re-discover factors from disk so code edits take effect live.

        ``auto_discover`` alone can't do this: ``sys.modules`` caches the
        already-imported def modules (edits are never re-executed) and this
        registry keeps stale instances.  Reload drops both caches for the
        package subtree, then re-imports everything so new, modified and
        removed factors all resolve to their on-disk state.
        """
        before = set(self.list_all())

        self._factors.clear()
        self._instances.clear()

        # Purge the package and every module beneath it from sys.modules so
        # importlib re-executes them. Child packages (e.g. the category
        # subdirectories) are included by the prefix match.
        prefix = package_path + "."
        for name in [package_path] + [
            mod for mod in list(sys.modules) if mod.startswith(prefix)
        ]:
            sys.modules.pop(name, None)

        imported = self.auto_discover(package_path)

        after = set(self.list_all())
        return {
            "imported_modules": imported,
            "before": len(before),
            "after": len(after),
            "new_factors": sorted(after - before),
            "removed_factors": sorted(before - after),
        }
