"""Shared test fixtures.

Database: the throwaway TEST database `seereonos_test` (nothing in the runtime configuration references it). Create it
once (as a superuser) with the same bootstrap as every environment:

    psql -U postgres -v dbname=seereonos_test -v owner_password=seereon_owner -v app_password=seereon_app \\
         -f database/bootstrap.sql

Two connection strings, exactly like production:
  * TEST_DATABASE_URL            the runtime role seereon_app — the ONLY one the API under test gets (DATABASE_URL);
  * TEST_MIGRATION_DATABASE_URL  the owner — used by the tests for migrations, the demo fixture and out-of-band SQL.
The session resets the schema (DROP SCHEMA -> migrations -> development demo fixture, dates shifted to this week);
`fresh` reloads the fixture in place between tests that need pristine data. Golden tests (tests/golden) need no
database. Run everything with TZ=Asia/Kolkata (the reference's process TZ).
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

# The reference runs with process TZ Asia/Kolkata; pin it before any app module computes a date, so the suite is
# correct however it is launched (importing pytest does not read the local time zone).
os.environ['TZ'] = 'Asia/Kolkata'
time.tzset()
# e-mail links in golden vectors use the reference's URL
os.environ.setdefault('APP_URL', 'https://app.seereonos.com')
for _k in ('DATABASE_APP_URL', 'DEMO_MODE', 'SEED_DEMO', 'SEED_JSON'):  # removed settings must not leak in from a shell
    os.environ.pop(_k, None)

OWNER = os.environ.get('TEST_MIGRATION_DATABASE_URL', 'postgres://seereon_owner:seereon_owner@localhost:5432/seereonos_test')
APP = os.environ.get('TEST_DATABASE_URL', 'postgres://seereon_app:seereon_app@localhost:5432/seereonos_test')
PASSWORD = 'Seereon@123'


def env_for(**over: Any) -> dict[str, str]:
    env = {
        'APP_ENV': 'test',
        'DATABASE_URL': APP,
        'LOG_LEVEL': 'silent',
        'STATIC_DIR': '',
        'APP_URL': 'https://app.seereonos.com',
        'RATE_LIMIT_AUTH': '100000',
        'RATE_LIMIT_GLOBAL': '1000000',
        'TZ': 'Asia/Kolkata',
    }
    env.update({k: str(v) for k, v in over.items()})
    return env


def _db_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(OWNER, connect_timeout=3) as c:
            c.execute('SELECT 1')
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope='session')
def seed() -> dict[str, Any]:
    """the development demo fixture (shifted to this week) + its password hashes, computed once"""
    from app.db.seed import hash_all
    from app.devtools.fixtures import demo_data

    data, info = demo_data()
    return {'data': data, 'hashes': hash_all(data.get('passwords') or []), 'info': info}


@pytest.fixture(scope='session')
def db(seed: dict[str, Any]) -> str:
    if not _db_available():
        pytest.skip(f'test database not reachable ({OWNER}); see tests/conftest.py')
    from app.db.migrate import migrate
    from app.db.pool import connect
    from app.db.reset import drop_all
    from app.db.seed import seed_database

    os.environ.pop('APP_ENV', None)
    with connect(OWNER) as c:
        drop_all(c)
        migrate(c, log=lambda s: None)
        seed_database(c, seed['data'], log=lambda s: None, hashes=seed['hashes'])
    return OWNER


def reload_fixture(seed: dict[str, Any]) -> None:
    """pristine demo data, in place (versions monotonic: running runtimes see it)"""
    from app.db.pool import connect
    from app.devtools.fixtures import load

    with connect(OWNER) as c:
        load(c, seed['data'], in_place=True, hashes=seed['hashes'], log=lambda s: None)


@pytest.fixture(scope='session')
def app(db: str, seed: dict[str, Any]):  # type: ignore[no-untyped-def]
    from app.config import load_config
    from app.main import create_app

    a = create_app(load_config(env_for()))
    yield a
    a.state.rt.backend.pools.close()


class Api:
    def __init__(self, client: Any) -> None:
        self.c = client
        self.tokens: dict[str, str] = {}

    def call(
        self, method: str, path: str, body: Any = None, token: str | None = None, headers: dict[str, str] | None = None, query: dict[str, Any] | None = None
    ):  # type: ignore[no-untyped-def]
        h = dict(headers or {})
        if token:
            h['Authorization'] = 'Bearer ' + token
        kw: dict[str, Any] = {'headers': h, 'params': query}
        if body is not None:
            kw['json'] = body
        return self.c.request(method, '/api/v1' + path, **kw)

    def login(self, email: str, password: str = PASSWORD) -> str:
        r = self.call('POST', '/auth/login', {'email': email, 'password': password})
        assert r.status_code == 200, r.text
        return r.json()['data']['token']

    def as_(self, email: str) -> str:
        if email not in self.tokens:
            self.tokens[email] = self.login(email)
        return self.tokens[email]


@pytest.fixture(scope='session')
def client(app):  # type: ignore[no-untyped-def]
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def api(client) -> Api:  # type: ignore[no-untyped-def]
    return Api(client)


@pytest.fixture()
def fresh(app, client, seed):  # type: ignore[no-untyped-def]
    """pristine demo data before the test (the fixture CLI's --in-place, like the parity harness)"""
    reload_fixture(seed)
    yield


def owner_sql(sql: str, *params: Any) -> Any:
    """out-of-band SQL as the owner (assertions on platform tables the runtime role cannot read)"""
    import psycopg

    with psycopg.connect(OWNER, autocommit=True) as c:
        cur = c.execute(sql, params or None)  # type: ignore[arg-type]
        try:
            return cur.fetchall()
        except psycopg.ProgrammingError:
            return None


def platform_mails(to: str) -> list[dict[str, Any]]:
    """platform e-mails (reset links, invitations) queued for `to`, newest first, bodies opened with the app's key"""
    from app.engine.secrets import open_secret

    rows = owner_sql(
        "SELECT message_id, subject, body, status, expires_at FROM email_deliveries WHERE kind = 'platform' AND to_addr = %s ORDER BY created_at DESC, message_id",
        to,
    )
    return [{'id': m, 'subject': s, 'body': open_secret(b) if b else None, 'sealed': b, 'status': st, 'expires_at': e} for m, s, b, st, e in rows or []]


def link_token(body: str, marker: str) -> str:
    """the token of the first link containing `marker` (e.g. '/reset-password?token=', '/invite/')"""
    import re
    from urllib.parse import unquote

    m = re.search(re.escape(marker) + r'([^\s&]+)', body or '')
    assert m, f'no {marker} link in {body!r}'
    return unquote(m.group(1))
