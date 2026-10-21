"""Two-step verification primitives — port of src/domain/totp.ts (RFC 6238 TOTP, base32, recovery codes).

Same outputs as the TypeScript (which implements SHA-1/SHA-256/HMAC by hand and is verified against node:crypto);
here the standard library does the hashing. Randomness: `secrets` (CSPRNG)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import Any

from app.js import encode_uri_component, js_str, nullish

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
RECOVERY_CODE_COUNT = 10
B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
RC_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'


def utf8(s: str) -> bytes:
    return s.encode('utf-8', 'surrogatepass')


def to_hex(b: bytes) -> str:
    return b.hex()


def random_bytes(n: int) -> bytes:
    return secrets.token_bytes(n)


def random_token(nbytes: int = 32) -> str:
    """random bytes as base64url without padding (43 chars for 32 bytes)"""
    return base64.urlsafe_b64encode(random_bytes(nbytes)).decode().rstrip('=')


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(utf8(a), utf8(b)) if len(a) == len(b) else False


def sha1(msg: bytes) -> bytes:
    return hashlib.sha1(msg).digest()


def sha256(msg: bytes) -> bytes:
    return hashlib.sha256(msg).digest()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(utf8(s)).hexdigest()


def hmac_sha1(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha1).digest()


def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def base32_encode(b: bytes) -> str:
    bits = 0
    value = 0
    out = []
    for x in b:
        value = ((value << 8) | x) & 0xFFFFFFFF
        bits += 8
        while bits >= 5:
            out.append(B32[(value >> (bits - 5)) & 31])
            bits -= 5
    if bits > 0:
        out.append(B32[(value << (5 - bits)) & 31])
    return ''.join(out)


def base32_decode(s: str) -> bytes | None:
    """case-insensitive; spaces, dashes and padding ignored; None when invalid"""
    clean = re.sub(r'[\s=-]', '', s.upper())
    out = bytearray()
    bits = 0
    value = 0
    for ch in clean:
        i = B32.find(ch)
        if i < 0:
            return None
        value = ((value << 5) | i) & 0xFFFFFFFF
        bits += 5
        if bits >= 8:
            out.append((value >> (bits - 8)) & 255)
            bits -= 8
    return bytes(out)


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS, hash: str = 'sha1') -> str:
    msg = int(counter).to_bytes(8, 'big', signed=False) if counter >= 0 else (int(counter) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, 'big')
    mac = hmac_sha1(key, msg) if hash == 'sha1' else hmac_sha256(key, msg)
    o = mac[-1] & 0x0F
    bin_ = ((mac[o] & 0x7F) << 24) | (mac[o + 1] << 16) | (mac[o + 2] << 8) | mac[o + 3]
    return str(bin_ % 10**digits).rjust(digits, '0')


def totp_step(time_ms: float, step: int = TOTP_STEP_SECONDS) -> int:
    return int((time_ms / 1000) // step)


def totp(secret_b32: str, time_ms: float | None = None, digits: int = TOTP_DIGITS) -> str:
    key = base32_decode(secret_b32)
    if key is None:
        raise ValueError('invalid base32 secret')
    return hotp(key, totp_step(time.time() * 1000 if time_ms is None else time_ms), digits)


def verify_totp(secret_b32: str, code: Any, now: float | None = None, window: int | None = None, after_step: int | None = None) -> int | None:
    """matched time step within ±window, or None; steps <= after_step are refused (single use)."""
    c = re.sub(r'\s', '', '' if nullish(code) else js_str(code))
    if not re.fullmatch(r'\d{6}', c, re.ASCII):
        return None
    key = base32_decode(secret_b32)
    if not key:
        return None
    cur = totp_step(time.time() * 1000 if now is None else now)
    w = TOTP_WINDOW if window is None else window
    matched = None
    for st in range(cur - w, cur + w + 1):  # no early exit (timing)
        if constant_time_equal(hotp(key, st), c) and matched is None and (after_step is None or st > after_step):
            matched = st
    return matched


def new_totp_secret() -> str:
    return base32_encode(random_bytes(20))


def otpauth_url(issuer: str, account: str, secret: str) -> str:
    i = encode_uri_component(issuer)
    return f'otpauth://totp/{i}:{encode_uri_component(account)}?secret={secret}&issuer={i}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}'


def new_recovery_codes(n: int = RECOVERY_CODE_COUNT) -> list[str]:
    out: list[str] = []
    while len(out) < n:
        b = random_bytes(10)
        s = ''.join(RC_ALPHABET[x % len(RC_ALPHABET)] for x in b)
        code = s[:5] + '-' + s[5:]
        if code not in out:
            out.append(code)
    return out


def normalize_recovery_code(s: Any) -> str:
    return re.sub(r'[^a-z0-9]', '', ('' if nullish(s) else js_str(s)).lower())


def hash_recovery_code(salt: str, code: str) -> str:
    return sha256_hex('rc1:' + salt + ':' + normalize_recovery_code(code))
