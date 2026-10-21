"""Wall time of the benchmark's read endpoints in-process (production config, response memo OFF, warm cache):
the cost of COMPUTING each response. Writes nothing except what the handlers themselves persist (sweeps).

    cd backend && TZ=Asia/Kolkata python -m tests.perf.timeit [--db seereonos_perf_dev] [--tenant big] [--n 3]
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='seereonos_perf_dev')
    ap.add_argument('--tenant', default='big')
    ap.add_argument('--n', type=int, default=3)
    ap.add_argument('--only', default='')
    ap.add_argument('--profile', default='', help='cProfile the measured calls of this case (sort: tottime/cumulative via --sort)')
    ap.add_argument('--sort', default='cumulative')
    ap.add_argument('--limit', type=int, default=40)
    a = ap.parse_args(argv)
    os.environ.update(
        APP_ENV='development',  # production-like: rows not frozen; no SMTP/APP_URL/CORS requirements for a local benchmark
        FREEZE_ROWS='false',
        TZ=os.environ.get('TZ', 'Asia/Kolkata'),
        DATABASE_URL=os.environ.get('PERF_DATABASE_URL') or f'postgres://seereon_app:seereon_app@localhost:5432/{a.db}',
        MFA_ENCRYPTION_KEY='perf-benchmark-key-0123456789abcdefghijklmnopqrstuvwxyz',
        LOG_LEVEL='warn',
        STATIC_DIR='',
        RATE_LIMIT_AUTH='100000',
        MEMO_MAX_MB='0',
    )
    time.tzset()
    import datetime as dt

    import orjson
    import psycopg

    from app.js import UNDEF
    from app.main import create_app
    from app.runtime import ClientInfo, ExecRequest
    from tests.perf.bench import TENANTS

    cfg = TENANTS[a.tenant]
    app = create_app(start_workers=False)
    rt = app.state.rt

    def call(method: str, path: str, token: str | None, body: bytes | None = None) -> tuple[int, bytes]:
        from urllib.parse import parse_qsl, urlsplit

        u = urlsplit(path)
        res = rt.execute(
            ExecRequest(
                method=method,
                path=u.path,
                query=dict(parse_qsl(u.query)),
                body_raw=body,
                body=orjson.loads(body) if body else UNDEF,
                headers={'Authorization': f'Bearer {token}' if token else None, 'X-View-As': None, 'If-Match': None, 'Idempotency-Key': None},
                client=ClientInfo('127.0.0.1', 'perf', 'perf-req-0001'),
            )
        )
        return res.status, res.body

    tok = {}
    for role in ('ceo', 'pm', 'emp'):
        st, b = call('POST', '/auth/login', None, orjson.dumps({'email': cfg[role], 'password': 'Seereon@123'}))
        assert st == 200, b[:200]
        tok[role] = orjson.loads(b)['data']['token']
    with psycopg.connect(f'postgres://seereon_owner:seereon_owner@localhost:5432/{a.db}') as c:
        pm_id = c.execute('SELECT id FROM users WHERE email = %s', [cfg['pm']]).fetchone()[0]
        proj = c.execute("SELECT id FROM projects WHERE tenant_id = %s AND pm_id = %s AND status = 'active' ORDER BY _ord LIMIT 1", [cfg['tenant'], pm_id]).fetchone()[0]
        big = c.execute(
            "SELECT p.id FROM projects p JOIN tasks t ON t.project_id = p.id WHERE p.tenant_id = %s AND p.status = 'active' GROUP BY p.id ORDER BY count(*) DESC LIMIT 1",
            [cfg['tenant']],
        ).fetchone()[0]
    today = dt.date.today()
    monday = (today - dt.timedelta(days=today.weekday())).isoformat()
    spec = orjson.dumps(
        {
            'spec': {
                'title': 'Hours by project',
                'dataset': 'hours',
                'measures': [{'field': 'hours', 'agg': 'sum'}],
                'dimensions': [{'field': 'project'}],
                'filters': [{'field': 'kind', 'op': 'eq', 'value': 'task'}],
                'period': {'preset': 'last_90_days'},
                'chart': 'hbar',
            }
        }
    )
    cases = [
        ('me', 'GET', '/me', 'emp', None),
        ('dash_ceo', 'GET', '/dashboards/ceo', 'ceo', None),
        ('dash_pm', 'GET', '/dashboards/pm', 'pm', None),
        ('dash_emp', 'GET', '/dashboards/employee', 'emp', None),
        ('tasks_ceo', 'GET', '/tasks', 'ceo', None),
        ('tasks_emp', 'GET', '/tasks', 'emp', None),
        ('ts_week', 'GET', f'/timesheets/{monday}', 'emp', None),
        ('project', 'GET', f'/projects/{proj}', 'pm', None),
        ('costing', 'GET', f'/projects/{big}/costing', 'ceo', None),
        ('margins', 'GET', '/finance/margins', 'ceo', None),
        ('invoices', 'GET', '/finance/invoices', 'ceo', None),
        ('insights', 'GET', '/insights', 'ceo', None),
        ('scorecard', 'GET', '/scorecard', 'ceo', None),
        ('report_run', 'POST', '/reports/run', 'ceo', spec),
    ]
    # writes (they really write to --db): log hours on this week, approve a re-opened week, create a task
    st, b = call('GET', f'/timesheets/{monday}', tok['emp'])
    wrows = [
        {k: x.get(k) for k in ('kind', 'task_id', 'internal_code', 'leave_type', 'note', 'project_id', 'hours')} for x in orjson.loads(b)['data']['rows'] if x['kind'] != 'leave'
    ]
    if not any(x['kind'] == 'internal' for x in wrows):
        wrows.append({'kind': 'internal', 'task_id': None, 'internal_code': 'meeting', 'leave_type': None, 'note': 'perf', 'project_id': None, 'hours': {}})
    internal = next(x for x in wrows if x['kind'] == 'internal')
    with psycopg.connect(f'postgres://seereon_owner:seereon_owner@localhost:5432/{a.db}', autocommit=True) as c:
        weeks = [
            r[0]
            for r in c.execute(
                "SELECT w.id FROM timesheet_weeks w JOIN users u ON u.id = w.user_id WHERE w.tenant_id = %s AND w.status = 'approved' AND u.reports_to_id = %s ORDER BY w.week_start DESC LIMIT %s",
                [cfg['tenant'], pm_id, 4 * (a.n + 1)],
            ).fetchall()
        ]
        c.execute("UPDATE timesheet_weeks SET status = 'submitted', decided_by = NULL, decided_at = NULL, version = version + 1 WHERE id = ANY(%s::text[])", [weeks])
        team = [r[0] for r in c.execute('SELECT DISTINCT owner_id FROM tasks WHERE project_id = %s AND owner_id IS NOT NULL ORDER BY 1', [proj]).fetchall() if r[0] != pm_id]
        ms = c.execute('SELECT id FROM milestones WHERE project_id = %s AND deleted_at IS NULL ORDER BY planned_end DESC LIMIT 1', [proj]).fetchone()[0]
    counter = [0]

    def log_body() -> bytes:
        counter[0] += 1
        h = dict(internal['hours'] or {})
        h[today.isoformat()] = 1 + counter[0] % 2
        internal['hours'] = h
        return orjson.dumps({'rows': wrows})

    def task_body() -> bytes:
        counter[0] += 1
        return orjson.dumps(
            {
                'title': f'Perf task {counter[0]}',
                'project_id': proj,
                'milestone_id': ms,
                'owner_id': team[counter[0] % len(team)],
                'reviewer_id': pm_id,
                'est_hours': 8,
                'start_on': today.isoformat(),
                'due_on': (today + dt.timedelta(days=14)).isoformat(),
                'complexity': 'medium',
                'priority': 'normal',
            }
        )

    wcases = [
        ('log_hours', 'PUT', lambda: f'/timesheets/{monday}', 'emp', log_body),
        ('approve_week', 'POST', lambda: f'/approvals/{weeks.pop()}/approve', 'pm', lambda: b'{}'),
        ('task_create', 'POST', lambda: '/tasks', 'pm', task_body),
    ]
    cases += [(n, m, pf, r, bf) for n, m, pf, r, bf in wcases]  # type: ignore[misc]
    only = set(a.only.split(',')) if a.only else None
    for name, method, path, role, body in cases:
        if only and name not in only:
            continue
        def once() -> tuple[int, bytes]:
            pth = path() if callable(path) else path
            bd = body() if callable(body) else body
            return call(method, pth, tok[role], bd)

        st, out = once()  # warm-up (cache fill, sweeps)
        if st != 200:
            print(name, st, out[:300])
        ts = []
        prof = None
        if a.profile == name:
            import cProfile

            prof = cProfile.Profile()
        for _ in range(a.n):
            t0 = time.perf_counter()
            if prof is not None:
                prof.enable()
            st, out = once()
            if prof is not None:
                prof.disable()
            ts.append((time.perf_counter() - t0) * 1000)
        if prof is not None:
            import io
            import pstats

            sio = io.StringIO()
            pstats.Stats(prof, stream=sio).strip_dirs().sort_stats(a.sort).print_stats(a.limit)
            print(sio.getvalue())
        print(f'{name:12s} {st} {len(out):>9d} B  median {statistics.median(ts):8.1f} ms  min {min(ts):8.1f}', flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
