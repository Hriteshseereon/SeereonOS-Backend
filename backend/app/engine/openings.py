"""Financial-year opening balances (v9, D10) — table fy_openings, one row per FY.

lines: [{id, kind, name, side, amount_paise, category?, party_id?, party_name?, reference?, due_on?, note?}]
  kind  cash | bank          money held at the FY start (debit; credit = overdraft)
        ledger               any other ledger account brought forward (capital, loans, fixed assets, …), debit/credit
        receivable           a customer owes us (debit), optionally a client id + reference/due date
        payable              we owe a vendor (credit), optionally a vendor id + reference/due date

Lock rule: editable until the FY has transactions (ledger entries, issued invoices, payments) or someone locks it by
hand; then only after a reopen with a reason (maker-checker entity 'opening_balance' when it is on). Locking again
clears the reopen.

Consumers: `cash_basis(ctx)` — the opening cash and the date the cash position counts from (finance ledger / P&L
runway and the CEO dashboard). With no opening rows it is the v8 rule (settings finance.opening_cash_paise, counted
from the first ledger entry), so nothing changes for tenants that never enter openings.
"""

from __future__ import annotations

from typing import Any

from app.ctx import Ctx, rows
from app.domain.calendar import fy_label, fy_range, fy_start_year
from app.engine.analytics import expand_ledger
from app.js import UNDEF, coalesce, js_truthy, prop

__all__ = [
    'KINDS',
    'KIND_LABEL',
    'fy_month',
    'current_fy',
    'opening_row',
    'totals_of',
    'fy_activity',
    'state_of',
    'cash_basis',
    'opening_summary',
    'opening_parties',
    'opening_age_bucket',
]

KINDS = ('cash', 'bank', 'ledger', 'receivable', 'payable')
KIND_LABEL = {
    'cash': 'Cash in hand',
    'bank': 'Bank accounts',
    'ledger': 'Other ledger accounts',
    'receivable': 'Receivables (customers owe you)',
    'payable': 'Payables (you owe vendors)',
}


def fy_month(ctx: Ctx) -> int:
    fm = prop(ctx.tenant.get('settings'), 'company.fy_start_month')
    return fm if isinstance(fm, int) and not isinstance(fm, bool) and 1 <= fm <= 12 else 4


def current_fy(ctx: Ctx) -> int:
    return fy_start_year(ctx.today, fy_month(ctx))


def opening_row(ctx: Ctx, fy: int) -> dict | None:
    return next((r for r in rows(ctx, 'fy_openings') if r.get('fy_start_year') == fy), None)


def _signed(line: dict) -> int:
    a = line.get('amount_paise') or 0
    return a if line.get('side') != 'credit' else -a


def totals_of(lines: Any) -> dict[str, int]:
    t = {
        'cash': 0,
        'bank': 0,
        'cash_bank': 0,
        'ledger_debit': 0,
        'ledger_credit': 0,
        'receivables': 0,
        'payables': 0,
        'debits': 0,
        'credits': 0,
        'difference': 0,
        'count': 0,
    }
    for ln in lines if isinstance(lines, list) else []:
        a = ln.get('amount_paise') or 0
        k = ln.get('kind')
        t['count'] += 1
        if ln.get('side') == 'credit':
            t['credits'] += a
        else:
            t['debits'] += a
        if k == 'cash':
            t['cash'] += _signed(ln)
        elif k == 'bank':
            t['bank'] += _signed(ln)
        elif k == 'receivable':
            t['receivables'] += a
        elif k == 'payable':
            t['payables'] += a
        elif k == 'ledger':
            t['ledger_debit' if ln.get('side') != 'credit' else 'ledger_credit'] += a
    t['cash_bank'] = t['cash'] + t['bank']
    t['difference'] = t['debits'] - t['credits']
    return t


def fy_activity(ctx: Ctx, fy: int) -> dict[str, int]:
    """transactions recorded in the FY (what locks its opening balances)"""
    rg = fy_range(fy, fy_month(ctx))
    frm, to = rg['from'], rg['to']
    upto = to if to < ctx.today else ctx.today
    ledger = 0
    for e in rows(ctx, 'ledger_entries'):
        if not js_truthy(e.get('recurring')) and frm <= e['date'] <= to:
            ledger += 1
    if frm <= upto:
        ledger += sum(1 for e in expand_ledger(ctx, frm, upto) if js_truthy(e.get('recurring')))
    invoices = sum(1 for i in rows(ctx, 'invoices') if i.get('status') not in ('draft', 'cancelled') and frm <= coalesce(i.get('invoiced_on', UNDEF), '') <= to)
    payments = sum(1 for p in rows(ctx, 'payments') if frm <= coalesce(p.get('paid_on', UNDEF), '') <= to)
    return {'ledger_entries': ledger, 'invoices': invoices, 'payments': payments, 'total': ledger + invoices + payments}


def state_of(ctx: Ctx, row: dict | None, fy: int) -> dict[str, Any]:
    act = fy_activity(ctx, fy)
    reopened = bool(row and row.get('reopened_at'))
    manual = bool(row and row.get('locked'))
    has_tx = act['total'] > 0
    locked = (manual or has_tx) and not reopened
    why = None
    if locked:
        why = 'locked by hand' if manual and not has_tx else f'the FY already has {act["total"]} transaction{"" if act["total"] == 1 else "s"}'
    return {'locked': locked, 'manual_lock': manual, 'has_transactions': has_tx, 'activity': act, 'reopened': reopened, 'lock_reason': why}


def cash_basis(ctx: Ctx) -> dict[str, Any]:
    """{opening, start, source, fy_label?}: the cash the cash position starts from and the day it counts from"""
    fm = fy_month(ctx)
    cur = fy_start_year(ctx.today, fm)
    best = None
    for r in rows(ctx, 'fy_openings'):
        y = r.get('fy_start_year')
        if isinstance(y, int) and y <= cur and any(ln.get('kind') in ('cash', 'bank') for ln in r.get('lines') or []):
            if best is None or y > best['fy_start_year']:
                best = r
    if best is not None:
        rg = fy_range(best['fy_start_year'], fm)
        return {'opening': totals_of(best['lines'])['cash_bank'], 'start': rg['from'], 'source': 'fy_opening', 'fy_label': fy_label(best['fy_start_year'])}
    opening = coalesce(prop(ctx.tenant.get('settings'), 'finance.opening_cash_paise'), 0)
    start = ctx.today
    for e in rows(ctx, 'ledger_entries'):
        if e['date'] < start:
            start = e['date']
    return {'opening': opening, 'start': start, 'source': 'settings'}


def opening_summary(ctx: Ctx, date: str) -> dict[str, Any] | None:
    """opening balances of the FY that contains `date` (None when none were entered) — for ledger / P&L screens"""
    if not rows(ctx, 'fy_openings'):
        return None
    fm = fy_month(ctx)
    fy = fy_start_year(date, fm)
    r = opening_row(ctx, fy)
    if r is None or not r.get('lines'):
        return None
    t = totals_of(r['lines'])
    rg = fy_range(fy, fm)
    return {
        'fy_start_year': fy,
        'fy_label': fy_label(fy),
        'as_of': rg['from'],
        'cash': t['cash'],
        'bank': t['bank'],
        'cash_bank': t['cash_bank'],
        'receivables': t['receivables'],
        'payables': t['payables'],
        'other_debit': t['ledger_debit'],
        'other_credit': t['ledger_credit'],
        'lines': t['count'],
        'locked': state_of(ctx, r, fy)['locked'],
    }


def opening_parties(ctx: Ctx) -> dict[str, Any] | None:
    """V9-FIX (item 9): the receivables and payables brought forward — the party lines of the LATEST financial year's
    opening balances up to the current FY (an older year's are superseded by the next year's), for Reports ›
    Receivables and Insights › Revenue. None when no such lines were entered.
    {fy_start_year, fy_label, as_of, receivables: [line + party_name], payables: [line + party_name]}"""
    fm = fy_month(ctx)
    cur = fy_start_year(ctx.today, fm)
    best = None
    for r in rows(ctx, 'fy_openings'):
        y = r.get('fy_start_year')
        if isinstance(y, int) and y <= cur and any(ln.get('kind') in ('receivable', 'payable') for ln in r.get('lines') or []):
            if best is None or y > best['fy_start_year']:
                best = r
    if best is None:
        return None
    clients = {c['id']: c.get('name') for c in rows(ctx, 'clients')}
    vendors = {v['id']: v.get('name') for v in rows(ctx, 'vendors')}

    def view(ln: dict, names: dict) -> dict:
        pid = ln.get('party_id')
        return {**ln, 'party_name': (names.get(pid) if pid else None) or ln.get('name')}

    lines = best.get('lines') or []
    return {
        'fy_start_year': best['fy_start_year'],
        'fy_label': fy_label(best['fy_start_year']),
        'as_of': fy_range(best['fy_start_year'], fm)['from'],
        'receivables': [view(ln, clients) for ln in lines if ln.get('kind') == 'receivable'],
        'payables': [view(ln, vendors) for ln in lines if ln.get('kind') == 'payable'],
    }


def opening_age_bucket(line: dict, today: str) -> str:
    """Ageing bucket of an opening balance line: by days past its due date when one was entered, else the OLDEST
    bucket (it was already outstanding when the financial year opened, invoice date unknown)."""
    from app.domain.calendar import diff_days

    due = line.get('due_on')
    if not due:
        return 'd90p'
    late = diff_days(today, due)
    return 'not_due' if late <= 0 else 'd30' if late <= 30 else 'd60' if late <= 60 else 'd90' if late <= 90 else 'd90p'
