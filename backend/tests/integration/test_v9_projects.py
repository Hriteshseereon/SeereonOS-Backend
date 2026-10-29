"""v9 A1 — customers master (D2), the New Project wizard (D3, one atomic create), the escalation matrix, the D1
commercials chain, the simplified cost calculator (All-milestone rows, % of price milestones) and change requests sent
to the team (item 7). Real PostgreSQL through the HTTP API (demo fixture + a signup tenant)."""

from __future__ import annotations

import uuid

from tests.conftest import PASSWORD


def _wizard(**over):  # type: ignore[no-untyped-def]
    b = {
        'request_key': str(uuid.uuid4()),
        'customer': {'mode': 'new', 'new': {'name': 'Odisha Rail Works', 'registered_name': 'Odisha Rail Works Pvt Ltd', 'gstin': '21AAKFM9876Q1ZP', 'cin': 'U72200OR2019PTC031234', 'state': 'Odisha',
                                            'contacts': [{'name': 'R. Das', 'email': 'rdas@orw.in'}], 'payments_profile': {'bank_name': 'SBI', 'account_no': '123456789012', 'ifsc': 'SBIN0001234'}}},
        'details': {'name': 'Signal app', 'pm_id': 'u_subhankar', 'start_on': '2026-11-02', 'end_on': '2027-02-26', 'type': 'service', 'billing_model': 'fixed', 'stream': 'services', 'priority': 2,
                    'contract_value_paise': None, 'description': '', 'client_details': {'contact_name': 'R. Das', 'po_reference': 'PO-1'}},
        'resources': [{'key': 'r1', 'user_id': 'u_bijaya', 'milestone_index': None, 'pct': 50}, {'key': 'r2', 'user_id': 'u_jagannath', 'milestone_index': 0, 'pct': 25}],
        'commercials': {'additional_costs': [{'id': 'a1', 'category': 'office_hardware', 'description': 'Phones', 'amount_paise': 5000000}, {'id': 'a2', 'category': 'travel', 'description': 'Visit', 'amount_paise': 1000000}],
                        'maintenance_paise': 0, 'contingency_pct': 0, 'markup_pct': 50, 'price_mode': 'markup'},
        'milestones': {'items': [{'name': 'Design', 'billing_amount_paise': 0, 'details': 'Wireframes signed', 'tasks': [{'title': 'Wireframes', 'owner_id': 'u_bijaya', 'est_hours': 16, 'subtasks': [{'title': 'Login screen', 'est_hours': 4}]}]},
                                 {'name': 'Build', 'billing_amount_paise': 0}]},
        'escalation': {'rows': [{'level': 1, 'user_id': 'u_subhankar', 'role': 'PM', 'after_value': 24, 'after_unit': 'hours'}, {'level': 2, 'name': 'R. Das', 'email': 'rdas@orw.in'}], 'notes': 'Call first.'},
    }
    b.update(over)
    return b


def test_wizard_creates_everything_in_one_call(api, fresh):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    # per-step validation without writing anything
    bad = _wizard()
    bad['customer'] = {'mode': 'new', 'new': {'name': 'X Corp', 'gstin': '21ABC'}}
    pv = api.call('POST', '/projects/wizard/preview', {**bad, 'step': 'customer'}, token=ceo).json()['data']
    assert pv['steps']['customer']['ok'] is False and pv['steps']['customer']['error']['field'] == 'customer.gstin'
    pv = api.call('POST', '/projects/wizard/preview', _wizard(), token=ceo).json()['data']
    assert all(s['ok'] for s in pv['steps'].values())
    t = pv['costing']['totals']
    groups = {g['key']: g['amount'] for g in t['groups']}
    assert groups['office_hardware'] == 5000000 and groups['expenses'] == 1000000  # travel maps onto Expenses (D1)
    assert t['total_cost'] == t['resources'] + 6000000 and t['price'] == t['actual_cost'] + round(t['actual_cost'] * 0.5)
    assert abs(t['margin'] - 1 / 3) < 0.001 and abs(t['markup'] - 0.5) < 0.001
    assert {p['name'] for p in t['people_share']} == {'Bijaya Jena', 'Jagannath Nayak'} and abs(sum(p['share'] for p in t['people_share']) - 1) < 1e-9
    body = _wizard()
    r = api.call('POST', '/projects/wizard', body, token=ceo)
    assert r.status_code == 200, r.text
    out = r.json()['data']
    p = out['project']
    assert out['tasks'] == 2 and p['status'] == 'initiated' and p['client'] == 'Odisha Rail Works' and p['client_id']
    assert p['contract_value_paise'] == t['price'] and p['client_details']['po_reference'] == 'PO-1'
    assert [x['level'] for x in p['escalation']['rows']] == [1, 2]
    # idempotent: the same submission returns the same project
    again = api.call('POST', '/projects/wizard', body, token=ceo).json()['data']
    assert again['duplicate'] is True and again['project']['id'] == p['id']
    d = api.call('GET', f'/projects/{p["id"]}', token=ceo).json()['data']
    assert [m['name'] for m in d['milestones']] == ['Design', 'Build'] and len(d['allocations']) == 2
    sub = next(x for x in d['tasks'] if x['title'] == 'Login screen')
    assert sub['parent_task_id'] == next(x for x in d['tasks'] if x['title'] == 'Wireframes')['id']
    # the customer lists the project; payments masked without finance.edit
    projs = api.call('GET', f'/clients/{p["client_id"]}/projects', token=ceo).json()['data']
    assert [x['code'] for x in projs['items']] == [p['code']]
    sales = api.login('baishali@seereon.co.in', PASSWORD)
    pp = api.call('GET', f'/clients/{p["client_id"]}', token=sales).json()['data']['client']['payments_profile']
    assert pp['account_no'].endswith('9012') and '•' in pp['account_no']
    # escalation tab: anyone on the project reads, only editors write
    emp = api.login('bijaya@seereon.co.in', PASSWORD)
    assert api.call('GET', f'/projects/{p["id"]}/escalation', token=emp).json()['data']['can_edit'] is False
    assert api.call('PUT', f'/projects/{p["id"]}/escalation', {'rows': [], 'notes': 'x'}, token=emp).status_code == 403
    r = api.call('PUT', f'/projects/{p["id"]}/escalation', {'rows': [{'level': 1, 'name': 'Ops', 'email': 'nope'}], 'notes': ''}, token=ceo)
    assert r.status_code == 422 and r.json()['error']['field'] == 'escalation.rows.0.email'


def test_wizard_is_atomic_and_permissioned(api, fresh):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    n0 = len(api.call('GET', '/projects?status=all', token=ceo).json()['data']['items'])
    c0 = len(api.call('GET', '/customers/options', token=ceo).json()['data']['items'])
    body = _wizard()
    body['milestones']['items'][0]['tasks'][0]['owner_id'] = 'u_nobody'  # fails after the customer would be created
    r = api.call('POST', '/projects/wizard', body, token=ceo)
    assert r.status_code == 422
    assert len(api.call('GET', '/projects?status=all', token=ceo).json()['data']['items']) == n0
    assert len(api.call('GET', '/customers/options', token=ceo).json()['data']['items']) == c0
    emp = api.login('hritesh@seereon.co.in', PASSWORD)
    assert api.call('POST', '/projects/wizard', _wizard(), token=emp).status_code == 403
    assert api.call('GET', '/customers/options', token=emp).status_code == 403


def test_change_request_sent_to_the_team(api, fresh):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    p = api.call('POST', '/projects/wizard', _wizard(), token=ceo).json()['data']['project']
    pm = api.login('subhankar@seereon.co.in', PASSWORD)
    cr = api.call('POST', f'/projects/{p["id"]}/change-requests', {'title': 'Offline mode', 'requested_by_client': 'R. Das', 'hours': 10, 'value_paise': 100000}, token=pm).json()['data']
    emp = api.login('bijaya@seereon.co.in', PASSWORD)
    assert api.call('POST', f'/change-requests/{cr["id"]}/send', {'user_ids': ['u_jagannath']}, token=emp).status_code == 403
    assert api.call('POST', f'/change-requests/{cr["id"]}/send', {'user_ids': ['u_subhankar']}, token=pm).status_code == 422
    r = api.call('POST', f'/change-requests/{cr["id"]}/send', {'user_ids': ['u_bijaya'], 'note': 'Check the effort'}, token=pm)
    assert r.status_code == 200 and [x['user_id'] for x in r.json()['data']['sent_to']] == ['u_bijaya']
    mine = api.call('GET', '/change-requests/sent-to-me', token=emp).json()['data']
    assert mine['unseen'] == 1 and mine['items'][0]['value_paise'] is None  # no billable value for an employee
    lst = api.call('GET', f'/projects/{p["id"]}/change-requests', token=emp).json()['data']
    assert lst['recipient_view'] is True and len(lst['items']) == 1
    notes = api.call('GET', '/notifications', token=emp).json()['data']
    items = notes['items'] if isinstance(notes, dict) else notes
    assert any('CR-1 sent to you' in n['title'] for n in items)
    assert api.call('POST', f'/change-requests/{cr["id"]}/seen', {}, token=emp).status_code == 200
    assert api.call('GET', '/change-requests/sent-to-me', token=emp).json()['data']['unseen'] == 0
    # the decision flow is unchanged
    r = api.call('POST', f'/change-requests/{cr["id"]}/decision', {'decision': 'approve'}, token=pm)
    assert r.status_code == 200 and r.json()['data']['status'] == 'approved'


def test_calculator_all_milestone_rows_and_price_shares(api, fresh):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    b = {'start_on': '2026-11-02', 'end_on': '2027-01-29', 'milestones': [{'name': 'A', 'billing_amount_paise': 0, 'billing_pct': 40}, {'name': 'B', 'billing_amount_paise': 0, 'billing_pct': 60}],
         'resources': [{'key': 'r1', 'role_label': 'React dev', 'monthly_ctc_paise': 9000000, 'milestone_index': None, 'pct': 100}],
         'additional_costs': [], 'maintenance_paise': 0, 'contingency_pct': 0, 'markup_pct': 50, 'price_mode': 'markup'}
    d = api.call('POST', '/estimates/preview', b, token=ceo).json()['data']
    assert d['rows'][0]['ms_code'] == 'All' and d['rows'][0]['error'] is None
    assert sum(m['people_cost'] for m in d['by_milestone']) == d['totals']['resources']
    assert sum(m['billing_amount_paise'] for m in d['milestones']) == d['totals']['price']
    assert d['milestones'][0]['billing_amount_paise'] == round(d['totals']['price'] * 0.4)


def test_new_company_wizard(api, fresh):  # type: ignore[no-untyped-def]
    r = api.call('POST', '/auth/signup', {'company': 'Lotus Works', 'name': 'Asha Rao', 'email': 'asha@lotus.example', 'password': 'Lotus flowers 42 bloom'})
    tok = r.json()['data']['token']
    me = api.call('GET', '/me', token=tok).json()['data']
    uid = me['user']['id'] if 'user' in me else me['id']
    assert api.call('GET', '/customers/options', token=tok).json()['data']['items'] == []
    body = _wizard(resources=[], escalation={'rows': [], 'notes': ''}, milestones={'items': []})
    body['details'] = {**body['details'], 'pm_id': uid}
    body['customer'] = {'mode': 'internal'}
    r = api.call('POST', '/projects/wizard', body, token=tok)
    assert r.status_code == 200, r.text
    p = r.json()['data']['project']
    assert p['client'] == 'Internal' and not p.get('client_id') and p['code'].endswith('001')
