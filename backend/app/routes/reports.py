"""Port of src/mock/routes/reports.ts — Module E backend: report datasets (tenant-scoped rows), the ReportSpec executor,
saved reports, and the AI endpoints (rule-based planner + data tools). Numbers are always computed here.

The engine, planner and narrative are in app/reports/* (ports of src/reports/*.ts)."""

from __future__ import annotations

from typing import Any

import app.engine.costing  # noqa: F401 — like reports.ts → projects.ts → costing.ts: installs the project metrics override
from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    can_see_margins,
    can_see_salaries,
    err,
    find,
    has,
    has_any,
    insert,
    log_activity,
    now_iso,
    patch,
    permissions_of,
    remove,
    rows,
    update,
    user_name,
)
from app.domain.calendar import (
    add_days,
    add_months,
    diff_days,
    fy_range,
    fy_start_year,
    is_working_day,
    max_iso,
    min_iso,
    month_of,
    month_range,
    quarter_of,
    week_start,
    working_days_between,
    working_days_in_month,
)
from app.domain.costing import cost_per_hour, rate_on, sales_compensation
from app.domain.labels import DEAL_STAGE
from app.engine.analytics import (
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
from app.engine.performance import DELAY_REASON_LABEL, task_stats, task_timeliness
from app.engine.performance import team_of as perf_team_of
from app.errors import ApiError
from app.js import (
    NAN,
    UNDEF,
    coalesce,
    js_join,
    js_len,
    js_lower,
    js_number,
    js_round,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    prop,
)
from app.reports.datasets import DATASET_MAP, datasets_for
from app.reports.engine import describe_spec, in_window, is_num, p, period_window, run_spec, sanitize_spec, u16slice
from app.reports.narrative import narrate
from app.reports.planner import SUGGESTIONS, plan_question

__all__ = [
    'S',
    's',
    'name_of',
    'team_of',
    'my_projects',
    'row_scope',
    'grants_of',
    'visible_datasets',
    'effective_week_status',
    'task_code',
    'week_history',
    'is_timesheet_user',
    'dataset_rows',
    'execute_spec',
    'planner_entities',
    'tool_project_health',
    'tool_pipeline',
    'tool_approvals',
    'tool_entities',
    'register',
]

METRIC_META = cat('api/company')['METRIC_META']


def _g(o: Any, k: str) -> Any:
    """o.k for a stored row (UNDEF when the column is absent)"""
    return o.get(k, UNDEF)


# ------------------------------------------------------------------
# Scope helpers
# ------------------------------------------------------------------
def S(ctx: Ctx) -> Any:
    """S(ctx) = ctx.tenant.settings"""
    return ctx.tenant['settings']


s = S


def name_of(ctx: Ctx) -> Any:
    """nameOf(ctx) → (id) => id ? name ?? '—' : null"""
    m: dict[Any, Any] = {}
    for u in rows(ctx, 'users'):
        m[u['id']] = _g(u, 'name')

    def nm(id_: Any) -> Any:
        if not js_truthy(id_):
            return None
        v = m.get(id_, UNDEF) if isinstance(id_, str) else UNDEF
        return coalesce(v, '—')

    return nm


class JsSet(dict):
    """an insertion-ordered set (JS Set): iteration in insertion order, `in`, add()"""

    def add(self, k: Any) -> None:
        self[k] = None


def team_of(ctx: Ctx, mgr_id: Any) -> JsSet:
    """Direct and indirect reports of a manager (a NEW insertion-ordered set each call)."""
    users = rows(ctx, 'users')
    by_mgr: dict[Any, list] = {}
    for u in users:
        by_mgr.setdefault(u.get('reports_to_id'), []).append(u)
    out = JsSet()

    def walk(id_: Any, depth: int) -> None:
        if depth > 6:
            return
        for u in by_mgr.get(id_, ()) if id_ is not None else ():
            if u['id'] not in out:
                out.add(u['id'])
                walk(u['id'], depth + 1)

    walk(mgr_id, 0)
    return out


def my_projects(ctx: Ctx) -> list[dict]:
    """Projects in the viewer's scope: projects.view_all → every project; otherwise ones they PM, work on, are allocated to or sold."""
    all_ = rows(ctx, 'projects')
    if has(ctx, 'projects.view_all'):
        return all_
    from app.routes.projects import my_project_ids

    ids = my_project_ids(ctx)
    return [x for x in all_ if x['id'] in ids]


def row_scope(ctx: Ctx) -> str:
    if has(ctx, 'people.view_all'):
        return 'all'
    if has_any(ctx, 'team.view', 'projects.edit'):
        return 'team'
    return 'own'


def grants_of(ctx: Ctx) -> dict[str, bool]:
    return {'margins': can_see_margins(ctx), 'salaries': can_see_salaries(ctx)}


def visible_datasets(ctx: Ctx) -> list[dict]:
    if not has_any(ctx, 'reports.view', 'ai.use'):
        return []
    return datasets_for(ctx.perms)


def _ai_enabled(ctx: Ctx) -> bool:
    return S(ctx).get('ai.enabled', UNDEF) is not False


def effective_week_status(ctx: Ctx, w: dict | None, monday: str) -> str:
    """Effective week status: an open week that has ended is locked (BR-06) unless unlocked."""
    ended = add_days(monday, 6) < ctx.today
    if w is None or w is UNDEF:
        return 'locked' if ended else 'missing'
    uu = w.get('unlock_until')
    if w['status'] == 'open' and ended and js_truthy(prop(S(ctx), 'timesheet.weekly_lock')) and not (js_truthy(uu) and uu >= ctx.today):
        return 'locked'
    return w['status']


def task_code(ms: dict | None, t: dict) -> str:
    frozen = t.get('wbs_code')
    if js_truthy(frozen):
        return frozen
    return f'{js_str(ms["no"]) if ms is not None else "0"}.{js_str(_g(t, "no"))}'


# ------------------------------------------------------------------
# Dataset row builders
# ------------------------------------------------------------------
def _by_id(lst: list) -> dict:
    """new Map(list.map((x) => [x.id, x])) (last duplicate wins)"""
    m: dict = {}
    for x in lst:
        m[x['id']] = x
    return m


def _b_projects(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    cm = can_see_margins(ctx)
    out = []
    for pr in my_projects(ctx):
        m = redact_metrics(ctx, project_metrics(ctx, pr), cm)
        out.append(
            {
                'id': pr['id'],
                'code': _g(pr, 'code'),
                'name': _g(pr, 'name'),
                'client': _g(pr, 'client'),
                'type': _g(pr, 'type'),
                'status': _g(pr, 'status'),
                'stream': _g(pr, 'stream'),
                'billing_model': _g(pr, 'billing_model'),
                'pm': nm(_g(pr, 'pm_id')),
                'priority': _g(pr, 'priority'),
                'start_on': _g(pr, 'start_on'),
                'end_on': _g(pr, 'end_on'),
                **{
                    k: m.get(k, UNDEF)
                    for k in (
                        'contract_value',
                        'invoiced',
                        'collected',
                        'outstanding',
                        'uninvoiced',
                        'est_hours',
                        'actual_hours',
                        'remaining_hours',
                        'progress',
                        'effort_ratio',
                        'spi',
                        'planned_cost',
                        'projected_cost',
                        'actual_cost',
                        'projected_margin',
                        'actual_margin',
                        'projected_markup',
                        'health',
                        'open_tasks',
                        'overdue_tasks',
                        'blocked_tasks',
                        'overdue_milestones',
                    )
                },
            }
        )
    return out


def _b_milestones(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    projs = _by_id(my_projects(ctx))
    tasks = rows(ctx, 'tasks')
    out = []
    for m in rows(ctx, 'milestones'):
        if js_truthy(m.get('deleted_at')) or m['project_id'] not in projs:
            continue
        pr = projs[m['project_id']]
        st = milestone_state(ctx, m, tasks)
        opn = not js_truthy(m.get('achieved_on')) and st['state'] != 'achieved_on_time' and st['state'] != 'achieved_late'
        out.append(
            {
                'id': m['id'],
                'project_id': pr['id'],
                'label': f'M{js_str(m["no"])} {js_str(m["name"])}',
                'project': _g(pr, 'name'),
                'client': _g(pr, 'client'),
                'no': m['no'],
                'pm': nm(_g(pr, 'pm_id')),
                'planned_start': _g(m, 'planned_start'),
                'planned_end': _g(m, 'planned_end'),
                'baseline_end': _g(m, 'baseline_end'),
                'slip_days': diff_days(m['planned_end'], m['baseline_end']),
                'billing_amount': _g(m, 'billing_amount_paise'),
                'billing_status': _g(m, 'billing_status'),
                'state': st['state'],
                'progress': st['progress'],
                'achieved_on': _g(m, 'achieved_on'),
                'is_overdue': st['state'] == 'overdue',
                'is_open': opn,
                'days_to_due': diff_days(m['planned_end'], ctx.today),
            }
        )
    return out


def _visible_tasks(ctx: Ctx) -> list[dict]:
    """v7: the task visibility rule (tasks.view_all → every task; otherwise canSeeTask)."""
    tasks = rows(ctx, 'tasks')
    if has(ctx, 'tasks.view_all'):
        return tasks
    from app.routes.tasks import can_see_task

    return [t for t in tasks if can_see_task(ctx, t)]


def _reviews_by_task(ctx: Ctx) -> dict[Any, list]:
    m: dict[Any, list] = {}
    for r in rows(ctx, 'task_reviews'):
        m.setdefault(r['task_id'], []).append(r)
    return m


def _delay_reason(x: dict) -> Any:
    code = x.get('delay_reason_code')
    if js_truthy(code):
        return coalesce(DELAY_REASON_LABEL.get(code, UNDEF) if isinstance(code, str) else UNDEF, code)
    return 'Other' if js_truthy(x.get('delay_reason')) else None


def _b_tasks(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    act = task_actuals(ctx)
    projs = _by_id(rows(ctx, 'projects'))
    mss = _by_id(rows(ctx, 'milestones'))
    reviews = _reviews_by_task(ctx)
    out = []
    for t in _visible_tasks(ctx):
        pr = projs.get(t['project_id'])
        ms = mss.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
        a = act[t['id']]['hours'] if t['id'] in act else 0
        opn = t['status'] != 'done'
        tl = task_timeliness(ctx, t)
        rs = reviews.get(t['id'], [])
        late_open = opn and t['due_on'] < ctx.today
        out.append(
            {
                'id': t['id'],
                'project_id': t['project_id'],
                'code': task_code(ms, t),
                'title': _g(t, 'title'),
                'project': coalesce(_g(pr, 'name') if pr else UNDEF, '—'),
                'client': coalesce(_g(pr, 'client') if pr else UNDEF, None),
                'milestone': f'M{js_str(ms["no"])} {js_str(ms["name"])}' if ms is not None else 'No milestone',
                'owner': nm(_g(t, 'owner_id')),
                'owner_id': _g(t, 'owner_id'),
                'backup': nm(_g(t, 'backup_id')),
                'status': t['status'],
                'complexity': _g(t, 'complexity'),
                'start_on': _g(t, 'start_on'),
                'due_on': t['due_on'],
                'done_on': _g(t, 'done_on'),
                'days_overdue': diff_days(ctx.today, t['due_on']) if late_open else 0,
                'is_overdue': late_open,
                'is_blocked': t['status'] == 'blocked',
                'is_open': opn,
                'on_time': (tl['timeliness'] != 'late') if tl else None,
                'est': _g(t, 'est_hours'),
                'actual': js_round(a * 100) / 100,
                'effort_ratio': js_round((a / t['est_hours']) * 1000) / 1000 if t['est_hours'] > 0 and a > 0 else None,
                'kind': coalesce(_g(t, 'kind'), 'general' if pr is not None and pr.get('code') == 'GEN' else 'project'),
                'assigned_by': nm(_g(t, 'assigned_by')),
                'reviewer': nm(_g(t, 'reviewer_id')),
                'priority': coalesce(_g(t, 'priority'), 'normal'),
                'submitted_on': coalesce(_g(t, 'submitted_on'), None),
                'approved_on': coalesce(_g(t, 'approved_on'), None),
                'timeliness': tl['timeliness'] if tl else None,
                'variance_days': tl['variance'] if tl else None,
                'delay_reason': _delay_reason(t),
                'quality_rating': coalesce(_g(t, 'quality_rating'), None),
                'review_rounds': max(len(rs), coalesce(_g(t, 'review_round'), 0)),
                'sent_back_count': sum(1 for r in rs if r.get('decision') == 'sent_back'),
                'on_time_rate': (0 if tl['timeliness'] == 'late' else 1) if tl else None,
                'is_late': (tl['timeliness'] == 'late') if tl else False,
                'is_in_review': t['status'] == 'in_review',
            }
        )
    return out


def _b_hours(ctx: Ctx, window: tuple[Any, list[dict]] | None = None) -> list[dict]:
    """window: (date field, ranges) of the report's period — rows whose `date` lies outside every range are never
    used by the report (engine.period_window), so they are not built"""
    users = _by_id(rows(ctx, 'users'))
    tasks = _by_id(rows(ctx, 'tasks'))
    projs = _by_id(rows(ctx, 'projects'))
    mss = _by_id(rows(ctx, 'milestones'))
    sal = can_see_salaries(ctx)
    es = costed_entries(ctx)
    sc = row_scope(ctx)
    me = ctx.user_id
    if sc == 'own':
        es = [e for e in es if e['user_id'] == me]
    elif sc == 'team':
        team = team_of(ctx, me)
        team.add(me)
        mine = {x['id'] for x in my_projects(ctx)}

        def keep(e: dict) -> bool:
            if e['user_id'] in team:
                return True
            tid = e.get('task_id')
            if not js_truthy(tid):
                return False
            t = tasks.get(tid)
            return coalesce(t.get('project_id', UNDEF) if t else UNDEF, '') in mine

        es = [e for e in es if keep(e)]
    if window is not None and window[0] == 'date':
        ranges = window[1]
        es = [e for e in es if in_window(e['work_date'], ranges)]
    out = []
    for e in es:
        t = tasks.get(e['task_id']) if js_truthy(e.get('task_id')) else None
        pr = projs.get(t['project_id']) if t is not None else None
        u = users.get(e['user_id'])
        out.append(
            {
                'date': e['work_date'],
                'person': coalesce(_g(u, 'name') if u else UNDEF, '—'),
                'department': coalesce(_g(u, 'department') if u else UNDEF, None),
                'project': coalesce(_g(pr, 'name') if pr else UNDEF, '—' if e['kind'] == 'task' else None),
                'client': coalesce(_g(pr, 'client') if pr else UNDEF, None),
                'task': f'{task_code(mss.get(t["milestone_id"]) if js_truthy(t.get("milestone_id")) else None, t)} {js_str(t["title"])}' if t is not None else None,
                'kind': e['kind'],
                'internal_code': _g(e, 'internal_code'),
                'leave_type': _g(e, 'leave_type'),
                'hours': e['hours'],
                'approved': _g(e, 'approved'),
                'week_status': _g(e, 'week_status'),
                'cost': js_round(e['cost']) if sal else None,
            }
        )
    return out


def _b_allocations(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    projs = _by_id(my_projects(ctx))
    mss = _by_id(rows(ctx, 'milestones'))
    c = cal(ctx)
    min_h = prop(S(ctx), 'calendar.min_hours_per_day')
    out = []
    for a in rows(ctx, 'allocations'):
        if a['project_id'] not in projs:
            continue
        wd = working_days_between(a['from_on'], a['to_on'], c)
        ms = mss.get(a['milestone_id']) if js_truthy(a.get('milestone_id')) else None
        out.append(
            {
                'person': nm(a['user_id']),
                'project': _g(projs[a['project_id']], 'name'),
                'milestone': f'M{js_str(ms["no"])} {js_str(ms["name"])}' if ms is not None else '—',
                'pct': a['pct'] / 100,
                'from_on': a['from_on'],
                'to_on': a['to_on'],
                'working_days': wd,
                'planned_hours': js_round(wd * min_h * (a['pct'] / 100) * 10) / 10,
                'is_active': a['from_on'] <= ctx.today and a['to_on'] >= ctx.today,
            }
        )
    return out


def _b_invoices(ctx: Ctx) -> list[dict]:
    projs = _by_id(rows(ctx, 'projects'))
    mss = _by_id(rows(ctx, 'milestones'))
    out = []
    for i in rows(ctx, 'invoices'):
        if i['status'] in ('draft', 'cancelled'):
            continue
        pr = projs.get(i.get('project_id'))
        ms = mss.get(i.get('milestone_id'))
        o = i['amount_paise'] - i['paid_amount_paise']
        late = o > 0 and i['due_on'] < ctx.today
        out.append(
            {
                'number': _g(i, 'number'),
                'client': coalesce(_g(pr, 'client') if pr else UNDEF, '—'),
                'project': coalesce(_g(pr, 'name') if pr else UNDEF, '—'),
                'milestone': f'M{js_str(ms["no"])} {js_str(ms["name"])}' if ms is not None else '—',
                'stream': coalesce(_g(pr, 'stream') if pr else UNDEF, None),
                'invoiced_on': _g(i, 'invoiced_on'),
                'due_on': i['due_on'],
                'paid_on': _g(i, 'paid_on'),
                'amount': i['amount_paise'],
                'paid': i['paid_amount_paise'],
                'outstanding': o,
                'status': 'paid' if o <= 0 else 'partial' if i['paid_amount_paise'] > 0 else 'unpaid',
                'days_overdue': diff_days(ctx.today, i['due_on']) if late else 0,
                'is_overdue': late,
                'age_days': diff_days(ctx.today, i['invoiced_on']),
            }
        )
    return out


def _b_ledger(ctx: Ctx) -> list[dict]:
    projs = _by_id(rows(ctx, 'projects'))
    invs = _by_id(rows(ctx, 'invoices'))
    frm = ctx.today
    for e in rows(ctx, 'ledger_entries'):
        if e['date'] < frm:
            frm = e['date']
    out: list[dict] = []
    for e in expand_ledger(ctx, frm, add_days(ctx.today, 400)):
        pr = projs.get(e['project_id']) if js_truthy(e.get('project_id')) else None
        pc = _g(pr, 'client') if pr else UNDEF
        if pc is None or pc is UNDEF:
            desc = e['description']
            client = js_trim(desc.split(' — ')[-1]) if ' — ' in desc else None
        else:
            client = pc
        inc = e['kind'] == 'income'
        collected = e['amount_paise'] if inc and not js_truthy(e.get('invoice_id')) and e['status'] == 'received' else 0
        out.append(
            {
                'date': e['occurrence'],
                'kind': e['kind'],
                'category': _g(e, 'category'),
                'description': e['description'],
                'stream': _g(e, 'stream'),
                'project': coalesce(_g(pr, 'name') if pr else UNDEF, None),
                'client': client if js_truthy(client) and client != 'Internal' else None,
                'amount': e['amount_paise'],
                'billed': e['amount_paise'] if inc else 0,
                'collected': collected,
                'spent': 0 if inc else e['amount_paise'],
                'net': e['amount_paise'] if inc else -e['amount_paise'],
                'status': e['status'],
                'entry': 'entry',
                'recurring': _g(e, 'recurring'),
            }
        )
    for pay in rows(ctx, 'payments'):
        inv = invs.get(pay['invoice_id'])
        if inv is None:
            continue
        pr = projs.get(inv.get('project_id'))
        out.append(
            {
                'date': pay['paid_on'],
                'kind': 'income',
                'category': 'Payment received',
                'description': f'{js_str(inv["number"])} · {js_str(_g(pay, "reference"))}',
                'stream': coalesce(_g(pr, 'stream') if pr else UNDEF, None),
                'project': coalesce(_g(pr, 'name') if pr else UNDEF, None),
                'client': coalesce(_g(pr, 'client') if pr else UNDEF, None),
                'amount': None,
                'billed': 0,
                'collected': pay['amount_paise'],
                'spent': 0,
                'net': 0,
                'status': 'received',
                'entry': 'payment',
                'recurring': False,
            }
        )
    return out


def _b_deals(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    stale = prop(S(ctx), 'sales.stale_deal_days')
    out = []
    for d in rows(ctx, 'deals'):
        opn = d['stage'] not in ('won', 'lost')
        created = d['created_at'][:10]
        idle = diff_days(ctx.today, d['last_activity_on'])
        chance = DEAL_STAGE[d['stage']]['chance']
        won_on = _g(d, 'won_on')
        out.append(
            {
                'id': d['id'],
                'title': _g(d, 'title'),
                'client': _g(d, 'client'),
                'product': _g(d, 'product'),
                'source': _g(d, 'source'),
                'stage': d['stage'],
                'owner': nm(_g(d, 'owner_id')),
                'is_open': opn,
                'is_won': d['stage'] == 'won',
                'is_lost': d['stage'] == 'lost',
                'value': d['value_paise'],
                'mrr': _g(d, 'mrr_paise'),
                'weighted': js_round(d['value_paise'] * chance) if opn else d['value_paise'] if d['stage'] == 'won' else 0,
                'chance': chance,
                'win': 1 if d['stage'] == 'won' else 0 if d['stage'] == 'lost' else None,
                'created_on': created,
                'expected_close': _g(d, 'expected_close'),
                'won_on': won_on,
                'lost_on': _g(d, 'lost_on'),
                'closed_on': coalesce(won_on, _g(d, 'lost_on'), None),
                'last_activity_on': d['last_activity_on'],
                'days_idle': idle if opn else None,
                'is_stale': opn and idle >= stale,
                'cycle_days': diff_days(won_on, created) if js_truthy(won_on) else None,
                'lost_reason': _g(d, 'lost_reason'),
            }
        )
    return out


def _b_deal_activities(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    deals = _by_id(rows(ctx, 'deals'))
    out = []
    for a in rows(ctx, 'deal_activities'):
        d = deals.get(a['deal_id'])
        out.append(
            {
                'date': _g(a, 'date'),
                'type': _g(a, 'type'),
                'deal': coalesce(_g(d, 'title') if d else UNDEF, '—'),
                'client': coalesce(_g(d, 'client') if d else UNDEF, None),
                'stage': coalesce(_g(d, 'stage') if d else UNDEF, None),
                'owner': nm(_g(a, 'by')),
                'note': _g(a, 'note'),
            }
        )
    return out


def _b_ops(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    out = []
    for o in rows(ctx, 'ops_items'):
        late = o['status'] == 'open' and o['due_on'] < ctx.today
        out.append(
            {
                'item': _g(o, 'item'),
                'category': _g(o, 'category'),
                'owner': nm(_g(o, 'owner_id')),
                'due_on': o['due_on'],
                'recurring': _g(o, 'recurring'),
                'status': o['status'],
                'is_overdue': late,
                'days_overdue': diff_days(ctx.today, o['due_on']) if late else 0,
                'last_done_on': _g(o, 'last_done_on'),
            }
        )
    return out


# ------------------------------------------------------------------ v3 business datasets
def _task_review_rows(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    tasks = _by_id(_visible_tasks(ctx))
    projs = _by_id(rows(ctx, 'projects'))
    out = []
    for r in rows(ctx, 'task_reviews'):
        t = tasks.get(r['task_id'])
        if t is None:
            continue
        pr = projs.get(t['project_id'])
        sub = r['submitted_at'][:10]
        dec = r['decided_at'][:10] if js_truthy(r.get('decided_at')) else None
        decision = r.get('decision')
        out.append(
            {
                'task': _g(t, 'title'),
                'project': coalesce(_g(pr, 'name') if pr else UNDEF, '—'),
                'task_kind': coalesce(_g(t, 'kind'), 'general' if pr is not None and pr.get('code') == 'GEN' else 'project'),
                'owner': nm(coalesce(_g(r, 'submitted_by'), _g(t, 'owner_id'))),
                'reviewer': nm(coalesce(_g(r, 'decided_by'), _g(t, 'reviewer_id'), _g(pr, 'pm_id') if pr else UNDEF, None)),
                'round': _g(r, 'round'),
                'submitted_at': sub,
                'decision': coalesce(_g(r, 'decision'), 'pending'),
                'decided_at': dec,
                'decision_reason': _g(r, 'decision_reason') if decision == 'sent_back' else None,
                'quality_rating': _g(r, 'quality_rating'),
                'delay_reason': _delay_reason(r),
                'late_at_submission': sub > t['due_on'],
                'is_sent_back': decision == 'sent_back',
                'is_approved': decision == 'approved',
                'is_pending': not js_truthy(decision),
                'days_to_decide': diff_days(dec, sub) if dec else None,
            }
        )
    return out


def _leave_rows(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    users = _by_id(rows(ctx, 'users'))
    me = ctx.user_id
    hr = has(ctx, 'people.manage')
    team = team_of(ctx, me)
    out = []
    for r in rows(ctx, 'leave_requests'):
        if not (hr or r['user_id'] == me or r.get('approver_id') == me or r['user_id'] in team):
            continue
        u = users.get(r['user_id'])
        out.append(
            {
                'person': nm(r['user_id']),
                'manager': nm(_g(u, 'reports_to_id') if u else UNDEF),
                'type': _g(r, 'type'),
                'from_on': _g(r, 'from_on'),
                'to_on': _g(r, 'to_on'),
                'days': _g(r, 'days'),
                'status': r['status'],
                'approver': nm(_g(r, 'approver_id')),
                'decided_by': nm(_g(r, 'decided_by')),
                'decision_reason': _g(r, 'decision_reason'),
                'reason': _g(r, 'reason'),
                'applied_on': r['created_at'][:10],
                'is_pending': r['status'] == 'pending',
                'is_approved': r['status'] == 'approved',
            }
        )
    return out


def _expense_rows(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    projs = _by_id(rows(ctx, 'projects'))
    me = ctx.user_id
    lst = rows(ctx, 'expense_claims')
    # Finance-side roles (expenses.pay / finance.view) and HR see every claim.
    sc = 'all' if has_any(ctx, 'expenses.pay', 'finance.view') else row_scope(ctx)
    if sc == 'team':
        mine = {x['id'] for x in my_projects(ctx)}
        team = team_of(ctx, me)
        lst = [
            e
            for e in lst
            if e['user_id'] == me or e.get('approver_id') == me or e['user_id'] in team or (js_truthy(e.get('project_id')) and e['project_id'] in mine)
        ]
    elif sc == 'own':
        lst = [e for e in lst if e['user_id'] == me or e.get('approver_id') == me]
    # drafts are private to their author
    lst = [e for e in lst if e['status'] != 'draft' or e['user_id'] == me]
    out = []
    for e in lst:
        pid = e.get('project_id')
        pr = projs.get(pid) if js_truthy(pid) else None
        out.append(
            {
                'person': nm(e['user_id']),
                'project': coalesce(_g(pr, 'name') if pr else UNDEF, '—') if js_truthy(pid) else None,
                'category': _g(e, 'category'),
                'description': _g(e, 'description'),
                'amount': _g(e, 'amount_paise'),
                'spent_on': _g(e, 'spent_on'),
                'status': e['status'],
                'approver': nm(coalesce(_g(e, 'decided_by'), _g(e, 'approver_id'))),
                'in_budget': _g(e, 'in_budget'),
                'billable': _g(e, 'billable_to_client'),
                'paid_on': _g(e, 'paid_on'),
                'is_unpaid': e['status'] == 'approved' and not js_truthy(e.get('paid_on')),
                'is_pending': e['status'] == 'submitted',
            }
        )
    return out


def _purchase_rows(ctx: Ctx) -> list[dict]:
    from app.routes.spend import po_total

    nm = name_of(ctx)
    vendors = _by_id(rows(ctx, 'vendors'))
    projs = _by_id(rows(ctx, 'projects'))
    bills = rows(ctx, 'vendor_bills')
    billed_by_po: dict[Any, Any] = {}
    for b in bills:
        if b.get('status') != 'disputed':
            billed_by_po[b.get('po_id')] = coalesce(billed_by_po.get(b.get('po_id'), UNDEF), 0) + b['amount_paise'] + b['gst_paise']
    out = []
    for po in rows(ctx, 'purchase_orders'):
        v = vendors.get(po.get('vendor_id'))
        pid = po.get('project_id')
        title = _g(po, 'title')
        out.append(
            {
                'number': _g(po, 'number'),
                'title': title if title is not None and title is not UNDEF else js_join((ln.get('description', UNDEF) for ln in po['lines']), ', '),
                'vendor': coalesce(_g(v, 'name') if v else UNDEF, '—'),
                'category': _g(po, 'budget_category'),
                'project': coalesce(_g(projs[pid], 'name') if js_truthy(pid) and pid in projs else UNDEF, '—') if js_truthy(pid) else None,
                'amount': po_total(po),
                'status': po['status'],
                'requested_by': nm(_g(po, 'requested_by')),
                'approver': nm(coalesce(_g(po, 'decided_by'), _g(po, 'approver_id'))),
                'approved': po['status'] in ('approved', 'closed'),
                'is_pending': po['status'] == 'pending',
                'over_budget': js_truthy(po.get('over_budget')),
                'raised_on': po['created_at'][:10],
                'needed_by': _g(po, 'needed_by'),
                'billed': coalesce(billed_by_po.get(po['id'], UNDEF), 0),
            }
        )
    return out


def _vendor_bill_rows(ctx: Ctx) -> list[dict]:
    vendors = _by_id(rows(ctx, 'vendors'))
    pos = _by_id(rows(ctx, 'purchase_orders'))
    out = []
    for b in rows(ctx, 'vendor_bills'):
        unpaid = b['status'] != 'paid'
        overdue = unpaid and b['status'] != 'disputed' and b['due_on'] < ctx.today
        po = pos.get(b['po_id']) if js_truthy(b.get('po_id')) else None
        v = vendors.get(b.get('vendor_id'))
        cat_ = coalesce(
            _g(b, 'category'),
            (_g(po, 'budget_category') if po else UNDEF) if js_truthy(b.get('po_id')) else None,
            _g(v, 'category') if v else UNDEF,
            'Other',
        )
        out.append(
            {
                'number': _g(b, 'number'),
                'vendor': coalesce(_g(v, 'name') if v else UNDEF, '—'),
                'po': coalesce(_g(po, 'number') if po else UNDEF, None) if js_truthy(b.get('po_id')) else None,
                'category': cat_,
                'amount': b['amount_paise'],
                'gst': b['gst_paise'],
                'tds': b['tds_paise'],
                'total': b['amount_paise'] + b['gst_paise'],
                'payable': b['amount_paise'] + b['gst_paise'] - b['tds_paise'],
                'bill_date': _g(b, 'bill_date'),
                'due_on': b['due_on'],
                'status': b['status'],
                'paid_on': _g(b, 'paid_on'),
                'is_overdue': overdue,
                'is_unpaid': unpaid,
                'days_overdue': diff_days(ctx.today, b['due_on']) if overdue else 0,
            }
        )
    return out


def _budget_rows(ctx: Ctx) -> list[dict]:
    """Budget vs actual per category-month, straight from the Spend module's budgetStatement (current and previous FY)."""
    from app.routes.spend import budget_statement

    fy = fy_start_year(ctx.today, prop(S(ctx), 'company.fy_start_month'))
    out = []
    for y in (fy - 1, fy):
        st = budget_statement(ctx, y)
        if not js_truthy(st['has_budgets']) and not st['expense'] and not st['revenue']:
            continue
        for ln in [*st['expense'], *st['revenue']]:
            over_ytd = ln['kind'] == 'expense' and js_truthy(ln['over']) and ln['ytd_actual'] > 0
            for i, m in enumerate(st['months']):
                mb, ma, mc = ln['monthly_budget'], ln['monthly_actual'], ln['monthly_committed']
                budget = coalesce(mb[i] if i < len(mb) else UNDEF, 0)
                actual = coalesce(ma[i] if i < len(ma) else UNDEF, 0) + coalesce(mc[i] if i < len(mc) else UNDEF, 0)
                variance = budget - actual if ln['kind'] == 'expense' else actual - budget
                out.append(
                    {
                        'category': ln['label'],
                        'kind': ln['kind'],
                        'month': f'{m}-01',
                        'budget': budget,
                        'actual': actual,
                        'variance': variance,
                        'variance_pct': variance / budget if js_truthy(budget) else None,
                        'is_over': actual > budget if ln['kind'] == 'expense' else actual < budget,
                        'over_budget_ytd': over_ytd,
                        'has_budget': ln['has_budget'],
                    }
                )
    return out


def _leakage_data_rows(ctx: Ctx) -> list[dict]:
    from app.routes.spend import leakage_rows

    out = []
    for r in leakage_rows(ctx):
        out.append(
            {
                'kind': _g(r, 'kind'),
                'client': _g(r, 'client'),
                'project': _g(r, 'project_name'),
                'amount': r['amount_paise'],
                'recovered': _g(r, 'recovered_paise'),
                'net': r['amount_paise'] - r['recovered_paise'],
                'date': _g(r, 'date'),
                'status': _g(r, 'status'),
                'auto': _g(r, 'auto'),
                'recoverable': _g(r, 'recoverable'),
                'reason': _g(r, 'reason'),
                'source': coalesce(_g(r, 'source'), 'System' if js_truthy(r.get('auto')) else 'Recorded manually'),
            }
        )
    return out


# Appraisal system score — mirrors computeMetrics in routes/hr.ts. Completed appraisals use the stored snapshot.
def _ts_compliance(ctx: Ctx, u: dict, frm: str, to: str) -> dict[str, Any]:
    weeks_all = rows(ctx, 'timesheet_weeks')
    adopted = ctx.today
    for w in weeks_all:
        if w['week_start'] < adopted:
            adopted = w['week_start']
    start = week_start(max_iso(max_iso(frm, u['joined_on']), adopted))
    end = min_iso(to, add_days(week_start(ctx.today), -1))
    by: dict[str, dict] = {}
    for w in weeks_all:
        if w['user_id'] == u['id']:
            by[w['week_start']] = w
    expected = 0
    approved = 0
    late = 0
    m = start
    while add_days(m, 6) <= end:
        expected += 1
        w = by.get(m)
        if w is not None and w['status'] == 'approved':
            approved += 1
        if w is not None and js_truthy(w.get('late')):
            late += 1
        m = add_days(m, 7)
    return {'late': late, 'pct': approved / expected if expected else None}


def _approval_sla_of(ctx: Ctx, manager_id: str, frm: str, to: str) -> Any:
    users = _by_id(rows(ctx, 'users'))
    decided = []
    for w in rows(ctx, 'timesheet_weeks'):
        if not js_truthy(w.get('submitted_at')) or not js_truthy(w.get('decided_at')) or w.get('decided_by') != manager_id:
            continue
        d = w['decided_at'][:10]
        if d < frm or d > to:
            continue
        owner = users.get(w['user_id'])
        if owner is not None and owner['id'] != manager_id:
            decided.append(w)
    if not decided:
        return None
    return sum(1 for w in decided if w['decided_at'][:10] <= sla_due(ctx, w['submitted_at'])) / len(decided)


def _r1(n: Any) -> Any:
    return js_round(n * 10) / 10


def _live_appraisal_metrics(ctx: Ctx, a: dict, c: dict) -> dict[str, Any]:
    u = next((x for x in rows(ctx, 'users') if x['id'] == a['user_id']), None)
    if u is None:
        return {'on_time_pct': None, 'suggested': None}
    ts = task_stats(ctx, {'userIds': [a['user_id']], 'from': c['period_from'], 'to': c['period_to']})
    tc = _ts_compliance(ctx, u, c['period_from'], c['period_to'])
    goals = [
        g
        for g in rows(ctx, 'goals')
        if g['user_id'] == a['user_id'] and g.get('cycle_id') == c['id'] and js_truthy(g.get('manager_approved')) and g['weight'] > 0
    ]
    gw: Any = 0
    for g in goals:
        gw = gw + g['weight']
    if gw:
        acc: Any = 0
        for g in goals:
            acc = acc + g['weight'] * min(1, g['current'] / g['target'] if js_truthy(g.get('target')) else 0)
        gs: Any = acc / gw
    else:
        gs = None
    is_mgr = any(x.get('reports_to_id') == a['user_id'] for x in rows(ctx, 'users'))
    sla = _approval_sla_of(ctx, a['user_id'], c['period_from'], c['period_to']) if is_mgr else None

    def scale(x: Any) -> Any:
        return _r1(max(1, min(5, 1 + 4 * x)))

    score_tasks = None
    if ts['completed'] > 0 and ts['on_time_pct'] is not None:
        parts = [(ts['on_time_pct'], 0.6)]
        if ts['first_pass_pct'] is not None:
            parts.append((ts['first_pass_pct'], 0.2))
        if ts['avg_quality'] is not None:
            parts.append(((ts['avg_quality'] - 1) / 4, 0.2))
        if sla is not None:
            parts.append((sla, 0.15))
        wsum: Any = 0
        for x in parts:
            wsum = wsum + x[1]
        tot: Any = 0
        for x in parts:
            tot = tot + x[0] * x[1]
        score_tasks = scale(tot / wsum)
    score_ts = None if tc['pct'] is None else _r1(max(1, scale(tc['pct']) - min(0.5, 0.1 * tc['late'])))
    w = c['weights']
    mr = _g(a, 'manager_rating')
    comps = [(None if gs is None else scale(gs), w['goals']), (score_tasks, w['tasks']), (score_ts, w['timesheets']), (NAN if mr is UNDEF else mr, w['behaviour'])]
    avail = [x for x in comps if x[0] is not None and x[1] > 0]
    ws: Any = 0
    for x in avail:
        ws = ws + x[1]
    if ws:
        acc2: Any = 0
        for x in avail:
            acc2 = acc2 + x[0] * x[1]
        suggested = _r1(acc2 / ws)
    else:
        suggested = None
    return {'on_time_pct': ts['on_time_pct'], 'suggested': suggested}


def _appraisal_rows(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    hr = has(ctx, 'appraisals.manage')
    me = ctx.user_id
    team = team_of(ctx, me)
    cycles = _by_id(rows(ctx, 'appraisal_cycles'))
    out = []
    for a in rows(ctx, 'appraisals'):
        if a['cycle_id'] not in cycles:
            continue
        if not (hr or (a['user_id'] != me and (a.get('manager_id') == me or a['user_id'] in team))):
            continue
        c = cycles[a['cycle_id']]
        done = a['status'] == 'completed'
        snap = a.get('metrics')
        if done and js_truthy(snap) and isinstance(snap, dict) and len(snap):
            m = {'on_time_pct': coalesce(snap.get('on_time_pct', UNDEF), None), 'suggested': coalesce(snap.get('suggested', UNDEF), None)}
        else:
            m = _live_appraisal_metrics(ctx, a, c)
        inc = _g(a, 'increment_pct')
        out.append(
            {
                'cycle': _g(c, 'name'),
                'period_from': c['period_from'],
                'person': nm(a['user_id']),
                'manager': nm(_g(a, 'manager_id')),
                'status': a['status'],
                'self_rating': _g(a, 'self_rating'),
                'manager_rating': _g(a, 'manager_rating'),
                'final_rating': _g(a, 'final_rating'),
                'increment_pct': None if inc is None else (inc / 100 if is_num(inc) else js_number(inc) / 100),
                'on_time_pct': m['on_time_pct'],
                'system_score': m['suggested'],
                'is_completed': done,
                'promotion_to': _g(a, 'promotion_to'),
            }
        )
    return out


def _candidate_rows(ctx: Ctx) -> list[dict]:
    if not has(ctx, 'recruitment.manage'):
        return []
    sal = can_see_salaries(ctx)
    openings = _by_id(rows(ctx, 'openings'))
    log: dict[Any, dict] = {}
    for x in rows(ctx, 'activity_log'):
        if x.get('entity') == 'candidate' and x.get('action') == 'hired':
            log.setdefault(x.get('entity_id'), x)
    out = []
    for c in rows(ctx, 'candidates'):
        if c['stage'] == 'hired':
            lg = log.get(c['id'])
            hired_at = coalesce(_g(lg, 'at') if lg else UNDEF, c['updated_at'])[:10]
        else:
            hired_at = ctx.today
        o = openings.get(c.get('opening_id'))
        out.append(
            {
                'opening': coalesce(_g(o, 'role') if o else UNDEF, '—'),
                'name': _g(c, 'name'),
                'source': _g(c, 'source'),
                'stage': c['stage'],
                'experience': _g(c, 'experience_years'),
                'expected_ctc': _g(c, 'expected_ctc_paise') if sal else None,
                'days_in_pipeline': max(0, diff_days(hired_at, c['created_at'][:10])),
                'applied_on': c['created_at'][:10],
                'rating': _g(c, 'rating'),
                'is_active': c['stage'] not in ('hired', 'rejected', 'withdrawn'),
                'is_hired': c['stage'] == 'hired',
            }
        )
    return out


def _document_rows(ctx: Ctx) -> list[dict]:
    """Documents — visibility mirrors routes/documents.ts (canSeeAs / canManageAs / isRequired, not exported there)."""
    nm = name_of(ctx)
    projs = _by_id(rows(ctx, 'projects'))
    members: dict[str, set] = {}

    def add(pid: Any, u: Any) -> None:
        if not js_truthy(u):
            return
        members.setdefault(pid, set()).add(u)

    for t in rows(ctx, 'tasks'):
        add(t['project_id'], t.get('owner_id'))
        add(t['project_id'], t.get('backup_id'))
    for a in rows(ctx, 'allocations'):
        add(a['project_id'], a['user_id'])
    for pr in projs.values():
        add(pr['id'], pr.get('pm_id'))

    def pm_of(pid: Any) -> Any:
        if not js_truthy(pid):
            return None
        pr = projs.get(pid)
        return coalesce(_g(pr, 'pm_id') if pr else UNDEF, None)

    def can_manage_as(vid: str, perms: Any, d: dict) -> bool:
        if not js_truthy(d.get('project_id')):
            return 'documents.company' in perms
        return 'documents.manage' in perms and ('documents.view_all' in perms or pm_of(d['project_id']) == vid)

    def can_see_as(vid: str, perms: Any, d: dict) -> bool:
        if js_truthy(d.get('archived')) and not can_manage_as(vid, perms, d):
            return False
        if d.get('visibility') == 'company':
            return True
        if 'documents.view_all' in perms:
            return True
        if not js_truthy(d.get('project_id')):
            return 'documents.manage' in perms or 'documents.company' in perms
        if pm_of(d['project_id']) == vid:
            return True
        if d.get('visibility') == 'managers':
            return False
        return vid in members.get(d['project_id'], set())

    def is_required(vid: str, perms: Any, d: dict) -> bool:
        if not can_see_as(vid, perms, d):
            return False
        if not js_truthy(d.get('project_id')) or d.get('visibility') == 'company':
            return True
        if pm_of(d['project_id']) == vid:
            return True
        if d.get('visibility') == 'managers':
            return 'documents.view_all' in perms
        return vid in members.get(d['project_id'], set())

    me = ctx.user_id
    mp = ctx.perms
    active = [u for u in rows(ctx, 'users') if u.get('status') == 'active']
    perms_cache: dict[str, set] = {}

    def perms_of(u: dict) -> set:
        x = perms_cache.get(u['id'])
        if x is None:
            x = set(permissions_of(u))
            perms_cache[u['id']] = x
        return x

    acks = rows(ctx, 'document_acks')
    acks_by: dict[Any, list] = {}
    for a in acks:
        acks_by.setdefault(a['document_id'], []).append(a)
    versions = rows(ctx, 'document_versions')
    ver_by: dict[tuple, dict] = {}
    for v in versions:
        ver_by.setdefault((v['document_id'], v['version']), v)
    out = []
    for d in rows(ctx, 'documents'):
        if not can_see_as(me, mp, d):
            continue
        cur = [a for a in acks_by.get(d['id'], []) if a['version'] == d['current_version']]
        acknowledged: Any = None
        pending: Any = None
        audience: Any = None
        if js_truthy(d.get('must_read')) and can_manage_as(me, mp, d):
            aud = [u for u in active if is_required(u['id'], perms_of(u), d)]
            ids = {u['id'] for u in aud}
            audience = len(aud)
            acknowledged = len({a['user_id'] for a in cur if a['user_id'] in ids})
            pending = audience - acknowledged
        v = ver_by.get((d['id'], d['current_version']))
        if not js_truthy(d.get('must_read')):
            my_ack = 'not_required'
        elif any(a['user_id'] == me for a in cur):
            my_ack = 'acknowledged'
        elif is_required(me, mp, d):
            my_ack = 'pending'
        else:
            my_ack = 'not_required'
        pid = d.get('project_id')
        out.append(
            {
                'title': _g(d, 'title'),
                'project': coalesce(_g(projs[pid], 'name') if pid in projs else UNDEF, '—') if js_truthy(pid) else 'Company library',
                'category': _g(d, 'category'),
                'visibility': _g(d, 'visibility'),
                'version': d['current_version'],
                'must_read': _g(d, 'must_read'),
                'acknowledged': acknowledged,
                'pending': pending,
                'audience': audience,
                'ack_pct': coalesce(acknowledged, 0) / audience if js_truthy(audience) else None,
                'my_ack': my_ack,
                'owner': nm(_g(d, 'owner_id')),
                'updated_on': coalesce(_g(v, 'uploaded_at') if v else UNDEF, d['updated_at'])[:10],
                'archived': _g(d, 'archived'),
            }
        )
    return out


def _okr_rows(ctx: Ctx) -> list[dict]:
    """OKR key results — auto values mirror autoValue/statusOf in routes/company.ts (not exported there)."""
    nm = name_of(ctx)
    fy_s = prop(S(ctx), 'company.fy_start_month')
    obj_map = _by_id(rows(ctx, 'objectives'))
    finance = has(ctx, 'finance.view')
    today = ctx.today

    def see_finance(metric: str) -> bool:
        return not js_truthy(METRIC_META[metric].get('finance', UNDEF)) or finance or (metric == 'deals_won_value' and has_any(ctx, 'deals.edit_own', 'projects.edit'))

    def clamp01(n: Any) -> Any:
        return max(0, min(1, n))

    def progress_of(start: Any, target: Any, current: Any) -> Any:
        if current is None or current is UNDEF:
            return None
        if target == start:
            return 1 if current == target else 0
        return clamp01((current - start) / (target - start))

    def elapsed_of(frm: str, to: str) -> Any:
        if today < frm:
            return 0
        if today > to:
            return 1
        return clamp01((diff_days(today, frm) + 1) / (diff_days(to, frm) + 1))

    def status_of(pr: Any, expected: Any, started: bool) -> str:
        if not started:
            return 'not_started'
        if pr is None or pr >= 1:
            return 'on_track'
        e = max(expected, 0.02)
        return 'on_track' if pr >= 0.9 * e else 'at_risk' if pr >= 0.6 * e else 'off_track'

    def scope_of(o: dict) -> list | None:
        if o['level'] == 'company':
            return None
        if o['level'] == 'team':
            return [o['owner_id'], *(u['id'] for u in perf_team_of(ctx, o['owner_id']))]
        return [o['owner_id']]

    def compliance(frm: str, to: str, ids: list | None) -> Any:
        users = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin')) and (ids is None or u['id'] in ids)]
        by_key: dict[str, dict] = {}
        for w in rows(ctx, 'timesheet_weeks'):
            by_key[w['user_id'] + '|' + w['week_start']] = w
        due = 0
        ok = 0
        last_monday = add_days(week_start(today), -7)
        mon = week_start(frm)
        while mon <= last_monday and mon <= to:
            for u in users:
                if u['joined_on'] > add_days(mon, 6):
                    continue
                due += 1
                w = by_key.get(u['id'] + '|' + mon)
                if w is not None and w['status'] in ('submitted', 'approved'):
                    ok += 1
            mon = add_days(mon, 7)
        return (ok / due) * 100 if due else None

    def auto_value(o: dict, kr: dict) -> Any:
        frm = o['period_from']
        to = o['period_to'] if o['period_to'] < today else today
        if frm > today:
            return None
        ids = scope_of(o)
        early = working_days_between(frm, to, cal(ctx)) < 10
        r_from = add_days(today, -29) if early else frm
        metric = kr['metric']
        if metric == 'revenue_billed':
            return revenue_between(ctx, frm, to)['billed']
        if metric == 'revenue_collected':
            return revenue_between(ctx, frm, to)['collected']
        if metric == 'deals_won_value':
            tot: Any = 0
            for d in rows(ctx, 'deals'):
                if d['stage'] == 'won' and js_truthy(d.get('won_on')) and d['won_on'] >= frm and d['won_on'] <= to and (ids is None or d.get('owner_id') in ids):
                    tot = tot + d['value_paise']
            return tot
        if metric == 'on_time_task_pct':
            st = task_stats(ctx, {'userIds': ids if ids is not None else UNDEF, 'from': r_from, 'to': to})
            return None if st['on_time_pct'] is None else js_round(st['on_time_pct'] * 1000) / 10
        if metric == 'timesheet_compliance':
            last_monday = add_days(week_start(today), -7)
            c_from = add_days(last_monday, -21) if early or week_start(frm) > last_monday else frm
            v = compliance(c_from, to, ids)
            return None if v is None else js_round(v * 10) / 10
        if metric == 'margin_pct':
            r = revenue_between(ctx, r_from, to)
            return None if r['margin'] is None else js_round(r['margin'] * 1000) / 10
        if metric == 'headcount':
            return sum(1 for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin')) and (ids is None or u['id'] in ids))
        if metric == 'leakage':
            tot = 0
            for x in rows(ctx, 'revenue_leakage'):
                if x['date'] >= frm and x['date'] <= to and x['status'] != 'recovered':
                    tot = tot + x['amount_paise'] - coalesce(_g(x, 'recovered_paise'), 0)
            return tot
        return _g(kr, 'current')

    obj_progress: dict[str, list] = {}
    pending: list[tuple[dict, dict]] = []
    for k in rows(ctx, 'key_results'):
        o = obj_map.get(k['objective_id'])
        if o is None:
            continue
        auto = k['metric'] != 'manual'
        current = auto_value(o, k) if auto else _g(k, 'current')
        prog = progress_of(k['start_value'], k['target'], current)
        visible = see_finance(k['metric'])
        money = METRIC_META[k['metric']]['kind'] == 'money' or (k['metric'] == 'manual' and k.get('unit') == '₹')

        def show(v: Any, visible: bool = visible, money: bool = money) -> Any:
            if not visible or v is None:
                return None
            if money:
                return js_round((NAN if v is UNDEF else v) / 100)
            return v

        expected = elapsed_of(o['period_from'], o['period_to'])
        row = {
            'objective': _g(o, 'title'),
            'key_result': _g(k, 'title'),
            'level': o['level'],
            'owner': nm(_g(k, 'owner_id')),
            'objective_owner': nm(_g(o, 'owner_id')),
            'period': o['period'] if js_truthy(o.get('period')) else quarter_of(o['period_from'], fy_s)['label'],
            'period_from': o['period_from'],
            'metric': k['metric'],
            'current': show(current),
            'target': show(k['target']),
            'unit': '₹' if money else _g(k, 'unit'),
            'progress_pct': prog,
            'status': 'done' if o['status'] == 'done' else status_of(prog, expected, today >= o['period_from']),
            'objective_status': None,
            'auto': auto,
        }
        pending.append((row, o))
        lst = obj_progress.get(o['id'], [])
        if prog is not None:
            lst.append(prog)
        obj_progress[o['id']] = lst
    out = []
    for row, o in pending:
        ps = obj_progress.get(o['id'], [])
        if ps:
            acc: Any = 0
            for x in ps:
                acc = acc + x
            pv: Any = acc / len(ps)
        else:
            pv = None
        row['objective_status'] = 'done' if o['status'] == 'done' else status_of(pv, elapsed_of(o['period_from'], o['period_to']), today >= o['period_from'])
        out.append(row)
    return out


def _payroll_rows(ctx: Ctx) -> list[dict]:
    """Payroll lines from runs; months without a run in the FY (up to this month) are estimated from CTC."""
    users = rows(ctx, 'users')
    umap = _by_id(users)
    runs = rows(ctx, 'payroll_runs')
    lines_by: dict[Any, list] = {}
    for ln in rows(ctx, 'payroll_lines'):
        lines_by.setdefault(ln['run_id'], []).append(ln)
    out: list[dict] = []
    covered: set[str] = set()
    for run in runs:
        for ln in lines_by.get(run['id'], []):
            u = umap.get(ln['user_id'])
            covered.add(f'{run["month"]}|{ln["user_id"]}')
            out.append(
                {
                    'month': f'{run["month"]}-01',
                    'person': coalesce(_g(u, 'name') if u else UNDEF, '—'),
                    'department': coalesce(_g(u, 'department') if u else UNDEF, None),
                    'role': coalesce(_g(u, 'role') if u else UNDEF, None),
                    'ctc': ln['monthly_ctc_paise'],
                    'base': ln['base_paise'],
                    'deduction': ln['deduction_paise'],
                    'variable': ln['variable_paise'],
                    'net': ln['net_paise'],
                    'working_days': ln['working_days'],
                    'unapproved_days': ln['unapproved_days'],
                    'lwp_days': ln['lwp_days'],
                    'status': run['status'],
                }
            )
    rates = rows(ctx, 'cost_rates')
    deals = rows(ctx, 'deals')
    c = cal(ctx)
    fy_s = prop(S(ctx), 'company.fy_start_month')
    start = add_months(fy_range(fy_start_year(ctx.today, fy_s) - 1, fy_s)['from'], 0)
    m = start
    while month_of(m) <= month_of(ctx.today):
        ym = month_of(m)
        end = month_range(ym)['to']
        for u in users:
            if js_truthy(u.get('is_super_admin')) or u.get('status') == 'invited' or f'{ym}|{u["id"]}' in covered:
                continue
            if u['joined_on'] > end:
                continue
            r = rate_on(rates, u['id'], end)
            if r is None:
                continue
            achieved: Any = 0
            for d in deals:
                if d.get('owner_id') == u['id'] and d['stage'] == 'won' and js_truthy(d.get('won_on')) and month_of(d['won_on']) == ym:
                    achieved = achieved + d['value_paise']
            comp = sales_compensation(r['monthly_ctc_paise'], r['variable_pct'], r.get('sales_target_paise'), achieved, prop(S(ctx), 'pay.variable_threshold_pct'))
            out.append(
                {
                    'month': f'{ym}-01',
                    'person': _g(u, 'name'),
                    'department': _g(u, 'department'),
                    'role': _g(u, 'role'),
                    'ctc': r['monthly_ctc_paise'],
                    'base': comp['base'],
                    'deduction': 0,
                    'variable': comp['variableEarned'],
                    'net': comp['base'] + comp['variableEarned'],
                    'working_days': working_days_in_month(ym, c),
                    'unapproved_days': 0,
                    'lwp_days': 0,
                    'status': 'estimate',
                }
            )
        m = add_months(m, 1)
    return out


def week_history(ctx: Ctx, user_id: str, n: int, end_monday: Any = UNDEF) -> list[dict]:
    """Weekly timesheet status grid for the last `n` completed weeks (oldest first)."""
    if end_monday is UNDEF or end_monday is None:
        end_monday = add_days(week_start(ctx.today), -7)
    by_start: dict[str, dict] = {}
    for w in rows(ctx, 'timesheet_weeks'):
        if w['user_id'] == user_id:
            by_start[w['week_start']] = w
    out = []
    for i in range(n - 1, -1, -1):
        mon = add_days(end_monday, -7 * i)
        out.append({'week_start': mon, 'status': effective_week_status(ctx, by_start.get(mon), mon)})
    return out


def is_timesheet_user(u: dict) -> bool:
    return u.get('status') == 'active' and not js_truthy(u.get('is_super_admin'))


def _people_rows(ctx: Ctx) -> list[dict]:
    nm = name_of(ctx)
    sal = can_see_salaries(ctx)
    rates = rows(ctx, 'cost_rates')
    tasks = rows(ctx, 'tasks')
    act = task_actuals(ctx)
    entries = costed_entries(ctx)
    c = cal(ctx)
    min_h = prop(S(ctx), 'calendar.min_hours_per_day')
    users = [u for u in rows(ctx, 'users') if is_timesheet_user(u)]
    if row_scope(ctx) != 'all':
        team = team_of(ctx, ctx.user_id)
        mine = {x['id'] for x in my_projects(ctx)}
        for t in tasks:
            if t['project_id'] in mine and js_truthy(t.get('owner_id')):
                team.add(t['owner_id'])
        team.add(ctx.user_id)
        users = [u for u in users if u['id'] in team]
    d = ctx.today
    next10: list[str] = []
    while len(next10) < 10:
        if is_working_day(d, c):
            next10.append(d)
        d = add_days(d, 1)
    from30 = add_days(ctx.today, -29)
    tasks_by_owner: dict[Any, list] = {}
    for t in tasks:
        tasks_by_owner.setdefault(t.get('owner_id'), []).append(t)
    ent_by: dict[Any, list] = {}
    for e in entries:
        ent_by.setdefault(e['user_id'], []).append(e)
    wd_now = working_days_in_month(month_of(ctx.today), c)
    out = []
    for u in users:
        r = rate_on(rates, u['id'], ctx.today)
        mine_t = tasks_by_owner.get(u['id'], [])
        done = [t for t in mine_t if t['status'] == 'done']
        done_est: Any = 0
        for t in done:
            done_est = done_est + t['est_hours']
        done_act: Any = 0
        for t in done:
            done_act = done_act + (act[t['id']]['hours'] if t['id'] in act else 0)
        hist = week_history(ctx, u['id'], 8)
        ok = sum(1 for h in hist if h['status'] in ('approved', 'submitted'))
        last = hist[-1]
        alloc10: Any = 0
        for x in next10:
            alloc10 = alloc10 + allocation_on(ctx, u['id'], x)
        h30: Any = 0
        for e in ent_by.get(u['id'], []):
            if e['work_date'] >= from30 and e['kind'] != 'leave':
                h30 = h30 + e['hours']
        out.append(
            {
                'name': _g(u, 'name'),
                'title': _g(u, 'title'),
                'role': _g(u, 'role'),
                'department': _g(u, 'department'),
                'manager': nm(_g(u, 'reports_to_id')),
                'joined_on': _g(u, 'joined_on'),
                'ctc': r['monthly_ctc_paise'] if sal and r is not None else None,
                'cost_per_hour': js_round(cost_per_hour(r['monthly_ctc_paise'], wd_now, min_h)) if sal and r is not None else None,
                'allocation_today': allocation_on(ctx, u['id'], ctx.today) / 100,
                'allocation_next_10d': alloc10 / 10 / 100,
                'open_tasks': sum(1 for t in mine_t if t['status'] != 'done'),
                'overdue_tasks': sum(1 for t in mine_t if t['status'] != 'done' and t['due_on'] < ctx.today),
                'effort_ratio': js_round((done_act / done_est) * 1000) / 1000 if js_truthy(done_est) and js_truthy(done_act) else None,
                'on_time': sum(1 for t in done if js_truthy(t.get('done_on')) and t['done_on'] <= t['due_on']) / len(done) if done else None,
                'hours_30d': h30,
                'last_week_status': 'locked' if last['status'] == 'missing' else last['status'],
                'unsubmitted_weeks_8w': len(hist) - ok,
                'ts_compliance_8w': ok / len(hist),
            }
        )
    return out


_BUILDERS: dict[str, Any] = {
    'projects': _b_projects,
    'milestones': _b_milestones,
    'tasks': _b_tasks,
    'hours': _b_hours,
    'allocations': _b_allocations,
    'invoices': _b_invoices,
    'ledger': _b_ledger,
    'deals': _b_deals,
    'deal_activities': _b_deal_activities,
    'payroll': _payroll_rows,
    'people': _people_rows,
    'ops': _b_ops,
    'task_reviews': _task_review_rows,
    'leave_requests': _leave_rows,
    'expenses': _expense_rows,
    'purchases': _purchase_rows,
    'vendor_bills': _vendor_bill_rows,
    'budget_vs_actual': _budget_rows,
    'revenue_leakage': _leakage_data_rows,
    'appraisals': _appraisal_rows,
    'candidates': _candidate_rows,
    'documents': _document_rows,
    'okr_key_results': _okr_rows,
}


_WINDOWED = frozenset({'hours'})  # builders that accept the report's period window


def dataset_rows(ctx: Ctx, id_: Any, window: tuple[Any, list[dict]] | None = None) -> dict[str, Any]:
    """Rows for a dataset, with any field the viewer can't see stripped (defence in depth) → {'ds', 'rows'}.
    window (execute_spec): rows outside the report's period may be left out by the builders that support it."""
    ds = next((d for d in visible_datasets(ctx) if d['id'] == id_), None) if isinstance(id_, str) else None
    if ds is None:
        dm = DATASET_MAP.get(js_str(id_))
        what = js_lower(dm['label']) if dm is not None else js_str(id_)
        err(403, 'FORBIDDEN', f'Your role can’t report on {what}. Ask an admin if you need it.')
    allowed = {f['key'] for f in ds['fields']}
    raw = _BUILDERS[id_](ctx, window) if window is not None and id_ in _WINDOWED else _BUILDERS[id_](ctx)
    hidden = [f['key'] for f in DATASET_MAP[id_]['fields'] if f['key'] not in allowed]
    if hidden:
        for r in raw:
            for k in hidden:
                r[k] = None
    return {'ds': ds, 'rows': raw}


def execute_spec(ctx: Ctx, spec: Any) -> dict[str, Any]:
    if not js_truthy(spec) or not isinstance(spec, (dict, list)) or not js_truthy(p(spec, 'dataset')):
        err(422, 'VAL_REPORT', 'Pick a dataset for the report.', 'dataset')
    window = None
    ds_id = spec['dataset']
    if isinstance(ds_id, str) and ds_id in _WINDOWED:
        # the same sanitised spec as below (sanitize_spec is pure): its period bounds the rows worth building
        ds0 = next((d for d in visible_datasets(ctx) if d['id'] == ds_id), None)
        if ds0 is not None:
            try:
                window = period_window(sanitize_spec(spec, ds0)['spec'], ds0, ctx.today, prop(S(ctx), 'company.fy_start_month'))
            except ApiError:
                window = None  # the real call below raises it, after building the rows, as before
    dr = dataset_rows(ctx, ds_id, window)
    ds = dr['ds']
    sv = sanitize_spec(spec, ds)
    return run_spec(sv['spec'], ds, dr['rows'], {'today': ctx.today, 'fyStart': prop(S(ctx), 'company.fy_start_month')}, sv['warnings'])


# ------------------------------------------------------------------
# Saved reports
# ------------------------------------------------------------------
def _curates_reports(ctx: Ctx) -> bool:
    """Workspace administrators (settings.edit) curate everyone's saved reports and the system templates."""
    return has(ctx, 'settings.edit')


def _can_edit_report(ctx: Ctx, r: dict) -> bool:
    return r['owner_id'] == ctx.user_id or _curates_reports(ctx)


def _visible_reports(ctx: Ctx) -> list[dict]:
    ds = {d['id'] for d in visible_datasets(ctx)}
    cur = _curates_reports(ctx)
    me = ctx.user_id
    return [
        r
        for r in rows(ctx, 'saved_reports')
        if p(r.get('spec'), 'dataset') in ds and (r['owner_id'] == me or js_truthy(r.get('shared')) or r.get('source') == 'system' or cur)
    ]


def _preview_of(ctx: Ctx, r: dict) -> Any:
    try:
        res = execute_spec(ctx, r['spec'])
        m = next((c for c in res['columns'] if c.get('role') == 'measure'), None)
        d = next((c for c in res['columns'] if c.get('role') == 'dimension'), None)
        if m is not None and m['type'] == 'money':
            fmt = 'money'
        elif m is not None and m['type'] == 'percent':
            fmt = 'percent'
        elif m is not None and m.get('unit') == 'hours':
            fmt = 'hours'
        else:
            fmt = 'number'
        if m is None:
            return {'labels': [], 'values': [], 'kind': 'table', 'value': res['rowCount'], 'format': 'number'}
        if d is None:
            return {'labels': [], 'values': [], 'kind': 'kpi', 'value': coalesce(res['totals'].get(m['key'], UNDEF), None), 'format': fmt}
        rs = res['rows'][:12]
        return {
            'labels': [js_str(x.get(d['key'], UNDEF)) for x in rs],
            'values': [js_number(coalesce(x.get(m['key'], UNDEF), 0)) for x in rs],
            'kind': r['spec']['chart'],
            'value': coalesce(res['totals'].get(m['key'], UNDEF), None),
            'format': fmt,
        }
    except (ApiError, Exception):  # noqa: BLE001 — the TS catches everything
        return None


def _view(ctx: Ctx, r: dict, with_preview: bool = True) -> dict[str, Any]:
    dsid = p(r.get('spec'), 'dataset')
    dm = DATASET_MAP.get(dsid) if isinstance(dsid, str) else None
    return {
        **r,
        'owner_name': user_name(ctx, r['owner_id']),
        'can_edit': _can_edit_report(ctx, r),
        'dataset_label': dm['label'] if dm is not None else dsid,
        'preview': _preview_of(ctx, r) if with_preview else None,
    }


def _validate_saved(body: dict) -> dict[str, Any]:
    name = js_trim(js_str(coalesce(body.get('name', UNDEF), '')))
    if js_len(name) < 2:
        err(422, 'VAL_REPORT', 'Give the report a name of at least 2 characters.', 'name')
    if js_len(name) > 120:
        err(422, 'VAL_REPORT', 'Keep the name under 120 characters.', 'name')
    sp = body.get('spec', UNDEF)
    if not js_truthy(sp) or not isinstance(sp, (dict, list)):
        err(422, 'VAL_REPORT', 'The report has no definition. Pick a dataset first.', 'spec')
    sch = body.get('schedule', UNDEF)
    schedule = sch if js_str(sch) in ('none', 'weekly', 'monthly') else 'none'
    return {'name': name, 'description': u16slice(js_trim(js_str(coalesce(body.get('description', UNDEF), ''))), 0, 400), 'schedule': schedule}


# ------------------------------------------------------------------
# AI helpers
# ------------------------------------------------------------------
def planner_entities(ctx: Ctx) -> dict[str, Any]:
    users = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin'))]
    projects = my_projects(ctx)
    ds_ids = {d['id'] for d in visible_datasets(ctx)}
    deals = rows(ctx, 'deals') if 'deals' in ds_ids else []
    clients: dict[Any, None] = {}
    for x in projects:
        if x.get('client') != 'Internal':
            clients.setdefault(_g(x, 'client'), None)
    for d in deals:
        clients.setdefault(_g(d, 'client'), None)
    products: dict[Any, None] = {}
    for d in deals:
        products.setdefault(_g(d, 'product'), None)
    categories: dict[Any, None] = {}
    if 'ledger' in ds_ids:
        for e in rows(ctx, 'ledger_entries'):
            categories.setdefault(_g(e, 'category'), None)
    return {
        'people': [{'name': _g(u, 'name'), 'role': _g(u, 'role')} for u in users],
        'projects': [{'name': _g(x, 'name'), 'code': _g(x, 'code'), 'client': _g(x, 'client')} for x in projects],
        'clients': list(clients.keys()),
        'products': list(products.keys()),
        'categories': list(categories.keys()),
    }


def _plan_for(ctx: Ctx, question: Any) -> dict[str, Any]:
    q = js_trim(js_str(coalesce(question, '')))
    if js_len(q) < 3:
        err(422, 'VAL_AI', 'Ask a question of at least a few words, for example “revenue by stream this FY”.', 'question')
    if js_len(q) > 600:
        err(422, 'VAL_AI', 'Keep the question under 600 characters.', 'question')
    datasets = visible_datasets(ctx)
    if not datasets:
        err(403, 'FORBIDDEN', 'Your role has no report data. Ask an admin for access.')
    return plan_question(
        q,
        {
            'today': ctx.today,
            'fyStart': prop(S(ctx), 'company.fy_start_month'),
            'role': ctx.role,
            'personal': row_scope(ctx) == 'own',
            'datasets': datasets,
            'entities': planner_entities(ctx),
            'me': ctx.user.get('name', UNDEF),
        },
    )


def tool_project_health(ctx: Ctx) -> list[dict]:
    cm = can_see_margins(ctx)
    out = []
    for x in my_projects(ctx):
        if x['status'] not in ('active', 'initiated', 'on_hold'):
            continue
        m = redact_metrics(ctx, project_metrics(ctx, x), cm)
        nx = m.get('next_milestone')
        out.append(
            {
                'project': _g(x, 'name'),
                'client': _g(x, 'client'),
                'status': x['status'],
                'health': m.get('health', UNDEF),
                'progress_pct': js_round(m['progress'] * 100),
                'spi': None if m['spi'] is None else js_round(m['spi'] * 100) / 100,
                'effort_ratio': None if m['effort_ratio'] is None else js_round(m['effort_ratio'] * 100) / 100,
                'projected_margin_pct': None if m.get('projected_margin') is None else js_round(m['projected_margin'] * 1000) / 10,
                'overdue_tasks': m.get('overdue_tasks', UNDEF),
                'blocked_tasks': m.get('blocked_tasks', UNDEF),
                'overdue_milestones': m.get('overdue_milestones', UNDEF),
                'next_milestone': f'M{js_str(nx["no"])} {js_str(nx["name"])} due {js_str(nx["planned_end"])}' if js_truthy(nx) else None,
            }
        )
    return out


def tool_pipeline(ctx: Ctx) -> dict[str, Any]:
    if not has(ctx, 'deals.edit_own'):
        err(403, 'FORBIDDEN', 'Pipeline data is for sales and admins.')
    pl = pipeline(ctx)

    def rupees(x: Any) -> Any:
        return None if x is None else js_round(x / 100)

    return {
        'open_value_rupees': rupees(pl['open_value']),
        'weighted_value_rupees': rupees(pl['weighted_value']),
        'open_deals': pl['open_count'],
        'won_value_rupees': rupees(pl['won_value']),
        'won_deals': pl['won_count'],
        'lost_deals': pl['lost_count'],
        'win_rate_pct': None if pl['win_rate'] is None else js_round(pl['win_rate'] * 1000) / 10,
        'avg_cycle_days': None if pl['avg_cycle_days'] is None else js_round(pl['avg_cycle_days']),
        'stale_deals': [
            {
                'deal': _g(d, 'title'),
                'client': _g(d, 'client'),
                'stage': d['stage'],
                'value_rupees': rupees(d['value_paise']),
                'days_idle': diff_days(ctx.today, d['last_activity_on']),
            }
            for d in pl['stale']
        ],
    }


def tool_approvals(ctx: Ctx) -> list[dict]:
    weeks = [w for w in rows(ctx, 'timesheet_weeks') if w['status'] == 'submitted' and js_truthy(w.get('submitted_at'))]
    if has(ctx, 'people.view_all'):
        scoped = weeks
    else:
        team = team_of(ctx, ctx.user_id)
        scoped = [w for w in weeks if w.get('approver_id') == ctx.user_id or w['user_id'] in team]
    nm = name_of(ctx)
    out = []
    for w in scoped:
        due = sla_due(ctx, w['submitted_at'])
        out.append({'person': nm(w['user_id']), 'approver': nm(_g(w, 'approver_id')), 'week_start': w['week_start'], 'due_by': due, 'overdue': due < ctx.today})
    return out


def tool_entities(ctx: Ctx, kind: str) -> Any:
    e = planner_entities(ctx)
    if kind == 'people':
        return e['people']
    if kind == 'projects':
        return e['projects']
    if kind == 'clients':
        return e['clients']
    if kind == 'products':
        return e['products']
    if kind == 'ledger_categories':
        return e['categories']
    err(422, 'VAL_AI', 'kind must be people, projects, clients, products or ledger_categories.')


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
def _body_get(body: Any, k: str) -> Any:
    """body?.k"""
    return body.get(k, UNDEF) if isinstance(body, dict) else UNDEF


def list_datasets(req: Req) -> Any:
    return visible_datasets(req.ctx)


def run_report(req: Req) -> Any:
    return execute_spec(req.ctx, coalesce(_body_get(req.body, 'spec'), req.body))


def list_reports(req: Req) -> Any:
    ctx = req.ctx
    q = js_lower(js_trim(js_str(coalesce(req.q('q'), ''))))
    lst = [x for x in _visible_reports(ctx) if not q or q in js_lower(js_str(x['name']) + ' ' + js_str(x['description']))]
    lst = sorted(lst, key=lambda x: (1 if js_truthy(x.get('pinned')) else 0, locale_key(x['updated_at'])), reverse=True)
    with_preview = req.q('preview') != '0'
    return [_view(ctx, x, with_preview) for x in lst]


def get_report(req: Req) -> Any:
    ctx = req.ctx
    rep = find(ctx, 'saved_reports', req.params['id'], 'Report')
    if not any(x['id'] == rep['id'] for x in _visible_reports(ctx)):
        err(404, 'NOT_FOUND', 'Report not found.')
    return _view(ctx, rep, False)


def create_report(req: Req) -> Any:
    ctx = req.ctx
    body = req.body if isinstance(req.body, dict) else {}
    if not has(ctx, 'reports.build'):
        err(403, 'FORBIDDEN', 'Your role can’t save reports.')
    v = _validate_saved(body)
    spec_in = body['spec']
    ds = dataset_rows(ctx, p(spec_in, 'dataset'))['ds']
    spec = sanitize_spec(spec_in, ds)['spec']
    pinned = js_truthy(body.get('pinned', UNDEF))
    rep = insert(
        ctx,
        'saved_reports',
        {
            'name': v['name'],
            'description': v['description'],
            'spec': {**spec, 'title': spec['title'] if js_truthy(spec['title']) else v['name']},
            'owner_id': ctx.user_id,
            'shared': js_truthy(body.get('shared', UNDEF)),
            'pinned': pinned,
            'source': 'ai' if body.get('source', UNDEF) == 'ai' else 'builder',
            'schedule': v['schedule'],
            'last_run_at': None,
        },
    )
    log_activity(ctx, 'report', rep['id'], 'create', f'Saved report “{v["name"]}”')
    return _view(ctx, rep, False)


def patch_report(req: Req) -> Any:
    ctx = req.ctx
    body = req.body if isinstance(req.body, dict) else {}
    rep = find(ctx, 'saved_reports', req.params['id'], 'Report')
    if not _can_edit_report(ctx, rep):
        err(403, 'FORBIDDEN', 'Only the owner or an admin can change this report. Duplicate it to make your own copy.')
    pt: dict[str, Any] = {}
    b_name = body.get('name', UNDEF)
    b_spec = body.get('spec', UNDEF)
    b_sched = body.get('schedule', UNDEF)
    b_desc = body.get('description', UNDEF)
    if b_name is not UNDEF or b_spec is not UNDEF:
        v = _validate_saved(
            {
                'name': coalesce(b_name, rep['name']),
                'spec': coalesce(b_spec, rep['spec']),
                'schedule': coalesce(b_sched, rep['schedule']),
                'description': coalesce(b_desc, rep['description']),
            }
        )
        pt['name'] = v['name']
        pt['description'] = v['description']
        pt['schedule'] = v['schedule']
    if b_desc is not UNDEF:
        pt['description'] = u16slice(js_str(b_desc), 0, 400)
    if b_sched is not UNDEF:
        pt['schedule'] = b_sched if isinstance(b_sched, str) and b_sched in ('none', 'weekly', 'monthly') else 'none'
    if js_truthy(b_spec):
        ds = dataset_rows(ctx, p(b_spec, 'dataset'))['ds']
        pt['spec'] = sanitize_spec(b_spec, ds)['spec']
    if body.get('shared', UNDEF) is not UNDEF:
        pt['shared'] = js_truthy(body['shared'])
    if body.get('pinned', UNDEF) is not UNDEF:
        pt['pinned'] = js_truthy(body['pinned'])
    out = update(ctx, 'saved_reports', rep['id'], pt, req.if_match)
    log_activity(ctx, 'report', rep['id'], 'update', f'Updated report “{js_str(out["name"])}”')
    return _view(ctx, out, False)


def delete_report(req: Req) -> Any:
    ctx = req.ctx
    rep = find(ctx, 'saved_reports', req.params['id'], 'Report')
    if not _can_edit_report(ctx, rep):
        err(403, 'FORBIDDEN', 'Only the owner or an admin can delete this report.')
    if rep.get('source') == 'system' and not _curates_reports(ctx):
        err(403, 'FORBIDDEN', 'System templates can’t be deleted.')
    remove(ctx, 'saved_reports', rep['id'], req.if_match)
    log_activity(ctx, 'report', rep['id'], 'delete', f'Deleted report “{js_str(rep["name"])}”')
    return {'ok': True}


def pin_report(req: Req) -> Any:
    ctx = req.ctx
    rep = find(ctx, 'saved_reports', req.params['id'], 'Report')
    if not _can_edit_report(ctx, rep):
        err(403, 'FORBIDDEN', 'Only the owner or an admin can pin this report for everyone. Duplicate it to pin your own copy.')
    bp = _body_get(req.body, 'pinned')
    return _view(ctx, update(ctx, 'saved_reports', rep['id'], {'pinned': (not js_truthy(rep.get('pinned'))) if bp is UNDEF else js_truthy(bp)}), False)


def share_report(req: Req) -> Any:
    ctx = req.ctx
    rep = find(ctx, 'saved_reports', req.params['id'], 'Report')
    if not _can_edit_report(ctx, rep):
        err(403, 'FORBIDDEN', 'Only the owner or an admin can change sharing.')
    bs = _body_get(req.body, 'shared')
    return _view(ctx, update(ctx, 'saved_reports', rep['id'], {'shared': (not js_truthy(rep.get('shared'))) if bs is UNDEF else js_truthy(bs)}), False)


def duplicate_report(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'reports.build'):
        err(403, 'FORBIDDEN', 'Your role can’t save reports.')
    rep = find(ctx, 'saved_reports', req.params['id'], 'Report')
    if not any(x['id'] == rep['id'] for x in _visible_reports(ctx)):
        err(404, 'NOT_FOUND', 'Report not found.')
    copy = insert(
        ctx,
        'saved_reports',
        {
            'name': u16slice(f'{js_str(rep["name"])} (copy)', 0, 120),
            'description': rep['description'],
            'spec': rep['spec'],
            'owner_id': ctx.user_id,
            'shared': False,
            'pinned': False,
            'source': 'builder',
            'schedule': 'none',
            'last_run_at': None,
        },
    )
    return _view(ctx, copy, False)


def run_saved(req: Req) -> Any:
    ctx = req.ctx
    rep = find(ctx, 'saved_reports', req.params['id'], 'Report')
    if not any(x['id'] == rep['id'] for x in _visible_reports(ctx)):
        err(404, 'NOT_FOUND', 'Report not found.')
    bp = _body_get(req.body, 'period')
    spec = {**rep['spec'], 'period': bp} if js_truthy(bp) else rep['spec']
    bc = _body_get(req.body, 'compare')
    res = execute_spec(ctx, {**spec, 'compare': bc} if js_truthy(bc) else spec)
    if not ctx.view_as:
        # the TS sets rep.last_run_at in place (no version bump); the reference server writes that change
        patch('saved_reports', rep, {'last_run_at': now_iso()})
    return res


def _ai_guard(ctx: Ctx) -> None:
    if not _ai_enabled(ctx):
        err(403, 'AI_DISABLED', 'The AI assistant is turned off for this workspace. An admin can turn it on in Settings → Company.')
    if not has(ctx, 'ai.use'):
        err(403, 'FORBIDDEN', 'Your role can’t use the AI assistant.')


def ai_context(req: Req) -> Any:
    ctx = req.ctx
    return {
        'enabled': _ai_enabled(ctx),
        'today': ctx.today,
        'fy_start_month': prop(S(ctx), 'company.fy_start_month'),
        'datasets': visible_datasets(ctx),
        'entities': planner_entities(ctx),
        'suggestions': coalesce(SUGGESTIONS.get(ctx.role, UNDEF) if isinstance(ctx.role, str) else UNDEF, SUGGESTIONS['employee']),
        'me': ctx.user.get('name', UNDEF),
        'tenant': ctx.tenant.get('name', UNDEF),
        'currency': 'INR',
        'money_unit': 'paise',
    }


def ai_plan(req: Req) -> Any:
    ctx = req.ctx
    _ai_guard(ctx)
    plan = _plan_for(ctx, _body_get(req.body, 'question'))
    return {'spec': plan['spec'], 'explanation': plan['explanation'], 'confidence': plan['confidence'], 'intent': plan['intent'], 'source': 'rules'}


def ai_answer(req: Req) -> Any:
    ctx = req.ctx
    _ai_guard(ctx)
    source = 'rules'
    confidence: Any = 1
    bspec = _body_get(req.body, 'spec')
    if js_truthy(bspec):
        spec = bspec
        explanation = js_str(coalesce(_body_get(req.body, 'explanation'), ''))
        source = 'llm' if _body_get(req.body, 'source') == 'llm' else 'rules'
    else:
        plan = _plan_for(ctx, _body_get(req.body, 'question'))
        spec = plan['spec']
        explanation = plan['explanation']
        confidence = plan['confidence']
    result = execute_spec(ctx, spec)
    return {
        'spec': result['spec'],
        'explanation': explanation,
        'confidence': confidence,
        'source': source,
        'result': result,
        'narrative': narrate(result, {'fyStart': prop(S(ctx), 'company.fy_start_month')}),
        'described': describe_spec(result['spec']),
    }


def ai_tool_project_health(req: Req) -> Any:
    _ai_guard(req.ctx)
    return tool_project_health(req.ctx)


def ai_tool_pipeline(req: Req) -> Any:
    _ai_guard(req.ctx)
    return tool_pipeline(req.ctx)


def ai_tool_approvals(req: Req) -> Any:
    _ai_guard(req.ctx)
    return tool_approvals(req.ctx)


def ai_tool_entities(req: Req) -> Any:
    _ai_guard(req.ctx)
    return tool_entities(req.ctx, js_str(coalesce(req.q('kind'), '')))


def register(r: Any) -> None:
    ro = {'allow_view_as_write': True}  # POSTs that only read
    r.get('/reports/datasets', list_datasets)
    r.post('/reports/run', run_report, **ro)
    r.get('/reports', list_reports)
    r.get('/reports/:id', get_report)
    r.post('/reports', create_report)
    r.patch('/reports/:id', patch_report)
    r.delete('/reports/:id', delete_report)
    r.post('/reports/:id/pin', pin_report)
    r.post('/reports/:id/share', share_report)
    r.post('/reports/:id/duplicate', duplicate_report)
    r.post('/reports/:id/run', run_saved, **ro)
    # ---------------- AI ----------------
    r.get('/ai/context', ai_context)
    r.post('/ai/plan', ai_plan, **ro)
    r.post('/ai/answer', ai_answer, **ro)
    r.get('/ai/tools/project_health', ai_tool_project_health)
    r.get('/ai/tools/pipeline', ai_tool_pipeline)
    r.get('/ai/tools/approvals', ai_tool_approvals)
    r.get('/ai/tools/entities', ai_tool_entities)
