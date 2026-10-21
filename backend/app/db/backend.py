"""PostgreSQL side of the unit of work.

ONE pool, ONE role (seereon_app, DATABASE_URL); RLS applies to every tenant statement:
* tenant reads: REPEATABLE READ READ ONLY snapshot with app.tenant_id set;
* super-admin (global) reads and flushes: app.platform_admin = 'on' in the transaction (the RLS platform policy) —
  entered only for routes flagged super_admin AND a session whose user is a platform super admin (app.runtime);
* platform/auth state (credentials, sessions, reset tokens, login attempts, idempotency keys, the delivery queue)
  only through the SECURITY DEFINER functions of V008 — the role has no privileges on those tables;
* one flush = one transaction: app.tenant_id -> advisory lock on the tenant -> read-set version check -> batched
  DELETE / UPDATE / INSERT -> change log -> platform operations -> e-mail queue -> idempotency record -> COMMIT.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import orjson
import psycopg

from app.db.mapping import Meta, ident, load_meta, row_from_tuple, values_to_pg, write_columns
from app.db.pool import Pools

MAX_PARAMS = 30000


@dataclass
class TableChange:
    table: str
    inserts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)  # (before, after)
    deletes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlatformChange:
    credential_upserts: list[tuple[str, str]] = field(default_factory=list)  # (user_id, scrypt hash)
    credential_deletes: list[str] = field(default_factory=list)
    session_inserts: list[dict[str, Any]] = field(default_factory=list)  # {token_hash, user_id, tenant_id, created_at}
    session_revokes: list[str] = field(default_factory=list)
    reset_inserts: list[dict[str, Any]] = field(default_factory=list)  # {token_hash, user_id, expires_at(ms), used}
    reset_used: list[str] = field(default_factory=list)
    reset_deletes: list[str] = field(default_factory=list)
    attempt_failures: list[dict[str, Any]] = field(default_factory=list)  # {email, at(ms)}
    attempt_clears: list[str] = field(default_factory=list)
    # platform e-mail (password reset links, invitations): {id, tenant_id, to, subject, body (sealed), expires_at (ISO)}
    mails: list[dict[str, Any]] = field(default_factory=list)

    def empty(self) -> bool:
        return not (
            self.credential_upserts
            or self.credential_deletes
            or self.session_inserts
            or self.session_revokes
            or self.reset_inserts
            or self.reset_used
            or self.reset_deletes
            or self.attempt_failures
            or self.attempt_clears
            or self.mails
        )


@dataclass
class IdempotencyRecord:
    tenant_id: str
    key: str
    method: str
    path: str
    status: int
    body: bytes  # the serialised envelope


@dataclass
class FlushPlan:
    mode: str  # tenant | global | fresh
    tenant_id: str | None
    read_set: dict[str, int]
    tables: list[TableChange]
    platform: PlatformChange
    client_ip: str | None = None
    user_agent: str | None = None
    session_absolute_days: int = 30
    idempotency: IdempotencyRecord | None = None
    lease: Lease | None = None


@dataclass
class FlushOutcome:
    status: str  # ok | conflict
    tables: list[str] = field(default_factory=list)  # conflict: tables whose version moved
    versions_before: dict[str, int] = field(default_factory=dict)
    versions_after: dict[str, int] = field(default_factory=dict)
    statements: dict[str, int] = field(default_factory=dict)


class Lease:
    """exclusive per-tenant write lock (a connection with an open transaction holding pg_advisory_xact_lock)"""

    def __init__(self, tenant_id: str, conn: psycopg.Connection, pool: Any) -> None:
        self.tenant_id = tenant_id
        self.conn = conn
        self._pool = pool
        self.open = True

    def close_after_flush(self) -> None:
        """the flush committed on this connection: give it back"""
        if self.open:
            self.open = False
            self._pool.putconn(self.conn)

    def release(self) -> None:
        if not self.open:
            return
        self.open = False
        try:
            self.conn.execute('ROLLBACK')
        except Exception:  # noqa: BLE001
            pass
        self._pool.putconn(self.conn)


class Snapshot:
    """a read snapshot of one tenant (REPEATABLE READ READ ONLY, app.tenant_id) or of all tenants (super-admin:
    app.platform_admin): versions + lazily loaded tables, all from the same snapshot."""

    def __init__(self, backend: PgBackend, tenant_id: str | None, global_: bool = False) -> None:
        self.backend = backend
        self.tenant_id = tenant_id
        self.global_ = global_
        self.pool = backend.pools.app
        self.conn: psycopg.Connection | None = self.pool.getconn()
        try:
            if global_:
                c = self.conn
                with c.pipeline():
                    c.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    c.execute("SELECT set_config('app.platform_admin', 'on', true)")
                self.versions: dict[str, int] = {}
            else:
                # one network round trip (pipeline) for the transaction, the tenant setting and the version vector
                c = self.conn
                with c.pipeline():
                    c.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    c.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
                    cur = c.execute('SELECT table_name, version FROM tenant_table_versions WHERE tenant_id = %s', [tenant_id])
                self.versions = dict(cur.fetchall())
        except Exception:
            self.close()
            raise

    def load(self, table: str) -> list[dict[str, Any]]:
        if self.conn is None:
            raise RuntimeError('snapshot closed')
        tm = self.backend.meta.tables[table]
        if self.global_:
            sql = tm.select_sql + ' ORDER BY _ord'
            cur = self.conn.execute(sql)  # type: ignore[arg-type]
        else:
            sql = tm.select_sql + (' WHERE id = %s' if table == 'tenants' else ' WHERE tenant_id = %s') + ' ORDER BY _ord'
            cur = self.conn.execute(sql, [self.tenant_id])  # type: ignore[arg-type]
        m = tm.mapper
        return [m(t) for t in cur.fetchall()]

    def load_where(self, table: str, field: str, value: str) -> list[dict[str, Any]]:
        """the tenant's rows with `field` = value, in table order (an index-backed query; RLS applies)"""
        if self.conn is None:
            raise RuntimeError('snapshot closed')
        tm = self.backend.meta.tables[table]
        cur = self.conn.execute(tm.select_sql + f' WHERE tenant_id = %s AND {ident(field)} = %s ORDER BY _ord', [self.tenant_id, value])  # type: ignore[arg-type]
        m = tm.mapper
        return [m(t) for t in cur.fetchall()]

    def delta(self, table: str, old_rows: list[dict], old_keys: list[Any], old_version: int, version: int) -> list[dict] | None:
        """The rows of `table` at `version` from those at `old_version` and the change log (V007), in the order a full
        read returns them (ORDER BY _ord): updated rows keep their place, deleted ones go, inserted ones — and rows
        deleted then inserted again — follow in _ord order (new rows always get a higher _ord). None when the log has
        no gapless chain from old_version to version, or more than delta_max rows changed."""
        b = self.backend
        if self.conn is None or self.global_ or not b.change_log or b.delta_max <= 0 or version <= old_version:
            return None
        tm = b.meta.tables[table]
        if tm.pk != ['id'] or table == 'tenants':
            return None
        steps = self.conn.execute(
            'SELECT version_before, version_after, upserted_ids, deleted_ids FROM table_changes'
            ' WHERE tenant_id = %s AND table_name = %s AND version_after > %s AND version_after <= %s ORDER BY version_after',
            [self.tenant_id, table, old_version, version],
        ).fetchall()
        prev = old_version
        state: dict[str, bool] = {}  # id -> present at the end
        moved: set[str] = set()  # ids deleted at some step: if present at the end they were inserted again (new _ord)
        for vb, va, ups, dels in steps:
            if vb != prev:
                return None
            prev = va
            for i in dels:
                state[i] = False
                moved.add(i)
            for i in ups:
                state[i] = True
            if len(state) > b.delta_max:
                return None
        if prev != version:
            return None
        want = [i for i, present in state.items() if present]
        fetched: dict[Any, dict] = {}
        if want:
            m = tm.mapper
            cur = self.conn.execute(tm.select_sql + ' WHERE tenant_id = %s AND id = ANY(%s::text[]) ORDER BY _ord', [self.tenant_id, want])  # type: ignore[arg-type]
            for t in cur.fetchall():
                r = m(t)
                fetched[r.get('id')] = r
        out: list[dict] = []
        append = out.append
        for r, k in zip(old_rows, old_keys):
            if type(k) is not str:
                return None  # ids are text; anything else: read the table
            st = state.get(k)
            if st is None:
                append(r)
            elif st and k not in moved:
                nr = fetched.pop(k, None)
                if nr is None:
                    return None  # logged as present but not found: never expected — read the table
                append(nr)  # updated in place: same _ord
        out.extend(fetched.values())  # inserted (and re-inserted) rows, in _ord order
        return out

    def close(self) -> None:
        c = self.conn
        if c is None:
            return
        self.conn = None
        try:
            c.execute('ROLLBACK')
        except Exception:  # noqa: BLE001
            pass
        self.pool.putconn(c)


class PgBackend:
    def __init__(self, pools: Pools, queue_emails: bool = False, delta_max: int = 2000, queue_platform_mails: bool = True) -> None:
        self.pools = pools
        self.queue_emails = queue_emails  # email_outbox rows → delivery queue (EMAIL_DELIVERY=smtp)
        # sealed platform mails (reset links, invitations): queued unless production runs with EMAIL_DELIVERY=disabled —
        # then nothing would ever drain them (development keeps them for python -m app.devtools.mailbox)
        self.queue_platform_mails = queue_platform_mails
        self.meta: Meta = Meta()
        self._lock = threading.Lock()
        self.change_log = False  # table_changes exists (V007)
        self.delta_max = delta_max  # incremental refresh up to this many changed rows (0 = off)

    def init(self) -> None:
        with self.pools.app.connection() as c:
            self.meta = load_meta(c)
            self.change_log = c.execute("SELECT to_regclass('public.table_changes') IS NOT NULL").fetchone()[0]  # type: ignore[index]
        if not self.meta.tables:
            raise RuntimeError('app_table_meta is empty — run the migrations (python -m app.db.migrate).')

    # ---------------- metadata ----------------
    def pk(self, table: str) -> list[str]:
        return self.meta.tables[table].pk

    def single_pk(self, table: str) -> str | None:
        pk = self.meta.tables[table].pk
        return pk[0] if len(pk) == 1 else None

    def key_fn(self, table: str):  # type: ignore[no-untyped-def]
        pk = self.meta.tables[table].pk
        if len(pk) == 1:
            c = pk[0]
            return lambda r: r.get(c)
        return lambda r: tuple(r.get(c) for c in pk)

    # ---------------- reads ----------------
    def snapshot(self, tenant_id: str | None, global_: bool = False) -> Snapshot:
        return Snapshot(self, tenant_id, global_)

    def _query(self, sql: str, params: Any = None, platform: bool = False) -> list[tuple]:
        """one statement on a pooled connection; platform=True: inside a transaction in the platform scope"""
        with self.pools.app.connection() as c:
            if not platform:
                return c.execute(sql, params).fetchall()  # type: ignore[arg-type]
            with c.transaction():
                c.execute("SELECT set_config('app.platform_admin', 'on', true)")
                return c.execute(sql, params).fetchall()  # type: ignore[arg-type]

    def password_hashes(self, tenant_id: str | None, user_ids: list[str]) -> dict[str, str]:
        """{user_id: scrypt hash} for these users of the tenant (None: any tenant — platform scope)"""
        ids = [u for u in dict.fromkeys(user_ids) if isinstance(u, str)]
        if not ids:
            return {}
        rows = self._query('SELECT user_id, password_hash FROM seereon_password_hashes(%s, %s::text[])', [tenant_id, ids], platform=tenant_id is None)
        return {u: h for u, h in rows}

    def load_sessions(self, tenant_id: str | None, idle_hours: int) -> list[dict[str, Any]]:
        rows = self._query(
            'SELECT token_hash, user_id, tenant_id, created_at FROM seereon_sessions(%s, %s)', [tenant_id, idle_hours], platform=tenant_id is None
        )
        return [{'token': t, 'user_id': u, 'tenant_id': tn, 'created_at': c} for t, u, tn, c in rows]

    def load_reset_tokens(self, tenant_id: str | None) -> list[dict[str, Any]]:
        rows = self._query('SELECT token_hash, user_id, expires_ms, used FROM seereon_reset_tokens(%s)', [tenant_id], platform=tenant_id is None)
        return [{'token': t, 'user_id': u, 'expires_at': int(e), 'used': bool(used)} for t, u, e, used in rows]

    def load_attempts(self, emails: list[str]) -> list[dict[str, Any]]:
        if not emails:
            return []
        rows = self._query('SELECT email, at_ms FROM seereon_login_attempts(%s::text[])', [emails])
        return [{'email': e, 'at': int(at)} for e, at in rows]

    def foreign_users_by_email(self, emails: list[str], except_tenant: str | None) -> list[dict[str, Any]]:
        if not emails:
            return []
        tm = self.meta.tables['users']
        sql = tm.select_sql.rsplit(' FROM ', 1)[0] + ' FROM seereon_users_by_email(%s::text[], %s) AS "users" ORDER BY _ord'
        with self.pools.app.connection() as c:
            cur = c.execute(sql, [emails, except_tenant])  # type: ignore[arg-type]
            return [row_from_tuple(tm, t) for t in cur.fetchall()]

    def resolve_session(self, token_hash: str, idle_hours: int) -> dict[str, Any] | None:
        rows = self._query('SELECT token_hash, user_id, tenant_id, is_super_admin FROM seereon_resolve_session(%s, %s)', [token_hash, idle_hours])
        if not rows:
            return None
        h, u, t, sa = rows[0]
        return {'token_hash': h, 'user_id': u, 'tenant_id': t, 'is_super_admin': bool(sa)}

    def user_by_email(self, email: str) -> dict[str, str] | None:
        rows = self._query('SELECT id, tenant_id FROM seereon_user_by_email(%s)', [email])
        return {'id': rows[0][0], 'tenant_id': rows[0][1]} if rows else None

    def free_slug(self, base: str) -> str:
        """`base`, or base-2, base-3 … — unique across every tenant (sign-up cannot see other tenants)"""
        rows = self._query('SELECT seereon_free_slug(%s)', [base])
        return rows[0][0] if rows and rows[0][0] else base

    def invite_tenant(self, token: str) -> str | None:
        rows = self._query('SELECT seereon_invite_tenant(%s)', [token])
        return rows[0][0] if rows else None

    def reset_token_tenant(self, token_hash: str) -> str | None:
        rows = self._query('SELECT seereon_reset_token_tenant(%s)', [token_hash])
        return rows[0][0] if rows else None

    def payment_tenant(self, provider: str, provider_order_id: str) -> str | None:
        rows = self._query('SELECT seereon_payment_tenant(%s, %s)', [provider, provider_order_id])
        return rows[0][0] if rows else None

    def idempotency_get(self, tenant_id: str, key: str) -> dict[str, Any] | None:
        rows = self._query('SELECT method, path, status, body FROM seereon_idempotency_get(%s, %s)', [tenant_id, key])
        if not rows:
            return None
        m, p, s, b = rows[0]
        return {'method': m, 'path': p, 'status': int(s), 'body': b}

    def idempotency_put(self, rec: IdempotencyRecord, conn: psycopg.Connection) -> None:
        """inside the flush transaction (the tenant of the transaction must be the record's)"""
        conn.execute(
            'SELECT seereon_idempotency_put(%s, %s, %s, %s, %s, %s::jsonb)', [rec.tenant_id, rec.key, rec.method, rec.path, rec.status, rec.body.decode()]
        )

    def ping(self) -> None:
        self.pools.ping()

    # ---------------- locking ----------------
    def lock_tenant(self, tenant_id: str) -> Lease:
        pool = self.pools.app
        c = pool.getconn()
        try:
            c.execute('BEGIN')
            c.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
            c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [tenant_id])
        except Exception:
            try:
                c.execute('ROLLBACK')
            except Exception:  # noqa: BLE001
                pass
            pool.putconn(c)
            raise
        return Lease(tenant_id, c, pool)

    # ---------------- flush ----------------
    def _versions_on(self, c: psycopg.Connection, tid: str) -> dict[str, int]:
        return dict(c.execute('SELECT table_name, version FROM tenant_table_versions WHERE tenant_id = %s', [tid]).fetchall())

    def flush(self, plan: FlushPlan) -> FlushOutcome:
        lease = plan.lease if plan.lease is not None and plan.lease.open and plan.lease.tenant_id == plan.tenant_id else None
        pool = self.pools.app
        c = lease.conn if lease else pool.getconn()
        committed = False
        try:
            if not lease:
                c.execute('BEGIN')
            tid = plan.tenant_id
            before: dict[str, int] = {}
            if lease:
                before = self._versions_on(c, tid)  # type: ignore[arg-type]
            elif plan.mode == 'global':
                c.execute("SELECT set_config('app.platform_admin', 'on', true)")
                tenants = sorted(
                    {
                        str(r['id'] if ch.table == 'tenants' else r['tenant_id'])
                        for ch in plan.tables
                        for r in [*ch.inserts, *ch.deletes, *(a for _, a in ch.updates)]
                    }
                )
                for t in tenants:
                    c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [t])
            elif tid:
                c.execute("SELECT set_config('app.tenant_id', %s, true)", [tid])
                c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [tid])
                before = self._versions_on(c, tid)
            if plan.mode != 'global' and tid:
                conflicts = [t for t, v in plan.read_set.items() if before.get(t, 0) != v]
                if conflicts:
                    c.execute('ROLLBACK')
                    return FlushOutcome('conflict', tables=conflicts)
            statements: dict[str, int] = {}
            if plan.tables:
                for ch in plan.tables:
                    self._apply_deletes(c, ch, statements)
                for ch in plan.tables:
                    self._apply_updates(c, ch, statements)
                for ch in plan.tables:
                    self._apply_inserts(c, ch, statements)
            after = self._versions_on(c, tid) if tid and plan.mode != 'global' else {}
            if tid and plan.mode != 'global' and plan.tables and self.change_log:
                self._log_changes(c, tid, plan, before, after)
            self._apply_platform(c, plan)
            if self.queue_emails:
                mails = next((ch.inserts for ch in plan.tables if ch.table == 'email_outbox'), [])
                if mails:
                    c.execute(
                        'SELECT seereon_email_enqueue_outbox(%s::text[], %s::text[])',
                        [[str(m['id']) for m in mails], [str(m['tenant_id']) for m in mails]],
                    )
            if plan.idempotency is not None:
                self.idempotency_put(plan.idempotency, c)
            c.execute('COMMIT')
            committed = True
            return FlushOutcome('ok', versions_before=before, versions_after=after, statements=statements)
        except BaseException:
            try:
                c.execute('ROLLBACK')
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            if lease:
                lease.close_after_flush()
            else:
                pool.putconn(c)
            _ = committed

    def _log_changes(self, c: psycopg.Connection, tid: str, plan: FlushPlan, before: dict[str, int], after: dict[str, int]) -> None:
        """table_changes rows (V007): per changed table keyed by `id`, the version step of this flush and the ids"""
        rows = []
        for ch in plan.tables:
            t = ch.table
            if self.meta.tables[t].pk != ['id'] or t == 'tenants':
                continue
            vb, va = before.get(t, 0), after.get(t, 0)
            if va <= vb:
                continue
            ups = [str(r['id']) for r in ch.inserts] + [str(a['id']) for _, a in ch.updates]
            dels = [str(r['id']) for r in ch.deletes]
            rows.append((tid, t, vb, va, ups, dels))
        if rows:
            with c.cursor() as cur:
                cur.executemany(
                    'INSERT INTO table_changes (tenant_id, table_name, version_before, version_after, upserted_ids, deleted_ids) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                    rows,
                )

    def _count(self, st: dict[str, int], table: str, rowcount: int) -> None:
        if rowcount and rowcount > 0:
            st[table] = st.get(table, 0) + 1

    def _apply_deletes(self, c: psycopg.Connection, ch: TableChange, st: dict[str, int]) -> None:
        if not ch.deletes:
            return
        pk = self.meta.tables[ch.table].pk
        for i in range(0, len(ch.deletes), 5000):
            chunk = ch.deletes[i : i + 5000]
            if len(pk) == 1:
                cur = c.execute(f'DELETE FROM {ident(ch.table)} WHERE {ident(pk[0])} = ANY(%s::text[])', [[str(x[pk[0]]) for x in chunk]])  # type: ignore[arg-type]
            else:
                cols = ', '.join(ident(p) for p in pk)
                arrays = [[str(x[p]) for x in chunk] for p in pk]
                cur = c.execute(f'DELETE FROM {ident(ch.table)} WHERE ({cols}) IN (SELECT * FROM unnest({", ".join(["%s::text[]"] * len(pk))}))', arrays)  # type: ignore[arg-type]
            self._count(st, ch.table, cur.rowcount)

    def _apply_updates(self, c: psycopg.Connection, ch: TableChange, st: dict[str, int]) -> None:
        tm = self.meta.tables[ch.table]
        pk = tm.pk
        for before, after in ch.updates:
            b = values_to_pg(ch.table, before, self.meta)
            a = values_to_pg(ch.table, after, self.meta)
            changed = [k for k in a if k != '_null_override' and a[k] != b[k]]
            null_changed = a['_null_override'] != b['_null_override']
            if not changed and not null_changed:
                continue
            cols = [*changed, '_null_override']
            values = [a[k] for k in cols] + [str(before[p]) for p in pk]
            sets = ', '.join(f'{ident(k)} = %s' for k in cols)
            where = ' AND '.join(f'{ident(p)} = %s' for p in pk)
            cur = c.execute(f'UPDATE {ident(ch.table)} SET {sets} WHERE {where}', values)  # type: ignore[arg-type]
            self._count(st, ch.table, cur.rowcount)

    def _apply_inserts(self, c: psycopg.Connection, ch: TableChange, st: dict[str, int]) -> None:
        if not ch.inserts:
            return
        cols = write_columns(ch.table, self.meta)
        per = max(1, MAX_PARAMS // len(cols))
        head = f'INSERT INTO {ident(ch.table)} ({", ".join(ident(x) for x in cols)}) VALUES '
        tup = '(' + ', '.join(['%s'] * len(cols)) + ')'
        for i in range(0, len(ch.inserts), per):
            chunk = ch.inserts[i : i + per]
            values: list[Any] = []
            for row in chunk:
                v = values_to_pg(ch.table, row, self.meta)
                values.extend(v[x] for x in cols)
            cur = c.execute(head + ', '.join([tup] * len(chunk)), values)  # type: ignore[arg-type]
            self._count(st, ch.table, cur.rowcount)

    def _apply_platform(self, c: psycopg.Connection, plan: FlushPlan) -> None:
        """the platform operations of the request, through the V008 functions (same transaction as the tenant rows: the
        functions check that every user belongs to the transaction's tenant)"""
        p = plan.platform
        ip = plan.client_ip
        ua = plan.user_agent[:400] if plan.user_agent else None
        for uid, h in p.credential_upserts:
            c.execute('SELECT seereon_set_password(%s, %s)', [uid, h])
        if p.credential_deletes:
            c.execute('SELECT seereon_delete_passwords(%s::text[])', [p.credential_deletes])
        for s in p.session_inserts:
            c.execute(
                'SELECT seereon_create_session(%s, %s, %s, %s::timestamptz, %s, %s, %s)',
                [s['token_hash'], s['user_id'], s['tenant_id'], s['created_at'], plan.session_absolute_days, ip, ua],
            )
        if p.session_revokes:
            c.execute('SELECT seereon_revoke_sessions(%s::text[])', [p.session_revokes])
        for r in p.reset_inserts:
            c.execute('SELECT seereon_create_reset_token(%s, %s, %s, %s)', [r['token_hash'], r['user_id'], int(r['expires_at']), bool(r['used'])])
        if p.reset_used:
            c.execute('SELECT seereon_use_reset_tokens(%s::text[])', [p.reset_used])
        if p.reset_deletes:
            c.execute('SELECT seereon_delete_reset_tokens(%s::text[])', [p.reset_deletes])
        for a in p.attempt_failures:
            c.execute('SELECT seereon_record_login_attempt(%s, %s, %s, false)', [a['email'], int(a['at']), ip])
        for e in p.attempt_clears:
            c.execute('SELECT seereon_record_login_attempt(%s, NULL, %s, true)', [e, ip])
        for m in p.mails if self.queue_platform_mails else []:
            c.execute(
                'SELECT seereon_email_enqueue_platform(%s, %s, %s, %s, %s, %s::timestamptz)',
                [m['id'], m['tenant_id'], m['to'], m['subject'], m['body'], m.get('expires_at')],
            )


def dumps(v: Any) -> bytes:
    return orjson.dumps(v)
