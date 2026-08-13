"""Configuration loading from YAML files.

Configuration hierarchy:
    config/default.yaml     — baseline defaults
    config/exchanges.yaml   — exchange-specific settings (API keys from env vars)
    config/factors.yaml     — factor parameters
"""

from pathlib import Path
from typing import Any

import yaml


class Config:
    """Application configuration loaded from YAML.

    Usage:
        cfg = Config.load("config/default.yaml")
        cfg = Config.load("config/default.yaml", "config/exchanges.yaml")
    """

    def __init__(self, data: dict[str, Any] | None = None):
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    @classmethod
    def load(cls, *paths: str) -> "Config":
        """Load and merge one or more YAML config files."""
        merged: dict[str, Any] = {}
        for path in paths:
            p = Path(path)
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                cls._deep_merge(merged, data)
        return cls(merged)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                Config._deep_merge(base[k], v)
            else:
                base[k] = v

    def to_dict(self) -> dict[str, Any]:
        return self._data

    def replace(self, data: dict[str, Any]) -> None:
        """Replace this Config's contents in place.

        Keeps the same object identity so references captured via
        ``from module import config`` continue to see the new values.
        """
        self._data = data
