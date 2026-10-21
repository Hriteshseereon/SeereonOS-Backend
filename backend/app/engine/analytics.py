"""Server-side analytics — port of src/mock/analytics.ts (shared by projects, dashboards, insights, reports, costing).

Every money figure is computed here; screens only format what the API returns. Exported names are the snake_case of
the TS exports (costedEntries -> costed_entries, currentRatePH -> current_rate_ph, …). Returned dicts keep the TS
property names (`msForecast`, `taskCount`, `approvedCost`, `byStream` …) because they flow into responses.

Per-request memo: the TS `WeakMap<Ctx, Map>` is `scope().memo[('analytics', ctx)]` — one memo per Ctx object.

Writes (sweep / sync of milestones) copy on write through the unit-of-work helpers; the one in-place assignment the TS
makes WITHOUT persist() (`m.empty_since = null` in sweepMilestones) is a patch that does not raise write intent.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, calendar_for, log_activity, notify, now_iso, patch, persist, remove_where, rows, rows_where, rows_where_ro, scope, user_name
from app.domain.calendar import (
    CalendarCtx,
    _is_weekly_off_d,
    add_days,
    diff_days,
    fmt_date,
    is_working_day,
    max_iso,
    min_iso,
    month_of,
    parse_iso,
    to_iso,
    working_days_between,
    working_days_in_month,
)
from app.domain.costing import cost_per_hour, effort_ratio, margin_pct, markup_pct, project_health, rate_on
from app.domain.delivery import progress_of, stage_of
from app.domain.labels import DEAL_STAGE
from app.domain.sales import deal_value
from app.js import (
    NAN,
    UNDEF,
    coalesce,
    date_parse,
    iso_from_ms,
    js_ceil,
    js_max,
    js_min,
    js_number,
    js_round,
    js_slice,
    js_sort,
    js_str,
    js_truthy,
    locale_key,
    prop,
    to_fixed,
)

__all__ = [
    'cal',
    'week_status_map',
    'costed_entries',
    'costed_task_entries_by_project',
    'task_actuals',
    'current_rate_ph',
    'deps_of',
    'open_blockers',
    'earned_hours',
    'wd_variance',
    'milestone_state',
    'set_project_metrics_override',
    'project_metrics',
    'redact_metrics',
    'expand_ledger',
    'PAYROLL_CATEGORIES',
    'payroll_for_month',
    'revenue_between',
    'pipeline',
    'allocation_on',
    'avg_allocation',
    'sla_due',
    'live_milestone_map',
    'wbs_code',
    'sort_wbs',
    'stage_since',
    'sync_milestone_dates',
    'sweep_milestones',
    'sweep_all',
    'ensure_swept',
    'schedule_of',
    'compute_schedule',
    'effort_ratio_samples',
    'estimate_hint',
]

# ---------------------------------------------------------------------------------------------------------------------
# memo per request (WeakMap<Ctx, Map<string, unknown>>)
# ---------------------------------------------------------------------------------------------------------------------
_MEMO = 'mock/analytics'


def _memo(ctx: Ctx) -> dict:
    m = scope().memo
    k = (_MEMO, ctx)
    d = m.get(k)
    if d is None:
        d = {}
        m[k] = d
    return d


def _cached(ctx: Ctx, key: str, f: Callable[[], Any]) -> Any:
    m = _memo(ctx)
    if key not in m:
        m[key] = f()
    return m[key]


def cal(ctx: Ctx) -> CalendarCtx:
    return _cached(ctx, 'cal', lambda: calendar_for(ctx))


def week_status_map(ctx: Ctx) -> dict:
    """new Map(rows(ctx, 'timesheet_weeks').map(w => [w.id, w])) (last duplicate wins, like Map)"""
    return _cached(ctx, 'weeks', lambda: {w['id']: w for w in rows(ctx, 'timesheet_weeks')})


def _min_hours(ctx: Ctx) -> Any:
    return prop(ctx.tenant['settings'], 'calendar.min_hours_per_day')


def _wdm(ctx: Ctx, ym: str) -> int:
    """workingDaysInMonth(ym, cal(ctx)) — memoised per ctx (cal(ctx) is itself fixed for the request, so the value is)"""
    m = _memo(ctx)
    k = 'wdm:' + ym
    v = m.get(k)
    if v is None:
        v = working_days_in_month(ym, cal(ctx))
        m[k] = v
    return v


def _rates_by_user(rates: list) -> dict[Any, list]:
    """user_id -> that user's rate rows in table order (rateOn only looks at one user's rows: same result)"""
    g: dict[Any, list] = {}
    for r in rates:
        g.setdefault(r['user_id'], []).append(r)
    return g


# the tables costed entries / task actuals are derived from (cal(ctx) without a user = settings + holidays)
COSTED_TABLES = ('timesheet_entries', 'timesheet_weeks', 'cost_rates', 'tenants', 'holidays')


def _costed_reuse(ctx: Ctx) -> tuple[dict, dict] | None:
    """The most recent shared costed rows (any versions of the entries / weeks) by id() of their entry row, and that
    version's weeks by id: an entry row that is still the SAME object, whose week row is still the same object, gets
    exactly the same costed row again — when cost rates, settings and holidays are the same versions and unchanged in
    this request (entries kept across a refill after a write are the same objects; a reload makes new ones)."""
    s = scope()
    latest = s.shared_latest(('an:costed',))
    if latest is None:
        return None
    tables, ents, base = latest
    for t, e in zip(tables, ents):
        if t in ('cost_rates', 'tenants', 'holidays') and (s.loaded_entry(t) is not e or s.has_overlay(t)):
            return None
    by_id = s.cache.derived_get(s.tenant_id, ('an:costedById',), ents)
    if by_id is None:
        m = dict(zip(map(id, ents[0].rows), base))  # ents[0] = timesheet_entries (COSTED_TABLES order)
        s.file_shared(('an:costedById',), tables, ents, m, 80 * len(m))
    else:
        m = by_id[0]
    return m, ents[1].index('id')  # ents[1] = timesheet_weeks (ids are unique: first = last)


def _costed_compute(ctx: Ctx) -> list[dict]:
    weeks = week_status_map(ctx)
    by_user = _rates_by_user(rows(ctx, 'cost_rates'))
    min_h = _min_hours(ctx)
    wdm: dict[str, Any] = {}  # month -> working days (the same _wdm values, looked up once per month)
    out = []
    append = out.append
    s = scope()
    reuse = _costed_reuse(ctx)
    if reuse is not None:
        prev, weeks0 = reuse
        for e in rows(ctx, 'timesheet_entries'):
            c = prev.get(id(e))
            if c is not None and weeks.get(e['week_id']) is weeks0.get(e['week_id']):
                append(c)
                continue
            append(_costed_row(ctx, e, weeks, by_user, min_h, wdm))
        if any(s.has_overlay(t) for t in COSTED_TABLES):
            s.note_derived(('an:costed',), COSTED_TABLES, out, 900 * len(out))
        return out
    for e in rows(ctx, 'timesheet_entries'):
        w = weeks.get(e['week_id'])
        r = rate_on(by_user.get(e['user_id'], ()), e['user_id'], e['work_date'])  # type: ignore[arg-type]
        if r:
            ym = month_of(e['work_date'])
            n = wdm.get(ym)
            if n is None:
                n = wdm[ym] = _wdm(ctx, ym)
            rph = cost_per_hour(r['monthly_ctc_paise'], n, min_h)
        else:
            rph = 0
        append(
            {
                **e,
                'week_status': coalesce(w.get('status', UNDEF) if w is not None else UNDEF, 'open'),
                'approved': (w.get('status') if w is not None else UNDEF) == 'approved',
                'rate_ph': rph,
                'cost': 0 if e.get('kind') == 'leave' else rph * e['hours'],
            }
        )
    if any(s.has_overlay(t) for t in COSTED_TABLES):
        s.note_derived(('an:costed',), COSTED_TABLES, out, 900 * len(out))
    return out


def _costed_row(ctx: Ctx, e: dict, weeks: dict, by_user: dict, min_h: Any, wdm: dict) -> dict:
    """one costed row — the loop body of _costed_compute"""
    w = weeks.get(e['week_id'])
    r = rate_on(by_user.get(e['user_id'], ()), e['user_id'], e['work_date'])  # type: ignore[arg-type]
    if r:
        ym = month_of(e['work_date'])
        n = wdm.get(ym)
        if n is None:
            n = wdm[ym] = _wdm(ctx, ym)
        rph = cost_per_hour(r['monthly_ctc_paise'], n, min_h)
    else:
        rph = 0
    return {
        **e,
        'week_status': coalesce(w.get('status', UNDEF) if w is not None else UNDEF, 'open'),
        'approved': (w.get('status') if w is not None else UNDEF) == 'approved',
        'rate_ph': rph,
        'cost': 0 if e.get('kind') == 'leave' else rph * e['hours'],
    }


def costed_entries(ctx: Ctx) -> list[dict]:
    """Entries with their week status, cost per hour and cost (paise). READ-ONLY rows: shared by every request of the
    process while the tables it is derived from are unchanged (uow.Scope.shared)."""
    return _cached(ctx, 'costed', lambda: scope().shared(('an:costed',), COSTED_TABLES, lambda: _costed_compute(ctx), lambda v: 900 * len(v)))


def _task_actuals_compute(ctx: Ctx) -> dict[str, dict]:
    m: dict[str, dict] = {}
    for e in costed_entries(ctx):
        if e.get('kind') != 'task' or not js_truthy(e.get('task_id')):
            continue
        cur = m.get(e['task_id'])
        if cur is None:
            cur = {'hours': 0, 'approved': 0, 'cost': 0, 'approvedCost': 0}
        cur['hours'] += e['hours']
        cur['cost'] += e['cost']
        if e['approved']:
            cur['approved'] += e['hours']
            cur['approvedCost'] += e['cost']
        m[e['task_id']] = cur
    return m


def task_actuals(ctx: Ctx) -> dict[str, dict]:
    """task id -> {hours, approved, cost, approvedCost} (READ-ONLY; shared like costed_entries)"""

    def f() -> dict[str, dict]:
        costed = costed_entries(ctx)
        s = scope()
        if s.shared_peek(('an:costed',), COSTED_TABLES) is not costed:
            v = _task_actuals_compute(ctx)  # this request's costed list is its own (changed tables): derive from it
            if s.noted_current(('an:costed',), costed, COSTED_TABLES):  # derived from the current rows: offer it too
                s.note_derived(('an:taskActuals',), COSTED_TABLES, v, 250 * len(v))
            return v
        return s.shared(('an:taskActuals',), COSTED_TABLES, lambda: _task_actuals_compute(ctx), lambda v: 250 * len(v))

    return _cached(ctx, 'taskActuals', f)


def costed_task_entries_by_project(ctx: Ctx) -> dict[Any, list[dict]]:
    """project id -> the costed task entries (kind 'task', a task id) whose task belongs to it, in costed order:
    `costed_entries(ctx).filter(e => e.kind === 'task' && e.task_id && taskIdsOfProject.has(e.task_id))` for every
    project at once (task ids are unique). READ-ONLY lists."""
    costed = costed_entries(ctx)
    s = scope()

    def build() -> dict[Any, list[dict]]:
        tp = {t['id']: t.get('project_id', UNDEF) for t in rows(ctx, 'tasks')}
        g: dict[Any, list[dict]] = {}
        for e in costed:
            if e.get('kind') == 'task' and js_truthy(e.get('task_id')):
                p = tp.get(e['task_id'], _NO_TASK)
                if p is not _NO_TASK:
                    g.setdefault(p, []).append(e)
        return g

    def shared_or_build() -> dict[Any, list[dict]]:
        if s.shared_peek(('an:costed',), COSTED_TABLES) is not costed:
            return build()
        return s.shared(('an:costedByProject',), (*COSTED_TABLES, 'tasks'), build, lambda v: 64 * len(costed) // 10)

    return s.memo_on(('an:costedByProject', ctx, id(costed)), ('tasks',), shared_or_build)


_NO_TASK = object()


def _current_rate_ph(ctx: Ctx, user_id: Any) -> Any:
    r = rate_on(rows(ctx, 'cost_rates'), user_id, ctx.today)
    if not r:
        return 0
    return cost_per_hour(r['monthly_ctc_paise'], _wdm(ctx, month_of(ctx.today)), _min_hours(ctx))


def current_rate_ph(ctx: Ctx, user_id: Any) -> Any:
    """Current cost/hour for a person (for estimates). Memoised per request and person while cost_rates and the
    tenant row are unchanged in it (the month's working days are fixed per ctx, see _wdm)."""
    if not js_truthy(user_id):
        return 0
    if type(user_id) is not str:
        return _current_rate_ph(ctx, user_id)
    return scope().memo_on(('an:crph', ctx, user_id), ('cost_rates', 'tenants'), lambda: _current_rate_ph(ctx, user_id))


# ---------------------------------------------------------------------------------------------------------------------
# dependencies & blocking
# ---------------------------------------------------------------------------------------------------------------------
def deps_of(ctx: Ctx, task_id: Any) -> list[dict]:
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    out = []
    for d in rows(ctx, 'task_dependencies'):
        if d['task_id'] == task_id:
            t = tasks.get(d['depends_on_task_id'])
            if t is not None:
                out.append(t)
    return out


def open_blockers(ctx: Ctx, task_id: Any) -> list[dict]:
    return [t for t in deps_of(ctx, task_id) if t.get('status') != 'done']


# ---------------------------------------------------------------------------------------------------------------------
# milestones
# ---------------------------------------------------------------------------------------------------------------------
def earned_hours(t: dict) -> Any:
    """Earned hours of a task = estimate × progress % (Feature Guide KPI "% complete")."""
    return (t['est_hours'] * progress_of(t)) / 100


def wd_variance(ctx: Ctx, frm: str, to: str) -> int:
    """Signed working days from `frm` to `to` (positive when `to` is later). Baseline variance / slip."""
    if frm == to:
        return 0
    c = cal(ctx).without_leave()
    return working_days_between(add_days(frm, 1), to, c) if to > frm else -working_days_between(add_days(to, 1), frm, c)


def milestone_state(ctx: Ctx, ms: dict, tasks: list[dict], forecast_end: Any = UNDEF) -> dict[str, Any]:
    """Milestone delivery state (see the TS doc). `forecast_end` defaults (UNDEF) to the project schedule's forecast."""
    mine = [t for t in tasks if t.get('milestone_id') == ms['id']]
    open_ = [t for t in mine if t.get('status') != 'done']
    est: Any = 0
    for t in mine:
        est = est + t['est_hours']
    done_est: Any = 0
    done_n = 0
    for t in mine:
        if t.get('status') == 'done':
            done_est = done_est + t['est_hours']
            done_n += 1
    earned: Any = 0
    for t in mine:
        earned = earned + earned_hours(t)
    if js_truthy(est):
        progress: Any = earned / est
    elif mine:
        progress = done_n / len(mine)
    else:
        progress = 1 if js_truthy(ms.get('achieved_on')) else 0
    slip = wd_variance(ctx, ms['baseline_end'], ms['planned_end'])
    if forecast_end is not UNDEF:
        forecast = forecast_end
    elif not js_truthy(ms.get('achieved_on')) and open_:
        forecast = coalesce(schedule_of(ctx, ms['project_id'])['msForecast'].get(ms['id'], UNDEF), None)
    else:
        forecast = None
    if js_truthy(ms.get('achieved_on')):
        state = 'achieved_on_time' if ms['achieved_on'] <= ms['planned_end'] else 'achieved_late'
    elif not open_ and mine:
        end = '0000-01-01'
        for t in mine:
            end = max_iso(end, coalesce(prop(t, 'done_on'), t['due_on']))
        state = 'achieved_on_time' if end <= ms['planned_end'] else 'achieved_late'
    elif ms['planned_end'] < ctx.today:
        state = 'overdue'
    elif js_truthy(forecast) and forecast > ms['planned_end']:
        state = 'at_risk'
    elif ms['planned_start'] > ctx.today and not any(progress_of(t) > 0 for t in mine):
        state = 'not_started'
    else:
        state = 'on_track'
    return {
        'state': state,
        'slip': slip,
        'progress': progress,
        'taskCount': len(mine),
        'openCount': len(open_),
        'est': est,
        'doneEst': done_est,
        'earned': earned,
        'forecast': forecast,
    }


# ---------------------------------------------------------------------------------------------------------------------
# project financials (BR-23..26)
# ---------------------------------------------------------------------------------------------------------------------
MetricsOverride = Callable[[Ctx, dict, dict], 'dict | None']
_metrics_override: MetricsOverride | None = None


def set_project_metrics_override(f: MetricsOverride | None) -> None:
    """v5: the costing engine (app/engine/costing.py) supplies planned / projected / actual cost and margins."""
    global _metrics_override
    _metrics_override = f


def get_project_metrics_override() -> MetricsOverride | None:
    """(Python only) the installed override — tests save/restore it"""
    return _metrics_override


# the inputs of project_metrics (with the costing override) — a superset; development runs check every read
METRICS_TABLES = (
    'projects',
    'tasks',
    'milestones',
    'invoices',
    'allocations',
    'change_requests',
    'task_dependencies',
    'payments',
    'ledger_entries',
    'users',
    *COSTED_TABLES,
)


def project_metrics(ctx: Ctx, p: dict) -> dict[str, Any]:
    ensure_swept(ctx)

    def g() -> dict[str, Any]:
        base = _project_metrics_base(ctx, p)
        o = _metrics_override(ctx, p, base) if _metrics_override is not None else None
        return {**base, **o} if js_truthy(o) else base

    def f() -> dict[str, Any]:
        pid = p['id']
        # shared across requests only for the stored row itself (a caller may pass an edited copy)
        s = scope()
        in_override = bool(s.memo.get(('mock/costing', 'inOverride')))  # nested in a costing override: not the plain value
        if type(pid) is str and not in_override and s.find_by('projects', 'id', pid) is p:
            return s.shared(('an:pm', pid, ctx.today), METRICS_TABLES, g)
        return g()

    return _cached(ctx, 'pm:' + js_str(p['id']), f)


def _project_metrics_base(ctx: Ctx, p: dict) -> dict[str, Any]:
    today = ctx.today
    pid = p['id']
    tasks = rows_where(ctx, 'tasks', 'project_id', pid)
    ms = [m for m in rows_where_ro(ctx, 'milestones', 'project_id', pid) if not js_truthy(m.get('deleted_at'))]
    act = task_actuals(ctx)
    invoices = rows_where(ctx, 'invoices', 'project_id', pid)
    zero = {'hours': 0, 'approved': 0, 'cost': 0, 'approvedCost': 0}
    done_est_logged: Any = 0
    est: Any = 0
    actual: Any = 0
    approved: Any = 0
    remaining: Any = 0
    planned_cost: Any = 0
    actual_cost: Any = 0
    logged_cost: Any = 0
    remaining_cost: Any = 0
    done_est: Any = 0
    done_actual: Any = 0
    planned: Any = 0
    for t in tasks:
        a = act.get(t['id'], zero)
        rate = current_rate_ph(ctx, prop(t, 'owner_id'))
        est += t['est_hours']
        actual += a['hours']
        approved += a['approved']
        actual_cost += a['approvedCost']
        logged_cost += a['cost']
        planned_cost += t['est_hours'] * rate
        rem = 0 if t['status'] == 'done' else js_max(0, t['est_hours'] - a['hours'])
        remaining += rem
        remaining_cost += rem * rate
        if t['status'] == 'done':
            done_est += t['est_hours']
            if a['hours'] > 0:
                done_actual += a['hours']
                done_est_logged += t['est_hours']
        # planned value by today (linear over task window)
        if t['due_on'] <= today:
            planned += t['est_hours']
        elif t['start_on'] < today:
            planned += (t['est_hours'] * js_max(0, diff_days(today, t['start_on']))) / js_max(1, diff_days(t['due_on'], t['start_on']))
    contract = p['contract_value_paise']
    invoiced: Any = 0
    collected: Any = 0
    for i in invoices:
        invoiced += i['amount_paise']
    for i in invoices:
        collected += i['paid_amount_paise']
    projected = logged_cost + remaining_cost
    earned: Any = 0
    for t in tasks:
        earned += earned_hours(t)
    states = [milestone_state(ctx, m, tasks) for m in ms]
    overdue_ms = len([s for s in states if s['state'] == 'overdue'])
    pm = margin_pct(contract, projected)
    open_est_to_date: Any = 0
    open_actual: Any = 0
    for t in tasks:
        if t['status'] != 'done' and t['start_on'] <= today:
            open_est_to_date += t['est_hours'] * js_min(1, js_max(1, diff_days(today, t['start_on'])) / js_max(1, diff_days(t['due_on'], t['start_on'])))
    for t in tasks:
        if t['status'] != 'done':
            a2 = act.get(t['id'])
            open_actual += a2['hours'] if a2 is not None else 0
    er = effort_ratio(done_actual, done_est_logged) if js_truthy(done_actual) else effort_ratio(open_actual, open_est_to_date)
    st = ctx.tenant['settings']
    if p['status'] in ('completed', 'closed'):
        health = 'on_track'
    else:
        health = project_health(pm if js_truthy(contract) else None, er, st['projects.margin_floor_pct'] / 100, st['projects.effort_ratio_alert'], overdue_ms)
    spi_val = js_min(2, earned / planned) if planned > 0 else None
    health_adj = 'at_risk' if health == 'on_track' and spi_val is not None and spi_val < 0.8 and p['status'] == 'active' else health
    cands = [m for m in ms if not js_truthy(m.get('achieved_on')) and m.get('billing_status') == 'none']
    next_ms = sorted(cands, key=lambda m: locale_key(m['planned_end']))[0] if cands else None
    uninvoiced: Any = 0
    for m in ms:
        if m.get('billing_status') == 'none':
            uninvoiced += m['billing_amount_paise']
    team = list(dict.fromkeys(t['owner_id'] for t in tasks if js_truthy(t.get('owner_id'))))
    return {
        'project_id': pid,
        'est_hours': est,
        'actual_hours': actual,
        'approved_hours': approved,
        'remaining_hours': remaining,
        'progress': earned / est if js_truthy(est) else 0,
        'planned_cost': js_round(planned_cost),
        'actual_cost': js_round(actual_cost),
        'logged_cost': js_round(logged_cost),
        'projected_cost': js_round(projected),
        'contract_value': contract,
        'invoiced': invoiced,
        'collected': collected,
        'outstanding': invoiced - collected,
        'uninvoiced': uninvoiced,
        'planned_margin': margin_pct(contract, planned_cost),
        'projected_margin': pm if js_truthy(contract) else None,
        'actual_margin': margin_pct(invoiced, actual_cost),
        'planned_markup': markup_pct(contract, planned_cost),
        'projected_markup': markup_pct(contract, projected),
        'actual_markup': markup_pct(invoiced, actual_cost),
        'effort_ratio': er,
        'spi': js_min(2, earned / planned) if planned > 0 else None,
        'health': health_adj,
        'overdue_milestones': overdue_ms,
        'open_tasks': len([t for t in tasks if t['status'] != 'done']),
        'overdue_tasks': len([t for t in tasks if t['status'] != 'done' and t['due_on'] < today]),
        'blocked_tasks': len([t for t in tasks if t['status'] == 'blocked']),
        'team': team,
        'next_milestone': {
            'id': next_ms['id'],
            'name': next_ms['name'],
            'no': next_ms['no'],
            'planned_end': next_ms['planned_end'],
            'billing_amount_paise': next_ms['billing_amount_paise'],
        }
        if next_ms is not None
        else None,
    }


_COST_KEYS = (
    'planned_cost',
    'actual_cost',
    'logged_cost',
    'projected_cost',
    'planned_margin',
    'projected_margin',
    'actual_margin',
    'planned_markup',
    'projected_markup',
    'actual_markup',
)
_FINANCE_KEYS = ('invoiced', 'collected', 'outstanding', 'uninvoiced')


def redact_metrics(ctx: Ctx, m: dict, can_margins: bool) -> dict:
    """Remove money fields the viewer may not see."""
    if can_margins:
        return m
    x = dict(m)
    for k in _COST_KEYS:
        x[k] = None
    if 'finance.view' not in ctx.perms:
        for k in _FINANCE_KEYS:
            x[k] = None
        if 'projects.edit' not in ctx.perms:
            x['contract_value'] = None
    return x


# ---------------------------------------------------------------------------------------------------------------------
# revenue & finance
# ---------------------------------------------------------------------------------------------------------------------
def _next_month_js(d: str) -> str:
    """const dt = new Date(d); dt.setMonth(dt.getMonth() + 1); dt.toISOString().slice(0, 10) — local-time month step
    (process TZ) with JS day overflow (31 Jan -> 3 Mar)."""
    ms = date_parse(d)
    if ms != ms:
        raise ValueError('RangeError: Invalid time value')
    sec, rem_ms = divmod(int(ms), 1000)
    lt = datetime.fromtimestamp(sec)  # local wall clock
    y, mo = lt.year, lt.month  # month 1..12
    mo0 = mo  # setMonth(getMonth() + 1): month index + 1
    y += mo0 // 12
    mo0 = mo0 % 12
    wall = datetime(y, mo0 + 1, 1, lt.hour, lt.minute, lt.second) + timedelta(days=lt.day - 1)
    new_ms = int(time.mktime(wall.timetuple())) * 1000 + rem_ms
    return iso_from_ms(new_ms)[:10]


def expand_ledger(ctx: Ctx, frm: str, to: str) -> list[dict]:
    """recurring entries expand monthly until until_on (or `to`)"""
    out: list[dict] = []
    today = ctx.today
    for e in rows(ctx, 'ledger_entries'):
        if not js_truthy(e.get('recurring')):
            if frm <= e['date'] <= to:
                out.append({**e, 'occurrence': e['date']})
            continue
        d = e['date']
        until = e.get('until_on')
        end = until if js_truthy(until) and until < to else to
        guard = 0
        while d <= end:
            g = guard
            guard += 1
            if not g < 240:
                break
            if d >= frm:
                out.append({**e, 'occurrence': d, 'status': ('received' if e['kind'] == 'income' else 'paid') if d <= today else 'expected'})
            d = _next_month_js(d)
    return out


PAYROLL_CATEGORIES: frozenset = cat('mock/analytics')['PAYROLL_CATEGORIES']


def payroll_for_month(ctx: Ctx, ym: str) -> dict[str, Any]:
    """Payroll cost of one month — the ONE salary basis every screen uses (see the TS doc)."""
    first = f'{ym}-01'
    last = add_days(month_of(add_days(first, 32)) + '-01', -1)
    rates = rows(ctx, 'cost_rates')
    by_user = _rates_by_user(rates)
    headcount = 0
    ctc: Any = 0
    for u in rows(ctx, 'users'):
        if u.get('status') == 'deactivated' or js_truthy(u.get('is_super_admin')) or not js_truthy(u.get('joined_on')) or u['joined_on'] > last:
            continue
        mine = by_user.get(u['id'], [])
        r = rate_on(mine, u['id'], ctx.today)
        if r is None:
            lst = sorted(mine, key=lambda x: locale_key(x['effective_from']), reverse=True)
            r = lst[0] if lst else None
        if not r:
            continue
        ctc += r['monthly_ctc_paise']
        headcount += 1
    booked = [e for e in expand_ledger(ctx, first, last) if e['kind'] == 'expense' and e.get('category') in PAYROLL_CATEGORIES]
    if booked:
        total: Any = 0
        for e in booked:
            total += e['amount_paise']
        return {'payroll': total, 'headcount': headcount, 'source': 'ledger'}
    return {'payroll': ctc, 'headcount': headcount, 'source': 'ctc'}


def revenue_between(ctx: Ctx, frm: str, to: str, opts: dict | None = None) -> dict[str, Any]:
    """Billed, collected and costs between two dates (see the TS doc). opts = {'scheduled': bool}"""
    o = opts or {}
    cap = to if (js_truthy(o.get('scheduled')) or to < ctx.today) else ctx.today
    entries = [] if frm > cap else expand_ledger(ctx, frm, cap)
    payments = rows(ctx, 'payments')
    invoices = {i['id']: i for i in rows(ctx, 'invoices')}
    billed: Any = 0
    collected: Any = 0
    other: Any = 0
    payroll: Any = 0
    by_stream: dict[str, Any] = {}
    for e in entries:
        if e['kind'] == 'income':
            billed += e['amount_paise']
            if js_truthy(e.get('stream')):
                by_stream[e['stream']] = coalesce(by_stream.get(e['stream'], UNDEF), 0) + e['amount_paise']
            if not js_truthy(e.get('invoice_id')) and e.get('status') == 'received':
                collected += e['amount_paise']
        elif e.get('category') not in PAYROLL_CATEGORIES:
            other += e['amount_paise']
    for p in payments:
        if frm <= p['paid_on'] <= cap and p['invoice_id'] in invoices:
            collected += p['amount_paise']
    m = month_of(frm)
    while frm <= cap and m <= month_of(cap):
        first = f'{m}-01'
        if frm <= first <= cap:
            payroll += payroll_for_month(ctx, m)['payroll']
        m = month_of(add_days(m + '-28', 7))
    cost = payroll + other
    return {
        'billed': billed,
        'collected': collected,
        'cost': cost,
        'payroll': payroll,
        'other_costs': other,
        'net': billed - cost,
        'margin': (billed - cost) / billed if js_truthy(billed) else None,
        'byStream': by_stream,
        'as_of': cap,
    }


# ---------------------------------------------------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------------------------------------------------
def pipeline(ctx: Ctx, owner_id: Any = UNDEF) -> dict[str, Any]:
    deals = [d for d in rows(ctx, 'deals') if not js_truthy(owner_id) or d.get('owner_id') == owner_id]
    open_ = [d for d in deals if d['stage'] != 'won' and d['stage'] != 'lost']
    won = [d for d in deals if d['stage'] == 'won']
    lost = [d for d in deals if d['stage'] == 'lost']
    stale_days = prop(ctx.tenant['settings'], 'sales.stale_deal_days')
    cycle = [x for x in (diff_days(d['won_on'], d['created_at'][:10]) for d in won) if x >= 0]

    def total(lst: list) -> Any:
        s: Any = 0
        for d in lst:
            s += deal_value(d)
        return s

    weighted: Any = 0
    for d in open_:
        weighted += deal_value(d) * DEAL_STAGE[d['stage']]['chance']
    csum: Any = 0
    for x in cycle:
        csum += x
    return {
        'open_value': total(open_),
        'weighted_value': js_round(weighted),
        'open_count': len(open_),
        'won_value': total(won),
        'won_count': len(won),
        'lost_count': len(lost),
        'win_rate': len(won) / (len(won) + len(lost)) if len(won) + len(lost) else None,
        'avg_deal': total(won) / len(won) if won else None,
        'avg_cycle_days': csum / len(cycle) if cycle else None,
        'stale': [d for d in open_ if diff_days(ctx.today, d['last_activity_on']) >= stale_days],
    }


# ---------------------------------------------------------------------------------------------------------------------
# people load
# ---------------------------------------------------------------------------------------------------------------------
def _allocations_of(ctx: Ctx, user_id: Any) -> list[dict]:
    """rows(ctx, 'allocations').filter(a => a.user_id === userId) via the per-request group index (table order)"""
    tid = ctx.tenant_id
    return [a for a in scope().group('allocations', 'user_id').get(user_id, ()) if a.get('tenant_id') == tid]


def _allocation_on(allocs: list[dict], date: str) -> Any:
    s: Any = 0
    for a in allocs:
        if a['from_on'] <= date and a['to_on'] >= date:
            s += a['pct']
    return s


def allocation_on(ctx: Ctx, user_id: Any, date: str) -> Any:
    return _allocation_on(_allocations_of(ctx, user_id), date)


def _day_walk(frm: str, to: str, c: CalendarCtx) -> Iterator[tuple[str, bool]]:
    """(d, isWorkingDay(d, c)) for `for (let d = frm; d <= to; d = addDays(d, 1))`, stepping a date object instead of
    re-parsing every string: addDays(s, 1) of a parseable s is to_iso(parse_iso(s) + 1 day), so the sequence and the
    flags are the same (an unparseable start falls back to the literal loop)."""
    if not frm <= to:
        return
    dt = parse_iso(frm)
    if dt is None:
        d = frm
        while d <= to:
            yield d, is_working_day(d, c)
            d = add_days(d, 1)
        return
    yield frm, is_working_day(frm, c)
    hs = c.holiday_set()
    leave = c.leave_dates
    one = timedelta(days=1)
    while True:
        dt = dt + one
        s = to_iso(dt)
        if not s <= to:
            return
        yield s, not _is_weekly_off_d(dt, c) and s not in hs and not (leave is not None and s in leave)


def avg_allocation(ctx: Ctx, user_id: Any, frm: str, to: str) -> Any:
    c = cal(ctx)
    allocs = _allocations_of(ctx, user_id)
    n = 0
    total: Any = 0
    for d, working in _day_walk(frm, to, c):
        if working:
            n += 1
            total += _allocation_on(allocs, d)
    return total / n if n else 0


def sla_due(ctx: Ctx, submitted_at: str) -> str:
    """Approval SLA due date for a submitted week (BR-10). A function of the submission DATE, cal(ctx) (fixed per
    ctx) and the tenant's setting: memoised per request by date while the tenant row is unchanged."""
    d0 = submitted_at[:10]
    tv = scope().view_token('tenants')
    m = _memo(ctx)
    memo = m.get('sla')
    if memo is None or memo[0] is not tv:
        memo = m['sla'] = (tv, {})
    v = memo[1].get(d0)
    if v is None:
        v = memo[1][d0] = _sla_due(ctx, submitted_at)
    return v


def _sla_due(ctx: Ctx, submitted_at: str) -> str:
    c = cal(ctx)
    d = submitted_at[:10]
    left = prop(ctx.tenant['settings'], 'approval.sla_working_days')
    while left > 0:
        d = add_days(d, 1)
        if is_working_day(d, c):
            left -= 1
    return d


# ---------------------------------------------------------------------------------------------------------------------
# WBS helpers, milestone upkeep and the schedule (Feature Guide §4–5)
# ---------------------------------------------------------------------------------------------------------------------
def live_milestone_map(ctx: Ctx) -> dict[str, dict]:
    return {m['id']: m for m in rows(ctx, 'milestones') if not js_truthy(m.get('deleted_at'))}


def wbs_code(t: dict, ms: dict) -> str:
    """Permanent number stored on the task (wbs_code) else `<milestone no>.<task no>`."""
    frozen = t.get('wbs_code')
    if js_truthy(frozen):
        return frozen
    mid = t.get('milestone_id')
    m = ms.get(mid) if js_truthy(mid) else None
    return f'{js_str(m["no"]) if m is not None and not js_truthy(m.get("deleted_at")) else "0"}.{js_str(prop(t, "no"))}'


def sort_wbs(a: str, b: str) -> Any:
    pa = [js_number(x) for x in a.split('.')]
    pb = [js_number(x) for x in b.split('.')]
    a1 = pa[0]
    a2 = pa[1] if len(pa) > 1 else NAN
    b1 = pb[0]
    b2 = pb[1] if len(pb) > 1 else NAN
    x = (a1 if js_truthy(a1) else 1e6) - (b1 if js_truthy(b1) else 1e6)
    return x if js_truthy(x) else a2 - b2


def stage_since(ctx: Ctx, t: dict) -> str:
    """Day a task entered its current stage (from the task log), else its last update."""
    at = ''
    tid = ctx.tenant_id
    for a in scope().group('activity_log', 'entity_id').get(t['id'], ()):
        if a.get('tenant_id') == tid and a.get('entity') == 'task' and a.get('action') in ('task.stage', 'task.status') and a['at'] > at:
            at = a['at']
    return (at if at else t['updated_at'])[:10]


def _patch_quiet(table: str, row: dict, changes: dict) -> dict:
    """`row.x = v` WITHOUT persist(): the change rides along if something else persists (no write intent of its own)"""
    s = scope()
    wi = s.write_intent
    new = patch(table, row, changes)
    s.write_intent = wi
    return new


def sync_milestone_dates(ctx: Ctx, ms_id: Any, rebaseline: bool = False) -> None:
    """A milestone's deadline is always the due_on of its last task; its start is the earliest task start."""
    if not js_truthy(ms_id):
        return
    m = next((x for x in rows_where_ro(ctx, 'milestones', 'id', ms_id)), None)
    if m is None or js_truthy(m.get('deleted_at')):
        return
    mine = rows_where(ctx, 'tasks', 'milestone_id', m['id'])
    if not mine:
        return
    end = '0000-01-01'
    for t in mine:
        end = max_iso(end, t['due_on'])
    start = '9999-12-31'
    for t in mine:
        start = min_iso(start, t['start_on'])
    changes: dict[str, Any] = {}
    if rebaseline and m['baseline_end'] != end:
        changes['baseline_end'] = end
    if m['planned_end'] != end or m['planned_start'] != start:
        changes['planned_end'] = end
        changes['planned_start'] = start
    if changes:
        changes['updated_at'] = now_iso()
        changes['version'] = m['version'] + 1
        patch('milestones', m, changes)
        persist()


def sweep_milestones(ctx: Ctx, p: dict) -> None:
    """BR-20 sweep: deadlines follow tasks; a milestone empty for N days is removed unless invoiced, billed or allocated."""
    days = prop(ctx.tenant['settings'], 'projects.empty_milestone_days')
    tasks = rows_where(ctx, 'tasks', 'project_id', p['id'])
    count: dict[Any, int] = {}
    for t in tasks:
        k = t.get('milestone_id')
        count[k] = count.get(k, 0) + 1
    # v8: a project with no tasks at all is still being planned (its milestones ARE the plan, the WBS comes next): its
    # milestones are never cleaned up, and the 7-day clock starts only once the project has a WBS
    planning = len(tasks) == 0
    # v8 QA: a milestone that carries a billing amount or has people allocated to it is part of the commercial and
    # resource plan (e.g. converted from an estimate) — rolling-wave planning adds its tasks later, so it is never
    # cleaned up (that deleted billing milestones and their allocations 7 days after the first phase got its WBS)
    allocated = {a.get('milestone_id') for a in rows_where(ctx, 'allocations', 'project_id', p['id'])}
    for m in [x for x in rows_where_ro(ctx, 'milestones', 'project_id', p['id']) if not js_truthy(x.get('deleted_at'))]:
        n = count.get(m['id'], 0)
        if n > 0:
            if js_truthy(m.get('empty_since')):
                m = _patch_quiet('milestones', m, {'empty_since': None})
            sync_milestone_dates(ctx, m['id'])
        elif planning or (m.get('billing_amount_paise') or 0) > 0 or m['id'] in allocated:
            if js_truthy(m.get('empty_since')):
                m = _patch_quiet('milestones', m, {'empty_since': None})
        elif m.get('billing_status') == 'none':
            if not js_truthy(m.get('empty_since')):
                m = patch('milestones', m, {'empty_since': ctx.today})
                persist()
            elif days > 0 and diff_days(ctx.today, m['empty_since']) >= days:
                mid = m['id']
                m = patch('milestones', m, {'deleted_at': now_iso()})
                remove_where(ctx, 'allocations', lambda a, mid=mid: a.get('milestone_id') == mid)
                log_activity(
                    ctx,
                    'project',
                    p['id'],
                    'milestone.auto_deleted',
                    f'M{js_str(m["no"])} {js_str(m["name"])} removed automatically after {js_str(days)} days with no tasks',
                )
                if js_truthy(p.get('pm_id')):
                    notify(
                        ctx,
                        p['pm_id'],
                        'NTF-15',
                        f'Milestone M{js_str(m["no"])} removed',
                        f'“{js_str(m["name"])}” on {js_str(p["name"])} had no tasks for {js_str(days)} days and was removed.',
                        f'/projects/{js_str(p["id"])}/wbs',
                    )
                persist()


def sweep_all(ctx: Ctx) -> None:
    """Sweep every project of the tenant (list pages, dashboards)."""
    for p in list(rows(ctx, 'projects')):
        sweep_milestones(ctx, p)


def ensure_swept(ctx: Ctx) -> None:
    """Run the sweep once per request before anything reads milestones."""

    def f() -> bool:
        sweep_all(ctx)
        return True

    _cached(ctx, 'swept', f)


# ---------------------------------------------------------------------------------------------------------------------
# schedule: forecast + critical path
# ---------------------------------------------------------------------------------------------------------------------
def schedule_of(ctx: Ctx, project_id: Any) -> dict[str, Any]:
    """Cached schedule per project for this request, plus each milestone's forecast end:
    {'schedule': Schedule | None, 'msForecast': {milestone id: date | None}}"""

    def g(p: dict) -> dict[str, Any]:
        schedule = compute_schedule(ctx, p)
        return {'schedule': schedule, 'msForecast': {m['id']: coalesce(m['achieved_on'], m['forecast_end']) for m in schedule['milestones']}}

    def f() -> dict[str, Any]:
        p = next((x for x in rows_where_ro(ctx, 'projects', 'id', project_id)), None)
        if p is None:
            return {'schedule': None, 'msForecast': {}}
        if type(project_id) is not str:
            return g(p)
        # shared across requests while every input table is unchanged (READ-ONLY value)
        return scope().shared(('an:sched', project_id, ctx.today), SCHEDULE_TABLES, lambda: g(p))

    return _cached(ctx, 'sched:' + js_str(project_id), f)


SCHEDULE_TABLES = ('projects', 'tasks', 'milestones', 'task_dependencies', 'users', *COSTED_TABLES)


def compute_schedule(ctx: Ctx, p: dict) -> dict[str, Any]:
    """Prototype §6.4/§7/§8: CPM float, critical path, dependency-adjusted forecast at productive capacity."""
    today = ctx.today
    c = cal(ctx).without_leave()
    ms = live_milestone_map(ctx)
    tasks = rows_where(ctx, 'tasks', 'project_id', p['id'])
    milestones = sorted((m for m in rows_where_ro(ctx, 'milestones', 'project_id', p['id']) if not js_truthy(m.get('deleted_at'))), key=lambda m: m['no'])
    by_id = {t['id']: t for t in tasks}
    deps: dict[str, list[str]] = {}
    succ: dict[str, list[str]] = {}
    for d in rows(ctx, 'task_dependencies'):
        if d['task_id'] not in by_id or d['depends_on_task_id'] not in by_id:
            continue
        deps.setdefault(d['task_id'], []).append(d['depends_on_task_id'])
        succ.setdefault(d['depends_on_task_id'], []).append(d['task_id'])
    lo = min_iso(p['start_on'], today)
    hi = max_iso(p['end_on'], today)
    for t in tasks:
        lo = min_iso(lo, min_iso(t['start_on'], t['due_on']))
        hi = max_iso(hi, max_iso(t['start_on'], t['due_on']))
    for m in milestones:
        hi = max_iso(hi, m['planned_end'])
    lo = add_days(lo, -14)
    hi = add_days(hi, 400)
    wdays: list[str] = []
    next_idx: dict[str, int] = {}
    for d, working in _day_walk(lo, hi, c):
        if working:
            wdays.append(d)
        next_idx[d] = len(wdays)
    nw = len(wdays)

    def w_next(x: str) -> int:
        v = next_idx.get(x)
        if v is None:
            v = 0 if x < lo else nw - 1
        return min(v, nw - 1)

    def w_prev(x: str) -> int:
        i = next_idx.get(x)
        if i is None:
            i = 0
        return i if (0 <= i < nw and wdays[i] == x) else max(0, i - 1)

    def date_at(i: int) -> Any:
        j = max(0, min(nw - 1, i))
        return wdays[j] if 0 <= j < nw else UNDEF

    ti = w_next(today)

    order: list[str] = []
    state: dict[str, int] = {}
    cycles: dict[str, None] = {}

    def visit(id_: str, path: list[str]) -> None:
        s = state.get(id_)
        if s == 2:
            return
        if s == 1:
            i = path.index(id_) if id_ in path else -1
            for x in js_slice(path, i):
                cycles[x] = None
            return
        state[id_] = 1
        for dd in deps.get(id_, []):
            visit(dd, [*path, id_])
        state[id_] = 2
        order.append(id_)

    for t in sorted(tasks, key=lambda t: locale_key(t['start_on'])):
        visit(t['id'], [])

    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for t in tasks:
        a = w_next(t['start_on'])
        b = w_prev(t['due_on'])
        es[t['id']] = min(a, b)
        ef[t['id']] = max(a, b)
    conflicts: list[dict] = []
    for t in tasks:
        for dd in deps.get(t['id'], []):
            dt = by_id[dd]
            if es[t['id']] <= ef[dd] and dt['status'] != 'done' and t['status'] != 'done':
                conflicts.append(
                    {
                        'task_id': t['id'],
                        'depends_on_id': dd,
                        'message': f'{wbs_code(t, ms)} {js_str(prop(t, "title"))} is planned to start {fmt_date(t["start_on"])}, but it depends on '
                        f'{wbs_code(dt, ms)} {js_str(prop(dt, "title"))}, which ends {fmt_date(dt["due_on"])}. Move its dates or remove the dependency.',
                    }
                )
    # Float to the milestone deadline (working days), per milestone group
    float_of: dict[str, int] = {}
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        mid = t.get('milestone_id')
        g = mid if js_truthy(mid) and mid in ms else '_none'
        groups.setdefault(g, []).append(t)
    rev_order = order[::-1]
    for g in groups.values():
        end = max(ef[t['id']] for t in g)
        in_g = {t['id'] for t in g}
        ls: dict[str, int] = {}
        for id_ in rev_order:
            if id_ not in in_g:
                continue
            dur = ef[id_] - es[id_] + 1
            lf = end
            for s in succ.get(id_, []):
                if s in in_g and s in ls:
                    lf = min(lf, ls[s] - 1)
            ls[id_] = lf - dur + 1
            float_of[id_] = lf - ef[id_]
    finish_idx = max(ef[t['id']] for t in tasks) if tasks else None
    # Float to the project end (guide §4: "spare days before it delays the project")
    proj_float: dict[str, int] = {}
    if finish_idx is not None:
        lsp: dict[str, int] = {}
        for id_ in rev_order:
            if id_ not in ef:
                continue
            dur = ef[id_] - es[id_] + 1
            lf = finish_idx
            for s in succ.get(id_, []):
                if s in lsp:
                    lf = min(lf, lsp[s] - 1)
            lsp[id_] = lf - dur + 1
            proj_float[id_] = lf - ef[id_]
    path: list[str] = []
    if finish_idx is not None:
        cands = sorted((t for t in tasks if ef[t['id']] == finish_idx), key=lambda t: -len(deps.get(t['id'], ())))
        cur: Any = cands[0]['id'] if cands else UNDEF
        guard = 0
        while js_truthy(cur):
            g0 = guard
            guard += 1
            if not (g0 < 500) or cur in path:
                break
            path.insert(0, cur)
            ds = [x for x in deps.get(cur, []) if x in by_id]
            if not ds:
                break
            cur = sorted(ds, key=lambda x: -ef[x])[0]

    # Forecast
    act = task_actuals(ctx)
    cap = js_max(1, _min_hours(ctx) * 0.75)
    fs: dict[str, int] = {}
    ff: dict[str, int] = {}
    rem_m: dict[str, Any] = {}
    wait: dict[str, str] = {}
    for id_ in order:
        t = by_id[id_]
        if t['status'] == 'done':
            f = w_prev(coalesce(prop(t, 'done_on'), t['due_on']))
            fs[id_] = min(w_next(t['actual_start_on']) if js_truthy(t.get('actual_start_on')) else es[id_], f)
            ff[id_] = f
            rem_m[id_] = 0
            continue
        a = act.get(id_)
        logged = a['hours'] if a is not None else 0
        prog = progress_of(t) / 100
        by_progress = t['est_hours'] * (1 - prog)
        rem = js_max(0.5, by_progress if prog > 0 else js_max(t['est_hours'] - logged, t['est_hours'] * 0.15))
        rem_m[id_] = js_round(rem * 10) / 10
        started = js_truthy(t.get('actual_start_on')) or t['status'] == 'in_progress' or t['status'] == 'in_review'
        s = max(ti, es[id_])
        if started:
            s = ti
        pm = -1
        for dd in deps.get(id_, []):
            dt = by_id[dd]
            if dt['status'] == 'done':
                f = min(ff.get(dd, 0), ti - 1)
            else:
                f = ff[dd] if dd in ff else ef[dd]
            if f > pm:
                pm = f
                if f + 1 > s:
                    wait[id_] = dd
        if pm + 1 > s:
            s = pm + 1
        rem_days = js_max(1, js_ceil(rem / cap))
        fs[id_] = min(w_next(t['actual_start_on']), s) if started and js_truthy(t.get('actual_start_on')) else s
        ff[id_] = s + rem_days - 1

    sched_tasks: list[dict] = []
    for t in tasks:
        mid = t.get('milestone_id')
        m = ms.get(mid) if js_truthy(mid) else None
        f_end = date_at(ff[t['id']])
        breach: Any = None
        if t['status'] != 'done':
            w = wait.get(t['id'])
            wt = by_id.get(w) if w else None
            if t['due_on'] < today:
                breach = {'kind': 'overdue', 'message': f'Overdue since {fmt_date(t["due_on"])}'}
            elif t['status'] == 'blocked':
                br = t.get('blocked_reason')
                breach = {'kind': 'blocked', 'message': f'Blocked: {js_str(br)}' if js_truthy(br) else 'Blocked'}
            elif f_end > t['due_on']:
                breach = {
                    'kind': 'forecast_late',
                    'message': f'Waiting on {wbs_code(wt, ms)} {js_str(prop(wt, "title"))} ({user_name(ctx, wt.get("owner_id"))}); forecast end {fmt_date(f_end)}, target {fmt_date(t["due_on"])}'
                    if wt is not None
                    else f'Forecast end {fmt_date(f_end)}, target {fmt_date(t["due_on"])}',
                }
            elif m is not None and f_end > m['planned_end']:
                breach = {'kind': 'milestone', 'message': f'Forecast end {fmt_date(f_end)} passes the M{js_str(m["no"])} deadline {fmt_date(m["planned_end"])}'}
        revs = coalesce(prop(t, 'target_revisions'), [])
        a = act.get(t['id'])
        sched_tasks.append(
            {
                'id': t['id'],
                'wbs': wbs_code(t, ms),
                'title': prop(t, 'title'),
                'owner_id': prop(t, 'owner_id'),
                'status': t['status'],
                'milestone_id': m['id'] if m is not None else None,
                'start_on': t['start_on'],
                'due_on': t['due_on'],
                'done_on': prop(t, 'done_on'),
                'forecast_start': date_at(fs[t['id']]),
                'forecast_end': f_end,
                'float_days': coalesce(float_of.get(t['id'], UNDEF), 0),
                'project_float_days': coalesce(proj_float.get(t['id'], UNDEF), 0),
                'critical': coalesce(float_of.get(t['id'], UNDEF), 1) <= 0,
                'deps': coalesce(deps.get(t['id'], UNDEF), []),
                'remaining_hours': coalesce(rem_m.get(t['id'], UNDEF), 0),
                'breach': breach,
                'stage': stage_of(t),
                'progress': progress_of(t),
                'est_hours': t['est_hours'],
                'logged_hours': a['hours'] if a is not None else 0,
                'baseline_due_on': coalesce(prop(t, 'baseline_due_on'), coalesce(prop(revs[0], 'from') if revs else UNDEF, None)),
                'revised_on': revs[-1]['at'][:10] if revs else None,
                'actual_start_on': coalesce(prop(t, 'actual_start_on'), None),
                'waiting_on_id': coalesce(wait.get(t['id'], UNDEF), None),
            }
        )
    sched_tasks = js_sort(sched_tasks, lambda a, b: sort_wbs(a['wbs'], b['wbs']))
    sched_ms = []
    for m in milestones:
        mine = [t for t in sched_tasks if t['milestone_id'] == m['id']]
        if mine:
            f_end = '0000-01-01'
            for t in mine:
                f_end = max_iso(f_end, coalesce(t['done_on'], t['due_on']) if t['status'] == 'done' else t['forecast_end'])
        else:
            f_end = None
        sched_ms.append(
            {
                'id': m['id'],
                'no': m['no'],
                'name': m['name'],
                'planned_start': m['planned_start'],
                'planned_end': m['planned_end'],
                'baseline_end': m['baseline_end'],
                'forecast_end': f_end,
                'achieved_on': prop(m, 'achieved_on'),
                'state': milestone_state(ctx, m, tasks, f_end)['state'],
            }
        )
    frm = min_iso(p['start_on'], today)
    to = max_iso(p['end_on'], today)
    for t in sched_tasks:
        frm = min_iso(frm, min_iso(t['start_on'], t['forecast_start']))
        to = max_iso(to, max_iso(t['due_on'], t['forecast_end']))
    for m in sched_ms:
        to = max_iso(to, max_iso(m['planned_end'], m['baseline_end']))
    if sched_tasks:
        forecast_finish: Any = '0000-01-01'
        for t in sched_tasks:
            forecast_finish = max_iso(forecast_finish, coalesce(t['done_on'], t['due_on']) if t['status'] == 'done' else t['forecast_end'])
    else:
        forecast_finish = None
    return {
        'from': add_days(frm, -2),
        'to': add_days(to, 3),
        'today': today,
        'project_start': p['start_on'],
        'project_end': p['end_on'],
        'cpm_finish': None if finish_idx is None else date_at(finish_idx),
        'forecast_finish': forecast_finish,
        'tasks': sched_tasks,
        'milestones': sched_ms,
        'critical_path': path,
        'conflicts': conflicts,
        'cycles': js_sort([wbs_code(by_id[i], ms) for i in cycles], sort_wbs),
        'can_edit': False,
    }


# ---------------------------------------------------------------------------------------------------------------------
# estimation hint (G15.04)
# ---------------------------------------------------------------------------------------------------------------------
def _median(a: list) -> Any:
    if not a:
        return 1
    s = sorted(a)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def effort_ratio_samples(ctx: Ctx) -> dict[str, Any]:
    """Effort-ratio samples (actual ÷ estimate) on closed tasks with logged time:
    {'byPerson': {...}, 'byCx': {...}, 'byPersonCx': {...}, 'team': [...]}"""

    def f() -> dict[str, Any]:
        act = task_actuals(ctx)
        by_person: dict[str, list] = {}
        by_cx: dict[str, list] = {}
        by_person_cx: dict[str, list] = {}
        team: list = []
        for t in rows(ctx, 'tasks'):
            if t['status'] != 'done' or not js_truthy(t.get('owner_id')) or t['est_hours'] <= 0:
                continue
            a0 = act.get(t['id'])
            a = a0['hours'] if a0 is not None else 0
            if a <= 0:
                continue
            r = a / t['est_hours']
            team.append(r)
            by_person.setdefault(t['owner_id'], []).append(r)
            by_cx.setdefault(t['complexity'], []).append(r)
            by_person_cx.setdefault(f'{js_str(t["owner_id"])}:{js_str(t["complexity"])}', []).append(r)
        return {'byPerson': by_person, 'byCx': by_cx, 'byPersonCx': by_person_cx, 'team': team}

    return _cached(ctx, 'ratioSamples', f)


_CX_LABEL = {'low': 'Low', 'medium': 'Medium', 'high': 'High', 'very_high': 'Very high'}


def estimate_hint(ctx: Ctx, owner_id: Any, complexity: Any, est: Any) -> dict[str, Any]:
    """ "Your typical effort ratio for High complexity is ×1.25 — consider 25% buffer (suggested 30h)"."""
    s = effort_ratio_samples(ctx)
    cx_label = coalesce(_CX_LABEL.get(complexity, UNDEF) if isinstance(complexity, str) else UNDEF, complexity)
    name = user_name(ctx, owner_id) if js_truthy(owner_id) else None
    pick = [
        ('person_complexity', s['byPersonCx'].get(f'{js_str(owner_id)}:{js_str(complexity)}') if js_truthy(owner_id) else None),
        ('person', s['byPerson'].get(owner_id) if js_truthy(owner_id) else None),
        ('complexity', s['byCx'].get(complexity) if isinstance(complexity, str) else None),
        ('team', s['team']),
    ]
    hit = next(((b, a) for b, a in pick if a is not None and len(a) >= 3), None)
    if hit is None:
        return {'ratio': None, 'basis': 'none', 'samples': 0, 'buffer_pct': 0, 'suggested_hours': None, 'message': None}
    basis, arr = hit
    r = js_round(_median(arr) * 100) / 100
    buffer = js_round((r - 1) * 100) if r > 1.05 else 0
    sugg = js_round(est * js_max(1, r) * 2) / 2 if js_truthy(est) and est > 0 else None
    owner = f'{name.split(" ")[0]}’s' if js_truthy(name) else 'The owner’s'
    cxl = js_str(cx_label)
    if basis == 'person_complexity':
        who = f'{owner} typical effort ratio for {cxl} complexity'
    elif basis == 'person':
        who = f'{owner} typical effort ratio'
    elif basis == 'complexity':
        who = f'The team’s typical effort ratio for {cxl} complexity'
    else:
        who = 'The team’s typical effort ratio'
    rf = to_fixed(r, 2)
    if buffer > 0:
        message = f'{who} is ×{rf} — consider a {js_str(buffer)}% buffer{f" (suggested {js_str(sugg)}h)" if js_truthy(sugg) else ""}.'
    elif r < 0.85:
        message = f'{who} is ×{rf} — estimates tend to run high; {f"{js_str(est)}h" if js_truthy(sugg) else "this estimate"} is safe.'
    else:
        message = f'{who} is ×{rf} — estimates are sound.'
    return {'ratio': r, 'basis': basis, 'samples': len(arr), 'buffer_pct': buffer, 'suggested_hours': sugg if buffer > 0 else None, 'message': message}
