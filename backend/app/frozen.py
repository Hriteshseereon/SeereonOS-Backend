"""Read-only containers for cached rows and catalogues.

Rows held by the per-process tenant cache are shared by every request, so handlers must never change them in place:
all writes go through the unit-of-work helpers (insert / patch / update / remove / …), which copy on write. In
development and tests (APP_ENV != production) cached rows and catalogue constants are deep-frozen with these types so
an illegal in-place mutation fails loudly (FrozenError) instead of silently corrupting the cache. They are real dict /
list subclasses: reading, iterating, `{**row}`, `[*lst]`, `dict(row)` and JSON serialisation work unchanged.
"""

from __future__ import annotations

from typing import Any


class FrozenError(TypeError):
    """An in-place mutation of a cached (shared) row or a catalogue constant."""


def _blocked(self: Any, *a: Any, **k: Any) -> Any:
    raise FrozenError(
        'This row/list is read-only (shared cache or catalogue). Use the unit-of-work helpers (patch/update/insert/remove/set_rows) '
        'or copy it first (dict(row), list(lst), thaw(value)).'
    )


class FrozenDict(dict):
    __slots__ = ()
    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked
    __ior__ = _blocked

    def __copy__(self) -> dict:
        return dict(self)

    def __deepcopy__(self, memo: dict) -> Any:
        return thaw(self)

    def __reduce__(self) -> Any:
        return (dict, (dict(self),))


class FrozenList(list):
    __slots__ = ()
    __setitem__ = _blocked
    __delitem__ = _blocked
    append = _blocked
    extend = _blocked
    insert = _blocked
    pop = _blocked
    remove = _blocked
    clear = _blocked
    sort = _blocked
    reverse = _blocked
    __iadd__ = _blocked
    __imul__ = _blocked

    def __copy__(self) -> list:
        return list(self)

    def __deepcopy__(self, memo: dict) -> Any:
        return thaw(self)

    def __reduce__(self) -> Any:
        return (list, (list(self),))


def freeze(v: Any) -> Any:
    """deep-frozen copy (dicts -> FrozenDict, lists -> FrozenList); scalars returned as is"""
    t = type(v)
    if t is dict or t is FrozenDict:
        if t is FrozenDict:
            return v
        return FrozenDict({k: freeze(x) for k, x in v.items()})
    if t is list or t is tuple or t is FrozenList:
        if t is FrozenList:
            return v
        return FrozenList([freeze(x) for x in v])
    return v


def thaw(v: Any) -> Any:
    """deep mutable copy (plain dicts / lists) — e.g. before changing a nested value of a row"""
    if isinstance(v, dict):
        return {k: thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [thaw(x) for x in v]
    return v


def is_frozen(v: Any) -> bool:
    return type(v) is FrozenDict or type(v) is FrozenList
