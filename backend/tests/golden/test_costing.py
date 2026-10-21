"""app/engine/costing.py against Node golden vectors (devtools-ts/golden-gen/costing.mjs) on the full demo seed.

Each case replays the generator's calls in the same order on a fresh in-memory tenant scope (per-ctx memo semantics
make a few results order dependent, exactly like the TS)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

import app.ctx
from app.ctx import Req, has, patch, rows
from app.engine import analytics as an
from app.engine import costing as co
from app.js import UNDEF, js_str
from tests.golden.memdb import DetIds, MemRuntime, jnorm, jsafe, load_gz, make_ctx, same, tenant_scope

V = load_gz('costing.json.gz')
SEED = load_gz('engine_seed.json.gz')
SAME_YEAR = V['year'] == time.localtime().tm_year  # fmt_date omits the current year (messages)
pytestmark = pytest.mark.skipif(not SAME_YEAR, reason='vectors computed in another calendar year (fmt_date output); regenerate')


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(SEED['tables'])


@pytest.fixture(autouse=True)
def det_ids(monkeypatch: pytest.MonkeyPatch) -> DetIds:
    ids = DetIds()
    monkeypatch.setattr(app.ctx, 'uid', ids)
    return ids


class _Cap:
    def __init__(self) -> None:
        self.h: dict[tuple[str, str], Any] = {}

    def _add(self, m: str, p: str, h: Any, **_: Any) -> None:
        self.h[(m, p)] = h

    def get(self, p: str, h: Any, **o: Any) -> None:
        self._add('GET', p, h)

    def post(self, p: str, h: Any, **o: Any) -> None:
        self._add('POST', p, h)

    def put(self, p: str, h: Any, **o: Any) -> None:
        self._add('PUT', p, h)


CAP = _Cap()
co.register_costing_routes(
    CAP,
    co.CostingDeps(
        can_view=lambda ctx, p: True,
        can_edit=lambda ctx, p: has(ctx, 'projects.manage_all') or (has(ctx, 'projects.edit') and p['pm_id'] == ctx.user_id),
    ),
)


def call(ctx: Any, method: str, pattern: str, params: dict | None = None, query: dict | None = None, body: Any = UNDEF) -> dict:
    before = len(ctx.warnings)
    req = Req(params or {}, query or {}, body, UNDEF, ctx)
    res = jsafe(lambda: CAP.h[(method, pattern)](req))
    return {'res': res, 'warnings': ctx.warnings[before:]}


def _diff(got: Any, want: Any, path: str = '$', out: list | None = None) -> list:
    out = [] if out is None else out
    if len(out) > 8:
        return out
    if isinstance(want, dict) and isinstance(got, dict) and 'throws' in want and 'throws' in got:
        return out  # a TS exception matches any Python exception (500 either way)
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got)):
            if k not in got:
                out.append(f'{path}.{k}: missing (want {str(want[k])[:80]})')
            elif k not in want:
                out.append(f'{path}.{k}: extra {str(got[k])[:80]}')
            else:
                _diff(got[k], want[k], f'{path}.{k}', out)
    elif isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            out.append(f'{path}: len {len(got)} != {len(want)}')
        for i, (g, w) in enumerate(zip(got, want, strict=False)):
            _diff(g, w, f'{path}[{i}]', out)
    elif got != want and not (isinstance(want, dict) and 'throws' in want and isinstance(got, dict) and 'throws' in got):
        out.append(f'{path}: {str(got)[:120]} != {str(want)[:120]}')
    return out


def test_read_cases(rt: MemRuntime, det_ids: DetIds) -> None:
    bad: list = []
    for c in V['cases']:
        det_ids.n = 0
        R: dict[str, Any] = {}
        with tenant_scope(rt, c['tenant']) as s:
            ctx = make_ctx(s, c['user'], c['perms'], c['today'])

            def projects() -> list:
                return rows(ctx, 'projects')  # noqa: B023

            R['rates'] = jsafe(lambda: co.cost_rate_days(ctx))  # noqa: B023
            R['an'], R['row'], R['sum'] = {}, {}, {}
            for p in projects():
                R['an'][p['id']] = jsafe(lambda: co.public_analysis(ctx, co.analyse(ctx, p)))  # noqa: B023
                R['row'][p['id']] = jsafe(lambda: co.project_row_info(ctx, p))  # noqa: B023
                R['sum'][p['id']] = jsafe(lambda: co.project_summary(ctx, p))  # noqa: B023
            R['portfolio'] = [jsafe(lambda: co.portfolio(ctx, projects(), sc)) for sc in ('active', 'open', 'all')]  # noqa: B023
            routes = []
            for p in projects():
                routes.append(call(ctx, 'GET', '/projects/:id/costing', {'id': p['id']}))
                routes.append(call(ctx, 'GET', '/projects/:id/summary', {'id': p['id']}))
            routes.append(call(ctx, 'GET', '/projects/:id/costing', {'id': 'p_nope'}))
            routes.append(call(ctx, 'GET', '/finance/margins', query={'scope': 'all'}))
            routes.append(call(ctx, 'GET', '/finance/margins', query={'scope': 'x'}))
            routes.append(call(ctx, 'GET', '/costing/rates'))
            routes.append(call(ctx, 'GET', '/projects/next-code'))
            routes.append(call(ctx, 'GET', '/projects/calendar-summary', query={'from': '2026-10-01', 'to': '2027-03-31'}))
            routes.append(call(ctx, 'GET', '/projects/calendar-summary', query={'from': '2026-10-01'}))
            R['routes'] = routes
            R['metrics'] = {p['id']: jsafe(lambda: an.project_metrics(ctx, p)) for p in projects()}  # noqa: B023
        for k in R:
            d = _diff(R[k], c['r'][k])
            if d:
                bad.append((c['today'], c['tenant'], c['user'], c['variant'], k, d))
    assert not bad, f'{len(bad)} mismatching groups; first: {bad[:4]}'


def _pieces(ctx: Any) -> list:
    out: list = []

    def push(name: str, f: Any) -> None:
        out.append({'name': name, 'v': jsafe(f)})

    users = [u['id'] for u in rows(ctx, 'users')]
    dates = ['2026-04-01', '2026-04-15', '2026-09-30', '2026-10-15', '2027-02-28', '2025-12-31', '2026-02-01']
    for u in users:
        for d in dates:
            push(f'cph {u} {d}', lambda u=u, d=d: [co.cost_per_hour_for(ctx, u, d), co.cost_per_day_for(ctx, u, d)])
    for a in rows(ctx, 'allocations'):
        push(
            f'rc {a["id"]}',
            lambda a=a: [
                co.range_cost(ctx, a['user_id'], a['pct'], a['from_on'], a['to_on']),
                co.person_range_cost(ctx, a['user_id'], a['pct'], a['from_on'], a['to_on']),
            ],
        )
    push(
        'rc edge',
        lambda: [
            co.range_cost(ctx, users[0], 50, '', '2026-10-10'),
            co.range_cost(ctx, users[0], 0, '2026-10-01', '2026-10-10'),
            co.range_cost(ctx, 'u_nobody', 100, '2026-10-01', '2026-12-10'),
            co.range_cost(ctx, users[1], 33, '2026-10-05', '2026-10-01'),
        ],
    )
    ranges = [
        ('2026-10-01', '2026-10-31'), ('2026-04-01', '2027-03-31'), ('2026-12-20', '2027-01-10'), ('2026-10-15', '2026-10-15'), ('2026-10-18', '2026-10-17'),
        ('2026-02-29', '2026-03-10'), ('2025-01-01', '2029-12-31'), ('', ''), ('2026-10-11', '2026-10-11'),
    ]  # fmt: skip
    for f, t in ranges:
        push(f'range {f} {t}', lambda f=f, t=t: [co.wd(ctx, f, t), co.slip_days(ctx, f or '2026-10-01', t or '2026-10-01'), co.calendar_summary(ctx, f, t)])
    for ctc in (0, -5, 6000000, 12345678.9, 25000000):
        for pct in (0, 1, 50, 100, 33.3):
            for f, t in ranges[:4]:
                push(f'ctc {ctc} {pct} {f} {t}', lambda ctc=ctc, pct=pct, f=f, t=t: co.range_cost_at_ctc(ctx, ctc, pct, f, t))
    combos = [
        ('markup', None, None), ('markup', 15, None), ('markup', 12.345, None), ('markup', -100, None), ('price', None, 15000000), ('price', 10, 0),
        ('price', None, -1), ('price', 5, None), ('markup', 0.005, 99.5),
    ]  # fmt: skip
    for total in (0, 1, 99999, 12345678, 100000000):
        for mode, mk, pr in combos:
            push(f'pm {total} {mode} {js_str(mk)} {js_str(pr)}', lambda total=total, mode=mode, mk=mk, pr=pr: co.price_and_markup(total, mode, mk, pr))
    ms_lists: list[list[dict]] = [
        [],
        [{'name': 'A', 'billing_amount_paise': 100000}],
        [{'name': 'A', 'billing_amount_paise': 300000}, {'name': 'B', 'billing_amount_paise': 100000}, {'name': 'C', 'billing_amount_paise': 600000}],
        [{'name': 'A', 'billing_amount_paise': 0}, {'name': 'B', 'billing_amount_paise': 100000}, {'name': 'C', 'billing_amount_paise': 1}],
        [{'name': f'M{i}', 'billing_amount_paise': (i + 1) * 1000} for i in range(7)],
    ]
    for lst in ms_lists:
        for f, t in ranges:
            push(f'spread {len(lst)} {f} {t}', lambda lst=lst, f=f, t=t: co.spread_milestones(ctx, f, t, lst))
    push('spread short', lambda: co.spread_milestones(ctx, '2026-10-11', '2026-10-12', ms_lists[2]))
    push('spread 1day', lambda: co.spread_milestones(ctx, '2026-10-14', '2026-10-14', ms_lists[4]))
    for d in dates:
        push(f'avgwd {d}', lambda d=d: co.avg_working_days_per_month(ctx, d))
    for ym in ('2026-02', '2026-10', '2026-12', '2027-01'):
        push(f'wdm {ym}', lambda ym=ym: co.wd_month(ctx, ym))
    for p in rows(ctx, 'projects'):
        push(f'pct {p["id"]}', lambda p=p: co.pct_complete([t for t in rows(ctx, 'tasks') if t['project_id'] == p['id']], p))
    push('minHours/loading', lambda: [co.min_hours(ctx), co.loading_pct(ctx), co.can_plan_cost(ctx)])
    return out


def test_pieces(rt: MemRuntime) -> None:
    bad = []
    for P in V['pieces']:
        with tenant_scope(rt, P['tenant']) as s:
            ctx = make_ctx(s, P['user'], P['perms'], P['today'])
            got = _pieces(ctx)
        assert [g['name'] for g in got] == [w['name'] for w in P['r']]
        for g, w in zip(got, P['r'], strict=True):
            if not same(g['v'], w['v']):
                bad.append((P['today'], P['tenant'], g['name'], g['v'], w['v']))
    assert not bad, f'{len(bad)} mismatches; first: {bad[:5]}'


def _in(x: Any) -> Any:
    return UNDEF if x == '$undef' else x


def test_validators() -> None:
    for c in V['additional']:
        x = _in(c['x'])
        assert same(jsafe(lambda: co.clean_additional(x)), c['clean']), c['x']  # noqa: B023
        assert same(jsafe(lambda: (co.validate_additional(x), 'ok')[1]), c['valid']), c['x']  # noqa: B023
    for c in V['docs']:
        x = _in(c['x'])
        assert same(jsafe(lambda: co.validate_create_documents(x)), c['v']), c['x']  # noqa: B023


def test_preview(rt: MemRuntime) -> None:
    bad = []
    for e in V['preview']:
        with tenant_scope(rt, e['tenant']) as s:
            ctx = make_ctx(s, e['user'], e['perms'], e['today'])
            for i, b in enumerate(e['bodies']):
                got = call(ctx, 'POST', '/projects/costing/preview', body=b)
                d = _diff(got, e['list'][i])
                if d:
                    bad.append((e['today'], e['tenant'], e['user'], e['variant'], i, json.dumps(b)[:200], d))
    assert not bad, f'{len(bad)} mismatches; first: {bad[:3]}'


TB = ['allocations', 'projects', 'activity_log', 'notifications']


def _key(r: Any) -> str:
    return json.dumps(jnorm(r), sort_keys=True)


def test_save(rt: MemRuntime, det_ids: DetIds) -> None:
    bad = []
    for e in V['save']:
        det_ids.n = 0
        with tenant_scope(rt, e['tenant']) as s:
            if e['block']:
                t = s.find_by('tenants', 'id', e['tenant'])
                patch('tenants', t, {'settings': {**t['settings'], 'allocation.block_over': True, 'allocation.max_pct_per_person': 100}})
            ctx = make_ctx(s, e['user'], e['perms'], e['today'])
            before = {k: {r['id']: _key(r) for r in s.view(k) if r.get('tenant_id') == e['tenant']} for k in TB}
            got = call(ctx, 'PUT', '/projects/:id/costing', {'id': e['body']['project_id']}, body=e['body'])
            diff = {}
            for k in TB:
                now = [r for r in s.view(k) if r.get('tenant_id') == e['tenant']]
                ids = {r['id'] for r in now}
                diff[k] = {'changed': jnorm([r for r in now if before[k].get(r['id']) != _key(r)]), 'removed': [i for i in before[k] if i not in ids]}
        d = _diff({**got, 'diff': diff}, {'res': e['res'], 'warnings': e['warnings'], 'diff': e['diff']})
        if d:
            bad.append((e['tenant'], e['user'], e['variant'], e['block'], json.dumps(e['body'])[:160], d))
    assert not bad, f'{len(bad)} mismatches; first: {bad[:3]}'
