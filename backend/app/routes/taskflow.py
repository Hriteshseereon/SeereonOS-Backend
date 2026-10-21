"""Port of src/mock/routes/taskflow.ts — v3 task governance: review flow (submit / approve / send back), v5 stages,
progress, slip flags and cancel, manager-assigned tasks, the v7 create rule (create_task_core) and delivery analytics.

Exported helpers (snake_case of the TS exports): review_step_label, general_project_id, ensure_general_project, is_top,
wide_task_scope, is_general, reviewer_of, is_manager_of, can_manage_general, can_submit, self_review, can_review_task,
reviews_of, reschedules_of, late_by, approve_task, can_move_task, assert_can_move, apply_stage, task_analytics,
RECURRENCES, create_rights, valid_attachment, create_task_core, spawn_next_occurrence.

JS identity: the TS `update()` changes the row object in place, so a handler that keeps reading `t` after updating it
sees the NEW values. The ports use the row returned by update() wherever the TS reads `t` after an update (a few
messages therefore print the new due date twice, exactly like the reference).
"""

from __future__ import annotations

import calendar as _calendar
import random
import re
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    err,
    find,
    has,
    insert,
    log_activity,
    notify,
    now_iso,
    patch,
    permissions_of,
    rows,
    rows_where_ro,
    scope,
    update,
    user_name,
    warn,
)
from app.domain.calendar import add_days, fmt_date, is_real_iso_date, is_working_day
from app.domain.delivery import STAGE, TASK_STAGES, progress_of, stage_of, status_for_stage
from app.engine.analytics import cal, open_blockers, sync_milestone_dates
from app.engine.doc_markup import _obj_get
from app.engine.performance import DELAY_REASON_LABEL, task_stats, task_timeliness, team_of, timeliness_of, working_day_variance
from app.engine.workflows import advance_approval, ensure_wf, start_approval, step_label, wf_clear, wf_patch
from app.js import (  # noqa: I001
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
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    mark_volatile,
    prop,
)
from app.routes import projects as _projects
from app.routes import tasks as _tasks

RECURRENCES = cat('mock/routes/taskflow')['RECURRENCES']
PRIORITIES = ('low', 'normal', 'high', 'urgent')
_P_CODES = {'P1': 'urgent', 'P2': 'high', 'P3': 'normal', 'P4': 'low'}
_TAG_KEYS = ('feature', 'bug', 'improvement', 'documentation', 'support', 'meeting')
_MAX_ATTACH_BYTES = 750 * 1024
_ACTIVE_GROUPS = frozenset({'active', 'testing'})
_DATA_URL_RE = re.compile(r'^data:[\w.+-]+/[\w.+-]+;base64,', re.ASCII)
_PROOF_RE = re.compile(r'^https?://\S+\.\S+')


def _b(body: Any, k: str) -> Any:
    return body.get(k, UNDEF) if isinstance(body, dict) else UNDEF


def _iso_test(s: Any) -> bool:
    return is_real_iso_date(s)


def review_step_label(t: dict) -> str | None:
    """ "Step 2 of 2 · Delivery head review" for multi-step task reviews (None otherwise)."""
    return step_label(t) if t.get('status') == 'in_review' else None


# =====================================================================================================================
# Helpers (shared with projects / tasks)
# =====================================================================================================================
def general_project_id(ctx: Ctx) -> str:
    return f'p_general_{js_str(ctx.tenant.get("slug", UNDEF))}'


def ensure_general_project(ctx: Ctx) -> dict:
    """Tenants created after the demo seed get their General & admin project on first use."""
    id_ = general_project_id(ctx)
    cur = _tasks._project(ctx, id_)
    if cur is not None:
        return cur
    admin = next(
        (u for u in rows(ctx, 'users') if u.get('status') == 'active' and 'projects.manage_all' in permissions_of(u) and not js_truthy(u.get('reports_to_id'))),
        None,
    )
    if admin is None:
        admin = ctx.user
    return insert(
        ctx,
        'projects',
        {
            'id': id_,
            'code': 'GEN',
            'name': 'General & admin',
            'client': 'Internal',
            'type': 'service',
            'status': 'active',
            'pm_id': admin['id'],
            'start_on': add_days(ctx.today, -365),
            'end_on': add_days(ctx.today, 3650),
            'contract_value_paise': 0,
            'billing_model': 'fixed',
            'stream': 'services',
            'description': 'Internal work assigned by line managers: admin, compliance, learning, presales support, internal tooling.',
            'priority': 3,
            'deal_id': None,
            'baseline_end_on': add_days(ctx.today, 3650),
            'color_hue': 210,
        },
    )


def _user_of(ctx: Ctx, id_: Any) -> dict | None:
    return _tasks._user(ctx, id_) if js_truthy(id_) else None


def is_top(ctx: Ctx, id_: Any) -> bool:
    """The top person: reports to nobody and can manage every project (may review their own work)."""
    u = _user_of(ctx, id_)
    if u is None or js_truthy(u.get('reports_to_id')):
        return False
    memo = scope().memo
    k = ('routes/taskflow:top', id(u))
    hit = memo.get(k)
    if hit is not None and hit[0] is u:
        return hit[1]
    v = 'projects.manage_all' in permissions_of(u)
    memo[k] = (u, v)
    return v


def wide_task_scope(ctx: Ctx) -> bool:
    """Company-wide task oversight: anyone who can see everyone's people records."""
    return has(ctx, 'people.view_all')


def is_general(ctx: Ctx, t: dict) -> bool:
    return t.get('kind') == 'general' or t.get('project_id') == general_project_id(ctx)


def reviewer_of(ctx: Ctx, t: dict) -> Any:
    """Reviewer = task.reviewer_id ?? project PM ?? assigner / owner's manager; the review workflow while in review."""
    wfr = t.get('wf_approver_id')
    if t.get('status') == 'in_review' and js_truthy(wfr):
        return wfr
    return _base_reviewer_of(ctx, t)


def _base_reviewer_of(ctx: Ctx, t: dict) -> Any:
    owner = _user_of(ctx, t.get('owner_id'))
    r = coalesce(prop(t, 'reviewer_id'), None)
    if not js_truthy(r):
        p = _tasks._project(ctx, t.get('project_id'))
        if not is_general(ctx, t) and js_truthy(prop(p, 'pm_id')):
            r = p['pm_id']  # type: ignore[index]
        else:
            r = coalesce(prop(t, 'assigned_by'), prop(owner, 'reports_to_id'), prop(p, 'pm_id'), None)
    if js_truthy(r) and r == t.get('owner_id') and not is_top(ctx, r):
        r = coalesce(prop(owner, 'reports_to_id'), None)
    return r


def _team_ids(ctx: Ctx, manager_id: Any) -> set:
    """ids of team_of(ctx, manager_id), memoised per request while the users list is unchanged"""
    users = rows(ctx, 'users')
    memo = scope().memo
    k = ('routes/taskflow:team', manager_id)
    hit = memo.get(k)
    if hit is not None and hit[0] is users:
        return hit[1]
    v = {u['id'] for u in team_of(ctx, manager_id)}
    memo[k] = (users, v)
    return v


def is_manager_of(ctx: Ctx, manager_id: Any, user_id: Any) -> bool:
    if not js_truthy(user_id):
        return False
    return user_id in _team_ids(ctx, manager_id)


def can_manage_general(ctx: Ctx, t: dict) -> bool:
    """Line-manager rights on a general task: assigner, reviewer or the owner's manager."""
    if not is_general(ctx, t):
        return False
    me = ctx.user['id']
    return has(ctx, 'projects.manage_all') or t.get('assigned_by') == me or reviewer_of(ctx, t) == me or is_manager_of(ctx, me, t.get('owner_id'))


def can_submit(ctx: Ctx, t: dict) -> bool:
    me = ctx.user['id']
    return (t.get('owner_id') == me or t.get('backup_id') == me) and t.get('status') in ('todo', 'in_progress', 'blocked')


def self_review(ctx: Ctx, t: dict) -> bool:
    return t.get('owner_id') == ctx.user['id'] and not is_top(ctx, ctx.user['id'])


def can_review_task(ctx: Ctx, t: dict) -> bool:
    if self_review(ctx, t):
        return False
    return has(ctx, 'projects.manage_all') or reviewer_of(ctx, t) == ctx.user['id']


def _assert_reviewer(ctx: Ctx, t: dict) -> None:
    if self_review(ctx, t):
        err(403, 'TASK_SELF_REVIEW', 'You can’t review your own task. Your manager or the project manager approves it.')
    if not can_review_task(ctx, t):
        err(403, 'FORBIDDEN', f'Only {user_name(ctx, reviewer_of(ctx, t))} (the reviewer) or an admin can review this task.')


def reviews_of(ctx: Ctx, task_id: Any) -> list[dict]:
    return sorted((r for r in rows(ctx, 'task_reviews') if r['task_id'] == task_id), key=lambda r: r['round'])


def _open_review(ctx: Ctx, t: dict) -> dict | None:
    rs = reviews_of(ctx, t['id'])
    last = rs[-1] if rs else None
    return last if last is not None and not js_truthy(last.get('decision')) else None


def reschedules_of(ctx: Ctx, task_id: Any) -> list[dict]:
    lst = [a for a in rows_where_ro(ctx, 'activity_log', 'entity_id', task_id) if a.get('entity') == 'task' and a.get('action') == 'task.rescheduled']
    return [{'at': a['at'], 'actor_id': prop(a, 'actor_id'), 'summary': prop(a, 'summary')} for a in sorted(lst, key=lambda a: locale_key(a['at']))]


def late_by(ctx: Ctx, due: str, on: str) -> int:
    """Working days late if finished on `on` (0 when on/before due)."""
    return working_day_variance(ctx, due, on) if on > due else 0


def _code(ctx: Ctx, t: dict) -> str:
    mid = t.get('milestone_id')
    m = next((x for x in rows(ctx, 'milestones') if x['id'] == mid and not js_truthy(x.get('deleted_at'))), None) if js_truthy(mid) else None
    return f'{js_str(m["no"]) if m is not None else "0"}.{js_str(prop(t, "no"))}'


def _text(v: Any) -> str:
    return js_trim(v) if isinstance(v, str) else ''


def _name(ctx: Ctx) -> str:
    return js_str(ctx.user.get('name', UNDEF))


def approve_task(ctx: Ctx, t: dict, rating: Any, note: Any, direct: bool = False) -> dict:
    """Approve: done_on = date of the latest submission, timeliness against due_on."""
    q = js_number(rating)
    if not is_integer(q) or q < 1 or q > 5:
        err(422, 'VALIDATION', 'Rate the quality of the work from 1 to 5 stars.', 'quality_rating')
    if js_truthy(note) and js_len(note) > 1000:
        err(422, 'VALIDATION', 'Keep the note under 1,000 characters.', 'note')
    me = ctx.user['id']
    rv = _open_review(ctx, t)
    if rv is None:
        rs = reviews_of(ctx, t['id'])
        rnd = coalesce(prop(rs[-1], 'round') if rs else UNDEF, 0) + 1
        rv = insert(
            ctx,
            'task_reviews',
            {
                'task_id': t['id'],
                'round': rnd,
                'submitted_by': coalesce(prop(t, 'owner_id'), me),
                'submitted_at': now_iso(),
                'submission_note': f'Marked done directly by {_name(ctx)}' if direct else '',
                'proof_url': None,
                'delay_reason_code': None,
                'delay_reason': None,
                'decision': None,
                'decided_by': None,
                'decided_at': None,
                'decision_reason': None,
                'quality_rating': None,
            },
        )
    # (t.status === 'in_review' && t.submitted_on) || rv.submitted_at.slice(0, 10) || ctx.today
    done_on = js_or(prop(t, 'submitted_on') if t.get('status') == 'in_review' else False, rv['submitted_at'][:10], ctx.today)
    variance = working_day_variance(ctx, t['due_on'], done_on)
    timeliness = timeliness_of(variance)
    update(
        ctx,
        'task_reviews',
        rv['id'],
        {'decision': 'approved', 'decided_by': me, 'decided_at': now_iso(), 'decision_reason': js_or(note, None), 'quality_rating': q},
    )
    out = update(
        ctx,
        'tasks',
        t['id'],
        {
            'status': 'done',
            'done_on': done_on,
            'submitted_on': coalesce(prop(t, 'submitted_on'), done_on),
            'approved_by': me,
            'approved_on': ctx.today,
            'timeliness': timeliness,
            'variance_days': variance,
            'quality_rating': q,
            'review_round': rv['round'],
            'blocked_reason': None,
            'stage': 'completed',
            'progress': 100,
            'actual_start_on': coalesce(prop(t, 'actual_start_on'), t['start_on']),
        },
    )
    _projects.sync_achieved(ctx, t.get('milestone_id'))
    c = _code(ctx, out)
    tl = f'early by {js_str(-variance)}d' if timeliness == 'early' else f'late by {js_str(variance)}d' if timeliness == 'late' else 'on time'
    note_s = f': {js_str(note)}' if js_truthy(note) else ''
    log_activity(ctx, 'task', t['id'], 'task.approved', f'Approved (round {js_str(rv["round"])}, {js_str(q)}/5, {tl}){note_s}')
    log_activity(ctx, 'project', t['project_id'], 'task.status', f'{c} {js_str(out["title"])}: approved by {_name(ctx)}')
    if js_truthy(t.get('owner_id')) and t['owner_id'] != me:
        notify(
            ctx,
            t['owner_id'],
            'NTF-05',
            'Task approved',
            f'{_name(ctx)} approved {c} {js_str(out["title"])} · {js_str(q)}/5 · finished {tl}.',
            f'/tasks/{t["id"]}',
        )
    for d in [d for d in rows(ctx, 'task_dependencies') if d['depends_on_task_id'] == t['id']]:
        nt = next((x for x in rows(ctx, 'tasks') if x['id'] == d['task_id']), None)
        if nt is not None and js_truthy(nt.get('owner_id')) and nt['owner_id'] != me and len(open_blockers(ctx, nt['id'])) == 0:
            notify(
                ctx,
                nt['owner_id'],
                'NTF-14',
                'You can start a task',
                f'{_code(ctx, nt)} {js_str(nt["title"])} is unblocked: {c} is done.',
                f'/tasks/{nt["id"]}',
            )
    told = {me, coalesce(prop(t, 'owner_id'), '')}
    for uid_ in [prop(t, 'concerned_manager_id'), *coalesce(prop(t, 'watcher_ids'), [])]:
        if js_truthy(uid_) and uid_ not in told:
            told.add(uid_)
            notify(ctx, uid_, 'NTF-05', 'Task completed', f'{c} {js_str(out["title"])} was approved by {_name(ctx)} ({tl}).', f'/tasks/{t["id"]}')
    spawn_next_occurrence(ctx, out)
    return out


# =====================================================================================================================
# v5 task stages
# =====================================================================================================================
def _move_rights(ctx: Ctx, t: dict) -> dict[str, Any]:
    p = find(ctx, 'projects', t['project_id'], 'Project')
    editor = _projects.can_edit(ctx, p) or can_manage_general(ctx, t)
    me = ctx.user['id']
    today = ctx.today
    delegate = any(
        d.get('to_user_id') == me
        and d.get('from_user_id') == t.get('owner_id')
        and d.get('include_tasks') is not False
        and (not js_truthy(d.get('project_id')) or d.get('project_id') == t.get('project_id'))
        and d['from_on'] <= today
        and d['to_on'] >= today
        for d in rows(ctx, 'delegations')
    )
    return {
        'p': p,
        'editor': editor,
        'ok': editor or delegate or t.get('owner_id') == me or t.get('backup_id') == me or can_review_task(ctx, t),
    }


def can_move_task(ctx: Ctx, t: dict) -> bool:
    return _move_rights(ctx, t)['ok']


def assert_can_move(ctx: Ctx, t: dict) -> dict[str, Any]:
    r = _move_rights(ctx, t)
    if not r['ok']:
        err(403, 'FORBIDDEN', 'Only the task owner, their backup or the project manager can change it.')
    return {'p': r['p'], 'editor': r['editor']}


def _blocker_message(ctx: Ctx, t: dict) -> str | None:
    open_ = open_blockers(ctx, t['id'])
    if not open_:
        return None
    lst = ', '.join(f'{_code(ctx, d)} {js_str(d["title"])} ({user_name(ctx, d.get("owner_id"))}, {STAGE[stage_of(d)]["label"]})' for d in open_)
    return f'This task waits on {lst}. Finish {"it" if len(open_) == 1 else "them"} first or remove the dependency.'


def apply_stage(ctx: Ctx, t: dict, stage: Any, opts: dict | None = None) -> dict:
    """Move a task to a detailed stage (status derived with status_for_stage)."""
    o = opts or {}
    r = assert_can_move(ctx, t)
    p, editor = r['p'], r['editor']
    if not isinstance(stage, str) or stage not in STAGE:
        err(422, 'VALIDATION', 'Choose a valid stage.', 'stage')
    cur = stage_of(t)
    note_raw = o.get('note', UNDEF)
    note = js_trim(note_raw) if isinstance(note_raw, str) else ''
    me = ctx.user['id']
    if t.get('status') == 'in_review':
        if stage == 'completed' and can_review_task(ctx, t):
            err(409, 'USE_REVIEW_FLOW', 'This task is waiting for review. Approve it (with a rating) or send it back.', 'stage')
        err(
            409,
            'USE_REVIEW_FLOW',
            f"This task is waiting for {user_name(ctx, reviewer_of(ctx, t))}'s review. The reviewer approves it or sends it back.",
            'stage',
        )
    if stage == 'completed':
        if t.get('status') == 'done':
            return t
        if self_review(ctx, t) or not can_review_task(ctx, t):
            err(
                409,
                'USE_REVIEW_FLOW',
                f'Tasks are completed through review. Submit it for review — {user_name(ctx, reviewer_of(ctx, t))} approves it.',
                'stage',
            )
        msg = _blocker_message(ctx, t)
        if msg:
            err(409, 'TASK_BLOCKED', msg, 'stage')
        qr = o.get('quality_rating', UNDEF)
        if qr is UNDEF or qr is None:
            err(422, 'VALIDATION', 'Rate the quality of the work (1–5) to mark it completed.', 'quality_rating')
        return approve_task(ctx, t, qr, note or None, True)
    if t.get('status') == 'done' and not editor and not can_review_task(ctx, t):
        err(403, 'FORBIDDEN', 'Only the reviewer or the project manager can reopen an approved task.')
    df = STAGE[stage]
    if df['group'] in _ACTIVE_GROUPS:
        msg = _blocker_message(ctx, t)
        if msg:
            err(409, 'TASK_BLOCKED', msg, 'stage')
    if stage == 'blocked' and js_len(note) < 3 and not js_truthy(t.get('blocked_reason')):
        err(422, 'VALIDATION', 'Say what is blocking the task (at least 3 characters).', 'note')
    if stage in ('waiting_feedback', 'on_hold') and js_len(note) < 5:
        err(
            422,
            'VALIDATION',
            'Say why the task is on hold (at least 5 characters).'
            if stage == 'on_hold'
            else 'Say whose feedback you are waiting for and on what (at least 5 characters).',
            'note',
        )
    prog = 90 if t.get('status') == 'done' else progress_of(t)
    pr = o.get('progress', UNDEF)
    if pr is not UNDEF and pr is not None and pr != '':
        n = js_number(pr)
        if not is_integer(n) or n < 0 or n > 100:
            err(422, 'VALIDATION', 'Progress must be a whole number from 0 to 100.', 'progress')
        prog = n
    prog = js_min(99, js_max(prog, df['minProgress']))
    if stage == cur and prog == progress_of(t) and t.get('status') != 'done' and not note:
        return t
    waiting = df['group'] == 'waiting'
    reopen = {'approved_by': None, 'approved_on': None, 'timeliness': None, 'variance_days': None, 'quality_rating': None} if t.get('status') == 'done' else {}
    out = update(
        ctx,
        'tasks',
        t['id'],
        {
            'stage': stage,
            'progress': prog,
            'status': status_for_stage(stage, 'todo' if t.get('status') == 'done' else t['status']),
            'done_on': None,
            'blocked_reason': js_or(note, prop(t, 'blocked_reason')) if waiting else None,
            'actual_start_on': coalesce(prop(t, 'actual_start_on'), ctx.today if df['group'] in _ACTIVE_GROUPS else None),
            **reopen,
        },
    )
    _projects.sync_achieved(ctx, t.get('milestone_id'))
    c = _code(ctx, out)
    note_s = f': {note}' if note else ''
    log_activity(ctx, 'task', t['id'], 'task.stage', f'{STAGE[cur]["label"]} → {df["label"]} ({js_str(prog)}%){note_s}')
    log_activity(ctx, 'project', t['project_id'], 'task.status', f'{c} {js_str(out["title"])}: {df["label"]}')
    if waiting and js_truthy(p.get('pm_id')) and p['pm_id'] != me:
        notify(ctx, p['pm_id'], 'NTF-14', f'{c} is {df["label"].lower()}', f'{js_str(out["title"])}{note_s}', f'/projects/{p["id"]}/schedule')
    return out


# =====================================================================================================================
# Row builders
# =====================================================================================================================
def _reviews_by_task(reviews: list[dict]) -> dict[Any, list[dict]]:
    memo = scope().memo
    k = ('routes/taskflow:rbt', id(reviews))
    hit = memo.get(k)
    if hit is not None and hit[0] is reviews:
        return hit[1]
    g: dict[Any, list[dict]] = {}
    for r in reviews:
        g.setdefault(r['task_id'], []).append(r)
    memo[k] = (reviews, g)
    return g


def _team_row(ctx: Ctx, t: dict, projects: dict, reviews: list[dict], resched: dict) -> dict[str, Any]:
    p = projects.get(t['project_id'])
    mine = sorted(_reviews_by_task(reviews).get(t['id'], ()), key=lambda r: r['round'])
    sent_back = [r for r in mine if r.get('decision') == 'sent_back']
    tl = task_timeliness(ctx, t)
    st_ = t.get('status')
    return {
        'id': t['id'],
        'wbs': _code(ctx, t),
        'title': prop(t, 'title'),
        'status': st_,
        'owner_id': prop(t, 'owner_id'),
        'reviewer_id': reviewer_of(ctx, t),
        'assigned_by': coalesce(prop(t, 'assigned_by'), None),
        'project_id': prop(t, 'project_id'),
        'project_code': coalesce(prop(p, 'code'), ''),
        'project_name': coalesce(prop(p, 'name'), '—'),
        'general': is_general(ctx, t),
        'priority': coalesce(prop(t, 'priority'), 'normal'),
        'start_on': prop(t, 'start_on'),
        'due_on': prop(t, 'due_on'),
        'done_on': prop(t, 'done_on'),
        'submitted_on': coalesce(prop(t, 'submitted_on'), None),
        'est_hours': prop(t, 'est_hours'),
        'review_round': coalesce(prop(t, 'review_round'), len(mine)),
        'timeliness': tl['timeliness'] if tl else None,
        'variance_days': coalesce(tl['variance'] if tl else UNDEF, None),
        'overdue_days': late_by(ctx, t['due_on'], ctx.today) if st_ != 'done' and st_ != 'in_review' and t['due_on'] < ctx.today else 0,
        'delay_reason_code': coalesce(prop(t, 'delay_reason_code'), None),
        'delay_reason': coalesce(prop(t, 'delay_reason'), None),
        'quality_rating': coalesce(prop(t, 'quality_rating'), None),
        'sent_back_count': len(sent_back),
        'last_sent_back_reason': coalesce(prop(sent_back[-1], 'decision_reason') if sent_back else UNDEF, None),
        'reschedule_count': resched.get(t['id'], 0),
        'stage': stage_of(t),
        'progress': progress_of(t),
        'tag': coalesce(prop(t, 'tag'), None),
        'complexity': prop(t, 'complexity'),
        'backup_id': prop(t, 'backup_id'),
        'version': prop(t, 'version'),
        'can_move': can_move_task(ctx, t),
        'can_submit': can_submit(ctx, t),
        'can_review': can_review_task(ctx, t) if st_ == 'in_review' else (can_review_task(ctx, t) and not self_review(ctx, t)),
    }


def _resched_counts(ctx: Ctx) -> dict[Any, int]:
    m: dict[Any, int] = {}
    for a in rows(ctx, 'activity_log'):
        if a.get('entity') == 'task' and a.get('action') == 'task.rescheduled':
            m[a['entity_id']] = m.get(a['entity_id'], 0) + 1
    return m


def _managed_tasks(ctx: Ctx) -> list[dict]:
    """Tasks a manager oversees (never wider than the task visibility rule)."""
    me = ctx.user['id']
    all_ = rows(ctx, 'tasks')
    if wide_task_scope(ctx) and has(ctx, 'tasks.view_all'):
        return all_
    team = {u['id'] for u in team_of(ctx, me)}
    my_projects = {p['id'] for p in rows(ctx, 'projects') if p.get('pm_id') == me}
    view_all = has(ctx, 'tasks.view_all')
    out = []
    for t in all_:
        owner = t.get('owner_id')
        in_team = js_truthy(owner) and owner in team
        if (in_team or t.get('assigned_by') == me or reviewer_of(ctx, t) == me or t['project_id'] in my_projects or t.get('concerned_manager_id') == me) and (
            view_all or _tasks.can_see_task(ctx, t) or in_team
        ):
            out.append(t)
    return out


# =====================================================================================================================
# Analytics
# =====================================================================================================================
def _top_reasons(s: dict, n: int = 3) -> list[dict]:
    entries = sorted(s['delay_reasons'].items(), key=lambda kv: -kv[1])[:n]
    return [{'code': code, 'label': coalesce(DELAY_REASON_LABEL.get(code, UNDEF), code), 'count': count} for code, count in entries]


def task_analytics(ctx: Ctx, q: dict) -> dict[str, Any]:
    me = ctx.user['id']
    wide = wide_task_scope(ctx) and has(ctx, 'tasks.analytics')
    team = team_of(ctx, me)
    qs = q.get('scope', UNDEF)
    scope_ = qs if qs in ('company', 'team', 'me') else ('company' if wide else 'team' if team else 'me')
    if scope_ == 'company' and not wide:
        scope_ = 'team' if team else 'me'
    if scope_ == 'team' and not team and not wide:
        scope_ = 'me'
    if not has(ctx, 'tasks.analytics') and not team:
        scope_ = 'me'
    active = [u for u in rows(ctx, 'users') if u.get('status') == 'active']
    people = [u for u in active if u['id'] == me] if scope_ == 'me' else (team if team else []) if scope_ == 'team' else active
    ids = [u['id'] for u in people]
    qf = q.get('from', UNDEF)
    qt = q.get('to', UNDEF)
    frm = qf if js_truthy(qf) and _iso_test(qf) else UNDEF
    to = qt if js_truthy(qt) and _iso_test(qt) else UNDEF
    total = task_stats(ctx, {'userIds': ids, 'from': frm, 'to': to})
    qg = q.get('group', UNDEF)
    group = qg if qg in ('project', 'manager') else 'person'
    out: list[dict] = []
    if group == 'person':
        for u in people:
            s = task_stats(ctx, {'userIds': [u['id']], 'from': frm, 'to': to})
            out.append({'key': u['id'], 'label': u['name'], 'user_id': u['id'], 'stats': s, 'top_reasons': _top_reasons(s)})
    elif group == 'project':
        id_set = set(ids)
        pids = {t['project_id'] for t in rows(ctx, 'tasks') if js_truthy(t.get('owner_id')) and t['owner_id'] in id_set}
        for p in rows(ctx, 'projects'):
            if p['id'] in pids:
                s = task_stats(ctx, {'userIds': ids, 'projectId': p['id'], 'from': frm, 'to': to})
                out.append(
                    {'key': p['id'], 'label': f'{js_str(p["code"])} · {js_str(p["name"])}', 'project_id': p['id'], 'stats': s, 'top_reasons': _top_reasons(s)}
                )
    else:
        for u in active:
            tm = team_of(ctx, u['id'])
            if len(tm) and (scope_ == 'company' or u['id'] == me or u['id'] in ids):
                s = task_stats(ctx, {'userIds': [x['id'] for x in tm], 'from': frm, 'to': to})
                out.append({'key': u['id'], 'label': u['name'], 'user_id': u['id'], 'stats': s, 'top_reasons': _top_reasons(s)})
    out = sorted((r for r in out if r['stats']['assigned'] > 0), key=lambda r: (-r['stats']['assigned'], locale_key(r['label'])))
    person_rows = out if group == 'person' else [{'user_id': u['id'], 'stats': task_stats(ctx, {'userIds': [u['id']], 'from': frm, 'to': to})} for u in people]
    top_late = sorted(
        (
            {'user_id': r['user_id'], 'late': r['stats']['late'], 'completed': r['stats']['completed'], 'avg_variance_days': r['stats']['avg_variance_days']}
            for r in person_rows
            if r['stats']['late'] > 0
        ),
        key=lambda x: (-x['late'], -coalesce(x['avg_variance_days'], 0)),
    )[:5]
    all_tasks = rows(ctx, 'tasks')
    by_task = _reviews_by_task(rows(ctx, 'task_reviews'))
    mine_open = [t for t in all_tasks if t.get('owner_id') == me and t.get('status') != 'done']

    def last_decision(t: dict) -> dict | None:
        rs = sorted(by_task.get(t['id'], ()), key=lambda r: -r['round'])
        return rs[0] if rs else None

    top_me = is_top(ctx, me)
    return {
        'scope': scope_,
        'from': coalesce(frm, ''),
        'to': coalesce(to, ''),
        'total': total,
        'rows': out,
        'top_late': top_late,
        'delay_reasons': _top_reasons(total, 9),
        'mine': {
            'review_queue': sum(1 for t in all_tasks if t.get('status') == 'in_review' and reviewer_of(ctx, t) == me and t.get('owner_id') != me)
            + sum(1 for t in all_tasks if t.get('status') == 'in_review' and t.get('owner_id') == me and top_me and reviewer_of(ctx, t) == me),
            'my_in_review': sum(1 for t in mine_open if t.get('status') == 'in_review'),
            'my_sent_back_open': sum(
                1 for t in mine_open if t.get('status') != 'in_review' and (lambda ld: ld is not None and ld.get('decision') == 'sent_back')(last_decision(t))
            ),
            'my_overdue_open': sum(1 for t in mine_open if t.get('status') != 'in_review' and t['due_on'] < ctx.today),
            'my_to_submit': sum(1 for t in mine_open if t.get('status') == 'in_progress'),
        },
    }


# =====================================================================================================================
# v7 — one create rule for every entry point (CEO point 7)
# =====================================================================================================================
def create_rights(ctx: Ctx, p: dict, owner_id: Any) -> dict[str, Any]:
    me = ctx.user['id']
    general = p['id'] == general_project_id(ctx)
    if has(ctx, 'projects.manage_all'):
        return {'ok': True, 'why': ''}
    if not general:
        if _projects.can_edit(ctx, p) and (has(ctx, 'tasks.edit') or has(ctx, 'projects.edit')):
            return {'ok': True, 'why': ''}
        return {
            'ok': False,
            'why': f'Only the project manager of {js_str(p["code"])} ({user_name(ctx, p.get("pm_id"))}) can add tasks to it. Create it under General & admin, or ask them.',
        }
    if owner_id == me:
        return {'ok': True, 'why': ''}
    if js_truthy(owner_id) and has(ctx, 'tasks.assign_team') and is_manager_of(ctx, me, owner_id):
        return {'ok': True, 'why': ''}
    return {
        'ok': False,
        'why': f'{user_name(ctx, owner_id)} isn’t in your reporting line. You can create general tasks for yourself and your reports.'
        if has(ctx, 'tasks.assign_team')
        else 'You can create general tasks for yourself. Your manager assigns work to others.',
    }


def _str_list(v: Any, mx: int, max_len: int, field: str, label: str) -> list[str]:
    if v is UNDEF or v is None:
        return []
    if not isinstance(v, list):
        err(422, 'VALIDATION', f'{label} must be a list.', field)
    vals = []
    for x in v:
        if isinstance(x, str):
            vals.append(js_trim(x))
        elif isinstance(x, dict) and 'text' in x:
            vals.append(js_trim(js_str(coalesce(x['text'], ''))))
        else:
            vals.append('')
    out = list(dict.fromkeys(x for x in vals if x))
    if len(out) > mx:
        err(422, 'VALIDATION', f'Keep {label.lower()} to {mx} or fewer.', field)
    for x in out:
        if js_len(x) > max_len:
            err(422, 'VALIDATION', f'Each of the {label.lower()} must be under {max_len} characters.', field)
    return out


def _active_person(ctx: Ctx, id_: Any, field: str, label: str, required: bool) -> dict | None:
    v = js_trim(id_) if isinstance(id_, str) else ''
    if not v:
        if required:
            err(422, 'VALIDATION', f'Choose the {label.lower()}.', field)
        return None
    u = _user_of(ctx, v)
    if u is None or u.get('status') != 'active':
        err(422, 'VALIDATION', f'Choose an active person as {label.lower()}.', field)
    return u


def _rand36(n: int) -> str:
    mark_volatile()
    return ''.join(random.choice('0123456789abcdefghijklmnopqrstuvwxyz') for _ in range(n))


def valid_attachment(ctx: Ctx, b: Any) -> dict[str, Any]:
    name = _text(_b(b, 'name'))
    if not name:
        err(422, 'VALIDATION', 'The file needs a name.', 'attachments')
    if js_len(name) > 160:
        err(422, 'VALIDATION', 'Keep file names under 160 characters.', 'attachments')
    size = js_number(coalesce(_b(b, 'size'), 0))
    du = _b(b, 'data_url')
    data = du if isinstance(du, str) else None
    if not is_finite(size) or size < 0:
        err(422, 'VALIDATION', 'The file size is not valid.', 'attachments')
    if size > _MAX_ATTACH_BYTES or (js_truthy(data) and js_len(data) > _MAX_ATTACH_BYTES * 1.4):  # type: ignore[arg-type]
        err(422, 'FILE_REJECTED', f'{name} is larger than 750 KB. Attach a link in the description for big files.', 'attachments')
    if js_truthy(data) and not _DATA_URL_RE.match(data):  # type: ignore[arg-type]
        err(422, 'FILE_REJECTED', f'{name} could not be read. Choose the file again.', 'attachments')
    return {
        'id': f'att_{_rand36(8)}',
        'name': name,
        'size': js_round(size),
        'type': js_or(_text(_b(b, 'type')), 'application/octet-stream'),
        'data_url': data,
        'uploaded_by': ctx.user['id'],
        'at': now_iso(),
    }


def create_task_core(ctx: Ctx, body: Any, opts: dict | None = None) -> dict:
    """Create a task with the full professional field set (validate, number, dependencies, notify, log)."""
    o = opts or {}
    recurrence_of = o.get('recurrenceOf', UNDEF)
    silent = o.get('silent', UNDEF)
    me = ctx.user['id']
    title = _text(_b(body, 'title'))
    if js_len(title) < 3:
        err(422, 'VALIDATION', 'Give the task a title (at least 3 characters).', 'title')
    if js_len(title) > 200:
        err(422, 'VALIDATION', 'Keep the title under 200 characters.', 'title')
    d_raw = _b(body, 'description')
    description = js_trim(d_raw) if isinstance(d_raw, str) else ''
    if js_len(description) > 6000:
        err(422, 'VALIDATION', 'Keep the description under 6,000 characters.', 'description')
    pid = _text(_b(body, 'project_id'))
    if not pid:
        err(422, 'VALIDATION', 'Choose the concerned project, or General & admin for non-project work.', 'project_id')
    gen = general_project_id(ctx)
    if pid == gen:
        ensure_general_project(ctx)
    p = find(ctx, 'projects', pid, 'Project')
    general = p['id'] == gen
    if p.get('status') not in ('active', 'initiated') and not general:
        err(422, 'VALIDATION', f'{js_str(p["code"])} is {p["status"].replace("_", " ", 1)}. Add tasks to an active project or General & admin.', 'project_id')
    owner = _active_person(ctx, _b(body, 'owner_id'), 'owner_id', 'Owner', True)
    assert owner is not None
    rights = create_rights(ctx, p, owner['id'])
    if not rights['ok']:
        err(403, 'FORBIDDEN', rights['why'], 'owner_id')
    live = [m for m in rows(ctx, 'milestones') if m['project_id'] == p['id'] and not js_truthy(m.get('deleted_at'))]
    m: dict | None = None
    b_ms = _b(body, 'milestone_id')
    if js_truthy(b_ms):
        m = next((x for x in live if x['id'] == b_ms), None)
        if m is None:
            err(422, 'VALIDATION', 'Choose a milestone of this project.', 'milestone_id')
    elif not general and len(live):
        err(422, 'VALIDATION', 'Choose the milestone this task delivers. Its target end feeds the milestone deadline.', 'milestone_id')
    backup = _active_person(ctx, _b(body, 'backup_id'), 'backup_id', 'Backup', False)
    if backup is not None and backup['id'] == owner['id']:
        err(422, 'VALIDATION', 'The backup must be a different person from the owner.', 'backup_id')
    reviewer_id: Any = _text(_b(body, 'reviewer_id'))
    if not reviewer_id:
        if not general and js_truthy(p.get('pm_id')) and p['pm_id'] != owner['id']:
            dflt: Any = p['pm_id']
        elif owner['id'] != me:
            dflt = me
        else:
            dflt = coalesce(prop(owner, 'reports_to_id'), owner['id'] if is_top(ctx, owner['id']) else None)
        reviewer_id = coalesce(dflt, '')
    reviewer = _active_person(ctx, reviewer_id, 'reviewer_id', 'Task reviewer', True)
    assert reviewer is not None
    if reviewer['id'] == owner['id'] and not is_top(ctx, reviewer['id']):
        err(
            422,
            'TASK_SELF_REVIEW',
            'The reviewer approves completion, so it must be someone other than the owner (usually the PM or their manager).',
            'reviewer_id',
        )
    b_cm = _b(body, 'concerned_manager_id')
    cm_raw = prop(owner, 'reports_to_id') if b_cm is UNDEF else b_cm
    cm = _active_person(ctx, cm_raw, 'concerned_manager_id', 'Concerned manager', False)
    if cm is not None and cm['id'] == owner['id'] and not is_top(ctx, cm['id']):
        err(422, 'VALIDATION', 'The concerned manager must be someone other than the owner.', 'concerned_manager_id')
    pr_raw = js_or(_text(_b(body, 'priority')), 'normal')
    priority = _P_CODES.get(pr_raw, pr_raw)
    if priority not in PRIORITIES:
        err(422, 'VALIDATION', 'Priority must be P1, P2, P3 or P4.', 'priority')
    complexity = js_or(_text(_b(body, 'complexity')), 'medium')
    if complexity not in ('low', 'medium', 'high', 'very_high'):
        err(422, 'VALIDATION', 'Complexity must be low, medium, high or very high.', 'complexity')
    tag = js_or(_text(_b(body, 'tag')), 'feature')
    if tag not in _TAG_KEYS:
        err(422, 'VALIDATION', 'Choose a task type: Feature, Bug, Improvement, Documentation, Support or Meeting.', 'tag')
    est_raw = _b(body, 'est_hours')
    est = js_number(est_raw)
    if est_raw is UNDEF or est_raw is None or est_raw == '' or not is_finite(est) or est < 0.5 or est > 999:
        err(422, 'VALIDATION', 'Estimated hours must be between 0.5 and 999.', 'est_hours')
    start_on = _text(_b(body, 'start_on'))
    if not _iso_test(start_on):
        err(422, 'VALIDATION', 'Set a start date.', 'start_on')
    due_on = _text(_b(body, 'due_on'))
    if not _iso_test(due_on):
        err(422, 'VALIDATION', 'Set an end / due date.', 'due_on')
    if due_on < start_on:
        err(422, 'DATE_RANGE', 'The end date is before the start date.', 'due_on')
    if not js_truthy(recurrence_of) and due_on < ctx.today:
        err(422, 'DATE_RANGE', 'The end date is in the past. Pick today or later.', 'due_on')
    c = cal(ctx)
    if not is_working_day(start_on, c):
        warn(ctx, 'NON_WORKING_DAY', f'{fmt_date(start_on)} (start) is a holiday or weekly off.')
    if not is_working_day(due_on, c):
        warn(ctx, 'NON_WORKING_DAY', f'{fmt_date(due_on)} (end) is a holiday or weekly off.')
    if not general and (start_on < p['start_on'] or due_on > p['end_on']):
        warn(ctx, 'OUTSIDE_PROJECT', f'The task runs outside {js_str(p["code"])}’s dates ({fmt_date(p["start_on"])} – {fmt_date(p["end_on"])}).')
    if m is not None and due_on > m['planned_end']:
        warn(
            ctx,
            'MS_SLIP',
            f'It ends after M{js_str(m["no"])}’s deadline ({fmt_date(m["planned_end"])}), so the milestone deadline moves to {fmt_date(due_on)}.',
        )
    dep_ids = _str_list(_b(body, 'depends_on'), 30, 64, 'depends_on', 'Dependencies')
    all_tasks = rows(ctx, 'tasks')
    for id_ in dep_ids:
        d = next((x for x in all_tasks if x['id'] == id_), None)
        if d is None or d['project_id'] != p['id']:
            err(422, 'VALIDATION', 'Dependencies must be tasks of the same project.', 'depends_on')
    watchers = _str_list(_b(body, 'watcher_ids'), 20, 64, 'watcher_ids', 'Watchers')
    for w in watchers:
        _active_person(ctx, w, 'watcher_ids', 'Watcher', True)
    criteria = [{'text': x, 'done': False} for x in _str_list(_b(body, 'acceptance_criteria'), 30, 300, 'acceptance_criteria', 'Acceptance criteria')]
    labels = _str_list(_b(body, 'labels'), 10, 30, 'labels', 'Labels')
    recurrence = js_or(_text(_b(body, 'recurrence')), 'none')
    if recurrence not in RECURRENCES:
        err(422, 'VALIDATION', 'Repeat must be none, weekly or monthly.', 'recurrence')
    b_files = _b(body, 'attachments')
    files = b_files if isinstance(b_files, list) else []
    if len(files) > 5:
        err(422, 'VALIDATION', 'Attach up to 5 files.', 'attachments')
    attachments = [valid_attachment(ctx, f) for f in files]
    parent: dict | None = None
    b_parent = _b(body, 'parent_task_id')
    if js_truthy(b_parent):
        parent = next((x for x in all_tasks if x['id'] == b_parent), None)
        if parent is None:
            err(422, 'VALIDATION', 'The parent task no longer exists.', 'parent_task_id')
    b_bill = _b(body, 'billable')
    billable = b_bill if isinstance(b_bill, bool) else not general
    if billable and general:
        warn(ctx, 'BILLABLE_GENERAL', 'General & admin work is marked billable — make sure a client is charged for it.')
    no = _projects.next_task_no(ctx, p['id'], m['id'] if m is not None else None, True)
    wbs_code = f'{js_str(m["no"]) if m is not None else "0"}.{js_str(no)}'
    was_empty = m is not None and not any(x.get('milestone_id') == m['id'] for x in all_tasks)
    est_q = js_round(est * 4) / 4
    t = insert(
        ctx,
        'tasks',
        {
            'project_id': p['id'],
            'milestone_id': m['id'] if m is not None else None,
            'no': no,
            'wbs_code': wbs_code,
            'title': title,
            'owner_id': owner['id'],
            'backup_id': backup['id'] if backup is not None else None,
            'est_hours': est_q,
            'start_on': start_on,
            'due_on': due_on,
            'done_on': None,
            'complexity': complexity,
            'status': 'todo',
            'description': description,
            'blocked_reason': None,
            'stage': 'new',
            'progress': 0,
            'tag': tag,
            'actual_start_on': None,
            'baseline_due_on': due_on,
            'target_revisions': [],
            'slip_flagged_on': None,
            'slip_note': None,
            'kind': 'general' if general else 'project',
            'assigned_by': me,
            'reviewer_id': reviewer['id'],
            'priority': priority,
            'review_round': 0,
            'submitted_on': None,
            'approved_by': None,
            'approved_on': None,
            'timeliness': None,
            'variance_days': None,
            'delay_reason_code': None,
            'delay_reason': None,
            'quality_rating': None,
            'concerned_manager_id': cm['id'] if cm is not None else None,
            'watcher_ids': watchers,
            'acceptance_criteria': criteria,
            'labels': labels,
            'billable': billable,
            'recurrence': recurrence,
            'recurrence_of': coalesce(recurrence_of, None),
            'attachments': attachments,
            'parent_task_id': parent['id'] if parent is not None else None,
        },
    )
    for id_ in dep_ids:
        insert(ctx, 'task_dependencies', {'task_id': t['id'], 'depends_on_task_id': id_})
    if m is not None:
        ch: dict[str, Any] = {'empty_since': None}
        if js_truthy(m.get('achieved_on')):
            ch['achieved_on'] = None
        m = patch('milestones', m, ch)
        sync_milestone_dates(ctx, m['id'], was_empty)
    where = 'General & admin' if general else js_str(p['code']) + (f' / M{js_str(m["no"])}' if m is not None else '')
    name = js_str(ctx.user.get('name', UNDEF))
    cm_s = f' · concerned manager {js_str(cm["name"])}' if cm is not None else ''
    rec_s = ' · next occurrence' if js_truthy(recurrence_of) else ''
    log_activity(
        ctx,
        'task',
        t['id'],
        'task.created',
        f'{name} created {wbs_code} {title} for {js_str(owner["name"])} ({where}), {fmt_date(start_on)} – {fmt_date(due_on)}, {js_str(est_q)}h · reviewer {js_str(reviewer["name"])}{cm_s}{rec_s}',
    )
    log_activity(ctx, 'project', p['id'], 'task.created', f'Added {wbs_code} {title} → {js_str(owner["name"])}')
    if parent is not None:
        log_activity(ctx, 'task', parent['id'], 'task.followup', f'Follow-up {wbs_code} {title} created for {js_str(owner["name"])}')
    if not js_truthy(silent):
        told = {me}
        pr = f' · {"P1" if priority == "urgent" else "P2"}' if priority in ('urgent', 'high') else ''

        def tell(uid_: Any, title2: str, body2: str) -> None:
            if not js_truthy(uid_) or uid_ in told:
                return
            told.add(uid_)
            notify(ctx, uid_, 'NTF-14', title2, body2, f'/tasks/{t["id"]}')

        on = js_str(owner['name'])
        tell(owner['id'], 'Task assigned to you', f'{name}: {wbs_code} {title} · {where} · due {fmt_date(due_on)}{pr}')
        tell(backup['id'] if backup is not None else None, 'You are the backup on a task', f'{wbs_code} {title} (owner {on}) · due {fmt_date(due_on)}')
        tell(reviewer['id'], 'You review a new task', f'{wbs_code} {title} for {on} · due {fmt_date(due_on)}. You approve it when it’s submitted.')
        tell(cm['id'] if cm is not None else None, 'New task in your team', f'{on} has {wbs_code} {title} · due {fmt_date(due_on)}{pr}')
        for w in watchers:
            tell(w, 'You’re watching a new task', f'{wbs_code} {title} · owner {on} · due {fmt_date(due_on)}')
    return t


def spawn_next_occurrence(ctx: Ctx, t: dict) -> dict | None:
    """Recurring tasks: when one is approved, the next occurrence is created (same people and fields, dates moved)."""
    rec = t.get('recurrence')
    if not js_truthy(rec) or rec == 'none':
        return None
    if any(x.get('recurrence_of') == t['id'] for x in rows(ctx, 'tasks')):
        return None

    def shift(d: str) -> str:
        if rec == 'weekly':
            return add_days(d, 7)
        y, mo, da = (int(x) for x in d.split('-'))
        ny, nmo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        last = _calendar.monthrange(ny, nmo)[1]
        return f'{ny:04d}-{nmo:02d}-{min(da, last):02d}'

    c = cal(ctx)

    def next_working(d: str) -> str:
        x = d
        i = 0
        while i < 10 and not is_working_day(x, c):
            x = add_days(x, 1)
            i += 1
        return x

    start = next_working(shift(t['start_on']))
    due = next_working(shift(t['due_on']))
    while due < ctx.today:
        start = next_working(add_days(start, 7) if rec == 'weekly' else shift(start))
        due = next_working(add_days(due, 7) if rec == 'weekly' else shift(due))
    p = _tasks._project(ctx, t.get('project_id'))
    gen = general_project_id(ctx)
    move_to_general = p is None or (p.get('status') not in ('active', 'initiated') and p['id'] != gen)
    owner = _user_of(ctx, t.get('owner_id'))
    if owner is None or owner.get('status') != 'active':
        return None

    def live(id_: Any) -> Any:
        if js_truthy(id_):
            u = _user_of(ctx, id_)
            if u is not None and u.get('status') == 'active':
                return id_
        return None

    mid = t.get('milestone_id')
    ms = next((x for x in rows(ctx, 'milestones') if x['id'] == mid and not js_truthy(x.get('deleted_at'))), None) if js_truthy(mid) else None
    out = _insert_occurrence(
        ctx,
        t,
        gen if move_to_general else t['project_id'],
        None if move_to_general else (ms['id'] if ms is not None else None),
        start,
        due,
        {'reviewer': live(t.get('reviewer_id')), 'cm': live(t.get('concerned_manager_id')), 'backup': live(t.get('backup_id'))},
    )
    log_activity(ctx, 'task', t['id'], 'task.recurred', f'Next {js_str(rec)} occurrence created: due {fmt_date(due)}')
    notify(ctx, owner['id'], 'NTF-14', 'Next occurrence of a recurring task', f'{js_str(out["title"])} · due {fmt_date(due)}', f'/tasks/{out["id"]}')
    return out


def _insert_occurrence(ctx: Ctx, t: dict, pid: str, ms_id: Any, start: str, due: str, who: dict) -> dict:
    general = pid == general_project_id(ctx)
    no = _projects.next_task_no(ctx, pid, ms_id, True)
    m = next((x for x in rows(ctx, 'milestones') if x['id'] == ms_id), None) if js_truthy(ms_id) else None
    wbs_code = f'{js_str(m["no"]) if m is not None else "0"}.{js_str(no)}'
    row = insert(
        ctx,
        'tasks',
        {
            'project_id': pid,
            'milestone_id': ms_id,
            'no': no,
            'wbs_code': wbs_code,
            'title': prop(t, 'title'),
            'owner_id': prop(t, 'owner_id'),
            'backup_id': who['backup'],
            'est_hours': prop(t, 'est_hours'),
            'start_on': start,
            'due_on': due,
            'done_on': None,
            'complexity': prop(t, 'complexity'),
            'status': 'todo',
            'description': prop(t, 'description'),
            'blocked_reason': None,
            'stage': 'new',
            'progress': 0,
            'tag': coalesce(prop(t, 'tag'), 'feature'),
            'actual_start_on': None,
            'baseline_due_on': due,
            'target_revisions': [],
            'slip_flagged_on': None,
            'slip_note': None,
            'kind': 'general' if general else 'project',
            'assigned_by': coalesce(prop(t, 'assigned_by'), ctx.user['id']),
            'reviewer_id': who['reviewer'],
            'priority': coalesce(prop(t, 'priority'), 'normal'),
            'review_round': 0,
            'submitted_on': None,
            'approved_by': None,
            'approved_on': None,
            'timeliness': None,
            'variance_days': None,
            'delay_reason_code': None,
            'delay_reason': None,
            'quality_rating': None,
            'concerned_manager_id': who['cm'],
            'watcher_ids': coalesce(prop(t, 'watcher_ids'), []),
            'acceptance_criteria': [{'text': prop(x, 'text'), 'done': False} for x in coalesce(prop(t, 'acceptance_criteria'), [])],
            'labels': coalesce(prop(t, 'labels'), []),
            'billable': coalesce(prop(t, 'billable'), not general),
            'recurrence': prop(t, 'recurrence'),
            'recurrence_of': t['id'],
            'attachments': [],
            'parent_task_id': coalesce(prop(t, 'parent_task_id'), None),
        },
    )
    if js_truthy(ms_id):
        sync_milestone_dates(ctx, ms_id)
    log_activity(
        ctx,
        'task',
        row['id'],
        'task.created',
        f'Created automatically: the next {js_str(prop(t, "recurrence"))} occurrence of {js_str(prop(t, "title"))}, due {fmt_date(due)}',
    )
    return row


# =====================================================================================================================
# Routes
# =====================================================================================================================
def submit(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    me = ctx.user['id']
    if t.get('owner_id') != me and t.get('backup_id') != me:
        err(403, 'FORBIDDEN', 'Only the task owner (or their backup) can submit it for review.')
    if t.get('status') == 'in_review':
        err(409, 'INVALID_STATE', 'This task is already waiting for review.')
    if t.get('status') == 'done':
        err(409, 'INVALID_STATE', 'This task is already approved.')
    open_ = open_blockers(ctx, t['id'])
    if open_:
        lst = ', '.join(f'{_code(ctx, d)} {js_str(d["title"])} ({user_name(ctx, d.get("owner_id"))})' for d in open_)
        err(409, 'TASK_BLOCKED', f'This task waits on {lst}. Finish {"it" if len(open_) == 1 else "them"} first or ask the PM to remove the dependency.')
    note = _text(req.b('note'))
    if js_len(note) < 5:
        err(422, 'VALIDATION', 'Describe what you delivered (at least 5 characters) so the reviewer knows what to check.', 'note')
    if js_len(note) > 2000:
        err(422, 'VALIDATION', 'Keep the note under 2,000 characters.', 'note')
    proof = js_or(_text(req.b('proof_url')), None)
    if js_truthy(proof) and not _PROOF_RE.match(proof):
        err(422, 'VALIDATION', 'The proof link must be a full web address starting with https://', 'proof_url')
    late = ctx.today > t['due_on']
    reason_code: Any = None
    reason: Any = None
    if late:
        reason_code = js_or(_text(req.b('delay_reason_code')), None)
        reason = js_or(_text(req.b('delay_reason')), None)
        if not js_truthy(reason_code) or not _tasks.js_in(reason_code, DELAY_REASON_LABEL):
            err(422, 'DELAY_REASON_REQUIRED', f'This task was due {fmt_date(t["due_on"])}. Choose why it’s late.', 'delay_reason_code')
        if not js_truthy(reason) or js_len(reason) < 15:
            err(422, 'DELAY_REASON_REQUIRED', 'Explain the delay in at least 15 characters — what happened and what you did about it.', 'delay_reason')
    rs = reviews_of(ctx, t['id'])
    rnd = coalesce(prop(rs[-1], 'round') if rs else UNDEF, 0) + 1
    insert(
        ctx,
        'task_reviews',
        {
            'task_id': t['id'],
            'round': rnd,
            'submitted_by': me,
            'submitted_at': now_iso(),
            'submission_note': note,
            'proof_url': proof,
            'delay_reason_code': reason_code,
            'delay_reason': reason,
            'decision': None,
            'decided_by': None,
            'decided_at': None,
            'decision_reason': None,
            'quality_rating': None,
        },
    )
    legacy = _base_reviewer_of(ctx, t)
    wf = start_approval(
        ctx,
        'task_review',
        {'requester_id': coalesce(prop(t, 'owner_id'), me), 'reviewer_id': legacy, 'project_id': None if is_general(ctx, t) else t['project_id']},
        legacy,
    )
    out = update(
        ctx,
        'tasks',
        t['id'],
        {
            'status': 'in_review',
            'submitted_on': ctx.today,
            'review_round': rnd,
            'blocked_reason': None,
            **({'delay_reason_code': reason_code, 'delay_reason': reason} if late else {}),
            **wf_patch(wf),
            'wf_approver_id': wf['approver_id'],
        },
    )
    c = _code(ctx, out)
    lb = late_by(ctx, t['due_on'], ctx.today) if late else 0
    # DELAY_REASON_LABEL[code] — an Object.prototype key passes the `in` check of the TS and prints the member
    lbl = js_str(_obj_get(DELAY_REASON_LABEL, reason_code)) if late else ''
    late_s = f' {js_str(lb)}d late — {lbl}: {js_str(reason)}' if late else ''
    log_activity(ctx, 'task', t['id'], 'task.submitted', f'Submitted for review (round {js_str(rnd)}){late_s}')
    log_activity(ctx, 'project', t['project_id'], 'task.status', f'{c} {js_str(out["title"])}: submitted for review')
    rev = reviewer_of(ctx, out)
    if js_truthy(rev) and rev != me:
        late_n = f' · {js_str(lb)}d late ({lbl})' if late else ''
        notify(
            ctx,
            rev,
            'NTF-04',
            'Task resubmitted for review' if rnd > 1 else 'Task waiting for your review',
            f'{_name(ctx)} submitted {c} {js_str(out["title"])}{late_n}.',
            '/team-tasks?tab=review',
        )
    return out


def approve(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    _assert_reviewer(ctx, t)
    if t.get('status') != 'in_review':
        err(
            409,
            'INVALID_STATE',
            'This task is already approved.' if t.get('status') == 'done' else 'Only tasks submitted for review can be approved. Ask the owner to submit it.',
        )
    q = js_number(req.b('quality_rating'))
    if not is_integer(q) or q < 1 or q > 5:
        err(422, 'VALIDATION', 'Rate the quality of the work from 1 to 5 stars.', 'quality_rating')
    me = ctx.user['id']
    legacy = _base_reviewer_of(ctx, t)
    st = ensure_wf(
        ctx,
        t,
        'task_review',
        {'requester_id': coalesce(prop(t, 'owner_id'), me), 'reviewer_id': legacy, 'project_id': None if is_general(ctx, t) else t['project_id']},
        legacy,
        reviewer_of(ctx, t),
    )
    adv = advance_approval(ctx, st, me)
    if adv['next'] is not None:
        nx = adv['next']
        note = js_or(_text(req.b('note')), None)
        out = update(ctx, 'tasks', t['id'], {**wf_patch(adv['state']), 'wf_approver_id': nx['approver_id']})
        st_now = out if st is t else st  # the TS `st` IS the task row when it already had a chain (updated in place)
        c = _code(ctx, out)
        step_n = js_str(st_now['wf_step'] + 1)
        n_steps = len(st_now['wf_chain'])
        done = adv['done']
        done_name = coalesce(prop(done, 'name') if done is not None else UNDEF, 'review')
        note_s = f': {js_str(note)}' if js_truthy(note) else ''
        log_activity(
            ctx,
            'task',
            t['id'],
            'task.review_step',
            f'Step {step_n} of {n_steps} ({js_str(done_name)}) approved by {_name(ctx)} · {js_str(q)}/5{note_s}; sent to {user_name(ctx, nx["approver_id"])}',
        )
        notify(
            ctx,
            nx['approver_id'],
            'NTF-04',
            'Task waiting for your review',
            f'{c} {js_str(out["title"])} was approved by {_name(ctx)} and needs your review ({js_str(nx.get("name", UNDEF))}).',
            '/team-tasks?tab=review',
        )
        warn(
            ctx,
            'NEXT_APPROVAL',
            f'Step {step_n} of {n_steps} approved — sent to {user_name(ctx, nx["approver_id"])} for {js_str(nx.get("name", UNDEF)).lower()}.',
        )
        return out
    approve_task(ctx, t, req.b('quality_rating'), js_or(_text(req.b('note')), None))
    return update(ctx, 'tasks', t['id'], {**wf_patch(adv['state']), 'wf_approver_id': None})


def send_back(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    _assert_reviewer(ctx, t)
    if t.get('status') != 'in_review':
        err(409, 'INVALID_STATE', 'Only tasks waiting for review can be sent back.')
    reason = _text(req.b('reason'))
    if js_len(reason) < 10:
        err(422, 'VALIDATION', 'Say what needs to change (at least 10 characters) so the owner can fix it.', 'reason')
    if js_len(reason) > 2000:
        err(422, 'VALIDATION', 'Keep the reason under 2,000 characters.', 'reason')
    new_due: Any = js_or(_text(req.b('new_due_on')), None)
    if js_truthy(new_due):
        if not _iso_test(new_due):
            err(422, 'VALIDATION', 'The new due date needs a valid date.', 'new_due_on')
        if new_due < ctx.today:
            err(422, 'DATE_RANGE', 'The new due date can’t be in the past.', 'new_due_on')
        if new_due < t['start_on']:
            err(422, 'DATE_RANGE', 'The new due date is before the task starts.', 'new_due_on')
        if new_due == t['due_on']:
            new_due = None
        elif not is_working_day(new_due, cal(ctx)):
            warn(ctx, 'NON_WORKING_DAY', f'{fmt_date(new_due)} is not a working day.')
    me = ctx.user['id']
    rv = _open_review(ctx, t)
    if rv is not None:
        update(ctx, 'task_reviews', rv['id'], {'decision': 'sent_back', 'decided_by': me, 'decided_at': now_iso(), 'decision_reason': reason})
    back = stage_of(t)
    back_stage = back if STAGE[back]['group'] in ('active', 'testing') else 'in_development'
    rev_patch = (
        {
            'due_on': new_due,
            'baseline_due_on': coalesce(prop(t, 'baseline_due_on'), t['due_on']),
            'target_revisions': [
                *coalesce(prop(t, 'target_revisions'), []),
                {'from': t['due_on'], 'to': new_due, 'at': now_iso(), 'by': me, 'reason': f'Sent back: {reason}'},
            ],
        }
        if js_truthy(new_due)
        else {}
    )
    out = update(
        ctx,
        'tasks',
        t['id'],
        {'status': 'in_progress', 'stage': back_stage, 'progress': js_min(progress_of(t), 95), **rev_patch, **wf_clear(), 'wf_approver_id': None},
    )
    if js_truthy(new_due):
        sync_milestone_dates(ctx, t.get('milestone_id'))
    c = _code(ctx, out)
    rnd = coalesce(prop(rv, 'round') if rv is not None else UNDEF, prop(t, 'review_round'), 1)
    log_activity(ctx, 'task', t['id'], 'task.sent_back', f'Sent back (round {js_str(rnd)}): {reason}')
    if js_truthy(new_due):
        # the TS reads t.due_on AFTER update() changed it in place: both dates are the new one
        log_activity(
            ctx,
            'task',
            t['id'],
            'task.rescheduled',
            f'Due moved {fmt_date(out["due_on"])} → {fmt_date(new_due)} on send-back (original due {out["due_on"]}): {reason}',
        )
    log_activity(ctx, 'project', t['project_id'], 'task.status', f'{c} {js_str(out["title"])}: sent back by {_name(ctx)}')
    if js_truthy(t.get('owner_id')) and t['owner_id'] != me:
        snip = _tasks._slice16(reason, 118) + '…' if js_len(reason) > 120 else reason
        nd = f' · now due {fmt_date(new_due)}' if js_truthy(new_due) else ''
        notify(ctx, t['owner_id'], 'NTF-06', 'Task sent back', f'{_name(ctx)} sent back {c} {js_str(out["title"])}: “{snip}”{nd}', f'/tasks/{t["id"]}')
    return out


def set_stage(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    stage = req.b('stage')
    if not any(s['key'] == stage for s in TASK_STAGES):
        err(422, 'VALIDATION', 'Choose a valid stage.', 'stage')
    return apply_stage(ctx, t, stage, {'progress': req.b('progress'), 'note': req.b('note'), 'quality_rating': req.b('quality_rating')})


def set_progress(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    assert_can_move(ctx, t)
    if t.get('status') == 'done':
        err(409, 'INVALID_STATE', 'This task is completed. Reopen it to change its progress.', 'progress')
    if t.get('status') == 'in_review':
        err(409, 'USE_REVIEW_FLOW', 'This task is waiting for review. The reviewer approves it or sends it back.', 'progress')
    raw = req.b('progress')
    n = js_number(raw)
    if raw == '' or raw is None or not is_integer(n) or n < 0 or n > 100:
        err(422, 'VALIDATION', 'Progress must be a whole number from 0 to 100.', 'progress')
    if n == 100:
        err(422, 'VALIDATION', 'At 100% the task is finished: set the stage to Completed so it goes through review.', 'progress')
    stage = stage_of(t)
    if n < STAGE[stage]['minProgress']:
        err(
            422,
            'VALIDATION',
            f'{STAGE[stage]["label"]} means at least {js_str(STAGE[stage]["minProgress"])}% done. Move the task back a stage first.',
            'progress',
        )
    note = _text(req.b('note'))
    if js_len(note) > 2000:
        err(422, 'VALIDATION', 'Keep the note under 2,000 characters.', 'note')
    before = progress_of(t)
    if n > 0 and STAGE[stage]['group'] == 'not_started':
        msg = _blocker_message(ctx, t)
        if msg:
            err(409, 'TASK_BLOCKED', msg, 'progress')
        stage = 'in_development'
    out = update(
        ctx,
        'tasks',
        t['id'],
        {
            'progress': n,
            'stage': stage,
            'status': status_for_stage(stage, t['status']),
            'actual_start_on': coalesce(prop(t, 'actual_start_on'), ctx.today if n > 0 else None),
        },
    )
    note_s = f': {note}' if note else ''
    log_activity(ctx, 'task', t['id'], 'task.progress', f'Progress {js_str(before)}% → {js_str(n)}%{note_s}')
    if note:
        insert(
            ctx,
            'comments',
            {'task_id': t['id'], 'body': f'Progress {js_str(before)}% → {js_str(n)}%. {note}', 'mentions': [], 'author_id': ctx.user['id'], 'parent_id': None},
        )
    return out


def flag_slip(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    p = assert_can_move(ctx, t)['p']
    if t.get('status') == 'done':
        err(409, 'INVALID_STATE', 'This task is completed.', 'note')
    note = _text(req.b('note'))
    if js_len(note) < 10:
        err(422, 'VALIDATION', 'Say why it will slip (at least 10 characters). It is kept as an early warning on the task.', 'note')
    new_due: Any = js_or(_text(req.b('new_due_on')), None)
    if js_truthy(new_due):
        if not _iso_test(new_due):
            err(422, 'VALIDATION', 'The new target end needs a valid date.', 'new_due_on')
        if new_due < t['start_on']:
            err(422, 'DATE_RANGE', 'The new target end is before the task starts.', 'new_due_on')
        if new_due < ctx.today:
            err(422, 'DATE_RANGE', 'The new target end can’t be in the past.', 'new_due_on')
        if new_due == t['due_on']:
            new_due = None
        elif not is_working_day(new_due, cal(ctx)):
            warn(ctx, 'NON_WORKING_DAY', f'{fmt_date(new_due)} is not a working day.')
    me = ctx.user['id']
    ch: dict[str, Any] = {'slip_flagged_on': ctx.today, 'slip_note': note}
    if js_truthy(new_due):
        ch['baseline_due_on'] = coalesce(prop(t, 'baseline_due_on'), t['due_on'])
        ch['target_revisions'] = [*coalesce(prop(t, 'target_revisions'), []), {'from': t['due_on'], 'to': new_due, 'at': now_iso(), 'by': me, 'reason': note}]
        ch['due_on'] = new_due
    out = update(ctx, 'tasks', t['id'], ch)
    if js_truthy(new_due):
        sync_milestone_dates(ctx, t.get('milestone_id'))
    c = _code(ctx, out)
    # the TS reads t.due_on AFTER update() changed it in place
    tgt = f'; target {fmt_date(out["due_on"])} → {fmt_date(new_due)}' if js_truthy(new_due) else ''
    log_activity(ctx, 'task', t['id'], 'task.slip_flagged', f'Slip flagged{tgt}: {note}')
    if js_truthy(new_due):
        log_activity(ctx, 'task', t['id'], 'task.rescheduled', f'Due moved {fmt_date(out["due_on"])} → {fmt_date(new_due)} (slip flagged early): {note}')
    nt = f', new target {fmt_date(new_due)}' if js_truthy(new_due) else ''
    log_activity(ctx, 'project', t['project_id'], 'task.slip_flagged', f'{c} {js_str(out["title"])}: slip flagged{nt}')
    if js_truthy(p.get('pm_id')) and p['pm_id'] != me:
        nt2 = f' · new target {fmt_date(new_due)}' if js_truthy(new_due) else ''
        notify(ctx, p['pm_id'], 'NTF-14', f'Slip flagged on {c}', f'{_name(ctx)}: {note}{nt2}', f'/projects/{p["id"]}/schedule')
    return out


def cancel(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'tasks', req.params['id'], 'Task')
    r = assert_can_move(ctx, t)
    p, editor = r['p'], r['editor']
    if not editor:
        err(403, 'FORBIDDEN', 'Only the project manager can cancel a task.')
    if t.get('status') == 'done':
        err(409, 'INVALID_STATE', 'This task is already closed.')
    reason = _text(req.b('reason'))
    if js_len(reason) < 5:
        err(422, 'VALIDATION', 'Say why the task is cancelled (at least 5 characters).', 'reason')
    logged: Any = 0
    for e in rows(ctx, 'timesheet_entries'):
        if e.get('task_id') == t['id']:
            logged = logged + e['hours']
    me = ctx.user['id']
    out = update(
        ctx,
        'tasks',
        t['id'],
        {
            'status': 'done',
            'stage': 'completed',
            'progress': 100,
            'done_on': ctx.today,
            'est_hours': js_round(logged * 4) / 4,
            'blocked_reason': None,
            'timeliness': None,
            'variance_days': None,
            'quality_rating': None,
            'cancelled_on': ctx.today,
            'cancel_reason': reason,
            'approved_by': me,
            'approved_on': ctx.today,
        },
    )
    _projects.sync_achieved(ctx, t.get('milestone_id'))
    c = _code(ctx, out)
    log_activity(ctx, 'task', t['id'], 'task.cancelled', f'Cancelled with {js_str(logged)}h logged: {reason}')
    log_activity(
        ctx, 'project', p['id'], 'task.cancelled', f'{c} {js_str(out["title"])} cancelled ({js_str(logged)}h logged stay on the project cost): {reason}'
    )
    if js_truthy(t.get('owner_id')) and t['owner_id'] != me:
        notify(ctx, t['owner_id'], 'NTF-14', 'Task cancelled', f'{_name(ctx)} cancelled {c} {js_str(out["title"])}: {reason}', f'/tasks/{t["id"]}')
    return out


def task_reviews(req: Req) -> Any:
    ctx = req.ctx
    tv = find(ctx, 'tasks', req.params['id'], 'Task')
    if not _tasks.can_see_task(ctx, tv) and not _projects.can_edit(ctx, find(ctx, 'projects', tv['project_id'], 'Project')):
        err(403, 'TASK_NOT_VISIBLE', 'This task isn’t yours, so its review history is private.')
    return reviews_of(ctx, req.params['id'])


def review_queue(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user['id']
    all_ = js_str(coalesce(req.q('all'), '')) == '1' and wide_task_scope(ctx)
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    reviews = rows(ctx, 'task_reviews')
    rs = _resched_counts(ctx)
    by_task = _reviews_by_task(reviews)
    out = []
    for t in rows(ctx, 'tasks'):
        if not (t.get('status') == 'in_review' and (all_ or (reviewer_of(ctx, t) == me and not self_review(ctx, t)))):
            continue
        base = _team_row(ctx, t, projects, reviews, rs)
        lst = sorted(by_task.get(t['id'], ()), key=lambda x: -x['round'])
        latest = lst[0] if lst else UNDEF
        sub = js_or(coalesce(prop(t, 'submitted_on'), latest['submitted_at'][:10] if latest is not UNDEF else UNDEF), ctx.today)
        out.append({**base, 'latest': latest, 'late_at_submission': late_by(ctx, t['due_on'], sub), 'can_review': can_review_task(ctx, t)})
    return sorted(out, key=lambda a: (locale_key(coalesce(a['submitted_on'], '')), locale_key(a['due_on'])))


def team_tasks(req: Req) -> Any:
    ctx = req.ctx
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    reviews = rows(ctx, 'task_reviews')
    rs = _resched_counts(ctx)
    since = add_days(ctx.today, -120)
    out = []
    for t in _managed_tasks(ctx):
        s = prop(projects.get(t['project_id']), 'status')
        keep = s != 'closed' if t.get('status') != 'done' else coalesce(prop(t, 'done_on'), '') >= since
        if keep:
            out.append(_team_row(ctx, t, projects, reviews, rs))
    return out


def team_task_options(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user['id']
    admin = has(ctx, 'projects.manage_all')
    gen = general_project_id(ctx)
    ms = [m for m in rows(ctx, 'milestones') if not js_truthy(m.get('deleted_at'))]
    projects = [p for p in rows(ctx, 'projects') if p.get('status') in ('active', 'initiated') and (p['id'] == gen or admin or p.get('pm_id') == me)]
    projects = sorted(projects, key=lambda p: (0, ()) if p['id'] == gen else (1, locale_key(p['code'])))
    return {
        'general_project_id': gen,
        'projects': [
            {
                'id': p['id'],
                'code': p['code'],
                'name': p['name'],
                'general': p['id'] == gen,
                'anyone': admin or (p['id'] != gen and p.get('pm_id') == me),
                'milestones': [
                    {'id': m['id'], 'no': m['no'], 'name': m['name']} for m in sorted((m for m in ms if m['project_id'] == p['id']), key=lambda m: m['no'])
                ],
            }
            for p in projects
        ],
        'team_ids': [u['id'] for u in team_of(ctx, me)],
        'is_admin': admin,
    }


def assign_team_task(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user['id']
    owner = _user_of(ctx, _text(req.b('owner_id')))
    if owner is None:
        err(422, 'VALIDATION', 'Choose who will do this task.', 'owner_id')
    if owner.get('status') != 'active':
        err(422, 'VALIDATION', f'{js_str(owner["name"])} is not active. Choose someone on the team today.', 'owner_id')
    title = _text(req.b('title'))
    if js_len(title) < 3:
        err(422, 'VALIDATION', 'Give the task a title (at least 3 characters).', 'title')
    if js_len(title) > 200:
        err(422, 'VALIDATION', 'Keep the title under 200 characters.', 'title')
    description = _text(req.b('description'))
    if js_len(description) > 4000:
        err(422, 'VALIDATION', 'Keep the description under 4,000 characters.', 'description')
    pid = js_or(_text(req.b('project_id')), general_project_id(ctx))
    p = find(ctx, 'projects', pid, 'Project')
    if p.get('status') not in ('active', 'initiated'):
        err(
            422,
            'VALIDATION',
            f'{js_str(p["code"])} is {p["status"].replace("_", " ", 1)}. Assign the task under an active project or General & admin.',
            'project_id',
        )
    general = p['id'] == general_project_id(ctx)
    allowed = has(ctx, 'projects.manage_all') or (not general and _projects.can_edit(ctx, p)) or is_manager_of(ctx, me, owner['id'])
    if not allowed:
        err(
            403,
            'FORBIDDEN',
            f'You can assign tasks to people in your reporting line, or to anyone on a project you manage. {js_str(owner["name"])} isn’t in your team.',
        )
    start_on = js_or(_text(req.b('start_on')), ctx.today)
    if not _iso_test(start_on):
        err(422, 'VALIDATION', 'Start needs a valid date.', 'start_on')
    due_on = _text(req.b('due_on'))
    if not _iso_test(due_on):
        err(422, 'VALIDATION', 'Set a due date.', 'due_on')
    if due_on < start_on:
        err(422, 'DATE_RANGE', 'The due date is before the start date.', 'due_on')
    if due_on < ctx.today:
        err(422, 'DATE_RANGE', 'The due date is in the past. Pick today or later.', 'due_on')
    if not is_working_day(due_on, cal(ctx)):
        warn(ctx, 'NON_WORKING_DAY', f'{fmt_date(due_on)} is not a working day — the task is due on a holiday or weekly off.')
    est = js_number(req.b('est_hours'))
    if not is_finite(est) or est < 0.5 or est > 200:
        err(422, 'VALIDATION', 'Estimated effort must be between 0.5 and 200 hours.', 'est_hours')
    bp = req.b('priority')
    priority = bp if isinstance(bp, str) and bp in PRIORITIES else 'normal'
    m: dict | None = None
    b_ms = req.b('milestone_id')
    if js_truthy(b_ms):
        m = next((x for x in rows(ctx, 'milestones') if x['id'] == b_ms and x['project_id'] == p['id'] and not js_truthy(x.get('deleted_at'))), None)
        if m is None:
            err(422, 'VALIDATION', 'Choose a milestone of this project.', 'milestone_id')
    reviewer = js_or(_text(req.b('reviewer_id')), me)
    rv = _user_of(ctx, reviewer)
    if rv is None or rv.get('status') != 'active':
        err(422, 'VALIDATION', 'Choose an active reviewer.', 'reviewer_id')
    if reviewer == owner['id'] and not is_top(ctx, reviewer):
        if js_truthy(req.b('reviewer_id')):
            err(422, 'TASK_SELF_REVIEW', 'The reviewer must be someone other than the owner.', 'reviewer_id')
        reviewer = coalesce(prop(owner, 'reports_to_id'), me)
    no = _projects.next_task_no(ctx, p['id'], m['id'] if m is not None else None, True)
    bc = req.b('complexity')
    complexity = bc if isinstance(bc, str) and bc in ('low', 'medium', 'high', 'very_high') else 'high' if est > 40 else 'medium' if est > 12 else 'low'
    t = insert(
        ctx,
        'tasks',
        {
            'project_id': p['id'],
            'milestone_id': m['id'] if m is not None else None,
            'no': no,
            'title': title,
            'owner_id': owner['id'],
            'backup_id': None,
            'est_hours': js_round(est * 4) / 4,
            'start_on': start_on,
            'due_on': due_on,
            'done_on': None,
            'complexity': complexity,
            'status': 'todo',
            'description': description,
            'blocked_reason': None,
            'stage': 'new',
            'progress': 0,
            'tag': coalesce(req.b('tag'), None),
            'actual_start_on': None,
            'baseline_due_on': due_on,
            'target_revisions': [],
            'slip_flagged_on': None,
            'slip_note': None,
            'kind': 'general' if general else 'project',
            'assigned_by': me,
            'reviewer_id': reviewer,
            'priority': priority,
            'review_round': 0,
            'submitted_on': None,
            'approved_by': None,
            'approved_on': None,
            'timeliness': None,
            'variance_days': None,
            'delay_reason_code': None,
            'delay_reason': None,
            'quality_rating': None,
        },
    )
    if m is not None:
        mid = m['id']
        was_empty = not any(x.get('milestone_id') == mid and x['id'] != t['id'] for x in rows(ctx, 'tasks'))
        ch: dict[str, Any] = {'empty_since': None}
        if js_truthy(m.get('achieved_on')):
            ch['achieved_on'] = None
        m = patch('milestones', m, ch)
        sync_milestone_dates(ctx, mid, was_empty)
    c = f'{js_str(m["no"]) if m is not None else "0"}.{js_str(no)}'
    log_activity(
        ctx,
        'task',
        t['id'],
        'task.created',
        f'{_name(ctx)} assigned {c} {title} to {js_str(owner["name"])}, due {fmt_date(due_on)} · reviewer {user_name(ctx, reviewer)}',
    )
    log_activity(ctx, 'project', p['id'], 'task.created', f'Assigned {c} {title} → {js_str(owner["name"])}')
    if owner['id'] != me:
        pr = f' · {priority} priority' if priority in ('urgent', 'high') else ''
        notify(ctx, owner['id'], 'NTF-14', 'Task assigned to you', f'{_name(ctx)}: {title} · due {fmt_date(due_on)}{pr}', f'/tasks/{t["id"]}')
    return t


def analytics_tasks(req: Req) -> Any:
    ctx = req.ctx
    return task_analytics(ctx, {'from': req.q('from'), 'to': req.q('to'), 'scope': req.q('scope'), 'group': req.q('group')})


def register(r: Any) -> None:
    r.post('/tasks/:id/submit', submit)
    r.post('/tasks/:id/approve', approve)
    r.post('/tasks/:id/send-back', send_back)
    r.post('/tasks/:id/stage', set_stage)
    r.post('/tasks/:id/progress', set_progress)
    r.post('/tasks/:id/flag-slip', flag_slip)
    r.post('/tasks/:id/cancel', cancel)
    r.get('/tasks/:id/reviews', task_reviews)
    r.get('/team-tasks/review-queue', review_queue)
    r.get('/team-tasks', team_tasks)
    r.get('/team-tasks/options', team_task_options)
    r.post('/team-tasks', assign_team_task)
    r.get('/analytics/tasks', analytics_tasks)
