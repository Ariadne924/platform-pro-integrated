"""Configuration read/write endpoints.

The Settings page is driven entirely by the dynamic schema generated from
the config value tree + YAML comments (see ``superplatform_web.config_schema``)
— adding a key to ``default.yaml`` makes it editable on the page with zero
backend code. Changes are persisted to ``config/settings.yaml``, a runtime
overlay that deep-merges over the base files on every load.
"""

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

import superplatform_web.state as _state
from superplatform_web.config_schema import build_schema, flatten_values

router = APIRouter(prefix="/api/config", tags=["config"])

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # routes/config.py → project root


def _settings_path() -> Path:
    """Path of the runtime settings overlay (last entry of the config files)."""
    last = _state._CONFIG_FILES[-1]
    return Path(last) if last else _PROJECT_ROOT / "config" / "settings.yaml"

# Keys that require re-building the provider registry when changed.
_PROVIDER_AFFECTING_PREFIXES = ("data.cache.", "exchanges.binance.proxy", "exchanges.binance.enabled")


def _symbol_suggestions() -> list[str]:
    """Common perpetual symbols + configured defaults for the search box."""
    configured = _state.config.get("data.symbols.perpetual") or []
    majors = [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    ]
    seen = set(configured)
    return configured + [s for s in majors if s not in seen]


def _exchange_summary(config) -> list[dict]:
    exchanges = config.get("exchanges") or {}
    result = []
    for name, cfg in exchanges.items():
        result.append({
            "name": name,
            "enabled": cfg.get("enabled", False),
            "proxy": cfg.get("proxy", ""),
            "default_market_type": cfg.get("default_market_type", "perpetual"),
        })
    return result


def _current_values(schema: dict) -> dict:
    return {field["key"]: _state.config.get(field["key"]) for field in schema["fields"]}


def _field_map(schema: dict) -> dict[str, dict]:
    return {field["key"]: field for field in schema["fields"]}


def _validate(field: dict, value) -> None:
    if not field.get("editable", True):
        raise ValueError(f"'{field['key']}' 是锁定字段，不能在 Web 端修改")

    vtype = field["type"]
    if vtype == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"'{field['key']}' 需要布尔值")
    elif vtype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"'{field['key']}' 需要整数")
        if "min" in field and value < field["min"]:
            raise ValueError(f"'{field['key']}' 不能小于 {field['min']}")
        if "max" in field and value > field["max"]:
            raise ValueError(f"'{field['key']}' 不能大于 {field['max']}")
    elif vtype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"'{field['key']}' 需要数字")
    elif vtype == "str":
        if not isinstance(value, str):
            raise ValueError(f"'{field['key']}' 需要字符串")
        enum = field.get("enum")
        if enum is not None and value not in enum:
            raise ValueError(f"'{field['key']}' 必须是 {enum} 之一")
    elif vtype == "date":
        if not isinstance(value, str):
            raise ValueError(f"'{field['key']}' 需要日期字符串")
        try:
            from datetime import datetime
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"'{field['key']}' 格式应为 YYYY-MM-DD") from exc
    elif vtype == "list":
        if not isinstance(value, list) or not all(isinstance(x, (str, int, float)) for x in value):
            raise ValueError(f"'{field['key']}' 需要标量列表")


def _set_nested(overlay: dict, key: str, value) -> None:
    """Write a dotted key into a nested dict, e.g. 'a.b.c' → overlay[a][b][c]."""
    parts = key.split(".")
    node = overlay
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _write_settings(overlay: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _load_current_settings() -> dict:
    path = _settings_path()
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {}


def load_settings_overlay() -> dict:
    """Public alias for other routers that also persist to the overlay."""
    return _load_current_settings()


def write_settings_overlay(overlay: dict) -> None:
    """Public alias for other routers that also persist to the overlay."""
    _write_settings(overlay)


# ── Dynamic schema ───────────────────────────────────────────────────


@router.get("/schema")
async def get_config_schema():
    """Return the full dynamic schema (flat fields + nested sections)."""
    schema = build_schema(_state.config)
    return {"schema": schema}


# ── Values ──────────────────────────────────────────────────────────


@router.get("/values")
async def get_config_values():
    """Return current values, base-file defaults, and the settings overlay."""
    schema = build_schema(_state.config)
    base = flatten_values(_state.base_config().to_dict())
    overlay = _load_current_settings()
    return {
        "values": _current_values(schema),
        "defaults": base,
        "settings": flatten_values(overlay) if overlay else {},
    }


@router.put("/values")
async def put_config_values(payload: dict):
    """Validate and apply a batch of setting changes.

    Body: { "evaluation.cost.maker_fee_bps": 3.0, ... }
    Every key must exist in the schema and be editable.
    """
    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再修改配置")

    schema = build_schema(_state.config)
    field_map = _field_map(schema)
    overlay = _load_current_settings()
    provider_affected = False

    for key, value in payload.items():
        field = field_map.get(key)
        if field is None:
            raise HTTPException(status_code=422, detail=f"未知配置项: {key}")
        try:
            _validate(field, value)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        _set_nested(overlay, key, value)
        if key.startswith(_PROVIDER_AFFECTING_PREFIXES):
            provider_affected = True

    _write_settings(overlay)
    _state.reload_config()
    if provider_affected:
        _state.reapply_providers()
    return {"values": _current_values(schema)}


@router.delete("/values")
async def reset_config_values():
    """Delete the runtime settings overlay and restore base-file defaults."""
    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再重置配置")

    path = _settings_path()
    if path.exists():
        path.unlink()
    _state.reload_config()
    _state.reapply_providers()
    schema = build_schema(_state.config)
    return {"values": _current_values(schema)}


@router.post("/reload")
async def reload_config_values():
    """Reload config from the YAML files (edits take effect without restart).

    Reads the base files + settings overlay back into the shared Config in
    place. Provider registry is left untouched — data-source changes still go
    through the Settings page / provider endpoints.
    """
    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再重新加载配置")

    _state.reload_config()
    schema = build_schema(_state.config)
    return {"values": _current_values(schema)}


# ── Combined view (backward compatible) ──────────────────────────────


@router.get("")
async def get_config():
    """Return relevant config sections, editable schema, and current values.

    Kept for backward compatibility; new code should prefer
    ``GET /api/config/schema`` + ``GET /api/config/values``.
    """
    config = _state.config
    schema = build_schema(config)
    return {
        "evaluation": {
            "sample_start": config.get("evaluation.sample_start"),
            "sample_end": config.get("evaluation.sample_end"),
            "oos_start": config.get("evaluation.oos_start"),
            "oos_end": config.get("evaluation.oos_end"),
            "layers": config.get("evaluation.layers", 5),
        },
        "data": {
            "symbols": config.get("data.symbols"),
            "frequencies": config.get("data.frequencies"),
        },
        "exchanges": _exchange_summary(config),
        "symbol_suggestions": _symbol_suggestions(),
        "schema": schema,
        "values": _current_values(schema),
    }


@router.put("")
async def put_config(payload: dict):
    """Alias of ``PUT /api/config/values`` (backward compatible)."""
    return await put_config_values(payload)


@router.delete("")
async def reset_config():
    """Alias of ``DELETE /api/config/values`` (backward compatible)."""
    return await reset_config_values()
