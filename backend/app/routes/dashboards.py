"""Port of src/mock/routes/dashboards.ts — Module E: role dashboards (/dashboards/*), Insights views (/insights/:view) and
the weekly scorecard (/scorecard). Every figure is computed here from tenant rows; screens only format.

The algorithms are the TypeScript's, step for step. Hot filter-in-loop patterns (`rows.filter(x => x.k === v)` inside a
loop) read per-request group indexes instead: a group keeps the table order, so every list, sum and tie order is the
same as the filter's. Other route modules (reports, tasks, taskflow, timesheets) are imported lazily, inside the
functions, so this module loads before they do (router order) and while they are being ported."""

from __future__ import annotations

import itertools
import math
import re
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, Req, can_see_margins, can_see_salaries, err, has, has_any, role_def, rows, scope
from app.domain.calendar import (
    CalendarCtx,
    add_days,
    add_months,
    diff_days,
    fy_label,
    fy_range,
    fy_start_year,
    is_working_day,
    month_of,
    month_range,
    period_range,
    previous_period,
    quarter_of,
    week_days,
    week_start,
    working_days_between,
)
from app.domain.delivery import STAGE, TASK_STAGES, TASK_TAG, progress_of, stage_of
from app.domain.labels import DEAL_STAGE, STREAM, TASK_STATUS
from app.domain.sales import deal_value
from app.engine.analytics import (  # noqa: I001
    COSTED_TABLES,
    _day_walk,
    allocation_on,
    cal,
    costed_entries,
    expand_ledger,
    milestone_state,
    pipeline,
    project_metrics,
    redact_metrics,
    revenue_between,
    sla_due,
    task_actuals,
)
from app.engine.approval_kpi import approval_kpi, approval_kpis
from app.engine.performance import working_day_variance
from app.js import (
    UNDEF,
    coalesce,
    fmt_date_en_in,
    js_div,
    js_max,
    js_min,
    js_or,
    js_round,
    js_sort,
    js_str,
    js_truthy,
    locale_compare,
    locale_key,
    prop,
    to_fixed,
)
from app.routes.sales import comp_for, month_label

_DC = cat('mock/routes/dashboards')
SCORE_WEIGHTS = _DC['SCORE_WEIGHTS']
SCORE_THRESHOLDS = _DC['SCORE_THRESHOLDS']

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$', re.ASCII)
# `params.view in LABELS` is also true for the keys every JS object inherits
_OBJECT_PROTO_KEYS = frozenset(
    (
        'constructor',
        '__proto__',
        'hasOwnProperty',
        'isPrototypeOf',
        'propertyIsEnumerable',
        'toLocaleString',
        'toString',
        'valueOf',
        '__defineGetter__',
        '__defineSetter__',
        '__lookupGetter__',
        '__lookupSetter__',
    )
)


# ---------------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------------
def _S(ctx: Ctx) -> Any:
    """S(ctx) of reports.ts: the tenant settings"""
    return ctx.tenant['settings']


def r1(v: Any) -> Any:
    return js_round(v * 10) / 10


def r3(v: Any) -> Any:
    return js_round(v * 1000) / 1000


def chg(cur: Any, prev: Any) -> Any:
    if js_truthy(prev):
        return (cur - prev) / abs(prev)
    return None if js_truthy(cur) else 0


def money4(ctx: Ctx, frm: str, to: str) -> dict[str, Any]:
    r = revenue_between(ctx, frm, to)
    return {'billed': r['billed'], 'collected': r['collected'], 'cost': r['cost'], 'net': r['net'], 'margin': r['margin']}


def fmt_day(d: str) -> str:
    """new Date(d + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })"""
    return fmt_date_en_in(d, day='numeric', month='short')


def _once(ctx: Ctx, key: str, fn: Any) -> Any:
    """Per-request memo (ctx is created per request)."""
    m = scope().memo
    k = ('mock/routes/dashboards', ctx, key)
    if k not in m:
        m[k] = fn()
    return m[k]


def _hours(act: dict, task_id: Any) -> Any:
    """act.get(id)?.hours ?? 0"""
    a = act.get(task_id)
    return coalesce(a.get('hours', UNDEF), 0) if a is not None else 0


def _by(items: Any, key: str) -> dict[Any, list]:
    """group rows by a field, table order kept (the same rows, in the same order, as `rows.filter(x => x[key] === v)`)"""
    g: dict[Any, list] = {}
    for x in items:
        g.setdefault(x.get(key), []).append(x)
    return g


def _group_now(ctx: Ctx, table: str, key: str) -> dict[Any, list]:
    """_by(rows(ctx, table), key) now — the scope's group index when rows(ctx, table) is the table view (shared by
    every request while the table is unchanged: READ-ONLY lists), else built"""
    g = scope().tenant_group(table, ctx.tenant_id, key)
    return g if g is not None else _by(rows(ctx, table), key)


def _tenant_group(ctx: Ctx, table: str, key: str) -> dict[Any, list]:
    return _once(ctx, f'group:{table}:{key}', lambda: _group_now(ctx, table, key))


def users_map(ctx: Ctx) -> dict[Any, dict]:
    return {u['id']: u for u in rows(ctx, 'users')}


def _is_ts_user(u: dict) -> bool:
    from app.routes.reports import is_timesheet_user

    return is_timesheet_user(u)


def _task_code(ms: Any, t: dict) -> str:
    from app.routes.reports import task_code

    return task_code(ms, t)


def _can_see_task(ctx: Ctx, t: dict) -> bool:
    from app.routes.tasks import can_see_task

    return can_see_task(ctx, t)


def _ordered(ids: Any) -> dict[Any, None]:
    """a JS Set (insertion ordered) as a dict"""
    return dict.fromkeys(ids)


def _team_ordered(ctx: Ctx, mgr_id: Any) -> dict[Any, None]:
    """reports.team_of(ctx, id) as an insertion-ordered set: the TS Set iterates in its walk order (direct report, then
    that person's reports, depth first, users in table order), which the Python set does not keep"""
    from app.routes.reports import team_of

    members = team_of(ctx, mgr_id)
    by_mgr: dict[Any, list] = {}
    for u in rows(ctx, 'users'):
        by_mgr.setdefault(u.get('reports_to_id'), []).append(u['id'])
    out: dict[Any, None] = {}

    def walk(id_: Any, depth: int) -> None:
        if depth > 6:
            return
        for x in by_mgr.get(id_, ()) if id_ is not None else ():
            if x not in out:
                out[x] = None
                walk(x, depth + 1)

    walk(mgr_id, 0)
    ordered = {x: None for x in out if x in members}
    for x in members:  # anything the walk missed keeps membership (never expected)
        ordered.setdefault(x, None)
    return ordered


def pill(ctx: Ctx, p: dict, cm: bool, nm: Any) -> dict[str, Any]:
    m = redact_metrics(ctx, project_metrics(ctx, p), cm)
    nx = m.get('next_milestone')
    return {
        'id': p['id'],
        'code': p['code'],
        'name': p['name'],
        'client': p['client'],
        'status': p['status'],
        'health': m['health'],
        'progress': m['progress'],
        'spi': None if m['spi'] is None else r3(m['spi']),
        'effort_ratio': None if m['effort_ratio'] is None else r3(m['effort_ratio']),
        'projected_margin': m['projected_margin'],
        'overdue_tasks': m['overdue_tasks'],
        'blocked_tasks': m['blocked_tasks'],
        'overdue_milestones': m['overdue_milestones'],
        'pm': nm(p.get('pm_id')),
        'next_milestone': {
            'id': nx['id'],
            'label': f'M{js_str(nx["no"])} {js_str(nx["name"])}',
            'planned_end': nx['planned_end'],
            'billing': nx['billing_amount_paise'],
            'days': diff_days(nx['planned_end'], ctx.today),
        }
        if nx
        else None,
    }


LIVE = ('initiated', 'active', 'on_hold')
HEALTH_ORDER = {'off_track': 0, 'at_risk': 1, 'on_track': 2}


def _ms_state(ctx: Ctx, m: dict) -> dict[str, Any]:
    """milestoneState(ctx, m, rows(ctx, 'tasks')) — the state only looks at the milestone's own tasks, so they are passed
    pre-grouped (same rows, same order)."""
    return milestone_state(ctx, m, _tenant_group(ctx, 'tasks', 'milestone_id').get(m['id'], []))


def milestone_rows(ctx: Ctx, projects: list[dict], frm: str, to: str) -> list[dict]:
    pmap = {p['id']: p for p in projects}
    out = []
    for m in rows(ctx, 'milestones'):
        if js_truthy(m.get('deleted_at')) or m['project_id'] not in pmap or js_truthy(m.get('achieved_on')) or not (frm <= m['planned_end'] <= to):
            continue
        p = pmap[m['project_id']]
        st = _ms_state(ctx, m)
        out.append(
            {
                'id': m['id'],
                'project_id': p['id'],
                'project': p['name'],
                'client': p['client'],
                'label': f'M{js_str(m["no"])} {js_str(m["name"])}',
                'planned_end': m['planned_end'],
                'days': diff_days(m['planned_end'], ctx.today),
                'state': st['state'],
                'progress': st['progress'],
                'billing': m['billing_amount_paise'],
                'billing_status': m['billing_status'],
            }
        )
    out = [x for x in out if x['state'] != 'achieved_on_time' and x['state'] != 'achieved_late']
    return sorted(out, key=lambda x: locale_key(x['planned_end']))


def task_row(ctx: Ctx, t: dict, mp: dict, reason: Any = None) -> dict[str, Any]:
    oid = t.get('owner_id')
    owner = mp['users'].get(oid) if js_truthy(oid) else None
    mid = t.get('milestone_id')
    proj = mp['projects'].get(t['project_id'])
    out = {
        'id': t['id'],
        'code': _task_code(mp['ms'].get(mid) if js_truthy(mid) else None, t),
        'title': t['title'],
        'project_id': t['project_id'],
        'project': coalesce(proj.get('name', UNDEF) if proj else UNDEF, '—'),
        'owner_id': t.get('owner_id'),
        'owner': coalesce(owner.get('name', UNDEF) if owner else UNDEF, None),
        'owner_hue': coalesce(owner.get('avatar_hue', UNDEF) if owner else UNDEF, 150),
        'status': t['status'],
        'due_on': t['due_on'],
        'days': diff_days(t['due_on'], ctx.today),
        'est': t['est_hours'],
        'actual': r1(_hours(mp['act'], t['id'])),
    }
    if js_truthy(reason):
        out['reason'] = reason
    return out


def maps(ctx: Ctx) -> dict[str, Any]:
    def f() -> dict[str, Any]:
        return {
            'projects': {p['id']: p for p in rows(ctx, 'projects')},
            'ms': {m['id']: m for m in rows(ctx, 'milestones')},
            'users': users_map(ctx),
            'act': task_actuals(ctx),
        }

    return _once(ctx, 'maps', f)


def cash_position(ctx: Ctx) -> dict[str, Any]:
    """Cash on hand = opening cash + received − paid (mirrors the finance module)."""
    opening = coalesce(prop(_S(ctx), 'finance.opening_cash_paise'), 0)
    all_ = rows(ctx, 'ledger_entries')
    start = ctx.today
    for e in all_:
        if e['date'] < start:
            start = e['date']
    inflow: Any = 0
    outflow: Any = 0
    for e in expand_ledger(ctx, start, ctx.today):
        if e['kind'] == 'income' and not js_truthy(e.get('invoice_id')) and e.get('status') == 'received':
            inflow += e['amount_paise']
        if e['kind'] == 'expense' and e.get('status') == 'paid':
            outflow += e['amount_paise']
    for p in rows(ctx, 'payments'):
        if p['paid_on'] <= ctx.today:
            inflow += p['amount_paise']
    balance = opening + inflow - outflow
    last3 = [month_range(month_of(add_months(month_of(ctx.today) + '-01', -i))) for i in (1, 2, 3)]
    revs = [revenue_between(ctx, m['from'], m['to']) for m in last3]
    cost_sum: Any = 0
    col_sum: Any = 0
    for r in revs:
        cost_sum += r['cost']
    for r in revs:
        col_sum += r['collected']
    burn = js_round(cost_sum / 3)
    rec = js_round(col_sum / 3)
    net = burn - rec
    return {
        'balance': balance,
        'monthly_burn': burn,
        'recurring_income': rec,
        'net_burn': net,
        'runway_months': balance / net if net > 0 else None,
        'cash_positive': net <= 0,
    }


def approver_rows(ctx: Ctx, from_monday: str, scope_: Any = None) -> list[dict]:
    """Approval KPI per manager (BR-11): score = −(late decisions + overdue pending) over the window."""
    users = users_map(ctx)
    out: dict[Any, dict] = {}
    for w in rows(ctx, 'timesheet_weeks'):
        if w['week_start'] < from_monday or not js_truthy(w.get('approver_id')) or w.get('decided_by') == w['user_id']:
            continue
        # decisions are credited to whoever decided (a delegate counts for themselves); pending stays with the approver
        st = w['status']
        who = w['decided_by'] if (st == 'approved' or st == 'rejected') and js_truthy(w.get('decided_by')) else w['approver_id']
        if scope_ is not None and who not in scope_:
            continue
        ap = users.get(who)
        if not ap:
            continue
        m = out.get(ap['id'])
        if m is None:
            m = {'id': ap['id'], 'name': ap['name'], 'hue': ap['avatar_hue'], 'pending': 0, 'overdue': 0, 'late': 0, 'on_time': 0, 'decided': 0, 'score': 0}
            out[ap['id']] = m
        if st == 'approved' or st == 'rejected':
            m['decided'] += 1
            if js_truthy(w.get('late')):
                m['late'] += 1
            else:
                m['on_time'] += 1
        elif st == 'submitted' and js_truthy(w.get('submitted_at')):
            m['pending'] += 1
            if ctx.today > sla_due(ctx, w['submitted_at']):
                m['overdue'] += 1
    lst = [{**m, 'score': -(m['late'] + m['overdue'])} for m in out.values()]
    return sorted(lst, key=lambda m: (m['score'], -m['pending']))


def deal_lite(ctx: Ctx, d: dict, nm: Any) -> dict[str, Any]:
    v = deal_value(d)
    return {
        'id': d['id'],
        'title': d['title'],
        'client': d['client'],
        'stage': d['stage'],
        'owner': nm(d.get('owner_id')),
        'value': v,
        'weighted': js_round(v * DEAL_STAGE[d['stage']]['chance']),
        'days_idle': diff_days(ctx.today, d['last_activity_on']),
        'expected_close': d['expected_close'],
        'next_step': coalesce(prop(d, 'next_step'), None),
        'next_step_on': coalesce(prop(d, 'next_step_on'), None),
    }


def next_working_days(ctx: Ctx, n: int) -> list[str]:
    def f() -> list[str]:
        c = cal(ctx)
        out: list[str] = []
        d = ctx.today
        guard = 0
        while len(out) < n:
            g = guard
            guard += 1
            if not g < 60:
                break
            if is_working_day(d, c):
                out.append(d)
            d = add_days(d, 1)
        return out

    return list(_once(ctx, f'nwd:{n}', f))


def pm_people(ctx: Ctx) -> dict[Any, None]:
    """People a PM is responsible for: reports (direct + indirect) and anyone owning tasks on their projects (ordered set)."""
    from app.routes.reports import my_projects

    team = _team_ordered(ctx, ctx.user_id)
    mine = {p['id'] for p in my_projects(ctx)}
    for t in rows(ctx, 'tasks'):
        if t['project_id'] in mine and js_truthy(t.get('owner_id')):
            team.setdefault(t['owner_id'], None)
    if has(ctx, 'people.view_all'):
        for u in rows(ctx, 'users'):
            if _is_ts_user(u):
                team.setdefault(u['id'], None)
    team.pop(ctx.user_id, None)
    return team


# ---------------------------------------------------------------------------------------------------------------------
# v5 shared delivery calculations (Feature Guide §1, §2, §14, §15)
# ---------------------------------------------------------------------------------------------------------------------
def week_of(ctx: Ctx, q: Any) -> str:
    """The week a request asks about (Monday ISO); defaults to this week."""
    s = js_str(coalesce(q, ''))
    return week_start(s) if _DATE_RE.match(s) else week_start(ctx.today)


# simulate + ratio_samples read these (and the request date); cal(ctx) = tenants + holidays
P50_TABLES = ('tasks', 'leave', 'task_dependencies', *COSTED_TABLES)


def p50_finish(ctx: Ctx) -> dict[str, str]:
    """P50 finish date per open task (each owner's queue re-run with their typical effort ratio). Shared by every
    request of the day while its input tables are unchanged (READ-ONLY)."""
    return _once(ctx, 'p50', lambda: scope().shared(('dash:p50', ctx.today), P50_TABLES, lambda: simulate(ctx, 0.5, ratio_samples(ctx))))


def breach_sets(ctx: Ctx, scope_: Any = None) -> dict[str, Any]:
    """Open tasks already past their target end, and open tasks not yet late whose P50 finish passes it."""
    run = p50_finish(ctx)
    t = ctx.today
    open_ = [x for x in rows(ctx, 'tasks') if x['status'] != 'done' and (scope_ is None or x['project_id'] in scope_)]
    return {
        'breached': [x for x in open_ if x['due_on'] < t],
        'forecast': [x for x in open_ if x['due_on'] >= t and run.get(x['id'], x['due_on']) > x['due_on']],
        'run': run,
    }


def critical_ids(ctx: Ctx, tasks: list[dict]) -> set:
    """Critical tasks: the task that sets its milestone's deadline plus every predecessor with no float before it."""
    c = cal(ctx)
    by_id = {t['id']: t for t in tasks}
    preds: dict[Any, list] = {}
    for d in rows(ctx, 'task_dependencies'):
        if d['task_id'] in by_id and d['depends_on_task_id'] in by_id:
            preds.setdefault(d['task_id'], []).append(d['depends_on_task_id'])
    groups: dict[Any, list] = {}
    for t in tasks:
        k = coalesce(prop(t, 'milestone_id'), f'{js_str(t["project_id"])}:none')
        groups.setdefault(k, []).append(t)
    out: set = set()
    for g in groups.values():
        end = '0000-01-01'
        for t in g:
            if t['due_on'] > end:
                end = t['due_on']
        stack = [t for t in g if t['due_on'] == end]
        while stack:
            t = stack.pop()
            if t['id'] in out:
                continue
            out.add(t['id'])
            for pid in preds.get(t['id'], ()):
                p = by_id[pid]
                # float = working days strictly between the predecessor's end and this task's start
                gap = 0 if p['due_on'] >= t['start_on'] else working_days_between(add_days(p['due_on'], 1), add_days(t['start_on'], -1), c)
                if gap <= 0:
                    stack.append(p)
    return out


def earned_pct(tasks: list[dict]) -> Any:
    """Earned progress of a set of tasks: Σ(est × progress) ÷ Σ est."""
    est: Any = 0
    for t in tasks:
        est += t['est_hours']
    if js_truthy(est):
        s: Any = 0
        for t in tasks:
            s += (t['est_hours'] * progress_of(t)) / 100
        return s / est
    return 1 if tasks and all(t['status'] == 'done' for t in tasks) else 0


def productive_hours(ctx: Ctx, u: dict) -> Any:
    ph = u.get('productive_hours')
    if isinstance(ph, (int, float)) and not isinstance(ph, bool) and ph > 0:
        return ph
    return coalesce(prop(_S(ctx), 'team.default_productive_hours'), 6)


def load_hours(ctx: Ctx, users: list[dict], days: list[str]) -> list[dict]:
    """Resource load over the given working days (see the TS doc)."""
    c = cal(ctx)
    act = task_actuals(ctx)
    open_by_owner = _once(
        ctx, 'open_tasks_by_owner', lambda: _by([t for t in rows(ctx, 'tasks') if t['status'] != 'done' and js_truthy(t.get('owner_id'))], 'owner_id')
    )
    leave_by_user = _tenant_group(ctx, 'leave', 'user_id')
    day_set = set(days)
    last = days[-1] if days else ctx.today
    today = ctx.today
    out = []
    for u in users:
        prod = productive_hours(ctx, u)
        lv: dict[str, Any] = {}
        for lrow in leave_by_user.get(u['id'], ()):
            if lrow['date'] in day_set:
                lv[lrow['date']] = lrow['portion']
        full = {d for d, p in lv.items() if p == 'full'}
        leave_days: Any = 0
        for p in lv.values():
            leave_days += 1 if p == 'full' else 0.5
        capacity = js_max(0, prod * (len(days) - leave_days))
        per_day: dict[str, Any] = {}
        mine = open_by_owner.get(u['id'], [])
        for t in mine:
            rem = js_max(0, t['est_hours'] - _hours(act, t['id']))
            if rem <= 0:
                continue
            frm = t['start_on'] if t['start_on'] > today else today
            if frm > last:
                continue
            to = frm if t['due_on'] < frm else t['due_on']
            span = [d for d, working in itertools.islice(_day_walk(frm, to, c), 400) if working and d not in full]
            if not span:
                span.append(next((d for d in days if d >= frm and d not in full), frm))
            share = rem / len(span)
            for d in span:
                if d in day_set:
                    per_day[d] = per_day.get(d, 0) + share
        booked: Any = 0
        for d in days:
            if d in full:
                continue
            alloc = (allocation_on(ctx, u['id'], d) / 100) * prod
            booked += js_max(per_day.get(d, 0), alloc)
        pct = booked / capacity if capacity > 0 else None
        out.append(
            {
                'id': u['id'],
                'name': u['name'],
                'hue': u['avatar_hue'],
                'title': u['title'],
                'booked': js_round(booked),
                'capacity': js_round(capacity),
                'pct': pct,
                'double_booked': pct is not None and pct > 1,
                'leave_days': leave_days,
                'open_tasks': len(mine),
            }
        )
    return out


def mentions_of(ctx: Ctx, user_id: Any, limit: int = 10) -> list[dict]:
    """Comments that @mention a person, newest first (Feature Guide §7 "Tagged you")."""
    mp = maps(ctx)
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    lst = [
        c
        for c in rows(ctx, 'comments')
        if user_id in coalesce(prop(c, 'mentions'), []) and c.get('author_id') != user_id and c['task_id'] in tasks and _can_see_task(ctx, tasks[c['task_id']])
    ]
    lst = sorted(lst, key=lambda c: locale_key(c['created_at']), reverse=True)[:limit]
    out = []
    for c in lst:
        t = tasks[c['task_id']]
        a = mp['users'].get(c.get('author_id'))
        tag = coalesce(prop(t, 'tag'), None)
        mid = t.get('milestone_id')
        proj = mp['projects'].get(t['project_id'])
        out.append(
            {
                'id': c['id'],
                'task_id': t['id'],
                'task_code': _task_code(mp['ms'].get(mid) if js_truthy(mid) else None, t),
                'task_title': t['title'],
                'project': coalesce(proj.get('name', UNDEF) if proj else UNDEF, '—'),
                'tag': TASK_TAG[tag]['label'] if js_truthy(tag) else None,
                'tag_tone': TASK_TAG[tag]['tone'] if js_truthy(tag) else None,
                'author': coalesce(a.get('name', UNDEF) if a else UNDEF, 'Someone'),
                'author_hue': coalesce(a.get('avatar_hue', UNDEF) if a else UNDEF, 150),
                'at': c['created_at'],
                'body': c['body'],
            }
        )
    return out


def _first_name(name: str) -> str:
    return name.split(' ')[0]


def covering_of(ctx: Ctx, user_id: Any, wk: str) -> list[dict]:
    """Tasks a person covers this week: they are the backup and the owner is on leave, or a delegation hands them over."""
    mp = maps(ctx)
    days = week_days(wk)
    frm = days[0]
    to = days[-1]
    leave_by_user = _by([lrow for lrow in rows(ctx, 'leave') if lrow['portion'] == 'full' and frm <= lrow['date'] <= to], 'user_id')
    out: dict[Any, dict] = {}
    for t in rows(ctx, 'tasks'):
        if t['status'] == 'done' or t.get('backup_id') != user_id or not js_truthy(t.get('owner_id')) or t['owner_id'] == user_id or not _can_see_task(ctx, t):
            continue
        off = js_sort([lrow['date'] for lrow in leave_by_user.get(t['owner_id'], ())])
        if not off:
            continue
        ou = mp['users'].get(t['owner_id'])
        owner = coalesce(ou.get('name', UNDEF) if ou else UNDEF, 'Owner')
        span = fmt_day(off[0]) if len(off) == 1 else f'{fmt_day(off[0])} – {fmt_day(off[-1])}'
        out[t['id']] = {**task_row(ctx, t, mp), 'for_name': owner, 'why': f'{_first_name(owner)} on leave {span}'}
    for d in rows(ctx, 'delegations'):
        if d.get('to_user_id') != user_id or d.get('include_tasks') is False or d['to_on'] < frm or d['from_on'] > to:
            continue
        fu = mp['users'].get(d.get('from_user_id'))
        owner = coalesce(fu.get('name', UNDEF) if fu else UNDEF, 'Colleague')
        for t in rows(ctx, 'tasks'):
            # the delegation itself makes the task visible to the delegate (G7.02 / G16.05) in whichever week the header shows;
            # canSeeTask only resolves covering for the current week, so it only gates someone else looking at this person
            if (
                t['status'] == 'done'
                or t.get('owner_id') != d.get('from_user_id')
                or (js_truthy(d.get('project_id')) and t['project_id'] != d['project_id'])
                or t['id'] in out
                or (user_id != ctx.user_id and not _can_see_task(ctx, t))
            ):
                continue
            if t['due_on'] < frm and t['start_on'] > to:
                continue
            out[t['id']] = {
                **task_row(ctx, t, mp),
                'for_name': owner,
                'why': f'Delegated by {_first_name(owner)} {fmt_day(d["from_on"])} – {fmt_day(d["to_on"])}',
            }
    return sorted(out.values(), key=lambda x: locale_key(x['due_on']))


# ---------------------------------------------------------------------------------------------------------------------
# CEO
# ---------------------------------------------------------------------------------------------------------------------
def ceo(ctx: Ctx, kind: str, anchor: str, wk: str) -> dict[str, Any]:
    from app.routes.reports import name_of

    if not has(ctx, 'finance.view'):
        err(403, 'FORBIDDEN', 'The CEO dashboard is for admins.')
    fy_s = prop(_S(ctx), 'company.fy_start_month')
    t = ctx.today
    # headline cards: month / quarter / FY-to-date vs the comparable previous period
    mr = month_range(month_of(t))
    pm = month_range(month_of(add_months(month_of(t) + '-01', -1)))
    q = quarter_of(t, fy_s)
    pq = quarter_of(add_days(q['from'], -1), fy_s)
    fy = fy_start_year(t, fy_s)
    fyr = fy_range(fy, fy_s)
    pfy = fy_range(fy - 1, fy_s)
    days = diff_days(t, fyr['from'])
    m_cur = money4(ctx, mr['from'], mr['to'])

    # like-for-like: the same number of days into the previous month / quarter (avoids a false −100% on day 2)
    def cap_at(frm: str, to: str, n: int) -> str:
        e = add_days(frm, n)
        return e if e < to else to

    m_prev = money4(ctx, pm['from'], cap_at(pm['from'], pm['to'], diff_days(t, mr['from'])))
    q_cur = money4(ctx, q['from'], q['to'])
    q_prev = money4(ctx, pq['from'], cap_at(pq['from'], pq['to'], diff_days(t, q['from'])))
    # every figure counts only what is dated up to today (revenueBetween caps the range at today)
    f_cur = money4(ctx, fyr['from'], fyr['to'])
    f_prev = money4(ctx, pfy['from'], add_days(pfy['from'], days))
    revenue = {
        'month': {
            **m_cur,
            'label': f'{period_range("month", t, fy_s)["label"]} to date',
            'change': chg(m_cur['billed'], m_prev['billed']),
            'prev_label': f'same days of {period_range("month", pm["from"], fy_s)["label"]}',
        },
        'quarter': {**q_cur, 'label': f'{q["label"]} to date', 'change': chg(q_cur['billed'], q_prev['billed']), 'prev_label': f'same days of {pq["label"]}'},
        'fy': {**f_cur, 'label': f'{fy_label(fy)} to date', 'change': chg(f_cur['billed'], f_prev['billed']), 'prev_label': f'same days of {fy_label(fy - 1)}'},
    }

    # selected period table
    pr = period_range(kind, anchor, fy_s)
    prev = previous_period(kind, anchor, fy_s)
    period_rows: list[dict] = []
    if kind != 'month':
        m = pr['from']
        while m <= pr['to']:
            r = month_range(month_of(m))
            if r['from'] > t:
                break
            period_rows.append(
                {'key': month_of(m), 'label': month_label(month_of(m)), 'from': r['from'], 'to': r['to'], 'kind': 'sub', **money4(ctx, r['from'], r['to'])}
            )
            m = add_months(m, 1)
    else:
        # weeks of the month
        w = week_start(pr['from'])
        while w <= pr['to']:
            frm = pr['from'] if w < pr['from'] else w
            to = pr['to'] if add_days(w, 6) > pr['to'] else add_days(w, 6)
            if frm > t:
                break
            period_rows.append({'key': frm, 'label': f'{fmt_day(frm)} – {fmt_day(to)}', 'from': frm, 'to': to, 'kind': 'sub', **money4(ctx, frm, to)})
            w = add_days(w, 7)
    period_rows.append({'key': 'total', 'label': pr['label'], 'from': pr['from'], 'to': pr['to'], 'kind': 'total', **money4(ctx, pr['from'], pr['to'])})
    period_rows.append(
        {'key': 'previous', 'label': prev['label'], 'from': prev['from'], 'to': prev['to'], 'kind': 'previous', **money4(ctx, prev['from'], prev['to'])}
    )

    # 12 months ending in the anchor month (never beyond this month)
    end_m = month_of(t if anchor > t else (t if pr['to'] > t else pr['to']))
    months = []
    for i in range(12):
        ym = month_of(add_months(end_m + '-01', i - 11))
        r = month_range(ym)
        x = revenue_between(ctx, r['from'], r['to'])
        months.append({'key': ym, 'label': month_label(ym), 'billed': x['billed'], 'collected': x['collected'], 'cost': x['cost'], 'net': x['net']})

    # streams for the FY containing the anchor (to date)
    sfy = fy_start_year(anchor, fy_s)
    sr = fy_range(sfy, fy_s)
    bs = revenue_between(ctx, sr['from'], sr['to'] if sr['to'] < t else t)['byStream']
    streams = [{'key': k, 'label': STREAM[k], 'value': coalesce(bs.get(k, UNDEF), 0)} for k in STREAM]
    streams = sorted([s for s in streams if s['value'] > 0], key=lambda s: s['value'], reverse=True)

    # revenue ahead
    live = {p['id'] for p in rows(ctx, 'projects') if p['status'] not in ('closed', 'npa')}
    open_ms = [
        m
        for m in rows(ctx, 'milestones')
        if not js_truthy(m.get('deleted_at')) and m['billing_status'] == 'none' and m['project_id'] in live and m['billing_amount_paise'] > 0
    ]

    def within(n: int) -> Any:
        # evaluated after the milestone sweep (the pills below): the TS reads the swept rows through object identity
        lim = add_days(t, n)
        s: Any = 0
        for m0 in open_ms:
            m = coalesce(scope().find_by('milestones', 'id', m0['id']), m0)
            if m['planned_end'] <= lim:
                s += m['billing_amount_paise']
        return s

    uninvoiced: Any = 0
    for m in open_ms:
        uninvoiced += m['billing_amount_paise']
    pipe = pipeline(ctx)

    # portfolio + delivery
    cm = can_see_margins(ctx)
    nm = name_of(ctx)
    active_projects = [p for p in rows(ctx, 'projects') if p['status'] in LIVE]
    projects = [pill(ctx, p, cm, nm) for p in active_projects]
    projects = sorted(projects, key=lambda p: (HEALTH_ORDER[p['health']], locale_key(p['name'])))
    tasks = rows(ctx, 'tasks')
    tasks_by_ms = _tenant_group(ctx, 'tasks', 'milestone_id')
    all_ms = [m for m in rows(ctx, 'milestones') if not js_truthy(m.get('deleted_at'))]
    achieved = [m for m in all_ms if js_truthy(m.get('achieved_on'))]
    on_time_ms = sum(1 for m in achieved if m['achieved_on'] <= m['baseline_end'])
    spis = [p['spi'] for p in projects if p['spi'] is not None]
    done = [x for x in tasks if x['status'] == 'done' and js_truthy(x.get('done_on'))]
    # Average lateness (Feature Guide §2): working days late, averaged over completed tasks that finished after their target end
    late_days = [v for v in (working_day_variance(ctx, x['due_on'], x['done_on']) for x in done if x['done_on'] > x['due_on']) if v > 0]
    live_ids = {p['id'] for p in active_projects}
    next10 = next_working_days(ctx, 10)
    delivery = [u for u in rows(ctx, 'users') if _is_ts_user(u) and u.get('department') == 'delivery']
    util: Any = None
    if delivery:
        tot: Any = 0
        for u in delivery:
            a: Any = 0
            for d in next10:
                a += allocation_on(ctx, u['id'], d)
            tot += js_div(a, len(next10))
        util = tot / len(delivery) / 100

    ms30 = milestone_rows(ctx, active_projects, add_days(t, -3650), add_days(t, 30))
    stale_days = prop(_S(ctx), 'sales.stale_deal_days')

    # ---- v5: fixed period table, stream shares, delivery KPIs, milestones and cash, finance highlights ----
    fixed_rows = [
        {
            'key': 'month',
            'label': 'This month',
            'sub': f'{period_range("month", t, fy_s)["label"]} to date',
            **m_cur,
            'change': revenue['month']['change'],
            'change_label': 'vs same days last month',
        },
        {
            'key': 'quarter',
            'label': f'This quarter ({q["label"]})',
            'sub': f'{q["label"]} to date',
            **q_cur,
            'change': revenue['quarter']['change'],
            'change_label': 'vs same days last quarter',
        },
        {
            'key': 'fy',
            'label': 'This financial year',
            'sub': f'{fy_label(fy)} to date',
            **f_cur,
            'change': revenue['fy']['change'],
            'change_label': 'vs same period last year',
        },
    ]
    # Feature Guide §2 stream names (STREAM): SAP consulting, Products, Services, Subscriptions, AMS
    s_total: Any = 0
    for x in streams:
        s_total += x['value']
    stream_shares = [{'key': x['key'], 'label': x['label'], 'value': x['value'], 'share': x['value'] / s_total if js_truthy(s_total) else 0} for x in streams]
    act = task_actuals(ctx)
    closed = [x for x in done if x['est_hours'] > 0 and _hours(act, x['id']) > 0]
    c_est: Any = 0
    c_act: Any = 0
    for x in closed:
        c_est += x['est_hours']
    for x in closed:
        c_act += _hours(act, x['id'])
    br = breach_sets(ctx, live_ids)
    p50 = br['run']
    people = [u for u in rows(ctx, 'users') if _is_ts_user(u)]
    loads = load_hours(ctx, delivery if delivery else people, next10)
    cap_sum: Any = 0
    for x in loads:
        cap_sum += x['capacity']

    def ms_forecast(m: dict) -> Any:
        open_ = [x for x in tasks_by_ms.get(m['id'], ()) if x['status'] != 'done']
        if not open_:
            return coalesce(prop(m, 'achieved_on'), None)
        mx = '0000-01-01'
        for x in open_:
            f = p50.get(x['id'], x['due_on'])
            if f > mx:
                mx = f
        return mx

    ms_by_id = {m['id']: m for m in all_ms}
    milestones_30 = []
    for r in ms30:
        m = ms_by_id[r['id']]
        mine = tasks_by_ms.get(m['id'], [])
        f = ms_forecast(m)
        milestones_30.append(
            {
                **r,
                'code': f'M{js_str(m["no"])}',
                'name': m['name'],
                'progress': earned_pct(mine),
                'forecast': f,
                'forecast_late': js_truthy(f) and f > m['planned_end'],
            }
        )
    live_ms = [m for m in all_ms if m['project_id'] in live]
    achieved_not_invoiced: Any = 0
    for m in live_ms:
        if (
            m['billing_status'] == 'none'
            and m['billing_amount_paise'] > 0
            and (js_truthy(m.get('achieved_on')) or _ms_state(ctx, m)['state'].startswith('achieved'))
        ):
            achieved_not_invoiced += m['billing_amount_paise']
    invoiced_awaiting: Any = 0
    for i in rows(ctx, 'invoices'):
        invoiced_awaiting += js_max(0, i['amount_paise'] - i['paid_amount_paise'])
    billable30: Any = 0
    lim30 = add_days(t, 30)
    for m in live_ms:
        if m['billing_status'] == 'none' and m['billing_amount_paise'] > 0 and not js_truthy(m.get('achieved_on')):
            f = ms_forecast(m)
            if js_truthy(f) and f <= lim30:
                billable30 += m['billing_amount_paise']
    ledger_rows = rows(ctx, 'ledger_entries')
    this_month = expand_ledger(ctx, mr['from'], mr['to'])
    recurring = sorted(
        [e for e in ledger_rows if js_truthy(e.get('recurring')) and (not js_truthy(e.get('until_on')) or e['until_on'] >= t)],
        key=lambda e: e['amount_paise'],
        reverse=True,
    )[:8]
    top_exp = sorted([e for e in this_month if e['kind'] == 'expense'], key=lambda e: e['amount_paise'], reverse=True)[:6]
    month_income: Any = 0
    month_expense: Any = 0
    for e in this_month:
        if e['kind'] == 'income':
            month_income += e['amount_paise']
    for e in this_month:
        if e['kind'] == 'expense':
            month_expense += e['amount_paise']
    finance = {
        'pnl': months[-6:],
        'recurring': [
            {
                'id': e['id'],
                'kind': e['kind'],
                'description': e['description'],
                'category': e['category'],
                'amount': e['amount_paise'],
                'until_on': prop(e, 'until_on'),
            }
            for e in recurring
        ],
        'top_expenses': [
            {
                'id': f'{e["id"]}:{e["occurrence"]}',
                'description': e['description'],
                'category': e['category'],
                'amount': e['amount_paise'],
                'date': e['occurrence'],
            }
            for e in top_exp
        ],
        'month_income': month_income,
        'month_expense': month_expense,
    }
    done_on_time = sum(1 for x in done if x['done_on'] <= x['due_on'])
    wk_end = add_days(wk, 6)
    spi_sum: Any = 0
    for x in spis:
        spi_sum += x
    late_sum: Any = 0
    for x in late_days:
        late_sum += x
    booked_sum: Any = 0
    for x in loads:
        booked_sum += x['booked']
    stale_value: Any = 0
    for d in pipe['stale']:
        stale_value += deal_value(d)
    t28 = add_days(t, -28)
    t56 = add_days(t, -56)
    return {
        'week': wk,
        'fixed_rows': fixed_rows,
        'net_change': chg(f_cur['net'], f_prev['net']),
        'stream_shares': stream_shares,
        'kpis': {
            'cpi': r3(c_est / c_act) if js_truthy(c_act) else None,
            'forecast_breach': len(br['forecast']),
            'breached_open': len(br['breached']),
            'red_projects': sum(1 for p in projects if p['health'] == 'off_track'),
            'active_projects': len(projects),
            'team_score': team_week_score(ctx, people, wk),
            'on_time_tasks': done_on_time / len(done) if done else None,
            'done_week': sum(1 for x in done if wk <= x['done_on'] <= wk_end),
        },
        'cash_ms': {
            'on_time': on_time_ms / len(achieved) if achieved else None,
            'achieved': len(achieved),
            'achieved_not_invoiced': achieved_not_invoiced,
            'invoiced_awaiting': invoiced_awaiting,
            'billable_30': billable30,
        },
        'finance': finance,
        'period': {'kind': kind, 'label': pr['label'], 'from': pr['from'], 'to': pr['to'], 'prev_label': prev['label']},
        'revenue': revenue,
        'period_rows': period_rows,
        'months': months,
        'streams': streams,
        'streams_label': f'{fy_label(sfy)} to date' if sfy == fy else fy_label(sfy),
        'ahead': {
            'in_30': within(30),
            'in_60': within(60),
            'in_90': within(90),
            'uninvoiced': uninvoiced,
            'uninvoiced_count': len(open_ms),
            'weighted_pipeline': pipe['weighted_value'],
            'open_pipeline': pipe['open_value'],
            'total': uninvoiced + pipe['weighted_value'],
        },
        'projects': projects,
        'delivery': {
            'milestones_on_time': on_time_ms / len(achieved) if achieved else None,
            'milestones_achieved': len(achieved),
            'milestones_on_time_count': on_time_ms,
            'avg_lateness': r1(late_sum / len(late_days)) if late_days else None,
            'late_tasks': len(late_days),
            'spi': r3(spi_sum / len(spis)) if spis else None,
            'overdue_tasks': sum(1 for x in tasks if x['status'] != 'done' and x['due_on'] < t and x['project_id'] in live_ids),
            'blocked_tasks': sum(1 for x in tasks if x['status'] == 'blocked' and x['project_id'] in live_ids),
            'tasks_on_time': done_on_time / len(done) if done else None,
            'done_4w': sum(1 for x in done if x['done_on'] > t28),
            'done_prev_4w': sum(1 for x in done if t56 < x['done_on'] <= t28),
            'utilisation': booked_sum / cap_sum if js_truthy(cap_sum) else util,
            'at_risk_projects': sum(1 for p in projects if p['health'] != 'on_track'),
            'active_projects': len(projects),
        },
        'milestones_30': milestones_30,
        'milestones_30_total': sum(m['billing'] if m['billing_status'] == 'none' else 0 for m in ms30),
        'approvals': approver_rows(ctx, add_days(week_start(t), -7 * 7)),
        'stale': {
            'count': len(pipe['stale']),
            'value': stale_value,
            'stale_days': stale_days,
            'deals': sorted([deal_lite(ctx, d, nm) for d in pipe['stale']], key=lambda d: d['days_idle'], reverse=True)[:6],
        },
        'cash': cash_position(ctx),
        'can_margins': cm,
    }


# ---------------------------------------------------------------------------------------------------------------------
# PM
# ---------------------------------------------------------------------------------------------------------------------
def _stage_def(x: dict) -> dict:
    return STAGE[stage_of(x)]


def pm_dash(ctx: Ctx, wk: str) -> dict[str, Any]:
    from app.routes.reports import my_projects, name_of, week_history
    from app.routes.taskflow import is_top, reviewer_of
    from app.routes.timesheets import can_decide

    if not can_delivery_insights(ctx):
        err(403, 'FORBIDDEN', 'The delivery dashboard is for people who manage projects.')
    t = ctx.today
    cm = can_see_margins(ctx)
    nm = name_of(ctx)
    mine = [p for p in my_projects(ctx) if p['status'] in LIVE]
    mine_ids = {p['id'] for p in mine}
    projects = sorted([pill(ctx, p, cm, nm) for p in mine], key=lambda p: (HEALTH_ORDER[p['health']], locale_key(p['name'])))
    mp = maps(ctx)
    # task lists use the same visibility rule as the Tasks screens (canSeeTask); aggregates below use all_mine
    tasks = [x for x in rows(ctx, 'tasks') if x['project_id'] in mine_ids and x['status'] != 'done' and _can_see_task(ctx, x)]
    due3 = add_days(t, 3)
    at_risk = []
    for x in tasks:
        if x['status'] != 'blocked' and (x['due_on'] < t or (x['due_on'] <= due3 and x['status'] == 'todo')):
            if x['due_on'] < t:
                reason = f'Overdue by {js_str(diff_days(t, x["due_on"]))}d'
            else:
                when = 'today' if x['due_on'] == t else f'in {js_str(diff_days(x["due_on"], t))}d'
                reason = f'Due {when}, not started'
            at_risk.append(task_row(ctx, x, mp, reason))
    at_risk = sorted(at_risk, key=lambda r: r['days'])
    blocked = sorted(
        [task_row(ctx, x, mp, coalesce(prop(x, 'blocked_reason'), 'Blocked')) for x in tasks if x['status'] == 'blocked'],
        key=lambda r: r['days'],
    )

    # resource load: allocation % per working day for the next 10
    people = pm_people(ctx)
    dates = next_working_days(ctx, 10)
    users = mp['users']
    load = []
    for pid in people:
        u = users.get(pid)
        if not u or not _is_ts_user(u) or u.get('role') not in ('employee', 'pm'):
            continue
        ds = [{'date': d, 'pct': allocation_on(ctx, u['id'], d) / 100} for d in dates]
        s: Any = 0
        for x in ds:
            s += x['pct']
        load.append({'id': u['id'], 'name': u['name'], 'hue': u['avatar_hue'], 'title': u['title'], 'avg': s / (len(ds) or 1), 'days': ds})
    load = sorted(load, key=lambda r: r['avg'], reverse=True)

    # approvals waiting on me
    entries_by_week = _tenant_group(ctx, 'timesheet_entries', 'week_id')
    approvals = []
    for w in rows(ctx, 'timesheet_weeks'):
        if w['status'] != 'submitted' or not can_decide(ctx, w)['ok']:
            continue
        due = sla_due(ctx, w['submitted_at']) if js_truthy(w.get('submitted_at')) else None
        u = users.get(w['user_id'])
        hrs: Any = 0
        for e in entries_by_week.get(w['id'], ()):
            hrs += e['hours']
        approvals.append(
            {
                'id': w['id'],
                'person': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
                'hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
                'week_start': w['week_start'],
                'hours': r1(hrs),
                'submitted_at': w.get('submitted_at'),
                'due': due,
                'overdue': js_truthy(due) and t > due,
            }
        )
    approvals = sorted(approvals, key=lambda a: (0 if a['overdue'] else 1, locale_key(a['week_start'])))

    see_value = has_any(ctx, 'change_requests.manage', 'finance.view')
    crs = []
    for c in rows(ctx, 'change_requests'):
        if c['project_id'] in mine_ids and (c['status'] == 'raised' or c['status'] == 'estimated'):
            proj = mp['projects'].get(c['project_id'])
            crs.append(
                {
                    'id': c['id'],
                    'project_id': c['project_id'],
                    'project': coalesce(proj.get('name', UNDEF) if proj else UNDEF, '—'),
                    'title': c['title'],
                    'status': c['status'],
                    'hours': c['hours'],
                    'value': prop(c, 'value_paise') if see_value else None,
                    'age_days': diff_days(t, c['created_at'][:10]),
                }
            )
    crs = sorted(crs, key=lambda c: c['age_days'], reverse=True)

    compliance = []
    for pid in people:
        u = users.get(pid)
        if not u or not _is_ts_user(u):
            continue
        h = week_history(ctx, u['id'], 8)
        ok = sum(1 for x in h if x['status'] == 'approved' or x['status'] == 'submitted')
        compliance.append(
            {'id': u['id'], 'name': u['name'], 'hue': u['avatar_hue'], 'last_week': h[-1]['status'], 'weeks': [x['status'] for x in h], 'pct': ok / len(h)}
        )
    compliance = sorted(compliance, key=lambda c: (c['pct'], locale_key(c['name'])))

    # ---- v5 delivery control, task pipeline, resource load in hours ----
    all_mine = [x for x in rows(ctx, 'tasks') if x['project_id'] in mine_ids]
    crit = critical_ids(ctx, all_mine)
    run = p50_finish(ctx)
    crit_risk = []
    for x in tasks:
        if x['id'] not in crit:
            continue
        group_ = _stage_def(x)['group']
        if not (x['due_on'] < t or x['status'] == 'blocked' or group_ == 'waiting' or run.get(x['id'], x['due_on']) > x['due_on']):
            continue
        if x['due_on'] < t:
            reason = f'Critical · overdue by {js_str(diff_days(t, x["due_on"]))}d'
        elif group_ == 'waiting':
            reason = f'Critical · {_stage_def(x)["label"].lower()}'
        else:
            reason = f'Critical · forecast {fmt_day(run[x["id"]])} vs target {fmt_day(x["due_on"])}'
        crit_risk.append(task_row(ctx, x, mp, reason))
    crit_risk = sorted(crit_risk, key=lambda r: locale_key(r['due_on']))
    by_id = {x['id']: x for x in all_mine}
    conflict_list: list[dict] = []
    for d in rows(ctx, 'task_dependencies'):
        a = by_id.get(d['task_id'])
        b = by_id.get(d['depends_on_task_id'])
        if not a or not b or a['status'] == 'done' or b['status'] == 'done':
            continue
        if a['start_on'] <= b['due_on']:
            ca = _task_code(mp['ms'].get(a.get('milestone_id')) if js_truthy(a.get('milestone_id')) else None, a)
            cb = _task_code(mp['ms'].get(b.get('milestone_id')) if js_truthy(b.get('milestone_id')) else None, b)
            conflict_list.append(
                {
                    'id': d['id'],
                    'task_id': a['id'],
                    'kind': 'dependency',
                    'message': f'{ca} {js_str(a["title"])} starts {fmt_day(a["start_on"])} but depends on {cb} {js_str(b["title"])}, which ends {fmt_day(b["due_on"])}.',
                }
            )
    dates10 = next_working_days(ctx, 10)
    load_people: dict[Any, dict] = {}
    for pid in [*people, *([ctx.user_id] if has(ctx, 'timesheet.own') else [])]:
        u = users.get(pid)
        if u and _is_ts_user(u):
            load_people[u['id']] = u
    load_hours_rows = sorted(load_hours(ctx, list(load_people.values()), dates10), key=lambda r: coalesce(r['pct'], 0), reverse=True)
    for lrow in load_hours_rows:
        if lrow['double_booked']:
            conflict_list.append(
                {
                    'id': f'db_{lrow["id"]}',
                    'task_id': '',
                    'kind': 'double_booked',
                    'message': f'{js_str(lrow["name"])} is double-booked: {js_str(lrow["booked"])}h of work against {js_str(lrow["capacity"])}h available in the next 10 working days.',
                }
            )
    control = {
        'active_projects': sum(1 for p in mine if p['status'] == 'active'),
        'critical_at_risk': len(crit_risk),
        'blocked_waiting': sum(1 for x in tasks if _stage_def(x)['group'] == 'waiting'),
        'past_start': sum(1 for x in tasks if _stage_def(x)['group'] == 'not_started' and x['start_on'] < t),
        'open_bugs': sum(1 for x in tasks if x.get('tag') == 'bug'),
        'in_testing': sum(1 for x in tasks if stage_of(x) in ('unit_testing', 'functional_testing')),
        'in_uat': sum(1 for x in tasks if stage_of(x) == 'uat'),
        'waiting_feedback': sum(1 for x in tasks if stage_of(x) == 'waiting_feedback'),
        'no_estimate': sum(1 for x in tasks if not (x['est_hours'] > 0)),
        'conflicts': len(conflict_list),
        'critical_list': crit_risk[:12],
        'conflict_list': conflict_list,  # every counted conflict is listed (count = list length)
    }
    # Task pipeline (Feature Guide §14): open tasks by stage, plus "In review"
    in_review = [x for x in tasks if x['status'] == 'in_review']
    pipeline_rows = [
        {
            'stage': st['key'],
            'label': st['label'],
            'count': sum(1 for x in tasks if x['status'] != 'in_review' and stage_of(x) == st['key']),
            'tone': st['tone'],
            'group': st['group'],
        }
        for st in TASK_STAGES
        if st['key'] != 'completed'
    ]
    idx = next((i for i, r in enumerate(pipeline_rows) if r['stage'] == 'waiting_feedback'), -1)
    pipeline_rows.insert(idx, {'stage': 'in_review', 'label': 'In review', 'count': len(in_review), 'tone': 'accent', 'group': 'review'})
    # "Needs attention" (v7 persona round)
    all_mine_by_ms = _by(all_mine, 'milestone_id')
    ms_forecast_late = 0
    for m in rows(ctx, 'milestones'):
        if js_truthy(m.get('deleted_at')) or js_truthy(m.get('achieved_on')) or m['project_id'] not in mine_ids:
            continue
        open_ = [x for x in all_mine_by_ms.get(m['id'], ()) if x['status'] != 'done']
        if not open_:
            continue
        f = '0000-01-01'
        for x in open_:
            v = run.get(x['id'], x['due_on'])
            if v > f:
                f = v
        if f > m['planned_end']:
            ms_forecast_late += 1
    me = ctx.user_id
    review_queue = 0
    for x in rows(ctx, 'tasks'):
        if x['status'] == 'in_review' and reviewer_of(ctx, x) == me and (x.get('owner_id') != me or is_top(ctx, me)):
            review_queue += 1
    attention = [
        # "overdue" matches the Tasks tab's Overdue chip: past the target end and not waiting for review
        {
            'key': 'overdue',
            'label': 'Overdue tasks',
            'count': sum(1 for x in tasks if x['due_on'] < t and x['status'] != 'in_review'),
            'to': '/tasks?chip=overdue',
            'hint': 'Open tasks past their target end on your projects (tasks waiting for review not counted).',
        },
        {
            'key': 'blocked',
            'label': 'Tasks blocked',
            'count': len(blocked),
            'to': '/insights/blocked',
            'hint': 'Marked blocked or waiting on an open dependency.',
        },
        {'key': 'timesheets', 'label': 'Timesheets waiting', 'count': len(approvals), 'to': '/approvals', 'hint': 'Submitted weeks waiting for your approval.'},
        {
            'key': 'milestones',
            'label': 'Milestones at risk',
            'count': ms_forecast_late,
            'to': '/delivery/milestones',
            'hint': 'Open milestones whose P50 forecast finish passes the deadline.',
        },
        {'key': 'reviews', 'label': 'Reviews waiting on me', 'count': review_queue, 'to': '/team-tasks?tab=review', 'hint': 'Tasks submitted for your review.'},
        {
            'key': 'crs',
            'label': 'CRs pending',
            'count': len(crs),
            'to': '/delivery/change-requests',
            'hint': 'Change requests raised or estimated, awaiting a decision.',
        },
    ]
    comp_sum: Any = 0
    for c in compliance:
        comp_sum += c['pct']
    wk_end = add_days(wk, 6)
    return {
        'week': wk,
        'control': control,
        'attention': attention,
        'pipeline': pipeline_rows,
        'done_count': sum(1 for x in all_mine if x['status'] == 'done'),
        'load_hours': {'from': dates10[0] if dates10 else t, 'to': dates10[-1] if dates10 else t, 'days': len(dates10), 'people': load_hours_rows},
        'due_week': sum(1 for x in tasks if wk <= x['due_on'] <= wk_end),
        'projects': projects,
        'kpis': {
            'active': len(projects),
            'at_risk': sum(1 for p in projects if p['health'] != 'on_track'),
            'overdue_tasks': sum(1 for x in tasks if x['due_on'] < t),
            'blocked': len(blocked),
            'due_3d': sum(1 for x in tasks if t <= x['due_on'] <= due3),
            'pending_approvals': len(approvals),
            'open_crs': len(crs),
            'compliance': comp_sum / len(compliance) if compliance else None,
        },
        'at_risk': at_risk[:25],
        'blocked': blocked,
        'load': {'dates': dates, 'people': load},
        'approvals': approvals,
        'change_requests': crs,
        'compliance': compliance,
        'milestones': milestone_rows(ctx, mine, add_days(t, -3650), add_days(t, 45))[:10],
    }


# ---------------------------------------------------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------------------------------------------------
_NEEDS_ORDER = {'overdue_close': 0, 'stale': 1, 'next_step': 2, 'closing': 3}


def sales_dash(ctx: Ctx) -> dict[str, Any]:
    from app.routes.reports import name_of

    if not has(ctx, 'deals.edit_own'):
        err(403, 'FORBIDDEN', 'The sales dashboard is for sales and admins.')
    t = ctx.today
    # deals.view_all → the whole pipeline; otherwise only their own deals.
    mine_only = not has(ctx, 'deals.view_all')
    owner = ctx.user_id if mine_only else UNDEF
    # The team's target and variable pay are pay data (cost_rates.view); without it show their own target.
    team_target = not mine_only and can_see_salaries(ctx)
    nm = name_of(ctx)
    p = pipeline(ctx, owner)
    deals = [d for d in rows(ctx, 'deals') if not js_truthy(owner) or d.get('owner_id') == owner]
    open_ = [d for d in deals if d['stage'] != 'won' and d['stage'] != 'lost']
    by_stage = []
    for s in ('lead', 'qualified', 'demo', 'proposal', 'negotiation'):
        ds = [d for d in open_ if d['stage'] == s]
        v: Any = 0
        w: Any = 0
        for d in ds:
            v += deal_value(d)
        for d in ds:
            w += deal_value(d) * DEAL_STAGE[s]['chance']
        by_stage.append({'stage': s, 'label': DEAL_STAGE[s]['label'], 'count': len(ds), 'value': v, 'weighted': js_round(w)})
    st = _S(ctx)
    stale_days = st['sales.stale_deal_days']
    m_end = month_range(month_of(t))['to']
    needs = []
    for d in open_:
        lite = deal_lite(ctx, d, nm)
        nso = d.get('next_step_on')
        if d['expected_close'] < t:
            needs.append(
                {
                    **lite,
                    'reason': 'overdue_close',
                    'reason_text': f'Expected close passed {js_str(diff_days(t, d["expected_close"]))}d ago — update the date or stage',
                }
            )
        elif lite['days_idle'] >= stale_days:
            needs.append({**lite, 'reason': 'stale', 'reason_text': f'No activity for {js_str(lite["days_idle"])} days'})
        elif js_truthy(nso) and nso < t:
            needs.append(
                {
                    **lite,
                    'reason': 'next_step',
                    'reason_text': f'Next step “{js_str(coalesce(prop(d, "next_step"), "follow-up"))}” was due {js_str(diff_days(t, nso))}d ago',
                }
            )
        elif d['expected_close'] <= m_end:
            needs.append({**lite, 'reason': 'closing', 'reason_text': f'Closing this month ({fmt_day(d["expected_close"])})'})
    needs = sorted(needs, key=lambda n: (_NEEDS_ORDER[n['reason']], -n['value']))

    target: Any = None
    history: list[dict] = []
    ym = month_of(t)
    threshold = st['pay.variable_threshold_pct']
    if not team_target:
        c = comp_for(ctx, ctx.user_id, ym)
        target = {
            'month_label': c['label'],
            'target': c['target'],
            'achieved': c['achieved'],
            'achievement': c['achievement'],
            'threshold_pct': threshold,
            'hit': c['hit'],
            'variable': c['variable'],
            'variable_earned': c['variable_earned'],
            'has_rate': c['has_rate'],
        }
        for i in range(5, -1, -1):
            m = month_of(add_months(ym + '-01', -i))
            x = comp_for(ctx, ctx.user_id, m)
            history.append({'label': x['label'], 'target': x['target'], 'achieved': x['achieved'], 'hit': x['hit']})
    else:
        sellers = [u for u in rows(ctx, 'users') if u.get('role') == 'sales' and u.get('status') == 'active']

        def agg(m: str) -> dict[str, Any]:
            s = {'target': 0, 'achieved': 0, 'variable': 0, 'earned': 0, 'label': month_label(m)}
            for u in sellers:
                c = comp_for(ctx, u['id'], m)
                s = {
                    'target': s['target'] + c['target'],
                    'achieved': s['achieved'] + c['achieved'],
                    'variable': s['variable'] + c['variable'],
                    'earned': s['earned'] + c['variable_earned'],
                    'label': c['label'],
                }
            return s

        c = agg(ym)
        ach = c['achieved'] / c['target'] if js_truthy(c['target']) else 0
        target = {
            'month_label': c['label'],
            'target': c['target'],
            'achieved': c['achieved'],
            'achievement': ach,
            'threshold_pct': threshold,
            'hit': ach * 100 >= threshold,
            'variable': c['variable'],
            'variable_earned': c['earned'],
            'has_rate': len(sellers) > 0,
        }
        for i in range(5, -1, -1):
            m = month_of(add_months(ym + '-01', -i))
            x = agg(m)
            history.append(
                {
                    'label': x['label'],
                    'target': x['target'],
                    'achieved': x['achieved'],
                    'hit': x['target'] > 0 and (x['achieved'] / x['target']) * 100 >= threshold,
                }
            )
    deal_map = {d['id']: d for d in rows(ctx, 'deals')}
    acts = [a for a in rows(ctx, 'deal_activities') if (not js_truthy(owner) or a['by'] == owner) and a['deal_id'] in deal_map]
    top = sorted(acts, key=lambda a: (locale_key(a['date']), locale_key(a['created_at'])), reverse=True)[:10]
    activities = [
        {
            'id': a['id'],
            'date': a['date'],
            'type': a['type'],
            'deal_id': a['deal_id'],
            'deal': deal_map[a['deal_id']]['title'],
            'client': deal_map[a['deal_id']]['client'],
            'note': a['note'],
            'by': nm(a['by']),
        }
        for a in top
    ]
    t30 = add_days(t, -30)
    return {
        'scope': 'mine' if mine_only else 'team',
        'target_scope': 'team' if team_target else 'mine',
        'summary': {
            'open_value': p['open_value'],
            'weighted_value': p['weighted_value'],
            'open_count': p['open_count'],
            'won_value': p['won_value'],
            'won_count': p['won_count'],
            'win_rate': p['win_rate'],
            'avg_cycle_days': None if p['avg_cycle_days'] is None else js_round(p['avg_cycle_days']),
            'stale_count': len(p['stale']),
        },
        'by_stage': by_stage,
        'needs_action': needs[:12],
        'target': target,
        'history': history,
        'activities': activities,
        'activity_30d': sum(1 for a in acts if a['date'] > t30),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------------------------------------------------
def ops_dash(ctx: Ctx) -> dict[str, Any]:
    from app.routes.reports import effective_week_status, name_of

    if not has(ctx, 'ops.manage'):
        err(403, 'FORBIDDEN', 'The operations dashboard is for operations and admins.')
    t = ctx.today
    nm = name_of(ctx)
    t7 = add_days(t, 7)
    t30 = add_days(t, 30)
    items = []
    for o in rows(ctx, 'ops_items'):
        if o['status'] != 'open' or not o['due_on'] <= t30:
            continue
        items.append(
            {
                'id': o['id'],
                'item': o['item'],
                'category': o['category'],
                'owner': nm(o.get('owner_id')),
                'due_on': o['due_on'],
                'days': diff_days(o['due_on'], t),
                'recurring': o['recurring'],
                'group': 'overdue' if o['due_on'] < t else 'due' if o['due_on'] <= t7 else 'upcoming',
            }
        )
    items = sorted(items, key=lambda i: locale_key(i['due_on']))
    t60 = add_days(t, 60)
    tm30 = add_days(t, -30)
    compliance = [
        {'id': o['id'], 'item': o['item'], 'due_on': o['due_on'], 'days': diff_days(o['due_on'], t), 'owner': nm(o.get('owner_id')), 'status': o['status']}
        for o in rows(ctx, 'ops_items')
        if o['category'] in ('compliance', 'payroll', 'finance')
        and o['due_on'] <= t60
        and (o['status'] == 'open' or (js_truthy(o.get('last_done_on')) and o['last_done_on'] >= tm30))
    ]
    compliance = sorted(compliance, key=lambda i: locale_key(i['due_on']))
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    op = [o for o in rows(ctx, 'openings') if o['status'] != 'filled']
    users = users_map(ctx)
    leave_today = []
    for lrow in rows(ctx, 'leave'):
        if lrow['date'] != t:
            continue
        u = users.get(lrow['user_id'])
        leave_today.append(
            {
                'id': lrow['user_id'],
                'name': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
                'hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
                'type': lrow['type'],
                'portion': lrow['portion'],
            }
        )
    runs = rows(ctx, 'payroll_runs')
    ym = month_of(t)
    lm = month_of(add_months(ym + '-01', -1))

    def run_of(m: str) -> dict | None:
        return next((r for r in runs if r['month'] == m), None)

    cur = run_of(ym)
    last = run_of(lm)
    people = [u for u in rows(ctx, 'users') if _is_ts_user(u)]
    last_mon = add_days(week_start(t), -7)
    weeks = rows(ctx, 'timesheet_weeks')
    week_by_user: dict[Any, dict] = {}
    for w in weeks:
        if w['week_start'] == last_mon:
            week_by_user.setdefault(w['user_id'], w)
    missing = 0
    for u in people:
        s = effective_week_status(ctx, week_by_user.get(u['id']), last_mon)
        if s != 'approved' and s != 'submitted':
            missing += 1
    positions: Any = 0
    for o in op:
        if o['status'] != 'on_hold':
            positions += o['count']
    openings = []
    for o in op:
        pid = o.get('project_id')
        proj = projects.get(pid) if js_truthy(pid) else None
        openings.append(
            {
                'id': o['id'],
                'role': o['role'],
                'project': coalesce(proj.get('name', UNDEF) if proj else UNDEF, None) if js_truthy(pid) else None,
                'count': o['count'],
                'status': o['status'],
                'needed_by': o.get('needed_by'),
                'owner': nm(o.get('owner_id')),
            }
        )
    openings = sorted(openings, key=lambda o: locale_key(coalesce(o['needed_by'], '9')))
    return {
        'counts': {
            'overdue': sum(1 for i in items if i['group'] == 'overdue'),
            'due_7': sum(1 for i in items if i['group'] == 'due'),
            'upcoming_30': sum(1 for i in items if i['group'] == 'upcoming'),
            'openings': len(op),
            'positions': positions,
            'on_leave_today': len(leave_today),
        },
        'items': items,
        'compliance': compliance,
        'openings': openings,
        'leave_today': leave_today,
        'payroll': {
            'month': ym,
            'label': month_label(ym),
            'status': cur['status'] if cur else 'not_started',
            'run_id': coalesce(cur.get('id', UNDEF) if cur else UNDEF, None),
            'last_month': {
                'label': month_label(lm),
                'status': last['status'] if last else 'not_started',
                'run_id': coalesce(last.get('id', UNDEF) if last else UNDEF, None),
            },
        },
        'timesheets': {'missing_last_week': missing, 'pending_approval': sum(1 for w in weeks if w['status'] == 'submitted'), 'people': len(people)},
    }


# ---------------------------------------------------------------------------------------------------------------------
# Employee
# ---------------------------------------------------------------------------------------------------------------------
def viewable_people(ctx: Ctx) -> list[dict]:
    """People whose employee view the viewer may open: everyone (people.view_all), their team and project people
    (team.view / projects.edit), else only themselves."""
    all_ = [u for u in rows(ctx, 'users') if _is_ts_user(u) or u['id'] == ctx.user_id]
    if has(ctx, 'people.view_all'):
        return all_
    if has_any(ctx, 'team.view', 'projects.edit'):
        s = pm_people(ctx)
        s[ctx.user_id] = None
        return [u for u in all_ if u['id'] in s]
    return [u for u in all_ if u['id'] == ctx.user_id]


def employee_dash(ctx: Ctx, user_q: Any, wk_in: str) -> dict[str, Any]:
    from app.routes.reports import effective_week_status

    t = ctx.today
    allowed = viewable_people(ctx)
    want = user_q if isinstance(user_q, str) and user_q else ctx.user_id
    if want != ctx.user_id and not any(u['id'] == want for u in allowed):
        err(403, 'FORBIDDEN', 'You can only open the employee view of people in your team.')
    person = next((u for u in rows(ctx, 'users') if u['id'] == want), None)
    if person is None:
        err(404, 'NOT_FOUND', 'That person doesn’t exist.')
    assert person is not None
    me = person['id']
    wk = wk_in
    mp = maps(ctx)
    all_ = rows(ctx, 'tasks')
    mine = [x for x in all_ if x.get('owner_id') == me]
    open_ = [x for x in mine if x['status'] != 'done' and _can_see_task(ctx, x)]
    wk_end = add_days(wk, 6)
    wk_from = wk if wk > t else add_days(t, 1)
    deps = rows(ctx, 'task_dependencies')
    deps_by_task = _by(deps, 'task_id')
    deps_by_pred = _by(deps, 'depends_on_task_id')
    by_id = {x['id']: x for x in all_}
    today = [task_row(ctx, x, mp) for x in open_ if x['due_on'] == t or (x['status'] == 'in_progress' and t <= x['due_on'] <= t)]
    week = sorted([task_row(ctx, x, mp) for x in open_ if wk_from <= x['due_on'] <= wk_end], key=lambda r: locale_key(r['due_on']))
    overdue = sorted(
        [task_row(ctx, x, mp, f'{js_str(diff_days(t, x["due_on"]))}d overdue') for x in open_ if x['due_on'] < t],
        key=lambda r: locale_key(r['due_on']),
    )

    def dep_status(d: dict) -> Any:
        p = by_id.get(d['depends_on_task_id'])
        return p['status'] if p is not None else UNDEF

    blocked = []
    for x in open_:
        if not (x['status'] == 'blocked' or any(dep_status(d) != 'done' for d in deps_by_task.get(x['id'], ()))):
            continue
        waits = [p for p in (by_id.get(d['depends_on_task_id']) for d in deps_by_task.get(x['id'], ())) if p and p['status'] != 'done']
        if waits:
            wu = mp['users'].get(coalesce(prop(waits[0], 'owner_id'), ''))
            alt = f'Waiting on “{js_str(waits[0]["title"])}” ({js_str(coalesce(wu.get("name", UNDEF) if wu else UNDEF, "unassigned"))})'
        else:
            alt = 'Blocked'
        blocked.append(task_row(ctx, x, mp, coalesce(prop(x, 'blocked_reason'), alt)))
    waiting_on_me = []
    for x in open_:
        succ = [s for s in (by_id.get(d['task_id']) for d in deps_by_pred.get(x['id'], ())) if s and s['status'] != 'done' and s.get('owner_id') != me]
        waiting = []
        for s in succ:
            su = mp['users'].get(coalesce(prop(s, 'owner_id'), ''))
            waiting.append({'id': s['id'], 'title': s['title'], 'owner': coalesce(su.get('name', UNDEF) if su else UNDEF, None)})
        if waiting:
            waiting_on_me.append({**task_row(ctx, x, mp), 'waiting': waiting})

    # the viewed week's timesheet
    mon = wk
    w = next((x for x in rows(ctx, 'timesheet_weeks') if x['user_id'] == me and x['week_start'] == mon), None)
    es = list(_tenant_group(ctx, 'timesheet_entries', 'week_id').get(w['id'], ())) if w else []
    base = cal(ctx)
    c = CalendarCtx(base.settings, base.holidays, {lrow['date'] for lrow in _tenant_group(ctx, 'leave', 'user_id').get(me, ()) if lrow['portion'] == 'full'})
    st = _S(ctx)
    min_h = st['calendar.min_hours_per_day']
    days = []
    for d in week_days(mon):
        working = is_working_day(d, c)
        h: Any = 0
        for e in es:
            if e['work_date'] == d:
                h += e['hours']
        days.append({'date': d, 'hours': r1(h), 'working': working, 'min': min_h if working else 0, 'future': d > t})

    # delivery stats
    act = mp['act']
    done = [x for x in mine if x['status'] == 'done' and js_truthy(x.get('done_on'))]
    ratios = [x for x in done if x['est_hours'] > 0 and _hours(act, x['id']) > 0]
    est_sum: Any = 0
    act_sum: Any = 0
    for x in ratios:
        est_sum += x['est_hours']
    for x in ratios:
        act_sum += _hours(act, x['id'])
    next10 = next_working_days(ctx, 10)
    ups = sorted([d for d in rows(ctx, 'daily_updates') if d['user_id'] == me], key=lambda d: locale_key(d['date']), reverse=True)
    total: Any = 0
    for d in days:
        total += d['hours']
    alloc: Any = 0
    for d in next10:
        alloc += allocation_on(ctx, me, d)
    t28 = add_days(t, -28)
    t56 = add_days(t, -56)
    return {
        'today': today,
        'week': week,
        'overdue': overdue,
        'blocked': blocked,
        'waiting_on_me': waiting_on_me,
        'timesheet': {
            'week_start': mon,
            'status': effective_week_status(ctx, w, mon) if w else 'missing',
            'total': r1(total),
            'min_per_day': min_h,
            'expected_to_date': sum(1 for d in days if d['date'] < t and d['working']) * min_h,
            'days': days,
        },
        'stats': {
            'effort_ratio': r3(act_sum / est_sum) if js_truthy(est_sum) else None,
            'on_time': sum(1 for x in done if x['done_on'] <= x['due_on']) / len(done) if done else None,
            'done_4w': sum(1 for x in done if x['done_on'] > t28),
            'done_prev_4w': sum(1 for x in done if t56 < x['done_on'] <= t28),
            'open': len(open_),
            'ratio_samples': len(ratios),
            'allocation_10d': alloc / (len(next10) or 1) / 100,
        },
        'daily': {
            'submitted_today': (ups[0]['date'] if ups else UNDEF) == t,
            'last_date': ups[0]['date'] if ups else None,
            'proof_required': prop(st, 'daily.proof_required'),
        },
        **employee_numbers(ctx, person, wk, allowed),
    }


def employee_numbers(ctx: Ctx, person: dict, wk: str, allowed: list[dict]) -> dict[str, Any]:
    t = ctx.today
    mine = [x for x in rows(ctx, 'tasks') if x.get('owner_id') == person['id']]
    done = [x for x in mine if x['status'] == 'done' and js_truthy(x.get('done_on'))]
    open_ = [x for x in mine if x['status'] != 'done']
    own = ratio_samples(ctx)['byPerson'].get(person['id'], [])
    # Average lateness: working days late over completed tasks that finished after their target end
    late = [v for v in (working_day_variance(ctx, x['due_on'], x['done_on']) for x in done if x['done_on'] > x['due_on']) if v > 0]
    loads = load_hours(ctx, [person], next_working_days(ctx, 10))
    load = loads[0] if loads else None
    sc = person_week_score(ctx, person, wk)
    me = ctx.user_id

    def cmp(a: dict, b: dict) -> Any:
        if a['id'] == me:
            return -1
        if b['id'] == me:
            return 1
        return locale_compare(a['name'], b['name'])

    late_sum: Any = 0
    for v in late:
        late_sum += v
    return {
        'viewed_week': wk,
        'person': {'id': person['id'], 'name': person['name'], 'title': person['title'], 'hue': person['avatar_hue']},
        'is_self': person['id'] == me,
        'viewable': [{'id': u['id'], 'name': u['name'], 'title': u['title']} for u in js_sort(allowed, cmp)],
        'mentions': mentions_of(ctx, person['id']),
        'covering': covering_of(ctx, person['id'], wk),
        'numbers': {
            'score': sc['score'],
            'score_cells': sc['cells'],
            'on_time': sum(1 for x in done if x['done_on'] <= x['due_on']) / len(done) if done else None,
            'effort_ratio': r3(median(own)) if own else None,
            'utilisation': coalesce(load.get('pct', UNDEF) if load else UNDEF, None),
            'booked': coalesce(load.get('booked', UNDEF) if load else UNDEF, 0),
            'capacity': coalesce(load.get('capacity', UNDEF) if load else UNDEF, 0),
            'open': len(open_),
            'overdue': sum(1 for x in open_ if x['due_on'] < t),
            'avg_lateness': r1(late_sum / len(late)) if late else None,
            'forecast_breach': sum(1 for x in breach_sets(ctx)['forecast'] if x.get('owner_id') == person['id']),
        },
    }


# ---------------------------------------------------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------------------------------------------------
def scope_projects(ctx: Ctx) -> list[dict]:
    """projects.view_all → every live project; otherwise the projects they PM / work on."""
    from app.routes.reports import my_projects

    return [p for p in my_projects(ctx) if p['status'] in LIVE]


def scope_people(ctx: Ctx) -> list[dict]:
    """people.view_all → everyone; otherwise reports + people on their projects."""
    all_ = [u for u in rows(ctx, 'users') if _is_ts_user(u)]
    if has(ctx, 'people.view_all'):
        return all_
    s = pm_people(ctx)
    s[ctx.user_id] = None
    return [u for u in all_ if u['id'] in s]


def can_delivery_insights(ctx: Ctx) -> bool:
    """Delivery insights / PM dashboard: people who manage projects."""
    return has_any(ctx, 'projects.edit', 'projects.manage_all')


def views_for(ctx: Ctx) -> list[str]:
    out: list[str] = []
    delivery = can_delivery_insights(ctx)
    people = delivery or has(ctx, 'people.view_all')
    if delivery:
        out += ['tasks_at_risk', 'blocked', 'overdue_milestones', 'effort_ratio']
    if people:
        out += ['allocation', 'timesheet_compliance', 'approval_kpi']
    if has(ctx, 'deals.edit_own'):
        out.append('stale_deals')
    if delivery:
        out += ['predictions', 'estimation']
    return out


LABELS: dict[str, tuple[str, str]] = {
    'tasks_at_risk': ('Tasks at risk', 'delivery'),
    'blocked': ('Blocked', 'delivery'),
    'overdue_milestones': ('Overdue milestones', 'delivery'),
    'effort_ratio': ('Effort overruns', 'delivery'),
    'allocation': ('Allocation', 'people'),
    'timesheet_compliance': ('Timesheet compliance', 'people'),
    'approval_kpi': ('Approval KPI', 'people'),
    'stale_deals': ('Stale deals', 'sales'),
    'predictions': ('Delivery predictions', 'prediction'),
    'estimation': ('Estimation accuracy', 'prediction'),
}


def quant(a: list, q: float) -> Any:
    if not a:
        return 1
    s = sorted(a)
    i = (len(s) - 1) * q
    lo = math.floor(i)
    hi = math.ceil(i)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def median(a: list) -> Any:
    return quant(a, 0.5)


def fmt_pct0(v: Any) -> str:
    return f'{js_str(js_round(v * 100))}%'


def ratio_samples(ctx: Ctx) -> dict[str, Any]:
    """Effort-ratio samples: actual ÷ estimate on done tasks with both (memoised per request; pure)."""

    def f() -> dict[str, Any]:
        act = task_actuals(ctx)
        by_person: dict[Any, list] = {}
        by_cx: dict[Any, list] = {}
        team: list = []
        for t in rows(ctx, 'tasks'):
            if t['status'] != 'done' or not js_truthy(t.get('owner_id')) or t['est_hours'] <= 0:
                continue
            a = _hours(act, t['id'])
            if a <= 0:
                continue
            r = a / t['est_hours']
            team.append(r)
            by_person.setdefault(t['owner_id'], []).append(r)
            by_cx.setdefault(t.get('complexity'), []).append(r)
        return {'byPerson': by_person, 'byCx': by_cx, 'team': team}

    return _once(ctx, 'ratio_samples', f)


QS = [0.2, 0.35, 0.5, 0.65, 0.8, 0.9]


def simulate(ctx: Ctx, q: float, samples: dict[str, Any]) -> dict[str, str]:
    """Prototype §15.1: re-run each person's queue with their own effort ratio at quantile q (see the TS doc)."""
    c = cal(ctx)
    act = task_actuals(ctx)
    cap = prop(_S(ctx), 'calendar.min_hours_per_day') * 0.7
    team = samples['team']

    def ratio_of(pid: Any) -> Any:
        own = samples['byPerson'].get(pid, [])
        return quant(own, q) if len(own) >= 3 else quant(team, q) if len(team) >= 3 else 1

    tasks = [t for t in rows(ctx, 'tasks') if t['status'] != 'done']
    finish: dict[str, str] = {}
    by_owner: dict[Any, list] = {}
    for t in tasks:
        by_owner.setdefault(coalesce(prop(t, 'owner_id'), '~'), []).append(t)
    nw_cache: dict[str, str] = {}

    def next_work(d: str) -> str:
        r = nw_cache.get(d)
        if r is not None:
            return r
        x = d
        g = 0
        while not is_working_day(x, c):
            gg = g
            g += 1
            if not gg < 30:
                break
            x = add_days(x, 1)
        nw_cache[d] = x
        return x

    leave_of: dict[Any, set] = {}
    for lrow in rows(ctx, 'leave'):
        if lrow['portion'] == 'full':
            leave_of.setdefault(lrow['user_id'], set()).add(lrow['date'])
    today = ctx.today
    for pid, lst in by_owner.items():
        lst = sorted(lst, key=lambda t: (locale_key(t['due_on']), locale_key(t['start_on'])))
        ratio = 1 if pid == '~' else ratio_of(pid)
        lv = leave_of.get(pid, set())
        day = next_work(today)
        left = cap
        for t in lst:
            st = next_work(t['start_on']) if t['start_on'] > day else day
            if st > day:
                day = st
                left = cap
            h = js_max(0.5, t['est_hours'] * ratio - _hours(act, t['id']))
            guard = 0
            while h > 0:
                g = guard
                guard += 1
                if not g < 800:
                    break
                if day in lv:
                    day = next_work(add_days(day, 1))
                    left = cap
                    continue
                take = js_min(h, left)
                h -= take
                left -= take
                if h > 0:
                    day = next_work(add_days(day, 1))
                    left = cap
            finish[t['id']] = day
            if left <= 0.01:
                day = next_work(add_days(day, 1))
                left = cap
    # dependency push (one ordered pass by due date)
    deps_by_task = _tenant_group(ctx, 'task_dependencies', 'task_id')
    order = sorted(tasks, key=lambda t: locale_key(t['due_on']))
    for _pass in range(2):
        for t in order:
            preds = [x for x in (finish.get(d['depends_on_task_id']) for d in deps_by_task.get(t['id'], ())) if x]
            if not preds:
                continue
            pm = preds[0]
            for x in preds[1:]:
                if x > pm:
                    pm = x
            cur = finish[t['id']]
            rem_days = js_max(1, math.ceil(js_max(0.5, t['est_hours'] - _hours(act, t['id'])) / cap))
            f = next_work(add_days(pm, 1))
            for _i in range(1, rem_days):
                f = next_work(add_days(f, 1))
            if f > cur:
                finish[t['id']] = f
    return finish


_TASK_COLS = [
    {'key': 'task', 'label': 'Task', 'type': 'task'},
    {'key': 'project', 'label': 'Project', 'type': 'project', 'hideBelow': 'lg'},
    {'key': 'owner', 'label': 'Owner', 'type': 'person'},
    {'key': 'status', 'label': 'Status', 'type': 'badge', 'hideBelow': 'md'},
    {'key': 'due_on', 'label': 'Due', 'type': 'date', 'align': 'right'},
]


def insight(ctx: Ctx, view: str) -> dict[str, Any]:
    from app.routes.reports import name_of, week_history

    allowed = views_for(ctx)
    if view not in allowed:
        err(403, 'FORBIDDEN', 'This insight isn’t available for your role.')
    t = ctx.today
    mp = maps(ctx)
    projects = scope_projects(ctx)
    pids = {p['id'] for p in projects}
    st = _S(ctx)
    alert = st['projects.effort_ratio_alert']
    open_tasks = [x for x in rows(ctx, 'tasks') if x['project_id'] in pids and x['status'] != 'done']

    def person(id_: Any) -> Any:
        return mp['users'].get(id_) if js_truthy(id_) else None

    task_cols = _TASK_COLS

    def trow(x: dict, extra: dict | None = None) -> dict[str, Any]:
        mid = x.get('milestone_id')
        ms = mp['ms'].get(mid) if js_truthy(mid) else None
        u = person(x.get('owner_id'))
        proj = mp['projects'].get(x['project_id'])
        return {
            'id': x['id'],
            'task': f'{_task_code(ms, x)} {js_str(x["title"])}',
            'task_id': x['id'],
            'project': coalesce(proj.get('name', UNDEF) if proj else UNDEF, '—'),
            'project_id': x['project_id'],
            'owner': coalesce(u.get('name', UNDEF) if u else UNDEF, 'Unassigned'),
            'owner_hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
            'status': TASK_STATUS[x['status']]['label'],
            'status_tone': TASK_STATUS[x['status']]['tone'],
            'due_on': x['due_on'],
            **(extra or {}),
        }

    base: dict[str, Any] = {'view': view, 'kpis': []}

    def hrs_left(x: dict) -> Any:
        return js_max(0, x['est_hours'] - _hours(mp['act'], x['id']))

    if view == 'tasks_at_risk':
        t3 = add_days(t, 3)
        lst = sorted([x for x in open_tasks if x['status'] == 'todo' and t <= x['due_on'] <= t3], key=lambda x: locale_key(x['due_on']))
        overdue_not_started = sum(1 for x in open_tasks if x['status'] == 'todo' and x['due_on'] < t)
        left: Any = 0
        for x in lst:
            left += hrs_left(x)
        return {
            **base,
            'title': 'Tasks at risk',
            'explanation': 'Open tasks due within the next 3 days that nobody has started. Start them today, move the date, or reassign.',
            'kpis': [
                {'label': 'At risk', 'value': len(lst), 'format': 'number', 'tone': 'warning' if lst else 'success'},
                {'label': 'Estimated hours left', 'value': r1(left), 'format': 'hours'},
                {'label': 'Already overdue, not started', 'value': overdue_not_started, 'format': 'number', 'tone': 'danger' if overdue_not_started else UNDEF},
            ],
            'columns': [
                *task_cols,
                {'key': 'days', 'label': 'Days left', 'type': 'days', 'align': 'right'},
                {'key': 'est', 'label': 'Estimate', 'type': 'hours', 'align': 'right', 'hideBelow': 'lg'},
            ],
            'rows': [trow(x, {'days': diff_days(x['due_on'], t), 'est': x['est_hours']}) for x in lst],
            'empty': 'Nothing due in the next 3 days is waiting to start.',
            'ask': 'Which tasks are due in the next 3 days and not started?',
        }
    if view == 'blocked':
        deps_by_task = _tenant_group(ctx, 'task_dependencies', 'task_id')
        by_id = {x['id']: x for x in rows(ctx, 'tasks')}
        lst = [x for x in open_tasks if x['status'] == 'blocked']

        def since(x: dict) -> Any:
            return diff_days(t, x['updated_at'][:10])

        stuck: Any = 0
        for x in lst:
            stuck += x['est_hours']
        out_rows = []
        for x in lst:
            w = next((p for p in (by_id.get(d['depends_on_task_id']) for d in deps_by_task.get(x['id'], ())) if p and p['status'] != 'done'), None)
            if w:
                wo = person(w.get('owner_id'))
                alt = f'Waiting on “{js_str(w["title"])}” ({js_str(coalesce(wo.get("name", UNDEF) if wo else UNDEF, "unassigned"))})'
            else:
                alt = '—'
            out_rows.append(trow(x, {'reason': js_or(prop(x, 'blocked_reason'), alt), 'since': since(x)}))
        out_rows = sorted(out_rows, key=lambda r: r['since'], reverse=True)
        return {
            **base,
            'title': 'Blocked tasks',
            'explanation': 'Tasks marked Blocked, with the reason and the open dependency they wait on. Clear the oldest first — each day blocked pushes the milestone.',
            'kpis': [
                {'label': 'Blocked', 'value': len(lst), 'format': 'number', 'tone': 'danger' if lst else 'success'},
                {'label': 'Hours stuck', 'value': r1(stuck), 'format': 'hours'},
                {'label': 'Longest blocked', 'value': js_max(*[since(x) for x in lst]) if lst else None, 'format': 'days'},
            ],
            'columns': [
                *task_cols[0:3],
                {'key': 'reason', 'label': 'Reason / waiting on', 'type': 'string', 'hideBelow': 'md'},
                {'key': 'since', 'label': 'Blocked for', 'type': 'days', 'align': 'right'},
                task_cols[4],
            ],
            'rows': out_rows,
            'empty': 'No blocked tasks. Nothing is waiting on anyone.',
            'ask': 'What is blocked right now and why?',
        }
    if view == 'overdue_milestones':
        lst = []
        for m in rows(ctx, 'milestones'):
            if js_truthy(m.get('deleted_at')) or m['project_id'] not in pids or js_truthy(m.get('achieved_on')):
                continue
            s_ = _ms_state(ctx, m)
            if s_['state'] == 'overdue':
                lst.append({'m': m, 'st': s_})
        lst = sorted(lst, key=lambda x: locale_key(x['m']['planned_end']))
        billing: Any = 0
        for x in lst:
            billing += x['m']['billing_amount_paise'] if x['m']['billing_status'] == 'none' else 0
        money = has(ctx, 'finance.view') or can_see_margins(ctx)
        open_in: Any = 0
        for x in lst:
            open_in += x['st']['openCount']
        return {
            **base,
            'title': 'Overdue milestones',
            'explanation': 'Milestones past their planned end with work still open. Billing on these is delayed until they are achieved.',
            'kpis': [
                {'label': 'Overdue', 'value': len(lst), 'format': 'number', 'tone': 'danger' if lst else 'success'},
                *([{'label': 'Billing held up', 'value': billing, 'format': 'money', 'tone': 'warning' if js_truthy(billing) else UNDEF}] if money else []),
                {'label': 'Open tasks inside', 'value': open_in, 'format': 'number'},
            ],
            'columns': [
                {'key': 'milestone', 'label': 'Milestone', 'type': 'string'},
                {'key': 'project', 'label': 'Project', 'type': 'project'},
                {'key': 'planned_end', 'label': 'Deadline', 'type': 'date', 'align': 'right'},
                {'key': 'late', 'label': 'Late by', 'type': 'days', 'align': 'right'},
                {'key': 'progress', 'label': 'Progress', 'type': 'percent', 'align': 'right', 'hideBelow': 'md'},
                {'key': 'open', 'label': 'Open tasks', 'type': 'number', 'align': 'right', 'hideBelow': 'lg'},
                *([{'key': 'billing', 'label': 'Billing', 'type': 'money', 'align': 'right'}] if money else []),
            ],
            'rows': [
                {
                    'id': x['m']['id'],
                    'milestone': f'M{js_str(x["m"]["no"])} {js_str(x["m"]["name"])}',
                    'project': coalesce(mp['projects'][x['m']['project_id']].get('name', UNDEF) if x['m']['project_id'] in mp['projects'] else UNDEF, '—'),
                    'project_id': x['m']['project_id'],
                    'planned_end': x['m']['planned_end'],
                    'late': diff_days(t, x['m']['planned_end']),
                    'progress': x['st']['progress'],
                    'open': x['st']['openCount'],
                    'billing': x['m']['billing_amount_paise'] if money else None,
                }
                for x in lst
            ],
            'empty': 'No milestone is past its deadline.',
            'ask': 'Which milestones are overdue and how much billing is held up?',
        }
    if view == 'effort_ratio':
        act = mp['act']
        lst = [{'x': x, 'a': _hours(act, x['id'])} for x in rows(ctx, 'tasks') if x['project_id'] in pids and x['est_hours'] > 0]
        lst = [v for v in lst if v['a'] / v['x']['est_hours'] > alert]
        lst = sorted(lst, key=lambda v: v['a'] / v['x']['est_hours'], reverse=True)
        extra: Any = 0
        for v in lst:
            extra += v['a'] - v['x']['est_hours']
        return {
            **base,
            'title': 'Effort overruns',
            'explanation': f'Tasks whose actual hours exceed {js_str(alert)}× the estimate (the tenant alert, projects.effort_ratio_alert). Open ones need a re-estimate; closed ones feed estimation accuracy.',
            'kpis': [
                {'label': 'Tasks over alert', 'value': len(lst), 'format': 'number', 'tone': 'warning' if lst else 'success'},
                {'label': 'Still open', 'value': sum(1 for v in lst if v['x']['status'] != 'done'), 'format': 'number'},
                {'label': 'Hours over estimate', 'value': r1(extra), 'format': 'hours', 'tone': 'danger' if extra > 0 else UNDEF},
            ],
            'columns': [
                task_cols[0],
                task_cols[1],
                task_cols[2],
                task_cols[3],
                {'key': 'est', 'label': 'Estimate', 'type': 'hours', 'align': 'right', 'hideBelow': 'md'},
                {'key': 'actual', 'label': 'Actual', 'type': 'hours', 'align': 'right'},
                {'key': 'ratio', 'label': 'Ratio', 'type': 'ratio', 'align': 'right'},
            ],
            'rows': [trow(v['x'], {'est': v['x']['est_hours'], 'actual': r1(v['a']), 'ratio': r3(v['a'] / v['x']['est_hours'])}) for v in lst],
            'empty': f'No task is above {js_str(alert)}× its estimate.',
            'ask': 'Which tasks are running over their estimate and by how much?',
        }
    if view == 'allocation':
        next10 = next_working_days(ctx, 10)
        people = [u for u in scope_people(ctx) if u.get('role') in ('employee', 'pm')]
        open_by_owner = _by([x for x in rows(ctx, 'tasks') if x['status'] != 'done'], 'owner_id')
        lst = []
        for u in people:
            al = [allocation_on(ctx, u['id'], d) for d in next10]
            s_: Any = 0
            for v in al:
                s_ += v
            avg = s_ / (len(next10) or 1) / 100
            peak = js_max(0, *al) / 100
            lst.append({'u': u, 'avg': avg, 'peak': peak, 'open': len(open_by_owner.get(u['id'], ()))})
        lst = sorted([v for v in lst if v['avg'] < 0.8 or v['avg'] > 1.1], key=lambda v: v['avg'], reverse=True)
        over = [v for v in lst if v['avg'] > 1.1]
        under = [v for v in lst if v['avg'] < 0.8]
        return {
            **base,
            'title': 'Allocation outliers',
            'explanation': 'People whose average allocation over the next 10 working days is under 80% (spare capacity) or over 110% (overloaded). Rebalance before the work slips.',
            'kpis': [
                {'label': 'Over 110%', 'value': len(over), 'format': 'number', 'tone': 'danger' if over else 'success'},
                {'label': 'Under 80%', 'value': len(under), 'format': 'number', 'tone': 'warning' if under else 'success'},
                {'label': 'People checked', 'value': len(people), 'format': 'number'},
            ],
            'columns': [
                {'key': 'person', 'label': 'Person', 'type': 'person'},
                {'key': 'title', 'label': 'Title', 'type': 'string', 'hideBelow': 'lg'},
                {'key': 'avg', 'label': 'Avg next 10 days', 'type': 'percent', 'align': 'right'},
                {'key': 'peak', 'label': 'Peak day', 'type': 'percent', 'align': 'right', 'hideBelow': 'md'},
                {'key': 'open', 'label': 'Open tasks', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
                {'key': 'flag', 'label': 'Flag', 'type': 'badge'},
            ],
            'rows': [
                {
                    'id': v['u']['id'],
                    'person': v['u']['name'],
                    'person_hue': v['u']['avatar_hue'],
                    'title': v['u']['title'],
                    'avg': r3(v['avg']),
                    'peak': r3(v['peak']),
                    'open': v['open'],
                    'flag': 'Overloaded' if v['avg'] > 1.1 else 'Spare capacity',
                    'flag_tone': 'danger' if v['avg'] > 1.1 else 'warning',
                }
                for v in lst
            ],
            'empty': 'Everyone is between 80% and 110% allocated for the next 10 working days.',
            'ask': 'Who is over-allocated or under-allocated in the next two weeks?',
        }
    if view == 'timesheet_compliance':
        people = scope_people(ctx)
        end_mon = add_days(week_start(t), -7)
        weeks = [add_days(end_mon, -7 * (7 - i)) for i in range(8)]
        lst = []
        for u in people:
            h = week_history(ctx, u['id'], 8, end_mon)
            ok = sum(1 for x in h if x['status'] == 'approved' or x['status'] == 'submitted')
            lst.append({'u': u, 'h': h, 'ok': ok, 'pct': ok / len(h)})
        avg: Any = None
        if lst:
            s_ = 0
            for v in lst:
                s_ += v['pct']
            avg = s_ / len(lst)
        kpis = [
            {
                'label': 'Team compliance',
                'value': avg,
                'format': 'percent',
                'tone': UNDEF if avg is None else 'success' if avg >= 0.9 else 'warning' if avg >= 0.75 else 'danger',
            },
            {'label': 'Missing last week', 'value': sum(1 for v in lst if v['h'][-1]['status'] not in ('approved', 'submitted')), 'format': 'number'},
            {'label': 'Fully compliant', 'value': sum(1 for v in lst if v['ok'] == 8), 'format': 'number', 'tone': 'success'},
        ]
        lst = sorted(lst, key=lambda v: (v['pct'], locale_key(v['u']['name'])))
        return {
            **base,
            'title': 'Timesheet compliance',
            'weeks': weeks,
            'explanation': 'Submitted or approved weeks out of the last 8 completed weeks. Locked, rejected or draft weeks count against compliance and delay payroll.',
            'kpis': kpis,
            'columns': [
                {'key': 'person', 'label': 'Person', 'type': 'person'},
                {'key': 'weeks', 'label': 'Last 8 weeks', 'type': 'weeks', 'hideBelow': 'md'},
                {'key': 'missed', 'label': 'Missed', 'type': 'number', 'align': 'right'},
                {'key': 'pct', 'label': 'Compliance', 'type': 'percent', 'align': 'right'},
            ],
            'rows': [
                {
                    'id': v['u']['id'],
                    'person': v['u']['name'],
                    'person_hue': v['u']['avatar_hue'],
                    'weeks': [x['status'] for x in v['h']],
                    'missed': 8 - v['ok'],
                    'pct': v['pct'],
                }
                for v in lst
            ],
            'empty': 'No people in scope.',
            'ask': 'Who has not submitted timesheets in the last 8 weeks?',
        }
    if view == 'approval_kpi':
        sc = None if has(ctx, 'people.view_all') else {ctx.user_id, *[u['id'] for u in scope_people(ctx)]}
        lst = approver_rows(ctx, add_days(week_start(t), -7 * 7), sc)
        pending: Any = 0
        past: Any = 0
        late: Any = 0
        for m in lst:
            pending += m['pending']
        for m in lst:
            past += m['overdue']
        for m in lst:
            late += m['late']
        return {
            **base,
            'title': 'Approval KPI',
            'explanation': f'Managers ranked by approval discipline over the last 8 weeks. Score = −(late decisions + pending past the {js_str(st["approval.sla_working_days"])}-working-day SLA). 0 is perfect; it never blocks anyone (BR-11).',
            'kpis': [
                {'label': 'Pending now', 'value': pending, 'format': 'number'},
                {'label': 'Past SLA', 'value': past, 'format': 'number', 'tone': 'danger' if any(m['overdue'] for m in lst) else 'success'},
                {'label': 'Late decisions', 'value': late, 'format': 'number', 'tone': 'warning' if any(m['late'] for m in lst) else UNDEF},
            ],
            'columns': [
                {'key': 'person', 'label': 'Manager', 'type': 'person'},
                {'key': 'decided', 'label': 'Decided', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
                {'key': 'on_time', 'label': 'On time', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
                {'key': 'late', 'label': 'Late', 'type': 'number', 'align': 'right'},
                {'key': 'pending', 'label': 'Pending', 'type': 'number', 'align': 'right', 'hideBelow': 'md'},
                {'key': 'overdue', 'label': 'Past SLA', 'type': 'number', 'align': 'right'},
                {'key': 'score', 'label': 'Score', 'type': 'badge', 'align': 'right'},
            ],
            'rows': [
                {
                    'id': m['id'],
                    'person': m['name'],
                    'person_hue': m['hue'],
                    'decided': m['decided'],
                    'on_time': m['on_time'],
                    'late': m['late'],
                    'pending': m['pending'],
                    'overdue': m['overdue'],
                    'score': js_str(m['score']),
                    'score_tone': 'success' if m['score'] == 0 else 'warning' if m['score'] >= -2 else 'danger',
                }
                for m in lst
            ],
            'empty': 'No approvals in the last 8 weeks.',
            'ask': 'Which managers approve timesheets late?',
        }
    if view == 'stale_deals':
        owner = UNDEF if has(ctx, 'deals.view_all') else ctx.user_id
        p = pipeline(ctx, owner)
        nm = name_of(ctx)
        lst = sorted([deal_lite(ctx, d, nm) for d in p['stale']], key=lambda d: d['days_idle'], reverse=True)
        val: Any = 0
        wtd: Any = 0
        for d in lst:
            val += d['value']
        for d in lst:
            wtd += d['weighted']
        return {
            **base,
            'title': 'Stale deals',
            'explanation': f'Open deals with no logged activity for {js_str(st["sales.stale_deal_days"])} days or more. Call, email or close them out — stale deals inflate the pipeline.',
            'kpis': [
                {'label': 'Stale deals', 'value': len(lst), 'format': 'number', 'tone': 'warning' if lst else 'success'},
                {'label': 'Value at stake', 'value': val, 'format': 'money'},
                {'label': 'Weighted', 'value': wtd, 'format': 'money'},
            ],
            'columns': [
                {'key': 'deal', 'label': 'Deal', 'type': 'deal'},
                {'key': 'client', 'label': 'Client', 'type': 'string', 'hideBelow': 'md'},
                {'key': 'stage', 'label': 'Stage', 'type': 'badge'},
                {'key': 'owner', 'label': 'Owner', 'type': 'person', 'hideBelow': 'lg'},
                {'key': 'value', 'label': 'Value', 'type': 'money', 'align': 'right'},
                {'key': 'days_idle', 'label': 'Idle', 'type': 'days', 'align': 'right'},
            ],
            'rows': [
                {
                    'id': d['id'],
                    'deal': d['title'],
                    'deal_id': d['id'],
                    'client': d['client'],
                    'stage': DEAL_STAGE[d['stage']]['label'],
                    'stage_tone': DEAL_STAGE[d['stage']]['tone'],
                    'owner': d['owner'],
                    'owner_hue': 150,
                    'value': d['value'],
                    'days_idle': d['days_idle'],
                }
                for d in lst
            ],
            'empty': 'Every open deal has had activity recently.',
            'ask': 'Which deals have gone stale and what are they worth?',
        }
    if view == 'predictions':
        return _predictions(ctx, base, mp, projects, pids, trow, person)
    return _estimation(ctx, base, mp)


def _end_of(ids: list, run: dict) -> Any:
    m: Any = None
    for id_ in ids:
        f = run.get(id_)
        if js_truthy(f) and (not js_truthy(m) or f > m):
            m = f
    return m


def _predictions(ctx: Ctx, base: dict, mp: dict, projects: list[dict], pids: set, trow: Any, person: Any) -> dict[str, Any]:
    t = ctx.today
    samples = ratio_samples(ctx)
    runs = [simulate(ctx, q, samples) for q in QS]
    p50 = runs[2]
    p80 = runs[4]
    tasks = rows(ctx, 'tasks')
    tasks_by_ms = _tenant_group(ctx, 'tasks', 'milestone_id')
    tasks_by_project = _tenant_group(ctx, 'tasks', 'project_id')
    c = cal(ctx)
    money = has(ctx, 'finance.view') or can_see_margins(ctx)
    ms_rows = []
    for m in rows(ctx, 'milestones'):
        if js_truthy(m.get('deleted_at')) or m['project_id'] not in pids or js_truthy(m.get('achieved_on')):
            continue
        ms_tasks = tasks_by_ms.get(m['id'], [])
        ids = [x['id'] for x in ms_tasks if x['status'] != 'done']
        if not ids:
            continue
        e50 = _end_of(ids, p50)
        e80 = _end_of(ids, p80)
        chance = sum(1 for r in runs if coalesce(_end_of(ids, r), '') <= m['planned_end']) / len(runs)
        proj = mp['projects'].get(m['project_id'])
        pname = coalesce(proj.get('name', UNDEF) if proj else UNDEF, '—')
        ms_rows.append(
            {
                'id': m['id'],
                'milestone': f'{js_str(pname)} M{js_str(m["no"])} {js_str(m["name"])}',
                'milestone_sub': f'{fmt_pct0(earned_pct(ms_tasks))} done',
                'project': pname,
                'project_id': m['project_id'],
                'planned_end': m['planned_end'],
                'p50': e50,
                'p80': e80,
                'chance': chance,
                'billing': m['billing_amount_paise'] if money else None,
                'open': len(ids),
            }
        )
    ms_rows = sorted(ms_rows, key=lambda x: locale_key(x['planned_end']))
    entries = costed_entries(ctx)
    d = t
    n = 0
    g = 0
    while n < 20:
        gg = g
        g += 1
        if not gg < 60:
            break
        d = add_days(d, -1)
        if is_working_day(d, c):
            n += 1
    from20 = d
    # entries of the last 20 working days, in table order (the per-project sums below add them in the same order)
    recent_entries = [e for e in entries if js_truthy(e.get('task_id')) and from20 <= e['work_date'] < t]
    proj_rows = []
    for p in projects:
        if p['id'].startswith('p_general_'):
            continue
        mine = tasks_by_project.get(p['id'], [])
        ids = [x['id'] for x in mine if x['status'] != 'done']
        if not ids:
            continue
        plan = p['end_on']
        id_set = {x['id'] for x in mine}
        recent: Any = 0
        for e in recent_entries:
            if e['task_id'] in id_set:
                recent += e['hours']
        remaining: Any = 0
        for x in mine:
            if x['status'] != 'done':
                remaining += x['est_hours'] * (1 - progress_of(x) / 100)
        per_day = recent / 20
        pace: Any = None
        if per_day > 0:
            dd = t
            k = math.ceil(remaining / per_day)
            g = 0
            while k > 0:
                gg = g
                g += 1
                if not gg < 2000:
                    break
                dd = add_days(dd, 1)
                if is_working_day(dd, c):
                    k -= 1
            pace = dd
        weekly = js_round(per_day * 5)
        ep = earned_pct(mine)
        proj_rows.append(
            {
                'id': p['id'],
                'project': p['name'],
                'project_id': p['id'],
                'project_sub': f'{fmt_pct0(ep)} done, {js_str(js_round(remaining))}h left',
                'planned_end': plan,
                'p50': _end_of(ids, p50),
                'p80': _end_of(ids, p80),
                'pace': pace,
                'pace_sub': f'{js_str(weekly)}h a week lately' if per_day > 0 else 'No hours logged lately',
                'weekly': weekly,
                'remaining': r1(remaining),
                'progress': ep,
                'chance': sum(1 for r in runs if coalesce(_end_of(ids, r), '') <= plan) / len(runs),
            }
        )
    proj_rows = sorted(proj_rows, key=lambda x: x['chance'])
    slip = []
    for x in tasks:
        if x['project_id'] in pids and x['status'] != 'done':
            ch = sum(1 for r in runs if r.get(x['id'], '') <= x['due_on']) / len(runs)
            if ch < 0.8:
                slip.append({'x': x, 'chance': ch})
    slip = sorted(slip, key=lambda v: (v['chance'], locale_key(v['x']['due_on'])))[:12]
    at_risk = [m for m in ms_rows if m['chance'] < 0.5]
    at_risk_billing: Any = 0
    for m in at_risk:
        at_risk_billing += coalesce(m['billing'], 0)
    team = samples['team']
    return {
        **base,
        'title': 'Delivery predictions',
        'explanation': 'Each person’s plan is re-run with their own history: P50 uses their typical effort ratio, P80 a bad-but-realistic one. Chance on time is the share of six runs (from their best 20% to worst 10% of tasks) that land by the date. Velocity-based uses the hours actually logged on the project in the last 20 working days.',
        'kpis': [
            {'label': 'Open milestones', 'value': len(ms_rows), 'format': 'number'},
            {'label': 'Below 50% chance', 'value': len(at_risk), 'format': 'number', 'tone': 'danger' if at_risk else 'success'},
            *([{'label': 'Billing below 50% chance', 'value': at_risk_billing, 'format': 'money', 'tone': 'warning'}] if money else []),
            {
                'label': 'Team typical effort ratio',
                'value': r3(median(team)) if team else None,
                'format': 'ratio',
                'hint': f'{len(team)} closed tasks with actual hours',
            },
        ],
        'columns': [
            {'key': 'project', 'label': 'Project', 'type': 'project'},
            {'key': 'planned_end', 'label': 'Planned end', 'type': 'date', 'align': 'right'},
            {'key': 'p50', 'label': 'P50', 'type': 'date', 'align': 'right'},
            {'key': 'p80', 'label': 'P80', 'type': 'date', 'align': 'right', 'hideBelow': 'md'},
            {'key': 'pace', 'label': 'At current pace', 'type': 'date', 'align': 'right'},
            {'key': 'chance', 'label': 'Chance on time', 'type': 'chance', 'align': 'right'},
        ],
        'rows': proj_rows,
        'main_title': 'Projects',
        'empty': 'No live projects with open work.',
        'ask': 'Which milestones are unlikely to finish on time?',
        'sections': [
            {
                'title': 'Milestones',
                'subtitle': 'Open milestones with their forecast finish and the billing that rides on them.',
                'columns': [
                    {'key': 'milestone', 'label': 'Milestone', 'type': 'string'},
                    {'key': 'planned_end', 'label': 'Deadline', 'type': 'date', 'align': 'right'},
                    {'key': 'p50', 'label': 'P50', 'type': 'date', 'align': 'right'},
                    {'key': 'p80', 'label': 'P80', 'type': 'date', 'align': 'right', 'hideBelow': 'md'},
                    {'key': 'chance', 'label': 'Chance on time', 'type': 'chance', 'align': 'right'},
                    *([{'key': 'billing', 'label': 'Billing at stake', 'type': 'money', 'align': 'right', 'hideBelow': 'md'}] if money else []),
                ],
                'rows': ms_rows,
                'empty': 'No open milestones with open tasks.',
            },
            {
                'title': 'Tasks most likely to slip',
                'subtitle': 'Open tasks below an 80% chance of finishing by their target, with the owner and their backup.',
                'columns': [
                    {'key': 'task', 'label': 'Task', 'type': 'task'},
                    {'key': 'owner', 'label': 'Owner', 'type': 'person'},
                    {'key': 'due_on', 'label': 'Target', 'type': 'date', 'align': 'right'},
                    {'key': 'p50', 'label': 'P50', 'type': 'date', 'align': 'right', 'hideBelow': 'md'},
                    {'key': 'p80', 'label': 'P80', 'type': 'date', 'align': 'right', 'hideBelow': 'md'},
                    {'key': 'chance', 'label': 'Chance on time', 'type': 'chance', 'align': 'right'},
                ],
                'rows': [
                    trow(
                        v['x'],
                        {
                            'task_sub': coalesce(
                                mp['projects'][v['x']['project_id']].get('name', UNDEF) if v['x']['project_id'] in mp['projects'] else UNDEF, None
                            ),
                            'owner_sub': f'backup {js_str(coalesce(person(v["x"]["backup_id"]).get("name", UNDEF) if person(v["x"]["backup_id"]) else UNDEF, "—"))}'
                            if js_truthy(v['x'].get('backup_id'))
                            else 'no backup',
                            'p50': p50.get(v['x']['id'], None),
                            'p80': p80.get(v['x']['id'], None),
                            'chance': v['chance'],
                        },
                    )
                    for v in slip
                ],
                'empty': 'No open task is below an 80% chance of finishing on time.',
            },
        ],
    }


_CX_LABEL = {'low': 'Low', 'medium': 'Medium', 'high': 'High', 'very_high': 'Very high'}


def _estimation(ctx: Ctx, base: dict, mp: dict) -> dict[str, Any]:
    s = ratio_samples(ctx)
    people = {u['id'] for u in scope_people(ctx)}
    act = mp['act']
    done = [x for x in rows(ctx, 'tasks') if x['status'] == 'done' and js_truthy(x.get('done_on'))]
    # The measured set: closed tasks with an owner, an estimate and actual hours — the same tasks the ratios use
    measured = [x for x in done if js_truthy(x.get('owner_id')) and x['est_hours'] > 0 and _hours(act, x['id']) > 0]
    measured_by_owner = _by(measured, 'owner_id')
    measured_by_cx = _by(measured, 'complexity')

    def on_time_of(xs: list) -> Any:
        return sum(1 for x in xs if x['done_on'] <= x['due_on']) / len(xs) if xs else None

    def action(m: Any) -> str:
        if m > 1.05:
            return f'Add {js_str(js_round((m - 1) * 100))}% to estimates'
        if m < 0.8:
            return f'Estimates run {js_str(js_round((1 - m) * 100))}% high'
        return 'Estimates are sound'

    def tone(m: Any) -> str:
        return 'success' if m <= 1.05 else 'warning' if m <= 1.3 else 'danger'

    person_rows = []
    for id_, a in s['byPerson'].items():
        if id_ not in people:
            continue
        u = mp['users'].get(id_)
        mine = measured_by_owner.get(id_, [])
        m = median(a)
        est: Any = 0
        actual: Any = 0
        for x in mine:
            est += x['est_hours']
        for x in mine:
            actual += _hours(act, x['id'])
        person_rows.append(
            {
                'id': id_,
                'person': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
                'person_hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
                'tasks': len(mine),
                'tasks_sub': f'{sum(1 for x in mine if x["done_on"] <= x["due_on"])} on time',
                'typical': r3(m),
                'typical_tone': tone(m),
                'p80': r3(quant(a, 0.8)),
                'on_time': on_time_of(mine),
                'est': r1(est),
                'actual': r1(actual),
                'action': action(m),
            }
        )
    person_rows = sorted(person_rows, key=lambda r: r['typical'], reverse=True)
    cx_rows = []
    for k in ('low', 'medium', 'high', 'very_high'):
        if k not in s['byCx']:
            continue
        a = s['byCx'][k]
        m = median(a)
        mine = measured_by_cx.get(k, [])
        cx_rows.append(
            {
                'id': k,
                'complexity': _CX_LABEL.get(k, k),
                'tasks': len(mine),
                'tasks_sub': f'{sum(1 for x in mine if x["done_on"] <= x["due_on"])} on time',
                'typical': r3(m),
                'typical_tone': tone(m),
                'p80': r3(quant(a, 0.8)),
                'on_time': on_time_of(mine),
                'action': action(m),
            }
        )
    team = s['team']
    tm = median(team) if team else None
    return {
        **base,
        'title': 'Estimation accuracy',
        'explanation': 'Compares estimates to actual hours on closed tasks, by task complexity and by person, and suggests how much buffer to add to future estimates. Effort ratio = actual ÷ estimated hours: ×1.00 is perfect; ×1.30 means work takes 30% longer than planned, so add 30% to estimates. New task estimates are checked against this in the Tasks form.',
        'kpis': [
            {'label': 'Team typical ratio', 'value': None if tm is None else r3(tm), 'format': 'ratio', 'tone': UNDEF if tm is None else tone(tm)},
            {'label': 'Team P80 ratio', 'value': r3(quant(team, 0.8)) if team else None, 'format': 'ratio'},
            {'label': 'Closed tasks measured', 'value': len(team), 'format': 'number'},
            {
                'label': 'On time, measured tasks',
                'value': on_time_of(measured),
                'format': 'percent',
                'hint': f'{sum(1 for x in measured if x["done_on"] <= x["due_on"])} of {len(measured)} closed tasks finished on or before their target end',
            },
        ],
        'main_title': 'By complexity',
        'columns': [
            {'key': 'complexity', 'label': 'Complexity', 'type': 'string'},
            {'key': 'tasks', 'label': 'Tasks', 'type': 'number', 'align': 'right'},
            {'key': 'typical', 'label': 'Typical ratio', 'type': 'ratio', 'align': 'right'},
            {'key': 'p80', 'label': 'P80 ratio', 'type': 'ratio', 'align': 'right'},
            {'key': 'on_time', 'label': 'On time', 'type': 'percent', 'align': 'right'},
            {'key': 'action', 'label': 'Action', 'type': 'string', 'hideBelow': 'md'},
        ],
        'rows': cx_rows,
        'empty': 'No closed tasks with actual hours yet. Close tasks with honest hours to build this.',
        'ask': 'How accurate are our estimates by person?',
        'sections': [
            {
                'title': 'By person',
                'columns': [
                    {'key': 'person', 'label': 'Person', 'type': 'person'},
                    {'key': 'tasks', 'label': 'Tasks', 'type': 'number', 'align': 'right'},
                    {'key': 'typical', 'label': 'Typical ratio', 'type': 'ratio', 'align': 'right'},
                    {'key': 'p80', 'label': 'P80 ratio', 'type': 'ratio', 'align': 'right', 'hideBelow': 'md'},
                    {'key': 'on_time', 'label': 'On time', 'type': 'percent', 'align': 'right', 'hideBelow': 'md'},
                    {'key': 'est', 'label': 'Estimated', 'type': 'hours', 'align': 'right', 'hideBelow': 'lg'},
                    {'key': 'actual', 'label': 'Actual', 'type': 'hours', 'align': 'right', 'hideBelow': 'lg'},
                    {'key': 'action', 'label': 'Action', 'type': 'string', 'hideBelow': 'md'},
                ],
                'rows': person_rows,
                'empty': 'No closed tasks yet.',
            },
        ],
    }


def insight_tabs(ctx: Ctx) -> list[dict]:
    t = ctx.today
    pids = {p['id'] for p in scope_projects(ctx)}
    open_ = [x for x in rows(ctx, 'tasks') if x['project_id'] in pids and x['status'] != 'done']
    alert = prop(_S(ctx), 'projects.effort_ratio_alert')
    act = task_actuals(ctx)
    t3 = add_days(t, 3)

    def overdue_ms() -> int:
        return sum(
            1
            for m in rows(ctx, 'milestones')
            if not js_truthy(m.get('deleted_at'))
            and m['project_id'] in pids
            and not js_truthy(m.get('achieved_on'))
            and _ms_state(ctx, m)['state'] == 'overdue'
        )

    count = {
        'tasks_at_risk': lambda: sum(1 for x in open_ if x['status'] == 'todo' and t <= x['due_on'] <= t3),
        'blocked': lambda: sum(1 for x in open_ if x['status'] == 'blocked'),
        'overdue_milestones': overdue_ms,
        'effort_ratio': lambda: sum(
            1 for x in rows(ctx, 'tasks') if x['project_id'] in pids and x['est_hours'] > 0 and _hours(act, x['id']) / x['est_hours'] > alert
        ),
        'stale_deals': lambda: len(pipeline(ctx, UNDEF if has(ctx, 'deals.view_all') else ctx.user_id)['stale']),
    }
    return [{'id': id_, 'label': LABELS[id_][0], 'group': LABELS[id_][1], 'count': count[id_]() if id_ in count else None} for id_ in views_for(ctx)]


# ---------------------------------------------------------------------------------------------------------------------
# Weekly scorecard (Feature Guide §15, prototype §9, §14)
# ---------------------------------------------------------------------------------------------------------------------
def _W(k: str) -> Any:
    return SCORE_WEIGHTS[k]['weight']


def _plural(n: int, word: str) -> str:
    return f'{word}{"" if n == 1 else "s"}'


def _due_fn(ctx: Ctx, data: dict[str, Any]) -> Any:
    """sla_due(ctx, submitted_at) through a by-date table kept with the request's score data (sla_due depends on the
    date, cal(ctx) and the tenant row only; the table is dropped when the tenant row changes in the request)"""
    tv = scope().view_token('tenants')
    sm = data.get('_sla_by_date')
    if sm is None or sm[0] is not tv:
        sm = data['_sla_by_date'] = (tv, {})
    dues = sm[1]

    def due(sa: str) -> str:
        d0 = sa[:10]
        v = dues.get(d0)
        if v is None:
            v = dues[d0] = sla_due(ctx, sa)
        return v

    return due


def score_for(ctx: Ctx, u: dict, wk: str, data: dict[str, Any]) -> dict[str, Any]:
    t = ctx.today
    role = u.get('role')
    kind = 'sales' if role == 'sales' else 'ops' if role == 'ops' else 'delivery'
    if t < wk:
        return {'cells': [], 'score': None, 'kind': kind}
    # Mon..Sat window; in the running week only finished days count (today is still in progress)
    if add_days(wk, 5) < t:
        end = add_days(wk, 5)
    elif add_days(t, -1) < wk:
        end = wk
    else:
        end = add_days(t, -1)
    base_cal = data['cal']
    c = CalendarCtx(base_cal.settings, base_cal.holidays, data['leave'].get(u['id']))
    min_h = prop(_S(ctx), 'calendar.min_hours_per_day')
    wdays = [d for d in _dates(wk, end) if is_working_day(d, c)]
    cells: list[dict] = []

    def pct(a: Any, b: Any) -> Any:
        return js_min(100, js_round((a / b) * 100)) if b > 0 else None

    uid_ = u['id']
    wk6 = add_days(wk, 6)
    # Updates: daily updates / elapsed working days
    ups = [x for x in data['updates_by_user'].get(uid_, ()) if wk <= x['date'] <= end]
    up_days = len({x['date'] for x in ups})
    upd = pct(up_days, len(wdays))
    # Timesheet: hours logged (leave counts) / expected hours
    hrs: Any = 0
    for e in data['entries_by_user'].get(uid_, ()):
        if wk <= e['work_date'] <= end:
            hrs += e['hours']
    ts = pct(hrs, len(wdays) * min_h)
    if kind == 'delivery':
        my_tasks = data['tasks_by_owner'].get(uid_, ())
        due = [x for x in my_tasks if wk <= x['due_on'] <= wk6 and x['due_on'] <= t]
        ok = sum(1 for x in due if x['status'] == 'done' and js_truthy(x.get('done_on')) and x['done_on'] <= x['due_on'])
        fail = sum(
            1
            for x in due
            if (x['status'] == 'done' and js_truthy(x.get('done_on')) and x['done_on'] > x['due_on']) or (x['status'] != 'done' and x['due_on'] < t)
        )
        cells.append(
            {
                'key': 'delivery',
                'label': 'On-time delivery',
                'weight': _W('delivery'),
                'value': js_round((ok / (ok + fail)) * 100) if ok + fail else None,
                'detail': f'{ok} of {ok + fail} due tasks on time' if ok + fail else 'No tasks due',
            }
        )
        # Effort accuracy: how close actual hours came to the estimate on tasks closed this week
        act = data['act']
        closed = [
            x
            for x in my_tasks
            if x['status'] == 'done' and js_truthy(x.get('done_on')) and wk <= x['done_on'] <= wk6 and x['est_hours'] > 0 and _hours(act, x['id']) > 0
        ]
        c_est: Any = 0
        c_act: Any = 0
        for x in closed:
            c_est += x['est_hours']
        for x in closed:
            c_act += _hours(act, x['id'])
        ratio = c_act / c_est if js_truthy(c_est) else None
        cells.append(
            {
                'key': 'effort_accuracy',
                'label': 'Effort accuracy',
                'weight': _W('effort_accuracy'),
                'value': None if ratio is None else js_max(0, js_round(100 - abs(1 - ratio) * 100)),
                'detail': 'No tasks closed' if ratio is None else f'Effort ratio ×{to_fixed(ratio, 2)} on {len(closed)} closed {_plural(len(closed), "task")}',
            }
        )
        demo = 100 if any(js_truthy(x.get('demo_shown')) for x in ups) else 0 if add_days(wk, 4) <= t else None
        cells.append(
            {
                'key': 'demo',
                'label': 'Friday demo',
                'weight': _W('demo'),
                'value': demo,
                'detail': 'Friday demo shown' if demo == 100 else 'No demo this week' if demo == 0 else 'Friday not reached',
            }
        )
        # Early warning: slips this week (late, or forecast to breach a target in this week) flagged before the due date
        p50 = data['p50']
        slipped = []
        for x in my_tasks:
            if x['due_on'] < wk or x['due_on'] > wk6:
                continue
            if x['status'] == 'done':
                if js_truthy(x.get('done_on')) and x['done_on'] > x['due_on']:
                    slipped.append(x)
            elif x['due_on'] < t or p50.get(x['id'], x['due_on']) > x['due_on']:
                slipped.append(x)
        flagged = sum(1 for x in slipped if js_truthy(x.get('slip_flagged_on')) and x['slip_flagged_on'] <= x['due_on'])
        cells.append(
            {
                'key': 'early_warning',
                'label': 'Early warning',
                'weight': _W('early_warning'),
                'value': js_round((flagged / len(slipped)) * 100) if slipped else None,
                'detail': f'{flagged} of {len(slipped)} slips flagged early' if slipped else 'No slips this week',
            }
        )
    elif kind == 'sales':
        acts = sum(1 for a in data['acts_by_user'].get(uid_, ()) if wk <= a['date'] <= end)
        cells.append(
            {
                'key': 'activities',
                'label': 'Activities',
                'weight': _W('activities'),
                'value': js_min(100, js_round((acts / 15) * 100)),
                'detail': f'{acts} of 15 logged',
            }
        )
        my_deals = data['deals_by_owner'].get(uid_, ())
        my_open = [d for d in my_deals if d['stage'] != 'won' and d['stage'] != 'lost']
        lim = end if end < t else t
        od = sum(1 for d in my_open if js_truthy(d.get('next_step_on')) and d['next_step_on'] < lim)
        cells.append(
            {
                'key': 'followups',
                'label': 'Follow-ups',
                'weight': _W('followups'),
                'value': js_max(0, 100 - 25 * od) if my_open else None,
                'detail': f'{od} overdue next {_plural(od, "step")}' if my_open else 'No open deals',
            }
        )
        leads = sum(1 for d in my_deals if wk <= d['created_at'][:10] <= end)
        cells.append({'key': 'leads', 'label': 'New leads', 'weight': _W('leads'), 'value': js_min(100, leads * 50), 'detail': f'{leads} of 2 new leads'})
    else:
        items = data['ops_by_owner'].get(uid_, ())
        hist = [h for o in items for h in coalesce(prop(o, 'history'), []) if wk <= h['due_on'] <= end]
        on_time = sum(1 for h in hist if js_truthy(h.get('on_time')))
        late = len(hist) - on_time
        missed = sum(1 for o in items if o['status'] == 'open' and wk <= o['due_on'] < t)
        n = on_time + late + missed
        cells.append(
            {
                'key': 'ops',
                'label': 'Duties on time',
                'weight': _W('ops'),
                'value': js_round((on_time / n) * 100) if n else None,
                'detail': f'{on_time} on time, {late + missed} late or missed' if n else 'Nothing due',
            }
        )
    cells.append({'key': 'updates', 'label': 'Daily updates', 'weight': _W('updates'), 'value': upd, 'detail': f'{up_days} of {len(wdays)} days'})
    cells.append(
        {'key': 'timesheet', 'label': 'Timesheet', 'weight': _W('timesheet'), 'value': ts, 'detail': f'{js_str(r1(hrs))}h of {js_str(len(wdays) * min_h)}h'}
    )
    # Managers carry their approval KPI (Feature Guide §9/§15) — the same calculation as the Approvals page
    if uid_ in data['managers'] or uid_ in data['kpi30']:
        if wk == week_start(t):
            k = data['kpi30'].get(uid_)
            if k is None:
                k = approval_kpi(ctx, uid_, 30)
            base_n = k['on_time'] + k['lapses']
            if base_n:
                over = f' ({js_str(k["overdue_now"])} overdue now)' if js_truthy(k['overdue_now']) else ''
                detail = f'{js_str(k["on_time"])} on time, {js_str(k["lapses"])} {_plural(k["lapses"], "lapse")}{over} · last 30 days'
            else:
                detail = 'Nothing to approve in the last 30 days'
            cells.append(
                {
                    'key': 'approvals',
                    'label': 'Approval KPI',
                    'weight': _W('approvals'),
                    'value': js_round((k['on_time'] / base_n) * 100) if base_n else None,
                    'detail': detail,
                }
            )
        else:
            wk_end = add_days(wk, 6)
            due = _due_fn(ctx, data)
            decided = [
                w
                for w in data['weeks_by_decider'].get(uid_, ())
                if (w['status'] == 'approved' or w['status'] == 'rejected')
                and w.get('decided_by') != w['user_id']
                and js_truthy(w.get('decided_at'))
                and wk <= w['decided_at'][:10] <= wk_end
            ]
            late_n = sum(
                1 for w in decided if js_truthy(w.get('late')) or (js_truthy(w.get('submitted_at')) and w['decided_at'][:10] > due(w['submitted_at']))
            )
            waiting = sum(
                1
                for w in data['weeks_by_approver'].get(uid_, ())
                if w['user_id'] != uid_
                and js_truthy(w.get('submitted_at'))
                and due(w['submitted_at']) < wk_end
                and (True if w['status'] == 'submitted' else js_truthy(w.get('decided_at')) and w['decided_at'][:10] > wk_end)
            )
            on_time_n = len(decided) - late_n
            lapses = late_n + waiting
            cells.append(
                {
                    'key': 'approvals',
                    'label': 'Approval KPI',
                    'weight': _W('approvals'),
                    'value': js_round((on_time_n / (on_time_n + lapses)) * 100) if on_time_n + lapses else None,
                    'detail': f'{on_time_n} on time, {lapses} {_plural(lapses, "lapse")}' if on_time_n + lapses else 'Nothing to approve',
                }
            )
    scored = [x for x in cells if x['value'] is not None]
    wsum: Any = 0
    for x in scored:
        wsum += x['weight']
    tot: Any = 0
    for x in scored:
        tot += x['value'] * x['weight']
    return {'cells': cells, 'score': js_round(tot / wsum) if js_truthy(wsum) else None, 'kind': kind}


def _dates(frm: str, to: str) -> list[str]:
    out = []
    d = frm
    while d <= to:
        out.append(d)
        d = add_days(d, 1)
    return out


def score_data(ctx: Ctx) -> dict[str, Any]:
    def f() -> dict[str, Any]:
        leave: dict[Any, set] = {}
        for lrow in rows(ctx, 'leave'):
            if lrow['portion'] == 'full':
                leave.setdefault(lrow['user_id'], set()).add(lrow['date'])
        users = [u for u in rows(ctx, 'users') if _is_ts_user(u)]
        weeks = rows(ctx, 'timesheet_weeks')
        return {
            'cal': cal(ctx),
            'leave': leave,
            # the TS filters these tables by person inside scoreFor; the groups hold the same rows in the same order
            'updates_by_user': _group_now(ctx, 'daily_updates', 'user_id'),
            'entries_by_user': _group_now(ctx, 'timesheet_entries', 'user_id'),
            'tasks': rows(ctx, 'tasks'),
            'tasks_by_owner': _group_now(ctx, 'tasks', 'owner_id'),
            'acts_by_user': _group_now(ctx, 'deal_activities', 'by'),
            'deals_by_owner': _group_now(ctx, 'deals', 'owner_id'),
            'ops_by_owner': _group_now(ctx, 'ops_items', 'owner_id'),
            'weeks_by_decider': _by(weeks, 'decided_by'),
            'weeks_by_approver': _by(weeks, 'approver_id'),
            'managers': {u['reports_to_id'] for u in users if js_truthy(u.get('reports_to_id'))},
            'p50': p50_finish(ctx),
            'act': task_actuals(ctx),
            'kpi30': approval_kpis(ctx, 30),
        }

    return _once(ctx, 'scoredata', f)


def person_week_score(ctx: Ctx, u: dict, wk: str) -> dict[str, Any]:
    """Score of one person for a week (None = nothing to score yet)."""
    return score_for(ctx, u, wk, score_data(ctx))


def team_week_score(ctx: Ctx, users: list[dict], wk: str) -> Any:
    """Average score of the given people for a week."""
    lim = add_days(wk, 6)
    v = [s for s in (person_week_score(ctx, u, wk)['score'] for u in users if u['joined_on'] <= lim) if s is not None]
    if not v:
        return None
    tot: Any = 0
    for x in v:
        tot += x
    return js_round(tot / len(v))


def scorecard(ctx: Ctx, week_q: Any) -> dict[str, Any]:

    if not has(ctx, 'insights.view'):
        err(403, 'FORBIDDEN', 'The scorecard is for managers, sales and operations.')
    t = ctx.today
    cur = week_start(t)
    wk = week_start(week_q) if js_truthy(week_q) and _DATE_RE.match(week_q) else cur
    data = score_data(ctx)
    users = [u for u in rows(ctx, 'users') if _is_ts_user(u)]
    scope_ = scope_people(ctx)
    # Without delivery or HR-wide access the scorecard covers their own reporting line only.
    if not can_delivery_insights(ctx) and not has(ctx, 'people.view_all'):
        s = _team_ordered(ctx, ctx.user_id)
        s[ctx.user_id] = None
        scope_ = [u for u in users if u['id'] in s]
    forecast = breach_sets(ctx)['forecast']
    forecast_by_owner = _by(forecast, 'owner_id')
    overdue_by_owner = _by([x for x in data['tasks'] if x['status'] != 'done' and x['due_on'] < t], 'owner_id')
    trend_weeks = [add_days(wk, -7 * (7 - i)) for i in range(8)]
    people = []
    lim = add_days(wk, 6)
    for u in scope_:
        if not u['joined_on'] <= lim:
            continue
        s = score_for(ctx, u, wk, data)
        trend = [None if u['joined_on'] > add_days(w, 6) else score_for(ctx, u, w, data)['score'] for w in trend_weeks]
        prev = trend[-2] if len(trend) >= 2 else UNDEF
        red_twice = (
            s['score'] is not None and s['score'] < SCORE_THRESHOLDS['amber'] and prev is not None and prev is not UNDEF and prev < SCORE_THRESHOLDS['amber']
        )
        flags = []
        if red_twice:
            flags.append('Red two weeks running: 1:1 due')
        fb = len(forecast_by_owner.get(u['id'], ()))
        if fb:
            flags.append(f'{fb} {_plural(fb, "task")} forecast to breach')
        overdue = len(overdue_by_owner.get(u['id'], ()))
        if overdue:
            flags.append(f'{overdue} {_plural(overdue, "task")} overdue')
        rd = role_def(u.get('tenant_id'), u.get('role_key'))
        people.append(
            {
                'id': u['id'],
                'name': u['name'],
                'title': u['title'],
                'role': u['role'],
                'role_name': coalesce(rd.get('name', UNDEF) if rd else UNDEF, prop(u, 'title')),
                'hue': u['avatar_hue'],
                'kind': s['kind'],
                'cells': s['cells'],
                'score': s['score'],
                'trend': trend,
                'flags': flags,
                'red_twice': red_twice,
                'forecast_breach': fb,
            }
        )
    people = sorted(people, key=lambda p: (coalesce(p['score'], 999), locale_key(p['name'])))
    scored = [p for p in people if p['score'] is not None]
    team_trend = []
    for i in range(len(trend_weeks)):
        v = [p['trend'][i] for p in people if p['trend'][i] is not None]
        if v:
            tot: Any = 0
            for x in v:
                tot += x
            team_trend.append(js_round(tot / len(v)))
        else:
            team_trend.append(None)

    def label(w: str) -> str:
        return f'{fmt_day(w)} – {fmt_day(add_days(w, 6))}'

    weeks = [{'week': w, 'label': label(w)} for w in (add_days(cur, -7 * i) for i in range(12))]
    team_score: Any = None
    if scored:
        tot = 0
        for p in scored:
            tot += p['score']
        team_score = js_round(tot / len(scored))
    green = SCORE_THRESHOLDS['green']
    amber = SCORE_THRESHOLDS['amber']
    return {
        'week': wk,
        'label': label(wk),
        'weeks': weeks,
        'is_current': wk == cur,
        'is_future': wk > cur,
        'team': {
            'score': team_score,
            'green': sum(1 for p in scored if p['score'] >= green),
            'amber': sum(1 for p in scored if amber <= p['score'] < green),
            'red': sum(1 for p in scored if p['score'] < amber),
            'none': len(people) - len(scored),
            'trend': team_trend,
        },
        'people': people,
        'thresholds': SCORE_THRESHOLDS,
    }


# ---------------------------------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------------------------------
def get_ceo(req: Req) -> Any:
    ctx = req.ctx
    pq = req.q('period')
    kind = pq if js_str(pq) in ('month', 'quarter', 'fy') else 'month'
    a = js_str(coalesce(req.q('anchor'), ''))
    anchor = a if _DATE_RE.match(a) else ctx.today
    return ceo(ctx, js_str(kind), ctx.today if anchor > ctx.today else anchor, week_of(ctx, req.q('week')))


def get_pm(req: Req) -> Any:
    return pm_dash(req.ctx, week_of(req.ctx, req.q('week')))


def get_sales(req: Req) -> Any:
    return sales_dash(req.ctx)


def get_ops(req: Req) -> Any:
    return ops_dash(req.ctx)


def get_employee(req: Req) -> Any:
    return employee_dash(req.ctx, req.q('user'), week_of(req.ctx, req.q('week')))


def get_mentions(req: Req) -> Any:
    """ "Tagged you": comments that mention a person (self, or someone in the viewer's employee-view scope)"""
    ctx = req.ctx
    uq = req.q('user')
    uid_ = uq if isinstance(uq, str) and uq else ctx.user_id
    if uid_ != ctx.user_id and not any(u['id'] == uid_ for u in viewable_people(ctx)):
        err(403, 'FORBIDDEN', 'You can only see mentions of people in your team.')
    return mentions_of(ctx, uid_, 20)


def get_rules_score(req: Req) -> Any:
    """Rules page: how the weekly score is built, with the live thresholds"""
    return {'weights': SCORE_WEIGHTS, 'thresholds': SCORE_THRESHOLDS}


def get_insights(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'insights.view'):
        err(403, 'FORBIDDEN', 'Insights are for managers, sales and operations.')
    return insight_tabs(ctx)


def get_insight(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'insights.view'):
        err(403, 'FORBIDDEN', 'Insights are for managers, sales and operations.')
    view = req.params['view']
    if view not in LABELS and view not in _OBJECT_PROTO_KEYS:
        err(404, 'NOT_FOUND', 'That insight doesn’t exist.')
    return insight(ctx, view)


def get_scorecard(req: Req) -> Any:
    w = req.q('week')
    return scorecard(req.ctx, js_str(w) if js_truthy(w) else UNDEF)


def register(r: Any) -> None:
    r.get('/dashboards/ceo', get_ceo)
    r.get('/dashboards/pm', get_pm)
    r.get('/dashboards/sales', get_sales)
    r.get('/dashboards/ops', get_ops)
    r.get('/dashboards/employee', get_employee)
    r.get('/dashboards/mentions', get_mentions)
    r.get('/rules/score', get_rules_score)
    r.get('/insights', get_insights)
    r.get('/insights/:view', get_insight)
    r.get('/scorecard', get_scorecard)
