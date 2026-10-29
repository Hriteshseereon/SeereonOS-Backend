"""DEVELOPMENT FIXTURES — never for production (refused when APP_ENV=production).

    python -m app.devtools.fixtures load-demo [--today YYYY-MM-DD] [--file PATH] [--in-place] [--dump-json PATH]

Loads the demo data set (database/fixtures/demo/demo_seed.json.gz: two demo workspaces, a platform super admin,
projects, timesheets, payroll… exported ONCE from the frozen TypeScript seed) into a development database. The data was
generated relative to its export day; every date and timestamp — in columns, inside JSON values and inside ids such as
`wk_<user>_<monday>` — is shifted by WHOLE WEEKS so the export day lands on the latest same weekday on or before
--today (default: today), so Mondays stay Mondays and "this week" looks like this week.

  (default)    the database must have no workspaces yet (a fresh `python -m app.db.migrate`)
  --in-place   replace ALL data atomically, keeping cache versions monotonic, so a RUNNING API stays coherent — our
               test / parity harness only (it wipes every account and session)
  --dump-json  also write the shifted tables (the parity harness loads the same data into the TypeScript reference)

Demo accounts use the password Seereon@123 (e.g. santosh@seereon.co.in, platform@seereon.co.in) — development only.
Connection: the OWNER role (MIGRATION_DATABASE_URL, else DATABASE_URL): it writes credentials directly. No Node.js
needed. Prints a warning on every run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import os
import re
import sys
import time
from typing import Any

import orjson

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_FIXTURE = os.path.join('database', 'fixtures', 'demo', 'demo_seed.json.gz')
# backend repository (database/ inside it) or monorepo (backend/ next to database/)
DEFAULT_FILE = next((os.path.abspath(p) for p in (os.path.join(_BACKEND, _FIXTURE), os.path.join(_BACKEND, '..', _FIXTURE))
                     if os.path.isfile(p)), os.path.abspath(os.path.join(_BACKEND, '..', _FIXTURE)))
DEMO_PASSWORD_NOTE = 'demo accounts use the password Seereon@123'
_DATE = re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)')


class FixtureError(RuntimeError):
    pass


def refuse_production(env: dict | None = None) -> None:
    env = dict(os.environ if env is None else env)
    if (env.get('APP_ENV') or env.get('NODE_ENV')) == 'production':
        raise FixtureError('Demo fixtures are development data and are refused when APP_ENV=production.')


def read_fixture(path: str = DEFAULT_FILE) -> dict[str, Any]:
    if not os.path.exists(path):
        raise FixtureError(f'No fixture at {path} (database/fixtures/demo/ is part of the database deliverable; pass --file).')
    with open(path, 'rb') as f:
        raw = f.read()
    if path.endswith('.gz'):
        raw = gzip.decompress(raw)
    d = orjson.loads(raw)
    if not isinstance(d, dict) or 'tables' not in d or 'generated_on' not in d:
        raise FixtureError(f'{path} is not a fixture file ({{generated_on, tables}}).')
    return d


def week_shift_days(generated_on: str, today: str) -> int:
    """whole weeks (in days) that move `generated_on` to the latest same weekday on or before `today`"""
    g = dt.date.fromisoformat(generated_on)
    t = dt.date.fromisoformat(today)
    return 7 * ((t - g).days // 7)


def _shift_str(s: str, days: int) -> str:
    if '-' not in s or len(s) < 10:
        return s

    def rep(m: re.Match[str]) -> str:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return m.group(0)
        return (d + dt.timedelta(days=days)).isoformat()

    return _DATE.sub(rep, s)


def shift(v: Any, days: int) -> Any:
    if days == 0:
        return v
    if isinstance(v, str):
        return _shift_str(v, days)
    if isinstance(v, list):
        return [shift(x, days) for x in v]
    if isinstance(v, dict):
        return {k: shift(x, days) for k, x in v.items()}
    return v


def link_demo_rows(tables: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """V9-FIX: bring the v8-era export up to the v9 data rules at load time (idempotent — only fills what is missing
    or stale, so loading an already linked export changes nothing):

    * projects.client_id (V010, D2): the customer whose name matches the project's client text (case/space-insensitive,
      first customer in table order — the same rule as V010's backfill, which runs before the fixture is loaded);
    * milestone dates (G4.02 / BR-20): a milestone with tasks starts at its earliest task start and ends on its last
      task's due date — what the sweep writes on the first read, so every screen agrees from the first request.
    """
    n = {'projects_linked': 0, 'milestones_synced': 0}
    by_name: dict[tuple[str, str], str] = {}
    for c in tables.get('clients') or []:
        by_name.setdefault((c['tenant_id'], (c.get('name') or '').strip().lower()), c['id'])
    for p in tables.get('projects') or []:
        if p.get('client_id'):
            continue
        cid = by_name.get((p['tenant_id'], (p.get('client') or '').strip().lower()))
        if cid:
            p['client_id'] = cid
            n['projects_linked'] += 1
    tasks_of: dict[Any, list[dict[str, Any]]] = {}
    for t in tables.get('tasks') or []:
        if t.get('milestone_id'):
            tasks_of.setdefault(t['milestone_id'], []).append(t)
    for m in tables.get('milestones') or []:
        mine = tasks_of.get(m['id'])
        if m.get('deleted_at') or not mine:
            continue
        end = max(t['due_on'] for t in mine)
        start = min(t['start_on'] for t in mine)
        if m.get('planned_end') != end or m.get('planned_start') != start:
            m['planned_end'], m['planned_start'] = end, start
            n['milestones_synced'] += 1
    return n


def demo_data(today: str | None = None, path: str = DEFAULT_FILE) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """(tables shifted to `today` and linked (link_demo_rows), info)"""
    fx = read_fixture(path)
    today = today or time.strftime('%Y-%m-%d')
    days = week_shift_days(fx['generated_on'], today)
    tables = {k: shift(v, days) for k, v in fx['tables'].items()}
    linked = link_demo_rows(tables)
    anchor = (dt.date.fromisoformat(fx['generated_on']) + dt.timedelta(days=days)).isoformat()
    info = {'file': path, 'generated_on': fx['generated_on'], 'today': today, 'shift_days': days, 'data_today': anchor, **linked}
    return tables, info


def load(conn: Any, tables: dict[str, list[dict[str, Any]]], *, in_place: bool = False, hashes: list[tuple[str, str]] | None = None, log: Any = print) -> int:
    """load into the migrated schema of `conn` (owner role); returns the row count"""
    from app.db.reset import reseed_in_place
    from app.db.seed import hash_all, seed_database

    refuse_production()
    if hashes is None:
        hashes = hash_all(tables.get('passwords') or [])
    if in_place:
        reseed_in_place(conn, tables, log=log, hashes=hashes)
    else:
        n = conn.execute('SELECT count(*) FROM tenants').fetchone()[0]
        if n:
            raise FixtureError(f'The database already has {n} workspace(s). Load the demo into an empty development database (python -m app.db.reset), or use --in-place to replace ALL data.')
        seed_database(conn, tables, log, hashes=hashes)
    return sum(len(v) for k, v in tables.items() if k != 'passwords')


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog='python -m app.devtools.fixtures', description='Development fixtures (refused in production)')
    sub = p.add_subparsers(dest='cmd', required=True)
    a1 = sub.add_parser('load-demo', help='load the demo data set into a development database')
    a1.add_argument('--today', help='YYYY-MM-DD the data should look current for (default: today)')
    a1.add_argument('--file', default=DEFAULT_FILE, help='fixture file (default: database/fixtures/demo/demo_seed.json.gz)')
    a1.add_argument('--in-place', action='store_true', help='replace ALL data atomically (test harness; keeps a running API coherent)')
    a1.add_argument('--dump-json', help='also write the shifted tables to this JSON file')
    a = p.parse_args(argv)
    try:
        refuse_production()
        if a.today and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', a.today):
            raise FixtureError('--today must be YYYY-MM-DD')
        print('WARNING: loading DEVELOPMENT demo data (fake people and companies; ' + DEMO_PASSWORD_NOTE + '). Never use it in production.', file=sys.stderr)
        tables, info = demo_data(a.today, a.file)
        if a.dump_json:
            os.makedirs(os.path.dirname(os.path.abspath(a.dump_json)), exist_ok=True)
            with open(a.dump_json, 'wb') as f:
                f.write(orjson.dumps({'generated_on': info['data_today'], 'tables': tables}))
        from app.db.migrate import migration_url
        from app.db.pool import connect

        with connect(migration_url()) as c:
            n = load(c, tables, in_place=a.in_place, log=lambda s: print(s, file=sys.stderr))
        print(orjson.dumps({**info, 'rows': n, 'in_place': a.in_place}).decode())
        return 0
    except FixtureError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
