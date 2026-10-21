"""Authentication over HTTP: login, lockout, MFA (RFC 6238 TOTP + recovery codes), reset, invites, signup, sessions
(idle / absolute expiry, revocation), /me/password, logout — the contract of server/src/auth/overrides.ts."""

from __future__ import annotations

import time

import psycopg
import pytest

from app.auth.crypto import sha256
from app.domain import totp as T
from app.engine.secrets import seal_secret
from tests.conftest import OWNER, PASSWORD, link_token, platform_mails

pytestmark = pytest.mark.usefixtures('fresh')


def q(sql: str, *params):  # type: ignore[no-untyped-def]
    with psycopg.connect(OWNER, autocommit=True) as c:
        cur = c.execute(sql, params or None)
        try:
            return cur.fetchall()
        except psycopg.ProgrammingError:
            return None


def test_login_success_and_me(api):  # type: ignore[no-untyped-def]
    r = api.call('POST', '/auth/login', {'email': ' Santosh@Seereon.co.in ', 'password': PASSWORD})
    assert r.status_code == 200
    d = r.json()['data']
    assert len(d['token']) == 43 and d['user']['id'] == 'u_santosh' and d['tenant']['id'] == 'tn_seereon'
    assert 'permissions' in d['user'] and d['user']['role_name'] == 'CEO'
    # only the hash is stored
    assert q('SELECT count(*) FROM sessions WHERE token_hash = %s', sha256(d['token']))[0][0] == 1
    assert q('SELECT count(*) FROM sessions WHERE token_hash = %s', d['token'])[0][0] == 0
    me = api.call('GET', '/me', token=d['token']).json()['data']
    assert me['user']['id'] == 'u_santosh' and me['is_super_admin'] is False and me['permissions'] == sorted(me['permissions'])


def test_login_failure_countdown_and_lockout(api):  # type: ignore[no-untyped-def]
    msgs = [api.call('POST', '/auth/login', {'email': 'priya@seereon.co.in', 'password': 'wrong-one'}).json()['error']['message'] for _ in range(5)]
    assert msgs[:4] == [f'Email or password is incorrect. {n} attempt{"" if n == 1 else "s"} left.' for n in (4, 3, 2, 1)]
    assert msgs[4] == 'Email or password is incorrect.'
    r = api.call('POST', '/auth/login', {'email': 'priya@seereon.co.in', 'password': PASSWORD})
    assert r.status_code == 429 and r.json()['error'] == {
        **r.json()['error'],
        'code': 'LOGIN_LOCKED',
        'message': 'Too many attempts. Try again in 15 minutes or reset your password.',
    }
    # unknown e-mail: same contract, no enumeration
    r = api.call('POST', '/auth/login', {'email': 'nobody@nowhere.io', 'password': 'x'})
    assert r.status_code == 401 and r.json()['error']['code'] == 'LOGIN_FAILED'


def test_login_clears_failures_after_success(api):  # type: ignore[no-untyped-def]
    for _ in range(3):
        api.call('POST', '/auth/login', {'email': 'hritesh@seereon.co.in', 'password': 'nope-nope'})
    api.login('hritesh@seereon.co.in')
    r = api.call('POST', '/auth/login', {'email': 'hritesh@seereon.co.in', 'password': 'nope-nope'})
    assert r.json()['error']['message'] == 'Email or password is incorrect. 4 attempts left.'


def _enable_factor(user_id: str, tenant_id: str, secret: str, codes: list[str]) -> None:
    salt = 'testsalt'
    payload = {
        'secret': seal_secret(secret),
        'salt': salt,
        'recovery': [T.hash_recovery_code(salt, c) for c in codes],
        'last_step': None,
        'fails': 0,
        'enabled_at': '2026-01-01T00:00:00.000Z',
    }
    import json

    q(
        "INSERT INTO pending_tokens (id, tenant_id, created_at, updated_at, version, kind, user_id, ref_id, payload, expires_at) VALUES (%s,%s,now(),now(),1,'two_factor',%s,NULL,%s::jsonb,'9999-12-31')",
        'tf_' + user_id,
        tenant_id,
        user_id,
        json.dumps(payload),
    )
    q('UPDATE users SET two_factor = true WHERE id = %s', user_id)


def test_mfa_login_totp_replay_and_recovery(api):  # type: ignore[no-untyped-def]
    from app.engine.secrets import set_secret_box  # noqa: F401  (the app installed the AES box)

    secret = T.new_totp_secret()
    codes = T.new_recovery_codes()
    _enable_factor('u_subhankar', 'tn_seereon', secret, codes)
    r = api.call('POST', '/auth/login', {'email': 'subhankar@seereon.co.in', 'password': PASSWORD})
    d = r.json()['data']
    assert d['mfa_required'] is True and d['expires_in'] == 300 and d['mfa_token'].startswith('tn_seereon.')
    assert 'token' not in d
    tok = d['mfa_token']
    # malformed / wrong codes
    r = api.call('POST', '/auth/login/mfa', {'mfa_token': tok, 'code': '12'})
    assert r.status_code == 422 and r.json()['error']['code'] == 'TWO_FACTOR_CODE'
    r = api.call('POST', '/auth/login/mfa', {'mfa_token': tok, 'code': T.totp(secret, time.time() * 1000 - 600_000)})
    assert r.status_code == 401 and r.json()['error']['message'].endswith('4 attempts left.')
    code = T.totp(secret)
    r = api.call('POST', '/auth/login/mfa', {'mfa_token': tok, 'code': code})
    assert r.status_code == 200, r.text
    assert len(r.json()['data']['token']) == 43 and 'recovery_codes_left' not in r.json()['data']
    # the challenge is single use
    r = api.call('POST', '/auth/login/mfa', {'mfa_token': tok, 'code': code})
    assert r.status_code == 401 and r.json()['error']['code'] == 'MFA_EXPIRED'
    # the same TOTP code cannot be replayed on a new challenge
    tok2 = api.call('POST', '/auth/login', {'email': 'subhankar@seereon.co.in', 'password': PASSWORD}).json()['data']['mfa_token']
    r = api.call('POST', '/auth/login/mfa', {'mfa_token': tok2, 'code': code})
    assert r.status_code == 401 and r.json()['error']['code'] == 'MFA_CODE_INVALID'
    # recovery code: accepted once, count reported
    r = api.call('POST', '/auth/login/mfa', {'mfa_token': tok2, 'recovery_code': codes[3].upper()})
    assert r.status_code == 200 and r.json()['data']['recovery_codes_left'] == 9
    tok3 = api.call('POST', '/auth/login', {'email': 'subhankar@seereon.co.in', 'password': PASSWORD}).json()['data']['mfa_token']
    r = api.call('POST', '/auth/login/mfa', {'mfa_token': tok3, 'recovery_code': codes[3]})
    assert r.status_code == 401 and r.json()['error']['field'] == 'recovery_code'
    # the factor secret is sealed at rest (AES-GCM, not plain)
    sealed = q("SELECT payload->>'secret' FROM pending_tokens WHERE kind = 'two_factor' AND user_id = 'u_subhankar'")[0][0]
    assert sealed.startswith('v1.') and secret not in sealed


def test_mfa_challenge_burns_after_five_wrong_codes(api):  # type: ignore[no-untyped-def]
    _enable_factor('u_baishali', 'tn_seereon', T.new_totp_secret(), T.new_recovery_codes())
    tok = api.call('POST', '/auth/login', {'email': 'baishali@seereon.co.in', 'password': PASSWORD}).json()['data']['mfa_token']
    codes = [api.call('POST', '/auth/login/mfa', {'mfa_token': tok, 'code': '000000'}).json()['error']['code'] for _ in range(5)]
    assert codes == ['MFA_CODE_INVALID'] * 4 + ['MFA_LOCKED']
    assert api.call('POST', '/auth/login/mfa', {'mfa_token': tok, 'code': '000000'}).json()['error']['code'] == 'MFA_EXPIRED'
    assert api.call('POST', '/auth/login/mfa', {'mfa_token': 'garbage', 'code': '000000'}).json()['error']['code'] == 'MFA_EXPIRED'


def test_two_factor_flag_without_factor_heals(api):  # type: ignore[no-untyped-def]
    q("UPDATE users SET two_factor = true WHERE id = 'u_priya'")
    r = api.call('POST', '/auth/login', {'email': 'priya@seereon.co.in', 'password': PASSWORD})
    assert r.status_code == 200 and 'token' in r.json()['data'] and r.json()['data']['user']['two_factor'] is False


def test_reset_flow_revokes_sessions(api):  # type: ignore[no-untyped-def]
    old = api.login('jayashree@seereon.co.in')
    t0 = time.monotonic()
    r = api.call('POST', '/auth/reset-request', {'email': 'jayashree@seereon.co.in'})
    assert time.monotonic() - t0 >= 0.39  # timing floor
    assert r.json()['data'] == {'accepted': True}  # the token never travels in the response: it is e-mailed
    r = api.call('POST', '/auth/reset-request', {'email': 'nobody@nowhere.io'})
    assert r.json()['data'] == {'accepted': True}
    assert platform_mails('nobody@nowhere.io') == []
    mail = platform_mails('jayashree@seereon.co.in')[0]
    assert mail['subject'] == 'Reset your SeereonOS password' and mail['status'] == 'pending'
    assert mail['sealed'].startswith('v1.') and 'reset-password' not in mail['sealed']  # sealed at rest (AES-GCM)
    token = link_token(mail['body'], 'https://app.seereonos.com/reset-password?token=')
    assert len(token) == 43 and q('SELECT count(*) FROM reset_tokens WHERE token_hash = %s', sha256(token))[0][0] == 1
    info = api.call('GET', f'/auth/reset-info/{token}').json()['data']
    assert info['min_length'] == 10 and info['email_hint'] == 'j••••••@seereon.co.in' and 59 <= info['expires_in_minutes'] <= 60
    assert api.call('POST', '/auth/reset', {'token': token, 'password': 'short'}).json()['error']['code'] == 'PASSWORD_WEAK'
    r = api.call('POST', '/auth/reset', {'token': token, 'password': 'A much longer passphrase 42'})
    assert r.status_code == 200 and r.json()['data'] == {'ok': True}
    assert api.call('GET', '/me', token=old).status_code == 401  # every session of the user ended
    assert api.call('POST', '/auth/reset', {'token': token, 'password': 'Another passphrase 43'}).json()['error']['code'] == 'RESET_EXPIRED'
    assert api.call('GET', f'/auth/reset-info/{token}').status_code == 410
    api.login('jayashree@seereon.co.in', 'A much longer passphrase 42')
    assert api.call('POST', '/auth/login', {'email': 'jayashree@seereon.co.in', 'password': PASSWORD}).status_code == 401


def test_invite_info_and_accept(api):  # type: ignore[no-untyped-def]
    q("""INSERT INTO invites (id, tenant_id, created_at, updated_at, created_by, version, email, name, role, role_key, reports_to_id, token, expires_at, accepted_at)
         VALUES ('inv_t1', 'tn_seereon', now(), now(), 'u_santosh', 1, 'newbie@seereon.co.in', 'New Bie', 'employee', 'employee', 'u_subhankar', 'inv_tok_123', now() + interval '3 days', NULL)""")
    info = api.call('GET', '/invites/inv_tok_123').json()['data']
    assert info['email'] == 'newbie@seereon.co.in' and info['role_name'] == 'Employee' and info['company']
    assert info['reports_to'] == q("SELECT name FROM users WHERE id = 'u_subhankar'")[0][0]
    assert api.call('GET', '/invites/nope').json()['error']['code'] == 'INVITE_EXPIRED'
    assert api.call('POST', '/invites/inv_tok_123/accept', {'name': 'N', 'password': 'Long enough pass 1'}).json()['error']['code'] == 'VALIDATION'
    r = api.call('POST', '/invites/inv_tok_123/accept', {'name': 'New Bie', 'password': 'Long enough pass 1'})
    assert r.status_code == 200, r.text
    d = r.json()['data']
    assert d['user']['email'] == 'newbie@seereon.co.in' and d['user']['role_key'] == 'employee' and d['user']['created_by'] == 'u_santosh'
    assert api.call('GET', '/invites/inv_tok_123').status_code == 410
    api.login('newbie@seereon.co.in', 'Long enough pass 1')


def test_signup_provisions_a_new_tenant(api):  # type: ignore[no-untyped-def]
    r = api.call(
        'POST', '/auth/signup', {'company': 'Acme Widgets Pvt Ltd', 'name': 'Asha Rao', 'email': 'asha@acme.example', 'password': 'Widgets are great 99'}
    )
    assert r.status_code == 200, r.text
    d = r.json()['data']
    tid = d['tenant']['id']
    assert (
        d['tenant']['slug'] == 'acme-widgets-pvt-ltd'
        and d['tenant']['org']['employee_code_prefix'] == 'AW'
        and d['tenant']['maker_checker']['enabled'] is False
    )
    assert d['user']['role_key'] == 'administrator' and d['user']['created_by'] == 'system'
    for table, n in (
        ('roles', 9),
        ('email_templates', 9),
        ('approval_workflows', 6),
        ('doc_templates', 11),
        ('wf_templates', 6),
        ('departments', 4),
        ('subscriptions', 1),
    ):
        assert q(f'SELECT count(*) FROM {table} WHERE tenant_id = %s', tid)[0][0] == n, table
    assert api.call('GET', '/tenant', token=d['token']).json()['data']['id'] == tid
    r = api.call('POST', '/auth/signup', {'company': 'Other', 'name': 'Asha Rao', 'email': 'SANTOSH@seereon.co.in', 'password': 'Widgets are great 99'})
    assert r.status_code == 409 and r.json()['error']['code'] == 'USER_EMAIL_TAKEN'


def test_session_idle_and_absolute_expiry(api):  # type: ignore[no-untyped-def]
    tok = api.login('subham@seereon.co.in')
    assert api.call('GET', '/me', token=tok).status_code == 200
    q("UPDATE sessions SET last_seen_at = now() - interval '13 hours' WHERE token_hash = %s", sha256(tok))
    assert api.call('GET', '/me', token=tok).status_code == 401
    tok = api.login('subham@seereon.co.in')
    q("UPDATE sessions SET expires_at = now() - interval '1 second' WHERE token_hash = %s", sha256(tok))
    assert api.call('GET', '/me', token=tok).status_code == 401


def test_password_change_revokes_other_sessions_and_logout(api):  # type: ignore[no-untyped-def]
    a = api.login('bijaya@seereon.co.in')
    b = api.login('bijaya@seereon.co.in')
    r = api.call('POST', '/me/password', {'current': 'wrong', 'next': 'New pass phrase 77'}, token=a)
    assert r.status_code == 422 and r.json()['error']['code'] == 'PASSWORD_WRONG'
    r = api.call('POST', '/me/password', {'current': PASSWORD, 'next': PASSWORD}, token=a)
    assert r.json()['error']['code'] in ('PASSWORD_REUSED', 'PASSWORD_WEAK')
    r = api.call('POST', '/me/password', {'current': PASSWORD, 'next': 'New pass phrase 77'}, token=a)
    assert r.status_code == 200 and r.json()['data']['ok'] is True
    assert api.call('GET', '/me', token=a).status_code == 200
    assert api.call('GET', '/me', token=b).status_code == 401
    assert api.call('POST', '/auth/logout', token=a).json()['data'] == {'ok': True}
    assert api.call('GET', '/me', token=a).status_code == 401


def test_deactivated_user_is_signed_out(api):  # type: ignore[no-untyped-def]
    tok = api.login('pragyasmita@seereon.co.in')
    q("UPDATE users SET status = 'deactivated' WHERE id = 'u_pragyasmita'")
    assert api.call('GET', '/me', token=tok).status_code == 401
    r = api.call('POST', '/auth/login', {'email': 'pragyasmita@seereon.co.in', 'password': PASSWORD})
    assert r.status_code == 403 and r.json()['error']['code'] == 'USER_DEACTIVATED'


def test_security_events_are_recorded(api):  # type: ignore[no-untyped-def]
    q('DELETE FROM security_events')
    api.call('POST', '/auth/login', {'email': 'santosh@seereon.co.in', 'password': 'bad-password'})
    api.login('santosh@seereon.co.in')
    import time as _t

    _t.sleep(0.3)  # audit_after runs after the response
    ev = q('SELECT event, outcome, email, user_id FROM security_events ORDER BY id')
    assert ('login.failure', 'failure', 'santosh@seereon.co.in', None) in ev
    assert ('login.success', 'success', 'santosh@seereon.co.in', 'u_santosh') in ev
