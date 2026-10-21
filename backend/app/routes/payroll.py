"""Port of src/mock/routes/payroll.ts — Module C payroll (UI-22). BR-12..BR-16, VAL-F21 (one run per month, final is
frozen). Finalisation is maker-checked (`payroll_finalise`); the applier books the month's Salaries ledger row and
issues payslips (docs_v7.generate_payslips).

Identity semantics: the TS mutates the run row in place (`run.warnings = …` in writeLines, runView on seeded final
runs) and keeps using it; the write paths take a private copy with `edit()` first.
"""

from __future__ import annotations

import re
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    edit,
    err,
    find,
    get_db,
    group,
    has,
    insert,
    log_activity,
    now_iso,
    require_action,
    rows,
    scope,
    set_rows,
    update,
    user_name,
    warn,
)
from app.domain.calendar import add_days, is_working_day, month_of, month_range, parse_iso, week_start, working_days_in_month
from app.domain.costing import rate_on, sales_compensation
from app.domain.money import fmt_money
from app.domain.sales import deal_value
from app.engine.analytics import cal
from app.engine.maker_checker import assert_base_version, eligible_checkers, needs_checker, pending_brief, propose, register_applier
from app.js import UNDEF, coalesce, fmt_date_en_in, js_round, js_sort, js_str, js_truthy, locale_key, to_fixed
from app.routes.timesheets import apply_lock, resolve_approver

__all__ = ['payroll_run_lines', 'register']

_MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$', re.ASCII)


def _month_name(ym: str) -> str:
    """parseISO(`${ym}-01`).toLocaleDateString('en-IN', { month: 'long', year: 'numeric' })"""
    d = parse_iso(f'{ym}-01')
    if d is None:
        return 'Invalid Date'
    return fmt_date_en_in(d, month='long', year='numeric')


def _last_day_of(ctx: Ctx, u: dict) -> str | None:
    """A leaver's last working day: the offboarding checklist's last day, else the day they were deactivated."""
    if u.get('status') != 'deactivated':
        return None
    ex = next((c for c in rows(ctx, 'lifecycle_checklists') if c.get('kind') == 'offboarding' and c.get('user_id') == u['id'] and js_truthy(c.get('last_day'))), None)
    return coalesce(ex.get('last_day', UNDEF) if ex is not None else UNDEF, u['updated_at'][:10])


def _compute_lines(ctx: Ctx, month: str) -> dict[str, Any]:
    s = ctx.tenant['settings']
    c = cal(ctx).without_leave()
    mr = month_range(month)
    frm, to = mr['from'], mr['to']
    wd_month = working_days_in_month(month, c)
    deduct_on = s['pay.deduct_unapproved_days']
    threshold = s['pay.variable_threshold_pct']
    rates = rows(ctx, 'cost_rates')
    weeks: dict[tuple, dict] = {}
    for x in rows(ctx, 'timesheet_weeks'):
        weeks.setdefault((x['user_id'], x['week_start']), x)
    leave = rows(ctx, 'leave')
    deals = rows(ctx, 'deals')
    warnings: list[str] = []
    no_rate: list[str] = []
    pending_people: list[str] = []
    lines: list[dict] = []
    # Deactivated people are on a month's payroll only if they were still employed in it (prorated to their last day).
    users = [
        u
        for u in rows(ctx, 'users')
        if u.get('status') != 'invited' and u['joined_on'] <= to and not (u.get('status') == 'deactivated' and coalesce(_last_day_of(ctx, u), '') < frm)
    ]
    for u in sorted(users, key=lambda u: locale_key(u['name'])):
        rate = rate_on(rates, u['id'], to)
        start = u['joined_on'] if u['joined_on'] > frm else frm
        # leavers are paid up to their last day (deactivation)
        last_day = coalesce(_last_day_of(ctx, u), to)
        end = last_day if last_day < to else to
        working = 0
        approved = 0
        unapproved: Any = 0  # not approved: the week is over and locked, rejected, still a draft or missing
        pending: Any = 0  # the week isn't over yet, or it is submitted and waiting for the manager
        lwp: Any = 0
        per_week: dict[str, dict] = {}
        lv = {lr['date']: lr for lr in leave if lr['user_id'] == u['id'] and lr['date'] >= frm and lr['date'] <= to}
        d = start
        while d <= end:
            if not is_working_day(d, c):
                d = add_days(d, 1)
                continue
            working += 1
            lr = lv.get(d)
            is_lwp = lr is not None and lr.get('type') == 'lwp'
            if is_lwp:
                lwp = lwp + (1 if lr['portion'] == 'full' else 0.5)
            wk = week_start(d)
            w = weeks.get((u['id'], wk))
            if w is not None:
                w = apply_lock(ctx, w)
                weeks[(u['id'], wk)] = w
                st = w['status']
            else:
                st = 'missing'
            if st == 'approved':
                approved += 1
            else:
                # An LWP full day is deducted once as LWP, not again as unapproved.
                portion = (0 if lr['portion'] == 'full' else 0.5) if is_lwp else 1
                week_over = add_days(wk, 6) < ctx.today
                if portion > 0 and (not week_over or st == 'submitted'):
                    pending = pending + portion
                    cur = per_week.get(wk) or {'days': 0, 'status': st}
                    cur['days'] = cur['days'] + portion
                    per_week[wk] = cur
                elif portion > 0:
                    unapproved = unapproved + portion
                    cur = per_week.get(wk) or {'days': 0, 'status': st}
                    cur['days'] = cur['days'] + portion
                    per_week[wk] = cur
            d = add_days(d, 1)
        ap = resolve_approver(ctx, u)
        week_states = [{'wk': wk, **v} for wk, v in per_week.items()]
        not_submitted = sum(1 for x in week_states if x['status'] in ('missing', 'locked', 'open', 'rejected') and add_days(x['wk'], 6) < ctx.today)
        waiting = sum(1 for x in week_states if x['status'] == 'submitted')
        not_over = sum(1 for x in week_states if x['status'] != 'submitted' and add_days(x['wk'], 6) >= ctx.today)
        flags = []
        if not_submitted:
            flags.append(f'{not_submitted} week{"s" if not_submitted > 1 else ""} not submitted')
        if waiting:
            flags.append(f'{waiting} week{"s" if waiting > 1 else ""} waiting for {user_name(ctx, ap) if ap else "approval"}')
        if not_over:
            flags.append(f'{not_over} week{"s" if not_over > 1 else ""} not over yet')
        # Feature Guide §10: pending days are deducted at calculation time; chase the approval first, then recalculate.
        if pending > 0 and js_truthy(deduct_on):
            pd = to_fixed(pending, 1) if pending % 1 else js_str(pending)
            flags.append(f'{pd} pending day{"" if pending == 1 else "s"} deducted — chase first')
        extra = {
            'pending_days': pending,
            'approver_name': user_name(ctx, ap) if ap else None,
            'self_approves': not ap,
            'employee_code': coalesce(u.get('employee_code', UNDEF), None),
            'flags': flags,
        }
        weeks_sorted = js_sort(list(per_week.keys()))
        detail = [{'week_start': k, **per_week[k]} for k in sorted(per_week.keys())]
        if rate is None:
            no_rate.append(u['name'])
            lines.append(
                {
                    'user_id': u['id'],
                    'monthly_ctc_paise': 0,
                    'base_paise': 0,
                    'working_days': working,
                    'approved_days': approved,
                    'unapproved_days': unapproved,
                    'unapproved_weeks': weeks_sorted,
                    'lwp_days': lwp,
                    'deduction_paise': 0,
                    'variable_paise': 0,
                    'net_paise': 0,
                    'unapproved_detail': detail,
                    'would_deduct_paise': 0,
                    'deduct_applied': deduct_on,
                    'sales': None,
                    'no_rate': True,
                    **extra,
                }
            )
            continue
        ctc = rate['monthly_ctc_paise']
        is_sales = rate['variable_pct'] > 0
        achieved: Any = 0
        for dl in deals:
            if dl.get('owner_id') == u['id'] and dl['stage'] == 'won' and js_truthy(dl.get('won_on')) and month_of(dl['won_on']) == month:
                achieved = achieved + deal_value(dl)
        target = ctc if s['pay.target_default'] == 'ctc' else coalesce(rate.get('sales_target_paise', UNDEF), ctc)
        comp = sales_compensation(ctc, rate['variable_pct'], target, achieved, threshold) if is_sales else None
        base = comp['base'] if comp is not None else ctc
        # Prorate joiners: base for the days they were employed in the month
        full_wd = wd_month or 1
        per_day = base / full_wd
        prorated_base = js_round(per_day * working) if working < full_wd else base
        lwp_ded = js_round(per_day * lwp)
        # Deduction = base ÷ working days × (unapproved days + LWP days); unapproved = not approved + still pending.
        unapproved_ded = js_round(per_day * (unapproved + pending))
        deduction = lwp_ded + (unapproved_ded if js_truthy(deduct_on) else 0)
        variable = comp['variableEarned'] if comp is not None else 0
        if unapproved > 0 or pending > 0:
            pending_people.append(u['name'])
        ded = min(prorated_base, deduction)
        lines.append(
            {
                'user_id': u['id'],
                'monthly_ctc_paise': ctc,
                'base_paise': prorated_base,
                'working_days': working,
                'approved_days': approved,
                'unapproved_days': unapproved,
                'unapproved_weeks': weeks_sorted,
                'lwp_days': lwp,
                'deduction_paise': ded,
                'variable_paise': variable,
                'net_paise': max(0, prorated_base - ded + variable),
                'unapproved_detail': detail,
                'would_deduct_paise': unapproved_ded,
                'deduct_applied': deduct_on,
                'sales': {
                    'target_paise': comp['target'],
                    'achieved_paise': achieved,
                    'achievement': comp['achievement'],
                    'variable_at_stake_paise': comp['variable'],
                    'threshold_pct': threshold,
                }
                if comp is not None
                else None,
                'no_rate': False,
                **extra,
            }
        )
    if no_rate:
        warnings.append(f'No cost rate for {", ".join(no_rate)}. Add one in Settings → Cost rates; they are paid ₹0 in this run.')
    if pending_people:
        n = len(pending_people)
        tail = (
            'Their pending and not-approved days are deducted in this calculation — recalculate after the weeks are approved.'
            if js_truthy(deduct_on)
            else 'Deduction is switched off, so they are shown as “would deduct” only.'
        )
        warnings.append(
            f'{n} {"person has" if n == 1 else "people have"} unsubmitted or unapproved weeks ({", ".join(pending_people)}). Chase the approvals before you pay. {tail}'
        )
    if to >= ctx.today:
        warnings.append(f'{_month_name(month)} isn’t over yet. Days after today count as unapproved until their weeks are approved.')
    return {'lines': lines, 'warnings': warnings}


def _lines_of(ctx: Ctx, run_id: Any) -> list[dict]:
    tid = ctx.tenant_id
    return [ln for ln in group('payroll_lines', 'run_id').get(run_id, ()) if ln.get('tenant_id') == tid]


def _write_lines(ctx: Ctx, run: dict) -> None:
    """run must be a private (edited / inserted) row: its warnings are set in place like the TS"""
    res = _compute_lines(ctx, run['month'])
    tid = ctx.tenant_id
    rid = run['id']
    set_rows('payroll_lines', [ln for ln in get_db().payroll_lines if not (ln.get('tenant_id') == tid and ln.get('run_id') == rid)])
    for ln in res['lines']:
        insert(ctx, 'payroll_lines', {'run_id': run['id'], **ln})
    run['warnings'] = res['warnings']
    scope().persist()


def _finalise_state(ctx: Ctx, run: dict) -> dict[str, Any]:
    """v4 extras on the run view (maker-checked finalisation)"""
    mc = needs_checker(ctx, 'payroll_finalise')
    return {
        'pending_finalise': pending_brief(ctx, 'payroll_finalise', run['id']),
        'finalise_needs_checker': mc,
        'can_finalise': run['status'] == 'draft' and (has(ctx, 'payroll.run') if mc else has(ctx, 'payroll.finalise')),
        'finalise_checkers': [u.get('name', UNDEF) for u in eligible_checkers(ctx, 'payroll.finalise', ctx.user_id)] if mc else [],
    }


def _run_view(ctx: Ctx, run: dict) -> dict[str, Any]:
    # Seeded final runs have no lines yet: generate them once (then they stay frozen).
    lines = _lines_of(ctx, run['id'])
    if not lines:
        run = edit('payroll_runs', run)
        _write_lines(ctx, run)
        if run['status'] == 'final':
            run['warnings'] = []
        lines = _lines_of(ctx, run['id'])
    users: dict[str, dict] = {}
    for u in rows(ctx, 'users'):
        users[u['id']] = u
    st = ctx.tenant['settings']
    out = []
    for ln in lines:
        u = users.get(ln['user_id'])
        ud = ln.get('unapproved_detail')
        out.append(
            {
                **ln,
                'unapproved_detail': ud if ud is not None else [{'week_start': w, 'days': 0, 'status': 'missing'} for w in ln['unapproved_weeks']],
                'would_deduct_paise': coalesce(ln.get('would_deduct_paise', UNDEF), 0),
                'deduct_applied': coalesce(ln.get('deduct_applied', UNDEF), st['pay.deduct_unapproved_days']),
                'sales': coalesce(ln.get('sales', UNDEF), None),
                'no_rate': coalesce(ln.get('no_rate', UNDEF), False),
                'user': {
                    'id': ln['user_id'],
                    'name': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
                    'title': coalesce(u.get('title', UNDEF) if u else UNDEF, ''),
                    'role': coalesce(u.get('role', UNDEF) if u else UNDEF, 'employee'),
                    'avatar_hue': coalesce(u.get('avatar_hue', UNDEF) if u else UNDEF, 150),
                },
            }
        )

    def sm(f: Any) -> Any:
        s: Any = 0
        for x in out:
            s = s + f(x)
        return s

    return {
        'run': run,
        'lines': out,
        'totals': {
            'ctc': sm(lambda x: x['monthly_ctc_paise']),
            'base': sm(lambda x: x['base_paise']),
            'deduction': sm(lambda x: x['deduction_paise']),
            'would_deduct': sm(lambda x: x['would_deduct_paise']),
            'variable': sm(lambda x: x['variable_paise']),
            'net': sm(lambda x: x['net_paise']),
            'unapproved_days': sm(lambda x: x['unapproved_days']),
            'lwp_days': sm(lambda x: x['lwp_days']),
            'pending_days': sm(lambda x: coalesce(x.get('pending_days', UNDEF), 0)),
        },
        'deduct_enabled': st['pay.deduct_unapproved_days'],
        'working_days': working_days_in_month(run['month'], cal(ctx)),
        **_finalise_state(ctx, run),
    }


def payroll_run_lines(ctx: Ctx, run_id: str) -> dict[str, Any]:
    """v7 documents: a run's lines as shown on the payroll page (payslips are issued from these) → {run, lines, working_days}"""
    run = find(ctx, 'payroll_runs', run_id, 'Payroll run')
    v = _run_view(ctx, run)
    return {'run': v['run'], 'lines': v['lines'], 'working_days': v['working_days']}


def _block_if_pending(ctx: Ctx, run: dict) -> None:
    p = pending_brief(ctx, 'payroll_finalise', run['id'])
    if p:
        names = ' or '.join(js_str(n) for n in p['checker_names']) or 'a checker'
        err(409, 'CHANGE_PENDING', f'Finalisation of {_month_name(run["month"])} is waiting for {names}. Withdraw it in Change approvals to recalculate.')


def _deny(ctx: Ctx) -> None:
    require_action(ctx, 'payroll.run', 'Payroll is for the admin only.')


def list_runs(req: Req) -> Any:
    ctx = req.ctx
    _deny(ctx)
    out = []
    for run in sorted(rows(ctx, 'payroll_runs'), key=lambda r: locale_key(r['month']), reverse=True):
        ls = _lines_of(ctx, run['id'])
        net: Any = 0
        for ln in ls:
            net = net + ln['net_paise']
        out.append({**run, 'people': len(ls), 'net_paise': net, 'finalise_pending': bool(pending_brief(ctx, 'payroll_finalise', run['id']))})
    return out


def run_payroll(req: Req) -> Any:
    ctx = req.ctx
    _deny(ctx)
    month = js_str(coalesce(req.b('month'), ''))
    if not _MONTH_RE.match(month):
        err(422, 'VALIDATION', 'Pick a month.', 'month')
    if month > month_of(ctx.today):
        err(422, 'VALIDATION', 'You can’t run payroll for a future month.', 'month')
    existing = next((x for x in rows(ctx, 'payroll_runs') if x['month'] == month), None)
    if existing is not None and existing['status'] == 'final':
        err(409, 'PAY_FINAL', f'Payroll for {_month_name(month)} is final and can’t be run again.', 'month')
    if existing is not None:
        _block_if_pending(ctx, existing)
        existing = edit('payroll_runs', existing)
        _write_lines(ctx, existing)
        update(ctx, 'payroll_runs', existing['id'], {'warnings': existing['warnings']})
        warn(ctx, 'PAY_EXISTS', f'A draft for {_month_name(month)} already existed, so it was recalculated.')
        log_activity(ctx, 'payroll_run', existing['id'], 'recomputed', f'Recalculated payroll for {_month_name(month)}')
        return _run_view(ctx, existing)
    run = insert(ctx, 'payroll_runs', {'month': month, 'status': 'draft', 'finalised_by': None, 'finalised_at': None, 'warnings': []})
    _write_lines(ctx, run)
    log_activity(ctx, 'payroll_run', run['id'], 'created', f'Ran payroll for {_month_name(month)}')
    return _run_view(ctx, run)


def get_run(req: Req) -> Any:
    ctx = req.ctx
    _deny(ctx)
    return _run_view(ctx, find(ctx, 'payroll_runs', req.params['id'], 'Payroll run'))


def recompute(req: Req) -> Any:
    ctx = req.ctx
    _deny(ctx)
    run = find(ctx, 'payroll_runs', req.params['id'], 'Payroll run')
    if run['status'] == 'final':
        err(409, 'PAY_FINAL', 'This payroll run is final. Its lines are frozen.')
    if req.if_match is not UNDEF and req.if_match != run['version']:
        err(409, 'STALE', 'This run changed in another tab. Reload to see the latest version.')
    _block_if_pending(ctx, run)
    run = edit('payroll_runs', run)
    _write_lines(ctx, run)
    update(ctx, 'payroll_runs', run['id'], {'warnings': run['warnings']})
    log_activity(ctx, 'payroll_run', run['id'], 'recomputed', f'Recalculated payroll for {_month_name(run["month"])}')
    return _run_view(ctx, run)


def finalise(req: Req) -> Any:
    """Finalise (lock) a run. With maker-checker on for payroll finalisation, whoever prepares payroll requests it and a
    second person holding “Finalise payroll” approves; otherwise it needs that permission."""
    ctx = req.ctx
    _deny(ctx)
    run = find(ctx, 'payroll_runs', req.params['id'], 'Payroll run')
    if run['status'] == 'final':
        err(409, 'PAY_FINAL', 'This payroll run is already final.')
    if req.if_match is not UNDEF and req.if_match != run['version']:
        err(409, 'STALE', 'This run changed in another tab. Reload to see the latest version.')
    mc = needs_checker(ctx, 'payroll_finalise')
    if not mc:
        require_action(ctx, 'payroll.finalise', 'Finalising payroll needs “Finalise payroll”. Ask someone who has it.')
    _block_if_pending(ctx, run)
    # Recompute one last time so the frozen lines reflect approvals made since the draft
    run = edit('payroll_runs', run)
    _write_lines(ctx, run)
    cur = update(ctx, 'payroll_runs', run['id'], {'warnings': run['warnings']})
    v = _run_view(ctx, cur)
    people = len(v['lines'])
    net = fmt_money(v['totals']['net'])
    nw = len(run['warnings'])
    return propose(
        ctx,
        {
            'entity': 'payroll_finalise',
            'entity_id': run['id'],
            'action': 'execute',
            'required_permission': 'payroll.finalise',
            'base_version': cur['version'],
            'summary': f'Finalise {_month_name(run["month"])} payroll — {people} people, net {net}',
            'before': {
                'Status': 'Draft',
                'People': people,
                'Net pay': net,
                'Deductions': fmt_money(v['totals']['deduction']),
                'Variable pay': fmt_money(v['totals']['variable']),
                'Warnings': nw,
            },
            'after': {
                'Status': 'Final (locked)',
                'People': people,
                'Net pay': net,
                'Deductions': fmt_money(v['totals']['deduction']),
                'Variable pay': fmt_money(v['totals']['variable']),
                'Warnings': nw,
            },
            'payload': {'run_id': run['id'], 'net_paise': v['totals']['net']},
        },
    )


def register(r: Any) -> None:
    r.get('/payroll', list_runs)
    r.post('/payroll/run', run_payroll)
    r.get('/payroll/:id', get_run)
    r.post('/payroll/:id/recompute', recompute)
    r.post('/payroll/:id/finalise', finalise)


def _book_payroll_cost(ctx: Ctx, month: str, amount: Any) -> None:
    """One Salaries expense row per month, dated the 1st (the payroll date `payrollForMonth` reads)."""
    first = f'{month}-01'
    desc = f'Payroll {_month_name(month)} (final)'
    existing = [
        e
        for e in rows(ctx, 'ledger_entries')
        if e['kind'] == 'expense' and e['category'] == 'Salaries' and not js_truthy(e.get('recurring')) and e['date'][:7] == month
    ]
    if existing:
        update(ctx, 'ledger_entries', existing[0]['id'], {'amount_paise': amount, 'description': desc, 'date': first, 'status': 'paid'})
        for e in existing[1:]:
            update(ctx, 'ledger_entries', e['id'], {'amount_paise': 0, 'description': f'{js_str(e["description"])} (replaced by final payroll)'})
    else:
        insert(
            ctx,
            'ledger_entries',
            {
                'kind': 'expense',
                'category': 'Salaries',
                'description': desc,
                'amount_paise': amount,
                'date': first,
                'recurring': False,
                'until_on': None,
                'status': 'paid',
                'project_id': None,
                'milestone_id': None,
                'invoice_id': None,
                'stream': None,
                'locked': False,
            },
        )


def _apply_finalise(ctx: Ctx, cs: dict) -> dict[str, Any]:
    run = find(ctx, 'payroll_runs', js_str(cs['payload'].get('run_id', UNDEF)), 'Payroll run')
    if run['status'] == 'final':
        err(409, 'PAY_FINAL', 'This payroll run is already final.')
    assert_base_version(cs, run['version'])
    # A checked finalisation freezes exactly the lines the maker submitted (no silent recompute).
    out = update(ctx, 'payroll_runs', run['id'], {'status': 'final', 'finalised_by': ctx.user_id, 'finalised_at': now_iso()})
    v = _run_view(ctx, out)
    by = f' (prepared by {user_name(ctx, cs["maker_id"])}, approved by {js_str(ctx.user.get("name", UNDEF))})' if cs['id'] != 'cs_direct' else ''
    log_activity(ctx, 'payroll_run', run['id'], 'finalised', f'Finalised payroll for {_month_name(run["month"])} — net {fmt_money(v["totals"]["net"])}{by}')
    # v7 wiring: the finalised payroll is the month's salary cost (Salaries ledger row replacing the estimate).
    _book_payroll_cost(ctx, run['month'], v['totals']['net'])
    # v7 documents: one payslip per employee from the Payslip template (idempotent per run + employee).
    try:
        from app.routes.docs_v7 import generate_payslips

        r = generate_payslips(ctx, run['id'])
        created = r.get('created', UNDEF) if isinstance(r, dict) else UNDEF
        if js_truthy(created):
            warn(ctx, 'PAYSLIPS_ISSUED', f'{js_str(created)} payslip{"" if created == 1 else "s"} issued to employees under Documents.')
    except Exception as e:  # noqa: BLE001 — the TS catches everything here
        msg = e.message if hasattr(e, 'message') else str(e)
        warn(ctx, 'PAYSLIPS_FAILED', f'Payroll is final, but payslips were not issued: {js_str(msg)} Generate them from the payroll page.')
    return {'result': v, 'result_id': run['id']}


register_applier('payroll_finalise', _apply_finalise)


