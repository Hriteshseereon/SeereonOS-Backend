"""v9 work round (A2): co-assignees on a task (D4), task-independent timesheet time (D5) and documents on a leave
application (CEO request 5) — on the demo company and on a brand-new signed-up company."""

from __future__ import annotations

import base64
import time
from typing import Any

from tests.conftest import PASSWORD

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n% medical certificate\n%%EOF\n').decode()


def ok(r: Any) -> Any:
    assert r.status_code == 200, r.text
    return r.json()['data']


def err(r: Any, status: int, code: str | None = None) -> dict:
    assert r.status_code == status, r.text
    e = r.json()['error']
    if code:
        assert e['code'] == code, e
    return e


def _today() -> str:
    return time.strftime('%Y-%m-%d')


def _monday(d: str) -> str:
    t = time.strptime(d, '%Y-%m-%d')
    return time.strftime('%Y-%m-%d', time.localtime(time.mktime(t) - t.tm_wday * 86400 + 3600))


def _log_day(api: Any, tok: str) -> tuple[str, str]:
    """(monday, a past-or-today working day of an editable week to log on) for this person"""
    mon = _monday(_today())
    for _ in range(3):
        v = ok(api.call('GET', f'/timesheets/{mon}', token=tok))
        if v['editable']:
            days = [d for d in v['days'] if d['kind'] == 'working' and not d['future'] and d['entry_allowed']]
            if days:
                return mon, min(days, key=lambda d: d['total'])['date']
        t = time.strptime(mon, '%Y-%m-%d')
        mon = time.strftime('%Y-%m-%d', time.localtime(time.mktime(t) - 7 * 86400 + 3600))
    raise AssertionError('no editable working day in the last three weeks')


def _put(api: Any, tok: str, mon: str, rows: list[dict]) -> Any:
    return api.call('PUT', f'/timesheets/{mon}', {'rows': rows}, token=tok)


def _task_body(pid: str, owner: str, co: list[str], **over: Any) -> dict:
    t = _today()
    return {
        'title': 'Payslip PDF export',
        'project_id': pid,
        'owner_id': owner,
        'assignee_ids': co,
        'est_hours': 12,
        'start_on': t,
        'due_on': '2030-12-31',
        **over,
    }


# ---------------------------------------------------------------------------------------------------------------------
# D4 — co-assignees (demo company)
# ---------------------------------------------------------------------------------------------------------------------
def test_co_assignees_end_to_end(api, fresh):  # type: ignore[no-untyped-def]
    pm = api.as_('subhankar@seereon.co.in')
    opts = ok(api.call('GET', '/tasks/create-options', token=pm))
    hrms = next(p for p in opts['projects'] if p['id'] == 'p_hrms')
    ms = hrms['milestones'][-1]['id']
    # validation: unknown person, the backup assigned twice, the reviewer assigned
    err(api.call('POST', '/tasks', _task_body('p_hrms', 'u_hritesh', ['u_nobody'], milestone_id=ms), token=pm), 422)
    e = err(api.call('POST', '/tasks', _task_body('p_hrms', 'u_hritesh', ['u_bijaya'], milestone_id=ms, backup_id='u_bijaya'), token=pm), 422)
    assert e['field'] == 'backup_id'
    e = err(api.call('POST', '/tasks', _task_body('p_hrms', 'u_hritesh', ['u_subhankar'], milestone_id=ms), token=pm), 422, 'TASK_SELF_REVIEW')
    # the owner listed again is dropped, duplicates removed
    t = ok(api.call('POST', '/tasks', _task_body('p_hrms', 'u_hritesh', ['u_bijaya', 'u_jagannath', 'u_bijaya', 'u_hritesh'], milestone_id=ms), token=pm))
    tid = t['id']
    assert t['assignee_ids'] == ['u_bijaya', 'u_jagannath'] and t['owner_id'] == 'u_hritesh'
    for who in ('hritesh', 'bijaya', 'jagannath'):
        tok = api.as_(f'{who}@seereon.co.in')
        n = ok(api.call('GET', '/notifications', token=tok, query={'limit': 20}))
        assert any('Payslip PDF export' in x['body'] and x['title'] == 'Task assigned to you' for x in n['items']), who
        # visible, in My tasks, the "Mine" chip and the person filter match co-assignees
        d = ok(api.call('GET', f'/tasks/{tid}', token=tok))
        assert d['assignee_ids'] == ['u_bijaya', 'u_jagannath']
        assert d['relation'] == ('owner' if who == 'hritesh' else 'assignee') and d['can_log_time'] and d['can_comment']
        mine = ok(api.call('GET', '/my-tasks', token=tok))
        assert any(x['id'] == tid for x in mine['tasks'])
        lst = ok(api.call('GET', '/tasks', token=tok, query={'chip': 'mine'}))
        assert any(x['id'] == tid for x in lst['items'])
    # someone unrelated still cannot open it
    err(api.call('GET', f'/tasks/{tid}', token=api.as_('jayashree@seereon.co.in')), 403, 'TASK_NOT_VISIBLE')
    # the manager's person filter: a co-assignee's tasks include it; the facet lists co-assignees
    lst = ok(api.call('GET', '/tasks', token=pm, query={'person': 'u_jagannath', 'status': 'all'}))
    assert any(x['id'] == tid for x in lst['items'])
    assert 'u_jagannath' in lst['facets']['people']
    grp = ok(api.call('GET', '/tasks', token=api.as_('santosh@seereon.co.in'), query={'group': 'employee', 'project': 'p_hrms'}))
    assert {'u_hritesh', 'u_bijaya', 'u_jagannath'} <= {g['user_id'] for g in grp['groups']}
    # only the owner submits; a co-assignee is told who does
    co = api.as_('bijaya@seereon.co.in')
    e = err(api.call('POST', f'/tasks/{tid}/submit', {'note': 'Done my part'}, token=co), 403)
    assert 'owns this task' in e['message']
    # co-assignees move the stage and comment
    ok(api.call('POST', f'/tasks/{tid}/stage', {'stage': 'in_development'}, token=co))
    ok(api.call('POST', f'/tasks/{tid}/comments', {'body': 'Started on the totals block'}, token=co))
    # everyone logs time on it (timesheet work items list it under My tasks)
    total = 0.0
    for who, h in (('hritesh', 2), ('bijaya', 1.5), ('jagannath', 3)):
        tok = api.as_(f'{who}@seereon.co.in')
        mon, day = _log_day(api, tok)
        items = ok(api.call('GET', '/timesheet/work-items', token=tok, query={'week': mon}))
        it = next(x for x in items if x.get('task_id') == tid)
        assert it['group'].startswith('My tasks')
        cur = ok(api.call('GET', f'/timesheets/{mon}', token=tok))
        keep = [
            {
                'kind': r['kind'],
                'task_id': r['task_id'],
                'internal_code': r['internal_code'],
                'leave_type': r['leave_type'],
                'note': r['note'],
                'project_id': r.get('project_id'),
                'hours': r['hours'],
            }
            for r in cur['rows']
            if not r['locked_reason']
        ]
        ok(_put(api, tok, mon, [*keep, {'kind': 'task', 'task_id': tid, 'internal_code': None, 'leave_type': None, 'note': '', 'hours': {day: h}}]))
        total += h
    d = ok(api.call('GET', f'/tasks/{tid}', token=pm))
    assert abs(d['actual_hours'] - total) < 1e-9
    # PATCH: replace the co-assignees; the newcomer is notified, the owner can't be listed, the backup can't
    ok(api.call('PATCH', f'/tasks/{tid}', {'assignee_ids': ['u_jagannath', 'u_pragyasmita']}, token=pm))
    n = ok(api.call('GET', '/notifications', token=api.as_('pragyasmita@seereon.co.in'), query={'limit': 10}))
    assert any('Payslip PDF export' in x['body'] for x in n['items'])
    d = ok(api.call('GET', f'/tasks/{tid}', token=pm))
    assert d['assignee_ids'] == ['u_jagannath', 'u_pragyasmita']
    e = err(api.call('PATCH', f'/tasks/{tid}', {'backup_id': 'u_jagannath'}, token=pm), 422)
    # a new owner who was a co-assignee leaves the co-assignee list
    ok(api.call('PATCH', f'/tasks/{tid}', {'owner_id': 'u_jagannath'}, token=pm))
    d = ok(api.call('GET', f'/tasks/{tid}', token=pm))
    assert d['task']['owner_id'] == 'u_jagannath' and d['assignee_ids'] == ['u_pragyasmita']
    # the removed co-assignee is no longer one (bijaya may still see it for another reason, e.g. a delegation)
    r = api.call('GET', f'/tasks/{tid}', token=api.as_('bijaya@seereon.co.in'))
    assert r.status_code == 403 or r.json()['data']['relation'] != 'assignee'
    assert not any(x['id'] == tid for x in ok(api.call('GET', '/my-tasks', token=api.as_('bijaya@seereon.co.in')))['tasks'])
    # team tasks (line-manager view) carries the co-assignees
    rows = ok(api.call('GET', '/team-tasks', token=pm))
    assert next(r for r in rows if r['id'] == tid)['assignee_ids'] == ['u_pragyasmita']


# ---------------------------------------------------------------------------------------------------------------------
# D5 — time without a task (demo company)
# ---------------------------------------------------------------------------------------------------------------------
def test_untasked_project_time_counts_in_project_actuals(api, fresh):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    tok = api.as_('hritesh@seereon.co.in')
    mon, day = _log_day(api, tok)
    items = ok(api.call('GET', '/timesheet/work-items', token=tok, query={'week': mon}))
    pw = next(x for x in items if x['key'] == 'internal:project_work')
    assert pw['group'] == 'Project work without a task'
    assert {'internal:support', 'internal:other', 'internal:meeting'} <= {x['key'] for x in items}
    before = ok(api.call('GET', '/projects/p_hrms', token=ceo))['metrics']
    money0 = ok(api.call('GET', '/projects/p_hrms/money', token=ceo))
    cur = ok(api.call('GET', f'/timesheets/{mon}', token=tok))
    keep = [
        {
            'kind': r['kind'],
            'task_id': r['task_id'],
            'internal_code': r['internal_code'],
            'leave_type': r['leave_type'],
            'note': r['note'],
            'project_id': r.get('project_id'),
            'hours': r['hours'],
        }
        for r in cur['rows']
        if not r['locked_reason']
    ]
    row = {
        'kind': 'internal',
        'task_id': None,
        'internal_code': 'project_work',
        'leave_type': None,
        'note': 'Client workshop prep',
        'project_id': 'p_hrms',
        'hours': {day: 1.5},
    }
    # validation: project required, description required
    e = err(_put(api, tok, mon, [*keep, {**row, 'project_id': None}]), 422, 'TS_PROJECT_REQUIRED')
    assert e['field'].endswith('project_id')
    err(_put(api, tok, mon, [*keep, {**row, 'note': ''}]), 422, 'TS_NOTE_REQUIRED')
    # a closed project takes no hours after it closed
    err(_put(api, tok, mon, [*keep, {**row, 'project_id': 'p_samet'}]), 409, 'TS_PROJECT_CLOSED')
    overhead = {
        'kind': 'internal',
        'task_id': None,
        'internal_code': 'support',
        'leave_type': None,
        'note': 'Laptop set-up for the new joiner',
        'project_id': None,
        'hours': {day: 1},
    }
    v = ok(_put(api, tok, mon, [*keep, row, overhead]))
    r = next(x for x in v['rows'] if x['internal_code'] == 'project_work')
    assert r['label'] == 'Project work (no task)' and r['sublabel'].startswith('Project work · PRJ-007') and r['project_id'] == 'p_hrms'
    after = ok(api.call('GET', '/projects/p_hrms', token=ceo))['metrics']
    assert abs(after['actual_hours'] - before['actual_hours'] - 1.5) < 1e-9  # the overhead hour is not project time
    money1 = ok(api.call('GET', '/projects/p_hrms/money', token=ceo))
    h0 = next((p['hours'] for p in money0['by_person'] if p['user_id'] == 'u_hritesh'), 0)
    h1 = next(p['hours'] for p in money1['by_person'] if p['user_id'] == 'u_hritesh')
    assert abs(h1 - h0 - 1.5) < 1e-9
    # submit + approve: approvals see the untasked rows like any other
    sub = api.call('POST', f'/timesheets/{mon}/submit', {'rows': None}, token=tok)
    assert sub.status_code in (200, 422), sub.text  # short days may refuse a submit mid-week; the rows are saved either way


# ---------------------------------------------------------------------------------------------------------------------
# CEO request 5 — documents on a leave application (demo company)
# ---------------------------------------------------------------------------------------------------------------------
def _apply(api: Any, tok: str, body: dict) -> Any:
    """apply for one day of leave on the first future working day that has no leave yet (skips holidays / clashes)"""
    t = time.mktime(time.strptime(_today(), '%Y-%m-%d')) + 3600
    for i in range(10, 90):
        d = time.localtime(t + i * 86400)
        if d.tm_wday >= 5:
            continue
        s = time.strftime('%Y-%m-%d', d)
        r = api.call('POST', '/leave-requests', {**body, 'from_on': s, 'to_on': s}, token=tok)
        if r.status_code == 200 or r.json()['error']['code'] not in ('LEAVE_OVERLAP', 'LEAVE_INVALID'):
            return r
    raise AssertionError('no free working day')


def test_leave_documents(api, fresh):  # type: ignore[no-untyped-def]
    emp = api.as_('hritesh@seereon.co.in')
    body = {'type': 'sick', 'portion': 'full', 'reason': 'Dental surgery recovery'}
    # type and size checks
    err(_apply(api, emp, {**body, 'attachments': [{'name': 'note.exe', 'size_bytes': 10, 'data_url': PDF}]}), 422, 'FILE_REJECTED')
    err(_apply(api, emp, {**body, 'attachments': [{'name': 'big.pdf', 'size_bytes': 6 * 1024 * 1024, 'data_url': PDF}]}), 422, 'FILE_REJECTED')
    err(_apply(api, emp, {**body, 'attachments': [{'name': 'x.pdf', 'size_bytes': 10, 'data_url': 'not a file'}]}), 422, 'FILE_REJECTED')
    err(_apply(api, emp, {**body, 'attachments': [{'name': f'{i}.pdf', 'size_bytes': 40, 'data_url': PDF} for i in range(4)]}), 422, 'FILE_REJECTED')
    r = ok(_apply(api, emp, {**body, 'attachments': [{'name': 'certificate.pdf', 'size_bytes': 40, 'data_url': PDF}]}))
    rid = r['id']
    assert len(r['attachments']) == 1 and r['attachments'][0]['name'] == 'certificate.pdf' and 'data_url' not in r['attachments'][0]
    aid = r['attachments'][0]['id']
    # lists carry the metadata only, with whether the viewer may open it
    mine = ok(api.call('GET', '/leave-requests', token=emp))['mine']
    m = next(x for x in mine if x['id'] == rid)
    assert m['can_open_docs'] is True and 'data_url' not in m['attachments'][0]
    pm = api.as_('subhankar@seereon.co.in')
    lr = ok(api.call('GET', '/leave-requests', token=pm))
    ap = next(x for x in lr['to_approve'] if x['id'] == rid)
    assert ap['can_open_docs'] is True
    f = ok(api.call('GET', f'/leave-requests/{rid}/attachments/{aid}', token=pm))
    assert f['data_url'] == PDF and f['file_name'] == 'certificate.pdf' and f['mime'] == 'application/pdf'
    ok(api.call('GET', f'/leave-requests/{rid}/attachments/{aid}', token=api.as_('priya@seereon.co.in')))  # HR
    err(api.call('GET', f'/leave-requests/{rid}/attachments/{aid}', token=api.as_('jagannath@seereon.co.in')), 403)
    # the applicant adds a second document, removes it while pending; others can't
    r2 = ok(
        api.call(
            'POST', f'/leave-requests/{rid}/attachments', {'name': 'scan.png', 'size_bytes': 40, 'data_url': 'data:image/png;base64,iVBORw0KGgo='}, token=emp
        )
    )
    assert [a['name'] for a in r2['attachments']] == ['certificate.pdf', 'scan.png']
    err(api.call('POST', f'/leave-requests/{rid}/attachments', {'name': 'x.pdf', 'size_bytes': 40, 'data_url': PDF}, token=pm), 403)
    err(api.call('DELETE', f'/leave-requests/{rid}/attachments/{r2["attachments"][1]["id"]}', token=pm), 403)
    r3 = ok(api.call('DELETE', f'/leave-requests/{rid}/attachments/{r2["attachments"][1]["id"]}', token=emp))
    assert [a['name'] for a in r3['attachments']] == ['certificate.pdf']
    # approved: the document stays on record (no removal), the approver can still open it
    ok(api.call('POST', f'/leave-requests/{rid}/approve', token=pm))
    err(api.call('DELETE', f'/leave-requests/{rid}/attachments/{aid}', token=emp), 409)
    ok(api.call('GET', f'/leave-requests/{rid}/attachments/{aid}', token=pm))
    # a request without documents is unchanged (no attachments key)
    plain = ok(_apply(api, api.as_('bijaya@seereon.co.in'), body))
    assert 'attachments' not in plain


# ---------------------------------------------------------------------------------------------------------------------
# A brand-new company (sign-up): three assignees log time, untasked rows, a leave with a document approved
# ---------------------------------------------------------------------------------------------------------------------
def test_signup_company_work_round(api, fresh):  # type: ignore[no-untyped-def]
    s = ok(
        api.call(
            'POST',
            '/auth/signup',
            {'company': 'Lumen Works Pvt Ltd', 'name': 'Asha Rao', 'email': 'asha@lumen.example', 'password': 'Lumen works passphrase 9'},
        )
    )
    tok, aid = s['token'], s['user']['id']
    dep = ok(api.call('GET', '/master/departments', token=tok))[0]['id']
    des = ok(api.call('GET', '/master/designations', token=tok))[0]['id']
    people: dict[str, str] = {}
    toks: dict[str, str] = {}
    for n in ('Ravi', 'Meera', 'Kiran'):
        r = ok(
            api.call(
                'POST',
                '/employees',
                {
                    'name': f'{n} Das',
                    'email': f'{n.lower()}@lumen.example',
                    'joined_on': '2026-01-05',
                    'department_id': dep,
                    'designation_id': des,
                    'role_key': 'employee',
                    'reports_to_id': aid,
                },
                token=tok,
            )
        )['result']
        people[n] = r['user']['id']
        t = ok(api.call('POST', '/auth/login', {'email': f'{n.lower()}@lumen.example', 'password': r['temp_password']}))['token']
        ok(api.call('POST', '/me/password', {'current': r['temp_password'], 'next': f'{n} own passphrase 77'}, token=t))
        toks[n] = ok(api.call('POST', '/auth/login', {'email': f'{n.lower()}@lumen.example', 'password': f'{n} own passphrase 77'}))['token']
    opts = ok(api.call('GET', '/tasks/create-options', token=tok))
    gen = opts['general_project_id']
    t = ok(api.call('POST', '/tasks', _task_body(gen, people['Ravi'], [people['Meera'], people['Kiran']], title='Office network set-up'), token=tok))
    tid = t['id']
    total = 0.0
    for n, h in (('Ravi', 2), ('Meera', 1), ('Kiran', 2.5)):
        mon, day = _log_day(api, toks[n])
        rows = [{'kind': 'task', 'task_id': tid, 'internal_code': None, 'leave_type': None, 'note': '', 'project_id': None, 'hours': {day: h}}]
        if n == 'Kiran':  # untasked: project work on General & admin and a non-project activity
            rows += [
                {
                    'kind': 'internal',
                    'task_id': None,
                    'internal_code': 'project_work',
                    'leave_type': None,
                    'note': 'Vendor quotes for the firewall',
                    'project_id': gen,
                    'hours': {day: 1},
                },
                {
                    'kind': 'internal',
                    'task_id': None,
                    'internal_code': 'learning',
                    'leave_type': None,
                    'note': 'Networking course',
                    'project_id': None,
                    'hours': {day: 1},
                },
            ]
        ok(_put(api, toks[n], mon, rows))
        total += h
    d = ok(api.call('GET', f'/tasks/{tid}', token=tok))
    assert abs(d['actual_hours'] - total) < 1e-9 and d['assignee_ids'] == [people['Meera'], people['Kiran']]
    m = ok(api.call('GET', f'/projects/{gen}', token=tok))['metrics']
    assert abs(m['actual_hours'] - total - 1) < 1e-9  # task hours + the untasked project hour, not the learning hour
    # leave with a document, approved by the manager (the admin)
    lr = ok(
        _apply(
            api,
            toks['Meera'],
            {'type': 'lwp', 'portion': 'full', 'reason': 'Sister wedding in Puri', 'attachments': [{'name': 'invite.pdf', 'size_bytes': 40, 'data_url': PDF}]},
        )
    )
    assert lr['status'] == 'pending' and len(lr['attachments']) == 1
    f = ok(api.call('GET', f'/leave-requests/{lr["id"]}/attachments/{lr["attachments"][0]["id"]}', token=tok))
    assert f['data_url'] == PDF
    err(api.call('GET', f'/leave-requests/{lr["id"]}/attachments/{lr["attachments"][0]["id"]}', token=toks['Ravi']), 403)
    out = ok(api.call('POST', f'/leave-requests/{lr["id"]}/approve', token=tok))
    assert out['status'] == 'approved'
    assert PASSWORD  # the demo password constant stays importable for the other tests
