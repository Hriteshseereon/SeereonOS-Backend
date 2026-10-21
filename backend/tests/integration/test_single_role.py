"""One database, one runtime role: the API runs on DATABASE_URL = seereon_app only. Proves that
  * the role is least-privilege (no superuser / BYPASSRLS / table ownership / DDL) and the startup checks refuse an
    owner or superuser connection in production;
  * row-level security still isolates tenants on that single role;
  * credentials, sessions, tokens, rate limits, the audit trail and the delivery queue are reachable only through the
    SECURITY DEFINER functions of V008, which refuse to act outside the tenant of the transaction;
  * no SECURITY DEFINER function is executable by PUBLIC, and every one pins its search_path.
"""

from __future__ import annotations

import psycopg
import pytest

from tests.conftest import APP, OWNER

PLATFORM_TABLES = ['user_credentials', 'sessions', 'reset_tokens', 'login_attempts', 'rate_limits', 'security_events', 'idempotency_keys', 'email_deliveries']


def app_conn() -> psycopg.Connection:
    return psycopg.connect(APP, autocommit=True)


def test_runtime_config_has_one_connection_string(app):  # type: ignore[no-untyped-def]
    cfg = app.state.cfg
    assert cfg.database_url == APP and not hasattr(cfg, 'database_app_url')
    pools = app.state.rt.backend.pools
    assert not hasattr(pools, 'owner')
    with pools.app.connection() as c:
        assert c.execute('SELECT current_user').fetchone()[0] == 'seereon_app'


def test_role_is_least_privilege(db):  # type: ignore[no-untyped-def]
    with app_conn() as c:
        sup, bypass, inherit = c.execute('SELECT rolsuper, rolbypassrls, rolinherit FROM pg_roles WHERE rolname = current_user').fetchone()
        assert (sup, bypass, inherit) == (False, False, False)
        assert c.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user").fetchone()[0] == 0
        for t in PLATFORM_TABLES:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f'SELECT 1 FROM {t} LIMIT 1')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('CREATE TABLE evil (x int)')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('DELETE FROM tenant_table_versions')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('DELETE FROM table_changes')


def test_startup_refuses_owner_or_superuser_in_production(db):  # type: ignore[no-untyped-def]
    from app.db.pool import Pools
    from app.startup import StartupError, check_database

    pools = Pools(OWNER, 2)
    try:
        with pytest.raises(StartupError) as e:
            check_database(pools, production=True)
        assert 'owner of' in str(e.value) and 'MIGRATION_DATABASE_URL' in str(e.value)
        check_database(pools, production=False)  # development: a warning only
    finally:
        pools.close()
    pools = Pools(APP, 2)
    try:
        info = check_database(pools, production=True)
        assert info['role'] == 'seereon_app' and info['migrations_pending'] == []
    finally:
        pools.close()


def test_rls_isolates_tenants_on_the_single_role(db):  # type: ignore[no-untyped-def]
    with app_conn() as c:
        assert c.execute('SELECT count(*) FROM projects').fetchone()[0] == 0  # no tenant set: nothing
        with c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_seereon', true)")
            tenants = {r[0] for r in c.execute('SELECT DISTINCT tenant_id FROM users').fetchall()}
            assert tenants == {'tn_seereon'}
            assert c.execute("SELECT count(*) FROM projects WHERE tenant_id = 'tn_kalinga'").fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):  # a row for another tenant cannot be written
                c.execute("INSERT INTO holidays (id, tenant_id, date, name, optional) VALUES ('h_x', 'tn_kalinga', '2026-12-25', 'X', false)")
        with c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_seereon', true)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):  # nor moved to another tenant
                c.execute("UPDATE holidays SET tenant_id = 'tn_kalinga' WHERE tenant_id = 'tn_seereon'")
        with c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_kalinga', true)")
            assert c.execute("SELECT count(*) FROM users WHERE tenant_id = 'tn_seereon'").fetchone()[0] == 0


def test_definer_functions_refuse_other_tenants(db):  # type: ignore[no-untyped-def]
    from app.auth.passwords import hash_password

    h = hash_password('Some long passphrase 1', gated=False)
    with app_conn() as c:
        kalinga_user = c.execute("SELECT id FROM seereon_user_by_email('ananya@kalingasoft.in')").fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_seereon', true)")
            c.execute('SELECT seereon_set_password(%s, %s)', [kalinga_user, h])
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_seereon', true)")
            c.execute("SELECT seereon_create_session(%s, %s, 'tn_kalinga', now(), 30, NULL, NULL)", ['a' * 64, kalinga_user])
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():  # no tenant at all
            c.execute('SELECT seereon_create_reset_token(%s, %s, 0, false)', ['b' * 64, kalinga_user])
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_seereon', true)")
            c.execute("SELECT seereon_idempotency_put('tn_kalinga', 'k', 'POST', '/x', 200, '{}')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_seereon', true)")
            c.execute("SELECT seereon_email_enqueue_platform('pm_x', 'tn_kalinga', 'a@b.c', 's', 'b', NULL)")
        # a plaintext / malformed value is never stored as a credential or session
        with pytest.raises(psycopg.errors.InvalidParameterValue), c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_kalinga', true)")
            c.execute("SELECT seereon_set_password(%s, 'Seereon@123')", [kalinga_user])
        with pytest.raises(psycopg.errors.InvalidParameterValue), c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_kalinga', true)")
            c.execute("SELECT seereon_create_session('plain-token', %s, 'tn_kalinga', now(), 30, NULL, NULL)", [kalinga_user])
        # bulk reads need a tenant (or the platform scope)
        assert c.execute('SELECT count(*) FROM seereon_password_hashes(NULL, ARRAY[%s])', [kalinga_user]).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM seereon_password_hashes('tn_seereon', ARRAY[%s])", [kalinga_user]).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM seereon_password_hashes('tn_kalinga', ARRAY[%s])", [kalinga_user]).fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM seereon_sessions(NULL, 12)').fetchone()[0] == 0
        with c.transaction():
            c.execute("SELECT set_config('app.tenant_id', 'tn_seereon', true)")
            assert c.execute("SELECT seereon_revoke_sessions(ARRAY(SELECT token_hash FROM seereon_sessions('tn_kalinga', 9999)))").fetchone()[0] == 0
        # the audit trail is append-only for the runtime
        c.execute("SELECT seereon_log_security_event('test.event', 'info', NULL, NULL, NULL, NULL, NULL, 'rid', NULL)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM security_events WHERE event = 'test.event'")


def test_platform_scope_reads_all_tenants(db):  # type: ignore[no-untyped-def]
    with app_conn() as c, c.transaction():
        c.execute("SELECT set_config('app.platform_admin', 'on', true)")
        assert {r[0] for r in c.execute('SELECT DISTINCT tenant_id FROM users').fetchall()} >= {'tn_seereon', 'tn_kalinga', 'platform'}


def test_no_definer_function_is_public_and_search_path_is_pinned(db):  # type: ignore[no-untyped-def]
    with psycopg.connect(OWNER, autocommit=True) as c:
        rows = c.execute(
            """SELECT p.proname, p.prosecdef, p.proconfig, p.proacl::text
                 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.proname LIKE 'seereon\\_%'"""
        ).fetchall()
    assert len(rows) >= 30
    for name, secdef, config, acl in rows:
        assert any(x.startswith('search_path=') for x in (config or [])), name
        assert acl is not None and not any(e.startswith('=') for e in acl.strip('{}').split(',')), (name, acl)  # no PUBLIC grant
        if secdef and name not in ('seereon_user_in_scope', 'seereon_require_user_in_scope'):
            assert 'seereon_app=X' in acl, (name, acl)
    internal = {n: acl for n, _s, _c, acl in rows if n in ('seereon_user_in_scope', 'seereon_require_user_in_scope')}
    assert all('seereon_app' not in a for a in internal.values())
