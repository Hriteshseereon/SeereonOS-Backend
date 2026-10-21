"""Port of src/mock/routes/operations.ts — Module D: operations register (UI-33): recurring duties, Mark done
roll-forward, NTF-13 reminders, and the operations summary (Feature Guide §14)."""

from __future__ import annotations

import re
from typing import Any

from app.ctx import Ctx, Req, err, find, get_db, has, has_any, insert, log_activity, notify, remove, require_action, rows, update, user_name
from app.domain.calendar import add_days, add_months, diff_days, fmt_date, fy_label, month_of, month_range, parse_iso, quarter_of, week_start
from app.engine.analytics import milestone_state
from app.js import UNDEF, coalesce, fmt_date_en_in, js_len, js_lower, js_str, js_trim, js_truthy, locale_key
from app.routes.reports import effective_week_status, is_timesheet_user

__all__ = ['period_of', 'next_due', 'ops_summary', 'register']

CATS = ['payroll', 'compliance', 'finance', 'admin', 'hr', 'it']
RECS = ['none', 'weekly', 'monthly', 'quarterly', 'yearly']
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$', re.ASCII)
# "Due" = within the next 7 days; later = upcoming.
DUE_WINDOW = 7


def period_of(due: str, rec: Any, fy_start: int = 4) -> str:
    """The period a due date belongs to: week (Monday), month, quarter or financial year."""
    if rec == 'weekly':
        return week_start(due)
    if rec == 'monthly':
        return month_of(due)
    if rec == 'quarterly':
        q = quarter_of(due, fy_start)
        return f'Q{q["q"]} {fy_label(q["fy"])}'
    if rec == 'yearly':
        return fy_label(quarter_of(due, fy_start)['fy'])
    return due


def next_due(due: str, rec: Any) -> str:
    if rec == 'weekly':
        return add_days(due, 7)
    if rec == 'monthly':
        return add_months(due, 1)
    if rec == 'quarterly':
        return add_months(due, 3)
    if rec == 'yearly':
        return add_months(due, 12)
    return due


def _group_of(ctx: Ctx, i: dict) -> str:
    if i['status'] == 'done':
        return 'done'
    if i['due_on'] < ctx.today:
        return 'overdue'
    if i['due_on'] <= add_days(ctx.today, DUE_WINDOW):
        return 'due'
    return 'upcoming'


def _view(ctx: Ctx, i: dict) -> dict:
    return {**i, 'owner_name': user_name(ctx, i.get('owner_id')), 'group': _group_of(ctx, i), 'days': diff_days(i['due_on'], ctx.today)}


def _in(b: Any, k: str) -> bool:
    """`k in b` (the JS `in` operator throws on non-objects)"""
    if isinstance(b, dict):
        return k in b
    if isinstance(b, list):
        return False
    raise TypeError(f"Cannot use 'in' operator to search for '{k}' in {js_str(b)}")


def _b(b: Any, k: str) -> Any:
    return b.get(k, UNDEF) if isinstance(b, dict) else UNDEF


def _validate(ctx: Ctx, b: Any, creating: bool) -> None:
    if creating or _in(b, 'item'):
        it = _b(b, 'item')
        if not js_truthy(it) or js_len(js_trim(js_str(it))) < 3:
            err(422, 'VALIDATION', 'Name the responsibility (at least 3 characters).', 'item')
    if creating or _in(b, 'category'):
        c = _b(b, 'category')
        if not js_truthy(c) or not (isinstance(c, str) and c in CATS):
            err(422, 'VALIDATION', 'Choose an area.', 'category')
    if creating or _in(b, 'recurring'):
        r = _b(b, 'recurring')
        if not js_truthy(r) or not (isinstance(r, str) and r in RECS):
            err(422, 'VALIDATION', 'Choose how often it repeats.', 'recurring')
    if creating or _in(b, 'due_on'):
        d = _b(b, 'due_on')
        if not js_truthy(d) or not DATE.match(js_str(d)):
            err(422, 'VALIDATION', 'Pick a due date.', 'due_on')
    if creating or _in(b, 'owner_id'):
        o = _b(b, 'owner_id')
        if not js_truthy(o) or not any(u['id'] == o and u.get('status') == 'active' for u in rows(ctx, 'users')):
            err(422, 'VALIDATION', 'Choose an active owner.', 'owner_id')


def _trim_str(v: Any) -> str:
    """v.trim() (TypeError on non-strings, like JS)"""
    if not isinstance(v, str):
        raise TypeError('x.trim is not a function')
    return js_trim(v)


def _remind_overdue(ctx: Ctx, items: list[dict]) -> None:
    """NTF-13 to the owner for overdue items — at most once per item per day."""
    tid = ctx.tenant_id
    sent = {
        f'{n["user_id"]}|{n["body"]}'
        for n in get_db().notifications
        if n.get('tenant_id') == tid and n.get('type') == 'NTF-13' and n['created_at'][:10] == ctx.today
    }
    for i in items:
        if i['group'] != 'overdue':
            continue
        days = i['days']
        body = f'{js_str(i["item"])} was due {fmt_date(i["due_on"])} ({js_str(-days)} day{"" if days == -1 else "s"} ago).'
        tag = f'{js_str(i.get("owner_id"))}|{body}'
        if tag in sent:
            continue
        sent.add(tag)
        notify(ctx, i['owner_id'], 'NTF-13', 'Ops item overdue', body, '/operations')


def list_ops(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'ops.manage', 'The operations register is for admins and operations.')
    lst = [_view(ctx, i) for i in rows(ctx, 'ops_items')]
    _remind_overdue(ctx, lst)
    oid = req.q('owner_id')
    if js_truthy(oid):
        lst = [i for i in lst if i.get('owner_id') == oid]
    cq = req.q('category')
    if js_truthy(cq):
        lst = [i for i in lst if i.get('category') == cq]
    q = js_lower(js_trim(js_str(coalesce(req.q('q'), ''))))
    if q:
        lst = [i for i in lst if q in js_lower(f'{js_str(i["item"])} {js_str(i.get("notes", UNDEF))}')]
    lst = sorted(lst, key=lambda i: locale_key(i['due_on']))
    counts = {'overdue': 0, 'due': 0, 'upcoming': 0, 'done': 0}
    for i in lst:
        counts[i['group']] += 1
    op = rows(ctx, 'openings')
    total_positions: Any = 0
    for o in op:
        if o['status'] not in ('filled', 'on_hold'):
            total_positions = total_positions + o['count']
    return {
        'items': lst,
        'counts': counts,
        'openings': {
            'open': sum(1 for o in op if o['status'] == 'open'),
            'interviewing': sum(1 for o in op if o['status'] == 'interviewing'),
            'offered': sum(1 for o in op if o['status'] == 'offered'),
            'total_positions': total_positions,
        },
        'can_edit': True,
    }


def ops_summary_route(req: Req) -> Any:
    return ops_summary(req.ctx)


def create_op(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'ops.manage', 'Only admins and operations can add ops items.')
    b = req.body
    _validate(ctx, b, True)
    i = insert(
        ctx,
        'ops_items',
        {
            'item': _trim_str(b['item']),
            'category': b['category'],
            'owner_id': b['owner_id'],
            'due_on': b['due_on'],
            'recurring': b['recurring'],
            'status': 'open',
            'last_done_on': None,
            'notes': _trim_str(coalesce(b.get('notes', UNDEF), '')),
        },
    )
    log_activity(ctx, 'ops_item', i['id'], 'create', f'Added ops item {i["item"]}')
    if i['owner_id'] != ctx.user_id:
        notify(ctx, i['owner_id'], 'NTF-13', 'New ops responsibility', f'{i["item"]} — first due {fmt_date(i["due_on"])}.', '/operations')
    return _view(ctx, i)


def patch_op(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'ops.manage', 'Only admins and operations can edit ops items.')
    cur = find(ctx, 'ops_items', req.params['id'], 'Ops item')
    b = req.body
    _validate(ctx, b, False)
    pt: dict[str, Any] = {}
    for k in ('item', 'category', 'owner_id', 'due_on', 'recurring', 'notes'):
        if _in(b, k):
            v = b[k] if isinstance(b, dict) else UNDEF
            pt[k] = js_trim(v) if isinstance(v, str) else v
    if _in(b, 'status') and isinstance(b, dict) and b['status'] in ('open', 'done') and isinstance(b['status'], str):
        pt['status'] = b['status']
    u = update(ctx, 'ops_items', cur['id'], pt, req.if_match)
    log_activity(ctx, 'ops_item', cur['id'], 'update', f'Edited ops item {js_str(u["item"])}')
    return _view(ctx, u)


def done_op(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'ops.manage', 'Only admins and operations can mark ops items done.')
    cur = find(ctx, 'ops_items', req.params['id'], 'Ops item')
    if cur['status'] == 'done':
        err(409, 'OPS_DONE', 'This item is already done.')
    raw_on = req.b('done_on')
    on = js_str(raw_on) if DATE.match(js_str(coalesce(raw_on, ''))) else ctx.today
    if on > ctx.today:
        err(422, 'VALIDATION', 'The done date cannot be in the future.', 'done_on')
    fy_s = coalesce(ctx.tenant['settings'].get('company.fy_start_month', UNDEF), 4)
    hist = [
        *coalesce(cur.get('history', UNDEF), []),
        {'due_on': cur['due_on'], 'done_on': on, 'period': period_of(cur['due_on'], cur['recurring'], fy_s), 'on_time': on <= cur['due_on'], 'by': ctx.user_id},
    ][-80:]
    pt: dict[str, Any] = {'last_done_on': on, 'history': hist}
    if cur['recurring'] == 'none':
        pt['status'] = 'done'
    else:
        # roll forward; if it was very late, keep rolling until the next due is after the done date
        nxt = next_due(cur['due_on'], cur['recurring'])
        guard = 0
        while nxt <= on:
            g = guard
            guard += 1
            if not g < 60:
                break
            nxt = next_due(nxt, cur['recurring'])
        pt['due_on'] = nxt
    u = update(ctx, 'ops_items', cur['id'], pt, req.if_match)
    # the TS reads cur.due_on AFTER update() changed that same object: the rolled-forward due date
    cur = u
    late = on > cur['due_on']
    late_s = f' ({diff_days(on, cur["due_on"])}d late)' if late else ''
    next_s = f'; next due {u["due_on"]}' if u['status'] == 'open' else ''
    log_activity(ctx, 'ops_item', cur['id'], 'done', f'Marked {js_str(cur["item"])} done{late_s}{next_s}')
    return {'item': _view(ctx, u), 'late': late, 'next_due': u['due_on'] if u['status'] == 'open' else None}


def delete_op(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'ops.manage', 'Only admins and operations can delete ops items.')
    cur = find(ctx, 'ops_items', req.params['id'], 'Ops item')
    remove(ctx, 'ops_items', cur['id'], req.if_match)
    log_activity(ctx, 'ops_item', cur['id'], 'delete', f'Deleted ops item {js_str(cur["item"])}')
    return {'ok': True}


def register(r: Any) -> None:
    r.get('/ops', list_ops)
    # Feature Guide §14: recurring-duty KPIs, due & overdue occurrences, and the other operations tabs' summaries.
    r.get('/ops/summary', ops_summary_route)
    r.post('/ops', create_op)
    r.patch('/ops/:id', patch_op)
    r.post('/ops/:id/done', done_op)
    r.delete('/ops/:id', delete_op)


def _occurrences(ctx: Ctx, items: list[dict]) -> list[dict]:
    """Occurrences of open duties that are overdue or due within the next 10 days (a missed weekly duty shows each missed week)."""
    t = ctx.today
    horizon = add_days(t, 10)
    fy_s = coalesce(ctx.tenant['settings'].get('company.fy_start_month', UNDEF), 4)
    out: list[dict] = []
    for i in items:
        if i['status'] != 'open':
            continue
        d = i['due_on']
        first = True
        g = 0
        while d <= horizon:
            gg = g
            g += 1
            if not gg < 60:
                break
            out.append(
                {
                    'key': f'{i["id"]}:{d}',
                    'id': i['id'],
                    'item': i['item'],
                    'category': i.get('category', UNDEF),
                    'recurring': i['recurring'],
                    'owner_id': i.get('owner_id', UNDEF),
                    'owner_name': user_name(ctx, i.get('owner_id')),
                    'due_on': d,
                    'period': period_of(d, i['recurring'], fy_s),
                    'days': diff_days(d, t),
                    'status': 'overdue' if d < t else 'upcoming',
                    'current': first,
                    'version': i['version'],
                }
            )
            first = False
            if i['recurring'] == 'none':
                break
            d = next_due(d, i['recurring'])
    return sorted(out, key=lambda o: (locale_key(o['due_on']), locale_key(o['item'])))


def _lbl(m: str) -> str:
    """new Date(y, m - 1, 1).toLocaleDateString('en-IN', { month: 'short', year: 'numeric' })"""
    d = parse_iso(f'{m[:7]}-01')
    return fmt_date_en_in(d, month='short', year='numeric') if d is not None else 'Invalid Date'


def ops_summary(ctx: Ctx) -> dict[str, Any]:
    if not has_any(ctx, 'ops.manage', 'finance.view'):
        err(403, 'FORBIDDEN', 'Operations is for admins and operations.')
    t = ctx.today
    items = rows(ctx, 'ops_items')
    occ = _occurrences(ctx, items)
    since = add_days(t, -90)
    hist = [h for i in items for h in coalesce(i.get('history', UNDEF), []) if h['due_on'] >= since and h['due_on'] <= t]
    missed = [o for o in occ if o['status'] == 'overdue' and o['due_on'] >= since]
    on_time = sum(1 for h in hist if js_truthy(h.get('on_time')))
    denom = len(hist) + len(missed)
    # people & timesheets
    people = [u for u in rows(ctx, 'users') if is_timesheet_user(u)]
    last_mon = add_days(week_start(t), -7)
    weeks = rows(ctx, 'timesheet_weeks')
    by_key: dict[tuple, dict] = {}
    for w in weeks:
        by_key.setdefault((w['user_id'], w['week_start']), w)
    missing = 0
    for u in people:
        st = effective_week_status(ctx, by_key.get((u['id'], last_mon)), last_mon)
        if st not in ('approved', 'submitted'):
            missing += 1
    # payroll
    runs = rows(ctx, 'payroll_runs')
    ym = month_of(t)
    lm = month_of(add_months(ym + '-01', -1))
    cur = next((r for r in runs if r['month'] == ym), None)
    last = next((r for r in runs if r['month'] == lm), None)
    # invoicing & collections
    can_money = has(ctx, 'finance.view')
    invoices = rows(ctx, 'invoices')
    outstanding = [i for i in invoices if i['amount_paise'] > i['paid_amount_paise']]
    overdue = [i for i in outstanding if i['due_on'] < t]
    live = {pr['id'] for pr in rows(ctx, 'projects') if pr['status'] not in ('closed', 'npa')}
    # "Achieved" = formally achieved (achieved_on set) OR all of the milestone's tasks done (milestoneState achieved_*).
    live_tasks = [x for x in rows(ctx, 'tasks') if x['project_id'] in live]
    achieved = [
        m
        for m in rows(ctx, 'milestones')
        if not js_truthy(m.get('deleted_at'))
        and m['project_id'] in live
        and m['billing_status'] == 'none'
        and m['billing_amount_paise'] > 0
        and milestone_state(ctx, m, live_tasks)['state'].startswith('achieved')
    ]
    mr = month_range(ym)
    collected: Any = 0
    for pmt in rows(ctx, 'payments'):
        if pmt['paid_on'] >= mr['from'] and pmt['paid_on'] <= mr['to']:
            collected = collected + pmt['amount_paise']

    def sum_out(lst: list[dict]) -> Any:
        s: Any = 0
        for i in lst:
            s = s + i['amount_paise'] - i['paid_amount_paise']
        return s

    ach_sum: Any = 0
    for m in achieved:
        ach_sum = ach_sum + m['billing_amount_paise']
    return {
        'kpis': {
            'on_time_90': on_time / denom if denom else None,
            'done_90': len(hist),
            'on_time_90_count': on_time,
            'overdue_now': sum(1 for o in occ if o['status'] == 'overdue'),
            'due_10': sum(1 for o in occ if o['status'] == 'upcoming'),
            'tracked': sum(1 for i in items if i['status'] == 'open'),
        },
        'occurrences': occ,
        'people': {
            'can': has_any(ctx, 'people.view_all', 'team.view', 'ops.manage'),
            'people': len(people),
            'missing_last_week': missing,
            'pending_approval': sum(1 for w in weeks if w['status'] == 'submitted'),
            'on_leave_today': sum(1 for lv in rows(ctx, 'leave') if lv['date'] == t),
            'last_week': last_mon,
        },
        'payroll': {
            'can': has_any(ctx, 'payroll.run', 'payroll.finalise'),
            'month': ym,
            'label': _lbl(ym),
            'status': cur['status'] if cur is not None else 'not_started',
            'run_id': cur['id'] if cur is not None else None,
            'last_label': _lbl(lm),
            'last_status': last['status'] if last is not None else 'not_started',
            'last_run_id': last['id'] if last is not None else None,
        },
        'invoicing': {
            'can': can_money,
            'outstanding': sum_out(outstanding) if can_money else 0,
            'overdue_count': len(overdue) if can_money else 0,
            'overdue_amount': sum_out(overdue) if can_money else 0,
            'achieved_not_invoiced': ach_sum if can_money else 0,
            'achieved_count': len(achieved) if can_money else 0,
            'collected_month': collected if can_money else 0,
        },
    }
