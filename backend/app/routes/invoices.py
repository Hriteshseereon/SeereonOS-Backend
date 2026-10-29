"""Port of src/mock/routes/invoices.ts — v7 GST tax invoices.

One invoice table serves the milestone billing and the full Indian GST tax invoice: seller / bill-to / ship-to parties,
place of supply, HSN/SAC lines, CGST+SGST (intra-state) or IGST (inter-state) or zero-rated export under LUT, round-off,
amount in words, bank details. Money rules: see the TS header (revenue = taxable value; GST never reaches revenue;
payments settle the GST-inclusive total; cancelling books a credit note).

Exports used by other modules (names are the contract): EXPORT_CODE, GST_STATES, state_code_of, state_name,
next_invoice_number, raise_milestone_invoice, record_revenue_payment, compute_totals, amount_in_words, indian_words, …
"""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    edit,
    err,
    find,
    find_by,
    has,
    has_any,
    insert,
    log_activity,
    now_iso,
    persist,
    remove,
    require_action,
    rows,
    rows_where_ro,
    update,
    user_name,
    warn,
)
from app.domain.calendar import add_days, diff_days, fy_label, fy_range, fy_start_year, is_real_iso_date
from app.domain.gst import GST_STATES, gstin_problem
from app.domain.money import fmt_money
from app.engine.analytics import costed_entries, current_rate_ph, milestone_state
from app.frozen import thaw
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_floor,
    js_len,
    js_lower,
    js_max,
    js_min,
    js_number,
    js_or,
    js_round,
    js_split,
    js_str,
    js_trim,
    js_truthy,
    js_upper,
    locale_key,
    nullish,
    pad_start,
    prop,
)
from app.modules.spend.labels import PAN_RE

__all__ = [
    'GST_STATES',
    'EXPORT_CODE',
    'GST_RATES',
    'UNITS',
    'state_code_of',
    'state_name',
    'default_invoice_settings',
    'invoice_settings',
    'settings_for',
    'seller_party',
    'client_for_project',
    'client_party',
    'tax_mode_of',
    'compute_totals',
    'indian_words',
    'amount_in_words',
    'next_invoice_number',
    'is_live',
    'settled_of',
    'balance_of',
    'display_status',
    'ensure_gst',
    'normalise',
    'validate_input',
    'issue_invoice',
    'apply_payment',
    'raise_milestone_invoice',
    'record_revenue_payment',
    'invoice_list',
    'invoice_detail',
    'milestone_line',
    'prefill',
    'billable_hours',
    'register',
]

_C = cat('mock/routes/invoices')

# ---------------------------------------------------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------------------------------------------------
STATE_BY_CODE: dict[str, str] = {s['code']: s['name'] for s in GST_STATES}
EXPORT_CODE: str = _C['EXPORT_CODE']
GST_RATES = _C['GST_RATES']
UNITS = _C['UNITS']
IFSC_RE = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}\Z')
SAC_RE = re.compile(r'^[0-9]{4,8}\Z')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+\Z')
_INDIA_RE = re.compile(r'^(india|in|bharat)\Z', re.I)


def _test(rx: re.Pattern[str], s: Any) -> bool:
    """rx.test(s) (String(s) like JS)"""
    return rx.search(s if isinstance(s, str) else js_str(s)) is not None


def _date_ok(s: Any) -> bool:
    """DATE_RE.test(s) = isRealISODate(s)"""
    return is_real_iso_date(s)


def _is_india(c: Any) -> bool:
    return not js_truthy(c) or _test(_INDIA_RE, js_trim(c))


def _g(o: Any, k: str) -> Any:
    """`o.k` on any JS value: a dict's key (UNDEF when absent); null/undefined throw (TypeError -> 500)"""
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    if o is None or o is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(o)} (reading '{k}')")
    return UNDEF


def _settings_fm(ctx: Ctx) -> Any:
    """ctx.tenant.settings['company.fy_start_month'] ?? 4"""
    return coalesce(prop(ctx.tenant.get('settings'), 'company.fy_start_month'), 4)


def state_code_of(name: Any) -> str:
    n = js_lower(js_trim(coalesce(name, '')))
    if not n:
        return ''
    hit = next((s for s in GST_STATES if js_lower(s['name']) == n or (n == 'orissa' and s['code'] == '21')), None)
    return hit['code'] if hit is not None else ''


def state_name(code: Any) -> str:
    if not js_truthy(code):
        return ''
    return STATE_BY_CODE.get(code, '') if isinstance(code, str) else ''


# ---------------------------------------------------------------------------------------------------------------------
# Settings, seller and client parties
# ---------------------------------------------------------------------------------------------------------------------
def _initials(name: str) -> str:
    w = [x for x in re.split(r'\s+', re.sub(r'[^A-Za-z ]', ' ', name)) if x]
    a = w[0][0] if len(w) > 0 else 'I'
    b = w[1][0] if len(w) > 1 else ''
    c = w[2][0] if len(w) > 2 else ''
    return js_upper(a + b + c)


def default_invoice_settings(t: dict) -> dict[str, Any]:
    o = t.get('org')
    legal = prop(o, 'legal_name')
    return {
        'prefix': js_or(prop(o, 'employee_code_prefix'), _initials(js_or(legal, t['name']))),
        'credit_note_prefix': 'CN',
        'payment_terms_days': 30,
        'default_sac': '998314',
        'default_gst_rate': 18,
        'default_tds_pct': 10,
        'default_unit': 'Nos',
        'bank': {
            'bank_name': coalesce(prop(o, 'bank_name'), ''),
            'account_name': js_or(legal, t['name']),
            'account_number': '',
            'ifsc': coalesce(prop(o, 'ifsc'), ''),
            'branch': '',
            'upi': '',
        },
        'signatory_name': '',
        'signatory_title': 'Authorised signatory',
        'lut_number': '',
        'default_notes': 'Thank you for your business.',
        'default_terms': '\n'.join(
            [
                'Payment is due within the credit period shown above, by bank transfer to the account below.',
                'Please quote the invoice number with your payment and email the TDS certificate (Form 16A) every quarter.',
                'Interest at 18% a year may be charged on payments received after the due date.',
                # the company's own city (Organisation profile); no jurisdiction line until it is set
                *([f'Subject to {city} jurisdiction.'] if (city := js_trim(js_str(coalesce(prop(o, 'city'), '')))) else []),
            ]
        ),
        'updated_at': None,
        'updated_by': None,
    }


def _tenant_row(ctx: Ctx) -> dict:
    return find_by('tenants', 'id', ctx.tenant_id) or ctx.tenant


def invoice_settings(ctx: Ctx) -> dict[str, Any]:
    t = _tenant_row(ctx)
    d = default_invoice_settings(t)
    s = t.get('invoicing')
    if js_truthy(s):
        from app.js import js_spread

        return {**d, **js_spread(s), 'bank': {**d['bank'], **js_spread(prop(s, 'bank'))}}
    return d


def _mask(acct: str) -> str:
    return re.sub(r'[0-9](?=[0-9]{4})', 'X', acct) if acct else ''


def settings_for(ctx: Ctx) -> dict[str, Any]:
    """Settings as the viewer may see them: the full account number only with finance.edit."""
    s = invoice_settings(ctx)
    full = has(ctx, 'finance.edit')
    org = _tenant_row(ctx).get('org')
    bank = s['bank']
    acct = prop(bank, 'account_number')
    masked = _mask(acct) if js_truthy(acct) else coalesce(prop(org, 'bank_account_masked'), '')
    return {**s, 'bank': {**bank, 'account_number': acct if full else masked}, 'account_masked': masked, 'can_edit': full}


def seller_party(ctx: Ctx) -> dict[str, Any]:
    t = _tenant_row(ctx)
    o = t.get('org')
    gstin = prop(o, 'gstin')
    code = js_or(gstin[:2] if js_truthy(gstin) and re.match(r'[0-9]{2}', gstin) else '', state_code_of(prop(o, 'state')))
    return {
        'name': js_or(prop(o, 'legal_name'), t['name']),
        'address': js_or(prop(o, 'registered_address'), prop(o, 'corporate_address'), ''),
        'city': coalesce(prop(o, 'city'), ''),
        'state': js_or(prop(o, 'state'), state_name(code)),
        'state_code': code,
        'pincode': coalesce(prop(o, 'pincode'), ''),
        'country': js_or(prop(o, 'country'), 'India'),
        'gstin': coalesce(gstin, ''),
        'pan': coalesce(prop(o, 'pan'), ''),
        'email': coalesce(prop(o, 'email'), ''),
        'phone': coalesce(prop(o, 'phone'), ''),
        'contact': '',
        'cin': coalesce(prop(o, 'cin'), ''),
        'website': coalesce(prop(o, 'website'), ''),
    }


def _lc(s: str) -> str:
    return js_lower(js_trim(s))


def client_for_project(ctx: Ctx, p: dict) -> dict | None:
    key = _lc(p['client'])
    return next((c for c in rows(ctx, 'clients') if _lc(c['name']) == key), None)


def client_party(c: dict | None, fallback_name: str = '') -> dict[str, Any]:
    if not c:
        return {
            'name': fallback_name,
            'address': '',
            'city': '',
            'state': '',
            'state_code': '',
            'pincode': '',
            'country': 'India',
            'gstin': '',
            'pan': '',
            'email': '',
            'phone': '',
            'contact': '',
        }
    country = prop(c, 'country')
    india = _is_india(country)
    gstin = prop(c, 'gstin')
    if india:
        code = js_or(
            prop(c, 'state_code'), gstin[:2] if js_truthy(gstin) and re.match(r'[0-9]{2}', gstin) else '', state_code_of(prop(c, 'state'))
        )
    else:
        code = EXPORT_CODE
    return {
        'name': c['name'],
        'address': coalesce(prop(c, 'billing_address'), ''),
        'city': coalesce(prop(c, 'city'), ''),
        'state': js_or(prop(c, 'state'), state_name(code)) if india else js_or(prop(c, 'state'), country),
        'state_code': code,
        'pincode': coalesce(prop(c, 'pincode'), ''),
        'country': js_or(country, 'India'),
        'gstin': coalesce(gstin, ''),
        'pan': js_or(prop(c, 'pan'), gstin[2:12] if js_truthy(gstin) and len(gstin) == 15 else ''),
        'email': coalesce(prop(c, 'email'), ''),
        'phone': coalesce(prop(c, 'phone'), ''),
        'contact': coalesce(prop(c, 'primary_contact'), ''),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------------------------------------------------
def tax_mode_of(seller_code: Any, place_of_supply: Any, buyer_country: Any = UNDEF) -> str:
    if place_of_supply == EXPORT_CODE or (js_truthy(buyer_country) and not _is_india(buyer_country)):
        return 'export'
    return 'intra' if js_truthy(seller_code) and js_truthy(place_of_supply) and seller_code == place_of_supply else 'inter'


def _num_or0(v: Any) -> Any:
    """Number(v) || 0"""
    return js_or(js_number(v), 0)


def compute_totals(lines: list, mode: str, reverse_charge: bool = False) -> dict[str, Any]:
    calc = []
    for l in lines:  # noqa: E741
        gross = js_round(_num_or0(_g(l, 'qty')) * _num_or0(_g(l, 'rate_paise')))
        discount = js_round((gross * js_min(100, js_max(0, _num_or0(_g(l, 'discount_pct'))))) / 100)
        taxable = gross - discount
        rate = 0 if mode == 'export' else _num_or0(_g(l, 'gst_rate'))
        cgst = js_round((taxable * rate) / 200) if mode == 'intra' else 0
        sgst = cgst
        igst = js_round((taxable * rate) / 100) if mode == 'inter' else 0
        calc.append(
            {
                'id': _g(l, 'id'),
                'gross': gross,
                'discount': discount,
                'taxable': taxable,
                'rate': rate,
                'cgst': cgst,
                'sgst': sgst,
                'igst': igst,
                'total': taxable + cgst + sgst + igst,
            }
        )

    def sum_(k: str) -> Any:
        s: Any = 0
        for c in calc:
            s = s + c[k]
        return s

    taxable = sum_('taxable')
    cgst, sgst, igst = sum_('cgst'), sum_('sgst'), sum_('igst')
    tax = cgst + sgst + igst
    before = taxable + (0 if reverse_charge else tax)
    total = js_round(before / 100) * 100
    rates: dict[Any, dict] = {}
    for c in calc:
        r = rates.get(c['rate'])
        if r is None:
            r = {'rate': c['rate'], 'taxable': 0, 'cgst': 0, 'sgst': 0, 'igst': 0}
            rates[c['rate']] = r
        r['taxable'] += c['taxable']
        r['cgst'] += c['cgst']
        r['sgst'] += c['sgst']
        r['igst'] += c['igst']
    return {
        'tax_mode': mode,
        'lines': calc,
        'subtotal': sum_('gross'),
        'discount': sum_('discount'),
        'taxable': taxable,
        'cgst': cgst,
        'sgst': sgst,
        'igst': igst,
        'tax': tax,
        'before_round': before,
        'round_off': total - before,
        'total': total,
        'reverse_charge': reverse_charge,
        'by_rate': sorted(rates.values(), key=lambda x: x['rate']),
        'words': amount_in_words(total),
    }


ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']  # fmt: skip
TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']


def _two(n: int) -> str:
    return ONES[n] if n < 20 else TENS[n // 10] + (' ' + ONES[n % 10] if n % 10 else '')


def _three(n: int) -> str:
    return ' '.join(x for x in [f'{ONES[n // 100]} Hundred' if n >= 100 else '', _two(n % 100) if n % 100 else ''] if x)


def indian_words(n: Any) -> str:
    """Indian numbering: 1,23,45,678 -> One Crore Twenty Three Lakh Forty Five Thousand Six Hundred Seventy Eight."""
    n = js_floor(abs(n))
    if n != n:  # NaN: every part is NaN (falsy) -> ''
        return ''
    if n == 0:
        return 'Zero'
    n = int(n)  # OverflowError for Infinity (the TS recurses until the stack overflows: 500 either way)
    crore = n // 10**7
    lakh = (n // 10**5) % 100
    thousand = (n // 10**3) % 100
    rest = n % 1000
    parts = [f'{indian_words(crore)} Crore' if crore else '', f'{_two(lakh)} Lakh' if lakh else '', f'{_two(thousand)} Thousand' if thousand else '', _three(rest) if rest else '']  # fmt: skip
    return ' '.join(x for x in parts if x)


def amount_in_words(paise: Any) -> str:
    """"Indian Rupees One Lakh Eighteen Thousand and Fifty Paise Only" """
    a = abs(js_round(paise))
    if a != a:
        return f'{"Minus " if paise < 0 else ""}Indian Rupees {indian_words(a)} Only'
    r = js_floor(a / 100)
    p = int(a % 100)
    return f'{"Minus " if paise < 0 else ""}Indian Rupees {indian_words(r)}{f" and {_two(p)} Paise" if p else ""} Only'


# ---------------------------------------------------------------------------------------------------------------------
# Numbering: <prefix>/<yy-yy>/<0001> per financial year
# ---------------------------------------------------------------------------------------------------------------------
def _fy_code(ctx: Ctx, date: str) -> str:
    y = fy_start_year(date, _settings_fm(ctx))
    if y != y:
        return 'NaN-NaN'
    return f'{pad_start(js_str(y % 100), 2, "0")}-{pad_start(js_str((y + 1) % 100), 2, "0")}'


def _next_in_series(taken: list, prefix: str, fy: str) -> str:
    rx = re.compile('^' + re.escape(prefix) + '/' + re.escape(fy) + r'/([0-9]+)\Z', re.I)
    mx: Any = 0
    for n in taken:
        x = rx.search(n if isinstance(n, str) else js_str(n))
        if x:
            mx = js_max(mx, js_number(x.group(1)))
    return f'{prefix}/{fy}/{pad_start(js_str(mx + 1), 4, "0")}'


def next_invoice_number(ctx: Ctx, date: str) -> str:
    return _next_in_series([i.get('number') for i in rows(ctx, 'invoices')], invoice_settings(ctx)['prefix'], _fy_code(ctx, date))


def _next_credit_note_number(ctx: Ctx, date: str) -> str:
    taken = [x for x in (coalesce(prop(i.get('credit_note'), 'number'), '') for i in rows(ctx, 'invoices')) if js_truthy(x)]
    return _next_in_series(taken, js_or(invoice_settings(ctx)['credit_note_prefix'], 'CN'), _fy_code(ctx, date))


def _next_draft_no(ctx: Ctx) -> Any:
    m: Any = 0
    for i in rows(ctx, 'invoices'):
        m = js_max(m, coalesce(i.get('draft_no'), 0))
    return m + 1


# ---------------------------------------------------------------------------------------------------------------------
# Status and legacy upgrade
# ---------------------------------------------------------------------------------------------------------------------
def _raw_status(i: dict) -> str:
    s = i.get('status')
    if not nullish(s):
        return s
    if i['paid_amount_paise'] >= i['amount_paise'] and i['amount_paise'] > 0:
        return 'paid'
    return 'partially_paid' if i['paid_amount_paise'] > 0 else 'issued'


def is_live(i: dict) -> bool:
    return i.get('status', UNDEF) != 'draft' and i.get('status', UNDEF) != 'cancelled'


def settled_of(i: dict) -> Any:
    return coalesce(i.get('received_paise'), 0) + coalesce(i.get('tds_received_paise'), 0)


def balance_of(i: dict) -> Any:
    return js_max(0, coalesce(i.get('total_paise'), i['amount_paise']) - settled_of(i)) if is_live(i) else 0


def display_status(ctx: Ctx, i: dict) -> str:
    s = _raw_status(i)
    if s in ('draft', 'cancelled', 'paid'):
        return s
    if balance_of(i) > 0 and i['due_on'] < ctx.today:
        return 'overdue'
    return s


def _legacy_line(ctx: Ctx, m: dict | None, p: dict | None, amount: Any, s: dict, rate: Any) -> dict[str, Any]:
    return {
        'id': 'l1',
        'item': f'M{js_str(m["no"])} {js_str(m["name"])}' if m else p['name'] if p else 'Professional services',
        'description': f'{js_str(p["code"])} · {js_str(p["name"])}' if p else '',
        'hsn_sac': s['default_sac'],
        'qty': 1,
        'unit': 'Lot',
        'rate_paise': amount,
        'discount_pct': 0,
        'gst_rate': rate,
        'source': {'kind': 'milestone', 'milestone_id': m['id']} if m else {'kind': 'manual'},
    }


def ensure_gst(ctx: Ctx, inv: dict) -> dict:
    """Fill GST detail on an invoice that predates v7 (or was raised through the legacy milestone endpoint). Returns
    the (possibly new) row — use it."""
    lines0 = inv.get('lines')
    if (
        isinstance(lines0, list)
        and len(lines0) > 0
        and inv.get('total_paise', UNDEF) is not UNDEF
        and js_truthy(inv.get('seller'))
        and js_truthy(inv.get('bill_to'))
    ):
        return inv
    s = invoice_settings(ctx)
    p = next((x for x in rows(ctx, 'projects') if x['id'] == inv.get('project_id')), None)
    mid0 = inv.get('milestone_id')
    m = next((x for x in rows(ctx, 'milestones') if x['id'] == mid0), None) if js_truthy(mid0) else None
    seller = coalesce(inv.get('seller'), UNDEF)
    if nullish(seller):
        seller = seller_party(ctx)
    client = client_for_project(ctx, p) if p else None
    bill = inv.get('bill_to')
    if nullish(bill):
        bill = client_party(client, coalesce(p.get('client') if p else UNDEF, ''))
    pos = inv.get('place_of_supply')
    if nullish(pos):
        pos = js_or(bill['state_code'], seller['state_code'])
    mode = tax_mode_of(seller['state_code'], pos, prop(bill, 'country'))
    lines = lines0 if (isinstance(lines0, list) and len(lines0)) else [_legacy_line(ctx, m, p, inv['amount_paise'], s, 0 if mode == 'export' else s['default_gst_rate'])]
    t = compute_totals(lines, mode, js_truthy(inv.get('reverse_charge')))
    # legacy payments carried only the ex-GST share: gross them up so received + TDS settles the GST total
    pays = []
    for pay in list(rows(ctx, 'payments')):
        if pay.get('invoice_id') != inv['id']:
            continue
        if pay.get('gross_paise', UNDEF) is UNDEF:
            gross = js_round((pay['amount_paise'] * t['total']) / t['taxable']) if js_truthy(t['taxable']) else pay['amount_paise']
            pay = edit('payments', pay)
            pay.update({'gross_paise': gross, 'tds_paise': 0})
        pays.append(pay)
    received: Any = 0
    for x in pays:
        received += coalesce(x.get('gross_paise'), 0)
    tds: Any = 0
    for x in pays:
        tds += coalesce(x.get('tds_paise'), 0)
    if inv['paid_amount_paise'] >= inv['amount_paise'] and inv['amount_paise'] > 0 and received + tds != t['total']:
        received = t['total'] - tds  # rounding: fully paid stays fully paid
    mids = inv.get('milestone_ids')
    changes = {
        'status': coalesce(inv.get('status'), _raw_status(inv)),
        'client_id': coalesce(inv.get('client_id'), client['id'] if client else UNDEF, None),
        'milestone_ids': mids if not nullish(mids) else ([mid0] if js_truthy(mid0) else []),
        'seller': seller,
        'bill_to': bill,
        'ship_to': coalesce(inv.get('ship_to'), None),
        'place_of_supply': pos,
        'tax_mode': mode,
        'reverse_charge': js_truthy(inv.get('reverse_charge')),
        'lines': lines,
        'bank': inv['bank'] if not nullish(inv.get('bank')) else {**s['bank']},
        'subtotal_paise': t['subtotal'],
        'discount_paise': t['discount'],
        'taxable_paise': t['taxable'],
        'cgst_paise': t['cgst'],
        'sgst_paise': t['sgst'],
        'igst_paise': t['igst'],
        'round_off_paise': t['round_off'],
        'total_paise': t['total'],
        'received_paise': received,
        'tds_received_paise': tds,
        'tds_pct': coalesce(inv.get('tds_pct'), 0 if mode == 'export' else s['default_tds_pct']),
        'notes': coalesce(inv.get('notes'), s['default_notes']),
        'terms': coalesce(inv.get('terms'), s['default_terms']),
        'po_reference': coalesce(inv.get('po_reference'), None),
        'signatory': coalesce(inv.get('signatory'), {'name': s['signatory_name'], 'title': s['signatory_title']}),
        'issued_on': coalesce(inv.get('issued_on'), inv.get('invoiced_on', UNDEF)),
        'emails': coalesce(inv.get('emails'), []),
    }
    inv = edit('invoices', inv)
    inv.update(thaw(changes))
    persist()
    return inv


# ---------------------------------------------------------------------------------------------------------------------
# Draft body: normalise + validate
# ---------------------------------------------------------------------------------------------------------------------
def _str(v: Any) -> str:
    """typeof v === 'string' ? v.trim() : v == null ? '' : String(v).trim()"""
    if isinstance(v, str):
        return js_trim(v)
    if nullish(v):
        return ''
    return js_trim(js_str(v))


def _party(v: Any) -> dict[str, Any]:
    o = coalesce(v, {})

    def g(k: str) -> Any:
        return _g(o, k)

    p: dict[str, Any] = {
        'name': _str(g('name')),
        'address': _str(g('address')),
        'city': _str(g('city')),
        'state': _str(g('state')),
        'state_code': _str(g('state_code')),
        'pincode': _str(g('pincode')),
        'country': js_or(_str(g('country')), 'India'),
        'gstin': js_upper(_str(g('gstin'))),
        'pan': js_upper(_str(g('pan'))),
        'email': _str(g('email')),
        'phone': _str(g('phone')),
        'contact': _str(g('contact')),
    }
    if g('cin') is not UNDEF:
        p['cin'] = js_upper(_str(g('cin')))
    if g('website') is not UNDEF:
        p['website'] = _str(g('website'))
    if not p['state'] and p['state_code']:
        p['state'] = state_name(p['state_code'])
    if not _is_india(p['country']):
        p['state_code'] = EXPORT_CODE
    return p


def normalise(body: Any) -> dict[str, Any]:
    def bg(k: str) -> Any:
        return _g(body, k)

    b = coalesce(bg('bank'), {})
    sig = coalesce(bg('signatory'), {})
    lines_in = bg('lines') if isinstance(bg('lines'), list) else []
    mids_in = bg('milestone_ids')
    mids: list = []
    if isinstance(mids_in, list):
        for x in mids_in:
            sx = _str(x)
            if sx and sx not in mids:
                mids.append(sx)
    lines = []
    for i, l in enumerate(lines_in):  # noqa: E741
        def lg(k: str, _l: Any = l) -> Any:
            return _g(_l, k)

        src = lg('source')
        lines.append(
            {
                'id': js_or(_str(lg('id')), f'l{i + 1}'),
                'item': _str(lg('item')),
                'description': _str(lg('description')),
                'hsn_sac': _str(lg('hsn_sac')),
                'qty': js_number(lg('qty')),
                'unit': js_or(_str(lg('unit')), 'Nos'),
                'rate_paise': js_round(js_number(lg('rate_paise'))),
                'discount_pct': js_or(js_number(coalesce(lg('discount_pct'), 0)), 0),
                'gst_rate': js_number(lg('gst_rate')),
                'source': thaw(src) if not nullish(src) else {'kind': 'manual'},
            }
        )
    terms = bg('terms')
    number = _str(bg('number'))
    return {
        'project_id': _str(bg('project_id')),
        'milestone_ids': mids,
        'client_id': js_or(_str(bg('client_id')), None),
        'seller': _party(bg('seller')),
        'bill_to': _party(bg('bill_to')),
        'ship_to': _party(bg('ship_to')) if js_truthy(bg('ship_to')) else None,
        'invoiced_on': _str(bg('invoiced_on')),
        'due_on': _str(bg('due_on')),
        'place_of_supply': _str(bg('place_of_supply')),
        'po_reference': _str(bg('po_reference')),
        'po_date': js_or(_str(bg('po_date')), None),
        'reverse_charge': js_truthy(bg('reverse_charge')),
        'lines': lines,
        'tds_pct': js_or(js_number(coalesce(bg('tds_pct'), 0)), 0),
        'bank': {
            'bank_name': _str(_g(b, 'bank_name')),
            'account_name': _str(_g(b, 'account_name')),
            'account_number': re.sub(r'\s+', '', _str(_g(b, 'account_number'))),
            'ifsc': js_upper(_str(_g(b, 'ifsc'))),
            'branch': _str(_g(b, 'branch')),
            'upi': _str(_g(b, 'upi')),
        },
        'notes': _str(bg('notes')),
        'terms': js_trim(terms) if isinstance(terms, str) else '',
        'signatory': {'name': _str(_g(sig, 'name')), 'title': _str(_g(sig, 'title'))},
        'number': number if number else UNDEF,
    }


def _check_party(out: list, key: str, p: dict, label: str, require_gstin: bool, complete: bool) -> None:
    """v8: `complete` (issuing) requires every field; a DRAFT may be incomplete — only what is filled in must be valid"""
    if complete and not p['name']:
        out.append({'field': f'{key}.name', 'message': f'Enter the {label} name.'})
    if complete and not p['address']:
        out.append({'field': f'{key}.address', 'message': f'Enter the {label} address.'})
    india = _is_india(p['country'])
    if india and (complete or js_truthy(p['state_code'])) and p['state_code'] not in STATE_BY_CODE:
        out.append({'field': f'{key}.state_code', 'message': f'Choose the {label} state.'})
    if india and p['pincode'] and not re.search(r'^[0-9]{6}\Z', p['pincode']):
        out.append({'field': f'{key}.pincode', 'message': 'PIN code has 6 digits.'})
    if p['gstin']:
        g = p['gstin']
        gp = gstin_problem(g)  # V9-FIX (item 3): say what is actually wrong
        if gp:
            out.append({'field': f'{key}.gstin', 'message': gp})
        elif india and p['state_code'] and g[:2] != p['state_code']:
            out.append(
                {
                    'field': f'{key}.gstin',
                    'message': f'GSTIN starts with {g[:2]} ({state_name(g[:2]) or "unknown state"}) but the state is {state_name(p["state_code"])} ({p["state_code"]}).',
                }
            )
        elif p['pan'] and g[2:12] != p['pan']:
            out.append({'field': f'{key}.pan', 'message': f'PAN {p["pan"]} does not match the PAN inside the GSTIN ({g[2:12]}).'})
        if not india:
            out.append({'field': f'{key}.gstin', 'message': 'Overseas parties have no GSTIN. Clear it or set the country to India.'})
    elif require_gstin:
        out.append({'field': f'{key}.gstin', 'message': f'Enter the {label} GSTIN — a tax invoice needs it.'})
    if p['pan'] and not _test(PAN_RE, p['pan']):
        out.append({'field': f'{key}.pan', 'message': 'PAN is 10 characters, e.g. AAKCS4821M.'})
    if p['email'] and not _test(EMAIL_RE, p['email']):
        out.append({'field': f'{key}.email', 'message': 'Enter a valid email address.'})


def _ms_ids_of(i: dict) -> list:
    """i.milestone_ids ?? (i.milestone_id ? [i.milestone_id] : [])"""
    ids = i.get('milestone_ids')
    if not nullish(ids):
        return ids
    return [i['milestone_id']] if js_truthy(i.get('milestone_id')) else []


def _ms_ids_len(i: dict) -> list:
    """i.milestone_ids?.length ? i.milestone_ids : i.milestone_id ? [i.milestone_id] : []"""
    ids = i.get('milestone_ids')
    if js_truthy(ids) and len(ids):
        return ids
    return [i['milestone_id']] if js_truthy(i.get('milestone_id')) else []


def validate_input(ctx: Ctx, v: dict, opts: dict) -> list[dict]:
    """Validate a draft. `issuing` adds the checks a final tax invoice needs. Returns issues (never throws)."""
    issuing = opts['issuing']
    self_id = opts.get('selfId', UNDEF)
    out: list[dict] = []
    p = next((x for x in rows(ctx, 'projects') if x['id'] == v['project_id']), None) if v['project_id'] else None
    if not p:
        out.append({'field': 'project_id', 'message': 'Choose the project this invoice is for.'})
    invs = rows(ctx, 'invoices')
    for mid in v['milestone_ids']:
        m = next((x for x in rows(ctx, 'milestones') if x['id'] == mid), None)
        if not m or js_truthy(m.get('deleted_at')) or (p and m['project_id'] != p['id']):
            out.append({'field': 'milestone_ids', 'message': 'A selected milestone is not part of this project.'})
            continue
        other = next((i for i in invs if i['id'] != self_id and is_live(i) and m['id'] in _ms_ids_of(i)), None)
        if other:
            out.append({'field': 'milestone_ids', 'message': f'M{js_str(m["no"])} {js_str(m["name"])} is already invoiced on {js_str(other["number"])}.'})
        elif m['billing_status'] != 'none':
            out.append(
                {'field': 'milestone_ids', 'message': f'M{js_str(m["no"])} {js_str(m["name"])} is already {"paid" if m["billing_status"] == "paid" else "invoiced"}.'}
            )
    _check_party(out, 'seller', v['seller'], 'seller', issuing, issuing)
    _check_party(out, 'bill_to', v['bill_to'], 'client', False, issuing)
    if v['ship_to']:
        _check_party(out, 'ship_to', v['ship_to'], 'ship-to', False, issuing)
    if not _date_ok(v['invoiced_on']):
        out.append({'field': 'invoiced_on', 'message': 'Pick the invoice date.'})
    elif issuing and v['invoiced_on'] > ctx.today:
        out.append({'field': 'invoiced_on', 'message': 'An invoice can’t be dated in the future. Use today or an earlier date.'})
    if not _date_ok(v['due_on']):
        out.append({'field': 'due_on', 'message': 'Pick the due date.'})
    elif _date_ok(v['invoiced_on']) and v['due_on'] < v['invoiced_on']:
        out.append({'field': 'due_on', 'message': 'The due date is before the invoice date.'})
    if (issuing or js_truthy(v['place_of_supply'])) and v['place_of_supply'] not in STATE_BY_CODE:
        out.append({'field': 'place_of_supply', 'message': 'Choose the place of supply (the client’s state).'})
    elif v['place_of_supply'] == EXPORT_CODE and _is_india(v['bill_to']['country']):
        out.append({'field': 'place_of_supply', 'message': 'Export applies only to clients outside India. Pick the client’s state.'})
    if v['po_date'] and not _date_ok(v['po_date']):
        out.append({'field': 'po_date', 'message': 'Pick a valid PO date.'})
    if not (v['tds_pct'] >= 0 and v['tds_pct'] <= 20):
        out.append({'field': 'tds_pct', 'message': 'TDS is between 0% and 20%.'})
    if issuing and not len(v['lines']):
        out.append({'field': 'lines', 'message': 'Add at least one line item.'})
    for i, l in enumerate(v['lines']):  # noqa: E741
        k = f'lines.{i}'
        n = i + 1
        if issuing and not l['item']:
            out.append({'field': f'{k}.item', 'message': f'Line {n}: enter the item name.'})
        if (issuing or js_truthy(l['hsn_sac'])) and not _test(SAC_RE, l['hsn_sac']):
            out.append({'field': f'{k}.hsn_sac', 'message': f'Line {n}: HSN/SAC is 4–8 digits (IT services: 998314).'})
        if not (l['qty'] > 0):
            out.append({'field': f'{k}.qty', 'message': f'Line {n}: quantity must be above zero.'})
        if not (is_finite(l['rate_paise']) and l['rate_paise'] >= 0):
            out.append({'field': f'{k}.rate_paise', 'message': f'Line {n}: enter the rate.'})
        if not (l['discount_pct'] >= 0 and l['discount_pct'] <= 100):
            out.append({'field': f'{k}.discount_pct', 'message': f'Line {n}: discount is 0–100%.'})
        if l['gst_rate'] not in GST_RATES:
            out.append({'field': f'{k}.gst_rate', 'message': f'Line {n}: GST rate is 0, 5, 12, 18 or 28%.'})
    mode = tax_mode_of(v['seller']['state_code'], v['place_of_supply'], v['bill_to']['country'])
    t = compute_totals(v['lines'], mode, v['reverse_charge'])
    if issuing and len(v['lines']) and t['total'] <= 0:
        out.append({'field': 'lines', 'message': 'The invoice total must be above zero.'})
    bank = v['bank']
    if issuing:
        if not bank['bank_name']:
            out.append({'field': 'bank.bank_name', 'message': 'Enter the bank name.'})
        if not re.search(r'^[0-9]{9,18}\Z', bank['account_number']):
            out.append({'field': 'bank.account_number', 'message': 'Enter the full account number (9–18 digits).'})
        if not bank['account_name']:
            out.append({'field': 'bank.account_name', 'message': 'Enter the account name.'})
        if not v['signatory']['name']:
            out.append({'field': 'signatory.name', 'message': 'Enter the authorised signatory.'})
    if bank['ifsc'] and not _test(IFSC_RE, bank['ifsc']):
        out.append({'field': 'bank.ifsc', 'message': 'IFSC is 11 characters, e.g. HDFC0001827.'})
    elif issuing and not bank['ifsc']:
        out.append({'field': 'bank.ifsc', 'message': 'Enter the IFSC.'})
    num = v.get('number', UNDEF)
    if issuing and num is not UNDEF:
        if not re.search(r'^[A-Za-z0-9/\-]{1,16}\Z', num):
            out.append({'field': 'number', 'message': 'Invoice number: up to 16 letters, digits, “/” or “-” (GST rule).'})
        elif any(i['id'] != self_id and _lc(i['number']) == _lc(num) for i in invs):
            out.append({'field': 'number', 'message': f'Invoice {num} already exists. Use {next_invoice_number(ctx, js_or(v["invoiced_on"], ctx.today))}.'})
    return out


def _fail(issues: list[dict]) -> None:
    f = issues[0]
    err(422, 'VALIDATION', f'{f["message"]} ({len(issues) - 1} more to fix)' if len(issues) > 1 else f['message'], f['field'])


# ---------------------------------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------------------------------
def _totals_fields(v: dict) -> dict[str, Any]:
    mode = tax_mode_of(v['seller']['state_code'], v['place_of_supply'], v['bill_to']['country'])
    t = compute_totals(v['lines'], mode, v['reverse_charge'])
    return {
        'tax_mode': mode,
        'subtotal_paise': t['subtotal'],
        'discount_paise': t['discount'],
        'taxable_paise': t['taxable'],
        'cgst_paise': t['cgst'],
        'sgst_paise': t['sgst'],
        'igst_paise': t['igst'],
        'round_off_paise': t['round_off'],
        'total_paise': t['total'],
    }


def _draft_fields(v: dict) -> dict[str, Any]:
    return {
        'project_id': v['project_id'],
        'milestone_ids': v['milestone_ids'],
        'client_id': v['client_id'],
        'seller': v['seller'],
        'bill_to': v['bill_to'],
        'ship_to': v['ship_to'],
        'invoiced_on': v['invoiced_on'],
        'due_on': v['due_on'],
        'place_of_supply': v['place_of_supply'],
        'po_reference': js_or(v['po_reference'], None),
        'po_date': v['po_date'],
        'reverse_charge': v['reverse_charge'],
        'lines': [{**l, 'id': js_or(l['id'], f'l{i + 1}')} for i, l in enumerate(v['lines'])],
        'tds_pct': v['tds_pct'],
        'bank': v['bank'],
        'notes': v['notes'],
        'terms': v['terms'],
        'signatory': v['signatory'],
        **_totals_fields(v),
    }


def _save_client_back(ctx: Ctx, v: dict) -> None:
    if not v['client_id']:
        err(422, 'VALIDATION', 'Pick the client in the bill-to block to save these details to the client master.', 'client_id')
    if not has_any(ctx, 'clients.manage', 'finance.edit'):
        err(403, 'FORBIDDEN', 'You can’t edit the client master.')
    c = find(ctx, 'clients', v['client_id'])
    b = v['bill_to']
    update(
        ctx,
        'clients',
        c['id'],
        {
            'billing_address': b['address'],
            'gstin': b['gstin'],
            'email': js_or(b['email'], prop(c, 'email')),
            'phone': js_or(b['phone'], prop(c, 'phone')),
            'primary_contact': js_or(b['contact'], prop(c, 'primary_contact')),
            'country': js_or(b['country'], prop(c, 'country')),
            'city': b['city'],
            'state': b['state'],
            'state_code': b['state_code'],
            'pincode': b['pincode'],
            'pan': b['pan'],
        },
    )
    log_activity(ctx, 'client', c['id'], 'edited', f'Billing details updated from an invoice (GSTIN {js_or(b["gstin"], "—")}, {js_or(b["state"], b["country"])})')


STREAM_CATEGORY = {'sap_consulting': 'SAP consulting', 'products': 'Product licence', 'services': 'Services', 'saas': 'Subscription / AMC', 'ams': 'AMS'}


def _ms_label(m: dict) -> str:
    return f'M{js_str(m["no"])} {js_str(m["name"])}'


def issue_invoice(ctx: Ctx, inv: dict, number_override: Any = UNDEF) -> dict:
    """Final step shared by the editor, the legacy milestone endpoint and the seed: number, revenue, milestones, ledger."""
    p = find(ctx, 'projects', inv['project_id'], 'Project')
    date = inv['invoiced_on']
    number = js_trim(number_override) if js_truthy(number_override) and js_trim(number_override) else next_invoice_number(ctx, date)
    if any(i['id'] != inv['id'] and _lc(i['number']) == _lc(number) for i in rows(ctx, 'invoices')):
        number = next_invoice_number(ctx, date)
    ms = [find(ctx, 'milestones', mid, 'Milestone') for mid in coalesce(inv.get('milestone_ids'), [])]
    taxable = coalesce(inv.get('taxable_paise'), 0)
    out = update(
        ctx,
        'invoices',
        inv['id'],
        {
            'number': number,
            'status': 'issued',
            'amount_paise': taxable,
            'paid_amount_paise': 0,
            'paid_on': None,
            'milestone_id': ms[0]['id'] if ms else '',
            'issued_on': ctx.today,
            'issued_by': ctx.user_id,
            'received_paise': 0,
            'tds_received_paise': 0,
        },
    )
    for m in ms:
        update(ctx, 'milestones', m['id'], {'billing_status': 'invoiced'})
    lines = inv.get('lines')
    first_item = prop(lines[0], 'item') if js_truthy(lines) and len(lines) else UNDEF
    what = ', '.join(_ms_label(m) for m in ms) if ms else coalesce(first_item, 'Services')
    insert(
        ctx,
        'ledger_entries',
        {
            'kind': 'income',
            'category': 'Milestone invoice' if ms else coalesce(STREAM_CATEGORY.get(p['stream']) if isinstance(p.get('stream'), str) else None, 'Services'),
            'description': f'{number} · {js_str(p["name"])} · {js_str(what)}',
            'amount_paise': taxable,
            'date': date,
            'recurring': False,
            'until_on': None,
            'status': 'expected',
            'project_id': p['id'],
            'milestone_id': ms[0]['id'] if ms else None,
            'invoice_id': inv['id'],
            'stream': p['stream'],
            'locked': True,
        },
    )
    for m in ms:
        bap = m.get('billing_amount_paise')
        if js_truthy(bap) and bap != _sum_milestone_lines(inv, m['id']):
            warn(
                ctx,
                'INV_AMOUNT_DIFFERS',
                f'M{js_str(m["no"])} is billed at {fmt_money(_sum_milestone_lines(inv, m["id"]))} against a milestone billing of {fmt_money(bap)}.',
            )
    log_activity(
        ctx,
        'invoice',
        inv['id'],
        'issued',
        f'Issued {number} for {fmt_money(coalesce(inv.get("total_paise"), taxable), True)} (taxable {fmt_money(taxable, True)})',
    )
    tail = f' for {", ".join("M" + js_str(m["no"]) for m in ms)}' if ms else ''
    log_activity(ctx, 'project', p['id'], 'invoice.raised', f'Invoice {number} raised{tail}: {fmt_money(taxable)} + GST')
    return out


def _sum_milestone_lines(inv: dict, mid: str) -> Any:
    mode = coalesce(inv.get('tax_mode'), 'intra')
    lines = [l for l in coalesce(inv.get('lines'), []) if prop(prop(l, 'source'), 'kind') == 'milestone' and l['source'].get('milestone_id', UNDEF) == mid]  # noqa: E741
    return compute_totals(lines, mode)['taxable']


def apply_payment(ctx: Ctx, inv0: dict, b: dict, if_match: Any = UNDEF) -> dict[str, Any]:
    """Record a settlement: cash received + TDS deducted, against the GST-inclusive total."""
    inv = ensure_gst(ctx, inv0)
    if not is_live(inv):
        err(409, 'INV_NOT_ISSUED', 'Issue the invoice before recording a payment.' if inv.get('status') == 'draft' else 'This invoice is cancelled.')
    if not is_integer(b['received']) or b['received'] < 0:
        err(422, 'MONEY_INVALID', 'Enter the amount received.', 'received_paise')
    if not is_integer(b['tds']) or b['tds'] < 0:
        err(422, 'MONEY_INVALID', 'TDS can’t be negative.', 'tds_paise')
    settle = b['received'] + b['tds']
    if settle <= 0:
        err(422, 'MONEY_INVALID', 'Enter the amount received (and any TDS deducted).', 'received_paise')
    if not _date_ok(b['paid_on']):
        err(422, 'VALIDATION', 'Pick the date the money arrived.', 'paid_on')
    if b['paid_on'] > ctx.today:
        err(422, 'VALIDATION', 'A payment date can’t be in the future.', 'paid_on')
    if b['paid_on'] < inv['invoiced_on']:
        err(422, 'DATE_RANGE', 'The payment date can’t be before the invoice date.', 'paid_on')
    total = coalesce(inv.get('total_paise'), inv['amount_paise'])
    balance = balance_of(inv)
    number = js_str(inv.get('number', UNDEF))
    if balance <= 0:
        err(409, 'INV_OVERPAY', f'Invoice {number} is already fully paid.')
    if settle > balance:
        err(422, 'INV_OVERPAY', f'Only {fmt_money(balance)} is due on {number} (incl. GST). Received + TDS must not exceed it.', 'received_paise')
    tx = coalesce(inv.get('taxable_paise'), inv['amount_paise'])
    taxable_tds = js_round((coalesce(inv.get('tds_pct'), 0) / 100) * tx)
    if b['tds'] > js_max(taxable_tds, js_round(tx * 0.2)) and b['tds'] > 0:
        err(422, 'VALIDATION', f'TDS of {fmt_money(b["tds"])} is more than 20% of the taxable value. Check the figure.', 'tds_paise')
    full = settle >= balance
    # revenue share of this settlement; the last settlement takes the remainder so paid = amount exactly
    left = inv['amount_paise'] - inv['paid_amount_paise']
    share = left if full else js_min(left, js_round((settle * inv['amount_paise']) / js_max(1, total)))
    pay = insert(
        ctx,
        'payments',
        {'invoice_id': inv['id'], 'amount_paise': share, 'paid_on': b['paid_on'], 'reference': b['reference'], 'gross_paise': b['received'], 'tds_paise': b['tds'], 'mode': b['mode']},
    )
    u = update(
        ctx,
        'invoices',
        inv['id'],
        {
            'paid_amount_paise': inv['paid_amount_paise'] + share,
            'paid_on': b['paid_on'] if full else inv.get('paid_on', UNDEF),
            'received_paise': coalesce(inv.get('received_paise'), 0) + b['received'],
            'tds_received_paise': coalesce(inv.get('tds_received_paise'), 0) + b['tds'],
            'status': 'paid' if full else 'partially_paid',
        },
        if_match,
    )
    if full:
        for mid in _ms_ids_of(inv):
            m = next((x for x in rows(ctx, 'milestones') if x['id'] == mid), None)
            if m and m['billing_status'] != 'paid':
                update(ctx, 'milestones', m['id'], {'billing_status': 'paid'})
        for le in [x for x in rows(ctx, 'ledger_entries') if x.get('invoice_id') == inv['id'] and x['amount_paise'] > 0]:
            update(ctx, 'ledger_entries', le['id'], {'status': 'received'})
    tds_txt = f' + {fmt_money(b["tds"])} TDS' if js_truthy(b['tds']) else ''
    what = f'{fmt_money(b["received"])} received{tds_txt} on {number}{" — paid in full" if full else ""}'
    log_activity(ctx, 'invoice', inv['id'], 'payment', what)
    log_activity(ctx, 'project', inv['project_id'], 'payment.recorded', what)
    return {'invoice': u, 'payment': pay, 'fully_paid': full}


def raise_milestone_invoice(ctx: Ctx, m: dict, b: dict) -> dict:
    """Legacy POST /milestones/:id/invoice (Costing tab dialog): a complete GST invoice, issued at once.
    b = {number, invoiced_on, due_on, amount_paise, file_id}"""
    p = find(ctx, 'projects', m['project_id'], 'Project')
    s = invoice_settings(ctx)
    seller = seller_party(ctx)
    client = client_for_project(ctx, p)
    bill = client_party(client, p['client'])
    pos = js_or(bill['state_code'], seller['state_code'])
    mode = tax_mode_of(seller['state_code'], pos, bill['country'])
    lines = [_legacy_line(ctx, m, p, b['amount_paise'], s, 0 if mode == 'export' else s['default_gst_rate'])]
    v = {
        'project_id': p['id'],
        'milestone_ids': [m['id']],
        'client_id': client['id'] if client else None,
        'seller': seller,
        'bill_to': bill,
        'ship_to': None,
        'invoiced_on': b['invoiced_on'],
        'due_on': b['due_on'],
        'place_of_supply': pos,
        'po_reference': '',
        'po_date': None,
        'reverse_charge': False,
        'lines': lines,
        'tds_pct': 0 if mode == 'export' else s['default_tds_pct'],
        'bank': {**s['bank']},
        'notes': s['default_notes'],
        'terms': s['default_terms'],
        'signatory': {'name': s['signatory_name'], 'title': s['signatory_title']},
    }
    inv = insert(
        ctx,
        'invoices',
        thaw(
            {
                'number': f'DRAFT-{js_str(_next_draft_no(ctx))}',
                'draft_no': _next_draft_no(ctx),
                'status': 'draft',
                'amount_paise': 0,
                'paid_amount_paise': 0,
                'paid_on': None,
                'milestone_id': '',
                'file_id': b.get('file_id', UNDEF),
                'emails': [],
                **_draft_fields(v),
            }
        ),
    )
    return issue_invoice(ctx, inv, b.get('number', UNDEF))


def record_revenue_payment(ctx: Ctx, inv0: dict, amount: Any, paid_on: str, reference: str) -> dict:
    """Legacy POST /invoices/:id/payments: the amount is on the ex-GST (revenue) basis — gross it up to cash."""
    inv = ensure_gst(ctx, inv0)
    outstanding = inv['amount_paise'] - inv['paid_amount_paise']
    number = js_str(inv.get('number', UNDEF))
    if outstanding <= 0:
        err(409, 'INV_OVERPAY', f'Invoice {number} is already fully paid.', 'amount_paise')
    if amount > outstanding:
        err(409, 'INV_OVERPAY', f'Only {fmt_money(outstanding)} (ex-GST) is outstanding on {number}. Record that amount or less.', 'amount_paise')
    gross = balance_of(inv) if amount >= outstanding else js_round((amount * coalesce(inv.get('total_paise'), amount)) / js_max(1, inv['amount_paise']))
    return apply_payment(ctx, inv, {'received': js_min(gross, balance_of(inv)), 'tds': 0, 'paid_on': paid_on, 'reference': reference, 'mode': 'Bank transfer'})['invoice']


# ---------------------------------------------------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------------------------------------------------
def _bucket_of(age: Any) -> str:
    return '0_30' if age <= 30 else '31_60' if age <= 60 else '61_90' if age <= 90 else '90_plus'


def _milestone_label(ctx: Ctx, i: dict) -> str:
    ids = _ms_ids_len(i)
    by_id = {}
    for m in rows(ctx, 'milestones'):
        by_id.setdefault(m['id'], m)
    ms = [by_id[x] for x in ids if x in by_id]
    if ms:
        return ', '.join(_ms_label(m) for m in ms)
    lines = i.get('lines')
    first = prop(lines[0], 'item') if js_truthy(lines) and len(lines) else UNDEF
    return coalesce(first, '—')


def _draft_label(i: dict) -> str:
    return js_trim(f'Draft {js_str(coalesce(i.get("draft_no"), ""))}')


def _list_row(ctx: Ctx, i: dict, projects: dict) -> dict[str, Any]:
    p = projects.get(i['project_id'])
    status = display_status(ctx, i)
    balance = balance_of(i)
    age = js_max(0, diff_days(ctx.today, i['invoiced_on']))
    return {
        'id': i['id'],
        'version': i['version'],
        'number': _draft_label(i) if i.get('status') == 'draft' else i['number'],
        'status': status,
        'project_id': i['project_id'],
        'project_code': p['code'] if p else '',
        'project_name': p['name'] if p else '—',
        'client': js_or(prop(i.get('bill_to'), 'name'), p['client'] if p else UNDEF, ''),
        'client_id': coalesce(i.get('client_id'), None),
        'milestone_label': _milestone_label(ctx, i),
        'invoiced_on': i['invoiced_on'],
        'due_on': i['due_on'],
        'taxable_paise': coalesce(i.get('taxable_paise'), i['amount_paise']),
        'gst_paise': coalesce(i.get('cgst_paise'), 0) + coalesce(i.get('sgst_paise'), 0) + coalesce(i.get('igst_paise'), 0),
        'total_paise': coalesce(i.get('total_paise'), i['amount_paise']),
        'paid_paise': settled_of(i),
        'balance_paise': balance,
        'days_overdue': diff_days(ctx.today, i['due_on']) if balance > 0 and i['due_on'] < ctx.today else 0,
        'age_days': age,
        'bucket': _bucket_of(age) if balance > 0 else None,
        'tax_mode': coalesce(i.get('tax_mode'), 'intra'),
        'stream': p['stream'] if p else 'services',
    }


def invoice_list(ctx: Ctx) -> dict[str, Any]:
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    all_ = [ensure_gst(ctx, i) for i in list(rows(ctx, 'invoices'))]
    items = sorted((_list_row(ctx, i, projects) for i in all_), key=lambda x: (locale_key(x['invoiced_on']), locale_key(x['number'])), reverse=True)
    fm = _settings_fm(ctx)
    fy = fy_start_year(ctx.today, fm)
    fr = fy_range(fy, fm)
    ageing = {k: {'amount': 0, 'count': 0} for k in ('0_30', '31_60', '61_90', '90_plus')}
    outstanding: Any = 0
    out_count = 0
    overdue: Any = 0
    overdue_count = 0
    for x in items:
        if not x['bucket']:
            continue
        outstanding += x['balance_paise']
        out_count += 1
        ageing[x['bucket']]['amount'] += x['balance_paise']
        ageing[x['bucket']]['count'] += 1
        if x['status'] == 'overdue':
            overdue += x['balance_paise']
            overdue_count += 1
    live = [x for x in items if x['status'] != 'draft' and x['status'] != 'cancelled']
    in_fy = [x for x in live if fr['from'] <= x['invoiced_on'] <= fr['to']]
    live_ids = {i['id'] for i in all_ if is_live(i)}
    pays = [p for p in rows(ctx, 'payments') if p['invoice_id'] in live_ids and fr['from'] <= p['paid_on'] <= fr['to']]
    lo = add_days(ctx.today, -90)
    billed90: Any = 0
    for x in live:
        if x['invoiced_on'] > lo and x['invoiced_on'] <= ctx.today:
            billed90 += x['total_paise']
    collected: Any = 0
    for p in pays:
        collected += coalesce(p.get('gross_paise'), p['amount_paise']) + coalesce(p.get('tds_paise'), 0)
    billed_fy: Any = 0
    gst_fy: Any = 0
    for x in in_fy:
        billed_fy += x['taxable_paise']
        gst_fy += x['gst_paise']
    return {
        'items': items,
        'summary': {
            'fy_label': fy_label(fy),
            'billed_fy': billed_fy,
            'gst_fy': gst_fy,
            'collected_fy': collected,
            'outstanding': outstanding,
            'outstanding_count': out_count,
            'overdue': overdue,
            'overdue_count': overdue_count,
            'drafts': sum(1 for x in items if x['status'] == 'draft'),
            'dso': js_round(outstanding / (billed90 / 90)) if billed90 > 0 else None,
            'ageing': ageing,
        },
        'can_edit': has(ctx, 'finance.edit'),
    }


def invoice_detail(ctx: Ctx, id_: str) -> dict[str, Any]:
    inv = ensure_gst(ctx, find(ctx, 'invoices', id_, 'Invoice'))
    p = find(ctx, 'projects', inv['project_id'], 'Project')
    mode = coalesce(inv.get('tax_mode'), 'intra')
    totals = compute_totals(coalesce(inv.get('lines'), []), mode, js_truthy(inv.get('reverse_charge')))
    pays = sorted((x for x in rows(ctx, 'payments') if x['invoice_id'] == inv['id']), key=lambda x: locale_key(x['paid_on']))
    ids = _ms_ids_len(inv)
    balance = balance_of(inv)
    can_edit = has(ctx, 'finance.edit')
    status = display_status(ctx, inv)
    live = is_live(inv)
    total_p = inv.get('total_paise')
    taxable_left = js_round((balance * coalesce(inv.get('taxable_paise'), 0)) / total_p) if live and js_truthy(total_p) else 0
    cid = inv.get('client_id')
    client = next((c for c in rows(ctx, 'clients') if c['id'] == cid), None) if js_truthy(cid) else None
    ms_by = {}
    for m in rows(ctx, 'milestones'):
        ms_by.setdefault(m['id'], m)
    pos = inv.get('place_of_supply')
    settled = settled_of(inv)
    return {
        'invoice': inv,
        'status': status,
        'totals': totals,
        'place_of_supply_label': f'{state_name(pos)} ({js_str(pos)})' if js_truthy(pos) else '—',
        'project': {'id': p['id'], 'code': p['code'], 'name': p['name'], 'client': p['client'], 'billing_model': p['billing_model'], 'stream': p['stream']},
        'milestones': [
            {'id': m['id'], 'no': m['no'], 'name': m['name'], 'billing_amount_paise': m['billing_amount_paise'], 'billing_status': m['billing_status']}
            for m in (ms_by.get(mid) for mid in ids)
            if m
        ],
        'payments': [
            {
                'id': x['id'],
                'paid_on': x['paid_on'],
                'reference': x.get('reference', UNDEF),
                'mode': coalesce(x.get('mode'), ''),
                'gross_paise': coalesce(x.get('gross_paise'), x['amount_paise']),
                'tds_paise': coalesce(x.get('tds_paise'), 0),
                'revenue_paise': x['amount_paise'],
            }
            for x in pays
        ],
        'settled_paise': settled,
        'received_paise': coalesce(inv.get('received_paise'), 0),
        'tds_paise': coalesce(inv.get('tds_received_paise'), 0),
        'balance_paise': balance,
        'days_overdue': diff_days(ctx.today, inv['due_on']) if balance > 0 and inv['due_on'] < ctx.today else 0,
        'suggested_tds_paise': js_round((coalesce(inv.get('tds_pct'), 0) / 100) * taxable_left),
        'ledger': [
            {'id': l['id'], 'date': l['date'], 'category': l['category'], 'amount_paise': l['amount_paise'], 'status': l.get('status', UNDEF)}
            for l in sorted((l for l in rows(ctx, 'ledger_entries') if l.get('invoice_id') == inv['id']), key=lambda l: locale_key(l['date']))  # noqa: E741
        ],
        'activity': [
            {'at': a['at'], 'actor': user_name(ctx, a.get('actor_id')), 'summary': a['summary']}
            for a in sorted(
                (a for a in rows_where_ro(ctx, 'activity_log', 'entity_id', inv['id']) if a.get('entity') == 'invoice'), key=lambda a: locale_key(a['at']), reverse=True
            )[:30]
        ],
        'emails': [{'to': prop(e, 'to'), 'at': prop(e, 'at'), 'by': user_name(ctx, prop(e, 'by'))} for e in coalesce(inv.get('emails'), [])],
        'client_email': js_or(prop(inv.get('bill_to'), 'email'), prop(client, 'email'), ''),
        'can_edit': can_edit,
        'can_pay': can_edit and live and balance > 0,
        'can_cancel': can_edit and live and settled == 0,
        'cancel_block': 'Payments are recorded on this invoice, so it can’t be cancelled.' if live and settled > 0 else None,
        'next_number': next_invoice_number(ctx, inv['invoiced_on'] if inv['invoiced_on'] <= ctx.today else ctx.today) if inv.get('status') == 'draft' else None,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Prefill and billable hours
# ---------------------------------------------------------------------------------------------------------------------
def _live_milestones(ctx: Ctx, pid: str) -> list[dict]:
    return sorted((m for m in rows(ctx, 'milestones') if m['project_id'] == pid and not js_truthy(m.get('deleted_at'))), key=lambda m: m['no'])


def milestone_line(ctx: Ctx, m: dict, p: dict, gst: Any) -> dict[str, Any]:
    s = invoice_settings(ctx)
    return {
        'id': f'ms_{m["id"]}',
        'item': _ms_label(m),
        'description': f'{js_str(p["name"])} — milestone {js_str(m["no"])}',
        'hsn_sac': s['default_sac'],
        'qty': 1,
        'unit': 'Lot',
        'rate_paise': m['billing_amount_paise'],
        'discount_pct': 0,
        'gst_rate': gst,
        'source': {'kind': 'milestone', 'milestone_id': m['id']},
    }


def _prefill_milestones(ctx: Ctx, p: dict, self_id: Any = UNDEF) -> list[dict]:
    tasks = [t for t in rows(ctx, 'tasks') if t['project_id'] == p['id']]
    invs = [i for i in rows(ctx, 'invoices') if i['project_id'] == p['id'] and i['id'] != self_id]
    out = []
    for m in _live_milestones(ctx, p['id']):
        own = [i for i in invs if m['id'] in _ms_ids_of(i)]
        live = next((i for i in own if is_live(i)), None)
        out.append(
            {
                'id': m['id'],
                'no': m['no'],
                'name': m['name'],
                'billing_amount_paise': m['billing_amount_paise'],
                'billing_status': m['billing_status'],
                'state': milestone_state(ctx, m, tasks)['state'],
                'planned_end': m['planned_end'],
                'invoice_number': coalesce(live.get('number', UNDEF), None) if live else None,
                'draft_numbers': [_draft_label(i) for i in own if i.get('status') == 'draft'],
            }
        )
    return out


def prefill(ctx: Ctx, project_id: str, milestone_ids: list) -> dict[str, Any]:
    p = find(ctx, 'projects', project_id, 'Project')
    s = invoice_settings(ctx)
    seller = seller_party(ctx)
    client = client_for_project(ctx, p)
    bill = client_party(client, p['client'])
    pos = js_or(bill['state_code'], seller['state_code'])
    mode = tax_mode_of(seller['state_code'], pos, bill['country'])
    gst = 0 if mode == 'export' else s['default_gst_rate']
    ms = [m for m in _live_milestones(ctx, p['id']) if m['id'] in milestone_ids]
    date = ctx.today
    terms = coalesce(client.get('payment_terms_days') if client else UNDEF, s['payment_terms_days'])
    contract = next((c for c in rows(ctx, 'contracts') if c.get('project_id') == p['id'] and c['status'] != 'terminated'), None)
    draft = {
        'project_id': p['id'],
        'milestone_ids': [m['id'] for m in ms],
        'client_id': client['id'] if client else None,
        'seller': seller,
        'bill_to': bill,
        'ship_to': None,
        'invoiced_on': date,
        'due_on': add_days(date, terms),
        'place_of_supply': pos,
        'po_reference': contract['title'] if contract else '',
        'po_date': coalesce(contract.get('start_on') if contract else UNDEF, None),
        'reverse_charge': False,
        'lines': [milestone_line(ctx, m, p, gst) for m in ms],
        'tds_pct': 0 if mode == 'export' else s['default_tds_pct'],
        'bank': {**s['bank']},
        'notes': s['default_notes'],
        'terms': s['default_terms'],
        'signatory': {'name': s['signatory_name'], 'title': s['signatory_title']},
    }
    return {
        'draft': draft,
        'project': {
            'id': p['id'],
            'code': p['code'],
            'name': p['name'],
            'client': p['client'],
            'billing_model': p['billing_model'],
            'stream': p['stream'],
            'status': p['status'],
        },
        'milestones': _prefill_milestones(ctx, p),
        'payment_terms_days': terms,
        'client_found': bool(client),
    }


def _r2(x: Any) -> Any:
    return js_round(x * 100) / 100


def billable_hours(ctx: Ctx, project_id: str, frm: str, to: str, self_id: Any = UNDEF) -> dict[str, Any]:
    p = find(ctx, 'projects', project_id, 'Project')
    if not _date_ok(frm) or not _date_ok(to):
        err(422, 'VALIDATION', 'Pick the period to bill.', 'from')
    if to < frm:
        err(422, 'DATE_RANGE', 'The end date is before the start date.', 'to')
    task_ids = {t['id'] for t in rows(ctx, 'tasks') if t['project_id'] == p['id']}
    entries = [
        e for e in costed_entries(ctx) if e.get('kind') == 'task' and js_truthy(e.get('task_id')) and e['task_id'] in task_ids and frm <= e['work_date'] <= to
    ]
    # ranges already billed (issued, not cancelled), per person
    billed: dict[str, list] = {}
    for i in rows(ctx, 'invoices'):
        if i['project_id'] != p['id'] or not is_live(i) or i['id'] == self_id:
            continue
        for l in coalesce(i.get('lines'), []):  # noqa: E741
            src = prop(l, 'source')
            if prop(src, 'kind') == 'hours' and js_truthy(prop(src, 'user_id')) and js_truthy(prop(src, 'from')) and js_truthy(prop(src, 'to')):
                billed[src['user_id']] = [*billed.get(src['user_id'], []), {'from': src['from'], 'to': src['to']}]

    def covered(uid_: str, d: str) -> bool:
        return any(r['from'] <= d <= r['to'] for r in billed.get(uid_, []))

    markup = coalesce(prop(p.get('costing'), 'markup_pct'), 35) / 100
    per: dict[str, dict] = {}
    for e in entries:
        r = per.get(e['user_id'])
        if r is None:
            r = {
                'user_id': e['user_id'],
                'name': user_name(ctx, e['user_id']),
                'approved_hours': 0,
                'billed_hours': 0,
                'unbilled_hours': 0,
                'pending_hours': 0,
                'rate_paise': 0,
            }
            per[e['user_id']] = r
        if not e['approved']:
            r['pending_hours'] += e['hours']
        elif covered(e['user_id'], e['work_date']):
            r['approved_hours'] += e['hours']
            r['billed_hours'] += e['hours']
        else:
            r['approved_hours'] += e['hours']
            r['unbilled_hours'] += e['hours']
    for r in per.values():
        rate = current_rate_ph(ctx, r['user_id']) * (1 + markup)
        r['rate_paise'] = js_max(5000, js_round(rate / 5000) * 5000) if rate > 0 else 150000  # nearest ₹50; ₹1,500/h when no cost rate
        for k in ('approved_hours', 'billed_hours', 'unbilled_hours', 'pending_hours'):
            r[k] = _r2(r[k])
    people = sorted(per.values(), key=lambda x: (-x['unbilled_hours'], locale_key(x['name'])))
    tot: Any = 0
    for x in people:
        tot += x['unbilled_hours']
    return {'from': frm, 'to': to, 'people': people, 'total_unbilled': _r2(tot)}


# ---------------------------------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------------------------------
VIEW = 'Invoices are visible to finance only (CEO and administrators by default).'
EDIT = 'Only people who can raise invoices (finance.edit) can do this.'


def _list(req: Req) -> Any:
    require_action(req.ctx, 'finance.view', VIEW)
    return invoice_list(req.ctx)


def _meta(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.view', VIEW)
    projects = sorted(
        (
            {'id': p['id'], 'code': p['code'], 'name': p['name'], 'client': p['client'], 'billing_model': p['billing_model'], 'status': p['status']}
            for p in rows(ctx, 'projects')
            if not p['code'].endswith('GEN')
        ),
        key=lambda x: locale_key(x['code']),
    )
    clients = sorted(
        (
            {'id': c['id'], 'name': c['name'], 'gstin': prop(c, 'gstin'), 'state': client_party(c)['state'], 'country': prop(c, 'country')}
            for c in rows(ctx, 'clients')
        ),
        key=lambda x: locale_key(x['name']),
    )
    return {
        'states': GST_STATES,
        'units': UNITS,
        'gst_rates': GST_RATES,
        'settings': settings_for(ctx),
        'seller': seller_party(ctx),
        'projects': projects,
        'clients': clients,
        'next_number': next_invoice_number(ctx, ctx.today),
        'today': ctx.today,
    }


def _get_settings(req: Req) -> Any:
    require_action(req.ctx, 'finance.view', VIEW)
    return settings_for(req.ctx)


_SERIES_RE = re.compile(r'^[A-Z0-9-]{1,6}\Z')


def _put_settings(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'finance.edit', EDIT)
    cur = invoice_settings(ctx)
    cb = cur['bank']

    def bg(k: str) -> Any:
        return _g(body, k)

    b = coalesce(bg('bank'), {})

    def bk(k: str) -> Any:
        return _g(b, k)

    prefix = js_upper(_str(coalesce(bg('prefix'), cur['prefix'])))
    if not _SERIES_RE.search(prefix):
        err(422, 'VALIDATION', 'Prefix: 1–6 letters or digits (e.g. SCI). The number becomes SCI/26-27/0001.', 'prefix')
    cn = js_upper(_str(coalesce(bg('credit_note_prefix'), cur['credit_note_prefix'])))
    if not _SERIES_RE.search(cn):
        err(422, 'VALIDATION', 'Credit-note prefix: 1–6 letters or digits (e.g. CN).', 'credit_note_prefix')
    terms = js_number(coalesce(bg('payment_terms_days'), cur['payment_terms_days']))
    if not is_integer(terms) or terms < 0 or terms > 180:
        err(422, 'VALIDATION', 'Payment terms are 0–180 days.', 'payment_terms_days')
    sac = _str(coalesce(bg('default_sac'), cur['default_sac']))
    if not SAC_RE.search(sac):
        err(422, 'VALIDATION', 'HSN/SAC is 4–8 digits (IT services: 998314).', 'default_sac')
    gst = js_number(coalesce(bg('default_gst_rate'), cur['default_gst_rate']))
    if gst not in GST_RATES:
        err(422, 'VALIDATION', 'GST rate is 0, 5, 12, 18 or 28%.', 'default_gst_rate')
    tds = js_number(coalesce(bg('default_tds_pct'), cur['default_tds_pct']))
    if not (tds >= 0 and tds <= 20):
        err(422, 'VALIDATION', 'TDS is between 0% and 20%.', 'default_tds_pct')
    acct = prop(cb, 'account_number') if bk('account_number') is UNDEF else re.sub(r'\s+', '', _str(bk('account_number')))
    if js_truthy(acct) and not re.search(r'^[0-9]{9,18}\Z', js_str(acct)):
        err(422, 'VALIDATION', 'Account number is 9–18 digits.', 'bank.account_number')
    ifsc = js_upper(_str(coalesce(bk('ifsc'), prop(cb, 'ifsc'))))
    if ifsc and not IFSC_RE.search(ifsc):
        err(422, 'VALIDATION', 'IFSC is 11 characters, e.g. HDFC0001827.', 'bank.ifsc')
    upi = _str(coalesce(bk('upi'), prop(cb, 'upi')))
    if upi and not re.search(r'^[\w.-]{2,}@[A-Za-z]{2,}\Z', upi, re.ASCII):
        err(422, 'VALIDATION', 'UPI ID looks like name@bank.', 'bank.upi')
    dterms = bg('default_terms')
    nxt = {
        'prefix': prefix,
        'credit_note_prefix': cn,
        'payment_terms_days': terms,
        'default_sac': sac,
        'default_gst_rate': gst,
        'default_tds_pct': tds,
        'default_unit': js_or(_str(coalesce(bg('default_unit'), cur['default_unit'])), 'Nos'),
        'bank': {
            'bank_name': _str(coalesce(bk('bank_name'), prop(cb, 'bank_name'))),
            'account_name': _str(coalesce(bk('account_name'), prop(cb, 'account_name'))),
            'account_number': acct,
            'ifsc': ifsc,
            'branch': _str(coalesce(bk('branch'), prop(cb, 'branch'))),
            'upi': upi,
        },
        'signatory_name': _str(coalesce(bg('signatory_name'), cur['signatory_name'])),
        'signatory_title': _str(coalesce(bg('signatory_title'), cur['signatory_title'])),
        'lut_number': _str(coalesce(bg('lut_number'), cur['lut_number'])),
        'default_notes': _str(coalesce(bg('default_notes'), cur['default_notes'])),
        'default_terms': js_trim(dterms) if isinstance(dterms, str) else cur['default_terms'],
        'updated_at': now_iso(),
        'updated_by': ctx.user_id,
    }
    t = edit('tenants', _tenant_row(ctx))
    t['invoicing'] = thaw(nxt)
    # keep the org profile's masked account + bank in step (shown on Setup → Organisation)
    if js_truthy(t.get('org')):
        t['org']['bank_name'] = nxt['bank']['bank_name']
        t['org']['ifsc'] = nxt['bank']['ifsc']
        if js_truthy(acct):
            t['org']['bank_account_masked'] = _mask(acct)
    persist()
    acct_txt = '••' + js_str(acct)[-4:] if js_truthy(acct) else ''
    log_activity(ctx, 'tenant', t['id'], 'invoice.settings', f'Invoice settings updated (series {prefix}, bank {js_or(nxt["bank"]["bank_name"], "—")} {acct_txt})')
    return settings_for(ctx)


def _next_number(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.view', VIEW)
    d = js_str(coalesce(req.q('date'), ''))
    date = d if _date_ok(d) else ctx.today
    return {'number': next_invoice_number(ctx, date)}


def _prefill(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', EDIT)
    pid = _str(req.q('project_id'))
    if not pid:
        err(422, 'VALIDATION', 'Choose a project.', 'project_id')
    mids = [x for x in (js_trim(y) for y in js_split(_str(req.q('milestone_ids')), ',')) if x]
    return prefill(ctx, pid, mids)


def _project_milestones(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.view', VIEW)
    p = find(ctx, 'projects', _str(req.q('project_id')), 'Project')
    return _prefill_milestones(ctx, p, js_or(_str(req.q('invoice_id')), UNDEF))


def _client_party(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.view', VIEW)
    c = find(ctx, 'clients', _str(req.q('client_id')), 'Client')
    return {'party': client_party(c), 'payment_terms_days': coalesce(c.get('payment_terms_days'), invoice_settings(ctx)['payment_terms_days'])}


def _billable(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', EDIT)
    return billable_hours(ctx, _str(req.q('project_id')), _str(req.q('from')), _str(req.q('to')), js_or(_str(req.q('invoice_id')), UNDEF))


def _preview(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.view', VIEW)
    v = normalise(req.body)
    self_id = js_or(_str(_g(req.body, 'id')), UNDEF)
    mode = tax_mode_of(v['seller']['state_code'], v['place_of_supply'], v['bill_to']['country'])
    totals = compute_totals(v['lines'], mode, v['reverse_charge'])
    return {
        'totals': totals,
        'issues': validate_input(ctx, v, {'issuing': True, 'selfId': self_id}),
        'next_number': next_invoice_number(ctx, v['invoiced_on'] if _date_ok(v['invoiced_on']) else ctx.today),
    }


def _detail(req: Req) -> Any:
    require_action(req.ctx, 'finance.view', VIEW)
    return invoice_detail(req.ctx, req.params['id'])


def _create(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', EDIT)
    v = normalise(req.body)
    issuing = js_truthy(_g(req.body, 'issue'))
    issues = validate_input(ctx, v, {'issuing': issuing})
    if issues:
        _fail(issues)
    if js_truthy(_g(req.body, 'save_client')):
        _save_client_back(ctx, v)
    no = _next_draft_no(ctx)
    inv = insert(
        ctx,
        'invoices',
        {
            'number': f'DRAFT-{js_str(no)}',
            'draft_no': no,
            'status': 'draft',
            'amount_paise': 0,
            'paid_amount_paise': 0,
            'paid_on': None,
            'milestone_id': '',
            'file_id': None,
            'emails': [],
            'received_paise': 0,
            'tds_received_paise': 0,
            **_draft_fields(v),
        },
    )
    log_activity(ctx, 'invoice', inv['id'], 'created', f'Draft {js_str(no)} created for {fmt_money(coalesce(inv.get("total_paise"), 0))}')
    if issuing:
        inv = issue_invoice(ctx, inv, v['number'])
    return invoice_detail(ctx, inv['id'])


def _js_keys(v: Any) -> list[str]:
    """Object.keys(v)"""
    if isinstance(v, dict):
        return list(v.keys())
    if isinstance(v, (list, str)):
        return [str(i) for i in range(len(v))]
    return []


def _patch(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'finance.edit', EDIT)
    inv = find(ctx, 'invoices', req.params['id'], 'Invoice')
    if inv.get('status') == 'cancelled':
        err(409, 'INV_LOCKED', 'This invoice is cancelled and can’t be changed.')
    if inv.get('status') != 'draft':
        # issued invoices are locked: notes only
        extra = [k for k in _js_keys(body) if k not in ('notes', 'id')]
        if extra:
            err(409, 'INV_LOCKED', 'An issued invoice is locked except its notes. Cancel it (with a reason) and raise a new one to change anything else.')
        u = update(ctx, 'invoices', inv['id'], {'notes': _str(_g(body, 'notes'))}, req.if_match)
        log_activity(ctx, 'invoice', inv['id'], 'notes', 'Notes updated')
        return invoice_detail(ctx, u['id'])
    v = normalise(body)
    issuing = js_truthy(_g(body, 'issue'))
    issues = validate_input(ctx, v, {'issuing': issuing, 'selfId': inv['id']})
    if issues:
        _fail(issues)
    if js_truthy(_g(body, 'save_client')):
        _save_client_back(ctx, v)
    u = update(ctx, 'invoices', inv['id'], _draft_fields(v), req.if_match)
    log_activity(ctx, 'invoice', inv['id'], 'edited', f'Draft {js_str(coalesce(inv.get("draft_no"), ""))} saved ({fmt_money(coalesce(u.get("total_paise"), 0))})')
    if issuing:
        u = issue_invoice(ctx, u, v['number'])
    return invoice_detail(ctx, u['id'])


def _fill_blanks_from_masters(ctx: Ctx, v: dict) -> dict:
    """the draft with its blank seller / bank / signatory fields taken from the organisation profile and the invoice
    settings (the same `v` object when nothing was blank)"""
    s = invoice_settings(ctx)
    master_seller = seller_party(ctx)
    seller = dict(v['seller'])
    for k, val in master_seller.items():
        if (k not in seller or seller[k] in ('', None)) and val not in ('', None):
            seller[k] = val
    bank = dict(v['bank'])
    for k, val in (s.get('bank') or {}).items():
        if bank.get(k) in ('', None) and val not in ('', None):
            bank[k] = val
    sig = dict(v['signatory'])
    if not sig.get('name') and s.get('signatory_name'):
        sig['name'] = s['signatory_name']
    if not sig.get('title') and s.get('signatory_title'):
        sig['title'] = s['signatory_title']
    if seller == v['seller'] and bank == v['bank'] and sig == v['signatory']:
        return v
    return {**v, 'seller': _party(seller), 'bank': bank, 'signatory': sig}


def _issue(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', EDIT)
    inv = find(ctx, 'invoices', req.params['id'], 'Invoice')
    if inv.get('status') != 'draft':
        err(409, 'INV_LOCKED', 'Only a draft can be issued.')
    v = normalise({**inv, 'number': _g(req.body, 'number')})
    # v8 QA: a draft saved while Setup → Organisation / invoice settings were still incomplete keeps blank seller, bank
    # and signatory fields; issuing fills ONLY those blanks from the current masters (never overrides what was typed)
    filled = _fill_blanks_from_masters(ctx, v)
    if filled is not v:
        v = filled
        inv = update(ctx, 'invoices', inv['id'], _draft_fields(v))
    issues = validate_input(ctx, v, {'issuing': True, 'selfId': inv['id']})
    if issues:
        _fail(issues)
    issue_invoice(ctx, inv, v['number'])
    return invoice_detail(ctx, inv['id'])


def _delete(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', EDIT)
    inv = find(ctx, 'invoices', req.params['id'], 'Invoice')
    if inv.get('status') != 'draft':
        err(409, 'INV_LOCKED', 'Only drafts can be discarded. Cancel an issued invoice instead.')
    remove(ctx, 'invoices', inv['id'], req.if_match)
    log_activity(ctx, 'project', inv['project_id'], 'invoice.discarded', f'Draft invoice {js_str(coalesce(inv.get("draft_no"), ""))} discarded')
    return {'ok': True}


def _cancel(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', EDIT)
    inv = ensure_gst(ctx, find(ctx, 'invoices', req.params['id'], 'Invoice'))
    if inv.get('status') == 'draft':
        err(409, 'INV_LOCKED', 'Drafts aren’t cancelled — discard the draft instead.')
    if inv.get('status') == 'cancelled':
        err(409, 'INV_LOCKED', 'This invoice is already cancelled.')
    if settled_of(inv) > 0:
        err(409, 'INV_PAID', 'Payments are recorded on this invoice, so it can’t be cancelled.')
    reason = _str(_g(req.body, 'reason'))
    if js_len(reason) < 10:
        err(422, 'VALIDATION', 'Give the reason for cancelling (at least 10 characters).', 'reason')
    p = find(ctx, 'projects', inv['project_id'], 'Project')
    cn_no = _next_credit_note_number(ctx, ctx.today)
    taxable = coalesce(inv.get('taxable_paise'), inv['amount_paise'])
    for mid in _ms_ids_len(inv):
        m = next((x for x in rows(ctx, 'milestones') if x['id'] == mid), None)
        if m and m['billing_status'] != 'none':
            update(ctx, 'milestones', m['id'], {'billing_status': 'none'})
    orig = next((l for l in rows(ctx, 'ledger_entries') if l.get('invoice_id') == inv['id'] and l['amount_paise'] > 0), None)  # noqa: E741
    if orig:
        update(ctx, 'ledger_entries', orig['id'], {'description': f'{js_str(orig["description"])} · cancelled by {cn_no}'})
    number = js_str(inv.get('number', UNDEF))
    insert(
        ctx,
        'ledger_entries',
        {
            'kind': 'income',
            'category': 'Credit note',
            'description': f'{cn_no} · cancels {number} · {js_str(p["name"])} · {reason}',
            'amount_paise': -taxable,
            'date': ctx.today,
            'recurring': False,
            'until_on': None,
            'status': 'expected',
            'project_id': p['id'],
            'milestone_id': None,
            'invoice_id': inv['id'],
            'stream': p['stream'],
            'locked': True,
        },
    )
    total = coalesce(inv.get('total_paise'), taxable)
    u = update(
        ctx,
        'invoices',
        inv['id'],
        {
            'status': 'cancelled',
            'amount_paise': 0,
            'paid_amount_paise': 0,
            'milestone_id': '',
            'cancelled_on': ctx.today,
            'cancel_reason': reason,
            'credit_note': {'number': cn_no, 'date': ctx.today, 'taxable_paise': taxable, 'total_paise': total},
        },
    )
    log_activity(ctx, 'invoice', inv['id'], 'cancelled', f'Cancelled {number} — credit note {cn_no} for {fmt_money(total)}. Reason: {reason}')
    log_activity(ctx, 'project', p['id'], 'invoice.cancelled', f'Invoice {number} cancelled ({cn_no}): {reason}')
    return invoice_detail(ctx, u['id'])


def _payments(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'finance.edit', 'Only people who can raise invoices can record payments.')
    inv = find(ctx, 'invoices', req.params['id'], 'Invoice')
    res = apply_payment(
        ctx,
        inv,
        {
            'received': js_round(js_number(coalesce(_g(body, 'received_paise'), 0))),
            'tds': js_round(js_number(coalesce(_g(body, 'tds_paise'), 0))),
            'paid_on': _str(_g(body, 'paid_on')),
            'reference': _str(_g(body, 'reference')),
            'mode': js_or(_str(_g(body, 'mode')), 'Bank transfer'),
        },
        req.if_match,
    )
    return {**res, 'detail': invoice_detail(ctx, inv['id'])}


def _email(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'finance.edit', EDIT)
    inv = ensure_gst(ctx, find(ctx, 'invoices', req.params['id'], 'Invoice'))
    if not is_live(inv):
        err(409, 'INV_NOT_ISSUED', 'Issue the invoice before emailing it.' if inv.get('status') == 'draft' else 'A cancelled invoice can’t be emailed.')
    to = _str(_g(body, 'to'))
    if not EMAIL_RE.search(to):
        err(422, 'VALIDATION', 'Enter the client’s email address.', 'to')
    cc = [x for x in re.split(r'[,;\s]+', _str(_g(body, 'cc'))) if x]
    bad_cc = next((x for x in cc if not EMAIL_RE.search(x)), None)
    if bad_cc:
        err(422, 'VALIDATION', f'{bad_cc} is not a valid email address.', 'cc')
    number = js_str(inv.get('number', UNDEF))
    subject = js_or(_str(_g(body, 'subject')), f'Tax invoice {number} from {js_str(coalesce(prop(inv.get("seller"), "name"), ""))}')
    message = _str(_g(body, 'message'))
    if js_len(message) < 5:
        err(422, 'VALIDATION', 'Write a short message to go with the invoice.', 'message')
    file = re.sub(r'[^\w-]+', '-', number, flags=re.ASCII) + '.pdf'
    bal = balance_of(inv)
    bank = inv.get('bank')
    parts = [
        message,
        '',
        f'Invoice: {number} dated {js_str(inv.get("invoiced_on", UNDEF))}',
        f'Amount: {fmt_money(coalesce(inv.get("total_paise"), 0), True)} (incl. GST)',
        f'Balance due: {fmt_money(bal, True)} by {js_str(inv.get("due_on", UNDEF))}',
        f'Bank: {js_str(prop(bank, "bank_name"))}, A/c {js_str(prop(bank, "account_number"))}, IFSC {js_str(prop(bank, "ifsc"))}' if js_truthy(bank) else '',
        '',
        f'Attachment: {file} (tax invoice PDF)',
        f'Cc: {", ".join(cc)}' if cc else '',
    ]
    body_text = '\n'.join(x for i, x in enumerate(parts) if x != '' or (i == 0 or parts[i - 1] != ''))
    msg = insert(
        ctx,
        'email_outbox',
        {
            'to': to,
            'to_user_id': None,
            'subject': subject,
            'body': body_text,
            'template_key': 'invoice',
            'related_entity': 'invoice',
            'related_id': inv['id'],
            'status': 'queued',
            'sent_at': None,
        },
    )
    emails = [*thaw(coalesce(inv.get('emails'), [])), {'to': to, 'at': now_iso(), 'by': ctx.user_id, 'outbox_id': msg['id']}]
    u = update(ctx, 'invoices', inv['id'], {'emails': emails})
    cc_txt = f' (cc {", ".join(cc)})' if cc else ''
    log_activity(ctx, 'invoice', inv['id'], 'emailed', f'Emailed to {to}{cc_txt} with {file}')
    return {'ok': True, 'outbox_id': msg['id'], 'detail': invoice_detail(ctx, u['id'])}


def _duplicate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', EDIT)
    src = ensure_gst(ctx, find(ctx, 'invoices', req.params['id'], 'Invoice'))
    s = invoice_settings(ctx)
    terms = js_max(0, diff_days(src['due_on'], src['invoiced_on']))
    no = _next_draft_no(ctx)
    # milestones are billed once: the copy keeps the lines as plain items, unlinked from milestones
    lines = [{**thaw(l), 'id': f'l{i + 1}', 'source': {'kind': 'manual'}} for i, l in enumerate(coalesce(src.get('lines'), []))]  # noqa: E741
    v = normalise(
        {**src, 'lines': lines, 'milestone_ids': [], 'invoiced_on': ctx.today, 'due_on': add_days(ctx.today, js_or(terms, s['payment_terms_days']))}
    )
    inv = insert(
        ctx,
        'invoices',
        {
            'number': f'DRAFT-{js_str(no)}',
            'draft_no': no,
            'status': 'draft',
            'amount_paise': 0,
            'paid_amount_paise': 0,
            'paid_on': None,
            'milestone_id': '',
            'file_id': None,
            'emails': [],
            'received_paise': 0,
            'tds_received_paise': 0,
            'duplicated_from': src['id'],
            **_draft_fields(v),
        },
    )
    frm = f'draft {js_str(src.get("draft_no", UNDEF))}' if src.get('status') == 'draft' else js_str(src.get('number', UNDEF))
    log_activity(ctx, 'invoice', inv['id'], 'created', f'Draft {js_str(no)} duplicated from {frm}')
    return invoice_detail(ctx, inv['id'])


def register(r: Any) -> None:
    r.get('/finance/invoices', _list)
    r.get('/finance/invoices/meta', _meta)
    r.get('/finance/invoices/settings', _get_settings)
    r.put('/finance/invoices/settings', _put_settings)
    r.get('/finance/invoices/next-number', _next_number)
    r.get('/finance/invoices/prefill', _prefill)
    r.get('/finance/invoices/project-milestones', _project_milestones)
    r.get('/finance/invoices/client-party', _client_party)
    r.get('/finance/invoices/billable-hours', _billable)
    r.post('/finance/invoices/preview', _preview)
    r.get('/finance/invoices/:id', _detail)
    r.post('/finance/invoices', _create)
    r.patch('/finance/invoices/:id', _patch)
    r.post('/finance/invoices/:id/issue', _issue)
    r.delete('/finance/invoices/:id', _delete)
    r.post('/finance/invoices/:id/cancel', _cancel)
    r.post('/finance/invoices/:id/payments', _payments)
    r.post('/finance/invoices/:id/email', _email)
    r.post('/finance/invoices/:id/duplicate', _duplicate)

