"""Tenant cache LRU (tenant count and bytes), frozen containers, and the production-mode (unfrozen) copy-on-write of
table lists in the request scope."""

import pytest

from app.db.cache import TableEntry, TenantCache
from app.frozen import FrozenDict, FrozenError, FrozenList, freeze, thaw


def entry(v, n=3, size=100):  # type: ignore[no-untyped-def]
    return TableEntry(v, [{'id': f'r{i}'} for i in range(n)], lambda r: r['id'], nbytes=size)


def test_lru_by_tenants_and_bytes():
    c = TenantCache(max_tenants=2, max_bytes=10_000)
    c.set('a', 't', entry(1))
    c.set('b', 't', entry(1))
    assert c.get('a', 't', 1) is not None  # touch a
    c.set('c', 't', entry(1))  # evicts b (least recently used)
    assert c.get('b', 't', 1) is None and c.get('a', 't', 1) is not None and c.get('c', 't', 1) is not None
    assert c.get('a', 't', 2) is None  # version mismatch = miss
    c2 = TenantCache(max_tenants=10, max_bytes=1000)
    c2.set('a', 't', entry(1, size=600))
    c2.set('b', 't', entry(1, size=600))  # over the byte budget: a goes
    assert c2.get('a', 't', 1) is None and c2.get('b', 't', 1) is not None
    c2.set('b', 't', entry(0))  # never replace a newer entry with an older one
    assert c2.get('b', 't', 1) is not None
    c2.invalidate('b', ['t'])
    assert c2.stats()['tenants'] == 0


def test_entry_indexes_are_first_match_and_groups_in_order():
    e = TableEntry(1, [{'id': 'a', 'k': 1}, {'id': 'b', 'k': 1}, {'id': 'c', 'k': 2}], lambda r: r['id'])
    assert e.index('k')[1]['id'] == 'a' and [r['id'] for r in e.group('k')[1]] == ['a', 'b'] and e.pk_index()['c']['k'] == 2


def test_frozen_containers():
    f = freeze({'a': [1, {'b': 2}]})
    assert isinstance(f, FrozenDict) and isinstance(f['a'], FrozenList)
    for op in (lambda: f.__setitem__('x', 1), lambda: f['a'].append(3), lambda: f['a'][1].update(c=1), lambda: f.pop('a'), lambda: f['a'].sort()):
        with pytest.raises(FrozenError):
            op()
    t = thaw(f)
    t['a'][1]['b'] = 3
    assert f['a'][1]['b'] == 2 and {**f}['a'] is f['a'] and dict(f) == {'a': [1, {'b': 2}]}


def test_scope_lists_copy_on_write_unfrozen(app):  # type: ignore[no-untyped-def]
    """production mode (no freezing): a list handed out by view() never changes behind the caller's back"""
    from app.db.uow import Scope

    rt = app.state.rt
    old = rt.freeze_rows
    rt.freeze_rows = False
    try:
        with Scope(rt, 'tenant', 'tn_seereon') as s:
            base = s.view('holidays')
            n = len(base)
            s.append('holidays', {'id': 'h_new', 'tenant_id': 'tn_seereon'})
            assert len(base) == n and len(s.view('holidays')) == n + 1
            v2 = s.view('holidays')
            s.set_rows('holidays', [r for r in v2 if r['id'] != 'h_new'])
            assert len(v2) == n + 1 and len(s.view('holidays')) == n
            assert s.state('holidays').entry.rows is base and len(base) == n  # the cached list itself was never touched
    finally:
        rt.freeze_rows = old
