"""v9 admin settings (A5, D10): leave types & policies, holiday calendars per location, financial-year opening
balances (lock / reopen / maker-checker, ledger + P&L), roles compare / who-has-it and the admin setup hub — on a
brand-new signed-up company and on the demo company."""

from __future__ import annotations

import base64
import time
from typing import Any

import pytest

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n% certificate\n%%EOF\n').decode()


def ok(r: Any) -> Any:
    assert r.status_code == 200, r.text
    return r.json()['data']


def err(r: Any, status: int, code: str | None = None) -> dict:
    assert r.status_code == status, r.text
    e = r.json()['error']
    if code:
        assert e['code'] == code, e
    return e


def _day(offset: int) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(time.time() + offset * 86400))


def _weekday_in(weeks: int, dow: int) -> str:
    """the given weekday (0 = Monday) `weeks` weeks from now"""
    t = time.localtime()
    return _day(weeks * 7 + (dow - t.tm_wday))


def _signup(api: Any) -> str:
    st = str(time.time_ns())[-9:]
    d = ok(
        api.call('POST', '/auth/signup', {'company': f'Admin Co {st}', 'name': 'Asha Rao', 'email': f'asha{st}@admin-co.example', 'password': 'Admin@12345!x'})
    )
    return d['token']


def _employee(api: Any, tok: str, name: str, loc: str, et: str = 'full_time') -> str:
    me = ok(api.call('GET', '/me', token=tok))
    me = me.get('user', me)
    m = {k: ok(api.call('GET', f'/master/{k}', token=tok)) for k in ('departments', 'designations', 'grades', 'shifts')}
    st = str(time.time_ns())[-9:]
    body = {
        'name': name,
        'email': f'{name.split()[0].lower()}{st}@admin-co.example',
        'personal_email': '',
        'phone': '',
        'joined_on': _day(-200),
        'employment_type': et,
        'department_id': m['departments'][1]['id'],
        'designation_id': m['designations'][2]['id'],
        'grade_id': m['grades'][1]['id'],
        'location_id': loc,
        'shift_id': m['shifts'][0]['id'],
        'cost_centre_id': None,
        'reports_to_id': me['id'],
        'role_key': 'employee',
        'extra_permissions': [],
        'monthly_ctc_paise': 6000000,
        'send_welcome': False,
    }
    d = ok(api.call('POST', '/employees', body, token=tok))
    d = d.get('result') or d
    t = ok(api.call('POST', '/auth/login', {'email': d['login_email'], 'password': d['temp_password']}))['token']
    ok(api.call('POST', '/me/password', {'current': d['temp_password'], 'next': 'Newpass@12345xY'}, token=t))
    return t


def test_new_company_admin_settings(api: Any) -> None:
    tok = _signup(api)
    hub = ok(api.call('GET', '/setup/hub', token=tok))
    items = {i['key']: i for g in hub['groups'] for i in g['items']}
    assert items['leave_types']['status'] == 'todo' and items['openings']['status'] == 'todo' and items['holidays']['status'] == 'todo'

    # ---- leave types & policies
    v = ok(api.call('GET', '/admin/leave-types', token=tok))
    assert not any(t['configured'] for t in v['types']) and v['can_edit']
    err(api.call('POST', '/admin/leave-types', {'code': 'casual', 'name': 'Casual', 'annual_days': 400, 'accrual': 'monthly'}, token=tok), 422, 'VALIDATION')
    err(api.call('POST', '/admin/leave-types', {'code': 'casual', 'name': 'Casual', 'annual_days': 12, 'accrual': 'weekly'}, token=tok), 422, 'VALIDATION')
    err(api.call('POST', '/admin/leave-types', {'code': 'lwp', 'name': 'LWP', 'annual_days': 1}, token=tok), 422)
    ok(api.call('POST', '/admin/leave-types', {'code': 'casual', 'name': 'Casual', 'annual_days': 12, 'accrual': 'annual'}, token=tok))
    ok(api.call('POST', '/admin/leave-types', {'code': 'sick', 'name': 'Sick', 'annual_days': 6, 'accrual': 'annual', 'attachment_after_days': 2}, token=tok))
    err(api.call('POST', '/admin/leave-types', {'name': 'casual', 'annual_days': 1}, token=tok), 409, 'LEAVE_TYPE_EXISTS')
    mat = ok(
        api.call(
            'POST',
            '/admin/leave-types',
            {'name': 'Maternity', 'annual_days': 182, 'accrual': 'annual', 'applicable_employment_types': ['full_time'], 'attachment_after_days': 0},
            token=tok,
        )
    )
    assert mat['code'] == 'maternity' and mat['is_custom']
    err(api.call('PATCH', '/admin/leave-types/maternity', {'annual_days': 180}, token=tok, headers={'If-Match': '99'}), 409, 'STALE')
    tmp = ok(api.call('POST', '/admin/leave-types', {'name': 'Bereavement', 'annual_days': 3}, token=tok))
    ok(api.call('DELETE', f'/admin/leave-types/{tmp["code"]}', token=tok))
    err(api.call('DELETE', '/admin/leave-types/casual', token=tok), 409, 'LEAVE_TYPE_BUILTIN')

    # ---- holiday calendars: a second calendar for a second location
    cals = ok(api.call('GET', '/holiday-calendars', token=tok))
    base = cals['default_id']
    hol_day = _weekday_in(3, 1)  # a Tuesday three weeks out
    pv = ok(
        api.call(
            'POST',
            '/holidays/import',
            {'rows': [{'date': _weekday_in(5, 2), 'name': 'Company day'}, {'date': '2026-02-30', 'name': 'Bad'}], 'mode': 'preview'},
            token=tok,
        )
    )
    assert pv['valid'] == 1 and pv['invalid'] == 1
    ok(api.call('POST', '/holidays/import', {'rows': [{'date': _weekday_in(5, 2), 'name': 'Company day'}], 'mode': 'commit'}, token=tok))
    second = ok(api.call('POST', '/holiday-calendars', {'name': 'Branch holidays', 'copy_from_id': base}, token=tok))
    assert second['copied'] == 1
    err(api.call('POST', '/holiday-calendars', {'name': 'branch HOLIDAYS'}, token=tok), 409, 'CALENDAR_EXISTS')
    ok(api.call('POST', '/holidays', {'date': hol_day, 'name': 'Branch festival', 'optional': False, 'calendar_id': second['id']}, token=tok))
    err(api.call('POST', '/holidays', {'date': hol_day, 'name': 'Again', 'optional': False, 'calendar_id': second['id']}, token=tok), 409, 'HOLIDAY_EXISTS')
    loc = ok(api.call('POST', '/master/locations', {'name': 'Branch', 'code': 'BR', 'holiday_calendar_id': second['id']}, token=tok))
    loc = loc.get('result') or loc
    assert loc['holiday_calendar_id'] == second['id'] and loc['holiday_calendar'] == 'Branch holidays'
    err(api.call('DELETE', f'/holiday-calendars/{second["id"]}', token=tok), 409, 'CALENDAR_IN_USE')
    ho = next(x for x in ok(api.call('GET', '/holiday-calendars', token=tok))['locations'] if x['id'] != loc['id'])

    branch = _employee(api, tok, 'Bina Branch', loc['id'])
    office = _employee(api, tok, 'Chetan Contract', ho['id'], 'contract')

    # options: maternity only for full-time; calendar of the person's location
    ob = ok(api.call('GET', '/leave/options', token=branch))
    oc = ok(api.call('GET', '/leave/options', token=office))
    assert ob['calendar']['id'] == second['id'] and oc['calendar']['id'] == base
    assert 'maternity' in [t['code'] for t in ob['types']] and 'maternity' not in [t['code'] for t in oc['types']]
    sick = next(t for t in ob['types'] if t['code'] == 'sick')
    assert sick['attachment_after_days'] == 2 and sick['balance']['remaining'] == 6  # annual: full quota

    # working-day maths follows the location calendar
    one = {'type': 'casual', 'portion': 'full', 'from_on': hol_day, 'to_on': hol_day, 'reason': 'Family function'}
    err(api.call('POST', '/leave-requests', one, token=branch), 422, 'LEAVE_INVALID')
    assert ok(api.call('POST', '/leave-requests', one, token=office))['days'] == 1
    wk = ok(api.call('GET', f'/timesheets/{_weekday_in(3, 0)}', token=branch))
    assert any(d['date'] == hol_day and d['label'] == 'Holiday' for d in wk['days'])
    wk2 = ok(api.call('GET', f'/timesheets/{_weekday_in(3, 0)}', token=office))
    assert not any(d['date'] == hol_day and d['label'] == 'Holiday' for d in wk2['days'])

    # attachment rule and applicability, server-side
    s3 = {'type': 'sick', 'portion': 'full', 'from_on': _weekday_in(6, 1), 'to_on': _weekday_in(6, 3), 'reason': 'Fever, doctor advised rest'}
    e = err(api.call('POST', '/leave-requests', s3, token=branch), 422, 'LEAVE_ATTACHMENT_REQUIRED')
    assert e['field'] == 'attachment'
    ok(api.call('POST', '/leave-requests', {**s3, 'attachments': [{'name': 'certificate.pdf', 'size_bytes': 40, 'data_url': PDF}]}, token=branch))
    m1 = {'type': 'maternity', 'portion': 'full', 'from_on': _weekday_in(8, 1), 'to_on': _weekday_in(8, 1), 'reason': 'Maternity leave'}
    err(api.call('POST', '/leave-requests', m1, token=office), 422, 'LEAVE_NOT_APPLICABLE')
    ok(api.call('PATCH', '/admin/leave-types/casual', {'active': False}, token=tok, headers={'If-Match': '1'}))
    err(api.call('POST', '/leave-requests', {**one, 'from_on': _weekday_in(7, 1), 'to_on': _weekday_in(7, 1)}, token=office), 422, 'LEAVE_TYPE_INACTIVE')
    err(api.call('POST', '/admin/leave-types', {'name': 'X type', 'annual_days': 1}, token=office), 403)
    err(api.call('POST', '/holiday-calendars', {'name': 'Mine'}, token=office), 403)

    # ---- opening balances
    o = ok(api.call('GET', '/finance/openings', token=tok))
    assert not o['locked'] and o['activity']['total'] == 0
    fy = o['fy_start_year']
    err(api.call('PUT', f'/finance/openings/{fy}', {'lines': [{'kind': 'bank', 'name': 'HDFC', 'amount_paise': -5}]}, token=tok), 422, 'MONEY_INVALID')
    err(api.call('PUT', f'/finance/openings/{fy}', {'lines': [{'kind': 'receivable', 'name': 'Cust', 'side': 'credit', 'amount_paise': 5}]}, token=tok), 422)
    lines = [
        {'kind': 'cash', 'name': 'Petty cash', 'amount_paise': 2_500_000},
        {'kind': 'bank', 'name': 'HDFC current', 'amount_paise': 150_000_000},
        {'kind': 'receivable', 'name': 'Old Client', 'amount_paise': 40_000_000},
        {'kind': 'payable', 'name': 'Landlord', 'amount_paise': 12_000_000},
        {'kind': 'ledger', 'name': 'Share capital', 'side': 'credit', 'amount_paise': 180_500_000},
    ]
    o = ok(api.call('PUT', f'/finance/openings/{fy}', {'lines': lines, 'notes': 'Audited'}, token=tok))
    assert o['totals']['cash_bank'] == 152_500_000 and o['totals']['difference'] == 0
    pnl = ok(api.call('GET', '/finance/pnl', token=tok))
    assert pnl['cash']['opening'] == 152_500_000 and pnl['cash']['opening_source'] == o['fy_label']
    assert pnl['opening_balances']['receivables'] == 40_000_000
    led = ok(api.call('GET', '/finance/ledger/entries', token=tok))
    assert led['opening']['closing_cash'] == 152_500_000
    # the first transaction of the FY locks them
    ok(
        api.call(
            'POST',
            '/finance/ledger',
            {'kind': 'expense', 'category': 'Rent', 'description': 'Office rent', 'amount_paise': 6_000_000, 'date': _day(-1)},
            token=tok,
        )
    )
    o = ok(api.call('GET', '/finance/openings', token=tok))
    assert o['locked'] and o['has_transactions']
    err(api.call('PUT', f'/finance/openings/{fy}', {'lines': lines}, token=tok, headers={'If-Match': str(o['version'])}), 409, 'OPENINGS_LOCKED')
    err(api.call('POST', f'/finance/openings/{fy}/reopen', {'reason': 'short'}, token=tok), 422)
    r = ok(api.call('POST', f'/finance/openings/{fy}/reopen', {'reason': 'Auditor corrected the bank balance'}, token=tok))
    assert r['pending'] is False and r['result']['reopened']
    lines[1]['amount_paise'] = 151_000_000
    o = ok(api.call('PUT', f'/finance/openings/{fy}', {'lines': lines}, token=tok, headers={'If-Match': str(r['result']['version'])}))
    o = ok(api.call('POST', f'/finance/openings/{fy}/lock', {}, token=tok, headers={'If-Match': str(o['version'])}))
    assert o['locked'] and [h['action'] for h in o['history']][:3] == ['locked', 'saved', 'reopened']
    pnl = ok(api.call('GET', '/finance/pnl', token=tok))
    assert pnl['cash']['opening'] == 153_500_000 and pnl['cash']['balance'] == 153_500_000 - 6_000_000
    err(api.call('GET', '/finance/openings', token=office), 403)

    hub = ok(api.call('GET', '/setup/hub', token=tok))
    items = {i['key']: i for g in hub['groups'] for i in g['items']}
    assert items['leave_types']['status'] == 'done' and items['openings']['status'] == 'done' and items['holidays']['status'] == 'done'
    err(api.call('GET', '/setup/hub', token=office), 403)


@pytest.mark.usefixtures('fresh')
def test_demo_reopen_with_maker_checker_and_roles(api: Any) -> None:
    ceo, admin = api.as_('santosh@seereon.co.in'), api.as_('debasish@seereon.co.in')
    cfg = ok(api.call('GET', '/maker-checker/config', token=ceo))
    r = ok(api.call('PUT', '/maker-checker/config', {'enabled': True, 'entities': [*cfg['entities'], 'opening_balance'], 'sla_days': 2}, token=ceo))
    if r.get('pending'):
        ok(api.call('POST', f'/changes/{r["change"]["id"]}/approve', {}, token=admin))
    o = ok(api.call('GET', '/finance/openings', token=ceo))
    assert o['locked'] and not o['exists']  # the demo FY already has transactions
    fy = o['fy_start_year']
    p = ok(api.call('POST', f'/finance/openings/{fy}/reopen', {'reason': 'Enter the audited balances'}, token=ceo))
    assert p['pending'] is True
    assert ok(api.call('GET', '/finance/openings', token=ceo))['pending']
    err(api.call('POST', f'/changes/{p["change"]["id"]}/approve', {}, token=ceo), 409, 'MAKER_CANNOT_CHECK')
    ok(api.call('POST', f'/changes/{p["change"]["id"]}/approve', {}, token=admin))
    o = ok(api.call('GET', '/finance/openings', token=ceo))
    assert o['reopened'] and not o['locked'] and o['history'][0]['action'] == 'reopened'
    ok(
        api.call(
            'PUT',
            f'/finance/openings/{fy}',
            {'lines': [{'kind': 'bank', 'name': 'ICICI current', 'amount_paise': 250_000_000}]},
            token=ceo,
            headers={'If-Match': str(o['version'])},
        )
    )
    assert ok(api.call('GET', '/finance/pnl', token=ceo))['cash']['opening'] == 250_000_000

    h = ok(api.call('GET', '/permissions/finance.view/holders', token=ceo))
    assert {x['key'] for x in h['roles']} >= {'administrator', 'ceo'} and any(x['via'] == 'role' for x in h['people'])
    err(api.call('GET', '/permissions/nope/holders', token=ceo), 404)
    c = ok(api.call('GET', '/roles-compare', token=ceo, query={'keys': 'ceo,employee'}))
    assert c['summary']['only']['ceo'] > 0 and c['summary']['only']['employee'] == 0
    err(api.call('GET', '/roles-compare', token=ceo, query={'keys': 'ceo'}), 422)
    err(api.call('GET', '/roles-compare', token=api.as_('hritesh@seereon.co.in'), query={'keys': 'ceo,employee'}), 403)
