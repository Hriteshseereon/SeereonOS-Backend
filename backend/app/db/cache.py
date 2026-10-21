"""Per-process tenant cache (port of server/src/uow/cache.ts).

Rows of each tenant table, tagged with the tenant_table_versions value they were read at. A request reuses an entry
only when its version equals the current version (one query per request), so instances and worker processes behind a
load balancer stay coherent. Bounded by tenant count and bytes (LRU). Entries are immutable: their rows are shared by
every request of the process (deep-frozen in development/tests), so derived lookups (pk index, field indexes, groups)
are built once per entry and reused across requests.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import orjson

from app.frozen import FrozenList
from app.js import prepare_json


def estimate_bytes(rows: list[dict[str, Any]]) -> int:
    """cache accounting only: twice the JSON size of an evenly spaced sample of at most 256 rows, scaled to the table
    (serialising a 100k-row table on every write to measure it exactly cost seconds)"""
    n = len(rows)
    if n == 0:
        return 0
    step = max(1, n // 256)
    sample = rows[::step]
    try:
        size = len(orjson.dumps(prepare_json(sample), option=orjson.OPT_NON_STR_KEYS))
    except TypeError:
        return 1000 * n
    return int(size * 2 * n / len(sample))


class TableEntry:
    __slots__ = ('version', 'rows', 'keys', 'bytes', '_pk_index', '_indexes', '_groups', '_lock', '_ids', 'refill')

    def __init__(
        self, version: int, rows: list[dict[str, Any]], key_of: Callable[[dict], Any], nbytes: int | None = None, pk: str | None = None, refill: bool = False
    ) -> None:
        """pk: the single primary-key column (when key_of is `r.get(pk)`) — a faster key list. refill: built from the
        previous version's rows (unchanged rows are the same objects), not loaded from the database"""
        self.refill = refill
        self.version = version
        self.rows = rows
        self.keys = [r.get(pk) for r in rows] if pk is not None else [key_of(r) for r in rows]
        if nbytes is None:
            nbytes = estimate_bytes(rows)
        self.bytes = nbytes + len(rows) * 64
        self._pk_index: dict[Any, dict] | None = None
        self._indexes: dict[str, dict[Any, Any]] = {}
        self._groups: dict[str, dict[Any, list]] = {}
        self._lock = threading.Lock()
        self._ids: set[int] | None = None

    def pk_index(self) -> dict[Any, dict]:
        """primary key -> row as loaded (the snapshot the flush diffs against, by identity)"""
        idx = self._pk_index
        if idx is None:
            idx = dict(zip(self.keys, self.rows))
            self._pk_index = idx
        return idx

    def carry_group(self, field: str, g: dict[Any, list]) -> None:
        """a group index of these rows built elsewhere (equal to group(field)); kept only if none was built"""
        self._groups.setdefault(field, g)

    def carry_index(self, field: str, idx: dict[Any, Any]) -> None:
        self._indexes.setdefault(field, idx)

    def row_ids(self) -> set[int]:
        """id() of every row (the rows are kept alive by the entry): `id(r) in row_ids()` = r IS a row of this version"""
        ids = self._ids
        if ids is None:
            ids = self._ids = {id(r) for r in self.rows}
        return ids

    def index(self, field: str) -> dict[Any, Any]:
        """value of `field` -> FIRST row with it (Array.find semantics)"""
        idx = self._indexes.get(field)
        if idx is None:
            idx = {}
            for r in self.rows:
                v = r.get(field)
                if v not in idx:
                    idx[v] = r
            self._indexes[field] = idx
        return idx

    def group(self, field: str) -> dict[Any, list]:
        """value of `field` -> rows with it, in table order (filter semantics). Shared by every request: read-only
        (frozen lists when the rows are frozen, i.e. outside production)."""
        g = self._groups.get(field)
        if g is None:
            g = {}
            for r in self.rows:
                g.setdefault(r.get(field), []).append(r)
            if isinstance(self.rows, FrozenList):
                g = {k: FrozenList(v) for k, v in g.items()}
            self._groups[field] = g
        return g


# derived values offered for incremental recomputation after their inputs change (uow.Scope.shared_latest): kept
# across a refill / incremental refresh until replaced; every other derived value is dropped with an old version
KEEP_ACROSS_VERSIONS = frozenset({('an:costed',), ('an:costedById',)})
MAX_DERIVED_PER_TENANT = 1000


class TenantCache:
    def __init__(self, max_tenants: int = 50, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.max_tenants = max_tenants
        self.max_bytes = max_bytes
        self._tenants: OrderedDict[str, dict[str, TableEntry]] = OrderedDict()
        # derived values shared across requests (uow.Scope.shared): tenant -> name -> (tables, entries, value, bytes)
        self._derived: dict[str, dict[Any, tuple[tuple[str, ...], tuple[TableEntry, ...], Any, int]]] = {}
        self._flights: dict[Any, threading.Event] = {}
        self._bytes = 0
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, tenant_id: str, table: str, version: int) -> TableEntry | None:
        with self._lock:
            t = self._tenants.get(tenant_id)
            e = t.get(table) if t else None
            if e is not None and e.version == version:
                self._tenants.move_to_end(tenant_id)
                self.hits += 1
                return e
            self.misses += 1
            return None

    # ---------------------------------------------------------------- single flight
    def flight(self, key: Any) -> tuple[bool, threading.Event]:
        """(leader, event): the first caller for `key` leads (does the work, then land()s); the others wait on the
        event and then look again — N concurrent requests for the same cold table / value do the work once"""
        with self._lock:
            ev = self._flights.get(key)
            if ev is not None:
                return False, ev
            ev = threading.Event()
            self._flights[key] = ev
            return True, ev

    def land(self, key: Any) -> None:
        with self._lock:
            ev = self._flights.pop(key, None)
        if ev is not None:
            ev.set()

    def peek(self, tenant_id: str, table: str) -> TableEntry | None:
        """the cached entry of any version (incremental refresh)"""
        t = self._tenants.get(tenant_id)
        return t.get(table) if t else None

    def set(self, tenant_id: str, table: str, entry: TableEntry) -> None:
        if self.max_tenants == 0:
            return
        with self._lock:
            t = self._tenants.get(tenant_id)
            if t is None:
                t = {}
                self._tenants[tenant_id] = t
            old = t.get(table)
            if old is not None:
                if old.version > entry.version:
                    return  # never replace a newer entry with an older one (concurrent fills)
                self._bytes -= old.bytes
                if old is not entry:
                    # values of the old version can never be read again; after a refill (unchanged rows are the same
                    # objects) the reusable ones stay until replaced, for incremental recomputation (derived_latest)
                    self._drop_derived(tenant_id, (table,), keep=KEEP_ACROSS_VERSIONS if entry.refill else frozenset())
            t[table] = entry
            self._bytes += entry.bytes
            self._tenants.move_to_end(tenant_id)
            self._evict(tenant_id)

    def derived_get(self, tenant_id: str, name: Any, entries: tuple[TableEntry, ...]) -> tuple[Any] | None:
        """(value,) when `name` was derived from exactly these entries (identity), else None"""
        d = self._derived.get(tenant_id)
        hit = d.get(name) if d else None
        if hit is None or len(hit[1]) != len(entries) or not all(a is b for a, b in zip(hit[1], entries)):
            return None
        return (hit[2],)

    def derived_latest(self, tenant_id: str, name: Any) -> tuple[tuple[str, ...], tuple[TableEntry, ...], Any] | None:
        """(tables, entries, value) of the most recent `name`, whatever versions it was derived from"""
        d = self._derived.get(tenant_id)
        hit = d.get(name) if d else None
        return None if hit is None else (hit[0], hit[1], hit[2])

    def derived_set(self, tenant_id: str, name: Any, tables: tuple[str, ...], entries: tuple[TableEntry, ...], value: Any, nbytes: int = 0) -> None:
        if self.max_tenants == 0:
            return
        with self._lock:
            if tenant_id not in self._tenants:
                return  # its tables were evicted meanwhile
            cur = self._tenants[tenant_id]
            if any(cur.get(t) is not e for t, e in zip(tables, entries)):
                return  # a newer version of an input arrived meanwhile: don't keep the older value
            d = self._derived.setdefault(tenant_id, {})
            old = d.pop(name, None)
            if old is not None:
                self._bytes -= old[3]
            d[name] = (tables, entries, value, nbytes)  # (re)inserted last: the dict order is the recency order
            self._bytes += nbytes
            while len(d) > MAX_DERIVED_PER_TENANT:
                self._bytes -= d.pop(next(iter(d)))[3]
            self._evict(tenant_id)

    def _drop_derived(self, tenant_id: str, tables: Any = None, keep: frozenset = frozenset()) -> None:
        d = self._derived.get(tenant_id)
        if not d:
            return
        for name in list(d):
            if name in keep:
                continue
            if tables is None or any(t in tables for t in d[name][0]):
                self._bytes -= d.pop(name)[3]
        if not d:
            self._derived.pop(tenant_id, None)

    def invalidate(self, tenant_id: str, tables: Any = None) -> None:
        with self._lock:
            tables = None if tables is None else list(tables)
            self._drop_derived(tenant_id, tables)
            t = self._tenants.get(tenant_id)
            if not t:
                return
            for name in list(t.keys()) if tables is None else tables:
                e = t.pop(name, None)
                if e is not None:
                    self._bytes -= e.bytes
            if not t:
                del self._tenants[tenant_id]

    def clear(self) -> None:
        with self._lock:
            self._tenants.clear()
            self._derived.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                'tenants': len(self._tenants),
                'bytes': self._bytes,
                'hits': self.hits,
                'misses': self.misses,
                'derived': sum(len(d) for d in self._derived.values()),
            }

    def _evict(self, keep: str) -> None:
        for tid in list(self._tenants.keys()):
            if len(self._tenants) <= self.max_tenants and self._bytes <= self.max_bytes:
                break
            if tid == keep:
                continue
            self.invalidate(tid)
