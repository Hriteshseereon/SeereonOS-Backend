"""Cold start and memory per worker: a ONE-worker server (so every number is one process), fresh for each phase.

    cd backend && python -m tests.perf.coldstart [--db seereonos_perf] [--port 8301] [--out var/perf/cold.json]

Phase A (large tenant): RSS after boot -> first sign-in -> first GET of each page (cold: tables load from PostgreSQL into
the tenant cache) -> second GET (warm) -> RSS with the large tenant cached.
Phase B (20 tenants): fresh worker; for each of 20 demo-sized tenants: sign in as its CEO, GET /dashboards/ceo, /tasks,
/dashboards/pm, /finance/margins (cold, then warm) -> RSS with 20 tenants cached.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import httpx
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, '..', '..'))
PAGES = ['/me', '/dashboards/ceo', '/tasks', '/dashboards/pm', '/finance/margins', '/scorecard', '/insights']


def rss_mb(pid: int) -> float:
    with open(f'/proc/{pid}/status') as f:
        for line in f:
            if line.startswith('VmRSS:'):
                return round(int(line.split()[1]) / 1024, 1)
    return 0.0


def worker_pid(port: int) -> int:
    master = int(open(os.path.join(BACKEND, 'var', f'perf-{port}.pid')).read().strip())
    out = subprocess.run(['pgrep', '-P', str(master)], capture_output=True, text=True).stdout.split()
    return int(out[0])


def start(db: str, port: int, env: dict | None = None) -> int:
    subprocess.run([os.path.join(HERE, 'serve.sh'), '--db', db, '--port', str(port), '--workers', '1'], check=True, env={**os.environ, **(env or {})})
    return worker_pid(port)


def stop(port: int) -> None:
    subprocess.run([os.path.join(HERE, 'serve.sh'), '--port', str(port), '--stop'], check=True)


def timed(client: httpx.Client, method: str, url: str, **kw) -> tuple[float, int]:  # noqa: ANN003
    t0 = time.perf_counter()
    r = client.request(method, url, **kw)
    return round((time.perf_counter() - t0) * 1000, 1), r.status_code


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='seereonos_perf')
    ap.add_argument('--port', type=int, default=8301)
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)
    base = f'http://localhost:{a.port}/api/v1'
    res: dict = {}
    with psycopg.connect(f'postgres://seereon_owner:seereon_owner@localhost:5432/{a.db}') as c:
        ceos = c.execute(
            "SELECT DISTINCT ON (t.id) t.id, u.email FROM tenants t JOIN users u ON u.tenant_id = t.id AND u.role = 'admin' AND u.status = 'active'"
            " WHERE t.id <> 'tn_big' ORDER BY t.id, u._ord"
        ).fetchall()
    # ---------------- A: large tenant
    pid = start(a.db, a.port)
    A: dict = {'rss_boot_mb': rss_mb(pid)}
    with httpx.Client(timeout=600) as cl:
        ms, st = timed(cl, 'POST', base + '/auth/login', json={'email': 'santosh@bigco.example', 'password': 'Seereon@123'})
        tok = cl.post(base + '/auth/login', json={'email': 'santosh@bigco.example', 'password': 'Seereon@123'}).json()['data']['token']
        A['first_login_ms'] = ms
        h = {'Authorization': f'Bearer {tok}'}
        A['pages'] = {}
        for p in PAGES:
            cold = timed(cl, 'GET', base + p, headers=h)
            warm = timed(cl, 'GET', base + p, headers=h)
            A['pages'][p] = {'cold_ms': cold[0], 'warm_ms': warm[0], 'status': [cold[1], warm[1]]}
            print('A', p, A['pages'][p], flush=True)
    A['rss_big_cached_mb'] = rss_mb(pid)
    stop(a.port)
    res['big'] = A
    print('A', {k: v for k, v in A.items() if k != 'pages'}, flush=True)
    # ---------------- B: 20 demo-sized tenants
    pid = start(a.db, a.port)
    B: dict = {'rss_boot_mb': rss_mb(pid), 'tenants': []}
    with httpx.Client(timeout=600) as cl:
        for tid, email in ceos[:20]:
            r = cl.post(base + '/auth/login', json={'email': email, 'password': 'Seereon@123'})
            if r.status_code != 200:
                print('skip', tid, r.status_code)
                continue
            h = {'Authorization': f'Bearer {r.json()["data"]["token"]}'}
            row = {'tenant': tid}
            cold_total = 0.0
            warm_total = 0.0
            for p in PAGES:
                cold_total += timed(cl, 'GET', base + p, headers=h)[0]
                warm_total += timed(cl, 'GET', base + p, headers=h)[0]
            row['cold_pages_ms'] = round(cold_total, 1)
            row['warm_pages_ms'] = round(warm_total, 1)
            row['rss_mb'] = rss_mb(pid)
            B['tenants'].append(row)
            print('B', row, flush=True)
    B['rss_20_cached_mb'] = rss_mb(pid)
    B['n_tenants'] = len(B['tenants'])
    stop(a.port)
    res['twenty'] = B
    print(json.dumps({'big': {k: v for k, v in A.items() if k != 'pages'}, 'twenty': {k: v for k, v in B.items() if k != 'tenants'}}))
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, 'w') as f:
            json.dump(res, f, indent=1)


if __name__ == '__main__':
    main(sys.argv[1:])
