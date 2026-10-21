"""Request scope = unit of work + identity map over the mock's DB shape (Pythonic port of server/src/uow/scope.ts).

Design (see PORTING.md for the rules route code follows):
  * Tables load lazily on first access, from ONE read snapshot per request (tenant: REPEATABLE READ READ ONLY with
    app.tenant_id set; global/super-admin: app.platform_admin set). Tenant tables come from the per-process cache
    when their version matches the snapshot's tenant_table_versions.
  * Cached rows are READ-ONLY and shared (deep-frozen in development/tests). Route code never changes a row in place:
    insert / patch / update / remove / remove_where / set_rows / next_number copy on write into the request OVERLAY
    and mark the scope dirty. Without an overlay a table view is the cached list itself (zero-copy GETs).
  * Rows created or copied by this request (returned by insert/patch) are private to the request and may be changed
    directly until the response is built (they are diffed at flush like everything in the overlay).
  * Flush (app.runtime): the overlay is diffed against the snapshot BY IDENTITY (unchanged rows are the cached objects)
    and by value for copied rows, then written in one transaction with read-set version validation.

Virtual platform tables keep the mock's shapes: `sessions` ({token (= SHA-256 of the bearer token for stored
sessions), user_id, tenant_id, created_at}), `reset_tokens` ({token (hash), user_id, expires_at (epoch ms), used}),
`login_attempts` ({email, at (epoch ms)}); passwords are only reachable through password_matches / set_password /
delete_password (one user at a time: the hashes of the users the request checks, never the tenant's whole list).
Platform e-mail (reset links, invitations) is queued with queue_platform_mail and written in the flush transaction.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
from typing import Any

from app.catalog import DB_KEYS
from app.frozen import FrozenList, freeze

VIRTUAL_TABLES = frozenset({'passwords', 'sessions', 'reset_tokens', 'login_attempts'})
MISSING: Any = object()  # Scope.shared_peek: no shared value
_DB_KEY_SET = frozenset(DB_KEYS)
_NO_TENANT_FK = frozenset({'users', 'admin_audit'})  # tenant tables without the tenant_id -> tenants(id) FK (V003)

_current: contextvars.ContextVar[Scope | None] = contextvars.ContextVar('seereon_scope', default=None)


def current_scope() -> Scope:
    s = _current.get()
    if s is None:
        raise RuntimeError('database access outside a request scope')
    return s


def maybe_scope() -> Scope | None:
    return _current.get()


def _single_pk(backend: Any, t: str) -> str | None:
    f = getattr(backend, 'single_pk', None)
    return f(t) if f is not None else None


def _diff_ops(old: list, new: list, limit: int = 2000) -> list[tuple] | None:
    """row operations turning `old` into `new` (by identity): one batch removal ('D', rows), then appends — e.g. a
    filtered copy is just a removal; None when more than `limit` rows would be involved"""
    removed: list = []
    i = j = 0
    n_old, n_new = len(old), len(new)
    while i < n_old and j < n_new:
        if old[i] is new[j]:
            i += 1
            j += 1
            continue
        removed.append(old[i])
        i += 1
        if len(removed) > limit:
            return None
    if (n_old - i) + (n_new - j) + len(removed) > limit:
        return None
    removed.extend(old[i:])
    ops: list[tuple] = [('D', removed)] if removed else []
    ops.extend(('a', x) for x in new[j:])
    return ops


class UnknownTable(KeyError):
    pass


_DROP = object()


class _OvIndex:
    """An index over a table OVERLAY ('g': value -> rows in table order, like a rebuild with setdefault/append;
    'i': value -> first row), kept identical to a full rebuild while the overlay changes:
      * append(row)              -> appended to its value's list / first row of a new value (both at the end, as a rebuild)
      * replace(old, new), same value -> swapped in place;  value changed -> dropped (rebuilt on next use)
      * remove(row)              -> removed from its list (its key deleted when it was the only row); dropped when it
                                   was the first of several (the key's position in a rebuild could move)
      * set_rows                 -> dropped
    Rows this request owns may also be changed IN PLACE (edit / insert): every lookup re-checks their recorded values
    and rebuilds when one changed. Lists shared with the cache entry (seeding) are copied before they change."""

    __slots__ = ('kind', 'field', 'd', 'mine', 'owned', 'handed')

    def __init__(self, kind: str, field: str, d: dict, mine: set | None) -> None:
        self.kind = kind
        self.field = field
        self.d = d
        self.mine = mine  # keys whose lists this index owns (None = all: built here)
        self.owned: dict[int, tuple[dict, Any]] = {}  # id(row) -> (row, value it is filed under) for owned rows
        self.handed = False  # `d` was returned to a caller: it (and its lists) are a snapshot from then on

    def hand_out(self) -> dict:
        self.handed = True
        return self.d

    def _list(self, v: Any) -> list:
        lst = self.d.get(v)
        if self.mine is not None and v not in self.mine:
            lst = list(lst) if lst is not None else []
            self.d[v] = lst
            self.mine.add(v)
        elif lst is None:
            lst = []
            self.d[v] = lst
        return lst

    def value_of(self, row: dict) -> Any:
        o = self.owned.get(id(row))
        return o[1] if o is not None and o[0] is row else row.get(self.field)

    def apply(self, op: tuple, owned_ids: dict) -> bool:
        """apply one row operation; False = this index can no longer be kept exact (drop it)"""
        f = self.field
        if self.handed:  # callers keep what they were given (as with a rebuild): change a copy
            self.d = dict(self.d)
            self.mine = set()
            self.handed = False
        try:
            if op[0] == 'a':
                row = op[1]
                v = row.get(f)
                if self.kind == 'g':
                    self._list(v).append(row)
                elif v not in self.d:
                    self.d[v] = row
                if id(row) in owned_ids:
                    self.owned[id(row)] = (row, v)
                return True
            if op[0] == 'r':
                old, new = op[1], op[2]
                v = self.value_of(old)
                if new.get(f) != v or (v != v):
                    return False
                if self.kind == 'g':
                    lst = self._list(v)
                    for i, x in enumerate(lst):
                        if x is old:
                            lst[i] = new
                            break
                    else:
                        return False
                elif self.d.get(v) is old:
                    self.d[v] = new
                self.owned.pop(id(old), None)
                if id(new) in owned_ids:
                    self.owned[id(new)] = (new, v)
                return True
            if op[0] == 'D':  # several rows removed at once: a key is gone when ALL its rows went
                by_v: dict[Any, set[int]] = {}
                for row in op[1]:
                    by_v.setdefault(self.value_of(row), set()).add(id(row))
                for v, ids in by_v.items():
                    cur = self.d.get(v)
                    if cur is None:
                        return False
                    if self.kind == 'i':
                        if id(cur) in ids:
                            return False  # the first row of the value went: the next one is unknown here
                        continue
                    n_rm = sum(1 for x in cur if id(x) in ids)
                    if n_rm != len(ids):
                        return False
                    if n_rm == len(cur):
                        del self.d[v]
                    elif id(cur[0]) in ids:
                        return False  # the key's first row moves: its place among the keys could change
                    else:
                        lst = self._list(v)
                        lst[:] = [x for x in lst if id(x) not in ids]
                for row in op[1]:
                    self.owned.pop(id(row), None)
                return True
            if op[0] == 'd':
                row = op[1]
                v = self.value_of(row)
                if self.kind == 'g':
                    cur = self.d.get(v)
                    if not cur:
                        return False
                    if cur[0] is row:
                        if len(cur) > 1:
                            return False
                        del self.d[v]
                    else:
                        lst = self._list(v)
                        for i, x in enumerate(lst):
                            if x is row:
                                del lst[i]
                                break
                        else:
                            return False
                elif self.d.get(v) is row:
                    return False
                self.owned.pop(id(row), None)
                return True
        except TypeError:  # an unhashable value
            return False
        return False

    def valid(self) -> bool:
        """owned rows still carry the values they are filed under"""
        f = self.field
        for row, v in self.owned.values():
            if row.get(f) != v:
                return False
        return True


class TableState:
    __slots__ = ('name', 'base', '_base_index', 'entry', 'version', 'rows', 'view_cache', 'dirty', 'idx', 'stub_count', '_base_ids', 'ops')

    def __init__(self, name: str, base: list, base_index: dict | None, entry: Any, version: int) -> None:
        self.name = name
        self.base = base  # rows as loaded (shared, read-only)
        self._base_index = base_index  # pk -> loaded row (identity = unchanged); from the entry on first use
        self.entry = entry  # TableEntry when cached (index reuse), else None
        self.version = version
        self.rows: list | None = None  # overlay (private list) — None while unchanged
        self.view_cache: list | None = None  # what view() hands out while the overlay is unchanged
        self.dirty = False
        self.idx: dict[tuple[str, str], _OvIndex] = {}  # per-request indexes over the overlay (kept up to date, see _OvIndex)
        self.stub_count = 0
        self.ops: list[tuple] | None = None  # row operations since the overlay was made from `base` (None after set_rows)
        self._base_ids: set[int] | None = None

    @property
    def base_index(self) -> dict:
        bi = self._base_index
        if bi is None:
            bi = self._base_index = self.entry.pk_index()
        return bi

    def base_ids(self) -> set[int]:
        """id() of every loaded row (kept alive by `base`): `id(r) in base_ids()` = r is an unchanged loaded row"""
        ids = self._base_ids
        if ids is None:
            ids = self._base_ids = self.entry.row_ids() if self.entry is not None else {id(r) for r in self.base}
        return ids


class Scope:
    """One attempt of one request. Mode: tenant | global | fresh | none."""

    def __init__(
        self,
        rt: Any,
        mode: str,
        tenant_id: str | None,
        candidates: Iterable[str] = (),
        emails: Iterable[str] = (),
        probe: bool = True,
        lease: Any = None,
    ) -> None:
        self.rt = rt
        self.backend = rt.backend
        self.cache = rt.cache
        self.freeze = rt.freeze_rows
        self.mode = mode
        self.tenant_id = tenant_id
        self.candidates = [c for c in dict.fromkeys(candidates) if isinstance(c, str) and 0 < len(c) <= 1024]
        self.emails = list(dict.fromkeys(e.lower() for e in emails))[:500]
        self.probe = probe
        self.lease = lease
        self.write_intent = False
        self._tables: dict[str, TableState] = {}
        self._snapshot: Any = None
        self._stubs: list[dict] | None = None
        self._stub_ids: set[int] = set()
        self._virt: dict[str, dict[str, Any]] = {}
        self._pw_known: dict[tuple[str, str], bool] = {}
        self._pw_writes: dict[str, str | None] = {}  # user_id -> new plaintext (None = deleted)
        self._pw_hashes: dict[str, str | None] = {}  # user_id -> stored hash (loaded per user on first use)
        self._mails: list[dict[str, Any]] = []  # platform e-mail queued by this request (flushed with it)
        self.memo: dict[Any, Any] = {}  # per-request memo for route helpers (the TS WeakMap<Ctx> memos)
        self._owned: dict[int, dict] = {}  # rows created / copied by this request (private, mutable), by id (kept alive)
        self._token: contextvars.Token | None = None
        self._track: list[set[str]] | None = None  # read tracking (shared derived values, response memo): tables touched
        # rows appended to tables this request has not read (tenant mode): the table is not loaded for an append, its
        # version is not part of the read set (the request's outcome does not depend on its contents); a later read
        # loads it and merges these rows at its end, exactly where an eager append would have put them
        self._pending: dict[str, list[dict]] = {}
        # tables read only through an index-backed query (rows_by_field): table -> version of the snapshot
        self._partial: dict[str, int] = {}
        # derived values computed on this request's own changed rows, offered to the cache after the flush
        self._noted: list[tuple[Any, tuple[str, ...], Any, tuple[Any, ...], int]] = []
        self.volatile = False  # the handler read the wall clock / generated randomness (app.js marks it): not memoisable

    # ------------------------------------------------------------------ context
    def __enter__(self) -> Scope:
        self._token = _current.set(self)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close_reads()
        if self._token is not None:
            _current.reset(self._token)
            self._token = None

    def close_reads(self) -> None:
        if self._snapshot is not None:
            self._snapshot.close()
            self._snapshot = None

    def persist(self) -> None:
        """write intent (the TS persist()): the request changed something that must be saved"""
        self.write_intent = True

    # ------------------------------------------------------------------ loading
    def _snap(self) -> Any:
        if self._snapshot is None:
            if self.mode == 'tenant':
                self._snapshot = self.backend.snapshot(self.tenant_id)
            elif self.mode == 'global':
                self._snapshot = self.backend.snapshot(None, global_=True)
            else:
                raise RuntimeError(f'no snapshot in {self.mode} mode')
        return self._snapshot

    def _load(self, t: str) -> TableState:
        if t not in self.backend.meta.tables:
            raise UnknownTable(t)
        key = self.backend.key_fn(t)
        if self.mode == 'tenant':
            snap = self._snap()
            v = snap.versions.get(t, 0)
            entry = self.cache.get(self.tenant_id, t, v) if self.cache is not None else None
            flight = None
            if entry is None and self.cache is not None and self.cache.max_tenants > 0:
                # another thread of this process is reading the same table version: wait for it, then use the cache
                fk = ('load', self.tenant_id, t, v)
                lead, ev = self.cache.flight(fk)
                if not lead:
                    ev.wait(30)
                    entry = self.cache.get(self.tenant_id, t, v)
                    if entry is None:
                        lead, ev = self.cache.flight(fk)
                if lead:
                    flight = fk
            if entry is None:
                try:
                    entry = self._read_table(snap, t, v, key)
                finally:
                    if flight is not None:
                        self.cache.land(flight)  # type: ignore[union-attr]
            st = TableState(t, entry.rows, None, entry, v)
        elif self.mode == 'global':
            rows = self._snap().load(t)
            if self.freeze:
                rows = freeze(rows)
            st = TableState(t, rows, {key(r): r for r in reversed(rows)}, None, 0)
        elif self.mode == 'fresh':
            empty: list = FrozenList() if self.freeze else []
            st = TableState(t, empty, {}, None, 0)
        else:
            raise RuntimeError(f'table "{t}" accessed without a session (public route without its own scope)')
        if t == 'users' and self.mode in ('tenant', 'fresh') and self.probe and self.emails:
            stubs = self._load_stubs()
            if stubs:
                st.rows = list(st.base) + stubs
                st.stub_count = len(stubs)
        self._tables[t] = st
        pend = self._pending.pop(t, None)
        if pend:
            if st.rows is None:
                st.rows = list(st.base)
                st.ops = []
            for r in pend:
                st.rows.append(r)
                if st.ops is not None:
                    st.ops.append(('a', r))
            st.dirty = True
        return st

    def _read_table(self, snap: Any, t: str, v: int, key: Any) -> Any:
        """the table at version v: from the cached older version + the change log, else read whole; cached"""
        from app.db.cache import TableEntry

        entry = None
        old = self.cache.peek(self.tenant_id, t) if self.cache is not None else None
        if old is not None and old.version < v:
            # another process changed the table: apply its logged changes (unchanged rows stay the same objects, like
            # after this process's own write) instead of reading the whole table again
            rows = snap.delta(t, old.rows, old.keys, old.version, v)
            if rows is not None:
                if self.freeze:
                    rows = freeze(rows)
                entry = TableEntry(v, rows, key, pk=_single_pk(self.backend, t), refill=True)
        if entry is None:
            rows = snap.load(t)
            if self.freeze:
                rows = freeze(rows)
            entry = TableEntry(v, rows, key, pk=_single_pk(self.backend, t))
        if self.cache is not None:
            self.cache.set(self.tenant_id, t, entry)
        return entry

    def _load_stubs(self) -> list[dict]:
        if self._stubs is None:
            rows = self.backend.foreign_users_by_email(self.emails, self.tenant_id)
            self._stubs = [freeze(r) if self.freeze else r for r in rows]
            self._stub_ids = {id(r) for r in self._stubs}
        return self._stubs

    def state(self, t: str) -> TableState:
        st = self._tables.get(t)
        if st is None:
            st = self._load(t)
        return st

    def accessed(self) -> list[str]:
        return list(self._tables.keys())

    def is_stub(self, row: Any) -> bool:
        return id(row) in self._stub_ids

    # ------------------------------------------------------------------ reads
    def _touch(self, t: str) -> None:
        for s in self._track:  # type: ignore[union-attr]
            s.add(t)

    def track_begin(self) -> set[str]:
        """start recording the tables read (nested recorders all see every read)"""
        rec: set[str] = set()
        if self._track is None:
            self._track = []
        self._track.append(rec)
        return rec

    def track_end(self, rec: set[str]) -> None:
        if self._track:
            self._track.remove(rec)
            if not self._track:
                self._track = None

    def view(self, t: str) -> list:
        """getDb()[t]: the current rows of the table (read-only list — never mutate it)"""
        if self._track is not None:
            self._touch(t)
        if t in VIRTUAL_TABLES:
            return self.virtual_view(t)
        st = self._tables.get(t) or self._load(t)
        if st.rows is None:
            return st.base
        if self.freeze:
            if st.view_cache is None:
                st.view_cache = FrozenList(st.rows)
            return st.view_cache
        st.view_cache = st.rows  # handed out: the next mutation copies the list first (copy on write)
        return st.rows

    def tenant_rows(self, t: str, tenant_id: str) -> list:
        """rows(ctx, t): rows of the tenant (no foreign-user stubs)"""
        st = self._tables.get(t) or self._load(t)
        lst = self.view(t)
        if self.mode == 'tenant' and t != 'tenants' and not st.stub_count and tenant_id == self.tenant_id:
            return lst
        if t == 'tenants':
            return [r for r in lst if r.get('id') == tenant_id]
        return [r for r in lst if r.get('tenant_id') == tenant_id and not (st.stub_count and id(r) in self._stub_ids)]

    def find_by(self, t: str, field: str, value: Any) -> dict | None:
        """first row of the table view with row[field] == value (Array.find), via a cached index"""
        if self._track is not None:
            self._touch(t)
        st = self._tables.get(t) or self._load(t)
        if st.rows is None and st.entry is not None:
            r = st.entry.index(field).get(value)
            return r if (r is not None and r.get(field) == value) else None
        return self._overlay_index(st, 'i', field).get(value)

    def index(self, t: str, field: str) -> dict[Any, dict]:
        """value -> FIRST row of the current view with it (read-only; rebuilt after this request changes the table)"""
        if self._track is not None:
            self._touch(t)
        st = self._tables.get(t) or self._load(t)
        if st.rows is None and st.entry is not None:
            return st.entry.index(field)
        return self._overlay_index(st, 'i', field)

    def group(self, t: str, field: str) -> dict[Any, list]:
        """value -> rows with it (table order)"""
        if self._track is not None:
            self._touch(t)
        st = self._tables.get(t) or self._load(t)
        if st.rows is None and st.entry is not None:
            return st.entry.group(field)
        return self._overlay_index(st, 'g', field)

    def _overlay_index(self, st: TableState, kind: str, field: str) -> dict:
        """index / group of the current view of a table that has an overlay (or no cache entry)"""
        c = st.idx.get((kind, field))
        if c is not None and c.valid():
            return c.hand_out()
        c = None
        if st.entry is not None and st.ops is not None and st.rows is not None:
            # seed from the cache entry's index of the loaded rows and replay this request's row operations
            base = st.entry.group(field) if kind == 'g' else st.entry.index(field)
            c = _OvIndex(kind, field, dict(base), set())
            for op in st.ops:
                if not c.apply(op, self._owned):
                    c = None
                    break
        if c is None:
            d: dict = {}
            lst = st.base if st.rows is None else st.rows
            if kind == 'g':
                for r in lst:
                    d.setdefault(r.get(field), []).append(r)
            else:
                for r in lst:
                    v = r.get(field)
                    if v not in d:
                        d[v] = r
            c = _OvIndex(kind, field, d, None)
            owned = self._owned
            if owned:
                for r in lst:
                    if id(r) in owned:
                        c.owned[id(r)] = (r, r.get(field))
        st.idx[(kind, field)] = c
        return c.hand_out()

    def _op(self, st: TableState, op: tuple) -> None:
        """record a row operation on the overlay and keep its indexes exact"""
        if st.ops is not None:
            st.ops.append(op)
        if st.idx:
            for k in list(st.idx):
                if not st.idx[k].apply(op, self._owned):
                    del st.idx[k]

    def tenant_group(self, t: str, tenant_id: Any, field: str) -> dict[Any, list] | None:
        """group(t, field) when rows(ctx, t) is the table view itself (tenant mode, the request's tenant, no foreign-user
        stubs) — then `group(...)[v]` holds exactly the rows of `[r for r in rows(ctx, t) if r.get(field) == v]`, in the
        same order (str values; see app.ctx.rows_where). None otherwise."""
        if self.mode != 'tenant' or t == 'tenants' or tenant_id != self.tenant_id or t in VIRTUAL_TABLES:
            return None
        st = self._tables.get(t) or self._load(t)
        if st.stub_count:
            return None
        return self.group(t, field)

    def rows_by_field(self, t: str, tenant_id: Any, field: str, value: Any) -> list | None:
        """SQL pushdown: the tenant's rows of `t` with row[field] == value, in table order (ORDER BY _ord), by an
        index-backed query — when the table is neither loaded in this request nor cached at the snapshot's version
        (then reading the whole table only to filter it is the expensive path). READ-ONLY rows that are not part of
        the request's views (never patch them). None: use the table view (rows_where) instead."""
        if self.mode != 'tenant' or tenant_id != self.tenant_id or t == 'tenants' or t in VIRTUAL_TABLES or type(value) is not str:
            return None
        if t in self._tables or t in self._pending:
            return None
        tm = self.backend.meta.tables.get(t)
        if tm is None or field not in tm.columns:
            return None
        snap = self._snap()
        v = snap.versions.get(t, 0)
        if self.cache is not None and self.cache.peek(self.tenant_id, t) is not None:
            return None  # cached (current, or refreshed by the change log): the view's group index is cheaper
        if self._track is not None:
            self._touch(t)
        rows = snap.load_where(t, field, value)
        self._partial[t] = v
        return [freeze(r) for r in rows] if self.freeze else rows

    def view_token(self, t: str) -> Any:
        """an object that stays identical while the table's view is unchanged in this request (the view list itself:
        copy on write hands out a new list after any change) — guards request memos against the request's own writes"""
        return self.view(t)

    def memo_on(self, key: Any, tables: tuple[str, ...], fn: Callable[[], Any]) -> Any:
        """per-request memo of a pure function of `tables` (+ whatever `key` names), recomputed when this request
        changed one of the tables since — exactly the value a fresh call would return"""
        toks = tuple(self.view_token(t) for t in tables)
        e = self.memo.get(key)
        if e is not None and all(a is b for a, b in zip(e[0], toks)):
            return e[1]
        v = fn()
        self.memo[key] = (toks, v)
        return v

    def shared_peek(self, name: Any, tables: tuple[str, ...]) -> Any:
        """the shared value `name` of the current versions of `tables` when one is held (and this request has not
        changed those tables), else MISSING — never computes"""
        if self.mode != 'tenant' or self.cache is None or self.tenant_id is None:
            return MISSING
        entries = []
        for t in tables:
            st = self._tables.get(t) or self._load(t)
            if st.rows is not None or st.entry is None or st.stub_count:
                return MISSING
            entries.append(st.entry)
        hit = self.cache.derived_get(self.tenant_id, name, tuple(entries))
        return MISSING if hit is None else hit[0]

    def shared_base(self, name: Any, tables: tuple[str, ...]) -> Any:
        """the shared value `name` of the versions of `tables` this request LOADED (whatever it changed since), or
        MISSING — for incremental recomputation from a previous result"""
        if self.mode != 'tenant' or self.cache is None or self.tenant_id is None:
            return MISSING
        entries = []
        for t in tables:
            st = self._tables.get(t) or self._load(t)
            if st.entry is None or st.stub_count:
                return MISSING
            entries.append(st.entry)
        hit = self.cache.derived_get(self.tenant_id, name, tuple(entries))
        return MISSING if hit is None else hit[0]

    def shared_base_set(self, name: Any, tables: tuple[str, ...], value: Any, nbytes: int = 0) -> None:
        """file `value` (derived from the LOADED versions of `tables`) for shared_base()"""
        if self.mode != 'tenant' or self.cache is None or self.tenant_id is None:
            return
        entries = []
        for t in tables:
            st = self._tables.get(t) or self._load(t)
            if st.entry is None or st.stub_count:
                return
            entries.append(st.entry)
        self.cache.derived_set(self.tenant_id, name, tables, tuple(entries), value, nbytes)

    def shared_latest(self, name: Any) -> tuple[tuple[str, ...], tuple[Any, ...], Any] | None:
        """the most recent shared value `name` with the table names and cache entries it was derived from (any
        versions) — for incremental recomputation"""
        if self.mode != 'tenant' or self.cache is None or self.tenant_id is None:
            return None
        return self.cache.derived_latest(self.tenant_id, name)

    def loaded_entry(self, t: str) -> Any:
        st = self._tables.get(t) or self._load(t)
        return None if st.stub_count else st.entry

    def file_shared(self, name: Any, tables: tuple[str, ...], entries: tuple[Any, ...], value: Any, nbytes: int = 0) -> None:
        """file a value derived from exactly these cache entries"""
        if self.mode == 'tenant' and self.cache is not None and self.tenant_id is not None:
            self.cache.derived_set(self.tenant_id, name, tables, entries, value, nbytes)

    def note_derived(self, name: Any, tables: tuple[str, ...], value: Any, nbytes: int = 0) -> None:
        """`value` was just derived from the CURRENT rows of `tables` (some changed by this request): after a successful
        flush it is filed as the shared value of the versions written, if those tables did not change in between
        (view tokens) and read back exactly as written (app.runtime._file_noted)"""
        if self.mode == 'tenant' and self.cache is not None:
            self._noted.append((name, tables, value, tuple(self.view_token(t) for t in tables), nbytes))

    def noted_current(self, name: Any, value: Any, tables: tuple[str, ...]) -> bool:
        """`value` was noted as `name` and none of its tables changed since"""
        for n, tbls, v, toks, _ in reversed(self._noted):
            if n == name and v is value:
                return tbls == tables and all(self.view_token(t) is tok for t, tok in zip(tbls, toks))
        return False

    def noted_derived(self) -> list[tuple[Any, tuple[str, ...], Any, tuple[Any, ...], int]]:
        return self._noted

    def has_overlay(self, t: str) -> bool:
        st = self._tables.get(t) or self._load(t)
        return st.rows is not None

    def loaded_rows(self, t: str) -> list:
        """the rows as loaded (before this request's changes)"""
        st = self._tables.get(t) or self._load(t)
        return st.base

    def shared(self, name: Any, tables: tuple[str, ...], fn: Callable[[], Any], nbytes: Callable[[Any], int] | None = None) -> Any:
        """A value derived ONLY from `tables` (and from what `name` encodes), shared by every request of the process
        while those tables are unchanged: keyed by the identity of their (immutable) cache entries, so a new table
        version — from any process — is a different key. Computed directly (not shared) when this request has changed
        one of the tables, outside tenant mode, or without a cache. Shared values are read-only (deep-frozen outside
        production). In development/tests `fn` runs under read tracking and a read of an undeclared table fails."""
        if self.mode != 'tenant' or self.cache is None or self.tenant_id is None:
            return fn()
        entries = []
        for t in tables:
            st = self._tables.get(t) or self._load(t)
            if st.rows is not None or st.entry is None or st.stub_count:
                return fn()
            entries.append(st.entry)
            if self._track is not None:
                self._touch(t)
        ents = tuple(entries)
        hit = self.cache.derived_get(self.tenant_id, name, ents)
        if hit is not None:
            return hit[0]
        # single flight: concurrent requests needing the same value compute it once
        fk = ('derived', self.tenant_id, name, tuple(id(e) for e in ents))
        lead, ev = self.cache.flight(fk)
        if not lead:
            ev.wait(30)
            hit = self.cache.derived_get(self.tenant_id, name, ents)
            if hit is not None:
                return hit[0]
            return self._shared_compute(name, tables, fn, nbytes, ents, None)
        return self._shared_compute(name, tables, fn, nbytes, ents, fk)

    def _shared_compute(self, name: Any, tables: tuple[str, ...], fn: Callable[[], Any], nbytes: Any, ents: tuple, flight: Any) -> Any:
        try:
            return self._shared_compute1(name, tables, fn, nbytes, ents)
        finally:
            if flight is not None:
                self.cache.land(flight)

    def _shared_compute1(self, name: Any, tables: tuple[str, ...], fn: Callable[[], Any], nbytes: Any, ents: tuple) -> Any:
        if self.freeze:
            rec = self.track_begin()
            try:
                v = fn()
            finally:
                self.track_end(rec)
            extra = rec - set(tables)
            if extra:
                import logging

                logging.getLogger('seereonos').error(f'shared value {name!r} read undeclared tables {sorted(extra)}')
                raise RuntimeError(f'shared value {name!r} read undeclared tables {sorted(extra)}')
            v = freeze(v)
        else:
            v = fn()
        if any(self._tables[t].rows is not None for t in tables):
            return v  # fn itself changed one of its tables (lazy provisioning): the value is not of these versions
        self.cache.derived_set(self.tenant_id, name, tables, ents, v, nbytes(v) if nbytes else 0)
        return v

    # ------------------------------------------------------------------ writes (copy on write)
    def _mutable(self, t: str) -> tuple[TableState, list]:
        if t in VIRTUAL_TABLES:
            raise RuntimeError(f'use the virtual-table helpers for {t}')
        st = self._tables.get(t) or self._load(t)
        if st.rows is None:
            st.rows = list(st.base)
            st.ops = []
            st.idx = {}
        elif st.view_cache is st.rows:
            st.rows = list(st.rows)
        st.view_cache = None
        st.dirty = True
        self.write_intent = True
        return st, st.rows

    def append(self, t: str, row: dict) -> dict:
        """getDb()[t].push(row) + persist()"""
        if t not in self._tables and self.mode == 'tenant' and t not in VIRTUAL_TABLES:
            if t not in self.backend.meta.tables:
                raise UnknownTable(t)
            self._pending.setdefault(t, []).append(row)
            self._owned[id(row)] = row
            self.write_intent = True
            return row
        st, lst = self._mutable(t)
        lst.append(row)
        self._owned[id(row)] = row
        self._op(st, ('a', row))
        return row

    def is_owned(self, row: Any) -> bool:
        """the row was created or copied by this request (private: may be changed in place)"""
        return id(row) in self._owned

    def edit(self, t: str, row: dict) -> dict:
        """a PRIVATE, deep-mutable copy of `row` put in its place (copy on write) — change it in place afterwards exactly
        like the TS does (row['status'] = …, row['history'].append(…), helpers that mutate it); it is diffed at flush.
        A row this request already owns is returned as is."""
        if id(row) in self._owned:
            self._mutable(t)
            return row
        from app.frozen import thaw

        new = thaw(row)
        self._owned[id(new)] = new
        self.replace_row(t, row, new)
        return new

    def replace_row(self, t: str, old: dict, new: dict) -> dict:
        """put `new` where `old` is (same position); `old` must be a row of the current view"""
        st, lst = self._mutable(t)
        for i, r in enumerate(lst):
            if r is old:
                lst[i] = new
                self._op(st, ('r', old, new))
                return new
        raise RuntimeError(f'replace_row: the row is not in {t} (stale reference — use the row returned by the last patch)')

    def remove_row(self, t: str, row: dict) -> None:
        st, lst = self._mutable(t)
        for i, r in enumerate(lst):
            if r is row:
                del lst[i]
                self._op(st, ('d', row))
                return
        raise RuntimeError(f'remove_row: the row is not in {t}')

    def set_rows(self, t: str, rows: Iterable[dict]) -> None:
        """getDb()[t] = rows (e.g. a filtered copy) + persist()"""
        if t in VIRTUAL_TABLES:
            self.set_virtual(t, rows)
            return
        new = list(rows)
        st, old = self._mutable(t)
        ops = _diff_ops(old, new) if st.ops is not None else None
        st.rows = new
        if ops is None:
            st.ops = None
            st.idx = {}
        else:
            for op in ops:
                self._op(st, op)

    def patch(self, t: str, row: dict, changes: dict) -> dict:
        """Object.assign(row, changes) + persist(), copy-on-write: returns the NEW row (use it from now on)."""
        if id(row) in self._owned:  # already private: change it in place (keeps other references to it valid)
            self._mutable(t)
            row.update(changes)
            return row
        new = {**row, **changes}
        self._owned[id(new)] = new
        self.replace_row(t, row, new)
        return new

    # ------------------------------------------------------------------ virtual platform tables
    def _virtual(self, t: str) -> dict[str, Any]:
        v = self._virt.get(t)
        if v is not None:
            return v
        if self.mode == 'none':
            raise RuntimeError(f'table "{t}" accessed without a session')
        b = self.backend
        tid = None if self.mode == 'global' else (self.tenant_id or '__none__')
        if t == 'passwords':
            raise RuntimeError('passwords are not readable as a table (use password_matches / has_password)')
        if self.mode == 'fresh':
            rows = b.load_attempts(self.emails) if t == 'login_attempts' else []
        elif t == 'sessions':
            rows = b.load_sessions(tid, self.rt.idle_hours)
        elif t == 'reset_tokens':
            rows = b.load_reset_tokens(tid)
        else:
            rows = b.load_attempts(self.emails)
        rows = [freeze(r) if self.freeze else r for r in rows]
        v = {'orig': rows, 'current': rows, 'changed': False}
        self._virt[t] = v
        return v

    def virtual_view(self, t: str) -> list:
        v = self._virtual(t)
        cur = v['current']
        return FrozenList(cur) if self.freeze and not isinstance(cur, FrozenList) else cur

    def set_virtual(self, t: str, rows: Iterable[dict]) -> None:
        v = self._virtual(t)
        v['current'] = list(rows)
        v['changed'] = True
        self.write_intent = True

    def _require_session_scope(self) -> None:
        if self.mode == 'none':
            raise RuntimeError('passwords accessed without a session')

    def password_hash_of(self, user_id: str) -> str | None:
        self._require_session_scope()
        if user_id in self._pw_hashes:
            return self._pw_hashes[user_id]
        if self.mode == 'fresh' or not isinstance(user_id, str):
            h = None  # a brand-new workspace has no credentials yet
        else:
            tid = None if self.mode == 'global' else (self.tenant_id or '__none__')
            h = self.backend.password_hashes(tid, [user_id]).get(user_id)
        self._pw_hashes[user_id] = h
        return h

    def password_matches(self, user_id: str, plaintext: Any) -> bool:
        """`d.passwords.find(p => p.user_id === id)?.password === plaintext` (scrypt verify, memoised per request)"""
        if not isinstance(plaintext, str):
            return False
        w = self._pw_writes.get(user_id, ...)
        if w is not ...:
            return w is not None and w == plaintext
        h = self.password_hash_of(user_id)
        if h is None:
            return False
        k = (h, plaintext)
        ok = self._pw_known.get(k)
        if ok is None:
            from app.auth.passwords import verify_password

            ok = verify_password(plaintext, h)
            self._pw_known[k] = ok
        return ok

    def has_password(self, user_id: str) -> bool:
        w = self._pw_writes.get(user_id, ...)
        if w is not ...:
            return w is not None
        return self.password_hash_of(user_id) is not None

    def set_password(self, user_id: str, plaintext: str) -> None:
        """d.passwords = d.passwords.filter(p => p.user_id !== id); d.passwords.push({ user_id, password })"""
        self._require_session_scope()
        self._pw_writes[user_id] = plaintext
        self.write_intent = True

    def delete_password(self, user_id: str) -> None:
        self._require_session_scope()
        self._pw_writes[user_id] = None
        self.write_intent = True

    def queue_platform_mail(self, to: str, subject: str, body: str, expires_at: str | None = None, tenant_id: str | None = None) -> str:
        """an e-mail that is not a tenant outbox message (password reset link, invitation): the body is sealed with
        the MFA key (AES-256-GCM) and queued for the worker in this request's flush transaction. Returns its id."""
        from app.auth.crypto import new_token
        from app.engine.secrets import seal_secret

        self._require_session_scope()
        mid = 'pm_' + new_token()[:24]
        self._mails.append(
            {'id': mid, 'tenant_id': tenant_id or self.tenant_id, 'to': to, 'subject': subject, 'body': seal_secret(body), 'expires_at': expires_at}
        )
        self.write_intent = True
        return mid

    # ------------------------------------------------------------------ flush plan
    def build_plan(self) -> tuple[list[Any], Any, str | None, dict[str, int]]:
        """(table changes, platform changes, tenant id, read set)"""
        from app.db.backend import PlatformChange, TableChange
        from app.js import prepare_json

        changes: list[TableChange] = []
        tenant_id = self.tenant_id
        for t, st in self._tables.items():
            if st.rows is None or not st.dirty:
                continue
            key = self.backend.key_fn(t)
            ch = TableChange(t)
            # Unchanged loaded rows are the loaded objects themselves (copy on write): they need no key, no tenant check
            # (they came from this tenant's snapshot) and keep their (unique) keys. Only the other rows are keyed and
            # checked; a base key is deleted when neither its row nor a row with its key is present any more.
            base_ids = st.base_ids()
            present: set[int] = set()  # ids of loaded rows still present
            seen: set = set()  # keys of the other rows
            dup_check = []
            for row in st.rows:
                rid = id(row)
                if rid in base_ids:
                    if rid in present:
                        raise RuntimeError(f'Duplicate primary key in {t}: {key(row)}')
                    present.add(rid)
                    continue
                if not isinstance(row, dict):
                    raise RuntimeError(f'DB.{t} contains a non-object row')
                if st.stub_count and rid in self._stub_ids:
                    continue
                if self.mode in ('tenant', 'fresh'):
                    row_tenant = row.get('id') if t == 'tenants' else row.get('tenant_id')
                    if self.mode == 'fresh' and tenant_id is None:
                        tenant_id = row_tenant
                    if row_tenant != tenant_id:
                        raise RuntimeError(f'Cross-tenant row in {t}: {key(row)} belongs to {row_tenant}, request tenant is {tenant_id}')
                k = key(row)
                if k in seen:
                    raise RuntimeError(f'Duplicate primary key in {t}: {k}')
                seen.add(k)
                o = st.base_index.get(k)
                if o is None:
                    ch.inserts.append(row)
                else:
                    dup_check.append((k, o))
                    if o != row and prepare_json(o) != prepare_json(row):
                        ch.updates.append((o, row))
            for k, o in dup_check:
                if id(o) in present:  # the loaded row is still there next to another row with its key
                    raise RuntimeError(f'Duplicate primary key in {t}: {k}')
            if len(present) != len(st.base_index) or seen:
                for k, o in st.base_index.items():
                    if id(o) not in present and k not in seen:
                        ch.deletes.append(o)
            if ch.inserts or ch.updates or ch.deletes:
                changes.append(ch)
        for t, pend in self._pending.items():  # appends to tables this request never read: all inserts
            key = self.backend.key_fn(t)
            ch = TableChange(t)
            seen_p: set = set()
            for row in pend:
                if not isinstance(row, dict):
                    raise RuntimeError(f'DB.{t} contains a non-object row')
                row_tenant = row.get('id') if t == 'tenants' else row.get('tenant_id')
                if row_tenant != tenant_id:
                    raise RuntimeError(f'Cross-tenant row in {t}: {key(row)} belongs to {row_tenant}, request tenant is {tenant_id}')
                k = key(row)
                if k in seen_p:
                    raise RuntimeError(f'Duplicate primary key in {t}: {k}')
                seen_p.add(k)
                ch.inserts.append(row)
            if ch.inserts:
                changes.append(ch)
        if changes and self.mode == 'tenant' and tenant_id is not None and self.find_by('tenants', 'id', tenant_id) is None:
            # a context without a workspace row (the platform super admin, tenant 'platform') cannot own tenant rows: the
            # lazy provisioning some reads do (default workflow templates, number sequences) stays request-local, like
            # the in-memory mock, instead of failing the request on the tenant foreign key (500). `users` and
            # `admin_audit` have no tenant foreign key (the platform admin's own row, e.g. last_login_at) and are kept.
            changes = [c for c in changes if c.table in _NO_TENANT_FK]
        self.tenant_id = tenant_id
        read_set = {**self._partial, **{t: st.version for t, st in self._tables.items()}} if self.mode == 'tenant' else {}
        return changes, self._platform_changes(PlatformChange()), tenant_id, read_set

    def _platform_changes(self, pc: Any) -> Any:
        from app.auth.crypto import is_token_hash, sha256
        from app.auth.passwords import hash_password, is_password_hash

        if self._mails:
            pc.mails.extend(m for m in self._mails if m.get('tenant_id'))

        if self._pw_writes:
            for uid, plain in self._pw_writes.items():
                if plain is None:
                    if self.password_hash_of(uid) is not None:
                        pc.credential_deletes.append(uid)
                    continue
                h = self.password_hash_of(uid)
                if h is not None and (plain == h or self._pw_known.get((h, plain))):
                    continue
                pc.credential_upserts.append((uid, plain if is_password_hash(plain) else hash_password(plain)))
        ss = self._virt.get('sessions')
        if ss and ss['changed']:
            orig_ids = {id(s) for s in ss['orig']}
            orig_tokens = {s['token'] for s in ss['orig']}
            cur_tokens = set()
            for s in ss['current']:
                cur_tokens.add(s['token'])
                if id(s) in orig_ids or s['token'] in orig_tokens:
                    continue
                pc.session_inserts.append(
                    {
                        'token_hash': s['token'] if is_token_hash(s['token']) else sha256(s['token']),
                        'user_id': s['user_id'],
                        'tenant_id': s['tenant_id'],
                        'created_at': s['created_at'],
                    }
                )
            pc.session_revokes.extend(t for t in orig_tokens if t not in cur_tokens)
        rt = self._virt.get('reset_tokens')
        if rt and rt['changed']:
            orig = {r['token']: r for r in rt['orig']}
            cur = set()
            for r in rt['current']:
                cur.add(r['token'])
                if r['token'] not in orig:
                    pc.reset_inserts.append(
                        {
                            'token_hash': r['token'] if is_token_hash(r['token']) else sha256(r['token']),
                            'user_id': r['user_id'],
                            'expires_at': r['expires_at'],
                            'used': bool(r['used']),
                        }
                    )
                elif r['used'] and not orig[r['token']]['used']:
                    pc.reset_used.append(r['token'])
            pc.reset_deletes.extend(t for t in orig if t not in cur)
        la = self._virt.get('login_attempts')
        if la and la['changed']:
            orig_ids = {id(a) for a in la['orig']}
            remaining = set()
            for a in la['current']:
                remaining.add(a['email'])
                if id(a) not in orig_ids:
                    pc.attempt_failures.append({'email': a['email'], 'at': a['at']})
            for e in dict.fromkeys(a['email'] for a in la['orig']):
                if e not in remaining:
                    pc.attempt_clears.append(e)
        return pc

    # ------------------------------------------------------------------ response memo (app.memo)
    def snapshot_versions(self) -> dict[str, int]:
        """tenant_table_versions of the request's snapshot (opens it)"""
        return self._snap().versions

    def memoisable(self) -> bool:
        """the response depends only on versioned tables of this snapshot: tenant mode, nothing written or about to
        be, no platform (virtual) tables, no foreign-user stubs"""
        if self.mode != 'tenant' or self.write_intent or self._virt or self._stubs or self._pw_writes or self._pw_hashes or self._pending or self._mails:
            return False
        return all(st.rows is None and not st.dirty for st in self._tables.values())

    def read_set(self) -> tuple[tuple[str, int], ...]:
        return tuple({**self._partial, **{t: st.version for t, st in self._tables.items()}}.items())

    def pending_rows(self, t: str) -> list[dict] | None:
        """rows appended to `t` without loading it (flushed as inserts), None when the table was loaded"""
        return self._pending.get(t)

    def current_rows(self, t: str) -> list | None:
        """the table after this request (without foreign-user stubs) — refills the cache after a flush"""
        st = self._tables.get(t)
        if st is None:
            return None
        lst = st.base if st.rows is None else st.rows
        if st.stub_count:
            return [r for r in lst if id(r) not in self._stub_ids]
        return lst

    def unchanged_row(self, t: str, row: dict) -> bool:
        """row IS a loaded row (keys are unique, so this is `base_index[key(row)] is row`)"""
        return id(row) in self._tables[t].base_ids()


class DbView:
    """getDb(): attribute / item access to the scope's tables.

    d = get_db(); d.users            # read-only list (getDb().users)
    d.sessions = [...]                # getDb().sessions = … (set_rows / virtual tables)
    """

    __slots__ = ('_scope',)

    def __init__(self, scope: Scope) -> None:
        object.__setattr__(self, '_scope', scope)

    def __getattr__(self, t: str) -> list:
        if t not in _DB_KEY_SET:
            raise AttributeError(t)
        return self._scope.view(t)

    def __getitem__(self, t: str) -> list:
        if t not in _DB_KEY_SET:
            raise KeyError(t)
        return self._scope.view(t)

    def __setattr__(self, t: str, rows: Iterable[dict]) -> None:
        if t not in _DB_KEY_SET:
            raise AttributeError(t)
        self._scope.set_rows(t, rows)

    __setitem__ = __setattr__

    def keys(self) -> tuple[str, ...]:
        return DB_KEYS


def run_in_scope(scope: Scope, fn: Callable[[], Any]) -> Any:
    with scope:
        return fn()
