"""AES-256-GCM sealing of TOTP secrets — byte-compatible port of server/src/auth/secretBox.ts.

Format "v1.<kid>:<iv b64url>:<ciphertext+tag b64url>"; kid = first 8 hex chars of SHA-256(key); key = SHA-256 of
"seereonos-mfa-v1\\0" + key material; AAD "seereonos:totp-secret". Values sealed by the Node reference open here."""

from __future__ import annotations

import base64
import hashlib
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AAD = b'seereonos:totp-secret'
_SEALED = re.compile(r'^v1\.([0-9a-f]{8}):([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)$')


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def derive_key(material: str) -> tuple[str, bytes]:
    key = hashlib.sha256(b'seereonos-mfa-v1\0' + material.encode()).digest()
    return hashlib.sha256(key).hexdigest()[:8], key


class AesSecretBox:
    def __init__(self, current: str, previous: list[str] | tuple[str, ...] = (), allow_plain: bool = False) -> None:
        self.kid, self.key = derive_key(current)
        self.keys = dict([derive_key(current), *(derive_key(p) for p in previous if p)])
        self.allow_plain = allow_plain

    def seal(self, plain: str) -> str:
        iv = os.urandom(12)
        ct = AESGCM(self.key).encrypt(iv, plain.encode(), _AAD)  # ciphertext + 16-byte tag (same layout as Node)
        return f'v1.{self.kid}:{_b64u(iv)}:{_b64u(ct)}'

    def open(self, sealed: str) -> str | None:
        if not isinstance(sealed, str):
            return None
        if sealed.startswith('plain:'):
            return sealed[6:] if self.allow_plain else None
        m = _SEALED.match(sealed)
        key = self.keys.get(m.group(1)) if m else None
        if not m or key is None:
            return None
        try:
            return AESGCM(key).decrypt(_unb64u(m.group(2)), _unb64u(m.group(3)), _AAD).decode()
        except Exception:  # noqa: BLE001
            return None


def create_secret_box(current: str, previous: list[str] | tuple[str, ...] = (), allow_plain: bool = False) -> AesSecretBox:
    return AesSecretBox(current, previous, allow_plain)
