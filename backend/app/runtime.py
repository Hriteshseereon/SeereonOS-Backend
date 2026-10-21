"""Request runtime — port of server/src/runtime.ts (HTTP-agnostic; app.main wires it to FastAPI).

execute(req): route match -> session (sha256 of the bearer token) -> Idempotency-Key replay -> unit of work
(Scope) -> dispatch (the mock's router core: ctx, platform / View-as / read-only / password-change checks, file
safety, handler) -> flush (one transaction, read-set validation) with the reference's retry semantics:
  * a lost write race re-runs the whole request; from the second attempt on, the request holds the tenant lease
    (pessimistic), so it cannot lose again; 409 CONFLICT_RETRY only after 3 conflicts;
  * GETs flush only when they wrote (write intent); non-GETs flush when they succeeded or wrote before failing (an
    error after writes keeps the writes, like the reference);
  * after a successful flush the cache is refilled from the request's own rows when the versions prove no interleaving.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import orjson

from app.auth.crypto import sha256
from app.db.backend import FlushPlan, IdempotencyRecord, PgBackend
from app.db.cache import TableEntry, TenantCache
from app.db.mapping import MappingError
from app.db.uow import Scope, _single_pk
from app.errors import ApiError, fail_body
from app.frozen import freeze
from app.js import UNDEF, VOLATILE, date_parse, iso_from_ms, js_number, prepare_json, today_iso
from app.memo import MemoEntry, ResponseMemo
from app.router import Route, Router

_log = logging.getLogger('seereonos')

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_LOCAL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+'-")
_EMAIL_DOMAIN = re.compile(r'[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


def _email_matches(v: str) -> Iterator[str]:
    """RE_EMAIL.finditer(v) without its quadratic backtracking on long runs of local-part characters (a 1 MB body of
    letters took minutes): a match starts at the first position (>= the previous match end) of the local-part run
    right before an '@', and exists iff the domain matches after that '@' — the same matches, in linear time."""
    pos = 0
    a = v.find('@')
    while a != -1:
        r = a
        while r > pos and v[r - 1] in _EMAIL_LOCAL:
            r -= 1
        if r < a:
            dm = _EMAIL_DOMAIN.match(v, a + 1)
            if dm is not None:
                yield v[r : dm.end()]
                pos = dm.end()
                a = v.find('@', pos)
                continue
        a = v.find('@', a + 1)


@dataclass
class ClientInfo:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str = ''


@dataclass
class ExecRequest:
    method: str
    path: str  # below /api/v1, without query string
    query: dict[str, Any]
    body_raw: bytes | None  # the JSON body as received (parsed fresh for every attempt = the reference's clone())
    body: Any  # parsed once (UNDEF when there is no body)
    headers: dict[str, str | None]  # Authorization, X-View-As, If-Match, Idempotency-Key (canonical names)
    client: ClientInfo = field(default_factory=ClientInfo)


@dataclass
class ExecResult:
    status: int
    body: bytes
    replayed: bool = False
    route: Route | None = None
    etag: str | None = None
    memo: Any = None


@dataclass
class Envelope:
    """a successful dispatch: the serialised `{data, warnings}` (taken at handler return = the reference's clone)"""

    body: bytes
    etag: str | None
    memo: Any = None  # the MemoEntry it was served from / stored as (its gzip copy is kept there)


class Runtime:
    def __init__(
        self,
        backend: PgBackend,
        router: Router,
        cache: TenantCache | None = None,
        freeze_rows: bool = True,
        idle_hours: int = 12,
        absolute_days: int = 30,
        max_conflicts: int = 3,
        log: Any = None,
        memo: ResponseMemo | None = None,
        lease_mode: str = 'adaptive',
        contention_window_s: float = 10.0,
    ) -> None:
        self.backend = backend
        self.router = router
        self.cache = cache
        self.freeze_rows = freeze_rows
        self.idle_hours = idle_hours
        self.absolute_days = absolute_days
        self.max_conflicts = max_conflicts
        self.log = log
        self.memo = memo
        # write requests of a tenant that just had a lost race take the tenant lease BEFORE their first attempt: under
        # contention every optimistic attempt would lose and run again anyway (same outcome, no wasted re-runs)
        self.lease_mode = lease_mode  # adaptive | optimistic | pessimistic
        self.contention_window_s = contention_window_s
        self._contended: dict[str, float] = {}
        self.stats = {'requests': 0, 'conflicts': 0, 'flushes': 0, 'leased_first': 0}
        self._lease_locks: dict[str, threading.Lock] = {}
        self._lease_guard = threading.Lock()
        self.local = threading.local()  # .client: ClientInfo of the request being served by this thread

    # ------------------------------------------------------------------ unit of work
    def _tenant_lock(self, tenant_id: str) -> threading.Lock:
        with self._lease_guard:
            lk = self._lease_locks.get(tenant_id)
            if lk is None:
                lk = threading.Lock()
                self._lease_locks[tenant_id] = lk
            return lk

    def run_scoped(
        self,
        mode: str,
        tenant_id: str | None,
        fn: Callable[[Scope], Any],
        *,
        is_write: bool,
        candidates: Any = (),
        emails: Any = (),
        probe: bool = True,
        idempotency: Callable[[Any], IdempotencyRecord | None] | None = None,
    ) -> Any:
        """Run fn inside a unit of work; flush; retry on a lost write race (see module doc)."""
        conflicts = 0
        lease = None
        local_lock: threading.Lock | None = None
        try:
            first_leased = False
            if is_write and mode == 'tenant' and tenant_id and self.lease_mode != 'optimistic':
                last = self._contended.get(tenant_id)
                first_leased = self.lease_mode == 'pessimistic' or (last is not None and time.monotonic() - last < self.contention_window_s)
            while True:
                if (conflicts > 0 or first_leased) and lease is None and mode == 'tenant' and tenant_id:
                    if conflicts == 0:
                        self.stats['leased_first'] += 1
                    # in-process queue first: at most one pooled connection per tenant and process waits on the PG lock
                    local_lock = self._tenant_lock(tenant_id)
                    local_lock.acquire()
                    try:
                        lease = self.backend.lock_tenant(tenant_id)
                    except BaseException:
                        local_lock.release()
                        local_lock = None
                        raise
                scope = Scope(self, mode, tenant_id, candidates, emails, probe, lease)
                value: Any = None
                error: BaseException | None = None
                with scope:
                    try:
                        value = fn(scope)
                    except ApiError as e:
                        error = e
                    scope.close_reads()
                    should_flush = mode != 'none' and (scope.write_intent or (is_write and error is None))
                    if should_flush:
                        outcome = self._flush(scope, lease, None if error is not None or idempotency is None else idempotency(value))
                        if outcome is not None and outcome.status == 'conflict':
                            self.stats['conflicts'] += 1
                            if scope.tenant_id:
                                self._contended[scope.tenant_id] = time.monotonic()
                            # (the cached entries stay: they are exact for their versions, and the next attempt
                            # refreshes them from the change log instead of reading whole tables again)
                            if lease is not None:
                                lease.release()
                                lease = None
                                if local_lock is not None:
                                    local_lock.release()
                                    local_lock = None
                            conflicts += 1
                            if conflicts >= self.max_conflicts:
                                raise ApiError(409, 'CONFLICT_RETRY', 'Too many people changed this at the same time. Try again.')
                            continue
                if error is not None:
                    raise error
                return value
        finally:
            if lease is not None:
                lease.release()
            if local_lock is not None:
                local_lock.release()

    def _flush(self, scope: Scope, lease: Any, idem: IdempotencyRecord | None) -> Any:
        tables, platform, tenant_id, read_set = scope.build_plan()
        if not tables and platform.empty() and idem is None:
            return None
        self.stats['flushes'] += 1
        client = getattr(self.local, 'client', None) or ClientInfo()
        plan = FlushPlan(
            mode=scope.mode if scope.mode != 'none' else 'tenant',
            tenant_id=tenant_id,
            read_set=read_set,
            tables=tables,
            platform=platform,
            client_ip=client.ip,
            user_agent=client.user_agent,
            session_absolute_days=self.absolute_days,
            idempotency=idem,
            lease=lease,
        )
        try:
            outcome = self.backend.flush(plan)
        except MappingError:
            raise
        except Exception as e:  # map database errors the shared code expects
            code = getattr(e, 'sqlstate', None)
            diag = getattr(e, 'diag', None)
            constraint = getattr(diag, 'constraint_name', None) or ''
            table = getattr(diag, 'table_name', None)
            if code == '23505' and 'email' in (constraint or str(e)).lower() and (table is None or table == 'users'):
                raise ApiError(409, 'USER_EMAIL_TAKEN', 'An account with this email already exists.', 'email') from e
            if code == '23505' and constraint == 'tenants_slug_uk':  # two sign-ups with the same company name at once
                raise ApiError(409, 'CONFLICT_RETRY', 'Another workspace with this name was created at the same moment. Try again.') from e
            if code == '22003':
                raise ApiError(422, 'VALIDATION', 'A number in this request is too large.') from e
            raise
        if outcome.status != 'ok' or self.cache is None:
            return outcome
        if scope.mode == 'tenant' and tenant_id:
            refilled: dict[str, tuple[Any, bool]] = {}  # table -> (new entry, every changed row read back unchanged)
            for ch in tables:
                t = ch.table
                before = outcome.versions_before.get(t, 0)
                after = outcome.versions_after.get(t, 0)
                n = outcome.statements.get(t, 0)
                cur = scope.current_rows(t)
                pend = scope.pending_rows(t)
                if pend is not None:
                    # appended without loading: the cached version it was appended to (if it is the one we wrote on)
                    # plus the new rows (in insert order = their _ord order)
                    base_entry = self.cache.get(tenant_id, t, before)
                    if base_entry is not None and after == before + n:
                        add = [self._normalize(t, r) for r in pend]
                        rows = list(base_entry.rows) + add
                        if self.freeze_rows:
                            rows = freeze(rows)
                        self.cache.set(tenant_id, t, TableEntry(after, rows, self.backend.key_fn(t), pk=_single_pk(self.backend, t), refill=True))
                    continue
                if cur is not None and read_set.get(t) == before and after == before + n:
                    st = scope.state(t)
                    ids = st.base_ids()
                    exact = True
                    swap: dict[int, dict] = {}  # id(row written) -> the row as read back, for rows that differ
                    rows = []
                    for r in cur:
                        if id(r) in ids:
                            rows.append(r)
                            continue
                        nr, same = self._normalize_x(t, r)
                        exact = exact and same
                        swap[id(r)] = nr
                        rows.append(nr)
                    if self.freeze_rows:
                        rows = freeze(rows)  # the list too (its rows are frozen already: no copies)
                    entry = TableEntry(after, rows, self.backend.key_fn(t), pk=_single_pk(self.backend, t), refill=True)
                    if exact:
                        _carry_indexes(st, entry, swap, self.freeze_rows)
                    self.cache.set(tenant_id, t, entry)
                    refilled[t] = (entry, exact)
                # else: someone else wrote the table in between — the cached entry stays exact for its version and is
                # brought up to date from the change log on the next read
            self._file_noted(scope, tenant_id, refilled)
        elif tenant_id:
            self.cache.invalidate(tenant_id, [c.table for c in tables])
        if scope.mode == 'global':
            for ch in tables:
                for r in [*ch.inserts, *ch.deletes, *(a for _, a in ch.updates)]:
                    self.cache.invalidate(str(r.get('id') if ch.table == 'tenants' else r.get('tenant_id')), [ch.table])
        return outcome

    def _file_noted(self, scope: Scope, tenant_id: str, refilled: dict[str, tuple[Any, bool]]) -> None:
        """derived values this request computed on its own (changed) rows: valid for the versions it just wrote when
        their inputs did not change after the computation and every row read back exactly as written"""
        for name, tbls, value, toks, nbytes in scope.noted_derived():
            entries = []
            for t, tok in zip(tbls, toks):
                if scope.view_token(t) is not tok:
                    break
                r = refilled.get(t)
                if r is not None:
                    if not r[1] or self.cache.get(tenant_id, t, r[0].version) is not r[0]:
                        break
                    entries.append(r[0])
                else:
                    e = scope.loaded_entry(t)
                    if e is None or scope.has_overlay(t) or self.cache.get(tenant_id, t, e.version) is not e:
                        break
                    entries.append(e)
            else:
                self.cache.derived_set(tenant_id, name, tbls, tuple(entries), freeze(value) if self.freeze_rows else value, nbytes)

    def _normalize_x(self, table: str, row: dict) -> tuple[dict, bool]:
        """(_normalize(row), whether it is exactly `row`: same keys in the same order, same types, same values)"""
        nr = self._normalize(table, row)
        return nr, _same(nr, row)

    def _normalize(self, table: str, row: dict) -> dict:
        """the row as it reads back from PostgreSQL (UNDEF keys dropped, JSON-normalised, timestamps as toISOString,
        integral floats as ints) — what a cache refill from the database would hold"""
        tm = self.backend.meta.tables[table]
        out = orjson.loads(orjson.dumps(prepare_json(row), option=orjson.OPT_NON_STR_KEYS))
        for k, v in out.items():
            c = tm.columns.get(k)
            if c is None:
                continue
            if c.kind == 'timestamp' and isinstance(v, str):
                ms = date_parse(v)
                if ms == ms:
                    iso = iso_from_ms(ms)
                    if iso != v:
                        out[k] = iso
            elif c.kind == 'float' and type(v) is float and v.is_integer():
                out[k] = int(v)
        return freeze(out) if self.freeze_rows else out

    # ------------------------------------------------------------------ the HTTP-agnostic entry point
    def execute(self, req: ExecRequest) -> ExecResult:
        self.local.client = req.client
        try:
            return self._execute(req)
        finally:
            self.local.client = None

    def _execute(self, req: ExecRequest) -> ExecResult:
        from app.dispatch import dispatch, resolver

        self.stats['requests'] += 1
        rid = req.client.request_id
        try:
            m = self.router.match(req.method, req.path)
            if m is None:
                return fail(ApiError(404, 'ROUTE_NOT_FOUND', f'No route for {req.method} {req.path}'), rid)
            route = m[0]
            raw = re.sub(r'^Bearer\s+', '', req.headers.get('Authorization') or '', flags=re.I).strip()
            session = self.backend.resolve_session(sha256(raw), self.idle_hours) if raw else None
            headers: dict[str, str] = {}
            if session:
                headers['Authorization'] = 'Bearer ' + session['token_hash']
            if req.headers.get('X-View-As'):
                headers['X-View-As'] = req.headers['X-View-As']  # type: ignore[assignment]
            im = parse_if_match(req.headers.get('If-Match'))
            if im is not None:
                headers['If-Match'] = im
            mode = 'none' if not session else ('global' if route.super_admin and session['is_super_admin'] else 'tenant')
            idem_key = (req.headers.get('Idempotency-Key') or '').strip() if req.method == 'POST' and session else ''
            if idem_key:
                if len(idem_key) > 200:
                    return fail(ApiError(422, 'VALIDATION', 'Idempotency-Key must be at most 200 characters.', 'Idempotency-Key'), rid)
                prev = self.backend.idempotency_get(session['tenant_id'], idem_key)  # type: ignore[index]
                if prev:
                    if prev['method'] != req.method or prev['path'] != req.path:
                        return fail(ApiError(422, 'IDEMPOTENCY_KEY_REUSED', 'This Idempotency-Key was already used for a different request.'), rid)
                    body = prev['body'].encode()
                    return ExecResult(prev['status'], body, replayed=True, route=route, etag=_etag_of(body) if prev['status'] == 200 else None)
            emails = [] if req.method == 'GET' else emails_in(req.body)
            memo_key = None
            if self.memo is not None and req.method == 'GET' and session and mode == 'tenant' and not route.public and not route.super_admin and not route.override:
                memo_key = ResponseMemo.key(
                    session['tenant_id'], route.pattern, req.path, req.query, session['user_id'], headers.get('X-View-As'), headers.get('If-Match')
                )

            def run(scope: Scope) -> Envelope:
                if memo_key is None:
                    return dispatch(self, m, req, headers, resolver(session))
                return self._memo_dispatch(scope, memo_key, m, req, headers, session)

            env: Envelope = self.run_scoped(
                mode,
                session['tenant_id'] if session else None,
                run,
                is_write=req.method != 'GET',
                candidates=password_candidates(req.body),
                emails=emails,
                idempotency=(lambda v: IdempotencyRecord(session['tenant_id'], idem_key, req.method, req.path, 200, v.body)) if idem_key else None,  # type: ignore[index]
            )
            return ExecResult(200, env.body, route=route, etag=env.etag, memo=env.memo)
        except ApiError as e:
            return fail(e, rid)


class _WatchedHeaders(dict):
    """request headers for a memoisable GET: a handler that reads the session token (Authorization) depends on WHICH
    session asks, which the memo key does not hold — mark it volatile"""

    def __init__(self, src: dict, flag: list) -> None:
        super().__init__(src)
        self._flag = flag

    def get(self, k: Any, d: Any = None) -> Any:
        if k == 'Authorization':
            self._flag[0] = True
        return super().get(k, d)

    def __getitem__(self, k: Any) -> Any:
        if k == 'Authorization':
            self._flag[0] = True
        return super().__getitem__(k)


def _memo_run(rt: Runtime, m: Any, req: ExecRequest, headers: dict[str, str], session: dict) -> tuple[Envelope, bool]:
    from app.dispatch import dispatch, resolver

    vol = [False]
    tok = VOLATILE.set(vol)
    try:
        env = dispatch(rt, m, req, _WatchedHeaders(headers, vol), resolver(session))
    finally:
        VOLATILE.reset(tok)
    return env, vol[0]


def _memo_dispatch(self: Runtime, scope: Scope, key: Any, m: Any, req: ExecRequest, headers: dict[str, str], session: dict) -> Envelope:
    memo = self.memo
    assert memo is not None
    today = today_iso()
    e = memo.get(key, today)
    if e is not None:
        if memo.valid(e, scope.snapshot_versions()):
            if not memo.verify:
                memo.hits += 1
                return Envelope(e.body, e.etag, e)
            fresh, _vol = _memo_run(self, m, req, headers, session)
            if fresh.body != e.body or fresh.etag != e.etag:
                memo.mismatches += 1
                _log.error('response memo mismatch', extra={'route': key[1], 'path': key[2]})
            else:
                memo.hits += 1
            return fresh
        memo.stale += 1
    # single flight: concurrent identical requests (a cold dashboard opened by many at once) compute it once
    flight = None
    if self.cache is not None:
        lead, ev = self.cache.flight(('memo', key))
        if lead:
            flight = ('memo', key)
        else:
            ev.wait(30)
            e = memo.get(key, today)
            if e is not None and memo.valid(e, scope.snapshot_versions()) and not memo.verify:
                memo.hits += 1
                return Envelope(e.body, e.etag, e)
    try:
        env, volatile = _memo_run(self, m, req, headers, session)
        if not volatile and today_iso() == today and scope.memoisable():
            entry = MemoEntry(env.body, env.etag, scope.read_set(), today, time.monotonic())
            memo.put(key, entry)
            env.memo = entry
        return env
    finally:
        if flight is not None:
            self.cache.land(flight)


Runtime._memo_dispatch = _memo_dispatch  # type: ignore[attr-defined]


def _same(a: Any, b: Any) -> bool:
    """deep equality that also requires the same types (8 vs 8.0 differ) and dict key order (frozen = plain)"""
    if isinstance(a, dict):
        if not isinstance(b, dict) or len(a) != len(b) or list(a) != list(b):
            return False
        return all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        if not isinstance(b, list) or len(a) != len(b):
            return False
        return all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _carry_indexes(st: Any, entry: TableEntry, swap: dict[int, dict], freeze_rows: bool) -> None:
    """The request's overlay indexes describe exactly the rows it wrote; give them to the new cache entry (its rows
    are the same objects, the written ones replaced by their read-back copies — equal, see _same) so the next
    request does not rebuild them over the whole table."""
    from app.frozen import FrozenList

    for (kind, fld), c in list(st.idx.items()):
        if not c.valid():
            continue
        d = dict(c.d)
        if kind == 'g':
            touched = set()
            for _rid, nr in swap.items():
                v = nr.get(fld)
                if v in touched:
                    continue
                lst = d.get(v)
                if lst is None:
                    break
                touched.add(v)
                d[v] = [swap.get(id(x), x) for x in lst]
            else:
                if freeze_rows:
                    d = {k: (v if isinstance(v, FrozenList) else FrozenList(v)) for k, v in d.items()}
                else:
                    d = {k: list(v) if type(v) is not list else v for k, v in d.items()}
                entry.carry_group(fld, d)
        else:
            for k, x in list(d.items()):
                y = swap.get(id(x))
                if y is not None:
                    d[k] = y
            entry.carry_index(fld, d)


def fail(e: ApiError, request_id: str) -> ExecResult:
    return ExecResult(e.status, fail_body(e, request_id))


def _etag_of(body: bytes) -> str | None:
    try:
        d = orjson.loads(body).get('data')
    except Exception:  # noqa: BLE001
        return None
    if isinstance(d, dict) and isinstance(d.get('version'), (int, float)) and not isinstance(d.get('version'), bool):
        from app.js import js_str

        return f'"{js_str(d["version"])}"'
    return None


def parse_if_match(v: str | None) -> str | None:
    """If-Match: "3", W/"3" or 3 -> "3" (the mock compares Number(header))"""
    if v is None or v == '':
        return None
    s = v.strip()
    if s.startswith('W/'):
        s = s[2:]
    m = re.fullmatch(r'"(.*)"', s, re.S)
    return m.group(1) if m else s


def if_match_number(headers: dict[str, str]) -> Any:
    v = headers.get('If-Match')
    return UNDEF if v is None else js_number(v)


def password_candidates(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    return [v for v in (body.get('password'), body.get('current')) if isinstance(v, str) and v]


def emails_in(body: Any, limit: int = 500) -> list[str]:
    """e-mail addresses anywhere in the body (bounded), for the shared code's cross-tenant uniqueness checks"""
    out: dict[str, None] = {}

    def walk(v: Any, depth: int) -> None:
        if len(out) >= limit or depth > 6:
            return
        if isinstance(v, str):
            if len(v) > 2_000_000 or v.startswith('data:'):
                return
            for m in _email_matches(v):
                out[m.lower()] = None
                if len(out) >= limit:
                    return
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x, depth + 1)

    walk(body, 0)
    return list(out)
