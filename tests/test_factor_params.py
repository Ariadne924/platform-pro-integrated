from types import SimpleNamespace

import pytest

from superplatform_web.factor_params import normalize_factor_params


def test_normalize_factor_params_merges_and_validates_declared_types():
    factor = SimpleNamespace(params_schema={
        "period": {"type": "int", "default": 10, "min": 1, "max": 30},
        "threshold": {"type": "float", "default": 0.5, "min": 0.0, "max": 1.0},
        "enabled": {"type": "bool", "default": True},
        "mode": {"type": "str", "default": "fast", "enum": ["fast", "slow"]},
    })

    assert normalize_factor_params(
        factor,
        configured_params={"period": 15, "legacy_param": "kept"},
        requested_params={"threshold": 1, "enabled": False, "mode": "slow"},
    ) == {
        "period": 15,
        "threshold": 1.0,
        "enabled": False,
        "mode": "slow",
        "legacy_param": "kept",
    }


def test_normalize_factor_params_accepts_decimal_aligned_values():
    factor = SimpleNamespace(params_schema={
        "gaussian_sigma": {
            "type": "float",
            "default": 1.0,
            "min": 0.01,
            "max": 3.0,
            "adjustment_unit": 0.01,
            "description": "高斯平滑强度",
        },
    })

    params = normalize_factor_params(factor, requested_params={"gaussian_sigma": 0.3})

    assert params["gaussian_sigma"] == 0.3


def test_normalize_factor_params_rejects_unaligned_value_with_nearest_suggestion():
    factor = SimpleNamespace(params_schema={
        "threshold_pct": {
            "type": "float",
            "default": 0.05,
            "min": 0.0,
            "max": 0.2,
            "adjustment_unit": 0.005,
            "description": "阈值",
        },
    })

    with pytest.raises(ValueError, match=r"adjustment_unit=0.005.*0.015.*0.02"):
        normalize_factor_params(factor, requested_params={"threshold_pct": 0.017})


@pytest.mark.parametrize("params", [
    {"period": True},
    {"threshold": "0.5"},
    {"enabled": 1},
    {"mode": "turbo"},
    {"unknownParam": 1},
])
def test_normalize_factor_params_rejects_invalid_or_unknown_request_params(params):
    factor = SimpleNamespace(params_schema={
        "period": {"type": "int", "default": 10},
        "threshold": {"type": "float", "default": 0.5},
        "enabled": {"type": "bool", "default": True},
        "mode": {"type": "str", "default": "fast", "enum": ["fast", "slow"]},
    })

    with pytest.raises(ValueError):
        normalize_factor_params(factor, requested_params=params)
