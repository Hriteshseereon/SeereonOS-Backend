"""In-process profiler for one API call (production config, warm per-process cache).

    cd backend && TZ=Asia/Kolkata python -m tests.perf.profile --user santosh@bigco.example GET /dashboards/ceo [--n 3]
        [--body '{...}'] [--sort cumulative|tottime] [--limit 40] [--cold] [--out file.prof]

Runs the request once to warm the tenant cache (unless --cold), then N times under cProfile; prints wall times and the
top functions. Writes go to the perf DB (use GET calls, or a disposable database).
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys
import time


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('method')
    ap.add_argument('path')
    ap.add_argument('--user', default='santosh@bigco.example')
    ap.add_argument('--password', default='Seereon@123')
    ap.add_argument('--db', default='seereonos_perf_dev')
    ap.add_argument('--n', type=int, default=3)
    ap.add_argument('--body', default=None)
    ap.add_argument('--sort', default='cumulative')
    ap.add_argument('--limit', type=int, default=45)
    ap.add_argument('--cold', action='store_true')
    ap.add_argument('--out', default=None)
    ap.add_argument('--view-as', default=None)
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
    )
    os.environ.setdefault('MEMO_MAX_MB', '0')  # profile the computation, not the response memo
    time.tzset()
    import orjson

    from app.main import create_app
    from app.runtime import ClientInfo, ExecRequest

    app = create_app(start_workers=False)
    rt = app.state.rt

    def call(method: str, path: str, body: bytes | None = None, token: str | None = None) -> tuple[int, bytes]:
        from urllib.parse import parse_qsl, urlsplit

        u = urlsplit(path)
        q = dict(parse_qsl(u.query))
        hdr: dict = {'Authorization': f'Bearer {token}' if token else None, 'X-View-As': a.view_as, 'If-Match': None, 'Idempotency-Key': None}
        from app.js import UNDEF

        res = rt.execute(
            ExecRequest(method=method, path=u.path, query=q, body_raw=body, body=orjson.loads(body) if body else UNDEF, headers=hdr, client=ClientInfo('127.0.0.1', 'perf', 'perf-req-0001'))
        )
        return res.status, res.body

    st, b = call('POST', '/auth/login', orjson.dumps({'email': a.user, 'password': a.password}))
    assert st == 200, b[:300]
    token = orjson.loads(b)['data']['token']
    body = a.body.encode() if a.body else None
    if not a.cold:
        t0 = time.perf_counter()
        st, out = call(a.method, a.path, body, token)
        print(f'warm-up: {st} {(time.perf_counter() - t0) * 1000:.1f} ms, {len(out)} bytes')
    pr = cProfile.Profile()
    times = []
    for _ in range(a.n):
        t0 = time.perf_counter()
        pr.enable()
        st, out = call(a.method, a.path, body, token)
        pr.disable()
        times.append((time.perf_counter() - t0) * 1000)
    print(f'{a.method} {a.path}: status {st}, {len(out)} bytes, wall ms {[round(t, 1) for t in times]}')
    if st >= 400:
        print(out[:500])
    if a.out:
        pr.dump_stats(a.out)
    s = io.StringIO()
    pstats.Stats(pr, stream=s).strip_dirs().sort_stats(a.sort).print_stats(a.limit)
    print(s.getvalue())


if __name__ == '__main__':
    main(sys.argv[1:])
