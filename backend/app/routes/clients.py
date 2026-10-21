"""Port of src/mock/routes/clients.ts — clients & contracts: client master (seeded from projects and deals, kept in
sync), computed status and health, revenue billed/collected/outstanding, contracts with renewal reminders.
Export used by spend.py (late bound): contract_renewals."""

from __future__ import annotations

import re
from datetime import date, timedelta
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
    notify,
    patch,
    remove,
    require_action,
    rows,
    scope,
    update,
    user_name,
)
from app.domain.calendar import add_days, diff_days, fy_range, fy_start_year, parse_iso, to_iso
from app.domain.money import fmt_money
from app.domain.sales import deal_value
from app.engine.analytics import project_metrics
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_len,
    js_lower,
    js_max,
    js_number,
    js_or,
    js_round,
    js_str,
    js_trim,
    js_truthy,
    js_upper,
    locale_key,
    nullish,
    prop,
    to_fixed,
)
from app.modules.spend.labels import CONTRACT_KIND, GSTIN_RE, PAN_RE, RENEWAL_WINDOW_DAYS
from app.routes.invoices import EXPORT_CODE, state_code_of, state_name
from app.routes.spend import leakage_rows, provide_clients_helpers

__all__ = ['contract_renewals', 'register']

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+\Z')
_YMD_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')


def _lc(s: Any) -> str:
    return js_lower(js_trim(s))


INTERNAL = frozenset({'internal', 'seereon', 'general & admin', ''})


def _g(o: Any, k: str) -> Any:
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    if o is None or o is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(o)} (reading '{k}')")
    return UNDEF


def _patch_quiet(table: str, row: dict, changes: dict) -> dict:
    """`row.x = v` WITHOUT persist(): the change rides along if something else persists"""
    s = scope()
    wi = s.write_intent
    new = patch(table, row, changes)
    s.write_intent = wi
    return new


def _fm(ctx: Ctx) -> Any:
    return coalesce(prop(ctx.tenant.get('settings'), 'company.fy_start_month'), 4)


def _sum(xs: Any) -> Any:
    s: Any = 0
    for x in xs:
        s = s + x
    return s


def _client_names(ctx: Ctx) -> dict[str, str]:
    names: dict[str, str] = {}
    for p in rows(ctx, 'projects'):
        if _lc(p['client']) not in INTERNAL and not p['code'].endswith('GEN') and p['code'] != 'GEN':
            names[_lc(p['client'])] = p['client']
    for d in rows(ctx, 'deals'):
        if _lc(d['client']) not in INTERNAL:
            names[_lc(d['client'])] = d['client']
    return names


def _sync_clients(ctx: Ctx) -> None:
    """Insert any client that appears on a project or deal but is not in the master yet."""
    have = {_lc(c['name']) for c in rows(ctx, 'clients')}
    for key, name in _client_names(ctx).items():
        if key in have:
            continue
        deal = next((d for d in rows(ctx, 'deals') if _lc(d['client']) == key), None)
        proj = next((p for p in rows(ctx, 'projects') if _lc(p['client']) == key), None)
        owner = coalesce(deal.get('owner_id', UNDEF) if deal else UNDEF, proj.get('pm_id', UNDEF) if proj else UNDEF, None)
        insert(
            ctx,
            'clients',
            {
                'name': name,
                'industry': '',
                'country': 'India',
                'gstin': '',
                'billing_address': '',
                'primary_contact': coalesce(deal.get('contact', UNDEF) if deal else UNDEF, ''),
                'email': coalesce(deal.get('email', UNDEF) if deal else UNDEF, ''),
                'phone': coalesce(deal.get('phone', UNDEF) if deal else UNDEF, ''),
                'owner_id': owner,
                'status': 'prospect',
                'since': None,
                'payment_terms_days': 30,
                'health': None,
            },
        )


def _matches_client(client: str, text: str) -> bool:
    c = _lc(client)
    t = _lc(text)
    if c in t:
        return True
    first = re.split(r'\s+', c)[0]
    if js_len(first) < 5:
        return False
    return re.search(r'\b' + re.sub(r'[^a-z0-9]', '', first) + r'\b', t, re.ASCII) is not None


def _contract_status(ctx: Ctx, c: dict) -> str:
    st = c.get('status')
    if st in ('draft', 'terminated', 'renewed'):
        return st
    if c['end_on'] < ctx.today:
        return 'expired'
    due = coalesce(c.get('renewal_on'), c['end_on'])
    if diff_days(due, ctx.today) <= RENEWAL_WINDOW_DAYS:
        return 'expiring'
    return 'active'


def _contract_row(ctx: Ctx, c: dict, clients: dict, pm: dict) -> dict[str, Any]:
    due = coalesce(c.get('renewal_on'), c['end_on'])
    f = next((x for x in rows(ctx, 'files') if x['id'] == c['file_id']), None) if js_truthy(c.get('file_id')) else None
    computed = _contract_status(ctx, c)
    cl = clients.get(c['client_id'])
    p = pm.get(c['project_id']) if js_truthy(c.get('project_id')) else None
    return {
        **c,
        'client_name': cl['name'] if cl else '—',
        'project_code': coalesce(p.get('code', UNDEF) if p else UNDEF, None) if js_truthy(c.get('project_id')) else None,
        'days_to_renewal': diff_days(due, ctx.today) if computed in ('active', 'expiring') else None,
        'computed_status': computed,
        'status': computed,
        'file_name': coalesce(f.get('name', UNDEF) if f else UNDEF, c.get('file_name', UNDEF), None),
    }


def _remind_renewals(ctx: Ctx) -> None:
    """Renewal reminders: notify the account manager once per contract when it enters the 60-day window."""
    clients = {c['id']: c for c in rows(ctx, 'clients')}
    for c in list(rows(ctx, 'contracts')):
        if _contract_status(ctx, c) != 'expiring' or js_truthy(c.get('reminded_on')):
            continue
        cl = clients.get(c['client_id'])
        to = cl.get('owner_id', UNDEF) if cl else UNDEF
        c = _patch_quiet('contracts', c, {'reminded_on': ctx.today})
        if not js_truthy(to):
            continue
        due = coalesce(c.get('renewal_on'), c['end_on'])
        notify(
            ctx,
            to,
            'NTF-13',
            'Contract renewal due',
            f'{js_str(cl["name"])} · {js_str(CONTRACT_KIND.get(c["kind"], UNDEF))} “{js_str(c["title"])}” {"auto-renews" if js_truthy(c.get("auto_renew")) else "is up for renewal"} on {js_str(due)} ({fmt_money(c["value_paise"])}).',  # type: ignore[index]
            f'/clients/{c["client_id"]}',
        )


def _client_model(ctx: Ctx) -> dict[str, Any]:
    _sync_clients(ctx)
    _remind_renewals(ctx)
    fy = fy_start_year(ctx.today, _fm(ctx))
    fy_from = fy_range(fy, _fm(ctx))['from']
    projects = rows(ctx, 'projects')
    pm = {p['id']: p for p in projects}
    invoices = [i for i in rows(ctx, 'invoices') if i.get('status') != 'draft' and i.get('status') != 'cancelled']
    deals = rows(ctx, 'deals')
    contracts = list(rows(ctx, 'contracts'))
    income_ledger = [l for l in rows(ctx, 'ledger_entries') if l['kind'] == 'income' and not js_truthy(l.get('invoice_id')) and l['date'] <= ctx.today]  # noqa: E741
    leak = [r for r in leakage_rows(ctx) if r['date'] >= fy_from]
    clients_raw = list(rows(ctx, 'clients'))
    cmap = {c['id']: c for c in clients_raw}
    items = []
    for c in clients_raw:
        key = _lc(c['name'])
        ps = [p for p in projects if _lc(p['client']) == key]
        ids = {p['id'] for p in ps}
        inv = [i for i in invoices if i['project_id'] in ids]
        other = [l for l in income_ledger if _matches_client(c['name'], l['description'])]  # noqa: E741
        ds = [d for d in deals if _lc(d['client']) == key]
        cs = [x for x in contracts if x['client_id'] == c['id']]
        billed = _sum(i['amount_paise'] for i in inv) + _sum(l['amount_paise'] for l in other)  # noqa: E741
        billed_fy = _sum(i['amount_paise'] for i in inv if i['invoiced_on'] >= fy_from) + _sum(l['amount_paise'] for l in other if l['date'] >= fy_from)  # noqa: E741
        collected = _sum(i['paid_amount_paise'] for i in inv) + _sum(l['amount_paise'] for l in other if l.get('status') == 'received')  # noqa: E741
        overdue_inv = [i for i in inv if i['amount_paise'] > i['paid_amount_paise'] and i['due_on'] < ctx.today]
        overdue = _sum(i['amount_paise'] - i['paid_amount_paise'] for i in overdue_inv)
        max_days: Any = 0
        for i in overdue_inv:
            max_days = js_max(max_days, diff_days(ctx.today, i['due_on']))
        # health: overdue invoices + project health
        reasons: list[str] = []
        score = 0
        if max_days > 60:
            score = 2
            reasons.append(f'Invoice overdue {js_str(max_days)} days')
        elif overdue > 0:
            score = max(score, 1)
            reasons.append(f'{fmt_money(overdue)} overdue')
        for p in ps:
            if p['status'] not in ('active', 'on_hold', 'npa'):
                continue
            h = project_metrics(ctx, p)['health']
            if p['status'] == 'npa' or h == 'off_track':
                score = 2
                reasons.append(f'{js_str(p["code"])} {"is NPA" if p["status"] == "npa" else "is off track"}')
            elif h == 'at_risk' or p['status'] == 'on_hold':
                score = max(score, 1)
                reasons.append(f'{js_str(p["code"])} {"on hold" if p["status"] == "on_hold" else "at risk"}')
        has_business = len(ps) > 0 or len(inv) > 0 or len(other) > 0 or len(cs) > 0 or any(d['stage'] == 'won' for d in ds)
        health = ('at_risk' if score == 2 else 'watch' if score == 1 else 'good') if has_business else None
        # status: churned is a manual decision; the rest follows the data
        status = c.get('status', UNDEF)
        if not js_truthy(c.get('status_manual')):
            active_p = any(p['status'] in ('active', 'on_hold', 'initiated', 'npa') for p in ps)
            active_c = any(_contract_status(ctx, x) in ('active', 'expiring') for x in cs)
            recent_won = any(d['stage'] == 'won' and js_truthy(d.get('won_on')) and diff_days(ctx.today, d['won_on']) <= 180 for d in ds)
            recent_income = any(diff_days(ctx.today, l['date']) <= 90 for l in other)  # noqa: E741
            status = 'active' if active_p or active_c or recent_won or recent_income else 'dormant' if billed > 0 or len(ps) else 'prospect'
        if status != c.get('status', UNDEF) or health != c.get('health', UNDEF):
            c = _patch_quiet('clients', c, {'status': status, 'health': health})
        live = [x for x in cs if _contract_status(ctx, x) in ('active', 'expiring')]
        dues = sorted(coalesce(x.get('renewal_on'), x['end_on']) for x in live)
        open_deals = [d for d in ds if d['stage'] != 'won' and d['stage'] != 'lost']
        items.append(
            {
                **c,
                'status': status,
                'health': health,
                'owner_name': user_name(ctx, c.get('owner_id')),
                'projects': len(ps),
                'active_projects': sum(1 for p in ps if p['status'] == 'active'),
                'open_deals': len(open_deals),
                'pipeline_value': _sum(deal_value(d) for d in open_deals),
                'billed': billed,
                'billed_fy': billed_fy,
                'collected': collected,
                'outstanding': js_max(0, _sum(i['amount_paise'] - i['paid_amount_paise'] for i in inv)),
                'overdue': overdue,
                'contracts': len(live),
                'contract_value': _sum(x['value_paise'] for x in live),
                'next_renewal': dues[0] if dues else None,
                'leakage_fy': _sum(l['amount_paise'] - l['recovered_paise'] for l in leak if _lc(l['client']) == key),  # noqa: E741
                'health_reasons': reasons,
            }
        )
    renewals = sorted(
        (_contract_row(ctx, x, cmap, pm) for x in contracts if _contract_status(ctx, x) == 'expiring'), key=lambda r: coalesce(r['days_to_renewal'], 0)
    )
    return {'items': items, 'renewals': renewals, 'cmap': cmap, 'pm': pm}


def contract_renewals(ctx: Ctx) -> list[dict]:
    clients = {c['id']: c for c in rows(ctx, 'clients')}
    out = []
    for c in rows(ctx, 'contracts'):
        if _contract_status(ctx, c) != 'expiring':
            continue
        due = coalesce(c.get('renewal_on'), c['end_on'])
        cl = clients.get(c['client_id'])
        out.append(
            {
                'id': c['id'],
                'client_id': c['client_id'],
                'client': cl['name'] if cl else '—',
                'title': c['title'],
                'kind': c['kind'],
                'renewal_on': due,
                'value_paise': c['value_paise'],
                'days': diff_days(due, ctx.today),
            }
        )
    return sorted(out, key=lambda x: x['days'])


def _validate_client(ctx: Ctx, body: Any, existing: dict | None = None) -> dict[str, Any]:
    def ex(k: str) -> Any:
        return existing.get(k, UNDEF) if existing else UNDEF

    def bg(k: str) -> Any:
        return _g(body, k)

    name = js_trim(js_str(coalesce(bg('name'), ex('name'), '')))
    if js_len(name) < 2:
        err(422, 'VAL_REQUIRED', 'Client name is required.', 'name')
    ex_id = ex('id')
    dup = next((c for c in rows(ctx, 'clients') if _lc(c['name']) == _lc(name) and c['id'] != ex_id), None)
    if dup:
        err(409, 'DUPLICATE_CLIENT', f'{js_str(dup["name"])} is already a client.', 'name')
    gstin = js_upper(js_trim(js_str(coalesce(bg('gstin'), ex('gstin'), ''))))
    if gstin and not GSTIN_RE.search(gstin):
        err(422, 'VAL_GSTIN', 'GSTIN must be 15 characters (e.g. 21AAKFM9876Q1ZP). Leave it blank for overseas clients.', 'gstin')
    email = js_trim(js_str(coalesce(bg('email'), ex('email'), '')))
    if email and not _EMAIL_RE.search(email):
        err(422, 'VAL_EMAIL', 'Enter a valid email address.', 'email')
    terms = js_number(coalesce(bg('payment_terms_days'), ex('payment_terms_days'), 30))
    if not is_integer(terms) or terms < 0 or terms > 180:
        err(422, 'VAL_RANGE', 'Payment terms must be between 0 and 180 days.', 'payment_terms_days')
    bo = bg('owner_id')
    owner_id = coalesce(ex('owner_id'), None) if bo is UNDEF else js_or(bo, None)
    if js_truthy(owner_id):
        find(ctx, 'users', owner_id, 'Account manager')
    status = bg('status')
    # v7 GST billing address: state + GST state code must agree with the GSTIN's first two digits
    country = js_or(js_trim(js_str(coalesce(bg('country'), ex('country'), 'India'))), 'India')
    india = re.search(r'^(india|in)\Z', country, re.I) is not None
    state_in = js_trim(js_str(coalesce(bg('state'), ex('state'), '')))
    state_code = js_or(js_trim(js_str(coalesce(bg('state_code'), ex('state_code'), ''))), state_code_of(state_in), gstin[:2] if gstin else '')
    if not india:
        state_code = EXPORT_CODE
    if india and state_code and not state_name(state_code):
        err(422, 'VAL_STATE', 'Choose a valid Indian state.', 'state')
    if india and gstin and state_code and gstin[:2] != state_code:
        err(
            422,
            'VAL_GSTIN',
            f'GSTIN starts with {gstin[:2]} ({state_name(gstin[:2]) or "unknown state"}) but the state is {state_name(state_code)}.',
            'gstin',
        )
    if not india and gstin:
        err(422, 'VAL_GSTIN', 'Overseas clients have no GSTIN. Clear it or set the country to India.', 'gstin')
    pincode = js_trim(js_str(coalesce(bg('pincode'), ex('pincode'), '')))
    if india and pincode and not re.search(r'^[0-9]{6}\Z', pincode):
        err(422, 'VAL_PIN', 'PIN code has 6 digits.', 'pincode')
    pan = js_upper(js_trim(js_str(coalesce(bg('pan'), ex('pan'), gstin[2:12] if gstin else ''))))
    if pan and not PAN_RE.search(pan):
        err(422, 'VAL_PAN', 'PAN is 10 characters, e.g. AAKCS4821M.', 'pan')
    if pan and gstin and gstin[2:12] != pan:
        err(422, 'VAL_PAN', f'PAN does not match the PAN inside the GSTIN ({gstin[2:12]}).', 'pan')
    out: dict[str, Any] = {
        'city': js_trim(js_str(coalesce(bg('city'), ex('city'), ''))),
        'state': js_or(state_in, state_name(state_code)) if india else state_in,
        'state_code': state_code,
        'pincode': pincode,
        'pan': pan,
        'name': name,
        'gstin': gstin,
        'email': email,
        'payment_terms_days': terms,
        'owner_id': owner_id,
        'industry': js_trim(js_str(coalesce(bg('industry'), ex('industry'), ''))),
        'country': country,
        'billing_address': js_trim(js_str(coalesce(bg('billing_address'), ex('billing_address'), ''))),
        'primary_contact': js_trim(js_str(coalesce(bg('primary_contact'), ex('primary_contact'), ''))),
        'phone': js_trim(js_str(coalesce(bg('phone'), ex('phone'), ''))),
        'notes': js_trim(js_str(coalesce(bg('notes'), ex('notes'), ''))),
    }
    if js_truthy(status):
        if status not in ('prospect', 'active', 'dormant', 'churned'):
            err(422, 'VAL_STATUS', 'Unknown status.', 'status')
        out['status'] = status
        out['status_manual'] = status == 'churned'
    return out


def _validate_contract(ctx: Ctx, body: Any, existing: dict | None = None) -> dict[str, Any]:
    def ex(k: str) -> Any:
        return existing.get(k, UNDEF) if existing else UNDEF

    def bg(k: str) -> Any:
        return _g(body, k)

    title = js_trim(js_str(coalesce(bg('title'), ex('title'), '')))
    if js_len(title) < 3:
        err(422, 'VAL_REQUIRED', 'Give the contract a title.', 'title')
    kind = js_str(coalesce(bg('kind'), ex('kind'), ''))
    if kind not in CONTRACT_KIND:
        err(422, 'VAL_REQUIRED', 'Choose the contract type.', 'kind')
    value = js_number(coalesce(bg('value_paise'), ex('value_paise'), 0))
    if not is_finite(value) or value < 0 or js_round(value) != value:
        err(422, 'VAL_AMOUNT', 'Contract value must be zero or more.', 'value_paise')
    start_on = js_str(coalesce(bg('start_on'), ex('start_on'), ''))
    end_on = js_str(coalesce(bg('end_on'), ex('end_on'), ''))
    if not _YMD_RE.search(start_on):
        err(422, 'VAL_DATE', 'Start date is required.', 'start_on')
    if not _YMD_RE.search(end_on):
        err(422, 'VAL_DATE', 'End date is required.', 'end_on')
    if end_on < start_on:
        err(422, 'DATE_RANGE', 'The end date is before the start date.', 'end_on')
    br = bg('renewal_on')
    renewal_on = coalesce(ex('renewal_on'), None) if br is UNDEF else js_or(br, None)
    if js_truthy(renewal_on) and (renewal_on < start_on or renewal_on > add_days(end_on, 1)):
        err(422, 'DATE_RANGE', 'The renewal date must fall within the contract term.', 'renewal_on')
    if not js_truthy(renewal_on) and kind != 'nda':
        renewal_on = end_on
    bp = bg('project_id')
    project_id = coalesce(ex('project_id'), None) if bp is UNDEF else js_or(bp, None)
    if js_truthy(project_id):
        find(ctx, 'projects', project_id, 'Project')
    status = js_str(coalesce(bg('status'), ex('status'), 'active'))
    if status not in ('draft', 'active', 'terminated') and status != ex('status'):
        err(422, 'VAL_STATUS', 'A contract can be draft, active or terminated; expiry is calculated from the dates.', 'status')
    ba = bg('auto_renew')
    return {
        'title': title,
        'kind': kind,
        'value_paise': value,
        'start_on': start_on,
        'end_on': end_on,
        'renewal_on': renewal_on,
        'project_id': project_id,
        'auto_renew': coalesce(ex('auto_renew'), False) if ba is UNDEF else js_truthy(ba),
        'status': status,
        'notes': js_trim(js_str(coalesce(bg('notes'), ex('notes'), ''))),
    }


def _add_months_js(s: str, n: int) -> str:
    """const d = new Date(s); d.setMonth(d.getMonth() + n); d.toISOString().slice(0, 10) — in Asia/Kolkata (UTC
    midnight is 05:30 local on the same calendar day; no DST), so the calendar month moves with JS day overflow."""
    d = parse_iso(s)
    if d is None:
        raise ValueError('RangeError: Invalid time value')
    m0 = d.month - 1 + n
    y = d.year + m0 // 12
    first = date(y, m0 % 12 + 1, 1)
    return to_iso(first + timedelta(days=d.day - 1))


MANAGE_CLIENTS = 'Only admin and sales manage clients.'
MANAGE_CONTRACTS = 'Only admin and sales manage contracts.'


def _list(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'clients.manage', MANAGE_CLIENTS)
    m = _client_model(ctx)
    items = m['items']
    if js_truthy(req.q('mine')):
        items = [c for c in items if c.get('owner_id') == ctx.user_id]
    items = sorted(items, key=lambda c: (-c['billed_fy'], locale_key(c['name'])))
    return {
        'items': items,
        'totals': {
            'clients': len(items),
            'active': sum(1 for c in items if c['status'] == 'active'),
            'at_risk': sum(1 for c in items if c['health'] == 'at_risk'),
            'billed_fy': _sum(c['billed_fy'] for c in items),
            'outstanding': _sum(c['outstanding'] for c in items),
            'overdue': _sum(c['overdue'] for c in items),
            'renewals_60': len(m['renewals']),
            'renewal_value': _sum(c['value_paise'] for c in m['renewals']),
        },
        'renewals': m['renewals'],
        'can_edit': True,
    }


def _detail(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'clients.manage', MANAGE_CLIENTS)
    cid = req.params['id']
    find(ctx, 'clients', cid, 'Client')
    m = _client_model(ctx)
    c = next(x for x in m['items'] if x['id'] == cid)
    key = _lc(c['name'])
    ps = [p for p in rows(ctx, 'projects') if _lc(p['client']) == key]
    ids = {p['id'] for p in ps}
    show_money = has(ctx, 'finance.view')
    deals = [d for d in rows(ctx, 'deals') if _lc(d['client']) == key]
    expenses = [
        e
        for e in rows(ctx, 'expense_claims')
        if js_truthy(e.get('billable_to_client')) and js_truthy(e.get('project_id')) and e['project_id'] in ids and e['status'] in ('approved', 'paid')
    ]
    contacts: list[dict] = []
    if js_truthy(c.get('primary_contact')):
        contacts.append({'name': c['primary_contact'], 'role': 'Primary contact', 'email': c.get('email', UNDEF), 'phone': c.get('phone', UNDEF), 'source': 'Client master'})
    for d in deals:
        if js_truthy(d.get('contact')) and not any(_lc(x['name']) == _lc(d['contact']) for x in contacts):
            contacts.append({'name': d['contact'], 'role': f'Deal · {js_str(d["title"])}', 'email': d.get('email', UNDEF), 'phone': d.get('phone', UNDEF), 'source': 'Pipeline'})
    contract_ids = {x['id'] for x in rows(ctx, 'contracts') if x['client_id'] == c['id']}
    see_value = show_money or has_any(ctx, 'projects.edit', 'margins.view')
    pmap = m['pm']
    invs = [i for i in rows(ctx, 'invoices') if i['project_id'] in ids and i.get('status') != 'draft' and i.get('status') != 'cancelled']
    acts = [
        a
        for a in rows(ctx, 'activity_log')
        if (a.get('entity') == 'client' and a.get('entity_id') == c['id']) or (a.get('entity') == 'contract' and a.get('entity_id') in contract_ids)
    ]
    return {
        'client': c,
        'projects': [
            {
                'id': p['id'],
                'code': p['code'],
                'name': p['name'],
                'status': p['status'],
                'billing_model': p['billing_model'],
                'contract_value_paise': p.get('contract_value_paise', UNDEF) if see_value else None,
                'health': project_metrics(ctx, p)['health'],
                'pm_name': user_name(ctx, p.get('pm_id')),
                'end_on': p.get('end_on', UNDEF),
            }
            for p in ps
        ],
        'deals': sorted(
            (
                {
                    'id': d['id'],
                    'title': d['title'],
                    'stage': d['stage'],
                    'value_paise': deal_value(d),
                    'owner_name': user_name(ctx, d.get('owner_id')),
                    'expected_close': d['expected_close'],
                    'lost_reason': d.get('lost_reason', UNDEF),
                }
                for d in deals
            ),
            key=lambda x: locale_key(x['expected_close']),
            reverse=True,
        ),
        'invoices': sorted(
            (
                {
                    'id': i['id'],
                    'number': i['number'],
                    'project_code': coalesce(pmap[i['project_id']]['code'] if i['project_id'] in pmap else UNDEF, ''),
                    'invoiced_on': i['invoiced_on'],
                    'due_on': i['due_on'],
                    'amount_paise': i['amount_paise'],
                    'paid_paise': i['paid_amount_paise'],
                    'outstanding_paise': i['amount_paise'] - i['paid_amount_paise'],
                    'days_overdue': js_max(0, diff_days(ctx.today, i['due_on'])) if i['amount_paise'] > i['paid_amount_paise'] else 0,
                }
                for i in invs
            ),
            key=lambda x: locale_key(x['invoiced_on']),
            reverse=True,
        ),
        'other_income': sorted(
            (
                {'date': l['date'], 'description': l['description'], 'amount_paise': l['amount_paise'], 'status': l.get('status', UNDEF)}
                for l in rows(ctx, 'ledger_entries')  # noqa: E741
                if l['kind'] == 'income' and not js_truthy(l.get('invoice_id')) and l['date'] <= ctx.today and _matches_client(c['name'], l['description'])
            ),
            key=lambda x: locale_key(x['date']),
            reverse=True,
        ),
        'contracts': sorted(
            (_contract_row(ctx, x, m['cmap'], pmap) for x in rows(ctx, 'contracts') if x['client_id'] == c['id']), key=lambda x: locale_key(x['end_on']), reverse=True
        ),
        'leakage': [l for l in leakage_rows(ctx) if _lc(l['client']) == key],  # noqa: E741
        'rebill': [
            {
                'id': e['id'],
                'date': e['spent_on'],
                'person': user_name(ctx, e['user_id']),
                'description': e['description'],
                'amount_paise': e['amount_paise'],
                'status': e['status'],
                'project_code': pmap[e['project_id']]['code'] if e['project_id'] in pmap else None,
            }
            for e in expenses
        ],
        'rebill_total': _sum(e['amount_paise'] for e in expenses),
        'contacts': contacts,
        'activity': [
            {'at': a['at'], 'actor': user_name(ctx, a.get('actor_id')), 'summary': a['summary']}
            for a in sorted(acts, key=lambda a: locale_key(a['at']), reverse=True)[:20]
        ],
    }


def _create(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'clients.manage', MANAGE_CLIENTS)
    v = _validate_client(ctx, req.body)
    row = insert(ctx, 'clients', {'status': 'prospect', 'since': None, 'health': None, **v})
    log_activity(ctx, 'client', row['id'], 'created', f'Added client {js_str(row["name"])}')
    return row


def _patch(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'clients.manage', MANAGE_CLIENTS)
    cur = find(ctx, 'clients', req.params['id'], 'Client')
    v = _validate_client(ctx, req.body, cur)
    if v['name'] and _lc(v['name']) != _lc(cur['name']):
        # keep projects and deals pointing at the renamed client
        old = _lc(cur['name'])
        for p in list(rows(ctx, 'projects')):
            if _lc(p['client']) == old:
                _patch_quiet('projects', p, {'client': v['name']})
        for d in list(rows(ctx, 'deals')):
            if _lc(d['client']) == old:
                _patch_quiet('deals', d, {'client': v['name']})
    if js_truthy(v.get('status')) and v['status'] != 'churned':
        v['status_manual'] = False
    row = update(ctx, 'clients', cur['id'], v, req.if_match)
    bs = _g(req.body, 'status')
    log_activity(ctx, 'client', row['id'], 'edited', f'Updated client {js_str(row["name"])}{" · status " + js_str(bs) if js_truthy(bs) else ""}')
    return row


def _create_contract(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'clients.manage', MANAGE_CONTRACTS)
    c = find(ctx, 'clients', req.params['id'], 'Client')
    v = _validate_contract(ctx, body)
    f = _g(body, 'file')
    file_id = None
    if js_truthy(f):
        if not js_truthy(_g(f, 'name')) or not js_truthy(_g(f, 'data_url')):
            err(422, 'FILE_REJECTED', 'The file could not be read. Try again.', 'file')
        sb = _g(f, 'size_bytes')
        if not nullish(sb) and js_number(sb) > 10 * 1024 * 1024:
            err(422, 'FILE_REJECTED', 'The file is larger than 10 MB.', 'file')
        file_id = insert(
            ctx, 'files', {'name': f['name'], 'mime': js_or(_g(f, 'mime'), 'application/pdf'), 'size_bytes': sb, 'entity': 'contract', 'entity_id': '', 'data_url': f['data_url']}
        )['id']
    row = insert(ctx, 'contracts', {'client_id': c['id'], 'file_id': file_id, **v, 'reminded_on': None})
    if file_id:
        fr = next((x for x in rows(ctx, 'files') if x['id'] == file_id), None)
        if fr is not None:
            patch('files', fr, {'entity_id': row['id']})
    log_activity(
        ctx,
        'contract',
        row['id'],
        'created',
        f'Added {js_str(CONTRACT_KIND.get(row["kind"], UNDEF))} “{js_str(row["title"])}” for {js_str(c["name"])} · {fmt_money(row["value_paise"])}',
    )
    return row


def _patch_contract(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'clients.manage', MANAGE_CONTRACTS)
    cur = find(ctx, 'contracts', req.params['id'], 'Contract')
    v = _validate_contract(ctx, body, cur)
    changes: dict[str, Any] = {**v}
    if v['end_on'] != cur.get('end_on', UNDEF) or v['renewal_on'] != cur.get('renewal_on', UNDEF):
        changes['reminded_on'] = None
    f = _g(body, 'file')
    if js_truthy(f):
        changes['file_id'] = insert(
            ctx,
            'files',
            {'name': _g(f, 'name'), 'mime': js_or(_g(f, 'mime'), 'application/pdf'), 'size_bytes': _g(f, 'size_bytes'), 'entity': 'contract', 'entity_id': cur['id'], 'data_url': _g(f, 'data_url')},
        )['id']
    row = update(ctx, 'contracts', cur['id'], changes, req.if_match)
    term = ' — terminated' if v['status'] == 'terminated' and cur.get('status') != 'terminated' else ''
    log_activity(ctx, 'contract', row['id'], 'edited', f'Updated {js_str(CONTRACT_KIND.get(row["kind"], UNDEF))} “{js_str(row["title"])}”{term}')
    return row


def _renew(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'clients.manage', MANAGE_CONTRACTS)
    cur = find(ctx, 'contracts', req.params['id'], 'Contract')
    if cur.get('status') in ('renewed', 'terminated'):
        err(409, 'INVALID_STATE', f'This contract is already {js_str(cur["status"])}.')
    months = js_number(coalesce(_g(body, 'months'), 12))
    if not is_integer(months) or months < 1 or months > 60:
        err(422, 'VAL_RANGE', 'Renew for 1 to 60 months.', 'months')
    bv = _g(body, 'value_paise')
    value = cur['value_paise'] if bv is UNDEF else js_number(bv)
    if not is_finite(value) or value < 0:
        err(422, 'VAL_AMOUNT', 'Contract value must be zero or more.', 'value_paise')
    start = add_days(cur['end_on'], 1)
    end = add_days(_add_months_js(start, int(months)), -1)
    update(ctx, 'contracts', cur['id'], {'status': 'renewed'})
    row = insert(
        ctx,
        'contracts',
        {
            'client_id': cur['client_id'],
            'project_id': cur.get('project_id', UNDEF),
            'title': cur['title'],
            'kind': cur['kind'],
            'value_paise': value,
            'start_on': start,
            'end_on': end,
            'renewal_on': end,
            'auto_renew': cur.get('auto_renew', UNDEF),
            'status': 'active',
            'file_id': None,
            'notes': js_or(js_trim(js_str(coalesce(_g(body, 'notes'), ''))), f'Renewal of the {js_str(cur["start_on"])} – {js_str(cur["end_on"])} term.'),
            'reminded_on': None,
            'renewed_from': cur['id'],
        },
    )
    cv = cur['value_paise']
    change = (value - cv) / cv if js_truthy(cv) else None
    chg = f' ({"+" if change > 0 else ""}{to_fixed(change * 100, 1)}%)' if js_truthy(change) else ''  # type: ignore[operator]
    log_activity(
        ctx,
        'contract',
        row['id'],
        'renewed',
        f'Renewed {js_str(CONTRACT_KIND.get(cur["kind"], UNDEF))} “{js_str(cur["title"])}” to {end} · {fmt_money(value)}{chg}',
    )
    return row


def _delete_contract(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'clients.manage', MANAGE_CONTRACTS)
    cur = find(ctx, 'contracts', req.params['id'], 'Contract')
    if cur.get('status') != 'draft':
        err(409, 'CONTRACT_LOCKED', 'Only draft contracts can be deleted. Terminate an active contract instead.')
    remove(ctx, 'contracts', cur['id'], req.if_match)
    log_activity(ctx, 'contract', cur['id'], 'deleted', f'Deleted draft contract “{js_str(cur["title"])}”')
    return {'ok': True}


def _lite(req: Req) -> Any:
    ctx = req.ctx
    _sync_clients(ctx)
    return sorted(({'id': c['id'], 'name': c['name']} for c in rows(ctx, 'clients')), key=lambda x: locale_key(x['name']))


def register(r: Any) -> None:
    provide_clients_helpers({'contract_renewals': contract_renewals})
    r.get('/clients', _list)
    r.get('/clients/:id', _detail)
    r.post('/clients', _create)
    r.patch('/clients/:id', _patch)
    r.post('/clients/:id/contracts', _create_contract)
    r.patch('/contracts/:id', _patch_contract)
    r.post('/contracts/:id/renew', _renew)
    r.delete('/contracts/:id', _delete_contract)
    r.get('/clients-lite', _lite)
