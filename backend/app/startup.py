"""Startup checks shared by the API and the worker: the database role and the schema version.

* The runtime role must be least-privilege: not a superuser, not BYPASSRLS, and not the owner of the tables (the
  owner is not filtered by row-level security because RLS is not FORCEd). Production refuses to start otherwise;
  development / test only warn (a single-role local database is allowed there).
* Every migration in MIGRATIONS_DIR must be applied (production refuses with "run python -m app.db.migrate").
  Without a migrations directory (an image built without it) the V008 functions must at least exist.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger('seereonos')


class StartupError(RuntimeError):
    pass


def check_database(pools: Any, production: bool, component: str = 'api') -> dict[str, Any]:
    from app.db.migrate import MIGRATIONS_DIR, pending

    problems: list[str] = []
    warnings: list[str] = []
    with pools.app.connection() as c:
        user, superuser, bypass = c.execute('SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user').fetchone()  # type: ignore[misc]
        owned = c.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user").fetchone()[0]  # type: ignore[index]
        has_v008 = c.execute("SELECT to_regprocedure('seereon_resolve_session(text,integer)') IS NOT NULL").fetchone()[0]  # type: ignore[index]
        applied: list[str] = []
        todo: list[str] = []
        if os.path.isdir(MIGRATIONS_DIR):
            try:
                applied, todo = pending(c, MIGRATIONS_DIR)
            except Exception as e:  # noqa: BLE001
                warnings.append(f'cannot read schema_migrations ({type(e).__name__}); run python -m app.db.migrate')
    if superuser or bypass:
        problems.append(f'DATABASE_URL connects as "{user}", a superuser / BYPASSRLS role: use the least-privilege role seereon_app.')
    if owned:
        problems.append(
            f'DATABASE_URL connects as "{user}", the owner of {owned} tables (row-level security does not apply to the owner): '
            'use seereon_app; the owner credential belongs in MIGRATION_DATABASE_URL only.'
        )
    if not has_v008:
        problems.append('The database schema is not migrated (V008 functions missing): run python -m app.db.migrate.')
    if todo:
        problems.append(f'{len(todo)} migration(s) pending ({", ".join(todo)}): run python -m app.db.migrate.')
    info = {'role': user, 'migrations_applied': len(applied), 'migrations_pending': todo}
    for w in warnings:
        log.warning(w)
    if problems:
        if production or not has_v008 or todo:
            raise StartupError('Startup checks failed:\n  - ' + '\n  - '.join(problems))
        for p in problems:
            log.warning(f'{p} (allowed outside production)', extra={'component': component})
    return info
