"""Read / write factor & strategy configuration in ``config/factors.yaml``.

Uses ruamel.yaml round-trip mode so comments and formatting are preserved
when a config is modified through the API. Changing the YAML on disk is
the only persistence step — ``state.reload_config()`` then re-merges it
into the live :class:`Config`.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # factor_config.py → project root
_FACTORS_PATH = _PROJECT_ROOT / "config" / "factors.yaml"


def _load() -> dict:
    rt = YAML(typ="rt")
    if not _FACTORS_PATH.exists():
        return {}
    with open(_FACTORS_PATH, encoding="utf-8") as f:
        data = rt.load(f)
    return data or {}


def _dump(data: dict) -> None:
    rt = YAML(typ="rt")
    rt.width = 4096  # avoid awkwardly wrapping long symbol lists
    rt.indent(mapping=2, sequence=4, offset=2)
    with open(_FACTORS_PATH, "w", encoding="utf-8") as f:
        rt.dump(data, f)


def factor_names() -> list[str]:
    return sorted((_load().get("factors") or {}).keys())


def strategy_names() -> list[str]:
    return sorted((_load().get("strategies") or {}).keys())


def get_factor_config(name: str) -> dict:
    """Config entry for a factor name (factory ``factors:`` or instance)."""
    data = _load()
    factor = (data.get("factors") or {}).get(name)
    if factor is not None:
        return dict(factor)
    instance = (data.get("factor_instances") or {}).get(name)
    return dict(instance) if instance else {}


def set_factor_config(name: str, patch: dict) -> dict:
    """Set top-level config fields (symbols/providers/params/frequency/…).

    Each key in ``patch`` replaces the existing value wholesale — the
    frontend sends the complete desired value, not a partial merge.
    """
    data = _load()
    factors = data.setdefault("factors", {})
    factor = factors.setdefault(name, {})
    for key, value in patch.items():
        factor[key] = value
    _dump(data)
    return dict(factor)


def remove_factor_config(name: str) -> bool:
    data = _load()
    factors = data.get("factors") or {}
    if name not in factors:
        return False
    del factors[name]
    _dump(data)
    return True


def instance_names() -> list[str]:
    return sorted((_load().get("factor_instances") or {}).keys())


def set_instance_config(name: str, entry: dict) -> dict:
    """Create/overwrite a factor instance entry in ``factor_instances:``."""
    data = _load()
    instances = data.setdefault("factor_instances", {})
    instances[name] = entry
    _dump(data)
    return dict(entry)


def remove_instance_config(name: str) -> bool:
    data = _load()
    instances = data.get("factor_instances") or {}
    if name not in instances:
        return False
    del instances[name]
    _dump(data)
    return True


def get_strategy_config(name: str) -> dict:
    strategy = (_load().get("strategies") or {}).get(name)
    return dict(strategy) if strategy else {}


def set_strategy_config(name: str, patch: dict) -> dict:
    """Set strategy fields (e.g. ``used_factors``)."""
    data = _load()
    strategies = data.setdefault("strategies", {})
    strategy = strategies.setdefault(name, {})
    for key, value in patch.items():
        strategy[key] = value
    _dump(data)
    return dict(strategy)
