"""Port of src/mock/routes/auth.ts (shared handlers). The cross-tenant routes (login, login/mfa, signup, reset,
invites, /me/password) are wrapped by app/auth/overrides.py exactly like server/src/auth/overrides.ts
wraps the TS handlers: the override locates the tenant, opens a unit of work for it and runs these handlers."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    err,
    get_db,
    insert,
    now_iso,
    patch,
    require_action,
    role_def,
    rows,
    scope,
    uid,
    with_access,
)
from app.domain.calendar import add_days, today_iso
from app.domain.password_policy import check_password
from app.domain.totp import random_token, sha256_hex
from app.engine.two_factor import MFA_LOGIN_MINUTES, MFA_MAX_ATTEMPTS, two_factor_record, use_second_factor
from app.js import UNDEF, coalesce, iso_from_ms, js_number, js_spread, js_str, js_trim, now_ms, nullish, prop

DEFAULT_SETTINGS = cat('mock/seed')['DEFAULT_SETTINGS']
DEFAULT_THEME = cat('mock/seed')['DEFAULT_THEME']


def public_user(u: dict) -> dict:
    return u


def issue(user: dict) -> dict[str, Any]:
    """a new session for the user (the token is 32 random bytes; only its SHA-256 is stored)"""
    from app.auth.crypto import new_token

    s = scope()
    d = get_db()
    token = new_token()
    d.sessions = [*d.sessions, {'token': token, 'user_id': user['id'], 'tenant_id': user['tenant_id'], 'created_at': now_iso()}]
    user = s.patch('users', user, {'last_login_at': now_iso()})
    tenant = s.find_by('tenants', 'id', user['tenant_id'])
    return {'token': token, 'user': with_access(user), 'tenant': tenant}


def mfa_challenge(user: dict) -> dict[str, Any]:
    """a sign-in that passed the password and waits for the second step; the token carries the tenant id as a
    routing prefix; only its SHA-256 is stored"""
    d = get_db()
    now = now_iso()
    token = f'{user["tenant_id"]}.{random_token()}'
    d.pending_tokens = [
        t
        for t in d.pending_tokens
        if not (t.get('kind') == 'mfa_login' and t.get('tenant_id') == user['tenant_id'] and (t.get('expires_at') <= now or t.get('user_id') == user['id']))
    ]
    scope().append(
        'pending_tokens',
        {
            'id': sha256_hex(token),
            'tenant_id': user['tenant_id'],
            'created_at': now,
            'updated_at': now,
            'created_by': user['id'],
            'version': 1,
            'kind': 'mfa_login',
            'user_id': user['id'],
            'ref_id': None,
            'payload': {'attempts': 0},
            'expires_at': iso_from_ms(now_ms() + MFA_LOGIN_MINUTES * 60_000),
        },
    )
    return {'mfa_required': True, 'mfa_token': token, 'expires_in': MFA_LOGIN_MINUTES * 60}


def tenant_of(ctx: Ctx) -> dict:
    return scope().find_by('tenants', 'id', ctx.tenant_id)  # type: ignore[return-value]


def _lower_email(v: Any) -> str:
    return js_trim(js_str(coalesce(v, ''))).lower()


def _settings_of(tenant: dict | None) -> Any:
    return coalesce(tenant.get('settings', UNDEF) if tenant else UNDEF, DEFAULT_SETTINGS)


def login(req: Req) -> Any:
    s = scope()
    d = get_db()
    email = _lower_email(req.b('email'))
    password = js_str(coalesce(req.b('password'), ''))
    user = next((u for u in d.users if u['email'].lower() == email), None)
    tenant = s.find_by('tenants', 'id', user['tenant_id']) if user else None
    st = _settings_of(tenant)
    window_ms = st['security.lockout_minutes'] * 60000
    now = now_ms()
    recent = [a for a in d.login_attempts if a['email'] == email and now - a['at'] < window_ms]
    if len(recent) >= st['security.lockout_attempts']:
        err(429, 'LOGIN_LOCKED', f'Too many attempts. Try again in {js_str(st["security.lockout_minutes"])} minutes or reset your password.')
    ok = user is not None and s.password_matches(user['id'], password)
    if not ok:
        d.login_attempts = [*d.login_attempts, {'email': email, 'at': now_ms()}]
        left = st['security.lockout_attempts'] - len(recent) - 1
        err(
            401,
            'LOGIN_FAILED',
            f'Email or password is incorrect. {js_str(left)} attempt{"" if left == 1 else "s"} left.' if left > 0 else 'Email or password is incorrect.',
        )
    assert user is not None
    if user.get('status') == 'deactivated':
        err(403, 'USER_DEACTIVATED', 'This account has been deactivated. Ask your admin to reactivate it.')
    if tenant and tenant.get('status') == 'suspended' and not user.get('is_super_admin'):
        err(403, 'TENANT_SUSPENDED', 'This workspace is suspended. Contact SeereonOS support.')
    # two-step verification: password right, no session yet — a short-lived single-use challenge (failed attempts are
    # NOT cleared here, so a stolen password cannot reset the lockout counter)
    if user.get('two_factor'):
        if two_factor_record(user):
            return mfa_challenge(user)
        user = s.patch('users', user, {'two_factor': False})  # flag without a factor (data from before real 2FA): heal
    d.login_attempts = [a for a in d.login_attempts if a['email'] != email]
    return issue(user)


def login_mfa(req: Req) -> Any:
    """second step of sign-in: {mfa_token, code} or {mfa_token, recovery_code} -> the normal sign-in payload"""
    s = scope()
    d = get_db()
    token = js_str(coalesce(req.b('mfa_token'), ''))
    id_ = sha256_hex(token) if token else ''
    ch = next((t for t in d.pending_tokens if t.get('kind') == 'mfa_login' and t.get('id') == id_), None) if id_ else None
    if not ch or ch['expires_at'] <= now_iso():
        err(401, 'MFA_EXPIRED', 'This sign-in has expired. Sign in again.')
    assert ch is not None
    user = next((u for u in d.users if u['id'] == ch['user_id'] and u['tenant_id'] == ch['tenant_id']), None)
    rec = two_factor_record(user) if user else None

    def burn(row: dict) -> None:
        d.pending_tokens = [t for t in d.pending_tokens if t is not row]

    if not user or not rec:
        burn(ch)
        err(401, 'MFA_EXPIRED', 'This sign-in has expired. Sign in again.')
    assert user is not None and rec is not None
    email = user['email'].lower()
    tenant = s.find_by('tenants', 'id', user['tenant_id'])
    st = _settings_of(tenant)
    now = now_ms()
    recent = [a for a in d.login_attempts if a['email'] == email and now - a['at'] < st['security.lockout_minutes'] * 60000]
    if len(recent) >= st['security.lockout_attempts']:
        burn(ch)
        err(429, 'LOGIN_LOCKED', f'Too many attempts. Try again in {js_str(st["security.lockout_minutes"])} minutes or reset your password.')
    rc = req.b('recovery_code')
    use_recovery = not nullish(rc) and js_trim(js_str(rc)) != ''
    if not use_recovery and not re.fullmatch(r'\d{6}', re.sub(r'\s', '', js_str(coalesce(req.b('code'), ''))), re.ASCII):
        err(422, 'TWO_FACTOR_CODE', 'Enter the 6-digit code from your authenticator app.', 'code')
    ok, rec = use_second_factor(rec, recovery_code=rc) if use_recovery else use_second_factor(rec, code=req.b('code'))
    if not ok:
        # counted toward the account lockout; the challenge burns after MFA_MAX_ATTEMPTS wrong codes
        d.login_attempts = [*d.login_attempts, {'email': email, 'at': now_ms()}]
        payload = ch.get('payload')
        attempts = js_number(coalesce(payload.get('attempts', UNDEF) if isinstance(payload, dict) else UNDEF, 0)) + 1
        if attempts >= MFA_MAX_ATTEMPTS:
            burn(ch)
            err(401, 'MFA_LOCKED', 'Too many wrong codes. Sign in again.')
        s.patch('pending_tokens', ch, {'payload': {'attempts': attempts}, 'updated_at': now_iso(), 'version': ch['version'] + 1})
        left = MFA_MAX_ATTEMPTS - attempts
        lead = 'That recovery code didn’t work.' if use_recovery else 'That code didn’t work. Check your authenticator app and try again.'
        err(401, 'MFA_CODE_INVALID', f'{lead} {js_str(left)} attempt{"" if left == 1 else "s"} left.', 'recovery_code' if use_recovery else 'code')
    if user.get('status') == 'deactivated':
        err(403, 'USER_DEACTIVATED', 'This account has been deactivated. Ask your admin to reactivate it.')
    if tenant and tenant.get('status') == 'suspended' and not user.get('is_super_admin'):
        err(403, 'TENANT_SUSPENDED', 'This workspace is suspended. Contact SeereonOS support.')
    burn(ch)
    d.login_attempts = [a for a in d.login_attempts if a['email'] != email]
    out = issue(s.find_by('users', 'id', user['id']) or user)
    return {**out, 'recovery_codes_left': len(rec['payload']['recovery'])} if ok == 'recovery' else out


def signup(req: Req) -> Any:
    """POST /auth/signup — a new workspace (fresh scope) and its first administrator, signed in"""
    return issue(create_workspace(req))


def create_workspace(req: Req) -> dict:
    """the provisioning of a new workspace (tenant, roles, master data, templates, workflows, subscription, number
    sequences) and its administrator with a password — shared by sign-up and `python -m app.cli create-tenant`"""
    from app.ctx import Ctx
    from app.seeds.access import provision_tenant

    s = scope()
    d = get_db()
    company = js_trim(js_str(coalesce(req.b('company'), '')))
    name = js_trim(js_str(coalesce(req.b('name'), '')))
    email = _lower_email(req.b('email'))
    password = js_str(coalesce(req.b('password'), ''))
    if len(company) < 2:
        err(422, 'VALIDATION', 'Enter your company name.', 'company')
    if len(name) < 2:
        err(422, 'VALIDATION', 'Enter your name.', 'name')
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        err(422, 'VALIDATION', 'Enter a valid work email.', 'email')
    weak = check_password(password, min=10, email=email)
    if weak:
        err(422, weak['code'], weak['message'], 'password')
    if any(u['email'].lower() == email for u in d.users):
        err(409, 'USER_EMAIL_TAKEN', 'An account with this email already exists. Sign in instead.', 'email')
    today = today_iso()
    tenant_id = 'tn_' + uid()
    slug = re.sub(r'^-|-$', '', re.sub(r'[^a-z0-9]+', '-', company.lower()))[:30]
    free_slug = getattr(s.backend, 'free_slug', None)  # unique across every tenant (two companies may share a name)
    if free_slug is not None:
        slug = free_slug(slug)
    tenant: dict[str, Any] = {
        'id': tenant_id,
        'name': company,
        'slug': slug,
        'status': 'trial',
        'industry': coalesce(req.b('industry'), 'it_services'),
        'trial_ends_on': add_days(today, 14),
        'time_zone': 'Asia/Kolkata',
        'plan_code': 'trial',
        'seats': 5,
        'created_at': now_iso(),
        'logo_data_url': None,
        'theme': dict(DEFAULT_THEME),
        'settings': {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()},
        'version': 1,
    }
    s.append('tenants', tenant)
    t0 = now_iso()
    tenant = s.replace_row(
        'tenants',
        tenant,
        provision_tenant(s, tenant, lambda: {'id': uid(), 'tenant_id': tenant_id, 'created_at': t0, 'updated_at': t0, 'created_by': None, 'version': 1}),
    )
    ctx = Ctx.synthetic_ctx(tenant, {'id': 'system'})
    import random

    user = insert(
        ctx,
        'users',
        {
            'email': email,
            'name': name,
            'title': 'Founder',
            'role': 'admin',
            'role_key': 'administrator',
            'extra_permissions': [],
            'revoked_permissions': [],
            'reports_to_id': None,
            'grants': [],
            'status': 'active',
            'last_login_at': None,
            'avatar_hue': int(random.random() * 360),
            'phone': None,
            'joined_on': today,
            'department': 'management',
            'two_factor': False,
            'prefs': {},
        },
    )
    s.set_password(user['id'], password)
    insert(
        ctx,
        'subscriptions',
        {'plan_code': 'trial', 'status': 'trialing', 'current_period_end': add_days(today, 14), 'seats': 5, 'razorpay_subscription_id': None},
    )
    s.append('number_sequences', {'tenant_id': tenant_id, 'scope': 'project', 'next_value': 1})
    return user


def logout(req: Req) -> Any:
    d = get_db()
    token = (req.headers.get('Authorization') or '').replace('Bearer ', '', 1) if req.headers.get('Authorization') is not None else UNDEF
    d.sessions = [x for x in d.sessions if x['token'] != token]
    return {'ok': True}


def issue_reset_token(req: Req) -> str | None:
    """a single-use reset token (60 min) for the account of `email`, or None when there is no such account; only its
    SHA-256 is stored (the flush hashes it). The caller e-mails the link (app/auth/overrides.py)."""
    from app.auth.crypto import new_token

    d = get_db()
    email = _lower_email(req.b('email'))
    user = next((u for u in d.users if u['email'].lower() == email), None)
    if not user:
        return None
    token = new_token()
    d.reset_tokens = [*d.reset_tokens, {'token': token, 'user_id': user['id'], 'expires_at': now_ms() + 60 * 60000, 'used': False}]
    return token


def reset_request(req: Req) -> Any:
    """UI-03 — always accepted, so the form never reveals whether an email exists (the link is e-mailed)."""
    issue_reset_token(req)
    return {'accepted': True}


def reset(req: Req) -> Any:
    s = scope()
    d = get_db()
    tok = req.b('token')
    t = next((x for x in d.reset_tokens if x['token'] == tok), None)
    if not t or t['used'] or t['expires_at'] < now_ms():
        err(410, 'RESET_EXPIRED', 'This reset link has expired or was already used. Request a new one.')
    assert t is not None
    user = s.find_by('users', 'id', t['user_id'])
    assert user is not None
    tenant = s.find_by('tenants', 'id', user['tenant_id'])
    weak = check_password(
        js_str(coalesce(req.b('password'), '')),
        min=coalesce(tenant['settings'].get('security.password_min_length', UNDEF) if tenant else UNDEF, 10),
        email=user['email'],
    )
    if weak:
        err(422, weak['code'], weak['message'], 'password')
    s.set_password(user['id'], req.b('password'))
    d.reset_tokens = [{**x, 'used': True} if x is t else x for x in d.reset_tokens]
    d.sessions = [x for x in d.sessions if x['user_id'] != user['id']]
    return {'ok': True}


def invite_info(req: Req) -> Any:
    s = scope()
    d = get_db()
    inv = next((i for i in d.invites if i.get('token') == req.params.get('token')), None)
    if not inv or inv.get('accepted_at') or inv['expires_at'] < now_iso():
        err(410, 'INVITE_EXPIRED', 'This invitation has expired or was already used. Ask your admin to send a new one.')
    assert inv is not None
    tenant = s.find_by('tenants', 'id', inv['tenant_id'])
    mgr = s.find_by('users', 'id', inv.get('reports_to_id')) if not nullish(inv.get('reports_to_id')) else None
    rd = role_def(tenant['id'], prop(inv, 'role_key'))  # type: ignore[index]
    return {
        'email': inv['email'],
        'name': prop(inv, 'name'),
        'role': prop(inv, 'role'),
        'role_name': coalesce(rd.get('name', UNDEF) if rd else UNDEF, None),
        'company': tenant['name'],  # type: ignore[index]
        'reports_to': coalesce(mgr.get('name', UNDEF) if mgr else UNDEF, None),
        'theme': prop(tenant, 'theme'),
        'logo_data_url': prop(tenant, 'logo_data_url'),
    }


def invite_accept(req: Req) -> Any:
    import random

    from app.ctx import Ctx

    s = scope()
    d = get_db()
    inv = next((i for i in d.invites if i.get('token') == req.params.get('token')), None)
    if not inv or inv.get('accepted_at') or inv['expires_at'] < now_iso():
        err(410, 'INVITE_EXPIRED', 'This invitation has expired or was already used.')
    assert inv is not None
    tenant = s.find_by('tenants', 'id', inv['tenant_id'])
    assert tenant is not None
    weak = check_password(js_str(coalesce(req.b('password'), '')), min=tenant['settings'].get('security.password_min_length', UNDEF), email=inv['email'])
    if weak:
        err(422, weak['code'], weak['message'], 'password')
    if len(js_trim(js_str(coalesce(req.b('name'), '')))) < 2:
        err(422, 'VALIDATION', 'Enter your name.', 'name')
    ctx = Ctx.synthetic_ctx(tenant, {'id': coalesce(prop(inv, 'created_by'), 'system')})
    user = next((u for u in d.users if u['email'] == inv['email'] and u['tenant_id'] == tenant['id']), None)
    name = js_trim(js_str(req.b('name')))
    if not user:
        rd = role_def(tenant['id'], prop(inv, 'role_key'))
        role = prop(inv, 'role')
        user = insert(
            ctx,
            'users',
            {
                'email': inv['email'],
                'name': name,
                'title': coalesce(req.b('title'), ''),
                'role': coalesce(rd.get('persona', UNDEF) if rd else UNDEF, role),
                'role_key': coalesce(prop(inv, 'role_key'), 'employee'),
                'extra_permissions': [],
                'revoked_permissions': [],
                'reports_to_id': prop(inv, 'reports_to_id'),
                'grants': [],
                'status': 'active',
                'last_login_at': None,
                'avatar_hue': int(random.random() * 360),
                'phone': None,
                'joined_on': today_iso(),
                'department': 'sales' if role == 'sales' else 'operations' if role == 'ops' else 'delivery',
                'two_factor': False,
                'prefs': {},
            },
        )
    else:
        user = s.patch('users', user, {'status': 'active', 'name': name})
    s.set_password(user['id'], req.b('password'))
    s.patch('invites', inv, {'accepted_at': now_iso()})
    return issue(user)


def me(req: Req) -> Any:
    ctx = req.ctx
    tenant = None if ctx.is_super else tenant_of(ctx)
    rd = role_def(ctx.tenant_id, ctx.role_key)
    return {
        'user': with_access(ctx.user),
        'tenant': tenant,
        'role': ctx.role,
        'real_role': ctx.real_role,
        'grants': ctx.grants,
        'view_as': ctx.view_as,
        'is_super_admin': ctx.is_super,
        'permissions': sorted(ctx.perms),
        'role_def': {'key': rd['key'], 'name': rd['name'], 'persona': rd['persona']} if rd else None,
    }


def me_prefs(req: Req) -> Any:
    s = scope()
    u = s.find_by('users', 'id', req.ctx.user_id)
    return s.patch('users', u, {'prefs': {**js_spread(prop(u, 'prefs')), **js_spread(req.body)}})  # type: ignore[arg-type]


def me_profile(req: Req) -> Any:
    s = scope()
    u = s.find_by('users', 'id', req.ctx.user_id)
    ch: dict[str, Any] = {}
    if req.b('name') is not UNDEF:
        if len(js_trim(js_str(req.b('name')))) < 2:
            err(422, 'VALIDATION', 'Enter your name.', 'name')
        ch['name'] = js_trim(js_str(req.b('name')))
    if req.b('phone') is not UNDEF:
        ch['phone'] = req.b('phone')
    if req.b('title') is not UNDEF:
        ch['title'] = req.b('title')
    ch['updated_at'] = now_iso()
    ch['version'] = u['version'] + 1  # type: ignore[index]
    return s.patch('users', u, ch)  # type: ignore[arg-type]


def me_password(req: Req) -> Any:
    s = scope()
    ctx = req.ctx
    uid_ = ctx.user_id
    cur = req.b('current')
    if not s.has_password(uid_) or not s.password_matches(uid_, cur):
        err(422, 'PASSWORD_WRONG', 'Current password is incorrect.', 'current')
    settings = prop(ctx.tenant, 'settings')
    mn = coalesce(prop(settings, 'security.password_min_length') if not nullish(settings) else UNDEF, 10)
    weak = check_password(js_str(coalesce(req.b('next'), '')), min=mn, email=ctx.user.get('email'))
    if weak:
        err(422, weak['code'], weak['message'], 'next')
    if req.b('next') == cur and type(req.b('next')) is type(cur):
        err(422, 'PASSWORD_REUSED', 'Choose a password different from the current one.', 'next')
    s.set_password(uid_, req.b('next'))
    me_row = s.patch('users', s.find_by('users', 'id', uid_), {'must_change_password': False})  # type: ignore[arg-type]
    return {'ok': True, 'user': with_access(me_row)}


def directory(req: Req) -> Any:
    out = []
    for u in rows(req.ctx, 'users'):
        rd = role_def(u.get('tenant_id'), u.get('role_key'))
        out.append(
            {
                'id': u['id'],
                'name': prop(u, 'name'),
                'title': prop(u, 'title'),
                'role': prop(u, 'role'),
                'role_key': prop(u, 'role_key'),
                'role_name': coalesce(rd.get('name', UNDEF) if rd else UNDEF, prop(u, 'title')),
                'email': prop(u, 'email'),
                'reports_to_id': prop(u, 'reports_to_id'),
                'avatar_hue': prop(u, 'avatar_hue'),
                'status': prop(u, 'status'),
                'department': prop(u, 'department'),
            }
        )
    return out


def search(req: Req) -> Any:
    """global search for the command palette (respects role visibility). Needs the helpers of the tasks, documents,
    workflowsV7 and docsV7 modules (can_see_task, visible_documents, can_view, can_see_issued)."""
    ctx = req.ctx
    q = js_trim(js_str(coalesce(req.q('q'), ''))).lower()
    if len(q) < 2:
        return []
    from app.routes.docs_v7 import can_see_issued  # type: ignore[import-not-found]
    from app.routes.documents import visible_documents  # type: ignore[import-not-found]
    from app.routes.tasks import can_see_task  # type: ignore[import-not-found]
    from app.routes.workflows_v7 import can_view as can_view_workflow  # type: ignore[import-not-found]

    out: list[dict[str, Any]] = []
    see_all = 'projects.view_all' in ctx.perms
    uid_ = ctx.user_id
    my_task_projects = {t.get('project_id') for t in rows(ctx, 'tasks') if t.get('owner_id') == uid_ or t.get('backup_id') == uid_}
    for p in rows(ctx, 'projects'):
        if (see_all or p['id'] in my_task_projects) and q in (
            js_str(p.get('name', UNDEF)) + ' ' + js_str(p.get('client', UNDEF)) + ' ' + js_str(p.get('code', UNDEF))
        ).lower():
            out.append(
                {
                    'kind': 'Project',
                    'id': p['id'],
                    'title': prop(p, 'name'),
                    'subtitle': f'{js_str(prop(p, "code"))} · {js_str(prop(p, "client"))}',
                    'to': f'/projects/{p["id"]}',
                }
            )
    projects_by_id = scope().index('projects', 'id')
    for t in rows(ctx, 'tasks'):
        if can_see_task(ctx, t) and q in js_str(t['title']).lower():
            p = next((x for x in rows(ctx, 'projects') if x['id'] == t.get('project_id')), None)
            out.append(
                {'kind': 'Task', 'id': t['id'], 'title': t['title'], 'subtitle': coalesce(p.get('name', UNDEF) if p else UNDEF, ''), 'to': f'task:{t["id"]}'}
            )
    if 'deals.edit_own' in ctx.perms or 'deals.view_all' in ctx.perms:
        for dl in rows(ctx, 'deals'):
            if ('deals.view_all' in ctx.perms or dl.get('owner_id') == uid_) and q in (js_str(prop(dl, 'title')) + ' ' + js_str(prop(dl, 'client'))).lower():
                out.append({'kind': 'Deal', 'id': dl['id'], 'title': prop(dl, 'title'), 'subtitle': prop(dl, 'client'), 'to': f'/sales/{dl["id"]}'})
    for u in rows(ctx, 'users'):
        if q in (js_str(prop(u, 'name')) + ' ' + js_str(prop(u, 'email')) + ' ' + js_str(prop(u, 'title'))).lower():
            out.append(
                {
                    'kind': 'Person',
                    'id': u['id'],
                    'title': prop(u, 'name'),
                    'subtitle': prop(u, 'title'),
                    'to': f'/people?focus={u["id"]}' if 'users.view' in ctx.perms else '/team',
                }
            )
    if 'estimates.manage' in ctx.perms:
        for e in rows(ctx, 'cost_estimates'):
            num = coalesce(prop(e, 'number'), '')
            if q in (js_str(prop(e, 'name')) + ' ' + js_str(prop(e, 'client')) + ' ' + js_str(num)).lower():
                out.append(
                    {
                        'kind': 'Estimate',
                        'id': e['id'],
                        'title': prop(e, 'name'),
                        'subtitle': f'{js_str(prop(e, "number")) + " · " if e.get("number") else ""}{js_str(prop(e, "client"))}',
                        'to': f'/projects/cost-calculator/{e["id"]}',
                    }
                )
    if 'finance.view' in ctx.perms:
        for i in rows(ctx, 'invoices'):
            p = projects_by_id.get(i.get('project_id'))
            bill_to = prop(i, 'bill_to')
            client = coalesce(bill_to.get('name', UNDEF) if isinstance(bill_to, dict) else UNDEF, p.get('client', UNDEF) if p else UNDEF, '')
            if q in (js_str(prop(i, 'number')) + ' ' + js_str(client) + ' ' + js_str(coalesce(p.get('name', UNDEF) if p else UNDEF, ''))).lower():
                out.append(
                    {
                        'kind': 'Invoice',
                        'id': i['id'],
                        'title': i.get('number') or 'Draft invoice',
                        'subtitle': f'{js_str(client)}{" · " + js_str(p.get("code", UNDEF)) if p else ""}',
                        'to': f'/finance/invoices/{i["id"]}',
                    }
                )
    for dc in visible_documents(ctx):
        if q in (js_str(prop(dc, 'title')) + ' ' + ' '.join(js_str(x) for x in dc['tags'])).lower():
            out.append(
                {
                    'kind': 'Document',
                    'id': dc['id'],
                    'title': prop(dc, 'title'),
                    'subtitle': 'Project document' if dc.get('project_id') else 'Company library',
                    'to': f'/documents?doc={dc["id"]}',
                }
            )
    users = scope().index('users', 'id')
    for dc in rows(ctx, 'issued_documents'):
        emp = users.get(dc.get('employee_id'))
        emp_name = coalesce(emp.get('name', UNDEF) if emp else UNDEF, '')
        if can_see_issued(ctx, dc) and q in (js_str(prop(dc, 'title')) + ' ' + js_str(coalesce(prop(dc, 'ref_no'), '')) + ' ' + js_str(emp_name)).lower():
            out.append(
                {
                    'kind': 'Document',
                    'id': dc['id'],
                    'title': prop(dc, 'title'),
                    'subtitle': f'{js_str(dc["ref_no"]) + " · " if dc.get("ref_no") else ""}{js_str(emp_name)}',
                    'to': f'/documents/issued/{dc["id"]}',
                }
            )
    if 'documents.templates' in ctx.perms:
        for t in rows(ctx, 'doc_templates'):
            if q in js_str(t['name']).lower():
                out.append({'kind': 'Template', 'id': t['id'], 'title': t['name'], 'subtitle': 'Document template', 'to': f'/documents/templates/{t["id"]}'})
    for w in rows(ctx, 'wf_instances'):
        if q in js_str(w['title']).lower() and can_view_workflow(ctx, w):
            out.append(
                {
                    'kind': 'Workflow',
                    'id': w['id'],
                    'title': w['title'],
                    'subtitle': js_str(w['status']).replace('_', ' ', 1),
                    'to': f'/workflows/run/{w["id"]}',
                }
            )
    if 'workflows.design' in ctx.perms:
        for t in rows(ctx, 'wf_templates'):
            if q in js_str(t['name']).lower():
                out.append({'kind': 'Workflow', 'id': t['id'], 'title': t['name'], 'subtitle': 'Workflow template', 'to': f'/workflows/designer/{t["id"]}'})
    return out[:24]


def tenant_get(req: Req) -> Any:
    t = tenant_of(req.ctx)
    return UNDEF if t is None else t  # .find() → undefined (the platform admin has no tenant row): no `data` key


THEME_KEYS = ('preset', 'primary', 'accent', 'radius', 'density', 'font', 'mode', 'sidebar')


def tenant_theme(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'theme.edit', 'Only admins can change the workspace theme.')
    t = tenant_of(ctx)
    if req.if_match is not UNDEF and req.if_match != t['version']:
        err(409, 'STALE', 'Settings were changed by someone else. Reload and try again.')
    hexre = re.compile(r'^#[0-9a-fA-F]{6}$')
    primary, accent = req.b('primary'), req.b('accent')
    if primary and not hexre.match(js_str(primary)):
        err(422, 'VALIDATION', 'Primary colour must be a hex value like #0C6B4E.', 'primary')
    if accent and not hexre.match(js_str(accent)):
        err(422, 'VALIDATION', 'Accent colour must be a hex value like #B8912B.', 'accent')
    p = {k: req.b(k) for k in THEME_KEYS if isinstance(req.b(k), str)}
    return patch('tenants', t, {'theme': {**js_spread(prop(t, 'theme')), **p}, 'version': t['version'] + 1})


def tenant_logo(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'settings.edit', 'Only admins can change the company logo.')
    t = tenant_of(ctx)
    data = coalesce(req.b('data_url'), None)
    if data and not re.match(r'^data:image/(png|svg\+xml|jpeg|webp);base64,', js_str(data)):
        err(422, 'FILE_REJECTED', 'Upload a PNG, SVG, JPG or WebP image.')
    if data and isinstance(data, str) and len(data) > 1.4 * 1024 * 1024:
        err(422, 'FILE_REJECTED', 'Logo must be 1 MB or smaller.')
    return patch('tenants', t, {'logo_data_url': data, 'version': t['version'] + 1})


def register(r: Any) -> None:
    r.post('/auth/login', login, public=True)
    r.post('/auth/login/mfa', login_mfa, public=True)
    r.post('/auth/signup', signup, public=True)
    r.post('/auth/logout', logout, allow_view_as_write=True)
    r.post('/auth/reset-request', reset_request, public=True)
    r.post('/auth/reset', reset, public=True)
    r.get('/invites/:token', invite_info, public=True)
    r.post('/invites/:token/accept', invite_accept, public=True)
    r.get('/me', me)
    r.patch('/me/prefs', me_prefs, allow_view_as_write=True)
    r.patch('/me/profile', me_profile)
    r.post('/me/password', me_password)
    r.get('/directory', directory)
    r.get('/search', search)
    r.get('/tenant', tenant_get)
    r.put('/tenant/theme', tenant_theme)
    r.put('/tenant/logo', tenant_logo)
