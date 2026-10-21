"""Request context and the route-module helpers — 1:1 port of the helpers of src/mock/server.ts (snake_case names).

    from app.ctx import Ctx, Req, err, warn, rows, find, insert, update, patch, remove, remove_where, has, has_any,
        require_action, require_any, role_def, permissions_of, users_with, with_access, require_role, assert_writable,
        paginate, next_number, notify, log_activity, calendar_for, user_name, can_see_salaries, can_see_margins, uid,
        now_iso, get_db, persist, set_rows, push, index, group, find_by, scope

Row access goes through the request scope (app.db.uow): read-only views, copy-on-write writes. See PORTING.md.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.auth.crypto import new_id
from app.db.uow import DbView, Scope, current_scope
from app.domain.calendar import CalendarCtx
from app.domain.permission_catalog import effective_permissions
from app.errors import ApiError, err
from app.js import UNDEF, coalesce, js_max, js_min, js_number, js_slice, now_iso, nullish, prop, today_iso

__all__ = [
    'Ctx',
    'Req',
    'ApiError',
    'err',
    'warn',
    'rows',
    'find',
    'insert',
    'update',
    'patch',
    'edit',
    'remove',
    'remove_where',
    'has',
    'has_any',
    'require_action',
    'require_any',
    'role_def',
    'permissions_of',
    'users_with',
    'with_access',
    'require_role',
    'assert_writable',
    'paginate',
    'next_number',
    'notify',
    'log_activity',
    'calendar_for',
    'user_name',
    'can_see_salaries',
    'can_see_margins',
    'uid',
    'now_iso',
    'get_db',
    'persist',
    'set_rows',
    'push',
    'index',
    'group',
    'find_by',
    'rows_where',
    'rows_where_ro',
    'rows_where_sql',
    'scope',
    'build_ctx',
    'anon_ctx',
    'PROTECTED_COLUMNS',
    'UNDEF',
]

PROTECTED_COLUMNS = ('id', 'tenant_id', 'created_at', 'created_by', 'updated_at', 'version')


def scope() -> Scope:
    """the current request scope (unit of work)"""
    return current_scope()


class Ctx:
    """Ctx of src/mock/server.ts. `user` and `tenant` always return the CURRENT row of the request (JS keeps object
    identity, so a handler that changed its own user/tenant row sees the change through ctx — this mirrors it)."""

    __slots__ = ('_user', '_tenant', 'role', 'real_role', 'view_as', 'role_key', 'perms', 'grants', 'is_super', 'today', 'warnings', 'synthetic')

    def __init__(
        self,
        user: dict,
        tenant: dict,
        role: str,
        real_role: str,
        view_as: str | None,
        role_key: Any,
        perms: set[str],
        is_super: bool,
        today: str,
        synthetic: bool = False,
    ) -> None:
        self._user = user
        self._tenant = tenant
        self.role = role
        self.real_role = real_role
        self.view_as = view_as
        self.role_key = role_key
        self.perms = perms
        self.grants: list = []
        self.is_super = is_super
        self.today = today
        self.warnings: list[dict[str, str]] = []
        self.synthetic = synthetic

    @property
    def user(self) -> dict:
        if self.synthetic:
            return self._user
        s = current_scope()
        uid_ = self._user.get('id')
        r = s.find_by('users', 'id', uid_)
        return r if r is not None else self._user

    @property
    def tenant(self) -> dict:
        if self.synthetic:
            return self._tenant
        s = current_scope()
        tid = self._tenant.get('id')
        if tid == '__none__':
            return self._tenant
        r = s.find_by('tenants', 'id', tid)
        return r if r is not None else self._tenant

    @property
    def tenant_id(self) -> str:
        return self._tenant['id']

    @property
    def user_id(self) -> str:
        return self._user['id']

    @classmethod
    def synthetic_ctx(cls, tenant: dict, user: dict) -> Ctx:
        """`{ tenant, user: { id: 'system' } } as Ctx` used by signup / invite acceptance"""
        return cls(user, tenant, 'employee', 'employee', None, 'anon', set(), False, today_iso(), synthetic=True)


@dataclass
class Req:
    """the request object a handler receives: params, query (str values / lists), body, if_match, ctx, headers"""

    params: dict[str, str]
    query: dict[str, Any]
    body: Any
    if_match: Any
    ctx: Ctx
    headers: dict[str, str] = field(default_factory=dict)

    def b(self, key: str) -> Any:
        """body.key (UNDEF when the body is not an object or the key is absent)"""
        body = self.body
        if isinstance(body, dict):
            return body.get(key, UNDEF)
        return UNDEF

    def q(self, key: str) -> Any:
        """query.key (UNDEF when absent)"""
        return self.query.get(key, UNDEF)


def warn(ctx: Ctx, code: str, message: str) -> None:
    ctx.warnings.append({'code': code, 'message': message})


def get_db() -> DbView:
    """getDb()"""
    return DbView(current_scope())


def persist() -> None:
    """persist(): write intent"""
    current_scope().persist()


def uid(prefix: str = '') -> str:
    return new_id(prefix)


def rows(ctx: Ctx, table: str) -> list:
    """rows(ctx, table): tenant-scoped rows (READ-ONLY list; use sorted()/list() to reorder)"""
    return current_scope().tenant_rows(table, ctx.tenant_id)


def rows_where(ctx: Ctx, table: str, field_: str, value: Any) -> list:
    """`rows(ctx, table).filter(r => r[field] === value)` — via the scope's group index when `value` is a string (the
    same rows in the same order, a fresh list), by a scan otherwise."""
    if type(value) is str:
        g = current_scope().tenant_group(table, ctx.tenant_id, field_)
        if g is not None:
            return list(g.get(value, ()))
    return [r for r in rows(ctx, table) if r.get(field_) == value]


def rows_where_ro(ctx: Ctx, table: str, field_: str, value: Any) -> list | tuple:
    """rows_where without the copy: a READ-ONLY sequence (shared index list) — iterate it, never mutate or keep it"""
    if type(value) is str:
        g = current_scope().tenant_group(table, ctx.tenant_id, field_)
        if g is not None:
            return g.get(value, ())
    return [r for r in rows(ctx, table) if r.get(field_) == value]


def rows_where_sql(ctx: Ctx, table: str, field_: str, value: Any) -> list:
    """rows_where for READ-ONLY use in reads of large tables: an index-backed query when the table is not loaded or
    cached (same rows, same order), else the table view. Never patch these rows."""
    r = current_scope().rows_by_field(table, ctx.tenant_id, field_, value)
    return r if r is not None else rows_where(ctx, table, field_, value)


def find_by(table: str, field_: str, value: Any) -> dict | None:
    """getDb()[table].find(r => r[field] === value) via a cached index (None when missing)"""
    return current_scope().find_by(table, field_, value)


def index(table: str, field_: str) -> dict:
    """value -> first row (read-only dict) of getDb()[table]"""
    return current_scope().index(table, field_)


def group(table: str, field_: str) -> dict:
    """value -> rows of getDb()[table] with that value (table order)"""
    return current_scope().group(table, field_)


def find(ctx: Ctx, table: str, id_: Any, label: str = 'Record') -> dict:
    """fetch by id inside the tenant; anything else is a 404 (never reveal other tenants)"""
    # an object/array id from a request body never equals a stored id (JS ===); unhashable in the index lookup
    r = None if isinstance(id_, (dict, list)) else current_scope().find_by(table, 'id', id_)
    if r is None or r.get('tenant_id') != ctx.tenant_id:
        err(404, 'NOT_FOUND', f'{label} not found.')
    return r


def insert(ctx: Ctx | None, table: str, data: dict) -> dict:
    t = now_iso()
    row = {
        'id': uid(),
        'tenant_id': ctx.tenant_id if ctx is not None else UNDEF,
        'created_at': t,
        'updated_at': t,
        'created_by': coalesce(ctx.user.get('id', UNDEF) if ctx is not None else UNDEF, None),
        'version': 1,
        **data,
    }
    current_scope().append(table, row)
    return row


def update(ctx: Ctx, table: str, id_: Any, changes: dict, if_match: Any = UNDEF) -> dict:
    """update(): If-Match check (409 STALE), protected columns dropped, updated_at + version bump. Returns the NEW row."""
    r = find(ctx, table, id_)
    if if_match is not UNDEF and if_match != r.get('version'):
        err(409, 'STALE', 'Someone else changed this record. Reload to see the latest version.')
    safe = {k: v for k, v in changes.items() if k not in PROTECTED_COLUMNS}
    return current_scope().patch(table, r, {**safe, 'updated_at': now_iso(), 'version': r['version'] + 1})


def patch(table: str, row: dict, changes: dict) -> dict:
    """Object.assign(row, changes) + persist() for a row of `table` (copy on write). Returns the NEW row — use it (and
    re-read lists) afterwards; the old object is unchanged."""
    return current_scope().patch(table, row, changes)


def edit(table: str, row: dict) -> dict:
    """a private, deep-mutable copy of a row put in its place: then mutate it in place like the TS (nested arrays,
    helper functions that change the row…); it is written at flush. Returns the copy — use it from now on."""
    return current_scope().edit(table, row)


def remove(ctx: Ctx, table: str, id_: Any, if_match: Any = UNDEF) -> None:
    r = find(ctx, table, id_)
    if if_match is not UNDEF and if_match != r.get('version'):
        err(409, 'STALE', 'Someone else changed this record. Reload to see the latest version.')
    current_scope().remove_row(table, r)


def remove_where(ctx: Ctx, table: str, pred: Callable[[dict], Any]) -> None:
    s = current_scope()
    tid = ctx.tenant_id
    s.set_rows(table, [r for r in s.view(table) if not (r.get('tenant_id') == tid and pred(r))])


def set_rows(table: str, new_rows: Iterable[dict]) -> None:
    """getDb()[table] = new_rows (+ persist)"""
    current_scope().set_rows(table, new_rows)


def push(table: str, row: dict) -> dict:
    """getDb()[table].push(row) (+ persist)"""
    return current_scope().append(table, row)


def has(ctx: Ctx, action: str) -> bool:
    return action in ctx.perms


def has_any(ctx: Ctx, *actions: str) -> bool:
    return any(a in ctx.perms for a in actions)


def require_action(ctx: Ctx, action: str, message: str = 'You do not have access to this.') -> None:
    if action not in ctx.perms:
        err(403, 'FORBIDDEN', message)


def require_any(ctx: Ctx, actions: Iterable[str], message: str = 'You do not have access to this.') -> None:
    if not any(a in ctx.perms for a in actions):
        err(403, 'FORBIDDEN', message)


def role_def(tenant_id: Any, key: Any) -> dict | None:
    """role definition of a tenant by key (None when missing)"""
    if not key:
        return None
    for r in current_scope().group('roles', 'key').get(key, ()):
        if r.get('tenant_id') == tenant_id:
            return r
    return None


def permissions_of(u: dict) -> list[str]:
    """effective permissions of a stored user (role ∪ extra − revoked); deactivated users have none"""
    if u.get('status') == 'deactivated':
        return []
    rd = role_def(u.get('tenant_id'), u.get('role_key'))
    return effective_permissions(
        rd['permissions'] if rd and rd.get('active') else [], coalesce(prop(u, 'extra_permissions'), []), coalesce(prop(u, 'revoked_permissions'), [])
    )


def users_with(ctx: Ctx, action: str) -> list[dict]:
    return [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not u.get('is_super_admin') and action in permissions_of(u)]


def with_access(u: dict) -> dict:
    """user as sent to the browser: stored fields + computed permissions and role name"""
    rd = role_def(u.get('tenant_id'), u.get('role_key'))
    return {**u, 'permissions': permissions_of(u), 'role_name': coalesce(rd.get('name', UNDEF) if rd else UNDEF, prop(u, 'title'))}


def require_role(ctx: Ctx, roles_: Iterable[str], message: str = 'You do not have access to this.') -> None:
    if ctx.role not in roles_:
        err(403, 'FORBIDDEN', message)


def assert_writable(ctx: Ctx, path: str) -> None:
    """called for every non-GET (the router does it): View-as is read-only (403), lapsed tenants are read-only (402)"""
    if ctx.view_as:
        err(403, 'VIEW_AS_READ_ONLY', 'You are viewing as another role. Switch back to make changes.')
    st = ctx.tenant.get('status')
    if st in ('read_only', 'suspended') and not path.startswith('/billing') and not path.startswith('/auth'):
        err(402, 'TENANT_READ_ONLY', 'Your plan has lapsed. The workspace is read-only until billing is renewed.')


def paginate(items: list, query: dict | None = None) -> dict[str, Any]:
    q = query or {}
    page = js_max(1, js_number(coalesce(q.get('page', UNDEF), 1)))
    size = js_min(200, js_max(1, js_number(coalesce(q.get('page_size', UNDEF), 50))))
    return {'items': js_slice(items, (page - 1) * size, page * size), 'total': len(items), 'page': page, 'page_size': size}


def next_number(ctx: Ctx, scope_name: str, start: int = 1) -> int:
    s = current_scope()
    tid = ctx.tenant_id
    seq = next((x for x in s.view('number_sequences') if x.get('tenant_id') == tid and x.get('scope') == scope_name), None)
    if seq is None:
        seq = s.append('number_sequences', {'tenant_id': tid, 'scope': scope_name, 'next_value': start})
    v = seq['next_value']
    s.patch('number_sequences', seq, {'next_value': v + 1})
    return v


def notify(ctx: Ctx, user_id: str, type_: str, title: str, body: str, link: Any = None) -> dict:
    return insert(ctx, 'notifications', {'user_id': user_id, 'type': type_, 'title': title, 'body': body, 'link': link, 'read_at': None})


def log_activity(ctx: Ctx, entity: str, entity_id: Any, action: str, summary: str) -> dict:
    return insert(
        ctx, 'activity_log', {'entity': entity, 'entity_id': entity_id, 'action': action, 'summary': summary, 'actor_id': ctx.user.get('id'), 'at': now_iso()}
    )


def calendar_for(ctx: Ctx, user_id: str | None = None) -> CalendarCtx:
    holidays = rows(ctx, 'holidays')
    leave_dates = None
    if user_id:
        leave_dates = {lv['date'] for lv in rows(ctx, 'leave') if lv.get('user_id') == user_id and lv.get('portion') == 'full'}
    return CalendarCtx(ctx.tenant.get('settings'), holidays, leave_dates)


def user_name(ctx: Ctx, id_: Any) -> str:
    if not id_:
        return '—'
    tid = ctx.tenant_id
    for u in current_scope().group('users', 'id').get(id_, ()):
        if u.get('tenant_id') == tid:
            return coalesce(u.get('name', UNDEF), '—')
    return '—'


def can_see_salaries(ctx: Ctx) -> bool:
    return 'cost_rates.view' in ctx.perms


def can_see_margins(ctx: Ctx) -> bool:
    return 'margins.view' in ctx.perms


# ---------------------------------------------------------------------------------------------------------------------
# context construction (buildCtx of server.ts)
# ---------------------------------------------------------------------------------------------------------------------
ANON_USER = {'id': 'anon', 'role': 'employee', 'grants': []}
ANON_TENANT = {'id': '__none__', 'settings': {}, 'status': 'active'}


def _default_settings() -> dict:
    from app.catalog import cat

    return dict(cat('mock/seed')['DEFAULT_SETTINGS'])


def build_ctx(user: dict, tenant_id: str, headers: dict[str, str]) -> Ctx:
    s = current_scope()
    # a user without a workspace row (the platform super admin) reads the DEFAULT workspace settings (buildCtx)
    tenant = s.find_by('tenants', 'id', tenant_id) or {**ANON_TENANT, 'id': tenant_id, 'settings': _default_settings()}
    user_perms = permissions_of(user)
    requested = headers.get('X-View-As') or None
    view_def = role_def(tenant['id'], requested) if requested and 'view_as' in user_perms else None
    view_perms = effective_permissions(view_def['permissions']) if view_def else None
    return Ctx(
        user=user,
        tenant=tenant,
        real_role=user.get('role'),
        role=view_def['persona'] if view_def else user.get('role'),
        view_as=view_def['key'] if view_def else None,
        role_key=view_def['key'] if view_def else prop(user, 'role_key'),
        perms=set(view_perms if view_perms is not None else user_perms),
        is_super=bool(user.get('is_super_admin')),
        today=today_iso(),
    )


def anon_ctx() -> Ctx:
    return Ctx(ANON_USER, ANON_TENANT, 'employee', 'employee', None, 'anon', set(), False, today_iso(), synthetic=True)


def nullish_or(v: Any, d: Any) -> Any:
    return d if nullish(v) else v
