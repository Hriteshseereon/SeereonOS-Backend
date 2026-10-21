"""Tenant provisioning (signup) vs the TypeScript: provisionTenant() run in Node on an empty DB with placeholder base
fields (app/catalog/data/provision/tenant.json) must equal the Python port row-for-row, key order included."""

from __future__ import annotations

from app.catalog import cat
from app.db.uow import Scope
from app.seeds.access import provision_tenant
from app.seeds.master import prefix_from_name


def test_provision_tenant_matches_typescript(app):  # type: ignore[no-untyped-def]
    tpl = cat('provision/tenant')
    rt = app.state.rt
    with Scope(rt, 'fresh', None) as s:
        tenant = {
            'id': '$TENANT',
            'name': '$NAME',
            'slug': '$SLUG',
            'status': 'trial',
            'industry': 'it_services',
            'trial_ends_on': None,
            'time_zone': 'Asia/Kolkata',
            'plan_code': 'trial',
            'seats': 5,
            'created_at': '$NOW',
            'logo_data_url': None,
            'theme': dict(cat('mock/seed')['DEFAULT_THEME']),
            'settings': dict(cat('mock/seed')['DEFAULT_SETTINGS']),
            'version': 1,
        }
        n = [0]

        def base():  # type: ignore[no-untyped-def]
            b = {'id': f'$ID{n[0]}', 'tenant_id': '$TENANT', 'created_at': '$NOW', 'updated_at': '$NOW', 'created_by': None, 'version': 1}
            n[0] += 1
            return b

        out = provision_tenant(s, tenant, base)
        assert n[0] == tpl['ids']
        assert out['maker_checker'] == tpl['tenant']['maker_checker'] and out['org'] == tpl['tenant']['org']
        for table, want in tpl['tables'].items():
            got = list(s.view(table))
            assert got == want, table
            assert [list(r.keys()) for r in got] == [list(r.keys()) for r in want], table  # key order too
        assert set(tpl['tables']) == {t for t in s.accessed() if s.view(t)}


def test_prefix_from_name():
    assert [prefix_from_name(x) for x in ('Kalinga Softworks', 'Acme', 'The Big Data and AI Company Pvt Ltd', '!!!', 'ab cd ef gh ij')] == [
        'KS',
        'ACM',
        'BDAC',
        'EMP',
        'ACEG',
    ]
