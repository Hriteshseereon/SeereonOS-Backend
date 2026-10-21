"""HTTP load generator for the API (asyncio + httpx, closed loop): p50/p95/p99, max, throughput and errors per scenario
and concurrency level, on the demo tenant or the large synthetic tenant (tests/perf/synthetic.py).

    cd backend && python -m tests.perf.bench --url http://localhost:8300 --tenant big --conc 1,10,50 \
        [--scenarios dash_ceo,tasks_ceo] [--duration 12] [--max-requests 400] [--out var/perf/big.json] [--db seereonos_perf]

Setup (users, ids, submitted weeks for the approval scenario) reads the database directly; every measured request goes
over HTTP like the browser (Accept-Encoding: gzip, keep-alive). Writes really write: run them on a disposable copy
(see run_all.sh, which restores the perf database from its template before each tenant run).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg

PASSWORD = 'Seereon@123'
TENANTS = {
    'demo': {'tenant': 'tn_seereon', 'ceo': 'santosh@seereon.co.in', 'pm': 'subhankar@seereon.co.in', 'emp': 'pragyasmita@seereon.co.in'},
    'big': {'tenant': 'tn_big', 'ceo': 'santosh@bigco.example', 'pm': 'subhankar@bigco.example', 'emp': 'e0001@bigco.example'},
}


@dataclass
class Result:
    name: str
    conc: int
    lat: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    wall: float = 0.0

    def summary(self) -> dict[str, Any]:
        lat = sorted(self.lat)
        n = len(lat)

        def pct(p: float) -> float | None:
            if not n:
                return None
            k = min(n - 1, max(0, int(round(p * n + 0.5)) - 1))
            return round(lat[k] * 1000, 1)

        return {
            'scenario': self.name,
            'conc': self.conc,
            'n': n,
            'errors': sum(self.errors.values()),
            'error_codes': self.errors,
            'p50': pct(0.50),
            'p95': pct(0.95),
            'p99': pct(0.99),
            'max': round(lat[-1] * 1000, 1) if n else None,
            'mean': round(statistics.fmean(lat) * 1000, 1) if n else None,
            'rps': round(n / self.wall, 1) if self.wall else None,
        }


class Ctx:
    def __init__(self, url: str, tenant: str, db: str) -> None:
        self.url = url.rstrip('/') + '/api/v1'
        self.cfg = TENANTS[tenant]
        self.tenant = tenant
        self.db = db
        self.tokens: dict[str, str] = {}
        self.emp_tokens: list[tuple[str, str]] = []  # (user_id, token)
        self.rng = random.Random(7)
        self.monday = ''
        self.today = ''

    def sql(self, q: str, *params: Any) -> list[tuple]:
        with psycopg.connect(f'postgres://seereon_owner:seereon_owner@localhost:5432/{self.db}', autocommit=True) as c:
            cur = c.execute(q, params)  # type: ignore[arg-type]
            return cur.fetchall() if cur.description else []


async def login(client: httpx.AsyncClient, url: str, email: str) -> tuple[str, str]:
    r = await client.post(url + '/auth/login', json={'email': email, 'password': PASSWORD})
    r.raise_for_status()
    d = r.json()['data']
    return d['user']['id'], d['token']


async def setup(c: Ctx, client: httpx.AsyncClient, n_emp: int) -> None:
    tid = c.cfg['tenant']
    os.environ.setdefault('TZ', 'Asia/Kolkata')
    time.tzset()
    today = dt.date.today()
    c.today = today.isoformat()
    c.monday = (today - dt.timedelta(days=today.weekday())).isoformat()
    for role in ('ceo', 'pm', 'emp'):
        uid, tok = await login(client, c.url, c.cfg[role])
        c.tokens[role] = tok
        c.tokens[role + '_id'] = uid
    # employees with an open week this Monday (log hours) — one per virtual user where the tenant has enough
    emails = [
        r[0]
        for r in c.sql(
            "SELECT email FROM users WHERE tenant_id = %s AND role_key = 'employee' AND status = 'active' ORDER BY email LIMIT %s",
            tid,
            n_emp,
        )
    ]
    res = []
    for k in range(0, len(emails), 4):  # a few at a time: sign-in is deliberately slow (scrypt)
        res += await asyncio.gather(*(login(client, c.url, e) for e in emails[k : k + 4]))
    c.emp_tokens = list(res)
    # the PM's project (detail) and its team (task create)
    c.pm_project = c.sql("SELECT id FROM projects WHERE tenant_id = %s AND pm_id = %s AND status = 'active' ORDER BY _ord LIMIT 1", tid, c.tokens['pm_id'])[0][0]
    ms = c.sql('SELECT id, planned_start, planned_end FROM milestones WHERE project_id = %s AND deleted_at IS NULL ORDER BY planned_end DESC LIMIT 1', c.pm_project)[0]
    c.pm_ms = ms[0]
    team = [r[0] for r in c.sql('SELECT DISTINCT owner_id FROM tasks WHERE project_id = %s AND owner_id IS NOT NULL', c.pm_project) if r[0] != c.tokens['pm_id']]
    c.pm_team = sorted(team)  # the PM reviews: an owner other than the PM (TASK_SELF_REVIEW otherwise)
    c.big_project = c.sql(
        "SELECT p.id FROM projects p JOIN tasks t ON t.project_id = p.id WHERE p.tenant_id = %s AND p.status = 'active' GROUP BY p.id ORDER BY count(*) DESC LIMIT 1", tid
    )[0][0]


def prepare_approvals(c: Ctx, n: int) -> list[str]:
    """turn up to n approved weeks of the PM's reports back into 'submitted' (out of band, like a burst of submissions),
    newest first — the approval scenario approves them over HTTP"""
    tid = c.cfg['tenant']
    rows = c.sql(
        """SELECT w.id FROM timesheet_weeks w JOIN users u ON u.id = w.user_id
            WHERE w.tenant_id = %s AND w.status = 'approved' AND u.reports_to_id = %s
            ORDER BY w.week_start DESC, w.id LIMIT %s""",
        tid,
        c.tokens['pm_id'],
        n,
    )
    ids = [r[0] for r in rows]
    if ids:
        c.sql(
            "UPDATE timesheet_weeks SET status = 'submitted', decided_by = NULL, decided_at = NULL, version = version + 1 WHERE id = ANY(%s::text[])",
            ids,
        )
    return ids


# ---------------------------------------------------------------------------------------------------------------------
# scenarios: name -> async fn(c, client, vu, i) -> (status, label)
# ---------------------------------------------------------------------------------------------------------------------
def H(tok: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {tok}'}


async def get(client: httpx.AsyncClient, c: Ctx, role: str, path: str, **kw: Any) -> int:
    r = await client.get(c.url + path, headers=H(c.tokens[role]), **kw)
    return r.status_code


def make_scenarios(c: Ctx) -> dict[str, Any]:
    week_rows: dict[str, Any] = {}

    async def s_login(client, vu, i):  # noqa: ANN001
        r = await client.post(c.url + '/auth/login', json={'email': c.cfg['emp'], 'password': PASSWORD})
        return r.status_code

    async def s_me(client, vu, i):  # noqa: ANN001
        return await get(client, c, 'emp', '/me')

    async def s_ts_week(client, vu, i):  # noqa: ANN001
        uid, tok = c.emp_tokens[vu % len(c.emp_tokens)]
        r = await client.get(c.url + f'/timesheets/{c.monday}', headers=H(tok))
        return r.status_code

    async def s_project(client, vu, i):  # noqa: ANN001
        return await get(client, c, 'pm', f'/projects/{c.pm_project}')

    async def s_costing(client, vu, i):  # noqa: ANN001
        return await get(client, c, 'ceo', f'/projects/{c.big_project}/costing')

    async def s_report(client, vu, i):  # noqa: ANN001
        spec = {
            'title': 'Hours by project',
            'dataset': 'hours',
            'measures': [{'field': 'hours', 'agg': 'sum'}],
            'dimensions': [{'field': 'project'}],
            'filters': [{'field': 'kind', 'op': 'eq', 'value': 'task'}],
            'period': {'preset': 'last_90_days'},
            'chart': 'hbar',
        }
        r = await client.post(c.url + '/reports/run', json={'spec': spec}, headers=H(c.tokens['ceo']))
        return r.status_code

    async def s_log_hours(client, vu, i):  # noqa: ANN001
        uid, tok = c.emp_tokens[vu % len(c.emp_tokens)]
        rows = week_rows.get(uid)
        if rows is None:
            r = await client.get(c.url + f'/timesheets/{c.monday}', headers=H(tok))
            if r.status_code != 200:
                return r.status_code
            d = r.json()['data']
            rows = [
                {k: x.get(k) for k in ('kind', 'task_id', 'internal_code', 'leave_type', 'note', 'project_id', 'hours')}
                for x in d['rows']
                if x['kind'] != 'leave'
            ]
            internal = next((x for x in rows if x['kind'] == 'internal'), None)
            if internal is None:
                rows.append({'kind': 'internal', 'task_id': None, 'internal_code': 'meeting', 'leave_type': None, 'note': 'perf', 'project_id': None, 'hours': {}})
            week_rows[uid] = rows
        internal = next(x for x in rows if x['kind'] == 'internal')
        if not (internal.get('note') or '').strip():
            internal['note'] = 'Stand-ups and reviews'  # internal time needs a note
        h = dict(internal['hours'] or {})
        h[c.today] = 1 if h.get(c.today) == 2 else 2
        internal['hours'] = h
        r = await client.put(c.url + f'/timesheets/{c.monday}', json={'rows': rows}, headers=H(tok))
        if r.status_code == 200:
            return 200
        return r.status_code

    approvals: list[str] = []

    async def s_approve(client, vu, i):  # noqa: ANN001
        if not approvals:
            return 'exhausted'
        wid = approvals.pop(0)
        r = await client.post(c.url + f'/approvals/{wid}/approve', json={}, headers=H(c.tokens['pm']))
        return r.status_code

    async def s_task_create(client, vu, i):  # noqa: ANN001
        owner = c.pm_team[(vu + i) % len(c.pm_team)]
        body = {
            'title': f'Perf task {vu}-{i} {c.rng.randrange(10**6)}',
            'project_id': c.pm_project,
            'milestone_id': c.pm_ms,
            'owner_id': owner,
            'reviewer_id': c.tokens['pm_id'],
            'est_hours': 8,
            'start_on': c.today,
            'due_on': (dt.date.fromisoformat(c.today) + dt.timedelta(days=14)).isoformat(),
            'complexity': 'medium',
            'priority': 'normal',
        }
        r = await client.post(c.url + '/tasks', json=body, headers=H(c.tokens['pm']))
        return r.status_code

    def g(role: str, path: str) -> Any:
        async def f(client, vu, i):  # noqa: ANN001
            return await get(client, c, role, path)

        return f

    sc = {
        'login': s_login,
        'me': s_me,
        'dash_ceo': g('ceo', '/dashboards/ceo'),
        'dash_pm': g('pm', '/dashboards/pm'),
        'dash_emp': g('emp', '/dashboards/employee'),
        'tasks_ceo': g('ceo', '/tasks'),
        'tasks_emp': g('emp', '/tasks'),
        'ts_week': s_ts_week,
        'project': s_project,
        'costing': s_costing,
        'margins': g('ceo', '/finance/margins'),
        'invoices': g('ceo', '/finance/invoices'),
        'insights': g('ceo', '/insights'),
        'scorecard': g('ceo', '/scorecard'),
        'report_run': s_report,
        'log_hours': s_log_hours,
        'approve_week': s_approve,
        'task_create': s_task_create,
    }
    sc['_approvals'] = approvals  # type: ignore[assignment]
    return sc


async def run_one(c: Ctx, fn: Any, name: str, conc: int, duration: float, max_req: int, min_req: int) -> Result:
    res = Result(name, conc)
    limits = httpx.Limits(max_connections=conc + 2, max_keepalive_connections=conc + 2)
    async with httpx.AsyncClient(timeout=900, limits=limits, headers={'Accept-Encoding': 'gzip'}) as client:
        count = 0
        t_end = time.perf_counter() + duration
        t0 = time.perf_counter()

        async def vu_loop(vu: int) -> None:
            nonlocal count
            i = 0
            while True:
                if count >= max_req or (time.perf_counter() > t_end and count >= min_req):
                    return
                count += 1
                s = time.perf_counter()
                try:
                    st = await fn(client, vu, i)
                except Exception as e:  # noqa: BLE001
                    st = type(e).__name__
                if st == 'exhausted':
                    count -= 1
                    return
                el = time.perf_counter() - s
                res.lat.append(el)
                if st != 200:
                    res.errors[str(st)] = res.errors.get(str(st), 0) + 1
                i += 1

        await asyncio.gather(*(vu_loop(v) for v in range(conc)))
        res.wall = time.perf_counter() - t0
    return res


async def main_async(a: argparse.Namespace) -> list[dict[str, Any]]:
    c = Ctx(a.url, a.tenant, a.db)
    concs = [int(x) for x in a.conc.split(',')]
    async with httpx.AsyncClient(timeout=120) as client:
        await setup(c, client, max(concs))
    sc = make_scenarios(c)
    names = [n for n in sc if not n.startswith('_')] if a.scenarios == 'all' else a.scenarios.split(',')
    out = []
    for name in names:
        fn = sc[name]
        for conc in concs:
            if name == 'approve_week':
                need = a.max_requests if conc > 1 else min(a.max_requests, 60)
                sc['_approvals'][:] = prepare_approvals(c, need)
            if not a.no_warm and name not in ('approve_week', 'login'):
                async with httpx.AsyncClient(timeout=120) as client:
                    for _ in range(2):  # both workers (best effort) see the request once before measuring
                        await fn(client, 0, 0)
            r = await run_one(c, fn, name, conc, a.duration, a.max_requests if conc > 1 else min(a.max_requests, a.max_requests_c1), a.min_requests)
            s = r.summary()
            s['tenant'] = a.tenant
            out.append(s)
            print(json.dumps(s), flush=True)
    return out


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://localhost:8300')
    ap.add_argument('--tenant', default='big', choices=list(TENANTS))
    ap.add_argument('--db', default='seereonos_perf')
    ap.add_argument('--conc', default='1,10,50')
    ap.add_argument('--scenarios', default='all')
    ap.add_argument('--duration', type=float, default=12.0)
    ap.add_argument('--max-requests', type=int, default=400)
    ap.add_argument('--max-requests-c1', type=int, default=40)
    ap.add_argument('--min-requests', type=int, default=20)
    ap.add_argument('--no-warm', action='store_true')
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)
    out = asyncio.run(main_async(a))
    if a.out:
        os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
        with open(a.out, 'w') as f:
            json.dump(out, f, indent=1)


if __name__ == '__main__':
    main(sys.argv[1:])
