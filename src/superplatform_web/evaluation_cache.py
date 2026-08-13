"""Small bounded in-process cache for web factor evaluations."""

from __future__ import annotations

import copy
from collections import OrderedDict
from typing import Any


class EvaluationResultCache:
    """Cache serialized results; callers never share mutable cached objects."""

    def __init__(self, max_entries: int = 32) -> None:
        self.max_entries = max_entries
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def get(self, key: str) -> dict[str, Any] | None:
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return copy.deepcopy(value)

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._items[key] = copy.deepcopy(value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
