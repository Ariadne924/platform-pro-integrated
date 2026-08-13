"""Request-time factor parameter normalization and validation."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from superplatform.factors.param_schema import (
    infer_unit_metadata,
    is_adjustment_aligned,
    nearest_adjustment_values,
    normalize_params_schema,
    validate_param_spec,
)


def validate_parameter_units(
    factor,
    params: dict[str, Any] | None,
    param_units: dict[str, str | None] | None,
) -> None:
    """Reject request units that disagree with the declared factor schema."""
    if param_units is None:
        return
    if not isinstance(param_units, dict):
        raise ValueError("param_units 必须是对象")
    params = params or {}
    schema = normalize_params_schema(factor.params_schema or {})
    for name, supplied_unit in param_units.items():
        if name not in params:
            raise ValueError(f"参数 {name} 提供了单位但没有对应参数值")
        if name not in schema:
            raise ValueError(f"未知因子参数 {name}，无法校验单位")
        if supplied_unit is not None and not isinstance(supplied_unit, str):
            raise ValueError(f"参数 {name} 的单位必须是字符串或 null")
        expected_unit = schema[name].get("unit")
        if expected_unit != supplied_unit:
            expected = expected_unit if expected_unit is not None else "无量纲"
            actual = supplied_unit if supplied_unit is not None else "无量纲"
            hint = schema[name].get("scale_hint") or infer_unit_metadata(name)["scale_hint"]
            suffix = f"；数值尺度应为 {hint}" if hint else ""
            raise ValueError(
                f"参数 {name} 单位冲突：schema 期望 {expected}，请求提供 {actual}{suffix}"
            )


def _validate_value(name: str, value: Any, spec: dict[str, Any]) -> Any:
    ptype = spec.get("type", "number")
    if ptype == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"参数 {name} 需要类型 int")
        normalized = value
    elif ptype in {"float", "number"}:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"参数 {name} 需要类型 float")
        normalized = float(value) if ptype == "float" else value
    elif ptype == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"参数 {name} 需要类型 bool")
        normalized = value
    elif ptype in {"str", "string", "enum"}:
        if not isinstance(value, str):
            raise ValueError(f"参数 {name} 需要类型 {ptype}")
        normalized = value
    else:
        raise ValueError(f"参数 {name} 使用了不支持的类型 {ptype}")

    if isinstance(normalized, (int, float)) and not isinstance(normalized, bool):
        if not math.isfinite(normalized):
            raise ValueError(f"参数 {name} 必须是有限数值")
        if "min" in spec and normalized < spec["min"]:
            raise ValueError(f"参数 {name} 不能小于 {spec['min']}")
        if "max" in spec and normalized > spec["max"]:
            raise ValueError(f"参数 {name} 不能大于 {spec['max']}")
        step = spec.get("adjustment_unit")
        if step is not None and not is_adjustment_aligned(normalized, spec["min"], step):
            lower, upper = nearest_adjustment_values(
                normalized, spec["min"], spec["max"], step
            )
            raise ValueError(
                f"参数 {name}={normalized} 未按 adjustment_unit={step} 对齐，"
                f"建议使用 {lower} 或 {upper}"
            )
    if "enum" in spec and normalized not in spec["enum"]:
        raise ValueError(f"参数 {name} 必须是 {spec['enum']} 之一")
    return normalized


def normalize_factor_params(
    factor,
    configured_params: dict[str, Any] | None = None,
    requested_params: dict[str, Any] | None = None,
    param_units: dict[str, str | None] | None = None,
    *,
    reject_unknown_requested: bool = True,
) -> dict[str, Any]:
    """Resolve defaults/config/request precedence and validate declared keys."""
    configured = configured_params or {}
    requested = requested_params or {}
    if not isinstance(configured, dict):
        raise ValueError("factor params must be an object")
    if not isinstance(requested, dict):
        raise ValueError("请求 params 必须是对象")

    schema = normalize_params_schema(factor.params_schema or {})
    for name, spec in schema.items():
        validate_param_spec(name, spec)
    validate_parameter_units(factor, requested, param_units)
    if reject_unknown_requested:
        unknown = sorted(set(requested).difference(schema))
        if unknown:
            raise ValueError(f"未知因子参数 {unknown}，可用参数: {sorted(schema)}")

    effective: dict[str, Any] = {
        name: spec["default"]
        for name, spec in schema.items()
        if "default" in spec
    }
    effective.update(configured)
    effective.update(requested)
    for name, spec in schema.items():
        if name in effective:
            effective[name] = _validate_value(name, effective[name], spec)
    return effective


def parameter_hash(params: dict[str, Any]) -> str:
    """Stable hash used by result-cache keys and run identifiers."""
    payload = json.dumps(
        params,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_hash(payload: Any) -> str:
    """Stable hash for the complete evaluation identity."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
