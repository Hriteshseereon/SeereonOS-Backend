"""Port of src/mock/routes/spend.ts — spend & commercial: expense claims, vendors, purchase orders, vendor bills, budgets
(budget vs actual + unbudgeted spend) and the revenue-leakage register. Every rupee is computed here; the screens only
format. Exports used by other modules: leakage_rows, budget_statement, po_total, provide_clients_helpers,
month_remaining, spend_authority, admin_id, mask_account."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    err,
    find,
    has,
    has_any,
    insert,
    log_activity,
    next_number,
    notify,
    now_iso,
    patch,
    remove,
    require_action,
    rows,
    rows_where_ro,
    scope,
    update,
    user_name,
    users_with,
    warn,
)
from app.domain.calendar import add_days, add_months, diff_days, fy_start_year, is_real_iso_date, month_of
from app.domain.labels import STREAM
from app.domain.money import fmt_money
from app.engine.analytics import costed_entries, expand_ledger, revenue_between
from app.engine.maker_checker import assert_base_version, needs_checker, pending_brief, propose, register_applier
from app.engine.workflows import (
    advance_approval,
    approval_route,
    ensure_wf,
    escalation_target,
    start_approval,
    step_due,
    step_label,
    wf_clear,
    wf_patch,
)
from app.frozen import thaw
from app.js import (
    NAN,
    UNDEF,
    coalesce,
    fmt_date_en_in,
    is_finite,
    is_integer,
    js_len,
    js_lower,
    js_max,
    js_min,
    js_number,
    js_or,
    js_round,
    js_slice,
    js_str,
    js_trim,
    js_truthy,
    js_upper,
    locale_key,
    nullish,
    pad_start,
    prop,
    to_fixed,
)
from app.modules.spend.labels import (
    BUDGET_EXPENSE_CATEGORIES,
    EXPENSE_CATEGORY,
    EXPENSE_CATEGORY_LABEL,
    EXPENSE_MAX_AGE_DAYS,
    GSTIN_RE,
    LEAKAGE_KIND,
    MANUAL_LEAKAGE_KINDS,
    PAN_RE,
    RECEIPT_LIMIT_PAISE,
)

VENDOR_SENSITIVE = ('gstin', 'pan', 'bank_name', 'bank_account', 'ifsc')
VENDOR_FIELD_LABEL = {
    'name': 'Name',
    'category': 'Category',
    'gstin': 'GSTIN',
    'pan': 'PAN',
    'contact': 'Contact',
    'email': 'Email',
    'phone': 'Phone',
    'payment_terms_days': 'Payment terms (days)',
    'bank_name': 'Bank',
    'bank_account': 'Account number',
    'ifsc': 'IFSC',
}
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+\Z')


def mask_account(a: Any) -> str:
    if not js_truthy(a):
        return ''
    return '•••• ' + a[-4:] if len(a) > 4 else a


# ---------------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------------
def _g(o: Any, k: str) -> Any:
    """`o.k` on any JS value (null/undefined throw a TypeError -> 500)"""
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    if o is None or o is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(o)} (reading '{k}')")
    return UNDEF


def _p(o: Any, k: str) -> Any:
    """`o?.k`"""
    return o.get(k, UNDEF) if isinstance(o, dict) else UNDEF


def _patch_quiet(table: str, row: dict, changes: dict) -> dict:
    """`row.x = v` WITHOUT persist(): the change rides along if something else persists (no write intent of its own)"""
    s = scope()
    wi = s.write_intent
    new = patch(table, row, changes)
    s.write_intent = wi
    return new


def _fy_month(ctx: Ctx) -> Any:
    return coalesce(prop(ctx.tenant.get('settings'), 'company.fy_start_month'), 4)


def _cur_fy(ctx: Ctx) -> Any:
    return fy_start_year(ctx.today, _fy_month(ctx))


def _fy_range(fy: Any, fm: Any) -> dict[str, str]:
    """fyRange(startYear, m) for any number (NaN -> 'NaN-04-01' … like the TS template)"""
    frm = f'{js_str(fy)}-{pad_start(js_str(fm), 2, "0")}-01'
    return {'from': frm, 'to': add_days(add_months(frm, 12), -1)}


def _fy_label(y: Any) -> str:
    return f'FY{pad_start(js_str(_jsmod(y, 100)), 2, "0")}-{pad_start(js_str(_jsmod(y + 1, 100)), 2, "0")}'


def _jsmod(a: Any, b: int) -> Any:
    if isinstance(a, float) and (a != a or a in (float('inf'), float('-inf'))):
        return NAN
    import math

    r = math.fmod(a, b)
    return int(r) if isinstance(a, int) or float(r).is_integer() else r


def _fy_months(ctx: Ctx, fy: Any) -> list[str]:
    frm = _fy_range(fy, _fy_month(ctx))['from']
    return [month_of(add_months(frm, i)) for i in range(12)]


def _index_of(lst: list, v: Any) -> int:
    try:
        return lst.index(v)
    except ValueError:
        return -1


def _at(lst: Any, i: Any) -> Any:
    """lst[i] (UNDEF out of range / negative)"""
    if isinstance(i, int) and 0 <= i < len(lst):
        return lst[i]
    return UNDEF


def spend_authority(ctx: Ctx) -> bool:
    """Spend authority: final approver of claims above the second-approval limit, purchase orders and vendor bills."""
    return has(ctx, 'budgets.manage')


def admin_id(ctx: Ctx) -> Any:
    """The person who holds spend authority (the top of the reporting line first)."""
    holders = sorted(users_with(ctx, 'budgets.manage'), key=lambda u: 1 if js_truthy(u.get('reports_to_id')) else 0)
    return holders[0]['id'] if holders else None


def _sees_all_claims(ctx: Ctx) -> bool:
    return has_any(ctx, 'expenses.pay', 'budgets.manage')


def _money(v: Any, field: str, label: str, allow_zero: bool = False) -> Any:
    n = js_number(v)
    if not is_finite(n) or n < 0 or (not allow_zero and n == 0) or js_round(n) != n:
        err(422, 'VAL_AMOUNT', f'{label} must be {"zero or more" if allow_zero else "more than zero"}.', field)
    if n > 10000000000000:
        err(422, 'VAL_AMOUNT', f'{label} looks too large. Check the number of zeros.', field)
    return n


def _need_date(v: Any, field: str, label: str) -> str:
    s = js_str(coalesce(v, ''))
    if not is_real_iso_date(s):
        err(422, 'VAL_DATE', f'{label} is required.', field)
    return s


def _need_text(v: Any, field: str, label: str, mn: int = 1, mx: int = 300) -> str:
    s = js_trim(js_str(coalesce(v, '')))
    if js_len(s) < mn:
        err(422, 'VAL_REQUIRED', f'{label} needs at least {mn} characters.' if mn > 1 else f'{label} is required.', field)
    if js_len(s) > mx:
        err(422, 'VAL_TOO_LONG', f'{label} is longer than {mx} characters.', field)
    return s


def _save_file(ctx: Ctx, f: Any, entity: str, field: str) -> Any:
    if not js_truthy(f):
        return None
    if not js_truthy(_g(f, 'name')) or not js_truthy(_g(f, 'data_url')):
        err(422, 'FILE_REJECTED', 'The file could not be read. Try again.', field)
    if _g(f, 'size_bytes') is not UNDEF and not nullish(f['size_bytes']) and js_number(f['size_bytes']) > 10 * 1024 * 1024:
        err(422, 'FILE_REJECTED', 'The file is larger than 10 MB.', field)
    return insert(
        ctx,
        'files',
        {'name': f['name'], 'mime': js_or(_g(f, 'mime'), 'application/octet-stream'), 'size_bytes': _g(f, 'size_bytes'), 'entity': entity, 'entity_id': '', 'data_url': f['data_url']},
    )['id']


def _link_file(ctx: Ctx, file_id: Any, entity_id: str) -> None:
    if not js_truthy(file_id):
        return
    f = next((x for x in rows(ctx, 'files') if x['id'] == file_id), None)
    if f is not None:
        _patch_quiet('files', f, {'entity_id': entity_id})


def _budget_key(fy: Any, kind: str, category: str) -> str:
    return f'budget_{js_str(fy)}_{kind}_{re.sub(r"[^a-z0-9]+", "_", js_lower(js_trim(category)))}'


def _project_map(ctx: Ctx) -> dict:
    return {p['id']: p for p in rows(ctx, 'projects')}


def _lc(s: Any) -> str:
    return js_lower(js_trim(s))


def _sum(xs: Any) -> Any:
    s: Any = 0
    for x in xs:
        s = s + x
    return s


# ---------------------------------------------------------------------------------------------------------------------
# Budget model: actuals from the ledger + committed claims and bills
# ---------------------------------------------------------------------------------------------------------------------
def _bill_category(ctx: Ctx, b: dict) -> Any:
    if js_truthy(b.get('category')):
        return b['category']
    if js_truthy(b.get('po_id')):
        po = next((p for p in rows(ctx, 'purchase_orders') if p['id'] == b['po_id']), None)
        if po:
            return po['budget_category']
    v = next((x for x in rows(ctx, 'vendors') if x['id'] == b['vendor_id']), None)
    return coalesce(v.get('category', UNDEF) if v else UNDEF, 'Other')


def _spend_items(ctx: Ctx, frm: str, to: str) -> list[dict]:
    expenses = rows(ctx, 'expense_claims')
    bills = rows(ctx, 'vendor_bills')
    pos = {p['id']: p for p in rows(ctx, 'purchase_orders')}
    by_ledger: dict[Any, dict] = {}
    for e in expenses:
        if js_truthy(e.get('ledger_entry_id')):
            by_ledger[e['ledger_entry_id']] = {'approved_by': e.get('decided_by'), 'source': 'expense', 'id': e['id']}
    for b in bills:
        if js_truthy(b.get('ledger_entry_id')):
            po = pos.get(b['po_id']) if js_truthy(b.get('po_id')) else None
            ab = coalesce(b.get('approved_by'), coalesce(po.get('decided_by') if po else UNDEF, None) if js_truthy(b.get('po_id')) else None)
            by_ledger[b['ledger_entry_id']] = {'approved_by': ab, 'source': 'bill', 'id': b['id']}
    out: list[dict] = []
    last_actual = to if to < ctx.today else ctx.today
    for l in expand_ledger(ctx, frm, last_actual):  # noqa: E741
        if l['kind'] != 'expense':
            continue
        link = by_ledger.get(l['id'])
        out.append(
            {
                'source': 'ledger',
                'ref_id': l['id'],
                'date': l['occurrence'],
                'category': l['category'],
                'description': l['description'],
                'amount': l['amount_paise'],
                'state': 'actual',
                'approved_by': coalesce(link['approved_by'] if link else UNDEF, l.get('created_by'), None),
            }
        )
    for e in expenses:
        if e['status'] != 'approved' or e['spent_on'] < frm or e['spent_on'] > to:
            continue
        out.append(
            {
                'source': 'expense',
                'ref_id': e['id'],
                'date': e['spent_on'],
                'category': EXPENSE_CATEGORY.get(e['category'], UNDEF),
                'description': f'{user_name(ctx, e["user_id"])} · {js_str(e["description"])}',
                'amount': e['amount_paise'],
                'state': 'committed',
                'approved_by': e.get('decided_by'),
            }
        )
    for b in bills:
        if b['status'] != 'approved' or b['bill_date'] < frm or b['bill_date'] > to:
            continue
        v = next((x for x in rows(ctx, 'vendors') if x['id'] == b['vendor_id']), None)
        out.append(
            {
                'source': 'bill',
                'ref_id': b['id'],
                'date': b['bill_date'],
                'category': _bill_category(ctx, b),
                'description': f'{js_str(coalesce(v.get("name", UNDEF) if v else UNDEF, "Vendor"))} · bill {js_str(b["number"])}',
                'amount': b['amount_paise'] + b['gst_paise'],
                'state': 'committed',
                'approved_by': coalesce(b.get('approved_by'), None),
            }
        )
    return out


def _budgets_for(ctx: Ctx, fy: Any) -> list[dict]:
    return [b for b in rows(ctx, 'budgets') if b['fy_start_year'] == fy]


def month_remaining(ctx: Ctx, category: str, date: str, exclude_ref: Any = UNDEF) -> dict | None:
    """Remaining budget for a category in the month of `date` (budget − actual − committed). None = no budget line."""
    fy = fy_start_year(date, _fy_month(ctx))
    b = next((x for x in _budgets_for(ctx, fy) if x['kind'] == 'expense' and _lc(x['category']) == _lc(category)), None)
    if not b:
        return None
    months = _fy_months(ctx, fy)
    idx = _index_of(months, month_of(date))
    budget = coalesce(_at(b['monthly_paise'], idx), 0)
    m = _at(months, idx)
    first = f'{js_str(m)}-01'
    frm, to = first, add_days(add_months(first, 1), -1)
    used = _sum(s['amount'] for s in _spend_items(ctx, frm, to) if _lc(s['category']) == _lc(category) and s['ref_id'] != exclude_ref)
    return {'budget': budget, 'used': used, 'remaining': budget - used}


def _fy_remaining(ctx: Ctx, category: str, date: str, exclude_po: Any = UNDEF) -> dict | None:
    """Remaining FY budget for a category, counting approved-but-unbilled POs as committed."""
    fy = fy_start_year(date, _fy_month(ctx))
    b = next((x for x in _budgets_for(ctx, fy) if x['kind'] == 'expense' and _lc(x['category']) == _lc(category)), None)
    if not b:
        return None
    fr = _fy_range(fy, _fy_month(ctx))
    used = _sum(s['amount'] for s in _spend_items(ctx, fr['from'], fr['to']) if _lc(s['category']) == _lc(category))
    bills = rows(ctx, 'vendor_bills')
    for po in rows(ctx, 'purchase_orders'):
        if po['id'] == exclude_po or po['status'] != 'approved' or _lc(po['budget_category']) != _lc(category):
            continue
        billed = _sum(x['amount_paise'] + x['gst_paise'] for x in bills if x.get('po_id') == po['id'] and x['status'] != 'disputed')
        used += js_max(0, po_total(po) - billed)
    budget = _sum(b['monthly_paise'])
    return {'budget': budget, 'used': used, 'remaining': budget - used}


def _slice_sum(a: list, n: int = 12) -> Any:
    return _sum(a[:n])


def budget_statement(ctx: Ctx, fy: Any) -> dict[str, Any]:
    months = _fy_months(ctx, fy)
    fr = _fy_range(fy, _fy_month(ctx))
    frm, to = fr['from'], fr['to']
    cur_idx = -1 if ctx.today < frm else 12 if ctx.today > to else _index_of(months, month_of(ctx.today))
    ytd_n = 0 if cur_idx < 0 else 12 if cur_idx >= 12 else cur_idx + 1
    budgets = _budgets_for(ctx, fy)
    items = _spend_items(ctx, frm, to)

    # expense lines
    cats: dict[str, str] = {}
    for c in BUDGET_EXPENSE_CATEGORIES:
        cats[_lc(c)] = c
    for b in budgets:
        if b['kind'] == 'expense' and _lc(b['category']) not in cats:
            cats[_lc(b['category'])] = b['category']
    for s in items:
        if _lc(s['category']) not in cats:
            cats[_lc(s['category'])] = s['category']
    expense: list[dict] = []
    for key, label in cats.items():
        b = next((x for x in budgets if x['kind'] == 'expense' and _lc(x['category']) == key), None)
        actual: list = [0] * 12
        committed: list = [0] * 12
        for s in items:
            if _lc(s['category']) != key:
                continue
            i = _index_of(months, month_of(s['date']))
            if i < 0:
                continue
            if s['state'] == 'actual':
                actual[i] += s['amount']
            else:
                committed[i] += s['amount']
        mb = list(b['monthly_paise'][:12]) if b else [0] * 12
        ytd_budget = _slice_sum(mb, ytd_n)
        ytd_actual = _slice_sum(actual, ytd_n) + _slice_sum(committed, ytd_n)
        fy_actual = _slice_sum(actual) + _slice_sum(committed)
        if not b and fy_actual == 0:
            continue
        variance = ytd_budget - ytd_actual
        cat_ = b['category'] if b else label
        expense.append(
            {
                'id': b['id'] if b else None,
                'version': b['version'] if b else None,
                'kind': 'expense',
                'category': cat_,
                'label': label,
                'owner_id': coalesce(b.get('owner_id') if b else UNDEF, None),
                'notes': coalesce(b.get('notes') if b else UNDEF, ''),
                'monthly_budget': mb,
                'monthly_actual': actual,
                'monthly_committed': committed,
                'budget_total': _slice_sum(mb),
                'ytd_budget': ytd_budget,
                'ytd_actual': ytd_actual,
                'ytd_variance': variance,
                'ytd_variance_pct': variance / ytd_budget if js_truthy(ytd_budget) else None,
                'fy_actual': fy_actual,
                'over': ytd_actual > ytd_budget,
                'has_budget': bool(b),
                'pending_change': pending_brief(ctx, 'budget', b['id'] if b else _budget_key(fy, 'expense', cat_)),
            }
        )
    expense = sorted(expense, key=lambda l: -(l['budget_total'] + l['fy_actual']))  # noqa: E741

    # revenue lines by stream
    revenue: list[dict] = []
    income = [l for l in expand_ledger(ctx, frm, to if to < ctx.today else ctx.today) if l['kind'] == 'income']  # noqa: E741
    for stream in STREAM:
        b = next((x for x in budgets if x['kind'] == 'revenue' and x['category'] == stream), None)
        actual = [0] * 12
        for l in income:  # noqa: E741
            if l.get('stream') != stream:
                continue
            i = _index_of(months, month_of(l['occurrence']))
            if i >= 0:
                actual[i] += l['amount_paise']
        mb = list(b['monthly_paise'][:12]) if b else [0] * 12
        if not b and _slice_sum(actual) == 0:
            continue
        ytd_budget = _slice_sum(mb, ytd_n)
        ytd_actual = _slice_sum(actual, ytd_n)
        variance = ytd_actual - ytd_budget
        revenue.append(
            {
                'id': b['id'] if b else None,
                'version': b['version'] if b else None,
                'kind': 'revenue',
                'category': stream,
                'label': STREAM[stream],
                'owner_id': coalesce(b.get('owner_id') if b else UNDEF, None),
                'notes': coalesce(b.get('notes') if b else UNDEF, ''),
                'monthly_budget': mb,
                'monthly_actual': actual,
                'monthly_committed': [0] * 12,
                'budget_total': _slice_sum(mb),
                'ytd_budget': ytd_budget,
                'ytd_actual': ytd_actual,
                'ytd_variance': variance,
                'ytd_variance_pct': variance / ytd_budget if js_truthy(ytd_budget) else None,
                'fy_actual': _slice_sum(actual),
                'over': ytd_actual < ytd_budget,
                'has_budget': bool(b),
                'pending_change': pending_brief(ctx, 'budget', b['id'] if b else _budget_key(fy, 'revenue', stream)),
            }
        )

    # unbudgeted / additional spend: per category-month, everything beyond the month's budget
    unbudgeted: list[dict] = []
    running: dict[str, Any] = {}
    for s in sorted(items, key=lambda x: (locale_key(x['date']), x['amount'])):
        line = next((l for l in expense if _lc(l['category']) == _lc(s['category'])), None)  # noqa: E741
        i = _index_of(months, month_of(s['date']))
        key = _lc(s['category']) + '|' + str(i)
        before = running.get(key, 0)
        after = before + s['amount']
        running[key] = after
        budget_m = coalesce(_at(line['monthly_budget'], i), 0) if line and line['has_budget'] else 0
        has_budget = bool(line and line['has_budget']) and line['budget_total'] > 0  # type: ignore[index]
        excess = js_max(0, js_min(s['amount'], after - budget_m)) if has_budget else s['amount']
        if excess <= 0:
            continue
        unbudgeted.append(
            {
                'key': f'{s["source"]}:{s["ref_id"]}:{s["date"]}',
                'source': s['source'],
                'ref_id': s['ref_id'],
                'date': s['date'],
                'category': line['label'] if line else s['category'],
                'description': s['description'],
                'amount': s['amount'],
                'excess': excess,
                'reason': 'over_budget' if has_budget else 'no_budget',
                'state': s['state'],
                'approved_by': s['approved_by'],
                'approved_by_name': user_name(ctx, s['approved_by']) if js_truthy(s['approved_by']) else 'Direct ledger entry',
                'justification': None,
                'counted': True,
            }
        )
    for po in rows(ctx, 'purchase_orders'):
        if not js_truthy(po.get('over_budget')) or po['status'] not in ('approved', 'closed'):
            continue
        d = coalesce(po.get('decided_at'), po['created_at'])[:10]
        if d < frm or d > to:
            continue
        v = next((x for x in rows(ctx, 'vendors') if x['id'] == po['vendor_id']), None)
        rem = po.get('budget_remaining_paise')
        title = po.get('title')
        unbudgeted.append(
            {
                'key': f'po:{po["id"]}',
                'source': 'po',
                'ref_id': po['id'],
                'date': d,
                'category': po['budget_category'],
                'description': f'{js_str(po["number"])} · {js_str(coalesce(v.get("name", UNDEF) if v else UNDEF, ""))}{" · " + js_str(title) if js_truthy(title) else ""}',
                'amount': po_total(po),
                'excess': po_total(po) if nullish(rem) else js_max(0, po_total(po) - js_max(0, rem)),
                'reason': 'no_budget' if nullish(rem) else 'over_budget',
                'state': 'commitment',
                'approved_by': po.get('decided_by'),
                'approved_by_name': user_name(ctx, po.get('decided_by')),
                'justification': coalesce(po.get('justification'), None),
                'counted': False,
            }
        )
    unbudgeted = sorted(unbudgeted, key=lambda u: locale_key(u['date']), reverse=True)

    monthly = [
        {
            'month': m,
            'budget': _sum(coalesce(_at(l['monthly_budget'], i), NAN) for l in expense),  # noqa: E741
            'actual': _sum(l['monthly_actual'][i] + l['monthly_committed'][i] for l in expense),  # noqa: E741
        }
        for i, m in enumerate(months)
    ]
    return {
        'fy_start_year': fy,
        'fy_label': _fy_label(fy),
        'months': months,
        'current_month_index': cur_idx,
        'expense': expense,
        'revenue': revenue,
        'totals': {
            'expense_budget': _sum(l['budget_total'] for l in expense),  # noqa: E741
            'expense_ytd_budget': _sum(l['ytd_budget'] for l in expense),  # noqa: E741
            'expense_ytd_actual': _sum(l['ytd_actual'] for l in expense),  # noqa: E741
            'expense_committed': _sum(_slice_sum(l['monthly_committed']) for l in expense),  # noqa: E741
            'revenue_budget': _sum(l['budget_total'] for l in revenue),  # noqa: E741
            'revenue_ytd_budget': _sum(l['ytd_budget'] for l in revenue),  # noqa: E741
            'revenue_ytd_actual': _sum(l['ytd_actual'] for l in revenue),  # noqa: E741
            'unbudgeted': _sum(u['excess'] for u in unbudgeted if u['counted']),
            'over_lines': sum(1 for l in expense if l['over'] and l['ytd_actual'] > 0),  # noqa: E741
        },
        'monthly': monthly,
        'unbudgeted': unbudgeted,
        'has_budgets': len(budgets) > 0,
        'prev_fy_has_budgets': len(_budgets_for(ctx, fy - 1)) > 0,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------------------------------------------------
def _approver_for(ctx: Ctx, user_id: Any, project_id: Any) -> Any:
    u = next((x for x in rows(ctx, 'users') if x['id'] == user_id), None)
    if js_truthy(project_id):
        p = next((x for x in rows(ctx, 'projects') if x['id'] == project_id), None)
        if p and js_truthy(p.get('pm_id')) and p['pm_id'] != user_id:
            return p['pm_id']
    if u and js_truthy(u.get('reports_to_id')):
        return u['reports_to_id']
    return admin_id(ctx)


def _is_duplicate(ctx: Ctx, e: dict) -> bool:
    return any(
        x['id'] != e['id'] and x['user_id'] == e['user_id'] and x['amount_paise'] == e['amount_paise'] and x['spent_on'] == e['spent_on'] and x['status'] != 'rejected'
        for x in rows(ctx, 'expense_claims')
    )


def _expense_row(ctx: Ctx, e: dict, pm: dict | None = None) -> dict[str, Any]:
    pm = _project_map(ctx) if pm is None else pm
    p = pm.get(e['project_id']) if js_truthy(e.get('project_id')) else None
    f = next((x for x in rows(ctx, 'files') if x['id'] == e['receipt_file_id']), None) if js_truthy(e.get('receipt_file_id')) else None
    is_admin = spend_authority(ctx)
    own = e['user_id'] == ctx.user_id
    admin = admin_id(ctx)
    pending = e['status'] == 'submitted'
    step = None
    if pending:
        ch = e.get('wf_chain')
        first = (coalesce(e.get('wf_step'), 0) > 0) if (js_truthy(ch) and len(ch)) else js_truthy(e.get('first_approved_by'))
        step = 'admin' if first or e.get('approver_id') == admin else 'manager'
    esc = escalation_target(ctx, e) if pending else None
    return {
        **e,
        'user_name': user_name(ctx, e['user_id']),
        'approver_name': user_name(ctx, e.get('approver_id')),
        'decided_by_name': user_name(ctx, e.get('decided_by')),
        'first_approved_by_name': user_name(ctx, e['first_approved_by']) if js_truthy(e.get('first_approved_by')) else None,
        'project_code': p['code'] if p else None,
        'project_name': p['name'] if p else None,
        'client': p['client'] if p else None,
        'ledger_category': EXPENSE_CATEGORY.get(e['category'], UNDEF),
        'receipt_name': coalesce(f.get('name', UNDEF) if f else UNDEF, None),
        'duplicate': _is_duplicate(ctx, e),
        'step': step,
        'step_label': step_label(e) if pending else None,
        'route': approval_route(ctx, e, pending),
        'due_on': step_due(ctx, e) if pending else None,
        'escalates_to': coalesce(esc['name'] if esc else UNDEF, None) if pending else None,
        'can_edit': own and (e['status'] == 'draft' or e['status'] == 'rejected'),
        'can_approve': e['status'] == 'submitted' and (e.get('approver_id') == ctx.user_id or is_admin) and (not own or is_admin),
        'can_pay': e['status'] == 'approved' and has(ctx, 'expenses.pay'),
    }


def _validate_expense(ctx: Ctx, body: Any, submitting: bool, existing: dict | None = None) -> dict[str, Any]:
    def ex(k: str) -> Any:
        return existing.get(k, UNDEF) if existing else UNDEF

    category = js_str(coalesce(_g(body, 'category'), ex('category'), ''))
    if category not in EXPENSE_CATEGORY:
        err(422, 'VAL_REQUIRED', 'Choose a category.', 'category')
    description = _need_text(coalesce(_g(body, 'description'), ex('description')), 'description', 'Description', 3, 200)
    amount = _money(coalesce(_g(body, 'amount_paise'), ex('amount_paise')), 'amount_paise', 'Amount')
    spent_on = _need_date(coalesce(_g(body, 'spent_on'), ex('spent_on')), 'spent_on', 'Date')
    if spent_on > ctx.today:
        err(422, 'DATE_IN_FUTURE', 'The expense date is in the future. Claim it after you spend it.', 'spent_on')
    if diff_days(ctx.today, spent_on) > EXPENSE_MAX_AGE_DAYS:
        err(422, 'EXPENSE_TOO_OLD', f'Claims must be filed within {js_str(EXPENSE_MAX_AGE_DAYS)} days of spending. Talk to accounts for anything older.', 'spent_on')
    bp = _g(body, 'project_id')
    project_id = coalesce(ex('project_id'), None) if bp is UNDEF else js_or(bp, None)
    if js_truthy(project_id):
        find(ctx, 'projects', project_id, 'Project')
    bb = _g(body, 'billable_to_client')
    billable = coalesce(ex('billable_to_client'), False) if bb is UNDEF else js_truthy(bb)
    if js_truthy(billable) and not js_truthy(project_id):
        err(422, 'VAL_REQUIRED', 'Pick the client project to re-bill this expense to.', 'project_id')
    return {'category': category, 'description': description, 'amount_paise': amount, 'spent_on': spent_on, 'project_id': project_id, 'billable_to_client': billable, 'submitting': submitting}


def _require_receipt(amount: Any, file_id: Any) -> None:
    if amount > RECEIPT_LIMIT_PAISE and not js_truthy(file_id):
        err(422, 'RECEIPT_REQUIRED', f'Attach the receipt — it is required for claims above {fmt_money(RECEIPT_LIMIT_PAISE)}.', 'receipt')


def _expense_input(e: dict) -> dict[str, Any]:
    return {'requester_id': e['user_id'], 'amount_paise': e['amount_paise'], 'project_id': e.get('project_id', UNDEF)}


def _submit_expense(ctx: Ctx, e: dict) -> dict:
    # Route through the tenant's expense workflow; the v3 rule decides when the workflow names nobody.
    wf = start_approval(ctx, 'expense', _expense_input(e), _approver_for(ctx, e['user_id'], e.get('project_id')))
    approver = wf['approver_id']
    r = update(
        ctx,
        'expense_claims',
        e['id'],
        {
            'status': 'submitted',
            'approver_id': approver,
            'decided_by': None,
            'decided_at': None,
            'decision_reason': None,
            'in_budget': None,
            'submitted_at': now_iso(),
            'first_approved_by': None,
            **wf_patch(wf),
        },
    )
    if _is_duplicate(ctx, r):
        warn(ctx, 'DUPLICATE_EXPENSE', f'You already have a {fmt_money(r["amount_paise"])} claim on this date. Check it isn’t the same expense.')
    if js_truthy(approver) and approver != ctx.user_id:
        notify(ctx, approver, 'NTF-04', 'Expense claim to approve', f'{user_name(ctx, e["user_id"])} claimed {fmt_money(e["amount_paise"])} — {js_str(e["description"])}.', '/expenses?tab=approve')
    log_activity(ctx, 'expense', e['id'], 'submitted', f'Submitted {fmt_money(e["amount_paise"])} · {js_str(e["description"])}')
    return r


def _expense_list(ctx: Ctx, tab: str, q: dict) -> dict[str, Any]:
    all_ = rows(ctx, 'expense_claims')
    can_all = _sees_all_claims(ctx)
    can_pay = has(ctx, 'expenses.pay')
    is_admin = spend_authority(ctx)
    me = ctx.user_id
    mine = [e for e in all_ if e['user_id'] == me]
    approve = [
        e
        for e in all_
        if e['status'] == 'submitted' and (e.get('approver_id') == me or (is_admin and e['user_id'] != me) or (is_admin and e.get('approver_id') == me))
    ]
    pay = [e for e in all_ if e['status'] == 'approved'] if can_pay else []
    can_approve = len(approve) > 0 or is_admin or has(ctx, 'expenses.approve')
    if tab == 'all' and not can_all:
        err(403, 'FORBIDDEN', 'Only admin and HR & operations can see every claim.')
    if tab == 'pay' and not can_pay:
        err(403, 'FORBIDDEN', 'Only admin and HR & operations pay claims.')
    lst = mine if tab == 'mine' else approve if tab == 'approve' else pay if tab == 'pay' else list(all_)
    qs = q.get('status', UNDEF)
    status = js_str(qs) if js_truthy(qs) else ''
    if status:
        lst = [e for e in lst if e['status'] == status]
    qq = q.get('q', UNDEF)
    if js_truthy(qq):
        s = _lc(js_str(qq))
        lst = [e for e in lst if s in _lc(e['description']) or s in _lc(user_name(ctx, e['user_id']))]
    pm = _project_map(ctx)
    items = sorted((_expense_row(ctx, e, pm) for e in lst), key=lambda x: (locale_key(x['spent_on']), locale_key(x['created_at'])), reverse=True)
    base = mine if tab == 'mine' else all_ if tab == 'all' else lst

    def tot(f: Callable[[dict], bool]) -> Any:
        return _sum(e['amount_paise'] for e in base if f(e))

    return {
        'tab': tab,
        'items': items,
        'counts': {
            'mine': sum(1 for e in mine if e['status'] in ('draft', 'rejected')),
            'approve': len(approve),
            'pay': len(pay) if can_pay else None,
            'all': sum(1 for e in all_ if e['status'] == 'submitted') if can_all else None,
        },
        'totals': {
            'claimed': tot(lambda e: e['status'] != 'draft'),
            'pending': tot(lambda e: e['status'] == 'submitted'),
            'approved_unpaid': tot(lambda e: e['status'] == 'approved'),
            'paid': tot(lambda e: e['status'] == 'paid'),
            'rejected': tot(lambda e: e['status'] == 'rejected'),
            'out_of_budget': tot(lambda e: e['status'] in ('approved', 'paid') and e.get('in_budget') is False),
        },
        'can': {'approve': can_approve, 'pay': can_pay, 'all': can_all},
    }


# ---------------------------------------------------------------------------------------------------------------------
# Purchases
# ---------------------------------------------------------------------------------------------------------------------
def po_total(po: dict) -> Any:
    return po['amount_paise'] + js_round((po['amount_paise'] * po['gst_pct']) / 100)


def _validate_vendor(ctx: Ctx, body: Any, existing: dict | None = None) -> dict[str, Any]:
    def ex(k: str) -> Any:
        return existing.get(k, UNDEF) if existing else UNDEF

    def bg(k: str) -> Any:
        return _g(body, k)

    name = _need_text(coalesce(bg('name'), ex('name')), 'name', 'Vendor name', 2, 120)
    ex_id = ex('id')
    dup = next((v for v in rows(ctx, 'vendors') if _lc(v['name']) == _lc(name) and v['id'] != ex_id), None)
    if dup:
        err(409, 'DUPLICATE_VENDOR', f'{js_str(dup["name"])} is already in the vendor list.', 'name')
    gstin = js_upper(js_trim(js_str(coalesce(bg('gstin'), ex('gstin'), ''))))
    pan = js_upper(js_trim(js_str(coalesce(bg('pan'), ex('pan'), ''))))
    if gstin and (js_len(gstin) != 15 or not GSTIN_RE.search(gstin)):
        err(
            422,
            'VAL_GSTIN',
            'GSTIN must be 15 characters: 2-digit state code, 10-character PAN, entity number, “Z” and a check character (e.g. 21AAKFM9876Q1ZP).',
            'gstin',
        )
    if pan and (js_len(pan) != 10 or not PAN_RE.search(pan)):
        err(422, 'VAL_PAN', 'PAN must be 10 characters: 5 letters, 4 digits, 1 letter (e.g. AAKFM9876Q).', 'pan')
    if gstin and pan and gstin[2:12] != pan:
        err(422, 'VAL_PAN', 'The PAN does not match characters 3–12 of the GSTIN.', 'pan')
    terms = js_number(coalesce(bg('payment_terms_days'), ex('payment_terms_days'), 30))
    if not is_integer(terms) or terms < 0 or terms > 180:
        err(422, 'VAL_RANGE', 'Payment terms must be between 0 and 180 days.', 'payment_terms_days')
    email = js_trim(js_str(coalesce(bg('email'), ex('email'), '')))
    if email and not _EMAIL_RE.search(email):
        err(422, 'VAL_EMAIL', 'Enter a valid email address.', 'email')
    bank_name = js_slice(js_trim(js_str(coalesce(bg('bank_name'), ex('bank_name'), ''))), 0, 80)
    bank_account = re.sub(r'[\s-]+', '', js_str(coalesce(bg('bank_account'), ex('bank_account'), '')))
    if bank_account and not re.search(r'^[0-9]{9,18}\Z', bank_account):
        err(422, 'VAL_BANK', 'The account number must be 9 to 18 digits.', 'bank_account')
    ifsc = js_upper(js_trim(js_str(coalesce(bg('ifsc'), ex('ifsc'), ''))))
    if ifsc and not re.search(r'^[A-Z]{4}0[A-Z0-9]{6}\Z', ifsc):
        err(422, 'VAL_IFSC', 'IFSC must be 11 characters: 4 letters, a zero, then 6 letters or digits (e.g. HDFC0001234).', 'ifsc')
    if bank_account and not ifsc:
        err(422, 'VAL_IFSC', 'Add the IFSC of the bank branch for this account.', 'ifsc')
    if bank_account and not bank_name:
        err(422, 'VAL_REQUIRED', 'Add the bank name for this account.', 'bank_name')
    return {
        'name': name,
        'gstin': gstin,
        'pan': js_or(pan, gstin[2:12] if gstin else ''),
        'payment_terms_days': terms,
        'email': email,
        'category': js_or(js_trim(js_str(coalesce(bg('category'), ex('category'), 'Other'))), 'Other'),
        'contact': js_trim(js_str(coalesce(bg('contact'), ex('contact'), ''))),
        'phone': js_trim(js_str(coalesce(bg('phone'), ex('phone'), ''))),
        'bank_name': bank_name,
        'bank_account': bank_account,
        'ifsc': ifsc,
    }


def _vendor_show(v: dict, keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        val = v.get(k, UNDEF)
        if k == 'bank_account':
            shown = js_or(mask_account(js_str(coalesce(val, ''))), '—')
        else:
            shown = '—' if (val == '' and isinstance(val, str)) or nullish(val) else val
        out[VENDOR_FIELD_LABEL.get(k, k)] = shown
    return out


def _can_decide_po(ctx: Ctx, po: dict) -> bool:
    """Current workflow approver or spend authority; never your own PO unless you hold spend authority (v3)."""
    if spend_authority(ctx):
        return True
    return po.get('approver_id') == ctx.user_id and po['requested_by'] != ctx.user_id


def _can_decide_bill(ctx: Ctx, b: dict) -> bool:
    return spend_authority(ctx) or (js_truthy(b.get('approver_id')) and b['approver_id'] == ctx.user_id and b.get('created_by') != ctx.user_id)


def _po_row(ctx: Ctx, po: dict, vendors: dict, pm: dict) -> dict[str, Any]:
    v = vendors.get(po['vendor_id'])
    p = pm.get(po['project_id']) if js_truthy(po.get('project_id')) else None
    bills = [b for b in rows(ctx, 'vendor_bills') if b.get('po_id') == po['id'] and b['status'] != 'disputed']
    billed = _sum(b['amount_paise'] for b in bills)
    total = po_total(po)
    pending = po['status'] == 'pending'
    return {
        **po,
        'vendor_name': v['name'] if v else '—',
        'vendor_status': v['status'] if v else 'active',
        'project_code': p['code'] if p else None,
        'project_name': p['name'] if p else None,
        'requested_by_name': user_name(ctx, po['requested_by']),
        'decided_by_name': user_name(ctx, po.get('decided_by')),
        'total_paise': total,
        'gst_amount_paise': total - po['amount_paise'],
        'billed_paise': billed,
        'balance_paise': js_max(0, po['amount_paise'] - billed),
        'bills': sum(1 for b in rows(ctx, 'vendor_bills') if b.get('po_id') == po['id']),
        'approver_name': user_name(ctx, po.get('approver_id')) if pending else '—',
        'step_label': step_label(po) if pending else None,
        'route': approval_route(ctx, po, pending),
        'can_approve': pending and _can_decide_po(ctx, po),
    }


def _po_input(po: dict) -> dict[str, Any]:
    return {'requester_id': po['requested_by'], 'amount_paise': po_total(po), 'project_id': po.get('project_id', UNDEF)}


def _bill_input(ctx: Ctx, b: dict) -> dict[str, Any]:
    po = next((x for x in rows(ctx, 'purchase_orders') if x['id'] == b['po_id']), None) if js_truthy(b.get('po_id')) else None
    return {
        'requester_id': coalesce(b.get('created_by'), ctx.user_id),
        'amount_paise': b['amount_paise'] + b['gst_paise'],
        'project_id': coalesce(po.get('project_id', UNDEF) if po else UNDEF, None),
    }


def _bill_row(ctx: Ctx, b: dict, vendors: dict, pos: dict) -> dict[str, Any]:
    po = pos.get(b['po_id']) if js_truthy(b.get('po_id')) else None
    f = next((x for x in rows(ctx, 'files') if x['id'] == b['file_id']), None) if js_truthy(b.get('file_id')) else None
    over = (b['amount_paise'] - po['amount_paise']) / po['amount_paise'] if po and js_truthy(po['amount_paise']) else None
    v = vendors.get(b['vendor_id'])
    received = b['status'] == 'received'
    return {
        **b,
        'category': _bill_category(ctx, b),
        'vendor_name': v['name'] if v else '—',
        'po_number': po['number'] if po else None,
        'po_amount_paise': po['amount_paise'] if po else None,
        'over_po_pct': over if over is not None and over > 0 else None,
        'total_paise': b['amount_paise'] + b['gst_paise'],
        'net_payable_paise': b['amount_paise'] + b['gst_paise'] - b['tds_paise'],
        'days_overdue': 0 if b['status'] == 'paid' else js_max(0, diff_days(ctx.today, b['due_on'])),
        'approved_by_name': user_name(ctx, b.get('approved_by')),
        'file_name': coalesce(f.get('name', UNDEF) if f else UNDEF, None),
        'approver_name': user_name(ctx, coalesce(b.get('approver_id'), admin_id(ctx))) if received else '—',
        'step_label': step_label(b) if received else None,
        'can_approve': received and _can_decide_bill(ctx, b),
    }


def _purchases(ctx: Ctx) -> dict[str, Any]:
    vendors_raw = rows(ctx, 'vendors')
    vendors = {v['id']: v for v in vendors_raw}
    pm = _project_map(ctx)
    pos_raw = rows(ctx, 'purchase_orders')
    pos_map = {p['id']: p for p in pos_raw}
    pos = sorted((_po_row(ctx, p, vendors, pm) for p in pos_raw), key=lambda x: locale_key(x['number']), reverse=True)
    bills = sorted((_bill_row(ctx, b, vendors, pos_map) for b in rows(ctx, 'vendor_bills')), key=lambda x: locale_key(x['bill_date']), reverse=True)
    frm = _fy_range(_cur_fy(ctx), _fy_month(ctx))['from']
    unpaid = [b for b in bills if b['status'] != 'paid']
    buckets = [
        {'key': 'not_due', 'label': 'Not yet due', 'amount': 0, 'count': 0},
        {'key': '0_30', 'label': '1–30 days', 'amount': 0, 'count': 0},
        {'key': '31_60', 'label': '31–60 days', 'amount': 0, 'count': 0},
        {'key': '61_90', 'label': '61–90 days', 'amount': 0, 'count': 0},
        {'key': '90_plus', 'label': 'Over 90 days', 'amount': 0, 'count': 0},
    ]
    for b in unpaid:
        d = b['days_overdue']
        k = 0 if d <= 0 else 1 if d <= 30 else 2 if d <= 60 else 3 if d <= 90 else 4
        buckets[k]['amount'] += b['net_payable_paise']
        buckets[k]['count'] += 1
    full_bank = has(ctx, 'procurement.manage')
    vendor_rows = []
    for v in vendors_raw:
        vb = [b for b in bills if b['vendor_id'] == v['id']]
        bank_account = v.get('bank_account', UNDEF)
        rest = {k: x for k, x in v.items() if k != 'bank_account'}
        vendor_rows.append(
            {
                **rest,
                'bank_name': coalesce(v.get('bank_name', UNDEF), ''),
                'ifsc': coalesce(v.get('ifsc', UNDEF), ''),
                'bank_account_masked': mask_account(bank_account),
                **({'bank_account': coalesce(bank_account, '')} if full_bank else {}),
                'pending_change': pending_brief(ctx, 'vendor', v['id']),
                'open_pos': sum(1 for p in pos if p['vendor_id'] == v['id'] and p['status'] in ('approved', 'pending')),
                'billed_fy': _sum(b['total_paise'] for b in vb if b['bill_date'] >= frm),
                'payable': _sum(b['net_payable_paise'] for b in vb if b['status'] != 'paid'),
                'overdue': _sum(b['net_payable_paise'] for b in vb if b['status'] != 'paid' and b['days_overdue'] > 0),
            }
        )
    vendor_rows = sorted(vendor_rows, key=lambda x: locale_key(x['name']))
    return {
        'vendors': vendor_rows,
        'pos': pos,
        'bills': bills,
        'ageing': buckets,
        'vendor_checked': needs_checker(ctx, 'vendor'),
        'can_approve': spend_authority(ctx) or any(p['can_approve'] for p in pos) or any(b['can_approve'] for b in bills),
        'totals': {
            'open_po_value': _sum(p['balance_paise'] for p in pos if p['status'] == 'approved'),
            'pending_po': sum(1 for p in pos if p['status'] == 'pending'),
            'payable': _sum(b['net_payable_paise'] for b in unpaid if b['status'] != 'disputed'),
            'overdue': _sum(b['net_payable_paise'] for b in unpaid if b['status'] != 'disputed' and b['days_overdue'] > 0),
            'paid_fy': _sum(b['net_payable_paise'] for b in bills if b['status'] == 'paid' and coalesce(b.get('paid_on'), '') >= frm),
            'disputed': _sum(b['total_paise'] for b in unpaid if b['status'] == 'disputed'),
        },
    }


def _validate_po(ctx: Ctx, body: Any, existing: dict | None = None) -> dict[str, Any]:
    def ex(k: str) -> Any:
        return existing.get(k, UNDEF) if existing else UNDEF

    def bg(k: str) -> Any:
        return _g(body, k)

    vendor_id = js_str(coalesce(bg('vendor_id'), ex('vendor_id'), ''))
    if not vendor_id:
        err(422, 'VAL_REQUIRED', 'Choose a vendor.', 'vendor_id')
    v = find(ctx, 'vendors', vendor_id, 'Vendor')
    if v['status'] == 'blocked':
        err(409, 'VENDOR_BLOCKED', f'{js_str(v["name"])} is blocked. Unblock the vendor before raising a PO.', 'vendor_id')
    lines_in = coalesce(bg('lines'), ex('lines'), [])
    if not isinstance(lines_in, list) or not len(lines_in):
        err(422, 'VAL_REQUIRED', 'Add at least one line.', 'lines')
    lines = []
    for i, l in enumerate(lines_in):  # noqa: E741
        description = js_trim(js_str(coalesce(_g(l, 'description'), '')))
        if not description:
            err(422, 'VAL_REQUIRED', f'Line {i + 1} needs a description.', 'lines')
        qty = js_number(_g(l, 'qty'))
        if not is_finite(qty) or qty <= 0:
            err(422, 'VAL_RANGE', f'Line {i + 1}: quantity must be more than zero.', 'lines')
        rate = _money(_g(l, 'rate_paise'), 'lines', f'Line {i + 1} rate')
        lines.append({'description': description, 'qty': qty, 'rate_paise': rate})
    amount = _sum(js_round(l['qty'] * l['rate_paise']) for l in lines)  # noqa: E741
    gst = js_number(coalesce(bg('gst_pct'), ex('gst_pct'), 18))
    if gst not in (0, 5, 12, 18, 28):
        err(422, 'VAL_RANGE', 'GST must be 0, 5, 12, 18 or 28%.', 'gst_pct')
    budget_category = _need_text(coalesce(bg('budget_category'), ex('budget_category')), 'budget_category', 'Budget category')
    bp = bg('project_id')
    project_id = coalesce(ex('project_id'), None) if bp is UNDEF else js_or(bp, None)
    if js_truthy(project_id):
        find(ctx, 'projects', project_id, 'Project')
    bn = bg('needed_by')
    needed_by = coalesce(ex('needed_by'), None) if bn is UNDEF else js_or(bn, None)
    title = js_slice(js_trim(js_str(coalesce(bg('title'), ex('title'), lines[0]['description']))), 0, 120)
    justification = js_or(js_trim(js_str(coalesce(bg('justification'), ex('justification'), ''))), None)
    return {
        'vendor_id': vendor_id,
        'lines': lines,
        'amount_paise': amount,
        'gst_pct': gst,
        'budget_category': budget_category,
        'project_id': project_id,
        'needed_by': needed_by,
        'title': title,
        'justification': justification,
    }


def _submit_po(ctx: Ctx, po: dict) -> dict:
    total = po_total(po)
    nb = po.get('needed_by')
    rem = _fy_remaining(ctx, po['budget_category'], nb if js_truthy(nb) and nb > ctx.today else ctx.today, po['id'])
    over = not rem or rem['remaining'] < total
    if over and js_len(js_trim(coalesce(po.get('justification'), ''))) < 15:
        cat_ = js_str(po['budget_category'])
        if rem:
            msg = f'This PO is {fmt_money(total - js_max(0, rem["remaining"]))} over the remaining {cat_} budget ({fmt_money(js_max(0, rem["remaining"]))} left this FY). Add a justification of at least 15 characters.'
        else:
            msg = f'There is no {cat_} budget this FY. Add a justification of at least 15 characters.'
        err(422, 'JUSTIFICATION_REQUIRED', msg, 'justification')
    wf = start_approval(ctx, 'purchase_order', _po_input(po), admin_id(ctx))
    admin = wf['approver_id']
    r = update(
        ctx,
        'purchase_orders',
        po['id'],
        {
            'status': 'pending',
            'approver_id': admin,
            'over_budget': over,
            'budget_remaining_paise': rem['remaining'] if rem else None,
            'decided_by': None,
            'decided_at': None,
            'decision_reason': None,
            **wf_patch(wf),
        },
    )
    if over:
        warn(ctx, 'OVER_BUDGET', f'Over budget: {user_name(ctx, admin) if js_truthy(admin) else "the approver"} will see your justification when approving.')
    if js_truthy(admin) and admin != ctx.user_id:
        notify(ctx, admin, 'NTF-04', 'Purchase order to approve', f'{js_str(po["number"])} · {fmt_money(total)}{" · over budget" if over else ""}', '/purchases?tab=pos')
    log_activity(ctx, 'purchase_order', po['id'], 'submitted', f'Submitted {js_str(po["number"])} for {fmt_money(total)}{" (over budget)" if over else ""}')
    return r


def _validate_bill(ctx: Ctx, body: Any, existing: dict | None = None) -> dict[str, Any]:
    def ex(k: str) -> Any:
        return existing.get(k, UNDEF) if existing else UNDEF

    def bg(k: str) -> Any:
        return _g(body, k)

    vendor_id = js_str(coalesce(bg('vendor_id'), ex('vendor_id'), ''))
    if not vendor_id:
        err(422, 'VAL_REQUIRED', 'Choose a vendor.', 'vendor_id')
    v = find(ctx, 'vendors', vendor_id, 'Vendor')
    bp = bg('po_id')
    po_id = coalesce(ex('po_id'), None) if bp is UNDEF else js_or(bp, None)
    number = _need_text(coalesce(bg('number'), ex('number')), 'number', 'Bill number', 1, 40)
    bill_date = _need_date(coalesce(bg('bill_date'), ex('bill_date')), 'bill_date', 'Bill date')
    if bill_date > ctx.today:
        err(422, 'DATE_IN_FUTURE', 'The bill date is in the future.', 'bill_date')
    amount = _money(coalesce(bg('amount_paise'), ex('amount_paise')), 'amount_paise', 'Bill amount')
    gst = _money(coalesce(bg('gst_paise'), ex('gst_paise'), 0), 'gst_paise', 'GST', True)
    tds = _money(coalesce(bg('tds_paise'), ex('tds_paise'), 0), 'tds_paise', 'TDS', True)
    if tds > amount:
        err(422, 'VAL_RANGE', 'TDS cannot be more than the bill amount.', 'tds_paise')
    ex_id = ex('id')
    dup = next((b for b in rows(ctx, 'vendor_bills') if b['vendor_id'] == vendor_id and _lc(b['number']) == _lc(number) and b['id'] != ex_id), None)
    if dup:
        err(409, 'DUPLICATE_BILL', f'Bill {number} from {js_str(v["name"])} is already recorded ({js_str(dup["status"])}). Check you are not entering it twice.', 'number')
    category = None
    if js_truthy(po_id):
        po = find(ctx, 'purchase_orders', po_id, 'Purchase order')
        if po['vendor_id'] != vendor_id:
            err(422, 'VAL_PO', 'That PO belongs to another vendor.', 'po_id')
        if po['status'] not in ('approved', 'closed'):
            err(409, 'PO_NOT_APPROVED', f'{js_str(po["number"])} is {js_str(po["status"])}. Bills can only be booked against an approved PO.', 'po_id')
        others = _sum(b['amount_paise'] for b in rows(ctx, 'vendor_bills') if b.get('po_id') == po_id and b['id'] != ex_id and b['status'] != 'disputed')
        limit = js_round(po['amount_paise'] * 1.05)
        if others + amount > limit:
            over = (others + amount - po['amount_paise']) / po['amount_paise']
            err(
                409,
                'BILL_EXCEEDS_PO',
                f'Bills against {js_str(po["number"])} would total {fmt_money(others + amount)} — {to_fixed(over * 100, 1)}% over the PO value of {fmt_money(po["amount_paise"])} (5% tolerance). Raise a revised PO or dispute the bill with the vendor.',
                'amount_paise',
            )
    else:
        category = js_or(js_str(coalesce(bg('category'), ex('category'), v.get('category', UNDEF), 'Other')), 'Other')
    bd = bg('due_on')
    due_on = _need_date(bd, 'due_on', 'Due date') if js_truthy(bd) else add_days(bill_date, v['payment_terms_days'])
    return {
        'vendor_id': vendor_id,
        'po_id': po_id,
        'number': number,
        'bill_date': bill_date,
        'due_on': due_on,
        'amount_paise': amount,
        'gst_paise': gst,
        'tds_paise': tds,
        'category': category,
        'description': js_trim(js_str(coalesce(bg('description'), ex('description'), ''))),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Revenue leakage
# ---------------------------------------------------------------------------------------------------------------------
def leakage_rows(ctx: Ctx) -> list[dict]:
    pm = _project_map(ctx)
    out: list[dict] = []

    def base(id_: str) -> dict[str, Any]:
        return {'id': id_, 'tenant_id': ctx.tenant_id, 'created_at': ctx.today + 'T00:00:00Z', 'updated_at': ctx.today + 'T00:00:00Z', 'version': 1}

    def push(r: dict, source: Any) -> None:
        p = pm.get(r['project_id']) if js_truthy(r.get('project_id')) else None
        out.append(
            {
                **r,
                'kind_label': LEAKAGE_KIND[r['kind']]['label'],
                'project_code': p['code'] if p else None,
                'project_name': p['name'] if p else None,
                'recorded_by_name': 'System' if js_truthy(r.get('auto')) else user_name(ctx, r.get('recorded_by')),
                'open_paise': js_max(0, r['amount_paise'] - r['recovered_paise']) if r['status'] == 'open' else 0,
                'source': source,
            }
        )

    # 1. lost deals
    for d in rows(ctx, 'deals'):
        if d['stage'] != 'lost':
            continue
        push(
            {
                **base('auto_deal_' + d['id']),
                'kind': 'lost_deal',
                'amount_paise': d['value_paise'],
                'date': coalesce(d.get('lost_on'), d['updated_at'][:10]),
                'client': d['client'],
                'project_id': None,
                'deal_id': d['id'],
                'reason': js_or(d.get('lost_reason'), 'No reason recorded'),
                'recorded_by': d['owner_id'],
                'recoverable': False,
                'status': 'accepted',
                'recovered_paise': 0,
                'auto': True,
            },
            f'Deal · {js_str(d["title"])}',
        )
    # 2. unbilled hours
    entries = [e for e in costed_entries(ctx) if e.get('kind') == 'task' and e['approved'] and js_truthy(e.get('task_id'))]
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    for p in pm.values():
        if not js_truthy(p.get('contract_value_paise')) or (p['billing_model'] != 'fixed' and p['billing_model'] != 'tm'):
            continue
        pt = [t for t in rows(ctx, 'tasks') if t['project_id'] == p['id']]
        est = _sum(t['est_hours'] for t in pt)
        if not js_truthy(est):
            continue
        rate = p['contract_value_paise'] / est  # blended billing rate per hour
        by_task: dict[str, Any] = {}
        for e in entries:
            t = tasks.get(e['task_id'])
            if not t or t['project_id'] != p['id']:
                continue
            by_task[t['id']] = by_task.get(t['id'], 0) + e['hours']
        if p['billing_model'] == 'fixed':
            over: Any = 0
            for tid, h in by_task.items():
                t = tasks.get(tid)
                over += js_max(0, h - coalesce(t.get('est_hours', UNDEF) if t else UNDEF, 0))
            if over >= 1:
                push(
                    {
                        **base('auto_unbilled_' + p['id']),
                        'kind': 'unbilled_hours',
                        'amount_paise': js_round(over * rate),
                        'date': ctx.today,
                        'client': p['client'],
                        'project_id': p['id'],
                        'deal_id': None,
                        'reason': f'{to_fixed(over, 1)} approved hours beyond task estimates on a fixed-price project, at the blended rate of {fmt_money(js_round(rate))}/h. Raise a change request to recover.',
                        'recorded_by': coalesce(p.get('pm_id'), ''),
                        'recoverable': True,
                        'status': 'open',
                        'recovered_paise': 0,
                        'auto': True,
                    },
                    'Timesheets beyond estimate',
                )
        else:
            hours = _sum(by_task.values())
            invoiced = _sum(i['amount_paise'] for i in rows(ctx, 'invoices') if i['project_id'] == p['id'])
            gap = js_round(hours * rate) - invoiced
            if gap > 0:
                push(
                    {
                        **base('auto_unbilled_' + p['id']),
                        'kind': 'unbilled_hours',
                        'amount_paise': gap,
                        'date': ctx.today,
                        'client': p['client'],
                        'project_id': p['id'],
                        'deal_id': None,
                        'reason': f'{to_fixed(hours, 1)} approved T&M hours worth {fmt_money(js_round(hours * rate))}; only {fmt_money(invoiced)} invoiced so far.',
                        'recorded_by': coalesce(p.get('pm_id'), ''),
                        'recoverable': True,
                        'status': 'open',
                        'recovered_paise': 0,
                        'auto': True,
                    },
                    'Approved T&M hours not invoiced',
                )
    # 3. overdue invoices > 90 days
    for inv in rows(ctx, 'invoices'):
        outstanding = inv['amount_paise'] - inv['paid_amount_paise']
        if outstanding <= 0:
            continue
        days = diff_days(ctx.today, inv['due_on'])
        if days <= 90:
            continue
        p = pm.get(inv['project_id'])
        push(
            {
                **base('auto_baddebt_' + inv['id']),
                'kind': 'bad_debt',
                'amount_paise': outstanding,
                'date': add_days(inv['due_on'], 90),
                'client': p['client'] if p else '—',
                'project_id': inv['project_id'],
                'deal_id': None,
                'reason': f'Invoice {js_str(inv["number"])} is {js_str(days)} days past due with {fmt_money(outstanding)} outstanding.',
                'recorded_by': coalesce(p.get('pm_id', UNDEF) if p else UNDEF, ''),
                'recoverable': True,
                'status': 'open',
                'recovered_paise': 0,
                'auto': True,
            },
            f'Invoice {js_str(inv["number"])}',
        )
    # 4. change requests rejected after work started
    for cr in rows(ctx, 'change_requests'):
        if cr['status'] != 'rejected' or not js_truthy(cr.get('decided_at')):
            continue
        p = pm.get(cr['project_id'])
        if not p or cr['decided_at'][:10] < p['start_on']:
            continue
        push(
            {
                **base('auto_cr_' + cr['id']),
                'kind': 'scope_creep',
                'amount_paise': cr['value_paise'],
                'date': cr['decided_at'][:10],
                'client': p['client'],
                'project_id': p['id'],
                'deal_id': None,
                'reason': f'Change request “{js_str(cr["title"])}” ({js_str(cr["hours"])}h) was rejected after work started.',
                'recorded_by': cr['raised_by'],
                'recoverable': False,
                'status': 'accepted',
                'recovered_paise': 0,
                'auto': True,
            },
            f'Change request · {js_str(cr["title"])}',
        )
    # manual rows
    for r in rows(ctx, 'revenue_leakage'):
        push(r, None)
    return sorted(out, key=lambda x: locale_key(x['date']), reverse=True)


def _leakage_result(ctx: Ctx, fy: Any) -> dict[str, Any]:
    fr = _fy_range(fy, _fy_month(ctx))
    frm, to = fr['from'], fr['to']
    items = [r for r in leakage_rows(ctx) if frm <= r['date'] <= to]
    total = _sum(r['amount_paise'] for r in items)
    recovered = _sum(r['recovered_paise'] for r in items)
    billed = revenue_between(ctx, frm, to if to < ctx.today else ctx.today)['billed']
    kinds: dict[str, dict] = {}
    clients: dict[str, dict] = {}
    for r in items:
        k = kinds.setdefault(r['kind'], {'amount': 0, 'count': 0})
        k['amount'] += r['amount_paise'] - r['recovered_paise']
        k['count'] += 1
        c = clients.setdefault(r['client'], {'amount': 0, 'count': 0})
        c['amount'] += r['amount_paise'] - r['recovered_paise']
        c['count'] += 1
    months = _fy_months(ctx, fy)
    return {
        'fy_start_year': fy,
        'fy_label': _fy_label(fy),
        'items': items,
        'summary': {
            'total': total,
            'recovered': recovered,
            'net': total - recovered,
            'recoverable_open': _sum(r['open_paise'] for r in items if js_truthy(r.get('recoverable')) and r['status'] == 'open'),
            'billed': billed,
            'pct_of_billed': (total - recovered) / billed if js_truthy(billed) else None,
            'by_kind': sorted(
                ({'kind': kind, 'label': LEAKAGE_KIND[kind]['label'], **v} for kind, v in kinds.items()), key=lambda x: -x['amount']
            ),
            'by_month': [{'month': m, 'amount': _sum(r['amount_paise'] - r['recovered_paise'] for r in items if month_of(r['date']) == m)} for m in months],
            'top_clients': sorted(({'client': c, **v} for c, v in clients.items()), key=lambda x: -x['amount'])[:6],
        },
    }


# ---------------------------------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------------------------------
def _meta(req: Req) -> Any:
    ctx = req.ctx
    cats: list = []
    for c in [*BUDGET_EXPENSE_CATEGORIES, *(b['category'] for b in rows(ctx, 'budgets') if b['kind'] == 'expense')]:
        if c not in cats:
            cats.append(c)
    return {
        'expense_categories': [{'value': k, 'label': EXPENSE_CATEGORY_LABEL[k], 'ledger': EXPENSE_CATEGORY[k]} for k in EXPENSE_CATEGORY],
        'budget_categories': cats,
        'revenue_streams': [{'value': k, 'label': STREAM[k]} for k in STREAM],
        'projects': sorted(
            (
                {'id': p['id'], 'code': p['code'], 'name': p['name'], 'client': p['client'], 'billing_model': p['billing_model']}
                for p in rows(ctx, 'projects')
                if p['status'] != 'closed'
            ),
            key=lambda x: locale_key(x['code']),
        ),
        'vendors': sorted(
            (
                {'id': v['id'], 'name': v['name'], 'status': v['status'], 'payment_terms_days': v['payment_terms_days'], 'category': v.get('category', UNDEF)}
                for v in rows(ctx, 'vendors')
            ),
            key=lambda x: locale_key(x['name']),
        ),
        'today': ctx.today,
        'fy_start_year': _cur_fy(ctx),
        'fy_start_month': _fy_month(ctx),
    }


def _list_expenses(req: Req) -> Any:
    return _expense_list(req.ctx, js_str(coalesce(req.q('tab'), 'mine')), req.query)


def _get_expense(req: Req) -> Any:
    ctx = req.ctx
    e = find(ctx, 'expense_claims', req.params['id'], 'Expense claim')
    is_mine = e['user_id'] == ctx.user_id
    if not is_mine and e.get('approver_id') != ctx.user_id and e.get('first_approved_by') != ctx.user_id and not _sees_all_claims(ctx):
        p = next((x for x in rows(ctx, 'projects') if x['id'] == e['project_id']), None) if js_truthy(e.get('project_id')) else None
        if (p.get('pm_id', UNDEF) if p else UNDEF) != ctx.user_id:
            err(404, 'NOT_FOUND', 'Expense claim not found.')
    f = next((x for x in rows(ctx, 'files') if x['id'] == e['receipt_file_id']), None) if js_truthy(e.get('receipt_file_id')) else None
    hist = sorted((a for a in rows_where_ro(ctx, 'activity_log', 'entity_id', e['id']) if a.get('entity') == 'expense'), key=lambda a: locale_key(a['at']))
    return {
        **_expense_row(ctx, e),
        'receipt': {'id': f['id'], 'name': f['name'], 'mime': f.get('mime', UNDEF), 'data_url': f.get('data_url', UNDEF)} if f else None,
        'history': [{'at': a['at'], 'actor': user_name(ctx, a.get('actor_id')), 'action': a['action'], 'summary': a['summary']} for a in hist],
    }


def _create_expense(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    submit = js_truthy(_g(body, 'submit'))
    v = _validate_expense(ctx, body, submit)
    file_id = _save_file(ctx, _g(body, 'receipt'), 'expense', 'receipt')
    if submit:
        _require_receipt(v['amount_paise'], file_id)
    e = insert(
        ctx,
        'expense_claims',
        {
            'user_id': ctx.user_id,
            'project_id': v['project_id'],
            'category': v['category'],
            'description': v['description'],
            'amount_paise': v['amount_paise'],
            'spent_on': v['spent_on'],
            'receipt_file_id': file_id,
            'billable_to_client': v['billable_to_client'],
            'status': 'draft',
            'approver_id': None,
            'decided_by': None,
            'decided_at': None,
            'decision_reason': None,
            'paid_on': None,
            'ledger_entry_id': None,
            'in_budget': None,
        },
    )
    _link_file(ctx, file_id, e['id'])
    log_activity(ctx, 'expense', e['id'], 'created', f'Created claim {fmt_money(e["amount_paise"])} · {js_str(e["description"])}')
    if submit:
        return _expense_row(ctx, _submit_expense(ctx, e))
    if _is_duplicate(ctx, e):
        warn(ctx, 'DUPLICATE_EXPENSE', f'You already have a {fmt_money(e["amount_paise"])} claim on this date. Check it isn’t the same expense.')
    return _expense_row(ctx, e)


def _patch_expense(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    e = find(ctx, 'expense_claims', req.params['id'], 'Expense claim')
    if e['user_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person who filed a claim can edit it.')
    if e['status'] != 'draft' and e['status'] != 'rejected':
        err(409, 'EXPENSE_LOCKED', 'Submitted claims cannot be edited. Ask your approver to reject it first.')
    submit = js_truthy(_g(body, 'submit'))
    v = _validate_expense(ctx, body, submit, e)
    file_id = e.get('receipt_file_id')
    if js_truthy(_g(body, 'receipt')):
        file_id = _save_file(ctx, body['receipt'], 'expense', 'receipt')
    if js_truthy(_g(body, 'remove_receipt')):
        file_id = None
    _link_file(ctx, file_id, e['id'])
    if submit:
        _require_receipt(v['amount_paise'], file_id)
    u = update(
        ctx,
        'expense_claims',
        e['id'],
        {
            'category': v['category'],
            'description': v['description'],
            'amount_paise': v['amount_paise'],
            'spent_on': v['spent_on'],
            'project_id': v['project_id'],
            'billable_to_client': v['billable_to_client'],
            'receipt_file_id': file_id,
            'status': 'draft',
        },
        req.if_match,
    )
    log_activity(ctx, 'expense', e['id'], 'edited', f'Edited claim · {fmt_money(u["amount_paise"])}')
    return _expense_row(ctx, _submit_expense(ctx, u) if submit else u)


def _submit_expense_route(req: Req) -> Any:
    ctx = req.ctx
    e = find(ctx, 'expense_claims', req.params['id'], 'Expense claim')
    if e['user_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person who filed a claim can submit it.')
    if e['status'] != 'draft' and e['status'] != 'rejected':
        err(409, 'INVALID_STATE', f'This claim is already {js_str(e["status"])}.')
    if req.if_match is not UNDEF and req.if_match != e['version']:
        err(409, 'STALE', 'Someone else changed this record. Reload to see the latest version.')
    _validate_expense(ctx, {}, True, e)
    _require_receipt(e['amount_paise'], e.get('receipt_file_id'))
    return _expense_row(ctx, _submit_expense(ctx, e))


def _delete_expense(req: Req) -> Any:
    ctx = req.ctx
    e = find(ctx, 'expense_claims', req.params['id'], 'Expense claim')
    if e['user_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person who filed a claim can delete it.')
    if e['status'] != 'draft' and e['status'] != 'rejected':
        err(409, 'EXPENSE_LOCKED', 'Only draft or rejected claims can be deleted.')
    remove(ctx, 'expense_claims', e['id'], req.if_match)
    log_activity(ctx, 'expense', e['id'], 'deleted', f'Deleted claim · {js_str(e["description"])}')
    return {'ok': True}


def _cap_first(s: str) -> str:
    return js_upper(s[0]) + s[1:]


def _approve_expense(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    e = find(ctx, 'expense_claims', req.params['id'], 'Expense claim')
    if e['status'] != 'submitted':
        err(409, 'INVALID_STATE', f'This claim is {js_str(e["status"])}; only submitted claims can be approved.')
    is_admin = spend_authority(ctx)
    if e.get('approver_id') != ctx.user_id and not is_admin:
        err(403, 'FORBIDDEN', f'This claim is waiting for {user_name(ctx, e.get("approver_id"))}.')
    if e['user_id'] == ctx.user_id and not is_admin:
        err(403, 'SELF_APPROVAL', 'You cannot approve your own claim.')
    note = js_trim(js_str(coalesce(_g(body, 'note'), '')))
    me_name = js_str(ctx.user.get('name', UNDEF))
    # Multi-step workflow (default: manager / PM, then the CEO above ₹25,000).
    st = ensure_wf(ctx, e, 'expense', _expense_input(e), _approver_for(ctx, e['user_id'], e.get('project_id')))
    adv = advance_approval(ctx, st, ctx.user_id)
    if adv['next']:
        nx = adv['next']
        of = f'step {js_str(st["wf_step"] + 1)} of {len(st["wf_chain"])}'
        r2 = update(
            ctx,
            'expense_claims',
            e['id'],
            {'approver_id': nx['approver_id'], 'first_approved_by': coalesce(e.get('first_approved_by'), ctx.user_id), **wf_patch(adv['state'])},
            req.if_match,
        )
        done_name = coalesce(_p(adv['done'], 'name'), 'approval')
        log_activity(
            ctx,
            'expense',
            e['id'],
            'approved_step',
            f'{_cap_first(of)} ({js_str(done_name)}) approved by {me_name}; sent to {user_name(ctx, nx["approver_id"])} for {js_str(nx.get("name", UNDEF))}{" — " + note if note else ""}',
        )
        notify(
            ctx,
            nx['approver_id'],
            'NTF-04',
            'Expense claim to approve',
            f'{user_name(ctx, e["user_id"])} · {fmt_money(e["amount_paise"])} — approved by {me_name}, needs your approval ({js_str(nx.get("name", UNDEF))}).',
            '/expenses?tab=approve',
        )
        notify(
            ctx,
            e['user_id'],
            'NTF-05',
            'Expense approved by manager',
            f'{me_name} approved {fmt_money(e["amount_paise"])} · {js_str(e["description"])}. It now needs {user_name(ctx, nx["approver_id"])}’s approval.',
            '/expenses',
        )
        warn(ctx, 'SECOND_APPROVAL', f'{_cap_first(of)} approved — sent to {user_name(ctx, nx["approver_id"])} for {js_lower(nx["name"])}.')
        return _expense_row(ctx, r2)
    ledger_cat = EXPENSE_CATEGORY.get(e['category'], UNDEF)
    rem = month_remaining(ctx, ledger_cat, e['spent_on'], e['id'])
    in_budget = bool(rem) and rem['remaining'] >= e['amount_paise']  # type: ignore[index]
    r2 = update(
        ctx,
        'expense_claims',
        e['id'],
        {'status': 'approved', 'decided_by': ctx.user_id, 'decided_at': now_iso(), 'decision_reason': js_or(note, None), 'in_budget': in_budget, **wf_patch(adv['state'])},
        req.if_match,
    )
    if not in_budget:
        if rem:
            msg = f'Approved over budget: {js_str(ledger_cat)} has {fmt_money(js_max(0, rem["remaining"]))} left for {month_of(e["spent_on"])}. It is listed under unbudgeted spend.'
        else:
            msg = f'There is no {js_str(ledger_cat)} budget for this month. It is listed under unbudgeted spend.'
        warn(ctx, 'OVER_BUDGET', msg)
    log_activity(ctx, 'expense', e['id'], 'approved', f'Approved {fmt_money(e["amount_paise"])}{"" if in_budget else " (outside budget)"}{" — " + note if note else ""}')
    notify(ctx, e['user_id'], 'NTF-05', 'Expense approved', f'{me_name} approved {fmt_money(e["amount_paise"])} · {js_str(e["description"])}. Accounts will reimburse it.', '/expenses')
    return _expense_row(ctx, r2)


def _reject_expense(req: Req) -> Any:
    ctx = req.ctx
    e = find(ctx, 'expense_claims', req.params['id'], 'Expense claim')
    if e['status'] != 'submitted':
        err(409, 'INVALID_STATE', f'This claim is {js_str(e["status"])}; only submitted claims can be rejected.')
    if e.get('approver_id') != ctx.user_id and not spend_authority(ctx):
        err(403, 'FORBIDDEN', f'This claim is waiting for {user_name(ctx, e.get("approver_id"))}.')
    reason = js_trim(js_str(coalesce(_g(req.body, 'reason'), '')))
    if js_len(reason) < 5:
        err(422, 'REASON_REQUIRED', 'Say why the claim is rejected (at least 5 characters) so it can be fixed.', 'reason')
    r2 = update(
        ctx, 'expense_claims', e['id'], {'status': 'rejected', 'decided_by': ctx.user_id, 'decided_at': now_iso(), 'decision_reason': reason, **wf_clear()}, req.if_match
    )
    log_activity(ctx, 'expense', e['id'], 'rejected', f'Rejected — {reason}')
    notify(
        ctx,
        e['user_id'],
        'NTF-06',
        'Expense rejected',
        f'{js_str(ctx.user.get("name", UNDEF))} rejected {fmt_money(e["amount_paise"])} · {js_str(e["description"])}: {reason}',
        '/expenses',
    )
    return _expense_row(ctx, r2)


def _pay_expense(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'expenses.pay', 'Only admin and HR & operations pay claims.')
    e = find(ctx, 'expense_claims', req.params['id'], 'Expense claim')
    if e['status'] != 'approved':
        err(409, 'INVALID_STATE', 'This claim is already paid.' if e['status'] == 'paid' else 'Only approved claims can be paid.')
    bp = _g(req.body, 'paid_on')
    paid_on = _need_date(bp, 'paid_on', 'Payment date') if js_truthy(bp) else ctx.today
    if paid_on > ctx.today:
        err(422, 'DATE_IN_FUTURE', 'The payment date is in the future.', 'paid_on')
    p = next((x for x in rows(ctx, 'projects') if x['id'] == e['project_id']), None) if js_truthy(e.get('project_id')) else None
    le = insert(
        ctx,
        'ledger_entries',
        {
            'kind': 'expense',
            'category': EXPENSE_CATEGORY.get(e['category'], UNDEF),
            'description': f'Expense claim · {user_name(ctx, e["user_id"])} · {js_str(e["description"])}{" (re-bill to client)" if js_truthy(e.get("billable_to_client")) else ""}',
            'amount_paise': e['amount_paise'],
            'date': paid_on,
            'recurring': False,
            'until_on': None,
            'status': 'paid',
            'project_id': e.get('project_id', UNDEF),
            'milestone_id': None,
            'invoice_id': None,
            'stream': coalesce(p.get('stream', UNDEF) if p else UNDEF, None),
            'locked': True,
        },
    )
    r2 = update(ctx, 'expense_claims', e['id'], {'status': 'paid', 'paid_on': paid_on, 'ledger_entry_id': le['id']}, req.if_match)
    log_activity(ctx, 'expense', e['id'], 'paid', f'Reimbursed {fmt_money(e["amount_paise"])} on {paid_on}')
    notify(ctx, e['user_id'], 'NTF-05', 'Expense reimbursed', f'{fmt_money(e["amount_paise"])} for “{js_str(e["description"])}” has been paid.', '/expenses')
    return _expense_row(ctx, r2)


def _get_purchases(req: Req) -> Any:
    require_action(req.ctx, 'procurement.manage', 'Only admin and HR & operations manage purchases.')
    return _purchases(req.ctx)


def _create_vendor(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    v = _validate_vendor(ctx, req.body)
    shown = [k for k in VENDOR_FIELD_LABEL if not (v.get(k, UNDEF) == '' and isinstance(v.get(k), str)) and not nullish(v.get(k, UNDEF))]
    summary = f'Add vendor {v["name"]}{" · GSTIN " + v["gstin"] if v["gstin"] else ""}{" · account " + mask_account(v["bank_account"]) if v["bank_account"] else ""}'
    return propose(
        ctx,
        {
            'entity': 'vendor',
            'entity_id': None,
            'action': 'create',
            'required_permission': 'procurement.manage',
            'summary': summary,
            'before': None,
            'after': _vendor_show(v, shown),
            'payload': {'op': 'create', 'data': v},
        },
    )


def _patch_vendor(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    cur = find(ctx, 'vendors', req.params['id'], 'Vendor')
    if req.if_match is not UNDEF and req.if_match != cur['version']:
        err(409, 'STALE', 'Someone else changed this vendor. Reload to see the latest version.')
    pend = pending_brief(ctx, 'vendor', cur['id'])
    if pend:
        err(409, 'CHANGE_PENDING', f'A change to {js_str(cur["name"])} is waiting for approval ({pend["maker_name"]}). Withdraw it or wait for the checker before editing again.')
    v = _validate_vendor(ctx, req.body, cur)
    changed = [k for k in v if js_str(coalesce(v[k], '')) != js_str(coalesce(cur.get(k, UNDEF), ''))]
    if not changed:
        return {'pending': False, 'result': cur, 'result_id': cur['id']}
    sensitive = [k for k in changed if k in VENDOR_SENSITIVE]
    labels = ', '.join(VENDOR_FIELD_LABEL.get(k, k) for k in changed)
    if not sensitive or not needs_checker(ctx, 'vendor'):
        row = update(ctx, 'vendors', cur['id'], v)
        log_activity(ctx, 'vendor', row['id'], 'edited', f'Edited vendor {js_str(row["name"])}: {labels}')
        return {'pending': False, 'result': row, 'result_id': row['id']}
    return propose(
        ctx,
        {
            'entity': 'vendor',
            'entity_id': cur['id'],
            'action': 'update',
            'required_permission': 'procurement.manage',
            'base_version': cur['version'],
            'summary': f'Change {js_str(cur["name"])}: {labels}',
            'before': _vendor_show(cur, changed),
            'after': _vendor_show(v, changed),
            'payload': {'op': 'update', 'id': cur['id'], 'data': v},
        },
    )


def _vendor_status(action: str) -> Callable[[Req], Any]:
    def h(req: Req) -> Any:
        ctx = req.ctx
        require_action(ctx, 'procurement.manage')
        cur = find(ctx, 'vendors', req.params['id'], 'Vendor')
        reason = js_trim(js_str(coalesce(_g(req.body, 'reason'), '')))
        if action == 'block' and js_len(reason) < 5:
            err(422, 'REASON_REQUIRED', 'Say why the vendor is blocked (at least 5 characters).', 'reason')
        row = update(ctx, 'vendors', cur['id'], {'status': 'blocked' if action == 'block' else 'active'})
        name = js_str(row['name'])
        log_activity(ctx, 'vendor', row['id'], 'blocked' if action == 'block' else 'unblocked', f'Blocked {name} — {reason}' if action == 'block' else f'Unblocked {name}')
        return row

    return h


def _create_po(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    v = _validate_po(ctx, req.body)
    fy = _cur_fy(ctx)
    n = next_number(ctx, f'po-{js_str(fy)}')
    number = f'PO/{pad_start(js_str(fy % 100), 2, "0")}-{pad_start(js_str((fy + 1) % 100), 2, "0")}/{pad_start(js_str(n), 4, "0")}'
    po = insert(
        ctx,
        'purchase_orders',
        {
            'number': number,
            'vendor_id': v['vendor_id'],
            'project_id': v['project_id'],
            'budget_category': v['budget_category'],
            'lines': v['lines'],
            'amount_paise': v['amount_paise'],
            'gst_pct': v['gst_pct'],
            'status': 'draft',
            'requested_by': ctx.user_id,
            'approver_id': None,
            'decided_by': None,
            'decided_at': None,
            'decision_reason': None,
            'needed_by': v['needed_by'],
            'title': v['title'],
            'justification': v['justification'],
            'over_budget': False,
            'budget_remaining_paise': None,
        },
    )
    log_activity(ctx, 'purchase_order', po['id'], 'created', f'Created {number} · {fmt_money(po_total(po))}')
    return _submit_po(ctx, po) if js_truthy(_g(req.body, 'submit')) else po


def _patch_po(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    cur = find(ctx, 'purchase_orders', req.params['id'], 'Purchase order')
    if cur['status'] != 'draft' and cur['status'] != 'rejected':
        err(409, 'PO_LOCKED', f'{js_str(cur["number"])} is {js_str(cur["status"])} and can no longer be edited.')
    v = _validate_po(ctx, req.body, cur)
    po = update(ctx, 'purchase_orders', cur['id'], {**v, 'status': 'draft'}, req.if_match)
    log_activity(ctx, 'purchase_order', po['id'], 'edited', f'Edited {js_str(po["number"])}')
    return _submit_po(ctx, po) if js_truthy(_g(req.body, 'submit')) else po


def _submit_po_route(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    cur = find(ctx, 'purchase_orders', req.params['id'], 'Purchase order')
    if cur['status'] != 'draft' and cur['status'] != 'rejected':
        err(409, 'INVALID_STATE', f'{js_str(cur["number"])} is already {js_str(cur["status"])}.')
    bj = _g(req.body, 'justification')
    if bj is not UNDEF:
        cur = _patch_quiet('purchase_orders', cur, {'justification': js_or(js_trim(js_str(bj)), None)})
    return _submit_po(ctx, cur)


def _approve_po(req: Req) -> Any:
    ctx = req.ctx
    po = find(ctx, 'purchase_orders', req.params['id'], 'Purchase order')
    if po['status'] != 'pending':
        err(409, 'INVALID_STATE', f'{js_str(po["number"])} is {js_str(po["status"])}; only pending POs can be approved.')
    if not _can_decide_po(ctx, po):
        err(
            403,
            'FORBIDDEN',
            'You raised this purchase order, so someone else approves it.' if po['requested_by'] == ctx.user_id else f'This purchase order is waiting for {user_name(ctx, po.get("approver_id"))}.',
        )
    v = find(ctx, 'vendors', po['vendor_id'], 'Vendor')
    if v['status'] == 'blocked':
        err(409, 'VENDOR_BLOCKED', f'{js_str(v["name"])} is blocked. Unblock the vendor or reject the PO.')
    note = js_trim(js_str(coalesce(_g(req.body, 'note'), '')))
    me_name = js_str(ctx.user.get('name', UNDEF))
    st = ensure_wf(ctx, po, 'purchase_order', _po_input(po), admin_id(ctx))
    adv = advance_approval(ctx, st, ctx.user_id)
    if adv['next']:
        nx = adv['next']
        r3 = update(ctx, 'purchase_orders', po['id'], {'approver_id': nx['approver_id'], **wf_patch(adv['state'])}, req.if_match)
        step = f'Step {js_str(st["wf_step"] + 1)} of {len(st["wf_chain"])}'
        done_name = js_str(coalesce(_p(adv['done'], 'name'), 'approval'))
        log_activity(
            ctx,
            'purchase_order',
            po['id'],
            'approved_step',
            f'{step} ({done_name}) approved by {me_name}; sent to {user_name(ctx, nx["approver_id"])} for {js_str(nx.get("name", UNDEF))}{" — " + note if note else ""}',
        )
        notify(
            ctx,
            nx['approver_id'],
            'NTF-04',
            'Purchase order to approve',
            f'{js_str(po["number"])} · {fmt_money(po_total(po))} — approved by {me_name}, needs your approval ({js_str(nx.get("name", UNDEF))}).',
            '/purchases?tab=pos',
        )
        warn(ctx, 'NEXT_APPROVAL', f'{step} approved — sent to {user_name(ctx, nx["approver_id"])} for {js_lower(nx["name"])}.')
        return r3
    r2 = update(
        ctx,
        'purchase_orders',
        po['id'],
        {'status': 'approved', 'decided_by': ctx.user_id, 'decided_at': now_iso(), 'decision_reason': js_or(note, None), **wf_patch(adv['state'])},
        req.if_match,
    )
    over_txt = ' (over budget: ' + js_str(coalesce(po.get('justification'), '')) + ')' if js_truthy(po.get('over_budget')) else ''
    log_activity(ctx, 'purchase_order', po['id'], 'approved', f'Approved {js_str(po["number"])} · {fmt_money(po_total(po))}{over_txt}')
    notify(
        ctx,
        po['requested_by'],
        'NTF-05',
        'Purchase order approved',
        f'{js_str(po["number"])} · {js_str(v["name"])} · {fmt_money(po_total(po))} was approved. Send it to the vendor.',
        '/purchases?tab=pos',
    )
    return r2


def _reject_po(req: Req) -> Any:
    ctx = req.ctx
    po = find(ctx, 'purchase_orders', req.params['id'], 'Purchase order')
    if po['status'] != 'pending':
        err(409, 'INVALID_STATE', f'{js_str(po["number"])} is {js_str(po["status"])}; only pending POs can be rejected.')
    if not _can_decide_po(ctx, po):
        err(403, 'FORBIDDEN', f'This purchase order is waiting for {user_name(ctx, po.get("approver_id"))}.')
    reason = js_trim(js_str(coalesce(_g(req.body, 'reason'), '')))
    if js_len(reason) < 5:
        err(422, 'REASON_REQUIRED', 'Say why the PO is rejected (at least 5 characters).', 'reason')
    r2 = update(
        ctx, 'purchase_orders', po['id'], {'status': 'rejected', 'decided_by': ctx.user_id, 'decided_at': now_iso(), 'decision_reason': reason, **wf_clear()}, req.if_match
    )
    log_activity(ctx, 'purchase_order', po['id'], 'rejected', f'Rejected {js_str(po["number"])} — {reason}')
    notify(ctx, po['requested_by'], 'NTF-06', 'Purchase order rejected', f'{js_str(po["number"])}: {reason}', '/purchases?tab=pos')
    return r2


def _close_po(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    po = find(ctx, 'purchase_orders', req.params['id'], 'Purchase order')
    if po['status'] != 'approved':
        err(409, 'INVALID_STATE', 'Only approved POs can be closed.')
    r2 = update(ctx, 'purchase_orders', po['id'], {'status': 'closed'})
    log_activity(ctx, 'purchase_order', po['id'], 'closed', f'Closed {js_str(po["number"])}')
    return r2


def _delete_po(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    po = find(ctx, 'purchase_orders', req.params['id'], 'Purchase order')
    if po['status'] != 'draft':
        err(409, 'PO_LOCKED', 'Only draft POs can be deleted. Reject or close it instead.')
    remove(ctx, 'purchase_orders', po['id'], req.if_match)
    log_activity(ctx, 'purchase_order', po['id'], 'deleted', f'Deleted draft {js_str(po["number"])}')
    return {'ok': True}


def _create_bill(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    v = _validate_bill(ctx, req.body)
    file_id = _save_file(ctx, _g(req.body, 'file'), 'vendor_bill', 'file')
    b = insert(ctx, 'vendor_bills', {**v, 'status': 'received', 'paid_on': None, 'file_id': file_id, 'ledger_entry_id': None, 'approved_by': None, 'dispute_reason': None})
    _link_file(ctx, file_id, b['id'])
    if js_truthy(b.get('po_id')):
        po = next(x for x in rows(ctx, 'purchase_orders') if x['id'] == b['po_id'])
        if b['amount_paise'] > po['amount_paise']:
            pct = to_fixed(((b['amount_paise'] - po['amount_paise']) / po['amount_paise']) * 100, 1)
            warn(ctx, 'BILL_OVER_PO', f'This bill is {pct}% over {js_str(po["number"])} — within the 5% tolerance.')
    log_activity(ctx, 'vendor_bill', b['id'], 'created', f'Recorded bill {js_str(b["number"])} · {fmt_money(b["amount_paise"] + b["gst_paise"])}')
    wf = start_approval(ctx, 'vendor_bill', _bill_input(ctx, b), admin_id(ctx))
    admin = wf['approver_id']
    b = update(ctx, 'vendor_bills', b['id'], {'approver_id': admin, **wf_patch(wf)})
    if js_truthy(admin) and admin != ctx.user_id:
        notify(
            ctx,
            admin,
            'NTF-04',
            'Vendor bill to approve',
            f'Bill {js_str(b["number"])} · {fmt_money(b["amount_paise"] + b["gst_paise"])}, due {js_str(b["due_on"])}.',
            '/purchases?tab=bills',
        )
    return b


def _patch_bill(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    cur = find(ctx, 'vendor_bills', req.params['id'], 'Vendor bill')
    if cur['status'] in ('paid', 'approved'):
        err(409, 'BILL_LOCKED', f'This bill is {js_str(cur["status"])} and can no longer be edited.')
    v = _validate_bill(ctx, req.body, cur)
    b = update(ctx, 'vendor_bills', cur['id'], {**v, 'status': 'received', 'dispute_reason': None}, req.if_match)
    log_activity(ctx, 'vendor_bill', b['id'], 'edited', f'Edited bill {js_str(b["number"])}')
    return b


def _approve_bill(req: Req) -> Any:
    ctx = req.ctx
    b = find(ctx, 'vendor_bills', req.params['id'], 'Vendor bill')
    if b['status'] != 'received':
        err(409, 'INVALID_STATE', 'Resolve the dispute before approving this bill.' if b['status'] == 'disputed' else f'This bill is already {js_str(b["status"])}.')
    if not _can_decide_bill(ctx, b):
        err(403, 'FORBIDDEN', f'This bill is waiting for {user_name(ctx, coalesce(b.get("approver_id"), admin_id(ctx)))}.')
    me_name = js_str(ctx.user.get('name', UNDEF))
    st = ensure_wf(ctx, b, 'vendor_bill', _bill_input(ctx, b), admin_id(ctx), coalesce(b.get('approver_id'), admin_id(ctx)))
    adv = advance_approval(ctx, st, ctx.user_id)
    if adv['next']:
        nx = adv['next']
        r3 = update(ctx, 'vendor_bills', b['id'], {'approver_id': nx['approver_id'], **wf_patch(adv['state'])}, req.if_match)
        step = f'Step {js_str(st["wf_step"] + 1)} of {len(st["wf_chain"])}'
        done_name = js_str(coalesce(_p(adv['done'], 'name'), 'approval'))
        log_activity(
            ctx,
            'vendor_bill',
            b['id'],
            'approved_step',
            f'{step} ({done_name}) of bill {js_str(b["number"])} approved by {me_name}; sent to {user_name(ctx, nx["approver_id"])}',
        )
        notify(
            ctx,
            nx['approver_id'],
            'NTF-04',
            'Vendor bill to approve',
            f'Bill {js_str(b["number"])} · {fmt_money(b["amount_paise"] + b["gst_paise"])} — approved by {me_name}, needs your approval ({js_str(nx.get("name", UNDEF))}).',
            '/purchases?tab=bills',
        )
        warn(ctx, 'NEXT_APPROVAL', f'{step} approved — sent to {user_name(ctx, nx["approver_id"])}.')
        return r3
    r2 = update(ctx, 'vendor_bills', b['id'], {'status': 'approved', 'approved_by': ctx.user_id, **wf_patch(adv['state'])}, req.if_match)
    log_activity(ctx, 'vendor_bill', b['id'], 'approved', f'Approved bill {js_str(b["number"])} for payment')
    creator = b.get('created_by')
    if js_truthy(creator) and creator != ctx.user_id:
        notify(ctx, creator, 'NTF-05', 'Vendor bill approved', f'Bill {js_str(b["number"])} is approved for payment by {js_str(b["due_on"])}.', '/purchases?tab=bills')
    return r2


def _dispute_bill(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    b = find(ctx, 'vendor_bills', req.params['id'], 'Vendor bill')
    if b['status'] == 'paid':
        err(409, 'INVALID_STATE', 'A paid bill cannot be disputed. Ask the vendor for a credit note.')
    reason = js_trim(js_str(coalesce(_g(req.body, 'reason'), '')))
    if js_len(reason) < 10:
        err(422, 'REASON_REQUIRED', 'Describe the dispute (at least 10 characters) so the vendor can respond.', 'reason')
    r2 = update(ctx, 'vendor_bills', b['id'], {'status': 'disputed', 'dispute_reason': reason})
    log_activity(ctx, 'vendor_bill', b['id'], 'disputed', f'Disputed bill {js_str(b["number"])} — {reason}')
    return r2


def _resolve_bill(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage')
    b = find(ctx, 'vendor_bills', req.params['id'], 'Vendor bill')
    if b['status'] != 'disputed':
        err(409, 'INVALID_STATE', 'This bill is not disputed.')
    r2 = update(ctx, 'vendor_bills', b['id'], {'status': 'received'})
    log_activity(ctx, 'vendor_bill', b['id'], 'resolved', f'Dispute on bill {js_str(b["number"])} resolved')
    return r2


def _pay_bill(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'procurement.manage', 'Only admin and HR & operations pay vendor bills.')
    b = find(ctx, 'vendor_bills', req.params['id'], 'Vendor bill')
    if b['status'] != 'approved':
        err(409, 'INVALID_STATE', 'This bill is already paid.' if b['status'] == 'paid' else 'Only approved bills can be paid.')
    bp = _g(req.body, 'paid_on')
    paid_on = _need_date(bp, 'paid_on', 'Payment date') if js_truthy(bp) else ctx.today
    if paid_on > ctx.today:
        err(422, 'DATE_IN_FUTURE', 'The payment date is in the future.', 'paid_on')
    v = find(ctx, 'vendors', b['vendor_id'], 'Vendor')
    po = next((x for x in rows(ctx, 'purchase_orders') if x['id'] == b['po_id']), None) if js_truthy(b.get('po_id')) else None
    p = next((x for x in rows(ctx, 'projects') if x['id'] == po['project_id']), None) if po and js_truthy(po.get('project_id')) else None
    tds_txt = f' (TDS {fmt_money(b["tds_paise"])} deducted)' if js_truthy(b['tds_paise']) else ''
    le = insert(
        ctx,
        'ledger_entries',
        {
            'kind': 'expense',
            'category': _bill_category(ctx, b),
            'description': f'Vendor bill {js_str(b["number"])} · {js_str(v["name"])}{" · " + js_str(po["number"]) if po else ""}{tds_txt}',
            'amount_paise': b['amount_paise'] + b['gst_paise'],
            'date': paid_on,
            'recurring': False,
            'until_on': None,
            'status': 'paid',
            'project_id': coalesce(po.get('project_id', UNDEF) if po else UNDEF, None),
            'milestone_id': None,
            'invoice_id': None,
            'stream': coalesce(p.get('stream', UNDEF) if p else UNDEF, None),
            'locked': True,
        },
    )
    r2 = update(ctx, 'vendor_bills', b['id'], {'status': 'paid', 'paid_on': paid_on, 'ledger_entry_id': le['id']}, req.if_match)
    log_activity(ctx, 'vendor_bill', b['id'], 'paid', f'Paid bill {js_str(b["number"])} · {fmt_money(b["amount_paise"] + b["gst_paise"] - b["tds_paise"])} to {js_str(v["name"])}')
    if po:
        billed = _sum(x['amount_paise'] for x in rows(ctx, 'vendor_bills') if x.get('po_id') == po['id'] and x['status'] == 'paid')
        if po['status'] == 'approved' and billed >= po['amount_paise']:
            update(ctx, 'purchase_orders', po['id'], {'status': 'closed'})
            log_activity(ctx, 'purchase_order', po['id'], 'closed', f'{js_str(po["number"])} closed — fully billed and paid')
    return r2


def _get_budgets(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'budgets.manage', 'Only the admin manages budgets.')
    fy = js_number(coalesce(req.q('fy'), _cur_fy(ctx)))
    return budget_statement(ctx, fy)


def _budget_show(ctx: Ctx, b: dict) -> dict[str, Any]:
    months = _fy_months(ctx, b['fy_start_year'])
    mp = b['monthly_paise']
    out: dict[str, Any] = {'Category': b['category'], 'Annual total': fmt_money(_sum(mp)), 'Owner': user_name(ctx, b.get('owner_id'))}
    for i, m in enumerate(months):
        out[fmt_date_en_in(m + '-01', month='short', year='2-digit')] = fmt_money(coalesce(_at(mp, i), 0))
    if js_truthy(b.get('notes')):
        out['Notes'] = b['notes']
    return out


def _put_budget(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'budgets.manage', 'Only the admin manages budgets.')
    fy = js_number(_g(body, 'fy_start_year'))
    if not is_integer(fy) or fy < 2000 or fy > 2100:
        err(422, 'VAL_REQUIRED', 'Choose the financial year.', 'fy_start_year')
    kind = 'revenue' if _g(body, 'kind') == 'revenue' else 'expense'
    category = _need_text(_g(body, 'category'), 'category', 'Category', 2, 60)
    if kind == 'revenue' and category not in STREAM:
        err(422, 'VAL_CATEGORY', 'Revenue budgets are set per revenue stream.', 'category')
    monthly = coalesce(_g(body, 'monthly_paise'), [])
    if not isinstance(monthly, list) or len(monthly) != 12:
        err(422, 'VAL_PHASING', 'Phase the budget across all 12 months.', 'monthly_paise')
    for i, m in enumerate(monthly):
        _money(m, 'monthly_paise', f'Month {i + 1}', True)
    existing = next((b for b in _budgets_for(ctx, fy) if b['kind'] == kind and _lc(b['category']) == _lc(category)), None)
    if req.if_match is not UNDEF and existing and req.if_match != existing['version']:
        err(409, 'STALE', 'Someone else changed this budget. Reload to see the latest version.')
    data = {
        'fy_start_year': fy,
        'kind': kind,
        'category': existing['category'] if existing else category,
        'owner_id': coalesce(_g(body, 'owner_id'), existing.get('owner_id', UNDEF) if existing else UNDEF, None),
        'monthly_paise': monthly,
        'notes': js_str(coalesce(_g(body, 'notes'), existing.get('notes', UNDEF) if existing else UNDEF, '')),
    }
    total = _sum(monthly)
    prev = f'{fmt_money(_sum(existing["monthly_paise"]))} → ' if existing else ''
    return propose(
        ctx,
        {
            'entity': 'budget',
            'entity_id': existing['id'] if existing else _budget_key(fy, kind, category),
            'action': 'update' if existing else 'create',
            'required_permission': 'budgets.manage',
            'base_version': existing['version'] if existing else None,
            'summary': f'{_fy_label(fy)} {kind} budget · {js_str(data["category"])}: {prev}{fmt_money(total)}',
            'before': _budget_show(ctx, existing) if existing else None,
            'after': _budget_show(ctx, data),
            'payload': {'op': 'save', 'id': existing['id'] if existing else None, 'data': data},
        },
    )


def _delete_budget(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'budgets.manage', 'Only the admin manages budgets.')
    b = find(ctx, 'budgets', req.params['id'], 'Budget')
    if req.if_match is not UNDEF and req.if_match != b['version']:
        err(409, 'STALE', 'Someone else changed this budget. Reload to see the latest version.')
    return propose(
        ctx,
        {
            'entity': 'budget',
            'entity_id': b['id'],
            'action': 'delete',
            'required_permission': 'budgets.manage',
            'base_version': b['version'],
            'summary': f'Remove {_fy_label(b["fy_start_year"])} {js_str(b["kind"])} budget · {js_str(b["category"])} ({fmt_money(_sum(b["monthly_paise"]))})',
            'before': _budget_show(ctx, b),
            'after': None,
            'payload': {'op': 'delete', 'id': b['id']},
        },
    )


def _copy_budgets_route(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'budgets.manage', 'Only the admin manages budgets.')
    to = js_number(_g(body, 'to_fy'))
    frm = js_number(coalesce(_g(body, 'from_fy'), to - 1))
    growth = js_number(coalesce(_g(body, 'growth_pct'), 0))
    if not is_finite(growth) or growth < -50 or growth > 200:
        err(422, 'VAL_RANGE', 'Growth must be between −50% and 200%.', 'growth_pct')
    src = _budgets_for(ctx, frm)
    if not src:
        err(409, 'NO_SOURCE_BUDGET', f'There is no {_fy_label(frm)} budget to copy.')
    if not is_integer(to) or to < 2000 or to > 2100:
        err(422, 'VAL_REQUIRED', 'Choose the financial year to copy into.', 'to_fy')
    overwrite = js_truthy(_g(body, 'overwrite'))
    g = js_str(growth)
    return propose(
        ctx,
        {
            'entity': 'budget',
            'entity_id': f'budget_copy_{js_str(to)}',
            'action': 'execute',
            'required_permission': 'budgets.manage',
            'summary': f'Copy {_fy_label(frm)} budgets to {_fy_label(to)} at {"+" if growth >= 0 else ""}{g}%{", replacing existing lines" if overwrite else ""}',
            'before': {'Target year': _fy_label(to), 'Lines already there': len(_budgets_for(ctx, to))},
            'after': {'Copied from': _fy_label(frm), 'Lines': len(src), 'Growth': f'{g}%', 'Replace existing': 'Yes' if overwrite else 'No'},
            'payload': {'op': 'copy', 'from': frm, 'to': to, 'growth': growth, 'overwrite': overwrite},
        },
    )


def _copy_budgets(ctx: Ctx, frm: Any, to: Any, growth: Any, overwrite: bool) -> dict[str, int]:
    src = _budgets_for(ctx, frm)
    created = replaced = skipped = 0
    for s in src:
        monthly = [js_round((m * (100 + growth)) / 100 / 100) * 100 for m in s['monthly_paise']]
        ex = next((b for b in _budgets_for(ctx, to) if b['kind'] == s['kind'] and _lc(b['category']) == _lc(s['category'])), None)
        if ex and not overwrite:
            skipped += 1
            continue
        if ex:
            update(ctx, 'budgets', ex['id'], {'monthly_paise': monthly, 'owner_id': s.get('owner_id', UNDEF), 'notes': s.get('notes', UNDEF)})
            replaced += 1
        else:
            insert(
                ctx,
                'budgets',
                {'fy_start_year': to, 'kind': s['kind'], 'category': s['category'], 'owner_id': s.get('owner_id', UNDEF), 'monthly_paise': monthly, 'notes': s.get('notes', UNDEF)},
            )
            created += 1
    log_activity(
        ctx,
        'budget',
        f'fy_{js_str(to)}',
        'copied',
        f'Copied {_fy_label(frm)} budgets to {_fy_label(to)} at {"+" if growth >= 0 else ""}{js_str(growth)}% ({created} new, {replaced} replaced, {skipped} kept)',
    )
    return {'created': created, 'replaced': replaced, 'skipped': skipped}


# ---------- maker-checker appliers (run directly when maker-checker is off for the entity) ----------
def _by(ctx: Ctx, cs: dict) -> str:
    return f' (proposed by {user_name(ctx, cs["maker_id"])})' if cs['id'] != 'cs_direct' else ''


def _apply_vendor(ctx: Ctx, cs: dict) -> dict[str, Any]:
    p = cs['payload']
    if p['op'] == 'create':
        v = _validate_vendor(ctx, p['data'])
        row = insert(ctx, 'vendors', {**v, 'status': 'active'})
        log_activity(ctx, 'vendor', row['id'], 'created', f'Added vendor {js_str(row["name"])}{_by(ctx, cs)}')
        return {'result': row, 'result_id': row['id']}
    cur = find(ctx, 'vendors', p.get('id', UNDEF), 'Vendor')
    assert_base_version(cs, cur['version'])
    v = _validate_vendor(ctx, p['data'], cur)
    row = update(ctx, 'vendors', cur['id'], v)
    log_activity(ctx, 'vendor', row['id'], 'edited', f'{js_str(cs["summary"])}{_by(ctx, cs)}')
    return {'result': row, 'result_id': row['id']}


def _apply_budget(ctx: Ctx, cs: dict) -> dict[str, Any]:
    p = cs['payload']
    if p['op'] == 'copy':
        r = _copy_budgets(ctx, p['from'], p['to'], p['growth'], js_truthy(p.get('overwrite')))
        return {'result': r, 'result_id': None}
    if p['op'] == 'delete':
        b = find(ctx, 'budgets', p['id'], 'Budget')
        assert_base_version(cs, b['version'])
        remove(ctx, 'budgets', b['id'])
        log_activity(ctx, 'budget', b['id'], 'deleted', f'Removed {_fy_label(b["fy_start_year"])} budget · {js_str(b["category"])}{_by(ctx, cs)}')
        return {'result': {'ok': True}, 'result_id': b['id']}
    d = thaw(p['data'])
    pid = p.get('id')
    if js_truthy(pid):
        existing = find(ctx, 'budgets', pid, 'Budget')
    else:
        existing = next((b for b in _budgets_for(ctx, d['fy_start_year']) if b['kind'] == d['kind'] and _lc(b['category']) == _lc(d['category'])), None)
    if existing:
        assert_base_version(cs, existing['version'])
    row = update(ctx, 'budgets', existing['id'], d) if existing else insert(ctx, 'budgets', d)
    log_activity(
        ctx,
        'budget',
        row['id'],
        'edited' if existing else 'created',
        f'{_fy_label(d["fy_start_year"])} {js_str(d["kind"])} budget · {js_str(row["category"])} · {fmt_money(_sum(d["monthly_paise"]))}{_by(ctx, cs)}',
    )
    return {'result': row, 'result_id': row['id']}


register_applier('vendor', _apply_vendor)
register_applier('budget', _apply_budget)


# ---------- revenue leakage ----------
def _get_leakage(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'leakage.view', 'Only the admin can see lost revenue.')
    return _leakage_result(ctx, js_number(coalesce(req.q('fy'), _cur_fy(ctx))))


def _validate_leak(ctx: Ctx, body: Any, existing: dict | None = None) -> dict[str, Any]:
    def ex(k: str) -> Any:
        return existing.get(k, UNDEF) if existing else UNDEF

    def bg(k: str) -> Any:
        return _g(body, k)

    kind = js_str(coalesce(bg('kind'), ex('kind'), ''))
    if kind not in MANUAL_LEAKAGE_KINDS:
        err(
            422,
            'VAL_KIND',
            'Choose discount, write-off, credit note, penalty, scope creep or churn. Lost deals, unbilled hours and bad debts are recorded automatically.',
            'kind',
        )
    amount = _money(coalesce(bg('amount_paise'), ex('amount_paise')), 'amount_paise', 'Amount')
    date = _need_date(coalesce(bg('date'), ex('date')), 'date', 'Date')
    if date > ctx.today:
        err(422, 'DATE_IN_FUTURE', 'The date is in the future.', 'date')
    reason = _need_text(coalesce(bg('reason'), ex('reason')), 'reason', 'Reason', 10, 400)
    bp = bg('project_id')
    project_id = coalesce(ex('project_id'), None) if bp is UNDEF else js_or(bp, None)
    p = find(ctx, 'projects', project_id, 'Project') if js_truthy(project_id) else None
    client = js_trim(js_str(coalesce(bg('client'), ex('client'), p['client'] if p else UNDEF, '')))
    if not client:
        err(422, 'VAL_REQUIRED', 'Choose the client.', 'client')
    status = js_str(coalesce(bg('status'), ex('status'), 'open'))
    if status not in ('open', 'recovered', 'accepted'):
        err(422, 'VAL_STATUS', 'Unknown status.', 'status')
    recovered = _money(coalesce(bg('recovered_paise'), ex('recovered_paise'), 0), 'recovered_paise', 'Recovered amount', True)
    if recovered > amount:
        err(422, 'VAL_RANGE', 'Recovered cannot be more than the amount lost.', 'recovered_paise')
    if status == 'recovered' and recovered == 0:
        recovered = amount
    br = bg('recoverable')
    recoverable = coalesce(ex('recoverable'), kind in ('penalty', 'credit_note')) if br is UNDEF else js_truthy(br)
    return {
        'kind': kind,
        'amount_paise': amount,
        'date': date,
        'reason': reason,
        'project_id': project_id,
        'client': client,
        'status': status,
        'recovered_paise': recovered,
        'recoverable': recoverable,
        'deal_id': coalesce(bg('deal_id'), ex('deal_id'), None),
    }


def _create_leak(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'leakage.view', 'Only the admin records lost revenue.')
    v = _validate_leak(ctx, req.body)
    row = insert(ctx, 'revenue_leakage', {**v, 'recorded_by': ctx.user_id, 'auto': False})
    log_activity(ctx, 'leakage', row['id'], 'created', f'Recorded {js_lower(LEAKAGE_KIND[row["kind"]]["label"])} {fmt_money(row["amount_paise"])} · {js_str(row["client"])}')
    return row


def _patch_leak(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'leakage.view')
    if req.params['id'].startswith('auto_'):
        err(409, 'AUTO_ROW', 'This row is calculated from deals, timesheets or invoices and cannot be edited. Fix the source record instead.')
    cur = find(ctx, 'revenue_leakage', req.params['id'], 'Leakage entry')
    v = _validate_leak(ctx, req.body, cur)
    row = update(ctx, 'revenue_leakage', cur['id'], v, req.if_match)
    rec = f' ({fmt_money(row["recovered_paise"])} recovered)' if js_truthy(row['recovered_paise']) else ''
    log_activity(ctx, 'leakage', row['id'], 'edited', f'Updated {js_lower(LEAKAGE_KIND[row["kind"]]["label"])} · {js_str(row["client"])} · {js_str(row["status"])}{rec}')
    return row


def _delete_leak(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'leakage.view')
    if req.params['id'].startswith('auto_'):
        err(409, 'AUTO_ROW', 'Calculated rows cannot be deleted.')
    cur = find(ctx, 'revenue_leakage', req.params['id'], 'Leakage entry')
    remove(ctx, 'revenue_leakage', cur['id'], req.if_match)
    log_activity(ctx, 'leakage', cur['id'], 'deleted', f'Removed {js_lower(LEAKAGE_KIND[cur["kind"]]["label"])} · {js_str(cur["client"])}')
    return {'ok': True}


# ---------- CEO dashboard summary ----------
def _spend_summary(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'budgets.manage', 'Only the admin sees the spend summary.')
    fy = _cur_fy(ctx)
    st = budget_statement(ctx, fy)
    i = js_max(0, js_min(11, st['current_month_index']))
    lk = _leakage_result(ctx, fy)
    exp = rows(ctx, 'expense_claims')
    bills = [b for b in _purchases(ctx)['bills'] if b['status'] in ('approved', 'received')]
    contract_renewals = _helpers.get('contract_renewals')
    approved = [e for e in exp if e['status'] == 'approved']
    submitted = [e for e in exp if e['status'] == 'submitted']
    return {
        'month': st['months'][i],
        'month_spend': _sum(l['monthly_actual'][i] for l in st['expense']),  # noqa: E741
        'month_committed': _sum(l['monthly_committed'][i] for l in st['expense']),  # noqa: E741
        'month_budget': _sum(coalesce(_at(l['monthly_budget'], i), NAN) for l in st['expense']),  # noqa: E741
        'unbudgeted_fy': st['totals']['unbudgeted'],
        'pending_reimbursement': {'amount': _sum(e['amount_paise'] for e in approved), 'count': len(approved)},
        'awaiting_approval': {'amount': _sum(e['amount_paise'] for e in submitted), 'count': len(submitted)},
        'bills_payable': {
            'amount': _sum(b['net_payable_paise'] for b in bills),
            'count': len(bills),
            'overdue': _sum(b['net_payable_paise'] for b in bills if b['days_overdue'] > 0),
        },
        'leakage_fy': lk['summary']['net'],
        'leakage_top': [{'kind': k['kind'], 'label': k['label'], 'amount': k['amount']} for k in lk['summary']['by_kind'][:3]],
        'renewals': contract_renewals(ctx) if contract_renewals else [],
    }


# Late-bound link to the clients module (avoids a circular import at load time).
_helpers: dict[str, Any] = {}


def provide_clients_helpers(h: dict[str, Any]) -> None:
    _helpers.update(h)


def register(r: Any) -> None:
    r.get('/spend/meta', _meta)
    r.get('/expenses', _list_expenses)
    r.get('/expenses/:id', _get_expense)
    r.post('/expenses', _create_expense)
    r.patch('/expenses/:id', _patch_expense)
    r.post('/expenses/:id/submit', _submit_expense_route)
    r.delete('/expenses/:id', _delete_expense)
    r.post('/expenses/:id/approve', _approve_expense)
    r.post('/expenses/:id/reject', _reject_expense)
    r.post('/expenses/:id/pay', _pay_expense)
    r.get('/purchases', _get_purchases)
    r.post('/vendors', _create_vendor)
    r.patch('/vendors/:id', _patch_vendor)
    for action in ('block', 'unblock'):
        r.post(f'/vendors/:id/{action}', _vendor_status(action))
    r.post('/purchase-orders', _create_po)
    r.patch('/purchase-orders/:id', _patch_po)
    r.post('/purchase-orders/:id/submit', _submit_po_route)
    r.post('/purchase-orders/:id/approve', _approve_po)
    r.post('/purchase-orders/:id/reject', _reject_po)
    r.post('/purchase-orders/:id/close', _close_po)
    r.delete('/purchase-orders/:id', _delete_po)
    r.post('/vendor-bills', _create_bill)
    r.patch('/vendor-bills/:id', _patch_bill)
    r.post('/vendor-bills/:id/approve', _approve_bill)
    r.post('/vendor-bills/:id/dispute', _dispute_bill)
    r.post('/vendor-bills/:id/resolve', _resolve_bill)
    r.post('/vendor-bills/:id/pay', _pay_bill)
    r.get('/budgets', _get_budgets)
    r.put('/budgets', _put_budget)
    r.delete('/budgets/:id', _delete_budget)
    r.post('/budgets/copy', _copy_budgets_route)
    # registerRest
    r.get('/leakage', _get_leakage)
    r.post('/leakage', _create_leak)
    r.patch('/leakage/:id', _patch_leak)
    r.delete('/leakage/:id', _delete_leak)
    r.get('/spend/summary', _spend_summary)

