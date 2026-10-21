"""The credentials journey of a NEW company on an EMPTY database in PRODUCTION configuration (no demo data, one runtime
role, SMTP on, the worker delivering e-mail through a real SMTP conversation) — the team's report "only the dummy
credentials work, the new credentials we create don't" must not be reproducible:

  sign-up -> sign in; create-superadmin / create-tenant (CLI) -> sign in; admin creates an employee (temporary password,
  welcome e-mail) -> employee signs in -> forced password change -> signs in again; invitation e-mailed -> accept ->
  sign in; forgot password -> e-mail with the link -> reset -> sign in; admin resets a password; deactivation ends every
  session; and a SECOND app instance (another process) sees all of it.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import pytest

from tests.conftest import APP, OWNER, env_for, link_token, owner_sql, reload_fixture
from tests.smtp_sink import SmtpSink

APP_URL = 'https://app.journey.example'


@pytest.fixture(scope='module')
def sink():  # type: ignore[no-untyped-def]
    s = SmtpSink()
    yield s
    s.close()


@pytest.fixture(scope='module')
def prod(db, seed, sink, app):  # type: ignore[no-untyped-def]
    """an EMPTY (migrated) database, a production-configured API and a worker; the demo data comes back afterwards"""
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.db.pool import Pools, connect
    from app.devtools.fixtures import load
    from app.main import create_app
    from app.worker import Worker

    with connect(OWNER) as c:
        load(c, {}, in_place=True, hashes=[], log=lambda s: None)  # every data table emptied, versions bumped
        assert c.execute('SELECT count(*) FROM tenants').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM users').fetchone()[0] == 0
    env = env_for(
        APP_ENV='production',
        DATABASE_URL='postgres://seereon_app:Str0ng-Prod-Pa55@localhost:5432/seereonos_test',  # validated, then swapped below
        MFA_ENCRYPTION_KEY='journey-test-key-' + 'k' * 40,
        APP_URL=APP_URL,
        CORS_ORIGINS=APP_URL,
        SMTP_URL=sink.url,
        MAIL_FROM='SeereonOS <no-reply@journey.example>',
        LOG_LEVEL='silent',
    )
    cfg = dataclasses.replace(load_config(env), database_url=APP)  # the local test role's password is not "production"
    assert cfg.production and cfg.email_delivery == 'smtp' and not cfg.freeze_rows
    a = create_app(cfg)  # startup checks: least-privilege role, schema up to date
    w = Worker(cfg, Pools(APP, 4))
    with TestClient(a) as client:
        yield {'client': client, 'app': a, 'worker': w, 'cfg': cfg}
    w.pools.close()
    a.state.rt.backend.pools.close()
    from app.engine.secrets import set_secret_box

    set_secret_box(None)
    reload_fixture(seed)
    # the session app (development key) is used by later tests
    from app.auth.secret_box import create_secret_box
    from app.config import DEV_MFA_KEY

    set_secret_box(create_secret_box(DEV_MFA_KEY, [], allow_plain=True))
    from app.config import set_settings

    set_settings(app.state.cfg)  # create_app installed the production configuration process-wide


def _call(p: dict, method: str, path: str, body: Any = None, token: str | None = None) -> Any:
    h = {'Authorization': 'Bearer ' + token} if token else {}
    return p['client'].request(method, '/api/v1' + path, json=body, headers=h)


def _login(p: dict, email: str, password: str) -> Any:
    return _call(p, 'POST', '/auth/login', {'email': email, 'password': password})


def _deliver(p: dict) -> dict:
    return p['worker'].run_job(p['worker'].jobs['email'])


def _mail(sink: SmtpSink, to: str, subject_part: str) -> dict:
    for m in reversed(sink.to(to)):
        if subject_part in m['subject']:
            return m
    raise AssertionError(f'no e-mail "{subject_part}" to {to}: {[x["subject"] for x in sink.to(to)]}')


def test_signup_then_sign_in_on_an_empty_database(prod):  # type: ignore[no-untyped-def]
    r = _call(prod, 'POST', '/auth/signup', {'company': 'Nova Analytics Pvt Ltd', 'name': 'Nisha Rao', 'email': 'nisha@nova.example', 'password': 'Nova analytics passphrase 1'})
    assert r.status_code == 200, r.text
    d = r.json()['data']
    assert d['tenant']['name'] == 'Nova Analytics Pvt Ltd' and d['user']['role_key'] == 'administrator'
    r = _login(prod, 'nisha@nova.example', 'Nova analytics passphrase 1')
    assert r.status_code == 200, r.text
    tok = r.json()['data']['token']
    me = _call(prod, 'GET', '/me', token=tok).json()['data']
    assert me['user']['email'] == 'nisha@nova.example' and 'users.manage' in me['permissions']
    assert _login(prod, 'nisha@nova.example', 'wrong password!!').status_code == 401
    # provisioning: roles, master data, workflows, email templates
    assert len(_call(prod, 'GET', '/roles', token=tok).json()['data']) >= 9
    assert _call(prod, 'GET', '/master/departments', token=tok).json()['data']
    # the response is never cacheable
    assert r.headers['cache-control'] == 'no-store' and 'Authorization' in r.headers['vary']


def test_cli_superadmin_and_tenant(prod):  # type: ignore[no-untyped-def]
    from app.cli import create_superadmin, create_tenant

    rt = prod['app'].state.rt
    out = create_superadmin(rt, 'ops@seereonos.example', 'Platform Ops', 'Platform ops passphrase 9')
    assert out['updated'] is False
    r = _login(prod, 'ops@seereonos.example', 'Platform ops passphrase 9')
    assert r.status_code == 200, r.text
    tenants = _call(prod, 'GET', '/admin/tenants', token=r.json()['data']['token'])
    assert tenants.status_code == 200 and [t['name'] for t in tenants.json()['data']['tenants']] == ['Nova Analytics Pvt Ltd']
    with pytest.raises(SystemExit):
        create_superadmin(rt, 'nisha@nova.example', 'X', 'Another passphrase 99')  # e-mail of a workspace user
    create_superadmin(rt, 'ops@seereonos.example', 'Platform Ops', 'Rotated ops passphrase 10', update_password=True)
    assert _login(prod, 'ops@seereonos.example', 'Platform ops passphrase 9').status_code == 401
    assert _login(prod, 'ops@seereonos.example', 'Rotated ops passphrase 10').status_code == 200
    t = create_tenant(rt, 'Nova Analytics Pvt Ltd', 'ceo@second.example', 'Second CEO', 'Second company passphrase 3')
    assert t['slug'] == 'nova-analytics-pvt-ltd-2'  # same company name: a unique slug, no 500
    r = _login(prod, 'ceo@second.example', 'Second company passphrase 3')
    assert r.status_code == 200 and r.json()['data']['tenant']['id'] == t['tenant_id']
    me = _call(prod, 'GET', '/me', token=r.json()['data']['token']).json()['data']
    assert me['role_def']['key'] == 'administrator'


def test_employee_temp_password_forced_change(prod, sink):  # type: ignore[no-untyped-def]
    admin = _login(prod, 'nisha@nova.example', 'Nova analytics passphrase 1').json()['data']
    tok, aid = admin['token'], admin['user']['id']
    dep = _call(prod, 'GET', '/master/departments', token=tok).json()['data'][0]['id']
    des = _call(prod, 'GET', '/master/designations', token=tok).json()['data'][0]['id']
    r = _call(
        prod,
        'POST',
        '/employees',
        {'name': 'Ravi Kumar', 'email': 'ravi@nova.example', 'joined_on': time.strftime('%Y-%m-%d'), 'department_id': dep, 'designation_id': des, 'role_key': 'employee', 'reports_to_id': aid},
        token=tok,
    )
    assert r.status_code == 200, r.text
    res = r.json()['data']['result']
    temp = res['temp_password']
    assert res['user']['must_change_password'] is True and len(temp) >= 10
    out = _deliver(prod)
    assert out['sent'] >= 1, out
    welcome = _mail(sink, 'ravi@nova.example', '')
    assert temp in welcome['body'] and f'{APP_URL}/login' in welcome['body']
    # sign in with the temporary password: writes are blocked until the password is changed
    r = _login(prod, 'ravi@nova.example', temp)
    assert r.status_code == 200, r.text
    etok = r.json()['data']['token']
    assert r.json()['data']['user']['must_change_password'] is True
    blocked = _call(prod, 'PATCH', '/me/profile', {'name': 'Ravi K'}, token=etok)
    assert blocked.status_code == 403 and blocked.json()['error']['code'] == 'PASSWORD_CHANGE_REQUIRED'
    r = _call(prod, 'POST', '/me/password', {'current': temp, 'next': 'Ravi own passphrase 77'}, token=etok)
    assert r.status_code == 200, r.text
    assert _login(prod, 'ravi@nova.example', temp).status_code == 401
    r = _login(prod, 'ravi@nova.example', 'Ravi own passphrase 77')
    assert r.status_code == 200 and r.json()['data']['user'].get('must_change_password') is False
    assert _call(prod, 'PATCH', '/me/profile', {'name': 'Ravi K'}, token=r.json()['data']['token']).status_code == 200


def test_invitation_link_by_email(prod, sink):  # type: ignore[no-untyped-def]
    admin = _login(prod, 'nisha@nova.example', 'Nova analytics passphrase 1').json()['data']
    tok, aid = admin['token'], admin['user']['id']
    r = _call(prod, 'POST', '/users/invite', {'name': 'Meera Das', 'email': 'meera@nova.example', 'role_key': 'employee', 'reports_to_id': aid}, token=tok)
    assert r.status_code == 200, r.text
    assert 'token' not in str(owner_sql("SELECT body FROM email_deliveries WHERE to_addr = 'meera@nova.example'"))  # sealed at rest
    _deliver(prod)
    mail = _mail(sink, 'meera@nova.example', 'invited you')
    token = link_token(mail['body'], f'{APP_URL}/invite/')
    info = _call(prod, 'GET', f'/invites/{token}')
    assert info.status_code == 200 and info.json()['data']['email'] == 'meera@nova.example'
    r = _call(prod, 'POST', f'/invites/{token}/accept', {'name': 'Meera Das', 'password': 'Meera chose this one 5'})
    assert r.status_code == 200, r.text
    assert _login(prod, 'meera@nova.example', 'Meera chose this one 5').status_code == 200
    assert _call(prod, 'GET', f'/invites/{token}').status_code == 410  # single use
    assert owner_sql("SELECT body FROM email_deliveries WHERE kind = 'platform' AND to_addr = 'meera@nova.example'")[0][0] is None  # scrubbed


def test_forgot_password_email_link(prod, sink):  # type: ignore[no-untyped-def]
    r = _call(prod, 'POST', '/auth/reset-request', {'email': 'meera@nova.example'})
    assert r.status_code == 200 and r.json()['data'] == {'accepted': True}
    _deliver(prod)
    mail = _mail(sink, 'meera@nova.example', 'Reset your SeereonOS password')
    token = link_token(mail['body'], f'{APP_URL}/reset-password?token=')
    assert _call(prod, 'GET', f'/auth/reset-info/{token}').status_code == 200
    r = _call(prod, 'POST', '/auth/reset', {'token': token, 'password': 'Meera reset passphrase 6'})
    assert r.status_code == 200, r.text
    assert _login(prod, 'meera@nova.example', 'Meera chose this one 5').status_code == 401
    assert _login(prod, 'meera@nova.example', 'Meera reset passphrase 6').status_code == 200


def test_admin_reset_and_deactivation_end_sessions(prod, sink):  # type: ignore[no-untyped-def]
    admin = _login(prod, 'nisha@nova.example', 'Nova analytics passphrase 1').json()['data']
    tok = admin['token']
    meera = _login(prod, 'meera@nova.example', 'Meera reset passphrase 6').json()['data']
    mid, mtok = meera['user']['id'], meera['token']
    r = _call(prod, 'POST', f'/users/{mid}/reset-password', token=tok)
    assert r.status_code == 200, r.text
    temp = r.json()['data']['temp_password']
    assert _call(prod, 'GET', '/me', token=mtok).status_code == 401  # her sessions ended
    r = _login(prod, 'meera@nova.example', temp)
    assert r.status_code == 200 and r.json()['data']['user']['must_change_password'] is True
    mtok2 = r.json()['data']['token']
    r = _call(prod, 'POST', f'/users/{mid}/deactivate', {'reason': 'Left the company'}, token=tok)
    assert r.status_code == 200, r.text
    assert _call(prod, 'GET', '/me', token=mtok2).status_code == 401
    r = _login(prod, 'meera@nova.example', temp)
    assert r.status_code == 403 and r.json()['error']['code'] == 'USER_DEACTIVATED'


def test_a_second_instance_sees_everything(prod):  # type: ignore[no-untyped-def]
    """another process (own pool + cache) on the same single database: the new credentials work there too"""
    from starlette.testclient import TestClient

    from app.main import create_app

    b = create_app(prod['cfg'])
    try:
        with TestClient(b) as c2:
            r = c2.post('/api/v1/auth/login', json={'email': 'ravi@nova.example', 'password': 'Ravi own passphrase 77'})
            assert r.status_code == 200, r.text
            people = c2.get('/api/v1/directory', headers={'Authorization': 'Bearer ' + r.json()['data']['token']}).json()['data']
            assert {'nisha@nova.example', 'ravi@nova.example', 'meera@nova.example'} <= {p['email'] for p in people}
    finally:
        b.state.rt.backend.pools.close()
    # nothing of the demo data exists in this database: everything above was created through the product
    assert owner_sql("SELECT count(*) FROM users WHERE email LIKE '%@seereon.co.in'")[0][0] == 0


def test_new_company_project_costing_and_invoice_draft(prod):  # type: ignore[no-untyped-def]
    """the first project of a brand-new company: no deadlock (allocation before any WBS: "All milestones"), an unsaved
    costing starts at markup 0% (not price 0 / −100%), and an invoice DRAFT saves before the company profile is filled
    in — completeness is enforced when issuing"""
    admin = _login(prod, 'nisha@nova.example', 'Nova analytics passphrase 1').json()['data']
    tok, aid = admin['token'], admin['user']['id']
    today = time.strftime('%Y-%m-%d')
    p = _call(
        prod,
        'POST',
        '/projects',
        {'name': 'Portal', 'client': 'Acme', 'type': 'service', 'billing_model': 'fixed', 'stream': 'services', 'start_on': today, 'end_on': '2027-12-31', 'contract_value_paise': 0},
        token=tok,
    )
    assert p.status_code == 200, p.text
    pid = p.json()['data']['id']
    c = _call(prod, 'GET', f'/projects/{pid}/costing', token=tok).json()['data']['preview']
    assert c['markup_pct'] == 0 and c['totals']['price'] == c['totals']['actual_cost'] == 0
    r = _call(prod, 'POST', f'/projects/{pid}/allocations', {'user_id': aid, 'pct': 50, 'from_on': today, 'to_on': '2027-01-31'}, token=tok)
    assert r.status_code == 200, r.text  # no milestone yet: an "All milestones" allocation
    c = _call(prod, 'GET', f'/projects/{pid}/costing', token=tok).json()['data']['preview']
    assert c['rows'][0]['milestone_id'] is None and 'milestone' not in (c['rows'][0]['error'] or '')  # costed on All milestones (no cost rate yet)
    assert _call(prod, 'POST', f'/projects/{pid}/tasks', {'title': 'Kick-off', 'owner_id': aid, 'est_hours': 4}, token=tok).status_code == 200
    draft = {'project_id': pid, 'invoiced_on': today, 'due_on': '2027-01-31', 'lines': [{'item': 'Phase 1', 'hsn_sac': '998314', 'qty': 1, 'rate_paise': 10000000, 'gst_rate': 18}]}
    r = _call(prod, 'POST', '/finance/invoices', draft, token=tok)
    assert r.status_code == 200, r.text
    inv = r.json()['data']
    r = _call(prod, 'POST', '/finance/invoices', {**draft, 'issue': True}, token=tok)
    assert r.status_code == 422 and r.json()['error']['code'] == 'VALIDATION'  # the seller details are needed to issue
    assert inv['status'] == 'draft'
