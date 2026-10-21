"""Two-step verification state and checks — port of src/mock/twoFactor.ts.

State lives in pending_tokens rows (multi-instance safe): two_factor_setup {secret, attempts} (30 min);
two_factor TwoFactorPayload (no expiry); mfa_login {attempts} (5 min, id = SHA-256 of the token).

Python difference: rows are copy-on-write, so `touch()` and `use_second_factor()` RETURN the updated row (the TS
mutates `rec` in place and callers keep reading it)."""

from __future__ import annotations

import re
from typing import Any

from app.ctx import now_iso, patch
from app.db.uow import current_scope
from app.domain.totp import constant_time_equal, hash_recovery_code, normalize_recovery_code, verify_totp
from app.engine.secrets import open_secret
from app.js import UNDEF, coalesce, js_str, nullish

MFA_LOGIN_MINUTES = 5
MFA_MAX_ATTEMPTS = 5
TWO_FACTOR_SETUP_MINUTES = 30
TWO_FACTOR_SETUP_ATTEMPTS = 5
FAR_FUTURE = '9999-12-31T00:00:00.000Z'


def two_factor_record(user: dict) -> dict | None:
    """the user's active factor (pending_tokens kind two_factor) or None"""
    uid, tid = user.get('id'), user.get('tenant_id')
    for t in current_scope().view('pending_tokens'):
        if t.get('kind') == 'two_factor' and t.get('user_id') == uid and t.get('tenant_id') == tid:
            return t
    return None


def touch(t: dict, payload: Any) -> dict:
    """t.payload = payload; updated_at; version++; persist — returns the new row"""
    return patch('pending_tokens', t, {'payload': payload, 'updated_at': now_iso(), 'version': t['version'] + 1})


def use_second_factor(rec: dict, code: Any = UNDEF, recovery_code: Any = UNDEF) -> tuple[str | None, dict]:
    """Checks a TOTP code or a recovery code against the user's active factor. On success the code is consumed (TOTP
    step remembered; recovery code removed). Returns (kind 'totp' | 'recovery' | None, the current row)."""
    p = rec['payload']
    c = re.sub(r'\s', '', js_str(coalesce(code, '')))
    rc = normalize_recovery_code(js_str(coalesce(recovery_code, '')))
    if c:
        secret = open_secret(p.get('secret'))
        step = verify_totp(secret, c, after_step=None if nullish(p.get('last_step')) else p.get('last_step')) if secret else None
        if step is None:
            return None, rec
        return 'totp', touch(rec, {**p, 'last_step': step, 'fails': 0})
    if len(rc) >= 8:
        h = hash_recovery_code(p['salt'], rc)
        hit = -1
        for i, x in enumerate(p['recovery']):
            if constant_time_equal(x, h) and hit < 0:
                hit = i
        if hit < 0:
            return None, rec
        return 'recovery', touch(rec, {**p, 'recovery': [x for i, x in enumerate(p['recovery']) if i != hit], 'fails': 0})
    return None, rec
