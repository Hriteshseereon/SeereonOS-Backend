"""Shared performance calculations (v3) — port of src/mock/performance.ts.

Used by task governance, appraisals, people dashboards, the approvals inbox and the report engine, so every screen
shows the same numbers.
"""

from __future__ import annotations

from typing import Any

from app.catalog import cat
from app.ctx import Ctx, rows
from app.domain.calendar import add_days, is_working_day
from app.engine.analytics import cal
from app.js import UNDEF, coalesce, js_truthy, nullish, prop

__all__ = [
    'is_cancelled',
    'working_day_variance',
    'timeliness_of',
    'task_timeliness',
    'task_stats',
    'reports_of',
    'team_of',
    'DELAY_REASON_LABEL',
]

DELAY_REASON_LABEL = cat('mock/performance')['DELAY_REASON_LABEL']


def is_cancelled(t: dict) -> bool:
    """A task closed as cancelled (it had logged hours, so it couldn't be deleted). Excluded from delivery stats."""
    return js_truthy(t.get('cancelled_on'))


def working_day_variance(ctx: Ctx, due: str, done: str) -> int:
    """Signed working-day difference: positive = late, negative = early, 0 = on the due date."""
    if done == due:
        return 0
    c = cal(ctx)
    n = 0
    if done > due:
        d = add_days(due, 1)
        while d <= done:
            if is_working_day(d, c):
                n += 1
            d = add_days(d, 1)
        return n
    d = add_days(done, 1)
    while d <= due:
        if is_working_day(d, c):
            n -= 1
        d = add_days(d, 1)
    return n


def timeliness_of(variance: Any) -> str:
    """Early = finished ≥1 working day before due; on time = on the due date; late = after due."""
    return 'early' if variance < 0 else 'on_time' if variance == 0 else 'late'


def task_timeliness(ctx: Ctx, t: dict) -> dict[str, Any] | None:
    """Timeliness for a completed task: stored value if the reviewer approved it, else derived from dates."""
    if t.get('status') != 'done' or not js_truthy(t.get('done_on')) or is_cancelled(t):
        return None
    if js_truthy(t.get('timeliness')) and not nullish(prop(t, 'variance_days')):
        return {'timeliness': t['timeliness'], 'variance': t['variance_days']}
    v = working_day_variance(ctx, t['due_on'], t['done_on'])
    return {'timeliness': timeliness_of(v), 'variance': v}


def task_stats(ctx: Ctx, opts: dict[str, Any]) -> dict[str, Any]:
    """Task stats for a person (owner) or set of people, for tasks due or completed inside [from, to].
    opts = {'userIds'?: [...], 'projectId'?: str, 'from'?: str, 'to'?: str} (TS option names)"""
    user_ids = opts.get('userIds', UNDEF)
    ids = set(user_ids) if js_truthy(user_ids) else None
    frm = opts.get('from', UNDEF)
    to = opts.get('to', UNDEF)
    project_id = opts.get('projectId', UNDEF)

    def in_window(t: dict) -> bool:
        d = coalesce(prop(t, 'done_on'), t['due_on'])
        return (not js_truthy(frm) or d >= frm) and (not js_truthy(to) or d <= to)

    tasks = [
        t
        for t in rows(ctx, 'tasks')
        if not is_cancelled(t)
        and (ids is None or (js_truthy(t.get('owner_id')) and t['owner_id'] in ids))
        and (not js_truthy(project_id) or t['project_id'] == project_id)
        and in_window(t)
    ]
    by_task: dict[Any, list] = {}
    for r in rows(ctx, 'task_reviews'):
        by_task.setdefault(r['task_id'], []).append(r)
    s: dict[str, Any] = {
        'assigned': len(tasks),
        'open': 0,
        'overdue_open': 0,
        'completed': 0,
        'early': 0,
        'on_time': 0,
        'late': 0,
        'on_time_pct': None,
        'avg_variance_days': None,
        'in_review': 0,
        'sent_back': 0,
        'first_pass_pct': None,
        'avg_quality': None,
        'delay_reasons': {},
    }
    var_sum: Any = 0
    q_sum: Any = 0
    q_n = 0
    first_pass = 0
    reviewed_done = 0
    for t in tasks:
        if t['status'] == 'in_review':
            s['in_review'] += 1
        if t['status'] != 'done':
            s['open'] += 1
            if t['due_on'] < ctx.today:
                s['overdue_open'] += 1
        else:
            s['completed'] += 1
            tl = task_timeliness(ctx, t)
            if tl:
                s[tl['timeliness']] += 1
                var_sum += tl['variance']
            if js_truthy(t.get('quality_rating')):
                q_sum += t['quality_rating']
                q_n += 1
        code = t.get('delay_reason_code')
        if js_truthy(code):
            dr = s['delay_reasons']
            dr[code] = coalesce(dr.get(code, UNDEF), 0) + 1
        rs = by_task.get(t['id'], [])
        s['sent_back'] += len([r for r in rs if r.get('decision') == 'sent_back'])
        if t['status'] == 'done' and rs:
            reviewed_done += 1
            if not any(r.get('decision') == 'sent_back' for r in rs):
                first_pass += 1
    if s['completed']:
        s['on_time_pct'] = (s['early'] + s['on_time']) / s['completed']
        s['avg_variance_days'] = var_sum / s['completed']
    if q_n:
        s['avg_quality'] = q_sum / q_n
    if reviewed_done:
        s['first_pass_pct'] = first_pass / reviewed_done
    return s


def reports_of(ctx: Ctx, manager_id: Any) -> list[dict]:
    """Direct reports of a manager (reports_to), active only."""
    return [u for u in rows(ctx, 'users') if u.get('reports_to_id') == manager_id and u.get('status') == 'active']


def team_of(ctx: Ctx, manager_id: Any) -> list[dict]:
    """Everyone below a manager in the reporting tree."""
    all_ = [u for u in rows(ctx, 'users') if u.get('status') == 'active']
    out: list[dict] = []

    def walk(id_: Any, depth: int) -> None:
        if depth > 8:
            return
        for u in all_:
            if u.get('reports_to_id') == id_:
                out.append(u)
                walk(u['id'], depth + 1)

    walk(manager_id, 0)
    return out
