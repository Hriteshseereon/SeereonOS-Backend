"""v4 roles & permissions, plus the organisation setup checklist — port of src/mock/routes/access.ts.

Roles are tenant data (roles). Every write goes through maker-checker (`propose`, entity 'role'); the applier below
performs the real change, so the same rules run on direct apply and on approval. Integrity rules:
  * role names are unique per tenant; permission keys must exist in the catalogue
  * `requires` dependencies are added automatically (and reported back as a warning)
  * system roles cannot be deleted or deactivated; Administrator keeps LOCKED_PERMISSIONS
  * custom roles can be deleted only when nobody holds them
  * nobody may lock the workspace out: at least one active person must keep both
    "Manage roles" and "Manage user access"

Exports used by app/routes/people.py: with_dependencies, clean_permissions, perm_label, diff_summary, is_access_admin,
assert_no_lockout, propose_named.
"""

from __future__ import annotations

import re
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    err,
    get_db,
    has,
    insert,
    log_activity,
    patch,
    remove,
    require_action,
    require_any,
    role_def,
    rows,
    scope,
    update,
    user_name,
    warn,
)
from app.domain.permission_catalog import (
    LOCKED_PERMISSIONS,
    MODULE_LABEL,
    MODULE_ORDER,
    PERMISSION_BY_KEY,
    PERMISSIONS,
    effective_permissions,
    is_permission_key,
)
from app.engine.maker_checker import assert_base_version, eligible_checkers, mc_config, pending_brief, propose, register_applier
from app.js import UNDEF, coalesce, js_join, js_len, js_or, js_str, js_trim, js_truthy, locale_key, nullish, prop

PERSONAS = ('admin', 'pm', 'sales', 'ops', 'employee')
ADMIN_PAIR = ('roles.manage', 'users.manage')


# ---------------------------------------------------------------------------------------------------------------------
# Helpers shared with routes/people.py
# ---------------------------------------------------------------------------------------------------------------------
def with_dependencies(keys: list[str]) -> dict[str, list[str]]:
    """Add every `requires` dependency (transitively). Returns the closed set and what was added."""
    s = dict.fromkeys(keys)
    added: list[str] = []
    grew = True
    while grew:
        grew = False
        for k in list(s):
            p = PERMISSION_BY_KEY.get(k) if isinstance(k, str) else None
            for dep in coalesce(p.get('requires', UNDEF) if p else UNDEF, []):
                if dep not in s:
                    s[dep] = None
                    added.append(dep)
                    grew = True
    return {'permissions': sorted(s), 'added': added}


def clean_permissions(inp: Any, field: str = 'permissions') -> list[str]:
    """Validate a permission list from the client: unknown keys are a 422."""
    if not isinstance(inp, list):
        err(422, 'VALIDATION', 'Send the permissions as a list.', field)
    bad = [k for k in inp if not isinstance(k, str) or not is_permission_key(k)]
    if bad:
        err(422, 'PERMISSION_UNKNOWN', f'Unknown permission{"" if len(bad) == 1 else "s"}: {js_join(bad[:4], ", ")}.', field)
    return sorted(dict.fromkeys(inp))


def perm_label(k: Any) -> str:
    p = PERMISSION_BY_KEY.get(k) if isinstance(k, str) else None
    return js_str(coalesce(p.get('label', UNDEF) if p else UNDEF, k))


def diff_summary(before: list[str], after: list[str]) -> dict[str, Any]:
    """ "+a, +b, −c" summary of a permission diff."""
    b = set(before)
    a = set(after)
    plus = [k for k in after if k not in b]
    minus = [k for k in before if k not in a]
    parts = ['+' + k for k in plus] + ['−' + k for k in minus]
    text = f'{", ".join(parts[:6])} and {len(parts) - 6} more' if len(parts) > 6 else ', '.join(parts)
    return {'plus': plus, 'minus': minus, 'text': text}


def is_access_admin(perms: Any) -> bool:
    """Is this capability-holder an access administrator (can manage roles or users)?"""
    return 'roles.manage' in perms or 'users.manage' in perms


def assert_no_lockout(ctx: Ctx, h: dict[str, Any]) -> None:
    """Throws 409 LOCKOUT when, after the hypothetical change, no active person keeps roles.manage + users.manage.
    h = {'roles': {key: {'permissions', 'active'} | None}, 'users': {id: {...partial user}}}"""
    h_roles = h.get('roles')
    h_users = h.get('users') or {}
    ok = False
    for u0 in rows(ctx, 'users'):
        if u0.get('is_super_admin'):
            continue
        u = {**u0, **(h_users.get(u0['id']) or {})}
        if u.get('status') != 'active':
            continue
        rk = u.get('role_key')
        if h_roles is not None and rk in h_roles:
            r = h_roles[rk]
            role_perms = r['permissions'] if r and r.get('active') else []
        else:
            rd = role_def(ctx.tenant_id, rk)
            role_perms = rd['permissions'] if rd and rd.get('active') else []
        eff = effective_permissions(role_perms, coalesce(prop(u, 'extra_permissions'), []), coalesce(prop(u, 'revoked_permissions'), []))
        if all(p in eff for p in ADMIN_PAIR):
            ok = True
            break
    if not ok:
        err(
            409,
            'LOCKOUT',
            'This would leave nobody who can manage roles and user access. Keep “Manage roles” and “Manage user access” with at least one active person.',
        )


def _slugify(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '_', name.lower())
    s = re.sub(r'^_|_$', '', s)
    return s[:40] or 'role'


def _unique_key(ctx: Ctx, name: str) -> str:
    base = _slugify(name)
    taken = {r['key'] for r in rows(ctx, 'roles')}
    if base not in taken:
        return base
    i = 2
    while True:
        if f'{base}_{i}' not in taken:
            return f'{base}_{i}'
        i += 1


def _assert_name_free(ctx: Ctx, name: str, except_key: Any = UNDEF) -> None:
    n = js_trim(name).lower()
    if any(r['key'] != except_key and js_trim(r['name']).lower() == n for r in rows(ctx, 'roles')):
        err(409, 'ROLE_NAME_TAKEN', f'A role called “{js_trim(name)}” already exists. Choose another name.', 'name')


def _role_or_404(ctx: Ctx, key: Any) -> dict:
    r = next((x for x in rows(ctx, 'roles') if x['key'] == key), None)
    if r is None:
        err(404, 'NOT_FOUND', 'Role not found.')
    return r  # type: ignore[return-value]


def _active_holders(ctx: Ctx, key: Any) -> list[dict]:
    return [u for u in rows(ctx, 'users') if u.get('role_key') == key and u.get('status') != 'deactivated' and not u.get('is_super_admin')]


def _role_row(ctx: Ctx, r: dict) -> dict[str, Any]:
    return {
        **r,
        'permissions': effective_permissions(r['permissions']),
        'user_count': len(_active_holders(ctx, r['key'])),
        'locked_permissions': list(LOCKED_PERMISSIONS) if r['key'] == 'administrator' else [],
        'pending': pending_brief(ctx, 'role', r['id']),
    }


def propose_named(ctx: Ctx, p: dict[str, Any]) -> dict[str, Any]:
    """propose() plus the names of who can approve, so the UI can say "Sent to Santosh for approval"."""
    r = propose(ctx, p)
    if not r['pending']:
        return r
    return {**r, 'checker_names': [u.get('name', UNDEF) for u in eligible_checkers(ctx, p['required_permission'], ctx.user_id)]}


# ---------------------------------------------------------------------------------------------------------------------
# Role payload validation
# ---------------------------------------------------------------------------------------------------------------------
def _g(body: Any, k: str) -> Any:
    return body.get(k, UNDEF) if isinstance(body, dict) else UNDEF


def _read_role_input(ctx: Ctx, body: Any, cur: dict | None) -> dict[str, Any]:
    b_name = _g(body, 'name')
    name = js_trim(js_str(coalesce(b_name, ''))) if b_name is not UNDEF else coalesce(cur.get('name', UNDEF) if cur else UNDEF, '')
    if js_len(name) < 2:
        err(422, 'VALIDATION', 'Give the role a name (at least 2 characters).', 'name')
    if js_len(name) > 60:
        err(422, 'VALIDATION', 'Keep the name under 60 characters.', 'name')
    _assert_name_free(ctx, name, cur['key'] if cur else UNDEF)
    b_desc = _g(body, 'description')
    description = js_trim(js_str(coalesce(b_desc, '')))[:240] if b_desc is not UNDEF else coalesce(cur.get('description', UNDEF) if cur else UNDEF, '')
    b_persona = _g(body, 'persona')
    persona = b_persona if b_persona is not UNDEF else coalesce(cur.get('persona', UNDEF) if cur else UNDEF, 'employee')
    if not (isinstance(persona, str) and persona in PERSONAS):
        err(422, 'VALIDATION', 'Choose what the role sees on Home.', 'persona')
    b_active = _g(body, 'active')
    active = js_truthy(b_active) if b_active is not UNDEF else coalesce(cur.get('active', UNDEF) if cur else UNDEF, True)
    b_perms = _g(body, 'permissions')
    perms = clean_permissions(b_perms) if b_perms is not UNDEF else effective_permissions(coalesce(cur.get('permissions', UNDEF) if cur else UNDEF, []))
    dep = with_dependencies(perms)
    perms = dep['permissions']
    if cur and cur.get('is_system') and not active:
        err(
            409,
            'ROLE_SYSTEM',
            f'{cur["name"]} is a built-in role and can’t be deactivated. Remove people from it or edit its permissions instead.',
            'active',
        )
    if cur and cur['key'] == 'administrator':
        missing = [k for k in LOCKED_PERMISSIONS if k not in perms]
        if missing:
            err(409, 'ROLE_LOCKED', f'Administrator always keeps {", ".join(perm_label(k) for k in missing)} — it is the way back in.', 'permissions')
        if persona != 'admin':
            err(409, 'ROLE_LOCKED', 'Administrator always opens the leadership dashboard.', 'persona')
    return {'data': {'name': name, 'description': description, 'persona': persona, 'permissions': perms, 'active': active}, 'added': dep['added']}


# ---------------------------------------------------------------------------------------------------------------------
# Setup checklist
# ---------------------------------------------------------------------------------------------------------------------
def _t(v: Any) -> str:
    """`x?.trim()` truthiness helper: '' for null/undefined/non-strings"""
    return js_trim(v) if isinstance(v, str) else ''


def _setup_status(ctx: Ctx) -> dict[str, Any]:
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    org = prop(t, 'org')

    def active(xs: list) -> list:
        return [x for x in xs if x.get('active')]

    locs = active(rows(ctx, 'locations'))
    shifts = active(rows(ctx, 'shifts'))
    depts = active(rows(ctx, 'departments'))
    desig = active(rows(ctx, 'designations'))
    grades = active(rows(ctx, 'grades'))
    roles = rows(ctx, 'roles')
    people = [u for u in rows(ctx, 'users') if not u.get('is_super_admin') and u.get('status') != 'deactivated']
    created = sum(1 for u in people if js_truthy(u.get('employee_code')))
    mc = mc_config(ctx)
    workflows_touched = any(w['version'] > 1 for w in rows(ctx, 'approval_workflows'))
    templates_touched = any(x['version'] > 1 for x in rows(ctx, 'email_templates'))
    sent = len(rows(ctx, 'email_outbox'))
    roles_touched = any(r['version'] > 1 or not r.get('is_system') for r in roles) or any(c.get('entity') == 'role' for c in rows(ctx, 'change_sets'))

    def n(k: int, one: str, many: str | None = None) -> str:
        return f'{k} {one if k == 1 else (many if many is not None else one + "s")}'

    org_ok = js_truthy(org)
    org_done = (
        org_ok
        and bool(_t(org.get('legal_name')))
        and bool(_t(org.get('gstin')) or _t(org.get('pan')))
        and bool(_t(org.get('registered_address')) or _t(org.get('corporate_address')))
    )
    custom = [r for r in roles if not r.get('is_system')]
    steps = [
        {
            'key': 'org',
            'title': 'Organisation profile',
            'body': 'Legal name, GSTIN or PAN, registered address and the employee code format.',
            'done': org_done,
            'meta': org.get('legal_name', UNDEF) if org_done else 'Legal name, tax ID and address missing',
            'links': [{'label': 'Open profile', 'to': '/setup/org', 'action': 'master.view'}],
        },
        {
            'key': 'places',
            'title': 'Locations & shifts',
            'body': 'Where people work and their working hours.',
            'done': len(locs) > 0 and len(shifts) > 0,
            'meta': f'{n(len(locs), "location")} · {n(len(shifts), "shift")}',
            'links': [
                {'label': 'Locations', 'to': '/setup/locations', 'action': 'master.view'},
                {'label': 'Shifts', 'to': '/setup/shifts', 'action': 'master.view'},
            ],
        },
        {
            'key': 'structure',
            'title': 'Departments, designations & grades',
            'body': 'Your structure. Designations can suggest a default role for new hires.',
            'done': len(depts) > 0 and len(desig) > 0 and len(grades) > 0,
            'meta': f'{n(len(depts), "department")} · {n(len(desig), "designation")} · {n(len(grades), "grade")}',
            'links': [
                {'label': 'Departments', 'to': '/setup/departments', 'action': 'master.view'},
                {'label': 'Designations', 'to': '/setup/designations', 'action': 'master.view'},
                {'label': 'Grades', 'to': '/setup/grades', 'action': 'master.view'},
            ],
        },
        {
            'key': 'roles',
            'title': 'Review roles & permissions',
            'body': 'Nine roles are ready. Adjust what each can see and do, or add your own.',
            'done': roles_touched,
            'meta': n(sum(1 for r in roles if r.get('active')), 'active role') + (' · ' + n(len(custom), 'custom role') if custom else ''),
            'links': [{'label': 'Roles & permissions', 'to': '/setup/roles', 'action': 'roles.view'}],
        },
        {
            'key': 'controls',
            'title': 'Approval workflows & maker-checker',
            'body': 'Who approves leave, expenses and purchases, and which changes need a second person.',
            'done': js_truthy(mc.get('enabled')) or workflows_touched,
            'meta': f'Maker-checker on for {n(len(mc["entities"]), "area")}' if js_truthy(mc.get('enabled')) else 'Maker-checker off',
            'links': [
                {'label': 'Workflows', 'to': '/setup/workflows', 'action': 'workflows.manage'},
                {'label': 'Maker-checker', 'to': '/setup/maker-checker', 'action': 'makerchecker.configure'},
            ],
        },
        {
            'key': 'people',
            'title': 'Create employees',
            'body': 'Add people with their role and manager. Each gets a welcome email with login details.',
            'done': len(people) >= 3,
            'meta': n(len(people), 'person', 'people') + ' active' + (' · ' + n(created, 'employee code') if created else ''),
            'links': [
                {'label': 'Create employee', 'to': '/people/new', 'action': 'employees.create'},
                {'label': 'People', 'to': '/people', 'action': 'users.view'},
            ],
        },
        {
            'key': 'emails',
            'title': 'Email templates',
            'body': 'Check the welcome email and other messages before people receive them.',
            'done': templates_touched or sent > 0,
            'meta': f'{n(sent, "email")} sent' if sent else n(len(rows(ctx, 'email_templates')), 'template'),
            'links': [{'label': 'Emails', 'to': '/setup/emails', 'action': 'emails.view'}],
        },
    ]
    done = sum(1 for s in steps if s['done'])
    return {
        'steps': steps,
        'done': done,
        'total': len(steps),
        'complete': done == len(steps),
        'company': js_or(org.get('brand_name', UNDEF) if org_ok else UNDEF, t['name'] if t else UNDEF),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------------------------------
def list_permissions(req: Req) -> Any:
    return [{'module': m, 'label': MODULE_LABEL[m], 'permissions': [p for p in PERMISSIONS if p['module'] == m]} for m in MODULE_ORDER]


def list_roles(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['roles.view', 'view_as', 'users.view', 'employees.create'])
    full = has(ctx, 'roles.view') or has(ctx, 'users.view') or has(ctx, 'employees.create')
    out = [_role_row(ctx, x) for x in rows(ctx, 'roles') if full or x.get('active')]
    return sorted(out, key=lambda r: -1 if r.get('is_system') else 0)


def get_role(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'roles.view')
    role = _role_or_404(ctx, req.params['key'])
    members = [
        {
            'id': u['id'],
            'name': prop(u, 'name'),
            'email': prop(u, 'email'),
            'title': prop(u, 'title'),
            'status': prop(u, 'status'),
            'avatar_hue': prop(u, 'avatar_hue'),
            'extra_permissions': coalesce(prop(u, 'extra_permissions'), []),
            'revoked_permissions': coalesce(prop(u, 'revoked_permissions'), []),
            'last_login_at': prop(u, 'last_login_at'),
        }
        for u in rows(ctx, 'users')
        if u.get('role_key') == role['key'] and not u.get('is_super_admin')
    ]
    members.sort(key=lambda m: locale_key(m['name']))
    return {**_role_row(ctx, role), 'members': members, 'created_by_name': user_name(ctx, role['created_by']) if js_truthy(role.get('created_by')) else None}


def create_role(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'roles.manage', 'You can’t create roles.')
    seed = None
    if js_truthy(req.b('copy_from')):
        seed = _role_or_404(ctx, js_str(req.b('copy_from')))
    from app.js import js_spread

    inp = {
        **js_spread(req.body),
        'permissions': coalesce(req.b('permissions'), seed['permissions'] if seed else []),
        'persona': coalesce(req.b('persona'), seed['persona'] if seed else UNDEF, 'employee'),
    }
    ri = _read_role_input(ctx, inp, None)
    data, added = ri['data'], ri['added']
    if added:
        warn(ctx, 'PERMISSIONS_ADDED', f'Also added what these need: {", ".join(perm_label(k) for k in added)}.')
    key = _unique_key(ctx, data['name'])
    copy_of = f' (copy of {seed["name"]})' if seed else ''
    return propose_named(
        ctx,
        {
            'entity': 'role',
            'entity_id': None,
            'action': 'create',
            'summary': f'New role {data["name"]}{copy_of}: {len(data["permissions"])} permissions',
            'before': None,
            'after': {**data, 'key': key},
            'payload': {'op': 'create', 'key': key, 'data': data},
            'required_permission': 'roles.manage',
        },
    )


def clone_role(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'roles.manage', 'You can’t create roles.')
    src = _role_or_404(ctx, req.params['key'])
    name = js_trim(js_str(coalesce(req.b('name'), '')))
    if not name:
        name = f'{src["name"]} (copy)'
        i = 2
        while any(x['name'].lower() == name.lower() for x in rows(ctx, 'roles')):
            name = f'{src["name"]} (copy {i})'
            i += 1
    ri = _read_role_input(
        ctx,
        {
            'name': name,
            'description': src.get('description', UNDEF),
            'persona': src.get('persona', UNDEF),
            'permissions': src.get('permissions', UNDEF),
            'active': True,
        },
        None,
    )
    data = ri['data']
    key = _unique_key(ctx, data['name'])
    return propose_named(
        ctx,
        {
            'entity': 'role',
            'entity_id': None,
            'action': 'create',
            'summary': f'New role {data["name"]} (copy of {src["name"]}): {len(data["permissions"])} permissions',
            'before': None,
            'after': {**data, 'key': key},
            'payload': {'op': 'create', 'key': key, 'data': data},
            'required_permission': 'roles.manage',
        },
    )


def patch_role(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'roles.manage', 'You can’t change roles.')
    role = _role_or_404(ctx, req.params['key'])
    if req.if_match is not UNDEF and req.if_match != role['version']:
        err(409, 'STALE', 'Someone else changed this role. Reload to see the latest version.')
    ri = _read_role_input(ctx, req.body, role)
    data, added = ri['data'], ri['added']
    if added:
        warn(ctx, 'PERMISSIONS_ADDED', f'Also turned on what these need: {", ".join(perm_label(k) for k in added)}.')
    before = {
        'name': role['name'],
        'description': prop(role, 'description'),
        'persona': role['persona'],
        'permissions': effective_permissions(role['permissions']),
        'active': role['active'],
    }
    parts: list[str] = []
    if data['name'] != role['name']:
        parts.append(f'rename to {data["name"]}')
    if data['persona'] != role['persona']:
        parts.append(f'home → {data["persona"]}')
    if data['active'] != role['active']:
        parts.append('activate' if data['active'] else 'deactivate')
    if data['description'] != prop(role, 'description'):
        parts.append('description')
    d = diff_summary(before['permissions'], data['permissions'])
    if d['text']:
        parts.append(d['text'])
    if not parts:
        return {'pending': False, 'result': _role_row(ctx, role), 'result_id': role['id']}
    if not data['active'] and _active_holders(ctx, role['key']):
        err(
            409,
            'ROLE_IN_USE',
            f'{len(_active_holders(ctx, role["key"]))} people hold {role["name"]}. Move them to another role before deactivating it.',
            'active',
        )
    assert_no_lockout(ctx, {'roles': {role['key']: {'permissions': data['permissions'], 'active': data['active']}}})
    return propose_named(
        ctx,
        {
            'entity': 'role',
            'entity_id': role['id'],
            'action': 'update',
            'summary': f'{role["name"]}: {"; ".join(parts)}',
            'before': before,
            'after': {**data},
            'payload': {'op': 'update', 'key': role['key'], 'data': data},
            'required_permission': 'roles.manage',
            'base_version': role['version'],
        },
    )


def delete_role(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'roles.manage', 'You can’t delete roles.')
    role = _role_or_404(ctx, req.params['key'])
    if role.get('is_system'):
        err(409, 'ROLE_SYSTEM', f'{role["name"]} is a built-in role and can’t be deleted.')
    holders = [u for u in rows(ctx, 'users') if u.get('role_key') == role['key'] and not u.get('is_super_admin')]
    if holders:
        err(409, 'ROLE_IN_USE', f'{len(holders)} {"person holds" if len(holders) == 1 else "people hold"} {role["name"]}. Move them to another role first.')
    return propose_named(
        ctx,
        {
            'entity': 'role',
            'entity_id': role['id'],
            'action': 'delete',
            'summary': f'Delete role {role["name"]}',
            'before': {'name': role['name'], 'permissions': role['permissions']},
            'after': None,
            'payload': {'op': 'delete', 'key': role['key']},
            'required_permission': 'roles.manage',
            'base_version': role['version'],
        },
    )


def setup_status(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['master.view', 'roles.view', 'employees.create', 'workflows.manage', 'makerchecker.configure', 'emails.view'])
    return _setup_status(ctx)


def register(r: Any) -> None:
    r.get('/permissions', list_permissions)
    r.get('/roles', list_roles)
    r.get('/roles/:key', get_role)
    r.post('/roles', create_role)
    r.post('/roles/:key/clone', clone_role)
    r.patch('/roles/:key', patch_role)
    r.delete('/roles/:key', delete_role)
    r.get('/setup/status', setup_status)
    register_v9(r)  # v9 (A5)


# ---------------------------------------------------------------------------------------------------------------------
# Applier — the one place a role is actually written
# ---------------------------------------------------------------------------------------------------------------------
def _apply_role(ctx: Ctx, cs: dict) -> dict[str, Any]:
    p = cs['payload']
    by = f' (proposed by {user_name(ctx, cs["maker_id"])})' if cs['id'] != 'cs_direct' else ''
    if p['op'] == 'create':
        data = p['data']
        _assert_name_free(ctx, data['name'])
        key = _unique_key(ctx, data['name']) if any(x['key'] == p['key'] for x in rows(ctx, 'roles')) else p['key']
        row = insert(
            ctx,
            'roles',
            {
                'key': key,
                'name': data['name'],
                'description': data['description'],
                'persona': data['persona'],
                'permissions': list(data['permissions']),
                'is_system': False,
                'active': data['active'],
            },
        )
        if cs['id'] != 'cs_direct':
            row['created_by'] = cs['maker_id']
        log_activity(ctx, 'role', row['id'], 'create', f'Created role {row["name"]}{by}')
        return {'result': _role_row(ctx, row), 'result_id': row['id']}
    role = _role_or_404(ctx, p['key'])
    assert_base_version(cs, role['version'])
    if p['op'] == 'delete':
        if role.get('is_system'):
            err(409, 'ROLE_SYSTEM', f'{role["name"]} is a built-in role and can’t be deleted.')
        if any(u.get('role_key') == role['key'] and not u.get('is_super_admin') for u in rows(ctx, 'users')):
            err(409, 'ROLE_IN_USE', f'People still hold {role["name"]}.')
        remove(ctx, 'roles', role['id'])
        log_activity(ctx, 'role', role['id'], 'delete', f'Deleted role {role["name"]}{by}')
        return {'result': {'ok': True}, 'result_id': role['id']}
    data = p['data']
    _assert_name_free(ctx, data['name'], role['key'])
    if role['key'] == 'administrator' and any(k not in data['permissions'] for k in LOCKED_PERMISSIONS):
        err(409, 'ROLE_LOCKED', 'Administrator must keep role and user management.')
    assert_no_lockout(ctx, {'roles': {role['key']: {'permissions': data['permissions'], 'active': data['active']}}})
    d = diff_summary(effective_permissions(role['permissions']), data['permissions'])
    out = update(
        ctx,
        'roles',
        role['id'],
        {
            'name': data['name'],
            'description': data['description'],
            'persona': data['persona'],
            'permissions': list(data['permissions']),
            'active': data['active'],
        },
    )
    # persona of holders follows the role (home dashboard, default views)
    tid = ctx.tenant_id
    for u in list(get_db().users):
        if u.get('tenant_id') == tid and u.get('role_key') == role['key'] and u.get('role') != data['persona'] and not scope().is_stub(u):
            patch('users', u, {'role': data['persona']})
    log_activity(ctx, 'role', role['id'], 'update', f'Changed role {data["name"]}{": " + d["text"] if d["text"] else ""}{by}')
    return {'result': _role_row(ctx, out), 'result_id': role['id']}


register_applier('role', _apply_role)

__all__ = [
    'with_dependencies',
    'clean_permissions',
    'perm_label',
    'diff_summary',
    'is_access_admin',
    'assert_no_lockout',
    'propose_named',
    'register',
    'nullish',
]


# ---------------------------------------------------------------------------------------------------------------------
# v9 (A5, D10): who holds a permission, compare roles, the admin setup hub
# ---------------------------------------------------------------------------------------------------------------------
def permission_holders(req: Req) -> Any:
    """everyone who holds a permission and how (their role, or an extra grant) + who lost it by a per-person removal"""
    ctx = req.ctx
    require_action(ctx, 'roles.view', 'Only people who can see roles can see who holds a permission.')
    key = req.params['key']
    p = PERMISSION_BY_KEY.get(key)
    if p is None:
        err(404, 'NOT_FOUND', 'Permission not found.')
    roles = rows(ctx, 'roles')
    with_it = {r['key'] for r in roles if r.get('active') and key in effective_permissions(r['permissions'])}
    people, revoked = [], []
    for u in rows(ctx, 'users'):
        if u.get('is_super_admin') or u.get('status') == 'deactivated':
            continue
        rd = role_def(ctx.tenant_id, u.get('role_key'))
        via_role = u.get('role_key') in with_it
        extra = key in coalesce(prop(u, 'extra_permissions'), [])
        removed = key in coalesce(prop(u, 'revoked_permissions'), [])
        base = {
            'id': u['id'],
            'name': prop(u, 'name'),
            'title': prop(u, 'title'),
            'email': prop(u, 'email'),
            'avatar_hue': prop(u, 'avatar_hue'),
            'status': prop(u, 'status'),
            'role_key': u.get('role_key'),
            'role_name': rd['name'] if rd else None,
        }
        if removed and (via_role or extra):
            revoked.append(base)
        elif via_role or extra:
            people.append({**base, 'via': 'role' if via_role else 'extra'})
    people.sort(key=lambda x: locale_key(js_str(x['name'])))
    revoked.sort(key=lambda x: locale_key(js_str(x['name'])))
    req_by = [q['key'] for q in PERMISSIONS if key in (q.get('requires') or [])]
    return {
        'permission': {**p, 'module_label': MODULE_LABEL[p['module']], 'required_by': req_by},
        'roles': [
            {'key': r['key'], 'name': r['name'], 'active': r.get('active'), 'is_system': r.get('is_system'), 'user_count': len(_active_holders(ctx, r['key']))}
            for r in sorted(roles, key=lambda r: (0 if r.get('is_system') else 1, locale_key(r['name'])))
            if r['key'] in with_it
        ],
        'people': people,
        'revoked': revoked,
        'total': len(people),
    }


def compare_roles(req: Req) -> Any:
    """2–6 roles side by side, permission by permission, grouped by module (with what differs)"""
    ctx = req.ctx
    require_action(ctx, 'roles.view')
    raw = req.q('keys')
    keys = [k for k in (raw.split(',') if isinstance(raw, str) else []) if k]
    keys = list(dict.fromkeys(keys))
    if len(keys) < 2 or len(keys) > 6:
        err(422, 'VALIDATION', 'Pick between 2 and 6 roles to compare.', 'keys')
    roles = [_role_or_404(ctx, k) for k in keys]
    sets = {r['key']: set(effective_permissions(r['permissions'])) for r in roles}
    modules = []
    common = diff = 0
    only = {r['key']: 0 for r in roles}
    for m in MODULE_ORDER:
        perms = []
        for q in PERMISSIONS:
            if q['module'] != m:
                continue
            has_ = {k: q['key'] in sets[k] for k in keys}
            n = sum(1 for v in has_.values() if v)
            same = n in (0, len(keys))
            if n == len(keys):
                common += 1
            elif n > 0:
                diff += 1
                if n == 1:
                    only[next(k for k, v in has_.items() if v)] += 1
            perms.append({'key': q['key'], 'label': q['label'], 'description': q['description'], 'sensitive': bool(q.get('sensitive')), 'has': has_, 'same': same})
        modules.append({'module': m, 'label': MODULE_LABEL[m], 'permissions': perms, 'differences': sum(1 for x in perms if not x['same'])})
    return {
        'roles': [{'key': r['key'], 'name': r['name'], 'count': len(sets[r['key']]), 'user_count': len(_active_holders(ctx, r['key'])), 'active': r.get('active')} for r in roles],
        'modules': modules,
        'summary': {'common': common, 'differences': diff, 'only': only},
    }


def _hub(ctx: Ctx) -> dict[str, Any]:
    from app.engine import holiday_cal as hc
    from app.engine.leave_types import is_active as leave_active
    from app.engine.openings import current_fy, opening_row, state_of
    from app.routes.invoices import invoice_settings
    from app.routes.settings import _settings_touched

    st = _setup_status(ctx)
    by = {s['key']: s for s in st['steps']}
    tid = ctx.tenant_id
    year = ctx.today[:4]

    def n(k: int, one: str, many: str | None = None) -> str:
        return f'{k} {one if k == 1 else (many if many is not None else one + "s")}'

    items: list[dict[str, Any]] = []

    def add(group: str, key: str, title: str, body: str, status: str, meta: str, to: str, action: str, required: bool = True) -> None:
        items.append({'group': group, 'key': key, 'title': title, 'body': body, 'status': status, 'meta': meta, 'to': to, 'action': action, 'required': required})

    def done(b: bool, partial: bool = False) -> str:
        return 'done' if b else ('attention' if partial else 'todo')

    # Organisation
    add('Organisation', 'org', 'Organisation profile', by['org']['body'], done(by['org']['done']), js_str(by['org']['meta']), '/setup/org', 'master.view')
    locs = [x for x in rows(ctx, 'locations') if x.get('active')]
    add('Organisation', 'locations', 'Locations & shifts', by['places']['body'], done(by['places']['done']), by['places']['meta'], '/setup/locations', 'master.view')
    add('Organisation', 'structure', 'Departments, designations & grades', by['structure']['body'], done(by['structure']['done']), by['structure']['meta'], '/setup/departments', 'master.view')
    try:
        teams = [t for t in rows(ctx, 'teams') if t.get('active', True)]
    except Exception:  # noqa: BLE001 — the teams table arrives with V012 (A3)
        teams = None
    if teams is not None:
        add('Organisation', 'teams', 'Teams', 'Delivery teams, their leads and members (People › Team groups by team).', 'done' if teams else 'optional', n(len(teams), 'team'), '/setup/teams', 'master.view', False)
    # People, leave & calendars
    pol = rows(ctx, 'leave_policies')
    act = [p for p in pol if leave_active(p)]
    add(
        'People & time',
        'leave_types',
        'Leave types & policies',
        'Quota, accrual, carry-forward, encashment, who each type applies to and when a document is needed.',
        done(len(act) > 0),
        f'{n(len(act), "active leave type")}' + (f' · {len(pol) - len(act)} switched off' if len(pol) > len(act) else '') if pol else 'No leave types yet — leave has no quota',
        '/setup/leave-types',
        'people.manage',
    )
    cals = hc.calendars_of(tid)
    def_id = hc.default_calendar_id(tid)
    hol_year = [h for h in rows(ctx, 'holidays') if h['date'].startswith(year)]
    by_cal: dict[str, int] = {}
    for h in hol_year:
        cid = hc.calendar_of_holiday(h, tid)
        by_cal[cid] = by_cal.get(cid, 0) + 1
    used = {hc.calendar_id_for_location(tid, loc) for loc in locs} | {def_id}
    empty = [c['name'] for c in cals if c['id'] in used and not by_cal.get(c['id'])]
    add(
        'People & time',
        'holidays',
        'Holiday calendars',
        'Public and optional holidays per location; working days, leave and timesheet targets follow them.',
        'done' if not empty else ('attention' if len(empty) < len(used) else 'todo'),
        f'{n(len(cals), "calendar")} · {n(len(hol_year), "holiday")} in {year}' + (f' · no holidays yet in {", ".join(empty[:2])}' if empty else ''),
        '/setup/holidays',
        'holidays.edit',
    )
    add('People & time', 'hours', 'Working hours & calendar', 'Minimum hours, weekly offs, Saturdays and the approval deadline.', done(_settings_touched(ctx)), 'Customised' if _settings_touched(ctx) else 'Using the defaults', '/settings/company#calendar', 'settings.edit')
    add('People & time', 'people', 'Employees', by['people']['body'], done(by['people']['done']), by['people']['meta'], '/people', 'users.view')
    people = [u for u in rows(ctx, 'users') if not u.get('is_super_admin') and u.get('status') == 'active']
    rated = {x.get('user_id') for x in rows(ctx, 'cost_rates')}
    with_rate = sum(1 for u in people if u['id'] in rated)
    add(
        'People & time',
        'cost_rates',
        'Salaries & cost rates',
        'Monthly CTC per person — drives costing, margins and payroll.',
        'done' if people and with_rate == len(people) else ('attention' if with_rate else 'todo'),
        f'{with_rate} of {n(len(people), "person", "people")} have a cost rate',
        '/settings/cost-rates',
        'cost_rates.view',
    )
    # Finance
    inv = invoice_settings(ctx)
    bank_ok = bool(_t(prop(inv.get('bank'), 'account_number')))
    org = prop(find_by_tenant(ctx), 'org') or {}
    gst_ok = bool(_t(org.get('gstin'))) if isinstance(org, dict) else False
    add(
        'Finance',
        'invoicing',
        'Invoice settings',
        'Prefix, bank account, IFSC and signatory printed on every tax invoice.',
        'done' if bank_ok and gst_ok else ('attention' if bank_ok or gst_ok else 'todo'),
        ('Bank account set' if bank_ok else 'Bank account missing') + (' · GSTIN set' if gst_ok else ' · GSTIN missing'),
        '/finance/invoices?settings=1',
        'finance.view',
    )
    fy = current_fy(ctx)
    ob = opening_row(ctx, fy)
    ost = state_of(ctx, ob, fy)
    from app.domain.calendar import fy_label

    add(
        'Finance',
        'openings',
        'Opening balances',
        'Cash, bank, receivables, payables and other ledger balances at the start of the financial year.',
        'done' if ob and ob.get('lines') and not ost['reopened'] else ('attention' if ob else 'todo'),
        (f'{fy_label(fy)}: {n(len(ob["lines"]), "line")}' + (' · reopened' if ost['reopened'] else ' · locked' if ost['locked'] else ' · editable')) if ob and ob.get('lines') else f'{fy_label(fy)}: not entered',
        '/setup/opening-balances',
        'finance.view',
    )
    # Access & controls
    add('Access & controls', 'roles', 'Roles & permissions', by['roles']['body'], done(by['roles']['done']), by['roles']['meta'], '/setup/roles', 'roles.view')
    add('Access & controls', 'controls', 'Approval workflows & maker-checker', by['controls']['body'], done(by['controls']['done']), by['controls']['meta'], '/setup/workflows', 'workflows.manage')
    add('Access & controls', 'emails', 'Email templates', by['emails']['body'], done(by['emails']['done']), by['emails']['meta'], '/setup/emails', 'emails.view', False)
    req_items = [i for i in items if i['required']]
    done_n = sum(1 for i in req_items if i['status'] == 'done')
    groups: list[dict[str, Any]] = []
    for i in items:
        g = next((x for x in groups if x['title'] == i['group']), None)
        if g is None:
            g = {'title': i['group'], 'items': []}
            groups.append(g)
        g['items'].append(i)
    return {'company': st['company'], 'groups': groups, 'done': done_n, 'total': len(req_items), 'complete': done_n == len(req_items), 'attention': sum(1 for i in items if i['status'] == 'attention')}


def find_by_tenant(ctx: Ctx) -> Any:
    return scope().find_by('tenants', 'id', ctx.tenant_id)


HUB_ACTIONS = ['master.view', 'roles.view', 'employees.create', 'workflows.manage', 'makerchecker.configure', 'emails.view', 'people.manage', 'holidays.edit', 'finance.view', 'settings.edit']


def setup_hub(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, HUB_ACTIONS, 'The admin setup page is for administrators.')
    return _hub(ctx)


def register_v9(r: Any) -> None:
    r.get('/permissions/:key/holders', permission_holders)
    r.get('/roles-compare', compare_roles)
    r.get('/setup/hub', setup_hub)
