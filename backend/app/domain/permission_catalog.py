"""Permission catalogue — port of src/domain/permissionCatalog.ts (data from the generated catalogue)."""

from __future__ import annotations

from collections.abc import Iterable

from app.catalog import cat

_C = cat('domain/permissionCatalog')
MODULE_LABEL = _C['MODULE_LABEL']
MODULE_ORDER = _C['MODULE_ORDER']
PERMISSIONS = _C['PERMISSIONS']
PERMISSION_KEYS = _C['PERMISSION_KEYS']
PERMISSION_BY_KEY = _C['PERMISSION_BY_KEY']
DEFAULT_ROLES = _C['DEFAULT_ROLES']
LOCKED_PERMISSIONS = _C['LOCKED_PERMISSIONS']
PERSONA_LABEL = _C['PERSONA_LABEL']
_KEYS = frozenset(PERMISSION_KEYS)


def is_permission_key(k: str) -> bool:
    return k in _KEYS


def effective_permissions(role_perms: Iterable[str] | None, extra: Iterable[str] = (), revoked: Iterable[str] = ()) -> list[str]:
    """role ∪ extra − revoked, known keys only, sorted (JS default sort = code unit order)."""
    s = dict.fromkeys(list(role_perms or []) + list(extra))
    for r in revoked:
        s.pop(r, None)
    return sorted(k for k in s if k in _KEYS)
