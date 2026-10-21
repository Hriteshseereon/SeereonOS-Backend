"""Port of src/mock/routes/tasks.ts — Module C: task detail (drawer), comments with @mentions, "My tasks", the Tasks tab,
the shared Create task form, watch / definition-of-done / attachments, mentions feed, estimate hint and delivery risks.

Exported helpers (snake_case of the TS exports, used by other route modules): milestone_map, wbs_of, freeze_wbs, to_ref,
on_leave, covering_for, TASK_RELATION_LABEL, concerned_manager_of, relation_of, can_see_task, task_access, visible_task,
task_detail.

`freeze_wbs` stores the code with a copy-on-write patch (the TS mutates the row in place): callers re-read the row.
"""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, Req, err, find, has, insert, log_activity, notify, rows, scope, update, user_name
from app.domain.calendar import CalendarCtx, add_days, add_working_days, fmt_date, week_start
from app.domain.delivery import STAGE, progress_of, stage_of
from app.domain.labels import COMPLEXITY
from app.engine.analytics import ensure_swept, estimate_hint, open_blockers, schedule_of, stage_since, task_actuals
from app.engine.performance import team_of
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_len,
    js_max,
    js_min,
    js_number,
    js_or,
    js_round,
    js_sort,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    numeric_key,
    prop,
)
from app.routes import projects as _projects
from app.routes import taskflow as _taskflow

TASK_RELATION_LABEL = cat('mock/routes/tasks')['TASK_RELATION_LABEL']

_ISO_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
# keys every JS object answers `k in obj` for (Object.prototype)
_PROTO_KEYS = frozenset(
    {
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
    }
)


def js_in(key: Any, obj: Any) -> bool:
    """`key in obj` for a plain JS object literal (own keys + Object.prototype members)"""
    k = js_str(key)
    return k in obj or k in _PROTO_KEYS


def _b(body: Any, k: str) -> Any:
    return body.get(k, UNDEF) if isinstance(body, dict) else UNDEF


def _slice16(s: str, n: int) -> str:
    """s.slice(0, n) in UTF-16 code units"""
    if all(ord(c) <= 0xFFFF for c in s):
        return s[:n]
    out = []
    units = 0
    for c in s:
        w = 2 if ord(c) > 0xFFFF else 1
        if units + w > n:
            if units < n:
                o = ord(c) - 0x10000
                out.append(chr(0xD800 + (o >> 10)))
            break
        out.append(c)
        units += w
    return ''.join(out)


def _user(ctx: Ctx, id_: Any) -> dict | None:
    """rows(ctx, 'users').find(u => u.id === id) (None when missing)"""
    if not isinstance(id_, str):
        return None
    tid = ctx.tenant_id
    for u in scope().group('users', 'id').get(id_, ()):
        if u.get('tenant_id') == tid and not scope().is_stub(u):
            return u
    return None


def _project(ctx: Ctx, id_: Any) -> dict | None:
    """rows(ctx, 'projects').find(p => p.id === id)"""
    if not isinstance(id_, str):
        return None
    tid = ctx.tenant_id
    for p in scope().group('projects', 'id').get(id_, ()):
        if p.get('tenant_id') == tid:
            return p
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Shared helpers (also used by timesheets.ts)
# ---------------------------------------------------------------------------------------------------------------------
def milestone_map(ctx: Ctx) -> dict[str, dict]:
    return {m['id']: m for m in rows(ctx, 'milestones')}


def wbs_of(t: dict, ms: dict) -> str:
    """A task's WBS number is permanent: stored `wbs_code`, else `<milestone no>.<task no>`."""
    frozen = t.get('wbs_code')
    if js_truthy(frozen):
        return frozen
    mid = t.get('milestone_id')
    m = ms.get(mid) if js_truthy(mid) else None
    return f'{js_str(m["no"]) if m is not None and not js_truthy(m.get("deleted_at")) else "0"}.{js_str(prop(t, "no"))}'


def freeze_wbs(t: dict, ms: dict) -> str:
    """Store the task's current number permanently (call before its milestone changes). Python: copy-on-write patch of
    the task row — re-read the row afterwards."""
    from app.ctx import patch

    if not js_truthy(t.get('wbs_code')):
        code = wbs_of(t, ms)
        patch('tasks', t, {'wbs_code': code})
        return code
    return t['wbs_code']


def to_ref(t: dict, ms: dict) -> dict[str, Any]:
    return {
        'id': t['id'],
        'wbs': wbs_of(t, ms),
        'title': prop(t, 'title'),
        'status': prop(t, 'status'),
        'owner_id': prop(t, 'owner_id'),
        'due_on': prop(t, 'due_on'),
        'project_id': prop(t, 'project_id'),
    }


def on_leave(ctx: Ctx, user_id: Any, date: str) -> bool:
    """Only a FULL-day leave counts."""
    if not js_truthy(user_id):
        return False
    return any(lv['user_id'] == user_id and lv['date'] == date and lv.get('portion') != 'half' for lv in rows(ctx, 'leave'))


def covering_for(ctx: Ctx, user_id: Any, frm: str, to: str) -> dict[str, dict[str, Any]]:
    """Tasks a person covers in a date range (G7.02): task id -> {days, reason, from_user_id} (insertion order)."""
    out: dict[str, dict[str, Any]] = {}
    leave_by: dict[Any, list[str]] = {}
    for lv in rows(ctx, 'leave'):
        if lv['date'] >= frm and lv['date'] <= to and lv.get('portion') != 'half':
            leave_by.setdefault(lv['user_id'], []).append(lv['date'])
    dels = [
        d
        for d in rows(ctx, 'delegations')
        if d.get('to_user_id') == user_id and d.get('include_tasks') is not False and d['from_on'] <= to and d['to_on'] >= frm
    ]
    for t in rows(ctx, 'tasks'):
        owner = t.get('owner_id')
        if t.get('status') == 'done' or not js_truthy(owner) or owner == user_id:
            continue
        if t.get('backup_id') == user_id:
            days = sorted(leave_by.get(owner, ()))
            if days:
                out[t['id']] = {'days': list(dict.fromkeys(days)), 'reason': 'leave', 'from_user_id': owner}
                continue
        d = next(
            (x for x in dels if x.get('from_user_id') == owner and (not js_truthy(x.get('project_id')) or x.get('project_id') == t.get('project_id'))), None
        )
        if d is not None:
            ds: list[str] = []
            x = frm if d['from_on'] < frm else d['from_on']
            end = to if d['to_on'] > to else d['to_on']
            while x <= end:
                ds.append(x)
                x = add_days(x, 1)
            out[t['id']] = {'days': ds, 'reason': 'delegation', 'from_user_id': owner}
    return out


# ---------------------------------------------------------------------------------------------------------------------
# v7 task visibility (CEO point 5)
# ---------------------------------------------------------------------------------------------------------------------
def concerned_manager_of(ctx: Ctx, t: dict) -> Any:
    """Effective concerned manager: the task's own setting, else the owner's reporting manager."""
    if js_truthy(t.get('concerned_manager_id')):
        return t['concerned_manager_id']
    if not js_truthy(t.get('owner_id')):
        return None
    u = _user(ctx, t['owner_id'])
    return coalesce(prop(u, 'reports_to_id') if u is not None else UNDEF, None)


def _rel_cache(ctx: Ctx) -> dict[str, Any]:
    m = scope().memo
    k = ('routes/tasks:rel', ctx)
    c = m.get(k)
    if c is None:
        wk = week_start(ctx.today)
        me = ctx.user['id']
        tagged: set[str] = set()
        for x in rows(ctx, 'comments'):
            if me in coalesce(prop(x, 'mentions'), []) and x.get('author_id') != me:
                tagged.add(x['task_id'])
        delegated = set(covering_for(ctx, me, add_days(wk, -28), add_days(wk, 90)).keys())
        c = {'cov': covering_for(ctx, me, wk, add_days(wk, 6)), 'tagged': tagged, 'delegated': delegated}
        m[k] = c
    return c


def relation_of(ctx: Ctx, t: dict) -> str | None:
    """The viewer's relation to a task (None = unrelated)."""
    me = ctx.user_id  # = ctx.user['id'] (a row's id never changes)
    if t.get('owner_id') == me:
        return 'owner'
    c = _rel_cache(ctx)
    if t['id'] in c['cov']:
        return 'covering'
    if t.get('backup_id') == me:
        return 'backup'
    if _taskflow.reviewer_of(ctx, t) == me or t.get('reviewer_id') == me:
        return 'reviewer'
    if concerned_manager_of(ctx, t) == me:
        return 'concerned_manager'
    p = _project(ctx, t.get('project_id'))
    if p is not None and p.get('pm_id') == me and not _taskflow.is_general(ctx, t):
        return 'project_manager'
    if t.get('assigned_by') == me:
        return 'assigned_by'
    if me in coalesce(prop(t, 'watcher_ids'), []):
        return 'watcher'
    if t['id'] in c['tagged']:
        return 'tagged'
    if t['id'] in c['delegated']:
        return 'covering'
    return None


def can_see_task(ctx: Ctx, t: dict) -> bool:
    """v7 rule: `tasks.view_all` sees every task; everyone else only tasks they are related to."""
    return has(ctx, 'tasks.view_all') or relation_of(ctx, t) is not None


def task_access(ctx: Ctx, t: dict) -> dict[str, bool]:
    p = _project(ctx, t.get('project_id'))
    me = ctx.user['id']
    can_edit = (p is not None and _projects.can_edit(ctx, p)) or _taskflow.can_manage_general(ctx, t)
    mine = t.get('owner_id') == me or t.get('backup_id') == me
    reviewer = _taskflow.reviewer_of(ctx, t) == me
    visible = can_edit or can_see_task(ctx, t)

    def on_project() -> bool:
        return p is not None and (p['id'] in _projects.my_project_ids(ctx) or _projects.can_view(ctx, p))

    can_change_status = can_edit or mine or _taskflow.can_review_task(ctx, t)
    can_comment = visible and (can_edit or mine or reviewer or relation_of(ctx, t) is not None or on_project())
    return {'visible': visible, 'can_edit': can_edit, 'can_change_status': can_change_status, 'can_comment': can_comment}


def visible_task(ctx: Ctx, id_: Any) -> dict:
    t = find(ctx, 'tasks', id_, 'Task')
    if not task_access(ctx, t)['visible']:
        err(
            403,
            'TASK_NOT_VISIBLE',
            'This task isn’t yours. You can see tasks you own, back up or cover, review, manage or watch; the project plan still lists it.',
        )
    return t


def _deps(ctx: Ctx) -> list[dict]:
    return rows(ctx, 'task_dependencies')


def task_detail(ctx: Ctx, t: dict) -> dict[str, Any]:
    ms = milestone_map(ctx)
    p = find(ctx, 'projects', t['project_id'], 'Project')
    m = ms.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
    all_ = rows(ctx, 'tasks')
    by_id = {x['id']: x for x in all_}
    deps = _deps(ctx)
    dependencies = [x for x in (by_id.get(d['depends_on_task_id']) for d in deps if d['task_id'] == t['id']) if x is not None]
    dependents = [x for x in (by_id.get(d['task_id']) for d in deps if d['depends_on_task_id'] == t['id']) if x is not None]
    act = task_actuals(ctx).get(t['id'])
    access = task_access(ctx, t)
    me = ctx.user['id']
    detail = {
        'task': t,
        'wbs': wbs_of(t, ms),
        'project': {'id': p['id'], 'name': prop(p, 'name'), 'code': prop(p, 'code'), 'status': prop(p, 'status'), 'pm_id': prop(p, 'pm_id')},
        'milestone': {'id': m['id'], 'no': m['no'], 'name': m['name']} if m is not None and not js_truthy(m.get('deleted_at')) else None,
        'dependencies': [to_ref(x, ms) for x in dependencies],
        'dependents': [to_ref(x, ms) for x in dependents],
        'blockers': [to_ref(x, ms) for x in open_blockers(ctx, t['id'])],
        'actual_hours': coalesce(act['hours'] if act else UNDEF, 0),
        'approved_hours': coalesce(act['approved'] if act else UNDEF, 0),
        'can_edit': access['can_edit'],
        'can_change_status': access['can_change_status'],
        'can_comment': access['can_comment'],
        # the DELETE /tasks/:id rule (project editors); hours logged -> 409, the UI offers Cancel instead
        'can_delete': _projects.can_edit(ctx, p),
        'milestones': [
            {'id': x['id'], 'no': x['no'], 'name': x['name']}
            for x in sorted((x for x in rows(ctx, 'milestones') if x['project_id'] == p['id'] and not js_truthy(x.get('deleted_at'))), key=lambda x: x['no'])
        ],
        'project_tasks': sorted((to_ref(x, ms) for x in all_ if x['project_id'] == p['id'] and x['id'] != t['id']), key=lambda r: numeric_key(r['wbs'])),
        'owner_on_leave': on_leave(ctx, t.get('owner_id'), ctx.today),
    }
    st = _sched_index(ctx, t['project_id']).get(t['id'])
    wk0 = week_start(ctx.today)
    cov_me = covering_for(ctx, me, wk0, add_days(wk0, 6)).get(t['id'])
    v5 = {
        'stage': stage_of(t),
        'progress': progress_of(t),
        'stage_since': stage_since(ctx, t),
        'forecast_start': coalesce(prop(st, 'forecast_start'), None),
        'forecast_end': coalesce(prop(st, 'forecast_end'), None),
        'float_days': prop(st, 'float_days') if st is not None else None,
        'critical': js_truthy(prop(st, 'critical')),
        'waiting_on': [_link_of(ctx, x, ms, coalesce(prop(st, 'forecast_start'), t['start_on']), 'waits') for x in dependencies],
        'blocks': [_link_of(ctx, x, ms, coalesce(prop(st, 'forecast_end'), t['due_on']), 'blocks') for x in dependents],
        'cancelled': js_truthy(t.get('cancelled_on')),
        'downstream_ids': _downstream_of(ctx, t['id']),
        'owner_leave_days': _owner_leave_days(ctx, t),
        'covering_me': {'days': cov_me['days'], 'reason': cov_me['reason'], 'from_user_id': cov_me['from_user_id']} if cov_me is not None else None,
        'can_log_time': t.get('status') != 'done' and (t.get('owner_id') == me or t.get('backup_id') == me or cov_me is not None),
        'comments_count': sum(1 for c in rows(ctx, 'comments') if c['task_id'] == t['id']),
    }
    review = {
        'reviewer_id': _taskflow.reviewer_of(ctx, t),
        'can_submit': _taskflow.can_submit(ctx, t),
        'can_review': t.get('status') == 'in_review' and _taskflow.can_review_task(ctx, t),
        'self_review': _taskflow.self_review(ctx, t),
        'late_now': t.get('status') != 'done' and ctx.today > t['due_on'],
        'late_by_days': _taskflow.late_by(ctx, t['due_on'], ctx.today) if t.get('status') != 'done' else 0,
        'reviews': _taskflow.reviews_of(ctx, t['id']),
        'reschedules': _taskflow.reschedules_of(ctx, t['id']),
        'assigned_by': coalesce(prop(t, 'assigned_by'), None),
    }
    return {**detail, 'review': review, **v5, **_task_v7(ctx, t, all_, ms, access)}


def _task_v7(ctx: Ctx, t: dict, all_: list[dict], ms: dict, access: dict) -> dict[str, Any]:
    """v7 professional fields on the task page / drawer."""
    me = ctx.user['id']
    parent = next((x for x in all_ if x['id'] == t['parent_task_id']), None) if js_truthy(t.get('parent_task_id')) else None
    children = [x for x in all_ if x.get('parent_task_id') == t['id']]
    next_occ = next((x for x in all_ if x.get('recurrence_of') == t['id']), None)
    reviewer = _taskflow.reviewer_of(ctx, t)
    involved = t.get('owner_id') == me or t.get('backup_id') == me or reviewer == me
    rel = relation_of(ctx, t)
    rec_of: Any = None
    if js_truthy(t.get('recurrence_of')):
        r = next((x for x in all_ if x['id'] == t['recurrence_of']), None)
        rec_of = to_ref(r, ms) if r is not None else None
    criteria = coalesce(prop(t, 'acceptance_criteria'), [])
    if parent is not None:
        parent_v: Any = to_ref(parent, ms) if can_see_task(ctx, parent) else {**to_ref(parent, ms), 'title': 'A task you can’t open'}
    else:
        parent_v = None
    return {
        'relation': rel,
        'relation_label': TASK_RELATION_LABEL[rel] if rel else None,
        'concerned_manager_id': concerned_manager_of(ctx, t),
        'concerned_manager_default': not js_truthy(t.get('concerned_manager_id')),
        'watcher_ids': coalesce(prop(t, 'watcher_ids'), []),
        'watching': me in coalesce(prop(t, 'watcher_ids'), []),
        'acceptance_criteria': criteria,
        'criteria_done': sum(1 for c in criteria if js_truthy(prop(c, 'done'))),
        'labels': coalesce(prop(t, 'labels'), []),
        'billable': coalesce(prop(t, 'billable'), not _taskflow.is_general(ctx, t)),
        'recurrence': coalesce(prop(t, 'recurrence'), 'none'),
        'recurrence_of': rec_of,
        'next_occurrence': to_ref(next_occ, ms) if next_occ is not None else None,
        'attachments': [{**a} for a in coalesce(prop(t, 'attachments'), [])],
        'parent': parent_v,
        'children': [to_ref(x, ms) for x in children],
        'priority': coalesce(prop(t, 'priority'), 'normal'),
        'can_tick_criteria': t.get('status') != 'done' and (access['can_edit'] or involved),
        'can_attach': access['can_comment'],
        'can_create_followup': access['can_edit'] or t.get('owner_id') == me or reviewer == me or has(ctx, 'tasks.assign_team'),
    }


# ---------------- v6 helpers ----------------
def _downstream_of(ctx: Ctx, id_: str) -> list[str]:
    """Every task that (transitively) depends on this one."""
    deps = _deps(ctx)
    out: dict[str, None] = {}
    stack = [id_]
    while stack:
        cur = stack.pop()
        for d in deps:
            if d['depends_on_task_id'] == cur and d['task_id'] not in out:
                out[d['task_id']] = None
                stack.append(d['task_id'])
    out.pop(id_, None)
    return list(out)


def _owner_leave_days(ctx: Ctx, t: dict) -> list[str]:
    """The owner's leave days in the current week (Mon–Sun)."""
    if not js_truthy(t.get('owner_id')) or t.get('status') == 'done':
        return []
    wk = week_start(ctx.today)
    end = add_days(wk, 6)
    return sorted(set(lv['date'] for lv in rows(ctx, 'leave') if lv['user_id'] == t['owner_id'] and lv['date'] >= wk and lv['date'] <= end))


# ---------------- v5 helpers ----------------
def _sched_index(ctx: Ctx, project_id: Any) -> dict[str, dict]:
    sc = schedule_of(ctx, project_id)['schedule']
    return {x['id']: x for x in (sc['tasks'] if sc is not None else [])}


def _link_of(ctx: Ctx, x: dict, ms: dict, pivot: str, kind: str) -> dict[str, Any]:
    st = _sched_index(ctx, x['project_id']).get(x['id'])
    done = x.get('status') == 'done'
    f_end = coalesce(prop(x, 'done_on'), x['due_on']) if done else coalesce(prop(st, 'forecast_end'), x['due_on'])
    return {
        'id': x['id'],
        'wbs': wbs_of(x, ms),
        'title': prop(x, 'title'),
        'owner_id': prop(x, 'owner_id'),
        'stage': stage_of(x),
        'progress': progress_of(x),
        'done': done,
        'start_on': prop(x, 'start_on'),
        'due_on': prop(x, 'due_on'),
        'done_on': prop(x, 'done_on'),
        'forecast_start': coalesce(prop(st, 'forecast_start'), x['start_on']),
        'forecast_end': f_end,
        'held_up': (not done and f_end >= pivot) if kind == 'waits' else (not done and x['start_on'] <= pivot),
    }


def _my_task(ctx: Ctx, t: dict, ms: dict, projects: dict, is_backup: bool) -> dict[str, Any]:
    p = projects.get(t['project_id'])
    m = ms.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
    sent_back: Any = None
    if t.get('status') != 'in_review':
        rv = _taskflow.reviews_of(ctx, t['id'])
        last = rv[-1] if rv else None
        if last is not None and last.get('decision') == 'sent_back':
            sent_back = {
                'reason': coalesce(prop(last, 'decision_reason'), ''),
                'by': prop(last, 'decided_by'),
                'at': prop(last, 'decided_at'),
                'round': prop(last, 'round'),
            }
    act = task_actuals(ctx).get(t['id'])
    return {
        'stage': stage_of(t),
        'progress': progress_of(t),
        'tag': coalesce(prop(t, 'tag'), None),
        'backup_id': prop(t, 'backup_id'),
        'can_submit': _taskflow.can_submit(ctx, t),
        'can_review': _taskflow.can_review_task(ctx, t)
        if t.get('status') == 'in_review'
        else (_taskflow.can_review_task(ctx, t) and not _taskflow.self_review(ctx, t)),
        **to_ref(t, ms),
        'project_name': coalesce(prop(p, 'name'), '—'),
        'project_code': coalesce(prop(p, 'code'), ''),
        'milestone_label': f'M{js_str(m["no"])} {js_str(m["name"])}' if m is not None and not js_truthy(m.get('deleted_at')) else None,
        'est_hours': prop(t, 'est_hours'),
        'actual_hours': coalesce(act['hours'] if act else UNDEF, 0),
        'complexity': prop(t, 'complexity'),
        'is_backup': is_backup,
        'blocked_by': [to_ref(x, ms) for x in open_blockers(ctx, t['id'])],
        'reviewer_id': _taskflow.reviewer_of(ctx, t),
        'review_round': coalesce(prop(t, 'review_round'), 0),
        'submitted_on': coalesce(prop(t, 'submitted_on'), None),
        'general': _taskflow.is_general(ctx, t),
        'sent_back': sent_back,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------------------------------
def create_options(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user['id']
    gen = _taskflow.general_project_id(ctx)
    _taskflow.ensure_general_project(ctx)
    team = [u['id'] for u in team_of(ctx, me)]
    ms_all = rows(ctx, 'milestones')
    ms_by_id = {m['id']: m for m in ms_all}
    all_tasks = rows(ctx, 'tasks')
    admin = has(ctx, 'projects.manage_all')
    projects_ = [
        p for p in rows(ctx, 'projects') if p['id'] == gen or (p.get('status') in ('active', 'initiated') and _taskflow.create_rights(ctx, p, None)['ok'])
    ]
    projects_ = sorted(projects_, key=lambda p: (0, ()) if p['id'] == gen else (1, locale_key(p['code'])))
    out_projects = []
    for p in projects_:
        general = p['id'] == gen
        live = sorted((m for m in ms_all if m['project_id'] == p['id'] and not js_truthy(m.get('deleted_at'))), key=lambda m: m['no'])
        tasks = sorted(
            (
                {
                    'id': t['id'],
                    'wbs': wbs_of(t, ms_by_id),
                    'title': prop(t, 'title'),
                    'status': prop(t, 'status'),
                    'owner_id': prop(t, 'owner_id'),
                    'milestone_id': prop(t, 'milestone_id'),
                }
                for t in all_tasks
                if t['project_id'] == p['id'] and not js_truthy(t.get('cancelled_on')) and (not general or can_see_task(ctx, t))
            ),
            key=lambda r: numeric_key(r['wbs']),
        )
        if general:
            team_ids: list = []
        else:
            seen: dict[Any, None] = {}
            for a in rows(ctx, 'allocations'):
                if a['project_id'] == p['id']:
                    seen.setdefault(a['user_id'], None)
            for t in all_tasks:
                if t['project_id'] == p['id'] and js_truthy(t.get('owner_id')):
                    seen.setdefault(t['owner_id'], None)
            team_ids = list(seen)
        out_projects.append(
            {
                'id': p['id'],
                'code': prop(p, 'code'),
                'name': prop(p, 'name'),
                'general': general,
                'status': prop(p, 'status'),
                'start_on': prop(p, 'start_on'),
                'end_on': prop(p, 'end_on'),
                'pm_id': prop(p, 'pm_id'),
                'anyone': admin or (not general and _taskflow.create_rights(ctx, p, None)['ok']),
                'team_only': general and not admin,
                'milestones': [
                    {
                        'id': m['id'],
                        'no': m['no'],
                        'name': m['name'],
                        'planned_start': m['planned_start'],
                        'planned_end': m['planned_end'],
                        'next_wbs': f'{js_str(m["no"])}.{js_str(_projects.next_task_no(ctx, p["id"], m["id"], False))}',
                    }
                    for m in live
                ],
                'milestone_required': not general and len(live) > 0,
                'next_wbs_none': f'0.{js_str(_projects.next_task_no(ctx, p["id"], None, False))}',
                'tasks': tasks,
                'team_ids': team_ids,
            }
        )
    labels_seen: dict[Any, None] = {}
    for t in all_tasks:
        if can_see_task(ctx, t):
            for lb in coalesce(prop(t, 'labels'), []):
                labels_seen.setdefault(lb, None)
    labels = js_sort(list(labels_seen))
    settings = ctx.tenant['settings']
    productive: dict[str, Any] = {}
    for u in rows(ctx, 'users'):
        if u.get('status') == 'active':
            productive[u['id']] = js_or(js_number(coalesce(prop(u, 'productive_hours'), settings.get('team.default_productive_hours', UNDEF), 6)), 6)
    return {
        'me': me,
        'my_manager_id': coalesce(prop(ctx.user, 'reports_to_id'), None),
        'general_project_id': gen,
        'view_all': has(ctx, 'tasks.view_all'),
        'can_assign_team': has(ctx, 'tasks.assign_team'),
        'team_ids': team,
        'projects': out_projects,
        'labels': labels,
        'min_hours_per_day': js_or(js_number(coalesce(settings.get('calendar.min_hours_per_day', UNDEF), 9)), 9),
        'productive_hours': productive,
    }


def create_task(req: Req) -> Any:
    ctx = req.ctx
    t = _taskflow.create_task_core(ctx, req.body)
    return {**t, 'wbs': wbs_of(t, milestone_map(ctx))}


def watch(req: Req) -> Any:
    ctx = req.ctx
    t = visible_task(ctx, req.params['id'])
    me = ctx.user['id']
    on = req.b('watch') is not False
    cur = coalesce(prop(t, 'watcher_ids'), [])
    nxt = list(dict.fromkeys([*cur, me])) if on else [x for x in cur if x != me]
    update(ctx, 'tasks', t['id'], {'watcher_ids': nxt})
    log_activity(
        ctx, 'task', t['id'], 'task.watch' if on else 'task.unwatch', f'{js_str(ctx.user.get("name", UNDEF))} {"started" if on else "stopped"} watching'
    )
    return task_detail(ctx, find(ctx, 'tasks', t['id']))


def criteria(req: Req) -> Any:
    ctx = req.ctx
    t = visible_task(ctx, req.params['id'])
    d = task_detail(ctx, t)
    if not d['can_tick_criteria']:
        err(
            403,
            'FORBIDDEN',
            'This task is approved. Reopen it to change its definition of done.'
            if t.get('status') == 'done'
            else 'Only the owner, backup, reviewer or project manager can tick the definition of done.',
        )
    lst = list(coalesce(prop(t, 'acceptance_criteria'), []))
    i = js_number(req.b('index'))
    if not is_integer(i) or i < 0 or i >= len(lst):
        err(422, 'VALIDATION', 'That checklist item no longer exists. Reload the task.', 'index')
    i = int(i)
    done = req.b('done') is True
    lst[i] = {**lst[i], 'done': done}
    update(ctx, 'tasks', t['id'], {'acceptance_criteria': lst})
    n = sum(1 for x in lst if js_truthy(prop(x, 'done')))
    log_activity(ctx, 'task', t['id'], 'task.criteria', f'{"Ticked" if done else "Unticked"} “{js_str(prop(lst[i], "text"))}” ({n}/{len(lst)} done)')
    return task_detail(ctx, find(ctx, 'tasks', t['id']))


def add_attachment(req: Req) -> Any:
    ctx = req.ctx
    t = visible_task(ctx, req.params['id'])
    if not task_access(ctx, t)['can_comment']:
        err(403, 'FORBIDDEN', 'Only people involved in this task can attach files.')
    lst = coalesce(prop(t, 'attachments'), [])
    if len(lst) >= 8:
        err(422, 'VALIDATION', 'A task holds up to 8 files. Remove one first.', 'attachments')
    a = _taskflow.valid_attachment(ctx, req.body)
    t = update(ctx, 'tasks', t['id'], {'attachments': [*lst, a]})
    log_activity(ctx, 'task', t['id'], 'task.attachment', f'Attached {a["name"]}')
    me = ctx.user['id']
    for uid_ in [t.get('owner_id'), _taskflow.reviewer_of(ctx, t)]:
        if js_truthy(uid_) and uid_ != me:
            notify(
                ctx,
                uid_,
                'NTF-14',
                'File attached to a task',
                f'{js_str(ctx.user.get("name", UNDEF))} attached {a["name"]} to {wbs_of(t, milestone_map(ctx))} {js_str(prop(t, "title"))}',
                f'/tasks/{t["id"]}',
            )
    return task_detail(ctx, find(ctx, 'tasks', t['id']))


def remove_attachment(req: Req) -> Any:
    ctx = req.ctx
    t = visible_task(ctx, req.params['id'])
    a = next((x for x in coalesce(prop(t, 'attachments'), []) if x.get('id') == req.params['aid']), None)
    if a is None:
        err(404, 'NOT_FOUND', 'That file was already removed.')
    if a.get('uploaded_by') != ctx.user['id'] and not task_access(ctx, t)['can_edit']:
        err(403, 'FORBIDDEN', 'Only the person who attached the file or the project manager can remove it.')
    update(ctx, 'tasks', t['id'], {'attachments': [x for x in coalesce(prop(t, 'attachments'), []) if x.get('id') != a.get('id')]})
    log_activity(ctx, 'task', t['id'], 'task.attachment', f'Removed {js_str(prop(a, "name"))}')
    return task_detail(ctx, find(ctx, 'tasks', t['id']))


def _cap_first(s: str) -> str:
    """s.replace(/^./, c => c.toUpperCase())"""
    if not s or s[0] in '\n\r  ':
        return s
    return s[0].upper() + s[1:]


def list_tasks(req: Req) -> Any:
    ctx = req.ctx
    ensure_swept(ctx)
    me = ctx.user['id']
    ms = milestone_map(ctx)
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    users = {u['id']: u for u in rows(ctx, 'users')}
    act = task_actuals(ctx)
    comments: dict[str, int] = {}
    tagged: set[str] = set()
    for c in rows(ctx, 'comments'):
        comments[c['task_id']] = comments.get(c['task_id'], 0) + 1
        if me in coalesce(prop(c, 'mentions'), []) and c.get('author_id') != me:
            tagged.add(c['task_id'])
    wk_raw = js_str(coalesce(req.q('week'), ''))
    wk = week_start(wk_raw) if _ISO_RE.fullmatch(wk_raw) else week_start(ctx.today)
    wk_end = add_days(wk, 6)
    cov = covering_for(ctx, me, wk, wk_end)
    leave_by: dict[Any, list[str]] = {}
    for lv in rows(ctx, 'leave'):
        if lv['date'] >= wk and lv['date'] <= wk_end:
            leave_by[lv['user_id']] = sorted(set([*leave_by.get(lv['user_id'], []), lv['date']]))
    view_all = has(ctx, 'tasks.view_all')
    all_rows = rows(ctx, 'tasks')
    visible = [t for t in all_rows if t['project_id'] in projects and (can_see_task(ctx, t) or t['id'] in cov)]
    manager = view_all or has(ctx, 'tasks.assign_team') or any(_projects.can_edit(ctx, p) for p in rows(ctx, 'projects'))
    person_raw = js_str(coalesce(req.q('person'), ''))
    person = '' if person_raw == 'all' or not person_raw else (me if person_raw == 'me' else person_raw)
    depts = {d['id']: d for d in rows(ctx, 'departments')}

    def dept_key(uid_: Any) -> str:
        u = users.get(uid_) if js_truthy(uid_) else None
        if u is None:
            return ''
        dep = u.get('department_id')
        return dep if js_truthy(dep) and dep in depts else f'kind:{js_str(prop(u, "department"))}'

    def dept_label(key: str) -> Any:
        if key.startswith('kind:'):
            return _cap_first(key[5:])
        d = depts.get(key)
        return coalesce(prop(d, 'name'), key)

    department = js_str(coalesce(req.q('department'), ''))
    label = js_str(coalesce(req.q('label'), ''))
    group = 'employee' if js_str(coalesce(req.q('group'), '')) == 'employee' else ''
    wk_mon = week_start(ctx.today)
    project = js_str(coalesce(req.q('project'), ''))
    milestone = js_str(coalesce(req.q('milestone'), ''))
    legacy = js_str(coalesce(req.q('stage'), ''))
    summary = ['todo', 'in_progress', 'blocked', 'in_review', 'done']
    status = js_str(coalesce(req.q('status'), ''))
    stage = legacy
    if not status and legacy in ('open', 'all', 'done', 'in_review'):
        status = legacy
        stage = ''
    if not status:
        status = 'open'
    if stage and not js_in(stage, STAGE):
        stage = ''
    chip = js_str(coalesce(req.q('chip'), ''))
    tag = js_str(coalesce(req.q('tag'), ''))
    cx = js_str(coalesce(req.q('complexity'), ''))
    overdue_only = js_str(coalesce(req.q('overdue'), '')) == '1'
    q = js_trim(js_str(coalesce(req.q('q'), ''))).lower()
    s_idx: dict[Any, dict] = {}

    def s_of(pid: Any) -> dict:
        v = s_idx.get(pid)
        if v is None:
            v = _sched_index(ctx, pid)
            s_idx[pid] = v
        return v

    by_id = {t['id']: t for t in all_rows}
    dep_by_task: dict[Any, list[dict]] = {}
    for d in rows(ctx, 'task_dependencies'):
        dep_by_task.setdefault(d['task_id'], []).append(d)
    counts = {'all': 0, 'mine': 0, 'covering': 0, 'tagged': 0, 'overdue': 0, 'blocked': 0}
    stage_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    items: list[dict] = []
    by_emp: dict[Any, dict] = {}
    dept_facet: dict[str, int] = {}
    label_facet: dict[Any, int] = {}
    today = ctx.today
    for t in visible:
        p = projects.get(t['project_id'])
        if p is None:
            continue
        sg = stage_of(t)
        dk = dept_key(t.get('owner_id'))
        if dk:
            dept_facet[dk] = dept_facet.get(dk, 0) + 1
        t_labels = coalesce(prop(t, 'labels'), [])
        for lb in t_labels:
            label_facet[lb] = label_facet.get(lb, 0) + 1
        if project and t['project_id'] != project:
            continue
        if department and dk != department:
            continue
        if label and label not in t_labels:
            continue
        if milestone and (js_truthy(t.get('milestone_id')) if milestone == 'none' else t.get('milestone_id') != milestone):
            continue
        if tag and coalesce(prop(t, 'tag'), '') != tag:
            continue
        if cx and t.get('complexity') != cx:
            continue
        if q:
            owner_u = users.get(coalesce(prop(t, 'owner_id'), ''))
            hay = (
                f'{wbs_of(t, ms)} {js_str(prop(t, "title"))} {js_str(prop(p, "code"))} {js_str(prop(p, "name"))} {js_str(coalesce(prop(owner_u, "name"), ""))}'
            )
            if q not in hay.lower():
                continue
        st_ = t.get('status')
        is_overdue = st_ != 'done' and st_ != 'in_review' and t['due_on'] < today
        if overdue_only and not is_overdue:
            continue
        deps = [x for x in (by_id.get(d['depends_on_task_id']) for d in dep_by_task.get(t['id'], ())) if x is not None]
        open_deps = [] if st_ == 'done' else [d for d in deps if d.get('status') != 'done']
        is_blocked = st_ != 'done' and (st_ == 'blocked' or len(open_deps) > 0)
        person_ok = not person or t.get('owner_id') == person
        if person_ok or group:
            key = coalesce(prop(t, 'owner_id'), '')
            g = by_emp.get(key)
            if g is None:
                g = {'user_id': prop(t, 'owner_id'), 'open': 0, 'overdue': 0, 'done_week': 0, 'total': 0, 'est_open': 0}
            g['total'] += 1
            if st_ != 'done':
                g['open'] += 1
                g['est_open'] += t['est_hours']
            if is_overdue:
                g['overdue'] += 1
            if st_ == 'done' and coalesce(prop(t, 'done_on'), '') >= wk_mon:
                g['done_week'] += 1
            by_emp[key] = g
        if status == 'all':
            status_ok = True
        elif status == 'open':
            status_ok = st_ != 'done'
        elif status in summary:
            status_ok = st_ == status
        else:
            status_ok = True
        stage_ok = not stage or sg == stage
        if person_ok and stage_ok:
            status_counts[st_] = status_counts.get(st_, 0) + 1
        if person_ok and status_ok:
            stage_counts[sg] = stage_counts.get(sg, 0) + 1
        if not status_ok or not stage_ok:
            continue
        is_cov = t['id'] in cov
        is_tagged = t['id'] in tagged
        if person_ok:
            counts['all'] += 1
        if t.get('owner_id') == me:
            counts['mine'] += 1
        if is_cov:
            counts['covering'] += 1
        if is_tagged:
            counts['tagged'] += 1
        if person_ok and is_overdue:
            counts['overdue'] += 1
        if person_ok and is_blocked:
            counts['blocked'] += 1
        if chip == 'mine':
            skip = t.get('owner_id') != me
        elif chip == 'covering':
            skip = not is_cov
        elif chip == 'tagged':
            skip = not is_tagged
        else:
            skip = not person_ok
        if skip:
            continue
        if chip == 'overdue' and not is_overdue:
            continue
        if chip == 'blocked' and not is_blocked:
            continue
        m = ms.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
        m_live = m is not None and not js_truthy(m.get('deleted_at'))
        st = s_of(t['project_id']).get(t['id'])
        acc = task_access(ctx, t)

        def dep(d: dict) -> dict[str, Any]:
            return {
                'id': d['id'],
                'wbs': wbs_of(d, ms),
                'title': prop(d, 'title'),
                'owner_id': prop(d, 'owner_id'),
                'stage': stage_of(d),
                'done': d.get('status') == 'done',
            }

        c = cov.get(t['id'])
        a_ = act.get(t['id'])
        items.append(
            {
                'id': t['id'],
                'wbs': wbs_of(t, ms),
                'title': prop(t, 'title'),
                'project_id': p['id'],
                'project_code': prop(p, 'code'),
                'project_name': prop(p, 'name'),
                'project_hue': prop(p, 'color_hue'),
                'general': _taskflow.is_general(ctx, t),
                'milestone_id': m['id'] if m_live else None,
                'milestone_label': f'M{js_str(m["no"])} {js_str(m["name"])}' if m_live else None,  # type: ignore[index]
                'owner_id': prop(t, 'owner_id'),
                'backup_id': prop(t, 'backup_id'),
                'reviewer_id': _taskflow.reviewer_of(ctx, t),
                'stage': sg,
                'progress': progress_of(t),
                'status': st_,
                'tag': coalesce(prop(t, 'tag'), None),
                'complexity': prop(t, 'complexity'),
                'est_hours': prop(t, 'est_hours'),
                'logged_hours': js_round(coalesce(a_['hours'] if a_ else UNDEF, 0) * 10) / 10,
                'start_on': prop(t, 'start_on'),
                'due_on': prop(t, 'due_on'),
                'done_on': prop(t, 'done_on'),
                'overdue_days': _taskflow.late_by(ctx, t['due_on'], today) if is_overdue else 0,
                'forecast_end': st['forecast_end'] if st is not None and st_ != 'done' else None,
                'critical': js_truthy(prop(st, 'critical')) and st_ != 'done',
                'blocked_by': len(open_deps),
                'comments': comments.get(t['id'], 0),
                'version': prop(t, 'version'),
                'can_move': _taskflow.can_move_task(ctx, t),
                'can_edit': acc['can_edit'],
                'can_submit': _taskflow.can_submit(ctx, t),
                'can_review': _taskflow.can_review_task(ctx, t) and not _taskflow.self_review(ctx, t),
                'depends_on': [dep(d) for d in deps],
                'blocking': [dep(d) for d in open_deps],
                'owner_leave_days': leave_by.get(t['owner_id'], []) if js_truthy(t.get('owner_id')) and st_ != 'done' else [],
                'covering': {'days': c['days'], 'reason': c['reason'], 'from_user_id': c['from_user_id']} if c is not None else None,
                'tagged': is_tagged,
                'can_log_time': st_ != 'done' and (t.get('owner_id') == me or t.get('backup_id') == me or is_cov),
                'relation': 'covering' if is_cov else relation_of(ctx, t),
                'priority': coalesce(prop(t, 'priority'), 'normal'),
                'concerned_manager_id': concerned_manager_of(ctx, t),
                'watcher_ids': coalesce(prop(t, 'watcher_ids'), []),
                'labels': t_labels,
                'billable': coalesce(prop(t, 'billable'), not _taskflow.is_general(ctx, t)),
                'recurrence': coalesce(prop(t, 'recurrence'), 'none'),
                'criteria_total': len(coalesce(prop(t, 'acceptance_criteria'), [])),
                'criteria_done': sum(1 for x in coalesce(prop(t, 'acceptance_criteria'), []) if js_truthy(prop(x, 'done'))),
                'attachments': len(coalesce(prop(t, 'attachments'), [])),
                'owner_department': dept_label(dk) if dk else None,
            }
        )

    def order(x: dict) -> int:
        if x['status'] == 'done':
            return 3
        if js_truthy(x['overdue_days']):
            return 0
        return 1 if (len(x['blocking']) or x['status'] == 'blocked') else 2

    items = sorted(items, key=lambda x: (order(x), locale_key(x['due_on']), locale_key(x['project_code']), numeric_key(x['wbs'])))
    proj_ids = list(dict.fromkeys(t['project_id'] for t in visible))
    ms_ids = list(dict.fromkeys(t['milestone_id'] for t in visible if js_truthy(t.get('milestone_id'))))
    facets = {
        'projects': sorted(
            ({'id': p['id'], 'code': p['code'], 'name': p['name']} for p in (projects.get(i) for i in proj_ids) if p is not None),
            key=lambda x: locale_key(x['code']),
        ),
        'milestones': [
            {'id': m['id'], 'project_id': m['project_id'], 'label': f'M{js_str(m["no"])} {js_str(m["name"])}'}
            for m in (ms.get(i) for i in ms_ids)
            if m is not None and not js_truthy(m.get('deleted_at'))
        ],
        'people': list(dict.fromkeys(t['owner_id'] for t in visible if js_truthy(t.get('owner_id')))),
        'departments': sorted(({'id': k, 'name': dept_label(k), 'count': n} for k, n in dept_facet.items()), key=lambda x: locale_key(x['name'])),
        'labels': sorted(({'name': k, 'count': n} for k, n in label_facet.items()), key=lambda x: (-x['count'], locale_key(x['name']))),
    }
    groups = []
    for g in by_emp.values():
        uid_ = g['user_id']
        if js_truthy(uid_):
            u = users.get(uid_)
            name = coalesce(prop(u, 'name'), 'Former user')
        else:
            name = 'Unassigned'
        groups.append({**g, 'est_open': js_round(g['est_open'] * 10) / 10, 'name': name, 'department': dept_label(dept_key(uid_)) if js_truthy(uid_) else None})
    groups = sorted(groups, key=lambda g: (-g['overdue'], -g['open'], locale_key(g['name'])))
    return {
        'items': items,
        'total': len(items),
        'facets': facets,
        'wide': view_all,
        'person': person or 'all',
        'status': status,
        'stage': stage,
        'chip': chip,
        'week': wk,
        'manager': manager,
        'counts': counts,
        'status_counts': status_counts,
        'stage_counts': stage_counts,
        'view_all': view_all,
        'department': department,
        'label': label,
        'group': group,
        'groups': groups,
        'scope_note': None if view_all else 'You see tasks you own, back up or cover, review, are the concerned manager of, assigned, watch or were tagged on.',
    }


def mentions(req: Req) -> Any:
    ctx = req.ctx
    user = js_or(js_str(coalesce(req.q('user'), '')), ctx.user['id'])
    if user != ctx.user['id'] and not has(ctx, 'people.view_all'):
        err(403, 'FORBIDDEN', 'You can only see your own mentions.')
    limit = js_max(1, js_min(50, js_or(js_number(coalesce(req.q('limit'), 10)), 10)))
    ms = milestone_map(ctx)
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    out: list[dict] = []
    lst = [x for x in rows(ctx, 'comments') if user in coalesce(prop(x, 'mentions'), []) and x.get('author_id') != user]
    for c in sorted(lst, key=lambda x: locale_key(x['created_at']), reverse=True):
        t = tasks.get(c['task_id'])
        if t is None or not task_access(ctx, t)['visible']:
            continue
        p = projects.get(t['project_id'])
        out.append(
            {
                'id': c['id'],
                'task_id': t['id'],
                'wbs': wbs_of(t, ms),
                'title': prop(t, 'title'),
                'tag': coalesce(prop(t, 'tag'), None),
                'stage': stage_of(t),
                'project_id': prop(t, 'project_id'),
                'project_code': coalesce(prop(p, 'code'), ''),
                'project_name': coalesce(prop(p, 'name'), '—'),
                'author_id': prop(c, 'author_id'),
                'at': prop(c, 'created_at'),
                'body': prop(c, 'body'),
            }
        )
        if len(out) >= limit:
            break
    return out


def estimate_hint_route(req: Req) -> Any:
    ctx = req.ctx
    cx = js_str(coalesce(req.q('complexity'), 'medium'))
    if not js_in(cx, COMPLEXITY):
        err(422, 'VALIDATION', 'Choose a complexity.', 'complexity')
    est = js_number(coalesce(req.q('est'), 0))
    return estimate_hint(ctx, js_or(req.q('owner_id'), None), cx, est if is_finite(est) and est > 0 else None)


def delivery_risks(req: Req) -> Any:
    ctx = req.ctx
    ensure_swept(ctx)
    me = ctx.user['id']
    pid = js_str(coalesce(req.q('project_id'), ''))
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    if pid:
        p0 = find(ctx, 'projects', pid, 'Project')
        if not _projects.can_view(ctx, p0):
            err(403, 'FORBIDDEN', 'You don’t have access to this project.')
        scope_ = [t for t in rows(ctx, 'tasks') if t['project_id'] == pid]
    else:

        def keep(t: dict) -> bool:
            pp = projects.get(t['project_id'])
            if coalesce(prop(pp, 'status'), '') not in ('active', 'initiated'):
                return False
            return _projects.can_edit(ctx, pp) or _taskflow.can_manage_general(ctx, t) or t.get('owner_id') == me or t.get('backup_id') == me  # type: ignore[arg-type]

        scope_ = [t for t in rows(ctx, 'tasks') if keep(t)]
    ms = milestone_map(ctx)
    act = task_actuals(ctx)
    comments: dict[str, int] = {}
    for c in rows(ctx, 'comments'):
        comments[c['task_id']] = comments.get(c['task_id'], 0) + 1
    all_tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    deps = rows(ctx, 'task_dependencies')
    horizon = add_working_days(ctx.today, 10, CalendarCtx(ctx.tenant['settings'], rows(ctx, 'holidays')))
    today = ctx.today
    out: list[dict] = []
    for t in scope_:
        if t.get('status') == 'done' or (t.get('status') == 'in_review' and t['due_on'] >= today):
            continue
        st = _sched_index(ctx, t['project_id']).get(t['id'])
        if st is None:
            continue
        sg = stage_of(t)
        a_ = act.get(t['id'])
        logged = coalesce(a_['hours'] if a_ else UNDEF, 0)
        prog = progress_of(t)
        reasons: list[str] = []
        breached = t['due_on'] < today
        if breached:
            reasons.append(f'Overdue since {fmt_date(t["due_on"])}')
        forecast_late = st['forecast_end'] > t['due_on']
        if forecast_late and not breached:
            reasons.append(f'Forecast end {fmt_date(st["forecast_end"])}, target {fmt_date(t["due_on"])}')
        waiting_stage = STAGE[sg]['group'] == 'waiting'
        since = stage_since(ctx, t) if waiting_stage else None
        if waiting_stage:
            reasons.append(f'{STAGE[sg]["label"]} since {fmt_date(since)}')
        est_h = t['est_hours']
        over = logged > est_h and est_h > 0
        logged_r = js_round(logged * 10) / 10
        if over:
            reasons.append(f'Logged {js_str(logged_r)}h of {js_str(est_h)}h estimate at {js_str(prog)}%')
        should_start = STAGE[sg]['group'] == 'not_started' and t['start_on'] < today
        if should_start:
            reasons.append(f'Should have started {fmt_date(t["start_on"])}')
        if js_truthy(t.get('slip_flagged_on')):
            reasons.append(f'Slip flagged {fmt_date(t["slip_flagged_on"])}')
        breach = st.get('breach')
        breach_ms = js_truthy(breach) and breach.get('kind') == 'milestone'
        high_risk = (
            forecast_late or waiting_stage or over or should_start or js_truthy(t.get('slip_flagged_on')) or (js_truthy(t.get('milestone_id')) and breach_ms)
        )
        critical = st['critical']
        soon = st['forecast_start'] <= horizon
        if not breached and not high_risk and not (js_truthy(critical) and soon):
            continue
        waits = [x for x in (all_tasks.get(d['depends_on_task_id']) for d in deps if d['task_id'] == t['id']) if x is not None]
        blocks = [x for x in (all_tasks.get(d['task_id']) for d in deps if d['depends_on_task_id'] == t['id']) if x is not None]
        waiting_on = [_link_of(ctx, x, ms, st['forecast_start'], 'waits') for x in waits]
        blocks_l = [_link_of(ctx, x, ms, st['forecast_end'], 'blocks') for x in blocks]
        p = projects[t['project_id']]
        m = ms.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
        m_live = m is not None and not js_truthy(m.get('deleted_at'))
        acc = task_access(ctx, t)
        revs = coalesce(prop(t, 'target_revisions'), [])
        hold_up = next((x for x in waiting_on if x['held_up']), None)
        if breached:
            headline = f'Overdue since {fmt_date(t["due_on"])}'
        elif waiting_stage:
            headline = f'{STAGE[sg]["label"]} since {fmt_date(since)}'
        elif forecast_late:
            pre = f'Waiting on “{js_str(hold_up["title"])}” ({user_name(ctx, hold_up["owner_id"])}); f' if hold_up is not None else 'F'
            headline = f'{pre}orecast end {fmt_date(st["forecast_end"])}, target {fmt_date(t["due_on"])}'
        elif breach_ms:
            headline = breach['message']
        elif should_start:
            headline = f'Should have started {fmt_date(t["start_on"])}'
        elif js_truthy(t.get('slip_flagged_on')):
            headline = f'Slip flagged {fmt_date(t["slip_flagged_on"])}; forecast end {fmt_date(st["forecast_end"])}, target {fmt_date(t["due_on"])}'
        elif over:
            headline = f'Logged {js_str(logged_r)}h of {js_str(est_h)}h estimate at {js_str(prog)}%'
        else:
            headline = f'Forecast end {fmt_date(st["forecast_end"])}, target {fmt_date(t["due_on"])}'
        out.append(
            {
                'id': t['id'],
                'wbs': wbs_of(t, ms),
                'title': prop(t, 'title'),
                'project_id': p['id'],
                'project_code': prop(p, 'code'),
                'project_name': prop(p, 'name'),
                'general': _taskflow.is_general(ctx, t) or p['id'] == _taskflow.general_project_id(ctx),
                'milestone_id': m['id'] if m_live else None,  # type: ignore[index]
                'milestone_label': m['name'] if m_live else None,  # type: ignore[index]
                'milestone_no': m['no'] if m_live else None,  # type: ignore[index]
                'owner_id': prop(t, 'owner_id'),
                'backup_id': prop(t, 'backup_id'),
                'reviewer_id': _taskflow.reviewer_of(ctx, t),
                'complexity': prop(t, 'complexity'),
                'tag': coalesce(prop(t, 'tag'), None),
                'est_hours': est_h,
                'logged_hours': logged_r,
                'stage': sg,
                'progress': prog,
                'status': prop(t, 'status'),
                'start_on': prop(t, 'start_on'),
                'due_on': prop(t, 'due_on'),
                'baseline_due_on': coalesce(prop(t, 'baseline_due_on'), prop(revs[0], 'from') if revs else UNDEF, None),
                'revised_on': revs[-1]['at'][:10] if revs else None,
                'revisions': len(revs),
                'slip_flagged_on': coalesce(prop(t, 'slip_flagged_on'), None),
                'slip_note': coalesce(prop(t, 'slip_note'), None),
                'actual_start_on': coalesce(prop(t, 'actual_start_on'), None),
                'forecast_start': st['forecast_start'],
                'forecast_end': st['forecast_end'],
                'note': coalesce(prop(t, 'blocked_reason'), None),
                'stage_since': since,
                'breached': breached,
                'high_risk': high_risk,
                'critical': critical,
                'float_days': prop(st, 'float_days'),
                'reasons': reasons,
                'headline': headline,
                'waiting_on': waiting_on,
                'blocks': blocks_l,
                'comments': comments.get(t['id'], 0),
                'version': prop(t, 'version'),
                'can_move': _taskflow.can_move_task(ctx, t),
                'can_edit': acc['can_edit'],
                'can_submit': _taskflow.can_submit(ctx, t),
                'can_review': _taskflow.can_review_task(ctx, t) and not _taskflow.self_review(ctx, t),
            }
        )

    def rank(x: dict) -> int:
        if x['breached']:
            return 0
        if x['high_risk'] and js_truthy(x['critical']):
            return 1
        return 2 if x['high_risk'] else 3

    return sorted(out, key=lambda x: (rank(x), locale_key(x['due_on']), numeric_key(x['wbs'])))


def get_task(req: Req) -> Any:
    ctx = req.ctx
    return task_detail(ctx, visible_task(ctx, req.params['id']))


def list_comments(req: Req) -> Any:
    ctx = req.ctx
    visible_task(ctx, req.params['id'])
    tid = req.params['id']
    return sorted((c for c in rows(ctx, 'comments') if c['task_id'] == tid), key=lambda c: locale_key(c['created_at']))


def add_comment(req: Req) -> Any:
    ctx = req.ctx
    t = visible_task(ctx, req.params['id'])
    if not task_access(ctx, t)['can_comment']:
        err(403, 'FORBIDDEN', 'Only people on this project can comment on its tasks.')
    text = js_trim(js_str(coalesce(req.b('body'), '')))
    if not text:
        err(422, 'VALIDATION', 'Write a comment first.', 'body')
    if js_len(text) > 4000:
        err(422, 'VALIDATION', 'Keep comments under 4,000 characters.', 'body')
    parent = coalesce(req.b('parent_id'), None)
    if js_truthy(parent):
        pc = find(ctx, 'comments', parent, 'Comment')
        if pc['task_id'] != t['id']:
            err(422, 'VALIDATION', 'Reply to a comment on this task.', 'parent_id')
        if js_truthy(pc.get('parent_id')):
            parent = pc['parent_id']
    users = {u['id']: u for u in rows(ctx, 'users') if u.get('status') == 'active'}
    raw = req.b('mentions')
    mentions_ = list(dict.fromkeys(x for x in (raw if isinstance(raw, list) else []) if isinstance(x, str) and x in users))
    me = ctx.user['id']
    c = insert(ctx, 'comments', {'task_id': t['id'], 'body': text, 'mentions': mentions_, 'author_id': me, 'parent_id': parent})
    ms = milestone_map(ctx)
    code = wbs_of(t, ms)
    snippet = _slice16(text, 88) + '…' if js_len(text) > 90 else text
    name = js_str(ctx.user.get('name', UNDEF))
    for uid_ in mentions_:
        if uid_ != me:
            notify(ctx, uid_, 'NTF-03', 'You were mentioned', f'{name} mentioned you on {code} {js_str(prop(t, "title"))}: “{snippet}”', f'/tasks/{t["id"]}')
    log_activity(ctx, 'task', t['id'], 'comment', f'Commented on {code} {js_str(prop(t, "title"))}')
    return c


def my_tasks(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user['id']
    ms = milestone_map(ctx)
    projects = {p['id']: p for p in rows(ctx, 'projects')}

    def live_project(t: dict) -> bool:
        s = prop(projects.get(t['project_id']), 'status')
        return s != 'closed' and s != 'completed'

    all_ = [t for t in rows(ctx, 'tasks') if t.get('status') != 'done' and live_project(t)]
    tasks = [_my_task(ctx, t, ms, projects, False) for t in all_ if t.get('owner_id') == me]
    qw = req.q('week')
    wk = week_start(qw) if isinstance(qw, str) and _ISO_RE.fullmatch(qw) else week_start(ctx.today)
    cov = covering_for(ctx, me, wk, add_days(wk, 6))
    covering = [
        {**_my_task(ctx, t, ms, projects, True), 'covering_days': cov[t['id']]['days'], 'covering_reason': cov[t['id']]['reason']}
        for t in all_
        if t['id'] in cov
    ]
    mon = week_start(ctx.today)
    sun = add_days(mon, 6)
    hours: Any = 0
    for e in rows(ctx, 'timesheet_entries'):
        if e['user_id'] == me and e['work_date'] >= mon and e['work_date'] <= sun and e.get('kind') != 'leave':
            hours = hours + e['hours']
    backing = sum(1 for t in all_ if t.get('backup_id') == me and t.get('owner_id') != me)
    return {'tasks': tasks, 'covering': covering, 'hours_this_week': hours, 'backing': backing, 'week': wk}


def register(r: Any) -> None:
    r.get('/tasks/create-options', create_options)
    r.post('/tasks', create_task)
    r.post('/tasks/:id/watch', watch)
    r.post('/tasks/:id/criteria', criteria)
    r.post('/tasks/:id/attachments', add_attachment)
    r.delete('/tasks/:id/attachments/:aid', remove_attachment)
    r.get('/tasks', list_tasks)
    r.get('/tasks/mentions', mentions)
    r.get('/tasks/estimate-hint', estimate_hint_route)
    r.get('/delivery/risks', delivery_risks)
    r.get('/tasks/:id', get_task)
    r.get('/tasks/:id/comments', list_comments)
    r.post('/tasks/:id/comments', add_comment)
    r.get('/my-tasks', my_tasks)
