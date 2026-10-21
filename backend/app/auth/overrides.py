"""Server-side wrappers of the auth/platform routes whose data is not tenant-scoped — port of
server/src/auth/overrides.ts. Each wrapper locates the tenant (by e-mail, token prefix or token hash), canonicalises
secrets (tokens -> SHA-256) and runs the SHARED handler (app/routes/auth.py) inside a unit of work of that tenant, so
the request / response / error contract is exactly the mock's. Session and reset tokens are 32 random bytes; only
their SHA-256 is stored.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.auth.crypto import new_token, sha256
from app.auth.passwords import hash_password, verify_password
from app.ctx import Req, get_db, scope
from app.js import UNDEF, coalesce, encode_uri_component, iso_from_ms, js_round, js_str, js_trim, now_ms
from app.routes import auth as A

AUTH = ['tenants', 'users', 'roles']


@dataclass
class OverrideDeps:
    rt: Any
    app_url: str  # the public URL of the web app (links in e-mails)


RESET_MINUTES = 60


def reset_mail(name: str, link: str) -> tuple[str, str]:
    """(subject, body) of the password-reset e-mail"""
    hello = f'Hi {name},' if name else 'Hi,'
    return (
        'Reset your SeereonOS password',
        f'{hello}\n\nSomeone (hopefully you) asked to reset the password of your SeereonOS account.\n'
        f'Use this link within {RESET_MINUTES} minutes to choose a new password:\n\n{link}\n\n'
        'If you did not ask for this, ignore this e-mail: your password stays as it is.\n\n— SeereonOS',
    )


_dummy_hash: str | None = None
_dummy_lock = threading.Lock()


def _dummy() -> str:
    """a real scrypt hash to verify against when the e-mail is unknown ("no such user" takes as long as "wrong password")"""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = hash_password('not-a-real-password-' + new_token())
        return _dummy_hash


def _not_before(t0: float, ms: int) -> None:
    wait = t0 + ms / 1000 - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _lower(v: Any) -> str:
    return js_trim(js_str(coalesce(v, ''))).lower()


def _candidates(body: Any) -> list[str]:
    from app.runtime import password_candidates

    return password_candidates(body)


def reset_info_shared(req: Req) -> Any:
    """GET /auth/reset-info/:token — the shared handler of src/mock/routes/people.ts (UI-03): tells the set-password
    form whether the token is valid and the tenant's minimum length. people.py registers THIS function."""
    s = scope()
    d = get_db()
    t = next((x for x in d.reset_tokens if x['token'] == req.params.get('token')), None)
    if not t or t['used'] or t['expires_at'] < now_ms():
        from app.errors import err

        err(410, 'RESET_EXPIRED', 'This reset link has expired or was already used. Request a new one.')
    assert t is not None
    u = s.find_by('users', 'id', t['user_id'])
    tenant = s.find_by('tenants', 'id', u.get('tenant_id')) if u else None
    email = coalesce(u.get('email', UNDEF) if u else UNDEF, '')
    m = re.match(r'^(.)(.*)(@.*)$', email, re.S) if isinstance(email, str) else None
    hint = (m.group(1) + '•' * min(6, len(m.group(2))) + m.group(3)) if m else email
    return {
        'min_length': coalesce(tenant['settings'].get('security.password_min_length', UNDEF) if tenant else UNDEF, 10),
        'email_hint': hint,
        'expires_in_minutes': max(1, js_round((t['expires_at'] - now_ms()) / 60000)),
    }


def register_overrides(r: Any, d: OverrideDeps) -> None:
    rt = d.rt

    def run(
        mode: str, tenant_id: str | None, fn: Callable[[], Any], *, is_write: bool, emails: list[str] | None = None, probe: bool = False, candidates: Any = ()
    ) -> Any:
        return rt.run_scoped(mode, tenant_id, lambda _s: fn(), is_write=is_write, emails=emails or [], probe=probe, candidates=candidates)

    # POST /auth/login — tenant of the e-mail; lockout + credentials + session through the virtual tables
    def login(req: Req) -> Any:
        email = _lower(req.b('email'))
        u = rt.backend.user_by_email(email) if email else None
        if not u:
            try:
                verify_password(js_str(coalesce(req.b('password'), '')), _dummy())
            except Exception:  # noqa: BLE001
                pass
        return run(
            'tenant',
            u['tenant_id'] if u else '__none__',
            lambda: A.login(req),
            is_write=True,
            emails=[email] if email else [],
            candidates=_candidates(req.body),
        )

    r.override('POST', '/auth/login', login, public=True)

    # POST /auth/login/mfa — the challenge token carries its tenant as a routing prefix ("<tenant>.<random>")
    def login_mfa(req: Req) -> Any:
        token = js_str(coalesce(req.b('mfa_token'), ''))
        m = re.match(r'^([A-Za-z0-9_-]{1,64})\.', token)
        prefix = m.group(1) if m else None
        tenant_id = prefix or '__none__'
        id_ = sha256(token)
        email = None
        if prefix:

            def find_email() -> Any:
                dd = get_db()
                ch = next((t for t in dd.pending_tokens if t.get('kind') == 'mfa_login' and t.get('id') == id_), None)
                if not ch:
                    return None
                u = next((x for x in dd.users if x['id'] == ch['user_id']), None)
                return u['email'].lower() if u else None

            email = run('tenant', tenant_id, find_email, is_write=False)
        return run('tenant', tenant_id, lambda: A.login_mfa(req), is_write=True, emails=[email] if email else [])

    r.override('POST', '/auth/login/mfa', login_mfa, public=True)

    # POST /auth/signup — a brand-new tenant (fresh scope); other tenants' users are probed for the e-mail check
    def signup(req: Req) -> Any:
        email = _lower(req.b('email'))
        return run('fresh', None, lambda: A.signup(req), is_write=True, emails=[email] if email else [], probe=True)

    r.override('POST', '/auth/signup', signup, public=True)

    # POST /auth/reset-request — always the same answer (and at least 400 ms), whether or not the e-mail exists; the
    # random token is stored as SHA-256 only and the link goes out by e-mail (platform message, sealed in the queue)
    def reset_request(req: Req) -> Any:
        t0 = time.monotonic()
        email = _lower(req.b('email'))
        u = rt.backend.user_by_email(email) if email else None

        def fn() -> Any:
            token = A.issue_reset_token(req)
            if token and u:
                user = scope().find_by('users', 'id', u['id'])
                link = f'{re.sub(r"/$", "", d.app_url)}/reset-password?token={encode_uri_component(token)}'
                subject, body = reset_mail(js_str(coalesce(user.get('name') if user else None, '')), link)
                scope().queue_platform_mail(email, subject, body, expires_at=iso_from_ms(now_ms() + RESET_MINUTES * 60000), tenant_id=u['tenant_id'])
            return {'accepted': True}

        out = run('tenant', u['tenant_id'] if u else '__none__', fn, is_write=True)
        _not_before(t0, 400)  # every answer takes at least 400 ms: the timing does not tell whether the account exists
        return out

    r.override('POST', '/auth/reset-request', reset_request, public=True)

    # reset-info / reset — the token is looked up by its hash
    def reset_info(req: Req) -> Any:
        h = sha256(js_str(coalesce(req.params.get('token'), '')))
        tenant_id = rt.backend.reset_token_tenant(h) or '__none__'
        shared = r.shared('GET', '/auth/reset-info/:token')
        handler = shared.handler if shared else reset_info_shared
        return run(
            'tenant',
            tenant_id,
            lambda: handler(Req(params={**req.params, 'token': h}, query=req.query, body=req.body, if_match=req.if_match, ctx=req.ctx, headers=req.headers)),
            is_write=False,
        )

    r.override('GET', '/auth/reset-info/:token', reset_info, public=True, tag='people')

    def reset(req: Req) -> Any:
        h = sha256(js_str(coalesce(req.b('token'), '')))
        tenant_id = rt.backend.reset_token_tenant(h) or '__none__'
        body = {**req.body, 'token': h} if isinstance(req.body, dict) else {'token': h}
        return run(
            'tenant',
            tenant_id,
            lambda: A.reset(Req(params=req.params, query=req.query, body=body, if_match=req.if_match, ctx=req.ctx, headers=req.headers)),
            is_write=True,
        )

    r.override('POST', '/auth/reset', reset, public=True)

    # invites — the token lives in the tenant's invites table
    def invite_info(req: Req) -> Any:
        tenant_id = rt.backend.invite_tenant(js_str(coalesce(req.params.get('token'), ''))) or '__none__'
        return run('tenant', tenant_id, lambda: A.invite_info(req), is_write=False)

    r.override('GET', '/invites/:token', invite_info, public=True)

    def invite_accept(req: Req) -> Any:
        tenant_id = rt.backend.invite_tenant(js_str(coalesce(req.params.get('token'), ''))) or '__none__'
        return run('tenant', tenant_id, lambda: A.invite_accept(req), is_write=True, candidates=_candidates(req.body))

    r.override('POST', '/invites/:token/accept', invite_accept, public=True)

    # POST /me/password — shared logic, plus: every OTHER session of the user is revoked
    def me_password(req: Req) -> Any:
        out = A.me_password(req)
        current = (req.headers.get('Authorization') or '').replace('Bearer ', '', 1)
        dd = get_db()
        uid = req.ctx.user_id
        dd.sessions = [x for x in dd.sessions if x['user_id'] != uid or x['token'] == current]
        return out

    r.override('POST', '/me/password', me_password)
