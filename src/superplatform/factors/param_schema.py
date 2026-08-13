"""Reusable UI parameter schemas for factor decorators."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

_NUMERIC_TYPES = {"int", "float", "number"}

_PARAMETER_TEMPLATES: dict[str, dict[str, Any]] = {
    "period": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "滚动计算窗口（期）",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "short_period": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "短周期滚动窗口（期）",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "long_period": {
        "type": "int",
        "min": 2,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "长周期滚动窗口（期）",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "skip_period": {
        "type": "int",
        "min": 0,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "动量计算时跳过的最近期数",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "lookback": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "回看窗口（期）",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "threshold_period": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "跳跃阈值估计窗口（期）",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "vol_period": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "基础波动率计算窗口（期）",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "window": {
        "type": "int",
        "min": 2,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar",
        "ui_precision": 0,
        "description": "二次滚动统计窗口（期）",
        "unit": "bar",
        "unit_display": "根",
        "scale_hint": "bar",
    },
    "annualization": {
        "type": "int",
        "min": 1,
        "max": 10000,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "bar/year",
        "ui_precision": 0,
        "description": "年化换算周期数",
        "unit": "bar/year",
        "unit_display": "根/年",
        "scale_hint": "bar_per_year",
    },
    "quantile": {
        "type": "float",
        "min": 0.01,
        "max": 0.99,
        "step": 0.01,
        "adjustment_unit": 0.01,
        "physical_unit": "pct",
        "ui_precision": 2,
        "description": "尾部风险统计分位数",
        "unit": "pct",
        "unit_display": "%",
        "scale_hint": "pct",
    },
    # Day-literal windows — wall-clock semantics, converted to rows via
    # `lookback_bars` at compute time so factor meaning does not change with
    # the evaluation cadence.
    "lookback_days": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "day",
        "ui_precision": 0,
        "description": "回看窗口（天）",
        "unit": "day",
        "unit_display": "天",
        "scale_hint": "day",
    },
    "long_window_days": {
        "type": "int",
        "min": 2,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "day",
        "ui_precision": 0,
        "description": "长窗口（天）",
        "unit": "day",
        "unit_display": "天",
        "scale_hint": "day",
    },
    "short_window_days": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "day",
        "ui_precision": 0,
        "description": "短窗口（天）",
        "unit": "day",
        "unit_display": "天",
        "scale_hint": "day",
    },
    "skip_days": {
        "type": "int",
        "min": 0,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "day",
        "ui_precision": 0,
        "description": "跳过的最近天数",
        "unit": "day",
        "unit_display": "天",
        "scale_hint": "day",
    },
    "vol_window_days": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "day",
        "ui_precision": 0,
        "description": "波动率窗口（天）",
        "unit": "day",
        "unit_display": "天",
        "scale_hint": "day",
    },
    "window_days": {
        "type": "int",
        "min": 2,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "day",
        "ui_precision": 0,
        "description": "滚动窗口（天）",
        "unit": "day",
        "unit_display": "天",
        "scale_hint": "day",
    },
    "threshold_window_days": {
        "type": "int",
        "min": 1,
        "max": 3650,
        "step": 1,
        "adjustment_unit": 1,
        "physical_unit": "day",
        "ui_precision": 0,
        "description": "阈值估计窗口（天）",
        "unit": "day",
        "unit_display": "天",
        "scale_hint": "day",
    },
}

_UNIT_BY_PARAMETER_NAME: dict[str, dict[str, str | None]] = {
    "period": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "short_period": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "long_period": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "skip_period": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "lookback": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "threshold_period": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "vol_period": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "window": {"unit": "bar", "unit_display": "根", "scale_hint": "bar"},
    "lookback_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "long_window_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "short_window_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "skip_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "vol_window_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "window_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "threshold_window_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "change_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "zscore_days": {"unit": "day", "unit_display": "天", "scale_hint": "day"},
    "annualization": {
        "unit": "bar/year",
        "unit_display": "根/年",
        "scale_hint": "bar_per_year",
    },
    "quantile": {"unit": "pct", "unit_display": "%", "scale_hint": "pct"},
    # Numerical floors are retained for legacy config compatibility but are
    # intentionally not advertised as business-level UI parameters.
    "epsilon": {"unit": None, "unit_display": None, "scale_hint": None},
}


def infer_unit_metadata(name: str) -> dict[str, str | None]:
    """Infer canonical parameter-unit metadata from a stable parameter name."""
    return dict(_UNIT_BY_PARAMETER_NAME.get(
        name,
        {"unit": None, "unit_display": None, "scale_hint": None},
    ))


def _decimal_places(value: Any) -> int:
    """Return a stable decimal precision for a JSON/YAML scalar."""
    try:
        decimal = Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return 2
    return max(0, -decimal.as_tuple().exponent)


def _infer_adjustment_unit(name: str, spec: dict[str, Any], ptype: str) -> tuple[Any, str]:
    """Infer a tuning quantum for old schemas that only expose ``step``."""
    if "adjustment_unit" in spec:
        return spec["adjustment_unit"], "declared"
    if "step" in spec:
        return spec["step"], "from_step"
    if ptype == "int":
        return 1, "auto"

    # Prefer the precision explicitly visible in the legacy bounds/default.
    candidates = [
        _decimal_places(spec.get(key))
        for key in ("default", "min", "max")
        if spec.get(key) is not None
    ]
    precision = max(candidates, default=2)
    # Preserve the historical float-control fallback of 0.01 unless the
    # schema itself makes a finer quantum explicit (for example 1e-12).
    precision = max(2, precision)
    return float(Decimal(1).scaleb(-min(precision, 15))), "auto"


def _is_aligned(value: Any, minimum: Any, step: Any, tolerance: Decimal = Decimal("1e-9")) -> bool:
    try:
        quotient = (Decimal(str(value)) - Decimal(str(minimum))) / Decimal(str(step))
    except (InvalidOperation, ZeroDivisionError):
        return False
    nearest = quotient.to_integral_value()
    return abs(quotient - nearest) <= tolerance


def is_adjustment_aligned(value: Any, minimum: Any, step: Any) -> bool:
    """Public Decimal-backed alignment check for request validation."""
    return _is_aligned(value, minimum, step)


def nearest_adjustment_values(
    value: Any,
    minimum: Any,
    maximum: Any,
    step: Any,
) -> tuple[Any, Any]:
    """Return the lower/upper legal grid values surrounding ``value``."""
    value_d = Decimal(str(value))
    minimum_d = Decimal(str(minimum))
    maximum_d = Decimal(str(maximum))
    step_d = Decimal(str(step))
    quotient = (value_d - minimum_d) / step_d
    lower_index = quotient.to_integral_value(rounding="ROUND_FLOOR")
    upper_index = quotient.to_integral_value(rounding="ROUND_CEILING")
    lower = minimum_d + lower_index * step_d
    upper = minimum_d + upper_index * step_d
    lower = max(minimum_d, min(maximum_d, lower))
    upper = max(minimum_d, min(maximum_d, upper))

    def _json_number(number: Decimal) -> int | float:
        return int(number) if number == number.to_integral_value() else float(number)

    return _json_number(lower), _json_number(upper)


def validate_param_spec(name: str, spec: dict[str, Any]) -> None:
    """Validate schema invariants shared by registration and request parsing."""
    ptype = spec.get("type", "number")
    if ptype not in _NUMERIC_TYPES:
        return

    step = spec.get("adjustment_unit")
    if isinstance(step, bool) or not isinstance(step, (int, float)):
        raise ValueError(f"参数 {name} 的 adjustment_unit 必须是正数")
    if not math.isfinite(float(step)) or float(step) <= 0:
        raise ValueError(f"参数 {name} 的 adjustment_unit 必须大于 0")
    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is None or maximum is None:
        raise ValueError(f"参数 {name} 必须声明 min 和 max")
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, (int, float))
        or not isinstance(maximum, (int, float))
        or not math.isfinite(float(minimum))
        or not math.isfinite(float(maximum))
    ):
        raise ValueError(f"参数 {name} 的 min/max 必须是有限数值")
    minimum_d = Decimal(str(minimum))
    maximum_d = Decimal(str(maximum))
    if maximum_d < minimum_d:
        raise ValueError(f"参数 {name} 的 max 不能小于 min")
    if not _is_aligned(maximum, minimum, step):
        raise ValueError(
            f"参数 {name} 的区间 [{minimum}, {maximum}] 无法按 "
            f"adjustment_unit={step} 离散化"
        )
    if "default" in spec:
        default = spec["default"]
        if (
            isinstance(default, bool)
            or not isinstance(default, (int, float))
            or not math.isfinite(float(default))
        ):
            raise ValueError(f"参数 {name} 的 default 必须是有限数值")
        if ptype == "int" and (not isinstance(default, int) or isinstance(default, bool)):
            raise ValueError(f"参数 {name} 的 int 默认值必须是整数")
        default_d = Decimal(str(default))
        if default_d < minimum_d or default_d > maximum_d:
            raise ValueError(
                f"参数 {name} 的 default={default} 必须位于 [{minimum}, {maximum}]"
            )
        if not _is_aligned(default, minimum, step):
            raise ValueError(
                f"参数 {name} 的 default={default} 未按 "
                f"adjustment_unit={step} 对齐"
            )


def normalize_params_schema(schema: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fill metadata fields on legacy schemas without breaking old configs."""
    normalized: dict[str, dict[str, Any]] = {}
    for name, raw_spec in schema.items():
        spec = dict(raw_spec)
        ptype = spec.get("type", "number")
        spec.setdefault("name", name)
        spec.setdefault("required", False)
        spec.setdefault("example", spec.get("default"))
        if ptype == "int":
            spec.setdefault("min", 1)
            spec.setdefault("max", 3650)
        elif ptype in {"float", "number"}:
            spec.setdefault("min", 0.0)
            spec.setdefault("max", 10000.0)
        adjustment_unit, inference = _infer_adjustment_unit(name, spec, ptype)
        if ptype in _NUMERIC_TYPES:
            spec.setdefault("adjustment_unit", adjustment_unit)
            spec.setdefault("ui_precision", _decimal_places(spec["adjustment_unit"]))
            spec.setdefault("step", spec["adjustment_unit"])
            spec["adjustment_unit_inference"] = inference
        else:
            spec.setdefault("adjustment_unit", None)
            spec.setdefault("ui_precision", None)
            spec["adjustment_unit_inference"] = "not_applicable"
        inferred = infer_unit_metadata(name)
        if "unit" not in spec:
            spec.update(inferred)
            spec["unit_inference"] = (
                "auto" if inferred["unit"] is not None else "manual_required"
            )
        elif inferred["unit"] is not None and spec["unit"] != inferred["unit"]:
            spec.setdefault("unit_display", None)
            spec.setdefault("scale_hint", None)
            spec["unit_inference"] = "conflict"
            spec["unit_inference_expected"] = inferred["unit"]
        else:
            spec.setdefault("unit_display", inferred["unit_display"])
            spec.setdefault("scale_hint", inferred["scale_hint"])
            spec.setdefault("unit_inference", "declared")
        spec.setdefault("physical_unit", spec.get("unit"))
        spec.setdefault("description", f"参数 {name}")
        if ptype in _NUMERIC_TYPES:
            validate_param_spec(name, spec)
        normalized[name] = spec
    return normalized


def factor_params(**defaults: Any) -> dict[str, dict[str, Any]]:
    """Build complete, UI-compatible schemas for standard factor parameters.

    The generated records retain the registry's mapping shape
    ``parameter_name -> specification`` while carrying explicit name, type,
    default, required, bounds, step, Chinese description, and example fields.
    """
    schema: dict[str, dict[str, Any]] = {}
    for name, default in defaults.items():
        template = _PARAMETER_TEMPLATES.get(name)
        if template is None:
            raise ValueError(f"No parameter schema template for {name!r}")
        schema[name] = {
            "name": name,
            **template,
            "default": default,
            "required": False,
            "example": default,
            "unit_inference": "declared",
        }
    return schema
