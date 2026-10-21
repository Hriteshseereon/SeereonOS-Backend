"""Port of src/mock/routes/company.ts — company layer (v3): approvals inbox, goals & OKRs, audit trail.
The inbox is computed straight from the other areas' tables (role- and reporting-line-scoped); its inline actions call
those areas' own contract endpoints from the client. The routes of other modules it needs (timesheets, taskflow,
workflowsV7) are imported lazily, inside the functions."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    calendar_for,
    err,
    find,
    get_db,
    has,
    has_any,
    insert,
    log_activity,
    permissions_of,
    remove,
    rows,
    update,
    user_name,
)
from app.domain.calendar import (
    add_days,
    add_working_days,
    diff_days,
    fmt_date,
    is_working_day,
    quarter_of,
    week_label,
    week_start,
    working_days_between,
)
from app.domain.permission_catalog import is_permission_key
from app.domain.sales import deal_value
from app.engine import workflows as wf_engine
from app.engine.analytics import cal, revenue_between, sla_due
from app.engine.maker_checker import changes_to_check, mc_config
from app.engine.performance import task_stats, team_of, working_day_variance
from app.engine.workflows import escalate_if_due, escalation_target, step_due, step_label
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    js_len,
    js_lower,
    js_max,
    js_number,
    js_or,
    js_round,
    js_slice,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    prop,
)

_AC = cat('api/company')
ENTITY_LABEL = _AC['ENTITY_LABEL']
METRIC_META = _AC['METRIC_META']


# =====================================================================================================================
# Shared
# =====================================================================================================================
def _S(ctx: Ctx) -> Any:
    return ctx.tenant['settings']


def _users_by_id(ctx: Ctx) -> dict[Any, dict]:
    """first user per id (rows(ctx, 'users').find(x => x.id === id))"""
    out: dict[Any, dict] = {}
    for u in rows(ctx, 'users'):
        out.setdefault(u['id'], u)
    return out


def person_of(ctx: Ctx, id_: Any, users: dict[Any, dict] | None = None) -> dict[str, Any] | None:
    if not js_truthy(id_):
        return None
    u = (users if users is not None else _users_by_id(ctx)).get(id_)
    return {'id': u['id'], 'name': u['name'], 'title': u['title'], 'hue': u['avatar_hue']} if u else None


def _date_of(s: Any) -> Any:
    return s[:10] if js_truthy(s) else None


def _body(req: Req) -> dict:
    return req.body if isinstance(req.body, dict) else {}


# =====================================================================================================================
# Approvals inbox
# =====================================================================================================================
GROUP_OF = {
    'timesheet': 'timesheets',
    'unlock': 'timesheets',
    'task_review': 'tasks',
    'leave': 'leave',
    'expense': 'expenses',
    'purchase_order': 'purchases',
    'vendor_bill': 'bills',
    'appraisal': 'appraisals',
    'change_request': 'other',
    'document': 'other',
    'change': 'changes',
    'workflow': 'other',
}
SCREEN_OF = {
    'timesheet': 'Timesheet approvals',
    'unlock': 'Timesheet approvals',
    'task_review': 'Task',
    'leave': 'Leave',
    'expense': 'Expenses',
    'purchase_order': 'Purchases',
    'vendor_bill': 'Purchases',
    'appraisal': 'Appraisals',
    'change_request': 'Project changes',
    'document': 'Documents',
    'change': 'Change approvals',
    'workflow': 'Workflows',
}


def _wf_extras(ctx: Ctx, rec: Any, fallback_due: Any) -> dict[str, Any]:
    """Workflow extras for an item: step label, step deadline (overrides the generic 2-day SLA) and escalation target."""
    tgt = escalation_target(ctx, rec)
    return {
        'step_label': step_label(rec),
        'due_at': coalesce(step_due(ctx, rec), fallback_due),
        'escalates_to': coalesce(tgt.get('name', UNDEF) if tgt else UNDEF, None),
    }


def _sweep_escalations(ctx: Ctx) -> None:
    """Lazy escalation: anything past its step deadline with an escalation target moves on when the inbox is read."""
    for x in list(rows(ctx, 'leave_requests')):
        if x['status'] == 'pending':
            escalate_if_due(ctx, 'leave_requests', x, 'leave_request', f'{user_name(ctx, x["user_id"])}’s leave from {fmt_date(x["from_on"])}', '/leave')
    for x in list(rows(ctx, 'expense_claims')):
        if x['status'] == 'submitted':
            escalate_if_due(
                ctx, 'expense_claims', x, 'expense', f'{user_name(ctx, x["user_id"])}’s expense claim “{js_str(x["description"])}”', '/expenses?tab=approve'
            )
    for x in list(rows(ctx, 'purchase_orders')):
        if x['status'] == 'pending':
            escalate_if_due(ctx, 'purchase_orders', x, 'purchase_order', x['number'], '/purchases?tab=pos')
    for x in list(rows(ctx, 'vendor_bills')):
        if x['status'] == 'received':
            escalate_if_due(ctx, 'vendor_bills', x, 'vendor_bill', f'Bill {js_str(x["number"])}', '/purchases?tab=bills')
    for x in list(rows(ctx, 'timesheet_weeks')):
        if x['status'] == 'submitted':
            escalate_if_due(ctx, 'timesheet_weeks', x, 'timesheet_week', f'{user_name(ctx, x["user_id"])}’s {week_label(x["week_start"])}', '/approvals')
    for x in list(rows(ctx, 'tasks')):
        if x['status'] == 'in_review':
            escalate_if_due(ctx, 'tasks', x, 'task', f'Task “{js_str(x["title"])}”', '/team-tasks?tab=review')


def _due2(ctx: Ctx, submitted: Any) -> Any:
    if not js_truthy(submitted):
        return None
    return add_working_days(submitted[:10], coalesce(prop(_S(ctx), 'approval.sla_working_days'), 2), cal(ctx))


def _mk(ctx: Ctx, p: dict[str, Any]) -> dict[str, Any]:
    due_at = p.get('due_at', UNDEF)
    overdue = js_truthy(due_at) and ctx.today > due_at
    sub = _date_of(p.get('submitted_at'))
    return {
        **p,
        'id': f'{p["kind"]}:{p["entity_id"]}:{p["stage"]}',
        'group': GROUP_OF[p['kind']],
        'screen': SCREEN_OF[p['kind']],
        'overdue': overdue,
        'days_waiting': js_max(0, diff_days(ctx.today, sub)) if js_truthy(sub) else 0,
        'bulk': js_truthy(p.get('bulk', UNDEF)) and len(p['warnings']) == 0,
    }


def _week_short_days(ctx: Ctx, w: dict, entries: list[dict]) -> tuple[Any, int]:
    """Short working days in a submitted week (past working days below the minimum)."""
    min_h = prop(_S(ctx), 'calendar.min_hours_per_day')
    c = calendar_for(ctx, w['user_id'])
    total: Any = 0
    short = 0
    for i in range(7):
        d = add_days(w['week_start'], i)
        h: Any = 0
        for e in entries:
            if e['work_date'] == d:
                h += e['hours']
        total += h
        if d <= ctx.today and is_working_day(d, c) and h < min_h:
            short += 1
    return js_round(total * 100) / 100, short


def _delegators_of(ctx: Ctx, me: Any) -> set:
    t = ctx.today
    return {d['from_user_id'] for d in rows(ctx, 'delegations') if d.get('to_user_id') == me and d['from_on'] <= t and d['to_on'] >= t}


def _project_members(ctx: Ctx) -> dict[Any, set]:
    m: dict[Any, set] = {}

    def add(p: Any, u: Any) -> None:
        if not js_truthy(u):
            return
        m.setdefault(p, set()).add(u)

    for t in rows(ctx, 'tasks'):
        add(t['project_id'], t.get('owner_id'))
        add(t['project_id'], t.get('backup_id'))
    for a in rows(ctx, 'allocations'):
        add(a['project_id'], a['user_id'])
    for p in rows(ctx, 'projects'):
        add(p['id'], p.get('pm_id'))
    return m


def _can_see_doc(ctx: Ctx, d: dict, members: dict[Any, set], projects: dict[Any, dict]) -> bool:
    """Mirrors the documents area's visibility rule."""
    if js_truthy(d.get('archived')):
        return False
    if d.get('visibility') == 'company':
        return True
    if has(ctx, 'documents.view_all'):
        return True
    if not js_truthy(d.get('project_id')):
        return has_any(ctx, 'documents.manage', 'documents.company')
    p = projects.get(d['project_id'])
    if p is not None and p.get('pm_id') == ctx.user_id:
        return True
    if d.get('visibility') == 'managers':
        return False
    return ctx.user_id in members.get(d['project_id'], ())


def _cat_label(c: str) -> str:
    return c.replace('_', ' ')


def inbox_items(ctx: Ctx) -> list[dict]:
    from app.routes.taskflow import reviewer_of
    from app.routes.timesheets import can_decide, sweep_locks
    from app.routes.workflows_v7 import wf_inbox_rows, wf_linked_appraisals

    if ctx.is_super and ctx.tenant_id == '__none__':
        return []
    me = ctx.user_id
    # Overrides — each inbox item follows the permission that lets someone decide it outside the routed approver:
    task_admin = has(ctx, 'projects.manage_all')  # review any task / handle any change request
    leave_admin = has(ctx, 'people.manage') and has(ctx, 'leave.approve')  # unrouted leave
    spend_admin = has(ctx, 'budgets.manage')  # unrouted claims, purchase orders, vendor bill approval
    out: list[dict] = []
    users = {u['id']: u for u in rows(ctx, 'users')}
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    dele = _delegators_of(ctx, me)

    def mine(approver: Any) -> bool:
        return js_truthy(approver) and (approver == me or approver in dele)

    def pers(id_: Any) -> Any:
        return person_of(ctx, id_, users)

    def uname(u: Any) -> Any:
        return coalesce(u.get('name', UNDEF) if u else UNDEF, '—')

    min_h = prop(_S(ctx), 'calendar.min_hours_per_day')

    # ---- timesheets + unlock requests ----
    sweep_locks(ctx)
    if not ctx.view_as:
        _sweep_escalations(ctx)
    entries_by_week: dict[Any, list] = {}
    for e in rows(ctx, 'timesheet_entries'):
        entries_by_week.setdefault(e['week_id'], []).append(e)
    for w in rows(ctx, 'timesheet_weeks'):
        unlock = w['status'] == 'locked' and js_truthy(w.get('unlock_requested_at'))
        if w['status'] != 'submitted' and not unlock:
            continue
        d = can_decide(ctx, w)
        if not d['ok']:
            continue
        u = users.get(w['user_id'])
        if unlock:
            out.append(
                _mk(
                    ctx,
                    {
                        'kind': 'unlock',
                        'stage': 'review',
                        'entity_id': w['id'],
                        'version': w['version'],
                        'title': f'Unlock {week_label(w["week_start"])}',
                        'subtitle': f'{js_str(uname(u))} · locked week',
                        'requester': pers(w['user_id']),
                        'amount': None,
                        'submitted_at': w['unlock_requested_at'],
                        'due_at': _due2(ctx, w['unlock_requested_at']),
                        'link': '/approvals',
                        'actions': ['open'],
                        'warnings': [],
                        'note': prop(w, 'unlock_reason'),
                        'via_delegation': d['viaDelegation'],
                        'week_start': w['week_start'],
                        'user_id': w['user_id'],
                        'facts': [
                            {'label': 'Week', 'value': week_label(w['week_start']), 'type': 'text'},
                            {'label': 'Requested', 'value': w['unlock_requested_at'], 'type': 'datetime'},
                        ],
                    },
                )
            )
            continue
        total, short = _week_short_days(ctx, w, entries_by_week.get(w['id'], []))
        due = sla_due(ctx, w['submitted_at']) if js_truthy(w.get('submitted_at')) else None
        warnings = [f'{short} working day{"s" if short > 1 else ""} below {js_str(min_h)}h'] if short else []
        out.append(
            _mk(
                ctx,
                {
                    'kind': 'timesheet',
                    'stage': 'approve',
                    'entity_id': w['id'],
                    'version': w['version'],
                    'title': f'Timesheet · {week_label(w["week_start"])}',
                    'subtitle': f'{js_str(uname(u))} · {js_str(total)}h',
                    'requester': pers(w['user_id']),
                    'amount': None,
                    'submitted_at': w.get('submitted_at'),
                    'link': '/approvals',
                    'actions': ['approve', 'reject'],
                    'warnings': warnings,
                    'note': None,
                    'via_delegation': d['viaDelegation'],
                    'week_start': w['week_start'],
                    'user_id': w['user_id'],
                    'bulk': True,
                    **_wf_extras(ctx, w, due),
                    'facts': [
                        {'label': 'Week', 'value': week_label(w['week_start']), 'type': 'text'},
                        {'label': 'Total', 'value': total, 'type': 'hours'},
                        {'label': 'Short days', 'value': short, 'type': 'number', 'tone': 'danger' if short else UNDEF},
                        {'label': 'Submitted', 'value': w.get('submitted_at'), 'type': 'datetime'},
                    ],
                },
            )
        )

    # ---- task reviews ----
    reviews_by_task: dict[Any, list] = {}
    for r in rows(ctx, 'task_reviews'):
        reviews_by_task.setdefault(r['task_id'], []).append(r)
    for t in rows(ctx, 'tasks'):
        if t['status'] != 'in_review':
            continue
        p = projects.get(t['project_id'])
        reviewer = reviewer_of(ctx, t)
        if t.get('owner_id') == me and not task_admin:
            continue
        if not (task_admin or mine(reviewer)):
            continue
        if task_admin and t.get('owner_id') == me and reviewer != me:
            continue
        cands = sorted(
            [r for r in reviews_by_task.get(t['id'], ()) if not js_truthy(r.get('decision'))], key=lambda r: locale_key(r['submitted_at']), reverse=True
        )
        open_ = cands[0] if cands else None
        so = t.get('submitted_on')
        submitted_at = coalesce(open_.get('submitted_at', UNDEF) if open_ else UNDEF, so + 'T09:00:00Z' if js_truthy(so) else t['updated_at'])
        sub_day = submitted_at[:10]
        variance = working_day_variance(ctx, t['due_on'], sub_day)
        round_ = coalesce(open_.get('round', UNDEF) if open_ else UNDEF, prop(t, 'review_round'), 1)
        warnings = []
        if variance > 0 and not js_truthy(coalesce(prop(open_, 'delay_reason_code'), prop(t, 'delay_reason_code'))):
            warnings.append('Submitted late without a delay reason')
        if variance < 0:
            timeliness = f'{js_str(-variance)} working day{"s" if variance < -1 else ""} early'
        elif variance == 0:
            timeliness = 'On the due date'
        else:
            timeliness = f'{js_str(variance)} working day{"s" if variance > 1 else ""} late'
        facts: list[dict] = [
            {'label': 'Project', 'value': f'{js_str(p["code"])} · {js_str(p["name"])}' if p else '—', 'type': 'text'},
            {'label': 'Due', 'value': t['due_on'], 'type': 'date'},
            {'label': 'Submitted', 'value': submitted_at, 'type': 'datetime'},
            {'label': 'Timeliness', 'value': timeliness, 'type': 'text', 'tone': 'danger' if variance > 0 else 'success'},
            {'label': 'Estimate', 'value': t['est_hours'], 'type': 'hours'},
            {'label': 'Review round', 'value': round_, 'type': 'number'},
        ]
        if js_truthy(prop(open_, 'delay_reason')) or js_truthy(prop(t, 'delay_reason')):
            facts.append({'label': 'Delay reason', 'value': coalesce(prop(open_, 'delay_reason'), prop(t, 'delay_reason')), 'type': 'text'})
        if js_truthy(prop(open_, 'proof_url')):
            facts.append({'label': 'Proof', 'value': open_['proof_url'], 'type': 'text'})  # type: ignore[index]
        pcode = coalesce(p.get('code', UNDEF) if p else UNDEF, '')
        out.append(
            _mk(
                ctx,
                {
                    'kind': 'task_review',
                    'stage': 'review',
                    'entity_id': t['id'],
                    'version': t['version'],
                    'task_id': t['id'],
                    'title': t['title'],
                    'subtitle': f'{js_str(pcode)} · {user_name(ctx, t.get("owner_id"))}' + (f' · round {js_str(round_)}' if round_ > 1 else ''),
                    'requester': pers(t.get('owner_id')),
                    'amount': None,
                    'submitted_at': submitted_at,
                    'link': f'/tasks/{t["id"]}',
                    'actions': ['approve', 'send_back'],
                    'warnings': warnings,
                    'note': js_or(prop(open_, 'submission_note'), None),
                    'via_delegation': reviewer != me and mine(reviewer),
                    **_wf_extras(ctx, t, _due2(ctx, submitted_at)),
                    'facts': facts,
                },
            )
        )

    # ---- leave requests ----
    for lr in rows(ctx, 'leave_requests'):
        if lr['status'] != 'pending' or lr['user_id'] == me:
            continue
        if not (mine(lr.get('approver_id')) or (leave_admin and not js_truthy(lr.get('approver_id')))):
            continue
        u = users.get(lr['user_id'])
        rng = fmt_date(lr['from_on']) if lr['from_on'] == lr['to_on'] else f'{fmt_date(lr["from_on"])} – {fmt_date(lr["to_on"])}'
        warnings = ['Starts today or has already started'] if lr['from_on'] <= ctx.today else []
        kind_txt = 'leave without pay' if lr['type'] == 'lwp' else f'{js_str(lr["type"])} leave'
        out.append(
            _mk(
                ctx,
                {
                    'kind': 'leave',
                    'stage': 'approve',
                    'entity_id': lr['id'],
                    'version': lr['version'],
                    'title': f'{js_str(lr["days"])} day{"" if lr["days"] == 1 else "s"} {kind_txt}',
                    'subtitle': f'{js_str(uname(u))} · {rng}',
                    'requester': pers(lr['user_id']),
                    'amount': None,
                    'submitted_at': lr['created_at'],
                    'link': '/leave',
                    'actions': ['approve', 'reject'],
                    'warnings': warnings,
                    'note': js_or(prop(lr, 'reason'), None),
                    'via_delegation': lr.get('approver_id') != me and mine(lr.get('approver_id')),
                    **_wf_extras(ctx, lr, _due2(ctx, lr['created_at'])),
                    'facts': [
                        {'label': 'From', 'value': lr['from_on'], 'type': 'date'},
                        {'label': 'To', 'value': lr['to_on'], 'type': 'date'},
                        {'label': 'Days', 'value': lr['days'], 'type': 'number'},
                        {'label': 'Portion', 'value': 'Half day' if lr.get('portion') == 'half' else 'Full day', 'type': 'text'},
                    ],
                },
            )
        )

    # ---- expenses: approve (approver) and pay (finance) ----
    can_pay = has(ctx, 'expenses.pay')
    for x in rows(ctx, 'expense_claims'):
        u = users.get(x['user_id'])
        p = projects.get(x['project_id']) if js_truthy(x.get('project_id')) else None
        base = {
            'kind': 'expense',
            'entity_id': x['id'],
            'version': x['version'],
            'requester': pers(x['user_id']),
            'amount': x['amount_paise'],
            'link': '/expenses',
            'note': js_or(x.get('description'), None),
            'facts': [
                {'label': 'Category', 'value': _cat_label(x['category']), 'type': 'text'},
                {'label': 'Spent on', 'value': x['spent_on'], 'type': 'date'},
                {'label': 'Amount', 'value': x['amount_paise'], 'type': 'money'},
                {'label': 'Project', 'value': f'{js_str(p["code"])} · {js_str(p["name"])}' if p else 'None', 'type': 'text'},
                {'label': 'Billable to client', 'value': 'Yes' if js_truthy(x.get('billable_to_client')) else 'No', 'type': 'text'},
                {
                    'label': 'Receipt',
                    'value': 'Attached' if js_truthy(x.get('receipt_file_id')) else 'Missing',
                    'type': 'text',
                    'tone': UNDEF if js_truthy(x.get('receipt_file_id')) else 'danger',
                },
            ],
        }
        if x['status'] == 'submitted' and x['user_id'] != me and (mine(x.get('approver_id')) or (spend_admin and not js_truthy(x.get('approver_id')))):
            warnings = []
            if not js_truthy(x.get('receipt_file_id')):
                warnings.append('No receipt attached')
            if diff_days(ctx.today, x['spent_on']) > 30:
                warnings.append('Spent more than 30 days ago')
            if js_truthy(x.get('billable_to_client')) and not js_truthy(x.get('project_id')):
                warnings.append('Billable to a client but no project chosen')
            out.append(
                _mk(
                    ctx,
                    {
                        **base,
                        'stage': 'approve',
                        'title': js_or(x.get('description'), 'Expense claim'),
                        'subtitle': f'{js_str(uname(u))} · {_cat_label(x["category"])}',
                        'submitted_at': x['updated_at'],
                        'actions': ['approve', 'reject'],
                        'warnings': warnings,
                        'bulk': True,
                        **_wf_extras(ctx, x, _due2(ctx, x['updated_at'])),
                    },
                )
            )
        elif x['status'] == 'approved' and not js_truthy(x.get('paid_on')) and can_pay and x['user_id'] != me:
            out.append(
                _mk(
                    ctx,
                    {
                        **base,
                        'stage': 'pay',
                        'title': f'Pay: {js_str(js_or(x.get("description"), "expense claim"))}',
                        'subtitle': f'{js_str(uname(u))} · approved by {user_name(ctx, x.get("decided_by"))}',
                        'submitted_at': x.get('decided_at'),
                        'due_at': _due2(ctx, x.get('decided_at')),
                        'actions': ['pay'],
                        'warnings': [],
                    },
                )
            )

    # ---- purchase orders ----
    vendors = {v['id']: v for v in rows(ctx, 'vendors')}
    for po in rows(ctx, 'purchase_orders'):
        if po['status'] != 'pending' or po.get('requested_by') == me:
            continue
        if not (spend_admin or mine(po.get('approver_id'))):
            continue
        v = vendors.get(po.get('vendor_id'))
        p = projects.get(po['project_id']) if js_truthy(po.get('project_id')) else None
        vname = coalesce(v.get('name', UNDEF) if v else UNDEF, 'Vendor')
        note = '; '.join(f'{js_str(ln["qty"])} × {js_str(ln["description"])}' for ln in coalesce(prop(po, 'lines'), []))
        out.append(
            _mk(
                ctx,
                {
                    'kind': 'purchase_order',
                    'stage': 'approve',
                    'entity_id': po['id'],
                    'version': po['version'],
                    'title': f'{js_str(po["number"])} · {js_str(vname)}',
                    'subtitle': f'{user_name(ctx, po.get("requested_by"))} · {js_str(po["budget_category"])}',
                    'requester': pers(po.get('requested_by')),
                    'amount': po['amount_paise'],
                    'submitted_at': po['updated_at'],
                    'link': '/purchases',
                    'actions': ['approve', 'reject'],
                    'warnings': [],
                    'note': js_or(note, None),
                    **_wf_extras(ctx, po, _due2(ctx, po['updated_at'])),
                    'facts': [
                        {'label': 'Vendor', 'value': coalesce(v.get('name', UNDEF) if v else UNDEF, '—'), 'type': 'text'},
                        {'label': 'Amount', 'value': po['amount_paise'], 'type': 'money'},
                        {'label': 'GST', 'value': f'{js_str(po["gst_pct"])}%', 'type': 'text'},
                        {'label': 'Budget', 'value': po['budget_category'], 'type': 'text'},
                        {'label': 'Project', 'value': f'{js_str(p["code"])} · {js_str(p["name"])}' if p else 'None', 'type': 'text'},
                        {'label': 'Needed by', 'value': prop(po, 'needed_by'), 'type': 'date'},
                    ],
                },
            )
        )

    # ---- vendor bills ----
    pos = rows(ctx, 'purchase_orders')
    for b in rows(ctx, 'vendor_bills'):
        if b['status'] != 'received' and b['status'] != 'approved':
            continue
        v = vendors.get(b.get('vendor_id'))
        pay = b['status'] == 'approved'
        # approve: the bill's workflow approver or spend authority; pay: procurement.manage
        b_approver = b.get('approver_id')
        if (not has(ctx, 'procurement.manage')) if pay else not (spend_admin or (mine(b_approver) and b.get('created_by') != me)):
            continue
        po = next((x for x in pos if x['id'] == b['po_id']), None) if js_truthy(b.get('po_id')) else None
        warnings = []
        if not pay and not js_truthy(b.get('po_id')):
            warnings.append('No purchase order linked')
        if not pay and po and b['amount_paise'] > po['amount_paise']:
            warnings.append('Bill is higher than its purchase order')
        vname = coalesce(v.get('name', UNDEF) if v else UNDEF, 'Vendor')
        item = {
            'kind': 'vendor_bill',
            'stage': 'pay' if pay else 'approve',
            'entity_id': b['id'],
            'version': b['version'],
            'title': f'{"Pay" if pay else "Approve"} bill {js_str(b["number"])}',
            'subtitle': f'{js_str(vname)} · due {fmt_date(b["due_on"])}',
            'requester': None,
            'amount': b['amount_paise'] + b['gst_paise'] - b['tds_paise'],
            'submitted_at': b['updated_at'] if pay else b['bill_date'] + 'T09:00:00Z',
            'link': '/purchases',
            'actions': ['pay' if pay else 'approve'],
            'warnings': warnings,
            'note': None,
            **({'due_at': b['due_on']} if pay else _wf_extras(ctx, b, _due2(ctx, b['bill_date']))),
            'facts': [
                {'label': 'Vendor', 'value': coalesce(v.get('name', UNDEF) if v else UNDEF, '—'), 'type': 'text'},
                {'label': 'Bill date', 'value': b['bill_date'], 'type': 'date'},
                {'label': 'Due', 'value': b['due_on'], 'type': 'date', 'tone': 'danger' if b['due_on'] < ctx.today else UNDEF},
                {'label': 'Amount', 'value': b['amount_paise'], 'type': 'money'},
                {'label': 'GST', 'value': b['gst_paise'], 'type': 'money'},
                {'label': 'TDS', 'value': b['tds_paise'], 'type': 'money'},
                {'label': 'Purchase order', 'value': coalesce(po.get('number', UNDEF) if po else UNDEF, 'None'), 'type': 'text'},
            ],
        }
        out.append(_mk(ctx, item))

    # ---- appraisals ----
    cycles = {c['id']: c for c in rows(ctx, 'appraisal_cycles')}
    hr = has(ctx, 'appraisals.manage')
    wf_managed = wf_linked_appraisals(ctx)  # v7: these appear below as workflow steps instead
    for a in rows(ctx, 'appraisals'):
        if a['id'] in wf_managed:
            continue
        c = cycles.get(a.get('cycle_id'))
        as_manager = a['status'] == 'manager_pending' and mine(a.get('manager_id'))
        as_hr = a['status'] == 'hr_pending' and hr and a['user_id'] != me
        if not as_manager and not as_hr:
            continue
        u = users.get(a['user_id'])
        submitted = prop(a, 'submitted_self_at') if as_manager else prop(a, 'submitted_manager_at')
        due = (prop(c, 'manager_due') if as_manager else prop(c, 'hr_due')) if c else _due2(ctx, submitted)
        facts = [
            {'label': 'Cycle', 'value': coalesce(c.get('name', UNDEF) if c else UNDEF, '—'), 'type': 'text'},
            {'label': 'Self rating', 'value': prop(a, 'self_rating'), 'type': 'number'},
        ]
        if as_hr:
            facts.append({'label': 'Manager rating', 'value': prop(a, 'manager_rating'), 'type': 'number'})
        facts.append({'label': 'Due', 'value': due, 'type': 'date'})
        out.append(
            _mk(
                ctx,
                {
                    'kind': 'appraisal',
                    'stage': 'review',
                    'entity_id': a['id'],
                    'version': a['version'],
                    'title': f'{"Manager" if as_manager else "HR"} review · {js_str(uname(u))}',
                    'subtitle': f'{js_str(coalesce(c.get("name", UNDEF) if c else UNDEF, "Appraisal"))} · {"self-review done" if as_manager else "manager review done"}',
                    'requester': pers(a['user_id']),
                    'amount': None,
                    'submitted_at': submitted,
                    'due_at': due,
                    'link': f'/appraisals/{a["id"]}',
                    'actions': ['open'],
                    'warnings': [],
                    'note': js_or(prop(a, 'self_comments') if as_manager else prop(a, 'manager_comments'), None),
                    'facts': facts,
                },
            )
        )

    # ---- v7 workflow steps waiting for me (owner: workflows — src/mock/routes/workflowsV7.ts) ----
    for w in wf_inbox_rows(ctx):
        item = _mk(
            ctx,
            {
                'kind': 'workflow',
                'stage': 'review',
                'entity_id': w['id'],
                'version': w['version'],
                'title': w['title'],
                'subtitle': f'{js_str(w["template_name"])} · {js_str(w["stage_name"])}',
                'requester': pers(w.get('subject_id')),
                'amount': None,
                'submitted_at': w.get('started_at'),
                'due_at': prop(w, 'due_on'),
                'link': f'/workflows/run/{w["id"]}',
                'actions': ['open'],
                'warnings': [],
                'note': js_or(prop(w, 'note'), None),
                'via_delegation': prop(w, 'via_delegation'),
                'step_label': prop(w, 'step_label'),
                'escalates_to': prop(w, 'escalates_to'),
                'facts': [
                    {'label': 'Workflow', 'value': w['template_name'], 'type': 'text'},
                    {'label': 'Stage', 'value': w['stage_name'], 'type': 'text'},
                    {'label': 'Due', 'value': prop(w, 'due_on'), 'type': 'date'},
                ],
            },
        )
        out.append({**item, 'group': 'appraisals' if w.get('process') in ('appraisal', 'promotion') else 'other'})

    # ---- change requests on my projects ----
    for cr in rows(ctx, 'change_requests'):
        if cr['status'] != 'raised' and cr['status'] != 'estimated':
            continue
        p = projects.get(cr['project_id'])
        if not p or not (task_admin or mine(p.get('pm_id'))):
            continue
        if cr.get('raised_by') == me and not task_admin and p.get('pm_id') != me:
            continue
        out.append(
            _mk(
                ctx,
                {
                    'kind': 'change_request',
                    'stage': 'review',
                    'entity_id': cr['id'],
                    'version': cr['version'],
                    'title': cr['title'],
                    'subtitle': f'{js_str(p["code"])} · {"needs an estimate" if cr["status"] == "raised" else "estimated, needs a decision"}',
                    'requester': pers(cr.get('raised_by')),
                    'amount': js_or(prop(cr, 'value_paise'), None),
                    'submitted_at': cr['created_at'],
                    'due_at': _due2(ctx, cr['created_at']),
                    'link': f'/projects/{p["id"]}/changes',
                    'actions': ['open'],
                    'warnings': [],
                    'note': js_or(prop(cr, 'description'), None),
                    'facts': [
                        {'label': 'Project', 'value': f'{js_str(p["code"])} · {js_str(p["name"])}', 'type': 'text'},
                        {'label': 'Status', 'value': 'Raised' if cr['status'] == 'raised' else 'Estimated', 'type': 'text'},
                        {'label': 'Hours', 'value': prop(cr, 'hours'), 'type': 'hours'},
                        {'label': 'Value', 'value': prop(cr, 'value_paise'), 'type': 'money'},
                    ],
                },
            )
        )

    # ---- maker-checker: changes I can check (never my own) ----
    if not ctx.view_as:
        sla = mc_config(ctx)['sla_days']
        for c in changes_to_check(ctx):
            out.append(
                _mk(
                    ctx,
                    {
                        'kind': 'change',
                        'stage': 'approve',
                        'entity_id': c['id'],
                        'version': c['version'],
                        'title': c['summary'],
                        'subtitle': f'{js_str(c["entity_label"])} · {js_str(c["maker_name"])}',
                        'requester': pers(c['maker_id']),
                        'amount': None,
                        'submitted_at': c['created_at'],
                        'due_at': add_working_days(c['created_at'][:10], sla, cal(ctx)),
                        'link': f'/changes?id={c["id"]}',
                        'actions': ['approve', 'reject'],
                        'warnings': [],
                        'note': None,
                        'facts': [
                            {'label': 'Area', 'value': c['entity_label'], 'type': 'text'},
                            {'label': 'Proposed by', 'value': c['maker_name'], 'type': 'text'},
                            {'label': 'Needs', 'value': c['required_permission_label'], 'type': 'text'},
                            {'label': 'Proposed', 'value': c['created_at'], 'type': 'datetime'},
                        ],
                    },
                )
            )

    # ---- must-read documents I haven't acknowledged ----
    docs = [d for d in rows(ctx, 'documents') if js_truthy(d.get('must_read')) and not js_truthy(d.get('archived'))]
    if docs:
        members = _project_members(ctx)
        acks = [a for a in rows(ctx, 'document_acks') if a.get('user_id') == me]
        versions = rows(ctx, 'document_versions')
        user_created = ctx.user['created_at']
        for d in docs:
            if not _can_see_doc(ctx, d, members, projects):
                continue
            if any(a['document_id'] == d['id'] and a['version'] == d['current_version'] for a in acks):
                continue
            v = next((x for x in versions if x['document_id'] == d['id'] and x['version'] == d['current_version']), None)
            published = coalesce(v.get('uploaded_at', UNDEF) if v else UNDEF, d['updated_at'])
            # a new joiner's clock starts when they joined, not when the policy was published
            at = user_created if user_created > published else published
            p = projects.get(d['project_id']) if js_truthy(d.get('project_id')) else None
            out.append(
                _mk(
                    ctx,
                    {
                        'kind': 'document',
                        'stage': 'read',
                        'entity_id': d['id'],
                        'version': d['version'],
                        'title': f'Read: {js_str(d["title"])}',
                        'subtitle': f'{js_str(p["code"]) if p else "Company library"} · version {js_str(d["current_version"])}',
                        'requester': pers(coalesce(v.get('uploaded_by', UNDEF) if v else UNDEF, prop(d, 'owner_id'))),
                        'amount': None,
                        'submitted_at': at,
                        'due_at': _due2(ctx, at),
                        'link': f'/documents?doc={d["id"]}',
                        'actions': ['open'],
                        'warnings': [],
                        'note': js_or(prop(v, 'note'), prop(d, 'description'), None),
                        'facts': [
                            {'label': 'Library', 'value': f'{js_str(p["code"])} · {js_str(p["name"])}' if p else 'Company', 'type': 'text'},
                            {'label': 'Version', 'value': d['current_version'], 'type': 'number'},
                            {'label': 'Updated', 'value': published, 'type': 'datetime'},
                        ],
                    },
                )
            )

    # ---- v7: letters issued to me that I haven't acknowledged (payslips need no acknowledgement) ----
    for d in rows(ctx, 'issued_documents'):
        if d.get('employee_id') != me or d['status'] != 'issued' or d.get('category') == 'payslip':
            continue
        day = coalesce(prop(d, 'issued_on'), d['created_at'][:10])
        at = day + 'T09:00:00Z'
        out.append(
            _mk(
                ctx,
                {
                    'kind': 'document',
                    'stage': 'read',
                    'entity_id': d['id'],
                    'version': d['version'],
                    'title': f'Acknowledge: {js_str(d["title"])}',
                    'subtitle': f'{js_str(coalesce(prop(d, "ref_no"), "Issued document"))} · issued {fmt_date(day)}',
                    'requester': pers(d.get('issued_by')),
                    'amount': None,
                    'submitted_at': at,
                    'due_at': _due2(ctx, at),
                    'link': f'/documents/issued/{d["id"]}',
                    'actions': ['open'],
                    'warnings': [],
                    'note': None,
                    'facts': [
                        {'label': 'Document', 'value': d['title'], 'type': 'text'},
                        {'label': 'Reference', 'value': coalesce(prop(d, 'ref_no'), '—'), 'type': 'text'},
                        {'label': 'Issued', 'value': at, 'type': 'datetime'},
                    ],
                },
            )
        )

    return sorted(
        out,
        key=lambda i: (
            0 if i['overdue'] else 1,
            locale_key(coalesce(i.get('due_at', UNDEF), '9999')),
            locale_key(coalesce(i.get('submitted_at', UNDEF), '')),
        ),
    )


# ---------------- "My requests" ----------------
_TS_LABEL = {'pending': 'Waiting', 'approved': 'Approved', 'sent_back': 'Sent back'}
_LEAVE_LABEL = {'pending': 'Waiting', 'approved': 'Approved', 'rejected': 'Rejected', 'cancelled': 'Cancelled'}
_EX_LABEL = {'pending': 'Waiting', 'approved': 'Approved · to be paid', 'rejected': 'Rejected', 'paid': 'Paid'}
_PO_LABEL = {'pending': 'Waiting', 'approved': 'Approved', 'rejected': 'Rejected', 'closed': 'Closed'}


def _lab(m: dict, k: Any) -> Any:
    """`{...}[k]` (undefined when absent)"""
    return m.get(k, UNDEF) if isinstance(k, str) else UNDEF


def my_requests(ctx: Ctx) -> list[dict]:
    from app.routes.taskflow import reviewer_of

    me = ctx.user_id
    out: list[dict] = []
    since = add_days(ctx.today, -90)

    def recent(d: Any) -> bool:
        return not js_truthy(d) or d[:10] >= since

    def decided_by(v: Any) -> Any:
        return user_name(ctx, v) if js_truthy(v) else None

    projects = {p['id']: p for p in rows(ctx, 'projects')}
    for w in rows(ctx, 'timesheet_weeks'):
        if w['user_id'] != me or w['status'] not in ('submitted', 'approved', 'rejected') or not recent(w.get('submitted_at')):
            continue
        st = 'pending' if w['status'] == 'submitted' else 'approved' if w['status'] == 'approved' else 'sent_back'
        out.append(
            {
                'id': 'ts:' + w['id'],
                'kind': 'timesheet',
                'title': f'Timesheet · {week_label(w["week_start"])}',
                'subtitle': 'Weekly timesheet',
                'state': st,
                'state_label': coalesce(_lab(_TS_LABEL, st), st),
                'submitted_at': w.get('submitted_at'),
                'decided_at': prop(w, 'decided_at'),
                'decided_by': decided_by(w.get('decided_by')),
                'waiting_on': user_name(ctx, w.get('approver_id')) if st == 'pending' else None,
                'reason': prop(w, 'reject_reason'),
                'amount': None,
                'link': f'/timesheet/{w["week_start"]}',
            }
        )
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    reviews = rows(ctx, 'task_reviews')
    for r in reviews:
        if r.get('submitted_by') != me or not recent(r.get('submitted_at')):
            continue
        t = tasks.get(r['task_id'])
        if not t:
            continue
        p = projects.get(t['project_id'])
        st = 'pending' if not js_truthy(r.get('decision')) else 'approved' if r['decision'] == 'approved' else 'sent_back'
        qr = r.get('quality_rating')
        if st == 'pending':
            label = 'In review'
        elif st == 'approved':
            label = 'Approved' + (f' · {js_str(qr)}/5' if js_truthy(qr) else '')
        else:
            label = 'Sent back'
        out.append(
            {
                'id': 'tr:' + r['id'],
                'kind': 'task_review',
                'title': t['title'],
                'subtitle': f'{js_str(coalesce(p.get("code", UNDEF) if p else UNDEF, ""))} · task submission'
                + (f', round {js_str(r["round"])}' if r['round'] > 1 else ''),
                'state': st,
                'state_label': label,
                'submitted_at': r['submitted_at'],
                'decided_at': prop(r, 'decided_at'),
                'decided_by': decided_by(r.get('decided_by')),
                'waiting_on': user_name(ctx, reviewer_of(ctx, t)) if st == 'pending' else None,
                'reason': prop(r, 'decision_reason'),
                'amount': None,
                'link': f'/tasks/{t["id"]}',
            }
        )
    # in-review tasks with no review rows yet (older data)
    reviewed = {r['task_id'] for r in reviews}
    for t in tasks.values():
        if t.get('owner_id') != me or t['status'] != 'in_review':
            continue
        if t['id'] in reviewed:
            continue
        p = projects.get(t['project_id'])
        so = t.get('submitted_on')
        out.append(
            {
                'id': 'tk:' + t['id'],
                'kind': 'task_review',
                'title': t['title'],
                'subtitle': f'{js_str(coalesce(p.get("code", UNDEF) if p else UNDEF, ""))} · task submission',
                'state': 'pending',
                'state_label': 'In review',
                'submitted_at': so + 'T09:00:00Z' if js_truthy(so) else t['updated_at'],
                'decided_at': None,
                'decided_by': None,
                'waiting_on': user_name(ctx, reviewer_of(ctx, t)),
                'reason': None,
                'amount': None,
                'link': f'/tasks/{t["id"]}',
            }
        )
    for lr in rows(ctx, 'leave_requests'):
        if lr['user_id'] != me or not recent(lr['created_at']):
            continue
        st = 'pending' if lr['status'] == 'pending' else lr['status']
        kind_txt = 'leave without pay' if lr['type'] == 'lwp' else f'{js_str(lr["type"])} leave'
        out.append(
            {
                'id': 'lv:' + lr['id'],
                'kind': 'leave',
                'title': f'{js_str(lr["days"])} day{"" if lr["days"] == 1 else "s"} {kind_txt}',
                'subtitle': fmt_date(lr['from_on']) if lr['from_on'] == lr['to_on'] else f'{fmt_date(lr["from_on"])} – {fmt_date(lr["to_on"])}',
                'state': st,
                'state_label': _lab(_LEAVE_LABEL, lr['status']),
                'submitted_at': lr['created_at'],
                'decided_at': prop(lr, 'decided_at'),
                'decided_by': decided_by(lr.get('decided_by')),
                'waiting_on': user_name(ctx, lr.get('approver_id')) if st == 'pending' else None,
                'reason': prop(lr, 'decision_reason'),
                'amount': None,
                'link': '/leave',
            }
        )
    for x in rows(ctx, 'expense_claims'):
        if x['user_id'] != me or x['status'] == 'draft' or not recent(x['created_at']):
            continue
        st = 'pending' if x['status'] == 'submitted' else x['status']
        out.append(
            {
                'id': 'ex:' + x['id'],
                'kind': 'expense',
                'title': js_or(x.get('description'), 'Expense claim'),
                'subtitle': f'{_cat_label(x["category"])} · spent {fmt_date(x["spent_on"])}',
                'state': st,
                'state_label': coalesce(_lab(_EX_LABEL, st), st),
                'submitted_at': x['updated_at'],
                'decided_at': x['paid_on'] + 'T09:00:00Z' if js_truthy(x.get('paid_on')) else prop(x, 'decided_at'),
                'decided_by': decided_by(x.get('decided_by')),
                'waiting_on': user_name(ctx, x.get('approver_id')) if st == 'pending' else None,
                'reason': prop(x, 'decision_reason'),
                'amount': x['amount_paise'],
                'link': '/expenses',
            }
        )
    for po in rows(ctx, 'purchase_orders'):
        if po.get('requested_by') != me or po['status'] == 'draft' or not recent(po['created_at']):
            continue
        st = 'pending' if po['status'] == 'pending' else 'approved' if po['status'] == 'closed' else po['status']
        out.append(
            {
                'id': 'po:' + po['id'],
                'kind': 'purchase_order',
                'title': js_str(po['number']),
                'subtitle': po['budget_category'],
                'state': st,
                'state_label': coalesce(_lab(_PO_LABEL, po['status']), st),
                'submitted_at': po['created_at'],
                'decided_at': prop(po, 'decided_at'),
                'decided_by': decided_by(po.get('decided_by')),
                'waiting_on': js_or(user_name(ctx, po.get('approver_id')), 'Admin') if st == 'pending' else None,
                'reason': prop(po, 'decision_reason'),
                'amount': po['amount_paise'],
                'link': '/purchases',
            }
        )
    for cr in rows(ctx, 'change_requests'):
        if cr.get('raised_by') != me or not recent(cr['created_at']):
            continue
        p = projects.get(cr['project_id'])
        st = 'pending' if cr['status'] in ('raised', 'estimated') else 'rejected' if cr['status'] == 'rejected' else 'approved'
        if st == 'pending':
            label = 'Raised' if cr['status'] == 'raised' else 'Estimated'
        elif st == 'rejected':
            label = 'Rejected'
        else:
            label = 'Done' if cr['status'] == 'done' else 'Approved'
        out.append(
            {
                'id': 'cr:' + cr['id'],
                'kind': 'change_request',
                'title': cr['title'],
                'subtitle': f'{js_str(coalesce(p.get("code", UNDEF) if p else UNDEF, ""))} · change request',
                'state': st,
                'state_label': label,
                'submitted_at': cr['created_at'],
                'decided_at': prop(cr, 'decided_at'),
                'decided_by': decided_by(cr.get('decided_by')),
                'waiting_on': user_name(ctx, p.get('pm_id') if p else UNDEF) if st == 'pending' else None,
                'reason': None,
                'amount': js_or(prop(cr, 'value_paise'), None),
                'link': f'/projects/{p["id"]}/changes' if p else '/projects',
            }
        )
    # Multi-step workflows: say which step a pending request is on.
    recs: dict[str, Any] = {}
    for prefix, table in (('ex:', 'expense_claims'), ('lv:', 'leave_requests'), ('po:', 'purchase_orders'), ('ts:', 'timesheet_weeks')):
        for x in rows(ctx, table):
            recs[prefix + x['id']] = x
    for m in out:
        if m['state'] != 'pending':
            continue
        lab = step_label(recs.get(m['id'], UNDEF))
        if js_truthy(lab):
            m['state_label'] = f'Waiting · {js_lower(lab.split(" · ")[0])}'
    out = sorted(out, key=lambda m: locale_key(coalesce(m['submitted_at'], '')), reverse=True)
    return sorted(out, key=lambda m: 0 if m['state'] == 'pending' else 1)


# =====================================================================================================================
# Goals & OKRs
# =====================================================================================================================
def _periods_around(ctx: Ctx) -> list[dict]:
    fy = prop(_S(ctx), 'company.fy_start_month')
    cur = quarter_of(ctx.today, fy)
    out: list[dict] = []
    seen: set = set()

    def add(anchor: str) -> None:
        q = quarter_of(anchor, fy)
        if q['label'] in seen:
            return
        seen.add(q['label'])
        out.append({'label': q['label'], 'from': q['from'], 'to': q['to'], 'current': q['label'] == cur['label']})

    for o in rows(ctx, 'objectives'):
        add(o['period_from'])
    add(add_days(cur['from'], -1))
    add(ctx.today)
    add(add_days(cur['to'], 1))
    return sorted(out, key=lambda p: locale_key(p['from']), reverse=True)


def _scope_of(ctx: Ctx, o: dict) -> list | None:
    """People in an objective's scope: company → everyone; team → owner + reporting tree; person → owner."""
    if o['level'] == 'company':
        return None
    if o['level'] == 'team':
        return [o['owner_id'], *[u['id'] for u in team_of(ctx, o['owner_id'])]]
    return [o['owner_id']]


def _compliance_between(ctx: Ctx, frm: str, to: str, ids: list | None) -> Any:
    users = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin')) and (ids is None or u['id'] in ids)]
    by_key = {js_str(w['user_id']) + '|' + js_str(w['week_start']): w for w in rows(ctx, 'timesheet_weeks')}
    due = 0
    ok = 0
    last_monday = add_days(week_start(ctx.today), -7)
    mon = week_start(frm)
    while mon <= last_monday and mon <= to:
        lim = add_days(mon, 6)
        for u in users:
            if u['joined_on'] > lim:
                continue
            due += 1
            w = by_key.get(js_str(u['id']) + '|' + mon)
            if w and (w['status'] == 'submitted' or w['status'] == 'approved'):
                ok += 1
        mon = add_days(mon, 7)
    return (ok / due) * 100 if due else None


def _first(name: str) -> str:
    return name.split(' ')[0]


def _auto_value(ctx: Ctx, o: dict, kr: dict) -> dict[str, Any]:
    frm = o['period_from']
    to = o['period_to'] if o['period_to'] < ctx.today else ctx.today
    if frm > ctx.today:
        return {'value': None, 'basis': 'Period has not started', 'forecast': None}
    ids = _scope_of(ctx, o)
    if o['level'] == 'company':
        scope_label = 'company'
    elif o['level'] == 'team':
        scope_label = f'{_first(user_name(ctx, o["owner_id"]))}’s team'
    else:
        scope_label = _first(user_name(ctx, o['owner_id']))
    span = 'whole period' if o['period_to'] < ctx.today else 'period to date'
    # Rate metrics need a few weeks of data; at the very start of a period they use a trailing 30 days.
    elapsed_wd = working_days_between(frm, to, cal(ctx))
    early = elapsed_wd < 10
    r_from = add_days(ctx.today, -29) if early else frm
    rate_basis = f'Last 30 days ({scope_label}) — the period has just started' if early else f'{span[0].upper()}{span[1:]} · {scope_label}'
    metric = kr['metric']
    if metric in ('revenue_billed', 'revenue_collected'):
        r = revenue_between(ctx, frm, to)
        full = revenue_between(ctx, frm, o['period_to'], {'scheduled': True})
        return {
            'value': r['billed'] if metric == 'revenue_billed' else r['collected'],
            'basis': f'Ledger, {span}',
            'forecast': (full['billed'] if metric == 'revenue_billed' else None) if o['period_to'] > ctx.today else None,
        }
    if metric == 'deals_won_value':
        won = [
            d
            for d in rows(ctx, 'deals')
            if d['stage'] == 'won' and js_truthy(d.get('won_on')) and frm <= d['won_on'] <= to and (ids is None or d.get('owner_id') in ids)
        ]
        v: Any = 0
        for d in won:
            v += deal_value(d)
        return {'value': v, 'basis': f'{len(won)} deal{"" if len(won) == 1 else "s"} won · {span} · {scope_label}', 'forecast': None}
    if metric == 'on_time_task_pct':
        opts: dict[str, Any] = {'from': r_from, 'to': to}
        if ids is not None:
            opts = {'userIds': ids, **opts}
        s = task_stats(ctx, opts)
        return {
            'value': None if s['on_time_pct'] is None else js_round(s['on_time_pct'] * 1000) / 10,
            'basis': f'{rate_basis} · {js_str(s["completed"])} task{"" if s["completed"] == 1 else "s"} done',
            'forecast': None,
        }
    if metric == 'timesheet_compliance':
        last_monday = add_days(week_start(ctx.today), -7)
        c_from = add_days(last_monday, -21) if early or week_start(frm) > last_monday else frm
        v = _compliance_between(ctx, c_from, to, ids)
        return {
            'value': None if v is None else js_round(v * 10) / 10,
            'basis': f'Last 4 completed weeks ({scope_label})' if early else f'Completed weeks, {span} · {scope_label}',
            'forecast': None,
        }
    if metric == 'margin_pct':
        r = revenue_between(ctx, r_from, to)
        return {
            'value': None if r['margin'] is None else js_round(r['margin'] * 1000) / 10,
            'basis': 'Ledger, last 30 days' if early else f'Ledger, {span}',
            'forecast': None,
        }
    if metric == 'headcount':
        n = sum(1 for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin')) and (ids is None or u['id'] in ids))
        return {'value': n, 'basis': f'Active people today · {scope_label}', 'forecast': None}
    if metric == 'leakage':
        lk = [x for x in rows(ctx, 'revenue_leakage') if frm <= x['date'] <= to and x['status'] != 'recovered']
        v = 0
        for x in lk:
            v += x['amount_paise'] - coalesce(prop(x, 'recovered_paise'), 0)
        return {'value': v, 'basis': f'{len(lk)} open item{"" if len(lk) == 1 else "s"} · {span}', 'forecast': None}
    return {'value': prop(kr, 'current'), 'basis': '', 'forecast': None}


def _clamp01(n: Any) -> Any:
    return js_max(0, min(1, n))


def okr_progress_of(start: Any, target: Any, current: Any) -> Any:
    if current is None or current is UNDEF:
        return None
    if target == start:
        return 1 if current == target else 0
    return _clamp01((current - start) / (target - start))


def _status_of(progress: Any, expected: Any, started: bool) -> str:
    if not started:
        return 'not_started'
    if progress is None:
        return 'on_track'
    if progress >= 1:
        return 'on_track'
    e = js_max(expected, 0.02)
    if progress >= 0.9 * e:
        return 'on_track'
    if progress >= 0.6 * e:
        return 'at_risk'
    return 'off_track'


def _elapsed_of(ctx: Ctx, frm: str, to: str) -> Any:
    if ctx.today < frm:
        return 0
    if ctx.today > to:
        return 1
    return _clamp01((diff_days(ctx.today, frm) + 1) / (diff_days(to, frm) + 1))


def _is_manager_of(ctx: Ctx, owner_id: Any) -> bool:
    return any(u['id'] == owner_id for u in team_of(ctx, ctx.user_id))


def _has_reports(ctx: Ctx) -> bool:
    return any(u.get('reports_to_id') == ctx.user_id and u.get('status') == 'active' for u in rows(ctx, 'users'))


def _can_edit_objective(ctx: Ctx, o: dict) -> bool:
    if has(ctx, 'okrs.manage'):
        return True
    if o['level'] == 'company':
        return False
    if o['owner_id'] == ctx.user_id:
        return o['level'] == 'person' or _has_reports(ctx)
    return _is_manager_of(ctx, o['owner_id'])


def _can_see_finance(ctx: Ctx, metric: Any) -> bool:
    if not METRIC_META[metric]['finance']:
        return True
    if has(ctx, 'finance.view'):
        return True
    return metric == 'deals_won_value' and has_any(ctx, 'deals.edit_own', 'projects.edit')


def _checkins_of(ctx: Ctx) -> dict[Any, list]:
    m: dict[Any, list] = {}
    for a in rows(ctx, 'activity_log'):
        if a.get('entity') != 'key_result':
            continue
        m.setdefault(a['entity_id'], []).append({'id': a['id'], 'at': a['at'], 'by': user_name(ctx, a.get('actor_id')), 'summary': a['summary']})
    return {k: sorted(v, key=lambda x: locale_key(x['at']), reverse=True) for k, v in m.items()}


def _view_objective(ctx: Ctx, o: dict, all_: list[dict], krs_by_obj: dict[Any, list], ci: dict[Any, list]) -> dict[str, Any]:
    editable = _can_edit_objective(ctx, o)
    kv = []
    for k in krs_by_obj.get(o['id'], ()):
        meta = METRIC_META[k['metric']]
        auto = k['metric'] != 'manual'
        a = _auto_value(ctx, o, k) if auto else {'value': prop(k, 'current'), 'basis': None, 'forecast': None}
        current = a['value']
        progress = okr_progress_of(k['start_value'], k['target'], current)
        redacted = not _can_see_finance(ctx, k['metric'])
        unit = k.get('unit')
        kind = ('percent' if unit == '%' else 'money' if unit == '₹' else 'number') if k['metric'] == 'manual' else meta['kind']
        kv.append(
            {
                **k,
                'current': None if redacted else current,
                'start_value': None if redacted else k['start_value'],
                'target': None if redacted else k['target'],
                'owner': person_of(ctx, k.get('owner_id')),
                'auto': auto,
                'kind': kind,
                'progress': progress,
                'basis': a['basis'],
                'forecast': None if redacted else a['forecast'],
                'redacted': redacted,
                'checkins': ci.get(k['id'], [])[:12],
                'can_edit': editable,
                'can_check_in': not auto and (editable or k.get('owner_id') == ctx.user_id),
            }
        )
    ps = [k['progress'] for k in kv if k['progress'] is not None]
    progress: Any = None
    if ps:
        s: Any = 0
        for x in ps:
            s += x
        progress = s / len(ps)
    expected = _elapsed_of(ctx, o['period_from'], o['period_to'])
    computed = 'done' if o.get('status') == 'done' else _status_of(progress, expected, ctx.today >= o['period_from'])
    parent = next((x for x in all_ if x['id'] == o['parent_id']), None) if js_truthy(o.get('parent_id')) else None
    return {
        **o,
        'owner': person_of(ctx, o['owner_id']),
        'parent_title': coalesce(parent.get('title', UNDEF) if parent else UNDEF, None),
        'key_results': kv,
        'progress': progress,
        'expected': expected,
        'computed_status': computed,
        'can_edit': editable,
    }


_LEVEL_RANK = {'company': 0, 'team': 1, 'person': 2}


def _okr_view(ctx: Ctx, period_label: Any, owner_id: Any) -> dict[str, Any]:
    periods = _periods_around(ctx)
    period = next((p for p in periods if p['label'] == period_label), None) or next((p for p in periods if p['current']), None) or periods[0]
    all_ = rows(ctx, 'objectives')
    krs = rows(ctx, 'key_results')
    krs_by_obj: dict[Any, list] = {}
    for k in krs:
        krs_by_obj.setdefault(k['objective_id'], []).append(k)
    ci = _checkins_of(ctx)
    objs = [o for o in all_ if o['period'] == period['label']]
    if js_truthy(owner_id):
        objs = [o for o in objs if o['owner_id'] == owner_id or any(k.get('owner_id') == owner_id for k in krs_by_obj.get(o['id'], ()))]
    views = [_view_objective(ctx, o, all_, krs_by_obj, ci) for o in objs]
    views = sorted(views, key=lambda v: (_LEVEL_RANK[v['level']], locale_key(v['created_at'])))
    ps = [v['progress'] for v in views if v['progress'] is not None]
    company = [v['progress'] for v in views if v['level'] == 'company' and v['progress'] is not None]
    owner_ids: dict[Any, None] = {}
    for o in all_:
        if o['period'] != period['label']:
            continue
        owner_ids.setdefault(o['owner_id'], None)
        for k in krs_by_obj.get(o['id'], ()):
            owner_ids.setdefault(k.get('owner_id'), None)
    users = _users_by_id(ctx)
    owners = [p for p in (person_of(ctx, id_, users) for id_ in owner_ids) if p]

    def avg(xs: list) -> Any:
        if not xs:
            return None
        s: Any = 0
        for x in xs:
            s += x
        return s / len(xs)

    krs_n = 0
    for v in views:
        krs_n += len(v['key_results'])
    return {
        'period': period,
        'periods': periods,
        'elapsed': _elapsed_of(ctx, period['from'], period['to']),
        'days_left': js_max(0, diff_days(period['to'], ctx.today)),
        'objectives': views,
        'scorecard': {
            'objectives': len(views),
            'progress': avg(ps),
            'on_track': sum(1 for v in views if v['computed_status'] == 'on_track'),
            'at_risk': sum(1 for v in views if v['computed_status'] == 'at_risk'),
            'off_track': sum(1 for v in views if v['computed_status'] == 'off_track'),
            'done': sum(1 for v in views if v['computed_status'] == 'done'),
            'company_progress': avg(company),
            'krs': krs_n,
        },
        'owners': sorted(owners, key=lambda p: locale_key(p['name'])),
        'can': {'company': has(ctx, 'okrs.manage'), 'team': has(ctx, 'okrs.manage') or _has_reports(ctx), 'person': True},
    }


METRICS = list(METRIC_META.keys())


def _clean_title(v: Any, label: str = 'Title') -> str:
    t = js_trim(js_str(coalesce(v, '')))
    if js_len(t) < 3:
        err(422, 'VALIDATION', f'{label} needs at least 3 characters.', 'title')
    if js_len(t) > 160:
        err(422, 'VALIDATION', f'{label} can be at most 160 characters.', 'title')
    return t


def _num(v: Any, field: str, label: str) -> Any:
    n = js_number(v)
    if v == '' or v is None or v is UNDEF or not is_finite(n):
        err(422, 'VALIDATION', f'{label} must be a number.', field)
    return n


# =====================================================================================================================
# Audit trail
# =====================================================================================================================
def _entity_label(e: str) -> str:
    if e in ENTITY_LABEL:
        return ENTITY_LABEL[e]
    s = e.replace('_', ' ')
    return re.sub(r'^\w', lambda m: m.group(0).upper(), s, count=1, flags=re.ASCII)


# JS `.` (no s flag) matches anything but line terminators; \s is Unicode in both
_NL = r'[^\n\r  ]'
_ARROW = re.compile(rf'^(?:{_NL}*?:\s)?({_NL}+?)\s→\s({_NL}+?)(?::\s{_NL}*)?$')
_REVIEW_RE = re.compile(r'^(approved|sent back)', re.I)


def _js_match_whole(rx: re.Pattern[str], s: str) -> re.Match[str] | None:
    """`s.match(/^…$/)` without the m flag: `$` is the very end (Python's `$` also matches before a final \\n)"""
    m = rx.match(s)
    return m if m is not None and m.end() == len(s) else None


def _audit_rows(ctx: Ctx) -> list[dict]:
    d = get_db()
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    deals = {x['id']: x for x in rows(ctx, 'deals')}
    weeks = {w['id']: w for w in rows(ctx, 'timesheet_weeks')}
    users = {u['id']: u for u in rows(ctx, 'users')}
    objectives = {o['id']: o for o in rows(ctx, 'objectives')}
    krs = {k['id']: k for k in rows(ctx, 'key_results')}
    docs = {x['id']: x for x in rows(ctx, 'documents')}
    first_by_id: dict[str, dict[Any, dict]] = {}

    def first(table: str, id_: Any) -> dict | None:
        m = first_by_id.get(table)
        if m is None:
            m = {}
            for r in rows(ctx, table):
                m.setdefault(r['id'], r)
            first_by_id[table] = m
        return m.get(id_)

    def uname(id_: Any, dflt: str) -> Any:
        u = users.get(id_)
        return coalesce(u.get('name', UNDEF) if u else UNDEF, dflt)

    def ref(entity: str, id_: Any) -> tuple[Any, Any]:
        if entity == 'project':
            p = projects.get(id_)
            return (f'{js_str(p["code"])} · {js_str(p["name"])}' if p else id_, f'/projects/{p["id"]}' if p else None)
        if entity == 'task':
            t = tasks.get(id_)
            return (t['title'] if t else id_, f'/tasks/{t["id"]}' if t else None)
        if entity == 'deal':
            x = deals.get(id_)
            return (f'{js_str(x["title"])} · {js_str(x["client"])}' if x else id_, f'/sales/{x["id"]}' if x else None)
        if entity == 'timesheet_week':
            w = weeks.get(id_)
            return (f'{js_str(uname(w["user_id"], "—"))} · {week_label(w["week_start"])}' if w else id_, f'/timesheet/{w["week_start"]}' if w else None)
        if entity == 'user':
            return (uname(id_, id_), None)
        if entity == 'objective':
            o = objectives.get(id_)
            return (coalesce(o.get('title', UNDEF) if o else UNDEF, id_), '/okrs')
        if entity == 'key_result':
            k = krs.get(id_)
            return (coalesce(k.get('title', UNDEF) if k else UNDEF, id_), '/okrs')
        if entity == 'document':
            x = docs.get(id_)
            return (coalesce(x.get('title', UNDEF) if x else UNDEF, id_), '/documents')
        if entity in ('expense', 'expense_claim'):
            x = first('expense_claims', id_)
            return (f'{js_str(js_or(x.get("description"), "Expense"))} · {js_str(uname(x["user_id"], ""))}' if x else id_, '/expenses')
        if entity == 'leave_request':
            x = first('leave_requests', id_)
            return (f'{js_str(uname(x["user_id"], ""))} · {fmt_date(x["from_on"])}' if x else id_, '/leave')
        if entity == 'purchase_order':
            x = first('purchase_orders', id_)
            return (coalesce(x.get('number', UNDEF) if x else UNDEF, id_), '/purchases')
        if entity == 'vendor_bill':
            x = first('vendor_bills', id_)
            return (coalesce(x.get('number', UNDEF) if x else UNDEF, id_), '/purchases')
        if entity == 'appraisal':
            x = first('appraisals', id_)
            return (uname(x['user_id'], id_) if x else id_, f'/appraisals/{x["id"]}' if x else None)
        return (id_, None)

    def actor(id_: Any) -> Any:
        return person_of(ctx, id_, users)

    out: list[dict] = []
    for a in rows(ctx, 'activity_log'):
        label, link = ref(a['entity'], a['entity_id'])
        arrow = _js_match_whole(_ARROW, a['summary'])
        out.append(
            {
                'id': a['id'],
                'at': a['at'],
                'actor': actor(a.get('actor_id')),
                'source': 'activity',
                'entity': a['entity'],
                'entity_label': label,
                'entity_id': a['entity_id'],
                'action': a['action'],
                'summary': a['summary'],
                'before': arrow.group(1) if arrow else None,
                'after': arrow.group(2) if arrow else None,
                'link': link,
            }
        )
    for h in rows(ctx, 'project_status_history'):
        # the projects area also logs status changes to activity_log; keep the structured row only
        label, link = ref('project', h['project_id'])
        reason = h.get('reason')
        out.append(
            {
                'id': h['id'],
                'at': h['changed_at'],
                'actor': actor(h.get('changed_by')),
                'source': 'project_status',
                'entity': 'project',
                'entity_label': label,
                'entity_id': h['project_id'],
                'action': 'status_changed',
                'summary': f'Project status {js_str(coalesce(prop(h, "from_status"), "new"))} → {js_str(h["to_status"])}'
                + (f': {js_str(reason)}' if js_truthy(reason) else ''),
                'before': prop(h, 'from_status'),
                'after': h['to_status'],
                'link': link,
            }
        )
    for h in rows(ctx, 'deal_stage_history'):
        label, link = ref('deal', h['deal_id'])
        reason = h.get('reason')
        out.append(
            {
                'id': h['id'],
                'at': h['at'],
                'actor': actor(h.get('by')),
                'source': 'deal_stage',
                'entity': 'deal',
                'entity_label': label,
                'entity_id': h['deal_id'],
                'action': 'stage_changed',
                'summary': f'Deal stage {js_str(coalesce(prop(h, "from_stage"), "new"))} → {js_str(h["to_stage"])}'
                + (f': {js_str(reason)}' if js_truthy(reason) else ''),
                'before': prop(h, 'from_stage'),
                'after': h['to_stage'],
                'link': link,
            }
        )
    for tr in rows(ctx, 'task_reviews'):
        if not js_truthy(tr.get('decision')) or not js_truthy(tr.get('decided_at')):
            continue
        label, link = ref('task', tr['task_id'])
        approved = tr['decision'] == 'approved'
        qr = tr.get('quality_rating')
        if approved:
            summary = f'Approved task (round {js_str(tr["round"])})' + (f', rated {js_str(qr)}/5' if js_truthy(qr) else '')
        else:
            summary = f'Sent back task (round {js_str(tr["round"])}): {js_str(coalesce(prop(tr, "decision_reason"), ""))}'
        out.append(
            {
                'id': tr['id'],
                'at': tr['decided_at'],
                'actor': actor(tr.get('decided_by')),
                'source': 'task_review',
                'entity': 'task',
                'entity_label': label,
                'entity_id': tr['task_id'],
                'action': 'review_approved' if approved else 'review_sent_back',
                'summary': summary,
                'before': 'In review',
                'after': 'Done' if approved else 'In progress',
                'link': link,
            }
        )
    tid = ctx.tenant_id
    all_users = d.users
    for a in [x for x in d.admin_audit if x.get('tenant_id') == tid]:
        au = next((u for u in all_users if u['id'] == a.get('actor_id')), None)
        reason = a.get('reason')
        out.append(
            {
                'id': a['id'],
                'at': a['at'],
                'actor': {'id': au['id'], 'name': au['name'], 'title': au['title'], 'hue': au['avatar_hue']} if au else None,
                'source': 'admin',
                'entity': 'workspace',
                'entity_label': ctx.tenant['name'],
                'entity_id': tid,
                'action': a['action'],
                'summary': f'Platform admin: {js_str(a["action"]).replace("_", " ")}' + (f' — {js_str(reason)}' if js_truthy(reason) else ''),
                'before': None,
                'after': None,
                'link': None,
            }
        )
    # de-duplicate project status changes that also appear in the activity log within the same minute
    status_keys = {f'{x["entity_id"]}|{x["at"][:16]}' for x in out if x['source'] == 'project_status'}
    review_keys = {f'{x["entity_id"]}|{x["at"][:16]}' for x in out if x['source'] == 'task_review'}
    filtered = [
        x
        for x in out
        if not (
            x['source'] == 'activity' and x['entity'] == 'project' and x['action'] == 'project.status' and f'{x["entity_id"]}|{x["at"][:16]}' in status_keys
        )
        and not (x['source'] == 'activity' and x['entity'] == 'task' and _REVIEW_RE.match(x['summary']) and f'{x["entity_id"]}|{x["at"][:16]}' in review_keys)
    ]
    return sorted(filtered, key=lambda x: locale_key(x['at']), reverse=True)


# =====================================================================================================================
# routes
# =====================================================================================================================
def directory_holders(req: Req) -> Any:
    """people who hold a permission (pickers: deal owners, PMs, ops owners)"""
    ctx = req.ctx
    keys = [k for k in (js_trim(s) for s in js_str(coalesce(req.q('perm'), '')).split(',')) if is_permission_key(k)]
    if not keys:
        err(422, 'VALIDATION', 'Name at least one permission.', 'perm')
    out = []
    for u in rows(ctx, 'users'):
        if u.get('status') != 'active' or js_truthy(u.get('is_super_admin')):
            continue
        perms = permissions_of(u)
        if any(k in perms for k in keys):
            out.append(u['id'])
    return out


def get_inbox(req: Req) -> Any:
    ctx = req.ctx
    items = inbox_items(ctx)
    counts = {
        'all': len(items),
        'overdue': sum(1 for i in items if i['overdue']),
        'timesheets': 0,
        'tasks': 0,
        'leave': 0,
        'expenses': 0,
        'purchases': 0,
        'bills': 0,
        'appraisals': 0,
        'changes': 0,
        'other': 0,
    }
    for i in items:
        counts[i['group']] = counts.get(i['group'], 0) + 1
    st = _S(ctx)
    return {
        'items': items,
        'counts': counts,
        'sla_days': prop(st, 'approval.sla_working_days'),
        'bulk_enabled': prop(st, 'approval.bulk_approve'),
        'can_pay_expenses': has(ctx, 'expenses.pay'),
    }


def inbox_summary(req: Req) -> Any:
    ctx = req.ctx
    if ctx.is_super and not rows(ctx, 'users'):
        return {'pending': 0, 'overdue': 0}
    items = inbox_items(ctx)
    return {'pending': len(items), 'overdue': sum(1 for i in items if i['overdue'])}


def inbox_mine(req: Req) -> Any:
    return my_requests(req.ctx)


def get_okrs(req: Req) -> Any:
    p = req.q('period')
    o = req.q('owner')
    return _okr_view(req.ctx, js_str(p) if js_truthy(p) else UNDEF, js_str(o) if js_truthy(o) else UNDEF)


def create_objective(req: Req) -> Any:
    ctx = req.ctx
    body = _body(req)
    level = body.get('level', UNDEF)
    if level not in ('company', 'team', 'person'):
        err(422, 'VALIDATION', 'Choose company, team or personal.', 'level')
    title = _clean_title(body.get('title', UNDEF), 'Objective')
    owner_id = js_str(coalesce(body.get('owner_id', UNDEF), ctx.user_id))
    find(ctx, 'users', owner_id, 'Owner')
    if not _can_edit_objective(ctx, {'level': level, 'owner_id': owner_id}):
        err(
            403,
            'FORBIDDEN',
            'Only the CEO (admin) sets company objectives.'
            if level == 'company'
            else 'Team objectives are set by managers for their own team.'
            if level == 'team'
            else 'You can set personal objectives for yourself or people who report to you.',
        )
    periods = _periods_around(ctx)
    bp = body.get('period', UNDEF)
    p = next((x for x in periods if x['label'] == bp), None) or next(x for x in periods if x['current'])
    raw = body.get('parent_id', UNDEF)
    parent_id = js_str(raw) if js_truthy(raw) else None
    if parent_id:
        parent = find(ctx, 'objectives', parent_id, 'Parent objective')
        if parent['level'] == 'person' or (level == 'team' and parent['level'] != 'company') or level == 'company':
            err(422, 'OKR_PARENT', 'Align a team objective to a company one, and a personal objective to a company or team one.', 'parent_id')
        if parent['period'] != p['label']:
            err(422, 'OKR_PARENT', 'The parent objective must be in the same period.', 'parent_id')
    else:
        parent_id = None
    o = insert(
        ctx,
        'objectives',
        {
            'title': title,
            'level': level,
            'owner_id': owner_id,
            'period': p['label'],
            'period_from': p['from'],
            'period_to': p['to'],
            'parent_id': parent_id,
            'status': 'on_track',
        },
    )
    log_activity(ctx, 'objective', o['id'], 'created', f'Added {level} objective “{title}” for {p["label"]}')
    return o


def patch_objective(req: Req) -> Any:
    ctx = req.ctx
    body = _body(req)
    o = find(ctx, 'objectives', req.params['id'], 'Objective')
    if not _can_edit_objective(ctx, o):
        err(403, 'FORBIDDEN', 'Only the owner, their manager or an admin can change this objective.')
    patch: dict[str, Any] = {}
    if 'title' in body:
        patch['title'] = _clean_title(body['title'], 'Objective')
    if 'parent_id' in body:
        pid = js_str(body['parent_id']) if js_truthy(body['parent_id']) else None
        if pid:
            parent = find(ctx, 'objectives', pid, 'Parent objective')
            if (
                pid == o['id']
                or parent['level'] == 'person'
                or o['level'] == 'company'
                or (o['level'] == 'team' and parent['level'] != 'company')
                or parent['period'] != o['period']
            ):
                err(
                    422,
                    'OKR_PARENT',
                    'Align a team objective to a company one, and a personal objective to a company or team one, in the same period.',
                    'parent_id',
                )
        patch['parent_id'] = pid
    if 'status' in body:
        if body['status'] != 'done' and body['status'] != 'on_track':
            err(422, 'VALIDATION', 'An objective can only be marked done or reopened.', 'status')
        patch['status'] = body['status']
    out = update(ctx, 'objectives', o['id'], patch, req.if_match)
    ps = patch.get('status', UNDEF)
    action = 'closed' if ps == 'done' else 'reopened' if js_truthy(ps) else 'updated'
    title = js_str(out['title'])
    summary = f'Marked “{title}” done' if ps == 'done' else f'Reopened “{title}”' if js_truthy(ps) else f'Updated “{title}”'
    log_activity(ctx, 'objective', o['id'], action, summary)
    return out


def delete_objective(req: Req) -> Any:
    ctx = req.ctx
    o = find(ctx, 'objectives', req.params['id'], 'Objective')
    if not _can_edit_objective(ctx, o):
        err(403, 'FORBIDDEN', 'Only the owner, their manager or an admin can delete this objective.')
    if any(x.get('parent_id') == o['id'] for x in rows(ctx, 'objectives')):
        err(409, 'OKR_HAS_CHILDREN', 'Other objectives are aligned to this one. Re-align or delete them first.')
    for k in [k for k in rows(ctx, 'key_results') if k['objective_id'] == o['id']]:
        remove(ctx, 'key_results', k['id'])
    remove(ctx, 'objectives', o['id'])
    log_activity(ctx, 'objective', o['id'], 'deleted', f'Deleted objective “{js_str(o["title"])}”')
    return {'ok': True}


def _kr_input(ctx: Ctx, body: dict, existing: dict | None = None) -> dict[str, Any]:
    metric = coalesce(body.get('metric', UNDEF), existing['metric'] if existing else UNDEF, 'manual')
    if metric not in METRICS:
        err(422, 'VALIDATION', 'Choose how this key result is measured.', 'metric')
    meta = METRIC_META[metric]
    title = _clean_title(body.get('title', UNDEF), 'Key result') if 'title' in body or not existing else existing['title']
    start_value = (
        _num(coalesce(body.get('start_value', UNDEF), 0), 'start_value', 'Start value') if 'start_value' in body or not existing else existing['start_value']
    )
    target = _num(body.get('target', UNDEF), 'target', 'Target') if 'target' in body or not existing else existing['target']
    if target == start_value:
        err(422, 'VALIDATION', 'Target must differ from the start value.', 'target')
    if meta['kind'] == 'percent' and (target < 0 or target > 100):
        err(422, 'VALIDATION', 'A percentage target must be between 0 and 100.', 'target')
    if metric == 'manual':
        unit = js_slice(js_trim(js_str(coalesce(body.get('unit', UNDEF), prop(existing, 'unit') if existing else UNDEF, ''))), 0, 16)
    else:
        unit = meta['unit']
    owner_id = js_str(coalesce(body.get('owner_id', UNDEF), prop(existing, 'owner_id') if existing else UNDEF, ctx.user_id))
    find(ctx, 'users', owner_id, 'Owner')
    return {'title': title, 'metric': metric, 'start_value': start_value, 'target': target, 'unit': unit, 'owner_id': owner_id}


def add_key_result(req: Req) -> Any:
    ctx = req.ctx
    o = find(ctx, 'objectives', req.params['id'], 'Objective')
    if not _can_edit_objective(ctx, o):
        err(403, 'FORBIDDEN', 'Only the owner, their manager or an admin can add key results.')
    v = _kr_input(ctx, _body(req))
    if sum(1 for k in rows(ctx, 'key_results') if k['objective_id'] == o['id']) >= 6:
        err(409, 'OKR_TOO_MANY', 'Keep it to five or six key results per objective.')
    k = insert(ctx, 'key_results', {**v, 'objective_id': o['id'], 'current': v['start_value']})
    log_activity(ctx, 'key_result', k['id'], 'created', f'Added key result “{js_str(k["title"])}” ({METRIC_META[k["metric"]]["label"]})')
    return k


def patch_key_result(req: Req) -> Any:
    ctx = req.ctx
    k = find(ctx, 'key_results', req.params['id'], 'Key result')
    o = find(ctx, 'objectives', k['objective_id'], 'Objective')
    if not _can_edit_objective(ctx, o):
        err(403, 'FORBIDDEN', 'Only the objective owner, their manager or an admin can change key results.')
    v = _kr_input(ctx, _body(req), k)
    out = update(ctx, 'key_results', k['id'], v, req.if_match)
    # update() changes the row in place in the TS, so `k.target` already reads the new target (mirrored)
    log_activity(ctx, 'key_result', k['id'], 'updated', f'Changed key result: target {js_str(out["target"])} → {js_str(out["target"])}')
    return out


def delete_key_result(req: Req) -> Any:
    ctx = req.ctx
    k = find(ctx, 'key_results', req.params['id'], 'Key result')
    o = find(ctx, 'objectives', k['objective_id'], 'Objective')
    if not _can_edit_objective(ctx, o):
        err(403, 'FORBIDDEN', 'Only the objective owner, their manager or an admin can remove key results.')
    remove(ctx, 'key_results', k['id'])
    log_activity(ctx, 'objective', o['id'], 'kr_deleted', f'Removed key result “{js_str(k["title"])}”')
    return {'ok': True}


def check_in(req: Req) -> Any:
    ctx = req.ctx
    k = find(ctx, 'key_results', req.params['id'], 'Key result')
    o = find(ctx, 'objectives', k['objective_id'], 'Objective')
    if k['metric'] != 'manual':
        err(409, 'OKR_AUTO', 'This key result is calculated from live data. It updates on its own.')
    if not (_can_edit_objective(ctx, o) or k.get('owner_id') == ctx.user_id):
        err(403, 'FORBIDDEN', 'Only the key result owner, the objective owner or their manager can check in.')
    value = _num(req.b('value'), 'value', 'Current value')
    note = js_trim(js_str(coalesce(req.b('note'), '')))
    if js_len(note) < 5:
        err(422, 'VALIDATION', 'Add a short note on what changed (at least 5 characters).', 'note')
    before = prop(k, 'current')
    out = update(ctx, 'key_results', k['id'], {'current': value}, req.if_match)
    unit = k.get('unit')
    u = ('%' if unit == '%' else f' {js_str(unit)}') if js_truthy(unit) else ''
    log_activity(ctx, 'key_result', k['id'], 'check_in', f'{js_str(before)}{u} → {js_str(value)}{u}: {note}')
    return out


def get_audit(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'audit.view'):
        err(403, 'FORBIDDEN', 'The audit trail is for admins.')
    all_ = _audit_rows(ctx)
    q = js_lower(js_trim(js_str(coalesce(req.q('q'), ''))))

    def qv(k: str) -> str:
        v = req.q(k)
        return js_str(v) if js_truthy(v) else ''

    actor = qv('actor')
    entity = qv('entity')
    frm = qv('from')
    to = qv('to')

    def match_base(x: dict) -> bool:
        a = x['actor']
        if actor and (a['id'] if a else UNDEF) != actor:
            return False
        if frm and not x['at'][:10] >= frm:
            return False
        if to and not x['at'][:10] <= to:
            return False
        if q:
            hay = js_lower(f'{js_str(x["summary"])} {js_str(x["entity_label"])} {js_str(a["name"]) if a else ""} {js_str(x["action"])}')
            if q not in hay:
                return False
        return True

    base = [x for x in all_ if match_base(x)]
    lst = [x for x in base if x['entity'] == entity] if entity else base
    counts: dict[Any, int] = {}
    for x in base:
        counts[x['entity']] = counts.get(x['entity'], 0) + 1
    size = 5000 if js_truthy(req.q('all')) else 100
    page = js_max(1, js_number(coalesce(req.q('page'), 1)))
    actors: dict[Any, dict] = {}
    for x in all_:
        if x['actor']:
            actors[x['actor']['id']] = x['actor']
    return {
        'items': js_slice(lst, (page - 1) * size, page * size),
        'total': len(lst),
        'page': page,
        'page_size': size,
        'entities': sorted([{'value': v, 'label': _entity_label(v), 'count': n} for v, n in counts.items()], key=lambda e: e['count'], reverse=True),
        'actors': sorted(actors.values(), key=lambda a: locale_key(a['name'])),
    }


def register(r: Any) -> None:
    # approval workflow configuration (src/mock/workflows.ts)
    wf_engine.register(r)
    r.get('/directory/holders', directory_holders)
    r.get('/inbox', get_inbox)
    r.get('/inbox/summary', inbox_summary)
    r.get('/inbox/mine', inbox_mine)
    r.get('/okrs', get_okrs)
    r.post('/okrs/objectives', create_objective)
    r.patch('/okrs/objectives/:id', patch_objective)
    r.delete('/okrs/objectives/:id', delete_objective)
    r.post('/okrs/objectives/:id/key-results', add_key_result)
    r.patch('/okrs/key-results/:id', patch_key_result)
    r.delete('/okrs/key-results/:id', delete_key_result)
    r.post('/okrs/key-results/:id/check-in', check_in)
    r.get('/audit', get_audit)
