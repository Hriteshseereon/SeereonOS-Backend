"""Production bootstrap and operations CLI (uses DATABASE_URL — the runtime role — like the API; no owner rights).

    python -m app.cli check
        configuration + database checks (role, migrations, worker heartbeat); exit 1 on a problem
    python -m app.cli create-superadmin --email ops@example.com --name "Platform Admin" [--password-stdin]
        the platform super administrator (tenant list, trials, suspensions). Prompts for the password twice unless
        --password-stdin. --update-password sets a new password for an existing super administrator.
    python -m app.cli create-tenant --company "Acme Pvt Ltd" --admin-email ceo@acme.example --admin-name "Asha Rao"
                                    [--industry it_services] [--password-stdin]
        a workspace with exactly the provisioning of self-service sign-up (roles + permissions, master data, e-mail and
        document templates, approval and HR workflows, subscription, number sequences) and its administrator.

Everything goes through the API's own unit of work, so the rows are identical to what the API writes.
"""

from __future__ import annotations

import argparse
import getpass
import random
import re
import sys
from typing import Any

import orjson


def _runtime(cfg: Any) -> Any:
    from app.auth.secret_box import create_secret_box
    from app.db.backend import PgBackend
    from app.db.pool import Pools
    from app.engine.secrets import set_secret_box
    from app.router import Router, load_modules
    from app.runtime import Runtime
    from app.startup import check_database

    pools = Pools(cfg.database_url, 4, cfg.statement_timeout_ms, application_name='seereonos-cli')
    check_database(pools, cfg.production, 'cli')
    backend = PgBackend(pools, queue_emails=cfg.email_delivery == 'smtp', queue_platform_mails=cfg.email_delivery == 'smtp' or not cfg.production)
    backend.init()
    router = Router()
    load_modules(router)
    set_secret_box(create_secret_box(cfg.mfa_encryption_key, list(cfg.mfa_encryption_keys_previous), allow_plain=not cfg.production))
    return Runtime(backend, router, None, freeze_rows=cfg.freeze_rows)


def _read_password(stdin: bool, email: str, min_len: int) -> str:
    from app.domain.password_policy import check_password

    if stdin:
        pw = sys.stdin.readline().rstrip('\n')
    else:
        pw = getpass.getpass('Password: ')
        if getpass.getpass('Repeat password: ') != pw:
            raise SystemExit('The passwords do not match.')
    weak = check_password(pw, min=min_len, email=email)
    if weak:
        raise SystemExit(f'Password rejected: {weak["message"]}')
    return pw


def _email(v: str) -> str:
    e = (v or '').strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', e):
        raise SystemExit(f'Not a valid e-mail address: {v!r}')
    return e


def create_superadmin(rt: Any, email: str, name: str, password: str, update_password: bool = False) -> dict[str, Any]:
    from app.ctx import now_iso, uid
    from app.db.uow import current_scope
    from app.domain.calendar import today_iso

    existing = rt.backend.user_by_email(email)
    if existing and not update_password:
        raise SystemExit(f'{email} already has an account (tenant {existing["tenant_id"]}). Use --update-password to change a super administrator password.')
    if existing and existing['tenant_id'] != 'platform':
        raise SystemExit(f'{email} belongs to workspace {existing["tenant_id"]}, not to the platform.')

    def fn(_s: Any) -> dict[str, Any]:
        s = current_scope()
        if existing:
            u = s.find_by('users', 'id', existing['id'])
            if u is None or not u.get('is_super_admin'):
                raise SystemExit(f'{email} is not a super administrator.')
            s.set_password(u['id'], password)
            s.patch('users', u, {'must_change_password': False, 'status': 'active', 'updated_at': now_iso(), 'version': u['version'] + 1})
            return {'user_id': u['id'], 'updated': True}
        t = now_iso()
        row = {
            'id': 'u_' + uid(),
            'tenant_id': 'platform',
            'created_at': t,
            'updated_at': t,
            'created_by': None,
            'version': 1,
            'email': email,
            'name': name,
            'title': 'SeereonOS Operations',
            'role': 'admin',
            'reports_to_id': None,
            'grants': [],
            'role_key': 'administrator',
            'extra_permissions': [],
            'revoked_permissions': [],
            'status': 'active',
            'last_login_at': None,
            'avatar_hue': int(random.random() * 360),
            'phone': None,
            'joined_on': today_iso(),
            'department': 'management',
            'two_factor': False,
            'prefs': {},
            'is_super_admin': True,
        }
        s.append('users', row)
        s.set_password(row['id'], password)
        return {'user_id': row['id'], 'updated': False}

    return rt.run_scoped('tenant', 'platform', fn, is_write=True)


def create_tenant(rt: Any, company: str, admin_email: str, admin_name: str, password: str, industry: str = 'it_services') -> dict[str, Any]:
    from app.ctx import Req, anon_ctx
    from app.errors import ApiError
    from app.js import UNDEF
    from app.routes.auth import create_workspace

    body = {'company': company, 'name': admin_name, 'email': admin_email, 'password': password, 'industry': industry}
    req = Req(params={}, query={}, body=body, if_match=UNDEF, ctx=anon_ctx(), headers={})

    def fn(s: Any) -> dict[str, Any]:
        user = create_workspace(req)
        tenant = s.find_by('tenants', 'id', user['tenant_id'])
        return {
            'tenant_id': tenant['id'],
            'slug': tenant['slug'],
            'status': tenant['status'],
            'trial_ends_on': tenant.get('trial_ends_on'),
            'admin_user_id': user['id'],
        }

    try:
        return rt.run_scoped('fresh', None, fn, is_write=True, emails=[admin_email], probe=True)
    except ApiError as e:
        raise SystemExit(f'{e.code}: {e.message}') from None


def check(cfg: Any) -> int:
    from app.db.pool import Pools
    from app.startup import StartupError, check_database

    pools = Pools(cfg.database_url, 2, cfg.statement_timeout_ms, application_name='seereonos-cli')
    ok = True
    try:
        try:
            info = check_database(pools, True, 'cli')
            print(f'database: ok (role {info["role"]}, {info["migrations_applied"]} migrations applied)')
        except StartupError as e:
            ok = False
            print(e)
        with pools.app.connection() as c:
            age = c.execute('SELECT extract(epoch FROM now() - max(last_beat_at)) FROM worker_heartbeats').fetchone()[0]  # type: ignore[index]
        print('worker: ' + ('no heartbeat (start python -m app.worker)' if age is None else f'last heartbeat {int(age)} s ago'))
        print(f'e-mail delivery: {cfg.email_delivery}; payments: {"razorpay" if cfg.payments_enabled else "off"}; app url: {cfg.app_url}')
        print(f'CORS origins: {", ".join(cfg.cors_origins) or "(none)"}; trusted proxies: {cfg.trust_proxy}')
        print(f'API docs (/api/docs, /api/v1/openapi.json): {"on" if cfg.api_docs else "off"}')
    finally:
        pools.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    from app.config import ConfigError, load_config, set_settings
    from app.logging_setup import configure_logging

    p = argparse.ArgumentParser(prog='python -m app.cli', description='SeereonOS operations CLI')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check', help='configuration and database checks')
    a1 = sub.add_parser('create-superadmin', help='create the platform super administrator')
    a1.add_argument('--email', required=True)
    a1.add_argument('--name', default='Platform Admin')
    a1.add_argument('--password-stdin', action='store_true', help='read the password from the first line of stdin')
    a1.add_argument('--update-password', action='store_true', help='set a new password for an existing super administrator')
    a2 = sub.add_parser('create-tenant', help='create a workspace and its administrator (same provisioning as sign-up)')
    a2.add_argument('--company', required=True)
    a2.add_argument('--admin-email', required=True)
    a2.add_argument('--admin-name', required=True)
    a2.add_argument('--industry', default='it_services', choices=['it_services', 'sap_consulting', 'product', 'other'])
    a2.add_argument('--password-stdin', action='store_true', help='read the password from the first line of stdin')
    a = p.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    set_settings(cfg)
    configure_logging('warn' if cfg.log_level not in ('silent',) else 'silent')
    if a.cmd == 'check':
        return check(cfg)
    if a.cmd == 'create-superadmin':
        email = _email(a.email)
        name = (a.name or '').strip()
        if len(name) < 2:
            raise SystemExit('Enter a name (--name).')
        pw = _read_password(a.password_stdin, email, 12)
        out = create_superadmin(_runtime(cfg), email, name, pw, a.update_password)
    else:
        email = _email(a.admin_email)
        if len(a.company.strip()) < 2 or len(a.admin_name.strip()) < 2:
            raise SystemExit('Enter the company and the administrator name.')
        pw = _read_password(a.password_stdin, email, 10)
        out = create_tenant(_runtime(cfg), a.company.strip(), email, a.admin_name.strip(), pw, a.industry)
        out['login_url'] = f'{cfg.app_url}/login'
    print(orjson.dumps(out, option=orjson.OPT_INDENT_2).decode())
    return 0


if __name__ == '__main__':
    sys.exit(main())
