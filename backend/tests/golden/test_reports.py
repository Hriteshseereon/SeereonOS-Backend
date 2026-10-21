"""app/routes/reports.py + app/reports/* against Node golden vectors (devtools-ts/golden-gen/reports.mjs): every dataset for
several users (row scoping, grant stripping), the spec executor over many specs, saved reports CRUD, the rule-based
planner on ~200 questions per role with /ai/answer narratives, the AI data tools and the exports used elsewhere."""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import pytest

from app.ctx import rows
from app.reports import datasets as dsm
from app.reports import engine as en
from app.reports import narrative as na
from app.reports import planner as pl
from app.routes import reports as rp
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('reports.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = ['saved_reports', 'activity_log', 'notifications']
CAP = Cap()
rp.register(CAP)


def _exports(ctx: Any, a: dict) -> Any:
    nm = rp.name_of(ctx)
    ms = {m['id']: m for m in rows(ctx, 'milestones')}
    return {
        'names': [nm(u['id']) for u in rows(ctx, 'users')] + [nm(None), nm('u_nope')],
        'team': list(rp.team_of(ctx, ctx.user_id)),
        'teamTop': list(rp.team_of(ctx, a['top'])),
        'myProjects': [p['id'] for p in rp.my_projects(ctx)],
        'rowScope': rp.row_scope(ctx),
        'grants': rp.grants_of(ctx),
        'visible': [[d['id'], len(d['fields'])] for d in rp.visible_datasets(ctx)],
        'hist': [[u['id'], rp.is_timesheet_user(u), rp.week_history(ctx, u['id'], 8), rp.week_history(ctx, u['id'], 3, '2026-09-28')] for u in rows(ctx, 'users')],
        'eff': [rp.effective_week_status(ctx, w, w['week_start']) for w in rows(ctx, 'timesheet_weeks')[:40]]
        + [rp.effective_week_status(ctx, None, '2026-10-05'), rp.effective_week_status(ctx, None, '2026-10-12')],
        'codes': [rp.task_code(ms.get(t.get('milestone_id')) if t.get('milestone_id') else None, t) for t in rows(ctx, 'tasks')[:30]],
        'entities': rp.planner_entities(ctx),
    }


def _engine(ctx: Any, a: dict) -> Any:
    d = dsm.DATASET_MAP.get(a['ds'])
    presets = list(dsm.PRESET_LABEL.keys())

    def prev(p: str) -> Any:
        r = en.preset_range(p, a['today'], a['fy'], a['cap'])
        return en.previous_range(p, r, a['fy']) if r else None

    def describe(s: Any) -> Any:
        try:
            return en.describe_spec(en.sanitize_spec(s, d)['spec'])
        except Exception as e:  # noqa: BLE001
            return {'throws': str(e)}

    return {
        'presets': [[en.preset_range(p, a['today'], a['fy'], False), en.preset_range(p, a['today'], a['fy'], True)] for p in presets],
        'prev': [prev(p) for p in presets] + [en.previous_range(None, {'from': '2026-01-10', 'to': '2026-02-20', 'label': 'x', 'capped': False}, a['fy'])],
        'buckets': [[en.bucket_of(x, g, a['fy']) for x in ['2026-01-01', '2026-03-31', '2026-04-01', '2026-10-15', '2027-02-28']] for g in ['day', 'week', 'month', 'quarter', 'fy']],
        'labels': [[en.measure_label({'field': f['key'], 'agg': agg}, d) for agg in ['sum', 'avg', 'count', 'min', 'max', 'count_distinct']] for f in (d['fields'] if d else [])],
        'describe': [describe(s) for s in a['specs']],
        'sanitize': [en.sanitize_spec(s, d) for s in a['specs']],
        'filters': [en.match_filter(r, f, fd) for r, f, fd in a['filters']],
        'aggs': [en.aggregate(r, m) for r, m in a['aggs']],
        'cells': [na.fmt_cell(v, c, compact) for v, c, compact in a['cells']],
    }


FNS: dict[str, Any] = {
    'exports': _exports,
    'datasetRows': lambda ctx, a: rp.dataset_rows(ctx, a['id'])['rows'],
    'tools': lambda ctx, a: [rp.tool_project_health(ctx), rp.tool_approvals(ctx)],
    'plan': lambda ctx, a: pl.plan_question(
        a['q'],
        {
            'today': ctx.today,
            'fyStart': ctx.tenant['settings']['company.fy_start_month'],
            'role': a.get('role') or ctx.role,
            'personal': a.get('personal'),
            'datasets': rp.visible_datasets(ctx),
            'entities': rp.planner_entities(ctx),
            'me': ctx.user['name'],
        },
    ),
    'parsePeriod': lambda ctx, a: [pl.parse_period(' ' + q + ' ', ctx.today, a.get('fy') or 4) for q in a['qs']],
    'engine': _engine,
}


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(SEED['tables'])


class _FixedTime:
    """the generator's fixed clock (2026-10-15T06:30Z) for fmt_date's same-year rule"""

    @staticmethod
    def localtime(*a: Any) -> Any:
        return time.localtime(1792045800)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Ids:
    monkeypatch.setattr(sys.modules['app.domain.calendar'], 'time', _FixedTime)
    return fixed_env(monkeypatch)


# The default vectors hold every read / CRUD / engine / planner scenario, the executor for one user and the first
# /ai/answer chunk per user (~15k calls); GOLDEN_FULL=1 adds reports_full.json.gz (the rest, ~49k calls, ~40 min).
if os.environ.get('GOLDEN_FULL') == '1':
    V['scenarios'] = V['scenarios'] + load_gz('reports_full.json.gz')['scenarios']
IDX = list(range(len(V['scenarios'])))


@pytest.mark.parametrize('idx', IDX, ids=[V['scenarios'][i]['name'] for i in IDX])
def test_scenarios(rt: MemRuntime, env: Ids, idx: int) -> None:
    sc = V['scenarios'][idx]
    bad = replay(rt, env, sc, CAP, FNS, TABLES, max_bad=6)
    assert not bad, f'{sc["name"]}: {bad[:6]}'
