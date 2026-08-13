"""Registry-level coverage for factor parameters exposed to the web UI."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from superplatform.factors.param_schema import normalize_params_schema
from superplatform.factors.registry import FactorRegistry

_DEFS_ROOT = Path(__file__).resolve().parents[1] / "src" / "superplatform" / "factors" / "defs"
_INTERNAL_PARAMS = {"epsilon"}
_REQUIRED_SCHEMA_FIELDS = {
    "name",
    "type",
    "default",
    "required",
    "min",
    "max",
    "step",
    "adjustment_unit",
    "physical_unit",
    "ui_precision",
    "description",
    "example",
}


def _factor_parameter_reads() -> dict[str, set[str]]:
    """Return every non-internal ``params.get`` key used by a factor function."""
    reads: dict[str, set[str]] = {}
    for path in _DEFS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            decorator = next(
                (
                    item for item in node.decorator_list
                    if isinstance(item, ast.Call) and getattr(item.func, "id", None) == "factor"
                ),
                None,
            )
            if decorator is None:
                continue
            kwargs = {item.arg: item.value for item in decorator.keywords if item.arg}
            name_node = kwargs.get("name") or (decorator.args[0] if decorator.args else None)
            if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
                continue
            keys = {
                call.args[0].value
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "get"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "params"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                and call.args[0].value not in _INTERNAL_PARAMS
            }
            if keys:
                reads[name_node.value] = keys
    return reads


def test_parameter_reads_have_complete_registered_schema():
    registry = FactorRegistry.get_instance()
    registry.auto_discover()

    for factor_name, used_params in _factor_parameter_reads().items():
        schema = registry.get(factor_name).params_schema
        assert used_params <= set(schema), factor_name
        for param_name in used_params:
            spec = schema[param_name]
            assert _REQUIRED_SCHEMA_FIELDS <= set(spec), (factor_name, param_name)
            assert spec["name"] == param_name
            assert spec["required"] is False
            assert spec["type"] in {"int", "float", "bool", "str"}
            if spec["type"] in {"int", "float"}:
                assert spec["min"] <= spec["default"] <= spec["max"]
                assert spec["adjustment_unit"] > 0
                assert spec["step"] == spec["adjustment_unit"]


def test_internal_numeric_epsilon_is_not_exposed_as_a_new_ui_control():
    registry = FactorRegistry.get_instance()
    registry.auto_discover()

    for factor_name in (
        "crypto_weekend_weekday_volume_ratio",
        "trend_strength",
        "volume_momentum",
    ):
        assert "epsilon" not in registry.get(factor_name).params_schema


def test_parameter_change_reaches_factor_compute():
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    timestamps = pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC")
    close = pd.Series(range(1, 31), dtype="float64")
    kline = pd.DataFrame({
        "timestamp": timestamps,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 100.0,
    })
    data = {"kline": {"TESTUSDT": kline}}
    factor = registry.get("momentum")

    short = factor.compute(data, lookback_days=3).values["value"]
    long = factor.compute(data, lookback_days=10).values["value"]

    assert short.iloc[-1] != long.iloc[-1]


def test_calendar_window_factors_preserve_wall_clock_semantics_across_cadences():
    registry = FactorRegistry.get_instance()
    registry.auto_discover()
    factor = registry.get("momentum")

    def _kline(frequency: str, periods: int) -> pd.DataFrame:
        timestamps = pd.date_range("2024-01-01", periods=periods, freq=frequency, tz="UTC")
        hours = (timestamps - timestamps[0]).total_seconds() / 3600
        close = pd.Series(100.0 + hours)
        return pd.DataFrame({
            "timestamp": timestamps,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 100.0,
        })

    daily = _kline("1D", 31)
    hourly = _kline("1h", 30 * 24 + 1)

    daily_value = factor.compute(
        {"kline": {"TESTUSDT": daily}}, lookback_days=10
    ).values["value"].iloc[-1]
    hourly_value = factor.compute(
        {"kline": {"TESTUSDT": hourly}}, lookback_days=10
    ).values["value"].iloc[-1]

    assert daily_value == pytest.approx(hourly_value)


def test_legacy_numeric_schema_gets_compatible_adjustment_unit_fallback():
    normalized = normalize_params_schema({
        "window_days": {
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 120,
            "description": "窗口",
        },
        "threshold": {
            "type": "float",
            "default": 0.05,
            "min": 0.0,
            "max": 0.2,
            "description": "阈值",
        },
    })

    assert normalized["window_days"]["adjustment_unit"] == 1
    assert normalized["window_days"]["adjustment_unit_inference"] == "auto"
    assert normalized["threshold"]["adjustment_unit"] == 0.01
    assert normalized["threshold"]["step"] == 0.01


@pytest.mark.parametrize("spec", [
    {
        "type": "float",
        "default": 0.1,
        "min": 0.0,
        "max": 1.0,
        "adjustment_unit": 0,
        "description": "无效步长",
    },
    {
        "type": "float",
        "default": 0.15,
        "min": 0.0,
        "max": 1.0,
        "adjustment_unit": 0.1,
        "description": "默认值未对齐",
    },
    {
        "type": "float",
        "default": 1.1,
        "min": 0.0,
        "max": 1.0,
        "adjustment_unit": 0.1,
        "description": "默认值超出范围",
    },
])
def test_invalid_adjustment_unit_schema_is_rejected(spec):
    with pytest.raises(ValueError):
        normalize_params_schema({"threshold": spec})
