"""Development / test database reset (refused when APP_ENV=production). Runs on the OWNER connection
(MIGRATION_DATABASE_URL, else DATABASE_URL).

    python -m app.db.reset              DROP SCHEMA public → migrate (an EMPTY schema, like production)
    python -m app.db.reset --migrate    migrate only

Sample data for local development: `python -m app.devtools.fixtures load-demo` (never in production).
`reseed_in_place` (the fixture CLI's --in-place, our test harness only): TRUNCATE every data table, re-insert the data
and bump every tenant_table_versions row, in ONE transaction (concurrent requests wait on the TRUNCATE lock and then
see fresh data; versions only increase, so every running API process's cache invalidates).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

import psycopg

from app.db.mapping import ident, load_meta
from app.db.migrate import migrate
from app.db.seed import SeedData, hash_all, seed_database

PLATFORM_DATA_TABLES = [
    'user_credentials',
    'sessions',
    'login_attempts',
    'reset_tokens',
    'idempotency_keys',
    'email_deliveries',
    'rate_limits',
    'security_events',
]
# V007 (the flush change log), V008 (worker liveness / job runs); absent on databases not yet migrated
PLATFORM_DATA_TABLES_OPTIONAL = ['table_changes', 'worker_heartbeats', 'job_runs']


def _refuse_production() -> None:
    env = os.environ.get('APP_ENV') or os.environ.get('NODE_ENV')
    if env == 'production':
        raise RuntimeError('db reset is disabled in production')


def drop_all(conn: psycopg.Connection) -> None:
    _refuse_production()
    conn.execute('DROP SCHEMA IF EXISTS public CASCADE')
    conn.execute('CREATE SCHEMA public')
    conn.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
    conn.execute('GRANT USAGE ON SCHEMA public TO seereon_app')
    conn.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO seereon_app')


def reset_database(conn: psycopg.Connection, data: SeedData | None = None, log: Callable[[str], None] = print) -> None:
    """DROP SCHEMA → migrate (→ load `data` when given: development fixtures / tests)"""
    drop_all(conn)
    migrate(conn, log=log)
    if data is not None:
        seed_database(conn, data, log)


def reseed_in_place(
    conn: psycopg.Connection, data: SeedData, log: Callable[[str], None] = lambda s: None, hashes: list[tuple[str, str]] | None = None
) -> None:
    _refuse_production()
    if hashes is None:
        hashes = hash_all(data.get('passwords') or [])  # slow: outside the transaction
    meta = load_meta(conn)
    tables = [*meta.tables.keys(), *PLATFORM_DATA_TABLES]
    tables += [t for t in PLATFORM_DATA_TABLES_OPTIONAL if conn.execute('SELECT to_regclass(%s) IS NOT NULL', [f'public.{t}']).fetchone()[0]]  # type: ignore[index]
    with conn.transaction():
        conn.execute(f'TRUNCATE {", ".join(ident(t) for t in tables)} RESTART IDENTITY')  # type: ignore[arg-type]
        seed_database(conn, data, log, own_transaction=False, hashes=hashes)
        conn.execute('UPDATE tenant_table_versions SET version = version + 1')


def main(argv: list[str]) -> None:
    from app.db.migrate import migration_url
    from app.db.pool import connect

    _refuse_production()
    with connect(migration_url()) as c:
        if '--migrate' in argv:
            migrate(c)
        else:
            reset_database(c)
            print('empty schema ready; sample data: python -m app.devtools.fixtures load-demo')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        print(e, file=sys.stderr)
        sys.exit(1)
