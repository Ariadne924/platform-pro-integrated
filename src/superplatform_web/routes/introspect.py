"""Introspection endpoints — what capabilities does superplatform expose?

These endpoints let the frontend discover, at runtime, which data types,
frequencies, exchanges, evaluation steps and visualization outputs are
available. The evaluation/visualization manifests are extracted from the
actual module signatures + docstrings, so adding a metric to the evaluation
layer shows up here without any web-side change.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, Request

import superplatform.evaluation as evaluation_pkg
from superplatform.data.enums import DataFrequency, MarketType
from superplatform.data.schema import DataSchema
from superplatform_web.state import exchange_label, provider_label

router = APIRouter(prefix="/api/introspect", tags=["introspect"])

# numpy dtype → human/JSON friendly name
_DTYPE_NAMES = {
    "datetime64": "datetime64[ns, UTC]",
    "float64": "float64",
    "int64": "int64",
    "bool": "bool",
    "str": "str",
}


def _dtype_name(dtype) -> str:
    name = str(dtype)
    for numpy_name, label in _DTYPE_NAMES.items():
        if numpy_name in name:
            return label
    return name


def _schema_classes() -> list[tuple[str, type[DataSchema]]]:
    """All DataSchema subclasses exported by superplatform.data.schema."""
    import superplatform.data.schema as schema_module

    result = []
    for name in dir(schema_module):
        obj = getattr(schema_module, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, DataSchema)
            and obj is not DataSchema
        ):
            result.append((name, obj))
    return result


# Schema class name → canonical data_type id (used by providers / factors).
_SCHEMA_DATA_TYPES = {
    "KLineSchema": "kline",
    "TradeSchema": "trade",
    "OrderBookSchema": "order_book",
    "FundingRateSchema": "funding_rate",
    "OpenInterestSchema": "open_interest",
    "BasisSchema": "basis",
}


def _capability(name: str, fn) -> dict:
    """One manifest entry from a function/class object."""
    kind = "class" if inspect.isclass(fn) else "function"
    doc = inspect.getdoc(fn) or ""
    entry: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "doc": doc.split("\n\n")[0] if doc else "",
    }
    if kind == "function":
        try:
            params = inspect.signature(fn).parameters
            entry["params"] = [
                {
                    "name": p,
                    "default": (
                        None
                        if param.default is inspect.Parameter.empty
                        else repr(param.default)
                    ),
                    "required": param.default is inspect.Parameter.empty
                    and param.kind not in (inspect.Parameter.VAR_KEYWORD,),
                }
                for p, param in params.items()
                if p not in ("self", "cls")
            ]
        except (TypeError, ValueError):
            entry["params"] = []
    return entry


@router.get("/data-types")
async def introspect_data_types():
    """Every known data_type and its column schema (name → dtype)."""
    schemas = []
    for name, cls in _schema_classes():
        data_type = _SCHEMA_DATA_TYPES.get(name, name.removesuffix("Schema").lower())
        schemas.append({
            "data_type": data_type,
            "schema_class": name,
            "columns": [
                {"name": col, "dtype": _dtype_name(dtype)}
                for col, dtype in cls.columns.items()
            ],
            "description": (inspect.getdoc(cls) or "").split("\n")[0],
        })
    return {"data_types": schemas}


@router.get("/frequencies")
async def introspect_frequencies():
    """Available bar frequencies."""
    return {
        "frequencies": [
            {"value": f.value, "label": f.value}
            for f in DataFrequency
        ]
    }


@router.get("/exchanges")
async def introspect_exchanges(request: Request):
    """Configured exchanges + the market types they can serve."""
    exchanges = request.app.state.config.get("exchanges") or {}
    result = []
    for name, cfg in exchanges.items():
        result.append({
            "name": name,
            "label": exchange_label(name),
            "enabled": cfg.get("enabled", False),
            "default_market_type": cfg.get("default_market_type", "perpetual"),
            "market_types": [m.value for m in MarketType],
        })
    return {"exchanges": result}


@router.get("/evaluation")
async def introspect_evaluation():
    """Capability manifest for the evaluation layer (from live code)."""
    manifest = []
    for name in evaluation_pkg.__all__:
        obj = getattr(evaluation_pkg, name, None)
        if obj is None:
            continue
        manifest.append(_capability(name, obj))
    return {"evaluation": manifest}


@router.get("/visualization")
async def introspect_visualization():
    """Capability manifest for the visualization layer."""
    import superplatform.visualization as viz_pkg

    manifest = []
    for name in dir(viz_pkg):
        obj = getattr(viz_pkg, name)
        if name.startswith("_") or not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        manifest.append(_capability(name, obj))
    return {"visualization": manifest}


@router.get("/providers")
async def introspect_providers(request: Request):
    """Registered providers grouped by data_type — the data-source pluggability map."""
    registry = request.app.state.providers
    providers = {}
    for provider_id in registry.list_all():
        prov = registry.get(provider_id)
        data_type = prov.data_type
        providers.setdefault(data_type, []).append({
            "provider_id": provider_id,
            "label": provider_label(provider_id),
            "exchange": prov.exchange,
            "market_type": prov.market_type.value if prov.market_type else None,
        })
    return {"providers": providers}


@router.get("/factors")
async def introspect_factor_categories():
    """Factor registry summary — categories and counts, for the explorer filters."""
    from superplatform.factors.registry import FactorRegistry

    reg = FactorRegistry.get_instance()
    return {
        "categories": [
            {"value": key, "label": key.replace("_", " "), "count": count}
            for key, count in reg.categories_summary().items()
        ],
        "total": len(reg.list_all()),
    }
