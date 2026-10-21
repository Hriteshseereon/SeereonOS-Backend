"""In-memory request scopes over a demo-seed JSON (no PostgreSQL) for engine golden tests.

The Node generators run the TypeScript engines on the mock with the same seed (setStore over the JSON); the Python
port runs inside `tenant_scope(...)`: a real `Scope` in tenant mode whose snapshot reads the JSON rows of that tenant,
deep-frozen and shared through a real TenantCache (so in-place mutations of read rows raise FrozenError, like in the
API under test). Usage:

    seed = load_seed('engine_seed.json.gz')
    with tenant_scope(seed, 'tn_seereon') as s:
        ctx = make_ctx(s, user_id, perms, today)
        ...
"""

from __future__ import annotations

import gzip
import json
import os
import re
from typing import Any

from app.catalog import cat
from app.ctx import Ctx
from app.db.cache import TenantCache
from app.db.mapping import Meta, TableMeta
from app.db.uow import Scope
from app.errors import ApiError
from app.js import INF, UNDEF
from tests.golden import VECTORS


def load_gz(name: str) -> Any:
    with gzip.open(os.path.join(VECTORS, name), 'rt', encoding='utf-8') as f:
        return json.load(f)


class _Snap:
    def __init__(self, be: MemBackend, tenant_id: str) -> None:
        self.be = be
        self.tenant_id = tenant_id
        self.versions: dict[str, int] = {}

    def load(self, t: str) -> list[dict]:
        rows = self.be.tables.get(t, [])
        if t == 'tenants':
            return [json.loads(json.dumps(r)) for r in rows if r.get('id') == self.tenant_id]
        if self.be.meta.tables[t].tenant_scoped:
            return [json.loads(json.dumps(r)) for r in rows if r.get('tenant_id') == self.tenant_id]
        return [json.loads(json.dumps(r)) for r in rows]

    def close(self) -> None:
        pass


class MemBackend:
    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.tables = tables
        self.meta = Meta()
        names = set(cat('tables')['DB_KEYS']) | set(tables)
        for n in names:
            rows = tables.get(n, [])
            pk = ['id'] if (not rows or 'id' in rows[0]) else [k for k in ('tenant_id', 'scope', 'user_id', 'key') if k in rows[0]]
            scoped = n != 'tenants' and (not rows or 'tenant_id' in rows[0])
            self.meta.tables[n] = TableMeta(name=n, pk=pk, tenant_scoped=scoped)

    def key_fn(self, table: str):  # type: ignore[no-untyped-def]
        pk = self.meta.tables[table].pk
        if len(pk) == 1:
            c = pk[0]
            return lambda r: r.get(c)
        return lambda r: tuple(r.get(c) for c in pk)

    def snapshot(self, tenant_id: str | None, global_: bool = False) -> _Snap:
        return _Snap(self, tenant_id or '')

    def foreign_users_by_email(self, emails: list[str], except_tenant: str | None) -> list[dict]:
        return []


class MemRuntime:
    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.backend = MemBackend(tables)
        self.cache = TenantCache()
        self.freeze_rows = True


def tenant_scope(rt: MemRuntime, tenant_id: str) -> Scope:
    return Scope(rt, 'tenant', tenant_id)


def make_ctx(s: Scope, user_id: str, perms: list[str], today: str, role: str | None = None) -> Ctx:
    """the ctx of a request by `user_id` with an explicit permission set and a pinned ctx.today"""
    user = s.find_by('users', 'id', user_id)
    assert user is not None, user_id
    tenant = s.find_by('tenants', 'id', user['tenant_id'])
    r = role or user.get('role')
    return Ctx(user, tenant, r, r, None, user.get('role_key'), set(perms), bool(user.get('is_super_admin')), today)


_ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z')


def jnorm(v: Any) -> Any:
    """the Python value as gen/_engine.mjs norm() records the TypeScript one: JSON.stringify semantics (UNDEF keys
    dropped, UNDEF array items null, integral floats as ints), ISO timestamps -> '<ts>', non-finite numbers ->
    '$num:NaN' / '$num:Infinity' / '$num:-Infinity', a top-level undefined -> {'$undef': True}"""
    if v is UNDEF:
        return {'$undef': True}
    return _jn(v)


def _jn(v: Any) -> Any:
    if isinstance(v, str):
        return _ISO.sub('<ts>', v)
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        if v != v:
            return '$num:NaN'
        if v in (INF, -INF):
            return '$num:Infinity' if v > 0 else '$num:-Infinity'
        return int(v) if v.is_integer() and abs(v) < 2**53 else v
    if isinstance(v, int):
        return v
    if isinstance(v, dict):
        return {k: _jn(x) for k, x in v.items() if x is not UNDEF and not callable(x)}
    if isinstance(v, (list, tuple)):
        return [None if x is UNDEF else _jn(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return {}
    return v


def jsafe(f: Any) -> dict:
    """gen/_engine.mjs safe(): {v} | {err: {status, code, message, field}} | {throws}"""
    try:
        return {'v': jnorm(f())}
    except ApiError as e:
        return {'err': {'status': e.status, 'code': e.code, 'message': e.message, 'field': None if e.field is UNDEF else e.field}}
    except Exception as e:  # noqa: BLE001
        return {'throws': f'{type(e).__name__}: {e}'}


def same(got: Any, want: Any) -> bool:
    """equality of normalised results; a TS exception matches any Python exception (messages differ by language)"""
    if isinstance(want, dict) and 'throws' in want:
        return isinstance(got, dict) and 'throws' in got
    return bool(got == want)


class DetIds:
    """deterministic ids like the generator's store (`<prefix>gid<n>`), reset per scenario"""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, prefix: str = '') -> str:
        self.n += 1
        return f'{prefix}gid{self.n}'
