"""Operational contracts: every API answer is no-store (+ Vary: Authorization); /metrics needs the metrics token;
logs never contain passwords, tokens or payment data; the development fixture CLI is refused in production; Sentry
events are scrubbed."""

from __future__ import annotations

import io
import json
import logging

import pytest

from tests.conftest import PASSWORD, env_for


def test_every_api_response_is_no_store(client):  # type: ignore[no-untyped-def]
    cases = [
        client.get('/api/v1/me'),  # 401
        client.post('/api/v1/auth/login', json={'email': 'santosh@seereon.co.in', 'password': PASSWORD}),
        client.get('/api/v1/no/such/route'),
        client.post('/api/v1/auth/login', content=b'{bad', headers={'content-type': 'application/json'}),
        client.get('/healthz'),
        client.get('/readyz'),
    ]
    tok = cases[1].json()['data']['token']
    cases.append(client.get('/api/v1/projects', headers={'Authorization': 'Bearer ' + tok, 'Accept-Encoding': 'gzip'}))
    for r in cases:
        assert r.headers['cache-control'] == 'no-store', (r.request.url, r.headers)
        assert r.headers['pragma'] == 'no-cache'
        vary = [v.strip().lower() for v in r.headers['vary'].split(',')]
        assert 'authorization' in vary and 'origin' in vary, r.headers['vary']
    assert 'accept-encoding' in [v.strip().lower() for v in cases[-1].headers['vary'].split(',')]


def test_metrics_need_the_token(db, app):  # type: ignore[no-untyped-def]
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app

    token = 'metrics-token-0123456789-abcdef'
    a = create_app(load_config(env_for(METRICS_TOKEN=token)))
    try:
        with TestClient(a) as c:
            c.get('/api/v1/me')
            assert c.get('/metrics').status_code == 401
            assert c.get('/metrics', headers={'Authorization': 'Bearer wrong-token'}).status_code == 401
            r = c.get('/metrics', headers={'Authorization': 'Bearer ' + token})
            assert r.status_code == 200 and r.headers['content-type'].startswith('text/plain') and r.headers['cache-control'] == 'no-store'
            t = r.text
            for metric in (
                'seereon_http_requests_total',
                'seereon_http_request_duration_seconds_bucket',
                'seereon_db_pool',
                'seereon_db_up 1',
                'seereon_email_queue',
                'seereon_worker_heartbeat_age_seconds',
            ):
                assert metric in t, metric
            assert 'route="/me"' in t and 'status="4xx"' in t
    finally:
        a.state.rt.backend.pools.close()
        from app.config import set_settings

        set_settings(app.state.cfg)


def test_metrics_disabled_without_a_token(client):  # type: ignore[no-untyped-def]
    assert client.get('/metrics').status_code == 404


def test_logs_never_carry_secrets(db, seed, app):  # type: ignore[no-untyped-def]
    """drive sign-in, a password change, a reset request, a failed login and a payment verify at LOG_LEVEL=debug; the
    captured log stream never contains the passwords, the session token, the reset link or the signature"""
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app
    from tests.conftest import reload_fixture

    a = create_app(load_config(env_for(LOG_LEVEL='debug')))
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    from app.logging_setup import JsonFormatter

    h.setFormatter(JsonFormatter())
    logging.getLogger('seereonos').addHandler(h)
    try:
        with TestClient(a) as c:
            r = c.post('/api/v1/auth/login', json={'email': 'hritesh@seereon.co.in', 'password': PASSWORD})
            tok = r.json()['data']['token']
            c.post('/api/v1/auth/login', json={'email': 'hritesh@seereon.co.in', 'password': 'Wrong-but-secret-Pw1'})
            c.post('/api/v1/me/password', json={'current': PASSWORD, 'next': 'Brand-new-secret-Pw42'}, headers={'Authorization': 'Bearer ' + tok})
            c.post('/api/v1/auth/reset-request', json={'email': 'bijaya@seereon.co.in'})
            c.get('/api/v1/auth/reset-info/some-secret-reset-token-value')
            c.get('/api/v1/invites/inv_secret_invite_token')
            c.post('/api/v1/billing/payments/verify', json={'razorpay_order_id': 'o', 'razorpay_payment_id': 'p', 'razorpay_signature': 'deadbeefsig'}, headers={'Authorization': 'Bearer ' + tok})
            logging.getLogger('seereonos').error('boom with Bearer %s and postgres://u:dbpass@h/x', tok)
    finally:
        logging.getLogger('seereonos').removeHandler(h)
        a.state.rt.backend.pools.close()
        reload_fixture(seed)
        from app.config import set_settings
        from app.logging_setup import configure_logging

        set_settings(app.state.cfg)
        configure_logging('silent')
    out = stream.getvalue()
    assert 'request completed' in out and out.count('\n') >= 7
    for secret in (PASSWORD, 'Wrong-but-secret-Pw1', 'Brand-new-secret-Pw42', tok, 'some-secret-reset-token-value', 'inv_secret_invite_token', 'deadbeefsig', 'dbpass'):
        assert secret not in out, secret
    for line in out.splitlines():
        json.loads(line)  # structured JSON lines


def test_fixture_cli_is_refused_in_production(monkeypatch, capsys):  # type: ignore[no-untyped-def]
    from app.devtools import fixtures

    monkeypatch.setenv('APP_ENV', 'production')
    assert fixtures.main(['load-demo']) == 2
    assert 'refused when APP_ENV=production' in capsys.readouterr().err
    from app.db import reset

    with pytest.raises(RuntimeError):
        reset.main([])


def test_sentry_events_are_scrubbed():
    from app.observability import scrub_event

    ev = {
        'request': {
            'url': 'https://api.example/api/v1/auth/login',
            'data': {'password': 'pw'},
            'cookies': {'a': 'b'},
            'query_string': 'token=abc',
            'headers': {'Authorization': 'Bearer abc', 'User-Agent': 'ua', 'X-Razorpay-Signature': 'sig'},
        },
        'user': {'email': 'x@y.z'},
        'extra': {'password': 'pw', 'nested': {'session_token': 't', 'ok': 'value'}, 'msg': 'Bearer abc.def'},
    }
    out = scrub_event(ev)
    assert 'data' not in out['request'] and 'cookies' not in out['request'] and 'query_string' not in out['request']
    assert out['request']['headers'] == {'User-Agent': 'ua'} and 'user' not in out
    assert out['extra']['password'] == '[redacted]' and out['extra']['nested']['session_token'] == '[redacted]' and out['extra']['nested']['ok'] == 'value'
    assert out['extra']['msg'] == 'Bearer [redacted]'
