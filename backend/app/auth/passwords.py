"""Password hashing — same format as server/db/passwords.ts: scrypt$N$r$p$salt_b64$hash_b64 (NFKC-normalised
password, N=32768 r=8 p=1, 16-byte random salt, 64-byte key). Hashes written by the Node reference verify here and
vice versa. Verification is timing-safe."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import unicodedata

SCRYPT = {'N': 32768, 'r': 8, 'p': 1, 'keylen': 64}
_MAXMEM = 256 * 1024 * 1024
_HASH_RE = re.compile(r'^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$')


# scrypt is CPU- and memory-bound (~100 ms, 32 MB each) and releases the GIL: unbounded, a burst of sign-ins runs
# dozens at once in one process, starving every other request (and a flush holding the tenant lock) of CPU. Bounded
# per process (SCRYPT_CONCURRENCY, default 1 = one per worker; workers ~ CPUs), the burst queues instead.
_GATE = threading.BoundedSemaphore(1)


def set_concurrency(n: int) -> None:
    global _GATE
    _GATE = threading.BoundedSemaphore(max(1, n))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, keylen: int, gated: bool = True) -> bytes:
    pw = unicodedata.normalize('NFKC', password).encode('utf-8', 'surrogatepass')
    if not gated:
        return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=keylen)
    with _GATE:
        return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=keylen)


def hash_password(password: str, gated: bool = True) -> str:
    """gated=False: tooling that hashes many passwords in parallel on purpose (seed loading)"""
    salt = secrets.token_bytes(16)
    key = _scrypt(password, salt, SCRYPT['N'], SCRYPT['r'], SCRYPT['p'], SCRYPT['keylen'], gated)
    return f'scrypt${SCRYPT["N"]}${SCRYPT["r"]}${SCRYPT["p"]}${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}'


def is_password_hash(s: object) -> bool:
    return isinstance(s, str) and bool(_HASH_RE.match(s))


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split('$')
    if len(parts) != 6 or parts[0] != 'scrypt':
        return False
    try:
        n, r, p = (int(x) for x in parts[1:4])
        salt = base64.b64decode(parts[4])
        expected = base64.b64decode(parts[5])
    except ValueError:
        return False
    if not n or not r or not p or not expected:
        return False
    try:
        key = _scrypt(password, salt, n, r, p, len(expected))
    except ValueError:
        return False
    return hmac.compare_digest(key, expected)
