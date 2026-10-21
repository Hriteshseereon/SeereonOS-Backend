"""People & HR (v3) — port of src/mock/routes/hr.ts: leave requests with approval + balances, appraisal cycles and
reviews (metrics from real work data), goals/KRAs, feedback, performance profiles, recruitment pipeline,
onboarding/exit checklists, asset register and the HR dashboard. Every rule is enforced here; the UI only reflects it.

Cross-module helpers (imported lazily, owned by other porters): app.routes.timesheets.find_week / resolve_approver,
app.routes.workflows_v7.appraisal_template / assert_not_workflow_managed / check_routable / set_appraisal_sync_hook /
start_workflow / wf_linked_appraisals.
"""

from __future__ import annotations

import re
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    err,
    find,
    get_db,
    has,
    insert,
    log_activity,
    notify,
    now_iso,
    patch,
    permissions_of,
    remove,
    require_action,
    require_any,
    rows,
    rows_where_ro,
    scope,
    uid,
    update,
    user_name,
    users_with,
    warn,
)
from app.domain.calendar import (
    add_days,
    add_months,
    dates_between,
    diff_days,
    fmt_date,
    fy_label,
    fy_range,
    fy_start_year,
    is_real_iso_date,
    is_working_day,
    max_iso,
    min_iso,
    month_of,
    month_range,
    parse_iso,
    quarter_of,
    week_start,
)
from app.domain.labels import LEAVE_TYPE
from app.engine.analytics import avg_allocation, cal, sla_due
from app.engine.performance import DELAY_REASON_LABEL, task_stats, task_timeliness
from app.engine.workflows import advance_approval, approval_route, ensure_wf, start_approval, step_label, wf_patch
from app.js import (
    NAN,
    UNDEF,
    coalesce,
    date_parse,
    fmt_date_en_in,
    is_finite,
    is_integer,
    iso_from_ms,
    js_len,
    js_max,
    js_min,
    js_number,
    js_round,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    nullish,
    prop,
)

LEAVE_TYPES = ['casual', 'sick', 'earned', 'compensatory', 'lwp', 'optional_holiday']


def _S(ctx: Ctx) -> dict:
    return ctx.tenant['settings']


def r1(n: Any) -> Any:
    return js_round(n * 10) / 10


def r2(n: Any) -> Any:
    return js_round(n * 100) / 100


def half(n: Any) -> Any:
    return js_round(n * 2) / 2


def _is_date(s: Any) -> bool:
    return isinstance(s, str) and is_real_iso_date(s) and parse_iso(s) is not None


def _str(v: Any) -> str:
    """String(v ?? '').trim()"""
    return js_trim(js_str(coalesce(v, '')))


def _g(b: Any, k: str) -> Any:
    return b.get(k, UNDEF) if isinstance(b, dict) else UNDEF


def _user(ctx: Ctx, id_: Any) -> dict | None:
    if not js_truthy(id_) or isinstance(id_, (dict, list)):
        return None
    u = scope().find_by('users', 'id', id_)
    return u if u is not None and u.get('tenant_id') == ctx.tenant_id else None


def _is_hr(ctx: Ctx) -> bool:
    return has(ctx, 'people.manage')


def _is_appraisal_admin(ctx: Ctx) -> bool:
    return has(ctx, 'appraisals.manage')


def _sees_all_people(ctx: Ctx) -> bool:
    """people.view_all: HR records (leave, feedback, profiles) of everyone."""
    return has(ctx, 'people.view_all')


def _leave_override(ctx: Ctx) -> bool:
    """HR-level leave authority: may decide any pending leave request (people.manage + leave.approve)."""
    return has(ctx, 'people.manage') and has(ctx, 'leave.approve')


def _is_above(ctx: Ctx, viewer: Any, target: Any) -> bool:
    """Is `viewer` anywhere above `target` in the reporting chain?"""
    u = _user(ctx, target)
    i = 0
    while u and i < 10:
        if not js_truthy(u.get('reports_to_id')):
            return False
        if u['reports_to_id'] == viewer:
            return True
        u = _user(ctx, u['reports_to_id'])
        i += 1
    return False


def _can_view_person(ctx: Ctx, target: Any) -> bool:
    return target == ctx.user_id or _sees_all_people(ctx) or _is_above(ctx, ctx.user_id, target)


def _hr_people(ctx: Ctx) -> list[dict]:
    return users_with(ctx, 'people.manage')


def _delegators_of(ctx: Ctx, me: Any, on: Any = UNDEF) -> set:
    """All-project delegations hand over approvals; a one-project delegation covers that project's tasks only."""
    d = ctx.today if on is UNDEF else on
    return {
        x['from_user_id']
        for x in rows(ctx, 'delegations')
        if x.get('to_user_id') == me and x['from_on'] <= d and x['to_on'] >= d and not js_truthy(x.get('project_id'))
    }


def _active_delegations_from(ctx: Ctx, approver: Any) -> list[dict]:
    t = ctx.today
    return [
        d
        for d in rows(ctx, 'delegations')
        if d.get('from_user_id') == approver and d['from_on'] <= t and d['to_on'] >= t and not js_truthy(d.get('project_id'))
    ]


# =====================================================================================================================
# Leave
# =====================================================================================================================
def _policy_of(ctx: Ctx, typ: Any) -> dict | None:
    return next((p for p in rows(ctx, 'leave_policies') if p.get('type') == typ), None)


def _leave_days_of(ls: list) -> Any:
    s: Any = 0
    for x in ls:
        s += 0.5 if x.get('portion') == 'half' else 1
    return s


def _months_idx(ym: str) -> Any:
    return js_number(ym[0:4]) * 12 + js_number(ym[5:7])


def balance_for(ctx: Ctx, u: dict, typ: Any, as_of: Any = UNDEF, exclude_request: Any = UNDEF) -> dict | None:
    """Balance of one leave type for a person in the FY that contains `as_of`."""
    if as_of is UNDEF:
        as_of = ctx.today
    p = _policy_of(ctx, typ)
    if not p:
        return None
    fm = prop(_S(ctx), 'company.fy_start_month')
    fy = fy_start_year(as_of, fm)
    rg = fy_range(fy, fm)
    frm, to = rg['from'], rg['to']
    start_month = month_of(u['joined_on']) if u['joined_on'] > frm else month_of(frm)
    months_in_fy = js_max(0, _months_idx(month_of(to)) - _months_idx(start_month) + 1)
    months_so_far = js_max(0, js_min(months_in_fy, _months_idx(month_of(as_of)) - _months_idx(start_month) + 1))
    accrued = half((p['annual_days'] * months_in_fy) / 12) if p['accrual'] == 'upfront' else half((p['annual_days'] * months_so_far) / 12)
    # carry forward from last FY (unused, capped)
    if p['carry_forward_max'] > 0 and u['joined_on'] < frm:
        prev = fy_range(fy - 1, fm)
        used_prev = _leave_days_of(
            [x for x in rows(ctx, 'leave') if x.get('user_id') == u['id'] and x.get('type') == typ and prev['from'] <= x['date'] <= prev['to']]
        )
        accrued += js_min(p['carry_forward_max'], js_max(0, p['annual_days'] - used_prev))
    used = _leave_days_of([x for x in rows(ctx, 'leave') if x.get('user_id') == u['id'] and x.get('type') == typ and frm <= x['date'] <= to])
    pending: Any = 0
    for r in rows(ctx, 'leave_requests'):
        if r.get('user_id') == u['id'] and r.get('type') == typ and r.get('status') == 'pending' and r['id'] != exclude_request and frm <= r['from_on'] <= to:
            pending += r['days']
    return {
        'type': typ,
        'annual': p['annual_days'],
        'accrued': accrued,
        'used': used,
        'pending': pending,
        'remaining': r2(accrued - used - pending),
        'accrual': p['accrual'],
    }


def _working_dates_in(ctx: Ctx, frm: str, to: str) -> list[str]:
    c = cal(ctx)
    if c.leave_dates is not None:
        from dataclasses import replace

        c = replace(c, leave_dates=None)
    return [d for d in dates_between(frm, to) if is_working_day(d, c)]


def _can_decide_leave(ctx: Ctx, r: dict) -> dict[str, bool]:
    if r['user_id'] == ctx.user_id or r['status'] != 'pending':
        return {'ok': False, 'via': False}
    if _leave_override(ctx):
        return {'ok': True, 'via': False}
    if r.get('approver_id') == ctx.user_id:
        return {'ok': True, 'via': False}
    if js_truthy(r.get('approver_id')) and r['approver_id'] in _delegators_of(ctx, ctx.user_id):
        return {'ok': True, 'via': True}
    return {'ok': False, 'via': False}


def _leave_view(ctx: Ctx, r: dict) -> dict[str, Any]:
    u = _user(ctx, r['user_id'])
    d = _can_decide_leave(ctx, r)
    can_cancel = r['user_id'] == ctx.user_id and not ctx.view_as and (r['status'] == 'pending' or (r['status'] == 'approved' and r['from_on'] > ctx.today))
    pend = r['status'] == 'pending'
    # step_label / route are v4 workflow extras (multi-step leave chains); not in the shared type.
    return {
        **r,
        'user_name': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
        'user_hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
        'approver_name': user_name(ctx, r['approver_id']) if js_truthy(r.get('approver_id')) else None,
        'can_approve': d['ok'] and not ctx.view_as,
        'can_cancel': can_cancel,
        'via_delegation': d['via'],
        'step_label': step_label(r) if pend else None,
        'route': approval_route(ctx, r, pend),
    }


def _leave_legacy(ctx: Ctx, user_id: Any) -> Any:
    """Leave routing: the v3 rule (reports-to, else first admin, top person auto-approved)."""
    from app.routes.timesheets import resolve_approver

    u = _user(ctx, user_id)
    return resolve_approver(ctx, u) if u else None


def _write_leave_entry(ctx: Ctx, lv: dict) -> bool:
    """Timesheet leave line for an approved leave day (mirrors the timesheet module's own behaviour)."""
    from app.routes.timesheets import find_week

    monday = week_start(lv['date'])
    w = find_week(ctx, lv['user_id'], monday)
    if w and w.get('status') in ('approved', 'submitted'):
        return False
    if not w:
        w = insert(
            ctx,
            'timesheet_weeks',
            {
                'id': f'wk_{re.sub(r"^u_", "", lv["user_id"], count=1)}_{monday}',
                'user_id': lv['user_id'],
                'week_start': monday,
                'status': 'open',
                'submitted_at': None,
                'approver_id': None,
                'decided_by': None,
                'decided_at': None,
                'late': False,
                'reject_reason': None,
                'unlock_requested_at': None,
                'unlock_reason': None,
                'unlock_until': None,
            },
        )
    min_h = prop(_S(ctx), 'calendar.min_hours_per_day')
    db = get_db()
    tid, wid, wd = ctx.tenant_id, w['id'], lv['date']
    db.timesheet_entries = [
        e for e in db.timesheet_entries if not (e.get('tenant_id') == tid and e.get('week_id') == wid and e.get('kind') == 'leave' and e.get('work_date') == wd)
    ]
    insert(
        ctx,
        'timesheet_entries',
        {
            'week_id': w['id'],
            'user_id': lv['user_id'],
            'work_date': lv['date'],
            'kind': 'leave',
            'task_id': None,
            'internal_code': None,
            'leave_type': lv['type'],
            'hours': min_h if lv['portion'] == 'full' else min_h / 2,
            'note': 'Approved leave request',
        },
    )
    w = scope().find_by('timesheet_weeks', 'id', w['id']) or w
    patch('timesheet_weeks', w, {'version': w['version'] + 1})
    return True


def _remove_leave_entry(ctx: Ctx, lv: dict) -> None:
    from app.routes.timesheets import find_week

    w = find_week(ctx, lv['user_id'], week_start(lv['date']))
    if not w or w.get('status') == 'approved':
        return
    db = get_db()
    tid, wid, wd = ctx.tenant_id, w['id'], lv['date']
    db.timesheet_entries = [
        e for e in db.timesheet_entries if not (e.get('tenant_id') == tid and e.get('week_id') == wid and e.get('kind') == 'leave' and e.get('work_date') == wd)
    ]
    w = scope().find_by('timesheet_weeks', 'id', w['id']) or w
    patch('timesheet_weeks', w, {'version': w['version'] + 1})


def _create_leave_rows(ctx: Ctx, r: dict, approved_by: Any) -> int:
    existing = {x['date'] for x in rows(ctx, 'leave') if x.get('user_id') == r['user_id']}
    skipped_weeks = 0
    n = 0
    for d in _working_dates_in(ctx, r['from_on'], r['to_on']):
        if d in existing:
            continue
        lv = insert(ctx, 'leave', {'user_id': r['user_id'], 'date': d, 'type': r['type'], 'portion': r['portion'], 'source': 'hr', 'approved_by': approved_by})
        if not _write_leave_entry(ctx, lv):
            skipped_weeks += 1
        n += 1
    if skipped_weeks:
        warn(
            ctx,
            'LEAVE_WEEK_SUBMITTED',
            f'{skipped_weeks} day{"s fall" if skipped_weeks > 1 else " falls"} in a submitted or approved timesheet week, so the timesheet line was not added there.',
        )
    return n


# =====================================================================================================================
# Performance metrics
# =====================================================================================================================
def _timesheet_compliance(ctx: Ctx, u: dict, frm: str, to: str) -> dict[str, Any]:
    # count from when the company started using timesheets (or the person joined), whichever is later
    adopted = ctx.today
    for w in rows(ctx, 'timesheet_weeks'):
        if w['week_start'] < adopted:
            adopted = w['week_start']
    start = week_start(max_iso(max_iso(frm, u['joined_on']), adopted))
    last_done = add_days(week_start(ctx.today), -1)  # last completed Sunday
    end = min_iso(to, last_done)
    by: dict = {}
    for w in rows(ctx, 'timesheet_weeks'):
        if w.get('user_id') == u['id']:
            by[w['week_start']] = w
    expected = approved = late = 0
    m = start
    while add_days(m, 6) <= end:
        expected += 1
        w = by.get(m)
        if w and w.get('status') == 'approved':
            approved += 1
        if w and js_truthy(w.get('late')):
            late += 1
        m = add_days(m, 7)
    return {'expected': expected, 'approved': approved, 'late': late, 'pct': approved / expected if expected else None}


def _approval_sla(ctx: Ctx, manager_id: Any, frm: str, to: str) -> Any:
    users = {u['id']: u for u in rows(ctx, 'users')}
    decided = []
    for w in rows(ctx, 'timesheet_weeks'):
        if not js_truthy(w.get('submitted_at')) or not js_truthy(w.get('decided_at')) or w.get('decided_by') != manager_id:
            continue
        d = w['decided_at'][:10]
        if d < frm or d > to:
            continue
        owner = users.get(w.get('user_id'))
        if owner and owner['id'] != manager_id:
            decided.append(w)
    if not decided:
        return None
    return sum(1 for w in decided if w['decided_at'][:10] <= sla_due(ctx, w['submitted_at'])) / len(decided)


def _goals_of(ctx: Ctx, user_id: Any, frm: Any = UNDEF, to: Any = UNDEF, cycle_id: Any = UNDEF) -> list[dict]:
    cycles = {c['id']: c for c in rows(ctx, 'appraisal_cycles')}
    out = []
    for g in rows(ctx, 'goals'):
        if g.get('user_id') != user_id:
            continue
        if js_truthy(cycle_id):
            if g.get('cycle_id') == cycle_id:
                out.append(g)
            continue
        if not js_truthy(frm) or not js_truthy(to):
            out.append(g)
            continue
        c = cycles.get(g['cycle_id']) if js_truthy(g.get('cycle_id')) else None
        if c:
            if c['period_from'] <= to and c['period_to'] >= frm:
                out.append(g)
            continue
        if frm <= g['due_on'] <= to:
            out.append(g)
    return out


def _goals_score(gs: list[dict]) -> Any:
    counted = [g for g in gs if js_truthy(g.get('manager_approved')) and g['weight'] > 0]
    w: Any = 0
    for g in counted:
        w += g['weight']
    if not w:
        return None
    s: Any = 0
    for g in counted:
        s += g['weight'] * js_min(1, g['current'] / g['target'] if js_truthy(g.get('target')) else 0)
    return s / w


def _compute_metrics(ctx: Ctx, a: dict, c: dict) -> dict[str, Any]:
    u = _user(ctx, a['user_id'])
    ts = task_stats(ctx, {'userIds': [a['user_id']], 'from': c['period_from'], 'to': c['period_to']})
    tc = _timesheet_compliance(ctx, u, c['period_from'], c['period_to'])  # type: ignore[arg-type]
    gs = _goals_score(_goals_of(ctx, a['user_id'], UNDEF, UNDEF, c['id']))
    is_mgr = any(x.get('reports_to_id') == a['user_id'] for x in rows(ctx, 'users'))
    sla = _approval_sla(ctx, a['user_id'], c['period_from'], c['period_to']) if is_mgr else None

    def scale(x: Any) -> Any:
        return r1(js_max(1, js_min(5, 1 + 4 * x)))

    score_tasks = None
    if ts['completed'] > 0 and ts['on_time_pct'] is not None:
        parts: list[tuple[Any, Any]] = [(ts['on_time_pct'], 0.6)]
        if ts['first_pass_pct'] is not None:
            parts.append((ts['first_pass_pct'], 0.2))
        if ts['avg_quality'] is not None:
            parts.append(((ts['avg_quality'] - 1) / 4, 0.2))
        if sla is not None:
            parts.append((sla, 0.15))
        wsum: Any = 0
        for p in parts:
            wsum += p[1]
        acc: Any = 0
        for p in parts:
            acc += p[0] * p[1]
        score_tasks = scale(acc / wsum)
    score_ts = None if tc['pct'] is None else r1(js_max(1, scale(tc['pct']) - js_min(0.5, 0.1 * tc['late'])))
    score_goals = None if gs is None else scale(gs)
    score_beh = a.get('manager_rating', UNDEF)
    w = c['weights']
    comps = [(score_goals, w['goals']), (score_tasks, w['tasks']), (score_ts, w['timesheets']), (score_beh, w['behaviour'])]
    avail = [x for x in comps if x[0] is not None and x[1] > 0]
    ws: Any = 0
    for x in avail:
        ws += x[1]
    if ws:
        acc2: Any = 0
        for x in avail:
            acc2 += (NAN if x[0] is UNDEF else x[0]) * x[1]
        suggested = r1(acc2 / ws)
    else:
        suggested = None
    return {
        'on_time_pct': ts['on_time_pct'],
        'avg_variance_days': None if ts['avg_variance_days'] is None else r2(ts['avg_variance_days']),
        'first_pass_pct': ts['first_pass_pct'],
        'avg_quality': None if ts['avg_quality'] is None else r2(ts['avg_quality']),
        'tasks_completed': ts['completed'],
        'timesheet_compliance': tc['pct'],
        'late_submissions': tc['late'],
        'goals_score': gs,
        'approval_sla_pct': sla,
        'score_tasks': score_tasks,
        'score_timesheets': score_ts,
        'score_goals': score_goals,
        'score_behaviour': score_beh,
        'suggested': suggested,
    }


def _goal_view(ctx: Ctx, g: dict) -> dict[str, Any]:
    own = g['user_id'] == ctx.user_id
    mgr = _is_above(ctx, ctx.user_id, g['user_id']) or _is_hr(ctx)
    return {
        **g,
        'user_name': user_name(ctx, g['user_id']),
        'progress': g['current'] / g['target'] if js_truthy(g.get('target')) else 0,
        'can_edit': not ctx.view_as and (own or mgr),
        'can_approve': not ctx.view_as and not own and mgr and not js_truthy(g.get('manager_approved')),
        # only a manager (or HR) closes a goal as achieved or missed — the same rule PATCH /goals/:id enforces
        'can_close': not ctx.view_as and mgr,
    }


def _feedback_visible(ctx: Ctx, f: dict) -> bool:
    if f.get('from_user_id') == ctx.user_id:
        return True
    if f.get('visibility') == 'public':
        return _can_view_person(ctx, f['to_user_id'])
    if f.get('visibility') == 'private':
        return f.get('to_user_id') == ctx.user_id
    # manager-only notes: the recipient's managers and HR, not the recipient
    return f.get('to_user_id') != ctx.user_id and (_is_above(ctx, ctx.user_id, f['to_user_id']) or _sees_all_people(ctx))


def _feedback_view(ctx: Ctx, f: dict) -> dict[str, Any]:
    frm = _user(ctx, f.get('from_user_id'))
    return {
        **f,
        'from_name': coalesce(frm.get('name', UNDEF) if frm else UNDEF, '—'),
        'from_hue': coalesce(frm.get('avatar_hue', UNDEF) if frm else UNDEF, 150),
        'to_name': user_name(ctx, f.get('to_user_id')),
    }


# =====================================================================================================================
# Appraisals
# =====================================================================================================================
def _cycle_view(ctx: Ctx, c: dict) -> dict[str, Any]:
    counts: dict[str, Any] = {'not_started': 0, 'self_pending': 0, 'manager_pending': 0, 'hr_pending': 0, 'completed': 0}
    as_ = [a for a in rows(ctx, 'appraisals') if a.get('cycle_id') == c['id']]
    for a in as_:
        s = a.get('status')
        counts[s] = counts[s] + 1 if s in counts else NAN
    return {**c, 'counts': counts, 'total': len(as_)}


def sync_cycle(ctx: Ctx, cycle_id: Any) -> dict:
    """A running cycle's phase follows its due dates: self review until self_due, manager review until manager_due, then HR."""
    c = find(ctx, 'appraisal_cycles', cycle_id)
    if c['status'] in ('draft', 'closed', 'goal_setting'):
        return c
    st = 'self_review' if ctx.today <= c['self_due'] else 'manager_review' if ctx.today <= c['manager_due'] else 'hr_review'
    return update(ctx, 'appraisal_cycles', c['id'], {'status': st}) if st != c['status'] else c


def _sweep_cycles(ctx: Ctx) -> None:
    for c in list(rows(ctx, 'appraisal_cycles')):
        if c['status'] not in ('draft', 'closed'):
            sync_cycle(ctx, c['id'])


def _due_of(a: dict, c: dict) -> Any:
    s = a['status']
    return c['self_due'] if s == 'self_pending' else c['manager_due'] if s == 'manager_pending' else c['hr_due'] if s == 'hr_pending' else None


def _can_see_appraisal(ctx: Ctx, a: dict) -> bool:
    return a['user_id'] == ctx.user_id or _is_appraisal_admin(ctx) or a.get('manager_id') == ctx.user_id or _is_above(ctx, ctx.user_id, a['user_id'])


def _appraisal_row(ctx: Ctx, a: dict, c: dict) -> dict[str, Any]:
    u = _user(ctx, a['user_id'])
    done = a['status'] == 'completed'
    m = a['metrics'] if done else _compute_metrics(ctx, a, c)
    own = a['user_id'] == ctx.user_id and not _is_appraisal_admin(ctx)
    due = _due_of(a, c)
    return {
        'id': a['id'],
        'cycle_id': a['cycle_id'],
        'user_id': a['user_id'],
        'user_name': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
        'user_title': coalesce(u.get('title', UNDEF) if u else UNDEF, ''),
        'user_hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
        'manager_id': prop(a, 'manager_id'),
        'manager_name': user_name(ctx, a['manager_id']) if js_truthy(a.get('manager_id')) else None,
        'status': a['status'],
        'self_rating': prop(a, 'self_rating'),
        'manager_rating': None if own and not done else prop(a, 'manager_rating'),
        'final_rating': prop(a, 'final_rating'),
        'suggested': None if own and not done else coalesce(prop(m, 'suggested'), None),
        'on_time_pct': coalesce(prop(m, 'on_time_pct'), None),
        'increment_pct': None if own and not done else prop(a, 'increment_pct'),
        'overdue': js_truthy(due) and due < ctx.today and c['status'] != 'closed',
        'due_on': due,
    }


def _get_appraisal(ctx: Ctx, id_: Any) -> dict:
    a = find(ctx, 'appraisals', id_, 'Appraisal')
    if not _can_see_appraisal(ctx, a):
        err(403, 'FORBIDDEN', 'You can only open your own appraisal or those of people who report to you.')
    return a


# =====================================================================================================================
# Lifecycle
# =====================================================================================================================
ONBOARDING = [
    ('offer_signed', 'Offer letter signed', 'ops', -7),
    ('documents', 'Documents collected (ID, PAN, bank)', 'ops', -3),
    ('laptop', 'Laptop issued (asset)', 'it', 0),
    ('accounts', 'Email & accounts created', 'it', 0),
    ('buddy', 'Buddy assigned', 'manager', 0),
    ('policies', 'Policy acknowledgements', 'employee', 3),
    ('checkin_30', '30-day check-in', 'manager', 30),
    ('checkin_60', '60-day check-in', 'manager', 60),
    ('checkin_90', '90-day check-in', 'manager', 90),
]


def _items(tpl: list) -> list[dict]:
    return [{'id': i, 'label': lb, 'owner_role': o, 'due_offset_days': d, 'done_on': None, 'done_by': None} for i, lb, o, d in tpl]


def _offboarding_items(start: str, last_day: str) -> list[dict]:
    ld = diff_days(last_day, start)
    return _items(
        [
            ('resignation', 'Resignation received', 'ops', 0),
            ('notice', 'Notice period agreed', 'manager', 2),
            ('kt', 'Knowledge transfer', 'manager', js_max(2, ld - 5)),
            ('exit_interview', 'Exit interview', 'ops', js_max(2, ld - 2)),
            ('asset_return', 'Asset return', 'it', ld),
            ('access', 'Access revoked', 'it', ld),
            ('fnf', 'Full & final settlement', 'finance', ld + 30),
            ('experience_letter', 'Experience letter issued', 'ops', ld + 7),
        ]
    )


def _onboarding_items() -> list[dict]:
    return _items(ONBOARDING)


def _person_of(ctx: Ctx, cl: dict) -> tuple[dict[str, Any], dict]:
    """A checklist's person is a user, or a pending invite (hired candidates before they accept). Relinks on acceptance.
    Returns (person, the current checklist row) — the TS relinks `cl.user_id` in place."""
    u = _user(ctx, cl.get('user_id'))
    if u:
        return (
            {
                'name': u['name'],
                'title': prop(u, 'title'),
                'hue': prop(u, 'avatar_hue'),
                'status': 'invited' if u.get('status') == 'invited' else prop(u, 'status'),
                'user': u,
                'manager': prop(u, 'reports_to_id'),
            },
            cl,
        )
    inv = next((i for i in rows(ctx, 'invites') if i['id'] == cl.get('user_id')), None)
    if inv:
        e = inv['email'].lower()
        accepted = next((x for x in rows(ctx, 'users') if x['email'].lower() == e), None)
        if accepted:
            cl = patch('lifecycle_checklists', cl, {'user_id': accepted['id']})
            return _person_of(ctx, cl)
        return (
            {'name': inv['name'], 'title': 'Joining · invite pending', 'hue': 200, 'status': 'invited', 'user': None, 'manager': prop(inv, 'reports_to_id')},
            cl,
        )
    return ({'name': '—', 'title': '', 'hue': 150, 'status': 'deactivated', 'user': None, 'manager': None}, cl)


def _deactivate_user(ctx: Ctx, u: dict, why: str) -> None:
    if u.get('status') == 'deactivated':
        return
    update(ctx, 'users', u['id'], {'status': 'deactivated'})
    d = get_db()
    uid_ = u['id']
    d.sessions = [s for s in d.sessions if s['user_id'] != uid_]
    log_activity(ctx, 'user', uid_, 'deactivate', f'Deactivated {u["name"]} — {why}')


def _sweep_exits(ctx: Ctx) -> None:
    """Exits take effect when the offboarding checklist is complete and the last day has passed."""
    for cl in list(rows(ctx, 'lifecycle_checklists')):
        if cl.get('kind') != 'offboarding' or cl.get('status') != 'completed' or not js_truthy(cl.get('last_day')) or cl['last_day'] > ctx.today:
            continue
        u = _user(ctx, cl.get('user_id'))
        if u and u.get('status') == 'active':
            _deactivate_user(ctx, u, f'exit completed, last day {fmt_date(cl["last_day"], "long")}')


def _can_toggle_item(ctx: Ctx, cl: dict, item: dict, p: dict) -> bool:
    if ctx.view_as:
        return False
    if _is_hr(ctx):
        return True
    if item.get('owner_role') == 'manager':
        return p['manager'] == ctx.user_id
    if item.get('owner_role') == 'employee':
        return cl.get('user_id') == ctx.user_id
    return False


def _checklist_view(ctx: Ctx, cl: dict) -> dict[str, Any]:
    p, cl = _person_of(ctx, cl)
    items = []
    for i in cl['items']:
        due = add_days(cl['start_on'], i['due_offset_days'])
        items.append(
            {
                **i,
                'due_on': due,
                'overdue': not js_truthy(i.get('done_on')) and due < ctx.today,
                'done_by_name': user_name(ctx, i['done_by']) if js_truthy(i.get('done_by')) else None,
                'can_toggle': _can_toggle_item(ctx, cl, i, p),
            }
        )
    pu = p['user']
    assets_out = sum(1 for a in rows(ctx, 'assets') if a.get('assigned_to') == pu['id'] and a.get('status') == 'assigned') if pu else 0
    return {
        **cl,
        'items': items,
        'person_name': p['name'],
        'person_title': p['title'],
        'person_hue': p['hue'],
        'person_status': p['status'],
        'done': sum(1 for i in items if js_truthy(i.get('done_on'))),
        'total': len(items),
        'overdue': sum(1 for i in items if i['overdue']),
        'assets_out': assets_out,
    }


# =====================================================================================================================
# Recruitment
# =====================================================================================================================
STAGES = ['applied', 'screening', 'interview', 'offer', 'hired', 'rejected', 'withdrawn']
_CLOSED_STAGES = ('hired', 'rejected', 'withdrawn')


def _hired_on(ctx: Ctx, c: dict) -> str:
    lg = next((x for x in rows_where_ro(ctx, 'activity_log', 'entity_id', c['id']) if x.get('entity') == 'candidate' and x.get('action') == 'hired'), None)
    return coalesce(lg.get('at', UNDEF) if lg else UNDEF, c['updated_at'])[:10]


def _candidate_row(ctx: Ctx, c: dict) -> dict[str, Any]:
    ivs = [i for i in rows(ctx, 'interviews') if i.get('candidate_id') == c['id']]
    o = next((x for x in rows(ctx, 'openings') if x['id'] == c.get('opening_id')), None)
    nx = sorted((i for i in ivs if i.get('result') == 'pending' and i['scheduled_at'][:10] >= ctx.today), key=lambda i: locale_key(i['scheduled_at']))
    f = next((x for x in rows(ctx, 'files') if x['id'] == c['resume_file_id']), None) if js_truthy(c.get('resume_file_id')) else None
    end = _hired_on(ctx, c) if c.get('stage') == 'hired' else ctx.today
    return {
        **c,
        'offer_ctc_paise': prop(c, 'offer_ctc_paise'),
        'opening_role': coalesce(o.get('role', UNDEF) if o else UNDEF, '—'),
        'interviews_total': len(ivs),
        'interviews_passed': sum(1 for i in ivs if i.get('result') == 'pass'),
        'next_interview': nx[0]['scheduled_at'] if nx else None,
        'resume_name': coalesce(f.get('name', UNDEF) if f else UNDEF, None),
        'days_in_pipeline': js_max(0, diff_days(end, c['created_at'][:10])),
    }


def _email_taken(ctx: Ctx, email: str) -> str | None:
    e = email.lower()
    if any(u['email'].lower() == e for u in rows(ctx, 'users')):
        return 'Someone with this email already has an account.'
    if any(not js_truthy(i.get('accepted_at')) and i['email'].lower() == e for i in rows(ctx, 'invites')):
        return 'An invite is already pending for this email.'
    return None


# =====================================================================================================================
# Assets
# =====================================================================================================================
def _asset_view(ctx: Ctx, a: dict) -> dict[str, Any]:
    u = _user(ctx, a.get('assigned_to'))
    offboarding = (
        any(c.get('kind') == 'offboarding' and c.get('user_id') == u['id'] and c.get('status') == 'open' for c in rows(ctx, 'lifecycle_checklists'))
        if u
        else False
    )
    return {
        **a,
        'assignee_name': coalesce(u.get('name', UNDEF), None) if u else None,
        'assignee_status': ('invited' if u.get('status') == 'invited' else prop(u, 'status')) if u else None,
        'pending_return': a.get('status') == 'assigned' and bool(u) and (u.get('status') == 'deactivated' or offboarding),  # type: ignore[union-attr]
        'warranty_expired': js_truthy(a.get('warranty_until')) and a['warranty_until'] < ctx.today,
    }


ASSET_CATS = ['laptop', 'monitor', 'phone', 'licence', 'furniture', 'other']


def _in(v: Any, options: Any) -> bool:
    """options.includes(v) for string options (strict equality)"""
    return isinstance(v, str) and v in options


# =====================================================================================================================
# Routes — leave
# =====================================================================================================================
def leave_taken(req: Req) -> Any:
    """Leave taken this financial year by person x type (Feature Guide §16). "Taken" counts only days up to today; later
    dates are "planned" and returned separately. Half days stay their own portion column (img-033)."""
    ctx = req.ctx
    hr = has(ctx, 'leave.hr_entry')
    reports = [u for u in rows(ctx, 'users') if u.get('reports_to_id') == ctx.user_id and u.get('status') != 'invited']
    see_all = hr or has(ctx, 'people.view_all')
    if not see_all and not reports:
        err(403, 'FORBIDDEN', 'The leave summary is for HR and managers.')
    fm = prop(_S(ctx), 'company.fy_start_month')
    fy_start = js_number(req.q('fy')) if js_truthy(req.q('fy')) else fy_start_year(ctx.today, fm)
    rg = fy_range(fy_start, fm)
    frm, to = rg['from'], rg['to']
    min_h = prop(_S(ctx), 'calendar.min_hours_per_day')
    base = [u for u in rows(ctx, 'users') if u.get('status') != 'invited' and not u.get('is_super_admin')] if see_all else [ctx.user, *reports]
    people = sorted(base, key=lambda u: locale_key(u['name']))
    leave_by: dict = {}
    for lv in rows(ctx, 'leave'):
        if frm <= lv['date'] <= to:
            leave_by.setdefault(lv.get('user_id'), []).append(lv)
    hours_by: dict = {}
    for e in rows(ctx, 'timesheet_entries'):
        if e.get('kind') == 'leave' and frm <= e['work_date'] <= to and e['work_date'] <= ctx.today:
            hours_by[e.get('user_id')] = hours_by.get(e.get('user_id'), 0) + e['hours']

    def count(ls: list) -> dict[str, Any]:
        tot: Any = 0
        for x in ls:
            tot += 0.5 if x.get('portion') == 'half' else 1
        return {
            'by_type': {t: sum(1 for x in ls if x.get('type') == t and x.get('portion') == 'full') for t in LEAVE_TYPES},
            'half_days': sum(1 for x in ls if x.get('portion') == 'half') * 0.5,
            'total': r2(tot),
        }

    out = []
    for u in people:
        mine = leave_by.get(u['id'], [])
        taken = count([x for x in mine if x['date'] <= ctx.today])
        later = sorted((x for x in mine if x['date'] > ctx.today), key=lambda x: locale_key(x['date']))
        planned = {**count(later), 'dates': [{'date': x['date'], 'type': x['type'], 'portion': x['portion']} for x in later]}
        timesheet_days = r2(hours_by.get(u['id'], 0) / min_h)
        row = {
            'user': {'id': u['id'], 'name': prop(u, 'name'), 'title': prop(u, 'title'), 'avatar_hue': prop(u, 'avatar_hue')},
            **taken,
            'timesheet_days': timesheet_days,
            'planned': planned,
        }
        if row['total'] > 0 or timesheet_days > 0 or planned['total'] > 0:
            out.append(row)
    return {'fy_label': fy_label(fy_start), 'from': frm, 'to': to, 'as_of': ctx.today, 'min_hours': min_h, 'types': LEAVE_TYPES, 'rows': out}


def list_leave_requests(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user
    hr = _is_hr(ctx)
    all_ = sorted(rows(ctx, 'leave_requests'), key=lambda x: locale_key(x['from_on']), reverse=True)
    mine = [_leave_view(ctx, x) for x in all_ if x['user_id'] == me['id']]
    dl = _delegators_of(ctx, me['id'])

    def mine_to_approve(x: dict) -> bool:
        return x['user_id'] != me['id'] and (x.get('approver_id') == me['id'] or (js_truthy(x.get('approver_id')) and x['approver_id'] in dl))

    to_approve = [_leave_view(ctx, x) for x in all_ if _can_decide_leave(ctx, x)['ok'] and (mine_to_approve(x) or _leave_override(ctx))]
    team = [
        _leave_view(ctx, x)
        for x in [
            x for x in all_ if x['user_id'] != me['id'] and (hr or _sees_all_people(ctx) or mine_to_approve(x) or _is_above(ctx, me['id'], x['user_id']))
        ][:200]
    ]
    balances = [b for b in (balance_for(ctx, me, t) for t in LEAVE_TYPES) if b]
    fm = prop(_S(ctx), 'company.fy_start_month')

    def type_rank(p: dict) -> int:
        t = p.get('type')
        return LEAVE_TYPES.index(t) if t in LEAVE_TYPES else -1

    return {
        'mine': mine,
        'to_approve': to_approve,
        'team': team,
        'balances': balances,
        'policies': sorted(rows(ctx, 'leave_policies'), key=type_rank),
        'can_manage_policy': hr and not ctx.view_as,
        'is_approver': any(u.get('reports_to_id') == me['id'] and u.get('status') == 'active' for u in rows(ctx, 'users'))
        or len(dl) > 0
        or _leave_override(ctx),
        'fy_label': fy_label(fy_start_year(ctx.today, fm)),
    }


def create_leave_request(req: Req) -> Any:
    ctx = req.ctx
    me = find(ctx, 'users', ctx.user_id)
    typ = req.b('type')
    if not _in(typ, LEAVE_TYPES):
        err(422, 'VALIDATION', 'Choose a leave type.', 'type')
    portion = 'half' if req.b('portion') == 'half' else 'full'
    frm = _str(req.b('from_on'))
    to_raw = req.b('to_on')
    to = frm if portion == 'half' else _str(to_raw if js_truthy(to_raw) else frm)
    if not _is_date(frm):
        err(422, 'VALIDATION', 'Pick the first day of leave.', 'from_on')
    if not _is_date(to):
        err(422, 'VALIDATION', 'Pick the last day of leave.', 'to_on')
    if to < frm:
        err(422, 'DATE_RANGE', 'The last day is before the first day.', 'to_on')
    if diff_days(to, frm) > 60:
        err(422, 'DATE_RANGE', 'Apply for at most 60 days at a time.', 'to_on')
    if frm < add_days(ctx.today, -30):
        err(422, 'DATE_RANGE', 'Leave more than 30 days in the past has to be entered by HR.', 'from_on')
    reason = _str(req.b('reason'))
    if js_len(reason) < 5:
        err(422, 'VALIDATION', 'Give a reason of at least 5 characters.', 'reason')
    dates = _working_dates_in(ctx, frm, to)
    if not dates:
        err(
            422,
            'LEAVE_INVALID',
            f'{fmt_date(frm, "long")} is a weekly off or holiday, so no leave is needed.'
            if frm == to
            else 'Every day in that range is a weekly off or holiday.',
            'from_on',
        )
    if typ == 'optional_holiday':
        opt = {h['date'] for h in rows(ctx, 'holidays') if js_truthy(h.get('optional'))}
        bad = next((d for d in dates if d not in opt), None)
        if bad:
            err(
                422,
                'LEAVE_INVALID',
                f'{fmt_date(bad, "long")} isn’t an optional holiday. Apply for an optional holiday on a date from the holiday calendar.',
                'from_on',
            )
    days: Any = 0.5 if portion == 'half' else len(dates)
    # overlap with other requests or recorded leave
    clash = next(
        (
            x
            for x in rows(ctx, 'leave_requests')
            if x['user_id'] == me['id'] and x['status'] in ('pending', 'approved') and x['from_on'] <= to and x['to_on'] >= frm
        ),
        None,
    )
    if clash:
        rng = f' – {fmt_date(clash["to_on"])}' if clash['to_on'] != clash['from_on'] else ''
        err(
            409,
            'LEAVE_OVERLAP',
            f'You already have a {clash["status"]} request for {fmt_date(clash["from_on"])}{rng}. Cancel it or pick other dates.',
            'from_on',
        )
    ds = set(dates)
    recorded = next((x for x in rows(ctx, 'leave') if x.get('user_id') == me['id'] and x['date'] in ds), None)
    if recorded:
        err(409, 'LEAVE_OVERLAP', f'Leave is already recorded on {fmt_date(recorded["date"], "long")}.', 'from_on')
    if typ != 'lwp':
        b = balance_for(ctx, me, typ, max_iso(frm, ctx.today))
        if b and days > b['remaining']:
            rem = b['remaining']
            err(
                422,
                'LEAVE_BALANCE',
                f'Not enough {LEAVE_TYPE[typ].lower()} leave: {js_str(rem)} day{"" if rem == 1 else "s"} left, this request needs {js_str(days)}. Pick fewer days or apply as leave without pay.',
                'type',
            )
    from app.routes.timesheets import resolve_approver

    wf = start_approval(ctx, 'leave', {'requester_id': me['id']}, resolve_approver(ctx, me))
    approver = wf['approver_id']
    row = insert(
        ctx,
        'leave_requests',
        {
            'user_id': me['id'],
            'from_on': frm,
            'to_on': to,
            'type': typ,
            'portion': portion,
            'days': days,
            'reason': reason,
            'status': 'pending' if approver else 'approved',
            'approver_id': approver,
            'decided_by': None if approver else me['id'],
            'decided_at': None if approver else now_iso(),
            'decision_reason': None if approver else 'Auto-approved (no manager above)',
            **(wf_patch(wf) if approver else {}),
        },
    )
    rng = f' – {fmt_date(to)}' if to != frm else ''
    log_activity(
        ctx,
        'leave_request',
        row['id'],
        'created',
        f'{me["name"]} applied for {js_str(days)} day{"" if days == 1 else "s"} of {LEAVE_TYPE[typ]} leave ({fmt_date(frm)}{rng})',
    )
    if approver:
        notify(
            ctx,
            approver,
            'NTF-04',
            'Leave request',
            f'{me["name"]} asked for {js_str(days)} day{"" if days == 1 else "s"} of {LEAVE_TYPE[typ].lower()} leave from {fmt_date(frm)}: “{reason[:80]}”',
            '/leave',
        )
        for dl in _active_delegations_from(ctx, approver):
            notify(
                ctx,
                dl['to_user_id'],
                'NTF-04',
                'Leave request (delegated)',
                f'{me["name"]} asked for leave from {fmt_date(frm)}. You are covering approvals for {user_name(ctx, approver)}.',
                '/leave',
            )
    else:
        _create_leave_rows(ctx, row, me['id'])
    return scope().find_by('leave_requests', 'id', row['id']) or row


def approve_leave(req: Req) -> Any:
    ctx = req.ctx
    r = find(ctx, 'leave_requests', req.params['id'], 'Leave request')
    if r['status'] != 'pending':
        err(409, 'NOT_PENDING', f'This request is already {r["status"]}.')
    require_action(ctx, 'approvals.act')
    d = _can_decide_leave(ctx, r)
    if not d['ok']:
        err(
            403,
            'FORBIDDEN',
            'You can’t approve your own leave.' if r['user_id'] == ctx.user_id else 'Only the person’s manager (or their delegate) can decide this request.',
        )
    # re-check balance at decision time (policy may have changed)
    person = find(ctx, 'users', r['user_id'])
    if r['type'] != 'lwp':
        b = balance_for(ctx, person, r['type'], max_iso(r['from_on'], ctx.today), r['id'])
        if b and r['days'] > b['remaining']:
            rem = b['remaining']
            err(
                422,
                'LEAVE_BALANCE',
                f'{person["name"]} has only {js_str(rem)} day{"" if rem == 1 else "s"} of {LEAVE_TYPE[r["type"]].lower()} leave left. Reject it, or ask them to re-apply as leave without pay.',
            )
    me_name = js_str(ctx.user.get('name', UNDEF))
    # Multi-step leave workflows: hand over to the next step before anything is recorded.
    st = ensure_wf(ctx, r, 'leave', {'requester_id': r['user_id']}, _leave_legacy(ctx, r['user_id']))
    adv = advance_approval(ctx, st, ctx.user_id)
    via = ' as delegate' if d['via'] else ''
    if adv['next']:
        nx = adv['next']
        mid = update(ctx, 'leave_requests', r['id'], {'approver_id': nx['approver_id'], **wf_patch(adv['state'])})
        done_name = coalesce(adv['done'].get('name', UNDEF) if adv['done'] else UNDEF, 'approval')
        log_activity(
            ctx,
            'leave_request',
            r['id'],
            'approved_step',
            f'Step {js_str(st["wf_step"] + 1)} of {len(st["wf_chain"])} ({js_str(done_name)}) approved by {me_name}{via}; sent to {user_name(ctx, nx["approver_id"])} for {js_str(nx.get("name", UNDEF))}',
        )
        notify(
            ctx,
            nx['approver_id'],
            'NTF-04',
            'Leave request',
            f'{person["name"]}’s {LEAVE_TYPE[r["type"]].lower()} leave from {fmt_date(r["from_on"])} was approved by {me_name} and needs your approval ({js_str(nx.get("name", UNDEF))}).',
            '/leave',
        )
        for dl in _active_delegations_from(ctx, nx['approver_id']):
            notify(
                ctx,
                dl['to_user_id'],
                'NTF-04',
                'Leave request (delegated)',
                f'{person["name"]}’s leave from {fmt_date(r["from_on"])} needs approval. You are covering approvals for {user_name(ctx, nx["approver_id"])}.',
                '/leave',
            )
        notify(
            ctx,
            r['user_id'],
            'NTF-05',
            'Leave approved — one more step',
            f'{me_name} approved your leave from {fmt_date(r["from_on"])}. It now needs {user_name(ctx, nx["approver_id"])}’s approval.',
            '/leave',
        )
        warn(
            ctx,
            'NEXT_APPROVAL',
            f'Step {js_str(st["wf_step"] + 1)} of {len(st["wf_chain"])} approved — sent to {user_name(ctx, nx["approver_id"])} for {js_str(nx.get("name", UNDEF)).lower()}.',
        )
        return mid
    out = update(ctx, 'leave_requests', r['id'], {'status': 'approved', 'decided_by': ctx.user_id, 'decided_at': now_iso(), **wf_patch(adv['state'])})
    n = _create_leave_rows(ctx, out, ctx.user_id)
    log_activity(
        ctx,
        'leave_request',
        r['id'],
        'approved',
        f'{me_name} approved {person["name"]}’s {LEAVE_TYPE[r["type"]]} leave ({n} day{"" if n == 1 else "s"} recorded){via}',
    )
    rng = f' to {fmt_date(r["to_on"])}' if r['to_on'] != r['from_on'] else ''
    notify(
        ctx,
        r['user_id'],
        'NTF-05',
        'Leave approved',
        f'{me_name} approved your {LEAVE_TYPE[r["type"]].lower()} leave from {fmt_date(r["from_on"])}{rng}.',
        '/leave',
    )
    return scope().find_by('leave_requests', 'id', r['id']) or out


def reject_leave(req: Req) -> Any:
    ctx = req.ctx
    r = find(ctx, 'leave_requests', req.params['id'], 'Leave request')
    if r['status'] != 'pending':
        err(409, 'NOT_PENDING', f'This request is already {r["status"]}.')
    d = _can_decide_leave(ctx, r)
    if not d['ok']:
        err(
            403,
            'FORBIDDEN',
            'You can’t decide your own leave.' if r['user_id'] == ctx.user_id else 'Only the person’s manager (or their delegate) can decide this request.',
        )
    reason = _str(req.b('reason'))
    if js_len(reason) < 5:
        err(422, 'VALIDATION', 'Tell them why in at least 5 characters.', 'reason')
    out = update(ctx, 'leave_requests', r['id'], {'status': 'rejected', 'decided_by': ctx.user_id, 'decided_at': now_iso(), 'decision_reason': reason})
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(ctx, 'leave_request', r['id'], 'rejected', f'{me_name} rejected {user_name(ctx, r["user_id"])}’s leave: {reason}')
    notify(ctx, r['user_id'], 'NTF-06', 'Leave not approved', f'{me_name} rejected your leave from {fmt_date(r["from_on"])}: “{reason}”', '/leave')
    return out


def cancel_leave(req: Req) -> Any:
    ctx = req.ctx
    r = find(ctx, 'leave_requests', req.params['id'], 'Leave request')
    if r['user_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person who applied can cancel a request.')
    if r['status'] == 'approved':
        if r['from_on'] <= ctx.today:
            err(409, 'LEAVE_STARTED', 'This leave has already started. Ask HR to change the recorded days.')
        dates = set(_working_dates_in(ctx, r['from_on'], r['to_on']))
        for lv in [
            x for x in rows(ctx, 'leave') if x.get('user_id') == r['user_id'] and x['date'] in dates and x.get('type') == r['type'] and x.get('source') == 'hr'
        ]:
            _remove_leave_entry(ctx, lv)
            remove(ctx, 'leave', lv['id'])
    elif r['status'] != 'pending':
        err(409, 'NOT_PENDING', f'This request is already {r["status"]}.')
    was = r['status']
    out = update(ctx, 'leave_requests', r['id'], {'status': 'cancelled'})
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(ctx, 'leave_request', r['id'], 'cancelled', f'{me_name} cancelled {was} {LEAVE_TYPE[r["type"]]} leave from {fmt_date(r["from_on"])}')
    if js_truthy(r.get('approver_id')) and r['approver_id'] != ctx.user_id:
        notify(
            ctx,
            r['approver_id'],
            'NTF-04',
            'Leave cancelled',
            f'{me_name} cancelled {"approved" if was == "approved" else "their pending"} leave from {fmt_date(r["from_on"])}.',
            '/leave',
        )
    return out


def put_leave_policy(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins can change leave policy.')
    typ = req.params['type']
    if typ not in LEAVE_TYPES or typ == 'lwp':
        err(422, 'VALIDATION', 'Pick a paid leave type.')
    annual = js_number(req.b('annual_days'))
    carry = js_number(req.b('carry_forward_max'))
    if not is_finite(annual) or annual < 0 or annual > 60 or annual * 2 != js_round(annual * 2):
        err(422, 'VALIDATION', 'Annual days must be between 0 and 60 (half days allowed).', 'annual_days')
    if not is_finite(carry) or carry < 0 or carry > 60:
        err(422, 'VALIDATION', 'Carry-forward must be between 0 and 60 days.', 'carry_forward_max')
    accrual = 'monthly' if req.b('accrual') == 'monthly' else 'upfront'
    cur = next((p for p in rows(ctx, 'leave_policies') if p.get('type') == typ), None)
    if cur:
        out = update(ctx, 'leave_policies', cur['id'], {'annual_days': annual, 'carry_forward_max': carry, 'accrual': accrual}, req.if_match)
    else:
        out = insert(ctx, 'leave_policies', {'type': typ, 'annual_days': annual, 'carry_forward_max': carry, 'accrual': accrual})
    log_activity(
        ctx, 'leave_policy', out['id'], 'updated', f'{LEAVE_TYPE[typ]} leave policy: {js_str(annual)} days/year, {accrual}, carry forward up to {js_str(carry)}'
    )
    return out


# =====================================================================================================================
# Routes — performance
# =====================================================================================================================
def performance_people(req: Req) -> Any:
    ctx = req.ctx
    all_ = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not u.get('is_super_admin')]
    lst = [u for u in all_ if _can_view_person(ctx, u['id'])]
    frm = add_days(ctx.today, -90)
    out = [
        {
            'id': u['id'],
            'name': prop(u, 'name'),
            'title': prop(u, 'title'),
            'avatar_hue': prop(u, 'avatar_hue'),
            'on_time_pct': task_stats(ctx, {'userIds': [u['id']], 'from': frm, 'to': ctx.today})['on_time_pct'],
        }
        for u in lst
    ]
    return sorted(out, key=lambda x: (0, '') if x['id'] == ctx.user_id else (1, locale_key(x['name'])))


def performance_of(req: Req) -> Any:
    ctx = req.ctx
    id_ = ctx.user_id if req.params['userId'] == 'me' else req.params['userId']
    u = find(ctx, 'users', id_, 'Person')
    if not _can_view_person(ctx, u['id']):
        err(403, 'FORBIDDEN', 'You can see your own performance and that of people who report to you.')
    qt, qf = req.q('to'), req.q('from')
    to = qt if _is_date(qt) else ctx.today
    frm = qf if _is_date(qf) else add_days(to, -90)
    ts = task_stats(ctx, {'userIds': [u['id']], 'from': frm, 'to': to})
    tc = _timesheet_compliance(ctx, u, frm, to)
    gs = _goals_of(ctx, u['id'], frm, to)
    is_mgr = any(x.get('reports_to_id') == u['id'] for x in rows(ctx, 'users'))
    kpis = {
        'on_time_pct': ts['on_time_pct'],
        'avg_variance_days': None if ts['avg_variance_days'] is None else r2(ts['avg_variance_days']),
        'first_pass_pct': ts['first_pass_pct'],
        'avg_quality': ts['avg_quality'],
        'completed': ts['completed'],
        'late': ts['late'],
        'overdue_open': ts['overdue_open'],
        'open': ts['open'],
        'timesheet_compliance': tc['pct'],
        'expected_weeks': tc['expected'],
        'approved_weeks': tc['approved'],
        'late_submissions': tc['late'],
        'utilisation': avg_allocation(ctx, u['id'], frm, min_iso(to, ctx.today)) / 100,
        'goals_total': len(gs),
        'goals_achieved': sum(1 for g in gs if g.get('status') == 'achieved' or (g['target'] > 0 and g['current'] >= g['target'])),
        'goals_score': _goals_score(gs),
        'approval_sla_pct': _approval_sla(ctx, u['id'], frm, to) if is_mgr else None,
    }
    trend = []
    anchor = f'{month_of(min_iso(to, ctx.today))}-01'
    for i in range(5, -1, -1):
        ym = month_of(add_months(anchor, -i))
        m = month_range(ym)
        s = task_stats(ctx, {'userIds': [u['id']], 'from': m['from'], 'to': m['to']})
        trend.append({'month': ym, 'label': fmt_date_en_in(parse_iso(m['from']), month='short'), 'on_time_pct': s['on_time_pct'], 'completed': s['completed']})
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    mss = {m['id']: m for m in rows(ctx, 'milestones')}

    def d_of(t: dict) -> str:
        return coalesce(prop(t, 'done_on'), t['due_on'])

    tl = [t for t in rows(ctx, 'tasks') if t.get('owner_id') == u['id'] and frm <= d_of(t) <= to]
    tl = sorted(tl, key=lambda t: locale_key(d_of(t)), reverse=True)[:40]
    tasks = []
    for t in tl:
        tt = task_timeliness(ctx, t)
        p = projects.get(t['project_id'])
        m = mss.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
        code = js_str(coalesce(p.get('code', UNDEF) if p else UNDEF, '')) + (f' {js_str(m["no"])}.{js_str(t["no"])}' if m else f' #{js_str(t["no"])}')
        drc = t.get('delay_reason_code')
        if js_truthy(drc):
            dr = js_str(coalesce(DELAY_REASON_LABEL.get(drc, UNDEF), drc)) + (f': {js_str(t["delay_reason"])}' if js_truthy(t.get('delay_reason')) else '')
        else:
            dr = coalesce(prop(t, 'delay_reason'), None)
        tasks.append(
            {
                'id': t['id'],
                'code': code,
                'title': t['title'],
                'project': coalesce(p.get('name', UNDEF) if p else UNDEF, '—'),
                'status': t['status'],
                'due_on': t['due_on'],
                'done_on': prop(t, 'done_on'),
                'timeliness': tt['timeliness'] if tt else None,
                'variance_days': tt['variance'] if tt else None,
                'delay_reason': dr,
                'quality_rating': coalesce(prop(t, 'quality_rating'), None),
                'overdue_days': diff_days(ctx.today, t['due_on']) if t['status'] != 'done' and t['due_on'] < ctx.today else 0,
            }
        )
    cycles: dict = {}
    for c in rows(ctx, 'appraisal_cycles'):
        cycles[c['id']] = c
    self_ = u['id'] == ctx.user_id
    appraisals = []
    for a in rows(ctx, 'appraisals'):
        if a.get('user_id') != u['id']:
            continue
        c = cycles[a['cycle_id']]
        hide = self_ and a['status'] != 'completed' and not _is_appraisal_admin(ctx)
        appraisals.append(
            {
                'id': a['id'],
                'cycle_name': c['name'],
                'period_from': c['period_from'],
                'period_to': c['period_to'],
                'status': a['status'],
                'final_rating': prop(a, 'final_rating'),
                'increment_pct': None if hide else prop(a, 'increment_pct'),
                'promotion_to': None if hide else prop(a, 'promotion_to'),
            }
        )
    appraisals.sort(key=lambda x: locale_key(x['period_from']), reverse=True)
    mgr = _user(ctx, u.get('reports_to_id'))

    def goal_ok(g: dict) -> bool:
        c = cycles.get(g['cycle_id']) if js_truthy(g.get('cycle_id')) else None
        return not c or c['status'] != 'closed' or (c['period_to'] >= frm and c['period_from'] <= to)

    goals = sorted((_goal_view(ctx, g) for g in _goals_of(ctx, u['id']) if goal_ok(g)), key=lambda g: -g['weight'])
    fb = sorted(
        (f for f in rows(ctx, 'feedback') if f.get('to_user_id') == u['id'] and _feedback_visible(ctx, f)), key=lambda f: locale_key(f['at']), reverse=True
    )[:30]
    return {
        'user': {
            'id': u['id'],
            'name': prop(u, 'name'),
            'title': prop(u, 'title'),
            'role': prop(u, 'role'),
            'avatar_hue': prop(u, 'avatar_hue'),
            'joined_on': prop(u, 'joined_on'),
            'manager_name': coalesce(mgr.get('name', UNDEF), None) if mgr else None,
            'department': prop(u, 'department'),
        },
        'from': frm,
        'to': to,
        'kpis': kpis,
        'trend': trend,
        'tasks': tasks,
        'delay_reasons': sorted(
            ({'code': k, 'label': coalesce(DELAY_REASON_LABEL.get(k, UNDEF), k), 'count': v} for k, v in ts['delay_reasons'].items()), key=lambda x: -x['count']
        ),
        'goals': goals,
        'feedback': [_feedback_view(ctx, f) for f in fb],
        'appraisals': appraisals,
        'cycles': [{'id': c['id'], 'name': c['name'], 'status': c['status']} for c in cycles.values() if c['status'] != 'closed'],
        'can_add_goal': not ctx.view_as and (self_ or _is_above(ctx, ctx.user_id, u['id']) or _is_hr(ctx)),
        'can_give_feedback': not ctx.view_as and not self_,
        'is_self': self_,
        'is_manager': _is_above(ctx, ctx.user_id, u['id']),
    }


# =====================================================================================================================
# Routes — goals
# =====================================================================================================================
def _assert_goal_weights(ctx: Ctx, user_id: Any, cycle_id: Any, extra: Any, exclude: Any = UNDEF, exact: bool = False) -> None:
    total: Any = 0
    for g in rows(ctx, 'goals'):
        if g.get('user_id') == user_id and g.get('cycle_id') == cycle_id and g['id'] != exclude and g.get('status') != 'missed':
            total += g['weight']
    total += extra
    if total > 100:
        err(
            422,
            'GOAL_WEIGHTS',
            f'Goal weights for this cycle would total {js_str(total)}%. They must add up to exactly 100% — lower another goal first.',
            'weight',
        )
    if exact and total != 100:
        err(422, 'GOAL_WEIGHTS', f'Goal weights for this cycle total {js_str(total)}%. Adjust them to exactly 100% before approving.', 'weight')


def _goal_input(body: Any, partial: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not partial or _g(body, 'title') is not UNDEF:
        t = _str(_g(body, 'title'))
        if js_len(t) < 4:
            err(422, 'VALIDATION', 'Give the goal a title of at least 4 characters.', 'title')
        out['title'] = t
    if not partial or _g(body, 'kra') is not UNDEF:
        out['kra'] = _str(_g(body, 'kra')) or 'General'
    if not partial or _g(body, 'weight') is not UNDEF:
        w = js_number(_g(body, 'weight'))
        if not is_integer(w) or w < 5 or w > 100:
            err(422, 'VALIDATION', 'Weight is a whole percentage between 5 and 100.', 'weight')
        out['weight'] = w
    if not partial or _g(body, 'measure') is not UNDEF:
        out['measure'] = _str(_g(body, 'measure'))
    if not partial or _g(body, 'target') is not UNDEF:
        t = js_number(_g(body, 'target'))
        if not is_finite(t) or t <= 0:
            err(422, 'VALIDATION', 'Set a target greater than zero.', 'target')
        out['target'] = t
    if not partial or _g(body, 'unit') is not UNDEF:
        out['unit'] = _str(_g(body, 'unit'))
    if not partial or _g(body, 'due_on') is not UNDEF:
        if not _is_date(_g(body, 'due_on')):
            err(422, 'VALIDATION', 'Pick a due date.', 'due_on')
        out['due_on'] = _g(body, 'due_on')
    return out


def create_goal(req: Req) -> Any:
    ctx = req.ctx
    user_id = _str(req.b('user_id')) or ctx.user_id
    u = find(ctx, 'users', user_id, 'Person')
    self_ = u['id'] == ctx.user_id
    if not self_ and not _is_above(ctx, ctx.user_id, u['id']) and not _is_hr(ctx):
        err(403, 'FORBIDDEN', 'You can set goals for yourself and for people who report to you.')
    cycle_id = _str(req.b('cycle_id')) or None
    if cycle_id:
        c = find(ctx, 'appraisal_cycles', cycle_id, 'Cycle')
        if c['status'] == 'closed':
            err(409, 'CYCLE_CLOSED', f'{c["name"]} is closed. Add goals to an open cycle.', 'cycle_id')
    g = _goal_input(req.body)
    _assert_goal_weights(ctx, u['id'], cycle_id, g['weight'])
    row = insert(
        ctx, 'goals', {**g, 'user_id': u['id'], 'cycle_id': cycle_id, 'current': 0, 'status': 'draft' if self_ else 'agreed', 'manager_approved': not self_}
    )
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(ctx, 'goal', row['id'], 'created', f'{me_name} added goal “{row["title"]}” ({js_str(row["weight"])}%) for {u["name"]}')
    if self_ and js_truthy(u.get('reports_to_id')):
        notify(
            ctx,
            u['reports_to_id'],
            'NTF-04',
            'Goal to approve',
            f'{u["name"]} drafted a goal: “{row["title"]}” ({js_str(row["weight"])}%).',
            f'/performance/{u["id"]}',
        )
    if not self_:
        notify(ctx, u['id'], 'NTF-14', 'New goal', f'{me_name} set a goal for you: “{row["title"]}”.', '/performance')
    return row


def patch_goal(req: Req) -> Any:
    ctx = req.ctx
    g = find(ctx, 'goals', req.params['id'], 'Goal')
    own = g['user_id'] == ctx.user_id
    mgr = _is_above(ctx, ctx.user_id, g['user_id']) or _is_hr(ctx)
    if not own and not mgr:
        err(403, 'FORBIDDEN', 'Only the person and their managers can change this goal.')
    pt: dict[str, Any] = {}
    defining = any(req.b(k) is not UNDEF for k in ('title', 'kra', 'weight', 'measure', 'target', 'unit', 'due_on'))
    if defining:
        if js_truthy(g.get('manager_approved')) and not mgr:
            err(409, 'GOAL_LOCKED', 'This goal is agreed with your manager. Ask them to change its definition.')
        pt.update(_goal_input(req.body, True))
        if 'weight' in pt:
            _assert_goal_weights(ctx, g['user_id'], g.get('cycle_id'), pt['weight'], g['id'])
    if req.b('current') is not UNDEF:
        c = js_number(req.b('current'))
        if not is_finite(c) or c < 0:
            err(422, 'VALIDATION', 'Progress must be zero or more.', 'current')
        if not js_truthy(g.get('manager_approved')):
            err(409, 'GOAL_NOT_AGREED', 'Progress can be logged once your manager approves the goal.')
        pt['current'] = c
    if req.b('status') is not UNDEF:
        st = req.b('status')
        if not _in(st, ('on_track', 'at_risk', 'achieved', 'missed')):
            err(422, 'VALIDATION', 'Pick a valid status.', 'status')
        if st in ('achieved', 'missed') and not mgr:
            err(403, 'FORBIDDEN', 'Your manager marks goals achieved or missed.')
        pt['status'] = st
    elif 'current' in pt and g.get('status') not in ('achieved', 'missed'):
        pt['status'] = 'on_track' if pt['current'] >= g['target'] else 'on_track' if g.get('status') == 'agreed' else g.get('status', UNDEF)
    out = update(ctx, 'goals', g['id'], pt, req.if_match)
    prog = f' — progress {js_str(out["current"])}/{js_str(out["target"])} {js_str(prop(out, "unit"))}' if 'current' in pt else ''
    stt = f' — {pt["status"].replace("_", " ", 1)}' if js_truthy(pt.get('status')) else ''
    log_activity(ctx, 'goal', g['id'], 'updated', f'{js_str(ctx.user.get("name", UNDEF))} updated goal “{out["title"]}”{prog}{stt}')
    return out


def approve_goal(req: Req) -> Any:
    ctx = req.ctx
    g = find(ctx, 'goals', req.params['id'], 'Goal')
    if g['user_id'] == ctx.user_id:
        err(403, 'FORBIDDEN', 'Your manager approves your goals.')
    if not _is_above(ctx, ctx.user_id, g['user_id']) and not _is_hr(ctx):
        err(403, 'FORBIDDEN', 'Only the person’s manager can approve their goals.')
    if js_truthy(g.get('manager_approved')):
        return g
    # approving the last draft must bring the cycle to exactly 100%
    drafts = [
        x
        for x in rows(ctx, 'goals')
        if x.get('user_id') == g['user_id'] and x.get('cycle_id') == g.get('cycle_id') and not js_truthy(x.get('manager_approved')) and x['id'] != g['id']
    ]
    if js_truthy(g.get('cycle_id')) and not drafts:
        _assert_goal_weights(ctx, g['user_id'], g['cycle_id'], 0, UNDEF, True)
    out = update(ctx, 'goals', g['id'], {'manager_approved': True, 'status': 'agreed'})
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(ctx, 'goal', g['id'], 'approved', f'{me_name} approved goal “{g["title"]}” for {user_name(ctx, g["user_id"])}')
    notify(ctx, g['user_id'], 'NTF-05', 'Goal approved', f'{me_name} approved your goal “{g["title"]}”.', '/performance')
    return out


def delete_goal(req: Req) -> Any:
    ctx = req.ctx
    g = find(ctx, 'goals', req.params['id'], 'Goal')
    own = g['user_id'] == ctx.user_id
    mgr = _is_above(ctx, ctx.user_id, g['user_id']) or _is_hr(ctx)
    if not own and not mgr:
        err(403, 'FORBIDDEN', 'Only the person and their managers can remove this goal.')
    if js_truthy(g.get('manager_approved')) and not mgr:
        err(409, 'GOAL_LOCKED', 'This goal is agreed. Ask your manager to remove it.')
    remove(ctx, 'goals', g['id'])
    log_activity(ctx, 'goal', g['id'], 'deleted', f'{js_str(ctx.user.get("name", UNDEF))} removed goal “{g["title"]}”')
    return None


# =====================================================================================================================
# Routes — feedback
# =====================================================================================================================
def create_feedback(req: Req) -> Any:
    ctx = req.ctx
    to = find(ctx, 'users', _str(req.b('to_user_id')), 'Person')
    if to['id'] == ctx.user_id:
        err(422, 'VALIDATION', 'Feedback goes to someone else.', 'to_user_id')
    kind = req.b('kind')
    if not _in(kind, ('praise', 'improvement', 'one_on_one')):
        err(422, 'VALIDATION', 'Pick the kind of feedback.', 'kind')
    vis = req.b('visibility')
    if not _in(vis, ('private', 'manager', 'public')):
        err(422, 'VALIDATION', 'Pick who can see it.', 'visibility')
    if kind == 'one_on_one' and not _is_above(ctx, ctx.user_id, to['id']) and not _is_hr(ctx):
        err(403, 'FORBIDDEN', '1:1 notes are written by the person’s manager.')
    text = _str(req.b('body'))
    if js_len(text) < 10:
        err(422, 'VALIDATION', 'Write at least 10 characters so it is useful.', 'body')
    row = insert(ctx, 'feedback', {'to_user_id': to['id'], 'from_user_id': ctx.user_id, 'kind': kind, 'body': text, 'visibility': vis, 'at': now_iso()})
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(ctx, 'feedback', row['id'], 'created', f'{me_name} gave {kind.replace("_", " ")} feedback to {to["name"]}')
    if vis != 'manager':
        notify(ctx, to['id'], 'NTF-03', 'You got praise' if kind == 'praise' else 'New feedback', f'{me_name}: “{text[:90]}”', '/performance')
    return row


# =====================================================================================================================
# Routes — appraisals
# =====================================================================================================================
def list_cycles(req: Req) -> Any:
    ctx = req.ctx
    _sweep_cycles(ctx)
    return [_cycle_view(ctx, c) for c in sorted(rows(ctx, 'appraisal_cycles'), key=lambda c: locale_key(c['period_from']), reverse=True)]


def create_cycle(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'appraisals.manage', 'Only HR and admins run appraisal cycles.')
    name = _str(req.b('name'))
    if js_len(name) < 3:
        err(422, 'VALIDATION', 'Name the cycle, e.g. “H2 FY26-27”.', 'name')
    if any(c['name'].lower() == name.lower() for c in rows(ctx, 'appraisal_cycles')):
        err(409, 'DUPLICATE', 'A cycle with this name already exists.', 'name')
    for k in ('period_from', 'period_to', 'self_due', 'manager_due', 'hr_due'):
        if not _is_date(req.b(k)):
            err(422, 'VALIDATION', 'Fill in every date.', k)
    b = req.b
    if b('period_to') <= b('period_from'):
        err(422, 'DATE_RANGE', 'The period must end after it starts.', 'period_to')
    if not (b('self_due') <= b('manager_due') and b('manager_due') <= b('hr_due')):
        err(422, 'DATE_RANGE', 'Due dates run in order: self review, then manager review, then HR review.', 'manager_due')
    if b('self_due') < b('period_from'):
        err(422, 'DATE_RANGE', 'Self reviews can’t be due before the period starts.', 'self_due')
    w = coalesce(b('weights'), {})
    weights = {k: js_number(_g(w, k)) for k in ('goals', 'tasks', 'timesheets', 'behaviour')}
    if any(not is_integer(x) or x < 0 or x > 100 for x in weights.values()):
        err(422, 'APPRAISAL_WEIGHTS', 'Each weight is a whole percentage between 0 and 100.', 'weights')
    sm = weights['goals'] + weights['tasks'] + weights['timesheets'] + weights['behaviour']
    if sm != 100:
        err(422, 'APPRAISAL_WEIGHTS', f'Weights add up to {js_str(sm)}%. Make goals, tasks, timesheets and behaviour total exactly 100%.', 'weights')
    c = insert(
        ctx,
        'appraisal_cycles',
        {
            'name': name,
            'period_from': b('period_from'),
            'period_to': b('period_to'),
            'status': 'draft',
            'self_due': b('self_due'),
            'manager_due': b('manager_due'),
            'hr_due': b('hr_due'),
            'weights': weights,
        },
    )
    log_activity(ctx, 'appraisal_cycle', c['id'], 'created', f'{js_str(ctx.user.get("name", UNDEF))} created appraisal cycle {name}')
    return c


def launch_cycle(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'appraisals.manage', 'Only HR and admins launch appraisal cycles.')
    c = find(ctx, 'appraisal_cycles', req.params['id'], 'Cycle')
    if c['status'] != 'draft':
        err(409, 'CYCLE_LAUNCHED', f'{c["name"]} has already been launched.')
    op = next((x for x in rows(ctx, 'appraisal_cycles') if x['id'] != c['id'] and x['status'] not in ('draft', 'closed')), None)
    if op:
        err(409, 'CYCLE_OPEN', f'{op["name"]} is still running. Close it before launching another cycle.')
    people = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not u.get('is_super_admin') and js_truthy(u.get('reports_to_id'))]
    if not people:
        err(409, 'NO_PEOPLE', 'There is nobody with a manager to appraise. Set reporting lines first.')
    # v7: optionally run every appraisal through a workflow (e.g. self -> manager -> unit head -> HR -> finance)
    wf_id = _str(req.b('workflow_template_id'))
    if wf_id:
        require_action(ctx, 'workflows.start', 'Starting appraisal workflows needs “Start workflows for others”.')
    from app.routes import workflows_v7 as wv

    tpl = wv.appraisal_template(ctx, wf_id) if wf_id else None
    if tpl:
        wv.check_routable(ctx, tpl, [u['id'] for u in people])
    for u in people:
        a = insert(
            ctx,
            'appraisals',
            {
                'cycle_id': c['id'],
                'user_id': u['id'],
                'manager_id': u['reports_to_id'],
                'status': 'self_pending',
                'self_rating': None,
                'self_comments': '',
                'manager_rating': None,
                'manager_comments': '',
                'hr_rating': None,
                'hr_comments': '',
                'final_rating': None,
                'increment_pct': None,
                'promotion_to': None,
                'metrics': {},
                'submitted_self_at': None,
                'submitted_manager_at': None,
                'closed_at': None,
            },
        )
        if tpl:
            wv.start_workflow(
                ctx,
                tpl,
                u['id'],
                {'cycle_id': c['id'], 'cycle_name': c['name'], 'appraisal_id': a['id'], 'note': f'Started with {c["name"]}'},
                f'{tpl["name"]} {c["name"]} — {u["name"]}',
            )
        else:
            notify(
                ctx, u['id'], 'NTF-14', 'Appraisal open', f'{c["name"]}: write your self review by {fmt_date(c["self_due"], "long")}.', f'/appraisals/{a["id"]}'
            )
    update(ctx, 'appraisal_cycles', c['id'], {'status': 'self_review'})
    sync_cycle(ctx, c['id'])
    via = f' through the “{tpl["name"]}” workflow' if tpl else ''
    log_activity(ctx, 'appraisal_cycle', c['id'], 'launched', f'{js_str(ctx.user.get("name", UNDEF))} launched {c["name"]} for {len(people)} people{via}')
    return {'created': len(people), 'workflows': len(people) if tpl else 0}


def close_cycle(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'appraisals.manage', 'Only HR and admins close appraisal cycles.')
    c = find(ctx, 'appraisal_cycles', req.params['id'], 'Cycle')
    if c['status'] == 'closed':
        return c
    if c['status'] == 'draft':
        err(409, 'CYCLE_DRAFT', 'This cycle was never launched.')
    op = sum(1 for a in rows(ctx, 'appraisals') if a.get('cycle_id') == c['id'] and a.get('status') != 'completed')
    if op:
        err(409, 'APPRAISALS_OPEN', f'{op} appraisal{" is" if op == 1 else "s are"} not completed yet. Finish the HR reviews first.')
    out = update(ctx, 'appraisal_cycles', c['id'], {'status': 'closed'})
    log_activity(ctx, 'appraisal_cycle', c['id'], 'closed', f'{js_str(ctx.user.get("name", UNDEF))} closed {c["name"]}')
    return out


def list_appraisals(req: Req) -> Any:
    ctx = req.ctx
    _sweep_cycles(ctx)
    cycles = sorted(rows(ctx, 'appraisal_cycles'), key=lambda c: locale_key(c['period_from']), reverse=True)
    hr = _is_appraisal_admin(ctx)
    visible = [a for a in rows(ctx, 'appraisals') if _can_see_appraisal(ctx, a)]
    mode = 'hr' if hr else 'manager' if any(a['user_id'] != ctx.user_id for a in visible) else 'self'
    active = next((c for c in cycles if c['status'] not in ('closed', 'draft')), None) or (cycles[0] if cycles else None)
    qc = req.q('cycle_id')
    cycle = coalesce(next((c for c in cycles if c['id'] == qc), UNDEF) if js_truthy(qc) else None, active, None)
    cmap: dict = {}
    for c in cycles:
        cmap[c['id']] = c
    scoped = visible if mode == 'self' else [a for a in visible if a.get('cycle_id') == (cycle['id'] if cycle else UNDEF)]
    scoped = [a for a in scoped if a.get('cycle_id') in cmap]
    rows_out = sorted((_appraisal_row(ctx, a, cmap[a['cycle_id']]) for a in scoped), key=lambda x: locale_key(x['user_name']))
    dist = [{'rating': str(n), 'count': 0} for n in (1, 2, 3, 4, 5)]
    for x in rows_out:
        v = coalesce(x['final_rating'], x['manager_rating'])
        if not nullish(v):
            dist[js_min(4, js_max(0, js_round(v) - 1))]['count'] += 1
    return {
        'mode': mode,
        'cycles': [_cycle_view(ctx, c) for c in (cycles if hr else [c for c in cycles if c['status'] != 'draft'])],
        'cycle': _cycle_view(ctx, cycle) if cycle else None,
        'rows': rows_out,
        'distribution': dist,
        'overdue': [x for x in rows_out if x['overdue']],
        'can_manage': hr and not ctx.view_as,
    }


def get_appraisal(req: Req) -> Any:
    ctx = req.ctx
    a = _get_appraisal(ctx, req.params['id'])
    c = find(ctx, 'appraisal_cycles', a['cycle_id'])
    u = find(ctx, 'users', a['user_id'])
    is_self = a['user_id'] == ctx.user_id
    is_mgr = not is_self and (a.get('manager_id') == ctx.user_id or _is_above(ctx, ctx.user_id, a['user_id']))
    is_hr = not is_self and _is_appraisal_admin(ctx)
    done = a['status'] == 'completed'
    met = a.get('metrics')
    live = met if done and js_truthy(met) and isinstance(met, dict) and 'suggested' in met else _compute_metrics(ctx, a, c)
    if ctx.view_as:
        stage = 'view'
    elif a['status'] == 'self_pending' and is_self:
        stage = 'self'
    elif a['status'] == 'manager_pending' and not is_self and (a.get('manager_id') == ctx.user_id or _is_appraisal_admin(ctx)):
        stage = 'manager'
    elif a['status'] == 'hr_pending' and is_hr:
        stage = 'hr'
    else:
        stage = 'view'
    from app.routes.workflows_v7 import wf_linked_appraisals

    wf_managed = a['id'] in wf_linked_appraisals(ctx)
    show = (not is_self or done or _is_appraisal_admin(ctx)) and not (wf_managed and is_self and not _is_appraisal_admin(ctx) and not done)
    backs = sorted(
        (x for x in rows_where_ro(ctx, 'activity_log', 'entity_id', a['id']) if x.get('entity') == 'appraisal' and x.get('action') == 'sent_back'),
        key=lambda x: locale_key(x['at']),
        reverse=True,
    )
    back = backs[0] if backs else None
    out = {
        **a,
        'manager_rating': prop(a, 'manager_rating') if show else None,
        'manager_comments': prop(a, 'manager_comments') if show else '',
        'hr_rating': prop(a, 'hr_rating') if show else None,
        'hr_comments': prop(a, 'hr_comments') if show else '',
        'increment_pct': prop(a, 'increment_pct') if show else None,
        'promotion_to': prop(a, 'promotion_to') if show else None,
        'cycle': c,
        'user': {'id': u['id'], 'name': prop(u, 'name'), 'title': prop(u, 'title'), 'avatar_hue': prop(u, 'avatar_hue'), 'joined_on': prop(u, 'joined_on')},
        'manager_name': user_name(ctx, a['manager_id']) if js_truthy(a.get('manager_id')) else None,
        'live': live if show else {**live, 'suggested': None, 'score_behaviour': None},
        'goals': [_goal_view(ctx, g) for g in _goals_of(ctx, a['user_id'], UNDEF, UNDEF, c['id'])],
    }
    if wf_managed and is_self:
        out.update({'manager_rating': None, 'manager_comments': '', 'hr_comments': ''})
    out['stage_for_viewer'] = 'view' if wf_managed else stage
    out['viewer'] = {'is_self': is_self, 'is_manager': is_mgr, 'is_hr': is_hr}
    out['show_ratings'] = show
    out['sent_back_reason'] = re.sub(r'^.*?: ', '', back['summary'], count=1) if back and a['status'] != 'completed' else None
    return out


def _rating_of(v: Any, step: float = 1) -> Any:
    n = js_number(v)
    if not is_finite(n) or n < 1 or n > 5 or js_round(n / step) * step != n:
        err(422, 'VALIDATION', 'Pick a rating from 1 to 5.' if step == 1 else 'Pick a rating from 1 to 5 (half points allowed).', 'rating')
    return n


def appraisal_self(req: Req) -> Any:
    ctx = req.ctx
    from app.routes.workflows_v7 import assert_not_workflow_managed

    a = _get_appraisal(ctx, req.params['id'])
    assert_not_workflow_managed(ctx, a['id'])
    if a['user_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person writes their self review.')
    if a['status'] != 'self_pending':
        err(409, 'APPRAISAL_STAGE', 'Your self review is already submitted.')
    c = find(ctx, 'appraisal_cycles', a['cycle_id'])
    if c['status'] == 'closed':
        err(409, 'CYCLE_CLOSED', 'This cycle is closed.')
    rating = _rating_of(req.b('rating'))
    comments = _str(req.b('comments'))
    if js_len(comments) < 30:
        err(422, 'VALIDATION', 'Write at least 30 characters about what you delivered and where you want to grow.', 'comments')
    nxt = 'manager_pending' if js_truthy(a.get('manager_id')) else 'hr_pending'
    out = update(ctx, 'appraisals', a['id'], {'self_rating': rating, 'self_comments': comments, 'status': nxt, 'submitted_self_at': now_iso()}, req.if_match)
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(ctx, 'appraisal', a['id'], 'self_submitted', f'{me_name} submitted the self review for {c["name"]} ({js_str(rating)}/5)')
    if js_truthy(a.get('manager_id')):
        notify(
            ctx,
            a['manager_id'],
            'NTF-04',
            'Appraisal to review',
            f'{me_name} submitted their self review for {c["name"]}. Your review is due {fmt_date(c["manager_due"], "long")}.',
            f'/appraisals/{a["id"]}',
        )
    else:
        for h in _hr_people(ctx):
            notify(ctx, h['id'], 'NTF-04', 'Appraisal to review', f'{me_name}’s appraisal is ready for HR review.', f'/appraisals/{a["id"]}')
    sync_cycle(ctx, a['cycle_id'])
    return scope().find_by('appraisals', 'id', a['id']) or out


def appraisal_manager(req: Req) -> Any:
    ctx = req.ctx
    from app.routes.workflows_v7 import assert_not_workflow_managed

    a = _get_appraisal(ctx, req.params['id'])
    assert_not_workflow_managed(ctx, a['id'])
    if a['user_id'] == ctx.user_id:
        err(403, 'FORBIDDEN', 'You can’t review your own appraisal.')
    if a.get('manager_id') != ctx.user_id and not _is_appraisal_admin(ctx):
        err(403, 'FORBIDDEN', 'Only the person’s manager writes the manager review.')
    if a['status'] != 'manager_pending':
        err(409, 'APPRAISAL_STAGE', 'Wait for the self review first.' if a['status'] == 'self_pending' else 'The manager review is already submitted.')
    c = find(ctx, 'appraisal_cycles', a['cycle_id'])
    rating = _rating_of(req.b('rating'))
    comments = _str(req.b('comments'))
    if js_len(comments) < 30:
        err(422, 'VALIDATION', 'Write at least 30 characters: what went well, what to improve, with examples.', 'comments')
    snap = _compute_metrics(ctx, {**a, 'manager_rating': rating}, c)
    out = update(
        ctx,
        'appraisals',
        a['id'],
        {'manager_rating': rating, 'manager_comments': comments, 'status': 'hr_pending', 'submitted_manager_at': now_iso(), 'metrics': snap},
        req.if_match,
    )
    me_name = js_str(ctx.user.get('name', UNDEF))
    sug = js_str(coalesce(snap['suggested'], '—'))
    log_activity(
        ctx,
        'appraisal',
        a['id'],
        'manager_submitted',
        f'{me_name} submitted the manager review for {user_name(ctx, a["user_id"])} ({js_str(rating)}/5, system suggests {sug})',
    )
    for h in _hr_people(ctx):
        if h['id'] != a['user_id'] and h['id'] != ctx.user_id:
            notify(
                ctx,
                h['id'],
                'NTF-04',
                'Appraisal ready for HR',
                f'{user_name(ctx, a["user_id"])}: manager review done ({js_str(rating)}/5). Calibrate by {fmt_date(c["hr_due"], "long")}.',
                f'/appraisals/{a["id"]}',
            )
    sync_cycle(ctx, a['cycle_id'])
    return scope().find_by('appraisals', 'id', a['id']) or out


def appraisal_hr(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'appraisals.manage', 'Only HR and admins calibrate appraisals.')
    from app.routes.workflows_v7 import assert_not_workflow_managed

    a = _get_appraisal(ctx, req.params['id'])
    assert_not_workflow_managed(ctx, a['id'])
    if a['user_id'] == ctx.user_id:
        err(403, 'FORBIDDEN', 'Someone else in HR or an admin must calibrate your own appraisal.')
    if a['status'] != 'hr_pending':
        err(
            409, 'APPRAISAL_STAGE', 'This appraisal is completed and locked.' if a['status'] == 'completed' else 'The manager review has to be submitted first.'
        )
    c = find(ctx, 'appraisal_cycles', a['cycle_id'])
    rating = _rating_of(req.b('rating'), 0.5)
    comments = _str(req.b('comments'))
    inc = js_number(req.b('increment_pct'))
    if not is_finite(inc) or inc < 0 or inc > 50:
        err(422, 'VALIDATION', 'Increment is a percentage between 0 and 50.', 'increment_pct')
    promo = _str(req.b('promotion_to')) or None
    snap = _compute_metrics(ctx, a, c)
    if snap['suggested'] is not None and abs(rating - snap['suggested']) > 1 and js_len(comments) < 15:
        err(
            422,
            'RATING_DEVIATION',
            f'The final rating differs from the system suggestion ({js_str(snap["suggested"])}) by more than 1. Explain why in at least 15 characters.',
            'comments',
        )
    if comments and js_len(comments) < 5:
        err(422, 'VALIDATION', 'Write a little more in the HR comments.', 'comments')
    out = update(
        ctx,
        'appraisals',
        a['id'],
        {
            'hr_rating': rating,
            'hr_comments': comments,
            'final_rating': rating,
            'increment_pct': r1(inc),
            'promotion_to': promo,
            'status': 'completed',
            'closed_at': now_iso(),
            'metrics': snap,
        },
        req.if_match,
    )
    # Increment flows into costing and payroll: a new cost-rate row effective the day after the cycle ends.
    if inc > 0:
        rates = sorted((x for x in rows(ctx, 'cost_rates') if x.get('user_id') == a['user_id']), key=lambda x: locale_key(x['effective_from']), reverse=True)
        cur = rates[0] if rates else None
        eff = add_days(c['period_to'], 1)
        if cur and not any(x['effective_from'] == eff for x in rates):
            insert(
                ctx,
                'cost_rates',
                {
                    'user_id': a['user_id'],
                    'monthly_ctc_paise': js_round(cur['monthly_ctc_paise'] * (1 + inc / 100)),
                    'variable_pct': cur['variable_pct'],
                    'sales_target_paise': js_round(cur['sales_target_paise'] * (1 + inc / 100)) if js_truthy(cur.get('sales_target_paise')) else None,
                    'effective_from': eff,
                },
            )
            log_activity(
                ctx,
                'cost_rate',
                a['user_id'],
                'increment',
                f'Cost rate for {user_name(ctx, a["user_id"])} raised {js_str(r1(inc))}% from {fmt_date(eff)} (appraisal {c["name"]})',
            )
    me_name = js_str(ctx.user.get('name', UNDEF))
    promoted = f', promoted to {promo}' if promo else ''
    log_activity(
        ctx,
        'appraisal',
        a['id'],
        'completed',
        f'{me_name} completed {user_name(ctx, a["user_id"])}’s appraisal: final {js_str(rating)}/5, increment {js_str(r1(inc))}%{promoted}',
    )
    notify(
        ctx,
        a['user_id'],
        'NTF-05',
        'Appraisal completed',
        f'Your {c["name"]} appraisal is complete. Open it to see the final rating.',
        f'/appraisals/{a["id"]}',
    )
    if js_truthy(a.get('manager_id')) and a['manager_id'] != ctx.user_id:
        notify(
            ctx, a['manager_id'], 'NTF-05', 'Appraisal completed', f'{user_name(ctx, a["user_id"])}: final rating {js_str(rating)}/5.', f'/appraisals/{a["id"]}'
        )
    sync_cycle(ctx, a['cycle_id'])
    return scope().find_by('appraisals', 'id', a['id']) or out


def appraisal_send_back(req: Req) -> Any:
    ctx = req.ctx
    from app.routes.workflows_v7 import assert_not_workflow_managed

    a = _get_appraisal(ctx, req.params['id'])
    assert_not_workflow_managed(ctx, a['id'])
    reason = _str(req.b('reason'))
    if js_len(reason) < 10:
        err(422, 'VALIDATION', 'Explain what to change in at least 10 characters.', 'reason')
    if a['status'] == 'hr_pending':
        require_action(ctx, 'appraisals.manage', 'Only HR sends a review back to the manager.')
        if a['user_id'] == ctx.user_id:
            err(403, 'FORBIDDEN', 'You can’t act on your own appraisal.')
        to = 'manager_pending' if js_truthy(a.get('manager_id')) else 'self_pending'
        notify_id = coalesce(a.get('manager_id', UNDEF), a['user_id'])
    elif a['status'] == 'manager_pending':
        if a.get('manager_id') != ctx.user_id and not _is_appraisal_admin(ctx):
            err(403, 'FORBIDDEN', 'Only the manager can send the self review back.')
        to = 'self_pending'
        notify_id = a['user_id']
    else:
        err(409, 'APPRAISAL_STAGE', 'Only reviews waiting on a manager or HR can be sent back.')
    out = update(ctx, 'appraisals', a['id'], {'status': to})
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(ctx, 'appraisal', a['id'], 'sent_back', f'{me_name} sent the review back: {reason}')
    if js_truthy(notify_id):
        notify(ctx, notify_id, 'NTF-06', 'Appraisal sent back', f'{me_name}: “{reason}”', f'/appraisals/{a["id"]}')
    sync_cycle(ctx, a['cycle_id'])
    return scope().find_by('appraisals', 'id', a['id']) or out


# =====================================================================================================================
# Routes — recruitment
# =====================================================================================================================
def recruitment(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'recruitment.manage')
    cands = rows(ctx, 'candidates')
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    by_opening: dict = {}
    for c in cands:
        by_opening.setdefault(c.get('opening_id'), []).append(c)
    openings = []
    for o in rows(ctx, 'openings'):
        cs = by_opening.get(o['id'], [])
        if not (o.get('status') != 'filled' or any(c.get('stage') not in _CLOSED_STAGES for c in cs)):
            continue
        pr = projects.get(o['project_id']) if js_truthy(o.get('project_id')) else None
        openings.append(
            {
                'id': o['id'],
                'role': prop(o, 'role'),
                'project': coalesce(pr.get('name', UNDEF) if pr else UNDEF, None) if js_truthy(o.get('project_id')) else None,
                'status': prop(o, 'status'),
                'count': prop(o, 'count'),
                'owner_id': prop(o, 'owner_id'),
                'owner_name': user_name(ctx, o.get('owner_id')),
                'needed_by': prop(o, 'needed_by'),
                'candidates': len(cs),
                'hired': sum(1 for c in cs if c.get('stage') == 'hired'),
            }
        )
    hired = [c for c in cands if c.get('stage') == 'hired']
    tth = [diff_days(_hired_on(ctx, c), c['created_at'][:10]) for c in hired]
    sources: dict = {}
    for c in cands:
        s = sources.get(c['source']) or {'count': 0, 'hired': 0}
        s['count'] += 1
        if c.get('stage') == 'hired':
            s['hired'] += 1
        sources[c['source']] = s
    open_positions: Any = 0
    for o in rows(ctx, 'openings'):
        if o.get('status') not in ('filled', 'on_hold'):
            open_positions += js_max(0, o['count'] - sum(1 for c in by_opening.get(o['id'], []) if c.get('stage') == 'hired'))
    return {
        'openings': openings,
        'candidates': sorted((_candidate_row(ctx, c) for c in cands), key=lambda c: locale_key(c['updated_at']), reverse=True),
        'metrics': {
            'open_positions': open_positions,
            'active': sum(1 for c in cands if c.get('stage') not in _CLOSED_STAGES),
            'avg_time_to_hire': js_round(sum(tth) / len(tth)) if tth else None,
            'hired_90': sum(1 for c in hired if _hired_on(ctx, c) >= add_days(ctx.today, -90)),
            'offers_out': sum(1 for c in cands if c.get('stage') == 'offer'),
            'by_source': sorted(({'source': k, **v} for k, v in sources.items()), key=lambda x: -x['count']),
        },
    }


def get_candidate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'recruitment.manage')
    c = find(ctx, 'candidates', req.params['id'], 'Candidate')
    f = next((x for x in rows(ctx, 'files') if x['id'] == c['resume_file_id']), None) if js_truthy(c.get('resume_file_id')) else None
    inv = next((i for i in rows(ctx, 'invites') if i['id'] == c['user_id']), None) if js_truthy(c.get('user_id')) else None
    ivs = sorted((i for i in rows(ctx, 'interviews') if i.get('candidate_id') == c['id']), key=lambda i: i['round'])
    hist = sorted(
        (lg for lg in rows_where_ro(ctx, 'activity_log', 'entity_id', c['id']) if lg.get('entity') == 'candidate'),
        key=lambda lg: locale_key(lg['at']),
        reverse=True,
    )
    return {
        **_candidate_row(ctx, c),
        'interviews': [{**i, 'interviewer_name': user_name(ctx, i.get('interviewer_id'))} for i in ivs],
        'resume': {'id': f['id'], 'name': prop(f, 'name'), 'size_bytes': prop(f, 'size_bytes'), 'data_url': prop(f, 'data_url')} if f else None,
        'history': [{'at': lg['at'], 'summary': lg['summary'], 'actor': user_name(ctx, lg.get('actor_id'))} for lg in hist],
        'invite': {'id': inv['id'], 'email': inv['email'], 'accepted': js_truthy(inv.get('accepted_at'))} if inv else None,
    }


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+\Z')


def _cand_input(body: Any, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    b = lambda k: _g(body, k)  # noqa: E731
    if not partial or b('name') is not UNDEF:
        n = _str(b('name'))
        if js_len(n) < 2:
            err(422, 'VALIDATION', 'Enter the candidate’s name.', 'name')
        out['name'] = n
    if not partial or b('email') is not UNDEF:
        e = _str(b('email')).lower()
        if not _EMAIL_RE.search(e):
            err(422, 'VALIDATION', 'Enter a valid email address.', 'email')
        out['email'] = e
    if b('phone') is not UNDEF:
        out['phone'] = _str(b('phone'))
    if not partial or b('source') is not UNDEF:
        s = b('source')
        if not _in(s, ('referral', 'job_portal', 'linkedin', 'campus', 'agency', 'website', 'other')):
            err(422, 'VALIDATION', 'Pick where the candidate came from.', 'source')
        out['source'] = s
    if b('experience_years') is not UNDEF:
        n = js_number(b('experience_years'))
        if not is_finite(n) or n < 0 or n > 50:
            err(422, 'VALIDATION', 'Experience is between 0 and 50 years.', 'experience_years')
        out['experience_years'] = n
    for k in ('current_ctc_paise', 'expected_ctc_paise', 'offer_ctc_paise'):
        if b(k) is not UNDEF:
            v = None if b(k) is None or b(k) == '' else js_number(b(k))
            if v is not None and (not is_integer(v) or v < 0):
                err(422, 'VALIDATION', 'Enter a valid amount.', k)
            out[k] = v
    if b('notice_days') is not UNDEF:
        v = None if b('notice_days') is None or b('notice_days') == '' else js_number(b('notice_days'))
        if v is not None and (not is_integer(v) or v < 0 or v > 180):
            err(422, 'VALIDATION', 'Notice is 0–180 days.', 'notice_days')
        out['notice_days'] = v
    if b('rating') is not UNDEF:
        v = None if b('rating') is None else js_number(b('rating'))
        if v is not None and (not is_integer(v) or v < 1 or v > 5):
            err(422, 'VALIDATION', 'Rating is 1 to 5.', 'rating')
        out['rating'] = v
    if b('notes') is not UNDEF:
        out['notes'] = _str(b('notes'))
    if b('joining_on') is not UNDEF:
        jo = b('joining_on')
        if jo is not None and not _is_date(jo):
            err(422, 'VALIDATION', 'Pick a joining date.', 'joining_on')
        out['joining_on'] = jo if js_truthy(jo) else None
    return out


def create_candidate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'recruitment.manage', 'Only HR, PMs and admins manage candidates.')
    o = find(ctx, 'openings', _str(req.b('opening_id')), 'Opening')
    if o.get('status') == 'filled':
        err(409, 'OPENING_FILLED', f'{o["role"]} is already filled.', 'opening_id')
    data = _cand_input(req.body, False)
    if any(c.get('opening_id') == o['id'] and c.get('email') == data['email'] for c in rows(ctx, 'candidates')):
        err(409, 'DUPLICATE', 'This candidate is already in the pipeline for this opening.', 'email')
    c = insert(
        ctx,
        'candidates',
        {
            'opening_id': o['id'],
            'name': data['name'],
            'email': data['email'],
            'phone': coalesce(data.get('phone', UNDEF), ''),
            'source': data['source'],
            'stage': 'applied',
            'experience_years': coalesce(data.get('experience_years', UNDEF), 0),
            'current_ctc_paise': coalesce(data.get('current_ctc_paise', UNDEF), None),
            'expected_ctc_paise': coalesce(data.get('expected_ctc_paise', UNDEF), None),
            'offer_ctc_paise': None,
            'notice_days': coalesce(data.get('notice_days', UNDEF), None),
            'resume_file_id': None,
            'rating': None,
            'notes': coalesce(data.get('notes', UNDEF), ''),
            'rejected_reason': None,
            'joining_on': None,
            'user_id': None,
        },
    )
    log_activity(ctx, 'candidate', c['id'], 'created', f'{js_str(ctx.user.get("name", UNDEF))} added {c["name"]} to {o["role"]}')
    if o.get('owner_id') != ctx.user_id:
        notify(ctx, o['owner_id'], 'NTF-14', 'New candidate', f'{c["name"]} was added to {o["role"]}.', '/recruitment')
    return c


def patch_candidate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'recruitment.manage', 'Only HR, PMs and admins manage candidates.')
    c = find(ctx, 'candidates', req.params['id'], 'Candidate')
    if c.get('stage') == 'hired':
        err(409, 'CANDIDATE_HIRED', 'This candidate is hired. Change their details in People.')
    pt = _cand_input(req.body, True)
    out = update(ctx, 'candidates', c['id'], pt, req.if_match)
    log_activity(ctx, 'candidate', c['id'], 'updated', f'{js_str(ctx.user.get("name", UNDEF))} updated {c["name"]}’s details')
    return out


def candidate_resume(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'recruitment.manage')
    c = find(ctx, 'candidates', req.params['id'], 'Candidate')
    name = _str(req.b('name'))
    size = js_number(req.b('size_bytes'))
    if not name or not js_str(coalesce(req.b('data_url'), '')).startswith('data:'):
        err(422, 'VALIDATION', 'Choose a resume file.', 'file')
    if size > 5 * 1024 * 1024:
        err(422, 'FILE_TOO_LARGE', 'Resumes can be up to 5 MB.', 'file')
    f = insert(
        ctx,
        'files',
        {
            'name': name,
            'mime': _str(req.b('mime')) or 'application/octet-stream',
            'size_bytes': size,
            'entity': 'candidate',
            'entity_id': c['id'],
            'data_url': js_str(req.b('data_url')),
        },
    )
    out = update(ctx, 'candidates', c['id'], {'resume_file_id': f['id']})
    log_activity(ctx, 'candidate', c['id'], 'resume', f'{js_str(ctx.user.get("name", UNDEF))} uploaded a resume ({name})')
    return out


def candidate_interview(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'recruitment.manage')
    c = find(ctx, 'candidates', req.params['id'], 'Candidate')
    if c.get('stage') in _CLOSED_STAGES:
        err(409, 'CANDIDATE_CLOSED', f'{c["name"]} is {c["stage"]}; no more interviews.')
    kind = req.b('kind')
    if not _in(kind, ('technical', 'hr', 'manager', 'assignment', 'final')):
        err(422, 'VALIDATION', 'Pick the interview type.', 'kind')
    at = _str(req.b('scheduled_at'))
    ms = date_parse(at) if at else NAN
    if not at or ms != ms:
        err(422, 'VALIDATION', 'Pick a date and time.', 'scheduled_at')
    iv = find(ctx, 'users', _str(req.b('interviewer_id')), 'Interviewer')
    if iv.get('status') != 'active':
        err(422, 'VALIDATION', 'Pick an active interviewer.', 'interviewer_id')
    rnd = sum(1 for i in rows(ctx, 'interviews') if i.get('candidate_id') == c['id']) + 1
    row = insert(
        ctx,
        'interviews',
        {
            'candidate_id': c['id'],
            'round': rnd,
            'kind': kind,
            'scheduled_at': iso_from_ms(ms),
            'interviewer_id': iv['id'],
            'result': 'pending',
            'rating': None,
            'feedback': '',
        },
    )
    if c.get('stage') in ('applied', 'screening'):
        update(ctx, 'candidates', c['id'], {'stage': 'interview'})
    log_activity(ctx, 'candidate', c['id'], 'interview', f'{js_str(ctx.user.get("name", UNDEF))} scheduled round {rnd} ({kind}) with {iv["name"]}')
    if iv['id'] != ctx.user_id:
        notify(ctx, iv['id'], 'NTF-14', 'Interview scheduled', f'{c["name"]} — round {rnd} ({kind}) on {fmt_date(at[:10], "long")}.', '/recruitment')
    return row


def patch_interview(req: Req) -> Any:
    ctx = req.ctx
    iv = find(ctx, 'interviews', req.params['id'], 'Interview')
    if iv.get('interviewer_id') != ctx.user_id and not has(ctx, 'recruitment.manage'):
        err(403, 'FORBIDDEN', 'Only the interviewer or the hiring team records the result.')
    result = req.b('result')
    if not _in(result, ('pending', 'pass', 'fail', 'no_show')):
        err(422, 'VALIDATION', 'Pick a result.', 'result')
    rb = req.b('rating')
    rating = None if nullish(rb) or rb == '' else js_number(rb)
    feedback = _str(req.b('feedback'))
    if result in ('pass', 'fail'):
        if rating is None or not is_integer(rating) or rating < 1 or rating > 5:
            err(422, 'VALIDATION', 'Rate the candidate 1 to 5.', 'rating')
        if js_len(feedback) < 10:
            err(422, 'VALIDATION', 'Write at least 10 characters of feedback.', 'feedback')
    out = update(ctx, 'interviews', iv['id'], {'result': result, 'rating': rating, 'feedback': feedback})
    c = find(ctx, 'candidates', iv['candidate_id'])
    rt = f' ({js_str(rating)}/5)' if js_truthy(rating) else ''
    log_activity(
        ctx,
        'candidate',
        c['id'],
        'interview_result',
        f'{js_str(ctx.user.get("name", UNDEF))} recorded round {js_str(iv["round"])}: {result.replace("_", " ", 1)}{rt}',
    )
    return out


def move_candidate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'recruitment.manage', 'Only HR, PMs and admins move candidates.')
    c = find(ctx, 'candidates', req.params['id'], 'Candidate')
    stage = req.b('stage')
    if not _in(stage, STAGES):
        err(422, 'VALIDATION', 'Pick a stage.', 'stage')
    if stage == c.get('stage'):
        return c
    if c.get('stage') == 'hired':
        err(409, 'CANDIDATE_HIRED', f'{c["name"]} is already hired.')
    o = find(ctx, 'openings', c['opening_id'], 'Opening')
    pt: dict[str, Any] = {'stage': stage}
    me_name = js_str(ctx.user.get('name', UNDEF))
    summary = f'{me_name} moved {c["name"]} to {stage}'
    if stage == 'offer':
        passed = any(i.get('candidate_id') == c['id'] and i.get('result') == 'pass' for i in rows(ctx, 'interviews'))
        if not passed:
            err(409, 'NEEDS_PASSED_INTERVIEW', f'{c["name"]} has no passed interview yet. Record at least one passed round before making an offer.')
        oc = req.b('offer_ctc_paise')
        if not nullish(oc):
            v = js_number(oc)
            if not is_integer(v) or v <= 0:
                err(422, 'VALIDATION', 'Enter the offered CTC.', 'offer_ctc_paise')
            pt['offer_ctc_paise'] = v
        if o.get('status') in ('open', 'interviewing'):
            o = update(ctx, 'openings', o['id'], {'status': 'offered'})
    if stage == 'interview' and o.get('status') == 'open':
        o = update(ctx, 'openings', o['id'], {'status': 'interviewing'})
    if stage in ('rejected', 'withdrawn'):
        reason = _str(req.b('reason'))
        if js_len(reason) < 5:
            err(422, 'VALIDATION', 'Give a reason of at least 5 characters.', 'reason')
        pt['rejected_reason'] = reason
        summary += f': {reason}'
    invite = None
    checklist = None
    if stage == 'hired':
        if c.get('stage') != 'offer':
            err(409, 'NOT_OFFERED', f'Make {c["name"]} an offer before marking them hired.')
        joining = coalesce(req.b('joining_on'), c.get('joining_on', UNDEF))
        if not _is_date(joining):
            err(422, 'VALIDATION', 'Set the joining date to mark the candidate hired.', 'joining_on')
        taken = _email_taken(ctx, c['email'])
        if taken:
            err(409, 'USER_EMAIL_TAKEN', f'{taken} Use another email for {c["name"]} first.')
        pt['joining_on'] = joining
        owner = _user(ctx, o.get('owner_id'))
        project = next((p for p in rows(ctx, 'projects') if p['id'] == o['project_id']), None) if js_truthy(o.get('project_id')) else None
        if owner and any(k in ('projects.edit', 'team.view') for k in permissions_of(owner)):
            reports_to = owner['id']
        else:
            reports_to = coalesce(project.get('pm_id', UNDEF) if project else UNDEF, o.get('owner_id', UNDEF))
        invite = insert(
            ctx,
            'invites',
            {
                'email': c['email'],
                'name': c['name'],
                'role': 'employee',
                'reports_to_id': reports_to,
                'token': 'inv_' + uid(),
                'expires_at': add_days(ctx.today, 14) + 'T00:00:00Z',
                'accepted_at': None,
            },
        )
        pt['user_id'] = invite['id']
        checklist = insert(
            ctx,
            'lifecycle_checklists',
            {
                'user_id': invite['id'],
                'kind': 'onboarding',
                'status': 'open',
                'start_on': joining,
                'last_day': None,
                'exit_reason': None,
                'exit_type': None,
                'rehire_eligible': None,
                'items': _onboarding_items(),
            },
        )
        hired_count = sum(1 for x in rows(ctx, 'candidates') if x.get('opening_id') == o['id'] and x.get('stage') == 'hired') + 1
        if hired_count >= o['count']:
            o = update(ctx, 'openings', o['id'], {'status': 'filled'})
        summary = f'{me_name} hired {c["name"]} for {o["role"]}, joining {fmt_date(joining, "long")}'
        log_activity(ctx, 'invite', invite['id'], 'create', f'Invited {c["name"]} ({c["email"]}) after hiring')
        log_activity(ctx, 'lifecycle', checklist['id'], 'created', f'Onboarding checklist started for {c["name"]}')
        for h in _hr_people(ctx):
            if h['id'] != ctx.user_id:
                notify(
                    ctx,
                    h['id'],
                    'NTF-14',
                    'New joiner',
                    f'{c["name"]} joins as {o["role"]} on {fmt_date(joining, "long")}. Onboarding checklist created.',
                    '/lifecycle',
                )
        if js_truthy(reports_to) and reports_to != ctx.user_id:
            notify(ctx, reports_to, 'NTF-14', 'New team member', f'{c["name"]} joins your team on {fmt_date(joining, "long")}.', '/lifecycle')
    out = update(ctx, 'candidates', c['id'], pt)
    log_activity(ctx, 'candidate', c['id'], 'hired' if stage == 'hired' else 'moved', summary)
    return {**out, 'invite_id': invite['id'] if invite else UNDEF, 'checklist_id': checklist['id'] if checklist else UNDEF}


# =====================================================================================================================
# Routes — lifecycle
# =====================================================================================================================
def list_lifecycle(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins manage onboarding and exits.')
    _sweep_exits(ctx)
    items = [_checklist_view(ctx, c) for c in list(rows(ctx, 'lifecycle_checklists'))]
    items = sorted(sorted(items, key=lambda x: locale_key(x['start_on']), reverse=True), key=lambda x: 0 if x['status'] == 'open' else 1)
    taken = {c.get('user_id') for c in rows(ctx, 'lifecycle_checklists') if c.get('status') == 'open'}
    people = [
        {'id': u['id'], 'name': prop(u, 'name'), 'kind': 'user'} for u in rows(ctx, 'users') if u.get('status') == 'active' and not u.get('is_super_admin')
    ] + [{'id': i['id'], 'name': f'{js_str(prop(i, "name"))} (invited)', 'kind': 'invite'} for i in rows(ctx, 'invites') if not js_truthy(i.get('accepted_at'))]
    return {'items': items, 'people': [p for p in people if p['id'] not in taken]}


def create_lifecycle(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins start onboarding or exits.')
    kind = 'offboarding' if req.b('kind') == 'offboarding' else 'onboarding'
    pid = _str(req.b('person_id'))
    u = next((x for x in rows(ctx, 'users') if x['id'] == pid), None)
    inv = None if u else next((i for i in rows(ctx, 'invites') if i['id'] == pid and not js_truthy(i.get('accepted_at'))), None)
    if not u and not inv:
        err(422, 'VALIDATION', 'Pick a person.', 'person_id')
    if kind == 'offboarding' and not u:
        err(422, 'VALIDATION', 'Exits are for people who have joined.', 'person_id')
    if u and u['id'] == ctx.user_id and kind == 'offboarding':
        err(403, 'FORBIDDEN', 'Someone else records your own exit.')
    if any(c.get('user_id') == pid and c.get('status') == 'open' and c.get('kind') == kind for c in rows(ctx, 'lifecycle_checklists')):
        err(409, 'DUPLICATE', f'An open {kind} checklist already exists for this person.', 'person_id')
    start = _str(req.b('start_on'))
    if not _is_date(start):
        err(422, 'VALIDATION', 'Pick the joining date.' if kind == 'onboarding' else 'Pick the resignation date.', 'start_on')
    name = u['name'] if u else inv['name']  # type: ignore[index]
    if kind == 'offboarding':
        last = _str(req.b('last_day'))
        if not _is_date(last):
            err(422, 'VALIDATION', 'Pick the last working day.', 'last_day')
        if last < start:
            err(422, 'DATE_RANGE', 'The last day can’t be before the resignation date.', 'last_day')
        et = req.b('exit_type')
        if not _in(et, ('resignation', 'termination', 'contract_end', 'retirement')):
            err(422, 'VALIDATION', 'Pick the exit type.', 'exit_type')
        reason = _str(req.b('exit_reason'))
        if js_len(reason) < 5:
            err(422, 'VALIDATION', 'Record the reason in at least 5 characters.', 'exit_reason')
        rh = req.b('rehire_eligible')
        row = insert(
            ctx,
            'lifecycle_checklists',
            {
                'user_id': u['id'],  # type: ignore[index]
                'kind': kind,
                'status': 'open',
                'start_on': start,
                'last_day': last,
                'exit_reason': reason,
                'exit_type': et,
                'rehire_eligible': None if nullish(rh) else js_truthy(rh),
                'items': _offboarding_items(start, last),
            },
        )
        if js_truthy(u.get('reports_to_id')):  # type: ignore[union-attr]
            notify(ctx, u['reports_to_id'], 'NTF-14', 'Exit started', f'{name}’s last day is {fmt_date(last, "long")}. Plan knowledge transfer.', '/lifecycle')  # type: ignore[index]
    else:
        row = insert(
            ctx,
            'lifecycle_checklists',
            {
                'user_id': pid,
                'kind': kind,
                'status': 'open',
                'start_on': start,
                'last_day': None,
                'exit_reason': None,
                'exit_type': None,
                'rehire_eligible': None,
                'items': _onboarding_items(),
            },
        )
    log_activity(ctx, 'lifecycle', row['id'], 'created', f'{js_str(ctx.user.get("name", UNDEF))} started {kind} for {name}')
    return row


def patch_lifecycle(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage')
    cl = find(ctx, 'lifecycle_checklists', req.params['id'], 'Checklist')
    pt: dict[str, Any] = {}
    ld = req.b('last_day')
    if ld is not UNDEF:
        if cl.get('kind') != 'offboarding':
            err(422, 'VALIDATION', 'Only exits have a last day.')
        if not _is_date(ld) or ld < cl['start_on']:
            err(422, 'DATE_RANGE', 'Pick a last day on or after the resignation date.', 'last_day')
        pt['last_day'] = ld
        fresh = {f['id']: f for f in reversed(_offboarding_items(cl['start_on'], ld))}
        pt['items'] = [
            {**i, 'due_offset_days': coalesce(fresh[i['id']]['due_offset_days'] if i['id'] in fresh else UNDEF, i['due_offset_days'])} for i in cl['items']
        ]
    if req.b('exit_reason') is not UNDEF:
        pt['exit_reason'] = _str(req.b('exit_reason'))
    if req.b('exit_type') is not UNDEF:
        pt['exit_type'] = req.b('exit_type')
    if req.b('rehire_eligible') is not UNDEF:
        pt['rehire_eligible'] = None if req.b('rehire_eligible') is None else js_truthy(req.b('rehire_eligible'))
    out = update(ctx, 'lifecycle_checklists', cl['id'], pt, req.if_match)
    p, out = _person_of(ctx, out)
    log_activity(ctx, 'lifecycle', cl['id'], 'updated', f'{js_str(ctx.user.get("name", UNDEF))} updated the {cl["kind"]} record for {js_str(p["name"])}')
    _sweep_exits(ctx)
    return scope().find_by('lifecycle_checklists', 'id', out['id']) or out


def toggle_item(req: Req) -> Any:
    ctx = req.ctx
    cl = find(ctx, 'lifecycle_checklists', req.params['id'], 'Checklist')
    item = next((i for i in cl['items'] if i['id'] == req.params['itemId']), None)
    if not item:
        err(404, 'NOT_FOUND', 'Checklist item not found.')
    assert item is not None
    p, cl = _person_of(ctx, cl)
    if not _can_toggle_item(ctx, cl, item, p):
        who = 'the person’s manager' if item.get('owner_role') == 'manager' else 'the person' if item.get('owner_role') == 'employee' else 'HR'
        err(403, 'FORBIDDEN', f'This item belongs to {who}.')
    done = js_truthy(req.b('done'))
    pu = p['user']
    if done and item['id'] == 'asset_return' and pu:
        out_ = [a for a in rows(ctx, 'assets') if a.get('assigned_to') == pu['id'] and a.get('status') == 'assigned']
        if out_:
            err(
                409,
                'ASSETS_OUTSTANDING',
                f'{len(out_)} asset{"s are" if len(out_) > 1 else " is"} still assigned ({", ".join(js_str(a.get("tag", UNDEF)) for a in out_)}). Record the return in Assets first.',
            )
    if (
        done
        and item['id'] == 'laptop'
        and pu
        and not any(a.get('assigned_to') == pu['id'] and a.get('category') == 'laptop' and a.get('status') == 'assigned' for a in rows(ctx, 'assets'))
    ):
        warn(ctx, 'NO_LAPTOP_ASSIGNED', f'No laptop is assigned to {js_str(p["name"])} in Assets yet.')
    items = [{**i, 'done_on': ctx.today if done else None, 'done_by': ctx.user_id if done else None} if i['id'] == item['id'] else i for i in cl['items']]
    complete = all(js_truthy(i.get('done_on')) for i in items)
    out = update(ctx, 'lifecycle_checklists', cl['id'], {'items': items, 'status': 'completed' if complete else 'open'})
    me_name = js_str(ctx.user.get('name', UNDEF))
    log_activity(
        ctx,
        'lifecycle',
        cl['id'],
        'item_done' if done else 'item_reopened',
        f'{me_name} {"completed" if done else "reopened"} “{item["label"]}” for {js_str(p["name"])}',
    )
    if complete:
        what = 'Onboarding' if cl['kind'] == 'onboarding' else 'Exit'
        log_activity(ctx, 'lifecycle', cl['id'], 'completed', f'{what} checklist completed for {js_str(p["name"])}')
        for h in _hr_people(ctx):
            if h['id'] != ctx.user_id:
                notify(ctx, h['id'], 'NTF-05', f'{what} complete', f'Every item for {js_str(p["name"])} is done.', '/lifecycle')
        if cl['kind'] == 'offboarding' and js_truthy(cl.get('last_day')) and cl['last_day'] <= ctx.today and pu and pu.get('status') == 'active':
            _deactivate_user(ctx, pu, f'exit completed, last day {fmt_date(cl["last_day"], "long")}')
            warn(ctx, 'USER_DEACTIVATED', f'{js_str(p["name"])} has been deactivated (last day {fmt_date(cl["last_day"])}).')
        elif cl['kind'] == 'offboarding' and js_truthy(cl.get('last_day')):
            warn(ctx, 'EXIT_SCHEDULED', f'{js_str(p["name"])} will be deactivated after the last day, {fmt_date(cl["last_day"])}.')
    return out


# =====================================================================================================================
# Routes — assets
# =====================================================================================================================
def list_assets(req: Req) -> Any:
    ctx = req.ctx
    all_ = _is_hr(ctx)
    lst = sorted((_asset_view(ctx, a) for a in rows(ctx, 'assets') if all_ or a.get('assigned_to') == ctx.user_id), key=lambda a: locale_key(a['tag']))
    value: Any = 0
    for a in lst:
        if a.get('status') not in ('retired', 'lost'):
            value += a['purchase_cost_paise']
    return {
        'mode': 'all' if all_ else 'mine',
        'items': lst,
        'summary': {
            'total': len(lst),
            'assigned': sum(1 for a in lst if a.get('status') == 'assigned'),
            'in_stock': sum(1 for a in lst if a.get('status') == 'in_stock'),
            'repair': sum(1 for a in lst if a.get('status') == 'repair'),
            'pending_return': sum(1 for a in lst if a['pending_return']),
            'value_paise': value,
        },
    }


def asset_history(req: Req) -> Any:
    ctx = req.ctx
    a = find(ctx, 'assets', req.params['id'], 'Asset')
    if not _is_hr(ctx) and a.get('assigned_to') != ctx.user_id:
        err(403, 'FORBIDDEN', 'You can see the history of assets assigned to you.')
    hist = sorted(
        (lg for lg in rows_where_ro(ctx, 'activity_log', 'entity_id', a['id']) if lg.get('entity') == 'asset'),
        key=lambda lg: locale_key(lg['at']),
        reverse=True,
    )
    return [{'at': lg['at'], 'summary': lg['summary'], 'actor': user_name(ctx, lg.get('actor_id'))} for lg in hist]


_TAG_RE = re.compile(r'^[A-Z0-9][A-Z0-9-]{2,23}\Z')


def _asset_input(ctx: Ctx, body: Any, partial: bool, self_id: Any = UNDEF) -> dict[str, Any]:
    out: dict[str, Any] = {}
    b = lambda k: _g(body, k)  # noqa: E731
    if not partial or b('tag') is not UNDEF:
        t = _str(b('tag')).upper()
        if not _TAG_RE.search(t):
            err(422, 'VALIDATION', 'Use a tag like SEE-LAP-015 (letters, digits and dashes).', 'tag')
        if any(a.get('tag') == t and a['id'] != self_id for a in rows(ctx, 'assets')):
            err(409, 'ASSET_TAG_TAKEN', f'Tag {t} is already used.', 'tag')
        out['tag'] = t
    if not partial or b('name') is not UNDEF:
        n = _str(b('name'))
        if js_len(n) < 2:
            err(422, 'VALIDATION', 'Describe the asset, e.g. “Dell Latitude 5440”.', 'name')
        out['name'] = n
    if not partial or b('category') is not UNDEF:
        if not _in(b('category'), ASSET_CATS):
            err(422, 'VALIDATION', 'Pick a category.', 'category')
        out['category'] = b('category')
    if b('serial') is not UNDEF:
        out['serial'] = _str(b('serial'))
    if not partial or b('purchase_on') is not UNDEF:
        if not _is_date(b('purchase_on')):
            err(422, 'VALIDATION', 'Pick the purchase date.', 'purchase_on')
        if b('purchase_on') > ctx.today:
            err(422, 'DATE_RANGE', 'The purchase date can’t be in the future.', 'purchase_on')
        out['purchase_on'] = b('purchase_on')
    if not partial or b('purchase_cost_paise') is not UNDEF:
        v = js_number(b('purchase_cost_paise'))
        if not is_integer(v) or v < 0:
            err(422, 'VALIDATION', 'Enter the purchase cost.', 'purchase_cost_paise')
        out['purchase_cost_paise'] = v
    if b('warranty_until') is not UNDEF:
        wu = b('warranty_until')
        if js_truthy(wu) and not _is_date(wu):
            err(422, 'VALIDATION', 'Pick the warranty end date.', 'warranty_until')
        out['warranty_until'] = wu if js_truthy(wu) else None
    if b('notes') is not UNDEF:
        out['notes'] = _str(b('notes'))
    return out


def create_asset(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins manage the asset register.')
    data = _asset_input(ctx, req.body, False)
    a = insert(ctx, 'assets', {'serial': '', 'warranty_until': None, 'notes': '', **data, 'status': 'in_stock', 'assigned_to': None, 'assigned_on': None})
    log_activity(ctx, 'asset', a['id'], 'created', f'{js_str(ctx.user.get("name", UNDEF))} registered {a["tag"]} ({a["name"]})')
    return a


def patch_asset(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins manage the asset register.')
    a = find(ctx, 'assets', req.params['id'], 'Asset')
    pt = _asset_input(ctx, req.body, True, a['id'])
    if req.b('status') is not UNDEF:
        st = req.b('status')
        if not _in(st, ('in_stock', 'repair', 'retired', 'lost')):
            err(422, 'VALIDATION', 'Use Assign or Return to change who holds it.', 'status')
        if a.get('status') == 'assigned' and st != 'lost':
            err(409, 'ASSET_ASSIGNED', f'{a["tag"]} is with {user_name(ctx, a.get("assigned_to"))}. Record the return first.', 'status')
        pt['status'] = st
        if st == 'lost':
            pt.update({'assigned_to': None, 'assigned_on': None})
    out = update(ctx, 'assets', a['id'], pt, req.if_match)
    stt = f' — status {pt["status"].replace("_", " ", 1)}' if js_truthy(pt.get('status')) else ''
    log_activity(ctx, 'asset', a['id'], 'updated', f'{js_str(ctx.user.get("name", UNDEF))} updated {out["tag"]}{stt}')
    return out


def assign_asset(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins assign assets.')
    a = find(ctx, 'assets', req.params['id'], 'Asset')
    if a.get('status') != 'in_stock':
        err(
            409,
            'ASSET_NOT_AVAILABLE',
            f'{a["tag"]} is already with {user_name(ctx, a.get("assigned_to"))}. Record the return first.'
            if a.get('status') == 'assigned'
            else f'{a["tag"]} is {js_str(a.get("status")).replace("_", " ", 1)} and can’t be assigned.',
        )
    u = find(ctx, 'users', _str(req.b('user_id')), 'Person')
    if u.get('status') != 'active':
        err(422, 'VALIDATION', f'{u["name"]} is not an active user.', 'user_id')
    out = update(ctx, 'assets', a['id'], {'status': 'assigned', 'assigned_to': u['id'], 'assigned_on': ctx.today})
    log_activity(ctx, 'asset', a['id'], 'assigned', f'{js_str(ctx.user.get("name", UNDEF))} assigned {a["tag"]} to {u["name"]}')
    notify(ctx, u['id'], 'NTF-14', 'Asset assigned', f'{a["tag"]} ({a["name"]}) is now assigned to you.', '/assets')
    return out


def return_asset(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins record returns.')
    a = find(ctx, 'assets', req.params['id'], 'Asset')
    if a.get('status') != 'assigned':
        err(409, 'ASSET_NOT_ASSIGNED', f'{a["tag"]} is not assigned to anyone.')
    st = 'repair' if req.b('status') == 'repair' else 'in_stock'
    note = _str(req.b('note'))
    who = user_name(ctx, a.get('assigned_to'))
    out = update(ctx, 'assets', a['id'], {'status': st, 'assigned_to': None, 'assigned_on': None})
    rep = ' (sent for repair)' if st == 'repair' else ''
    nt = f' — {note}' if note else ''
    log_activity(ctx, 'asset', a['id'], 'returned', f'{js_str(ctx.user.get("name", UNDEF))} recorded the return of {a["tag"]} from {who}{rep}{nt}')
    return out


# =====================================================================================================================
# Routes — dashboard
# =====================================================================================================================
def hr_dashboard(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['people.manage', 'people.view_all'], 'The HR dashboard is for HR and admins.')
    _sweep_exits(ctx)
    fm = prop(_S(ctx), 'company.fy_start_month')
    q = quarter_of(ctx.today, fm)
    users = [u for u in rows(ctx, 'users') if not u.get('is_super_admin')]
    active = [u for u in users if u.get('status') == 'active']
    lcs = rows(ctx, 'lifecycle_checklists')

    def exits_in(frm: str, to: str) -> list[dict]:
        return [c for c in lcs if c.get('kind') == 'offboarding' and js_truthy(c.get('last_day')) and frm <= c['last_day'] <= to]

    exits12 = len(exits_in(add_days(ctx.today, -365), ctx.today))
    cands = rows(ctx, 'candidates')
    pend = sorted((x for x in rows(ctx, 'leave_requests') if x.get('status') == 'pending'), key=lambda x: locale_key(x['from_on']))
    cycs = [c for c in rows(ctx, 'appraisal_cycles') if c['status'] != 'draft']
    cycs = sorted(sorted(cycs, key=lambda c: locale_key(c['period_from']), reverse=True), key=lambda c: 1 if c['status'] == 'closed' else 0)
    cyc = cycs[0] if cycs else None
    cv = _cycle_view(ctx, cyc) if cyc else None
    due = []
    for cl in [c for c in lcs if c.get('status') == 'open']:
        p, cl = _person_of(ctx, cl)
        for i in cl['items']:
            if js_truthy(i.get('done_on')):
                continue
            d = add_days(cl['start_on'], i['due_offset_days'])
            days = diff_days(d, ctx.today)
            if days <= 7:
                due.append({'checklist_id': cl['id'], 'person': p['name'], 'kind': cl['kind'], 'label': i['label'], 'due_on': d, 'days': days})
    lcs = rows(ctx, 'lifecycle_checklists')
    back = [a for a in (_asset_view(ctx, a) for a in rows(ctx, 'assets')) if a['pending_return']]
    # v7 "Needs attention": who needs HR's action today (persona round).
    pending_expenses = sum(1 for x in rows(ctx, 'expense_claims') if x.get('status') in ('submitted', 'approved'))
    wf_waiting = sum(1 for i in rows(ctx, 'wf_instances') if i.get('status') == 'in_progress' and i.get('current_assignee_id') == ctx.user_id)
    docs_to_verify = sum(1 for d in rows(ctx, 'employee_documents') if d.get('status') == 'pending')
    # Probation ends 6 months after joining; due = ends within 30 days (or recently ended) with no confirmation letter issued yet.
    confirmed = {d.get('employee_id') for d in rows(ctx, 'issued_documents') if d.get('category') == 'confirmation_letter' and d.get('status') != 'revoked'}
    lo, hi = add_days(ctx.today, -90), add_days(ctx.today, 30)
    probation_due = 0
    for u in active:
        if u['id'] in confirmed:
            continue
        end = add_months(u['joined_on'], 6)
        if lo <= end <= hi:
            probation_due += 1
    # Outstanding acknowledgements on must-read company policies across active staff.
    must_read = [
        d
        for d in rows(ctx, 'documents')
        if not js_truthy(d.get('project_id')) and js_truthy(d.get('must_read')) and not js_truthy(d.get('archived')) and d.get('visibility') == 'company'
    ]
    best: dict = {}
    for a in rows(ctx, 'document_acks'):
        k = (a.get('document_id'), a.get('user_id'))
        best.setdefault(k, []).append(a.get('version'))
    unacked = 0
    for doc in must_read:
        for u in active:
            vs = best.get((doc['id'], u['id']))
            if not vs or not any(v is not None and v >= doc['current_version'] for v in vs):
                unacked += 1
    attention = [
        {
            'key': 'leave',
            'label': 'Leave approvals pending',
            'count': len(pend),
            'to': '/leave',
            'hint': 'Leave requests waiting for a decision across the company.',
        },
        {
            'key': 'expenses',
            'label': 'Expense claims open',
            'count': pending_expenses,
            'to': '/expenses',
            'hint': 'Submitted claims waiting for a decision anywhere in the company, plus approved claims waiting to be paid out.',
        },
        {'key': 'workflows', 'label': 'Workflow steps on me', 'count': wf_waiting, 'to': '/workflows', 'hint': 'Workflow stages currently assigned to you.'},
        {
            'key': 'docs',
            'label': 'Documents to verify',
            'count': docs_to_verify,
            'to': '/documents?tab=employees',
            'hint': 'Employee uploads waiting for HR verification.',
        },
        {
            'key': 'probation',
            'label': 'Probation confirmations due',
            'count': probation_due,
            'to': '/workflows',
            'hint': 'Probation (6 months) ends within 30 days, or ended recently, with no confirmation letter issued.',
        },
        {
            'key': 'policies',
            'label': 'Policy acks outstanding',
            'count': unacked,
            'to': '/documents?tab=policies',
            'hint': 'Must-read policies not yet acknowledged, totalled across active staff.',
        },
    ]
    joiners = sum(1 for u in users if q['from'] <= u['joined_on'] <= q['to']) + sum(
        1
        for i in rows(ctx, 'invites')
        if not js_truthy(i.get('accepted_at')) and any(c.get('user_id') == i['id'] and q['from'] <= c['start_on'] <= q['to'] for c in lcs)
    )
    open_positions: Any = 0
    for o in rows(ctx, 'openings'):
        if o.get('status') not in ('filled', 'on_hold'):
            open_positions += js_max(0, o['count'] - sum(1 for c in cands if c.get('opening_id') == o['id'] and c.get('stage') == 'hired'))
    pli = []
    for x in pend[:6]:
        u = _user(ctx, x['user_id'])
        pli.append(
            {
                'id': x['id'],
                'name': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
                'hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
                'type': x['type'],
                'from_on': x['from_on'],
                'to_on': x['to_on'],
                'days': x['days'],
                'approver': user_name(ctx, x['approver_id']) if js_truthy(x.get('approver_id')) else None,
            }
        )
    na = len(active)
    return {
        'attention': attention,
        'headcount': na,
        'joiners_q': joiners,
        'exits_q': len(exits_in(q['from'], q['to'])),
        'attrition_pct': exits12 / ((na + na + exits12) / 2) if na else None,
        'quarter_label': q['label'],
        'open_positions': open_positions,
        'candidates_by_stage': [
            {'stage': s, 'count': sum(1 for c in cands if c.get('stage') == s)} for s in ('applied', 'screening', 'interview', 'offer', 'hired')
        ],
        'pending_leave': len(pend),
        'pending_leave_items': pli,
        'on_time_pct': task_stats(ctx, {'userIds': [u['id'] for u in active], 'from': add_days(ctx.today, -90), 'to': ctx.today})['on_time_pct'],
        'cycle': {
            'id': cv['id'],
            'name': cv['name'],
            'status': cv['status'],
            'total': cv['total'],
            'completed': cv['counts']['completed'],
            'counts': cv['counts'],
        }
        if cv
        else None,
        'lifecycle_due': sorted(due, key=lambda x: x['days'])[:8],
        'assets_due_back': [
            {
                'id': a['id'],
                'tag': a['tag'],
                'name': a['name'],
                'person': coalesce(a['assignee_name'], '—'),
                'last_day': coalesce(
                    next((c.get('last_day', UNDEF) for c in lcs if c.get('kind') == 'offboarding' and c.get('user_id') == a.get('assigned_to')), UNDEF),
                    None,
                ),
            }
            for a in back
        ],
    }


# =====================================================================================================================
def register(r: Any) -> None:
    # v7: appraisal workflows mirror their progress onto the appraisal and keep the cycle status in step
    try:
        from app.routes.workflows_v7 import set_appraisal_sync_hook
    except ImportError:  # workflows_v7 not ported yet
        set_appraisal_sync_hook = None  # type: ignore[assignment]
    if set_appraisal_sync_hook is not None:

        def _hook(ctx: Ctx, cycle_id: Any) -> None:
            sync_cycle(ctx, cycle_id)

        set_appraisal_sync_hook(_hook)
    r.get('/leave/taken', leave_taken)
    # leave requests
    r.get('/leave-requests', list_leave_requests)
    r.post('/leave-requests', create_leave_request)
    r.post('/leave-requests/:id/approve', approve_leave)
    r.post('/leave-requests/:id/reject', reject_leave)
    r.post('/leave-requests/:id/cancel', cancel_leave)
    r.put('/leave-policies/:type', put_leave_policy)
    # performance
    r.get('/performance-people', performance_people)
    r.get('/performance/:userId', performance_of)
    # goals
    r.post('/goals', create_goal)
    r.patch('/goals/:id', patch_goal)
    r.post('/goals/:id/approve', approve_goal)
    r.delete('/goals/:id', delete_goal)
    # feedback
    r.post('/feedback', create_feedback)
    # appraisals
    r.get('/appraisal-cycles', list_cycles)
    r.post('/appraisal-cycles', create_cycle)
    r.post('/appraisal-cycles/:id/launch', launch_cycle)
    r.post('/appraisal-cycles/:id/close', close_cycle)
    r.get('/appraisals', list_appraisals)
    r.get('/appraisals/:id', get_appraisal)
    r.post('/appraisals/:id/self', appraisal_self)
    r.post('/appraisals/:id/manager', appraisal_manager)
    r.post('/appraisals/:id/hr', appraisal_hr)
    r.post('/appraisals/:id/send-back', appraisal_send_back)
    # recruitment
    r.get('/recruitment', recruitment)
    r.get('/candidates/:id', get_candidate)
    r.post('/candidates', create_candidate)
    r.patch('/candidates/:id', patch_candidate)
    r.post('/candidates/:id/resume', candidate_resume)
    r.post('/candidates/:id/interviews', candidate_interview)
    r.patch('/interviews/:id', patch_interview)
    r.post('/candidates/:id/move', move_candidate)
    # lifecycle
    r.get('/lifecycle', list_lifecycle)
    r.post('/lifecycle', create_lifecycle)
    r.patch('/lifecycle/:id', patch_lifecycle)
    r.post('/lifecycle/:id/items/:itemId', toggle_item)
    # assets
    r.get('/assets', list_assets)
    r.get('/assets/:id/history', asset_history)
    r.post('/assets', create_asset)
    r.patch('/assets/:id', patch_asset)
    r.post('/assets/:id/assign', assign_asset)
    r.post('/assets/:id/return', return_asset)
    # dashboard
    r.get('/hr/dashboard', hr_dashboard)


__all__ = ['register', 'sync_cycle', 'balance_for']
