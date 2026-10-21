"""Port of src/mock/routes/finance.ts — Module D finance: ledger (UI-31), profit & loss with runway. (The invoice
register + payments live in invoices.py.) Export: monthly_pnl, INCOME_CATEGORIES, EXPENSE_CATEGORIES."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, Req, err, find, has, insert, log_activity, remove, require_action, rows, update
from app.domain.calendar import add_days, add_months, diff_days, fy_label, fy_range, fy_start_year, month_of, month_range
from app.domain.labels import DEAL_STAGE, STREAM
from app.domain.money import fmt_money
from app.domain.sales import deal_value
from app.engine.analytics import PAYROLL_CATEGORIES, expand_ledger, payroll_for_month, revenue_between
from app.js import (
    UNDEF,
    coalesce,
    fmt_date_en_in,
    is_integer,
    js_len,
    js_lower,
    js_round,
    js_sort,
    js_str,
    js_trim,
    js_truthy,
    locale_compare,
    locale_key,
    prop,
)

__all__ = ['INCOME_CATEGORIES', 'EXPENSE_CATEGORIES', 'monthly_pnl', 'register']

_C = cat('mock/routes/finance')
INCOME_CATEGORIES = _C['INCOME_CATEGORIES']
EXPENSE_CATEGORIES = _C['EXPENSE_CATEGORIES']
DIRECT = frozenset({'Salaries', 'Subcontractors', 'Contractors', 'Cost of delivery'})
_DATE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')


def _date_ok(v: Any) -> bool:
    """DATE.test(v) (String(v))"""
    return _DATE.search(v if isinstance(v, str) else js_str(v)) is not None


def _month_label(ym: str) -> str:
    return fmt_date_en_in(ym[:7] + '-01', month='short', year='2-digit')


def _settings(ctx: Ctx) -> Any:
    return ctx.tenant.get('settings')


def _range(ctx: Ctx, query: dict) -> dict[str, str]:
    d = month_range(month_of(ctx.today))
    qf = js_str(coalesce(query.get('from', UNDEF), ''))
    qt = js_str(coalesce(query.get('to', UNDEF), ''))
    frm = qf if _date_ok(qf) else d['from']
    to = qt if _date_ok(qt) else d['to']
    if to < frm:
        err(422, 'DATE_RANGE', 'The end date must be on or after the start date.')
    return {'from': frm, 'to': to}


def _in(b: Any, k: str) -> bool:
    """`k in b`"""
    return isinstance(b, dict) and k in b


def _bget(b: Any, k: str) -> Any:
    return b.get(k, UNDEF) if isinstance(b, dict) else UNDEF


def _validate_ledger(b: Any, creating: bool) -> None:
    g = _bget(b, 'kind')
    if (creating or _in(b, 'kind')) and g != 'income' and g != 'expense':
        err(422, 'VALIDATION', 'Choose Income or Expense.', 'kind')
    g = _bget(b, 'category')
    if (creating or _in(b, 'category')) and (not js_truthy(g) or not js_trim(js_str(g))):
        err(422, 'VALIDATION', 'Choose a category.', 'category')
    g = _bget(b, 'description')
    if (creating or _in(b, 'description')) and (not js_truthy(g) or js_len(js_trim(js_str(g))) < 3):
        err(422, 'VALIDATION', 'Describe the entry (at least 3 characters).', 'description')
    g = _bget(b, 'amount_paise')
    if (creating or _in(b, 'amount_paise')) and (isinstance(g, bool) or not isinstance(g, (int, float)) or not is_integer(g) or g <= 0):
        err(422, 'MONEY_INVALID', 'Enter an amount above zero.', 'amount_paise')
    g = _bget(b, 'date')
    if (creating or _in(b, 'date')) and (not js_truthy(g) or not _date_ok(g)):
        err(422, 'VALIDATION', 'Pick a date.', 'date')
    g = _bget(b, 'until_on')
    if _in(b, 'until_on') and js_truthy(g) and not _date_ok(g):
        err(422, 'VALIDATION', 'Pick a valid end date.', 'until_on')
    g = _bget(b, 'status')
    if _in(b, 'status') and js_truthy(g) and g not in ('expected', 'received', 'paid'):
        err(422, 'VALIDATION', 'Choose a status.', 'status')
    g = _bget(b, 'stream')
    if _in(b, 'stream') and js_truthy(g) and js_str(g) not in STREAM:
        err(422, 'VALIDATION', 'Choose a revenue stream.', 'stream')


def _cash_position(ctx: Ctx) -> dict[str, Any]:
    """Cash on hand today = opening cash + money actually received − money actually paid."""
    opening = coalesce(prop(_settings(ctx), 'finance.opening_cash_paise'), 0)
    start = ctx.today
    for e in rows(ctx, 'ledger_entries'):
        if e['date'] < start:
            start = e['date']
    inflow: Any = 0
    outflow: Any = 0
    for e in expand_ledger(ctx, start, ctx.today):
        if e['kind'] == 'income' and not js_truthy(e.get('invoice_id')) and e.get('status') == 'received':
            inflow += e['amount_paise']
        if e['kind'] == 'expense' and e.get('status') == 'paid':
            outflow += e['amount_paise']
    for p in rows(ctx, 'payments'):
        if p['paid_on'] <= ctx.today:
            inflow += p['amount_paise']
    return {'opening': opening, 'balance': opening + inflow - outflow}


def monthly_pnl(ctx: Ctx, ym: str) -> dict[str, Any]:
    """G13.01 one month: billed, collected, payroll, other costs (non-salary expenses), net = billed − costs."""
    mr = month_range(ym)
    x = revenue_between(ctx, mr['from'], mr['to'])
    pay = payroll_for_month(ctx, ym)
    return {
        'month': ym,
        'label': _month_label(ym),
        'billed': x['billed'],
        'collected': x['collected'],
        'payroll': x['payroll'],
        'other_costs': x['other_costs'],
        'net': x['net'],
        'headcount': pay['headcount'],
        'to_date': mr['to'] > ctx.today,
    }


def _sales_pipeline(ctx: Ctx) -> dict[str, Any]:
    open_ = [d for d in rows(ctx, 'deals') if d['stage'] != 'won' and d['stage'] != 'lost']
    ov: Any = 0
    wv: Any = 0
    for d in open_:
        ov += deal_value(d)
    for d in open_:
        wv += deal_value(d) * DEAL_STAGE[d['stage']]['chance']
    return {'open_value': ov, 'weighted_value': js_round(wv)}


def _by_date_desc_kind(a: dict, b: dict) -> int:
    return locale_compare(b['date'], a['date']) or locale_compare(a['kind'], b['kind'])


# ---------------- Ledger ----------------
def _ledger(req: Req) -> Any:
    ctx = req.ctx
    query = req.query
    require_action(ctx, 'ledger.view', 'The ledger is visible to admins and operations.')
    rg = _range(ctx, query)
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    items = []
    for e in expand_ledger(ctx, rg['from'], rg['to']):
        pid = e.get('project_id')
        p = projects.get(pid) if js_truthy(pid) else None
        items.append(
            {
                'key': f'{e["id"]}:{e["occurrence"]}',
                'id': e['id'],
                'version': e['version'],
                'kind': e['kind'],
                'category': e['category'],
                'description': e['description'],
                'amount_paise': e['amount_paise'],
                'date': e['occurrence'],
                'origin_date': e['date'],
                'recurring': e.get('recurring', UNDEF),
                'until_on': e.get('until_on', UNDEF),
                'status': e.get('status', UNDEF),
                'project_id': e.get('project_id', UNDEF),
                'project_name': coalesce(p.get('name', UNDEF) if p else UNDEF, None) if js_truthy(pid) else None,
                'project_code': coalesce(p.get('code', UNDEF) if p else UNDEF, None) if js_truthy(pid) else None,
                'invoice_id': e.get('invoice_id', UNDEF),
                'stream': e.get('stream', UNDEF),
                'locked': e.get('locked', UNDEF),
            }
        )
    q = js_lower(js_trim(js_str(coalesce(query.get('q', UNDEF), ''))))
    for k in ('kind', 'category', 'status', 'project_id', 'stream'):
        qv = query.get(k, UNDEF)
        if js_truthy(qv):
            items = [x for x in items if x[k] == qv]
    if query.get('recurring', UNDEF) == 'true':
        items = [x for x in items if js_truthy(x['recurring'])]
    if q:
        items = [x for x in items if q in js_lower(f'{js_str(x["description"])} {js_str(x["category"])} {js_str(coalesce(x["project_name"], ""))}')]
    items = js_sort(items, _by_date_desc_kind)
    t = {'income': 0, 'expense': 0, 'net': 0, 'received': 0, 'paid': 0, 'expected_in': 0, 'expected_out': 0, 'count': len(items)}
    for x in items:
        if x['kind'] == 'income':
            t['income'] += x['amount_paise']
            if x['status'] == 'received':
                t['received'] += x['amount_paise']
            else:
                t['expected_in'] += x['amount_paise']
        else:
            t['expense'] += x['amount_paise']
            if x['status'] == 'paid':
                t['paid'] += x['amount_paise']
            else:
                t['expected_out'] += x['amount_paise']
    t['net'] = t['income'] - t['expense']
    all_ = rows(ctx, 'ledger_entries')

    def cats(k: str, base: Any) -> list:
        seen: list = []
        for c in [*base, *(e['category'] for e in all_ if e['kind'] == k)]:
            if c not in seen:
                seen.append(c)
        return js_sort(seen)

    return {
        'from': rg['from'],
        'to': rg['to'],
        'items': items,
        'totals': t,
        'categories': {'income': cats('income', INCOME_CATEGORIES), 'expense': cats('expense', EXPENSE_CATEGORIES)},
        'can_edit': has(ctx, 'finance.edit'),
    }


def _ledger_entries(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'ledger.view', 'The ledger is visible to admins and operations.')
    fm = prop(_settings(ctx), 'company.fy_start_month')
    fy = fy_start_year(ctx.today) if fm is UNDEF else fy_start_year(ctx.today, fm)
    fr = fy_range(fy) if fm is UNDEF else fy_range(fy, fm)
    scope_ = 'all' if req.q('scope') == 'all' else 'fy'
    all_ = rows(ctx, 'ledger_entries')
    frm = fr['from']
    if scope_ == 'all':
        for e in all_:
            if e['date'] < frm:
                frm = e['date']
    to = fr['to']
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    occ: dict[Any, dict] = {}
    in_scope = expand_ledger(ctx, frm, to)
    for e in in_scope:
        c = occ.setdefault(e['id'], {'n': 0, 'amt': 0})
        c['n'] += 1
        c['amt'] += e['amount_paise']

    def manual(e: dict) -> bool:
        return not js_truthy(e.get('locked')) and not js_truthy(e.get('invoice_id')) and e.get('category') not in PAYROLL_CATEGORIES

    items = []
    for e in all_:
        rec = js_truthy(e.get('recurring'))
        if not (manual(e) and (e['id'] in occ or (rec and e['date'] > ctx.today and e['date'] <= to))):
            continue
        until = e.get('until_on')
        run_state = None if not rec else 'upcoming' if e['date'] > ctx.today else 'ended' if js_truthy(until) and until < ctx.today else 'running'
        pid = e.get('project_id')
        p = projects.get(pid) if js_truthy(pid) else None
        o = occ.get(e['id'])
        items.append(
            {
                'id': e['id'],
                'version': e['version'],
                'kind': e['kind'],
                'category': e['category'],
                'description': e['description'],
                'amount_paise': e['amount_paise'],
                'date': e['date'],
                'recurring': e.get('recurring', UNDEF),
                'until_on': e.get('until_on', UNDEF),
                'status': e.get('status', UNDEF),
                'run_state': run_state,
                'stream': e.get('stream', UNDEF),
                'project_id': e.get('project_id', UNDEF),
                'project_code': coalesce(p.get('code', UNDEF) if p else UNDEF, None) if js_truthy(pid) else None,
                'booked_paise': o['amt'] if o else 0,
                'months': o['n'] if o else 0,
            }
        )
    items = js_sort(items, _by_date_desc_kind)

    def group(key: str, label: str, f: Any) -> dict[str, Any]:
        xs = [e for e in in_scope if f(e)]
        s: Any = 0
        for e in xs:
            s += e['amount_paise']
        return {'key': key, 'label': label, 'count': len(xs), 'amount': s}

    return {
        'scope': scope_,
        'fy_label': fy_label(fy),
        'from': frm,
        'to': to,
        'items': items,
        'automatic': [
            group('milestone', 'Milestone invoices (income added automatically)', lambda e: js_truthy(e.get('invoice_id'))),
            group('spend', 'Paid expense claims and vendor bills', lambda e: js_truthy(e.get('locked')) and not js_truthy(e.get('invoice_id'))),
            group('payroll', 'Monthly salaries', lambda e: not js_truthy(e.get('locked')) and e.get('category') in PAYROLL_CATEGORIES),
        ],
        'can_edit': has(ctx, 'finance.edit'),
    }


def _trim_str(v: Any) -> str:
    """v!.trim() on a value validation let through (a non-string throws like the TS)"""
    if not isinstance(v, str):
        raise TypeError(f'{js_str(v)}.trim is not a function')
    return js_trim(v)


def _create(req: Req) -> Any:
    ctx = req.ctx
    b = req.body
    require_action(ctx, 'finance.edit', 'Only an admin can add ledger entries.')
    _validate_ledger(b, True)
    recurring = js_truthy(_bget(b, 'recurring'))
    until = _bget(b, 'until_on')
    if recurring and js_truthy(until) and until < b['date']:
        err(422, 'DATE_RANGE', 'The repeat end date must be on or after the first date.', 'until_on')
    pid = _bget(b, 'project_id')
    if js_truthy(pid):
        find(ctx, 'projects', pid, 'Project')
    status = _bget(b, 'status')
    if status is UNDEF or status is None:
        status = ('received' if b['kind'] == 'income' else 'paid') if b['date'] <= ctx.today else 'expected'
    if b['kind'] == 'income' and status == 'paid':
        err(422, 'VALIDATION', 'Income is either Expected or Received.', 'status')
    if b['kind'] == 'expense' and status == 'received':
        err(422, 'VALIDATION', 'An expense is either Expected or Paid.', 'status')
    stream = _bget(b, 'stream')
    e = insert(
        ctx,
        'ledger_entries',
        {
            'kind': b['kind'],
            'category': _trim_str(b['category']),
            'description': _trim_str(b['description']),
            'amount_paise': b['amount_paise'],
            'date': b['date'],
            'recurring': recurring,
            'until_on': (until if js_truthy(until) else None) if recurring else None,
            'status': status,
            'project_id': pid if js_truthy(pid) else None,
            'milestone_id': None,
            'invoice_id': None,
            'stream': (stream if js_truthy(stream) else None) if b['kind'] == 'income' else None,
            'locked': False,
        },
    )
    log_activity(ctx, 'ledger', e['id'], 'create', f'Added {e["kind"]} {fmt_money(e["amount_paise"])} · {e["description"]}{" (monthly)" if recurring else ""}')
    return e


_LOCKED_KEYS = ('kind', 'amount_paise', 'date', 'status', 'recurring', 'project_id')
_PATCH_KEYS = ('kind', 'category', 'description', 'amount_paise', 'date', 'recurring', 'until_on', 'status', 'project_id', 'stream')


def _patch(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', 'Only an admin can edit ledger entries.')
    e = find(ctx, 'ledger_entries', req.params['id'], 'Ledger entry')
    body = req.body
    b = {k: v for k, v in body.items() if k not in ('id', 'tenant_id', 'invoice_id', 'milestone_id', 'locked', 'version', 'created_at')} if isinstance(body, dict) else {}
    if js_truthy(e.get('locked')):
        blocked = [k for k in _LOCKED_KEYS if k in b and b[k] != e.get(k, UNDEF)]
        if blocked:
            err(409, 'LEDGER_LOCKED', 'This row comes from a milestone invoice. Change the amount, date or payment on the invoice instead.')
    _validate_ledger(b, False)
    nxt = {**e, **b}
    nu = nxt.get('until_on')
    if js_truthy(nxt.get('recurring')) and js_truthy(nu) and nu < nxt['date']:
        err(422, 'DATE_RANGE', 'The repeat end date must be on or after the first date.', 'until_on')
    if nxt.get('kind') == 'income' and nxt.get('status') == 'paid':
        err(422, 'VALIDATION', 'Income is either Expected or Received.', 'status')
    if nxt.get('kind') == 'expense' and nxt.get('status') == 'received':
        err(422, 'VALIDATION', 'An expense is either Expected or Paid.', 'status')
    if js_truthy(b.get('project_id')):
        find(ctx, 'projects', b['project_id'], 'Project')
    changes: dict[str, Any] = {}
    for k in _PATCH_KEYS:
        if k in b:
            v = b[k]
            changes[k] = (js_trim(v) or None) if isinstance(v, str) else v
    if 'recurring' in changes and changes['recurring'] is False:
        changes['until_on'] = None
    if nxt.get('kind') == 'expense':
        changes['stream'] = None
    u = update(ctx, 'ledger_entries', e['id'], changes, req.if_match)
    log_activity(ctx, 'ledger', e['id'], 'update', f'Edited {js_str(u.get("kind", UNDEF))} · {js_str(u.get("description", UNDEF))}')
    return u


def _delete(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', 'Only an admin can delete ledger entries.')
    e = find(ctx, 'ledger_entries', req.params['id'], 'Ledger entry')
    if js_truthy(e.get('locked')) or js_truthy(e.get('invoice_id')):
        err(409, 'LEDGER_LOCKED', 'This row was created by a milestone invoice and cannot be deleted. Adjust the invoice from the project instead.')
    remove(ctx, 'ledger_entries', e['id'], req.if_match)
    log_activity(ctx, 'ledger', e['id'], 'delete', f'Deleted {e["kind"]} {fmt_money(e["amount_paise"])} · {js_str(e["description"])}')
    return {'ok': True}


def _projects_lite(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'ledger.view')
    return sorted(
        (
            {'id': p['id'], 'code': p['code'], 'name': p['name'], 'client': p['client'], 'stream': p.get('stream', UNDEF), 'status': p['status']}
            for p in rows(ctx, 'projects')
        ),
        key=lambda x: locale_key(x['code']),
    )


# ---------------- Profit & loss ----------------
def _pnl(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.view', 'Profit & loss is visible to admins only.')
    rg = _range(ctx, req.query)
    frm, to = rg['from'], rg['to']
    days = diff_days(to, frm) + 1
    prev_to = add_days(frm, -1)
    prev_from = add_days(prev_to, -(days - 1))
    # Only what has happened counts: nothing dated after today (current and future periods are "to date").
    as_of = to if to < ctx.today else ctx.today
    prev_as_of = prev_to if prev_to < ctx.today else ctx.today
    cur = [] if frm > as_of else expand_ledger(ctx, frm, as_of)
    prev = [] if prev_from > prev_as_of else expand_ledger(ctx, prev_from, prev_as_of)
    rv = revenue_between(ctx, frm, to)
    rv_prev = revenue_between(ctx, prev_from, prev_to)

    def group(lst: list, kind: str, key: Any) -> dict:
        m: dict = {}
        for e in lst:
            if e['kind'] == kind and not (kind == 'expense' and e.get('category') in PAYROLL_CATEGORIES):
                k = coalesce(key(e), 'unassigned')
                m[k] = m.get(k, 0) + e['amount_paise']
        return m

    def lines(a: dict, b: dict, label: Any) -> list[dict]:
        keys = list(a.keys()) + [k for k in b if k not in a]
        out = [{'key': k, 'label': label(k), 'amount': a.get(k, 0), 'prev': b.get(k, 0)} for k in keys]
        out = [x for x in out if js_truthy(x['amount']) or js_truthy(x['prev'])]
        return sorted(out, key=lambda x: -x['amount'])

    def stream_label(k: Any) -> str:
        return STREAM[k] if isinstance(k, str) and k in STREAM else 'Unassigned'

    def stream_of(e: dict) -> Any:
        return e.get('stream', UNDEF)

    def cat_of(e: dict) -> Any:
        return e.get('category', UNDEF)

    inc_stream = lines(group(cur, 'income', stream_of), group(prev, 'income', stream_of), stream_label)
    inc_cat = lines(group(cur, 'income', cat_of), group(prev, 'income', cat_of), lambda k: k)
    # Salaries: the shared payroll basis (same as the CEO dashboard and the monthly P&L)
    exp_map = group(cur, 'expense', cat_of)
    exp_prev_map = group(prev, 'expense', cat_of)
    if js_truthy(rv['payroll']):
        exp_map['Salaries'] = rv['payroll']
    if js_truthy(rv_prev['payroll']):
        exp_prev_map['Salaries'] = rv_prev['payroll']
    exp_cat = lines(exp_map, exp_prev_map, lambda k: k)

    def s(xs: Any) -> Any:
        t: Any = 0
        for x in xs:
            t += x
        return t

    income = s(l['amount'] for l in inc_cat)  # noqa: E741
    prev_income = s(l['prev'] for l in inc_cat)  # noqa: E741
    expense = s(l['amount'] for l in exp_cat)  # noqa: E741
    prev_expense = s(l['prev'] for l in exp_cat)  # noqa: E741
    direct = s(l['amount'] for l in exp_cat if l['key'] in DIRECT)  # noqa: E741

    # 12 months ending with the month of `to`, never beyond this month (no future months on the chart)
    end_m = month_of(as_of)
    months = []
    for i in range(12):
        ym = month_of(add_months(end_m + '-01', i - 11))
        m = monthly_pnl(ctx, ym)
        months.append(
            {
                'month': ym,
                'label': _month_label(ym),
                'billed': m['billed'],
                'collected': m['collected'],
                'cost': m['payroll'] + m['other_costs'],
                'net': m['net'],
                'payroll': m['payroll'],
                'other_costs': m['other_costs'],
                'net_pl': m['net'],
            }
        )
    this_m = month_of(ctx.today)
    monthly = [monthly_pnl(ctx, month_of(add_months(this_m + '-01', i - 5))) for i in range(6)]

    # Cash runway: burn = average monthly expenses over the last 3 full months; recurring income likewise
    cash = _cash_position(ctx)
    last3 = [month_range(month_of(add_months(month_of(ctx.today) + '-01', -i))) for i in (1, 2, 3)]
    burn = js_round(s(revenue_between(ctx, m['from'], m['to'])['cost'] for m in last3) / 3)
    recurring_income = js_round(s(revenue_between(ctx, m['from'], m['to'])['collected'] for m in last3) / 3)
    net_burn = burn - recurring_income
    runway = cash['balance'] / net_burn if net_burn > 0 else None

    # Revenue ahead
    live_projects = {p['id'] for p in rows(ctx, 'projects') if p['status'] not in ('closed', 'npa')}
    open_ = [
        m
        for m in rows(ctx, 'milestones')
        if not js_truthy(m.get('deleted_at')) and m['billing_status'] == 'none' and m['project_id'] in live_projects and m['billing_amount_paise'] > 0
    ]

    def within(n: int) -> Any:
        lim = add_days(ctx.today, n)
        return s(m['billing_amount_paise'] for m in open_ if m['planned_end'] <= lim)

    uninvoiced = s(m['billing_amount_paise'] for m in open_)
    pipe = _sales_pipeline(ctx)
    return {
        'from': frm,
        'to': to,
        'prev_from': prev_from,
        'prev_to': prev_to,
        'as_of': as_of,
        'income': {'by_stream': inc_stream, 'by_category': inc_cat, 'total': income, 'prev_total': prev_income},
        'expense': {'by_category': exp_cat, 'direct': direct, 'total': expense, 'prev_total': prev_expense},
        'gross_profit': income - direct,
        'gross_margin': (income - direct) / income if js_truthy(income) else None,
        'net_profit': income - expense,
        'net_margin': (income - expense) / income if js_truthy(income) else None,
        'prev_net_profit': prev_income - prev_expense,
        'collected': rv['collected'],
        'months': months,
        'monthly': monthly,
        'cash': {
            'opening': cash['opening'],
            'balance': cash['balance'],
            'monthly_burn': burn,
            'recurring_income': recurring_income,
            'net_burn': net_burn,
            'runway_months': runway,
            'cash_positive': net_burn <= 0,
        },
        'revenue_ahead': {
            'uninvoiced': uninvoiced,
            'uninvoiced_count': len(open_),
            'in_30': within(30),
            'in_60': within(60),
            'in_90': within(90),
            'weighted_pipeline': pipe['weighted_value'],
            'open_pipeline': pipe['open_value'],
            'total': uninvoiced + pipe['weighted_value'],
        },
    }


def register(r: Any) -> None:
    r.get('/finance/ledger', _ledger)
    r.get('/finance/ledger/entries', _ledger_entries)
    r.post('/finance/ledger', _create)
    r.patch('/finance/ledger/:id', _patch)
    r.delete('/finance/ledger/:id', _delete)
    r.get('/finance/projects-lite', _projects_lite)
    r.get('/finance/pnl', _pnl)
