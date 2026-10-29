"""v9 Insights with visuals (CEO request 13): every insight view answers ONE specific question and carries the chart
that answers it — a project portfolio Gantt, a milestone timeline, a person × week utilisation heatmap, revenue and
collections, margin by project, the pipeline funnel, task throughput — plus a chart for each of the ten v5 views.

GET /insight-views            the catalogue for the viewer (group, label, question, count), in display order
GET /insight-views/:view      {view, title, question, finding, explanation, kpis, charts[], columns, rows, sections?…}

The v5 views (/insights/:view, Feature Guide §15) are unchanged and still served by app/routes/dashboards.py; this
module wraps them (same rows, same permissions) and adds `question`, `finding` and `charts`. Every number is computed
here; the frontend only draws. Chart titles state the finding; subtitles say what is plotted and in which unit.

Chart contract (snake_case JSON):
  cartesian  {id, kind: 'column'|'bar'|'line', title, subtitle, format, x_label, y_label, categories[], links[]?,
              series[{key, label, values[], tone?}], stacked?, refs[{value, label}]?, empty?}
  gantt      {id, kind: 'gantt', mode: 'bars'|'milestones'|'forecast', title, subtitle, from, to, today,
              rows[{id, label, sub, link, start, end, forecast?, range?{from,to}, progress?, status, status_label,
                    markers[{date, label, state, state_label, baseline?}], detail[]}], legend[{key, label}]}
  heatmap    {id, kind: 'heatmap', title, subtitle, format, split, cols[{key, label, sub, future}],
              rows[{id, label, sub, link?, cells[{v, note}]}], thresholds{over, under}}
  funnel     {id, kind: 'funnel', title, subtitle, format, stages[{key, label, count, value, conversion}]}
"""

from __future__ import annotations

from typing import Any

from app.ctx import Ctx, Req, can_see_margins, err, has, rows
from app.domain.calendar import CalendarCtx, add_days, add_months, diff_days, is_working_day, month_of, month_range, week_start
from app.domain.labels import DEAL_STAGE
from app.domain.money import fmt_money_compact
from app.domain.sales import deal_value
from app.engine.analytics import allocation_on, costed_entries, milestone_state, pipeline, project_metrics, revenue_between, schedule_of
from app.engine.holiday_cal import holidays_for_user
from app.engine.performance import task_timeliness
from app.js import js_round, js_truthy, prop
from app.routes import dashboards as da

# ------------------------------------------------------------------ catalogue
GROUPS = [
    ('prediction', 'Looking ahead'),
    ('delivery', 'Delivery'),
    ('people', 'People'),
    ('finance', 'Finance'),
    ('sales', 'Sales'),
]

# id -> (label, group, question); order = display order inside a group
VIEWS: dict[str, tuple[str, str, str]] = {
    'predictions': ('Delivery predictions', 'prediction', 'Will each project and milestone land on its date?'),
    'estimation': ('Estimation accuracy', 'prediction', 'How far off are our estimates, and by whom?'),
    'portfolio': ('Project timeline', 'delivery', 'Which projects will finish after their end date?'),
    'milestone_timeline': ('Milestone timeline', 'delivery', 'Which milestones have slipped, and what is due next?'),
    'throughput': ('Task throughput', 'delivery', 'Are we closing work on time, week by week?'),
    'tasks_at_risk': ('Tasks at risk', 'delivery', 'What is due in the next 3 days and not started?'),
    'blocked': ('Blocked', 'delivery', 'What is blocked, and for how long?'),
    'overdue_milestones': ('Overdue milestones', 'delivery', 'Which milestones are past their deadline?'),
    'effort_ratio': ('Effort overruns', 'delivery', 'Which tasks are running over their estimate?'),
    'utilisation': ('Utilisation heatmap', 'people', 'Who is over- or under-used, week by week?'),
    'allocation': ('Allocation', 'people', 'Who is over- or under-allocated for the next two weeks?'),
    'timesheet_compliance': ('Timesheet compliance', 'people', 'Are timesheets coming in every week?'),
    'approval_kpi': ('Approval KPI', 'people', 'Which managers approve late?'),
    'revenue': ('Revenue & collections', 'finance', 'Is cash coming in as fast as we bill?'),
    'margins': ('Project margins', 'finance', 'Which projects are eroding margin?'),
    'funnel': ('Pipeline funnel', 'sales', 'Where does the pipeline value sit, and how much converts?'),
    'stale_deals': ('Stale deals', 'sales', 'Which deals have gone quiet?'),
}
LEGACY = set(da.LABELS)


def _new_views(ctx: Ctx) -> list[str]:
    delivery = da.can_delivery_insights(ctx) or has(ctx, 'projects.view_all')
    people = da.can_delivery_insights(ctx) or has(ctx, 'people.view_all')
    out: list[str] = []
    if delivery:
        out += ['portfolio', 'milestone_timeline', 'throughput']
    if people:
        out.append('utilisation')
    if has(ctx, 'finance.view'):
        out.append('revenue')
    if can_see_margins(ctx) and (delivery or has(ctx, 'finance.view')):
        out.append('margins')
    if has(ctx, 'deals.edit_own'):
        out.append('funnel')
    return out


def allowed_views(ctx: Ctx) -> list[str]:
    s = set(da.views_for(ctx)) | set(_new_views(ctx))
    return [v for v in VIEWS if v in s]


def catalogue(ctx: Ctx) -> dict[str, Any]:
    if not has(ctx, 'insights.view'):
        err(403, 'FORBIDDEN', 'Insights aren’t part of your role.')
    counts = {t['id']: t['count'] for t in da.insight_tabs(ctx)}
    ids = allowed_views(ctx)
    views = [{'id': v, 'label': VIEWS[v][0], 'group': VIEWS[v][1], 'question': VIEWS[v][2], 'count': counts.get(v)} for v in ids]
    groups = [{'id': g, 'label': lab} for g, lab in GROUPS if any(x['group'] == g for x in views)]
    default = 'predictions' if 'predictions' in ids else ids[0] if ids else None
    return {'groups': groups, 'views': views, 'default': default}


# ------------------------------------------------------------------ helpers
def _pct(n: Any, d: Any) -> Any:
    return n / d if d else None


def _p0(v: Any) -> str:
    return '—' if v is None else f'{js_round(v * 100)}%'


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f'{n} {one if n == 1 else (many or one + "s")}'


def _short(s: Any, n: int = 34) -> str:
    s = str(s or '')
    return s if len(s) <= n else s[: n - 1] + '…'


def _cart(id_: str, kind: str, title: str, subtitle: str, fmt: str, categories: list, series: list[dict], **kw: Any) -> dict[str, Any]:
    return {'id': id_, 'kind': kind, 'title': title, 'subtitle': subtitle, 'format': fmt, 'categories': categories, 'series': series, **kw}


def _ms_state_label(state: str) -> tuple[str, str]:
    """milestone_state → (gantt state, label)"""
    return {
        'achieved_on_time': ('done', 'Achieved on time'),
        'achieved_late': ('done_late', 'Achieved late'),
        'overdue': ('late', 'Overdue'),
        'at_risk': ('at_risk', 'Forecast late'),
        'not_started': ('not_started', 'Not started'),
        'on_track': ('on_track', 'On track'),
    }.get(state, ('on_track', 'On track'))


STATUS_LABEL = {'on_track': 'On track', 'at_risk': 'At risk', 'late': 'Forecast late', 'done': 'Completed', 'not_started': 'Not started', 'on_hold': 'On hold'}
GANTT_LEGEND = [
    {'key': 'on_track', 'label': 'On track'},
    {'key': 'at_risk', 'label': 'At risk'},
    {'key': 'late', 'label': 'Late / forecast late'},
    {'key': 'done', 'label': 'Achieved'},
]


def _delivery_projects(ctx: Ctx) -> list[dict]:
    return [p for p in da.scope_projects(ctx) if not p['id'].startswith('p_general_')]


# ------------------------------------------------------------------ new views
def _portfolio(ctx: Ctx) -> dict[str, Any]:
    t = ctx.today
    out_rows = []
    table = []
    for p in sorted(_delivery_projects(ctx), key=lambda x: (x['end_on'], x['name'])):
        sch = schedule_of(ctx, p['id'])['schedule'] or {}
        m = project_metrics(ctx, p)
        forecast = sch.get('forecast_finish')
        late_by = diff_days(forecast, p['end_on']) if forecast else 0
        if p['status'] == 'on_hold':
            status = 'on_hold'
        elif (forecast and forecast > p['end_on']) or (p['end_on'] < t):
            status = 'late'
        elif m.get('health') in ('at_risk', 'off_track'):
            status = 'at_risk'
        elif p['start_on'] > t:
            status = 'not_started'
        else:
            status = 'on_track'
        markers = []
        for ms in sch.get('milestones', []):
            st, lab = _ms_state_label(ms['state'])
            markers.append(
                {
                    'date': ms['planned_end'],
                    'label': f'M{ms["no"]} {ms["name"]}',
                    'state': st,
                    'state_label': lab,
                    'baseline': ms['baseline_end'] if ms.get('baseline_end') and ms['baseline_end'] != ms['planned_end'] else None,
                    'forecast': ms.get('forecast_end'),
                    **_ms_link(p['id'], ms['id']),
                }
            )
        detail = [f'Planned {p["start_on"]} → {p["end_on"]}', f'{_p0(m.get("progress"))} complete']
        if forecast:
            detail.append(f'Forecast finish {forecast}' + (f' ({late_by} days late)' if late_by > 0 else ''))
        out_rows.append(
            {
                'id': p['id'],
                'label': p['name'],
                'sub': f'{p.get("code") or ""} · {p.get("client") or ""}'.strip(' ·'),
                'link': f'/projects/{p["id"]}',
                'start': p['start_on'],
                'end': p['end_on'],
                'forecast': forecast,
                'progress': m.get('progress'),
                'status': status,
                'status_label': STATUS_LABEL[status],
                'markers': markers,
                'detail': detail,
            }
        )
        table.append(
            {
                'id': p['id'],
                'project': p['name'],
                'project_id': p['id'],
                'project_sub': p.get('client'),
                'start': p['start_on'],
                'end': p['end_on'],
                'forecast': forecast,
                'slip': late_by if forecast and late_by > 0 else None,
                'progress': m.get('progress'),
                'status': STATUS_LABEL[status],
                'status_tone': {'late': 'danger', 'at_risk': 'warning', 'on_track': 'success', 'on_hold': 'neutral', 'not_started': 'neutral'}[status],
            }
        )
    n = len(out_rows)
    late = [r for r in table if r['status'] == STATUS_LABEL['late']]
    risk = [r for r in table if r['status'] == STATUS_LABEL['at_risk']]
    if not n:
        finding = 'No live projects in your scope'
    elif late:
        worst = max(late, key=lambda r: r['slip'] or 0)
        finding = f'{len(late)} of {n} projects are forecast to finish after their end date'
        if worst['slip']:
            finding += f' — {_short(worst["project"], 40)} by {_plural(worst["slip"], "day")}'
    else:
        finding = f'All {n} live projects are forecast to finish by their end date'
    frm = min([r['start'] for r in out_rows] + [add_days(t, -30)]) if out_rows else add_days(t, -30)
    to = max([max(r['end'], r['forecast'] or r['end']) for r in out_rows] + [add_days(t, 60)]) if out_rows else add_days(t, 60)
    frm = max(frm, add_days(t, -365))
    due30 = sum(1 for r in out_rows for mk in r['markers'] if mk['state'] not in ('done', 'done_late') and t <= mk['date'] <= add_days(t, 30))
    slips = [r['slip'] for r in late if r['slip']]
    return {
        'title': 'Project timeline',
        'finding': finding,
        'explanation': 'Every live project as a bar from its start to its planned end, with the schedule’s dependency-adjusted forecast finish and its milestones. Red means the forecast finish (or today) is past the planned end; amber means the project health is at risk.',
        'kpis': [
            {'label': 'Live projects', 'value': n, 'format': 'number'},
            {'label': 'Forecast late', 'value': len(late), 'format': 'number', 'tone': 'danger' if late else 'success'},
            {'label': 'At risk', 'value': len(risk), 'format': 'number', 'tone': 'warning' if risk else None},
            {'label': 'Milestones due in 30 days', 'value': due30, 'format': 'number'},
        ],
        'charts': [
            {
                'id': 'gantt',
                'kind': 'gantt',
                'mode': 'bars',
                'title': finding,
                'subtitle': 'Planned span per project; the dark segment is work done, the tick is the forecast finish, diamonds are milestones.',
                'from': frm,
                'to': add_days(to, 7),
                'today': t,
                'rows': out_rows,
                'legend': GANTT_LEGEND,
                'empty': 'No live projects in your scope.',
            }
        ],
        'columns': [
            {'key': 'project', 'label': 'Project', 'type': 'project'},
            {'key': 'end', 'label': 'Planned end', 'type': 'date', 'align': 'right'},
            {'key': 'forecast', 'label': 'Forecast finish', 'type': 'date', 'align': 'right'},
            {'key': 'slip', 'label': 'Late by (days)', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'progress', 'label': 'Done', 'type': 'percent', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'status', 'label': 'Status', 'type': 'badge'},
        ],
        'rows': table,
        'empty': 'No live projects in your scope.',
        'ask': 'Which projects will finish after their planned end date?',
        'avg_slip': js_round(sum(slips) / len(slips)) if slips else None,
    }


def _milestone_timeline(ctx: Ctx) -> dict[str, Any]:
    t = ctx.today
    money = has(ctx, 'finance.view') or can_see_margins(ctx)
    tasks = rows(ctx, 'tasks')
    g_rows = []
    table = []
    frm = add_days(t, -90)
    to = add_days(t, 150)
    for p in _delivery_projects(ctx):
        mss = sorted([m for m in rows(ctx, 'milestones') if m['project_id'] == p['id'] and not js_truthy(m.get('deleted_at'))], key=lambda m: m['no'])
        if not mss:
            continue
        markers = []
        for m in mss:
            stt = milestone_state(ctx, m, tasks)
            st, lab = _ms_state_label(stt['state'])
            is_open = st not in ('done', 'done_late')
            if is_open and st in ('on_track', 'not_started') and m['planned_end'] <= add_days(t, 14):
                st, lab = 'due_soon', 'Due in 14 days'
            slip = diff_days(m['planned_end'], m['baseline_end']) if m.get('baseline_end') else 0
            markers.append(
                {
                    'date': m['planned_end'],
                    'label': f'M{m["no"]} {m["name"]}',
                    'state': st,
                    'state_label': lab,
                    'baseline': m['baseline_end'] if slip else None,
                    'forecast': stt.get('forecast'),
                    **_ms_link(p['id'], m['id']),
                }
            )
            table.append(
                {
                    'id': m['id'],
                    'milestone': f'M{m["no"]} {m["name"]}',
                    'project': p['name'],
                    'project_id': p['id'],
                    'baseline': m.get('baseline_end'),
                    'planned_end': m['planned_end'],
                    'slip': slip if slip > 0 else None,
                    'forecast': stt.get('forecast'),
                    'state': lab,
                    'state_tone': {'done': 'success', 'done_late': 'neutral', 'late': 'danger', 'at_risk': 'warning', 'due_soon': 'info'}.get(st, 'neutral'),
                    'billing': m['billing_amount_paise'] if money else None,
                    '_open': is_open,
                    '_st': st,
                }
            )
        g_rows.append(
            {
                'id': p['id'],
                'label': p['name'],
                'sub': p.get('client') or '',
                'link': f'/projects/{p["id"]}',
                'start': p['start_on'],
                'end': p['end_on'],
                'status': 'on_track',
                'status_label': '',
                'markers': markers,
                'detail': [],
            }
        )
    open_ms = [r for r in table if r['_open']]
    slipped = [r for r in table if (r['slip'] or 0) > 0]
    overdue = [r for r in open_ms if r['_st'] == 'late']
    upcoming = sorted([r for r in open_ms if t <= r['planned_end'] <= add_days(t, 30)], key=lambda r: r['planned_end'])
    billing_30 = sum((r['billing'] or 0) for r in upcoming)
    if not table:
        finding = 'No milestones in your projects yet'
    elif slipped:
        finding = f'{_plural(len(slipped), "milestone")} slipped {_plural(sum(r["slip"] for r in slipped), "day")} in total against baseline'
        if overdue:
            finding += f'; {len(overdue)} {"is" if len(overdue) == 1 else "are"} overdue'
    elif overdue:
        finding = f'{_plural(len(overdue), "milestone")} past {"its" if len(overdue) == 1 else "their"} deadline, none slipped against baseline'
    else:
        finding = f'No milestone has slipped against its baseline; {len(upcoming)} due in the next 30 days'
    for r in table:
        r.pop('_open', None)
        r.pop('_st', None)
    table.sort(key=lambda r: r['planned_end'])
    return {
        'title': 'Milestone timeline',
        'finding': finding,
        'explanation': 'Each milestone at its planned end date, one row per project. A hollow diamond joined by a line is the original baseline date — the longer the line, the more it slipped. Colour shows the delivery state.',
        'kpis': [
            {'label': 'Open milestones', 'value': len(open_ms), 'format': 'number'},
            {'label': 'Overdue', 'value': len(overdue), 'format': 'number', 'tone': 'danger' if overdue else 'success'},
            {'label': 'Slipped vs baseline', 'value': len(slipped), 'format': 'number', 'tone': 'warning' if slipped else None},
            *(
                [{'label': 'Billing due in 30 days', 'value': billing_30, 'format': 'money'}]
                if money
                else [{'label': 'Due in 30 days', 'value': len(upcoming), 'format': 'number'}]
            ),
        ],
        'charts': [
            {
                'id': 'timeline',
                'kind': 'gantt',
                'mode': 'milestones',
                'title': finding,
                'subtitle': 'Milestones by planned end date (last 90 days to the next 5 months); hollow diamonds are baseline dates.',
                'from': frm,
                'to': to,
                'today': t,
                'rows': g_rows,
                'legend': [
                    {'key': 'on_track', 'label': 'On track'},
                    {'key': 'due_soon', 'label': 'Due in 14 days'},
                    {'key': 'at_risk', 'label': 'Forecast late'},
                    {'key': 'late', 'label': 'Overdue'},
                    {'key': 'done', 'label': 'Achieved'},
                ],
                'empty': 'No milestones in your projects yet.',
            }
        ],
        'columns': [
            {'key': 'milestone', 'label': 'Milestone', 'type': 'string'},
            {'key': 'project', 'label': 'Project', 'type': 'project', 'hideBelow': 'md'},
            {'key': 'baseline', 'label': 'Baseline', 'type': 'date', 'align': 'right', 'hideBelow': 'lg'},
            {'key': 'planned_end', 'label': 'Planned end', 'type': 'date', 'align': 'right'},
            {'key': 'slip', 'label': 'Slipped (days)', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'state', 'label': 'State', 'type': 'badge'},
            *([{'key': 'billing', 'label': 'Billing', 'type': 'money', 'align': 'right', 'hideBelow': 'lg'}] if money else []),
        ],
        'rows': table,
        'empty': 'No milestones in your projects yet.',
        'ask': 'Which milestones have slipped against their baseline?',
    }


def _throughput(ctx: Ctx) -> dict[str, Any]:
    t = ctx.today
    pids = {p['id'] for p in da.scope_projects(ctx)}
    cur = week_start(t)
    weeks = [add_days(cur, -7 * (11 - i)) for i in range(12)]
    idx = {w: i for i, w in enumerate(weeks)}
    on_time = [0] * 12
    late = [0] * 12
    opened = [0] * 12
    tasks = [x for x in rows(ctx, 'tasks') if x['project_id'] in pids]
    for x in tasks:
        c = (x.get('created_at') or '')[:10]
        if c:
            i = idx.get(week_start(c))
            if i is not None:
                opened[i] += 1
        if x['status'] == 'done' and js_truthy(x.get('done_on')):
            i = idx.get(week_start(x['done_on']))
            if i is None:
                continue
            tl = task_timeliness(ctx, x)
            if tl and tl['timeliness'] == 'late':
                late[i] += 1
            else:
                on_time[i] += 1
    rate = [_pct(on_time[i], on_time[i] + late[i]) for i in range(12)]
    labels = [f'{w[8:10]}/{w[5:7]}' for w in weeks]

    def last(a: list, n: int, off: int = 0) -> int:
        return sum(a[12 - n - off : 12 - off])

    c4 = last(on_time, 4) + last(late, 4)
    r4 = _pct(last(on_time, 4), c4)
    p4 = last(on_time, 4, 4) + last(late, 4, 4)
    rp = _pct(last(on_time, 4, 4), p4)
    open_now = sum(1 for x in tasks if x['status'] != 'done')
    if c4:
        finding = f'Closed {_plural(c4, "task")} in the last 4 weeks, {_p0(r4)} on time'
        if rp is not None:
            finding += f' ({_p0(rp)} the 4 weeks before)'
    else:
        finding = 'No tasks closed in the last 4 weeks'
    o4 = last(opened, 4)
    table = [
        {
            'id': weeks[i],
            'week': labels[i],
            'week_sub': f'from {weeks[i]}',
            'opened': opened[i],
            'closed': on_time[i] + late[i],
            'on_time': on_time[i],
            'late': late[i],
            'rate': rate[i],
        }
        for i in range(11, -1, -1)
    ]
    return {
        'title': 'Task throughput',
        'finding': finding,
        'explanation': 'Tasks closed each week in your projects, split by whether they finished by their target end (the same timeliness rule as the scorecard), and the on-time rate. The current week is still in progress.',
        'kpis': [
            {'label': 'Closed, last 4 weeks', 'value': c4, 'format': 'number'},
            {
                'label': 'On time, last 4 weeks',
                'value': r4,
                'format': 'percent',
                'tone': None if r4 is None else 'success' if r4 >= 0.8 else 'warning' if r4 >= 0.6 else 'danger',
            },
            {'label': 'Opened, last 4 weeks', 'value': o4, 'format': 'number', 'hint': 'Tasks created in your projects in the last 4 weeks'},
            {'label': 'Open now', 'value': open_now, 'format': 'number'},
        ],
        'charts': [
            _cart(
                'closed',
                'column',
                finding,
                'Tasks closed per week (count), stacked by on time and late.',
                'number',
                labels,
                [{'key': 'on_time', 'label': 'On time', 'values': on_time}, {'key': 'late', 'label': 'Late', 'values': late}],
                stacked=True,
                x_label='Week',
                y_label='Tasks closed',
                empty=None if any(on_time) or any(late) else 'No tasks closed in the last 12 weeks.',
            ),
            _cart(
                'rate',
                'line',
                f'On-time rate {"is " + _p0(r4) if r4 is not None else "has no closed tasks"} over the last 4 weeks',
                'Share of tasks closed on time each week (%); weeks with nothing closed are gaps. The line marks the 80% target.',
                'percent',
                labels,
                [{'key': 'rate', 'label': 'On-time rate', 'values': rate}],
                refs=[{'value': 0.8, 'label': '80% target'}],
                x_label='Week',
                y_label='On time',
                empty=None if any(v is not None for v in rate) else 'No tasks closed in the last 12 weeks.',
            ),
        ],
        'columns': [
            {'key': 'week', 'label': 'Week', 'type': 'string'},
            {'key': 'opened', 'label': 'Opened', 'type': 'number', 'align': 'right'},
            {'key': 'closed', 'label': 'Closed', 'type': 'number', 'align': 'right'},
            {'key': 'on_time', 'label': 'On time', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'late', 'label': 'Late', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'rate', 'label': 'On-time rate', 'type': 'percent', 'align': 'right'},
        ],
        'rows': table,
        'empty': 'No tasks in your projects yet.',
        'ask': 'How many tasks did we close on time each week?',
    }


def _utilisation(ctx: Ctx) -> dict[str, Any]:
    t = ctx.today
    min_h = prop(ctx.tenant['settings'], 'calendar.min_hours_per_day') or 8
    people = [u for u in da.scope_people(ctx) if u.get('role') in ('employee', 'pm')]
    cur = week_start(t)
    past = [add_days(cur, -7 * (8 - i)) for i in range(8)]
    future = [add_days(cur, 7 * i) for i in range(4)]
    weeks = past + future
    days_of = {w: [add_days(w, i) for i in range(7)] for w in weeks}
    # each person's own holiday calendar (their location's, v9 A5) — the working days their timesheet expects
    pcal = {u['id']: CalendarCtx(ctx.tenant.get('settings'), holidays_for_user(ctx.tenant_id, u['id'])) for u in people}
    # ... and only from the day they joined: weeks before that were never their capacity (a new company, a new joiner)
    joined_of = {u['id']: str(u.get('joined_on') or '') for u in people}
    wdays_of = {uid: {w: [d for d in days_of[w] if is_working_day(d, uc) and d >= joined_of[uid]] for w in weeks} for uid, uc in pcal.items()}
    ids = {u['id'] for u in people}
    hours: dict[tuple, float] = {}
    first = past[0]
    for e in costed_entries(ctx):
        if e['user_id'] not in ids or e.get('kind') == 'leave' or not (first <= e['work_date'] < cur):
            continue
        k = (e['user_id'], week_start(e['work_date']))
        hours[k] = hours.get(k, 0) + e['hours']
    leave: dict[tuple, float] = {}
    for lv in rows(ctx, 'leave'):
        if lv['user_id'] in ids and first <= lv['date'] < add_days(cur, 28) and is_working_day(lv['date'], pcal[lv['user_id']]):
            k = (lv['user_id'], week_start(lv['date']))
            leave[k] = leave.get(k, 0) + (1 if lv['portion'] == 'full' else 0.5)
    out_rows = []
    table = []
    for u in sorted(people, key=lambda x: x['name']):
        cells = []
        wdays = wdays_of[u['id']]
        for w in past:
            wd = len(wdays[w]) - leave.get((u['id'], w), 0)
            h = hours.get((u['id'], w), 0)
            if wd <= 0:
                cells.append({'v': None, 'note': 'Before joining' if add_days(w, 6) < joined_of[u['id']] else 'On leave / no working days'})
                continue
            capacity = wd * min_h
            cells.append({'v': round(h / capacity, 3), 'note': f'{round(h, 1)}h logged of {round(capacity, 1)}h capacity'})
        for w in future:
            wd = wdays[w]
            if not wd:
                cells.append({'v': None, 'note': 'No working days'})
                continue
            al = [allocation_on(ctx, u['id'], d) for d in wd]
            v = sum(al) / len(al) / 100
            lv = leave.get((u['id'], w), 0)
            cells.append({'v': round(v, 3), 'note': f'Planned allocation {js_round(v * 100)}%' + (f', {lv:g} leave day(s)' if lv else '')})
        act4 = [x['v'] for x in cells[4:8] if x['v'] is not None]
        plan = [x['v'] for x in cells[8:] if x['v'] is not None]
        a4 = sum(act4) / len(act4) if act4 else None
        p4 = sum(plan) / len(plan) if plan else None
        nxt = cells[9]['v'] if len(cells) > 9 else None
        flag = 'Overloaded' if (nxt or 0) > 1.1 else 'Under-used' if a4 is not None and a4 < 0.6 else 'Healthy'
        out_rows.append({'id': u['id'], 'label': u['name'], 'sub': u.get('title') or '', 'cells': cells})
        table.append(
            {
                'id': u['id'],
                'person': u['name'],
                'person_hue': u.get('avatar_hue', 150),
                'person_sub': u.get('title'),
                'actual4': a4,
                'next_week': nxt,
                'plan4': p4,
                'flag': flag,
                'flag_tone': {'Overloaded': 'danger', 'Under-used': 'warning', 'Healthy': 'success'}[flag],
            }
        )
    over = [r for r in table if r['flag'] == 'Overloaded']
    under = [r for r in table if (r['actual4'] is not None and r['actual4'] < 0.6)]
    vals = [r['actual4'] for r in table if r['actual4'] is not None]
    team4 = sum(vals) / len(vals) if vals else None
    if not table:
        finding = 'No employees or project managers in your scope yet'
    elif over or under:
        parts = []
        if over:
            parts.append(f'{_plural(len(over), "person", "people")} booked over 110% next week')
        if under:
            parts.append(f'{len(under)} averaged under 60% in the last 4 weeks')
        finding = '; '.join(parts)
        finding = finding[0].upper() + finding[1:]
    else:
        finding = f'Everyone sits between 60% and 110% — team average {_p0(team4)} over the last 4 weeks'
    table.sort(key=lambda r: (-(r['next_week'] or 0), r['person']))
    labels = [f'{w[8:10]}/{w[5:7]}' for w in weeks]
    return {
        'title': 'Utilisation heatmap',
        'finding': finding,
        'explanation': f'Past 8 weeks: hours logged ÷ capacity (working days less leave × {min_h:g}h). This week and the next 3: planned allocation from the resource plan. Above 110% is overloaded; under 60% is spare capacity.',
        'kpis': [
            {'label': 'Team utilisation, last 4 weeks', 'value': team4, 'format': 'percent'},
            {'label': 'Over 110% next week', 'value': len(over), 'format': 'number', 'tone': 'danger' if over else 'success'},
            {'label': 'Under 60%, last 4 weeks', 'value': len(under), 'format': 'number', 'tone': 'warning' if under else None},
            {'label': 'People', 'value': len(table), 'format': 'number'},
        ],
        'charts': [
            {
                'id': 'heatmap',
                'kind': 'heatmap',
                'title': finding,
                'subtitle': 'Utilisation per person and week (%). Columns after the divider are planned allocation, before it actual hours.',
                'format': 'percent',
                'split': len(past),
                'cols': [{'key': w, 'label': labels[i], 'sub': 'Plan' if w >= cur else 'Actual', 'future': w >= cur} for i, w in enumerate(weeks)],
                'rows': out_rows,
                'thresholds': {'over': 1.1, 'under': 0.6},
                'empty': 'No people in your scope.',
            }
        ],
        'columns': [
            {'key': 'person', 'label': 'Person', 'type': 'person'},
            {'key': 'actual4', 'label': 'Actual, last 4 wks', 'type': 'percent', 'align': 'right'},
            {'key': 'next_week', 'label': 'Planned next week', 'type': 'percent', 'align': 'right'},
            {'key': 'plan4', 'label': 'Planned, next 4 wks', 'type': 'percent', 'align': 'right', 'hideBelow': 'xl'},
            {'key': 'flag', 'label': 'Flag', 'type': 'badge'},
        ],
        'rows': table,
        'empty': 'No people in your scope.',
        'ask': 'Who is over- or under-utilised over the last and next few weeks?',
    }


def _revenue(ctx: Ctx) -> dict[str, Any]:
    t = ctx.today
    months = [month_of(add_months(f'{month_of(t)}-01', -(11 - i))) for i in range(12)]
    billed = []
    collected = []
    for m in months:
        r = month_range(m)
        rv = revenue_between(ctx, r['from'], r['to'])
        billed.append(rv['billed'])
        collected.append(rv['collected'])
    labels = [f'{["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][int(m[5:7]) - 1]} {m[2:4]}' for m in months]
    b6 = sum(billed[6:])
    c6 = sum(collected[6:])
    rate6 = _pct(c6, b6)
    buckets = [('not_due', 'Not due'), ('d30', '1–30 days'), ('d60', '31–60 days'), ('d90', '61–90 days'), ('d90p', 'Over 90 days')]
    ageing = dict.fromkeys([k for k, _ in buckets], 0)
    for inv in rows(ctx, 'invoices'):
        if inv['status'] in ('draft', 'cancelled'):
            continue
        o = inv['amount_paise'] - inv['paid_amount_paise']
        if o <= 0:
            continue
        late = diff_days(t, inv['due_on'])
        k = 'not_due' if late <= 0 else 'd30' if late <= 30 else 'd60' if late <= 60 else 'd90' if late <= 90 else 'd90p'
        ageing[k] += o
    # V9-FIX (item 9): receivables brought forward in the FY opening balances age with the invoices (by their due date,
    # else in the oldest bucket); opening payables are shown beside them
    from app.engine.openings import opening_age_bucket, opening_parties

    ob = opening_parties(ctx)
    open_recv = sum(ln['amount_paise'] for ln in ob['receivables']) if ob else 0
    open_pay = sum(ln['amount_paise'] for ln in ob['payables']) if ob else 0
    for ln in ob['receivables'] if ob else []:
        ageing[opening_age_bucket(ln, t)] += ln['amount_paise']
    outstanding = sum(ageing.values())
    overdue = outstanding - ageing['not_due']
    if b6 or c6:
        finding = f'Collected {fmt_money_compact(c6)} against {fmt_money_compact(b6)} billed in the last 6 months ({_p0(rate6)})'
    else:
        finding = 'Nothing billed or collected in the last 6 months'
    old = ageing['d90'] + ageing['d90p']
    age_title = f'{fmt_money_compact(overdue)} of {fmt_money_compact(outstanding)} outstanding is overdue' if outstanding else 'No outstanding receivables'
    if old:
        age_title += f'; {fmt_money_compact(old)} is over 60 days late'
    table = [
        {
            'id': months[i],
            'month': labels[i],
            'billed': billed[i],
            'collected': collected[i],
            'gap': billed[i] - collected[i],
            'rate': _pct(collected[i], billed[i]),
        }
        for i in range(11, -1, -1)
    ]
    return {
        'title': 'Revenue & collections',
        'finding': finding,
        'explanation': 'Billed = income booked in the month (issued invoices and direct income), collected = payments received in the month — the same figures as the CEO dashboard. Receivables age by days past the invoice due date.'
        + (f' Opening receivables from the {ob["fy_label"]} opening balances are included in the ageing (by their due date, else as over 90 days).' if ob else ''),
        'kpis': [
            {'label': 'Billed, last 6 months', 'value': b6, 'format': 'money'},
            {'label': 'Collected, last 6 months', 'value': c6, 'format': 'money'},
            {
                'label': 'Collection rate',
                'value': rate6,
                'format': 'percent',
                'tone': None if rate6 is None else 'success' if rate6 >= 0.9 else 'warning' if rate6 >= 0.7 else 'danger',
            },
            {'label': 'Overdue receivables', 'value': overdue, 'format': 'money', 'tone': 'danger' if overdue else 'success'},
            *(
                [
                    {'label': f'Opening receivables ({ob["fy_label"]}), in the ageing', 'value': open_recv, 'format': 'money'},
                    {'label': f'Opening payables ({ob["fy_label"]})', 'value': open_pay, 'format': 'money'},
                ]
                if ob
                else []
            ),
        ],
        'charts': [
            _cart(
                'trend',
                'column',
                finding,
                'Billed and collected per month, last 12 months (₹).',
                'money',
                labels,
                [{'key': 'billed', 'label': 'Billed', 'values': billed}, {'key': 'collected', 'label': 'Collected', 'values': collected}],
                x_label='Month',
                y_label='₹',
                empty=None if any(billed) or any(collected) else 'No billing or collections in the last 12 months.',
            ),
            _cart(
                'ageing',
                'bar',
                age_title,
                'Outstanding receivables by days past due (₹).',
                'money',
                [lab for _, lab in buckets],
                [{'key': 'outstanding', 'label': 'Outstanding', 'values': [ageing[k] for k, _ in buckets]}],
                ordinal=True,
                x_label='Days past due',
                y_label='₹',
                links=['/finance/invoices'] * len(buckets),
                empty=None if outstanding else 'Every issued invoice is fully paid.',
            ),
        ],
        'columns': [
            {'key': 'month', 'label': 'Month', 'type': 'string'},
            {'key': 'billed', 'label': 'Billed', 'type': 'money', 'align': 'right'},
            {'key': 'collected', 'label': 'Collected', 'type': 'money', 'align': 'right'},
            {'key': 'gap', 'label': 'Billed − collected', 'type': 'money', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'rate', 'label': 'Collected ÷ billed', 'type': 'percent', 'align': 'right'},
        ],
        'rows': table,
        'empty': 'No billing yet.',
        'ask': 'How much did we bill and collect each month this year?',
    }


MARGIN_SCOPE = 'Live incl. on hold'  # the projects of Insights › Project margins: initiated, active and on hold
MARGIN_SCOPE_NOTE = (
    ' Covers live projects incl. on hold (initiated, active and on hold) with a contract value: Finance › Profit margins'
    ' totals active projects by default and Reports › Project margins all statuses, so their portfolio totals differ;'
    ' each project’s margins are the same everywhere.'
)


def _scoped(label: str) -> str:
    return f'{label} · {MARGIN_SCOPE}'


def _ms_link(pid: str, mid: str) -> dict[str, str]:
    """V9-FIX (item 12): a Gantt milestone marker opens its milestone on the project's WBS tab"""
    return {'link': f'/projects/{pid}/wbs?milestone={mid}'}


def _margins(ctx: Ctx) -> dict[str, Any]:
    floor = prop(ctx.tenant['settings'], 'projects.margin_floor_pct')
    floor = (floor if isinstance(floor, (int, float)) else 25) / 100
    ps = [p for p in da.scope_projects(ctx) if not p['id'].startswith('p_general_') and (p.get('contract_value_paise') or 0) > 0]
    table = []
    for p in ps:
        m = project_metrics(ctx, p)
        pl, pr, ac = m.get('planned_margin'), m.get('projected_margin'), m.get('actual_margin')
        erosion = (pl - pr) if pl is not None and pr is not None else None
        below = pr is not None and pr < floor
        table.append(
            {
                'id': p['id'],
                'project': p['name'],
                'project_id': p['id'],
                'project_sub': p.get('client'),
                'contract': m.get('contract_value'),
                'planned_cost': m.get('planned_cost'),
                'projected_cost': m.get('projected_cost'),
                'actual_cost': m.get('actual_cost'),
                'planned': pl,
                'projected': pr,
                'actual': ac if m.get('invoiced') else None,
                'erosion': erosion,
                'flag': 'Below floor' if below else 'Eroding' if erosion is not None and erosion > 0.05 else 'Healthy',
                'flag_tone': 'danger' if below else 'warning' if erosion is not None and erosion > 0.05 else 'success',
            }
        )
    table.sort(key=lambda r: r['projected'] if r['projected'] is not None else 9)
    below = [r for r in table if r['flag'] == 'Below floor']
    eroded = [r for r in table if r['erosion'] is not None and r['erosion'] > 0]
    tot_c = sum(r['contract'] or 0 for r in table)
    tot_pc = sum(r['projected_cost'] or 0 for r in table)
    port = (tot_c - tot_pc) / tot_c if tot_c else None
    if not table:
        finding = 'No live projects with a contract value'
    elif below:
        finding = f'{len(below)} of {len(table)} projects are projected below the {js_round(floor * 100)}% margin floor'
        worst = max(eroded, key=lambda r: r['erosion']) if eroded else None
        if worst and worst['erosion'] > 0:
            finding += f' — {_short(worst["project"], 36)} lost {js_round(worst["erosion"] * 100)} points'
    elif eroded:
        worst = max(eroded, key=lambda r: r['erosion'])
        finding = (
            f'All projects clear the {js_round(floor * 100)}% floor; {_short(worst["project"], 36)} has eroded most ({js_round(worst["erosion"] * 100)} points)'
        )
    else:
        finding = f'All {len(table)} projects are at or above planned margin'
    top = table[:12]
    return {
        'title': 'Project margins',
        'finding': finding,
        'explanation': 'Planned margin = contract value against the estimate-based cost; projected = against cost logged so far plus remaining estimate; actual = invoiced against approved cost (shown once something is invoiced). The line is the workspace margin floor (Settings → Projects).'
        + MARGIN_SCOPE_NOTE,
        'kpis': [
            {
                # V9-FIX (item 11): portfolio totals name their scope (Finance › Profit margins defaults to active
                # projects, Reports › Project margins to all statuses — per-project numbers are the same everywhere)
                'label': _scoped('Portfolio projected margin'),
                'value': port,
                'format': 'percent',
                'tone': None if port is None else 'success' if port >= floor else 'danger',
            },
            {'label': 'Below floor', 'value': len(below), 'format': 'number', 'tone': 'danger' if below else 'success'},
            {'label': 'Eroding vs plan', 'value': len(eroded), 'format': 'number', 'tone': 'warning' if eroded else None},
            {'label': _scoped('Contract value'), 'value': tot_c, 'format': 'money'},
        ],
        'charts': [
            _cart(
                'margins',
                'bar',
                finding,
                'Margin per project (%): planned, projected and actual. Lowest projected margin first' + ('; top 12 shown.' if len(table) > 12 else '.'),
                'percent',
                [r['project'] for r in top],
                [
                    {'key': 'planned', 'label': 'Planned', 'values': [r['planned'] for r in top]},
                    {'key': 'projected', 'label': 'Projected', 'values': [r['projected'] for r in top]},
                    {'key': 'actual', 'label': 'Actual', 'values': [r['actual'] for r in top]},
                ],
                refs=[{'value': floor, 'label': f'{js_round(floor * 100)}% floor'}],
                links=[f'/projects/{r["id"]}' for r in top],
                x_label='Project',
                y_label='Margin',
                empty=None if table else 'No live projects with a contract value.',
            )
        ],
        'columns': [
            {'key': 'project', 'label': 'Project', 'type': 'project'},
            {'key': 'contract', 'label': 'Contract', 'type': 'money', 'align': 'right', 'hideBelow': 'lg'},
            {'key': 'projected_cost', 'label': 'Projected cost', 'type': 'money', 'align': 'right', 'hideBelow': 'lg'},
            {'key': 'planned', 'label': 'Planned', 'type': 'percent', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'projected', 'label': 'Projected', 'type': 'percent', 'align': 'right'},
            {'key': 'actual', 'label': 'Actual', 'type': 'percent', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'flag', 'label': 'Flag', 'type': 'badge'},
        ],
        'rows': table,
        'empty': 'No live projects with a contract value.',
        'ask': 'Which projects are projected below our margin floor?',
    }


def _funnel(ctx: Ctx) -> dict[str, Any]:
    owner = None if has(ctx, 'deals.view_all') else ctx.user_id
    deals = [d for d in rows(ctx, 'deals') if owner is None or d.get('owner_id') == owner]
    order = sorted([k for k in DEAL_STAGE if k != 'lost'], key=lambda k: DEAL_STAGE[k]['order'])
    count = dict.fromkeys(order, 0)
    value = dict.fromkeys(order, 0)
    for d in deals:
        if d['stage'] in count:
            count[d['stage']] += 1
            value[d['stage']] += deal_value(d)
    # reached = deals now at this stage or any later one (lost deals are left out: their last stage isn't kept)
    reached = {}
    acc = 0
    for k in reversed(order):
        acc += count[k]
        reached[k] = acc
    stages = []
    prev = None
    for k in order:
        stages.append(
            {
                'key': k,
                'label': DEAL_STAGE[k]['label'],
                'count': count[k],
                'value': value[k],
                'reached': reached[k],
                'conversion': _pct(reached[k], prev) if prev is not None else None,
            }
        )
        prev = reached[k]
    pl = pipeline(ctx, owner if owner else None)
    open_stages = [s for s in stages if s['key'] != 'won']
    big = max(open_stages, key=lambda s: s['value']) if open_stages and any(s['value'] for s in open_stages) else None
    if pl['open_count']:
        finding = f'{fmt_money_compact(pl["open_value"])} open across {_plural(pl["open_count"], "deal")}'
        if big:
            finding += f'; the most value sits in {big["label"]} ({fmt_money_compact(big["value"])})'
    else:
        finding = 'No open deals in the pipeline'
    weak = min([s for s in stages if s['conversion'] is not None], key=lambda s: s['conversion'], default=None)
    return {
        'title': 'Pipeline funnel',
        'finding': finding,
        'explanation': 'Deals by their current stage, with the value they carry. “Reached” counts deals at that stage or beyond (won included); conversion is the share of the previous stage that got this far. Lost deals are not in the funnel.',
        'kpis': [
            {'label': 'Open pipeline', 'value': pl['open_value'], 'format': 'money'},
            {'label': 'Weighted', 'value': pl['weighted_value'], 'format': 'money'},
            {'label': 'Win rate', 'value': pl['win_rate'], 'format': 'percent'},
            {'label': 'Average cycle', 'value': js_round(pl['avg_cycle_days']) if pl['avg_cycle_days'] is not None else None, 'format': 'days'},
        ],
        'charts': [
            {
                'id': 'funnel',
                'kind': 'funnel',
                'title': finding,
                'subtitle': 'Deals reaching each stage (count) with the value at that stage (₹)'
                + (f'. Weakest step: {weak["label"]} ({_p0(weak["conversion"])} of the previous stage).' if weak else '.'),
                'format': 'money',
                'stages': stages,
                'empty': None if deals else 'No deals yet.',
            }
        ],
        'columns': [
            {'key': 'stage', 'label': 'Stage', 'type': 'string'},
            {'key': 'count', 'label': 'Deals now', 'type': 'number', 'align': 'right'},
            {'key': 'value', 'label': 'Value now', 'type': 'money', 'align': 'right'},
            {'key': 'reached', 'label': 'Reached', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'conversion', 'label': 'Conversion', 'type': 'percent', 'align': 'right'},
        ],
        'rows': [{'id': s['key'], 'stage': s['label'], **{k: s[k] for k in ('count', 'value', 'reached', 'conversion')}} for s in stages],
        'empty': 'No deals yet.',
        'ask': 'Where does our pipeline value sit by stage?',
    }


# ------------------------------------------------------------------ charts for the v5 views
def _legacy_charts(ctx: Ctx, view: str, d: dict) -> tuple[str, list[dict]]:
    rs = d.get('rows') or []
    if view == 'tasks_at_risk':
        days = [0, 1, 2, 3]
        cnt = [sum(1 for r in rs if r.get('days') == k) for k in days]
        hrs = sum((r.get('est') or 0) for r in rs)
        f = (
            f'{_plural(len(rs), "task")} due within 3 days {"hasn" if len(rs) == 1 else "haven"}’t started ({js_round(hrs)}h of estimates)'
            if rs
            else 'Nothing due in the next 3 days is waiting to start'
        )
        return f, [
            _cart(
                'due',
                'column',
                f,
                'Not-started tasks by days until due (count).',
                'number',
                ['Today', 'Tomorrow', 'In 2 days', 'In 3 days'],
                [{'key': 'n', 'label': 'Tasks', 'values': cnt}],
                x_label='Due',
                y_label='Tasks',
                empty=None if rs else 'Nothing at risk.',
            )
        ]
    if view == 'blocked':
        top = rs[:10]
        f = f'{_plural(len(rs), "task")} blocked; the longest for {_plural(rs[0]["since"], "day")} ({_short(rs[0]["task"], 40)})' if rs else 'No blocked tasks'
        return f, [
            _cart(
                'since',
                'bar',
                f,
                'Days each task has been blocked (longest first' + (', top 10).' if len(rs) > 10 else ').'),
                'days',
                [_short(r['task'], 40) for r in top],
                [{'key': 'since', 'label': 'Days blocked', 'values': [r['since'] for r in top]}],
                x_label='Task',
                y_label='Days',
                empty=None if rs else 'No blocked tasks.',
            )
        ]
    if view == 'overdue_milestones':
        top = sorted(rs, key=lambda r: -r['late'])[:10]
        f = (
            f'{_plural(len(rs), "milestone")} overdue; the latest by {_plural(top[0]["late"], "day")} ({_short(top[0]["milestone"], 36)})'
            if rs
            else 'No milestone is past its deadline'
        )
        return f, [
            _cart(
                'late',
                'bar',
                f,
                'Days past the planned end (latest first).',
                'days',
                [f'{_short(r["project"], 18)} · {_short(r["milestone"], 24)}' for r in top],
                [{'key': 'late', 'label': 'Days late', 'values': [r['late'] for r in top]}],
                links=[f'/projects/{r["project_id"]}' for r in top],
                x_label='Milestone',
                y_label='Days',
                empty=None if rs else 'No overdue milestones.',
            )
        ]
    if view == 'effort_ratio':
        alert = prop(ctx.tenant['settings'], 'projects.effort_ratio_alert')
        top = rs[:10]
        f = (
            f'{_plural(len(rs), "task")} above ×{alert:g} of estimate; worst ×{rs[0]["ratio"]:.2f} ({_short(rs[0]["task"], 36)})'
            if rs
            else f'No task is above ×{alert:g} of its estimate'
        )
        return f, [
            _cart(
                'ratio',
                'bar',
                f,
                'Actual ÷ estimated hours per task (×); the line is the alert threshold.',
                'ratio',
                [_short(r['task'], 40) for r in top],
                [{'key': 'ratio', 'label': 'Effort ratio', 'values': [r['ratio'] for r in top]}],
                refs=[{'value': alert, 'label': f'×{alert:g} alert'}],
                x_label='Task',
                y_label='Ratio',
                empty=None if rs else 'No overruns.',
            )
        ]
    if view == 'allocation':
        f = (
            f'{sum(1 for r in rs if r["avg"] > 1.1)} overloaded, {sum(1 for r in rs if r["avg"] < 0.8)} with spare capacity over the next 10 working days'
            if rs
            else 'Everyone is between 80% and 110% allocated'
        )
        top = rs[:14]
        return f, [
            _cart(
                'avg',
                'bar',
                f,
                'Average allocation over the next 10 working days (%); lines at 80% and 110%. Only outliers are listed.',
                'percent',
                [r['person'] for r in top],
                [{'key': 'avg', 'label': 'Average allocation', 'values': [r['avg'] for r in top]}],
                refs=[{'value': 0.8, 'label': '80%'}, {'value': 1.1, 'label': '110%'}],
                x_label='Person',
                y_label='Allocation',
                empty=None if rs else 'No allocation outliers.',
            )
        ]
    if view == 'timesheet_compliance':
        weeks = d.get('weeks') or []
        n = len(rs)

        def due_in(i: int) -> int:  # people for whom week i was due (joined by then)
            return sum(1 for r in rs if r['weeks'][i] != 'not_joined')

        rate = [(_pct(sum(1 for r in rs if r['weeks'][i] in ('approved', 'submitted')), due_in(i)) if due_in(i) else None) for i in range(len(weeks))]
        avg = d['kpis'][0]['value'] if d.get('kpis') else None
        any_due = any(due_in(i) for i in range(len(weeks)))
        f = (
            (f'Team compliance is {_p0(avg)} over the last 8 weeks; last week {_p0(rate[-1]) if rate and rate[-1] is not None else "—"}' if any_due else 'No completed week since your people joined yet')
            if n
            else 'No people in scope'
        )
        return f, [
            _cart(
                'weekly',
                'column',
                f,
                'Share of people with the week submitted or approved (%).',
                'percent',
                [f'{w[8:10]}/{w[5:7]}' for w in weeks],
                [{'key': 'rate', 'label': 'Compliance', 'values': rate}],
                refs=[{'value': 0.9, 'label': '90% target'}],
                x_label='Week',
                y_label='Compliance',
                empty=None if n and any_due else 'No people in scope.' if not n else 'The first week closes on Sunday; compliance starts from there.',
            )
        ]
    if view == 'approval_kpi':
        top = rs[:12]
        late = sum(r['late'] for r in rs)
        f = (
            f'{_plural(late, "late decision")} and {_plural(sum(r["overdue"] for r in rs), "approval")} past SLA in 8 weeks'
            if rs
            else 'No approvals in the last 8 weeks'
        )
        return f, [
            _cart(
                'decisions',
                'bar',
                f,
                'Timesheet decisions per manager in 8 weeks (count): on time and late.',
                'number',
                [r['person'] for r in top],
                [
                    {'key': 'on_time', 'label': 'On time', 'values': [r['on_time'] for r in top]},
                    {'key': 'late', 'label': 'Late', 'values': [r['late'] for r in top]},
                ],
                stacked=True,
                x_label='Manager',
                y_label='Decisions',
                empty=None if rs else 'No approvals.',
            )
        ]
    if view == 'stale_deals':
        top = rs[:12]
        val = sum(r['value'] for r in rs)
        f = (
            f'{_plural(len(rs), "deal")} worth {fmt_money_compact(val)} have had no activity for {prop(ctx.tenant["settings"], "sales.stale_deal_days")}+ days'
            if rs
            else 'Every open deal has recent activity'
        )
        return f, [
            _cart(
                'idle',
                'bar',
                f,
                'Days since the last logged activity, per deal.',
                'days',
                [_short(r['deal'], 36) for r in top],
                [{'key': 'idle', 'label': 'Days idle', 'values': [r['days_idle'] for r in top]}],
                links=[f'/sales/{r["deal_id"]}' for r in top],
                x_label='Deal',
                y_label='Days',
                empty=None if rs else 'No stale deals.',
            )
        ]
    if view == 'predictions':
        g_rows = []
        for r in rs:
            st = 'on_track' if r['chance'] >= 0.8 else 'at_risk' if r['chance'] >= 0.5 else 'late'
            g_rows.append(
                {
                    'id': r['id'],
                    'label': r['project'],
                    'sub': r.get('project_sub') or '',
                    'link': f'/projects/{r["project_id"]}',
                    'start': ctx.today,
                    'end': r['planned_end'],
                    'range': {'from': r['p50'], 'to': r['p80']} if r.get('p50') else None,
                    'status': st,
                    'status_label': f'{_p0(r["chance"])} chance on time',
                    'markers': [],
                    'detail': [
                        f'Planned end {r["planned_end"]}',
                        f'P50 {r.get("p50") or "—"} · P80 {r.get("p80") or "—"}',
                        f'At current pace {r.get("pace") or "—"}',
                    ],
                }
            )
        unlikely = [r for r in rs if r['chance'] < 0.5]
        f = (
            f'{_plural(len(unlikely), "project")} below a 50% chance of finishing by the planned end'
            if unlikely
            else ('Every project has at least a 50% chance of landing on time' if rs else 'No live projects with open work')
        )
        ends = [x for r in rs for x in (r['planned_end'], r.get('p80')) if x]
        to = max(ends + [add_days(ctx.today, 30)])
        return f, [
            {
                'id': 'forecast',
                'kind': 'gantt',
                'mode': 'forecast',
                'title': f,
                'subtitle': 'From today to the planned end (track); the band runs from the P50 to the P80 forecast finish.',
                'from': add_days(ctx.today, -3),
                'to': add_days(to, 7),
                'today': ctx.today,
                'rows': g_rows,
                'legend': [{'key': 'on_track', 'label': '80%+ chance'}, {'key': 'at_risk', 'label': '50–80%'}, {'key': 'late', 'label': 'Under 50%'}],
                'empty': 'No live projects with open work.',
            }
        ]
    if view == 'estimation':
        ppl = (d.get('sections') or [{}])[0].get('rows') or []
        top = ppl[:14]
        tm = d['kpis'][0]['value'] if d.get('kpis') else None
        f = (
            f'Work takes ×{tm:.2f} the estimate across the team' + (f'; {ppl[0]["person"]} runs highest at ×{ppl[0]["typical"]:.2f}' if ppl else '')
            if tm is not None
            else 'No closed tasks with actual hours yet'
        )
        return f, [
            _cart(
                'people',
                'bar',
                f,
                'Typical effort ratio per person (actual ÷ estimate, ×); the line is a perfect ×1.00.',
                'ratio',
                [r['person'] for r in top],
                [{'key': 'typical', 'label': 'Typical ratio', 'values': [r['typical'] for r in top]}],
                refs=[{'value': 1, 'label': '×1.00'}],
                x_label='Person',
                y_label='Ratio',
                empty=None if ppl else 'No closed tasks measured yet.',
            )
        ]
    return d.get('title', ''), []


NEW = {
    'portfolio': _portfolio,
    'milestone_timeline': _milestone_timeline,
    'throughput': _throughput,
    'utilisation': _utilisation,
    'revenue': _revenue,
    'margins': _margins,
    'funnel': _funnel,
}


def view_of(ctx: Ctx, view: str) -> dict[str, Any]:
    if not has(ctx, 'insights.view'):
        err(403, 'FORBIDDEN', 'Insights aren’t part of your role.')
    if view not in VIEWS:
        err(404, 'NOT_FOUND', 'That insight doesn’t exist.')
    if view not in allowed_views(ctx):
        err(403, 'FORBIDDEN', 'This insight isn’t available for your role.')
    label, group, question = VIEWS[view]
    if view in LEGACY:
        d = da.insight(ctx, view)
        finding, charts = _legacy_charts(ctx, view, d)
        return {**d, 'view': view, 'label': label, 'group': group, 'question': question, 'finding': finding, 'charts': charts}
    d = NEW[view](ctx)
    return {'view': view, 'label': label, 'group': group, 'question': question, **d}


def get_catalogue(req: Req) -> Any:
    return catalogue(req.ctx)


def get_view(req: Req) -> Any:
    return view_of(req.ctx, req.params['view'])


def register(r: Any) -> None:
    r.get('/insight-views', get_catalogue)
    r.get('/insight-views/:view', get_view)


__all__ = ['register', 'catalogue', 'view_of', 'allowed_views', 'VIEWS']
