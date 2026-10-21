"""RFC 6238 appendix B test vectors (SHA-1, 8 digits) and base32 round trip for app/domain/totp.py."""

from __future__ import annotations

from app.domain.totp import base32_decode, base32_encode, hotp, totp, totp_step, verify_totp

SEED = b'12345678901234567890'
VECTORS = [(59, '94287082'), (1111111109, '07081804'), (1111111111, '14050471'), (1234567890, '89005924'), (2000000000, '69279037'), (20000000000, '65353130')]


def test_rfc6238_appendix_b_sha1() -> None:
    for t, code in VECTORS:
        assert hotp(SEED, totp_step(t * 1000), 8) == code
        assert totp(base32_encode(SEED), t * 1000, 8) == code


def test_base32_and_verify_window() -> None:
    assert base32_decode(base32_encode(SEED)) == SEED
    secret = base32_encode(SEED)
    now = 1_111_111_111_000
    assert verify_totp(secret, totp(secret, now), now) == totp_step(now)
    assert verify_totp(secret, totp(secret, now - 30_000), now) == totp_step(now) - 1  # one step of drift accepted
    assert verify_totp(secret, totp(secret, now - 90_000), now) is None
