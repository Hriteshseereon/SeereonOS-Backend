"""v9 integrated regression (QA9-B): the five v9 slices feed the same numbers to every screen — report types vs the
costing tab, People › Team overview vs the timesheet, per-location holiday calendars, change requests in the inbox."""

from __future__ import annotations

import datetime as dt
from typing import Any


def ok(r: Any) -> Any:
    assert r.status_code == 200, r.text
    return r.json()['data']


def _last_monday() -> str:
    t = dt.date.today()
    return (t - dt.timedelta(days=t.weekday() + 7)).isoformat()


def test_report_planned_margin_matches_costing_tab(fresh, api):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    rows = ok(api.call('POST', '/report-types/project_margins/run', {'period': {'preset': 'all'}}, token=ceo))['rows']
    for pid in ('p_mgs', 'p_hrms', 'p_crm'):
        money = ok(api.call('GET', f'/projects/{pid}/money', token=ceo))
        code = ok(api.call('GET', f'/projects/{pid}', token=ceo))['project']['code']
        row = next(r for r in rows if r['code'] == code)
        assert abs(row['planned_margin'] - money['planned']['margin']) < 1e-9, (pid, row['planned_margin'], money['planned']['margin'])
        assert abs(row['projected_margin'] - money['projected']['margin']) < 1e-9


def test_team_overview_agrees_with_timesheets(fresh, api):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    wk = _last_monday()
    team = ok(
        api.call('POST', '/teams', {'name': 'QA9 interplay', 'lead_id': 'u_hritesh', 'member_ids': ['u_hritesh', 'u_jayashree', 'u_pragyasmita']}, token=ceo)
    )
    ov = ok(api.call('GET', f'/teams/overview?week={wk}', token=ceo))
    g = next(x for x in ov['groups'] if x['id'] == team['id'])
    rep = ok(
        api.call(
            'POST',
            '/report-types/hours/run',
            {
                'period': {'from': wk, 'to': (dt.date.fromisoformat(wk) + dt.timedelta(days=6)).isoformat()},
                'filters': {'team': [team['id']]},
                'group_by': 'person',
                'fields': ['person', 'hours'],
            },
            token=ceo,
        )
    )
    by_rep = {r['person']: r['sum_hours'] for r in rep['rows']}
    comp = ok(
        api.call(
            'POST',
            '/report-types/timesheet_compliance/run',
            {'period': {'from': wk, 'to': wk}, 'filters': {'team': [team['id']]}, 'fields': ['person', 'target_hours']},
            token=ceo,
        )
    )
    by_target = {r['person']: r['target_hours'] for r in comp['rows']}
    for m in g['members']:
        ts = ok(api.call('GET', f'/timesheets/{wk}', token=api.as_(m['email'])))
        assert m['week']['hours'] == ts['total'] == by_rep.get(m['name'], 0), (m['name'], m['week']['hours'], ts['total'], by_rep.get(m['name']))
        assert m['week']['target'] == ts['target'] == by_target[m['name']], (m['name'], m['week']['target'], ts['target'], by_target[m['name']])
    assert g['totals']['hours'] == sum(m['week']['hours'] for m in g['members'])


def test_location_calendar_reaches_team_reports_and_insights(fresh, api):  # type: ignore[no-untyped-def]
    ceo = api.as_('santosh@seereon.co.in')
    wk = _last_monday()
    wed = (dt.date.fromisoformat(wk) + dt.timedelta(days=2)).isoformat()
    cal = ok(api.call('POST', '/holiday-calendars', {'name': 'Pune QA9'}, token=ceo))
    ok(api.call('PUT', f'/holiday-calendars/{cal["id"]}/locations', {'location_ids': ['loc_seereon_pune']}, token=ceo))  # Jagannath works from Pune
    ok(api.call('POST', '/holidays', {'date': wed, 'name': 'Pune local holiday', 'calendar_id': cal['id'], 'optional': False}, token=ceo))
    ts = ok(api.call('GET', f'/timesheets/{wk}', token=api.as_('jagannath@seereon.co.in')))
    ov = ok(api.call('GET', f'/teams/overview?week={wk}', token=ceo))
    mine = next(m for g in ov['groups'] for m in g['members'] if m['id'] == 'u_jagannath')
    assert mine['week']['target'] == ts['target']
    assert next(d for d in mine['week']['days'] if d['date'] == wed)['working'] is False
    comp = ok(api.call('POST', '/report-types/timesheet_compliance/run', {'period': {'from': wk, 'to': wk}, 'fields': ['person', 'target_hours']}, token=ceo))
    assert next(r for r in comp['rows'] if r['person'].startswith('Jagannath'))['target_hours'] == ts['target']
    util = ok(api.call('GET', '/insight-views/utilisation', token=ceo))['charts'][0]
    row = next(r for r in util['rows'] if r['id'] == 'u_jagannath')
    cell = next(c for c, col in zip(row['cells'], util['cols']) if col['key'] == wk)
    assert f'of {round(ts["target"], 1)}h capacity' in cell['note'], cell


def test_change_request_sent_to_a_decider_says_so_in_the_inbox(fresh, api):  # type: ignore[no-untyped-def]
    pm = api.as_('subhankar@seereon.co.in')
    cr = ok(
        api.call(
            'POST',
            '/projects/p_mgs/change-requests',
            {'title': 'QA9 MIS pack', 'hours': 16, 'value_paise': 4000000, 'requested_by_client': 'Principal'},
            token=pm,
        )
    )
    ok(api.call('POST', f'/change-requests/{cr["id"]}/send', {'user_ids': ['u_santosh', 'u_hritesh'], 'note': 'please review'}, token=pm))
    ceo_items = ok(api.call('GET', '/inbox', token=api.as_('santosh@seereon.co.in')))
    item = next(i for i in (ceo_items['items'] if isinstance(ceo_items, dict) else ceo_items) if i['kind'] == 'change_request' and i['entity_id'] == cr['id'])
    assert any(f['label'] == 'Sent to you' and 'please review' in f['value'] for f in item['facts'])
    emp = api.as_('hritesh@seereon.co.in')
    emp_items = ok(api.call('GET', '/inbox', token=emp))
    assert not any(i.get('entity_id') == cr['id'] for i in (emp_items['items'] if isinstance(emp_items, dict) else emp_items))  # FYI only
    assert any(x['id'] == cr['id'] for x in ok(api.call('GET', '/change-requests/sent-to-me', token=emp))['items'])
