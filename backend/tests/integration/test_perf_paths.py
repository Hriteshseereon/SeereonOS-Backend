"""The performance paths (backend/PERFORMANCE.md) give exactly the results of the plain paths they replace:
incremental cache refresh from the change log (V007) vs a full read, incrementally kept overlay indexes vs rebuilds,
the identity-based flush diff vs the reference algorithm, generated row mappers + SQL-rendered timestamps vs the
generic mapper + text loader, the response memo (hits, staleness, volatility, isolation), shared derived values
(declared reads, reuse, recomputation), costed entries reused across versions, appends without loading."""

from __future__ import annotations

import random
from typing import Any

import orjson
import psycopg
import pytest

from app.ctx import build_ctx, find, insert, patch, remove, rows
from app.js import prepare_json
from tests.conftest import APP, OWNER

pytestmark = pytest.mark.usefixtures('fresh')
T = 'tn_seereon'


def q(sql: str, *params: Any) -> Any:
    with psycopg.connect(OWNER, autocommit=True) as c:
        cur = c.execute(sql, params or None)  # type: ignore[arg-type]
        return cur.fetchall() if cur.description else None


def in_tenant(rt: Any, fn: Any, user: str = 'u_santosh', is_write: bool = True) -> Any:
    def body(s: Any) -> Any:
        return fn(s, build_ctx(s.find_by('users', 'id', user), T, {}))

    return rt.run_scoped('tenant', T, body, is_write=is_write)


def js(v: Any) -> bytes:
    return orjson.dumps(prepare_json(v), option=orjson.OPT_NON_STR_KEYS)


@pytest.fixture()
def rt(app: Any) -> Any:
    return app.state.rt


@pytest.fixture()
def rt_b() -> Any:
    """a second 'process': own pools and cache on the same database"""
    from app.db.backend import PgBackend
    from app.db.cache import TenantCache
    from app.db.pool import Pools
    from app.router import Router
    from app.runtime import Runtime

    pools = Pools(APP, 2)
    b = PgBackend(pools)
    b.init()
    r = Runtime(b, Router(), TenantCache(), freeze_rows=True)
    yield r
    pools.close()


def full_read(rt: Any, table: str) -> list[dict]:
    snap = rt.backend.snapshot(T)
    try:
        return snap.load(table)
    finally:
        snap.close()


# ---------------------------------------------------------------------------------------------------------------------
# incremental refresh from the change log
# ---------------------------------------------------------------------------------------------------------------------
def test_delta_refresh_equals_full_read(rt: Any, rt_b: Any, monkeypatch: Any) -> None:
    tables = ('notifications', 'tasks', 'timesheet_entries', 'holidays')
    in_tenant(rt_b, lambda s, c: [len(rows(c, t)) for t in tables], is_write=False)  # B caches them
    rng = random.Random(5)
    created: list[str] = []

    def writes(s: Any, ctx: Any) -> None:
        for _ in range(rng.randrange(1, 4)):
            n = insert(ctx, 'notifications', {'user_id': 'u_santosh', 'type': 'NTF-01', 'title': f't{rng.random()}', 'body': 'b', 'link': None, 'read_at': None})
            created.append(n['id'])
        ts = rows(ctx, 'tasks')
        t = ts[rng.randrange(len(ts))]
        patch('tasks', t, {'title': t['title'] + ' ·'})
        if created and rng.random() < 0.5:
            remove(ctx, 'notifications', created.pop(0))
        es = rows(ctx, 'timesheet_entries')
        e = es[rng.randrange(len(es))]
        patch('timesheet_entries', e, {'note': (e.get('note') or '') + '.'})

    for _ in range(4):  # A writes several times: B must apply the whole chain
        in_tenant(rt, writes)
    # a row deleted then inserted again with the same id moves to the end (new _ord)
    nid = q("SELECT id FROM notifications WHERE tenant_id = %s ORDER BY _ord LIMIT 1", T)[0][0]
    row = in_tenant(rt, lambda s, c: dict(find(c, 'notifications', nid)), is_write=False)
    in_tenant(rt, lambda s, c: remove(c, 'notifications', nid))
    in_tenant(rt, lambda s, c: s.append('notifications', {**row, 'title': 'again'}))

    from app.db import backend as B

    full: list[str] = []
    deltas: list[str] = []
    orig_load, orig_delta = B.Snapshot.load, B.Snapshot.delta
    monkeypatch.setattr(B.Snapshot, 'load', lambda self, t: full.append(t) or orig_load(self, t))
    monkeypatch.setattr(B.Snapshot, 'delta', lambda self, t, *a: (lambda r: (deltas.append(t) if r is not None else None) or r)(orig_delta(self, t, *a)))
    got = in_tenant(rt_b, lambda s, c: {t: list(rows(c, t)) for t in tables}, is_write=False)
    changed = {'notifications', 'tasks', 'timesheet_entries'}  # holidays did not change: still cached
    assert set(deltas) == changed and not set(full) & set(tables)
    for t in tables:
        assert js(got[t]) == js(full_read(rt_b, t)), t
    assert got['notifications'][-1]['id'] == nid and got['notifications'][-1]['title'] == 'again'


def test_delta_falls_back_on_a_gap(rt: Any, rt_b: Any, monkeypatch: Any) -> None:
    in_tenant(rt_b, lambda s, c: len(rows(c, 'holidays')), is_write=False)
    hid = q('SELECT id FROM holidays WHERE tenant_id = %s ORDER BY _ord LIMIT 1', T)[0][0]
    in_tenant(rt, lambda s, c: patch('holidays', find(c, 'holidays', hid), {'name': 'logged'}))
    q("UPDATE holidays SET name = 'out of band' WHERE id = %s", hid)  # bumps the version, logs nothing
    from app.db import backend as B

    full: list[str] = []
    orig = B.Snapshot.load
    monkeypatch.setattr(B.Snapshot, 'load', lambda self, t: full.append(t) or orig(self, t))
    got = in_tenant(rt_b, lambda s, c: find(c, 'holidays', hid)['name'], is_write=False)
    assert got == 'out of band' and 'holidays' in full


# ---------------------------------------------------------------------------------------------------------------------
# overlay indexes, set_rows diffs, the flush diff
# ---------------------------------------------------------------------------------------------------------------------
def _rebuild(lst: list[dict], field: str) -> tuple[dict, dict]:
    g: dict = {}
    i: dict = {}
    for r in lst:
        v = r.get(field)
        g.setdefault(v, []).append(r)
        if v not in i:
            i[v] = r
    return g, i


def _same_index(a: dict, b: dict) -> bool:
    return list(a) == list(b) and all((x is y) if isinstance(x, dict) else (len(x) == len(y) and all(p is q_ for p, q_ in zip(x, y))) for x, y in zip(a.values(), b.values()))


@pytest.mark.parametrize('seed_', range(6))
def test_overlay_indexes_equal_rebuilds(rt: Any, seed_: int) -> None:
    from app.db.uow import Scope

    rng = random.Random(seed_)
    fields = ('week_id', 'user_id', 'kind')
    snap_checks = []
    scope = Scope(rt, 'tenant', T)
    with scope:
        ctx = build_ctx(scope.find_by('users', 'id', 'u_santosh'), T, {})
        for _ in range(60):
            lst = scope.view('timesheet_entries')
            f = rng.choice(fields)
            g = scope.group('timesheet_entries', f)
            i = scope.index('timesheet_entries', f)
            rg, ri = _rebuild(list(lst), f)
            assert _same_index(g, rg) and _same_index(i, ri)
            snap_checks.append((g, {k: list(v) for k, v in g.items()}))
            op = rng.randrange(6)
            cur = list(scope.view('timesheet_entries'))
            r = cur[rng.randrange(len(cur))]
            if op == 0:
                insert(ctx, 'timesheet_entries', {**{k: v for k, v in r.items() if k not in ('id',)}, 'note': 'new'})
            elif op == 1:
                patch('timesheet_entries', r, {'note': 'p'})
            elif op == 2:
                patch('timesheet_entries', r, {'kind': 'internal' if r['kind'] != 'internal' else 'task'})
            elif op == 3:
                scope.remove_row('timesheet_entries', r)
            elif op == 4:
                scope.set_rows('timesheet_entries', [x for x in cur if x.get('week_id') != r.get('week_id')])
            else:
                e = scope.edit('timesheet_entries', r)
                e['user_id'] = 'u_hritesh'  # an owned row changed in place
        # handed-out indexes stay snapshots
        for g, copy in snap_checks:
            assert {k: list(v) for k, v in g.items()} == copy


def _reference_plan(st: Any, key: Any) -> tuple[list, list, list]:
    ins, upd, dele = [], [], []
    seen: set = set()
    for row in st.rows:
        k = key(row)
        seen.add(k)
        o = st.base_index.get(k)
        if o is None:
            ins.append(row)
        elif o is not row and o != row and prepare_json(o) != prepare_json(row):
            upd.append((o, row))
    for k, o in st.base_index.items():
        if k not in seen:
            dele.append(o)
    return ins, upd, dele


@pytest.mark.parametrize('seed_', range(4))
def test_flush_diff_equals_reference(rt: Any, seed_: int) -> None:
    from app.db.uow import Scope

    rng = random.Random(100 + seed_)
    scope = Scope(rt, 'tenant', T)
    with scope:
        ctx = build_ctx(scope.find_by('users', 'id', 'u_santosh'), T, {})
        for _ in range(30):
            cur = list(scope.view('tasks'))
            r = cur[rng.randrange(len(cur))]
            op = rng.randrange(4)
            if op == 0:
                insert(ctx, 'tasks', {**{k: v for k, v in r.items() if k != 'id'}, 'title': 'n'})
            elif op == 1:
                patch('tasks', r, {'title': r['title'] + '!'})
            elif op == 2:
                scope.remove_row('tasks', r)
            else:
                patch('tasks', r, {'title': r['title']})  # a copy that changes nothing
        st = scope.state('tasks')
        ref = _reference_plan(st, rt.backend.key_fn('tasks'))
        changes, _p, _t, _rs = scope.build_plan()
        ch = next(c for c in changes if c.table == 'tasks')
        assert [id(x) for x in ch.inserts] == [id(x) for x in ref[0]]
        assert [(id(a), id(b)) for a, b in ch.updates] == [(id(a), id(b)) for a, b in ref[1]]
        assert [id(x) for x in ch.deletes] == [id(x) for x in ref[2]]


# ---------------------------------------------------------------------------------------------------------------------
# row mapping
# ---------------------------------------------------------------------------------------------------------------------
def test_generated_mappers_equal_generic(rt: Any) -> None:
    from app.db.mapping import row_from_tuple_generic

    meta = rt.backend.meta
    with psycopg.connect(OWNER, autocommit=True) as c:
        for t, tm in meta.tables.items():
            for tup in c.execute(tm.select_sql + ' ORDER BY _ord LIMIT 300').fetchall():  # type: ignore[arg-type]
                a, b = tm.mapper(tup), row_from_tuple_generic(tm, tup)
                assert list(a) == list(b) and js(a) == js(b) and all(type(a[k]) is type(b[k]) for k in a), t


def test_sql_timestamps_equal_the_text_loader() -> None:
    from app.db.mapping import _TS_SQL
    from app.db.pool import connect

    values = [
        '2026-10-16 05:30:00+00',
        '2026-10-16 05:30:00.5+00',
        '2026-10-16 05:30:00.123456+00',
        '2026-10-16 05:30:00.999999+00',
        '0001-01-01 00:00:00+00',
        '0999-03-04 01:02:03.004+00',
        '9999-12-31 23:59:59.999999+00',
        '0044-03-15 12:00:00+00 BC',
        'infinity',
        '-infinity',
        '2026-03-29 01:30:00+05:30',
    ]
    with connect(OWNER) as c:
        c.execute('CREATE TEMP TABLE tsx (ts timestamptz)')
        for v in values:
            c.execute('INSERT INTO tsx VALUES (%s::timestamptz)', [v])
        c.execute('INSERT INTO tsx VALUES (NULL)')
        loader = [r[0] for r in c.execute('SELECT ts FROM tsx').fetchall()]
        sql = [r[0] for r in c.execute('SELECT ' + _TS_SQL.format(c='ts') + ' FROM tsx').fetchall()]  # type: ignore[arg-type]
    assert sql == loader


# ---------------------------------------------------------------------------------------------------------------------
# response memo
# ---------------------------------------------------------------------------------------------------------------------
def test_memo_hits_and_staleness(app: Any, api: Any) -> None:
    rt = app.state.rt
    tok = api.as_('santosh@seereon.co.in')
    api.call('GET', '/dashboards/ceo', token=tok)  # after the reseed the first view tidies data (a write): not stored
    rt.memo.clear()
    h0 = rt.memo.hits
    a = api.call('GET', '/dashboards/ceo', token=tok)
    b = api.call('GET', '/dashboards/ceo', token=tok)
    assert a.status_code == 200 and a.content == b.content and rt.memo.hits == h0 + 1
    # a write to a table the response read: the entry is stale, the answer recomputed (and equal to a fresh one)
    assert api.call('POST', '/holidays', {'date': '2031-02-02', 'name': 'Memo day', 'optional': False}, token=tok).status_code == 200
    s0 = rt.memo.stale
    c = api.call('GET', '/dashboards/ceo', token=tok)
    assert rt.memo.stale == s0 + 1
    rt.memo.clear()
    d = api.call('GET', '/dashboards/ceo', token=tok)
    assert c.content == d.content


def test_memo_keys_users_and_view_as(app: Any, api: Any) -> None:
    rt = app.state.rt
    ceo, emp = api.as_('santosh@seereon.co.in'), api.as_('hritesh@seereon.co.in')
    for _ in range(2):
        api.call('GET', '/tasks', token=ceo)  # stored (after any first-view tidy-up)
    rt.memo.clear()
    api.call('GET', '/tasks', token=ceo)
    h0 = rt.memo.hits
    b = api.call('GET', '/tasks', token=emp)
    c = api.call('GET', '/tasks', token=ceo, headers={'X-View-As': 'employee'})
    a = api.call('GET', '/tasks', token=ceo)
    assert a.content != b.content and a.content != c.content and rt.memo.hits == h0 + 1  # only the last one


def test_memo_never_stores_clock_or_platform_reads(app: Any, api: Any) -> None:
    rt = app.state.rt
    tok = api.as_('santosh@seereon.co.in')
    rt.memo.clear()
    assert api.call('GET', '/sessions', token=tok).status_code == 200  # platform table (sessions)
    assert api.call('GET', '/invites', token=tok).status_code == 200  # reads the clock (invite expiry)
    assert rt.memo.stats()['entries'] == 0
    api.call('GET', '/me', token=tok)
    assert rt.memo.stats()['entries'] == 1


# ---------------------------------------------------------------------------------------------------------------------
# shared derived values, costed entries reuse, appends without loading
# ---------------------------------------------------------------------------------------------------------------------
def test_shared_value_declares_its_reads(rt: Any) -> None:
    def fn(s: Any, ctx: Any) -> Any:
        return s.shared(('t:x',), ('tasks',), lambda: len(rows(ctx, 'tasks')) + len(rows(ctx, 'milestones')))

    with pytest.raises(RuntimeError, match='undeclared'):
        in_tenant(rt, fn, is_write=False)


def test_shared_value_is_reused_and_recomputed(rt: Any) -> None:
    n = []

    def fn(s: Any, ctx: Any) -> Any:
        return s.shared(('t:count',), ('holidays',), lambda: n.append(1) or len(rows(ctx, 'holidays')))

    a = in_tenant(rt, fn, is_write=False)
    b = in_tenant(rt, fn, is_write=False)
    assert a == b and len(n) == 1
    in_tenant(rt, lambda s, c: insert(c, 'holidays', {'date': '2032-01-01', 'name': 'x', 'optional': False}))
    assert in_tenant(rt, fn, is_write=False) == a + 1 and len(n) == 2


def test_costed_entries_reuse_equals_full_computation(rt: Any) -> None:
    from app.engine import analytics as an

    in_tenant(rt, lambda s, c: an.costed_entries(c), is_write=False)  # shared value of these versions
    eid = q("SELECT id FROM timesheet_entries WHERE tenant_id = %s AND kind = 'task' ORDER BY _ord LIMIT 1", T)[0][0]

    def write(s: Any, ctx: Any) -> Any:
        e = find(ctx, 'timesheet_entries', eid)
        patch('timesheet_entries', e, {'hours': e['hours'] + 1})
        reused = an.costed_entries(ctx)  # the request's own (changed) rows: reuse of the unchanged ones
        s.memo.clear()
        latest = s.cache._derived.pop(T, None)
        full = an.costed_entries(ctx)  # nothing to reuse: computed from scratch
        if latest is not None:
            s.cache._derived[T] = latest
        return js(reused) == js(full)

    assert in_tenant(rt, write)


def test_append_without_loading(rt: Any, monkeypatch: Any) -> None:
    from app.db import backend as B

    loads: list[str] = []
    orig = B.Snapshot.load
    monkeypatch.setattr(B.Snapshot, 'load', lambda self, t: loads.append(t) or orig(self, t))
    rt.cache.clear()

    def fn(s: Any, ctx: Any) -> Any:
        a = insert(ctx, 'activity_log', {'entity': 'x', 'entity_id': 'y', 'action': 'z', 'summary': 's', 'actor_id': 'u_santosh', 'at': '2026-01-01T00:00:00.000Z'})
        assert 'activity_log' not in loads  # not read for an append
        last = rows(ctx, 'activity_log')[-1]  # a later read sees it at the end, like an eager append
        return a['id'], last['id']

    aid, last = in_tenant(rt, fn)
    assert aid == last
    assert q('SELECT id FROM activity_log WHERE tenant_id = %s ORDER BY _ord DESC LIMIT 1', T)[0][0] == aid

    def only_append(s: Any, ctx: Any) -> Any:
        return insert(ctx, 'activity_log', {'entity': 'x', 'entity_id': 'y', 'action': 'z', 'summary': 's2', 'actor_id': 'u_santosh', 'at': '2026-01-01T00:00:00.000Z'})['id']

    loads.clear()
    in_tenant(rt, lambda s, c: len(rows(c, 'activity_log')), is_write=False)  # cached
    bid = in_tenant(rt, only_append)
    got = in_tenant(rt, lambda s, c: [r['id'] for r in rows(c, 'activity_log')], is_write=False)
    assert got == [r[0] for r in q('SELECT id FROM activity_log WHERE tenant_id = %s ORDER BY _ord', T)] and got[-1] == bid
    assert 'activity_log' not in loads  # the cache is refilled with the appended rows each time, never read again


def test_conflict_semantics_kept_for_read_tables(rt: Any) -> None:
    """appends do not join the read set, reads still do (the lost race below is detected)"""
    rt._contended.clear()
    hid = q('SELECT id FROM holidays WHERE tenant_id = %s ORDER BY _ord LIMIT 1', T)[0][0]
    calls = []

    def fn(s: Any, ctx: Any) -> Any:
        calls.append(1)
        h = find(ctx, 'holidays', hid)
        insert(ctx, 'notifications', {'user_id': 'u_santosh', 'type': 'NTF-01', 'title': 'race', 'body': 'b', 'link': None, 'read_at': None})
        if len(calls) == 1:
            q("UPDATE holidays SET name = name || '?' WHERE id = %s", hid)
        patch('holidays', h, {'optional': not h['optional']})

    in_tenant(rt, fn)
    assert len(calls) == 2
    assert q("SELECT count(*) FROM notifications WHERE title = 'race'")[0][0] == 1


def test_pushdown_rows_equal_the_view(rt: Any, monkeypatch: Any) -> None:
    from app.ctx import rows_where, rows_where_sql
    from app.db import backend as B

    loads: list[str] = []
    orig = B.Snapshot.load
    monkeypatch.setattr(B.Snapshot, 'load', lambda self, t: loads.append(t) or orig(self, t))
    rt.cache.clear()
    users = [r[0] for r in q('SELECT DISTINCT user_id FROM notifications WHERE tenant_id = %s', T)]

    def fn(s: Any, ctx: Any) -> Any:
        got = {u: rows_where_sql(ctx, 'notifications', 'user_id', u) for u in users}
        assert 'notifications' not in loads  # answered by queries
        want = {u: rows_where(ctx, 'notifications', 'user_id', u) for u in users}  # loads the table
        assert 'notifications' in loads
        return all(js(got[u]) == js(want[u]) for u in users)

    assert in_tenant(rt, fn, is_write=False)


def test_writes_after_a_lost_race_take_the_lease_first(rt: Any) -> None:
    rt._contended.clear()
    hid = q('SELECT id FROM holidays WHERE tenant_id = %s ORDER BY _ord LIMIT 1', T)[0][0]
    seen: list[bool] = []

    def lose_once(s: Any, ctx: Any) -> Any:
        seen.append(s.lease is not None)
        h = find(ctx, 'holidays', hid)
        if len(seen) == 1:
            q("UPDATE holidays SET name = name || '~' WHERE id = %s", hid)
        patch('holidays', h, {'optional': not h['optional']})

    in_tenant(rt, lose_once)
    assert seen == [False, True]
    seen.clear()
    lf = rt.stats['leased_first']
    c0 = rt.stats['conflicts']

    def plain(s: Any, ctx: Any) -> Any:
        seen.append(s.lease is not None)
        h = find(ctx, 'holidays', hid)
        patch('holidays', h, {'optional': not h['optional']})

    in_tenant(rt, plain)  # within the contention window: leased before the first attempt, no lost race possible
    assert seen == [True] and rt.stats['leased_first'] == lf + 1 and rt.stats['conflicts'] == c0
    rt._contended.clear()
