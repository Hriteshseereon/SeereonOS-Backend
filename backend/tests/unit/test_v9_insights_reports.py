"""v9 (A4): insight views with charts (app/routes/insight_views.py) and report types (app/reports/types.py), run on the
golden engine seed in memory: every view and every report type for each role renders, numbers agree with their own
tables/KPIs, permissions are enforced (salary types need salary access, finance types need finance access), configs
are sanitised, and the v5 /insights views are returned unchanged inside the new payload."""

from __future__ import annotations

from typing import Any

import pytest

import app.engine.costing  # noqa: F401  (installs the project-metrics override, as in the app)
from app.catalog import cat
from app.domain.permission_catalog import effective_permissions
from app.errors import ApiError
from app.reports import types as rt
from app.routes import dashboards as da
from app.routes import insight_views as iv
from tests.golden.memdb import MemRuntime, load_gz, make_ctx, tenant_scope

SEED = load_gz('engine_seed.json.gz')
ROLES = {r['key']: r['permissions'] for r in cat('domain/permissionCatalog')['DEFAULT_ROLES']}
TODAY = '2026-10-15'
USERS = {'ceo': 'u_santosh', 'pm': 'u_subhankar', 'hr': 'u_priya', 'sales': 'u_baishali', 'emp': 'u_hritesh'}
ROLE_OF = {'ceo': 'ceo', 'pm': 'project_manager', 'hr': 'hr_manager', 'sales': 'sales_manager', 'emp': 'employee'}


@pytest.fixture(scope='module')
def mem() -> MemRuntime:
    return MemRuntime(SEED['tables'])


def _ctx(s: Any, who: str, extra: tuple[str, ...] = (), revoke: tuple[str, ...] = ()) -> Any:
    perms = effective_permissions(ROLES[ROLE_OF[who]], extra, revoke)
    return make_ctx(s, USERS[who], list(perms), TODAY)


def _err(f: Any) -> ApiError:
    with pytest.raises(ApiError) as e:
        f()
    return e.value


@pytest.mark.parametrize('who', ['ceo', 'pm', 'hr', 'sales'])
def test_every_insight_view_renders(mem: MemRuntime, who: str) -> None:
    with tenant_scope(mem, 'tn_seereon') as s:
        ctx = _ctx(s, who)
        cat_ = iv.catalogue(ctx)
        ids = [v['id'] for v in cat_['views']]
        assert ids, who
        assert cat_['default'] in ids
        for v in ids:
            d = iv.view_of(ctx, v)
            assert d['question'] and d['finding'], v
            assert d['charts'], v
            for c in d['charts']:
                assert c['title'], (v, c)
                if c['kind'] in ('column', 'bar', 'line'):
                    assert all(len(x['values']) == len(c['categories']) for x in c['series']), (v, c['id'])
                if c['kind'] == 'heatmap':
                    assert all(len(r['cells']) == len(c['cols']) for r in c['rows']), v
            if v in iv.LEGACY:  # the v5 payload is untouched inside the new one
                legacy = da.insight(ctx, v)
                assert {k: d[k] for k in legacy} == legacy, v


def test_insight_permissions(mem: MemRuntime) -> None:
    with tenant_scope(mem, 'tn_seereon') as s:
        pm = _ctx(s, 'pm')
        assert 'revenue' not in iv.allowed_views(pm)  # finance only
        assert _err(lambda: iv.view_of(pm, 'revenue')).status == 403
        assert _err(lambda: iv.view_of(pm, 'nope')).status == 404
        emp = _ctx(s, 'emp')
        assert _err(lambda: iv.catalogue(emp)).status == 403
        sales = _ctx(s, 'sales')
        assert 'funnel' in iv.allowed_views(sales) and 'portfolio' not in iv.allowed_views(sales)


def test_heatmap_and_funnel_numbers(mem: MemRuntime) -> None:
    with tenant_scope(mem, 'tn_seereon') as s:
        ctx = _ctx(s, 'ceo')
        f = iv.view_of(ctx, 'funnel')
        st = f['charts'][0]['stages']
        assert [x['reached'] for x in st] == sorted([x['reached'] for x in st], reverse=True)  # cumulative, never grows
        assert sum(x['count'] for x in st) == st[0]['reached']
        assert all(x['count'] == r['count'] and x['value'] == r['value'] for x, r in zip(st, f['rows']))
        u = iv.view_of(ctx, 'utilisation')
        hm = u['charts'][0]
        assert hm['split'] == 8 and len(hm['cols']) == 12
        assert len(hm['rows']) == len(u['rows'])


@pytest.mark.parametrize('who', ['ceo', 'pm', 'hr', 'sales'])
def test_every_report_type_runs(mem: MemRuntime, who: str) -> None:
    with tenant_scope(mem, 'tn_seereon') as s:
        ctx = _ctx(s, who)
        catalogue = rt.catalogue(ctx)
        visible = [t['id'] for c in catalogue['categories'] for t in c['types']]
        locked = [t['id'] for c in catalogue['categories'] for t in c['locked']]
        assert sorted(visible + locked) == sorted(t['id'] for t in rt.TYPES)
        for tid in visible:
            d = rt.describe(ctx, rt.TYPE_MAP[tid], with_options=True)
            assert d['default_fields'], tid
            r = rt.run(ctx, tid, {})
            assert r['finding'] and r['kpis'], tid
            assert [c['key'] for c in r['columns']] == r['config']['fields'] or r['grouped'], tid
            # listing totals are the sums of the listed rows (when nothing is cut off)
            if not r['grouped'] and r['row_count'] <= rt.MAX_TABLE_ROWS:
                for k, v in r['totals'].items():
                    s_ = sum(x[k] for x in r['rows'] if isinstance(x.get(k), (int, float)))
                    assert abs(s_ - v) < 0.01, (tid, k)
            # grouped: every group, then all rows again in the total
            g = d['groups'][0]['value'] if d['groups'] else None
            if g:
                rg = rt.run(ctx, tid, {'group_by': g})
                assert rg['grouped'] and sum(x['count'] for x in rg['rows']) == rg['totals']['count'] == rg['source_rows'], tid
        for tid in locked:
            assert _err(lambda tid=tid: rt.run(ctx, tid, {})).status == 403


def test_salary_types_need_salary_access(mem: MemRuntime) -> None:
    with tenant_scope(mem, 'tn_seereon') as s:
        hr = _ctx(s, 'hr')
        assert rt.run(hr, 'payroll_register', {})['row_count'] >= 0
        no_sal = _ctx(s, 'hr', revoke=('cost_rates.view',))
        e = _err(lambda: rt.run(no_sal, 'payroll_register', {}))
        assert e.status == 403 and 'salary access' in e.message
        assert _err(lambda: rt.run(no_sal, 'ctc_rates', {})).status == 403
        pm = _ctx(s, 'pm')
        assert _err(lambda: rt.run(pm, 'pnl', {})).status == 403
        assert _err(lambda: rt.run(pm, 'nope', {})).status == 404


def test_config_is_sanitised_and_filters_apply(mem: MemRuntime) -> None:
    with tenant_scope(mem, 'tn_seereon') as s:
        ctx = _ctx(s, 'ceo')
        r = rt.run(ctx, 'tasks', {'fields': ['title', 'bogus', 'title', 'owner'], 'group_by': 'nope', 'chart': 'pie3d', 'period': {'preset': 'all'}})
        assert r['config']['fields'] == ['title', 'owner'] and r['config']['group_by'] is None and r['config']['chart'] == 'auto'
        allr = r['row_count']
        d = rt.describe(ctx, rt.TYPE_MAP['tasks'], with_options=True)
        proj = d['options']['project'][0]
        f = rt.run(ctx, 'tasks', {'period': {'preset': 'all'}, 'filters': {'project': [proj]}, 'fields': ['title', 'project']})
        assert 0 < f['row_count'] <= allr and all(x['project'] == proj for x in f['rows'])
        assert any(a['key'] == 'project' and a['value'] == proj for a in f['filters_applied'])
        st = rt.run(ctx, 'tasks', {'period': {'preset': 'all'}, 'filters': {'status': ['done']}, 'fields': ['title', 'status']})
        assert all(x['status'] == 'Done' for x in st['rows'])
        e = _err(lambda: rt.run(ctx, 'tasks', {'period': {'from': '2026-10-20', 'to': '2026-10-01'}}))
        assert e.status == 422 and e.field == 'period'


def test_pnl_matches_its_kpis(mem: MemRuntime) -> None:
    with tenant_scope(mem, 'tn_seereon') as s:
        ctx = _ctx(s, 'ceo')
        r = rt.run(ctx, 'pnl', {'group_by': 'kind', 'period': {'preset': 'this_fy'}})
        k = {x['key']: x['value'] for x in r['kpis']}
        assert k['net'] == k['income'] - k['cost']
        assert r['totals']['sum_income'] == k['income'] and r['totals']['sum_cost'] == k['cost']
        assert r['chart'] and r['chart']['series'][0]['key'] == 'income'
