"""Equivalence of two builds on the LARGE tenant (and the demo tenant): the same calls against the reference code and the
optimised code, each on its own copy of the same perf database, responses compared after normalisation.

    cd backend && python -m tests.perf.equiv --ref /path/to/reference/backend [--template seereonos_perf_tpl7]
        [--tenants big,demo] [--routes-limit N] [--out var/perf/equiv.json]

The tests/e2e parity harness compares the API with the TypeScript mock on the demo seed; the mock cannot hold the
synthetic large tenant, so on that tenant the reference is the previous (unoptimised) Python build, which passed parity.

Per tenant and user (CEO, PM, employee): every GET route of the registry (ids from the tenant's first rows), each called
TWICE on the optimised build (the second answer may come from the response memo and must equal the first); then the
same writes on both (log hours, approve a week, create a task, run a report) and a re-read of the pages they touch.
Normalised: generated ids, ISO timestamps, tokens/request ids (the same rules as tests/e2e/parity.ts).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

import httpx
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, '..', '..'))
PASSWORD = 'Seereon@123'
TENANTS = {
    'big': {'tenant': 'tn_big', 'ceo': 'santosh@bigco.example', 'pm': 'subhankar@bigco.example', 'emp': 'e0001@bigco.example'},
    'demo': {'tenant': 'tn_seereon', 'ceo': 'santosh@seereon.co.in', 'pm': 'subhankar@seereon.co.in', 'emp': 'priya@seereon.co.in'},
}
VOLATILE_KEYS = {'token', 'temp_password', 'avatar_hue', 'demo_token', 'otpauth_url', 'secret', 'request_id', 'payment_id', 'razorpay_subscription_id', 'recovery_codes'}
ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z')
GEN_ID = re.compile(r'\b(?:[a-z]{1,8}_){0,3}(?=[0-9a-z]*\d)(?=[0-9a-z]*[a-z])[0-9a-z]{13,}\b')
TABLE = [
    (r'^/projects/', 'projects'), (r'^/tasks/', 'tasks'), (r'^/(users|people)/', 'users'), (r'^/(sales/deals|deals)/', 'deals'), (r'^/clients/', 'clients'),
    (r'^/finance/invoices/', 'invoices'), (r'^/invoices/', 'invoices'), (r'^/milestones/', 'milestones'), (r'^/documents/', 'documents'), (r'^/candidates/', 'candidates'),
    (r'^/expenses/', 'expense_claims'), (r'^/purchase-orders/', 'purchase_orders'), (r'^/vendor-bills/', 'vendor_bills'), (r'^/estimates/', 'cost_estimates'),
    (r'^/wf/instances/', 'wf_instances'), (r'^/wf/templates/', 'wf_templates'), (r'^/doc-templates/', 'doc_templates'), (r'^/issued-docs/', 'issued_documents'),
    (r'^/employee-docs/', 'employee_documents'), (r'^/changes/', 'change_sets'), (r'^/appraisals/', 'appraisals'), (r'^/appraisal-cycles/', 'appraisal_cycles'),
    (r'^/contracts/', 'contracts'), (r'^/assets/', 'assets'), (r'^/okrs/', 'objectives'), (r'^/lifecycle/', 'lifecycle_checklists'), (r'^/reports/', 'saved_reports'),
    (r'^/payroll/', 'payroll_runs'), (r'^/ops/', 'ops_items'), (r'^/vendors/', 'vendors'), (r'^/budgets/', 'budgets'), (r'^/leave-requests/', 'leave_requests'),
]


def norm(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: ('<v>' if k in VOLATILE_KEYS else norm(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [norm(x) for x in v]
    if isinstance(v, str):
        return GEN_ID.sub('<id>', ISO.sub('<ts>', v))
    return v


def first_diff(a: Any, b: Any, path: str = '$') -> str | None:
    if type(a) is not type(b):
        return f'{path}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}'
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f'{path}.{k}: missing on {"ref" if k not in a else "new"}'
            d = first_diff(a[k], b[k], f'{path}.{k}')
            if d:
                return d
        if list(a) != list(b):
            return f'{path}: key order {list(a)[:8]} != {list(b)[:8]}'
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f'{path}: length {len(a)} != {len(b)}'
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f'{path}[{i}]')
            if d:
                return d
        return None
    return None if a == b else f'{path}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}'


def body(r: httpx.Response) -> Any:
    try:
        j = r.json()
    except ValueError:
        return {'_raw': r.text[:2000]}
    if isinstance(j, dict):
        j.pop('request_id', None)
        if isinstance(j.get('error'), dict):
            j['error'].pop('request_id', None)
    return j


def nb(r: httpx.Response, path: str) -> Any:
    v = norm(body(r))
    if path == '/sessions':  # session ids are random per sign-in
        v = json.loads(re.sub(r'"id": "[0-9a-f]{10}"', '"id": "<v>"', json.dumps(v)))
    return v


def psql(sql: str) -> None:
    subprocess.run(['su', 'postgres', '-c', f'psql -q -v ON_ERROR_STOP=1 -c "{sql}"'], check=True)


def make_db(name: str, template: str) -> None:
    psql(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
    psql(f'CREATE DATABASE {name} TEMPLATE {template} OWNER seereon_owner')
    psql(f'GRANT CONNECT ON DATABASE {name} TO seereon_app')


def serve(tree: str, db: str, port: int, stop: bool = False) -> None:
    args = [os.path.join(tree, 'tests', 'perf', 'serve.sh'), '--port', str(port)]
    args += ['--stop'] if stop else ['--db', db, '--workers', '1']
    subprocess.run(args, check=True, env={**os.environ, 'PERF_LOG': os.path.join(BACKEND, 'var', f'equiv-{port}.log')})


def get_routes(limit: int | None) -> list[str]:
    sys.path.insert(0, BACKEND)
    from app.router import Router, load_modules

    r = load_modules(Router())
    pats = [x.pattern for x in r.routes if x.method == 'GET' and not x.override and not x.public and not x.super_admin]
    return pats[:limit] if limit else pats


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ref', required=True, help='backend directory of the reference build')
    ap.add_argument('--template', default='seereonos_perf_tpl7')
    ap.add_argument('--tenants', default='big,demo')
    ap.add_argument('--routes-limit', type=int, default=None)
    ap.add_argument('--no-twice', action='store_true', help='call each GET once (no memo check): GETs with side effects then match exactly')
    ap.add_argument('--out', default=os.path.join(BACKEND, 'var', 'perf', 'equiv.json'))
    a = ap.parse_args(argv)
    os.environ['TZ'] = 'Asia/Kolkata'
    time.tzset()
    today = dt.date.today()
    monday = (today - dt.timedelta(days=today.weekday())).isoformat()
    patterns = get_routes(a.routes_limit)
    dbs = {'ref': 'seereonos_eq_ref', 'new': 'seereonos_eq_new'}
    ports = {'ref': 8311, 'new': 8312}
    trees = {'ref': os.path.abspath(a.ref), 'new': BACKEND}
    # out-of-band preparation on the template copies (identical on both): reopen approved weeks for the approval write
    for side in dbs:
        make_db(dbs[side], a.template)
    stats = {'calls': 0, 'equal': 0, 'diffs': [], 'twice_diffs': [], 'status': {}}
    for side in dbs:
        serve(trees[side], dbs[side], ports[side])
    try:
        with httpx.Client(timeout=900) as cl, psycopg.connect(f'postgres://seereon_owner:seereon_owner@localhost:5432/{dbs["ref"]}') as pg:

            def q(sql: str, *p: Any) -> list[tuple]:
                return pg.execute(sql, p).fetchall()

            def call(side: str, method: str, path: str, tok: str | None, js: Any = None) -> httpx.Response:
                return cl.request(method, f'http://localhost:{ports[side]}/api/v1{path}', json=js, headers={'Authorization': f'Bearer {tok}'} if tok else {})

            def both(label: str, method: str, path: str, toks: dict[str, str], js: Any = None, twice: bool = False) -> tuple[Any, Any]:
                rr = call('ref', method, path, toks['ref'], js)
                rn = call('new', method, path, toks['new'], js)
                stats['calls'] += 1
                stats['status'][rn.status_code] = stats['status'].get(rn.status_code, 0) + 1
                br, bn = nb(rr, path), nb(rn, path)
                d = f'status {rr.status_code} != {rn.status_code}' if rr.status_code != rn.status_code else first_diff(br, bn)
                if d:
                    stats['diffs'].append(f'{label} {method} {path}: {d}')
                    print('DIFF', label, method, path, d, flush=True)
                else:
                    stats['equal'] += 1
                if rn.status_code >= 500:
                    stats.setdefault('server_errors', []).append(f'{label} {method} {path} ref={rr.status_code}')
                if twice and method == 'GET' and not a.no_twice:
                    rn2 = call('new', method, path, toks['new'])
                    d2 = f'status {rn.status_code} != {rn2.status_code}' if rn.status_code != rn2.status_code else first_diff(bn, nb(rn2, path))
                    if d2:
                        stats['twice_diffs'].append(f'{label} {path}: {d2}')
                        print('TWICE', label, path, d2, flush=True)
                return body(rr), body(rn)

            for tkey in a.tenants.split(','):
                cfg = TENANTS[tkey]
                tid = cfg['tenant']
                toks: dict[str, dict[str, str]] = {}
                uids: dict[str, str] = {}
                for role in ('ceo', 'pm', 'emp'):
                    toks[role] = {}
                    for side in dbs:
                        r = call(side, 'POST', '/auth/login', None, {'email': cfg[role], 'password': PASSWORD})
                        assert r.status_code == 200, r.text[:300]
                        toks[role][side] = r.json()['data']['token']
                        uids[role] = r.json()['data']['user']['id']
                first: dict[str, str] = {}

                def first_id(table: str) -> str:
                    if table not in first:
                        try:
                            rows = q(f'SELECT id FROM {table} WHERE tenant_id = %s ORDER BY _ord LIMIT 1', tid)
                        except psycopg.Error:
                            pg.rollback()
                            rows = []
                        first[table] = rows[0][0] if rows else 'x'
                    return first[table]

                month = today.isoformat()[:7]

                def fill(pat: str, role: str) -> str:
                    fixed = {':key': 'hr_executive', ':week': monday, ':process': 'expense', ':view': 'predictions', ':type': 'departments', ':token': 'x', ':userId': uids[role], ':uid': uids[role], ':month': month}
                    tbl = next((t for rx, t in TABLE if re.search(rx, pat)), None)
                    return re.sub(r':[A-Za-z_]+', lambda m: fixed.get(m.group(0)) or (first_id(tbl) if m.group(0) == ':id' and tbl else 'x'), pat)

                t0 = time.time()
                for role in ('ceo', 'pm', 'emp'):
                    for pat in patterns:
                        both(f'{tkey}:{role}', 'GET', fill(pat, role), toks[role], twice=True)
                    print(f'[equiv] {tkey} {role}: sweep done, {stats["calls"]} calls, {len(stats["diffs"])} diffs, {time.time() - t0:.0f}s', flush=True)
                # ---- writes (same order on both builds)
                _, wk = both(f'{tkey}:emp', 'GET', f'/timesheets/{monday}', toks['emp'])
                rows = [{k: x.get(k) for k in ('kind', 'task_id', 'internal_code', 'leave_type', 'note', 'project_id', 'hours')} for x in (wk.get('data') or {}).get('rows', []) if x['kind'] != 'leave']
                internal = next((x for x in rows if x['kind'] == 'internal'), None)
                if internal is None:
                    internal = {'kind': 'internal', 'task_id': None, 'internal_code': 'meeting', 'leave_type': None, 'note': 'equiv', 'project_id': None, 'hours': {}}
                    rows.append(internal)
                internal['note'] = internal.get('note') or 'Stand-ups and reviews'
                internal['hours'] = {**(internal['hours'] or {}), today.isoformat(): 3}
                both(f'{tkey}:emp', 'PUT', f'/timesheets/{monday}', toks['emp'], {'rows': rows})
                both(f'{tkey}:emp', 'GET', f'/timesheets/{monday}', toks['emp'], twice=True)
                sub = q(
                    "SELECT w.id FROM timesheet_weeks w JOIN users u ON u.id = w.user_id WHERE w.tenant_id = %s AND w.status = 'submitted' AND u.reports_to_id = %s ORDER BY w.week_start DESC, w.id LIMIT 2",
                    tid,
                    uids['pm'],
                )
                for (wid,) in sub:
                    both(f'{tkey}:pm', 'POST', f'/approvals/{wid}/approve', toks['pm'], {})
                proj = q("SELECT id FROM projects WHERE tenant_id = %s AND pm_id = %s AND status = 'active' ORDER BY _ord LIMIT 1", tid, uids['pm'])
                if proj:
                    pid = proj[0][0]
                    ms = q('SELECT id FROM milestones WHERE project_id = %s AND deleted_at IS NULL ORDER BY planned_end DESC LIMIT 1', pid)[0][0]
                    owner = sorted(r[0] for r in q('SELECT DISTINCT owner_id FROM tasks WHERE project_id = %s AND owner_id IS NOT NULL', pid) if r[0] != uids['pm'])[0]
                    task = {'title': 'Equivalence task', 'project_id': pid, 'milestone_id': ms, 'owner_id': owner, 'reviewer_id': uids['pm'], 'est_hours': 8, 'start_on': today.isoformat(), 'due_on': (today + dt.timedelta(days=14)).isoformat(), 'complexity': 'medium', 'priority': 'normal'}
                    both(f'{tkey}:pm', 'POST', '/tasks', toks['pm'], task)
                    both(f'{tkey}:pm', 'GET', f'/projects/{pid}', toks['pm'], twice=True)
                    both(f'{tkey}:ceo', 'GET', f'/projects/{pid}/costing', toks['ceo'], twice=True)
                spec = {'title': 'Hours by project', 'dataset': 'hours', 'measures': [{'field': 'hours', 'agg': 'sum'}], 'dimensions': [{'field': 'project'}], 'period': {'preset': 'last_90_days'}, 'chart': 'hbar'}
                both(f'{tkey}:ceo', 'POST', '/reports/run', toks['ceo'], {'spec': spec})
                for role, path in [('ceo', '/dashboards/ceo'), ('pm', '/dashboards/pm'), ('emp', '/dashboards/employee'), ('ceo', '/tasks'), ('emp', '/tasks'), ('pm', '/approvals'), ('ceo', '/finance/margins'), ('ceo', '/scorecard'), ('ceo', '/insights'), ('emp', '/notifications')]:
                    both(f'{tkey}:{role}:after-writes', 'GET', path, toks[role], twice=True)
                print(f'[equiv] {tkey}: done, {stats["calls"]} calls, {len(stats["diffs"])} diffs, {len(stats["twice_diffs"])} twice diffs', flush=True)
    finally:
        for side in dbs:
            serve(trees[side], dbs[side], ports[side], stop=True)
    summary = {k: v for k, v in stats.items()}
    print(json.dumps({'calls': stats['calls'], 'equal': stats['equal'], 'diffs': len(stats['diffs']), 'twice_diffs': len(stats['twice_diffs']), 'status': stats['status']}))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w') as f:
        json.dump(summary, f, indent=1)
    sys.exit(1 if stats['diffs'] or stats['twice_diffs'] else 0)


if __name__ == '__main__':
    main(sys.argv[1:])
