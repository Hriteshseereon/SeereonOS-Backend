"""Opaque tokens and ids (port of server/src/auth/crypto.ts)."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time

_B36 = '0123456789abcdefghijklmnopqrstuvwxyz'
_TOKEN_HASH = re.compile(r'^[0-9a-f]{64}$')


def new_token() -> str:
    """32 random bytes, base64url (43 chars)"""
    from app.js import mark_volatile

    mark_volatile()
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8', 'surrogatepass')).hexdigest()


def is_token_hash(s: object) -> bool:
    return isinstance(s, str) and bool(_TOKEN_HASH.match(s))


def _b36(n: int) -> str:
    if n == 0:
        return '0'
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return ''.join(reversed(out))


def new_id(prefix: str = '') -> str:
    """ids in the mock's shape (prefix + base36 time + random base36) with crypto randomness"""
    from app.js import mark_volatile

    mark_volatile()
    b = secrets.token_bytes(10)
    return prefix + _b36(time.time_ns() // 1_000_000) + ''.join(_B36[x % 36] for x in b)
