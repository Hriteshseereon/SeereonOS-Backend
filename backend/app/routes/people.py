"""Module A — people & users, Create employee, invites, delegations, sessions, 2FA — port of src/mock/routes/people.ts.

Capability rules (v4 — permissions only, never the persona):
  * GET /users                      users.view
  * job fields (PATCH /users/:id)   employees.edit, applied directly
  * access fields (role, extra/revoked permissions)  users.manage, maker-checked (entity user_access)
  * POST /employees                 employees.create, maker-checked (entity employee, action create)
  * deactivate                      employees.deactivate, maker-checked (entity employee, action delete)
  * reset password                  users.manage
Escalation guard: nobody can grant a permission they don't hold, and only holders of users.manage can give someone a
role that manages roles or users.

Sessions live in the platform table (token = SHA-256 of the bearer token; the request's Authorization header carries
the same hash), so deactivation / password reset / 2FA changes revoke them exactly like the Node reference.
`GET /auth/reset-info/:token` is the shared handler of app/auth/overrides.py (the server override wraps it).
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from app.auth.overrides import reset_info_shared
from app.ctx import (
    Ctx,
    Req,
    calendar_for,
    err,
    find,
    find_by,
    get_db,
    has,
    insert,
    log_activity,
    notify,
    now_iso,
    patch,
    permissions_of,
    remove,
    remove_where,
    require_action,
    require_any,
    role_def,
    rows,
    scope,
    update,
    user_name,
    warn,
    with_access,
)
from app.domain.calendar import add_days, avg_working_days_per_month, fy_start_year, is_real_iso_date, month_of, today_iso, working_days_in_month
from app.domain.costing import cost_per_hour
from app.domain.permission_catalog import effective_permissions
from app.domain.totp import hash_recovery_code, new_recovery_codes, new_totp_secret, otpauth_url, random_token, verify_totp
from app.engine.docgen import ensure_doc_defaults
from app.engine.email import send_email
from app.engine.maker_checker import needs_checker, pending_brief, register_applier
from app.engine.secrets import open_secret, seal_secret
from app.engine.two_factor import FAR_FUTURE, TWO_FACTOR_SETUP_ATTEMPTS, TWO_FACTOR_SETUP_MINUTES, touch, two_factor_record, use_second_factor
from app.errors import ApiError
from app.js import (
    UNDEF,
    coalesce,
    fmt_date_en_in,
    is_finite,
    iso_from_ms,
    js_floor,
    js_len,
    js_max,
    js_number,
    js_round,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    now_ms,
    nullish,
    pad_start,
    prop,
)
from app.routes.access import assert_no_lockout, clean_permissions, diff_summary, is_access_admin, perm_label, propose_named
from app.routes.auth import tenant_of

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+\Z')
PHONE_RE = re.compile(r'^[+0-9][0-9\s-]{6,16}\Z')
SIX_DIGITS = re.compile(r'^[0-9]{6}\Z')
INVITE_DAYS = 7
TRIAL_SEAT_CAP = 25
EMPLOYMENT_TYPES = ('full_time', 'part_time', 'contract', 'intern')
DEPT_FROM_PERSONA = {'admin': 'management', 'pm': 'delivery', 'employee': 'delivery', 'sales': 'sales', 'ops': 'operations'}


def _date_ok(s: Any) -> bool:
    """DATE_RE.test(s) = isRealISODate(s)"""
    return is_real_iso_date(s)


def _s(v: Any) -> str:
    """String(v ?? '')"""
    return js_str(coalesce(v, ''))


def _bearer(req: Req) -> Any:
    """headers['Authorization']?.replace('Bearer ', '')"""
    a = req.headers.get('Authorization')
    return a.replace('Bearer ', '', 1) if a is not None else UNDEF


def _invite_view(ctx: Ctx, i: dict) -> dict[str, Any]:
    status = 'accepted' if js_truthy(i.get('accepted_at')) else 'expired' if i['expires_at'] < now_iso() else 'pending'
    rd = role_def(ctx.tenant_id, i.get('role_key'))
    return {**i, 'status': status, 'link': f'/invite/{i["token"]}', 'role_name': coalesce(rd.get('name', UNDEF) if rd else UNDEF, prop(i, 'role'))}


# ---------------------------------------------------------------------------------------------------------------------
# User rows
# ---------------------------------------------------------------------------------------------------------------------
def _first_by_id(lst: list) -> dict:
    out: dict = {}
    for x in lst:
        k = x.get('id')
        if k not in out:
            out[k] = x
    return out


def _name_in(by_id: dict, id_: Any) -> Any:
    if not js_truthy(id_):
        return None
    try:
        x = by_id.get(id_)
    except TypeError:
        x = None
    return coalesce(x.get('name', UNDEF) if x else UNDEF, None)


def _user_rows(ctx: Ctx, lst: list) -> list[dict]:
    all_ = rows(ctx, 'users')
    by_user = _first_by_id(all_)
    depts = _first_by_id(rows(ctx, 'departments'))
    desig = _first_by_id(rows(ctx, 'designations'))
    grades = _first_by_id([{**g, 'name': f'{g["code"]} · {g["name"]}' if js_truthy(g.get('code')) else g.get('name', UNDEF)} for g in rows(ctx, 'grades')])
    locs = _first_by_id(rows(ctx, 'locations'))
    shifts = _first_by_id(rows(ctx, 'shifts'))
    ccs = _first_by_id(rows(ctx, 'cost_centres'))
    active_reports: dict = {}
    for x in all_:
        if x.get('status') == 'active':
            k = x.get('reports_to_id')
            active_reports[k] = active_reports.get(k, 0) + 1
    out = []
    for u in lst:
        rd = role_def(u.get('tenant_id'), u.get('role_key'))
        mgr = by_user.get(u['reports_to_id']) if js_truthy(u.get('reports_to_id')) else None
        out.append(
            {
                **with_access(u),
                'role_name': coalesce(rd.get('name', UNDEF) if rd else UNDEF, prop(u, 'title')),
                'permissions': permissions_of(u),
                'role_permissions': effective_permissions(rd['permissions']) if rd else [],
                'extra_permissions': coalesce(prop(u, 'extra_permissions'), []),
                'revoked_permissions': coalesce(prop(u, 'revoked_permissions'), []),
                'department_name': _name_in(depts, u.get('department_id')),
                'designation_name': _name_in(desig, u.get('designation_id')),
                'grade_name': _name_in(grades, u.get('grade_id')),
                'location_name': _name_in(locs, u.get('location_id')),
                'shift_name': _name_in(shifts, u.get('shift_id')),
                'cost_centre_name': _name_in(ccs, u.get('cost_centre_id')),
                'manager_name': (coalesce(mgr.get('name', UNDEF), None) if mgr else None) if js_truthy(u.get('reports_to_id')) else None,
                'direct_reports': active_reports.get(u['id'], 0),
                'pending_access': pending_brief(ctx, 'user_access', u['id']),
                'pending_employee': pending_brief(ctx, 'employee', u['id']),
            }
        )
    return out


def user_row(ctx: Ctx, u: dict) -> dict:
    return _user_rows(ctx, [u])[0]


# ---------------------------------------------------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------------------------------------------------
def _email_taken(ctx: Ctx, email: str, except_user_id: Any = UNDEF) -> str | None:
    d = get_db()
    if any(u['email'].lower() == email and u['id'] != except_user_id for u in d.users):
        return 'An account with this email already exists.'
    now = now_iso()
    if any(i['email'].lower() == email and not js_truthy(i.get('accepted_at')) and i['expires_at'] > now for i in rows(ctx, 'invites')):
        return 'This email already has a pending invite. Resend or revoke it under Invited.'
    for c in rows(ctx, 'change_sets'):
        if c.get('status') == 'pending' and c.get('entity') == 'employee' and c.get('action') == 'create':
            inp = prop(c.get('payload'), 'input') if isinstance(c.get('payload'), dict) else UNDEF
            em = prop(inp, 'email') if isinstance(inp, dict) else UNDEF
            if js_str(coalesce(em, '')) == email:
                return 'An employee with this email is already waiting for approval.'
    return None


def _assert_seats(ctx: Ctx, adding: int) -> None:
    """Seat rule: active users + pending invites may not exceed purchased seats (trial: soft cap 25)."""
    t = tenant_of(ctx)
    now = now_iso()
    used = sum(1 for u in rows(ctx, 'users') if u.get('status') == 'active' and not u.get('is_super_admin')) + sum(
        1 for i in rows(ctx, 'invites') if not js_truthy(i.get('accepted_at')) and i['expires_at'] > now
    )
    from app.routes.billing import seat_limit

    cap = seat_limit(t)  # trial: at least TRIAL_SEAT_CAP (the trial plan's 25)
    if used + adding > cap:
        err(
            409,
            'SEATS_FULL',
            f'Trials include up to {js_str(cap)} people. Choose a plan in Billing to add more.'
            if t['status'] == 'trial'
            else f'All {js_str(cap)} seats are in use. Add seats in Billing, or deactivate someone first.',
        )


def _creates_loop(ctx: Ctx, user_id: str, manager_id: Any) -> bool:
    """Would setting `manager_id` as `user_id`'s manager create a loop?"""
    if not js_truthy(manager_id):
        return False
    if manager_id == user_id:
        return True
    by_id = {u['id']: u for u in rows(ctx, 'users')}  # new Map(...) keeps the LAST duplicate (ids are unique anyway)
    cur = manager_id
    seen: set = set()
    while js_truthy(cur):
        if cur == user_id:
            return True
        if cur in seen:
            return True
        seen.add(cur)
        x = by_id.get(cur)
        cur = coalesce(x.get('reports_to_id', UNDEF) if x else UNDEF, None)
    return False


def _assert_manager(ctx: Ctx, user_id: Any, manager_id: Any) -> None:
    if not js_truthy(manager_id):
        return
    mgr = find(ctx, 'users', manager_id, 'Manager')
    if mgr.get('status') != 'active':
        err(422, 'VALIDATION', f'{js_str(prop(mgr, "name"))} is not active. Choose an active person as manager.', 'reports_to_id')
    if js_truthy(user_id) and manager_id == user_id:
        err(409, 'USER_REPORTS_LOOP', 'A person cannot report to themselves.', 'reports_to_id')
    if js_truthy(user_id) and _creates_loop(ctx, user_id, manager_id):
        err(409, 'USER_REPORTS_LOOP', 'This would create a reporting loop. Pick someone who is not in their reporting line.', 'reports_to_id')


MASTER_LABEL = {
    'departments': 'Department',
    'designations': 'Designation',
    'grades': 'Grade',
    'locations': 'Location',
    'shifts': 'Shift',
    'cost_centres': 'Cost centre',
}


def _master_ref(ctx: Ctx, table: str, id_: Any, field: str, current: Any = UNDEF) -> Any:
    v = coalesce(current, None) if id_ is UNDEF else (id_ if js_truthy(id_) else None)
    if not js_truthy(v):
        return None
    rec = next((x for x in rows(ctx, table) if x.get('id') == v and type(x.get('id')) is type(v)), None)
    if rec is None:
        err(422, 'VALIDATION', f'{MASTER_LABEL[table]} not found. Choose one from the list.', field)
    if not rec.get('active') and v != current:  # type: ignore[union-attr]
        err(422, 'VALIDATION', f'That {MASTER_LABEL[table].lower()} is inactive. Choose an active one.', field)
    return v


def _has_active(ctx: Ctx, table: str) -> bool:
    return any(x.get('active') for x in rows(ctx, table))


def _g(b: Any, k: str) -> Any:
    return b.get(k, UNDEF) if isinstance(b, dict) else UNDEF


def _sorted_strs(lst: Any) -> list:
    """[...x].sort() (code-unit order; the values are permission keys)"""
    return sorted(lst, key=lambda v: js_str(v))


def _read_access(ctx: Ctx, body: Any, cur: dict | None) -> dict[str, Any]:
    """Validate role + overrides, including the escalation guard."""
    role_key = js_str(coalesce(_g(body, 'role_key'), cur.get('role_key', UNDEF) if cur else UNDEF, ''))
    role = role_def(ctx.tenant_id, role_key)
    if not role:
        err(422, 'VALIDATION', 'Choose a role.', 'role_key')
    assert role is not None
    if not role.get('active') and role_key != (cur.get('role_key', UNDEF) if cur else UNDEF):
        err(422, 'VALIDATION', f'{role["name"]} is inactive. Choose an active role.', 'role_key')
    role_perms = effective_permissions(role['permissions'])
    rp = set(role_perms)
    cur_extra = coalesce(prop(cur, 'extra_permissions'), []) if cur else []
    cur_revoked = coalesce(prop(cur, 'revoked_permissions'), []) if cur else []
    b_extra = _g(body, 'extra_permissions')
    b_rev = _g(body, 'revoked_permissions')
    extra = [k for k in (clean_permissions(b_extra, 'extra_permissions') if b_extra is not UNDEF else cur_extra) if k not in rp]
    revoked = [k for k in (clean_permissions(b_rev, 'revoked_permissions') if b_rev is not UNDEF else cur_revoked) if k in rp]
    # escalation guard — compare with what the person already had so unchanged overrides never block
    had = set(permissions_of(cur) if cur else [])
    gaining = [k for k in effective_permissions(role_perms, extra, revoked) if k not in had]
    if not has(ctx, 'users.manage') and is_access_admin(gaining):
        err(403, 'PERMISSION_ESCALATION', f'{role["name"]} can manage roles or user access. Only someone with “Manage user access” can give that.', 'role_key')
    new_extras = [k for k in extra if k not in cur_extra]
    beyond = [k for k in new_extras if k not in ctx.perms]
    if beyond:
        err(
            403,
            'PERMISSION_ESCALATION',
            f'You can only grant permissions you hold yourself. Remove: {", ".join(perm_label(k) for k in beyond)}.',
            'extra_permissions',
        )
    return {'access': {'role_key': role['key'], 'extra_permissions': _sorted_strs(extra), 'revoked_permissions': _sorted_strs(revoked)}, 'role': role}


def _temp_password(mn: Any) -> str:
    """Strong temporary password that meets the tenant's minimum length."""
    sets = ['ABCDEFGHJKLMNPQRSTUVWXYZ', 'abcdefghijkmnopqrstuvwxyz', '23456789', '@#$%&*!?']
    ln = js_max(12, js_number(mn) + 2)
    chars = [s[secrets.randbelow(len(s))] for s in sets]
    all_ = ''.join(sets)
    while len(chars) < ln:
        chars.append(all_[secrets.randbelow(len(all_))])
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return ''.join(chars)


def _min_len(ctx: Ctx) -> Any:
    st = prop(ctx.tenant, 'settings')
    return coalesce(prop(st, 'security.password_min_length') if isinstance(st, dict) else UNDEF, 10)


def _next_employee_code(ctx: Ctx) -> str:
    """Next employee code from the org profile (prefix + running number); falls back to EMP + max existing."""
    t = tenant_of(ctx)
    users = rows(ctx, 'users')
    org = prop(t, 'org')
    has_org = js_truthy(org)
    pv = org.get('employee_code_prefix', UNDEF) if has_org else UNDEF
    prefix = js_trim(pv if js_truthy(pv) else 'EMP')
    sep = '' if re.search(r'[-/_]\Z', prefix) else '-'

    def fmt(n: Any) -> str:
        return f'{prefix}{sep}{pad_start(js_str(n), 3, "0")}'

    max_existing: Any = 0
    up = prefix.upper()
    for u in users:
        c = coalesce(prop(u, 'employee_code'), '')
        if not c.upper().startswith(up):
            continue
        n = js_number(re.sub(r'^[^0-9]*', '', c[len(prefix) :]))
        if is_finite(n):
            max_existing = js_max(max_existing, n)
    n = js_max(coalesce(org.get('employee_code_next', UNDEF) if has_org else UNDEF, 1), max_existing + 1)
    codes = {u.get('employee_code') for u in users}
    while fmt(n) in codes:
        n += 1
    if has_org:
        patch('tenants', t, {'org': {**org, 'employee_code_next': n + 1}})
    else:
        scope().persist()
    return fmt(n)


# ---------------------------------------------------------------------------------------------------------------------
# Create employee
# ---------------------------------------------------------------------------------------------------------------------
def _read_employee(ctx: Ctx, b: Any) -> dict[str, Any]:
    name = js_trim(_s(_g(b, 'name')))
    email = js_trim(_s(_g(b, 'email'))).lower()
    personal = js_trim(_s(_g(b, 'personal_email'))).lower() or None
    phone = js_trim(_s(_g(b, 'phone'))) or None
    joined_on = _s(_g(b, 'joined_on'))
    if js_len(name) < 2:
        err(422, 'VALIDATION', 'Enter the employee’s full name.', 'name')
    if not EMAIL_RE.search(email):
        err(422, 'VALIDATION', 'Enter a valid work email.', 'email')
    taken = _email_taken(ctx, email)
    if taken:
        err(409, 'USER_EMAIL_TAKEN', taken, 'email')
    if personal and not EMAIL_RE.search(personal):
        err(422, 'VALIDATION', 'Enter a valid personal email, or leave it empty.', 'personal_email')
    if personal and personal == email:
        err(422, 'VALIDATION', 'Personal email should differ from the work email.', 'personal_email')
    if phone and not PHONE_RE.search(phone):
        err(422, 'VALIDATION', 'Enter a valid phone number.', 'phone')
    if not _date_ok(joined_on):
        err(422, 'VALIDATION', 'Choose the joining date.', 'joined_on')
    if joined_on < add_days(today_iso(), -365):
        err(422, 'DATE_RANGE', 'Joining date is more than a year ago. Check the date.', 'joined_on')
    et = js_str(coalesce(_g(b, 'employment_type'), 'full_time'))
    if et not in EMPLOYMENT_TYPES:
        err(422, 'VALIDATION', 'Choose the employment type.', 'employment_type')
    department_id = _master_ref(ctx, 'departments', _g(b, 'department_id'), 'department_id')
    if not department_id and _has_active(ctx, 'departments'):
        err(422, 'VALIDATION', 'Choose a department.', 'department_id')
    designation_id = _master_ref(ctx, 'designations', _g(b, 'designation_id'), 'designation_id')
    if not designation_id and _has_active(ctx, 'designations'):
        err(422, 'VALIDATION', 'Choose a designation.', 'designation_id')
    grade_id = _master_ref(ctx, 'grades', _g(b, 'grade_id'), 'grade_id')
    location_id = coalesce(
        _master_ref(ctx, 'locations', _g(b, 'location_id'), 'location_id'),
        next((x['id'] for x in rows(ctx, 'locations') if x.get('active') and x.get('is_head_office')), None),
    )
    shift_id = coalesce(
        _master_ref(ctx, 'shifts', _g(b, 'shift_id'), 'shift_id'),
        next((x['id'] for x in rows(ctx, 'shifts') if x.get('active') and x.get('is_default')), None),
    )
    cost_centre_id = _master_ref(ctx, 'cost_centres', _g(b, 'cost_centre_id'), 'cost_centre_id')
    ra = _read_access(ctx, {'role_key': _g(b, 'role_key'), 'extra_permissions': coalesce(_g(b, 'extra_permissions'), [])}, None)
    access, role = ra['access'], ra['role']
    bx = _g(b, 'extra_permissions')
    if not nullish(bx) and js_truthy(_js_length(bx)) and not has(ctx, 'users.manage'):
        err(
            403,
            'FORBIDDEN',
            'Extra permissions need “Manage user access”. Create the employee with their role, then ask an administrator.',
            'extra_permissions',
        )
    rt = _g(b, 'reports_to_id')
    reports_to_id = rt if js_truthy(rt) else None
    if not reports_to_id and role['persona'] != 'admin':
        err(422, 'VALIDATION', 'Choose who they report to. Their manager approves their leave and timesheets.', 'reports_to_id')
    _assert_manager(ctx, None, reports_to_id)
    ctc: Any = None
    mc = _g(b, 'monthly_ctc_paise')
    if mc is not UNDEF and mc is not None and mc != '' and js_number(mc) > 0:
        if not has(ctx, 'cost_rates.edit'):
            err(403, 'FORBIDDEN', 'Setting CTC needs “Change salaries & cost rates”. Leave it empty.', 'monthly_ctc_paise')
        ctc = js_round(js_number(mc))
        if not is_finite(ctc) or ctc < 100_00 or ctc > 1_00_00_000_00:
            err(422, 'VALIDATION', 'Enter a monthly CTC between ₹100 and ₹1 crore.', 'monthly_ctc_paise')
    return {
        'name': name,
        'email': email,
        'personal_email': personal,
        'phone': phone,
        'joined_on': joined_on,
        'employment_type': et,
        'department_id': department_id,
        'designation_id': designation_id,
        'grade_id': grade_id,
        'location_id': location_id,
        'shift_id': shift_id,
        'cost_centre_id': cost_centre_id,
        'reports_to_id': reports_to_id,
        'role_key': access['role_key'],
        'extra_permissions': access['extra_permissions'],
        'monthly_ctc_paise': ctc,
        'send_welcome': _g(b, 'send_welcome') is not False,
        'issue_joining_letter': _read_joining_letter(ctx, b),
        'start_onboarding': _read_onboarding(ctx, b),
    }


def _js_length(v: Any) -> Any:
    """`v.length` (strings and arrays; UNDEF otherwise)"""
    if isinstance(v, str):
        return js_len(v)
    if isinstance(v, list):
        return len(v)
    return UNDEF


def _read_onboarding(ctx: Ctx, b: Any) -> bool:
    """v7: start the Employee onboarding workflow for the new joiner (needs "Start workflows for others")."""
    if _g(b, 'start_onboarding') is not True:
        return False
    if not has(ctx, 'workflows.start'):
        err(403, 'FORBIDDEN', 'Starting the onboarding workflow needs “Start workflows for others”. Untick it.', 'start_onboarding')
    if not _onboarding_template(ctx):
        err(
            422,
            'TEMPLATE_MISSING',
            'There is no active onboarding workflow. Activate one in the Workflow designer, or untick “Start onboarding workflow”.',
            'start_onboarding',
        )
    return True


def _onboarding_template(ctx: Ctx) -> dict | None:
    lst = [t for t in rows(ctx, 'wf_templates') if t.get('process') == 'onboarding' and t.get('active')]
    return next((t for t in lst if t.get('key') == 'employee_onboarding'), None) or (lst[0] if lst else None)


def _read_joining_letter(ctx: Ctx, b: Any) -> bool:
    if _g(b, 'issue_joining_letter') is not True:
        return False
    if not has(ctx, 'documents.templates'):
        err(403, 'FORBIDDEN', 'Issuing letters needs “Document templates & issuing”. Untick “Issue joining letter”.', 'issue_joining_letter')
    ensure_doc_defaults(ctx)
    if not any(t.get('key') == 'joining_letter' and t.get('active') for t in rows(ctx, 'doc_templates')):
        err(
            422,
            'TEMPLATE_MISSING',
            'The Joining letter template is switched off. Turn it on under Documents → Templates, or untick “Issue joining letter”.',
            'issue_joining_letter',
        )
    return True


def _error_message(e: BaseException) -> str:
    return e.message if isinstance(e, ApiError) else str(e)


def _create_employee(ctx: Ctx, cs: dict) -> dict[str, Any]:
    i = cs['payload']['input']
    # re-check what may have changed while it waited for approval
    if any(u['email'].lower() == i['email'] for u in get_db().users):
        err(409, 'USER_EMAIL_TAKEN', 'An account with this email was created meanwhile.', 'email')
    role = role_def(ctx.tenant_id, i['role_key'])
    if not role or not role.get('active'):
        err(409, 'VALIDATION', 'The chosen role no longer exists or is inactive.', 'role_key')
    assert role is not None
    if js_truthy(i.get('reports_to_id')):
        _assert_manager(ctx, None, i['reports_to_id'])
    _assert_seats(ctx, 1)
    dept = next((d for d in rows(ctx, 'departments') if d['id'] == i['department_id']), None) if js_truthy(i.get('department_id')) else None
    desig = next((d for d in rows(ctx, 'designations') if d['id'] == i['designation_id']), None) if js_truthy(i.get('designation_id')) else None
    code = _next_employee_code(ctx)
    user = insert(
        ctx,
        'users',
        {
            'email': i['email'],
            'name': i['name'],
            'title': coalesce(desig.get('name', UNDEF) if desig else UNDEF, role['name']),
            'role': role['persona'],
            'role_key': role['key'],
            'extra_permissions': list(i['extra_permissions']),
            'revoked_permissions': [],
            'reports_to_id': i['reports_to_id'],
            'grants': [],
            'status': 'active',
            'last_login_at': None,
            'avatar_hue': js_floor(secrets.randbelow(1 << 30) / (1 << 30) * 360),
            'phone': i['phone'],
            'joined_on': i['joined_on'],
            'department': coalesce(dept.get('kind', UNDEF) if dept else UNDEF, DEPT_FROM_PERSONA.get(role['persona'], UNDEF)),
            'two_factor': False,
            'prefs': {},
            'must_change_password': True,
            'employee_code': code,
            'department_id': i['department_id'],
            'designation_id': i['designation_id'],
            'grade_id': i['grade_id'],
            'location_id': i['location_id'],
            'shift_id': i['shift_id'],
            'cost_centre_id': coalesce(i['cost_centre_id'], dept.get('cost_centre_id', UNDEF) if dept else UNDEF, None),
            'employment_type': i['employment_type'],
            'personal_email': i['personal_email'],
        },
    )
    if cs['id'] != 'cs_direct':
        user['created_by'] = cs['maker_id']
    temp = _temp_password(_min_len(ctx))
    scope().set_password(user['id'], temp)
    # CTC: part of an approved hire, or direct when salaries are not maker-checked on their own
    ctc_saved = False
    if js_truthy(i.get('monthly_ctc_paise')):
        if cs['id'] != 'cs_direct' or not needs_checker(ctx, 'cost_rate'):
            insert(
                ctx,
                'cost_rates',
                {
                    'user_id': user['id'],
                    'monthly_ctc_paise': i['monthly_ctc_paise'],
                    'variable_pct': 0,
                    'sales_target_paise': None,
                    'effective_from': i['joined_on'],
                },
            )
            ctc_saved = True
        else:
            warn(
                ctx,
                'CTC_NEEDS_APPROVAL',
                'Salaries need a second person in this workspace, so the CTC was not saved. Add it under Settings → Cost rates to send it for approval.',
            )
    mgr = find_by('users', 'id', i['reports_to_id']) if js_truthy(i.get('reports_to_id')) else None
    email = None
    if i['send_welcome']:
        email = send_email(
            ctx,
            'welcome',
            {'email': user['email'], 'user_id': user['id']},
            {
                'employee_name': user['name'],
                'username': user['email'],
                'temp_password': temp,
                'role_name': role['name'],
                'manager_name': coalesce(mgr.get('name', UNDEF) if mgr else UNDEF, '—'),
                'joining_date': fmt_date_en_in(i['joined_on'], day='numeric', month='long', year='numeric'),
                'employee_code': code,
            },
            {'entity': 'user', 'id': user['id']},
        )
        if not email:
            warn(ctx, 'EMAIL_TEMPLATE_OFF', 'The welcome email template is turned off, so no email was sent. Share the login details yourself.')
    if mgr:
        mp = permissions_of(mgr)
        notify(
            ctx,
            mgr['id'],
            'NTF-14',
            'New team member',
            f'{user["name"]} joins your team on {i["joined_on"]} as {coalesce(desig.get("name", UNDEF) if desig else UNDEF, role["name"])}.',
            f'/people?focus={user["id"]}' if 'users.view' in mp else '/team' if 'team.view' in mp else None,
        )
    joining_letter_id = None
    if i.get('issue_joining_letter'):
        try:
            from app.routes.docs_v7 import issue_joining_letter

            joining_letter_id = issue_joining_letter(ctx, user['id'], i['monthly_ctc_paise'])['id']
        except ApiError as e:
            warn(ctx, 'JOINING_LETTER_FAILED', f'The employee was created, but the joining letter was not issued: {_error_message(e)}')
    onboarding_id = None
    if i.get('start_onboarding'):
        t = _onboarding_template(ctx)
        try:
            if not t:
                raise ApiError(500, 'ERROR', 'there is no active onboarding workflow.')
            from app.routes.workflows_v7 import start_workflow

            onboarding_id = start_workflow(ctx, t, user['id'], {'note': f'Started when {user["name"]} was created'})['id']
        except ApiError as e:
            warn(
                ctx,
                'ONBOARDING_NOT_STARTED',
                f'The employee was created, but the onboarding workflow was not started: {_error_message(e)} Start it from Workflows.',
            )
    if cs['id'] != 'cs_direct':
        notify(
            ctx,
            cs['maker_id'],
            'NTF-05',
            'Employee created',
            f'{user["name"]} ({code}) was approved and created.{" The welcome email has been sent." if email else ""}',
            f'/people?focus={user["id"]}',
        )
    log_activity(
        ctx,
        'user',
        user['id'],
        'create',
        f'Created employee {user["name"]} ({code}) as {role["name"]}{" — proposed by " + user_name(ctx, cs["maker_id"]) if cs["id"] != "cs_direct" else ""}',
    )
    user = find_by('users', 'id', user['id']) or user
    return {
        'user': user_row(ctx, user),
        'employee_code': code,
        'login_email': user['email'],
        'temp_password': temp,
        'email': email,
        'manager_name': coalesce(mgr.get('name', UNDEF) if mgr else UNDEF, None),
        'ctc_saved': ctc_saved,
        'joining_letter_id': joining_letter_id,
        'onboarding_workflow_id': onboarding_id,
    }


def _revoke_sessions(user_id: Any, keep: Any = UNDEF) -> None:
    """d.sessions = d.sessions.filter((s) => s.user_id !== id [|| s.token === keep])"""
    d = get_db()
    d.sessions = [s for s in d.sessions if s['user_id'] != user_id or (keep is not UNDEF and s['token'] == keep)]


def _deactivate_user(ctx: Ctx, cs: dict) -> dict[str, Any]:
    id_ = js_str(cs['payload']['user_id'])
    u = find(ctx, 'users', id_, 'Person')
    if u.get('status') == 'deactivated':
        return {'result': user_row(ctx, u), 'result_id': u['id']}
    assert_no_lockout(ctx, {'users': {u['id']: {'status': 'deactivated'}}})
    reports = [x for x in rows(ctx, 'users') if x.get('reports_to_id') == u['id'] and x.get('status') == 'active']
    out = update(ctx, 'users', u['id'], {'status': 'deactivated'})
    _revoke_sessions(u['id'])
    if reports:
        warn(
            ctx,
            'REPORTS_ORPHANED',
            f'{len(reports)} {"person reports" if len(reports) == 1 else "people report"} to {u["name"]}. Assign them a new manager so their approvals keep moving.',
        )
    log_activity(
        ctx, 'user', u['id'], 'deactivate', f'Deactivated {u["name"]}{" — proposed by " + user_name(ctx, cs["maker_id"]) if cs["id"] != "cs_direct" else ""}'
    )
    return {'result': user_row(ctx, out), 'result_id': u['id']}


def _apply_employee(ctx: Ctx, cs: dict) -> dict[str, Any]:
    op = cs['payload'].get('op')
    if op == 'create':
        r = _create_employee(ctx, cs)
        return {'result': r, 'result_id': r['user']['id']}
    if op == 'deactivate':
        return _deactivate_user(ctx, cs)
    err(422, 'VALIDATION', 'Unknown employee change.')
    return {}


register_applier('employee', _apply_employee)


def _access_of(u: dict) -> dict[str, Any]:
    return {
        'role_key': prop(u, 'role_key'),
        'extra_permissions': _sorted_strs(coalesce(prop(u, 'extra_permissions'), [])),
        'revoked_permissions': _sorted_strs(coalesce(prop(u, 'revoked_permissions'), [])),
    }


def _apply_user_access(ctx: Ctx, cs: dict) -> dict[str, Any]:
    p = cs['payload']
    u = find(ctx, 'users', p['user_id'], 'Person')
    cur = _access_of(u)
    if cs['id'] != 'cs_direct' and _json_ne(cur, p.get('before')):
        err(409, 'STALE', 'Their access changed after this was proposed. Reject it and ask the maker to propose again.')
    role = role_def(ctx.tenant_id, p['access']['role_key'])
    if not role:
        err(409, 'VALIDATION', 'The chosen role no longer exists.')
    assert role is not None
    assert_no_lockout(ctx, {'users': {u['id']: dict(p['access'])}})
    u = patch(
        'users',
        u,
        {
            'role_key': role['key'],
            'role': role['persona'],
            'extra_permissions': list(p['access']['extra_permissions']),
            'revoked_permissions': list(p['access']['revoked_permissions']),
            'version': u['version'] + 1,
            'updated_at': now_iso(),
        },
    )
    log_activity(ctx, 'user', u['id'], 'access', f'{cs["summary"]}{" — proposed by " + user_name(ctx, cs["maker_id"]) if cs["id"] != "cs_direct" else ""}')
    if cs['id'] != 'cs_direct':
        notify(ctx, u['id'], 'NTF-05', 'Your access changed', f'You are now {role["name"]}. Sign-in pages update within a minute.', '/home')
    return {'result': user_row(ctx, u), 'result_id': u['id']}


def _json_ne(a: Any, b: Any) -> bool:
    """JSON.stringify(a) !== JSON.stringify(b)"""
    from app.js import js_json_dumps

    return js_json_dumps(a) != js_json_dumps(b)


register_applier('user_access', _apply_user_access)


# ---------------------------------------------------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------------------------------------------------
_LEGACY = {
    'admin': 'administrator',
    'pm': 'project_manager',
    'manager': 'project_manager',
    'sales': 'sales_executive',
    'ops': 'hr_executive',
    'hr': 'hr_executive',
    'operations': 'hr_executive',
    'developer': 'employee',
    'engineer': 'employee',
    'member': 'employee',
}


def _role_from_text(ctx: Ctx, v: str) -> dict | None:
    s = js_trim(v).lower()
    roles = [r for r in rows(ctx, 'roles') if r.get('active')]
    hit = next((r for r in roles if r['key'] == s or r['name'].lower() == s), None)
    if hit is not None:
        return hit
    lk = _LEGACY.get(s)
    return next((r for r in roles if lk is not None and r['key'] == lk), None)


def _new_invite(ctx: Ctx, name: str, email: str, role: dict, reports_to_id: Any) -> dict:
    inv = insert(
        ctx,
        'invites',
        {
            'email': email,
            'name': name,
            'role': role['persona'],
            'role_key': role['key'],
            'reports_to_id': reports_to_id,
            'token': 'inv_' + random_token(24),
            'expires_at': add_days(today_iso(), INVITE_DAYS) + 'T23:59:59Z',
            'accepted_at': None,
        },
    )
    _mail_invite(ctx, inv)
    return inv


def _mail_invite(ctx: Ctx, inv: dict) -> None:
    """the invitation e-mail with the accept link (APP_URL/invite/<token>): a platform message — sealed in the
    delivery queue, not in the workspace's sent log; it expires with the invite"""
    from app.config import settings
    from app.js import encode_uri_component

    tenant = ctx.tenant if isinstance(ctx.tenant, dict) else {}
    org = tenant.get('org') if isinstance(tenant.get('org'), dict) else {}
    company = js_str(org.get('brand_name') or tenant.get('name') or 'your company')
    inviter = js_str(ctx.user.get('name') or '') if isinstance(ctx.user, dict) else ''
    link = f'{settings().app_url}/invite/{encode_uri_component(inv["token"])}'
    who = f'{inviter} invited you' if inviter else 'You are invited'
    body = (
        f'Hi {js_str(inv.get("name") or "")},\n\n{who} to join {company} on SeereonOS.\n\n'
        f'Accept the invitation and choose your password here:\n{link}\n\n'
        f'The link works once and expires on {js_str(inv["expires_at"])[:10]}. If you were not expecting this, ignore this e-mail.\n\n— SeereonOS'
    )
    scope().queue_platform_mail(inv['email'], f'{who} to {company} on SeereonOS', body, expires_at=inv['expires_at'], tenant_id=ctx.tenant_id)


def _two_factor_pending(ctx: Ctx, user_id: str) -> dict | None:
    """2FA setup secrets live in pending_tokens (kind two_factor_setup), so the verify call may land on any instance."""
    now = now_iso()
    return next((t for t in rows(ctx, 'pending_tokens') if t.get('kind') == 'two_factor_setup' and t.get('user_id') == user_id and t['expires_at'] > now), None)


# ---------------------------------------------------------------------------------------------------------------------
# Routes: people
# ---------------------------------------------------------------------------------------------------------------------
def list_users(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'users.view')
    out = _user_rows(ctx, [u for u in rows(ctx, 'users') if not u.get('is_super_admin')])
    return sorted(out, key=lambda r: locale_key(r['name']))


def get_user(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'users.view')
    return user_row(ctx, find(ctx, 'users', req.params['id'], 'Person'))


JOB = [
    'name',
    'title',
    'department_id',
    'designation_id',
    'grade_id',
    'location_id',
    'shift_id',
    'cost_centre_id',
    'employment_type',
    'reports_to_id',
    'phone',
    'personal_email',
    'joined_on',
    'productive_hours',
]
ACCESS = ['role_key', 'extra_permissions', 'revoked_permissions']
_REFS = [
    ('departments', 'department_id'),
    ('designations', 'designation_id'),
    ('grades', 'grade_id'),
    ('locations', 'location_id'),
    ('shifts', 'shift_id'),
    ('cost_centres', 'cost_centre_id'),
]


def patch_user(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    u = find(ctx, 'users', req.params['id'], 'Person')
    if u.get('is_super_admin'):
        err(404, 'NOT_FOUND', 'Person not found.')
    wants_job = any(req.b(k) is not UNDEF for k in JOB)
    wants_access = any(req.b(k) is not UNDEF for k in ACCESS)
    if not wants_job and not wants_access:
        err(422, 'VALIDATION', 'Nothing to change.')
    if wants_job:
        require_action(ctx, 'employees.edit', 'You can’t edit employee records.')
    if wants_access:
        require_action(ctx, 'users.manage', 'Changing a role or permissions needs “Manage user access”.')
    if u.get('status') == 'deactivated':
        err(409, 'USER_DEACTIVATED', 'Reactivate this person before editing.')
    if req.if_match is not UNDEF and req.if_match != u['version']:
        err(409, 'STALE', 'Someone else changed this person. Reload to see the latest version.')

    # validate everything before writing anything
    pt: dict[str, Any] = {}
    if wants_job:
        if req.b('name') is not UNDEF:
            if js_len(js_trim(js_str(req.b('name')))) < 2:
                err(422, 'VALIDATION', 'Enter a name.', 'name')
            pt['name'] = js_trim(js_str(req.b('name')))
        if req.b('title') is not UNDEF:
            pt['title'] = js_trim(_s(req.b('title')))[:80]
        if req.b('phone') is not UNDEF:
            ph = js_trim(_s(req.b('phone')))
            if ph and not PHONE_RE.search(ph):
                err(422, 'VALIDATION', 'Enter a valid phone number.', 'phone')
            pt['phone'] = ph or None
        if req.b('personal_email') is not UNDEF:
            pe = js_trim(_s(req.b('personal_email'))).lower()
            if pe and not EMAIL_RE.search(pe):
                err(422, 'VALIDATION', 'Enter a valid personal email.', 'personal_email')
            pt['personal_email'] = pe or None
        if req.b('productive_hours') is not UNDEF:
            # hours of task work a day planning and forecasts assume (Feature Guide §16); null/'' = company default
            raw = req.b('productive_hours')
            if raw is None or raw == '':
                pt['productive_hours'] = None
            else:
                h = js_number(raw)
                min_h = prop(prop(ctx.tenant, 'settings'), 'calendar.min_hours_per_day')
                too_big = isinstance(min_h, (int, float)) and not isinstance(min_h, bool) and h > min_h
                if not is_finite(h) or h < 0.5 or too_big or abs(h * 2 - js_round(h * 2)) > 1e-9:
                    err(422, 'VALIDATION', f'Productive hours must be 0.5 to {js_str(min_h)} (the daily minimum), in half hours.', 'productive_hours')
                pt['productive_hours'] = h
        if req.b('joined_on') is not UNDEF:
            if not _date_ok(js_str(req.b('joined_on'))):
                err(422, 'VALIDATION', 'Choose the joining date.', 'joined_on')
            pt['joined_on'] = js_str(req.b('joined_on'))
        if req.b('employment_type') is not UNDEF:
            et = req.b('employment_type')
            if js_truthy(et) and not (isinstance(et, str) and et in EMPLOYMENT_TYPES):
                err(422, 'VALIDATION', 'Choose the employment type.', 'employment_type')
            pt['employment_type'] = et if js_truthy(et) else None
        for table, field in _REFS:
            if req.b(field) is not UNDEF:
                pt[field] = _master_ref(ctx, table, req.b(field), field, prop(u, field))
        if js_truthy(pt.get('department_id')):
            dd = next((d for d in rows(ctx, 'departments') if d['id'] == pt['department_id']), None)
            pt['department'] = coalesce(dd.get('kind', UNDEF) if dd else UNDEF, prop(u, 'department'))
        if js_truthy(pt.get('designation_id')) and pt['designation_id'] != prop(u, 'designation_id') and req.b('title') is UNDEF:
            ds = next((d for d in rows(ctx, 'designations') if d['id'] == pt['designation_id']), None)
            pt['title'] = coalesce(ds.get('name', UNDEF) if ds else UNDEF, prop(u, 'title'))
        if req.b('reports_to_id') is not UNDEF:
            m = req.b('reports_to_id') if js_truthy(req.b('reports_to_id')) else None
            _assert_manager(ctx, u['id'], m)
            if not m and u.get('role') != 'admin':
                err(422, 'VALIDATION', 'Choose who they report to. Only leadership roles may have no manager.', 'reports_to_id')
            pt['reports_to_id'] = m
    access_req = None
    if wants_access:
        ra = _read_access(ctx, body, u)
        access, role = ra['access'], ra['role']
        before = _access_of(u)
        if _json_ne(before, access):
            manager_id = pt['reports_to_id'] if 'reports_to_id' in pt else prop(u, 'reports_to_id')
            if not js_truthy(manager_id) and role['persona'] != 'admin':
                err(422, 'VALIDATION', f'{role["name"]} needs a reporting manager. Set “Reports to” first.', 'role_key')
            assert_no_lockout(ctx, {'users': {u['id']: access}})
            access_req = (access, role, before)

    # access first: maker-checker may still refuse it (pending change, no checker) before anything is written
    access_out = None
    if access_req:
        a, role, before = access_req
        parts: list[str] = []
        if a['role_key'] != before['role_key']:
            brd = role_def(ctx.tenant_id, before['role_key'])
            parts.append(f'role {js_str(coalesce(brd.get("name", UNDEF) if brd else UNDEF, before["role_key"]))} → {role["name"]}')
        dx = diff_summary(before['extra_permissions'], a['extra_permissions'])
        if dx['text']:
            parts.append(f'extra {dx["text"]}')
        dr = diff_summary(before['revoked_permissions'], a['revoked_permissions'])
        if dr['text']:
            parts.append(f'removed {dr["text"]}')
        access_out = propose_named(
            ctx,
            {
                'entity': 'user_access',
                'entity_id': u['id'],
                'action': 'update',
                'summary': f'{u["name"]}: {"; ".join(parts)}',
                'before': {**before},
                'after': {**a},
                'payload': {'user_id': u['id'], 'access': a, 'before': before},
                'required_permission': 'users.manage',
            },
        )
    out = u
    if wants_job and pt:
        out = update(ctx, 'users', u['id'], pt)
        log_activity(ctx, 'user', u['id'], 'update', f'Updated {out["name"]}’s job details')
    return {'user': user_row(ctx, find(ctx, 'users', out['id'])), 'access': access_out}


def create_employee(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'employees.create', 'You can’t create employees.')
    inp = _read_employee(ctx, req.body)
    _assert_seats(ctx, 1)
    role = role_def(ctx.tenant_id, inp['role_key'])
    assert role is not None
    mgr = user_name(ctx, inp['reports_to_id']) if js_truthy(inp['reports_to_id']) else None
    return propose_named(
        ctx,
        {
            'entity': 'employee',
            'entity_id': None,
            'action': 'create',
            'summary': f'New employee {inp["name"]} ({inp["email"]}) as {role["name"]}{", reporting to " + mgr if mgr else ""}',
            'before': None,
            'after': {
                'name': inp['name'],
                'email': inp['email'],
                'role': role['name'],
                'reports_to': mgr,
                'joined_on': inp['joined_on'],
                'extra_permissions': inp['extra_permissions'],
            },
            'payload': {'op': 'create', 'input': inp},
            'required_permission': 'employees.create',
        },
    )


def reset_password(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'users.manage', 'Resetting a password needs “Manage user access”.')
    u = find(ctx, 'users', req.params['id'], 'Person')
    if u.get('is_super_admin'):
        err(404, 'NOT_FOUND', 'Person not found.')
    if u.get('status') == 'deactivated':
        err(409, 'USER_DEACTIVATED', 'Reactivate this person first.')
    if u['id'] == ctx.user_id:
        err(409, 'VALIDATION', 'Change your own password under Settings → Security.')
    temp = _temp_password(_min_len(ctx))
    scope().set_password(u['id'], temp)
    _revoke_sessions(u['id'])
    u = patch('users', u, {'must_change_password': True, 'version': u['version'] + 1, 'updated_at': now_iso()})
    email = send_email(
        ctx,
        'password_reset',
        {'email': u['email'], 'user_id': u['id']},
        {'employee_name': u['name'], 'username': u['email'], 'temp_password': temp},
        {'entity': 'user', 'id': u['id']},
    )
    log_activity(ctx, 'user', u['id'], 'reset_password', f'Reset {u["name"]}’s password')
    return {'user': user_row(ctx, u), 'temp_password': temp, 'login_email': u['email'], 'email': email}


def reset_2fa(req: Req) -> Any:
    """lost phone and recovery codes: an access admin removes the person's second factor; every session of theirs ends"""
    ctx = req.ctx
    require_action(ctx, 'users.manage', 'Resetting two-step verification needs “Manage user access”.')
    u = find(ctx, 'users', req.params['id'], 'Person')
    if u.get('is_super_admin'):
        err(404, 'NOT_FOUND', 'Person not found.')
    if u['id'] == ctx.user_id:
        err(409, 'VALIDATION', 'Turn your own two-step verification off under Settings → Security.')
    if not js_truthy(u.get('two_factor')):
        err(409, 'TWO_FACTOR_OFF', f'{u["name"]} doesn’t use two-step verification.')
    uid_ = u['id']
    remove_where(ctx, 'pending_tokens', lambda t: t.get('kind') in ('two_factor', 'two_factor_setup', 'mfa_login') and t.get('user_id') == uid_)
    u = patch('users', u, {'two_factor': False, 'version': u['version'] + 1, 'updated_at': now_iso()})
    _revoke_sessions(uid_)
    log_activity(ctx, 'user', uid_, 'reset_2fa', f'Reset {u["name"]}’s two-step verification')
    return {'user': user_row(ctx, u)}


def deactivate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'employees.deactivate', 'You can’t deactivate people.')
    u = find(ctx, 'users', req.params['id'], 'Person')
    if u.get('is_super_admin'):
        err(404, 'NOT_FOUND', 'Person not found.')
    if u.get('status') == 'deactivated':
        return {'pending': False, 'result': user_row(ctx, u), 'result_id': u['id']}
    if u['id'] == ctx.user_id:
        err(409, 'VALIDATION', 'You can’t deactivate yourself. Ask another administrator.')
    if is_access_admin(permissions_of(u)) and not has(ctx, 'users.manage'):
        err(403, 'PERMISSION_ESCALATION', f'{u["name"]} manages roles or users. Only someone with “Manage user access” can deactivate them.')
    assert_no_lockout(ctx, {'users': {u['id']: {'status': 'deactivated'}}})
    return propose_named(
        ctx,
        {
            'entity': 'employee',
            'entity_id': u['id'],
            'action': 'delete',
            'summary': f'Deactivate {u["name"]}',
            'before': {'status': u.get('status', UNDEF)},
            'after': {'status': 'deactivated'},
            'payload': {'op': 'deactivate', 'user_id': u['id']},
            'required_permission': 'employees.deactivate',
        },
    )


def reactivate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'employees.deactivate', 'You can’t reactivate people.')
    u = find(ctx, 'users', req.params['id'], 'Person')
    if u.get('status') == 'active':
        return user_row(ctx, u)
    _assert_seats(ctx, 1)
    out = update(ctx, 'users', u['id'], {'status': 'active'})
    if js_truthy(u.get('reports_to_id')):
        m = find_by('users', 'id', u['reports_to_id'])
        if not m or m.get('status') != 'active':
            warn(ctx, 'MANAGER_INACTIVE', f'{u["name"]}’s manager is no longer active. Choose a new manager.')
    log_activity(ctx, 'user', u['id'], 'reactivate', f'Reactivated {u["name"]}')
    return user_row(ctx, out)


# ---------------------------------------------------------------------------------------------------------------------
# Routes: invites
# ---------------------------------------------------------------------------------------------------------------------
def list_invites(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'users.view')
    out = [_invite_view(ctx, i) for i in rows(ctx, 'invites') if not js_truthy(i.get('accepted_at'))]
    return sorted(out, key=lambda x: locale_key(x['created_at']), reverse=True)


def invite_user(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'employees.create', 'You can’t add people.')
    name = js_trim(_s(req.b('name')))
    email = js_trim(_s(req.b('email'))).lower()
    rt = req.b('reports_to_id')
    reports_to_id = rt if js_truthy(rt) else None
    if js_len(name) < 2:
        err(422, 'VALIDATION', 'Enter their name.', 'name')
    if not EMAIL_RE.search(email):
        err(422, 'VALIDATION', 'Enter a valid email address.', 'email')
    role = _role_from_text(ctx, js_str(coalesce(req.b('role_key'), req.b('role'), 'employee')))
    if not role:
        err(422, 'VALIDATION', 'Choose a role.', 'role_key')
    assert role is not None
    _read_access(ctx, {'role_key': role['key'], 'extra_permissions': []}, None)
    taken = _email_taken(ctx, email)
    if taken:
        err(409, 'USER_EMAIL_TAKEN', taken, 'email')
    _assert_manager(ctx, None, reports_to_id)
    _assert_seats(ctx, 1)
    inv = _new_invite(ctx, name, email, role, reports_to_id)
    log_activity(ctx, 'invite', inv['id'], 'create', f'Invited {name} ({email}) as {role["name"]}')
    return _invite_view(ctx, inv)


def bulk_invite(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'employees.create', 'You can’t add people.')
    inp = coalesce(req.b('rows'), [])
    ln = _js_length(inp)
    if not js_truthy(ln):
        err(422, 'VALIDATION', 'Add at least one row: name,email,role,reports_to_email.')
    if ln > 200:
        err(422, 'VALIDATION', 'Invite up to 200 people at a time.')
    if not isinstance(inp, list):
        raise TypeError('input.map is not a function')
    users = rows(ctx, 'users')
    seen: set[str] = set()
    out = []
    for idx, row in enumerate(inp):
        if nullish(row):
            raise TypeError("Cannot read properties of null (reading 'name')")
        errors: list[str] = []
        name = js_trim(_s(_g(row, 'name')))
        email = js_trim(_s(_g(row, 'email'))).lower()
        role = _role_from_text(ctx, _s(_g(row, 'role')) or 'employee')
        mgr_email = js_trim(_s(_g(row, 'reports_to_email'))).lower()
        if js_len(name) < 2:
            errors.append('Name is missing')
        if not EMAIL_RE.search(email):
            errors.append('Email is not valid')
        else:
            if email in seen:
                errors.append('Email appears twice in this file')
            t = _email_taken(ctx, email)
            if t:
                errors.append('Already has a pending invite' if 'pending' in t else 'Email already has an account')
        seen.add(email)
        if not role:
            errors.append(f'Role "{js_str(_g(row, "role"))}" is not a role in this workspace')
        elif not has(ctx, 'users.manage') and is_access_admin(role['permissions']):
            errors.append(f'{role["name"]} needs “Manage user access” to assign')
        reports_to_id = None
        if mgr_email:
            m = next((u for u in users if u['email'].lower() == mgr_email and u.get('status') == 'active'), None)
            if not m:
                errors.append(f'Manager {mgr_email} is not an active person in this workspace')
            else:
                reports_to_id = m['id']
        out.append(
            {
                'line': idx + 1,
                'name': name,
                'email': email,
                'role': role['persona'] if role else 'employee',
                'role_key': role['key'] if role else '',
                'role_name': role['name'] if role else _s(_g(row, 'role')),
                'reports_to_id': reports_to_id,
                'errors': errors,
            }
        )
    bad = sum(1 for x in out if x['errors'])
    if req.b('mode') != 'commit':
        return {'rows': out, 'valid': len(out) - bad, 'invalid': bad, 'committed': False}
    if bad:
        err(422, 'VALIDATION', f'{bad} row{" has" if bad == 1 else "s have"} errors. Fix them and try again — nothing was invited.')
    _assert_seats(ctx, len(out))
    created = [_invite_view(ctx, _new_invite(ctx, x['name'], x['email'], role_def(ctx.tenant_id, x['role_key']), x['reports_to_id'])) for x in out]  # type: ignore[arg-type]
    log_activity(ctx, 'invite', 'bulk', 'create', f'Bulk-invited {len(created)} people')
    return {'rows': out, 'valid': len(out), 'invalid': 0, 'committed': True, 'invites': created}


def resend_invite(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'employees.create', 'You can’t resend invites.')
    inv = find(ctx, 'invites', req.params['id'], 'Invite')
    if js_truthy(inv.get('accepted_at')):
        err(409, 'INVITE_ACCEPTED', 'This invite was already accepted.')
    out = update(ctx, 'invites', inv['id'], {'token': 'inv_' + random_token(24), 'expires_at': add_days(today_iso(), INVITE_DAYS) + 'T23:59:59Z'})
    _mail_invite(ctx, out)
    log_activity(ctx, 'invite', inv['id'], 'resend', f'Resent invite to {inv["email"]}')
    return _invite_view(ctx, out)


def revoke_invite(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'employees.create', 'You can’t revoke invites.')
    inv = find(ctx, 'invites', req.params['id'], 'Invite')
    if js_truthy(inv.get('accepted_at')):
        err(409, 'INVITE_ACCEPTED', 'This invite was already accepted. Deactivate the person instead.')
    remove(ctx, 'invites', inv['id'])
    log_activity(ctx, 'invite', inv['id'], 'revoke', f'Revoked invite for {inv["email"]}')
    return {'ok': True}


# ---------------------------------------------------------------------------------------------------------------------
# Routes: team roster (Feature Guide §16 "Team")
# ---------------------------------------------------------------------------------------------------------------------
def team_roster(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'users.view')
    st = ctx.tenant['settings']
    see_ctc = has(ctx, 'cost_rates.view')
    fm = st['company.fy_start_month']
    cal = calendar_for(ctx)
    avg = avg_working_days_per_month(fy_start_year(ctx.today, fm), cal, fm)
    loading = coalesce(st.get('finance.cost_loading_pct', UNDEF), 15)  # kept in the payload for older clients; no longer used
    min_h = st.get('calendar.min_hours_per_day', UNDEF)
    min_h = min_h if js_truthy(min_h) else 9
    month = month_of(ctx.today)
    wd_this_month = working_days_in_month(month, calendar_for(ctx))
    def_hours = coalesce(st.get('team.default_productive_hours', UNDEF), 6)
    depts = rows(ctx, 'departments')
    dept_by = _first_by_id(depts)
    all_ = [u for u in rows(ctx, 'users') if not u.get('is_super_admin') and u.get('status') != 'invited']
    rates_by: dict = {}
    for x in rows(ctx, 'cost_rates'):
        if x['effective_from'] <= ctx.today:
            rates_by.setdefault(x['user_id'], []).append(x)

    def rate_of(id_: Any) -> dict | None:
        lst = rates_by.get(id_)
        if not lst:
            return None
        return sorted(lst, key=lambda r: locale_key(r['effective_from']), reverse=True)[0]

    people_sorted = sorted(all_, key=lambda u: (u.get('status') != 'active', js_truthy(u.get('reports_to_id')), locale_key(u['joined_on'])))
    by_id = _first_by_id(all_)
    people = []
    for u in people_sorted:
        rd = role_def(u.get('tenant_id'), u.get('role_key'))
        rate = rate_of(u['id']) if see_ctc else None
        mgr = by_id.get(u['reports_to_id']) if js_truthy(u.get('reports_to_id')) else None
        cph = cost_per_hour(rate['monthly_ctc_paise'], wd_this_month, min_h) if see_ctc and rate else None
        people.append(
            {
                'id': u['id'],
                'version': u['version'],
                'status': prop(u, 'status'),
                'name': prop(u, 'name'),
                'title': prop(u, 'title'),
                'avatar_hue': prop(u, 'avatar_hue'),
                'employee_code': coalesce(prop(u, 'employee_code'), None),
                'role_key': prop(u, 'role_key'),
                'role_name': coalesce(rd.get('name', UNDEF) if rd else UNDEF, prop(u, 'title')),
                'persona': prop(u, 'role'),
                'department_id': coalesce(prop(u, 'department_id'), None),
                'department_name': coalesce(_name_in(dept_by, u.get('department_id')), None),
                'department_kind': prop(u, 'department'),
                'joined_on': prop(u, 'joined_on'),
                'reports_to_id': prop(u, 'reports_to_id'),
                'reports_to_name': coalesce(mgr.get('name', UNDEF) if mgr else UNDEF, None),
                'self_approves': not js_truthy(u.get('reports_to_id')),
                'productive_hours': coalesce(prop(u, 'productive_hours'), None),
                'effective_productive_hours': coalesce(prop(u, 'productive_hours'), def_hours),
                'monthly_ctc_paise': coalesce(rate.get('monthly_ctc_paise', UNDEF) if rate else UNDEF, None) if see_ctc else None,
                'variable_pct': coalesce(rate.get('variable_pct', UNDEF) if rate else UNDEF, 0) if see_ctc else None,
                'cost_per_hour_paise': js_round(cph) if cph is not None else None,
                # a full working day = minimum hours × cost per hour (= CTC ÷ working days in the month)
                'cost_per_day_paise': js_round(cph * min_h) if cph is not None else None,
                'pending_ctc': pending_brief(ctx, 'cost_rate', u['id']) if see_ctc else None,
            }
        )
    return {
        'people': people,
        'avg_working_days': avg,
        'cost_loading_pct': loading,
        'cost_month': month,
        'working_days_month': wd_this_month,
        'default_productive_hours': def_hours,
        'min_hours': st.get('calendar.min_hours_per_day', UNDEF),
        'departments': [{'id': d['id'], 'name': prop(d, 'name')} for d in depts if d.get('active', UNDEF) is not False],
        'roles': [{'key': x['key'], 'name': x['name'], 'persona': x['persona']} for x in rows(ctx, 'roles') if x.get('active')],
        'can_edit': has(ctx, 'employees.edit') and not ctx.view_as,
        'can_edit_access': has(ctx, 'users.manage') and not ctx.view_as,
        'can_see_ctc': see_ctc,
        'can_edit_ctc': has(ctx, 'cost_rates.edit') and not ctx.view_as,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Routes: delegations (UI-38) — anyone who approves can delegate their own approvals; user administrators manage all
# ---------------------------------------------------------------------------------------------------------------------
def _manages_all(ctx: Ctx) -> bool:
    return has(ctx, 'users.manage')


def list_delegations(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['delegations.manage', 'users.manage'])
    all_ = rows(ctx, 'delegations')
    mine = all_ if _manages_all(ctx) else [d for d in all_ if d.get('from_user_id') == ctx.user_id or d.get('to_user_id') == ctx.user_id]
    return sorted(mine, key=lambda d: locale_key(d['from_on']), reverse=True)


def delegation_options(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['delegations.manage', 'users.manage'])
    ps = sorted([p for p in rows(ctx, 'projects') if p.get('status') not in ('completed', 'closed')], key=lambda p: locale_key(p['code']))
    return {'projects': [{'id': p['id'], 'code': p['code'], 'name': p['name']} for p in ps], 'can_manage_all': _manages_all(ctx)}


def create_delegation(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['delegations.manage', 'users.manage'], 'Only people who approve for others can delegate approvals.')
    bf = req.b('from_user_id')
    frm = js_str(bf if js_truthy(bf) else ctx.user_id) if _manages_all(ctx) else ctx.user_id
    if not _manages_all(ctx) and js_truthy(bf) and bf != ctx.user_id:
        err(403, 'FORBIDDEN', 'You can only delegate your own approvals.')
    to = _s(req.b('to_user_id'))
    from_on = _s(req.b('from_on'))
    to_on = _s(req.b('to_on'))
    reason = js_trim(_s(req.b('reason')))[:200]
    project_id = js_str(req.b('project_id')) if js_truthy(req.b('project_id')) else None
    # Feature Guide §16: approvals (and optionally task follow-ups). A one-project delegation is task follow-ups only.
    include_tasks = True if req.b('include_tasks') is UNDEF else js_truthy(req.b('include_tasks'))
    if project_id and not include_tasks:
        err(
            422,
            'VALIDATION',
            'A one-project delegation covers that project’s task follow-ups. Tick “Include task follow-ups” or choose All projects.',
            'project_id',
        )
    from_u = find(ctx, 'users', frm, 'Person')
    if project_id:
        p = find(ctx, 'projects', project_id, 'Project')
        if p.get('status') in ('completed', 'closed'):
            err(422, 'VALIDATION', f'{p["name"]} is {p["status"]}. Pick an active project or All projects.', 'project_id')
    if not to:
        err(422, 'VALIDATION', 'Choose who will approve on their behalf.', 'to_user_id')
    to_u = find(ctx, 'users', to, 'Delegate')
    if to_u.get('status') != 'active':
        err(422, 'VALIDATION', 'The delegate must be an active person.', 'to_user_id')
    if to == frm:
        err(422, 'VALIDATION', 'Pick someone other than the person delegating.', 'to_user_id')
    if not _date_ok(from_on) or not _date_ok(to_on):
        err(422, 'VALIDATION', 'Choose both dates.', 'from_on')
    if to_on < from_on:
        err(422, 'DATE_RANGE', 'The end date must be on or after the start date.', 'to_on')
    if to_on < today_iso():
        err(422, 'DATE_RANGE', 'The range has already ended. Choose dates from today onwards.', 'to_on')
    if reason and js_len(reason) < 3:
        err(422, 'VALIDATION', 'Make the note at least 3 characters, or leave it empty.', 'reason')
    # overlapping delegations clash when their scopes overlap (all projects overlaps everything)
    clash = next(
        (
            d
            for d in rows(ctx, 'delegations')
            if d.get('from_user_id') == frm
            and d['from_on'] <= to_on
            and from_on <= d['to_on']
            and (not js_truthy(d.get('project_id')) or not project_id or d.get('project_id') == project_id)
        ),
        None,
    )
    if clash:
        err(
            409,
            'DELEGATION_OVERLAP',
            f'{from_u["name"]} already has a delegation{" for that project" if js_truthy(clash.get("project_id")) else ""} from {clash["from_on"]} to {clash["to_on"]}. Change the dates or remove that one first.',
            'from_on',
        )
    out = insert(
        ctx,
        'delegations',
        {'from_user_id': frm, 'to_user_id': to, 'from_on': from_on, 'to_on': to_on, 'reason': reason, 'project_id': project_id, 'include_tasks': include_tasks},
    )
    if project_id:
        pr = next((p for p in rows(ctx, 'projects') if p['id'] == project_id), None)
        scope_label = coalesce(pr.get('name', UNDEF) if pr else UNDEF, 'one project')
    else:
        scope_label = 'all projects'
    what = 'task follow-ups' if project_id else 'approvals and task follow-ups' if include_tasks else 'approvals'
    log_activity(ctx, 'delegation', out['id'], 'create', f'{from_u["name"]} delegated {what} ({scope_label}) to {to_u["name"]}, {from_on} to {to_on}')
    handed = f'their {scope_label} tasks' if project_id else 'their approvals and open tasks' if include_tasks else 'their approvals'
    notify(
        ctx,
        to,
        'NTF-14',
        'You are covering',
        f'{from_u["name"]} handed you {handed} from {from_on} to {to_on}{": " + reason if reason else ""}.',
        '/my-tasks' if project_id else '/approvals',
    )
    return out


def delete_delegation(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['delegations.manage', 'users.manage'])
    d = find(ctx, 'delegations', req.params['id'], 'Delegation')
    if not _manages_all(ctx) and d.get('from_user_id') != ctx.user_id:
        err(403, 'FORBIDDEN', 'You can only remove your own delegations.')
    remove(ctx, 'delegations', d['id'])
    log_activity(ctx, 'delegation', d['id'], 'delete', 'Removed a delegation')
    return {'ok': True}


# ---------------------------------------------------------------------------------------------------------------------
# Routes: sessions & 2FA
# ---------------------------------------------------------------------------------------------------------------------
def list_sessions(req: Req) -> Any:
    ctx = req.ctx
    token = _bearer(req)
    out = [
        {'id': js_str(s['token'])[-10:], 'created_at': s['created_at'], 'current': s['token'] == token}
        for s in get_db().sessions
        if s['user_id'] == ctx.user_id
    ]
    return sorted(out, key=lambda x: (not x['current'], _Desc(locale_key(x['created_at']))))


class _Desc:
    """sort key wrapper: descending order of the wrapped key"""

    __slots__ = ('k',)

    def __init__(self, k: Any) -> None:
        self.k = k

    def __lt__(self, o: _Desc) -> bool:
        return o.k < self.k

    def __eq__(self, o: object) -> bool:
        return isinstance(o, _Desc) and o.k == self.k


def revoke_other_sessions(req: Req) -> Any:
    ctx = req.ctx
    token = _bearer(req)
    d = get_db()
    before = len(d.sessions)
    d.sessions = [s for s in d.sessions if s['user_id'] != ctx.user_id or s['token'] == token]
    return {'revoked': before - len(d.sessions)}


def delete_session(req: Req) -> Any:
    ctx = req.ctx
    token = _bearer(req)
    d = get_db()
    s = next((x for x in d.sessions if x['user_id'] == ctx.user_id and js_str(x['token'])[-10:] == req.params['id']), None)
    if not s:
        err(404, 'NOT_FOUND', 'Session not found.')
    assert s is not None
    if s['token'] == token:
        err(409, 'VALIDATION', 'Use Sign out to end this session.')
    d.sessions = [x for x in d.sessions if x is not s]
    return {'ok': True}


def _me(ctx: Ctx) -> dict:
    return find_by('users', 'id', ctx.user_id)  # type: ignore[return-value]


def get_2fa(req: Req) -> Any:
    u = _me(req.ctx)
    rec = two_factor_record(u) if js_truthy(u.get('two_factor')) else None
    return {
        'enabled': bool(rec),
        'enabled_at': coalesce(prop(rec['payload'], 'enabled_at'), None) if rec else None,
        'recovery_codes_left': len(rec['payload']['recovery']) if rec else 0,
    }


def setup_2fa(req: Req) -> Any:
    ctx = req.ctx
    u = _me(ctx)
    if js_truthy(u.get('two_factor')) and two_factor_record(u):
        err(409, 'TWO_FACTOR_ON', 'Two-step verification is already on.')
    secret = new_totp_secret()
    now = now_iso()
    uid_ = u['id']
    remove_where(ctx, 'pending_tokens', lambda t: t.get('kind') == 'two_factor_setup' and (t.get('user_id') == uid_ or t['expires_at'] <= now))
    insert(
        ctx,
        'pending_tokens',
        {
            'kind': 'two_factor_setup',
            'user_id': uid_,
            'ref_id': None,
            'payload': {'secret': seal_secret(secret), 'attempts': 0},
            'expires_at': iso_from_ms(now_ms() + TWO_FACTOR_SETUP_MINUTES * 60_000),
        },
    )
    return {'secret': secret, 'otpauth_url': otpauth_url('SeereonOS', u['email'], secret)}


def verify_2fa(req: Req) -> Any:
    ctx = req.ctx
    d = get_db()
    u = _me(ctx)
    pending = _two_factor_pending(ctx, u['id'])
    if not pending:
        err(409, 'TWO_FACTOR_SETUP', 'Start the setup again — the key has expired.')
    assert pending is not None
    code = re.sub(r'\s', '', _s(req.b('code')))
    if not SIX_DIGITS.search(code):
        err(422, 'TWO_FACTOR_CODE', 'Enter the 6-digit code from your authenticator app.', 'code')
    p = pending['payload']
    secret = open_secret(p.get('secret'))
    step = verify_totp(secret, code) if secret else None
    if step is None:
        attempts = coalesce(p.get('attempts', UNDEF), 0) + 1
        if attempts >= TWO_FACTOR_SETUP_ATTEMPTS:
            pid = pending['id']
            remove_where(ctx, 'pending_tokens', lambda t: t['id'] == pid)
            err(409, 'TWO_FACTOR_SETUP', 'Too many wrong codes. Start the setup again.')
        touch(pending, {**p, 'attempts': attempts})
        err(422, 'TWO_FACTOR_CODE', 'That code didn’t match. Check that your phone’s clock is set automatically and enter the current code.', 'code')
    codes = new_recovery_codes()
    salt = random_token(16)
    uid_ = u['id']
    remove_where(ctx, 'pending_tokens', lambda t: t.get('kind') in ('two_factor_setup', 'two_factor') and t.get('user_id') == uid_)
    insert(
        ctx,
        'pending_tokens',
        {
            'kind': 'two_factor',
            'user_id': uid_,
            'ref_id': None,
            'expires_at': FAR_FUTURE,
            'payload': {
                'secret': p['secret'],
                'salt': salt,
                'recovery': [hash_recovery_code(salt, c) for c in codes],
                'last_step': step,
                'fails': 0,
                'enabled_at': now_iso(),
            },
        },
    )
    u = patch('users', u, {'two_factor': True, 'version': u['version'] + 1, 'updated_at': now_iso()})
    # the account's protection changed: other signed-in devices (which never passed the second step) are signed out
    current = _bearer(req)
    d.sessions = [x for x in d.sessions if x['user_id'] != uid_ or x['token'] == current]
    return {'user': with_access(u), 'recovery_codes': codes}


def _check_factor(rec: dict, code: Any, recovery_code: Any) -> dict:
    """an authenticated check of the second factor (turn off / new recovery codes); 5 wrong codes in a row -> password
    only. Returns the (possibly updated) factor row."""
    fails = rec['payload'].get('fails', UNDEF)
    if isinstance(fails, (int, float)) and not isinstance(fails, bool) and fails >= 5:
        err(429, 'TWO_FACTOR_LOCKED', 'Too many wrong codes. Use your password instead.', 'code')
    kind, rec2 = use_second_factor(rec, code=code, recovery_code=recovery_code)
    if not kind:
        touch(rec2, {**rec2['payload'], 'fails': coalesce(rec2['payload'].get('fails', UNDEF), 0) + 1})
        err(
            422,
            'TWO_FACTOR_CODE',
            'That code didn’t work. Enter the current code from your authenticator app.',
            'recovery_code' if js_truthy(recovery_code) else 'code',
        )
    return rec2


def disable_2fa(req: Req) -> Any:
    ctx = req.ctx
    s = scope()
    u = _me(ctx)
    rec = two_factor_record(u)
    by_code = not js_truthy(req.b('password')) and (js_truthy(req.b('code')) or js_truthy(req.b('recovery_code'))) and rec
    if by_code:
        _check_factor(rec, req.b('code'), req.b('recovery_code'))  # type: ignore[arg-type]
    else:
        pw = req.b('password')
        if not s.has_password(u['id']) or not js_truthy(pw) or not s.password_matches(u['id'], pw):
            err(422, 'PASSWORD_WRONG', 'Password is incorrect.', 'password')
    t = find_by('tenants', 'id', u.get('tenant_id'))
    # "admins" = people who can manage roles or user access (a capability, not a persona)
    if is_access_admin(permissions_of(u)) and t and js_truthy(prop(t.get('settings'), 'security.require_2fa_admins')):
        err(409, 'TWO_FACTOR_REQUIRED', 'Your workspace requires two-step verification for people who manage users or roles. Turn that off in Security first.')
    uid_ = u['id']
    remove_where(ctx, 'pending_tokens', lambda x: x.get('kind') in ('two_factor', 'two_factor_setup') and x.get('user_id') == uid_)
    u = patch('users', _me(ctx), {'two_factor': False, 'version': _me(ctx)['version'] + 1, 'updated_at': now_iso()})
    return with_access(u)


def recovery_codes(req: Req) -> Any:
    u = _me(req.ctx)
    rec = two_factor_record(u) if js_truthy(u.get('two_factor')) else None
    if not rec:
        err(409, 'TWO_FACTOR_OFF', 'Turn on two-step verification first.')
    assert rec is not None
    if not SIX_DIGITS.search(re.sub(r'\s', '', _s(req.b('code')))):
        err(422, 'TWO_FACTOR_CODE', 'Enter the 6-digit code from your authenticator app.', 'code')
    rec = _check_factor(rec, req.b('code'), UNDEF)
    codes = new_recovery_codes()
    touch(rec, {**rec['payload'], 'recovery': [hash_recovery_code(rec['payload']['salt'], c) for c in codes]})
    return {'recovery_codes': codes}


def me_avatar(req: Req) -> Any:
    ctx = req.ctx
    u = _me(ctx)
    hue = js_number(req.b('hue'))
    if not is_finite(hue) or hue < 0 or hue > 360:
        err(422, 'VALIDATION', 'Pick a colour.', 'hue')
    u = patch('users', u, {'avatar_hue': js_round(hue), 'version': u['version'] + 1, 'updated_at': now_iso()})
    return with_access(u)


def register(r: Any) -> None:
    # people
    r.get('/users', list_users)
    r.get('/users/:id', get_user)
    r.patch('/users/:id', patch_user)
    r.post('/employees', create_employee)
    r.post('/users/:id/reset-password', reset_password)
    r.post('/users/:id/2fa/reset', reset_2fa)
    r.post('/users/:id/deactivate', deactivate)
    r.post('/users/:id/reactivate', reactivate)
    # invites
    r.get('/invites', list_invites)
    r.post('/users/invite', invite_user)
    r.post('/users/bulk-invite', bulk_invite)
    r.post('/invites/:id/resend', resend_invite)
    r.delete('/invites/:id', revoke_invite)
    # team roster
    r.get('/team-roster', team_roster)
    # delegations
    r.get('/delegations', list_delegations)
    r.get('/delegations/options', delegation_options)
    r.post('/delegations', create_delegation)
    r.delete('/delegations/:id', delete_delegation)
    # sessions & 2FA
    r.get('/sessions', list_sessions)
    r.post('/sessions/revoke-others', revoke_other_sessions, allow_view_as_write=True)
    r.delete('/sessions/:id', delete_session, allow_view_as_write=True)
    r.get('/me/2fa', get_2fa)
    r.post('/me/2fa/setup', setup_2fa, allow_view_as_write=True)
    r.post('/me/2fa/verify', verify_2fa, allow_view_as_write=True)
    r.post('/me/2fa/disable', disable_2fa, allow_view_as_write=True)
    r.post('/me/2fa/recovery-codes', recovery_codes, allow_view_as_write=True)
    # UI-03: tells the set-password form whether the token is valid and the tenant's minimum length
    r.get('/auth/reset-info/:token', reset_info_shared, public=True)
    r.patch('/me/avatar', me_avatar)


__all__ = ['register', 'user_row']
