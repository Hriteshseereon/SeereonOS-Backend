"""HTTP-level contract vs the Node reference (vectors recorded from the running Node API by
tests/golden/gen/node_http.mjs): status codes, framework-level error envelopes, security headers + CSP, static files
with SPA fallback and a real 404 for missing assets; plus body limits, request ids, CORS, idempotency, rate limits."""

from __future__ import annotations

import os
import re
import time

import pytest

from tests.conftest import PASSWORD, env_for
from tests.golden import load_vectors

NODE = load_vectors('node_http.json')
NODE_CSP = (
    "default-src 'self';script-src 'self';style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;font-src 'self' data: https://fonts.gstatic.com;"
    "img-src 'self' data: blob:;connect-src 'self';frame-src 'self' blob: data:;worker-src 'self' blob:;object-src 'none';base-uri 'self';form-action 'self';"
    "frame-ancestors 'none'"
)
DIST = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'frontend', 'dist'))
# headers whose values legitimately differ (framework, compression, file metadata)
IGNORE = {
    'date',
    'x-request-id',
    'content-length',
    'etag',
    'last-modified',
    'keep-alive',
    'connection',
    'ratelimit-remaining',
    'ratelimit-reset',
    'vary',
    'content-encoding',
    'accept-ranges',
    'transfer-encoding',
    'ratelimit-limit',
}


@pytest.fixture(scope='module')
def http_client(db, seed, tmp_path_factory):  # type: ignore[no-untyped-def]
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app

    dist = DIST
    if not os.path.isfile(os.path.join(dist, 'index.html')):  # the optional STATIC_DIR serving, with a stand-in build
        dist = str(tmp_path_factory.mktemp('dist'))
        os.makedirs(os.path.join(dist, 'assets'))
        with open(os.path.join(dist, 'index.html'), 'w') as f:
            f.write('<!doctype html><html><head><title>SeereonOS</title></head><body><div id="root"></div></body></html>')
    a = create_app(load_config(env_for(STATIC_DIR=dist)), start_workers=False)
    with TestClient(a, raise_server_exceptions=False) as c:
        yield c
    a.state.rt.backend.pools.close()


@pytest.mark.parametrize('case', NODE, ids=lambda c: f'{c["method"]} {c["path"][:40]}')
def test_matches_node_reference(http_client, case):  # type: ignore[no-untyped-def]
    body = case['body']
    if isinstance(body, dict):
        import json

        body = json.dumps({'a': 'x y' * 400000})
    headers = dict(case['headers'])
    if body is not None and not any(k.lower() == 'content-type' for k in headers):
        headers['content-type'] = 'text/plain;charset=UTF-8'  # what fetch() sends for a string body
    rt = http_client.app.state.rt
    sub = case['path'][len('/api/v1') :]
    if case['path'].startswith('/api/v1/') and case['status'] not in (404, 400, 413, 415) and rt.router.match(case['method'], sub) is None and sub != '/me':
        pytest.skip(f'route module of {case["method"]} {sub} not ported yet')
    r = http_client.request(case['method'], case['path'], headers=headers, content=body)
    assert r.status_code == case['status'], r.text[:300]
    text = '' if case['method'] == 'HEAD' else re.sub(r'"request_id":"[^"]+"', '"request_id":"<rid>"', r.text)
    if case['resBody'] == '<html>':
        assert text.lower().startswith('<!doctype')
    elif case['path'] == '/readyz':
        assert r.json()['status'] == 'ready' and r.json()['database'] == 'ok'  # v8: + worker heartbeat (test_readyz_reports_worker)
    else:
        assert text == case['resBody']
    if case['status'] == 400 and 'BAD_REQUEST' in case['resBody']:
        return  # Node answers a malformed URL before its header hooks run; the Python API keeps the security headers
    got = {k.lower(): v for k, v in r.headers.items() if k.lower() not in IGNORE and k.lower() != 'pragma'}
    want = {k.lower(): v for k, v in case['resHeaders'].items() if k.lower() not in IGNORE}
    if 'content-security-policy' in want and want['content-security-policy'] == NODE_CSP:
        from app.main import CSP

        want['content-security-policy'] = CSP  # v8: + connect-src data: blob: (the app fetches its own data:/blob: attachments)
    if case['path'].startswith(('/api/', '/healthz', '/readyz')):
        want['cache-control'] = 'no-store'  # v8: every API / health response is no-store (+ Pragma, Vary: Authorization)
    assert got == want


def test_request_id_echo_and_replace(client):  # type: ignore[no-untyped-def]
    r = client.get('/api/v1/me', headers={'x-request-id': 'abcdefgh-123'})
    assert r.headers['x-request-id'] == 'abcdefgh-123' and r.json()['error']['request_id'] == 'abcdefgh-123'
    r = client.get('/api/v1/me', headers={'x-request-id': 'bad id!'})
    assert re.fullmatch(r'[0-9a-f-]{36}', r.headers['x-request-id'])


def test_body_limits(client):  # type: ignore[no-untyped-def]
    big = '{"a":"' + 'x' * (1024 * 1024 + 10) + '"}'
    r = client.post('/api/v1/notifications/read-all', content=big, headers={'content-type': 'application/json'})
    assert r.status_code == 413 and r.json()['error']['code'] == 'PAYLOAD_TOO_LARGE'
    r = client.post('/api/v1/tasks', content=big, headers={'content-type': 'application/json'})
    assert r.status_code in (401, 404)  # upload route: 15 MB (404 until the tasks module is ported)
    huge = '{"a":"' + 'x' * (15 * 1024 * 1024 + 10) + '"}'
    r = client.post('/api/v1/tasks', content=huge, headers={'content-type': 'application/json'})
    assert r.status_code == 413


def test_file_safety_on_bodies(api):  # type: ignore[no-untyped-def]
    tok = api.as_('santosh@seereon.co.in')
    import base64

    bad = 'data:image/png;base64,' + base64.b64encode(b'<html><script>alert(1)</script>').decode()
    r = api.call('PUT', '/tenant/logo', {'data_url': bad}, token=tok)
    e = r.json()['error']
    assert r.status_code == 422 and e['code'] == 'FILE_REJECTED' and 'field' not in e  # top-level data URL: no field (like the TS)
    r = api.call('PUT', '/tenant/logo', {'logo': {'data_url': bad}}, token=tok)
    assert r.json()['error']['field'] == 'logo'


def test_idempotency_replay(api, fresh):  # type: ignore[no-untyped-def]
    tok = api.login('santosh@seereon.co.in')
    h = {'Idempotency-Key': 'idem-test-1'}
    r1 = api.call('POST', '/notifications/read-all', token=tok, headers=h)
    r2 = api.call('POST', '/notifications/read-all', token=tok, headers=h)
    assert r1.status_code == r2.status_code == 200 and r2.headers.get('idempotent-replayed') == 'true' and 'idempotent-replayed' not in r1.headers
    assert r1.json() == r2.json()
    r3 = api.call('POST', '/billing/quote', {'plan_code': 'growth', 'seats': 3}, token=tok, headers=h)
    assert r3.status_code == 422 and r3.json()['error']['code'] == 'IDEMPOTENCY_KEY_REUSED'
    r4 = api.call('POST', '/billing/quote', {'plan_code': 'growth', 'seats': 3}, token=tok, headers={'Idempotency-Key': 'x' * 201})
    assert r4.status_code == 422 and r4.json()['error']['field'] == 'Idempotency-Key'


def test_etag_and_if_match(api, fresh):  # type: ignore[no-untyped-def]
    tok = api.login('santosh@seereon.co.in')
    r = api.call('GET', '/tenant', token=tok)
    v = r.json()['data']['version']
    assert r.headers['etag'] == f'"{v}"' and r.headers['cache-control'] == 'no-store'
    stale = api.call('PUT', '/tenant/theme', {'accent': '#B8912B'}, token=tok, headers={'If-Match': f'"{v - 1}"'})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'STALE'
    ok = api.call('PUT', '/tenant/theme', {'accent': '#B8912B'}, token=tok, headers={'If-Match': f'W/"{v}"'})
    assert ok.status_code == 200 and ok.json()['data']['version'] == v + 1 and ok.headers['etag'] == f'"{v + 1}"'


def test_view_as_read_only_and_tenant_read_only(api, fresh):  # type: ignore[no-untyped-def]
    from tests.integration.test_auth import q

    tok = api.login('santosh@seereon.co.in')
    r = api.call('PUT', '/tenant/theme', {'accent': '#B8912B'}, token=tok, headers={'X-View-As': 'employee'})
    assert r.status_code == 403 and r.json()['error']['code'] == 'VIEW_AS_READ_ONLY'
    me = api.call('GET', '/me', token=tok, headers={'X-View-As': 'employee'}).json()['data']
    assert me['view_as'] == 'employee' and me['role'] == 'employee' and me['real_role'] == 'admin'
    assert api.call('POST', '/notifications/read-all', token=tok, headers={'X-View-As': 'employee'}).status_code == 200  # allowViewAsWrite
    q("UPDATE tenants SET status = 'read_only' WHERE id = 'tn_seereon'")
    r = api.call('PUT', '/tenant/theme', {'accent': '#B8912B'}, token=tok)
    assert r.status_code == 402 and r.json()['error']['code'] == 'TENANT_READ_ONLY'
    assert api.call('POST', '/billing/quote', {'plan_code': 'growth', 'seats': 3}, token=tok).status_code == 200  # /billing stays writable


def test_must_change_password_guard(api, fresh):  # type: ignore[no-untyped-def]
    from tests.integration.test_auth import q

    tok = api.login('hritesh@seereon.co.in')
    q("UPDATE users SET must_change_password = true WHERE id = 'u_hritesh'")
    r = api.call('POST', '/notifications/read-all', token=tok)
    assert r.status_code == 403 and r.json()['error']['code'] == 'PASSWORD_CHANGE_REQUIRED'
    assert api.call('PATCH', '/me/prefs', {'density': 'compact'}, token=tok).status_code == 200
    r = api.call('POST', '/me/password', {'current': PASSWORD, 'next': 'Fresh passphrase 8'}, token=tok)
    assert r.status_code == 200 and r.json()['data']['user']['must_change_password'] is False


def test_cross_tenant_is_404_and_superadmin_403(api):  # type: ignore[no-untyped-def]
    tok = api.as_('ananya@kalingasoft.in')
    from tests.integration.test_auth import q

    nid = q("SELECT id FROM notifications WHERE tenant_id = 'tn_seereon' LIMIT 1")[0][0]
    r = api.call('POST', f'/notifications/{nid}/read', token=tok)
    assert r.status_code == 404 and r.json()['error']['message'] == 'Notification not found.'
    r = api.call('GET', '/admin/tenants', token=tok)
    assert r.status_code == 403 and r.json()['error']['message'] == 'Platform administrators only.'


def test_cors_allowlist(db, seed):  # type: ignore[no-untyped-def]
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app

    a = create_app(load_config(env_for(CORS_ORIGINS='https://app.example.com')), start_workers=False)
    with TestClient(a) as c:
        r = c.options('/api/v1/me', headers={'Origin': 'https://app.example.com', 'Access-Control-Request-Method': 'POST'})
        assert r.status_code == 204 and r.headers['access-control-allow-origin'] == 'https://app.example.com'
        assert 'Idempotency-Key' in r.headers['access-control-allow-headers'] and r.headers['access-control-max-age'] == '600'
        r = c.get('/api/v1/me', headers={'Origin': 'https://app.example.com'})
        assert r.headers['access-control-allow-origin'] == 'https://app.example.com' and 'ETag' in r.headers['access-control-expose-headers']
        assert 'access-control-allow-credentials' not in r.headers
        r = c.get('/api/v1/me', headers={'Origin': 'https://evil.example.com'})
        assert 'access-control-allow-origin' not in r.headers
        r = c.options('/api/v1/me', headers={'Origin': 'https://evil.example.com', 'Access-Control-Request-Method': 'POST'})
        assert r.status_code == 404
    a.state.rt.backend.pools.close()


def test_global_rate_limit(db, seed):  # type: ignore[no-untyped-def]
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app

    a = create_app(load_config(env_for(RATE_LIMIT_GLOBAL='3')), start_workers=False)
    with TestClient(a) as c:
        codes = [c.get('/api/v1/me').status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429]
        r = c.get('/api/v1/me')
        assert r.json()['error']['code'] == 'RATE_LIMITED' and re.fullmatch(r'Too many requests\. Try again in \d+ seconds\.', r.json()['error']['message'])
        assert int(r.headers['retry-after']) >= 1 and r.headers['ratelimit-limit'] == '3' and r.headers['ratelimit-remaining'] == '0'
    a.state.rt.backend.pools.close()


def test_auth_rate_limit_is_shared_across_processes(db, seed):  # type: ignore[no-untyped-def]
    """two independent app instances (own pools, own stores) = two processes sharing the database: ONE limit"""
    import psycopg
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app
    from tests.conftest import OWNER

    with psycopg.connect(OWNER, autocommit=True) as c:
        c.execute('DELETE FROM rate_limits')
    apps = [create_app(load_config(env_for(RATE_LIMIT_AUTH='4')), start_workers=False) for _ in range(2)]
    try:
        with TestClient(apps[0]) as a, TestClient(apps[1]) as b:
            body = {'email': 'ratelimit-probe@nowhere.io', 'password': 'x'}
            t0 = time.time()
            codes = [(a if i % 2 else b).post('/api/v1/auth/login', json=body).status_code for i in range(5)]
            if time.time() - t0 < 55:  # all inside one 60 s window
                assert codes == [401, 401, 401, 401, 429]
            r = a.post('/api/v1/auth/login', json=body)
            assert r.status_code == 429 and r.json()['error']['code'] == 'RATE_LIMITED' and r.headers['ratelimit-limit'] == '4'
    finally:
        for x in apps:
            x.state.rt.backend.pools.close()


def test_openapi_and_docs(client):  # type: ignore[no-untyped-def]
    d = client.get('/api/v1/openapi.json').json()
    assert d['openapi'] == '3.1.0' and '/api/v1/auth/login' in d['paths'] and d['paths']['/api/v1/auth/login']['post']['tags'] == ['auth']
    assert d['paths']['/api/v1/auth/reset-info/{token}']['get']['tags'] == ['people']
    r = client.get('/api/docs')
    assert r.status_code == 200 and 'swagger-ui' in r.text and "script-src 'self'" in r.headers['content-security-policy']
    # the Swagger UI assets are vendored in app/static/swagger-ui (no node_modules at runtime)
    for name in ('swagger-ui.css', 'index.css', 'swagger-ui-bundle.js', 'swagger-ui-standalone-preset.js', 'swagger-initializer.js'):
        a = client.get('/api/docs/static/' + name)
        assert a.status_code == 200 and len(a.content) > 50, name
    assert client.get('/api/docs/static/nope.js').status_code == 404


def test_api_docs_can_be_switched_off(db, seed):  # type: ignore[no-untyped-def]
    """API_DOCS_ENABLED=false (the production default): no schema, no Swagger UI — 404 like any unknown route"""
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app

    a = create_app(load_config(env_for(API_DOCS_ENABLED='false')), start_workers=False)
    try:
        with TestClient(a) as c:
            for path in ('/api/v1/openapi.json', '/api/docs', '/api/docs/json', '/api/docs/static/swagger-ui.css'):
                r = c.get(path)
                assert r.status_code == 404 and r.json()['error']['code'] == 'ROUTE_NOT_FOUND', path
            assert c.get('/healthz').status_code == 200
    finally:
        a.state.rt.backend.pools.close()


def test_platform_admin_on_workspace_routes(api, fresh):  # type: ignore[no-untyped-def]
    """G1: the platform super admin has no workspace row. Tenant routes behave like the TypeScript (default settings,
    `undefined` for a missing tenant) instead of 500s; the reads that lazily provision rows stay request-local."""
    from tests.integration.test_auth import q

    tok = api.as_('platform@seereon.co.in')
    for path in ('/inbox/summary', '/holidays', '/daily', '/approvals', '/wf/templates', '/wf/meta', '/leave-requests', '/okrs', '/tasks/create-options'):
        r = api.call('GET', path, token=tok)
        assert r.status_code == 200, (path, r.text)
    assert api.call('GET', '/inbox/summary', token=tok).json()['data'] == {'pending': 0, 'overdue': 0}
    r = api.call('GET', '/settings', token=tok)
    assert r.status_code == 404 and r.json()['error']['message'] == 'Workspace not found.'
    assert 'data' not in api.call('GET', '/tenant', token=tok).json()  # .find() -> undefined
    assert q("SELECT count(*) FROM wf_templates WHERE tenant_id = 'platform'")[0][0] == 0
    assert q("SELECT last_login_at IS NOT NULL FROM users WHERE id = 'u_platform'")[0][0] is True
