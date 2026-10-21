"""Permission checks — port of src/domain/permissions.ts."""

from __future__ import annotations

from typing import Any

from app.catalog import cat

ROLE_LABEL = cat('domain/permissions')['ROLE_LABEL']
ROLE_SHORT = cat('domain/permissions')['ROLE_SHORT']


def can(perms: Any, action: str) -> bool:
    if not perms:
        return False
    return action in perms


def can_any(perms: Any, *actions: str) -> bool:
    return any(can(perms, a) for a in actions)
