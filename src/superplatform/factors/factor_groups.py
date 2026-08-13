"""Factor groups — named, kind-dispatched factor selectors.

A factor group is declared in ``config/factors.yaml`` under ``factor_groups:``
and lets a user pick many factors at once instead of ticking each one:

    factor_groups:
      momentum_study:
        kind: list
        description: 动量与反转
        factors: [momentum, short_term_reversal, rsi_14]

      volatility_study:
        kind: category
        category: volatility

The ``kind`` field dispatches to a registered resolver that turns the group's
config into an ordered list of factor names. New selector kinds plug in by
calling :func:`register_resolver` — no route or config-schema change needed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from superplatform.factors.base import FactorCategory
from superplatform.factors.registry import FactorRegistry
from superplatform.runtime.config import Config


class FactorGroup(BaseModel):
    """Config shape of one factor group.

    ``kind`` is the selector dispatch key; kind-specific parameters (``factors``
    for ``list``, ``category`` for ``category``, ...) are carried as extra fields
    so new kinds need no model change.
    """

    name: str
    kind: str
    description: str = ""
    model_config = ConfigDict(extra="allow")


@dataclass
class GroupResolution:
    """One group resolved against the factor registry.

    ``factors`` keeps declaration order with duplicates removed; ``unknown``
    lists names that were declared but are not registered (e.g. members of a
    group written ahead of their factor definitions).
    """

    name: str
    kind: str
    description: str
    factors: list[str]
    unknown: list[str]


# kind -> resolver(group, registry) -> declared factor names (ordered, un-deduped;
# registry membership and dedup are handled by resolve_group).
_RESOLVERS: dict[str, Callable[[FactorGroup, FactorRegistry], list[str]]] = {}


def register_resolver(kind: str, fn: Callable[[FactorGroup, FactorRegistry], list[str]]) -> None:
    """Register a factor-group selector kind for config dispatch."""
    _RESOLVERS[kind] = fn


def supported_kinds() -> list[str]:
    """Registered selector kinds, sorted."""
    return sorted(_RESOLVERS)


def _resolve_list(group: FactorGroup, _registry: FactorRegistry) -> list[str]:
    factors = getattr(group, "factors", None)
    if not isinstance(factors, list) or not factors:
        raise ValueError(
            f"factor group '{group.name}'（kind='list'）需要一个非空的 `factors: List[str]`"
        )
    if not all(isinstance(f, str) and f for f in factors):
        raise ValueError(
            f"factor group '{group.name}'（kind='list'）的 `factors` 必须是字符串列表"
        )
    return factors


def _resolve_category(group: FactorGroup, registry: FactorRegistry) -> list[str]:
    raw = getattr(group, "category", None)
    try:
        category = FactorCategory(raw)
    except ValueError:
        valid = "、".join(c.value for c in FactorCategory)
        raise ValueError(
            f"factor group '{group.name}'（kind='category'）的 `category` 必须是"
            f" {valid} 之一，收到：{raw!r}"
        ) from None
    return registry.list_by_category(category)


register_resolver("list", _resolve_list)
register_resolver("category", _resolve_category)


def factor_groups(config: Config) -> list[FactorGroup]:
    """Parse the ``factor_groups:`` section of config into :class:`FactorGroup`.

    Raises ``ValueError`` with an actionable Chinese message on the first
    malformed entry (non-mapping section/entry, or a missing/unknown ``kind``).
    Absent config is a no-op (returns ``[]``).
    """
    raw = config.get("factor_groups")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError("`factor_groups` 需要是一个映射（组名 → 组定义）")
    groups: list[FactorGroup] = []
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"factor group '{name}' 需要是一个映射（含 `kind` 字段）")
        kind = spec.get("kind")
        if not isinstance(kind, str) or kind not in _RESOLVERS:
            raise ValueError(
                f"factor group '{name}' 缺少或使用了未知的 `kind`（收到 {kind!r}）。"
                f"支持：{'、'.join(supported_kinds())}"
            )
        groups.append(FactorGroup(name=str(name), **spec))
    return groups


def resolve_group(group: FactorGroup, registry: FactorRegistry) -> GroupResolution:
    """Resolve one group to registered factor names (dedup, order preserved)."""
    declared = _RESOLVERS[group.kind](group, registry)
    known = set(registry.list_all())
    seen: set[str] = set()
    registered: list[str] = []
    unknown: list[str] = []
    for name in declared:
        if name in seen:
            continue
        seen.add(name)
        (registered if name in known else unknown).append(name)
    return GroupResolution(
        name=group.name,
        kind=group.kind,
        description=group.description,
        factors=registered,
        unknown=unknown,
    )
