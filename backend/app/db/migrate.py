"""Migration runner (same schema_migrations rows and sha256 checksums as the Node-era runner).

Applies database/migrations/V###__name.sql in version order, each file in its own transaction, recorded with the
sha256 of its contents. An applied file whose contents changed is refused (add a new migration instead). Concurrent
runners are serialised with an advisory lock.

    python -m app.db.migrate             apply pending migrations
    python -m app.db.migrate --status    list applied / pending (exit 1 when something is pending)

Connection: MIGRATION_DATABASE_URL — the OWNER role (seereon_owner), used by this command only; when it is not set,
DATABASE_URL is used if that role has the rights (single-role local setups). The API, the worker and the CLI never
read MIGRATION_DATABASE_URL.
Files: MIGRATIONS_DIR, default ../database/migrations relative to backend/ (monorepo layout), database/migrations inside
the backend repository (separate-repository layout) or /app/migrations
(the Docker image copies database/migrations there at build time).
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def default_migrations_dir() -> str:
    explicit = os.environ.get('MIGRATIONS_DIR')
    if explicit:
        return explicit
    # backend repository (database/ inside it), monorepo (backend/ next to database/), Docker image, legacy
    for d in (os.path.join(_BACKEND, 'database', 'migrations'), os.path.join(_BACKEND, '..', 'database', 'migrations'),
              '/app/migrations', os.path.join(_BACKEND, 'migrations')):
        if os.path.isdir(d):
            return os.path.abspath(d)
    return os.path.abspath(os.path.join(_BACKEND, '..', 'database', 'migrations'))


MIGRATIONS_DIR = default_migrations_dir()
LOCK_KEY = 7_405_311_001


@dataclass
class MigrationFile:
    version: str
    order: int
    checksum: str
    sql: str


def read_migrations(directory: str = MIGRATIONS_DIR) -> list[MigrationFile]:
    out = []
    for f in os.listdir(directory):
        if not re.fullmatch(r'V\d+__[\w-]+\.sql', f, re.ASCII):
            continue
        with open(os.path.join(directory, f), 'rb') as fh:
            raw = fh.read()
        out.append(MigrationFile(f, int(re.match(r'V(\d+)', f).group(1)), hashlib.sha256(raw).hexdigest(), raw.decode('utf-8')))  # type: ignore[union-attr]
    return sorted(out, key=lambda m: m.order)


def migrate(conn: psycopg.Connection, directory: str = MIGRATIONS_DIR, log: Callable[[str], None] = print) -> list[str]:
    files = read_migrations(directory)
    seen: set[int] = set()
    for f in files:
        if f.order in seen:
            raise RuntimeError(f'Duplicate migration version V{f.order} ({f.version})')
        seen.add(f.order)
    conn.autocommit = True
    conn.execute('SELECT pg_advisory_lock(%s)', [LOCK_KEY])
    try:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())'
        )
        applied = dict(conn.execute('SELECT version, checksum FROM schema_migrations').fetchall())
        for v in applied:
            if not any(f.version == v for f in files):
                raise RuntimeError(f'Applied migration {v} is missing from {directory}')
        done: list[str] = []
        for f in files:
            prev = applied.get(f.version)
            if prev is not None:
                if prev != f.checksum:
                    raise RuntimeError(
                        f'Migration {f.version} was changed after it was applied (checksum {prev[:12]}… → {f.checksum[:12]}…). Add a new migration instead.'
                    )
                continue
            t0 = time.time()
            try:
                with conn.transaction():
                    conn.execute(f.sql)  # type: ignore[arg-type]
                    conn.execute('INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)', [f.version, f.checksum])
            except Exception as e:
                raise RuntimeError(f'Migration {f.version} failed: {e}') from e
            done.append(f.version)
            log(f'applied {f.version} ({int((time.time() - t0) * 1000)} ms)')
        if not done:
            log('schema is up to date')
        return done
    finally:
        conn.execute('SELECT pg_advisory_unlock(%s)', [LOCK_KEY])


def pending(conn: psycopg.Connection, directory: str = MIGRATIONS_DIR) -> tuple[list[str], list[str]]:
    """(applied versions, pending versions) — read-only; works for the runtime role (SELECT on schema_migrations)"""
    files = read_migrations(directory)
    exists = conn.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()[0]  # type: ignore[index]
    applied = [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall()] if exists else []
    return applied, [f.version for f in files if f.version not in applied]


def migration_url(env: dict | None = None) -> str:
    env = dict(os.environ if env is None else env)
    url = env.get('MIGRATION_DATABASE_URL') or env.get('DATABASE_URL')
    if not url:
        raise RuntimeError('Set MIGRATION_DATABASE_URL (the owner role, seereon_owner) to run migrations.')
    return url


def main(argv: list[str] | None = None) -> None:
    from app.db.pool import connect

    argv = sys.argv[1:] if argv is None else argv
    with connect(migration_url()) as c:
        if '--status' in argv:
            applied, todo = pending(c)
            print(f'migrations: {MIGRATIONS_DIR}')
            for v in applied:
                print(f'  applied  {v}')
            for v in todo:
                print(f'  pending  {v}')
            if todo:
                sys.exit(1)
            return
        migrate(c)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(e, file=sys.stderr)
        sys.exit(1)
