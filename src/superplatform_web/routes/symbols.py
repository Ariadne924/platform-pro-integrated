"""Symbol group endpoints: named, kind-dispatched symbol selectors.

Mirrors the factor-group endpoints in ``routes/factors.py``: preconfigured
groups live in ``config/default.yaml`` (``symbol_groups:``), user-saved groups
in ``config/user_symbol_groups.yaml``. Groups resolve against the *current*
active universe + 24h quote volume (see ``superplatform.factors.symbol_groups``),
so delisted symbols surface as ``unknown`` rather than being silently traded.

``GET /top`` is the ad-hoc Top-N entry used by the shared picker's "按成交额
Top N" control — it resolves a synthetic ``top_n`` group without persisting it.
"""

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from superplatform.factors.symbol_groups import (
    UniverseContext,
    resolve_group,
    symbol_groups,
)
from superplatform_web.universe import fetch_tickers, stored_active

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


def _user_symbol_groups_path() -> Path:
    """Path to the user-saved symbol groups file (gitignored, machine-local).

    Derived from ``_CONFIG_FILES`` (which carries the same basename) so tests
    that isolate config to a temp dir redirect this file there too.
    """
    import superplatform_web.state as _state

    for entry in _state._CONFIG_FILES:
        path = Path(entry)
        if path.name == "user_symbol_groups.yaml":
            return path
    return _state._PROJECT_ROOT / "config" / "user_symbol_groups.yaml"


def _load_user_symbol_groups() -> dict:
    """The ``symbol_groups:`` section of user_symbol_groups.yaml ({} when absent)."""
    path = _user_symbol_groups_path()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("symbol_groups") or {}
    return raw if isinstance(raw, dict) else {}


def _save_user_symbol_groups(groups: dict) -> None:
    """Write the ``symbol_groups:`` section back to user_symbol_groups.yaml.

    Other keys in the file are preserved; only user groups live here (the
    preconfigured ones stay in default.yaml).
    """
    path = _user_symbol_groups_path()
    data: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data["symbol_groups"] = groups
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


async def _active_universe() -> set[str]:
    """Active symbols = ticker symbols ∪ stored universe, so Top-N resolution
    still works offline (stored universe) and stored-active validation works
    when the ticker fetch is unavailable."""
    return set(await fetch_tickers()) | stored_active()


def _group_payload(group, active: set[str], tickers: dict, *, deletable: bool = False) -> dict:
    """One configured symbol group with resolved members + availability.

    Symbols already resolve against the active universe, so ``available_count``
    equals ``count`` (there is no second data-source filter as with factors);
    the field is kept for API symmetry with factor groups.
    """
    res = resolve_group(group, UniverseContext(active=active, quote_volume=tickers))
    return {
        "name": res.name,
        "kind": res.kind,
        "description": res.description,
        "symbols": res.symbols,
        "count": len(res.symbols),
        "available_count": len(res.symbols),
        "unknown": res.unknown,
        "deletable": deletable,
    }


async def _all_group_payloads(request: Request) -> list[dict]:
    """Every configured symbol group (preconfigured + user-saved) as payloads.

    ``deletable`` marks groups declared in ``config/user_symbol_groups.yaml`` —
    the only ones the frontend may remove. Malformed config → 422.
    """
    try:
        groups = symbol_groups(request.app.state.config)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    tickers = await fetch_tickers()
    active = set(tickers) | stored_active()
    user_groups = set(_load_user_symbol_groups())
    return [
        _group_payload(g, active, tickers, deletable=g.name in user_groups)
        for g in groups
    ]


@router.get("/groups")
async def list_symbol_groups(request: Request):
    """Configured symbol groups with resolved members and availability.

    A group is declared under ``symbol_groups:`` in config (preconfigured; see
    ``superplatform.factors.symbol_groups`` for the ``kind`` dispatch) or
    ``config/user_symbol_groups.yaml`` (user-saved via POST /groups). Members
    resolve against the active universe; malformed config → 422.
    """
    return await _all_group_payloads(request)


@router.get("/top")
async def top_symbols(request: Request, n: int = Query(default=5, ge=1, le=50)):
    """Resolve the ad-hoc Top-N-by-volume group (no persistence).

    Backs the shared picker's "按成交额 Top N" control. When the ticker source
    is unreachable the result is empty — the UI surfaces that gracefully.
    """
    from superplatform.factors.symbol_groups import SymbolGroup

    tickers = await fetch_tickers()
    if not tickers:
        return {"n": n, "symbols": [], "count": 0}
    active = set(tickers) | stored_active()
    group = SymbolGroup(name="__adhoc_top__", kind="top_n", n=n)
    res = resolve_group(group, UniverseContext(active=active, quote_volume=tickers))
    return {"n": n, "symbols": res.symbols, "count": len(res.symbols)}


# ── User symbol groups (config/user_symbol_groups.yaml) ───────────────


class SymbolGroupSaveRequest(BaseModel):
    name: str
    symbols: list[str] = Field(min_length=1)


@router.post("/groups")
async def save_symbol_group(data: SymbolGroupSaveRequest, request: Request):
    """Save the current selection as a user symbol group in user_symbol_groups.yaml.

    The group is stored with ``kind: list`` so it flows through the same
    kind-dispatched resolution pipeline as preconfigured groups. Names matching
    a preconfigured group are rejected (409); re-saving an existing user group
    overwrites it. Symbols not in the active universe are rejected (422) so a
    saved group never silently trades delisted symbols. Returns all groups.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再保存分组")

    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="分组名不能为空")

    active = await _active_universe()
    if active:
        unknown = sorted(set(data.symbols) - active)
        if unknown:
            raise HTTPException(status_code=422, detail=f"未知（已下架/未收录）标的：{'、'.join(unknown)}")
    symbols = list(dict.fromkeys(data.symbols))  # order-preserving dedup

    user_groups = _load_user_symbol_groups()
    if name not in user_groups:
        # Merged config carries both preconfigured and user groups; a name that
        # exists there but is NOT a user group must be a preconfigured one.
        try:
            merged = {g.name for g in symbol_groups(request.app.state.config)}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if name in merged:
            raise HTTPException(status_code=409, detail=f"「{name}」是预配置标的组，不能覆盖")

    user_groups[name] = {"kind": "list", "description": "用户保存", "symbols": symbols}
    _save_user_symbol_groups(user_groups)
    _state.reload_config()
    return await _all_group_payloads(request)


@router.delete("/groups/{name}")
async def delete_symbol_group(name: str, request: Request):
    """Remove a user-saved symbol group from user_symbol_groups.yaml.

    Preconfigured groups (declared in default.yaml) are never deletable → 404.
    Returns all groups, updated.
    """
    import superplatform_web.state as _state

    if _state.live_is_running():
        raise HTTPException(status_code=409, detail="模拟会话运行中，请先停止再删除分组")

    user_groups = _load_user_symbol_groups()
    if name not in user_groups:
        raise HTTPException(status_code=404, detail=f"「{name}」不是用户保存的分组")

    del user_groups[name]
    _save_user_symbol_groups(user_groups)
    _state.reload_config()
    return await _all_group_payloads(request)
