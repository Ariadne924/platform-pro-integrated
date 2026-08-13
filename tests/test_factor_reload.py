"""Live factor reload: new / edited / removed def files apply without a restart.

The ``@factor`` decorator registers into the process-wide singleton registry,
so these tests exercise that same registry and restore the real defs in a
finally block.
"""

import pytest

from superplatform.factors.registry import FactorRegistry


@pytest.fixture
def temp_pkg(tmp_path, monkeypatch):
    """A throwaway importable package holding a hot-swappable factor module.

    ``reload()`` replaces the whole registry with the named package, so the
    tests must start from an empty registry (a fresh process) to keep the
    before/after diff deterministic regardless of which other tests already
    discovered the real factor defs.
    """
    pkg = tmp_path / "reload_test_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    reg = FactorRegistry.get_instance()
    reg._factors.clear()
    reg._instances.clear()
    return "reload_test_pkg"


def _write_module(pkg: str, tmp_path, *, period: int = 10, with_second: bool = False) -> None:
    """(Re)write reload_test_pkg/reload_module.py."""
    lines = [
        "import pandas as pd",
        "from superplatform.factors.base import FactorCategory, factor",
        "",
        "@factor('reload_factor_a', FactorCategory.MOMENTUM_REVERSAL, description='a',",
        "        required_data=['kline'],",
        f"        params_schema={{'period': {{'type': 'int', 'default': {period}}}}})",
        f"def reload_factor_a(data, period={period}):",
        "    return pd.DataFrame({'timestamp': pd.date_range(",
        "        '2024-01-01', periods=5, freq='D', tz='UTC'),",
        "        'value': [period] * 5})",
    ]
    if with_second:
        lines += [
            "",
            "@factor('reload_factor_b', FactorCategory.VOLATILITY, description='b',",
            "        required_data=['kline'])",
            "def reload_factor_b(data):",
            "    return pd.DataFrame({'timestamp': pd.date_range(",
            "        '2024-01-01', periods=5, freq='D', tz='UTC'),",
            "        'value': [1] * 5})",
        ]
    module = tmp_path / "reload_test_pkg" / "reload_module.py"
    module.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _restore_defs() -> None:
    """Put the real factor defs back in the shared registry."""
    FactorRegistry.get_instance().reload()


def test_reload_discovers_new_factor(temp_pkg, tmp_path):
    reg = FactorRegistry.get_instance()
    try:
        _write_module(temp_pkg, tmp_path, period=10)
        reload = reg.reload(temp_pkg)
        assert "reload_factor_a" in reg.list_all()
        assert reload["new_factors"] == ["reload_factor_a"]
        assert reload["removed_factors"] == []
    finally:
        _restore_defs()


def test_reload_applies_edit_and_new_factor(temp_pkg, tmp_path):
    reg = FactorRegistry.get_instance()
    try:
        _write_module(temp_pkg, tmp_path, period=10)
        reg.reload(temp_pkg)
        assert reg.get("reload_factor_a").params_schema["period"]["default"] == 10

        # Edit the file: change the default and add a second factor.
        _write_module(temp_pkg, tmp_path, period=30, with_second=True)
        reload = reg.reload(temp_pkg)

        assert reg.get("reload_factor_a").params_schema["period"]["default"] == 30
        assert "reload_factor_b" in reg.list_all()
        assert reload["new_factors"] == ["reload_factor_b"]
    finally:
        _restore_defs()


def test_reload_removes_deleted_file(temp_pkg, tmp_path):
    reg = FactorRegistry.get_instance()
    try:
        _write_module(temp_pkg, tmp_path)
        reg.reload(temp_pkg)
        assert "reload_factor_a" in reg.list_all()

        (tmp_path / "reload_test_pkg" / "reload_module.py").unlink()
        reload = reg.reload(temp_pkg)

        assert "reload_factor_a" not in reg.list_all()
        assert reload["removed_factors"] == ["reload_factor_a"]
    finally:
        _restore_defs()
