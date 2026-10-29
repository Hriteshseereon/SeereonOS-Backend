"""V9-FIX: the items QA9-A / QA9-B left open — joiner/leaver proration, per-person calendars in costing, GSTIN messages,
opening receivables in reports and insights, demo fixture linkage, milestone sync before margins, one definition of
approved hours, portfolio scope labels and Gantt marker links."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from tests.integration.test_v9_admin import _signup


def ok(r: Any) -> Any:
    assert r.status_code == 200, r.text
    return r.json()['data']


def err(r: Any, status: int, code: str | None = None) -> dict:
    assert r.status_code == status, r.text
    e = r.json()['error']
    if code:
        assert e['code'] == code, e
    return e


def _employee(api: Any, tok: str, name: str, joined_on: str, ctc: int) -> None:
    me = ok(api.call('GET', '/me', token=tok))
    me = me.get('user', me)
    m = {k: ok(api.call('GET', f'/master/{k}', token=tok)) for k in ('departments', 'designations', 'grades', 'shifts', 'locations')}
    st = str(time.time_ns())[-9:]
    body = {
        'name': name,
        'email': f'{name.split()[0].lower()}{st}@fix-co.example',
        'personal_email': '',
        'phone': '',
        'joined_on': joined_on,
        'employment_type': 'full_time',
        'department_id': m['departments'][1]['id'],
        'designation_id': m['designations'][2]['id'],
        'grade_id': m['grades'][1]['id'],
        'location_id': m['locations'][0]['id'],
        'shift_id': m['shifts'][0]['id'],
        'cost_centre_id': None,
        'reports_to_id': me['id'],
        'role_key': 'employee',
        'extra_permissions': [],
        'monthly_ctc_paise': ctc,
        'send_welcome': False,
    }
    ok(api.call('POST', '/employees', body, token=tok))


def test_joiner_is_prorated_in_payroll_register_and_pnl(api):  # type: ignore[no-untyped-def]
    tok = _signup(api)
    first = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).replace(day=1)  # last month
    last = dt.date.today().replace(day=1) - dt.timedelta(days=1)
    joined = first.replace(day=15).isoformat()
    _employee(api, tok, 'Mid Joiner', joined, 6_000_000)
    _employee(api, tok, 'Early Bird', (first - dt.timedelta(days=40)).isoformat(), 3_000_000)
    month_wd = ok(api.call('GET', f'/projects/calendar-summary?from={first.isoformat()}&to={last.isoformat()}', token=tok))['working_days']
    employed_wd = ok(api.call('GET', f'/projects/calendar-summary?from={joined}&to={last.isoformat()}', token=tok))['working_days']
    rep = ok(
        api.call(
            'POST',
            '/report-types/payroll_register/run',
            {'period': {'from': first.isoformat(), 'to': last.isoformat()}, 'fields': ['person', 'ctc', 'base', 'net', 'working_days']},
            token=tok,
        )
    )
    rows = {r['person']: r for r in rep['rows']}
    mid, early = rows['Mid Joiner'], rows['Early Bird']
    assert mid['ctc'] == 6_000_000 and mid['working_days'] == employed_wd < month_wd
    assert mid['base'] == round(6_000_000 / month_wd * employed_wd), (mid, month_wd, employed_wd)
    assert early['base'] == 3_000_000 and early['working_days'] == month_wd
    # the P&L payroll basis (no salaries booked): the same prorated CTC
    pnl = ok(api.call('GET', f'/finance/pnl?from={first.isoformat()}&to={last.isoformat()}', token=tok))
    month = next(m for m in pnl['monthly'] if m['month'] == first.isoformat()[:7])
    assert month['payroll'] == mid['base'] + early['base'], (month, mid['base'], early['base'])


def test_gstin_message_says_what_is_wrong(api):  # type: ignore[no-untyped-def]
    tok = _signup(api)
    cases = {
        '21AAKFM9876Q1Z': 'this one has 14',
        '00AAKFM9876Q1ZB': '“00” is not a state code',
        '21AAK1M9876Q1ZB': 'is not a valid PAN',
        '21AAKFM9876Q0ZB': 'can’t be 0',
        '21AAKFM9876Q1XB': 'always “Z”',
    }
    for g, part in cases.items():
        e = err(api.call('POST', '/clients', {'name': f'Customer {g}', 'gstin': g, 'state': 'Odisha'}, token=tok), 422, 'VAL_GSTIN')
        assert part in e['message'], (g, e['message'])
    # well formed but the check character does not match: saved, with a note
    r = api.call('POST', '/clients', {'name': 'Typo Co', 'gstin': '27AAPFU0939F1ZW', 'state': 'Maharashtra'}, token=tok)
    assert r.status_code == 200 and any(w['code'] == 'GSTIN_CHECK' and '“V”' in w['message'] for w in r.json()['warnings']), r.json()
    r = api.call('POST', '/clients', {'name': 'Right Co', 'gstin': '27AAPFU0939F1ZV', 'state': 'Maharashtra'}, token=tok)
    assert r.status_code == 200 and not r.json().get('warnings'), r.json()


def test_opening_receivables_in_receivables_report_and_insights(api):  # type: ignore[no-untyped-def]
    tok = _signup(api)
    o = ok(api.call('GET', '/finance/openings', token=tok))
    fy = o['fy_start_year']
    lines = [
        {'kind': 'bank', 'name': 'HDFC current', 'amount_paise': 10_000_000},
        {'kind': 'receivable', 'name': 'Old Client', 'amount_paise': 4_000_000},
        {'kind': 'receivable', 'name': 'Newer Client', 'amount_paise': 1_000_000, 'due_on': (dt.date.today() + dt.timedelta(days=10)).isoformat()},
        {'kind': 'payable', 'name': 'Landlord', 'amount_paise': 2_000_000},
        {'kind': 'ledger', 'name': 'Share capital', 'side': 'credit', 'amount_paise': 13_000_000},
    ]
    ok(api.call('PUT', f'/finance/openings/{fy}', {'lines': lines}, token=tok))
    rep = ok(api.call('POST', '/report-types/receivables/run', {'period': {'preset': 'this_fy'}, 'fields': ['number', 'client', 'outstanding', 'ageing']}, token=tok))
    rows = [r for r in rep['rows'] if r['number'] == 'Opening balance']
    by = {r['client']: r for r in rows}
    assert by['Old Client']['outstanding'] == 4_000_000 and by['Old Client']['ageing'] == 'Over 90 days overdue'
    assert by['Newer Client']['ageing'] == 'Not due'
    assert next(k for k in rep['kpis'] if k['key'] == 'outstanding')['value'] == 5_000_000
    ins = ok(api.call('GET', '/insight-views/revenue', token=tok))
    kp = {k['label']: k['value'] for k in ins['kpis']}
    assert any(lbl.startswith('Opening receivables') and v == 5_000_000 for lbl, v in kp.items()), kp
    assert any(lbl.startswith('Opening payables') and v == 2_000_000 for lbl, v in kp.items()), kp
    ageing = next(c for c in ins['charts'] if c.get('id') == 'ageing')
    vals = dict(zip(ageing['categories'], ageing['series'][0]['values'], strict=True))
    assert vals['Over 90 days'] == 4_000_000 and vals['Not due'] == 1_000_000, vals


def test_demo_fixture_links_customers_and_margins_agree_on_first_read(fresh, api):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    m = ok(api.call('GET', '/finance/margins?scope=all', token=ceo))
    first = {r['project_id']: r['projected_margin'] for r in m['rows']}
    for pid, cid in (('p_mgs', 'cl_mgs'), ('p_hrms', 'cl_mmg'), ('p_crm', 'cl_dr')):
        p = ok(api.call('GET', f'/projects/{pid}', token=ceo))['project']
        assert p.get('client_id') == cid, (pid, p.get('client_id'))
        money = ok(api.call('GET', f'/projects/{pid}/money', token=ceo))
        assert abs(money['projected']['margin'] - first[pid]) < 1e-12, (pid, money['projected']['margin'], first[pid])
    again = {r['project_id']: r['projected_margin'] for r in ok(api.call('GET', '/finance/margins?scope=all', token=ceo))['rows']}
    assert again == first


def test_approved_hours_one_definition(fresh, api):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    for pid in ('p_mgs', 'p_hrms', 'p_crm'):
        money = ok(api.call('GET', f'/projects/{pid}/money', token=ceo))
        table = sum(p['approved_hours'] for p in money['by_person'])
        engine = money['analysis']['actual_basis']['approved_hours']
        metrics = ok(api.call('GET', f'/projects/{pid}', token=ceo))['metrics']['approved_hours']
        assert abs(table - engine) < 1e-9 and abs(metrics - engine) < 1e-9, (pid, table, engine, metrics)


def test_cost_per_hour_follows_the_location_calendar(fresh, api):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    t = dt.date.today()
    first, last = t.replace(day=1), (t.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    summ = ok(api.call('GET', f'/projects/calendar-summary?from={first.isoformat()}&to={last.isoformat()}', token=ceo))
    hol = {h['date'] for h in summ['holiday_list']}
    day = next(
        (first + dt.timedelta(days=i)).isoformat()
        for i in range((last - first).days + 1)
        if (first + dt.timedelta(days=i)).weekday() < 5 and (first + dt.timedelta(days=i)).isoformat() not in hol
    )
    before = {p['user_id']: p for p in ok(api.call('GET', '/costing/rates', token=ceo))['people']}
    base = ok(api.call('GET', '/holiday-calendars', token=ceo))['default_id']
    cal = ok(api.call('POST', '/holiday-calendars', {'name': 'Pune V9-FIX', 'copy_from_id': base}, token=ceo))  # company holidays + one more
    ok(api.call('PUT', f'/holiday-calendars/{cal["id"]}/locations', {'location_ids': ['loc_seereon_pune']}, token=ceo))  # Jagannath
    ok(api.call('POST', '/holidays', {'date': day, 'name': 'Pune local holiday', 'calendar_id': cal['id'], 'optional': False}, token=ceo))
    after = {p['user_id']: p for p in ok(api.call('GET', '/costing/rates', token=ceo))['people']}
    wd = summ['working_days']
    j = after['u_jagannath']
    assert j['cost_per_day_paise'] == round(j['monthly_ctc_paise'] / (wd - 1)), (j, wd)
    assert after['u_hritesh'] == before['u_hritesh']  # Bhubaneswar: company calendar, unchanged


def test_scope_labels_and_gantt_marker_links(fresh, api):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    ins = ok(api.call('GET', '/insight-views/margins', token=ceo))
    assert any(k['label'] == 'Portfolio projected margin · Live incl. on hold' for k in ins['kpis'])
    rep = ok(api.call('POST', '/report-types/project_margins/run', {'period': {'preset': 'all'}}, token=ceo))
    assert any(k['label'].endswith('· All statuses') for k in rep['kpis'])
    port = ok(api.call('GET', '/insight-views/portfolio', token=ceo))
    g = next(c for c in port['charts'] if c['kind'] == 'gantt')
    mk = next(m for r in g['rows'] for m in r['markers'])
    assert mk['link'].startswith('/projects/') and '/wbs?milestone=' in mk['link']
