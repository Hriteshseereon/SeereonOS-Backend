"""v9 report types (CEO request 14, decision D9): a catalogue of report TYPES by category — Financial, Attendance,
Salary, Task, Project, Timesheet — each with a field picker, typed filters (period, project, customer, team, person,
status), one group-by (a field or a date grain), sort and chart. The server computes everything: rows (through the same
permission-scoped dataset builders as the report engine, or a type's own builder), the KPI summary, the chart series,
the detailed table and its totals, and a one-line finding. Permissions are per type and enforced here; salary types need
salary access (cost_rates.view). Saved typed reports live in saved_reports (report_type + report_config, V013).

Config (client → server, all optional):
  {fields: [key…], period: {preset} | {from, to}, filters: {project: [name…], customer: [name…], team: [id…],
   person: [name…], status: [value…]}, group_by: 'field' | 'field:grain', sort: {field, dir}, chart: 'auto' | 'bar' |
   'column' | 'line' | 'donut' | 'none'}
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.ctx import Ctx, can_see_salaries, err, has, has_any, now_iso, rows, user_name
from app.domain.calendar import CalendarCtx, add_days, diff_days, is_working_day, month_of, month_range, week_start, working_days_between
from app.domain.money import fmt_money_compact
from app.engine.analytics import PAYROLL_CATEGORIES, cal, expand_ledger, payroll_for_month, project_metrics
from app.engine.holiday_cal import holidays_for_user
from app.js import js_round, prop
from app.reports.datasets import DATASET_MAP, GRAIN_LABEL, PRESET_LABEL
from app.reports.engine import bucket_of, preset_range

CATEGORIES = [
    {'id': 'financial', 'label': 'Financial', 'description': 'Profit & loss, ledger, receivables, margins and spend.'},
    {'id': 'attendance', 'label': 'Attendance', 'description': 'Presence, leave and the holiday calendar.'},
    {'id': 'salary', 'label': 'Salary', 'description': 'Payroll register and cost rates — salary access only.'},
    {'id': 'task', 'label': 'Task', 'description': 'Task register and on-time delivery.'},
    {'id': 'project', 'label': 'Project', 'description': 'Portfolio status and milestones.'},
    {'id': 'timesheet', 'label': 'Timesheet', 'description': 'Hours logged and weekly compliance.'},
]
CHARTS = ('auto', 'bar', 'column', 'line', 'donut', 'none')
GRAINS = ('day', 'week', 'month', 'quarter', 'fy')
MAX_TABLE_ROWS = 2000


# ------------------------------------------------------------------ helpers
def _opts(ds: str, key: str) -> list:
    return next((f.get('options') or [] for f in DATASET_MAP[ds]['fields'] if f['key'] == key), [])


def _sum(rs: list[dict], k: str) -> Any:
    s: Any = 0
    for r in rs:
        v = r.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s += v
    return s


def _avg(rs: list[dict], k: str) -> Any:
    vals = [r[k] for r in rs if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
    return sum(vals) / len(vals) if vals else None


def _pct(a: Any, b: Any) -> Any:
    return a / b if b else None


def _p0(v: Any) -> str:
    return '—' if v is None else f'{js_round(v * 100)}%'


def _m(v: Any) -> str:
    return fmt_money_compact(v)


def _n(v: Any) -> str:
    if not isinstance(v, (int, float)):
        return '—'
    return f'{v:,.0f}' if float(v).is_integer() else f'{v:,.1f}'


def _h(v: Any) -> str:
    return f'{v:,.1f}h' if isinstance(v, (int, float)) else '—'


def _lc(s: str) -> str:
    return s if s[:2].isupper() else s[:1].lower() + s[1:]


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f'{n:,} {one if n == 1 else (many or one + "s")}'


def K(key: str, label: str, value: Any, fmt: str = 'number', tone: str | None = None, hint: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {'key': key, 'label': label, 'value': value, 'format': fmt}
    if tone:
        out['tone'] = tone
    if hint:
        out['hint'] = hint
    return out


def F(key: str, label: str, type_: str, unit: str | None = None, options: list | None = None, total: str | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {'key': key, 'label': label, 'type': type_}
    if unit:
        f['unit'] = unit
    if options:
        f['options'] = options
    if total:
        f['total'] = total
    return f


def _team_rows(ctx: Ctx) -> list[dict]:
    """Teams master (v9 D6, A3) — falls back to departments when the workspace has no teams."""
    try:
        teams = [t for t in rows(ctx, 'teams') if t.get('active', True)]
    except Exception:  # noqa: BLE001 — the teams table may not exist in older databases / test snapshots
        teams = []
    if teams:
        return [
            {
                'id': t['id'],
                'name': t['name'],
                'members': list(dict.fromkeys([*(t.get('member_ids') or []), *([t['lead_id']] if t.get('lead_id') else [])])),
                'kind': 'team',
            }
            for t in teams
        ]
    users = [u for u in rows(ctx, 'users') if u.get('status') == 'active']
    try:
        deps = rows(ctx, 'departments')
    except Exception:  # noqa: BLE001
        deps = []
    out = []
    for d in deps:
        if d.get('active') is False:
            continue
        members = [u['id'] for u in users if u.get('department_id') == d['id']]
        if members:
            out.append({'id': d['id'], 'name': d['name'], 'members': members, 'kind': 'department'})
    return out


# ------------------------------------------------------------------ custom builders (types without a dataset)
def _pnl_rows(ctx: Ctx) -> list[dict]:
    """Ledger to date with salaries on the shared payroll basis (same as /finance/pnl and the CEO dashboard)."""
    projs = {p['id']: p for p in rows(ctx, 'projects')}
    frm = ctx.today
    for e in rows(ctx, 'ledger_entries'):
        if e['date'] < frm:
            frm = e['date']
    out = []
    for e in expand_ledger(ctx, frm, ctx.today):
        if e['kind'] == 'expense' and e.get('category') in PAYROLL_CATEGORIES:
            continue
        pr = projs.get(e.get('project_id')) if e.get('project_id') else None
        inc = e['kind'] == 'income'
        out.append(
            {
                'date': e['occurrence'],
                'kind': e['kind'],
                'category': e.get('category') or 'Other',
                'description': e['description'],
                'stream': e.get('stream'),
                'project': pr['name'] if pr else None,
                'client': (pr.get('client') if pr else None) or None,
                'income': e['amount_paise'] if inc else 0,
                'cost': 0 if inc else e['amount_paise'],
                'net': e['amount_paise'] if inc else -e['amount_paise'],
                'status': e.get('status'),
            }
        )
    m = month_of(frm)
    while m <= month_of(ctx.today):
        pay = payroll_for_month(ctx, m)
        if pay['payroll']:
            out.append(
                {
                    'date': f'{m}-01',
                    'kind': 'expense',
                    'category': 'Salaries',
                    'description': f'Salaries ({pay["headcount"]} people, {"booked" if pay["source"] == "ledger" else "from CTC"})',
                    'stream': None,
                    'project': None,
                    'client': None,
                    'income': 0,
                    'cost': pay['payroll'],
                    'net': -pay['payroll'],
                    'status': 'paid',
                }
            )
        m = month_of(add_days(f'{m}-28', 7))
    return out


def _scope_users(ctx: Ctx) -> list[dict]:
    from app.routes.reports import is_timesheet_user, team_of

    users = [u for u in rows(ctx, 'users') if is_timesheet_user(u)]
    if has(ctx, 'people.view_all'):
        return users
    team = team_of(ctx, ctx.user_id)
    team.add(ctx.user_id)
    return [u for u in users if u['id'] in team]


def _person_cal(ctx: Ctx, cache: dict, user_id: str) -> Any:
    """The person's holiday calendar (their location's, v9 A5) without leave dates — the working days their own
    timesheet expects. Company-wide maths keeps cal(ctx) (the default calendar)."""
    c = cache.get(user_id)
    if c is None:
        c = cache[user_id] = CalendarCtx(ctx.tenant.get('settings'), holidays_for_user(ctx.tenant_id, user_id))
    return c


def _attendance_rows(ctx: Ctx, rng: dict | None) -> list[dict]:
    """One row per person for the period: working days, present (days with logged work), leave, unaccounted."""
    from app.engine.analytics import costed_entries

    c = cal(ctx)
    t = ctx.today
    frm = rng['from'] if rng else month_range(month_of(t))['from']
    to = min(rng['to'] if rng else t, add_days(t, -1))  # today is still being logged: count up to yesterday
    users = _scope_users(ctx)
    ids = {u['id'] for u in users}
    present: dict[str, set] = {}
    hours: dict[str, float] = {}
    for e in costed_entries(ctx):
        if e['user_id'] in ids and frm <= e['work_date'] <= to and e.get('kind') != 'leave' and e['hours'] > 0:
            present.setdefault(e['user_id'], set()).add(e['work_date'])
            hours[e['user_id']] = hours.get(e['user_id'], 0) + e['hours']
    pcal: dict = {}
    leave: dict[str, dict] = {}
    for lv in rows(ctx, 'leave'):
        if lv['user_id'] in ids and frm <= lv['date'] <= to and is_working_day(lv['date'], _person_cal(ctx, pcal, lv['user_id'])):
            d = leave.setdefault(lv['user_id'], {})
            d[lv['date']] = d.get(lv['date'], 0) + (1 if lv['portion'] == 'full' else 0.5)
    wd_all = working_days_between(frm, to, c) if to >= frm else 0
    out = []
    for u in users:
        uc = _person_cal(ctx, pcal, u['id'])
        joined = u.get('joined_on') or frm
        start = max(frm, joined)
        wd = working_days_between(start, to, uc) if to >= start else 0
        lv = sum(min(1, v) for v in leave.get(u['id'], {}).values())
        pres_days = {d for d in present.get(u['id'], set()) if is_working_day(d, uc)}
        pres = len(pres_days)
        un = max(0, wd - pres - lv)
        out.append(
            {
                'person': u['name'],
                'department': u.get('department'),
                'title': u.get('title'),
                'working_days': wd,
                'present_days': pres,
                'leave_days': lv,
                'unaccounted_days': un,
                'attendance': _pct(pres + lv, wd) if wd else None,
                'hours': round(hours.get(u['id'], 0), 2),
                'avg_hours': round(hours.get(u['id'], 0) / pres, 2) if pres else None,
                'date': frm,
                '_wd_all': wd_all,
            }
        )
    return out


def _holiday_rows(ctx: Ctx) -> list[dict]:
    try:
        cals = {x['id']: x for x in rows(ctx, 'holiday_calendars')}
    except Exception:  # noqa: BLE001 — holiday calendars (v9 D10, A5) may not exist yet
        cals = {}
    out = []
    for h in rows(ctx, 'holidays'):
        cal_row = cals.get(h.get('calendar_id')) if h.get('calendar_id') else None
        out.append(
            {
                'date': h['date'],
                'name': h['name'],
                'weekday': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][_weekday(h['date'])],
                'kind': 'optional' if h.get('optional') else 'mandatory',
                'calendar': cal_row['name'] if cal_row else 'Company calendar',
            }
        )
    return out


def _weekday(d: str) -> int:
    import datetime as _dt

    return _dt.date.fromisoformat(d[:10]).weekday()


def _compliance_rows(ctx: Ctx, rng: dict | None) -> list[dict]:
    """One row per person: weeks expected / submitted / approved / missing in the period (completed weeks)."""
    from app.routes.reports import effective_week_status

    t = ctx.today
    last_mon = add_days(week_start(t), -7)
    frm = week_start(rng['from']) if rng else add_days(last_mon, -7 * 7)
    to_mon = min(week_start(rng['to']) if rng else last_mon, last_mon)
    weeks = []
    w = frm
    while w <= to_mon and len(weeks) < 60:
        weeks.append(w)
        w = add_days(w, 7)
    by: dict[tuple, dict] = {}
    for x in rows(ctx, 'timesheet_weeks'):
        by[(x['user_id'], x['week_start'])] = x
    min_h = prop(ctx.tenant['settings'], 'calendar.min_hours_per_day') or 8
    pcal: dict = {}
    leave_of: dict[str, dict] = {}  # user -> date -> portion ('full' wins over 'half')
    for lv_ in rows(ctx, 'leave'):
        cur = leave_of.setdefault(lv_['user_id'], {})
        cur[lv_['date']] = 'full' if lv_.get('portion') == 'full' or cur.get(lv_['date']) == 'full' else 'half'
    hours: dict[tuple, float] = {}
    from app.engine.analytics import costed_entries

    for e in costed_entries(ctx):
        if weeks and weeks[0] <= e['work_date'] <= add_days(weeks[-1], 6):
            k = (e['user_id'], week_start(e['work_date']))
            hours[k] = hours.get(k, 0) + e['hours']
    out = []
    for u in _scope_users(ctx):
        mine = [w for w in weeks if w >= week_start(u.get('joined_on') or w)]
        st = [effective_week_status(ctx, by.get((u['id'], w)), w) for w in mine]
        sub = sum(1 for s in st if s in ('submitted', 'approved'))
        appr = sum(1 for s in st if s == 'approved')
        uc = _person_cal(ctx, pcal, u['id'])
        # the timesheet's own target: working days × minimum hours, less leave (full day 0, half day half)
        lv = leave_of.get(u['id'], {})
        target = sum(working_days_between(w, add_days(w, 6), uc) for w in mine) * min_h - sum(
            min_h * (1 if p == 'full' else 0.5) for d, p in lv.items() if any(w <= d <= add_days(w, 6) for w in mine) and is_working_day(d, uc)
        )
        logged = sum(hours.get((u['id'], w), 0) for w in mine)
        out.append(
            {
                'person': u['name'],
                'department': u.get('department'),
                'weeks': len(mine),
                'submitted': sub,
                'approved': appr,
                'missing': len(mine) - sub,
                'compliance': _pct(sub, len(mine)),
                'hours': round(logged, 2),
                'target_hours': target,
                'hours_pct': _pct(logged, target),
                'last_status': st[-1] if st else None,
                'date': weeks[0] if weeks else frm,
                '_weeks': list(zip(mine, st)),
            }
        )
    return out


# ------------------------------------------------------------------ derived fields on dataset rows
def _age_bucket(r: dict) -> str:
    if r.get('_age'):
        return r['_age']  # an opening balance row (V9-FIX item 9): its own bucket
    o = r.get('outstanding') or 0
    if o <= 0:
        return 'paid'
    d = r.get('days_overdue') or 0
    return 'not_due' if d <= 0 else 'd30' if d <= 30 else 'd60' if d <= 60 else 'd90' if d <= 90 else 'd90p'


def _opening_receivable_rows(ctx: Ctx, rng: dict | None) -> list[dict]:
    """V9-FIX (item 9): the receivables brought forward in the latest FY opening balances, one "Opening balance" row
    per line (dated the FY start; never paid through invoices, so outstanding = the amount), aged by its due date or,
    without one, in the oldest bucket."""
    from app.engine.openings import opening_age_bucket, opening_parties

    ob = opening_parties(ctx)
    if ob is None:
        return []
    t = ctx.today
    out = []
    for ln in ob['receivables']:
        age = opening_age_bucket(ln, t)
        due = ln.get('due_on') or None
        out.append(
            {
                'number': 'Opening balance',
                'client': ln['party_name'],
                'project': '—',
                'milestone': f'{ob["fy_label"]} opening' + (f' · {ln["reference"]}' if ln.get('reference') else ''),
                'stream': None,
                'invoiced_on': ob['as_of'],
                'due_on': due,
                'paid_on': None,
                'amount': ln['amount_paise'],
                'paid': 0,
                'outstanding': ln['amount_paise'],
                'status': 'unpaid',
                'days_overdue': diff_days(t, due) if due and due < t else (diff_days(t, ob['as_of']) if not due else 0),
                'is_overdue': age != 'not_due',
                'age_days': diff_days(t, ob['as_of']),
                '_age': age,
            }
        )
    return out


AGE_OPTIONS = [
    {'value': 'paid', 'label': 'Paid'},
    {'value': 'not_due', 'label': 'Not due'},
    {'value': 'd30', 'label': '1–30 days overdue'},
    {'value': 'd60', 'label': '31–60 days overdue'},
    {'value': 'd90', 'label': '61–90 days overdue'},
    {'value': 'd90p', 'label': 'Over 90 days overdue'},
]


def _planned_margin(ctx: Ctx, r: dict) -> Any:
    """The project's planned margin exactly as the Costing tab, CEO margins and Insights show it (project_metrics:
    the costing plan's planned revenue, i.e. before approved change requests) — not contract value incl. CRs."""
    pc = r.get('planned_cost')
    if not isinstance(pc, (int, float)):
        return None  # planned cost hidden for this viewer (no margins access) or not computed
    p = next((x for x in rows(ctx, 'projects') if x['id'] == r.get('id')), None) if r.get('id') else None
    if p is not None:
        return project_metrics(ctx, p).get('planned_margin')
    cv = r.get('contract_value')
    return (cv - pc) / cv if isinstance(cv, (int, float)) and cv else None


# ------------------------------------------------------------------ the catalogue
def _k_pnl(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    inc, cost = _sum(rs, 'income'), _sum(rs, 'cost')
    net = inc - cost
    mg = _pct(net, inc)
    ks = [
        K('income', 'Income', inc, 'money'),
        K('cost', 'Costs', cost, 'money'),
        K('net', 'Net profit' if net >= 0 else 'Net loss', net, 'money', 'success' if net >= 0 else 'danger'),
        K('margin', 'Net margin', mg, 'percent'),
    ]
    f = (
        (f'Net {"profit" if net >= 0 else "loss"} of {_m(abs(net))} on {_m(inc)} income' + (f' — {_p0(mg)} margin' if mg is not None else ''))
        if (inc or cost)
        else 'No income or costs in this period'
    )
    return ks, f


def _k_ledger(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    b, c, s = _sum(rs, 'billed'), _sum(rs, 'collected'), _sum(rs, 'spent')
    ks = [K('rows', 'Entries', len(rs)), K('billed', 'Billed', b, 'money'), K('collected', 'Collected', c, 'money'), K('spent', 'Spent', s, 'money')]
    return ks, (f'{_plural(len(rs), "entry", "entries")}: {_m(b)} billed, {_m(c)} collected, {_m(s)} spent' if rs else 'No ledger entries in this period')


def _k_recv(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    a, p, o = _sum(rs, 'amount'), _sum(rs, 'paid'), _sum(rs, 'outstanding')
    od = _sum([r for r in rs if r.get('is_overdue')], 'outstanding')
    ks = [
        K('amount', 'Invoiced', a, 'money'),
        K('paid', 'Collected', p, 'money'),
        K('outstanding', 'Outstanding', o, 'money', 'warning' if o else None),
        K('overdue', 'Overdue', od, 'money', 'danger' if od else 'success'),
    ]
    f = f'{_m(o)} of {_m(a)} invoiced is still outstanding' + (f'; {_m(od)} overdue' if od else ', none overdue') if rs else 'No invoices in this period'
    return ks, f


def _k_margins(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    floor = (prop(ctx.tenant['settings'], 'projects.margin_floor_pct') or 25) / 100
    cv = _sum(rs, 'contract_value')
    pc = _sum(rs, 'projected_cost')
    port = _pct(cv - pc, cv)
    below = [r for r in rs if isinstance(r.get('projected_margin'), (int, float)) and r['projected_margin'] < floor]
    ks = [
        K('contract', 'Contract value', cv, 'money'),
        K('projected_cost', 'Projected cost', pc, 'money'),
        K('port', 'Portfolio projected margin', port, 'percent', None if port is None else 'success' if port >= floor else 'danger'),
        K('below', f'Below {js_round(floor * 100)}% floor', len(below), 'number', 'danger' if below else 'success'),
    ]
    f = (f'Portfolio projected margin {_p0(port)}; {_plural(len(below), "project")} below the {js_round(floor * 100)}% floor') if rs else 'No projects match'
    return ks, f


def _k_expenses(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    tot = _sum(rs, 'cost')
    cats: dict[str, Any] = {}
    for r in rs:
        cats[r['category']] = cats.get(r['category'], 0) + (r.get('cost') or 0)
    top = max(cats.items(), key=lambda kv: kv[1]) if cats else None
    ks = [
        K('cost', 'Total spend', tot, 'money'),
        K('entries', 'Entries', len(rs)),
        K('cats', 'Categories', len(cats)),
        K('top', f'Largest: {top[0]}' if top else 'Largest category', top[1] if top else None, 'money'),
    ]
    f = f'{_m(tot)} spent; {top[0]} is the largest at {_m(top[1])} ({_p0(_pct(top[1], tot))})' if top and tot else 'No spend in this period'
    return ks, f


def _k_claims(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    a = _sum(rs, 'amount')
    pend = [r for r in rs if r.get('status') == 'submitted']
    unpaid = [r for r in rs if r.get('is_unpaid')]
    ks = [
        K('claims', 'Claims', len(rs)),
        K('amount', 'Claimed', a, 'money'),
        K('pending', 'Awaiting approval', len(pend), 'number', 'warning' if pend else None),
        K('unpaid', 'Approved, unpaid', _sum(unpaid, 'amount'), 'money'),
    ]
    return ks, (f'{_plural(len(rs), "claim")} worth {_m(a)}; {len(pend)} awaiting approval' if rs else 'No expense claims in this period')


def _k_att(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    wd, pr, lv, un = _sum(rs, 'working_days'), _sum(rs, 'present_days'), _sum(rs, 'leave_days'), _sum(rs, 'unaccounted_days')
    att = _pct(pr + lv, wd)
    ks = [
        K(
            'att',
            'Attendance',
            att,
            'percent',
            None if att is None else 'success' if att >= 0.95 else 'warning' if att >= 0.85 else 'danger',
            'Present or on approved leave ÷ working days',
        ),
        K('present', 'Days present', pr),
        K('leave', 'Leave days', lv),
        K('unaccounted', 'Unaccounted days', un, 'number', 'danger' if un else 'success'),
    ]
    f = (
        (
            f'{_p0(att)} attendance across {_plural(len(rs), "person", "people")}'
            + (f'; {_n(un)} working days have neither time logged nor leave' if un else '; every working day is accounted for')
        )
        if rs
        else 'No people in scope'
    )
    if rs and not wd:
        f = 'No working days yet in this period for the people in scope'
    return ks, f


def _k_leave(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    appr = [r for r in rs if r.get('status') == 'approved']
    days = _sum(appr, 'days')
    pend = [r for r in rs if r.get('status') == 'pending']
    types: dict[str, Any] = {}
    for r in appr:
        types[r.get('type')] = types.get(r.get('type'), 0) + (r.get('days') or 0)
    top = max(types.items(), key=lambda kv: kv[1]) if types else None
    lab = {o['value']: o['label'] for o in _opts('leave_requests', 'type')}
    ks = [
        K('requests', 'Requests', len(rs)),
        K('days', 'Days approved', days),
        K('pending', 'Pending', len(pend), 'number', 'warning' if pend else None),
        K('people', 'People', len({r.get('person') for r in rs})),
    ]
    if not rs:
        f = 'No leave requests in this period'
    elif not appr:
        f = 'No leave approved in this period' + (f'; {_plural(len(pend), "request")} pending' if pend else '')
    else:
        f = f'{_n(days)} leave {"day" if days == 1 else "days"} approved across {_plural(len(appr), "request")}'
        f += (f'; most taken as {lab.get(top[0], top[0]).lower()} leave' if top else '') + (f'; {len(pend)} pending' if pend else '')
    return ks, f


def _k_hol(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    mand = [r for r in rs if r['kind'] == 'mandatory']
    wk = [r for r in mand if _weekday(r['date']) < 6]
    up = [r for r in rs if r['date'] >= ctx.today]
    nxt = min(up, key=lambda r: r['date']) if up else None
    ks = [
        K('total', 'Holidays', len(rs)),
        K('mandatory', 'Company holidays', len(mand)),
        K('optional', 'Optional', len(rs) - len(mand)),
        K('upcoming', 'Still to come', len(up)),
    ]
    f = (
        (f'{_plural(len(mand), "company holiday")} ({len(wk)} on weekdays)' + (f'; next: {nxt["name"]} on {nxt["date"]}' if nxt else ''))
        if rs
        else 'No holidays in this period'
    )
    return ks, f


def _k_payroll(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    ctc, net, ded = _sum(rs, 'ctc'), _sum(rs, 'net'), _sum(rs, 'deduction')
    ppl = len({r.get('person') for r in rs})
    ks = [K('ctc', 'Total CTC', ctc, 'money'), K('net', 'Net pay', net, 'money'), K('deduction', 'Deductions', ded, 'money'), K('people', 'People', ppl)]
    return ks, (
        f'Net pay {_m(net)} on {_m(ctc)} CTC for {_plural(ppl, "person", "people")}' + (f'; {_m(ded)} deducted' if ded else '')
        if rs
        else 'No payroll in this period'
    )


def _k_ctc(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    ctc = _sum(rs, 'ctc')
    cph = _avg(rs, 'cost_per_hour')
    ks = [
        K('people', 'People', len(rs)),
        K('ctc', 'Monthly CTC', ctc, 'money'),
        K('annual', 'Annual CTC', ctc * 12, 'money'),
        K('cph', 'Average cost per hour', js_round(cph) if cph is not None else None, 'money'),
    ]
    if rs and not ctc:
        return ks, f'No CTC recorded yet for {_plural(len(rs), "person", "people")} — add cost rates under People'
    return ks, (
        f'{_m(ctc)} a month ({_m(ctc * 12)} a year) for {_plural(len(rs), "person", "people")}; average {_m(cph)} per hour' if rs else 'No people in scope'
    )


def _k_tasks(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    done = [r for r in rs if r.get('status') == 'done']
    ot = [r for r in done if r.get('on_time') is True]
    od = [r for r in rs if r.get('is_overdue')]
    rate = _pct(len(ot), len([r for r in done if r.get('on_time') is not None]))
    ks = [
        K('tasks', 'Tasks', len(rs)),
        K('done', 'Done', len(done)),
        K('on_time', 'Done on time', rate, 'percent', None if rate is None else 'success' if rate >= 0.8 else 'warning'),
        K('overdue', 'Overdue', len(od), 'number', 'danger' if od else 'success'),
    ]
    f = (f'{_plural(len(rs), "task")}: {len(done)} done ({_p0(rate)} on time), {len(od)} overdue') if rs else 'No tasks match'
    return ks, f


def _k_delivery(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    ot = [r for r in rs if r.get('on_time') is True]
    rate = _pct(len(ot), len(rs))
    var = _avg([r for r in rs if r.get('timeliness') == 'late'], 'variance_days')
    er = _avg(rs, 'effort_ratio')
    ks = [
        K('done', 'Tasks closed', len(rs)),
        K('rate', 'On time', rate, 'percent', None if rate is None else 'success' if rate >= 0.8 else 'warning' if rate >= 0.6 else 'danger'),
        K('late_by', 'Average lateness (late tasks)', round(var, 1) if var is not None else None, 'days'),
        K('er', 'Average effort ratio', round(er, 2) if er is not None else None, 'ratio'),
    ]
    return ks, (
        f'{_plural(len(rs), "task")} closed, {_p0(rate)} on time' + (f'; late ones ran {var:.1f} working days over' if var else '')
        if rs
        else 'No tasks closed in this period'
    )


def _k_projects(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    live = [r for r in rs if r.get('status') in ('initiated', 'active', 'on_hold')]
    risk = [r for r in rs if r.get('health') in ('at_risk', 'off_track') and r.get('status') in ('initiated', 'active')]
    ks = [
        K('projects', 'Projects', len(rs)),
        K('live', 'Live', len(live)),
        K('risk', 'At risk or off track', len(risk), 'number', 'warning' if risk else 'success'),
        K('contract', 'Contract value', _sum(rs, 'contract_value'), 'money'),
    ]
    return ks, (f'{len(live)} of {_plural(len(rs), "project")} live; {len(risk)} at risk or off track' if rs else 'No projects match')


def _k_ms(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    ach = [r for r in rs if r.get('state') in ('achieved_on_time', 'achieved_late')]
    od = [r for r in rs if r.get('state') == 'overdue']
    slip = [r for r in rs if (r.get('slip_days') or 0) > 0]
    ks = [
        K('ms', 'Milestones', len(rs)),
        K('achieved', 'Achieved', len(ach)),
        K('overdue', 'Overdue', len(od), 'number', 'danger' if od else 'success'),
        K('slipped', 'Slipped vs baseline', len(slip), 'number', 'warning' if slip else None),
    ]
    return ks, (
        f'{len(ach)} of {_plural(len(rs), "milestone")} achieved, {len(od)} overdue, {len(slip)} slipped against baseline'
        if rs
        else 'No milestones in this period'
    )


def _k_hours(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    tot = _sum(rs, 'hours')
    proj = _sum([r for r in rs if r.get('project') and r.get('kind') != 'leave'], 'hours')
    over = _sum([r for r in rs if not r.get('project') and r.get('kind') != 'leave'], 'hours')
    ppl = len({r.get('person') for r in rs})
    ks = [
        K('hours', 'Hours logged', tot, 'hours'),
        K('project', 'On projects', proj, 'hours'),
        K('overhead', 'Non-project (overhead)', over, 'hours'),
        K('people', 'People', ppl),
    ]
    return ks, (f'{_h(tot)} logged by {_plural(ppl, "person", "people")}, {_p0(_pct(proj, tot))} on projects' if rs else 'No hours logged in this period')


def _k_comp(rs: list[dict], ctx: Ctx) -> tuple[list, str]:
    wk, sub, miss = _sum(rs, 'weeks'), _sum(rs, 'submitted'), _sum(rs, 'missing')
    rate = _pct(sub, wk)
    full = [r for r in rs if r.get('missing') == 0 and r.get('weeks')]
    ks = [
        K('rate', 'Compliance', rate, 'percent', None if rate is None else 'success' if rate >= 0.9 else 'warning' if rate >= 0.75 else 'danger'),
        K('missing', 'Weeks missing', miss, 'number', 'danger' if miss else 'success'),
        K('full', 'Fully compliant', len(full)),
        K('people', 'People', len(rs)),
    ]
    if not wk:
        return ks, 'No completed timesheet weeks in this period yet'
    return ks, (
        f'{_p0(rate)} of expected weeks submitted; {_plural(miss, "week")} missing across {_plural(len([r for r in rs if r.get("missing")]), "person", "people")}'
        if rs
        else 'No people in scope'
    )


Kpis = Callable[[list[dict], Ctx], tuple[list, str]]

# A type: source = dataset id (permission-scoped builder) or 'custom' with `build`; `pick` = selectable fields in order;
# `fields` = own definitions (custom types, derived fields, label overrides); `measures` = numeric fields summed when
# grouped; `chart` = default chart {group, measures, kind}.
TYPES: list[dict[str, Any]] = [
    # ---------------- Financial
    {
        'id': 'pnl',
        'noun': 'Entries',
        'category': 'financial',
        'label': 'Profit & loss',
        'description': 'Income against costs (salaries on the payroll basis), by category or month, to date.',
        'perms': ['finance.view'],
        'source': 'custom',
        'build': lambda ctx, rng: _pnl_rows(ctx),
        'date_field': 'date',
        'date_label': 'Entry date',
        'default_period': 'ytd',
        'fields': [
            F('date', 'Date', 'date'),
            F('kind', 'Type', 'enum', options=[{'value': 'income', 'label': 'Income'}, {'value': 'expense', 'label': 'Cost'}]),
            F('category', 'Category', 'string'),
            F('description', 'Description', 'string'),
            F('stream', 'Revenue stream', 'enum', options=_opts('ledger', 'stream')),
            F('project', 'Project', 'string'),
            F('client', 'Customer', 'string'),
            F('income', 'Income', 'money', total='sum'),
            F('cost', 'Costs', 'money', total='sum'),
            F('net', 'Net', 'money', total='sum'),
        ],
        'default_fields': ['date', 'kind', 'category', 'description', 'project', 'income', 'cost', 'net'],
        'measures': ['income', 'cost', 'net'],
        'groups': ['category', 'kind', 'stream', 'project', 'client', 'date:month', 'date:quarter'],
        'default_group': 'category',
        'default_sort': {'field': 'net', 'dir': 'desc'},
        'chart': {'group': 'date:month', 'measures': ['income', 'cost'], 'kind': 'column'},
        'filters': ['period', 'project', 'customer'],
        'project_field': 'project',
        'customer_field': 'client',
        'kpis': _k_pnl,
    },
    {
        'id': 'ledger',
        'noun': 'Entries',
        'category': 'financial',
        'label': 'General ledger',
        'description': 'Every income, cost and payment entry with billed, collected and spent columns.',
        'perms': ['ledger.view', 'finance.view'],
        'source': 'ledger',
        'actuals': True,
        'date_field': 'date',
        'date_label': 'Entry date',
        'default_period': 'this_month',
        'pick': [
            'date',
            'kind',
            'category',
            'description',
            'stream',
            'project',
            'client',
            'billed',
            'collected',
            'spent',
            'net',
            'status',
            'entry',
            'recurring',
        ],
        'default_fields': ['date', 'kind', 'category', 'description', 'project', 'billed', 'collected', 'spent', 'status'],
        'measures': ['billed', 'collected', 'spent', 'net'],
        'groups': ['category', 'kind', 'status', 'stream', 'project', 'client', 'date:week', 'date:month'],
        'default_group': None,
        'default_sort': {'field': 'date', 'dir': 'desc'},
        'chart': {'group': 'date:month', 'measures': ['collected', 'spent'], 'kind': 'column'},
        'filters': ['period', 'project', 'customer', 'status'],
        'status_field': 'status',
        'project_field': 'project',
        'customer_field': 'client',
        'kpis': _k_ledger,
    },
    {
        'id': 'receivables',
        'noun': 'Invoices',
        'category': 'financial',
        'label': 'Receivables & invoices',
        'description': 'Issued invoices with what is paid, outstanding and overdue, aged by days past due.',
        'perms': ['finance.view'],
        'source': 'invoices',
        'date_field': 'invoiced_on',
        'date_label': 'Invoice date',
        'default_period': 'this_fy',
        'pick': [
            'number',
            'client',
            'project',
            'milestone',
            'stream',
            'invoiced_on',
            'due_on',
            'paid_on',
            'amount',
            'paid',
            'outstanding',
            'status',
            'ageing',
            'days_overdue',
            'age_days',
        ],
        'fields': [F('ageing', 'Ageing', 'enum', options=AGE_OPTIONS), F('client', 'Customer', 'string')],
        'derive': lambda ctx, r: {'ageing': _age_bucket(r)},
        'extra': _opening_receivable_rows,  # V9-FIX (item 9): receivables brought forward (FY opening balances)
        'default_fields': ['number', 'client', 'project', 'invoiced_on', 'due_on', 'amount', 'paid', 'outstanding', 'status', 'days_overdue'],
        'measures': ['amount', 'paid', 'outstanding'],
        'groups': ['client', 'project', 'status', 'ageing', 'stream', 'invoiced_on:month', 'invoiced_on:quarter'],
        'default_group': None,
        'default_sort': {'field': 'invoiced_on', 'dir': 'desc'},
        'chart': {'group': 'ageing', 'measures': ['outstanding'], 'kind': 'bar', 'order': [o['value'] for o in AGE_OPTIONS]},
        'filters': ['period', 'project', 'customer', 'status'],
        'status_field': 'status',
        'project_field': 'project',
        'customer_field': 'client',
        'kpis': _k_recv,
    },
    {
        'id': 'project_margins',
        'noun': 'Projects',
        'category': 'financial',
        'label': 'Project margins',
        'description': 'Contract value against planned, projected and actual cost, with the margin at each stage.',
        'perms': ['projects.view_all', 'projects.edit'],
        'needs_margins': True,
        'source': 'projects',
        'date_field': 'start_on',
        'date_label': 'Project start',
        'default_period': 'all',
        'pick': [
            'code',
            'name',
            'client',
            'status',
            'pm',
            'contract_value',
            'planned_cost',
            'projected_cost',
            'actual_cost',
            'planned_margin',
            'projected_margin',
            'actual_margin',
            'invoiced',
            'collected',
            'health',
        ],
        'fields': [F('planned_margin', 'Planned margin', 'percent'), F('client', 'Customer', 'string')],
        'derive': lambda ctx, r: {'planned_margin': _planned_margin(ctx, r)},
        'default_fields': ['code', 'name', 'client', 'status', 'contract_value', 'projected_cost', 'planned_margin', 'projected_margin', 'actual_margin'],
        'measures': ['contract_value', 'planned_cost', 'projected_cost', 'actual_cost'],
        'groups': ['client', 'status', 'pm', 'health'],
        'default_group': None,
        'default_sort': {'field': 'projected_margin', 'dir': 'asc'},
        'chart': {'group': 'name', 'measures': ['planned_margin', 'projected_margin', 'actual_margin'], 'kind': 'bar', 'agg': 'avg'},
        'filters': ['period', 'project', 'customer', 'status'],
        'status_field': 'status',
        'project_field': 'name',
        'customer_field': 'client',
        'kpis': _k_margins,
        'scope_kpis': ('contract', 'port'),
    },
    {
        'id': 'expenses',
        'noun': 'Entries',
        'category': 'financial',
        'label': 'Company expenses',
        'description': 'Costs booked in the ledger (salaries included on the payroll basis), by category, project or month.',
        'perms': ['finance.view', 'ledger.view'],
        'source': 'custom',
        'build': lambda ctx, rng: [r for r in _pnl_rows(ctx) if r['kind'] == 'expense'],
        'date_field': 'date',
        'date_label': 'Entry date',
        'default_period': 'this_fy',
        'fields': [
            F('date', 'Date', 'date'),
            F('category', 'Category', 'string'),
            F('description', 'Description', 'string'),
            F('project', 'Project', 'string'),
            F('client', 'Customer', 'string'),
            F('cost', 'Amount', 'money', total='sum'),
            F('status', 'Status', 'enum', options=_opts('ledger', 'status')),
        ],
        'default_fields': ['date', 'category', 'description', 'project', 'cost', 'status'],
        'measures': ['cost'],
        'groups': ['category', 'project', 'status', 'date:month', 'date:quarter'],
        'default_group': 'category',
        'default_sort': {'field': 'cost', 'dir': 'desc'},
        'chart': {'group': 'category', 'measures': ['cost'], 'kind': 'bar'},
        'filters': ['period', 'project', 'status'],
        'status_field': 'status',
        'project_field': 'project',
        'kpis': _k_expenses,
    },
    {
        'id': 'expense_claims',
        'noun': 'Claims',
        'category': 'financial',
        'label': 'Expense claims',
        'description': 'Employee claims with approval and reimbursement status.',
        'perms': ['finance.view', 'expenses.approve', 'expenses.pay'],
        'source': 'expenses',
        'date_field': 'spent_on',
        'date_label': 'Spent on',
        'default_period': 'this_quarter',
        'pick': ['person', 'project', 'category', 'description', 'amount', 'spent_on', 'status', 'approver', 'in_budget', 'billable', 'paid_on'],
        'default_fields': ['spent_on', 'person', 'category', 'description', 'project', 'amount', 'status'],
        'measures': ['amount'],
        'groups': ['category', 'person', 'project', 'status', 'spent_on:month'],
        'default_group': None,
        'default_sort': {'field': 'spent_on', 'dir': 'desc'},
        'chart': {'group': 'category', 'measures': ['amount'], 'kind': 'bar'},
        'filters': ['period', 'project', 'team', 'person', 'status'],
        'status_field': 'status',
        'person_field': 'person',
        'project_field': 'project',
        'kpis': _k_claims,
    },
    # ---------------- Attendance
    {
        'id': 'attendance',
        'noun': 'People',
        'category': 'attendance',
        'label': 'Attendance summary',
        'description': 'Per person: working days, days with time logged, approved leave and unaccounted days.',
        'perms': ['people.view_all', 'team.view', 'leave.approve'],
        'source': 'custom',
        'build': _attendance_rows,
        'period_rows': True,
        'date_field': None,
        'date_label': 'Working days',
        'default_period': 'this_month',
        'fields': [
            F('person', 'Person', 'string'),
            F('title', 'Title', 'string'),
            F('department', 'Department', 'enum', options=_opts('people', 'department')),
            F('working_days', 'Working days', 'number', unit='days', total='sum'),
            F('present_days', 'Days present', 'number', unit='days', total='sum'),
            F('leave_days', 'Leave days', 'number', unit='days', total='sum'),
            F('unaccounted_days', 'Unaccounted days', 'number', unit='days', total='sum'),
            F('attendance', 'Attendance', 'percent'),
            F('hours', 'Hours logged', 'number', unit='hours', total='sum'),
            F('avg_hours', 'Average hours a day', 'number', unit='hours'),
        ],
        'default_fields': ['person', 'department', 'working_days', 'present_days', 'leave_days', 'unaccounted_days', 'attendance', 'hours'],
        'measures': ['working_days', 'present_days', 'leave_days', 'unaccounted_days', 'hours'],
        'groups': ['department'],
        'default_group': None,
        'default_sort': {'field': 'attendance', 'dir': 'asc'},
        'chart': {'group': 'person', 'measures': ['present_days', 'leave_days', 'unaccounted_days'], 'kind': 'bar', 'stacked': True},
        'filters': ['period', 'team', 'person'],
        'person_field': 'person',
        'kpis': _k_att,
    },
    {
        'id': 'leave',
        'noun': 'Requests',
        'category': 'attendance',
        'label': 'Leave register',
        'description': 'Leave requests by person and type with their approval status.',
        'perms': ['people.view_all', 'team.view', 'leave.approve'],
        'source': 'leave_requests',
        'date_field': 'from_on',
        'date_label': 'Leave start',
        'default_period': 'this_fy',
        'pick': ['person', 'manager', 'type', 'from_on', 'to_on', 'days', 'status', 'approver', 'decided_by', 'reason', 'applied_on'],
        'default_fields': ['person', 'type', 'from_on', 'to_on', 'days', 'status', 'approver'],
        'measures': ['days'],
        'groups': ['type', 'person', 'status', 'from_on:month'],
        'default_group': None,
        'default_sort': {'field': 'from_on', 'dir': 'desc'},
        'chart': {'group': 'type', 'measures': ['days'], 'kind': 'bar', 'where': {'status': 'approved'}},
        'filters': ['period', 'team', 'person', 'status'],
        'status_field': 'status',
        'person_field': 'person',
        'kpis': _k_leave,
    },
    {
        'id': 'holidays',
        'noun': 'Holidays',
        'category': 'attendance',
        'label': 'Holiday calendar',
        'description': 'Company and optional holidays in the period, by month and weekday.',
        'perms': [],
        'source': 'custom',
        'build': lambda ctx, rng: _holiday_rows(ctx),
        'date_field': 'date',
        'date_label': 'Holiday date',
        'default_period': 'this_fy',
        'fields': [
            F('date', 'Date', 'date'),
            F('name', 'Holiday', 'string'),
            F('weekday', 'Weekday', 'string'),
            F('kind', 'Type', 'enum', options=[{'value': 'mandatory', 'label': 'Company holiday'}, {'value': 'optional', 'label': 'Optional'}]),
            F('calendar', 'Calendar', 'string'),
        ],
        'default_fields': ['date', 'name', 'weekday', 'kind', 'calendar'],
        'measures': [],
        'groups': ['kind', 'calendar', 'weekday', 'date:month'],
        'default_group': None,
        'default_sort': {'field': 'date', 'dir': 'asc'},
        'chart': {'group': 'date:month', 'measures': [], 'kind': 'column'},
        'filters': ['period', 'status'],
        'status_field': 'kind',
        'kpis': _k_hol,
    },
    # ---------------- Salary (salary access only)
    {
        'id': 'payroll_register',
        'noun': 'Payslips',
        'category': 'salary',
        'label': 'Payroll register',
        'description': 'Monthly CTC, base, variable, deductions and net pay per person (estimated from CTC where no run exists).',
        'perms': ['payroll.run', 'payroll.finalise'],
        'needs_salary': True,
        'source': 'payroll',
        'date_field': 'month',
        'date_label': 'Payroll month',
        'default_period': 'last_month',
        'pick': [
            'month',
            'person',
            'department',
            'role',
            'ctc',
            'base',
            'variable',
            'deduction',
            'net',
            'working_days',
            'unapproved_days',
            'lwp_days',
            'status',
        ],
        'default_fields': ['month', 'person', 'department', 'ctc', 'base', 'variable', 'deduction', 'net', 'lwp_days', 'status'],
        'measures': ['ctc', 'base', 'variable', 'deduction', 'net'],
        'groups': ['department', 'person', 'status', 'month:month'],
        'default_group': None,
        'default_sort': {'field': 'person', 'dir': 'asc'},
        'chart': {'group': 'month:month', 'measures': ['net', 'deduction'], 'kind': 'column', 'stacked': True},
        'filters': ['period', 'team', 'person', 'status'],
        'status_field': 'status',
        'person_field': 'person',
        'kpis': _k_payroll,
    },
    {
        'id': 'ctc_rates',
        'noun': 'People',
        'category': 'salary',
        'label': 'CTC & cost rates',
        'description': 'Current monthly and annual CTC and the cost per hour used for project costing.',
        'perms': ['people.view_all', 'team.view'],
        'needs_salary': True,
        'source': 'people',
        'date_field': None,
        'date_label': None,
        'default_period': 'all',
        'pick': ['name', 'title', 'role', 'department', 'manager', 'joined_on', 'ctc', 'annual_ctc', 'cost_per_hour'],
        'fields': [F('annual_ctc', 'Annual CTC', 'money', total='sum'), F('name', 'Person', 'string')],
        'derive': lambda ctx, r: {'annual_ctc': r['ctc'] * 12 if isinstance(r.get('ctc'), (int, float)) else None},
        'default_fields': ['name', 'title', 'department', 'joined_on', 'ctc', 'annual_ctc', 'cost_per_hour'],
        'measures': ['ctc', 'annual_ctc'],
        'groups': ['department', 'role', 'manager'],
        'default_group': None,
        'default_sort': {'field': 'ctc', 'dir': 'desc'},
        'chart': {'group': 'department', 'measures': ['ctc'], 'kind': 'bar'},
        'filters': ['team', 'person'],
        'person_field': 'name',
        'kpis': _k_ctc,
    },
    # ---------------- Task
    {
        'id': 'tasks',
        'noun': 'Tasks',
        'category': 'task',
        'label': 'Task register',
        'description': 'Tasks with owner, dates, status, estimate and actual hours.',
        'perms': [],
        'source': 'tasks',
        'date_field': 'due_on',
        'date_label': 'Due date',
        'default_period': 'this_month',
        'pick': [
            'code',
            'title',
            'project',
            'client',
            'milestone',
            'owner',
            'status',
            'priority',
            'complexity',
            'start_on',
            'due_on',
            'done_on',
            'est',
            'actual',
            'effort_ratio',
            'days_overdue',
            'timeliness',
            'reviewer',
        ],
        'fields': [F('client', 'Customer', 'string')],
        'default_fields': ['code', 'title', 'project', 'owner', 'status', 'priority', 'due_on', 'est', 'actual'],
        'measures': ['est', 'actual'],
        'groups': ['status', 'owner', 'project', 'client', 'milestone', 'priority', 'complexity', 'due_on:week', 'due_on:month'],
        'default_group': None,
        'default_sort': {'field': 'due_on', 'dir': 'asc'},
        'chart': {'group': 'status', 'measures': [], 'kind': 'bar', 'order': ['todo', 'in_progress', 'blocked', 'in_review', 'done']},
        'filters': ['period', 'project', 'customer', 'team', 'person', 'status'],
        'status_field': 'status',
        'person_field': 'owner',
        'project_field': 'project',
        'customer_field': 'client',
        'kpis': _k_tasks,
    },
    {
        'id': 'task_delivery',
        'noun': 'Tasks',
        'category': 'task',
        'label': 'On-time delivery',
        'description': 'Tasks closed in the period: on time or late, lateness and effort ratio, by person or week.',
        'perms': [],
        'source': 'tasks',
        'where': lambda r: r.get('status') == 'done' and bool(r.get('done_on')),
        'date_field': 'done_on',
        'date_label': 'Done on',
        'default_period': 'last_90_days',
        'pick': [
            'code',
            'title',
            'project',
            'owner',
            'due_on',
            'done_on',
            'delivery',
            'on_time_rate',
            'timeliness',
            'variance_days',
            'est',
            'actual',
            'effort_ratio',
            'delay_reason',
            'quality_rating',
        ],
        'fields': [F('delivery', 'Delivery', 'enum', options=[{'value': 'on_time', 'label': 'On time'}, {'value': 'late', 'label': 'Late'}])],
        'derive': lambda ctx, r: {'delivery': 'late' if r.get('timeliness') == 'late' else 'on_time'},
        'default_fields': ['code', 'title', 'project', 'owner', 'due_on', 'done_on', 'delivery', 'on_time_rate', 'variance_days', 'effort_ratio'],
        'measures': ['est', 'actual'],
        'groups': ['owner', 'project', 'delivery', 'done_on:week', 'done_on:month'],
        'default_group': 'owner',
        'default_sort': {'field': 'count', 'dir': 'desc'},
        'chart': {'group': 'done_on:week', 'measures': [], 'kind': 'column', 'split': 'delivery'},
        'filters': ['period', 'project', 'team', 'person'],
        'person_field': 'owner',
        'project_field': 'project',
        'kpis': _k_delivery,
    },
    # ---------------- Project
    {
        'id': 'projects',
        'noun': 'Projects',
        'category': 'project',
        'label': 'Project portfolio',
        'description': 'Projects with status, health, progress, schedule index and commercials.',
        'perms': ['projects.view_all', 'projects.edit'],
        'source': 'projects',
        'date_field': 'start_on',
        'date_label': 'Project start',
        'default_period': 'all',
        'pick': [
            'code',
            'name',
            'client',
            'type',
            'status',
            'stream',
            'billing_model',
            'pm',
            'start_on',
            'end_on',
            'progress',
            'spi',
            'effort_ratio',
            'health',
            'contract_value',
            'invoiced',
            'collected',
            'outstanding',
            'est_hours',
            'actual_hours',
            'open_tasks',
            'overdue_tasks',
            'overdue_milestones',
        ],
        'fields': [F('client', 'Customer', 'string')],
        'default_fields': ['code', 'name', 'client', 'status', 'pm', 'end_on', 'progress', 'health', 'contract_value', 'overdue_tasks'],
        'measures': ['contract_value', 'invoiced', 'collected', 'outstanding', 'est_hours', 'actual_hours'],
        'groups': ['status', 'health', 'client', 'pm', 'type', 'stream', 'billing_model'],
        'default_group': None,
        'default_sort': {'field': 'end_on', 'dir': 'asc'},
        'chart': {'group': 'health', 'measures': [], 'kind': 'bar', 'order': ['on_track', 'at_risk', 'off_track']},
        'filters': ['period', 'project', 'customer', 'status'],
        'status_field': 'status',
        'project_field': 'name',
        'customer_field': 'client',
        'kpis': _k_projects,
    },
    {
        'id': 'milestones',
        'noun': 'Milestones',
        'category': 'project',
        'label': 'Milestone status',
        'description': 'Milestones with deadline, baseline slip, delivery state and billing.',
        'perms': ['projects.view_all', 'projects.edit'],
        'source': 'milestones',
        'date_field': 'planned_end',
        'date_label': 'Deadline',
        'default_period': 'this_quarter',
        'pick': [
            'label',
            'project',
            'client',
            'pm',
            'planned_start',
            'planned_end',
            'baseline_end',
            'slip_days',
            'state',
            'progress',
            'achieved_on',
            'billing_amount',
            'billing_status',
            'days_to_due',
        ],
        'fields': [F('client', 'Customer', 'string')],
        'default_fields': ['label', 'project', 'planned_end', 'baseline_end', 'slip_days', 'state', 'progress', 'billing_amount', 'billing_status'],
        'measures': ['billing_amount', 'slip_days'],
        'groups': ['state', 'project', 'client', 'billing_status', 'planned_end:month'],
        'default_group': None,
        'default_sort': {'field': 'planned_end', 'dir': 'asc'},
        'chart': {
            'group': 'state',
            'measures': [],
            'kind': 'bar',
            'order': ['achieved_on_time', 'achieved_late', 'on_track', 'not_started', 'at_risk', 'overdue'],
        },
        'filters': ['period', 'project', 'customer', 'status'],
        'status_field': 'state',
        'project_field': 'project',
        'customer_field': 'client',
        'kpis': _k_ms,
    },
    # ---------------- Timesheet
    {
        'id': 'hours',
        'noun': 'Entries',
        'category': 'timesheet',
        'label': 'Hours logged',
        'description': 'Time entries by person, project, customer or week — project work, untasked activities and overhead.',
        'perms': [],
        'source': 'hours',
        'actuals': True,
        'date_field': 'date',
        'date_label': 'Work date',
        'default_period': 'last_month',
        'pick': [
            'date',
            'person',
            'department',
            'project',
            'client',
            'task',
            'kind',
            'internal_code',
            'leave_type',
            'hours',
            'approved',
            'week_status',
            'cost',
        ],
        'fields': [
            F('client', 'Customer', 'string'),
            # v9 (D5): time on a project without a task is project work, not "Internal" overhead
            F(
                'kind',
                'Entry type',
                'enum',
                options=[
                    {'value': 'task', 'label': 'Project task'},
                    {'value': 'project_work', 'label': 'Project work (no task)'},
                    {'value': 'internal', 'label': 'Non-project activity'},
                    {'value': 'leave', 'label': 'Leave'},
                ],
            ),
        ],
        'derive': lambda ctx, r: {'kind': 'project_work'} if r.get('kind') == 'internal' and (r.get('internal_code') == 'project_work' or r.get('project')) else {},
        'default_fields': ['date', 'person', 'project', 'task', 'kind', 'hours', 'week_status'],
        'measures': ['hours', 'cost'],
        'groups': ['person', 'project', 'client', 'kind', 'internal_code', 'department', 'week_status', 'date:week', 'date:month'],
        'default_group': 'person',
        'default_sort': {'field': 'sum_hours', 'dir': 'desc'},
        'chart': {'group': 'date:week', 'measures': ['hours'], 'kind': 'column', 'split': 'kind'},
        'filters': ['period', 'project', 'customer', 'team', 'person', 'status'],
        'status_field': 'week_status',
        'person_field': 'person',
        'project_field': 'project',
        'customer_field': 'client',
        'kpis': _k_hours,
    },
    {
        'id': 'timesheet_compliance',
        'noun': 'People',
        'category': 'timesheet',
        'label': 'Timesheet compliance',
        'description': 'Completed weeks per person: submitted, approved, missing, and hours against the target.',
        'perms': ['people.view_all', 'team.view', 'approvals.act'],
        'source': 'custom',
        'build': _compliance_rows,
        'period_rows': True,
        'date_field': None,
        'date_label': 'Completed weeks',
        'default_period': 'last_90_days',
        'fields': [
            F('person', 'Person', 'string'),
            F('department', 'Department', 'enum', options=_opts('people', 'department')),
            F('weeks', 'Weeks expected', 'number', total='sum'),
            F('submitted', 'Submitted', 'number', total='sum'),
            F('approved', 'Approved', 'number', total='sum'),
            F('missing', 'Missing', 'number', total='sum'),
            F('compliance', 'Compliance', 'percent'),
            F('hours', 'Hours logged', 'number', unit='hours', total='sum'),
            F('target_hours', 'Target hours', 'number', unit='hours', total='sum'),
            F('hours_pct', 'Hours vs target', 'percent'),
            F(
                'last_status',
                'Last week',
                'enum',
                options=[
                    {'value': v, 'label': lab}
                    for v, lab in (
                        ('open', 'Open'),
                        ('submitted', 'Submitted'),
                        ('approved', 'Approved'),
                        ('rejected', 'Rejected'),
                        ('locked', 'Locked'),
                        ('missing', 'Missing'),
                    )
                ],
            ),
        ],
        'default_fields': ['person', 'weeks', 'submitted', 'approved', 'missing', 'compliance', 'hours', 'target_hours', 'last_status'],
        'measures': ['weeks', 'submitted', 'missing', 'hours'],
        'groups': ['department', 'last_status'],
        'default_group': None,
        'default_sort': {'field': 'compliance', 'dir': 'asc'},
        'chart': {'group': '_week', 'measures': [], 'kind': 'column'},
        'filters': ['period', 'team', 'person'],
        'person_field': 'person',
        'kpis': _k_comp,
    },
]
TYPE_MAP = {t['id']: t for t in TYPES}


# ------------------------------------------------------------------ access
def denial(ctx: Ctx, t: dict) -> str | None:
    """Why this viewer can't run the type (None = allowed)."""
    if not has_any(ctx, 'reports.view', 'reports.build'):
        return 'Reports aren’t part of your role.'
    if t['perms'] and not has_any(ctx, *t['perms']):
        return f'Your role can’t run the {_lc(t["label"])} report. Ask an admin if you need it.'
    if t.get('needs_salary') and not can_see_salaries(ctx):
        return f'The {_lc(t["label"])} report shows salaries. It needs salary access (See salaries & cost rates).'
    if t.get('needs_margins') and not has(ctx, 'margins.view'):
        return f'The {_lc(t["label"])} report shows margins. It needs margin access (See project margins).'
    return None


def visible_types(ctx: Ctx) -> list[dict]:
    return [t for t in TYPES if denial(ctx, t) is None]


def require_type(ctx: Ctx, type_id: Any) -> dict:
    t = TYPE_MAP.get(type_id) if isinstance(type_id, str) else None
    if t is None:
        err(404, 'NOT_FOUND', 'That report type doesn’t exist.')
    why = denial(ctx, t)
    if why:
        err(403, 'FORBIDDEN', why)
    return t  # type: ignore[return-value]


# ------------------------------------------------------------------ fields
def _fields_of(ctx: Ctx, t: dict) -> list[dict]:
    """The type's selectable fields for this viewer (dataset fields the viewer may see + the type's own)."""
    own = {f['key']: f for f in t.get('fields', [])}
    if t['source'] == 'custom':
        return list(own.values())
    from app.routes.reports import visible_datasets

    ds = next((d for d in visible_datasets(ctx) if d['id'] == t['source']), None)
    base = {f['key']: f for f in (ds['fields'] if ds else [])}
    out = []
    for k in t['pick']:
        if k in base:
            f = {**base[k], **({'label': own[k]['label']} if k in own else {}), **({'options': own[k]['options']} if k in own and own[k].get('options') else {})}
            out.append(f)
        elif k in own and not _derived_hidden(k, base):
            out.append(own[k])
    for f in out:
        if f['key'] in _NOT_ADDITIVE:
            f.pop('total', None)
        elif f['type'] == 'money' and 'total' not in f:
            f['total'] = 'sum'
        elif (
            f.get('unit') in ('hours', 'days')
            and f['type'] == 'number'
            and 'total' not in f
            and f['key'] not in ('days_overdue', 'age_days', 'variance_days', 'days_to_due', 'slip_days')
        ):
            f['total'] = 'sum'
    return out


_NOT_ADDITIVE = frozenset({'cost_per_hour', 'priority', 'no'})


def _derived_hidden(k: str, base: dict) -> bool:
    """derived fields disappear when the field they come from is hidden for this viewer"""
    needs = {'planned_margin': 'planned_cost', 'annual_ctc': 'ctc', 'ageing': 'outstanding', 'delivery': 'timeliness'}
    return k in needs and needs[k] not in base


def _group_options(t: dict, fields: list[dict]) -> list[dict]:
    fm = {f['key']: f for f in fields}
    out = []
    for g in t['groups']:
        k, _, grain = g.partition(':')
        if k not in fm:
            continue
        out.append({'value': g, 'label': f'{fm[k]["label"]} by {GRAIN_LABEL[grain].lower()}' if grain else fm[k]['label']})
    return out


def _options(ctx: Ctx, t: dict, raw: list[dict], fields: list[dict]) -> dict[str, Any]:
    fm = {f['key']: f for f in fields}

    def distinct(k: str | None) -> list[str]:
        if not k:
            return []
        s = sorted({r.get(k) for r in raw if isinstance(r.get(k), str) and r.get(k) and r.get(k) != '—'}, key=str.lower)
        return s[:500]

    st = t.get('status_field')
    status_opts = (fm.get(st) or {}).get('options') if st else None
    if st and not status_opts:
        status_opts = [{'value': v, 'label': v} for v in distinct(st)]
    ff = t['filters']
    return {
        'project': distinct(t.get('project_field')) if 'project' in ff else [],
        'customer': distinct(t.get('customer_field')) if 'customer' in ff else [],
        'person': distinct(t.get('person_field')) if 'person' in ff else [],
        'team': [{'value': x['id'], 'label': x['name'], 'kind': x['kind']} for x in _team_rows(ctx)] if 'team' in ff else [],
        'status': status_opts or [] if 'status' in ff else [],
    }


def describe(ctx: Ctx, t: dict, with_options: bool = False) -> dict[str, Any]:
    fields = _fields_of(ctx, t)
    keys = {f['key'] for f in fields}
    out = {
        'id': t['id'],
        'category': t['category'],
        'label': t['label'],
        'description': t['description'],
        'filters': t['filters'],
        'date_label': t.get('date_label'),
        'has_period': 'period' in t['filters'],
        'default_period': t['default_period'],
        'fields': fields,
        'default_fields': [k for k in t['default_fields'] if k in keys],
        'groups': _group_options(t, fields),
        'default_group': t['default_group'] if t['default_group'] and t['default_group'].split(':')[0] in keys else None,
        'default_sort': t['default_sort'],
        'charts': list(CHARTS),
        'chart_by': chart_options(t, fields),
        'default_chart_by': t['chart']['group'],
        'noun': t.get('noun', 'Rows'),
        'needs_salary': bool(t.get('needs_salary')),
    }
    if with_options:
        raw, _ = _rows(ctx, t, None)
        out['options'] = _options(ctx, t, raw, fields)
    return out


def catalogue(ctx: Ctx) -> dict[str, Any]:
    if not has_any(ctx, 'reports.view', 'reports.build'):
        err(403, 'FORBIDDEN', 'Reports aren’t part of your role.')
    vis = visible_types(ctx)
    cats = []
    for c in CATEGORIES:
        mine = [t for t in vis if t['category'] == c['id']]
        locked = [t for t in TYPES if t['category'] == c['id'] and t not in vis]
        cats.append(
            {
                **c,
                'types': [{'id': t['id'], 'label': t['label'], 'description': t['description'], 'needs_salary': bool(t.get('needs_salary'))} for t in mine],
                'locked': [{'id': t['id'], 'label': t['label'], 'reason': denial(ctx, t)} for t in locked],
            }
        )
    return {'categories': cats, 'can_save': has(ctx, 'reports.build')}


# ------------------------------------------------------------------ run
def _range(ctx: Ctx, t: dict, period: Any) -> dict | None:
    fy = prop(ctx.tenant['settings'], 'company.fy_start_month') or 4
    p = period if isinstance(period, dict) else {}
    frm, to = p.get('from'), p.get('to')
    if isinstance(frm, str) and isinstance(to, str) and len(frm) == 10 and len(to) == 10:
        if to < frm:
            err(422, 'VAL_REPORT', 'The period ends before it starts. Pick an end date on or after the start date.', 'period')
        return {'from': frm, 'to': to, 'label': f'{_fmt_d(frm)} – {_fmt_d(to)}', 'preset': None}
    preset = p.get('preset') if p.get('preset') in PRESET_LABEL else t['default_period']
    if preset == 'all':
        return None
    r = preset_range(preset, ctx.today, fy, bool(t.get('actuals')))
    if r is None:
        return None
    return {'from': r['from'], 'to': r['to'], 'label': r['label'] + (' (to date)' if r.get('capped') and preset != 'ytd' else ''), 'preset': preset}


def _fmt_d(d: str) -> str:
    import datetime as _dt

    x = _dt.date.fromisoformat(d)
    return f'{x.day} {x.strftime("%b")} {x.year}'


def _rows(ctx: Ctx, t: dict, rng: dict | None) -> tuple[list[dict], list[dict]]:
    """(rows, fields) for this viewer — dataset rows are already scoped and grant-stripped by the builders."""
    fields = _fields_of(ctx, t)
    if t['source'] == 'custom':
        raw = t['build'](ctx, rng)
    else:
        from app.routes.reports import dataset_rows

        raw = [dict(r) for r in dataset_rows(ctx, t['source'])['rows']]
    if t.get('extra'):
        raw += t['extra'](ctx, rng)
    if t.get('where'):
        raw = [r for r in raw if t['where'](r)]
    if t.get('derive'):
        for r in raw:
            r.update(t['derive'](ctx, r))
    keys = {f['key'] for f in fields}
    for f in t.get('fields', []):
        if f['key'] not in keys and f['key'] in ('annual_ctc', 'planned_margin'):
            for r in raw:
                r[f['key']] = None
    return raw, fields


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return [x for x in (v if isinstance(v, list) else [v]) if isinstance(x, str) and x]


def _display(f: dict | None, v: Any) -> Any:
    if f is not None and f.get('type') == 'enum' and f.get('options') and v is not None:
        o = next((o for o in f['options'] if o['value'] == v), None)
        return o['label'] if o else v
    return v


def _cell(f: dict, v: Any) -> Any:
    if v is None or v == '':
        return None
    if f['type'] == 'enum':
        return _display(f, v)
    return v


def _agg_for(f: dict) -> str:
    return (
        'avg'
        if f['type'] == 'percent' or (f['key'] in ('effort_ratio', 'spi', 'variance_days', 'avg_hours', 'quality_rating', 'priority', 'cost_per_hour'))
        else 'sum'
    )


def _aggregate(rs: list[dict], key: str, how: str) -> Any:
    if how == 'count':
        return len(rs)
    if how == 'avg':
        v = _avg(rs, key)
        return round(v, 4) if v is not None else None
    s = _sum(rs, key)
    return round(s, 2) if isinstance(s, float) else s


def _group_key(r: dict, g: str, fm: dict, fy: int) -> tuple[str, str]:
    k, _, grain = g.partition(':')
    v = r.get(k)
    if grain:
        b = bucket_of(v[:10], grain, fy) if isinstance(v, str) and v else None
        return (b['key'], b['label']) if b else ('~', 'No date')
    if v is None or v == '' or v == '—':
        return ('~', '(none)')
    if isinstance(v, bool):
        return (str(v), 'Yes' if v else 'No')
    return (str(v), str(_display(fm.get(k), v)))


def _sort(rows_: list[dict], sort: dict | None, keys: set) -> list[dict]:
    if not sort or sort.get('field') not in keys:
        return rows_
    k = sort['field']
    rev = sort.get('dir') == 'desc'
    present = [r for r in rows_ if r.get(k) is not None]
    missing = [r for r in rows_ if r.get(k) is None]

    def key(r: dict) -> Any:
        v = r.get(k)
        return (0, v) if isinstance(v, (int, float)) and not isinstance(v, bool) else (1, str(v).lower())

    return sorted(present, key=key, reverse=rev) + missing


def chart_options(t: dict, fields: list[dict]) -> list[dict]:
    """what the chart can be drawn by: the type's default view first, then every group-by option"""
    opts = []
    spec = t['chart']
    fm = {f['key']: f for f in fields}
    if spec['group'] == '_week':
        opts.append({'value': '_week', 'label': 'Week'})
    for o in _group_options(t, fields):
        opts.append(o)
    if spec['group'] not in [o['value'] for o in opts] and spec['group'].split(':')[0] in fm:
        k, _, grain = spec['group'].partition(':')
        opts.insert(0, {'value': spec['group'], 'label': f'{fm[k]["label"]} by {GRAIN_LABEL[grain].lower()}' if grain else fm[k]['label']})
    return opts


def _chart(t: dict, cur: list[dict], fm: dict, fy: int, cfg_chart: str, chart_by: str) -> dict | None:
    if cfg_chart == 'none':
        return None
    spec = t['chart']
    native = chart_by == spec['group']
    g = chart_by
    split = spec.get('split') if native else None
    ms = [m for m in spec['measures'] if m in fm][:3]
    agg = spec.get('agg')
    noun = t.get('noun', 'Rows')
    src = cur
    if spec.get('where') and native:
        src = [r for r in cur if all(r.get(k) == v for k, v in spec['where'].items())]
    if g == '_week':  # compliance: share of people with each completed week submitted
        weeks: dict[str, list] = {}
        for r in src:
            for w, st in r.get('_weeks', []):
                weeks.setdefault(w, []).append(st in ('submitted', 'approved'))
        cats = sorted(weeks)
        return {
            'kind': 'line' if cfg_chart == 'line' else 'column',
            'format': 'percent',
            'stacked': False,
            'x_label': 'Week',
            'y_label': 'Compliance',
            'categories': [f'{w[8:10]}/{w[5:7]}' for w in cats],
            'series': [{'key': 'compliance', 'label': 'Weeks submitted', 'values': [round(sum(weeks[w]) / len(weeks[w]), 4) for w in cats]}],
            'refs': [{'value': 0.9, 'label': '90% target'}],
            'subtitle': 'Share of people with each completed week submitted or approved (%).',
        }
    k, _, grain = g.partition(':')
    if k not in fm:
        return None
    groups: dict[str, dict] = {}
    for r in src:
        key, label = _group_key(r, g, fm, fy)
        x = groups.setdefault(key, {'label': label, 'rows': []})
        x['rows'].append(r)
    is_time = bool(grain)
    keys = list(groups)
    ordered = bool(spec.get('order')) and native
    if is_time:
        keys.sort()
    elif ordered:
        order = {v: i for i, v in enumerate(spec['order'])}
        keys.sort(key=lambda x: order.get(x, 99))
    if split and split in fm:
        sopts = fm[split].get('options') or []
        svals = [o['value'] for o in sopts] or sorted({str(r.get(split)) for r in src})
        series = []
        for sv in svals:
            vals = []
            for x in keys:
                rs = [r for r in groups[x]['rows'] if r.get(split) == sv]
                vals.append(_sum(rs, ms[0]) if ms else len(rs))
            series.append({'key': sv, 'label': str(_display(fm[split], sv)), 'values': vals})
        series = [x for x in series if any(x['values'])]
        unit_f = fm.get(ms[0]) if ms else None
        fmt = _fmt_of(unit_f) if unit_f else 'number'
        stacked = True
        y = unit_f['label'] if unit_f else noun
        what = f'{y} by {fm[split]["label"].lower()}'
    else:
        if ms:
            series = [{'key': m, 'label': fm[m]['label'], 'values': [_aggregate(groups[x]['rows'], m, agg or _agg_for(fm[m])) for x in keys]} for m in ms]
            fmt = _fmt_of(fm[ms[0]])
            y = fm[ms[0]]['label'] if len(ms) == 1 else ('₹' if fmt == 'money' else _unit_word(fmt) or fm[ms[0]]['label'])
            what = ', '.join(x['label'] for x in series)
        else:
            series = [{'key': 'count', 'label': noun, 'values': [len(groups[x]['rows']) for x in keys]}]
            fmt = 'number'
            y = noun
            what = noun
        stacked = bool(spec.get('stacked')) and native
    if not is_time and not ordered:
        # entities: biggest first, top 12 (the tail folds into "Other" for additive series)
        tot = [sum(abs(x['values'][i] or 0) for x in series) for i in range(len(keys))]
        idx = sorted(range(len(keys)), key=lambda i: -tot[i])
        if len(idx) > 12:
            keep, rest = idx[:11], idx[11:]
            additive = (all(_agg_for(fm[m]) == 'sum' for m in ms) if ms else True) and not agg
            for x in series:
                x['values'] = [x['values'][i] for i in keep] + ([sum((x['values'][i] or 0) for i in rest)] if additive else [])
            cats = [groups[keys[i]]['label'] for i in keep] + ([f'Other ({len(rest)})'] if additive else [])
        else:
            for x in series:
                x['values'] = [x['values'][i] for i in idx]
            cats = [groups[keys[i]]['label'] for i in idx]
    else:
        cats = [groups[x]['label'] for x in keys]
    kind = cfg_chart if cfg_chart in ('bar', 'column', 'line', 'donut') else (spec['kind'] if native else 'column' if is_time else 'bar')
    if kind == 'line' and not is_time:
        kind = 'bar'
    if kind == 'donut' and (len(series) != 1 or fmt == 'percent' or any((v or 0) < 0 for v in series[0]['values'])):
        kind = 'bar'
    x_label = GRAIN_LABEL[grain] if grain else fm[k]['label']
    unit = _unit_word(fmt)
    return {
        'kind': kind,
        'format': fmt,
        'stacked': stacked,
        'x_label': x_label,
        'y_label': y,
        'categories': cats,
        'series': series,
        'subtitle': f'{what} per {x_label.lower()}' + (f' ({unit})' if unit and (fmt in ('money', 'percent') or unit != y.lower()) else '') + '.',
    }


def _unit_word(fmt: str) -> str:
    return {'money': '₹', 'hours': 'hours', 'percent': '%', 'days': 'days'}.get(fmt, '')


def _fmt_of(f: dict) -> str:
    if f['type'] == 'money':
        return 'money'
    if f['type'] == 'percent':
        return 'percent'
    if f.get('unit') == 'hours':
        return 'hours'
    if f.get('unit') == 'days':
        return 'days'
    if f['key'] == 'effort_ratio':
        return 'ratio'
    return 'number'


def _col(f: dict) -> dict[str, Any]:
    t = f['type']
    return {
        'key': f['key'],
        'label': f['label'],
        'type': 'hours' if f.get('unit') == 'hours' else 'days' if f.get('unit') == 'days' and t == 'number' else 'ratio' if f['key'] == 'effort_ratio' else t,
        'align': 'right' if t in ('money', 'number', 'percent') else 'left',
    }


def sanitize_config(ctx: Ctx, t: dict, cfg: Any, fields: list[dict] | None = None) -> dict[str, Any]:
    c = cfg if isinstance(cfg, dict) else {}
    fields = fields if fields is not None else _fields_of(ctx, t)
    keys = [f['key'] for f in fields]
    sel = [k for k in (c.get('fields') if isinstance(c.get('fields'), list) else []) if k in keys]
    sel = list(dict.fromkeys(sel)) or [k for k in t['default_fields'] if k in keys]
    g = c.get('group_by', UNSET)
    if g is UNSET:
        g = t['default_group']
    valid_groups = {o['value'] for o in _group_options(t, fields)}
    group = g if isinstance(g, str) and g in valid_groups else None
    period = c.get('period') if isinstance(c.get('period'), dict) else {'preset': t['default_period']}
    flt_in = c.get('filters') if isinstance(c.get('filters'), dict) else {}
    filters = {k: _as_list(flt_in.get(k))[:200] for k in ('project', 'customer', 'team', 'person', 'status') if k in t['filters'] and _as_list(flt_in.get(k))}
    s = c.get('sort') if isinstance(c.get('sort'), dict) else None
    sort = {'field': s.get('field'), 'dir': 'asc' if s.get('dir') == 'asc' else 'desc'} if s and isinstance(s.get('field'), str) else None
    if sort is None and not group:
        sort = t['default_sort'] if t['default_sort']['field'] in keys else None
    chart = c.get('chart') if c.get('chart') in CHARTS else 'auto'
    cb = c.get('chart_by')
    chart_by = cb if isinstance(cb, str) and cb in {o['value'] for o in chart_options(t, fields)} else t['chart']['group']
    return {
        'fields': sel,
        'period': {k: period[k] for k in ('preset', 'from', 'to') if isinstance(period.get(k), str)},
        'filters': filters,
        'group_by': group,
        'sort': sort,
        'chart': chart,
        'chart_by': chart_by,
    }


UNSET = object()


def run(ctx: Ctx, type_id: Any, cfg_in: Any) -> dict[str, Any]:
    t = require_type(ctx, type_id)
    fields0 = _fields_of(ctx, t)
    cfg = sanitize_config(ctx, t, cfg_in, fields0)
    rng = _range(ctx, t, cfg['period']) if 'period' in t['filters'] else None
    raw, fields = _rows(ctx, t, rng)
    fm = {f['key']: f for f in fields}
    fy = prop(ctx.tenant['settings'], 'company.fy_start_month') or 4
    applied: list[dict] = []
    cur = raw
    df = t.get('date_field')
    if rng is not None:
        applied.append({'key': 'period', 'label': 'Period', 'value': rng['label'] + (f' · {t["date_label"].lower()}' if t.get('date_label') and df else '')})
        if df and not t.get('period_rows'):
            cur = [r for r in cur if isinstance(r.get(df), str) and rng['from'] <= r[df][:10] <= rng['to']]
    elif 'period' in t['filters']:
        applied.append({'key': 'period', 'label': 'Period', 'value': 'All time'})
    f = cfg['filters']
    if f.get('project') and t.get('project_field'):
        s = set(f['project'])
        cur = [r for r in cur if r.get(t['project_field']) in s]
        applied.append({'key': 'project', 'label': 'Project', 'value': ', '.join(f['project'])})
    if f.get('customer') and t.get('customer_field'):
        s = set(f['customer'])
        cur = [r for r in cur if r.get(t['customer_field']) in s]
        applied.append({'key': 'customer', 'label': 'Customer', 'value': ', '.join(f['customer'])})
    if f.get('team') and t.get('person_field'):
        teams = {x['id']: x for x in _team_rows(ctx)}
        names = set()
        labels = []
        for tid in f['team']:
            x = teams.get(tid)
            if x:
                labels.append(x['name'])
                for uid in x['members']:
                    names.add(user_name(ctx, uid))
        cur = [r for r in cur if r.get(t['person_field']) in names]
        applied.append({'key': 'team', 'label': 'Team', 'value': ', '.join(labels) or '—'})
    if f.get('person') and t.get('person_field'):
        s = set(f['person'])
        cur = [r for r in cur if r.get(t['person_field']) in s]
        applied.append({'key': 'person', 'label': 'Person', 'value': ', '.join(f['person'])})
    if f.get('status') and t.get('status_field'):
        s = set(f['status'])
        cur = [r for r in cur if str(r.get(t['status_field'])) in s]
        sf = fm.get(t['status_field'])
        applied.append({'key': 'status', 'label': sf['label'] if sf else 'Status', 'value': ', '.join(str(_display(sf, v)) for v in f['status'])})

    kpis, finding = t['kpis'](cur, ctx)
    if t.get('scope_kpis'):
        # V9-FIX (item 11): portfolio totals name the projects they cover (all statuses unless a status filter is set)
        sf = fm.get(t['status_field']) if t.get('status_field') else None
        scope_lbl = ', '.join(str(_display(sf, v)) for v in f['status']) if f.get('status') else 'All statuses'
        kpis = [{**k, 'label': f'{k["label"]} · {scope_lbl}'} if k.get('key') in t['scope_kpis'] else k for k in kpis]
    sel = [k for k in cfg['fields'] if k in fm]
    group = cfg['group_by']
    measures = [k for k in sel if fm[k]['type'] in ('money', 'number', 'percent') and k != group] if group else []
    if group:
        gk = group.split(':')[0]
        meas = measures or [m for m in t['measures'] if m in fm][:3]
        groups: dict[str, dict] = {}
        for r in cur:
            key, label = _group_key(r, group, fm, fy)
            g = groups.setdefault(key, {'key': key, 'label': label, 'rows': []})
            g['rows'].append(r)
        gl = group.partition(':')
        glabel = f'{fm[gk]["label"]} ({GRAIN_LABEL[gl[2]].lower()})' if gl[2] else fm[gk]['label']
        columns = [
            {'key': gk, 'label': glabel, 'type': 'string', 'align': 'left'},
            {'key': 'count', 'label': t.get('noun', 'Rows'), 'type': 'number', 'align': 'right'},
        ]
        for m in meas:
            how = _agg_for(fm[m])
            col = _col(fm[m])
            if how == 'avg' and fm[m]['type'] != 'percent':
                col['label'] = f'Average {col["label"][0].lower()}{col["label"][1:]}'
            columns.append({**col, 'key': f'{how}_{m}', 'agg': how})
        out_rows = []
        for g in groups.values():
            row = {'_key': g['key'], gk: g['label'], 'count': len(g['rows'])}
            for m in meas:
                how = _agg_for(fm[m])
                row[f'{how}_{m}'] = _aggregate(g['rows'], m, how)
            out_rows.append(row)
        totals: dict[str, Any] = {'count': len(cur)}
        for m in meas:
            how = _agg_for(fm[m])
            totals[f'{how}_{m}'] = _aggregate(cur, m, how)
        if ':' in group and not cfg['sort']:
            out_rows.sort(key=lambda r: r['_key'])
        else:
            srt = cfg['sort'] or {'field': columns[2]['key'] if len(columns) > 2 else 'count', 'dir': 'desc'}
            if srt['field'] in meas:  # a raw field key sorts by its aggregate column
                srt = {**srt, 'field': f'{_agg_for(fm[srt["field"]])}_{srt["field"]}'}
            if srt['field'] == gk:
                out_rows.sort(key=lambda r: (r['_key'] == '~', str(r[gk]).lower()), reverse=srt['dir'] == 'desc')
            else:
                out_rows = _sort(out_rows, srt, {c['key'] for c in columns})
        for r in out_rows:
            r.pop('_key', None)
        row_count = len(out_rows)
    else:
        columns = [_col(fm[k]) for k in sel]
        listed = _sort(list(cur), cfg['sort'], set(fm))
        out_rows = [{k: _cell(fm[k], r.get(k)) for k in sel} for r in listed[:MAX_TABLE_ROWS]]
        totals = {}
        for k in sel:
            tot = fm[k].get('total')
            if tot == 'sum':
                totals[k] = _aggregate(cur, k, 'sum')
        row_count = len(cur)
    chart = _chart(t, cur, fm, fy, cfg['chart'], cfg['chart_by'])
    if chart is not None:
        chart['title'] = finding
        if not cur:
            chart['empty'] = 'No rows match these filters.'
    warnings = []
    if not group and row_count > MAX_TABLE_ROWS:
        warnings.append(
            f'The table shows the first {MAX_TABLE_ROWS:,} of {row_count:,} rows; totals, KPIs and the chart include all of them. Export to get every row, or group the report.'
        )
    return {
        'type': t['id'],
        'category': t['category'],
        'category_label': next(c['label'] for c in CATEGORIES if c['id'] == t['category']),
        'title': t['label'],
        'description': t['description'],
        'config': cfg,
        'period': {'from': rng['from'], 'to': rng['to'], 'label': rng['label']} if rng else None,
        'filters_applied': applied,
        'finding': finding,
        'kpis': kpis,
        'chart': chart,
        'grouped': bool(group),
        'columns': columns,
        'rows': out_rows,
        'totals': totals,
        'row_count': row_count,
        'source_rows': len(cur),
        'generated_at': now_iso(),
        'generated_by': ctx.user.get('name') if isinstance(ctx.user, dict) else None,
        'warnings': warnings,
    }


def preview(ctx: Ctx, type_id: str, cfg: Any) -> dict[str, Any] | None:
    """Mini chart for a saved typed report card (same shape as reports._preview_of)."""
    try:
        res = run(ctx, type_id, cfg)
    except Exception:  # noqa: BLE001 — a card never fails the list
        return None
    k0 = res['kpis'][0] if res['kpis'] else None
    ch = res['chart']
    fmt = (ch or {}).get('format') or (k0 or {}).get('format') or 'number'
    if ch and ch['series']:
        return {
            'labels': ch['categories'][:12],
            'values': [v or 0 for v in ch['series'][0]['values'][:12]],
            'kind': 'line' if ch['kind'] == 'line' else 'pie' if ch['kind'] == 'donut' else 'hbar' if ch['kind'] == 'bar' else 'bar',
            'value': k0['value'] if k0 else None,
            'format': fmt if fmt in ('money', 'percent', 'hours', 'number') else 'number',
        }
    return {
        'labels': [],
        'values': [],
        'kind': 'kpi',
        'value': k0['value'] if k0 else res['row_count'],
        'format': (k0 or {}).get('format', 'number') if (k0 or {}).get('format') in ('money', 'percent', 'hours', 'number') else 'number',
    }


def category_label(cid: str) -> str:
    return next((c['label'] for c in CATEGORIES if c['id'] == cid), cid)
