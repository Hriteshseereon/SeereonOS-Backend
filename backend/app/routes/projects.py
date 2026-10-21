"""Port of src/mock/routes/projects.ts — Module B: projects & delivery. Projects, milestones, WBS tasks (write side of the
shared task contract), allocations, schedule / critical path, money (margins, invoices, payments), change requests,
openings, WBS import and the client view. The v5 costing routes are registered first (app.engine.costing).

Exported helpers (snake_case of the TS exports): my_project_ids, can_view, can_edit, next_task_no, sync_achieved.

JS identity: the TS `update()` changes the row object in place; where the TS reads a row after updating it, the port
reads the row returned by update() (several messages / checks therefore see the NEW values, exactly like the reference).
"""

from __future__ import annotations

import calendar as _calendar
import copy
import re
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    calendar_for,
    can_see_margins,
    err,
    find,
    find_by,
    get_db,
    has,
    has_any,
    insert,
    log_activity,
    next_number,
    notify,
    now_iso,
    patch,
    permissions_of,
    remove,
    remove_where,
    rows,
    scope,
    uid,
    update,
    user_name,
    warn,
)
from app.domain.calendar import add_days, add_working_days, diff_days, fmt_date, is_real_iso_date, is_working_day, max_iso, min_iso, working_days_between
from app.domain.costing import margin_pct, markup_pct
from app.domain.delivery import STAGE, TASK_TAG, progress_of, stage_of
from app.domain.labels import PROJECT_STATUS
from app.domain.money import fmt_money
from app.engine.analytics import (
    allocation_on,
    cal,
    costed_entries,
    current_rate_ph,
    deps_of,
    ensure_swept,
    milestone_state,
    open_blockers,
    project_metrics,
    redact_metrics,
    schedule_of,
    sort_wbs,
    sync_milestone_dates,
    task_actuals,
)
from app.engine.analytics import (
    compute_schedule as compute_schedule_core,
)
from app.engine.analytics import (
    sweep_milestones as sweep_milestones_core,
)
from app.engine.costing import (
    CostingDeps,
    analyse,
    can_plan_cost,
    insert_plan_allocations,
    insert_project_documents,
    project_row_info,
    public_analysis,
    register_costing_routes,
    spread_milestones,
    validate_costing,
    validate_create_documents,
)
from app.engine.performance import task_stats, task_timeliness
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    iso_from_ms,
    js_ceil,
    js_len,
    js_max,
    js_number,
    js_or,
    js_round,
    js_sort,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    now_ms,
    pad_start,
    prop,
    to_fixed,
)
from app.routes import taskflow as _taskflow
from app.routes import tasks as _tasks

# =====================================================================================================================
# Access
# =====================================================================================================================
STATUSES = ['initiated', 'active', 'on_hold', 'npa', 'completed', 'closed']
TYPES = ['product', 'service', 'saas', 'ams']
MODELS = ['fixed', 'tm', 'subscription', 'ams_retainer']
STREAMS = ['sap_consulting', 'products', 'services', 'saas', 'ams']
TASK_STATUSES = ['todo', 'in_progress', 'blocked', 'in_review', 'done']
CX = ['low', 'medium', 'high', 'very_high']
TAGS = list(TASK_TAG.keys())


def _iso_test(s: Any) -> bool:
    return is_real_iso_date(s)


def _g(o: Any, k: str) -> Any:
    """o?.k for a JSON value (UNDEF unless o is an object with k)"""
    return o.get(k, UNDEF) if isinstance(o, dict) else UNDEF


def _has_key(o: Any, k: str) -> bool:
    return isinstance(o, dict) and k in o


def _in(v: Any, lst: list) -> bool:
    """lst.includes(v) for a list of strings (non-strings never match)"""
    return isinstance(v, str) and v in lst


def _intify(n: Any) -> Any:
    return int(n) if isinstance(n, float) and n.is_integer() and abs(n) < 2**53 else n


def _my_deal_project_ids(ctx: Ctx) -> set:
    me = ctx.user['id']
    mine = [d for d in rows(ctx, 'deals') if d.get('owner_id') == me]
    ids = {d['project_id'] for d in mine if js_truthy(d.get('project_id'))}
    deal_ids = {d['id'] for d in mine}
    for p in rows(ctx, 'projects'):
        if js_truthy(p.get('deal_id')) and p['deal_id'] in deal_ids:
            ids.add(p['id'])
    return ids


def _my_task_project_ids(ctx: Ctx) -> set:
    me = ctx.user['id']
    return {t['project_id'] for t in rows(ctx, 'tasks') if t.get('owner_id') == me or t.get('backup_id') == me}


def my_project_ids(ctx: Ctx) -> set:
    """Projects a user is on without `projects.view_all`: they PM it, own/back up a task, are allocated, or own its deal.
    Memoised per request while the four source lists are unchanged (returns a fresh set)."""
    srcs = (rows(ctx, 'tasks'), rows(ctx, 'projects'), rows(ctx, 'allocations'), rows(ctx, 'deals'))
    memo = scope().memo
    k = ('routes/projects:mine', ctx)
    hit = memo.get(k)
    if hit is not None and all(a is b for a, b in zip(hit[0], srcs, strict=True)):
        return set(hit[1])
    me = ctx.user['id']
    ids = _my_task_project_ids(ctx)
    for p in srcs[1]:
        if p.get('pm_id') == me:
            ids.add(p['id'])
    for a in srcs[2]:
        if a.get('user_id') == me:
            ids.add(a['project_id'])
    ids |= _my_deal_project_ids(ctx)
    memo[k] = (srcs, frozenset(ids))
    return set(ids)


def can_view(ctx: Ctx, p: dict) -> bool:
    if has(ctx, 'projects.view_all'):
        return True
    return p['id'] in my_project_ids(ctx)


def can_edit(ctx: Ctx, p: dict) -> bool:
    """`projects.manage_all` on any project; `projects.edit` on projects they manage."""
    return has(ctx, 'projects.manage_all') or (has(ctx, 'projects.edit') and p.get('pm_id') == ctx.user['id'])


def _get_project(ctx: Ctx, id_: Any) -> dict:
    p = find(ctx, 'projects', id_, 'Project')
    if not can_view(ctx, p):
        err(403, 'FORBIDDEN', 'You don’t have access to this project.')
    return p


def _editable_project(ctx: Ctx, id_: Any) -> dict:
    p = _get_project(ctx, id_)
    if not can_edit(ctx, p):
        err(
            403,
            'FORBIDDEN',
            'Only the project manager of this project (or an admin) can change it.' if has(ctx, 'projects.edit') else 'You can’t change this project.',
        )
    return p


def _is_admin(ctx: Ctx) -> bool:
    return has(ctx, 'projects.manage_all')


def _need(v: Any, field: str, label: str) -> str:
    s = js_trim(v) if isinstance(v, str) else ''
    if not s:
        err(422, 'VALIDATION', f'{label} is required.', field)
    return s


def _need_date(v: Any, field: str, label: str) -> str:
    s = js_trim(v) if isinstance(v, str) else ''
    if not _iso_test(s):
        err(422, 'VALIDATION', f'{label} needs a valid date.', field)
    return s


def _money(v: Any, field: str, label: str, allow_zero: bool = True) -> Any:
    n = js_number(coalesce(v, 0))
    if not is_finite(n) or n < 0 or (not allow_zero and n <= 0) or not is_integer(n):
        err(422, 'MONEY_INVALID', f'{label} must be {"zero or more" if allow_zero else "more than zero"}.', field)
    return _intify(n)


def _active_user(ctx: Ctx, id_: Any, field: str, must_manage_projects: bool = False) -> dict | None:
    if not js_truthy(id_):
        return None
    u = _tasks._user(ctx, id_)
    if u is None or u.get('status') != 'active':
        err(422, 'VALIDATION', 'Choose an active person from your team.', field)
    if must_manage_projects and 'projects.edit' not in permissions_of(u):
        err(422, 'VALIDATION', f'{js_str(u["name"])} can’t manage projects. Choose a project manager or an admin.', field)
    return u


# =====================================================================================================================
# Numbering (BR-19: never reused)
# =====================================================================================================================
def _seq(ctx: Ctx, scope_name: str) -> dict | None:
    tid = ctx.tenant_id
    return next((x for x in get_db().number_sequences if x.get('tenant_id') == tid and x.get('scope') == scope_name), None)


def _peek_number(ctx: Ctx, scope_name: str, start: Any) -> Any:
    s = _seq(ctx, scope_name)
    return js_max(coalesce(prop(s, 'next_value'), start), start)


def _take_number(ctx: Ctx, scope_name: str, start: Any) -> Any:
    s = _seq(ctx, scope_name)
    if s is not None and s['next_value'] < start:
        patch('number_sequences', s, {'next_value': start})
    return next_number(ctx, scope_name, start)


def _all_milestones(ctx: Ctx, pid: Any) -> list[dict]:
    return [m for m in rows(ctx, 'milestones') if m['project_id'] == pid]


def _live_milestones(ctx: Ctx, pid: Any) -> list[dict]:
    return sorted((m for m in _all_milestones(ctx, pid) if not js_truthy(m.get('deleted_at'))), key=lambda m: m['no'])


def _ms_scope(pid: Any) -> str:
    return f'milestone:{js_str(pid)}'


def _task_scope(pid: Any, ms_id: Any) -> str:
    return f'task:{js_str(ms_id)}' if js_truthy(ms_id) else f'task:none:{js_str(pid)}'


def _next_ms_no(ctx: Ctx, pid: Any, take: bool) -> Any:
    start = js_max(0, *[m['no'] for m in _all_milestones(ctx, pid)]) + 1
    return _take_number(ctx, _ms_scope(pid), start) if take else _peek_number(ctx, _ms_scope(pid), start)


def next_task_no(ctx: Ctx, pid: Any, ms_id: Any, take: bool) -> Any:
    """Next task number in a milestone (2.1, 2.2 …); numbers are never reused."""
    all_ = {m['id']: m for m in rows(ctx, 'milestones')}
    if js_truthy(ms_id):
        mm = all_.get(ms_id)
        pno = coalesce(prop(mm, 'no'), 0)
    else:
        pno = 0
    prefix = f'{js_str(pno)}.'
    used = []
    for t in rows(ctx, 'tasks'):
        if t['project_id'] == pid:
            c = _tasks.wbs_of(t, all_)
            if c.startswith(prefix):
                used.append(js_or(js_number(c[len(prefix) :]), 0))
    start = js_max(0, *used) + 1
    return _take_number(ctx, _task_scope(pid, ms_id), start) if take else _peek_number(ctx, _task_scope(pid, ms_id), start)


def _new_task_code(ctx: Ctx, pid: Any, m: dict | None) -> dict[str, Any]:
    no = next_task_no(ctx, pid, m['id'] if m is not None else None, True)
    return {'no': no, 'wbs_code': f'{js_str(m["no"]) if m is not None else "0"}.{js_str(no)}'}


# =====================================================================================================================
# WBS helpers
# =====================================================================================================================
def _ms_map(ctx: Ctx) -> dict[str, dict]:
    return {m['id']: m for m in rows(ctx, 'milestones') if not js_truthy(m.get('deleted_at'))}


def _task_ref(t: dict, ms: dict) -> dict[str, Any]:
    return _tasks.to_ref(t, ms)


def _build_task_detail(ctx: Ctx, t: dict) -> dict[str, Any]:
    """Local builder for the shared TaskDetail contract (module C owns GET /tasks/:id)."""
    p = find(ctx, 'projects', t['project_id'], 'Project')
    ms = _ms_map(ctx)
    all_ = rows(ctx, 'tasks')
    by_id = {x['id']: x for x in all_}
    deps = deps_of(ctx, t['id'])
    dependents = [x for x in (by_id.get(d['task_id']) for d in rows(ctx, 'task_dependencies') if d['depends_on_task_id'] == t['id']) if x is not None]
    a = task_actuals(ctx).get(t['id'])
    editor = can_edit(ctx, p)
    m = ms.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
    me = ctx.user['id']
    return {
        'task': t,
        'wbs': _tasks.wbs_of(t, ms),
        'project': {'id': p['id'], 'name': prop(p, 'name'), 'code': prop(p, 'code'), 'status': prop(p, 'status'), 'pm_id': prop(p, 'pm_id')},
        'milestone': {'id': m['id'], 'no': m['no'], 'name': m['name']} if m is not None else None,
        'dependencies': [_task_ref(d, ms) for d in deps],
        'dependents': [_task_ref(d, ms) for d in dependents],
        'blockers': [_task_ref(d, ms) for d in deps if d.get('status') != 'done'],
        'actual_hours': coalesce(a['hours'] if a else UNDEF, 0),
        'approved_hours': coalesce(a['approved'] if a else UNDEF, 0),
        'can_edit': editor,
        'can_change_status': editor or t.get('owner_id') == me or t.get('backup_id') == me,
        'can_comment': editor or can_view(ctx, p),
    }


def _auto_delete_on(ctx: Ctx, m: dict) -> Any:
    if not js_truthy(m.get('empty_since')) or m.get('billing_status') != 'none':
        return None
    return add_days(m['empty_since'], prop(ctx.tenant['settings'], 'projects.empty_milestone_days'))


def sync_achieved(ctx: Ctx, ms_id: Any) -> None:
    if not js_truthy(ms_id):
        return
    m = find_by('milestones', 'id', ms_id)
    if m is None or m.get('tenant_id') != ctx.tenant_id:
        return
    mine = [t for t in rows(ctx, 'tasks') if t.get('milestone_id') == ms_id]
    all_done = len(mine) > 0 and all(t.get('status') == 'done' for t in mine)
    if all_done and not js_truthy(m.get('achieved_on')):
        v = '0000-01-01'
        for t in mine:
            v = max_iso(v, coalesce(prop(t, 'done_on'), ctx.today))
        patch('milestones', m, {'achieved_on': v})
    elif not all_done and js_truthy(m.get('achieved_on')) and mine:
        patch('milestones', m, {'achieved_on': None})


def _detect_cycle(edges: dict, start: Any) -> bool:
    seen: set = set()
    stack = list(edges.get(start, []))
    while stack:
        x = stack.pop()
        if x == start:
            return True
        if _hashable(x) and x in seen:
            continue
        if _hashable(x):
            seen.add(x)
        stack.extend(edges.get(x, []) if _hashable(x) else [])
    return False


def _hashable(x: Any) -> bool:
    try:
        hash(x)
    except TypeError:
        return False
    return True


def _dep_edges(ctx: Ctx) -> dict:
    m: dict[Any, list] = {}
    for d in rows(ctx, 'task_dependencies'):
        m.setdefault(d['task_id'], []).append(d['depends_on_task_id'])
    return m


# =====================================================================================================================
# Views
# =====================================================================================================================
def _list_item(ctx: Ctx, p: dict) -> dict[str, Any]:
    m = redact_metrics(ctx, project_metrics(ctx, p), can_see_margins(ctx))
    return {**_strip_project(ctx, p), 'metrics': m, 'can_edit': can_edit(ctx, p), 'team_ids': m['team'], 'row': project_row_info(ctx, p)}


def _strip_project(ctx: Ctx, p: dict) -> dict:
    if has_any(ctx, 'finance.view', 'margins.view', 'projects.edit'):
        return p
    return {**p, 'contract_value_paise': 0, **({'costing': None} if p.get('costing', UNDEF) is not UNDEF else {})}


def _sched_task_index(ctx: Ctx, project_id: Any) -> dict[str, dict]:
    sc = schedule_of(ctx, project_id)['schedule']
    if sc is None:
        return {}
    memo = scope().memo
    k = ('routes/projects:sti', id(sc))
    hit = memo.get(k)
    if hit is not None and hit[0] is sc:
        return hit[1]
    idx: dict[str, dict] = {}
    for x in sc['tasks']:
        idx.setdefault(x['id'], x)
    memo[k] = (sc, idx)
    return idx


def _task_view(ctx: Ctx, t: dict, ms: dict) -> dict[str, Any]:
    a = task_actuals(ctx).get(t['id'])
    deps = deps_of(ctx, t['id'])
    blockers = [
        {'id': d['id'], 'wbs': _tasks.wbs_of(d, ms), 'title': prop(d, 'title'), 'owner_id': prop(d, 'owner_id')} for d in deps if d.get('status') != 'done'
    ]
    risk: Any = None
    st_ = t.get('status')
    if st_ != 'done' and st_ != 'in_review':
        if t['due_on'] < ctx.today:
            risk = 'overdue'
        elif st_ == 'blocked':
            risk = 'blocked'
        elif st_ == 'todo' and diff_days(t['due_on'], ctx.today) <= 3:
            risk = 'due_soon'
    tl = task_timeliness(ctx, t)
    st = _sched_task_index(ctx, t['project_id']).get(t['id'])
    revs = coalesce(prop(t, 'target_revisions'), [])
    return {
        **t,
        'stage': stage_of(t),
        'progress': progress_of(t),
        'timeliness': tl['timeliness'] if tl else None,
        'variance_days': coalesce(tl['variance'] if tl else UNDEF, None),
        'wbs': _tasks.wbs_of(t, ms),
        'actual_hours': coalesce(a['hours'] if a else UNDEF, 0),
        'approved_hours': coalesce(a['approved'] if a else UNDEF, 0),
        'dep_ids': [d['id'] for d in deps],
        'blockers': blockers,
        'risk': risk,
        'dep_wbs': js_sort([_tasks.wbs_of(d, ms) for d in deps], sort_wbs),
        'forecast_start': coalesce(prop(st, 'forecast_start'), None),
        'forecast_end': coalesce(prop(st, 'forecast_end'), None),
        'float_days': prop(st, 'float_days') if st is not None else None,
        'project_float_days': coalesce(prop(st, 'project_float_days'), None) if st is not None else None,
        'critical': js_truthy(prop(st, 'critical')),
        'revised_on': revs[-1]['at'][:10] if revs else None,
    }


def _milestone_views(ctx: Ctx, p: dict, tasks: list[dict]) -> list[dict]:
    act = task_actuals(ctx)
    invoices = rows(ctx, 'invoices')
    sched = schedule_of(ctx, p['id'])
    fin = has(ctx, 'finance.view')
    out = []
    for m in _live_milestones(ctx, p['id']):
        fc = coalesce(sched['msForecast'].get(m['id'], UNDEF), None)
        s = milestone_state(ctx, m, tasks, None if js_truthy(m.get('achieved_on')) else fc)
        mine = [t for t in tasks if t.get('milestone_id') == m['id']]
        inv = next((i for i in invoices if i.get('milestone_id') == m['id']), None)
        achieved = s['state'] in ('achieved_on_time', 'achieved_late')
        if prop(m, 'achieved_on') is not None and prop(m, 'achieved_on') is not UNDEF:
            end_on = m['achieved_on']
        elif achieved:
            end_on = '0000-01-01'
            for t in mine:
                end_on = max_iso(end_on, coalesce(prop(t, 'done_on'), t['due_on']))
        else:
            end_on = fc
        logged: Any = 0
        for t in mine:
            x = act.get(t['id'])
            logged = logged + coalesce(x['hours'] if x else UNDEF, 0)
        out.append(
            {
                **m,
                'state': s['state'],
                'slip': s['slip'],
                'progress': s['progress'],
                'task_count': s['taskCount'],
                'open_count': s['openCount'],
                'est_hours': s['est'],
                'earned_hours': js_round(s['earned'] * 10) / 10,
                'logged_hours': logged,
                'invoice_id': inv['id'] if inv is not None else None,
                'invoice': {
                    'id': inv['id'],
                    'number': inv['number'],
                    'amount_paise': inv['amount_paise'],
                    'outstanding_paise': inv['amount_paise'] - inv['paid_amount_paise'],
                }
                if fin and inv is not None
                else None,
                'auto_delete_on': _auto_delete_on(ctx, m),
                'forecast_end': fc,
                'end_kind': ('achieved' if achieved else 'forecast') if mine else None,
                'end_on': end_on if mine else None,
            }
        )
    return out


def _billing_summary(ctx: Ctx, p: dict, tasks: list[dict]) -> dict | None:
    if not has_any(ctx, 'finance.view', 'projects.edit', 'margins.view'):
        return None
    out = {'total': 0, 'paid': 0, 'invoiced_unpaid': 0, 'achieved_not_invoiced': 0, 'not_due': 0}
    for m in _live_milestones(ctx, p['id']):
        amt = m['billing_amount_paise']
        out['total'] += amt
        if m.get('billing_status') == 'paid':
            out['paid'] += amt
        elif m.get('billing_status') == 'invoiced':
            out['invoiced_unpaid'] += amt
        else:
            st = milestone_state(ctx, m, tasks)['state']
            if st in ('achieved_on_time', 'achieved_late'):
                out['achieved_not_invoiced'] += amt
            else:
                out['not_due'] += amt
    return out


def _alloc_view(ctx: Ctx, a: dict, ms: dict) -> dict[str, Any]:
    mid = a.get('milestone_id')
    m = ms.get(mid) if js_truthy(mid) else None
    return {
        **a,
        'ms_code': (f'M{js_str(m["no"])}' if m is not None else '—') if js_truthy(mid) else 'All',
        'ms_name': coalesce(prop(m, 'name'), 'Removed milestone') if js_truthy(mid) else 'All milestones',
    }


# =====================================================================================================================
# Schedule
# =====================================================================================================================
def _compute_schedule(ctx: Ctx, p: dict) -> dict[str, Any]:
    ensure_swept(ctx)
    sc = schedule_of(ctx, p['id'])['schedule']
    return {**(sc if sc is not None else compute_schedule_core(ctx, p)), 'can_edit': can_edit(ctx, p)}


# =====================================================================================================================
# Allocations (BR-22, SVC-12)
# =====================================================================================================================
def _validate_allocation(ctx: Ctx, p: dict, b: Any, self_id: Any) -> dict[str, Any]:
    user_id = _g(b, 'user_id') if isinstance(_g(b, 'user_id'), str) else ''
    milestone_id = _g(b, 'milestone_id') if isinstance(_g(b, 'milestone_id'), str) else ''
    b_pct = _g(b, 'pct')
    pct = js_number(b_pct)
    from_on = _g(b, 'from_on') if isinstance(_g(b, 'from_on'), str) else ''
    to_on = _g(b, 'to_on') if isinstance(_g(b, 'to_on'), str) else ''
    if not user_id:
        err(422, 'ALLOC_INVALID', 'Choose the person to allocate.', 'user_id')
    u = _active_user(ctx, user_id, 'user_id')
    assert u is not None
    un = js_str(u['name'])
    # Feature Guide G6.01: person, milestone OR "All milestones" (no milestone_id), % and both dates (v8: allocating
    # before the WBS exists is allowed — the allocation spans its dates across the whole project)
    if milestone_id:
        m = next((x for x in rows(ctx, 'milestones') if x['id'] == milestone_id and not js_truthy(x.get('deleted_at'))), None)
        if m is None or m['project_id'] != p['id']:
            err(422, 'ALLOC_INVALID', 'The milestone must belong to this project.', 'milestone_id')
    if not is_finite(pct) or pct < 1 or pct > 100 or b_pct is None or b_pct == '':
        err(422, 'ALLOC_INVALID', f'{un}: set an allocation between 1% and 100%.', 'pct')
    if not _iso_test(from_on):
        err(422, 'ALLOC_INVALID', f'{un}: the start date is required.', 'from_on')
    if not _iso_test(to_on):
        err(422, 'ALLOC_INVALID', f'{un}: the end date is required.', 'to_on')
    if from_on > to_on:
        err(422, 'ALLOC_INVALID', f'{un}: the start date is after the end date.', 'to_on')
    if from_on < p['start_on'] or to_on > p['end_on']:
        err(
            422,
            'ALLOC_INVALID',
            f'Allocation dates must sit inside the project ({fmt_date(p["start_on"])} – {fmt_date(p["end_on"])}).',
            'from_on' if from_on < p['start_on'] else 'to_on',
        )
    settings = ctx.tenant['settings']
    mx = settings['allocation.max_pct_per_person']
    self_ = next((a for a in rows(ctx, 'allocations') if a['id'] == self_id), None) if js_truthy(self_id) else None
    c = calendar_for(ctx, user_id)
    over: list[str] = []
    d = from_on
    while d <= to_on:
        if is_working_day(d, c):
            total = allocation_on(ctx, user_id, d) + pct
            if self_ is not None and self_['user_id'] == user_id and self_['from_on'] <= d and self_['to_on'] >= d:
                total -= self_['pct']
            if total > mx:
                over.append(d)
        d = add_days(d, 1)
    if over:
        rng = f' – {fmt_date(over[-1])}' if len(over) > 1 else ''
        msg = f'{un} would be over {js_str(mx)}% on {len(over)} working day{"" if len(over) == 1 else "s"} ({fmt_date(over[0])}{rng}).'
        if js_truthy(settings.get('allocation.block_over', UNDEF)):
            err(409, 'ALLOC_OVER', msg + ' Lower the percentage or shorten the dates.', 'pct')
        warn(ctx, 'ALLOC_OVER', msg)
    return {'user_id': user_id, 'milestone_id': js_or(milestone_id, None), 'pct': js_round(pct), 'from_on': from_on, 'to_on': to_on, 'project_id': p['id']}


def _project_load(ctx: Ctx, p: dict) -> dict[str, Any]:
    settings = ctx.tenant['settings']
    mx = settings['allocation.max_pct_per_person']
    allocs = [a for a in rows(ctx, 'allocations') if a['project_id'] == p['id']]
    people = list(dict.fromkeys([*(a['user_id'] for a in allocs), *project_metrics(ctx, p)['team']]))
    c = cal(ctx).without_leave()
    nxt: list[str] = []
    d = ctx.today
    while len(nxt) < 10:
        if is_working_day(d, c):
            nxt.append(d)
        d = add_days(d, 1)
    out = []
    for uid2 in people:
        days = []
        for dd in nxt:
            pp: Any = 0
            for a in allocs:
                if a['user_id'] == uid2 and a['from_on'] <= dd and a['to_on'] >= dd:
                    pp = pp + a['pct']
            days.append({'date': dd, 'total_pct': allocation_on(ctx, uid2, dd), 'project_pct': pp})
        mine = [a for a in allocs if a['user_id'] == uid2]
        over: list[dict] = []
        pc = calendar_for(ctx, uid2)
        seen: set[str] = set()
        for a in mine:
            dd = max_iso(a['from_on'], ctx.today)
            while dd <= a['to_on']:
                if dd not in seen and is_working_day(dd, pc):
                    seen.add(dd)
                    tot = allocation_on(ctx, uid2, dd)
                    if tot > mx:
                        over.append({'date': dd, 'pct': tot})
                dd = add_days(dd, 1)
        over = sorted(over, key=lambda o: locale_key(o['date']))
        tp: Any = 0
        for x in days:
            tp = tp + x['total_pct']
        out.append({'user_id': uid2, 'days': days, 'over_days': over, 'avg_pct': tp / len(days)})
    out = sorted(out, key=lambda o: locale_key(user_name(ctx, o['user_id'])))
    return {'max_pct': mx, 'block_over': settings.get('allocation.block_over', UNDEF), 'people': out}


# =====================================================================================================================
# Money (BR-23..27)
# =====================================================================================================================
def _suggest_invoice_number(ctx: Ctx) -> dict[str, Any]:
    from app.routes.invoices import next_invoice_number

    return {'number': next_invoice_number(ctx, ctx.today)}


def _invoice_view(ctx: Ctx, i: dict) -> dict[str, Any]:
    fid = i.get('file_id')
    f = next((x for x in rows(ctx, 'files') if x['id'] == fid), None) if js_truthy(fid) else None
    return {
        **i,
        'payments': sorted((p for p in rows(ctx, 'payments') if p.get('invoice_id') == i['id']), key=lambda p: locale_key(p['paid_on'])),
        'file': {'id': f['id'], 'name': prop(f, 'name'), 'data_url': prop(f, 'data_url')} if f is not None else None,
        'outstanding': i['amount_paise'] - i['paid_amount_paise'],
    }


def _project_money(ctx: Ctx, p: dict) -> dict[str, Any]:
    m = project_metrics(ctx, p)
    tasks = [t for t in rows(ctx, 'tasks') if t['project_id'] == p['id']]
    task_ids = {t['id'] for t in tasks}
    ms = _live_milestones(ctx, p['id'])
    entries = [e for e in costed_entries(ctx) if e.get('kind') == 'task' and js_truthy(e.get('task_id')) and e['task_id'] in task_ids]
    people: dict[Any, dict] = {}

    def touch(id_: Any) -> dict:
        r = people.get(id_)
        if r is None:
            r = {'user_id': id_, 'est_hours': 0, 'hours': 0, 'approved_hours': 0, 'rate_ph': js_round(current_rate_ph(ctx, id_)), 'planned_cost': 0, 'cost': 0}
            people[id_] = r
        return r

    for t in tasks:
        if js_truthy(t.get('owner_id')):
            r = touch(t['owner_id'])
            r['est_hours'] += t['est_hours']
            r['planned_cost'] += js_round(t['est_hours'] * current_rate_ph(ctx, t['owner_id']))
    for e in entries:
        r = touch(e['user_id'])
        r['hours'] += e['hours']
        if e['approved']:
            r['approved_hours'] += e['hours']
        r['cost'] += js_round(e['cost'])
    by_task: dict[Any, dict] = {}
    for e in entries:
        x = by_task.get(e['task_id'])
        if x is None:
            x = {'h': 0, 'c': 0}
        x['h'] += e['hours']
        x['c'] += e['cost']
        by_task[e['task_id']] = x
    ms_ids = {x['id'] for x in ms}
    groups = [*({'id': x['id'], 'code': f'M{js_str(x["no"])}', 'name': x['name']} for x in ms), {'id': None, 'code': '—', 'name': 'No milestone'}]
    by_milestone = []
    for g in groups:
        if g['id'] is None:
            ts = [t for t in tasks if prop(t, 'milestone_id') is None or (js_truthy(t.get('milestone_id')) and t['milestone_id'] not in ms_ids)]
        else:
            ts = [t for t in tasks if t.get('milestone_id') == g['id']]
        est: Any = 0
        logged: Any = 0
        pc: Any = 0
        lc: Any = 0
        for t in ts:
            est = est + t['est_hours']
        for t in ts:
            bt = by_task.get(t['id'])
            logged = logged + coalesce(bt['h'] if bt else UNDEF, 0)
        for t in ts:
            pc = pc + t['est_hours'] * current_rate_ph(ctx, t.get('owner_id'))
        for t in ts:
            bt = by_task.get(t['id'])
            lc = lc + coalesce(bt['c'] if bt else UNDEF, 0)
        row = {**g, 'est_hours': est, 'logged_hours': logged, 'variance_hours': logged - est, 'planned_cost': js_round(pc), 'logged_cost': js_round(lc)}
        if row['id'] is not None or est > 0 or logged > 0:
            by_milestone.append(row)
    invoices = [i for i in rows(ctx, 'invoices') if i.get('project_id') == p['id']]
    admin = has(ctx, 'finance.view')
    billing = None
    if admin:
        billing = []
        for x in ms:
            if x['billing_amount_paise'] > 0 or x.get('billing_status') != 'none':
                inv = next((i for i in invoices if i.get('milestone_id') == x['id']), None)
                billing.append(
                    {
                        'milestone_id': x['id'],
                        'no': x['no'],
                        'name': x['name'],
                        'amount': x['billing_amount_paise'],
                        'billing_status': x['billing_status'],
                        'state': milestone_state(ctx, x, tasks)['state'],
                        'planned_end': x['planned_end'],
                        'invoice': _invoice_view(ctx, inv) if inv is not None else None,
                    }
                )

    def trip(rev: Any, cost: Any) -> dict[str, Any]:
        return {'revenue': rev, 'cost': cost, 'margin': margin_pct(rev, cost), 'markup': markup_pct(rev, cost)}

    return {
        'floor_pct': prop(ctx.tenant['settings'], 'projects.margin_floor_pct'),
        'contract': p['contract_value_paise'],
        'planned': trip(p['contract_value_paise'], m['planned_cost']),
        'projected': trip(p['contract_value_paise'], m['projected_cost']),
        'actual': trip(m['invoiced'], m['actual_cost']),
        'invoiced': m['invoiced'] if admin else None,
        'collected': m['collected'] if admin else None,
        'by_person': sorted(people.values(), key=lambda r: -(r['cost'] + r['planned_cost'])),
        'by_milestone': by_milestone,
        'billing': billing,
        'can_invoice': admin and has(ctx, 'finance.edit'),
    }


# =====================================================================================================================
# Import (UI-11)
# =====================================================================================================================
def _parse_csv(text: str) -> list[list[str]]:
    """parseCSV of src/lib/utils.ts"""
    out: list[list[str]] = []
    row: list[str] = []
    cell = ''
    q = False
    sep = '\t' if ('\t' in text and ',' not in text) else ','
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if q:
            if ch == '"' and i + 1 < n and text[i + 1] == '"':
                cell += '"'
                i += 1
            elif ch == '"':
                q = False
            else:
                cell += ch
        elif ch == '"':
            q = True
        elif ch == sep:
            row.append(cell)
            cell = ''
        elif ch in ('\n', '\r'):
            if ch == '\r' and i + 1 < n and text[i + 1] == '\n':
                i += 1
            row.append(cell)
            out.append(row)
            row = []
            cell = ''
        else:
            cell += ch
        i += 1
    if cell or row:
        row.append(cell)
        out.append(row)
    return [r for r in out if any(js_trim(c) != '' for c in r)]


def _import_signature(ctx: Ctx, pid: Any) -> str:
    ts = [t for t in rows(ctx, 'tasks') if t['project_id'] == pid]
    ms = _all_milestones(ctx, pid)
    v: Any = 0
    for t in ts:
        v = v + t['version']
    return f'{len(ts)}:{len(ms)}:{sum(1 for m in ms if not js_truthy(m.get("deleted_at")))}:{js_str(v)}'


_MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
_D1 = re.compile(r'^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})\Z')
_D2 = re.compile(r'^([0-9]{1,2})[/.\-]([0-9]{1,2})[/.\-]([0-9]{4})\Z')
_D3 = re.compile(r'^([0-9]{1,2})[\s\-]([A-Za-z]{3})[A-Za-z]*[\s\-,]+([0-9]{4})\Z')


def _parse_date(s: str) -> str | None:
    v = js_trim(s)
    if not v:
        return None
    m = _D1.match(v)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = _D2.match(v)
        if m:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        else:
            m = _D3.match(v)
            if m:
                lw = m[2].lower()
                d, mo, y = int(m[1]), (_MONTHS.index(lw) + 1 if lw in _MONTHS else 0), int(m[3])
            else:
                return None
    if mo < 1 or mo > 12 or d < 1 or d > 31:
        return None
    iso = f'{y}-{mo:02d}-{d:02d}'
    y_eff = 1900 + y if 0 <= y <= 99 else y  # new Date(y, m, d) maps years 0..99 to 1900..1999
    return iso if d <= _calendar.monthrange(y_eff, mo)[1] else None


_BILL_STRIP = re.compile(r'[₹,\s]')
_CX_SEP = re.compile(r'[\s-]+')
_DEP_SPLIT = re.compile(r'[;,|\s]+')
_WBS_RE = re.compile(r'^[0-9]+\.[0-9]+\Z')


def _import_preview(ctx: Ctx, p: dict, text: str) -> dict[str, Any]:
    max_rows = 1000
    table = _parse_csv(re.sub(r'^﻿', '', js_str(coalesce(text, ''))))
    if len(table) < 2:
        err(422, 'WBS_INVALID', 'Paste a header row and at least one task row. Use the template.')
    head = [js_trim(h).lower() for h in table[0]]

    def find_idx(pred: Any) -> int:
        return next((i for i, h in enumerate(head) if pred(h)), -1)

    def col(prefix: str) -> int:
        return find_idx(lambda h: h.startswith(prefix))

    ix = {
        'ms': find_idx(lambda h: h.startswith('milestone') and 'billing' not in h),
        'task': col('task'),
        'owner': col('owner'),
        'est': col('estimated'),
        'start': col('start'),
        'end': col('end'),
        'cx': col('complexity'),
        'dep': col('depends'),
        'bill': find_idx(lambda h: 'billing' in h),
    }
    missing = [
        n
        for n, i in [
            ('Milestone', ix['ms']),
            ('Task', ix['task']),
            ('Owner', ix['owner']),
            ('Estimated hours', ix['est']),
            ('Start', ix['start']),
            ('End', ix['end']),
        ]
        if i < 0
    ]
    if missing:
        err(422, 'WBS_INVALID', f'The header is missing {", ".join(missing)}. Download the template and keep its first row.')
    body = table[1:]
    if len(body) > max_rows:
        err(422, 'WBS_INVALID', f'The file has {len(body)} rows. Import at most {max_rows} rows at a time.')
    users = [u for u in rows(ctx, 'users') if u.get('status') == 'active']
    ms = _live_milestones(ctx, p['id'])
    ms_by_name = {js_trim(m['name']).lower(): m for m in ms}
    new_ms: dict[str, dict] = {}
    next_ms = [_next_ms_no(ctx, p['id'], False)]
    counters: dict[str, Any] = {}
    known: set[str] = set()
    ms_map_all = _ms_map(ctx)
    for t in rows(ctx, 'tasks'):
        if t['project_id'] == p['id']:
            known.add(_tasks.wbs_of(t, ms_map_all))

    def cell(r: list[str], i: int) -> str:
        return js_trim(r[i] if i < len(r) else '') if i >= 0 else ''

    out: list[dict] = []
    for k, r in enumerate(body):
        errors: list[str] = []
        ms_name = cell(r, ix['ms'])
        ms_code = ''
        is_new = False
        wbs = ''
        billing_paise: Any = None
        if not ms_name:
            errors.append('Milestone is empty.')
        else:
            ex = ms_by_name.get(ms_name.lower())
            if ex is not None:
                ms_code = f'M{js_str(ex["no"])}'
                ms_key = ex['id']
                n = counters[ms_key] if ms_key in counters else next_task_no(ctx, p['id'], ex['id'], False)
                counters[ms_key] = n + 1
                wbs = f'{js_str(ex["no"])}.{js_str(n)}'
            else:
                key = ms_name.lower()
                if key not in new_ms:
                    new_ms[key] = {'code': f'M{js_str(next_ms[0])}', 'name': ms_name, 'no': next_ms[0], 'billing_paise': 0}
                    next_ms[0] = next_ms[0] + 1
                nm = new_ms[key]
                is_new = True
                ms_code = nm['code']
                ms_key = 'new:' + key
                n = counters[ms_key] if ms_key in counters else 1
                counters[ms_key] = n + 1
                wbs = f'{js_str(nm["no"])}.{js_str(n)}'
                bill = _BILL_STRIP.sub('', cell(r, ix['bill']))
                if bill:
                    v = js_number(bill)
                    if not is_finite(v) or v < 0:
                        errors.append(f'Milestone billing “{cell(r, ix["bill"])}” is not a number.')
                    else:
                        billing_paise = js_round(v * 100)
                        if not js_truthy(nm['billing_paise']):
                            nm['billing_paise'] = billing_paise
        title = cell(r, ix['task'])
        if not title:
            errors.append('Task is empty.')
        elif js_len(title) > 200:
            errors.append('Task title is longer than 200 characters.')
        owner_label = cell(r, ix['owner'])
        owner_id: Any = None
        if not owner_label:
            errors.append('Owner is empty.')
        else:
            ol = owner_label.lower()
            o = next((u for u in users if js_str(u.get('email', UNDEF)).lower() == ol or js_str(u.get('name', UNDEF)).lower() == ol), None)
            if o is None:
                errors.append(f'Unknown owner “{owner_label}”. Use a team member’s full name or email.')
            else:
                owner_id = o['id']
        est_raw = cell(r, ix['est'])
        est = js_number(est_raw)
        if not est_raw or not is_finite(est) or est <= 0 or est > 999:
            errors.append(f'Estimated hours “{est_raw}” must be a number between 0.5 and 999.')
        s = _parse_date(cell(r, ix['start']))
        e = _parse_date(cell(r, ix['end']))
        if not s:
            errors.append(f'Start “{cell(r, ix["start"])}” is not a date. Use YYYY-MM-DD.')
        if not e:
            errors.append(f'End “{cell(r, ix["end"])}” is not a date. Use YYYY-MM-DD.')
        if s and e and s > e:
            errors.append('End is before Start.')
        cx_raw = _CX_SEP.sub('_', js_trim(cell(r, ix['cx']).lower()))
        if cx_raw:
            cx: Any = next((c for c in CX if c == cx_raw or (c == 'medium' and cx_raw == 'med')), None)
        else:
            cx = 'medium'
        if not cx:
            errors.append(f'Complexity “{cell(r, ix["cx"])}” must be Low, Medium, High or Very high.')
        dep_raw = cell(r, ix['dep'])
        depends_on = [x for x in (js_trim(x) for x in _DEP_SPLIT.split(dep_raw)) if x] if dep_raw else []
        if wbs:
            known.add(wbs)
        out.append(
            {
                'line': k + 2,
                'milestone': ms_name,
                'milestone_code': ms_code,
                'milestone_new': is_new,
                'wbs': wbs,
                'title': title,
                'owner_id': owner_id,
                'owner_label': owner_label,
                'est_hours': est if is_finite(est) and est > 0 else None,
                'start_on': s,
                'due_on': e,
                'complexity': cx,
                'depends_on': depends_on,
                'billing_paise': billing_paise,
                'errors': errors,
            }
        )
    for r in out:
        for d in r['depends_on']:
            if not _WBS_RE.match(d):
                r['errors'].append(f'Depends on “{d}” should be a WBS code like 1.2.')
            elif d not in known:
                r['errors'].append(f'Depends on {d}, which doesn’t exist in the project or this file.')
            elif d == r['wbs']:
                r['errors'].append('A task can’t depend on itself.')
    file_edges = {r['wbs']: r['depends_on'] for r in out}
    for r in out:
        seen: set[str] = set()
        stack = list(r['depends_on'])
        while stack:
            x = stack.pop()
            if x == r['wbs']:
                r['errors'].append('Circular dependency. Remove one of the links.')
                break
            if x in seen:
                continue
            seen.add(x)
            stack.extend(file_edges.get(x, []))
    token = uid('wbs_')
    now = now_iso()
    remove_where(ctx, 'pending_tokens', lambda t: t.get('kind') == 'wbs_import' and t['expires_at'] <= now)
    insert(
        ctx,
        'pending_tokens',
        {
            'id': token,
            'kind': 'wbs_import',
            'user_id': ctx.user['id'],
            'ref_id': p['id'],
            'payload': {'rows': out, 'signature': _import_signature(ctx, p['id'])},
            'expires_at': iso_from_ms(now_ms() + 15 * 60_000),
        },
    )
    return {
        'token': token,
        'rows': out,
        'error_count': sum(1 for r in out if r['errors']),
        'task_count': len(out),
        'new_milestones': [{'code': m['code'], 'name': m['name'], 'billing_paise': m['billing_paise']} for m in new_ms.values()],
        'max_rows': max_rows,
    }


def _import_commit(ctx: Ctx, p: dict, token: str) -> dict[str, Any]:
    pending = next((x for x in rows(ctx, 'pending_tokens') if x['id'] == token and x.get('kind') == 'wbs_import'), None)
    me = ctx.user['id']
    if pending is None or pending.get('ref_id') != p['id'] or pending.get('user_id') != me or pending['expires_at'] <= now_iso():
        err(409, 'WBS_INVALID', 'This preview has expired. Preview the file again, then import.')
    t = pending['payload']
    if any(len(r['errors']) for r in t['rows']):
        err(422, 'WBS_INVALID', 'Fix the rows with errors first. Nothing was imported.')
    if _import_signature(ctx, p['id']) != t['signature']:
        err(409, 'STALE', 'The project’s WBS changed since you previewed. Preview again before importing.')
    ms_by_name = {js_trim(m['name']).lower(): m for m in _live_milestones(ctx, p['id'])}
    created: list[dict] = []
    wbs_to_id: dict[str, Any] = {}
    ms_all = _ms_map(ctx)
    for x in rows(ctx, 'tasks'):
        if x['project_id'] == p['id']:
            wbs_to_id[_tasks.wbs_of(x, ms_all)] = x['id']
    new_tasks: list[tuple[dict, dict]] = []
    sorted_by_ms: dict[str, list[dict]] = {}
    for r in t['rows']:
        sorted_by_ms.setdefault(r['milestone'].lower(), []).append(r)
    for key, lst in sorted_by_ms.items():
        m = ms_by_name.get(key)
        was_empty = m is None or not any(x.get('milestone_id') == m['id'] for x in rows(ctx, 'tasks'))
        if m is None:
            starts = sorted(r['start_on'] for r in lst)
            ends = sorted(r['due_on'] for r in lst)
            billing = coalesce(next((b for b in (r['billing_paise'] for r in lst) if js_truthy(b)), UNDEF), 0)
            no = _next_ms_no(ctx, p['id'], True)
            m = insert(
                ctx,
                'milestones',
                {
                    'project_id': p['id'],
                    'no': no,
                    'name': lst[0]['milestone'],
                    'planned_start': starts[0],
                    'planned_end': ends[-1],
                    'baseline_end': ends[-1],
                    'billing_amount_paise': billing,
                    'billing_status': 'none',
                    'achieved_on': None,
                    'empty_since': None,
                    'deleted_at': None,
                },
            )
            ms_by_name[key] = m
            created.append(m)
        for r in lst:
            code = _new_task_code(ctx, p['id'], m)
            task = insert(
                ctx,
                'tasks',
                {
                    'project_id': p['id'],
                    'milestone_id': m['id'],
                    'no': code['no'],
                    'wbs_code': code['wbs_code'],
                    'title': r['title'],
                    'owner_id': r['owner_id'],
                    'backup_id': None,
                    'est_hours': r['est_hours'],
                    'start_on': r['start_on'],
                    'due_on': r['due_on'],
                    'done_on': None,
                    'complexity': coalesce(r['complexity'], 'medium'),
                    'status': 'todo',
                    'description': '',
                    'blocked_reason': None,
                    'kind': 'project',
                    'assigned_by': me,
                    'review_round': 0,
                    'priority': 'normal',
                    'stage': 'new',
                    'progress': 0,
                    'tag': None,
                    'actual_start_on': None,
                    'baseline_due_on': r['due_on'],
                    'target_revisions': [],
                    'slip_flagged_on': None,
                    'slip_note': None,
                },
            )
            wbs_to_id[code['wbs_code']] = task['id']
            wbs_to_id[r['wbs']] = task['id']
            new_tasks.append((r, task))
        m = patch('milestones', m, {'empty_since': None})
        sync_milestone_dates(ctx, m['id'], was_empty)
    dep_count = 0
    for row, task in new_tasks:
        for d in row['depends_on']:
            target = wbs_to_id.get(d)
            if js_truthy(target) and target != task['id']:
                insert(ctx, 'task_dependencies', {'task_id': task['id'], 'depends_on_task_id': target})
                dep_count += 1
    remove_where(ctx, 'pending_tokens', lambda x: x['id'] == token)
    by_owner: dict[Any, int] = {}
    for _, task in new_tasks:
        if js_truthy(task.get('owner_id')) and task['owner_id'] != me:
            by_owner[task['owner_id']] = by_owner.get(task['owner_id'], 0) + 1
    name = js_str(ctx.user.get('name', UNDEF))
    for o, n in by_owner.items():
        notify(
            ctx,
            o,
            'NTF-14',
            f'{n} task{"" if n == 1 else "s"} assigned to you',
            f'{name} imported the WBS for {js_str(p["name"])}.',
            f'/projects/{p["id"]}/wbs',
        )
    extra = f' and {len(created)} new milestone{"" if len(created) == 1 else "s"}' if created else ''
    log_activity(ctx, 'project', p['id'], 'wbs.imported', f'Imported {len(new_tasks)} tasks{extra}')
    return {'tasks': len(new_tasks), 'milestones': len(created), 'dependencies': dep_count}


# =====================================================================================================================
# Client view (prototype §16)
# =====================================================================================================================
_CLIENT_MAP = {
    'achieved_on_time': ('Achieved', 'success'),
    'achieved_late': ('Achieved', 'success'),
    'overdue': ('Delayed', 'danger'),
    'at_risk': ('At risk', 'warning'),
    'on_track': ('On track', 'info'),
    'not_started': ('Planned', 'neutral'),
}


def _client_view(ctx: Ctx, p: dict) -> dict[str, Any]:
    sch = _compute_schedule(ctx, p)
    ms = _ms_map(ctx)
    tasks = [t for t in rows(ctx, 'tasks') if t['project_id'] == p['id']]
    m = project_metrics(ctx, p)
    c = cal(ctx).without_leave()
    d = ctx.today
    n = 10
    while n > 0:
        d = add_days(d, -1)
        if is_working_day(d, c):
            n -= 1
    two_weeks_ago = d
    ahead = add_working_days(ctx.today, 10, c)
    ff = sch.get('forecast_finish')
    late = working_days_between(add_days(p['end_on'], 1), ff, c) if js_truthy(ff) and ff > p['end_on'] else 0
    milestones = []
    for x in sch['milestones']:
        st = x['state']
        done = st in ('achieved_on_time', 'achieved_late')
        fe = x.get('forecast_end')
        note = f'Now expected {fmt_date(fe, "long")}' if st in ('overdue', 'at_risk') and js_truthy(fe) and fe > x['planned_end'] else None
        s = milestone_state(ctx, find(ctx, 'milestones', x['id']), tasks)
        milestones.append(
            {
                'no': x['no'],
                'name': x['name'],
                'target': coalesce(prop(x, 'achieved_on'), prop(x, 'forecast_end'), x['planned_end']) if done else x['planned_end'],
                'status': _CLIENT_MAP[st][0],
                'tone': _CLIENT_MAP[st][1],
                'note': note,
                'progress': s['progress'],
            }
        )
    completed = [
        {'wbs': _tasks.wbs_of(t, ms), 'title': prop(t, 'title'), 'done_on': t['done_on']}
        for t in sorted(
            (t for t in tasks if t.get('status') == 'done' and js_truthy(t.get('done_on')) and t['done_on'] >= two_weeks_ago),
            key=lambda t: locale_key(coalesce(prop(t, 'done_on'), '')),
            reverse=True,
        )
    ]
    upcoming = [
        {'wbs': t['wbs'], 'title': prop(t, 'title'), 'due_on': t['forecast_end']}
        for t in sorted((t for t in sch['tasks'] if t.get('status') != 'done' and t['forecast_end'] <= ahead), key=lambda t: locale_key(t['forecast_end']))
    ]
    inputs: list[str] = []
    for t in tasks:
        if t.get('status') == 'in_review':
            inputs.append(f'{_tasks.wbs_of(t, ms)} {js_str(prop(t, "title"))}: ready for your review')
    for cr in rows(ctx, 'change_requests'):
        if cr['project_id'] == p['id'] and cr.get('status') in ('raised', 'estimated'):
            hrs = f' ({js_str(cr["hours"])}h of work)' if js_truthy(cr.get('hours')) else ''
            inputs.append(f'Change request “{js_str(cr["title"])}”{hrs}: awaiting your approval')
    return {
        'company': prop(ctx.tenant, 'name'),
        'generated_on': ctx.today,
        'project': {
            'code': prop(p, 'code'),
            'name': prop(p, 'name'),
            'client': prop(p, 'client'),
            'start_on': prop(p, 'start_on'),
            'end_on': prop(p, 'end_on'),
            'status': prop(p, 'status'),
            'description': prop(p, 'description'),
        },
        'progress': m['progress'],
        'forecast_end': sch.get('forecast_finish', UNDEF),
        'late_days': late,
        'milestones': milestones,
        'completed': completed,
        'upcoming': upcoming,
        'inputs': inputs,
    }


# =====================================================================================================================
# Routes
# =====================================================================================================================
_HUES = [152, 210, 32, 260, 140, 350, 20, 190, 300, 90]


def list_projects(req: Req) -> Any:
    ctx = req.ctx
    visible = [p for p in rows(ctx, 'projects') if can_view(ctx, p)]
    q = js_trim(js_str(coalesce(req.q('q'), ''))).lower()
    typ = js_str(coalesce(req.q('type'), ''))
    base = [
        p
        for p in visible
        if (not q or q in f'{js_str(prop(p, "name"))} {js_str(prop(p, "client"))} {js_str(prop(p, "code"))}'.lower()) and (not typ or p.get('type') == typ)
    ]
    counts: dict[str, int] = {'all': len(base)}
    for s in STATUSES:
        counts[s] = sum(1 for p in base if p.get('status') == s)
    status = js_str(coalesce(req.q('status'), 'all'))
    items = [_list_item(ctx, p) for p in base if status == 'all' or p.get('status') == status]
    sort = js_str(coalesce(req.q('sort'), 'priority'))
    health_rank = {'off_track': 0, 'at_risk': 1, 'on_track': 2}
    if sort == 'name':
        items = sorted(items, key=lambda x: locale_key(x['name']))
    elif sort == 'end':
        items = sorted(items, key=lambda x: locale_key(x['end_on']))
    elif sort == 'health':
        items = sorted(items, key=lambda x: (health_rank[x['metrics']['health']], x['priority']))
    elif sort == 'progress':
        items = sorted(items, key=lambda x: -x['metrics']['progress'])
    else:
        items = sorted(items, key=lambda x: (1 if x['status'] in ('completed', 'closed') else 0, x['priority'], locale_key(x['end_on'])))
    return {'items': items, 'counts': counts, 'total': len(items)}


def create_project(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'projects.create'):
        err(403, 'FORBIDDEN', 'Only admins and project managers can create projects.')
    name = _need(req.b('name'), 'name', 'Project name')
    client = _need(req.b('client'), 'client', 'Client')
    if not _in(req.b('type'), TYPES):
        err(422, 'VALIDATION', 'Choose a project type: Product, Service, SaaS or AMS.', 'type')
    if not _in(req.b('billing_model'), MODELS):
        err(422, 'VALIDATION', 'Choose a commercial model.', 'billing_model')
    if not _in(req.b('stream'), STREAMS):
        err(422, 'VALIDATION', 'Choose a revenue stream.', 'stream')
    start_on = _need_date(req.b('start_on'), 'start_on', 'Start date')
    end_on = _need_date(req.b('end_on'), 'end_on', 'Planned end')
    if end_on < start_on:
        err(422, 'DATE_RANGE', 'The planned end is before the start date.', 'end_on')
    contract = _money(req.b('contract_value_paise'), 'contract_value_paise', 'Contract value')
    me = ctx.user['id']
    pm_id = js_or(req.b('pm_id'), me if has(ctx, 'projects.edit') else None)
    if not js_truthy(pm_id):
        err(422, 'VALIDATION', 'Choose the project manager.', 'pm_id')
    _active_user(ctx, pm_id, 'pm_id', True)
    if not has(ctx, 'projects.manage_all') and pm_id != me:
        err(403, 'FORBIDDEN', 'Project managers create projects they manage. Ask an admin to assign someone else.')
    pr = js_number(req.b('priority'))
    priority = _intify(pr) if pr in (1, 2, 3, 4, 5) else 3
    b_ms = req.b('milestones')
    ms_in = [m for m in b_ms if js_trim(js_str(coalesce(_g(m, 'name'), '')))] if isinstance(b_ms, list) else []
    seen_ms: dict[str, int] = {}
    for i, m in enumerate(ms_in):
        nm = js_trim(js_str(_g(m, 'name')))
        k = nm.lower()
        if k in seen_ms:
            err(422, 'VALIDATION', f'Milestone {i + 1}: “{nm}” is already M{seen_ms[k] + 1}. Give each milestone its own name.', f'milestones.{i}.name')
        seen_ms[k] = i
        _money(_g(m, 'billing_amount_paise'), f'milestones.{i}.billing_amount_paise', f'Milestone {i + 1} billing')
    explicit = len(ms_in) > 0 and all(_iso_test(coalesce(_g(m, 'planned_end'), '')) for m in ms_in)

    def bill_of(m: Any) -> Any:
        return js_or(js_number(_g(m, 'billing_amount_paise')), 0)

    planned = spread_milestones(ctx, start_on, end_on, [{'name': js_trim(js_str(_g(m, 'name'))), 'billing_amount_paise': bill_of(m)} for m in ms_in])
    if explicit:
        prev = start_on
        planned = []
        for i, m in enumerate(ms_in):
            end = m['planned_end']
            if end < prev or end > end_on:
                err(
                    422,
                    'DATE_RANGE',
                    f'Milestone {i + 1}: the end date must fall between {fmt_date(prev)} and the project end {fmt_date(end_on)}, in delivery order.',
                    f'milestones.{i}.planned_end',
                )
            start = start_on if i == 0 else min_iso(add_days(prev, 1), end)
            prev = end
            planned.append(
                {
                    'index': i,
                    'id': None,
                    'code': f'M{i + 1}',
                    'name': js_trim(js_str(_g(m, 'name'))),
                    'billing_amount_paise': bill_of(m),
                    'planned_start': start,
                    'planned_end': end,
                }
            )
    ms_bill: Any = 0
    for m in ms_in:
        ms_bill = ms_bill + bill_of(m)
    if contract > 0 and ms_bill > contract:
        warn(ctx, 'MS_BILLING_OVER', f'Milestone billing ({fmt_money(ms_bill)}) is more than the contract value ({fmt_money(contract)}).')
    if contract > 0 and ms_bill > 0 and ms_bill < contract:
        warn(ctx, 'MS_BILLING_UNDER', f'Milestone billing ({fmt_money(ms_bill)}) covers less than the contract value ({fmt_money(contract)}).')
    cb0 = req.b('costing')
    cb = cb0 if js_truthy(cb0) and isinstance(cb0, (dict, list)) else None
    has_costing = cb is not None and (
        (isinstance(_g(cb, 'resources'), list) and len(_g(cb, 'resources')) > 0)
        or (isinstance(_g(cb, 'additional_costs'), list) and len(_g(cb, 'additional_costs')) > 0)
        or js_number(_g(cb, 'maintenance_paise')) > 0
    )
    if has_costing and not can_plan_cost(ctx):
        err(403, 'FORBIDDEN', 'Costing needs the margins or salaries permission. Create the project without it.')
    if has_costing and not has(ctx, 'allocation.edit'):
        err(403, 'FORBIDDEN', 'You can’t allocate people, so the resource plan can’t be saved.')
    vc = validate_costing(ctx, None, {'start': start_on, 'end': end_on}, planned, cb) if has_costing else None  # type: ignore[arg-type]
    docs = validate_create_documents(req.b('documents'))
    prefix = js_or(ctx.tenant['settings'].get('projects.code_prefix', UNDEF), 'PRJ')
    taken = {p['code'] for p in rows(ctx, 'projects')}
    while True:
        code = f'{js_str(prefix)}-{pad_start(js_str(next_number(ctx, "project")), 3, "0")}'
        if code not in taken:
            break
    hue = _HUES[len(rows(ctx, 'projects')) % 10]
    d_id = req.b('deal_id')
    p = insert(
        ctx,
        'projects',
        {
            'code': code,
            'name': name,
            'client': client,
            'type': req.b('type'),
            'status': 'initiated',
            'pm_id': pm_id,
            'start_on': start_on,
            'end_on': end_on,
            'contract_value_paise': contract,
            'billing_model': req.b('billing_model'),
            'stream': req.b('stream'),
            'description': js_trim(js_str(coalesce(req.b('description'), ''))),
            'priority': priority,
            'deal_id': d_id if isinstance(d_id, str) else None,
            'baseline_end_on': end_on,
            'color_hue': hue,
            'costing': {**vc['costing'], 'saved_at': now_iso(), 'saved_by': me} if vc is not None else None,
        },
    )
    insert(
        ctx,
        'project_status_history',
        {'project_id': p['id'], 'from_status': None, 'to_status': 'initiated', 'reason': None, 'changed_by': me, 'changed_at': now_iso()},
    )
    ms_ids = []
    for m in planned:
        no = _next_ms_no(ctx, p['id'], True)
        ms_ids.append(
            insert(
                ctx,
                'milestones',
                {
                    'project_id': p['id'],
                    'no': no,
                    'name': m['name'],
                    'planned_start': m['planned_start'],
                    'planned_end': m['planned_end'],
                    'baseline_end': m['planned_end'],
                    'billing_amount_paise': m['billing_amount_paise'],
                    'billing_status': 'none',
                    'achieved_on': None,
                    'empty_since': ctx.today,
                    'deleted_at': None,
                },
            )['id']
        )
    if vc is not None:
        insert_plan_allocations(ctx, p['id'], vc, ms_ids)
    insert_project_documents(ctx, p['id'], docs)
    if js_truthy(p.get('deal_id')):
        d = next((x for x in rows(ctx, 'deals') if x['id'] == p['deal_id']), None)
        if d is not None and not js_truthy(d.get('project_id')):
            patch('deals', d, {'project_id': p['id']})
    bits = [
        x
        for x in [
            f'{len(planned)} milestone{"" if len(planned) == 1 else "s"}' if planned else '',
            f'a costing plan ({len(vc["resolved"])} resource row{"" if len(vc["resolved"]) == 1 else "s"})' if vc is not None else '',
            f'{len(docs)} document{"" if len(docs) == 1 else "s"}' if docs else '',
        ]
        if x
    ]
    log_activity(ctx, 'project', p['id'], 'project.created', f'Created {code} {name}{" with " + ", ".join(bits) if bits else ""}')
    if pm_id != me:
        notify(
            ctx,
            pm_id,
            'NTF-14',
            f'You manage {code}',
            f'{js_str(ctx.user.get("name", UNDEF))} created {name} and made you the project manager.',
            f'/projects/{p["id"]}',
        )
    return p


def get_project_detail(req: Req) -> Any:
    ctx = req.ctx
    p = _get_project(ctx, req.params['id'])
    sweep_milestones_core(ctx, p)
    ensure_swept(ctx)
    tasks = [t for t in rows(ctx, 'tasks') if t['project_id'] == p['id']]
    ms = _ms_map(ctx)
    metrics = redact_metrics(ctx, project_metrics(ctx, p), can_see_margins(ctx))
    team = list(dict.fromkeys([*metrics['team'], *(a['user_id'] for a in rows(ctx, 'allocations') if a['project_id'] == p['id'])]))
    team_set = set(team)
    since = add_days(ctx.today, -10)
    st = task_stats(ctx, {'projectId': p['id']})
    awaiting = sorted(
        (
            {
                'id': t['id'],
                'wbs': _tasks.wbs_of(t, ms),
                'title': prop(t, 'title'),
                'owner_id': prop(t, 'owner_id'),
                'reviewer_id': _taskflow.reviewer_of(ctx, t),
                'submitted_on': coalesce(prop(t, 'submitted_on'), None),
                'due_on': prop(t, 'due_on'),
                'round': coalesce(prop(t, 'review_round'), 1),
                'late': coalesce(prop(t, 'submitted_on'), ctx.today) > t['due_on'],
                'can_review': _taskflow.can_review_task(ctx, t),
            }
            for t in tasks
            if t.get('status') == 'in_review'
        ),
        key=lambda a: locale_key(coalesce(a['submitted_on'], '')),
    )
    cr = (
        sorted((c for c in rows(ctx, 'change_requests') if c['project_id'] == p['id']), key=lambda c: locale_key(c['created_at']), reverse=True)
        if has_any(ctx, 'change_requests.manage', 'projects.view_all')
        else []
    )
    return {
        'project': _strip_project(ctx, p),
        'metrics': metrics,
        'can_edit': can_edit(ctx, p),
        'can_see_money': can_see_margins(ctx),
        'is_admin': _is_admin(ctx),
        'milestones': _milestone_views(ctx, p, tasks),
        'billing_summary': _billing_summary(ctx, p, tasks),
        'can_bill': has(ctx, 'finance.edit') and has_any(ctx, 'finance.view'),
        'tasks': js_sort([_task_view(ctx, t, ms) for t in tasks], lambda a, b: sort_wbs(a['wbs'], b['wbs'])),
        'allocations': sorted(
            (_alloc_view(ctx, a, ms) for a in rows(ctx, 'allocations') if a['project_id'] == p['id']),
            key=lambda a: (locale_key(a['ms_code']), locale_key(a['from_on'])),
        ),
        'change_requests': cr,
        'status_history': sorted(
            (h for h in rows(ctx, 'project_status_history') if h['project_id'] == p['id']), key=lambda h: locale_key(h['changed_at']), reverse=True
        ),
        'recent_updates': sorted(
            (u for u in rows(ctx, 'daily_updates') if u['user_id'] in team_set and u['date'] >= since),
            key=lambda u: (locale_key(u['date']), locale_key(u['created_at'])),
            reverse=True,
        )[:8],
        'team_ids': team,
        'delivery': {
            'completed': st['completed'],
            'early': st['early'],
            'on_time': st['on_time'],
            'late': st['late'],
            'on_time_pct': st['on_time_pct'],
            'avg_variance_days': st['avg_variance_days'],
            'first_pass_pct': st['first_pass_pct'],
            'avg_quality': st['avg_quality'],
            'sent_back': st['sent_back'],
            'awaiting_review': awaiting,
        },
    }


def _changed_label(k: str, pat: str) -> str:
    return re.sub(pat, '', k).replace('_', ' ', 1)


def patch_project(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    p = _editable_project(ctx, req.params['id'])
    if _has_key(body, 'status'):
        err(422, 'VALIDATION', 'Change the status with the status control; it records the reason and history.', 'status')
    ch: dict[str, Any] = {}
    if _has_key(body, 'name'):
        ch['name'] = _need(body['name'], 'name', 'Project name')
    if _has_key(body, 'client'):
        ch['client'] = _need(body['client'], 'client', 'Client')
    if _has_key(body, 'type'):
        if not _in(body['type'], TYPES):
            err(422, 'VALIDATION', 'Choose a project type.', 'type')
        ch['type'] = body['type']
    if _has_key(body, 'billing_model'):
        if not _in(body['billing_model'], MODELS):
            err(422, 'VALIDATION', 'Choose a billing model.', 'billing_model')
        ch['billing_model'] = body['billing_model']
    if _has_key(body, 'stream'):
        if not _in(body['stream'], STREAMS):
            err(422, 'VALIDATION', 'Choose a revenue stream.', 'stream')
        ch['stream'] = body['stream']
    if _has_key(body, 'start_on'):
        ch['start_on'] = _need_date(body['start_on'], 'start_on', 'Start date')
    if _has_key(body, 'end_on'):
        ch['end_on'] = _need_date(body['end_on'], 'end_on', 'End date')
    if coalesce(ch.get('end_on', UNDEF), p['end_on']) < coalesce(ch.get('start_on', UNDEF), p['start_on']):
        err(422, 'DATE_RANGE', 'The end date is before the start date.', 'end_on')
    if _has_key(body, 'contract_value_paise'):
        ch['contract_value_paise'] = _money(body['contract_value_paise'], 'contract_value_paise', 'Contract value')
    if _has_key(body, 'priority'):
        pr = js_number(body['priority'])
        ch['priority'] = _intify(pr) if pr in (1, 2, 3, 4, 5) else 3
    if _has_key(body, 'description'):
        ch['description'] = js_trim(js_str(coalesce(body['description'], '')))
    if _has_key(body, 'pm_id') and body['pm_id'] != p.get('pm_id'):
        if not _is_admin(ctx):
            err(403, 'FORBIDDEN', 'Only an admin can hand a project to another project manager.', 'pm_id')
        _active_user(ctx, body['pm_id'], 'pm_id', True)
        ch['pm_id'] = body['pm_id']
    out = update(ctx, 'projects', p['id'], ch, req.if_match)
    changed = list(ch.keys())
    log_activity(ctx, 'project', p['id'], 'project.updated', f'Updated {", ".join(_changed_label(k, "_paise|_on|_id") for k in changed)}')
    me = ctx.user['id']
    if js_truthy(ch.get('pm_id')) and ch['pm_id'] != me:
        notify(
            ctx,
            ch['pm_id'],
            'NTF-14',
            f'You now manage {js_str(p["code"])}',
            f'{js_str(ctx.user.get("name", UNDEF))} made you the project manager of {js_str(out["name"])}.',
            f'/projects/{p["id"]}',
        )
    if 'contract_value_paise' in ch and can_see_margins(ctx):
        m = project_metrics(copy.copy(ctx), out)  # {...ctx}: a new ctx object = a fresh analytics memo
        floor = prop(ctx.tenant['settings'], 'projects.margin_floor_pct') / 100
        pm = margin_pct(out['contract_value_paise'], m['projected_cost'])
        if pm is not None and pm < floor:
            warn(
                ctx,
                'MARGIN_BELOW_FLOOR',
                f'Projected margin is {to_fixed(pm * 100, 1)}%, below the {js_str(ctx.tenant["settings"]["projects.margin_floor_pct"])}% floor.',
            )
    return out


def set_project_status(req: Req) -> Any:
    ctx = req.ctx
    p = _editable_project(ctx, req.params['id'])
    to = req.b('status')
    if not _in(to, STATUSES):
        err(422, 'VALIDATION', 'Choose a valid status.', 'status')
    if to == p.get('status'):
        err(409, 'SAME_STATUS', f'{js_str(p["name"])} is already {PROJECT_STATUS[to]["label"]}.', 'status')
    rb = req.b('reason')
    reason = js_trim(rb) if isinstance(rb, str) else ''
    if PROJECT_STATUS[to].get('needsReason') and js_len(reason) < 5:
        err(422, 'BR_17_REASON_REQUIRED', f'Moving a project to {PROJECT_STATUS[to]["label"]} needs a reason (at least 5 characters).', 'reason')
    me = ctx.user['id']
    insert(
        ctx,
        'project_status_history',
        {'project_id': p['id'], 'from_status': p['status'], 'to_status': to, 'reason': reason or None, 'changed_by': me, 'changed_at': now_iso()},
    )
    out = update(ctx, 'projects', p['id'], {'status': to})
    # the TS reads p.status AFTER update() changed it in place: "<new> → <new>"
    log_activity(
        ctx, 'project', p['id'], 'project.status', f'{PROJECT_STATUS[out["status"]]["label"]} → {PROJECT_STATUS[to]["label"]}{": " + reason if reason else ""}'
    )
    if js_truthy(p.get('pm_id')) and p['pm_id'] != me:
        notify(
            ctx,
            p['pm_id'],
            'NTF-14',
            f'{js_str(p["code"])} is now {PROJECT_STATUS[to]["label"]}',
            reason or f'{js_str(ctx.user.get("name", UNDEF))} changed the status.',
            f'/projects/{p["id"]}',
        )
    return out


def add_milestone(req: Req) -> Any:
    ctx = req.ctx
    p = _editable_project(ctx, req.params['id'])
    name = _need(req.b('name'), 'name', 'Milestone name')
    if js_len(name) > 120:
        err(422, 'VALIDATION', 'Keep the milestone name under 120 characters.', 'name')
    provisional = not js_truthy(req.b('planned_start')) and not js_truthy(req.b('planned_end'))
    last = None
    for m in _live_milestones(ctx, p['id']):
        if last is None or m['planned_end'] > last['planned_end']:
            last = m
    p_start = add_days(last['planned_end'], 1) if last is not None else p['start_on']
    planned_start = p_start if provisional else _need_date(req.b('planned_start'), 'planned_start', 'Start date')
    planned_end = add_working_days(planned_start, 10, cal(ctx)) if provisional else _need_date(req.b('planned_end'), 'planned_end', 'End date')
    if planned_end < planned_start:
        err(422, 'DATE_RANGE', 'The end date is before the start date.', 'planned_end')
    if not provisional and (planned_start < p['start_on'] or planned_end > p['end_on']):
        warn(
            ctx,
            'MS_OUTSIDE_PROJECT',
            f'The milestone runs outside the project dates ({fmt_date(p["start_on"])} – {fmt_date(p["end_on"])}). Extend the project if that’s intended.',
        )
    bill = _money(req.b('billing_amount_paise'), 'billing_amount_paise', 'Billing amount')
    no = _next_ms_no(ctx, p['id'], True)
    m = insert(
        ctx,
        'milestones',
        {
            'project_id': p['id'],
            'no': no,
            'name': name,
            'planned_start': planned_start,
            'planned_end': planned_end,
            'baseline_end': planned_end,
            'billing_amount_paise': bill,
            'billing_status': 'none',
            'achieved_on': None,
            'empty_since': ctx.today,
            'deleted_at': None,
        },
    )
    total: Any = 0
    for x in _live_milestones(ctx, p['id']):
        total = total + x['billing_amount_paise']
    if p['contract_value_paise'] > 0 and total > p['contract_value_paise']:
        warn(ctx, 'MS_BILLING_OVER', f'Milestone billing now totals {fmt_money(total)}, more than the contract value {fmt_money(p["contract_value_paise"])}.')
    log_activity(ctx, 'project', p['id'], 'milestone.created', f'Added M{js_str(no)} {name}')
    return m


def patch_milestone(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    m = find(ctx, 'milestones', req.params['id'], 'Milestone')
    if js_truthy(m.get('deleted_at')):
        err(404, 'NOT_FOUND', 'Milestone not found.')
    p = _editable_project(ctx, m['project_id'])
    ch: dict[str, Any] = {}
    if _has_key(body, 'name'):
        ch['name'] = _need(body['name'], 'name', 'Milestone name')
    if _has_key(body, 'planned_start'):
        ch['planned_start'] = _need_date(body['planned_start'], 'planned_start', 'Start date')
    if _has_key(body, 'planned_end'):
        ch['planned_end'] = _need_date(body['planned_end'], 'planned_end', 'End date')
    has_tasks = any(t.get('milestone_id') == m['id'] for t in rows(ctx, 'tasks'))
    if has_tasks:
        if js_truthy(ch.get('planned_end')) and ch['planned_end'] != m['planned_end']:
            err(
                409,
                'MS_DEADLINE_AUTO',
                f'M{js_str(m["no"])}’s deadline is the target end of its last task ({fmt_date(m["planned_end"])}). Move that task’s target end instead.',
                'planned_end',
            )
        if js_truthy(ch.get('planned_start')) and ch['planned_start'] != m['planned_start']:
            err(
                409,
                'MS_DEADLINE_AUTO',
                f'M{js_str(m["no"])} starts when its first task starts ({fmt_date(m["planned_start"])}). Move that task instead.',
                'planned_start',
            )
        ch.pop('planned_start', None)
        ch.pop('planned_end', None)
    if coalesce(ch.get('planned_end', UNDEF), m['planned_end']) < coalesce(ch.get('planned_start', UNDEF), m['planned_start']):
        err(422, 'DATE_RANGE', 'The end date is before the start date.', 'planned_end')
    if _has_key(body, 'billing_amount_paise'):
        v = _money(body['billing_amount_paise'], 'billing_amount_paise', 'Billing amount')
        if v != m['billing_amount_paise'] and m.get('billing_status') != 'none':
            err(409, 'MS_INVOICED', 'This milestone has been invoiced, so its billing amount is fixed.', 'billing_amount_paise')
        ch['billing_amount_paise'] = v
    out = update(ctx, 'milestones', m['id'], ch, req.if_match)
    if js_truthy(ch.get('planned_end')) and ch['planned_end'] > out['baseline_end']:
        warn(ctx, 'MS_SLIP', f'M{js_str(out["no"])} now ends {js_str(diff_days(ch["planned_end"], out["baseline_end"]))} days after its baseline.')
    log_activity(ctx, 'project', p['id'], 'milestone.updated', f'Updated M{js_str(out["no"])} {js_str(out["name"])}')
    return out


def delete_milestone(req: Req) -> Any:
    ctx = req.ctx
    m = find(ctx, 'milestones', req.params['id'], 'Milestone')
    if js_truthy(m.get('deleted_at')):
        err(404, 'NOT_FOUND', 'Milestone not found.')
    p = _editable_project(ctx, m['project_id'])
    if m.get('billing_status') != 'none':
        err(409, 'MS_INVOICED', 'This milestone has been invoiced, so it can’t be deleted. Edit it instead.')
    moved = sorted((t for t in rows(ctx, 'tasks') if t.get('milestone_id') == m['id']), key=lambda t: t['no'])
    ms_all = {x['id']: x for x in rows(ctx, 'milestones')}
    owners = []
    for t in moved:
        ch: dict[str, Any] = {}
        if not js_truthy(t.get('wbs_code')):
            ch['wbs_code'] = _tasks.wbs_of(t, ms_all)
        ch.update({'milestone_id': None, 'updated_at': now_iso(), 'version': t['version'] + 1})
        patch('tasks', t, ch)
        owners.append(t.get('owner_id'))
    mid = m['id']
    allocs = sum(1 for a in rows(ctx, 'allocations') if a.get('milestone_id') == mid)
    remove_where(ctx, 'allocations', lambda a: a.get('milestone_id') == mid)
    update(ctx, 'milestones', mid, {'deleted_at': now_iso()})
    n = len(moved)
    extra = f'; {n} task{"" if n == 1 else "s"} moved to No milestone' if n else ''
    log_activity(ctx, 'project', p['id'], 'milestone.deleted', f'Deleted M{js_str(m["no"])} {js_str(m["name"])}{extra}')
    told: set = set()
    me = ctx.user['id']
    for uidx in [p.get('pm_id'), *owners]:
        if js_truthy(uidx) and uidx != me and uidx not in told:
            told.add(uidx)
            notify(
                ctx,
                uidx,
                'NTF-15',
                f'Milestone M{js_str(m["no"])} deleted',
                f'{js_str(ctx.user.get("name", UNDEF))} deleted “{js_str(m["name"])}” on {js_str(p["name"])}.{" Its tasks are now under No milestone." if n else ""}',
                f'/projects/{p["id"]}/wbs',
            )
    return {'moved_tasks': n, 'removed_allocations': allocs}


def add_project_task(req: Req) -> Any:
    ctx = req.ctx
    p = _editable_project(ctx, req.params['id'])
    title = _need(req.b('title'), 'title', 'Task title')
    if js_len(title) > 200:
        err(422, 'VALIDATION', 'Keep the title under 200 characters.', 'title')
    ms_id = js_or(req.b('milestone_id'), None)
    m: dict | None = None
    if js_truthy(ms_id):
        m = next((x for x in rows(ctx, 'milestones') if x['id'] == ms_id and not js_truthy(x.get('deleted_at')) and x['project_id'] == p['id']), None)
        if m is None:
            err(422, 'VALIDATION', 'Choose a milestone of this project.', 'milestone_id')
    else:
        ms_id = None
    c = cal(ctx)
    owner = _active_user(ctx, req.b('owner_id'), 'owner_id')
    b_start = req.b('start_on')
    b_due = req.b('due_on')
    start_on = js_or(b_start, max_iso(ctx.today, coalesce(prop(m, 'planned_start'), p['start_on'])))
    if not _iso_test(start_on):
        err(422, 'VALIDATION', 'Start needs a valid date.', 'start_on')
    due_on = js_or(b_due, UNDEF)
    if not js_truthy(due_on):
        due_on = add_working_days(start_on, 2, c)
    if not js_truthy(b_due) and m is not None and due_on > m['planned_end'] and m['planned_end'] >= start_on:
        due_on = m['planned_end']
    if not js_truthy(b_start) and m is not None and start_on > m['planned_end']:
        start_on = due_on = m['planned_end']
    if not _iso_test(due_on):
        err(422, 'VALIDATION', 'Due needs a valid date.', 'due_on')
    if due_on < start_on:
        err(422, 'DATE_RANGE', 'The due date is before the start date.', 'due_on')
    b_est = req.b('est_hours')
    est = 8 if b_est is UNDEF else js_number(b_est)
    if not is_finite(est) or est < 0 or est > 999:
        err(422, 'VALIDATION', 'Estimated hours must be between 0 and 999.', 'est_hours')
    b_cx = req.b('complexity')
    if b_cx is not UNDEF and b_cx is not None and b_cx != '' and not _in(b_cx, CX):
        err(422, 'VALIDATION', 'Complexity must be low, medium, high or very high.', 'complexity')
    complexity = b_cx if _in(b_cx, CX) else 'medium'
    b_tag = req.b('tag')
    if js_truthy(b_tag) and not _in(b_tag, TAGS):
        err(422, 'VALIDATION', 'Choose a tag: Feature, Bug, Improvement, Documentation, Support or Meeting.', 'tag')
    backup = _active_user(ctx, req.b('backup_id'), 'backup_id')
    if backup is not None and owner is not None and backup['id'] == owner['id']:
        err(422, 'VALIDATION', 'The backup must be a different person from the owner.', 'backup_id')
    was_empty = m is not None and not any(x.get('milestone_id') == m['id'] for x in rows(ctx, 'tasks'))
    code = _new_task_code(ctx, p['id'], m)
    me = ctx.user['id']
    t = insert(
        ctx,
        'tasks',
        {
            'project_id': p['id'],
            'milestone_id': ms_id,
            'no': code['no'],
            'wbs_code': code['wbs_code'],
            'title': title,
            'owner_id': owner['id'] if owner is not None else None,
            'backup_id': backup['id'] if backup is not None else None,
            'est_hours': est,
            'start_on': start_on,
            'due_on': due_on,
            'done_on': None,
            'complexity': complexity,
            'status': 'todo',
            'description': js_trim(js_str(coalesce(req.b('description'), ''))),
            'blocked_reason': None,
            'kind': 'general' if p['id'] == f'p_general_{js_str(ctx.tenant.get("slug", UNDEF))}' else 'project',
            'assigned_by': me,
            'review_round': 0,
            'priority': 'normal',
            'stage': 'new',
            'progress': 0,
            'tag': coalesce(b_tag, None),
            'actual_start_on': None,
            'baseline_due_on': due_on,
            'target_revisions': [],
            'slip_flagged_on': None,
            'slip_note': None,
        },
    )
    if m is not None:
        ch: dict[str, Any] = {'empty_since': None}
        if js_truthy(m.get('achieved_on')):
            ch['achieved_on'] = None
        m = patch('milestones', m, ch)
        sync_milestone_dates(ctx, m['id'], was_empty)
    wc = code['wbs_code']
    log_activity(ctx, 'task', t['id'], 'task.created', f'Added {wc} {title}')
    log_activity(ctx, 'project', p['id'], 'task.created', f'Added task {wc} {title}')
    if owner is not None and owner['id'] != me:
        notify(ctx, owner['id'], 'NTF-14', 'Task assigned to you', f'{wc} {title} · {js_str(p["name"])}', f'/my-tasks?task={t["id"]}')
    return t


def patch_task(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    p = find(ctx, 'projects', t['project_id'], 'Project') if _taskflow.can_manage_general(ctx, t) else _editable_project(ctx, t['project_id'])
    ch: dict[str, Any] = {}

    def owner_after() -> Any:
        return coalesce(body['owner_id'] if _has_key(body, 'owner_id') else prop(t, 'owner_id'), None)

    if _has_key(body, 'priority'):
        if not _in(body['priority'], ['low', 'normal', 'high', 'urgent']):
            err(422, 'VALIDATION', 'Priority must be low, normal, high or urgent.', 'priority')
        ch['priority'] = body['priority']
    if _has_key(body, 'reviewer_id'):
        rv = _active_user(ctx, body['reviewer_id'], 'reviewer_id')
        if rv is not None and rv['id'] == owner_after() and not _taskflow.is_top(ctx, rv['id']):
            err(422, 'TASK_SELF_REVIEW', 'The reviewer must be someone other than the owner.', 'reviewer_id')
        ch['reviewer_id'] = rv['id'] if rv is not None else None
    if _has_key(body, 'status'):
        err(422, 'VALIDATION', 'Change status with the status control so dependencies are checked.', 'status')
    if _has_key(body, 'concerned_manager_id'):
        cm = _active_user(ctx, body['concerned_manager_id'], 'concerned_manager_id')
        if cm is not None and cm['id'] == owner_after() and not _taskflow.is_top(ctx, cm['id']):
            err(422, 'VALIDATION', 'The concerned manager must be someone other than the owner.', 'concerned_manager_id')
        ch['concerned_manager_id'] = cm['id'] if cm is not None else None
    if _has_key(body, 'watcher_ids'):
        w = body['watcher_ids']
        if not isinstance(w, list) or len(w) > 20:
            err(422, 'VALIDATION', 'Choose up to 20 watchers.', 'watcher_ids')
        ids = []
        for i in w:
            u = _active_user(ctx, i, 'watcher_ids')
            ids.append(u['id'] if u is not None else None)  # activeUser(…)!.id: a falsy id throws in the TS (500)
            if u is None:
                raise TypeError("Cannot read properties of null (reading 'id')")
        ch['watcher_ids'] = list(dict.fromkeys(ids))
    if _has_key(body, 'acceptance_criteria'):
        ac = body['acceptance_criteria']
        if not isinstance(ac, list) or len(ac) > 30:
            err(422, 'VALIDATION', 'Keep the definition of done to 30 items or fewer.', 'acceptance_criteria')
        lst = []
        for c in ac:
            txt = js_trim(js_str(c if isinstance(c, str) else coalesce(_g(c, 'text'), '')))
            lst.append({'text': txt, 'done': isinstance(c, dict) and c.get('done') is True})
        ch['acceptance_criteria'] = [c for c in lst if c['text']]
        if any(js_len(c['text']) > 300 for c in ch['acceptance_criteria']):
            err(422, 'VALIDATION', 'Each criterion must be under 300 characters.', 'acceptance_criteria')
    if _has_key(body, 'labels'):
        lb = body['labels']
        if not isinstance(lb, list):
            err(422, 'VALIDATION', 'Labels must be a list.', 'labels')
        ls = list(dict.fromkeys(x for x in (js_trim(js_str(coalesce(x, ''))) for x in lb) if x))
        if len(ls) > 10 or any(js_len(x) > 30 for x in ls):
            err(422, 'VALIDATION', 'Use up to 10 labels of 30 characters or fewer.', 'labels')
        ch['labels'] = ls
    if _has_key(body, 'billable'):
        if not isinstance(body['billable'], bool):
            err(422, 'VALIDATION', 'Billable must be yes or no.', 'billable')
        ch['billable'] = body['billable']
    if _has_key(body, 'recurrence'):
        if not _in(body['recurrence'], ['none', 'weekly', 'monthly']):
            err(422, 'VALIDATION', 'Repeat must be none, weekly or monthly.', 'recurrence')
        ch['recurrence'] = body['recurrence']
    if _has_key(body, 'title'):
        ch['title'] = _need(body['title'], 'title', 'Task title')
        if js_len(ch['title']) > 200:
            err(422, 'VALIDATION', 'Keep the title under 200 characters.', 'title')
    if _has_key(body, 'description'):
        ch['description'] = js_str(coalesce(body['description'], ''))
    if _has_key(body, 'owner_id'):
        u = _active_user(ctx, body['owner_id'], 'owner_id')
        ch['owner_id'] = u['id'] if u is not None else None
    if _has_key(body, 'backup_id'):
        u = _active_user(ctx, body['backup_id'], 'backup_id')
        ch['backup_id'] = u['id'] if u is not None else None
    owner = ch['owner_id'] if 'owner_id' in ch else prop(t, 'owner_id')
    backup = ch['backup_id'] if 'backup_id' in ch else prop(t, 'backup_id')
    if js_truthy(owner) and js_truthy(backup) and owner == backup:
        err(422, 'VALIDATION', 'The backup must be a different person from the owner.', 'backup_id')
    if _has_key(body, 'est_hours'):
        v = js_number(body['est_hours'])
        if not is_finite(v) or v < 0 or v > 999:
            err(422, 'VALIDATION', 'Estimated hours must be between 0 and 999.', 'est_hours')
        ch['est_hours'] = js_round(v * 4) / 4
    if _has_key(body, 'start_on'):
        ch['start_on'] = _need_date(body['start_on'], 'start_on', 'Start')
    if _has_key(body, 'due_on'):
        ch['due_on'] = _need_date(body['due_on'], 'due_on', 'Due')
    dr = req.b('due_change_reason')
    due_reason = js_trim(dr) if isinstance(dr, str) else ''
    if js_truthy(ch.get('due_on')) and ch['due_on'] != t['due_on']:
        if t.get('status') == 'done':
            err(409, 'INVALID_STATE', 'This task is approved. Reopen it before changing the due date.', 'due_on')
        if js_truthy(t.get('owner_id')) and js_len(due_reason) < 10:
            err(
                422,
                'DUE_REASON_REQUIRED',
                'Say why the due date is moving (at least 10 characters). Reschedules are kept in the task history.',
                'due_change_reason',
            )
    elif 'due_on' in ch and ch['due_on'] == t['due_on']:
        del ch['due_on']
    if coalesce(ch.get('due_on', UNDEF), t['due_on']) < coalesce(ch.get('start_on', UNDEF), t['start_on']):
        err(422, 'DATE_RANGE', 'The due date is before the start date.', 'due_on')
    if _has_key(body, 'complexity'):
        if not _in(body['complexity'], CX):
            err(422, 'VALIDATION', 'Complexity must be low, medium, high or very high.', 'complexity')
        ch['complexity'] = body['complexity']
    if _has_key(body, 'tag'):
        if js_truthy(body['tag']) and not _in(body['tag'], TAGS):
            err(422, 'VALIDATION', 'Choose a tag: Feature, Bug, Improvement, Documentation, Support or Meeting.', 'tag')
        ch['tag'] = js_or(body['tag'], None)
    me = ctx.user['id']
    if js_truthy(ch.get('due_on')) and ch['due_on'] != t['due_on']:
        ch['baseline_due_on'] = coalesce(prop(t, 'baseline_due_on'), t['due_on'])
        ch['target_revisions'] = [
            *coalesce(prop(t, 'target_revisions'), []),
            {'from': t['due_on'], 'to': ch['due_on'], 'at': now_iso(), 'by': me, 'reason': js_or(due_reason, 'Rescheduled')},
        ]
    old_ms = t.get('milestone_id')
    if _has_key(body, 'milestone_id') and js_or(body['milestone_id'], None) != old_ms:
        bm = body['milestone_id']
        nm = (
            next((x for x in rows(ctx, 'milestones') if x['id'] == bm and not js_truthy(x.get('deleted_at')) and x['project_id'] == p['id']), None)
            if js_truthy(bm)
            else None
        )
        if js_truthy(bm) and nm is None:
            err(422, 'VALIDATION', 'Choose a milestone of this project.', 'milestone_id')
        ch['milestone_id'] = nm['id'] if nm is not None else None
        _tasks.freeze_wbs(t, {x['id']: x for x in rows(ctx, 'milestones')})
        t = find(ctx, 'tasks', t['id'])
        if nm is not None:
            patch('milestones', nm, {'empty_since': None})
    new_ms_was_empty = not any(x.get('milestone_id') == ch['milestone_id'] for x in rows(ctx, 'tasks')) if js_truthy(ch.get('milestone_id')) else False
    out = update(ctx, 'tasks', t['id'], ch, req.if_match)
    if 'milestone_id' in ch:
        sync_achieved(ctx, old_ms)
        sync_achieved(ctx, ch['milestone_id'])
        if js_truthy(old_ms) and not any(x.get('milestone_id') == old_ms for x in rows(ctx, 'tasks')):
            om = next((x for x in rows(ctx, 'milestones') if x['id'] == old_ms), None)
            if om is not None and om.get('billing_status') == 'none':
                patch('milestones', om, {'empty_since': ctx.today})
        sync_milestone_dates(ctx, old_ms)
        sync_milestone_dates(ctx, ch['milestone_id'], new_ms_was_empty)
    elif js_truthy(ch.get('due_on')) or js_truthy(ch.get('start_on')):
        sync_milestone_dates(ctx, out.get('milestone_id'))
    ms = _ms_map(ctx)
    code = _tasks.wbs_of(out, ms)
    changed = [k for k in ch if k != 'no']
    if changed:
        log_activity(ctx, 'task', t['id'], 'task.updated', f'Updated {", ".join(re.sub("_on|_id|_hours", "", k) for k in changed)}')
    # The TS compares the patch with `t` AFTER update() changed it in place: these conditions are always false
    # (no notifications / reschedule log from PATCH) — mirrored by comparing with the updated row.
    if js_truthy(ch.get('owner_id')) and ch['owner_id'] != out.get('owner_id') and ch['owner_id'] != me:
        notify(ctx, ch['owner_id'], 'NTF-14', 'Task assigned to you', f'{code} {js_str(out["title"])} · {js_str(p["name"])}', f'/my-tasks?task={t["id"]}')
    for w in ch.get('watcher_ids', []):
        if w != me and w not in coalesce(prop(out, 'watcher_ids'), []):
            notify(
                ctx,
                w,
                'NTF-14',
                'You were added as a watcher',
                f'{js_str(ctx.user.get("name", UNDEF))} added you to {code} {js_str(out["title"])}',
                f'/tasks/{t["id"]}',
            )
    if js_truthy(ch.get('reviewer_id')) and ch['reviewer_id'] != out.get('reviewer_id') and ch['reviewer_id'] != me:
        notify(
            ctx, ch['reviewer_id'], 'NTF-14', 'You review a task', f'{code} {js_str(out["title"])} · you approve it when it’s submitted', f'/tasks/{t["id"]}'
        )
    if js_truthy(ch.get('concerned_manager_id')) and ch['concerned_manager_id'] != out.get('concerned_manager_id') and ch['concerned_manager_id'] != me:
        notify(ctx, ch['concerned_manager_id'], 'NTF-14', 'You’re the concerned manager on a task', f'{code} {js_str(out["title"])}', f'/tasks/{t["id"]}')
    if 'owner_id' in ch and ch['owner_id'] != out.get('owner_id'):
        log_activity(ctx, 'project', p['id'], 'task.assigned', f'{code} {js_str(out["title"])} → {user_name(ctx, ch["owner_id"])}')
    if js_truthy(ch.get('due_on')) and ch['due_on'] != out['due_on']:
        n = (
            sum(1 for a in rows(ctx, 'activity_log') if a.get('entity') == 'task' and a.get('entity_id') == t['id'] and a.get('action') == 'task.rescheduled')
            + 1
        )
        log_activity(
            ctx,
            'task',
            t['id'],
            'task.rescheduled',
            f'Due moved {fmt_date(out["due_on"])} → {fmt_date(ch["due_on"])}{f" (reschedule #{n})" if n > 1 else ""}: {js_or(due_reason, "no reason given")}',
        )
    return out


def set_task_status(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    p = find(ctx, 'projects', t['project_id'], 'Project')
    editor = can_edit(ctx, p) or _taskflow.can_manage_general(ctx, t)
    me = ctx.user['id']
    if not editor and t.get('owner_id') != me and t.get('backup_id') != me and not _taskflow.can_review_task(ctx, t):
        err(403, 'FORBIDDEN', 'Only the task owner, their backup or the project manager can change its status.')
    to = req.b('status')
    if not _in(to, TASK_STATUSES):
        err(422, 'VALIDATION', 'Choose a valid status.', 'status')
    if to == t.get('status'):
        return t
    if to == 'in_review':
        err(409, 'USE_REVIEW_FLOW', 'Use “Submit for review” so your note (and delay reason, if late) reaches the reviewer.', 'status')
    if t.get('status') == 'in_review':
        err(
            409,
            'USE_REVIEW_FLOW',
            f"This task is waiting for {user_name(ctx, _taskflow.reviewer_of(ctx, t))}'s review. The reviewer approves it or sends it back.",
            'status',
        )
    if to == 'done':
        if _taskflow.self_review(ctx, t) or not _taskflow.can_review_task(ctx, t):
            err(
                409,
                'USE_REVIEW_FLOW',
                f'Tasks are completed through review. Submit it for review — {user_name(ctx, _taskflow.reviewer_of(ctx, t))} approves it.',
                'status',
            )
        open_ = open_blockers(ctx, t['id'])
        if open_:
            lst = ', '.join(f'{_tasks.wbs_of(d, _ms_map(ctx))} {js_str(d["title"])}' for d in open_)
            err(409, 'TASK_BLOCKED', f'This task waits on {lst}. Finish {"it" if len(open_) == 1 else "them"} first.', 'status')
        qr = req.b('quality_rating')
        if qr is UNDEF or qr is None:
            err(422, 'VALIDATION', 'Rate the quality of the work (1–5) to mark it done.', 'quality_rating')
        bn = req.b('note')
        return _taskflow.approve_task(ctx, t, qr, js_or(js_trim(bn), None) if isinstance(bn, str) else None, True)
    cur = stage_of(t)
    grp = STAGE[cur]['group']
    if to == 'todo':
        stage = cur if grp == 'not_started' else 'new'
    elif to == 'in_progress':
        stage = cur if grp in ('active', 'testing') else 'in_development'
    else:
        stage = 'blocked'
    br = req.b('blocked_reason')
    return _taskflow.apply_stage(ctx, t, stage, {'note': br if isinstance(br, str) else None})


def _unique(lst: list) -> list:
    """[...new Set(lst)] (SameValueZero; unhashable values by identity)"""
    out: list = []
    seen: set = set()
    for x in lst:
        if _hashable(x) and not isinstance(x, bool):
            k = (type(x) is str, x)
            if k in seen:
                continue
            seen.add(k)
            out.append(x)
        elif not any(y is x for y in out):
            out.append(x)
    return out


def set_dependencies(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    _editable_project(ctx, t['project_id'])
    bd = req.b('depends_on')
    ids = _unique(bd) if isinstance(bd, list) else []
    all_ = rows(ctx, 'tasks')
    for id_ in ids:
        if id_ == t['id'] and isinstance(id_, str):
            err(422, 'VALIDATION', 'A task can’t depend on itself.', 'depends_on')
        d = next((x for x in all_ if isinstance(id_, str) and x['id'] == id_), None)
        if d is None or d['project_id'] != t['project_id']:
            err(422, 'VALIDATION', 'Dependencies must be tasks of the same project.', 'depends_on')
    edges = _dep_edges(ctx)
    edges[t['id']] = ids
    if _detect_cycle(edges, t['id']):
        err(409, 'DEP_CYCLE', 'That would create a circular dependency. Remove one of the links.', 'depends_on')
    tid = t['id']
    remove_where(ctx, 'task_dependencies', lambda d: d['task_id'] == tid)
    for id_ in ids:
        insert(ctx, 'task_dependencies', {'task_id': tid, 'depends_on_task_id': id_})
    ms = _ms_map(ctx)
    by_id = {x['id']: x for x in all_}
    log_activity(
        ctx, 'task', tid, 'task.dependencies', f'Depends on {", ".join(_tasks.wbs_of(by_id[i], ms) for i in ids)}' if ids else 'Removed all dependencies'
    )
    if ids and t.get('status') in ('in_progress', 'in_review') and open_blockers(ctx, tid):
        warn(ctx, 'TASK_BLOCKED', 'This task is in progress but now waits on an open task.')
    return _build_task_detail(ctx, find(ctx, 'tasks', tid))


def delete_task(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    p = _editable_project(ctx, t['project_id'])
    hours: Any = 0
    for e in rows(ctx, 'timesheet_entries'):
        if e.get('task_id') == t['id']:
            hours = hours + e['hours']
    if hours > 0:
        err(
            409,
            'TASK_HAS_HOURS',
            f'{js_str(hours)}h are logged against this task, so it can’t be deleted (the hours stay on the project cost). Cancel it instead.',
        )
    ms = _ms_map(ctx)
    code = _tasks.wbs_of(t, ms)
    remove(ctx, 'tasks', t['id'], req.if_match)
    tid = t['id']
    remove_where(ctx, 'task_dependencies', lambda d: d['task_id'] == tid or d['depends_on_task_id'] == tid)
    remove_where(ctx, 'comments', lambda c: c['task_id'] == tid)
    mid = t.get('milestone_id')
    if js_truthy(mid) and not any(x.get('milestone_id') == mid for x in rows(ctx, 'tasks')):
        m = next((x for x in rows(ctx, 'milestones') if x['id'] == mid), None)
        if m is not None and m.get('billing_status') == 'none':
            patch('milestones', m, {'empty_since': ctx.today})
    else:
        sync_achieved(ctx, mid)
        sync_milestone_dates(ctx, mid)
    log_activity(ctx, 'project', p['id'], 'task.deleted', f'Deleted {code} {js_str(t["title"])}')
    return None


def get_schedule(req: Req) -> Any:
    ctx = req.ctx
    return _compute_schedule(ctx, _get_project(ctx, req.params['id']))


def get_load(req: Req) -> Any:
    ctx = req.ctx
    return _project_load(ctx, _get_project(ctx, req.params['id']))


def add_allocation(req: Req) -> Any:
    ctx = req.ctx
    p = _editable_project(ctx, req.params['id'])
    if not has(ctx, 'allocation.edit'):
        err(403, 'FORBIDDEN', 'You can’t change allocations.')
    v = _validate_allocation(ctx, p, req.body, None)
    a = insert(ctx, 'allocations', v)
    m = find(ctx, 'milestones', v['milestone_id']) if js_truthy(v['milestone_id']) else None
    log_activity(
        ctx,
        'project',
        p['id'],
        'allocation.created',
        f'Allocated {user_name(ctx, v["user_id"])} {js_str(v["pct"])}% to {"M" + js_str(m["no"]) if m is not None else "all milestones"} ({fmt_date(v["from_on"])} – {fmt_date(v["to_on"])})',
    )
    return a


def patch_allocation(req: Req) -> Any:
    ctx = req.ctx
    a = find(ctx, 'allocations', req.params['id'], 'Allocation')
    p = _editable_project(ctx, a['project_id'])
    v = _validate_allocation(ctx, p, {**a, **(req.body if isinstance(req.body, dict) else {})}, a['id'])
    out = update(ctx, 'allocations', a['id'], v, req.if_match)
    log_activity(
        ctx,
        'project',
        p['id'],
        'allocation.updated',
        f'Allocation for {user_name(ctx, v["user_id"])} set to {js_str(v["pct"])}% ({fmt_date(v["from_on"])} – {fmt_date(v["to_on"])})',
    )
    return out


def delete_allocation(req: Req) -> Any:
    ctx = req.ctx
    a = find(ctx, 'allocations', req.params['id'], 'Allocation')
    p = _editable_project(ctx, a['project_id'])
    remove(ctx, 'allocations', a['id'])
    log_activity(ctx, 'project', p['id'], 'allocation.deleted', f'Removed {user_name(ctx, a["user_id"])}’s {js_str(a["pct"])}% allocation')
    return None


def get_money(req: Req) -> Any:
    ctx = req.ctx
    p = _get_project(ctx, req.params['id'])
    if not can_see_margins(ctx):
        return None
    money = _project_money(ctx, p)
    an = public_analysis(ctx, analyse(ctx, p))

    def trip(c: dict) -> dict[str, Any]:
        return {'revenue': c['revenue'], 'cost': c['cost'], 'margin': c['margin'], 'markup': c['markup']}

    return {**money, 'planned': trip(an['planned']), 'projected': trip(an['projected']), 'actual': trip(an['actual']), 'analysis': an}


def invoice_next_number(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'finance.edit'):
        err(403, 'FORBIDDEN', 'Only admins raise invoices.')
    return {'number': _suggest_invoice_number(ctx)['number']}


def raise_invoice(req: Req) -> Any:
    from app.routes.invoices import raise_milestone_invoice

    ctx = req.ctx
    if not has(ctx, 'finance.edit'):
        err(403, 'FORBIDDEN', 'Only admins raise invoices.')
    m = find(ctx, 'milestones', req.params['id'], 'Milestone')
    if js_truthy(m.get('deleted_at')):
        err(404, 'NOT_FOUND', 'Milestone not found.')
    _get_project(ctx, m['project_id'])
    if m.get('billing_status') != 'none':
        err(409, 'MS_INVOICED', f'M{js_str(m["no"])} is already invoiced.')
    number = _need(req.b('number'), 'number', 'Invoice number')
    nl = number.lower()
    if any(js_str(i.get('number', UNDEF)).lower() == nl for i in rows(ctx, 'invoices')):
        err(409, 'INV_NUMBER_TAKEN', f'Invoice {number} already exists. Use {_suggest_invoice_number(ctx)["number"]}.', 'number')
    invoiced_on = _need_date(req.b('invoiced_on'), 'invoiced_on', 'Invoice date')
    if invoiced_on > ctx.today:
        err(422, 'VALIDATION', 'An invoice can’t be dated in the future. Use today or an earlier date.', 'invoiced_on')
    due_on = _need_date(req.b('due_on'), 'due_on', 'Due date') if js_truthy(req.b('due_on')) else add_days(invoiced_on, 30)
    if due_on < invoiced_on:
        err(422, 'DATE_RANGE', 'The due date is before the invoice date.', 'due_on')
    amount = _money(req.b('amount_paise'), 'amount_paise', 'Amount', False)
    file_id: Any = None
    f = req.b('file')
    if js_truthy(f):
        if not js_truthy(_g(f, 'name')) or not js_truthy(_g(f, 'data_url')):
            err(422, 'FILE_REJECTED', 'The file could not be read. Try again.', 'file')
        if _g(f, 'size_bytes') is not UNDEF and _g(f, 'size_bytes') is not None and js_number(_g(f, 'size_bytes')) > 10 * 1024 * 1024:
            err(422, 'FILE_REJECTED', 'The file is larger than 10 MB.', 'file')
        file_id = insert(
            ctx,
            'files',
            {
                'name': f['name'],
                'mime': js_or(_g(f, 'mime'), 'application/octet-stream'),
                'size_bytes': _g(f, 'size_bytes'),
                'entity': 'invoice',
                'entity_id': '',
                'data_url': f['data_url'],
            },
        )['id']
    inv = raise_milestone_invoice(ctx, m, {'number': number, 'invoiced_on': invoiced_on, 'due_on': due_on, 'amount_paise': amount, 'file_id': file_id})
    if js_truthy(file_id):
        fr = next((x for x in rows(ctx, 'files') if x['id'] == file_id), None)
        if fr is not None:
            patch('files', fr, {'entity_id': inv['id']})
    return inv


def record_payment(req: Req) -> Any:
    from app.routes.invoices import record_revenue_payment

    ctx = req.ctx
    if not has(ctx, 'finance.edit'):
        err(403, 'FORBIDDEN', 'Only admins record payments.')
    inv = find(ctx, 'invoices', req.params['id'], 'Invoice')
    amount = _money(req.b('amount_paise'), 'amount_paise', 'Amount', False)
    paid_on = _need_date(req.b('paid_on'), 'paid_on', 'Payment date')
    if paid_on < inv['invoiced_on']:
        err(422, 'DATE_RANGE', 'The payment date is before the invoice date.', 'paid_on')
    return record_revenue_payment(ctx, inv, amount, paid_on, js_trim(js_str(coalesce(req.b('reference'), ''))))


# ---------- change requests (G5.04) ----------
def _cr_project(ctx: Ctx, id_: Any) -> dict:
    if not has(ctx, 'change_requests.manage'):
        err(403, 'FORBIDDEN', 'Only admins and the project manager handle change requests.')
    return _editable_project(ctx, id_)


def _cr_number(ctx: Ctx, pid: Any, take: bool) -> Any:
    start = js_max(0, *[coalesce(prop(c, 'number'), 0) for c in rows(ctx, 'change_requests') if c['project_id'] == pid]) + 1
    return _take_number(ctx, f'cr:{js_str(pid)}', start) if take else _peek_number(ctx, f'cr:{js_str(pid)}', start)


def _proposed_tasks(ctx: Ctx, v: Any) -> Any:
    if v is UNDEF:
        return UNDEF
    if not isinstance(v, list):
        err(422, 'VALIDATION', 'Proposed tasks must be a list.', 'proposed_tasks')
    lst = [x for x in v if js_trim(coalesce(_g_strict(x, 'title'), ''))]
    if len(lst) > 30:
        err(422, 'VALIDATION', 'Propose at most 30 tasks per change request.', 'proposed_tasks')
    out = []
    for i, x in enumerate(lst):
        h = js_number(coalesce(_g(x, 'est_hours'), 0))
        if not is_finite(h) or h < 0 or h > 999:
            err(422, 'VALIDATION', f'Proposed task {i + 1}: hours must be between 0 and 999.', f'proposed_tasks.{i}.est_hours')
        title = js_trim(js_str(x['title']))
        if js_len(title) > 200:
            err(422, 'VALIDATION', f'Proposed task {i + 1}: keep the title under 200 characters.', f'proposed_tasks.{i}.title')
        u = _active_user(ctx, coalesce(_g(x, 'owner_id'), None), f'proposed_tasks.{i}.owner_id')
        out.append({'title': title, 'est_hours': js_round(h * 4) / 4, 'owner_id': u['id'] if u is not None else None})
    return out


def _g_strict(x: Any, k: str) -> Any:
    """x.k where x must be an object (null / undefined items throw in the TS)"""
    if x is None or x is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(x)} (reading '{k}')")
    return _g(x, k)


def _cr_fields(ctx: Ctx, body: Any, partial: bool) -> dict[str, Any]:
    ch: dict[str, Any] = {}
    b = body if isinstance(body, dict) else {}
    if not partial or 'title' in b:
        ch['title'] = _need(b.get('title', UNDEF), 'title', 'Title')
        if js_len(ch['title']) > 200:
            err(422, 'VALIDATION', 'Keep the title under 200 characters.', 'title')
    if not partial or 'description' in b:
        ch['description'] = js_trim(js_str(coalesce(b.get('description', UNDEF), '')))
    if not partial or 'note' in b:
        ch['note'] = js_trim(js_str(coalesce(b.get('note', UNDEF), '')))
    if not partial or 'requested_by_client' in b:
        ch['requested_by_client'] = js_trim(js_str(coalesce(b.get('requested_by_client', UNDEF), '')))
        if not partial and not ch['requested_by_client']:
            err(422, 'VALIDATION', 'Say who at the client asked for it.', 'requested_by_client')
    if not partial or 'requested_on' in b:
        d = js_or(js_trim(js_str(coalesce(b.get('requested_on', UNDEF), ''))), '' if partial else ctx.today)
        if d and not _iso_test(d):
            err(422, 'VALIDATION', 'Requested on needs a valid date.', 'requested_on')
        if d and d > ctx.today:
            err(422, 'DATE_RANGE', 'Requested on can’t be in the future.', 'requested_on')
        ch['requested_on'] = d or None
    if not partial or 'hours' in b:
        h = js_number(coalesce(b.get('hours', UNDEF), 0))
        if not is_finite(h) or h < 0 or h > 9999:
            err(422, 'VALIDATION', 'Effort hours must be zero or more.', 'hours')
        ch['hours'] = h
    if not partial or 'impact_days' in b:
        n = js_number(coalesce(b.get('impact_days', UNDEF), 0))
        if not is_integer(n) or n < 0 or n > 365:
            err(422, 'VALIDATION', 'Working days impact must be a whole number of days (0 or more).', 'impact_days')
        ch['impact_days'] = n
    if not partial or 'value_paise' in b:
        ch['value_paise'] = _money(b.get('value_paise', UNDEF), 'value_paise', 'Billable value')
    pt = _proposed_tasks(ctx, b.get('proposed_tasks', UNDEF))
    if pt is not UNDEF:
        ch['proposed_tasks'] = pt
    if pt is not UNDEF and len(pt) and not js_truthy(coalesce(ch.get('hours', UNDEF), 0)) and not ('hours' in b and js_number(b['hours']) > 0):
        s: Any = 0
        for x in pt:
            s = s + x['est_hours']
        ch['hours'] = s
    return ch


def _cr_list(ctx: Ctx, p: dict) -> dict[str, Any]:
    lst = sorted(
        (c for c in rows(ctx, 'change_requests') if c['project_id'] == p['id']),
        key=lambda c: (coalesce(prop(c, 'number'), 0), locale_key(c['created_at'])),
        reverse=True,
    )
    cr_task = {t['id'] for t in rows(ctx, 'tasks') if t['project_id'] == p['id'] and js_truthy(t.get('cr_id'))}
    baseline: Any = 0
    for t in rows(ctx, 'tasks'):
        if t['project_id'] == p['id'] and t['id'] not in cr_task:
            baseline = baseline + t['est_hours']
    approved = [c for c in lst if c.get('status') in ('approved', 'done')]
    ah: Any = 0
    av: Any = 0
    for c in approved:
        ah = ah + c['hours']
    for c in approved:
        av = av + c['value_paise']
    return {
        'items': lst,
        'raised': len(lst),
        'awaiting': sum(1 for c in lst if c.get('status') in ('raised', 'estimated')),
        'approved_hours': ah,
        'approved_value': av,
        'baseline_hours': baseline,
        'scope_growth': ah / baseline if baseline > 0 else None,
        'next_number': _cr_number(ctx, p['id'], False),
        'can_manage': can_edit(ctx, p) and has(ctx, 'change_requests.manage'),
    }


def list_change_requests(req: Req) -> Any:
    ctx = req.ctx
    p = _get_project(ctx, req.params['id'])
    if not has_any(ctx, 'change_requests.manage', 'projects.view_all') and not can_edit(ctx, p):
        err(403, 'FORBIDDEN', 'Only the project manager and admins see change requests.')
    return _cr_list(ctx, p)


def add_change_request(req: Req) -> Any:
    ctx = req.ctx
    p = _cr_project(ctx, req.params['id'])
    f = _cr_fields(ctx, req.body, False)
    number = _cr_number(ctx, p['id'], True)
    hours = coalesce(f.get('hours', UNDEF), 0)
    cr = insert(
        ctx,
        'change_requests',
        {
            'project_id': p['id'],
            'title': f['title'],
            'description': coalesce(f.get('description', UNDEF), ''),
            'hours': hours,
            'value_paise': coalesce(f.get('value_paise', UNDEF), 0),
            'status': 'estimated' if hours > 0 else 'raised',
            'raised_by': ctx.user['id'],
            'milestone_id': None,
            'decided_by': None,
            'decided_at': None,
            'number': number,
            'requested_by_client': coalesce(f.get('requested_by_client', UNDEF), ''),
            'requested_on': coalesce(f.get('requested_on', UNDEF), ctx.today),
            'impact_days': coalesce(f.get('impact_days', UNDEF), 0),
            'note': coalesce(f.get('note', UNDEF), ''),
            'proposed_tasks': coalesce(f.get('proposed_tasks', UNDEF), []),
            'tasks_added_on': None,
        },
    )
    extra = f' ({js_str(cr["hours"])}h, {fmt_money(cr["value_paise"])})' if js_truthy(cr['hours']) else ''
    log_activity(ctx, 'project', p['id'], 'cr.raised', f'CR-{js_str(number)} raised: {js_str(cr["title"])}{extra}')
    return cr


_CR_EDIT_KEYS = ['title', 'description', 'hours', 'value_paise', 'requested_by_client', 'requested_on', 'impact_days', 'note', 'proposed_tasks']


def patch_change_request(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    cr = find(ctx, 'change_requests', req.params['id'], 'Change request')
    p = _cr_project(ctx, cr['project_id'])
    editing = any(_has_key(body, k) for k in _CR_EDIT_KEYS)
    if editing and cr.get('status') not in ('raised', 'estimated'):
        err(409, 'CR_DECIDED', 'This change request has been decided. Raise a new one for more changes.')
    ch = _cr_fields(ctx, body, True)
    bs = req.b('status')
    if bs == 'estimated':
        if cr.get('status') != 'raised':
            err(409, 'CR_STATUS', 'Only a raised change request can be estimated.')
        if coalesce(ch.get('hours', UNDEF), cr['hours']) <= 0:
            err(422, 'VALIDATION', 'Add the estimated hours first.', 'hours')
        ch['status'] = 'estimated'
    elif bs == 'done':
        if cr.get('status') != 'approved':
            err(409, 'CR_STATUS', 'Only an approved change request can be marked done.')
        ch['status'] = 'done'
    elif cr.get('status') == 'raised' and coalesce(ch.get('hours', UNDEF), 0) > 0:
        ch['status'] = 'estimated'
    out = update(ctx, 'change_requests', cr['id'], ch, req.if_match)
    st = f' → {ch["status"]}' if js_truthy(ch.get('status')) else ' updated'
    log_activity(ctx, 'project', p['id'], 'cr.updated', f'CR-{js_str(coalesce(prop(cr, "number"), "?"))} “{js_str(out["title"])}”{st}')
    return out


def decide_change_request(req: Req) -> Any:
    ctx = req.ctx
    cr = find(ctx, 'change_requests', req.params['id'], 'Change request')
    p = _cr_project(ctx, cr['project_id'])
    if cr.get('status') not in ('raised', 'estimated'):
        err(409, 'CR_DECIDED', 'This change request has already been decided.')
    decision = req.b('decision')
    if decision != 'approve' and decision != 'reject':
        err(422, 'VALIDATION', 'Choose approve or reject.', 'decision')
    tag = f'CR-{js_str(coalesce(prop(cr, "number"), "?"))}'
    me = ctx.user['id']
    if decision == 'reject':
        reason = js_trim(js_str(coalesce(req.b('reason'), '')))
        if js_len(reason) < 5:
            err(422, 'VALIDATION', 'Say why it is rejected (at least 5 characters). The client sees this in the change log.', 'reason')
        note = ' · '.join(js_str(x) for x in [cr.get('note'), f'Rejected: {reason}'] if js_truthy(x))
        out = update(ctx, 'change_requests', cr['id'], {'status': 'rejected', 'decided_by': me, 'decided_at': now_iso(), 'note': note})
        log_activity(ctx, 'project', p['id'], 'cr.rejected', f'{tag} rejected: {js_str(cr["title"])} ({reason})')
        if cr.get('raised_by') != me:
            notify(ctx, cr['raised_by'], 'NTF-06', f'{tag} rejected', f'{js_str(cr["title"])} on {js_str(p["name"])}: {reason}', f'/projects/{p["id"]}/changes')
        return out
    if cr['hours'] <= 0:
        err(422, 'VALIDATION', 'Estimate the effort (hours) before approving, so the plan and margin stay honest.', 'hours')
    ms_id: Any = None
    bm = req.b('milestone')
    if js_truthy(bm):
        name = _need(_g(bm, 'name'), 'milestone.name', 'Milestone name')
        end = _need_date(_g(bm, 'planned_end'), 'milestone.planned_end', 'Milestone end')
        base = max_iso(ctx.today, p['start_on'])
        start = end if base > end else base
        no = _next_ms_no(ctx, p['id'], True)
        m = insert(
            ctx,
            'milestones',
            {
                'project_id': p['id'],
                'no': no,
                'name': name,
                'planned_start': start,
                'planned_end': end,
                'baseline_end': end,
                'billing_amount_paise': cr['value_paise'],
                'billing_status': 'none',
                'achieved_on': None,
                'empty_since': ctx.today,
                'deleted_at': None,
            },
        )
        ms_id = m['id']
        if end > p['end_on']:
            update(ctx, 'projects', p['id'], {'end_on': end})
        log_activity(ctx, 'project', p['id'], 'milestone.created', f'Added M{js_str(no)} {name} from {tag}')
    add_value = req.b('add_to_contract') is not False
    if add_value and cr['value_paise'] > 0:
        cur = find(ctx, 'projects', p['id'])
        update(ctx, 'projects', p['id'], {'contract_value_paise': cur['contract_value_paise'] + cr['value_paise']})
    out = update(
        ctx,
        'change_requests',
        cr['id'],
        {'status': 'approved', 'decided_by': me, 'decided_at': now_iso(), 'milestone_id': coalesce(ms_id, prop(cr, 'milestone_id'))},
    )
    extra = f'; contract +{fmt_money(cr["value_paise"])}' if add_value and js_truthy(cr['value_paise']) else ''
    log_activity(ctx, 'project', p['id'], 'cr.approved', f'{tag} approved: {js_str(cr["title"])}{extra}')
    if cr.get('raised_by') != me:
        notify(ctx, cr['raised_by'], 'NTF-05', f'{tag} approved', f'{js_str(cr["title"])} on {js_str(p["name"])}', f'/projects/{p["id"]}/changes')
    return out


def add_cr_tasks(req: Req) -> Any:
    ctx = req.ctx
    cr = find(ctx, 'change_requests', req.params['id'], 'Change request')
    p = _cr_project(ctx, cr['project_id'])
    tag = f'CR-{js_str(coalesce(prop(cr, "number"), "?"))}'
    if cr.get('status') not in ('approved', 'done'):
        err(409, 'CR_NOT_APPROVED', f'{tag} isn’t approved yet. Nothing enters the plan without an approved change request.')
    if js_truthy(cr.get('tasks_added_on')):
        err(409, 'CR_TASKS_ADDED', f'{tag}’s tasks were already added to the WBS on {fmt_date(cr["tasks_added_on"][:10])}.')
    pt = _proposed_tasks(ctx, req.b('tasks'))
    tasks_in = coalesce(pt, prop(cr, 'proposed_tasks'), [])
    if not len(tasks_in):
        err(422, 'VALIDATION', 'Add at least one task (title and hours).', 'tasks')
    c = cal(ctx)
    start = ctx.today
    bs = req.b('start_on')
    if bs is not UNDEF and bs is not None and bs != '':
        asked = _need_date(bs, 'start_on', 'Start')
        if asked < ctx.today:
            err(422, 'VALIDATION', 'Start the change request’s work today or later.', 'start_on')
        start = asked
    start = max_iso(start, p['start_on'])
    i = 0
    while i < 14 and not is_working_day(start, c):
        start = add_days(start, 1)
        i += 1
    bmi = req.b('milestone_id')
    if bmi == 'new' or (not js_truthy(bmi) and not js_truthy(cr.get('milestone_id'))):
        no = _next_ms_no(ctx, p['id'], True)
        m = insert(
            ctx,
            'milestones',
            {
                'project_id': p['id'],
                'no': no,
                'name': _tasks._slice16(f'{tag} {js_str(cr["title"])}', 120),
                'planned_start': start,
                'planned_end': start,
                'baseline_end': start,
                'billing_amount_paise': 0 if js_truthy(cr.get('milestone_id')) else cr['value_paise'],
                'billing_status': 'none',
                'achieved_on': None,
                'empty_since': None,
                'deleted_at': None,
            },
        )
        log_activity(ctx, 'project', p['id'], 'milestone.created', f'Added M{js_str(no)} {m["name"]} for {tag}')
    else:
        id_ = js_or(bmi, prop(cr, 'milestone_id'))
        m = next((x for x in rows(ctx, 'milestones') if x['id'] == id_ and x['project_id'] == p['id'] and not js_truthy(x.get('deleted_at'))), None)
        if m is None:
            err(422, 'VALIDATION', 'Choose a milestone of this project, or a new one for the change request.', 'milestone_id')
    mid = m['id']
    was_empty = not any(x.get('milestone_id') == mid for x in rows(ctx, 'tasks'))
    created: list[dict] = []
    me = ctx.user['id']
    per_day = js_max(1, prop(ctx.tenant['settings'], 'calendar.min_hours_per_day') * 0.75)
    for x in tasks_in:
        days = js_max(1, js_ceil(x['est_hours'] / per_day))
        due = add_working_days(start, js_max(0, days - 1), c)
        code = _new_task_code(ctx, p['id'], m)
        t = insert(
            ctx,
            'tasks',
            {
                'project_id': p['id'],
                'milestone_id': mid,
                'no': code['no'],
                'wbs_code': code['wbs_code'],
                'title': x['title'],
                'owner_id': x['owner_id'],
                'backup_id': None,
                'est_hours': js_or(x['est_hours'], 8),
                'start_on': start,
                'due_on': due,
                'done_on': None,
                'complexity': 'medium',
                'status': 'todo',
                'description': f'From {tag}: {js_str(cr["title"])}',
                'blocked_reason': None,
                'kind': 'project',
                'assigned_by': me,
                'review_round': 0,
                'priority': 'normal',
                'stage': 'new',
                'progress': 0,
                'tag': 'feature',
                'actual_start_on': None,
                'baseline_due_on': due,
                'target_revisions': [],
                'slip_flagged_on': None,
                'slip_note': None,
                'cr_id': cr['id'],
            },
        )
        created.append(t)
        if js_truthy(x['owner_id']) and x['owner_id'] != me:
            notify(
                ctx,
                x['owner_id'],
                'NTF-14',
                'Task assigned to you',
                f'{code["wbs_code"]} {js_str(x["title"])} · {js_str(p["name"])} ({tag})',
                f'/tasks/{t["id"]}',
            )
        start = add_working_days(due, 1, c)
    ch: dict[str, Any] = {'empty_since': None}
    m = find_by('milestones', 'id', mid) or m
    if js_truthy(m.get('achieved_on')):
        ch['achieved_on'] = None
    m = patch('milestones', m, ch)
    sync_milestone_dates(ctx, mid, was_empty)
    last_due = '0000-01-01'
    for t in created:
        last_due = max_iso(last_due, t['due_on'])
    if last_due > p['end_on']:
        warn(
            ctx,
            'CR_PAST_END',
            f'{tag}’s tasks run to {fmt_date(last_due)}, {working_days_between(add_days(p["end_on"], 1), last_due, c)} working days after the planned end ({fmt_date(p["end_on"])}). Agree the new end date with the client.',
        )
    out = update(ctx, 'change_requests', cr['id'], {'tasks_added_on': now_iso(), 'milestone_id': mid})
    m = find_by('milestones', 'id', mid) or m
    n = len(created)
    log_activity(ctx, 'project', p['id'], 'cr.tasks_added', f'{tag}: {n} task{"" if n == 1 else "s"} added to M{js_str(m["no"])} {js_str(m["name"])}')
    return {'change_request': out, 'milestone_id': mid, 'tasks': n}


def project_activity(req: Req) -> Any:
    ctx = req.ctx
    p = _get_project(ctx, req.params['id'])
    task_ids = {t['id'] for t in rows(ctx, 'tasks') if t['project_id'] == p['id']}
    hist = [
        {
            'id': h['id'],
            'kind': 'status',
            'at': h['changed_at'],
            'actor_id': prop(h, 'changed_by'),
            'title': f'{PROJECT_STATUS[h["from_status"]]["label"]} → {PROJECT_STATUS[h["to_status"]]["label"]}'
            if js_truthy(h.get('from_status'))
            else f'Created as {PROJECT_STATUS[h["to_status"]]["label"]}',
            'detail': prop(h, 'reason'),
            'action': 'project.status',
        }
        for h in rows(ctx, 'project_status_history')
        if h['project_id'] == p['id']
    ]
    logs = [
        {
            'id': lg['id'],
            'kind': 'log',
            'at': lg['at'],
            'actor_id': prop(lg, 'actor_id'),
            'title': prop(lg, 'summary'),
            'detail': None,
            'action': prop(lg, 'action'),
        }
        for lg in rows(ctx, 'activity_log')
        if (lg.get('entity') == 'project' and lg.get('entity_id') == p['id'] and lg.get('action') != 'project.status')
        or (lg.get('entity') == 'task' and lg.get('entity_id') in task_ids and lg.get('action') == 'comment.created')
    ]
    return sorted([*hist, *logs], key=lambda a: locale_key(a['at']), reverse=True)[:300]


def client_view(req: Req) -> Any:
    ctx = req.ctx
    return _client_view(ctx, _get_project(ctx, req.params['id']))


def import_wbs(req: Req) -> Any:
    ctx = req.ctx
    p = _editable_project(ctx, req.params['id'])
    if req.q('mode') == 'commit':
        return _import_commit(ctx, p, js_str(coalesce(req.b('token'), '')))
    return _import_preview(ctx, p, js_str(coalesce(req.b('text'), '')))


# ---------- openings (UI-35) ----------
_OPEN_STATUS = ['open', 'interviewing', 'offered', 'filled', 'on_hold']
_OPEN_RANK = {'open': 0, 'interviewing': 1, 'offered': 2, 'on_hold': 3, 'filled': 4}


def _opening_guard(ctx: Ctx, o: dict | None = None) -> None:
    if not has(ctx, 'openings.manage'):
        err(403, 'FORBIDDEN', 'Only admins, project managers and operations manage openings.')
    if o is not None and not has_any(ctx, 'projects.manage_all', 'people.view_all'):
        pid = o.get('project_id')
        pr = next((x for x in rows(ctx, 'projects') if x['id'] == pid), None) if js_truthy(pid) else None
        me = ctx.user['id']
        if o.get('owner_id') != me and prop(pr, 'pm_id') != me and o.get('created_by') != me:
            err(403, 'FORBIDDEN', 'You can change openings you own or that are on your projects.')


def _validate_opening(ctx: Ctx, b: Any, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    bb = b if isinstance(b, dict) else {}
    if not partial or 'role' in bb:
        out['role'] = _need(bb.get('role', UNDEF), 'role', 'Role')
    if not partial or 'count' in bb:
        n = js_number(bb.get('count', UNDEF))
        if not is_integer(n) or n < 1 or n > 50:
            err(422, 'VALIDATION', 'Count must be between 1 and 50.', 'count')
        out['count'] = _intify(n)
    if not partial or 'status' in bb:
        s = coalesce(bb.get('status', UNDEF), 'open')
        if not _in(s, _OPEN_STATUS):
            err(422, 'VALIDATION', 'Choose a valid status.', 'status')
        out['status'] = s
    if not partial or 'owner_id' in bb:
        if not js_truthy(bb.get('owner_id', UNDEF)):
            err(422, 'VALIDATION', 'Choose who owns this opening.', 'owner_id')
        out['owner_id'] = _active_user(ctx, bb['owner_id'], 'owner_id')['id']  # type: ignore[index]
    if not partial or 'project_id' in bb:
        pid = js_or(bb.get('project_id', UNDEF), None)
        if js_truthy(pid):
            find(ctx, 'projects', pid, 'Project')
        out['project_id'] = pid
    if not partial or 'needed_by' in bb:
        d = js_or(bb.get('needed_by', UNDEF), None)
        if js_truthy(d) and not _iso_test(d):
            err(422, 'VALIDATION', 'Needed by must be a date.', 'needed_by')
        out['needed_by'] = d
    if not partial or 'notes' in bb:
        out['notes'] = js_trim(js_str(coalesce(bb.get('notes', UNDEF), '')))
    return out


def _opening_view(ctx: Ctx, o: dict) -> dict[str, Any]:
    pid = o.get('project_id')
    pr = next((x for x in rows(ctx, 'projects') if x['id'] == pid), None) if js_truthy(pid) else None
    return {**o, 'project_code': coalesce(prop(pr, 'code'), None), 'project_name': coalesce(prop(pr, 'name'), None)}


def list_openings(req: Req) -> Any:
    ctx = req.ctx
    _opening_guard(ctx)
    return sorted(
        (_opening_view(ctx, o) for o in rows(ctx, 'openings')),
        key=lambda o: (_OPEN_RANK[o['status']], locale_key(coalesce(prop(o, 'needed_by'), '9999'))),
    )


def add_opening(req: Req) -> Any:
    ctx = req.ctx
    _opening_guard(ctx)
    v = _validate_opening(ctx, req.body, False)
    o = insert(ctx, 'openings', v)
    log_activity(ctx, 'opening', o['id'], 'opening.created', f'Opening: {js_str(o["count"])} × {js_str(o["role"])}')
    if o['owner_id'] != ctx.user['id']:
        notify(ctx, o['owner_id'], 'NTF-14', 'Opening assigned to you', f'{js_str(o["count"])} × {js_str(o["role"])}', '/openings')
    return o


def patch_opening(req: Req) -> Any:
    ctx = req.ctx
    o = find(ctx, 'openings', req.params['id'], 'Opening')
    _opening_guard(ctx, o)
    out = update(ctx, 'openings', o['id'], _validate_opening(ctx, req.body, True), req.if_match)
    log_activity(ctx, 'opening', o['id'], 'opening.updated', f'Opening {js_str(out["role"])}: {js_str(out["status"]).replace("_", " ", 1)}')
    return out


def delete_opening(req: Req) -> Any:
    ctx = req.ctx
    o = find(ctx, 'openings', req.params['id'], 'Opening')
    _opening_guard(ctx, o)
    remove(ctx, 'openings', o['id'])
    log_activity(ctx, 'opening', o['id'], 'opening.deleted', f'Removed opening {js_str(o["role"])}')
    return None


def register(r: Any) -> None:
    # v5 costing routes first: static paths (/projects/costing/preview, /projects/calendar-summary) before /projects/:id
    register_costing_routes(r, CostingDeps(can_view, can_edit))
    r.get('/projects', list_projects)
    r.post('/projects', create_project)
    r.get('/projects/:id', get_project_detail)
    r.patch('/projects/:id', patch_project)
    r.post('/projects/:id/status', set_project_status)
    r.post('/projects/:id/milestones', add_milestone)
    r.patch('/milestones/:id', patch_milestone)
    r.delete('/milestones/:id', delete_milestone)
    r.post('/projects/:id/tasks', add_project_task)
    r.patch('/tasks/:id', patch_task)
    r.post('/tasks/:id/status', set_task_status)
    r.put('/tasks/:id/dependencies', set_dependencies)
    r.delete('/tasks/:id', delete_task)
    r.get('/projects/:id/schedule', get_schedule)
    r.get('/projects/:id/load', get_load)
    r.post('/projects/:id/allocations', add_allocation)
    r.patch('/allocations/:id', patch_allocation)
    r.delete('/allocations/:id', delete_allocation)
    r.get('/projects/:id/money', get_money)
    r.get('/invoices/next-number', invoice_next_number)
    r.post('/milestones/:id/invoice', raise_invoice)
    r.post('/invoices/:id/payments', record_payment)
    r.get('/projects/:id/change-requests', list_change_requests)
    r.post('/projects/:id/change-requests', add_change_request)
    r.patch('/change-requests/:id', patch_change_request)
    r.post('/change-requests/:id/decision', decide_change_request)
    r.post('/change-requests/:id/add-tasks', add_cr_tasks)
    r.get('/projects/:id/activity', project_activity)
    r.get('/projects/:id/client-view', client_view)
    r.post('/projects/:id/import', import_wbs)
    r.get('/openings', list_openings)
    r.post('/openings', add_opening)
    r.patch('/openings/:id', patch_opening)
    r.delete('/openings/:id', delete_opening)
