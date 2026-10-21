"""Sealing of secrets the shared logic stores but never shows again (TOTP keys) — port of src/mock/secrets.ts.

The browser demo keeps them as "plain:<secret>"; the API installs an AES-256-GCM box keyed from MFA_ENCRYPTION_KEY
(app.auth.secret_box, byte-compatible with server/src/auth/secretBox.ts)."""

from __future__ import annotations

from typing import Any, Protocol


class SecretBox(Protocol):
    def seal(self, plain: str) -> str: ...

    def open(self, sealed: str) -> str | None: ...


class _PlainBox:
    def seal(self, plain: str) -> str:
        return 'plain:' + plain

    def open(self, sealed: str) -> str | None:
        return sealed[6:] if isinstance(sealed, str) and sealed.startswith('plain:') else None


_plain = _PlainBox()
_box: Any = _plain


def set_secret_box(b: Any) -> None:
    global _box
    _box = b if b is not None else _plain


def seal_secret(plain: str) -> str:
    return _box.seal(plain)


def open_secret(sealed: Any) -> str | None:
    return _box.open(sealed) if isinstance(sealed, str) else None
