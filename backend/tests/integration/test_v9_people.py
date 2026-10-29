"""v9 people round (A3): public apply links on openings (D7), the teams master + People › Team overview (D6) and
Documents › Team documents (D8) — on the demo company and on a brand-new signed-up company."""

from __future__ import annotations

import base64
import time
from typing import Any

import pytest

from tests.conftest import PASSWORD, Api, env_for, owner_sql

PDF = b'%PDF-1.4\n1 0 obj<< /Type /Catalog >>endobj\ntrailer<<>>\n%%EOF\n'


def ok(r: Any) -> Any:
    assert r.status_code == 200, r.text
    return r.json()['data']


def err(r: Any, status: int, code: str | None = None, field: str | None = None) -> dict:
    assert r.status_code == status, r.text
    e = r.json()['error']
    if code:
        assert e['code'] == code, e
    if field:
        assert e.get('field') == field, e
    return e


def resume(data: bytes = PDF, name: str = 'cv.pdf', mime: str = 'application/pdf') -> dict:
    return {'name': name, 'mime': mime, 'size_bytes': len(data), 'data_url': f'data:{mime};base64,' + base64.b64encode(data).decode()}


def application(email: str, **over: Any) -> dict:
    b = {
        'name': 'Asha Verma',
        'email': email,
        'phone': '+91 98765 43210',
        'linkedin_url': 'https://www.linkedin.com/in/asha-verma',
        'current_company': 'Acme',
        'current_title': 'ABAP developer',
        'experience_years': 4,
        'notice_days': 30,
        'expected_ctc_paise': 150000000,
        'location': 'Pune',
        'cover_note': 'Keen to join.',
        'consent': True,
        'resume': resume(),
        'website': '',
    }
    b.update(over)
    return b


@pytest.fixture(scope='module')
def capi(db, seed):  # type: ignore[no-untyped-def]
    """an API whose careers limit does not get in the way of the functional checks"""
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app
    from tests.conftest import reload_fixture

    reload_fixture(seed)
    owner_sql('DELETE FROM rate_limits')  # buckets are hashed; the test database only
    a = create_app(load_config(env_for(RATE_LIMIT_CAREERS='1000')))
    with TestClient(a) as c:
        yield Api(c)
    a.state.rt.backend.pools.close()


def _link(api: Api, tok: str) -> tuple[dict, str]:
    ops = ok(api.call('GET', '/openings', token=tok))
    o = next(x for x in ops if x['status'] in ('open', 'interviewing'))
    assert 'public_url' not in o or o['public_url']
    v = ok(api.call('POST', f'/openings/{o["id"]}/public-link', {}, token=tok))
    assert v['public_url'].startswith('https://app.seereonos.com/careers/'), v  # APP_URL drives the link
    return v, v['public_url'].rsplit('/', 1)[1]


def test_public_apply_flow(capi):  # type: ignore[no-untyped-def]
    api = capi
    ceo = api.as_('santosh@seereon.co.in')
    o, token = _link(api, ceo)
    # job description
    ok(
        api.call(
            'PATCH',
            f'/openings/{o["id"]}',
            {
                'about': 'Own integrations.',
                'requirements': '- ABAP\n- CPI',
                'work_mode': 'hybrid',
                'salary_min_paise': 100,
                'salary_max_paise': 200,
                'salary_visible': False,
            },
            token=ceo,
        )
    )
    err(api.call('PATCH', f'/openings/{o["id"]}', {'salary_min_paise': 500}, token=ceo), 422, 'VALIDATION', 'salary_min_paise')
    err(api.call('PATCH', f'/openings/{o["id"]}', {'employment_type': 'gig'}, token=ceo), 422, 'VALIDATION', 'employment_type')
    pub = ok(api.call('GET', f'/public/openings/{token}'))
    assert pub['accepting'] and pub['opening']['requirements'] == ['ABAP', 'CPI'] and pub['opening']['salary_min_paise'] is None  # hidden
    assert 'notes' not in pub['opening'] and 'owner_id' not in pub['opening'] and 'project_id' not in pub['opening']
    err(api.call('GET', '/public/openings/' + 'A' * 43), 404, 'NOT_FOUND')
    err(api.call('GET', '/public/openings/short'), 404, 'NOT_FOUND')
    # validation + file checks
    email = f'asha.{time.time_ns()}@example.com'
    err(api.call('POST', f'/public/openings/{token}', application(email, consent=False)), 422, field='consent')
    err(api.call('POST', f'/public/openings/{token}', application(email, linkedin_url='https://evil.example/in/x')), 422, field='linkedin_url')
    err(api.call('POST', f'/public/openings/{token}', application(email, resume=resume(name='cv.exe'))), 422, 'FILE_REJECTED', 'resume')
    err(
        api.call('POST', f'/public/openings/{token}', application(email, resume=resume(b'PK\x03\x04 zip pretending', name='cv.pdf'))),
        422,
        'FILE_REJECTED',
        'resume',
    )
    err(
        api.call('POST', f'/public/openings/{token}', application(email, resume=resume(b'%PDF-1.4' + b'0' * (5 * 1024 * 1024)))),
        422,
        'FILE_TOO_LARGE',
        'resume',
    )
    docx = b'PK\x03\x04' + b'\x00' * 40
    # honeypot: looks fine, stores nothing
    assert ok(api.call('POST', f'/public/openings/{token}', application('bot@example.com', website='http://spam')))['received']
    # the real application (a session header is ignored: the public route resolves the tenant from the token)
    assert ok(
        api.call(
            'POST',
            f'/public/openings/{token}',
            application(email, resume=resume(docx, 'cv.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
        )
    )['received']
    err(api.call('POST', f'/public/openings/{token}', application(email)), 409, 'ALREADY_APPLIED')
    rec = ok(api.call('GET', '/recruitment', token=ceo))
    c = next(x for x in rec['candidates'] if x['email'] == email)
    assert c['source'] == 'careers' and c['stage'] == 'applied' and c['linkedin_url'] and c['consent_at'] and c['resume_name'] == 'cv.docx'
    assert not any(x['email'] == 'bot@example.com' for x in rec['candidates'])
    owner_sql_n = owner_sql("SELECT count(*) FROM notifications WHERE user_id = %s AND title = 'New application' AND body LIKE 'Asha Verma%%'", o['owner_id'])
    assert owner_sql_n[0][0] >= 1
    v = next(x for x in ok(api.call('GET', '/openings', token=ceo)) if x['id'] == o['id'])
    assert v['applications'] >= 1 and v['accepting_now'] is True
    # closed: status, then the manual switch
    ok(api.call('PATCH', f'/openings/{o["id"]}', {'status': 'filled'}, token=ceo))
    pub = ok(api.call('GET', f'/public/openings/{token}'))
    assert pub['accepting'] is False and pub['closed_reason'] == 'filled'
    err(api.call('POST', f'/public/openings/{token}', application('late@example.com')), 409, 'OPENING_CLOSED')
    ok(api.call('PATCH', f'/openings/{o["id"]}', {'status': 'open', 'accepting_applications': False}, token=ceo))
    err(api.call('POST', f'/public/openings/{token}', application('late@example.com')), 409, 'OPENING_CLOSED')
    ok(api.call('PATCH', f'/openings/{o["id"]}', {'accepting_applications': True}, token=ceo))
    # regenerate: the old link stops working
    new = ok(api.call('POST', f'/openings/{o["id"]}/public-link', {'regenerate': True}, token=ceo))['public_url'].rsplit('/', 1)[1]
    assert new != token
    err(api.call('GET', f'/public/openings/{token}'), 404)
    ok(api.call('GET', f'/public/openings/{new}'))
    # permissions
    err(api.call('POST', f'/openings/{o["id"]}/public-link', {}, token=api.as_('hritesh@seereon.co.in')), 403)


def test_careers_rate_limit(db, seed):  # type: ignore[no-untyped-def]
    from starlette.testclient import TestClient

    from app.config import load_config
    from app.main import create_app

    owner_sql('DELETE FROM rate_limits')  # buckets are hashed; the test database only
    a = create_app(load_config(env_for(RATE_LIMIT_CAREERS='2')))
    with TestClient(a) as c:
        codes = [c.post('/api/v1/public/openings/' + 'B' * 43, json={}).status_code for _ in range(3)]
        assert codes == [404, 404, 429], codes
        r = c.post('/api/v1/public/openings/' + 'B' * 43, json={})
        assert r.json()['error']['code'] == 'RATE_LIMITED' and int(r.headers['retry-after']) >= 1
    a.state.rt.backend.pools.close()
    owner_sql('DELETE FROM rate_limits')  # buckets are hashed; the test database only


def test_teams_overview_and_team_documents(capi):  # type: ignore[no-untyped-def]
    api = capi
    ceo = api.as_('santosh@seereon.co.in')
    emp = api.as_('hritesh@seereon.co.in')
    ov = ok(api.call('GET', '/teams/overview', token=ceo))
    users = [m for g in ov['groups'] for m in g['members']]
    ids = list(dict.fromkeys(m['id'] for m in users))
    err(api.call('POST', '/teams', {'name': 'Q'}, token=ceo), 422, field='name')
    err(api.call('POST', '/teams', {'name': 'Hackers'}, token=emp), 403)
    t = ok(api.call('POST', '/teams', {'name': f'Core {time.time_ns()}', 'lead_id': ids[0], 'member_ids': ids[1:3]}, token=ceo))
    assert t['member_ids'][0] == ids[0] and t['member_count'] == 3
    err(api.call('POST', '/teams', {'name': t['name'].upper()}, token=ceo), 409, 'DUPLICATE')
    ov = ok(api.call('GET', '/teams/overview', token=ceo))
    assert ov['mode'] == 'teams'
    g = next(x for x in ov['groups'] if x['id'] == t['id'])
    assert len(g['members']) == 3 and all(m['week'] and len(m['week']['days']) == 7 for m in g['members'])
    assert g['totals']['hours'] == round(sum(m['week']['hours'] for m in g['members']), 2)
    err(api.call('GET', '/teams/overview', token=emp), 403)
    docs = ok(api.call('GET', '/employee-docs/teams', token=ceo))
    assert docs['mode'] == 'teams' and any(x['id'] == t['id'] and len(x['people']) == 3 for x in docs['groups'])
    err(api.call('GET', '/employee-docs/teams', token=emp), 403)
    err(api.call('GET', '/employee-docs/teams', token=api.as_('subhankar@seereon.co.in')), 403)
    ok(api.call('DELETE', f'/teams/{t["id"]}', token=ceo))


def test_new_company(capi):  # type: ignore[no-untyped-def]
    api = capi
    email = f'owner.{time.time_ns()}@newco.example'
    s = ok(api.call('POST', '/auth/signup', {'company': 'Newco Labs', 'name': 'Nia Owner', 'email': email, 'password': PASSWORD}))
    tok = s['token']
    ov = ok(api.call('GET', '/teams/overview', token=tok))
    assert ov['mode'] == 'departments' and ov['totals']['people'] >= 1
    assert ok(api.call('GET', '/teams', token=tok)) == []
    me = ok(api.call('GET', '/me', token=tok))['user']
    t = ok(api.call('POST', '/teams', {'name': 'Founders', 'lead_id': me['id'], 'member_ids': []}, token=tok))
    assert ok(api.call('GET', '/teams/overview', token=tok))['groups'][0]['id'] == t['id']
    assert ok(api.call('GET', '/employee-docs/teams', token=tok))['groups'][0]['name'] == 'Founders'
    o = ok(
        api.call(
            'POST',
            '/openings',
            {'role': 'First engineer', 'count': 1, 'status': 'open', 'owner_id': me['id'], 'publish_link': True, 'about': 'Build v1.'},
            token=tok,
        )
    )
    token = o['public_url'].rsplit('/', 1)[1]
    pub = ok(api.call('GET', f'/public/openings/{token}'))
    assert pub['company']['name'] == 'Newco Labs' and pub['company']['logo_data_url'] is None
    ok(api.call('POST', f'/public/openings/{token}', application('first.applicant@example.com')))
    rec = ok(api.call('GET', '/recruitment', token=tok))
    assert [c['source'] for c in rec['candidates']] == ['careers']
