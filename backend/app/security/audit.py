"""Authentication audit trail — port of server/src/security/audit.ts. Every sign-in success/failure, second step,
lockout, sign-out, password change/reset, 2FA change, session revocation, admin credential reset and rate-limit block
is written to security_events (who, where, request id, a small detail object; never secrets)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import orjson

from app.auth.crypto import sha256
from app.security.store import SecurityEvent

AUDITED = frozenset(
    {
        'POST /auth/login',
        'POST /auth/login/mfa',
        'POST /auth/logout',
        'POST /auth/signup',
        'POST /auth/reset-request',
        'POST /auth/reset',
        'POST /invites/:token/accept',
        'POST /me/password',
        'POST /me/2fa/verify',
        'POST /me/2fa/disable',
        'POST /me/2fa/recovery-codes',
        'POST /sessions/revoke-others',
        'DELETE /sessions/:id',
        'POST /users/:id/reset-password',
        'POST /users/:id/2fa/reset',
        'POST /users/:id/deactivate',
    }
)


@dataclass
class AuditCtx:
    key: str
    params: dict[str, str]
    base: dict[str, Any]


def _lower(v: Any) -> str | None:
    return v.strip().lower()[:254] if isinstance(v, str) and v.strip() else None


def audit_before(
    rt: Any, store: Any, method: str, path: str, body: Any, headers: dict[str, str | None], ip: str | None, user_agent: str | None, request_id: str
) -> AuditCtx | None:
    try:
        m = rt.router.match(method, path)
    except Exception:  # noqa: BLE001
        return None
    if m is None:
        return None
    key = f'{method} {m[0].pattern}'
    if key not in AUDITED:
        return None
    b = body if isinstance(body, dict) else {}
    base: dict[str, Any] = {'ip': ip, 'user_agent': (user_agent or '')[:300] or None, 'request_id': request_id, 'email': _lower(b.get('email'))}
    try:
        raw = re.sub(r'^Bearer\s+', '', headers.get('Authorization') or '', flags=re.I).strip()
        if raw:
            s = rt.backend.resolve_session(sha256(raw), rt.idle_hours)
            if s:
                base.update(user_id=s['user_id'], tenant_id=s['tenant_id'])
        elif key == 'POST /auth/login/mfa' and isinstance(b.get('mfa_token'), str):
            o = store.mfa_owner(sha256(b['mfa_token']))
            if o:
                base.update(user_id=o['user_id'], tenant_id=o['tenant_id'])
    except Exception:  # noqa: BLE001 — auditing never blocks the request
        pass
    return AuditCtx(key, m[1], base)


def audit_after(c: AuditCtx, status: int, body: bytes, store: Any) -> None:
    ok = status == 200
    try:
        b = orjson.loads(body)
    except Exception:  # noqa: BLE001
        b = {}
    code = (b.get('error') or {}).get('code') if isinstance(b, dict) else None
    data = (b.get('data') or {}) if ok and isinstance(b, dict) else {}
    if not isinstance(data, dict):
        data = {}
    signed_in: dict[str, Any] = {}
    user = data.get('user')
    if isinstance(user, dict) and isinstance(data.get('token'), str):
        signed_in = {'user_id': user.get('id'), 'tenant_id': user.get('tenant_id'), 'email': _lower(user.get('email')) or c.base.get('email')}

    def ev(event: str, outcome: str, **extra: Any) -> None:
        store.log(SecurityEvent(event=event, outcome=outcome, **{**c.base, **extra}))

    def fail(event: str) -> None:
        ev(event, 'blocked' if status == 429 else 'failure', detail={'code': code, 'status': status})

    k = c.key
    if k == 'POST /auth/login':
        if ok and data.get('mfa_required'):
            return ev('login.mfa_required', 'info')
        if ok:
            return ev('login.success', 'success', **signed_in, detail={'method': 'password'})
        return fail('login.locked' if code == 'LOGIN_LOCKED' else 'login.failure')
    if k == 'POST /auth/login/mfa':
        if ok:
            left = data.get('recovery_codes_left')
            return ev(
                'login.success',
                'success',
                **signed_in,
                detail={'method': 'recovery_code' if left is not None else 'totp', **({'recovery_codes_left': left} if left is not None else {})},
            )
        return fail('mfa.challenge_burned' if code == 'MFA_LOCKED' else 'login.locked' if code == 'LOGIN_LOCKED' else 'mfa.failure')
    if k == 'POST /auth/logout':
        return ev('logout', 'success') if ok else None
    if k == 'POST /auth/signup':
        return ev('signup', 'success', **signed_in) if ok else fail('signup.failure')
    if k == 'POST /auth/reset-request':
        return ev('password.reset_requested', 'info')
    if k == 'POST /auth/reset':
        return ev('password.reset', 'success') if ok else fail('password.reset_failure')
    if k == 'POST /invites/:token/accept':
        return ev('invite.accepted', 'success', **signed_in) if ok else fail('invite.accept_failure')
    if k == 'POST /me/password':
        return ev('password.changed', 'success') if ok else fail('password.change_failure')
    if k == 'POST /me/2fa/verify':
        return ev('mfa.enabled', 'success') if ok else fail('mfa.enable_failure')
    if k == 'POST /me/2fa/disable':
        return ev('mfa.disabled', 'success') if ok else fail('mfa.disable_failure')
    if k == 'POST /me/2fa/recovery-codes':
        return ev('mfa.recovery_codes_regenerated', 'success') if ok else fail('mfa.recovery_codes_failure')
    if k == 'POST /sessions/revoke-others':
        return ev('session.revoke_others', 'success', detail={'revoked': data.get('revoked')}) if ok else None
    if k == 'DELETE /sessions/:id':
        return ev('session.revoked', 'success', detail={'session': c.params.get('id')}) if ok else None
    if k == 'POST /users/:id/reset-password':
        return ev('admin.password_reset', 'success', detail={'target_user_id': c.params.get('id')}) if ok else fail('admin.password_reset_failure')
    if k == 'POST /users/:id/2fa/reset':
        return ev('admin.mfa_reset', 'success', detail={'target_user_id': c.params.get('id')}) if ok else fail('admin.mfa_reset_failure')
    if k == 'POST /users/:id/deactivate':
        return ev('admin.user_deactivate', 'success', detail={'target_user_id': c.params.get('id'), 'pending': data.get('pending')}) if ok else None
    return None
