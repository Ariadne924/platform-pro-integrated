"""Factor listing, configuration CRUD and evaluation endpoints."""

import asyncio
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from superplatform.data.enums import DataFrequency
from superplatform.factors.factor_groups import factor_groups, resolve_group
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.instances import FactorInstance
from superplatform.factors.param_schema import normalize_params_schema
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import factor_entry, resolve_factor
from superplatform_web import factor_config as fc
from superplatform_web.factor_params import normalize_factor_params
from superplatform_web.jobs import create_batch_job, get_batch_job, record_job_event
from superplatform_web.research import (
    batch_evaluate,
    evaluate_factor,
    factor_available_frequencies,
    run_factory_sweep,
)
from superplatform_web.state import (
    _DATA_TYPE_LABELS,
    default_exchange,
    default_market,
    provider_label,
)

router = APIRouter(prefix="/api/factors", tags=["factors"])

# Soft cap on factory-sweep grid points. Exceeding it still runs (no hard
# block) but the response carries a warning; the factory page also prompts
# before dispatching a grid above this size. Calibrated to the measured
# per-combo cost of the shared-fetch sweep.
SWEEP_SOFT_LIMIT = 150


class FactorEvaluationRequest(BaseModel):
    factor: str
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
    params: dict[str, Any] | None = None
    param_units: dict[str, str | None] | None = None
    frequency: str | None = None


class FactorExperimentRequest(FactorEvaluationRequest):
    oos_start: str
    oos_end: str


class FactorBatchRequest(BaseModel):
    factors: list[str] = Field(min_length=1)
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
    frequency: str | None = None


def _factor_info(
    factor,
    available_data_types: set[str],
    config,
    providers,
    exchange: str,
    market: str,
) -> dict:
    missing = [d for d in factor.required_data if d not in available_data_types]
    # Pull default params from the factor's config entry (factory or instance)
    # so the UI can pre-fill editable fields.
    factor_cfg = factor_entry(config, factor.name)
    available_frequencies = (
        factor_available_frequencies(factor, providers, exchange, market, config)
        if len(missing) == 0
        else []
    )
    is_instance = isinstance(factor, FactorInstance)
    info = {
        "name": factor.name,
        "kind": "instance" if is_instance else "factory",
        "category": factor.category.value,
        "description": factor.description,
        "required_data": factor.required_data,
        "required_symbols": factor.required_symbols,
        "available": len(missing) == 0,
        "missing_data_types": missing,
        "available_frequencies": available_frequencies,
        "params": factor_cfg.get("params", {}),
        "params_schema": factor.params_schema or {},
    }
    if is_instance:
        info["factory"] = factor.factory_name
        info["instance_params"] = factor.params
    return info


def _available_data_types(request: Request) -> set[str]:
    """Return the set of data types that have at least one registered provider."""
    registry = request.app.state.providers
    types: set[str] = set()
    for provider_id in registry.list_all():
        types.add(registry.get(provider_id).data_type)
    return types


@router.get("")
async def list_factors(request: Request):
    factory_registry = FactorRegistry.get_instance()
    instance_registry = FactorInstanceRegistry.get_instance()
    available = _available_data_types(request)
    config = request.app.state.config
    providers = request.app.state.providers
    exchange = default_exchange()
    market = default_market()
    names = sorted(
        set(factory_registry.list_all()) | set(instance_registry.list_all())
    )
    return [
        _factor_info(
            resolve_factor(n, factory_registry=factory_registry),
            available, config, providers, exchange, market,
        )
        for n in names
    ]


@router.post("/evaluate")
async def evaluate(data: FactorEvaluationRequest, request: Request):
    """Run one factor evaluation without persisting the result."""
    try:
        return await evaluate_factor(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            factor_name=data.factor,
            symbols=data.symbols,
            start=data.start,
            end=data.end,
            params=data.params,
            param_units=data.param_units,
            frequency=data.frequency,
            result_cache=request.app.state.evaluation_cache,
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/providers")
async def list_providers(request: Request):
    """List providers with human-readable labels."""
    registry = request.app.state.providers
    return [
        {
            "provider_id": pid,
            "label": provider_label(pid),
            "data_type": registry.get(pid).data_type,
            "market_type": registry.get(pid).market_type.value if registry.get(pid).market_type else None,
            "exchange": registry.get(pid).exchange,
        }
        for pid in registry.list_all()
    ]


def _group_payload(group, registry, available_data_types: set[str], *, deletable: bool = False) -> dict:
    """One configured factor group with resolved members + availability."""
    res = resolve_group(group, registry)
    unavailable = [
        name
        for name in res.factors
        if any(d not in available_data_types for d in registry.get(name).required_data)
    ]
    return {
        "name": res.name,
        "kind": res.kind,
        "description": res.description,
        "factors": res.factors,
        "count": len(res.factors),
        "available_count": len(res.factors) - len(unavailable),
        "unavailable": unavailable,
        "unknown": res.unknown,
        "deletable": deletable,
    }


def _all_group_payloads(request: Request) -> list[dict]:
    """Every configured factor group (preconfigured + user-saved) as payloads.

    ``deletable`` marks groups declared in ``config/user_groups.yaml`` — the
    only ones the frontend may remove. Malformed config → 422 with a message.
    """
    try:
        groups = factor_groups(request.app.state.config)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    registry = FactorRegistry.get_instance()
    available = _available_data_types(request)
    user_groups = set(_load_user_groups())
    return [
        _group_payload(g, registry, available, deletable=g.name in user_groups)
        for g in groups
    ]


@router.get("/groups")
async def list_factor_groups(request: Request):
    """Configured factor groups with resolved members and availability.

    A group is declared under ``factor_groups:`` in ``config/factors.yaml``
    (preconfigured; see ``superplatform.factors.factor_groups`` for the ``kind``
    dispatch) or ``config/user_groups.yaml`` (user-saved via POST /groups).
    Members are resolved against the registry; malformed config → 422.
    """
    return _all_group_payloads(request)


# ── User factor groups (config/user_groups.yaml) ─────────────────────


def _user_groups_path() -> Path:
    """Path to the user-saved factor groups file (gitignored, machine-local).

    Derived from ``_CONFIG_FILES`` (which carries the same basename) so tests
    that isolate config to a temp dir redirect this file there too.
    """
    import superplatform_web.state as _state

    for entry in _state._CONFIG_FILES:
        path = Path(entry)
        if path.name == "user_groups.yaml":
            return path
    return _state._PROJECT_ROOT / "config" / "user_groups.yaml"


def _load_user_groups() -> dict:
    """The ``factor_groups:`` section of user_groups.yaml ({} when absent)."""
    path = _user_groups_path()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("factor_groups") or {}
    return raw if isinstance(raw, dict) else {}


def _save_user_groups(groups: dict) -> None:
    """Write the ``factor_groups:`` section back to user_groups.yaml.

    Other keys in the file are preserved; only user groups live here (the
    preconfigured ones stay in factors.yaml). Mirrors ``_write_settings``.
    """
    path = _user_groups_path()
    data: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data["factor_groups"] = groups
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


class FactorGroupSaveRequest(BaseModel):
    name: str
    factors: list[str] = Field(min_length=1)


@router.post("/groups")
async def save_factor_group(data: FactorGroupSaveRequest, request: Request):
    """Save the current selection as a user factor group in user_groups.yaml.

    The group is stored with ``kind: list`` so it flows through the same
    kind-dispatched resolution/availability pipeline as preconfigured groups.
    Names matching a preconfigured group are rejected (409); re-saving an
    existing user group overwrites it. Returns all groups, updated.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再保存分组")

    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="分组名不能为空")

    registry = FactorRegistry.get_instance()
    known = set(registry.list_all())
    unknown = sorted(set(data.factors) - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知因子：{'、'.join(unknown)}")
    factors = list(dict.fromkeys(data.factors))  # order-preserving dedup

    user_groups = _load_user_groups()
    if name not in user_groups:
        # Merged config carries both preconfigured and user groups; a name that
        # exists there but is NOT a user group must be a preconfigured one.
        try:
            merged = {g.name for g in factor_groups(request.app.state.config)}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if name in merged:
            raise HTTPException(status_code=409, detail=f"「{name}」是预配置因子组，不能覆盖")

    user_groups[name] = {"kind": "list", "description": "用户保存", "factors": factors}
    _save_user_groups(user_groups)
    _state.reload_config()
    return _all_group_payloads(request)


@router.delete("/groups/{name}")
async def delete_factor_group(name: str, request: Request):
    """Remove a user-saved factor group from user_groups.yaml.

    Preconfigured groups (declared in factors.yaml) are never deletable → 404.
    Returns all groups, updated.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再删除分组")

    user_groups = _load_user_groups()
    if name not in user_groups:
        raise HTTPException(status_code=404, detail=f"「{name}」不是用户保存的分组")

    del user_groups[name]
    _save_user_groups(user_groups)
    _state.reload_config()
    return _all_group_payloads(request)


def _format_speed(rows: int, nbytes: int | float, elapsed: float) -> str:
    """Human-readable download speed, e.g. '1200 行 · 96.0 KB/s'."""
    if elapsed <= 0:
        return ""
    parts = [f"{rows / elapsed:.0f} 行/s"] if rows else []
    if nbytes > 0:
        bps = nbytes / elapsed
        if bps >= 1024**3:
            parts.append(f"{bps / 1024**3:.2f} GB/s")
        elif bps >= 1024**2:
            parts.append(f"{bps / 1024**2:.2f} MB/s")
        elif bps >= 1024:
            parts.append(f"{bps / 1024:.1f} KB/s")
        else:
            parts.append(f"{bps:.0f} B/s")
    return " · ".join(parts)


def _format_progress_event(event: dict) -> str:
    """Turn a structured pipeline event into a human-readable Chinese line."""
    kind = event.get("kind", "")
    factor = event.get("factor", "")
    freq = event.get("frequency", "")
    symbol = event.get("symbol", "")
    data_type = _DATA_TYPE_LABELS.get(event.get("data_type", ""), event.get("data_type", ""))
    if kind == "batch_start":
        return f"开始批量评估（{event.get('factor_count', 0)} 个因子）"
    if kind == "factor_start":
        return f"评估因子：{factor}"
    if kind == "fetch_start":
        return f"拉取 {symbol} {data_type}（{freq}）"
    if kind == "fetch_pending":
        elapsed = event.get("elapsed", 0)
        return f"拉取 {symbol} {data_type}（{freq}）… 已用 {elapsed:.0f}s"
    if kind == "fetch_done":
        rows = event.get("rows", 0)
        elapsed = event.get("elapsed")
        suffix = ""
        if isinstance(elapsed, (int, float)) and elapsed > 0:
            nbytes = event.get("bytes", 0)
            speed = _format_speed(rows, nbytes, elapsed)
            suffix = f" · {elapsed:.1f}s · {speed}"
        return f"已拉取 {symbol} {data_type}（{freq}）· {rows} 行{suffix}"
    if kind == "compute":
        return f"计算 {factor}（{event.get('group', '')}）因子值"
    if kind == "cross_section":
        return f"构建截面：{factor}"
    if kind == "metrics":
        return f"计算评估指标：{factor}"
    if kind == "forward_bias":
        return f"前视偏差检查：{factor}（{event.get('group', '')}）{event.get('i', '')}/{event.get('n', '')}"
    if kind == "factor_done":
        return f"{factor} 完成（ICIR={event.get('icir', '')}）"
    if kind == "correlation":
        return "计算因子相关矩阵"
    if kind == "serialize":
        return f"序列化结果：{factor}"
    if kind == "batch_done":
        return "批量评估完成"
    return str(event)


async def _run_batch_job(job, data: FactorBatchRequest, request: Request) -> None:
    """Run a batch evaluation in the background, recording progress events."""
    try:
        def _emit(event: dict) -> None:
            record_job_event(
                job,
                event.get("kind", ""),
                _format_progress_event(event),
                payload=event,
            )

        result = await batch_evaluate(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            factor_names=data.factors,
            symbols=data.symbols,
            start=data.start,
            end=data.end,
            frequency=data.frequency,
            progress=_emit,
        )
        job.result = result
        job.status = "done"
    except Exception as error:  # surface any failure to the status endpoint
        job.error = str(error)
        job.status = "error"


@router.post("/batch")
async def batch_evaluate_factors(data: FactorBatchRequest, request: Request):
    """Start a batch evaluation as a background job; poll /batch/{job_id} for progress."""
    job = create_batch_job()
    asyncio.create_task(_run_batch_job(job, data, request))
    return {"job_id": job.job_id}


@router.get("/batch/{job_id}")
async def batch_job_status(job_id: str):
    """Progress events and (when finished) the result of a batch job."""
    job = get_batch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"未知 batch job: {job_id}")
    payload: dict[str, Any] = {
        "status": job.status,
        "events": job.events,
        # Monotonic aggregate of completed fetches — survives event-ring eviction.
        "download": job.download,
    }
    if job.result is not None:
        payload["result"] = job.result
    if job.error is not None:
        payload["error"] = job.error
    return payload


@router.post("/experiments")
async def create_experiment(data: FactorExperimentRequest, request: Request):
    """Run and persist matched in-sample and out-of-sample factor evaluations."""
    common = {
        "factor_name": data.factor,
        "symbols": data.symbols,
        "start": data.start,
        "end": data.end,
        "params": data.params,
        "param_units": data.param_units,
        "frequency": data.frequency,
    }
    try:
        in_sample = await evaluate_factor(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            **common,
        )
        out_of_sample = await evaluate_factor(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            **{**common, "end": data.oos_end, "start": data.oos_start},
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    result = {"in_sample": in_sample, "out_of_sample": out_of_sample}
    experiment_id = request.app.state.experiments.save(data.model_dump(), result)
    return {"experiment_id": experiment_id, **result}


@router.get("/experiments")
async def list_experiments(request: Request, limit: int = Query(default=50, ge=1, le=200)):
    return request.app.state.experiments.list(limit)


@router.get("/experiments/{experiment_id}")
async def get_experiment(experiment_id: str, request: Request):
    experiment = request.app.state.experiments.get(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail=f"Unknown experiment: {experiment_id}")
    return experiment


# ── Factor configuration CRUD (factors.yaml) ─────────────────────────


def _map_param_type(t: str) -> str:
    """Map a params_schema type onto a config-schema leaf type."""
    return {
        "int": "int",
        "float": "number",
        "number": "number",
        "str": "str",
        "string": "str",
        "enum": "str",
        "bool": "bool",
    }.get(t, "number")


def _factor_config_schema(factor, cfg: dict, request: Request) -> list[dict]:
    """Editable-field schema for one factor, driving the config form.

    The vocabulary matches the Settings config schema (``children`` /
    ``editable`` / ``default`` / ``min`` / ``max`` / ``enum``) so the frontend
    can render it with the same FormNode/FormField components. Every leaf is
    editable (unlike the global config, factor entries carry no locked keys).

    The ``frequency`` enums are restricted to the cadences the factor can
    natively serve (``available_frequencies``), so the config UI never offers
    a cadence that would fail at evaluation time. A saved non-native value is
    kept selectable so existing configs round-trip without being dropped.
    """
    registry = request.app.state.providers
    native = factor_available_frequencies(
        factor, registry, default_exchange(), default_market(), request.app.state.config
    ) or [f.value for f in DataFrequency]
    frequencies = list(native)
    for saved in (cfg.get("frequency"), (cfg.get("evaluation_price") or {}).get("frequency")):
        if saved and saved not in frequencies:
            frequencies.append(saved)

    def _provider_enum(data_type: str) -> list[str]:
        return [pid for pid in registry.list_by_data_type(data_type)]

    def _leaf(key: str, label: str, ftype: str, value: Any, *, description: str = "",
              enum: list[str] | None = None, min: int | float | None = None,
              max: int | float | None = None, adjustment_unit: int | float | None = None,
              physical_unit: str | None = None, ui_precision: int | None = None) -> dict:
        node: dict[str, Any] = {
            "key": key,
            "label": label,
            "type": ftype,
            "editable": True,
            "default": value,
        }
        if description:
            node["description"] = description
        if enum:
            node["enum"] = enum
        if min is not None:
            node["min"] = min
        if max is not None:
            node["max"] = max
        if adjustment_unit is not None:
            node["adjustment_unit"] = adjustment_unit
            # Existing config controls read ``step``; retain it as a
            # compatibility mirror while new clients consume adjustment_unit.
            node["step"] = adjustment_unit
        if physical_unit is not None:
            node["physical_unit"] = physical_unit
        if ui_precision is not None:
            node["ui_precision"] = ui_precision
        return node

    # Params come from the factor's declared params_schema when available;
    # unknown YAML-only params still show up (typed number) for old configs.
    params_schema = factor.params_schema or {}
    yaml_params = cfg.get("params") or {}
    param_fields: list[dict] = []
    for pk in [*params_schema.keys(), *yaml_params.keys()]:
        if any(f["key"] == f"params.{pk}" for f in param_fields):
            continue
        spec = params_schema.get(pk, {})
        default = spec.get("default", yaml_params.get(pk))
        param_fields.append(_leaf(
            f"params.{pk}", pk, _map_param_type(spec.get("type", "number")), default,
            description=spec.get("description", ""),
            enum=spec.get("enum"),
            min=spec.get("min"),
            max=spec.get("max"),
            adjustment_unit=spec.get("adjustment_unit", spec.get("step")),
            physical_unit=spec.get("physical_unit", spec.get("unit")),
            ui_precision=spec.get("ui_precision"),
        ))

    fields: list[dict] = [
        _leaf("symbols", "标的列表", "list", cfg.get("symbols"),
              description="因子计算覆盖的标的；[[A,B],[C,D]] 表示按组计算"),
        _leaf("frequency", "频率", "str", cfg.get("frequency"), enum=frequencies,
              description="数据采样频率"),
        {
            "key": "providers",
            "label": "数据源映射",
            "type": "object",
            "children": [
                _leaf(f"providers.{dt}", dt, "str", (cfg.get("providers") or {}).get(dt),
                      enum=_provider_enum(dt))
                for dt in factor.required_data
            ],
        },
        {
            "key": "evaluation_price",
            "label": "评估价格",
            "type": "object",
            "children": [
                _leaf("evaluation_price.provider", "provider", "str",
                      (cfg.get("evaluation_price") or {}).get("provider"),
                      enum=_provider_enum("kline")),
                _leaf("evaluation_price.frequency", "frequency", "str",
                      (cfg.get("evaluation_price") or {}).get("frequency"), enum=frequencies),
            ],
        },
        {
            "key": "params",
            "label": "参数",
            "type": "object",
            "children": param_fields,
        },
    ]
    return [f for f in fields if f["type"] != "object" or f["children"]]


@router.post("/refresh")
async def refresh_factors():
    """Re-discover factors from disk so code edits load without a restart.

    ``reload()`` purges the ``sys.modules`` cache for the defs tree and the
    registry's own factor/instance caches, then re-imports every def module —
    new files appear, edited code takes effect, removed files drop out.
    """
    import superplatform_web.state as _state

    result = FactorRegistry.get_instance().reload()
    FactorInstanceRegistry.get_instance().build_from_config(
        _state.config, FactorRegistry.get_instance()
    )
    _state.evaluation_cache.clear()
    return result


# ── Factor instances (fixed-parameter layer) ──────────────────────────


class FactorInstanceCreateRequest(BaseModel):
    name: str
    factory: str
    params: dict[str, Any]
    description: str | None = None


@router.post("/instances")
async def create_factor_instance(data: FactorInstanceCreateRequest, request: Request):
    """Create a fixed-parameter factor instance from a factory + preset.

    The instance entry inherits the factory's symbols/frequency, so a saved
    preset is immediately evaluable and selectable in the factor layer.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再创建实例")

    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="实例名不能为空")
    if name in FactorRegistry.get_instance().list_all():
        raise HTTPException(status_code=422, detail=f"「{name}」已是工厂因子名，实例名不能与之重复")

    try:
        factory = FactorRegistry.get_instance().get(data.factory)
    except KeyError as error:
        raise HTTPException(status_code=422, detail=f"未知工厂因子: {data.factory}") from error

    try:
        effective = normalize_factor_params(factory, None, data.params, None)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    factory_cfg = fc.get_factor_config(data.factory)
    entry: dict[str, Any] = {
        "factory": data.factory,
        "params": effective,
        "description": data.description or "",
    }
    if isinstance(factory_cfg.get("symbols"), list):
        entry["symbols"] = factory_cfg["symbols"]
    if isinstance(factory_cfg.get("frequency"), str):
        entry["frequency"] = factory_cfg["frequency"]

    fc.set_instance_config(name, entry)
    _state.reload_config()
    FactorInstanceRegistry.get_instance().build_from_config(_state.config, FactorRegistry.get_instance())
    return {"name": name, "config": entry}


@router.delete("/instances/{name}")
async def delete_factor_instance(name: str, request: Request):
    """Remove a factor instance from ``factor_instances:``."""
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再删除实例")
    if not FactorInstanceRegistry.get_instance().has(name):
        raise HTTPException(status_code=404, detail=f"「{name}」不是已配置的实例")

    removed = fc.remove_instance_config(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"factors.yaml 中没有实例 {name}")
    _state.reload_config()
    FactorInstanceRegistry.get_instance().build_from_config(_state.config, FactorRegistry.get_instance())
    return {"name": name, "removed": True}


# ── Factory parameter sweep (factory-layer exploration) ───────────────


class FactorSweepRequest(BaseModel):
    factory: str
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
    frequency: str | None = None
    # [{param, from, to, step}] — 1–2 axes; values must align to the schema's
    # adjustment_unit (enforced by normalize_factor_params at evaluation time).
    sweep: list[dict[str, Any]] = Field(min_length=1, max_length=2)
    fixed: dict[str, Any] | None = None


@router.post("/sweep")
async def sweep_factory(data: FactorSweepRequest, request: Request):
    """Evaluate a factory factor across a parameter grid and report metrics.

    Returns ``{param_names, results: [{params, metrics}]}`` where ``metrics``
    is the factor's ``ic_stats`` (icir / mean_ic / std_ic / ...). Supports one
    axis (1D curve) or two axes (2D heatmap).
    """
    try:
        factory = FactorRegistry.get_instance().get(data.factory)
    except KeyError as error:
        raise HTTPException(status_code=422, detail=f"未知工厂因子: {data.factory}") from error

    schema = normalize_params_schema(factory.params_schema or {})
    axes: list[tuple[str, list[float | int]]] = []
    for spec in data.sweep:
        if not isinstance(spec, dict):
            raise HTTPException(status_code=422, detail="sweep 项需要是对象")
        param = spec.get("param")
        if param not in schema:
            raise HTTPException(
                status_code=422,
                detail=f"参数 {param} 不在工厂 {data.factory} 的 schema 中",
            )
        lo = spec.get("from")
        hi = spec.get("to")
        step = spec.get("step")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lo, hi, step)):
            raise HTTPException(status_code=422, detail=f"sweep 参数 {param} 需要 from/to/step 数值")
        if step <= 0 or lo > hi:
            raise HTTPException(status_code=422, detail=f"sweep 参数 {param} 区间非法")
        values = [lo + i * step for i in range(int((hi - lo) / step) + 1)]
        values = [v for v in values if v <= hi]
        axes.append((param, values))

    param_names = [a for a, _ in axes]
    import itertools

    combos = []
    for combo in itertools.product(*(vals for _, vals in axes)):
        params = dict(zip(param_names, combo, strict=True))
        params.update(data.fixed or {})
        combos.append(params)

    try:
        payload = await run_factory_sweep(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            factory_name=data.factory,
            symbols=data.symbols,
            start=data.start,
            end=data.end,
            frequency=data.frequency,
            combos=combos,
        )
    except (ValueError, KeyError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if payload["combos"] > SWEEP_SOFT_LIMIT:
        payload["warning"] = (
            f"网格共 {payload['combos']} 个组合，超过软限制 {SWEEP_SOFT_LIMIT}。"
            f"本次耗时约 {payload['elapsed_ms'] / 1000:.0f}s"
            f"（每组合 {payload['ms_per_combo']}ms）。建议收窄范围。"
        )
    return payload


@router.get("/{name}/config")
async def get_factor_config(name: str, request: Request):
    """Full config entry for one factor + the schema of its editable fields."""
    try:
        factor = resolve_factor(name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"未知因子: {name}") from error

    cfg = fc.get_factor_config(name)
    return {
        "name": name,
        "config": cfg,
        "schema": _factor_config_schema(factor, cfg, request),
    }


def _validate_params(factor, params: dict) -> None:
    """Type/bounds-check request params against the factor's params_schema.

    Existing YAML-only keys stay compatible, while every declared parameter is
    checked using the same parser as a runtime evaluation request.
    """
    try:
        normalize_factor_params(
            factor,
            configured_params={},
            requested_params=params,
            reject_unknown_requested=False,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/{name}/config")
async def put_factor_config(name: str, payload: dict, request: Request):
    """Write one factor's config back to ``factors.yaml``.

    Body is a nested dict of the fields the frontend edited, e.g.
    ``{"symbols": [...], "providers": {...}, "params": {...}}``.
    Each top-level key replaces the existing value wholesale.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再修改因子配置")

    try:
        factor = resolve_factor(name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"未知因子: {name}") from error

    # Validate types of the top-level fields we know how to interpret.
    for field in ("symbols",):
        if field in payload and not isinstance(payload[field], list):
            raise HTTPException(status_code=422, detail=f"'{field}' 需要列表")
    for field in ("providers", "params", "evaluation_price"):
        if field in payload and not isinstance(payload[field], dict):
            raise HTTPException(status_code=422, detail=f"'{field}' 需要对象")

    if "params" in payload:
        _validate_params(factor, payload["params"])

    cfg = fc.set_factor_config(name, payload)
    _state.reload_config()
    return {"name": name, "config": cfg}


@router.delete("/{name}")
async def delete_factor_config(name: str, request: Request):
    """Remove one factor's entry from ``factors.yaml``.

    The ``@factor`` implementation file is untouched — the factor stays
    registered but falls back to code defaults until re-added to config.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再修改因子配置")

    removed = fc.remove_factor_config(name)
    if not removed:
        removed = fc.remove_instance_config(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"factors.yaml 中没有 {name} 的配置")
    _state.reload_config()
    if FactorInstanceRegistry.get_instance().has(name):
        FactorInstanceRegistry.get_instance().build_from_config(
            _state.config, FactorRegistry.get_instance()
        )
    return {"name": name, "removed": True}
