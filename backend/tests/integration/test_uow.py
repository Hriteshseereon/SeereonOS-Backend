"""Unit of work + cache against PostgreSQL: frozen cached rows, copy-on-write overlay, flush (insert/update/delete),
read-set validation and the retry semantics of server/src/runtime.ts, errors after writes, cache refill from the
request's own diff, coherence across processes, mass assignment, cross-tenant guard, mapping round-trip of the seed."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import psycopg
import pytest

from app.ctx import build_ctx, find, insert, now_iso, patch, rows, update
from app.errors import ApiError
from app.frozen import FrozenError
from tests.conftest import APP, OWNER

pytestmark = pytest.mark.usefixtures('fresh')


def q(sql: str, *params):  # type: ignore[no-untyped-def]
    with psycopg.connect(OWNER, autocommit=True) as c:
        cur = c.execute(sql, params or None)
        try:
            return cur.fetchall()
        except psycopg.ProgrammingError:
            return None


def in_tenant(rt, fn, user='u_santosh', tenant='tn_seereon', is_write=True):  # type: ignore[no-untyped-def]
    def body(s):  # type: ignore[no-untyped-def]
        ctx = build_ctx(s.find_by('users', 'id', user), tenant, {})
        return fn(s, ctx)

    return rt.run_scoped('tenant', tenant, body, is_write=is_write)


@pytest.fixture()
def rt(app):  # type: ignore[no-untyped-def]
    return app.state.rt


def test_cached_rows_are_frozen(rt):  # type: ignore[no-untyped-def]
    def fn(s, ctx):  # type: ignore[no-untyped-def]
        u = rows(ctx, 'users')[0]
        with pytest.raises(FrozenError):
            u['name'] = 'x'
        with pytest.raises(FrozenError):
            rows(ctx, 'users').append({})
        with pytest.raises(FrozenError):
            rows(ctx, 'users').sort(key=lambda r: r['id'])
        with pytest.raises(FrozenError):
            u['prefs']['x'] = 1  # nested values are frozen too
        return True

    assert in_tenant(rt, fn, is_write=False)


def test_patch_is_copy_on_write_and_flushes(rt):  # type: ignore[no-untyped-def]
    hid = q("SELECT id FROM holidays WHERE tenant_id = 'tn_seereon' ORDER BY _ord LIMIT 1")[0][0]

    def fn(s, ctx):  # type: ignore[no-untyped-def]
        old = find(ctx, 'holidays', hid)
        pos = s.view('holidays').index(old)
        new = patch('holidays', old, {'name': 'Renamed'})
        assert old['name'] != 'Renamed' and new['name'] == 'Renamed'
        assert s.view('holidays')[pos] is new  # same position
        assert s.state('holidays').entry.rows[pos] is old  # the cached entry is untouched
        return new

    in_tenant(rt, fn)
    assert q('SELECT name FROM holidays WHERE id = %s', hid)[0][0] == 'Renamed'


def test_cache_refill_from_own_diff(rt, monkeypatch):  # type: ignore[no-untyped-def]
    def add(s, ctx):  # type: ignore[no-untyped-def]
        return insert(ctx, 'holidays', {'date': '2031-01-01', 'name': 'Future day', 'optional': False})

    in_tenant(rt, lambda s, c: len(rows(c, 'holidays')), is_write=False)  # warm the cache
    row = in_tenant(rt, add)
    v_db = q("SELECT version FROM tenant_table_versions WHERE tenant_id = 'tn_seereon' AND table_name = 'holidays'")[0][0]
    entry = rt.cache.get('tn_seereon', 'holidays', v_db)
    assert entry is not None and any(r['id'] == row['id'] for r in entry.rows)
    from app.db import backend as B

    loads = []
    orig = B.Snapshot.load
    monkeypatch.setattr(B.Snapshot, 'load', lambda self, t: loads.append(t) or orig(self, t))
    n = in_tenant(rt, lambda s, c: len(rows(c, 'holidays')), is_write=False)
    assert 'holidays' not in loads and n == q("SELECT count(*) FROM holidays WHERE tenant_id = 'tn_seereon'")[0][0]


def test_lost_race_reruns_under_lease(rt):  # type: ignore[no-untyped-def]
    rt._contended.clear()  # no recent lost race: the first attempt is optimistic (adaptive leasing)
    calls = []
    hid = q("SELECT id FROM holidays WHERE tenant_id = 'tn_seereon' ORDER BY _ord LIMIT 1")[0][0]

    def fn(s, ctx):  # type: ignore[no-untyped-def]
        calls.append(s.lease is not None)
        h = find(ctx, 'holidays', hid)
        if len(calls) == 1:  # another writer commits between our read and our flush
            q("UPDATE holidays SET name = name || '!' WHERE id = %s", hid)
        patch('holidays', h, {'optional': not h['optional']})
        return h['name']

    before = rt.stats['conflicts']
    name = in_tenant(rt, fn)
    assert calls == [False, True] and rt.stats['conflicts'] == before + 1
    assert name.endswith('!')  # the re-run saw the other writer's change


def test_conflict_retry_after_three_losses(rt):  # type: ignore[no-untyped-def]
    rt._contended.clear()
    hid = q("SELECT id FROM holidays WHERE tenant_id = 'tn_seereon' ORDER BY _ord LIMIT 1")[0][0]

    def fn(s, ctx):  # type: ignore[no-untyped-def]
        h = find(ctx, 'holidays', hid)
        with psycopg.connect(OWNER, autocommit=True) as c:  # out-of-band writes do not take the tenant lock
            c.execute("UPDATE holidays SET name = name || '.' WHERE id = %s", [hid])
        patch('holidays', h, {'optional': not h['optional']})

    with pytest.raises(ApiError) as e:
        in_tenant(rt, fn)
    assert e.value.status == 409 and e.value.code == 'CONFLICT_RETRY'


def test_error_after_write_keeps_the_write_and_500_does_not(rt):  # type: ignore[no-untyped-def]
    def api_error(s, ctx):  # type: ignore[no-untyped-def]
        insert(ctx, 'notifications', {'user_id': 'u_santosh', 'type': 'NTF-01', 'title': 'kept', 'body': 'b', 'link': None, 'read_at': None})
        raise ApiError(422, 'VALIDATION', 'after the write')

    def crash(s, ctx):  # type: ignore[no-untyped-def]
        insert(ctx, 'notifications', {'user_id': 'u_santosh', 'type': 'NTF-01', 'title': 'dropped', 'body': 'b', 'link': None, 'read_at': None})
        raise ZeroDivisionError

    with pytest.raises(ApiError):
        in_tenant(rt, api_error)
    with pytest.raises(ZeroDivisionError):
        in_tenant(rt, crash)
    titles = [r[0] for r in q("SELECT title FROM notifications WHERE title IN ('kept', 'dropped')")]
    assert titles == ['kept']


def test_get_without_write_intent_does_not_flush(rt):  # type: ignore[no-untyped-def]
    f0 = rt.stats['flushes']
    in_tenant(rt, lambda s, c: [r['id'] for r in rows(c, 'projects')], is_write=False)
    in_tenant(rt, lambda s, c: None, is_write=True)  # a write request that changed nothing: no transaction either
    assert rt.stats['flushes'] == f0


def test_update_ignores_protected_columns(rt):  # type: ignore[no-untyped-def]
    hid = q("SELECT id FROM holidays WHERE tenant_id = 'tn_seereon' ORDER BY _ord LIMIT 1")[0][0]
    v0 = q('SELECT version FROM holidays WHERE id = %s', hid)[0][0]

    def fn(s, ctx):  # type: ignore[no-untyped-def]
        return update(
            ctx,
            'holidays',
            hid,
            {'id': 'hacked', 'tenant_id': 'tn_kalinga', 'version': 99, 'created_by': 'x', 'created_at': '2000-01-01T00:00:00.000Z', 'name': 'Patched'},
        )

    r = in_tenant(rt, fn)
    assert r['id'] == hid and r['tenant_id'] == 'tn_seereon' and r['version'] == v0 + 1 and r['name'] == 'Patched'
    assert q('SELECT tenant_id, version, name FROM holidays WHERE id = %s', hid)[0] == ('tn_seereon', v0 + 1, 'Patched')
    with pytest.raises(ApiError) as e:
        in_tenant(rt, lambda s, c: update(c, 'holidays', hid, {'name': 'x'}, if_match=1))
    assert e.value.code == 'STALE'


def test_cross_tenant_row_is_refused(rt):  # type: ignore[no-untyped-def]
    def fn(s, ctx):  # type: ignore[no-untyped-def]
        s.append(
            'holidays',
            {
                'id': 'h_x',
                'tenant_id': 'tn_kalinga',
                'created_at': now_iso(),
                'updated_at': now_iso(),
                'created_by': None,
                'version': 1,
                'date': '2031-02-02',
                'name': 'x',
                'optional': False,
            },
        )

    with pytest.raises(RuntimeError, match='Cross-tenant row'):
        in_tenant(rt, fn)
    assert q("SELECT count(*) FROM holidays WHERE id = 'h_x'")[0][0] == 0


def test_absent_vs_null_round_trip(rt):  # type: ignore[no-untyped-def]
    from app.js import UNDEF

    def fn(s, ctx):  # type: ignore[no-untyped-def]
        a = insert(ctx, 'notifications', {'user_id': 'u_santosh', 'type': 'NTF-01', 'title': 'a', 'body': 'b', 'link': UNDEF, 'read_at': None})
        return a['id']

    nid = in_tenant(rt, fn)
    rt.cache.clear()
    got = in_tenant(rt, lambda s, c: dict(find(c, 'notifications', nid)), is_write=False)
    assert 'link' not in got and got['read_at'] is None


def test_other_process_write_is_seen(rt):  # type: ignore[no-untyped-def]
    """a second process (own pools + cache) writes; this process's cached table is refreshed by the version check"""
    hid = q("SELECT id FROM holidays WHERE tenant_id = 'tn_seereon' ORDER BY _ord LIMIT 1")[0][0]
    in_tenant(rt, lambda s, c: find(c, 'holidays', hid)['name'], is_write=False)  # cached here
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {os.getcwd()!r})
        from app.config import load_config
        from app.db.pool import Pools
        from app.db.backend import PgBackend
        from app.db.cache import TenantCache
        from app.router import Router
        from app.runtime import Runtime
        from app.ctx import build_ctx, find, patch
        pools = Pools({APP!r}, 2)
        b = PgBackend(pools); b.init()
        rt = Runtime(b, Router(), TenantCache())
        def fn(s):
            ctx = build_ctx(s.find_by('users', 'id', 'u_santosh'), 'tn_seereon', {{}})
            patch('holidays', find(ctx, 'holidays', {hid!r}), {{'name': 'From process B'}})
        rt.run_scoped('tenant', 'tn_seereon', fn, is_write=True)
        pools.close()
    """)
    subprocess.run([sys.executable, '-c', script], check=True, env={**os.environ, 'TZ': 'Asia/Kolkata'})
    assert in_tenant(rt, lambda s, c: find(c, 'holidays', hid)['name'], is_write=False) == 'From process B'
    q("UPDATE holidays SET name = 'Out of band' WHERE id = %s", hid)  # plain SQL bumps the version too (triggers)
    assert in_tenant(rt, lambda s, c: find(c, 'holidays', hid)['name'], is_write=False) == 'Out of band'


def test_seed_round_trips_key_for_key(app, seed):  # type: ignore[no-untyped-def]
    """every seed row -> PostgreSQL -> row_from_tuple equals the seed object key-for-key (types included); timestamps
    compare as instants (the one documented normalisation) — the B1 fidelity proof, on the Python mapping"""
    from app.db.mapping import load_meta, row_from_tuple
    from app.js import date_parse

    data = seed['data']
    problems = []

    def same(e, a, ts=False):  # type: ignore[no-untyped-def]
        if ts and isinstance(e, str) and isinstance(a, str):
            return date_parse(e) == date_parse(a)
        if isinstance(e, bool) or isinstance(a, bool):
            return type(e) is type(a) and e == a
        if isinstance(e, (int, float)) and isinstance(a, (int, float)):
            return e == a
        if isinstance(e, dict) and isinstance(a, dict):
            return e.keys() == a.keys() and all(same(e[k], a[k]) for k in e)
        if isinstance(e, list) and isinstance(a, list):
            return len(e) == len(a) and all(same(x, y) for x, y in zip(e, a))
        return type(e) is type(a) and e == a

    with psycopg.connect(OWNER, autocommit=True) as c:
        from app.db.pool import install_loaders

        install_loaders()
        meta = load_meta(c)
        n = 0
        for t, tm in meta.tables.items():
            exp = data.get(t) or []
            act = [row_from_tuple(tm, r) for r in c.execute(tm.select_sql + ' ORDER BY _ord').fetchall()]  # type: ignore[arg-type]
            if len(exp) != len(act):
                problems.append(f'{t}: {len(exp)} != {len(act)}')
                continue
            ts = {k for k, cm in tm.columns.items() if cm.kind == 'timestamp'}
            for e, a in zip(exp, act):
                n += 1
                if e.keys() != a.keys():
                    problems.append(f'{t} {e.get("id")}: keys {sorted(set(e) ^ set(a))}')
                    continue
                for k in e:
                    if not same(e[k], a[k], k in ts):
                        problems.append(f'{t} {e.get("id")}.{k}: {e[k]!r} != {a[k]!r}')
    assert not problems, problems[:20]
    assert n > 3000


def test_edit_gives_js_identity_semantics(rt):  # type: ignore[no-untyped-def]
    from app.ctx import edit

    tid = q("SELECT id FROM tenants WHERE id = 'tn_seereon'")[0][0]

    def fn(s, ctx):  # type: ignore[no-untyped-def]
        t = edit('tenants', s.find_by('tenants', 'id', tid))
        t['theme']['accent'] = '#123456'  # nested in-place change
        t['settings']['calendar.weekly_off'].append(6)
        same = patch('tenants', t, {'version': t['version'] + 1})  # patch() on an owned row works in place
        assert same is t
        assert ctx.tenant is t  # ctx.tenant follows the current row
        h = edit('holidays', rows(ctx, 'holidays')[0])
        h2 = update(ctx, 'holidays', h['id'], {'name': 'Edited'})  # update() too
        assert h2 is h and h['version'] == h2['version']
        return t['version']

    v = in_tenant(rt, fn)
    accent, off, ver = q("SELECT theme->>'accent', settings->'calendar.weekly_off', version FROM tenants WHERE id = %s", tid)[0]
    assert accent == '#123456' and off[-1] == 6 and ver == v
