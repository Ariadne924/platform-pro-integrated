"""Symbol groups — named, kind-dispatched symbol selectors.

A symbol group is declared in config under ``symbol_groups:`` (baseline groups
in ``config/default.yaml``, user-saved groups in ``config/user_symbol_groups.yaml``)
and lets a user pick many symbols at once instead of ticking each one:

    symbol_groups:
      core_two:
        kind: list
        description: 核心双标的
        symbols: [BTCUSDT, ETHUSDT]

      top10_volume:
        kind: top_n
        description: 按 24h 成交额 Top 10
        n: 10

The ``kind`` field dispatches to a registered resolver that turns the group's
config into an ordered list of symbol names. New selector kinds plug in by
calling :func:`register_resolver` — no route or config-schema change needed.

Resolution happens against a :class:`UniverseContext` (the active universe plus
24h quote volume) supplied at request time, so a group always resolves against
the *current* market — delisted symbols surface as ``unknown`` rather than being
silently traded or evaluated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from superplatform.runtime.config import Config


class SymbolGroup(BaseModel):
    """Config shape of one symbol group.

    ``kind`` is the selector dispatch key; kind-specific parameters (``symbols``
    for ``list``, ``n`` for ``top_n``, ...) are carried as extra fields so new
    kinds need no model change.
    """

    name: str
    kind: str
    description: str = ""
    model_config = ConfigDict(extra="allow")


@dataclass
class UniverseContext:
    """Request-time inputs a group resolution can consult.

    ``active`` is the set of currently-tradeable symbols (delisted symbols are
    excluded); ``quote_volume`` maps symbol → 24h quote volume in USDT, possibly
    empty when the ticker source is unreachable.
    """

    active: set[str]
    quote_volume: dict[str, float] = field(default_factory=dict)


@dataclass
class GroupResolution:
    """One group resolved against the universe.

    ``symbols`` keeps declaration order with duplicates removed; ``unknown``
    lists symbols that were declared but are not in the active universe (e.g.
    delisted or never-listed members).
    """

    name: str
    kind: str
    description: str
    symbols: list[str]
    unknown: list[str]


# kind -> resolver(group, ctx) -> declared symbol names (ordered, un-deduped;
# active-universe filtering and dedup are handled by resolve_group).
_RESOLVERS: dict[str, Callable[[SymbolGroup, UniverseContext], list[str]]] = {}


def register_resolver(kind: str, fn: Callable[[SymbolGroup, UniverseContext], list[str]]) -> None:
    """Register a symbol-group selector kind for config dispatch."""
    _RESOLVERS[kind] = fn


def supported_kinds() -> list[str]:
    """Registered selector kinds, sorted."""
    return sorted(_RESOLVERS)


def _resolve_list(group: SymbolGroup, _ctx: UniverseContext) -> list[str]:
    symbols = getattr(group, "symbols", None)
    if not isinstance(symbols, list) or not symbols:
        raise ValueError(
            f"symbol group '{group.name}'（kind='list'）需要一个非空的 `symbols: List[str]`"
        )
    if not all(isinstance(s, str) and s for s in symbols):
        raise ValueError(
            f"symbol group '{group.name}'（kind='list'）的 `symbols` 必须是字符串列表"
        )
    return symbols


def _resolve_top_n(group: SymbolGroup, ctx: UniverseContext) -> list[str]:
    raw = getattr(group, "n", None)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ValueError(
            f"symbol group '{group.name}'（kind='top_n'）需要一个正整数 `n`，收到：{raw!r}"
        )
    # Sort by 24h quote volume desc; resolve_group caps at [:group.n] so the
    # resolver stays a pure ordering over available tickers.
    return sorted(ctx.quote_volume, key=lambda s: ctx.quote_volume[s], reverse=True)


register_resolver("list", _resolve_list)
register_resolver("top_n", _resolve_top_n)


def symbol_groups(config: Config) -> list[SymbolGroup]:
    """Parse the ``symbol_groups:`` section of config into :class:`SymbolGroup`.

    Raises ``ValueError`` with an actionable Chinese message on the first
    malformed entry (non-mapping section/entry, or a missing/unknown ``kind``).
    Absent config is a no-op (returns ``[]``).
    """
    raw = config.get("symbol_groups")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError("`symbol_groups` 需要是一个映射（组名 → 组定义）")
    groups: list[SymbolGroup] = []
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"symbol group '{name}' 需要是一个映射（含 `kind` 字段）")
        kind = spec.get("kind")
        if not isinstance(kind, str) or kind not in _RESOLVERS:
            raise ValueError(
                f"symbol group '{name}' 缺少或使用了未知的 `kind`（收到 {kind!r}）。"
                f"支持：{'、'.join(supported_kinds())}"
            )
        groups.append(SymbolGroup(name=str(name), **spec))
    return groups


def resolve_group(group: SymbolGroup, ctx: UniverseContext) -> GroupResolution:
    """Resolve one group to active-universe symbol names (dedup, order preserved)."""
    declared = _RESOLVERS[group.kind](group, ctx)
    if group.kind == "top_n":
        declared = declared[: group.n]
    active = set(ctx.active)
    seen: set[str] = set()
    resolved: list[str] = []
    unknown: list[str] = []
    for symbol in declared:
        if symbol in seen:
            continue
        seen.add(symbol)
        (resolved if symbol in active else unknown).append(symbol)
    return GroupResolution(
        name=group.name,
        kind=group.kind,
        description=group.description,
        symbols=resolved,
        unknown=unknown,
    )
