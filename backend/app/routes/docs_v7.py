"""Port of src/mock/routes/docsV7.ts — the v7 Documents hub: letter templates, issuing (single + bulk, drafts, revoke,
re-issue, acknowledgement), payslips from finalised payroll runs, and employee documents with HR verification.

Permissions (server-enforced):
  templates + issuing + the issued register ........ documents.templates
  employee documents of others, verify / reject ...... documents.employee_files
  own issued documents (not drafts), own uploads ...... everyone (self only)
  payslip generation ................................. payroll.run or documents.templates

Exported helpers: can_see_issued (GET /search), suggest_vars, generate_payslips (payroll.py), issue_joining_letter
(people.py).
"""

from __future__ import annotations

import re
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    err,
    find,
    get_db,
    has,
    has_any,
    insert,
    log_activity,
    notify,
    persist,
    remove,
    require_action,
    require_any,
    rows,
    scope,
    set_rows,
    update,
    user_name,
    users_with,
    warn,
)
from app.domain.calendar import add_days, month_range, parse_iso
from app.domain.costing import rate_on
from app.domain.money import fmt_money
from app.engine.doc_markup import (
    CATEGORY_LABEL,
    STANDARD_KEYS,
    TEMPLATE_CATEGORIES,
    _obj_get,
    js_object,
    known_placeholders,
    placeholders_in,
    process_placeholders,
    rupees_in_words,
)
from app.engine.docgen import announce_issued, ensure_doc_defaults, issue_document, long_date, render_body, standard_vars
from app.frozen import FrozenError, thaw
from app.js import (
    UNDEF,
    coalesce,
    fmt_date_en_in,
    js_div,
    js_join,
    js_len,
    js_lower,
    js_max,
    js_mod,
    js_number,
    js_or,
    js_round,
    js_slice,
    js_sort,
    js_spread,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    pad_start,
    prop,
    to_fixed,
)

CATS: list[str] = list(TEMPLATE_CATEGORIES)
KINDS = ['id_proof', 'address_proof', 'pan', 'aadhaar', 'passport', 'education', 'experience', 'bank', 'photo', 'other']
MANDATORY = ['pan', 'aadhaar', 'education', 'bank']
EXT_MIME = {
    'pdf': 'application/pdf',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
MAX_EMP_BYTES = 5 * 1024 * 1024
DAY = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{1,39}\Z')
_WORD_KEY_RE = re.compile(r'^[A-Za-z0-9_]+\Z')
_DATE_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')


# ---------------------------------------------------------------------------------------------------------------------
# small JS helpers
# ---------------------------------------------------------------------------------------------------------------------
def _g(o: Any, k: str) -> Any:
    """o.k for a JSON value (TypeError on null/undefined like JS; non-objects have no such property)"""
    if o is None or o is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(o)} (reading '{k}')")
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    return UNDEF


def _q(o: Any, k: str) -> Any:
    """o?.k"""
    if o is None or o is UNDEF:
        return UNDEF
    return _g(o, k)


def _s(v: Any) -> str:
    """String(v ?? '').trim()"""
    return js_trim(js_str(coalesce(v, '')))


def _u16slice(s: str, a: int, b: int) -> str:
    if all(ord(c) <= 0xFFFF for c in s):
        return js_slice(s, a, b)
    units = s.encode('utf-16-le', 'surrogatepass')
    return units[a * 2 : b * 2].decode('utf-16-le', 'surrogatepass')


def _gt0(v: Any) -> bool:
    """v > 0 with JS coercion (null -> 0)"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _first(lst: Any, pred: Any) -> Any:
    return next((x for x in lst if pred(x)), None)


def _day(i: Any) -> str:
    return DAY[i] if isinstance(i, int) and 0 <= i < 7 else 'undefined'


def _month_label(ym: str) -> str:
    return fmt_date_en_in(parse_iso(f'{ym}-01'), month='long', year='numeric')


def _short_date(iso: str) -> str:
    return fmt_date_en_in(parse_iso(iso), day='numeric', month='short', year='numeric')


def _hm(t: str) -> str:
    parts = [js_number(x) for x in js_str(t).split(':')]
    h = parts[0]
    m = parts[1] if len(parts) > 1 else UNDEF
    hh = js_mod(h + 11, 12) + 1
    mm = m if js_truthy(m) else 0
    return f'{js_str(hh)}:{pad_start(js_str(mm), 2, "0")} {"AM" if h < 12 else "PM"}'


def _day_text(days: list) -> str:
    contiguous = all(i == 0 or d == days[i - 1] + 1 for i, d in enumerate(days))
    if contiguous and len(days) > 1:
        return f'{_day(days[0])} to {_day(days[-1])}'
    return ', '.join(_day(d) for d in days)


# ---------------------------------------------------------------------------------------------------------------------
# lazy defaults & demo payslips
# ---------------------------------------------------------------------------------------------------------------------
def _boot(ctx: Ctx) -> None:
    ensure_doc_defaults(ctx)
    _ensure_seed_payslips(ctx)


def _ctx_as(ctx: Ctx, user: dict) -> Ctx:
    """{ ...ctx, user, warnings: [] }"""
    return Ctx(user, ctx.tenant, ctx.role, ctx.real_role, ctx.view_as, ctx.role_key, ctx.perms, ctx.is_super, ctx.today)


def _ensure_seed_payslips(ctx: Ctx) -> None:
    """The first time the documents area is used, issue payslips for the last finalised payroll run (the demo seed)."""
    s = scope()
    tid = ctx.tenant_id
    if any(x.get('tenant_id') == tid and x.get('scope') == 'docs_v7_payslips' for x in s.view('number_sequences')):
        return
    s.append('number_sequences', {'tenant_id': tid, 'scope': 'docs_v7_payslips', 'next_value': 1})
    finals = sorted((r for r in rows(ctx, 'payroll_runs') if r.get('status') == 'final'), key=lambda r: locale_key(r['month']), reverse=True)
    run = finals[0] if finals else None
    if not run or not any(t.get('key') == 'payslip' and js_truthy(t.get('active')) for t in rows(ctx, 'doc_templates')):
        return
    by = _first(rows(ctx, 'users'), lambda u: u['id'] == run.get('finalised_by')) or ctx.user
    fa = run.get('finalised_at')
    try:
        generate_payslips(_ctx_as(ctx, by), run['id'], {'notify': False, 'issued_on': fa[:10] if isinstance(fa, str) else ctx.today})
    except FrozenError:
        raise
    except Exception:  # noqa: BLE001 — demo seed only (the TS swallows everything)
        pass


# ---------------------------------------------------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------------------------------------------------
def _letterhead_of(ctx: Ctx) -> dict[str, Any]:
    t = _first(get_db().tenants, lambda x: x.get('id') == ctx.tenant_id)
    o = _q(t, 'org')
    state_pin = ' '.join(js_str(v) for v in (_q(o, 'state'), _q(o, 'pincode')) if js_truthy(v))
    address = ', '.join(js_str(v) for v in (_q(o, 'registered_address'), _q(o, 'city'), state_pin) if js_truthy(v))
    return {
        'legal_name': js_or(_q(o, 'legal_name'), _q(t, 'name'), ''),
        'brand_name': js_or(_q(o, 'brand_name'), _q(t, 'name'), ''),
        'address': address,
        'cin': coalesce(_q(o, 'cin'), ''),
        'gstin': coalesce(_q(o, 'gstin'), ''),
        'email': coalesce(_q(o, 'email'), ''),
        'phone': coalesce(_q(o, 'phone'), ''),
        'website': coalesce(_q(o, 'website'), ''),
        'logo': 'seereon' if _q(t, 'slug') == 'seereon' else coalesce(_q(t, 'logo_data_url'), None),
    }


def _template_row(ctx: Ctx, t: dict, issued: list | None = None) -> dict[str, Any]:
    if issued is None:
        issued = rows(ctx, 'issued_documents')
    mine = [d for d in issued if d.get('template_id') == t['id'] and d.get('status') != 'draft']
    days = js_sort([coalesce(d.get('issued_on', UNDEF), '') for d in mine])
    last = days[-1] if days and js_truthy(days[-1]) else None
    return {
        **t,
        'placeholders': placeholders_in(t['body']),
        'issued_count': len(mine),
        'last_used_on': last,
        'process_placeholders': [p['key'] for p in process_placeholders(t['body'], t['category'], coalesce(t.get('custom_placeholders', UNDEF), []))],
    }


def _issued_row(ctx: Ctx, d: dict, users: dict | None = None, tpls: dict | None = None) -> dict[str, Any]:
    if users is None:
        users = {u['id']: u for u in rows(ctx, 'users')}
    if tpls is None:
        tpls = {t['id']: t for t in rows(ctx, 'doc_templates')}
    u = users.get(d.get('employee_id'))
    tid = d.get('template_id')
    return {
        'id': d['id'],
        'version': prop(d, 'version'),
        'title': prop(d, 'title'),
        'category': prop(d, 'category'),
        'template_id': prop(d, 'template_id'),
        'template_name': coalesce(prop(tpls.get(tid), 'name'), None) if js_truthy(tid) else None,
        'employee_id': prop(d, 'employee_id'),
        'employee_name': coalesce(prop(u, 'name'), '—'),
        'employee_code': coalesce(prop(u, 'employee_code'), ''),
        'ref_no': coalesce(prop(d, 'ref_no'), ''),
        'period': prop(d, 'period'),
        'status': prop(d, 'status'),
        'issued_on': prop(d, 'issued_on'),
        'acknowledged_on': prop(d, 'acknowledged_on'),
        'issued_by': prop(d, 'issued_by'),
        'source': prop(d, 'source'),
        'revoked_on': coalesce(prop(d, 'revoked_on'), None),
        'created_at': prop(d, 'created_at'),
    }


def can_see_issued(ctx: Ctx, d: dict) -> bool:
    if has(ctx, 'documents.templates'):
        return True
    if d.get('status') == 'draft':
        return False
    return d.get('employee_id') == ctx.user_id or has(ctx, 'documents.employee_files')


def _load_issued(ctx: Ctx, id_: str) -> dict:
    d = find(ctx, 'issued_documents', id_, 'Document')
    if not can_see_issued(ctx, d):
        err(404, 'NOT_FOUND', 'Document not found.')
    return d


def _issued_detail(ctx: Ctx, d: dict) -> dict[str, Any]:
    u = _first(rows(ctx, 'users'), lambda x: x['id'] == d.get('employee_id'))
    tid = d.get('template_id')
    tpl = _first(rows(ctx, 'doc_templates'), lambda t: t['id'] == tid) if js_truthy(tid) else None

    def ref(id_: Any) -> Any:
        x = _first(rows(ctx, 'issued_documents'), lambda y: y['id'] == id_) if js_truthy(id_) else None
        return {'id': x['id'], 'ref_no': coalesce(prop(x, 'ref_no'), '')} if x and can_see_issued(ctx, x) else None

    return {
        'doc': {
            **d,
            'employee_name': coalesce(prop(u, 'name'), '—'),
            'employee_code': coalesce(prop(u, 'employee_code'), ''),
            'template_name': coalesce(prop(tpl, 'name'), None),
        },
        'letterhead': None if d.get('letterhead', UNDEF) is False else _letterhead_of(ctx),
        'can_manage': has(ctx, 'documents.templates'),
        'can_ack': d.get('employee_id') == ctx.user_id and d.get('status') == 'issued',
        'replaces': ref(d.get('replaces_id')),
        'replaced_by': ref(d.get('replaced_by_id')),
        'process_fields': [
            {'key': p['key'], 'label': p['label'], 'type': p.get('type', UNDEF)}
            for p in process_placeholders(tpl['body'], tpl['category'], coalesce(tpl.get('custom_placeholders', UNDEF), []))
        ]
        if tpl
        else [],
    }


def _expiry_state(d: dict, today: str) -> str | None:
    exp = d.get('expires_on')
    if not js_truthy(exp):
        return None
    if exp < today:
        return 'expired'
    if exp <= add_days(today, 60):
        return 'expiring'
    return 'ok'


def _emp_doc_row(ctx: Ctx, d: dict) -> dict[str, Any]:
    vb = d.get('verified_by')
    return {
        **d,
        'uploaded_by_name': user_name(ctx, d.get('uploaded_by')),
        'verified_by_name': user_name(ctx, vb) if js_truthy(vb) else None,
        'expiry_state': _expiry_state(d, ctx.today),
    }


def _missing_of(docs: list) -> list[str]:
    return [k for k in MANDATORY if not any(d.get('kind') == k and d.get('status') != 'rejected' for d in docs)]


def _master_name(ctx: Ctx, table: str, id_: Any) -> Any:
    if not js_truthy(id_):
        return ''
    x = _first(rows(ctx, table), lambda r: r['id'] == id_)
    return coalesce(prop(x, 'name'), '')


# ---------------------------------------------------------------------------------------------------------------------
# template validation
# ---------------------------------------------------------------------------------------------------------------------
def _read_template(ctx: Ctx, b: Any, cur: dict | None = None) -> dict[str, Any]:
    bn = _g(b, 'name')
    name = _s(bn) if bn is not UNDEF else _g(cur, 'name')
    if js_len(name) < 3 or js_len(name) > 80:
        err(422, 'VALIDATION', 'Give the template a name of 3 to 80 characters.', 'name')
    low = js_lower(name)
    cur_id = _q(cur, 'id')
    if any(t['id'] != cur_id and js_lower(t['name']) == low for t in rows(ctx, 'doc_templates')):
        err(409, 'DUPLICATE', f'A template called “{name}” already exists. Choose another name.', 'name')
    bc = _g(b, 'category')
    category = js_str(bc) if bc is not UNDEF else _g(cur, 'category')
    if category not in CATS:
        err(422, 'VALIDATION', 'Choose a category.', 'category')
    bb = _g(b, 'body')
    body = js_trim(js_str(coalesce(bb, '')).replace('\r\n', '\n')) if bb is not UNDEF else _g(cur, 'body')
    if js_len(body) < 20:
        err(422, 'VALIDATION', 'Write the letter: the body needs at least a few sentences.', 'body')
    if js_len(body) > 20000:
        err(422, 'VALIDATION', 'Keep the body under 20,000 characters.', 'body')
    bcp = _g(b, 'custom_placeholders')
    if bcp is not UNDEF:
        custom: list = []
        for x in bcp if isinstance(bcp, list) else []:
            c = js_lower(js_trim(js_str(x)))
            if c and c not in custom:
                custom.append(c)
    else:
        custom = list(coalesce(_q(cur, 'custom_placeholders'), []))
    for c in custom:
        if not _KEY_RE.match(c):
            err(
                422,
                'VALIDATION',
                f'“{c}” isn’t a valid placeholder name. Use lowercase letters, digits and underscores, e.g. bonus_amount.',
                'custom_placeholders',
            )
    known = known_placeholders(category, custom)
    unknown = [p for p in placeholders_in(body) if p not in known]
    if unknown:
        err(
            422,
            'UNKNOWN_PLACEHOLDER',
            f'{", ".join("{{" + u + "}}" for u in unknown)} {"isn’t a known placeholder" if len(unknown) == 1 else "aren’t known placeholders"} for {CATEGORY_LABEL[category]} templates. Pick one from the placeholder list, or add it as a custom field.',
            'body',
        )
    if category == 'payslip' and 'net_pay' not in placeholders_in(body):
        err(422, 'VALIDATION', 'A payslip template must show {{net_pay}}.', 'body')
    bd = _g(b, 'description')
    bl = _g(b, 'letterhead')
    ba = _g(b, 'active')
    return {
        'name': name,
        'category': category,
        'body': body,
        'custom_placeholders': custom,
        'description': _u16slice(_s(bd), 0, 300) if bd is not UNDEF else coalesce(_q(cur, 'description'), ''),
        'letterhead': js_truthy(bl) if bl is not UNDEF else coalesce(_q(cur, 'letterhead'), True),
        'active': js_truthy(ba) if ba is not UNDEF else coalesce(_q(cur, 'active'), True),
    }


def _slug(ctx: Ctx, name: str) -> str:
    base = re.sub(r'[^a-z0-9]+', '_', js_lower(name))
    base = re.sub(r'^_|_\Z', '', base)[:40] or 'template'
    keys = {t.get('key') for t in rows(ctx, 'doc_templates')}
    k = base
    i = 2
    while k in keys:
        k = f'{base}_{i}'
        i += 1
    return k


# ---------------------------------------------------------------------------------------------------------------------
# suggestions (defaults from the employee's records)
# ---------------------------------------------------------------------------------------------------------------------
def suggest_vars(ctx: Ctx, user_id: str) -> dict[str, str]:
    """Process values the system can fill from records: CTC from cost rates (only for viewers who may see salaries),
    designation, probation dates from the joining date, shift timings, exit date from the offboarding checklist."""
    u = _first(rows(ctx, 'users'), lambda x: x['id'] == user_id)
    if not u:
        return {}
    out: dict[str, Any] = {}
    desig = js_or(_master_name(ctx, 'designations', u.get('designation_id')), prop(u, 'title'))
    out['offer_designation'] = desig
    out['new_designation'] = desig
    out['last_designation'] = desig
    out['proposed_joining_date'] = long_date(u.get('joined_on'))
    out['probation_months'] = '6'
    out['notice_period_days'] = '60'
    end = add_days(u['joined_on'], 182)
    out['probation_end_date'] = long_date(add_days(end, -1))
    out['confirmation_date'] = long_date(end)
    out['offer_valid_until'] = long_date(add_days(ctx.today, 7))
    out['effective_date'] = long_date(ctx.today)
    out['term_years'] = '3'
    t = _first(get_db().tenants, lambda x: x.get('id') == ctx.tenant_id)
    org = _q(t, 'org')
    if js_truthy(_q(org, 'city')):
        out['governing_city'] = org['city']
    shifts = rows(ctx, 'shifts')
    shift = _first(shifts, lambda x: x['id'] == u.get('shift_id')) or _first(shifts, lambda x: js_truthy(x.get('is_default')) and js_truthy(x.get('active')))
    if shift:
        out['reporting_time'] = _hm(shift['start_time'])
        days = js_sort(shift['working_days'])
        out['working_hours'] = f'{_hm(shift["start_time"])} to {_hm(shift["end_time"])}, {_day_text(days)}'
    ex = _first(rows(ctx, 'lifecycle_checklists'), lambda c: c.get('kind') == 'offboarding' and c.get('user_id') == u['id'] and js_truthy(c.get('last_day')))
    if ex and js_truthy(ex.get('last_day')):
        out['last_working_day'] = long_date(ex['last_day'])
    if has(ctx, 'cost_rates.view'):
        rates = sorted((r for r in rows(ctx, 'cost_rates') if r.get('user_id') == u['id']), key=lambda r: locale_key(r['effective_from']))
        cur = rate_on(rates, u['id'], ctx.today)
        if cur:
            out['ctc_monthly'] = fmt_money(cur['monthly_ctc_paise'])
            out['ctc_annual'] = fmt_money(cur['monthly_ctc_paise'] * 12)
            earlier = [r for r in rates if r['effective_from'] < cur['effective_from']]
            prev = earlier[-1] if earlier else None
            out['new_ctc'] = out['ctc_annual']
            if prev:
                out['old_ctc'] = fmt_money(prev['monthly_ctc_paise'] * 12)
                out['hike_pct'] = to_fixed(js_div(cur['monthly_ctc_paise'] - prev['monthly_ctc_paise'], prev['monthly_ctc_paise']) * 100, 1)
                out['effective_from'] = long_date(cur['effective_from'])
                ef = cur['effective_from']
                fy = js_number(ef[0:4]) - (1 if js_number(ef[5:7]) < 4 else 0)
                out['appraisal_period'] = f'FY {js_str(fy - 1)}-{js_str(fy)[2:]}'
    return out


# ---------------------------------------------------------------------------------------------------------------------
# issuing
# ---------------------------------------------------------------------------------------------------------------------
def _clean_vars(v: Any) -> dict[str, str]:
    if not (js_truthy(v) and isinstance(v, (dict, list))):
        return {}
    out = {}
    for k, x in js_spread(v).items():
        if _WORD_KEY_RE.match(k) and k not in STANDARD_KEYS:
            out[k] = _u16slice(_s(x), 0, 2000)
    return js_object(out)


def _pick(sug: dict[str, str], t: dict) -> dict[str, str]:
    """Only the suggestions this template actually uses."""
    used = set(placeholders_in(t['body']))
    return {k: v for k, v in sug.items() if k in used}


def _read_issue(ctx: Ctx, t: dict, b: Any) -> dict[str, Any]:
    if not js_truthy(t.get('active')):
        err(409, 'TEMPLATE_INACTIVE', 'This template is switched off. Turn it on before issuing.')
    be = _g(b, 'employee_ids')
    ids: list[str] = []
    for x in be if isinstance(be, list) else []:
        s = js_str(x)
        if s not in ids:
            ids.append(s)
    if not ids:
        err(422, 'VALIDATION', 'Choose at least one employee.', 'employee_ids')
    if len(ids) > 100:
        err(422, 'VALIDATION', 'Issue to at most 100 employees at a time.', 'employee_ids')
    users = rows(ctx, 'users')
    people = []
    for id_ in ids:
        u = _first(users, lambda x, i=id_: x['id'] == i and not js_truthy(x.get('is_super_admin')))
        if not u:
            err(422, 'VALIDATION', 'One of the chosen employees no longer exists.', 'employee_ids')
        people.append(u)
    shared = _clean_vars(_g(b, 'vars'))
    bp = _g(b, 'per_employee')
    per = js_spread(bp) if js_truthy(bp) and isinstance(bp, (dict, list)) else {}
    fields = process_placeholders(t['body'], t['category'], coalesce(t.get('custom_placeholders', UNDEF), []))
    return {'people': people, 'shared': shared, 'per': per, 'fields': fields}


def _vars_for(shared: dict, per: dict, id_: str, suggested: dict | None = None) -> dict[str, str]:
    own = _clean_vars(_obj_get(per, id_) if isinstance(per, dict) else UNDEF)
    out: dict[str, Any] = dict(suggested or {})
    for k, v in js_object({**shared, **own}).items():
        if v and k != '__proto__':  # out.__proto__ = string is ignored by JS
            out[k] = v
    return js_object(out)


# ---------------------------------------------------------------------------------------------------------------------
# payslips
# ---------------------------------------------------------------------------------------------------------------------
def _days(n: Any) -> str:
    return to_fixed(n, 1) if js_truthy(js_mod(n, 1)) else js_str(n)


def _m(p: Any) -> str:
    return fmt_money(p, decimals=True)


def generate_payslips(ctx: Ctx, run_id: str, opts: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.routes.payroll import payroll_run_lines  # payroll.py imports this module (cycle)

    opts = opts or {}
    ensure_doc_defaults(ctx)
    prl = payroll_run_lines(ctx, run_id)
    run, lines = prl['run'], prl['lines']
    if run.get('status') != 'final':
        err(409, 'PAY_NOT_FINAL', 'Finalise the payroll run first. Payslips are issued only from a final run.')
    tpls = rows(ctx, 'doc_templates')
    tpl = _first(tpls, lambda t: t.get('key') == 'payslip' and js_truthy(t.get('active'))) or _first(
        tpls, lambda t: t.get('category') == 'payslip' and js_truthy(t.get('active'))
    )
    if not tpl:
        err(422, 'TEMPLATE_MISSING', 'There is no active payslip template. Turn one on under Documents → Templates.')
    existing = [
        d
        for d in rows(ctx, 'issued_documents')
        if _g(d.get('source'), 'kind') == 'payroll' and _g(d.get('source'), 'ref_id') == run['id'] and d.get('status') != 'revoked'
    ]
    existing_emp = {d.get('employee_id') for d in existing}
    rng = month_range(run['month'])
    frm, to = rng['from'], rng['to']
    t = _first(get_db().tenants, lambda x: x.get('id') == ctx.tenant_id)
    org = _q(t, 'org')
    out: dict[str, Any] = {'created': 0, 'skipped_existing': 0, 'skipped_no_ctc': [], 'total': len(lines)}
    month_label = _month_label(run['month'])
    for ln in lines:
        if ln.get('user_id') in existing_emp:
            out['skipped_existing'] += 1
            continue
        if js_truthy(ln.get('no_rate')) or not js_truthy(ln.get('monthly_ctc_paise')):
            out['skipped_no_ctc'].append(ln['user']['name'])
            continue
        base = ln['base_paise']
        basic = js_round(base * 0.5)
        hra = js_round(base * 0.2)
        special = base - basic - hra
        pending = coalesce(ln.get('pending_days', UNDEF), 0)
        unapp = ln['unapproved_days'] + pending if js_truthy(ln.get('deduct_applied')) else 0
        total_days = ln['lwp_days'] + unapp
        lwp_ded = js_round((ln['deduction_paise'] * ln['lwp_days']) / total_days) if js_truthy(total_days) else 0
        unapp_ded = ln['deduction_paise'] - lwp_ded
        earnings = [
            f'| Basic (50% of fixed pay) | {_m(basic)} |',
            f'| House rent allowance (20% of fixed pay) | {_m(hra)} |',
            f'| Special allowance | {_m(special)} |',
        ]
        if js_truthy(ln.get('variable_paise')):
            earnings.append(f'| Sales variable pay | {_m(ln["variable_paise"])} |')
        deductions = [f'| Leave without pay ({_days(ln["lwp_days"])} days) | {_m(lwp_ded)} |']
        if js_truthy(ln.get('deduct_applied')):
            deductions.append(f'| Unapproved timesheet days ({_days(unapp)} days) | {_m(unapp_ded)} |')
        gross = base + ln['variable_paise']
        paid = js_max(0, ln['working_days'] - ln['lwp_days'] - unapp)
        fa = run.get('finalised_at')
        issue_document(
            ctx,
            {
                'template_id': tpl['id'],
                'employee_id': ln['user_id'],
                'period': run['month'],
                'title': f'Payslip — {month_label}',
                'source': {'kind': 'payroll', 'ref_id': run['id']},
                'notify': opts.get('notify', UNDEF),
                'issued_on': opts.get('issued_on', UNDEF),
                'vars': {
                    'month_label': month_label,
                    'pay_period': f'{_short_date(frm)} – {_short_date(to)}',
                    'working_days': _days(ln['working_days']),
                    'paid_days': _days(paid),
                    'lwp_days': _days(ln['lwp_days']),
                    'unapproved_days': _days(unapp),
                    'earnings_rows': '\n'.join(earnings),
                    'deductions_rows': '\n'.join(deductions),
                    'monthly_ctc': _m(ln['monthly_ctc_paise']),
                    'gross_earnings': _m(gross),
                    'total_deductions': _m(ln['deduction_paise']),
                    'variable_pay': _m(ln['variable_paise']),
                    'net_pay': _m(ln['net_paise']),
                    'net_pay_words': rupees_in_words(ln['net_paise']),
                    'bank_name': f'Bank transfer from {js_str(org["bank_name"])}' if js_truthy(_q(org, 'bank_name')) else 'Bank transfer',
                    'pay_date': long_date(fa[:10] if isinstance(fa, str) else ctx.today),
                },
            },
        )
        out['created'] += 1
    if out['created']:
        n = out['created']
        log_activity(ctx, 'payroll_run', run['id'], 'payslips', f'Issued {n} payslip{"" if n == 1 else "s"} for {month_label}')
    return out


# ---------------------------------------------------------------------------------------------------------------------
# onboarding
# ---------------------------------------------------------------------------------------------------------------------
def issue_joining_letter(ctx: Ctx, user_id: str, monthly_ctc_paise: Any) -> dict:
    """Joining letter for a new employee (Create employee → "Issue joining letter"). Vars come from their shift and CTC."""
    u = find(ctx, 'users', user_id, 'Employee')
    shifts = rows(ctx, 'shifts')
    shift = _first(shifts, lambda x: x['id'] == u.get('shift_id')) or _first(shifts, lambda x: js_truthy(x.get('is_default')) and js_truthy(x.get('active')))
    days = js_sort(shift['working_days']) if shift else [1, 2, 3, 4, 5]
    day_text = _day_text(days)
    ctc = js_truthy(monthly_ctc_paise)
    return issue_document(
        ctx,
        {
            'template_key': 'joining_letter',
            'employee_id': u['id'],
            'source': {'kind': 'onboarding', 'ref_id': u['id']},
            'vars': {
                'ctc_annual': fmt_money(monthly_ctc_paise * 12) if ctc else 'As per your offer letter',
                'ctc_monthly': fmt_money(monthly_ctc_paise) if ctc else 'As per your offer letter',
                'reporting_time': _hm(shift['start_time']) if shift else '9:30 AM',
                'working_hours': f'{_hm(shift["start_time"])} to {_hm(shift["end_time"])}, {day_text}' if shift else '9:30 AM to 6:30 PM, Monday to Friday',
                'probation_months': '6',
                'notice_period_days': '60',
            },
        },
    )


# ---------------------------------------------------------------------------------------------------------------------
# routes: templates
# ---------------------------------------------------------------------------------------------------------------------
def _cat_index(c: Any) -> int:
    return CATS.index(c) if c in CATS else -1


def list_templates(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Document templates are for HR.')
    _boot(ctx)
    issued = rows(ctx, 'issued_documents')
    out = [_template_row(ctx, t, issued) for t in rows(ctx, 'doc_templates')]
    return sorted(out, key=lambda t: (-js_number(t['active']), _cat_index(t['category']), locale_key(t['name'])))


def template_sample(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Document templates are for HR.')
    find(ctx, 'users', req.params['employeeId'], 'Employee')
    return {**standard_vars(ctx, req.params['employeeId']), 'ref_no': 'Assigned when issued'}


def template_suggest(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Document templates are for HR.')
    find(ctx, 'users', req.params['employeeId'], 'Employee')
    return suggest_vars(ctx, req.params['employeeId'])


def get_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Document templates are for HR.')
    _boot(ctx)
    return _template_row(ctx, find(ctx, 'doc_templates', req.params['id'], 'Template'))


def create_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can create document templates.')
    v = _read_template(ctx, req.body)
    t = insert(ctx, 'doc_templates', {'key': _slug(ctx, v['name']), **v, 'placeholders': placeholders_in(v['body']), 'updated_by': ctx.user_id})
    log_activity(ctx, 'doc_template', t['id'], 'created', f'Created the “{t["name"]}” template')
    return _template_row(ctx, t)


def patch_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can edit document templates.')
    cur = find(ctx, 'doc_templates', req.params['id'], 'Template')
    v = _read_template(ctx, req.body, cur)
    if cur.get('key') == 'payslip' and v['category'] != 'payslip':
        err(409, 'VALIDATION', 'The payroll payslip template must stay in the Payslip category.', 'category')
    t = update(ctx, 'doc_templates', cur['id'], {**v, 'placeholders': placeholders_in(v['body']), 'updated_by': ctx.user_id}, req.if_match)
    # the TS compares with cur.active AFTER update() changed cur in place (same row object): always 'Edited' (mirrored)
    what = ('Turned on' if v['active'] else 'Turned off') if v['active'] != t.get('active', UNDEF) else 'Edited'
    log_activity(ctx, 'doc_template', t['id'], 'updated', f'{what} the “{t["name"]}” template')
    return _template_row(ctx, t)


def duplicate_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can create document templates.')
    cur = find(ctx, 'doc_templates', req.params['id'], 'Template')
    names = {js_lower(t['name']) for t in rows(ctx, 'doc_templates')}
    name = f'{cur["name"]} (copy)'
    i = 2
    while js_lower(name) in names:
        name = f'{cur["name"]} (copy {i})'
        i += 1
    t = insert(
        ctx,
        'doc_templates',
        {
            'key': _slug(ctx, name),
            'name': name,
            'category': cur['category'],
            'body': cur['body'],
            'description': coalesce(cur.get('description', UNDEF), ''),
            'placeholders': placeholders_in(cur['body']),
            'custom_placeholders': list(coalesce(cur.get('custom_placeholders', UNDEF), [])),
            'letterhead': prop(cur, 'letterhead'),
            'active': False,
            'updated_by': ctx.user_id,
        },
    )
    log_activity(ctx, 'doc_template', t['id'], 'created', f'Duplicated “{cur["name"]}” as “{name}”')
    return _template_row(ctx, t)


def delete_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can delete document templates.')
    t = find(ctx, 'doc_templates', req.params['id'], 'Template')
    if any(d.get('template_id') == t['id'] for d in rows(ctx, 'issued_documents')):
        err(409, 'TEMPLATE_IN_USE', 'Documents were issued from this template, so it can’t be deleted. Turn it off instead.')
    if t.get('key') == 'payslip':
        err(409, 'TEMPLATE_IN_USE', 'Payroll issues payslips from this template. Edit it instead of deleting it.')
    remove(ctx, 'doc_templates', t['id'])
    log_activity(ctx, 'doc_template', t['id'], 'deleted', f'Deleted the “{t["name"]}” template')
    return {'ok': True}


def preview_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can issue documents.')
    t = find(ctx, 'doc_templates', req.params['id'], 'Template')
    ri = _read_issue(ctx, {**t, 'active': True}, req.body)
    out = []
    btitle = _s(_g(req.body, 'title'))
    for u in ri['people']:
        vars_ = {
            **standard_vars(ctx, u['id']),
            'ref_no': 'Assigned when issued',
            **_vars_for(ri['shared'], ri['per'], u['id'], _pick(suggest_vars(ctx, u['id']), t)),
        }
        rb = render_body(t['body'], vars_)
        out.append(
            {
                'employee_id': u['id'],
                'name': prop(u, 'name'),
                'title': btitle or f'{js_str(t["name"])} — {js_str(u.get("name", UNDEF))}',
                'body_html': rb['body_html'],
                'missing': [k for k in rb['missing'] if k != 'ref_no'],
            }
        )
    return out


def issue_template(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'documents.templates', 'Only HR can issue documents.')
    t = find(ctx, 'doc_templates', req.params['id'], 'Template')
    ri = _read_issue(ctx, t, body)
    people, shared, per, fields = ri['people'], ri['shared'], ri['per'], ri['fields']
    status = 'draft' if _g(body, 'status') == 'draft' else 'issued'
    if t.get('category') == 'payslip' and status == 'issued' and not js_truthy(_g(body, 'allow_manual_payslip')):
        warn(ctx, 'MANUAL_PAYSLIP', 'Payslips are normally issued from a finalised payroll run, so the numbers match payroll.')
    if status == 'issued':
        for u in people:
            v = _vars_for(shared, per, u['id'], _pick(suggest_vars(ctx, u['id']), t))
            missing = [f for f in fields if not js_truthy(_obj_get(v, f['key']))]
            if missing:
                lbl = ', '.join(js_lower(f['label']) for f in missing)
                who = f' for {js_str(u.get("name", UNDEF))}' if len(people) > 1 else ''
                err(422, 'VALIDATION', f'Fill {lbl}{who} before issuing, or save as a draft.', missing[0]['key'])
    title = _u16slice(_s(_g(body, 'title')), 0, 140)
    created = []
    for u in people:
        created.append(
            issue_document(
                ctx,
                {
                    'template_id': t['id'],
                    'employee_id': u['id'],
                    'vars': _vars_for(shared, per, u['id'], _pick(suggest_vars(ctx, u['id']), t)),
                    'status': status,
                    'title': (f'{title} — {js_str(u.get("name", UNDEF))}' if len(people) > 1 else title) if title else UNDEF,
                },
            )
        )
    users = {u['id']: u for u in rows(ctx, 'users')}
    return {'created': [_issued_row(ctx, d, users) for d in created], 'emailed': sum(1 for d in created if js_truthy(d.get('emailed_to')))}


# ---------------------------------------------------------------------------------------------------------------------
# routes: issued documents
# ---------------------------------------------------------------------------------------------------------------------
def list_issued(req: Req) -> Any:
    ctx = req.ctx
    _boot(ctx)
    mine = js_truthy(req.q('mine'))
    qe = req.q('employee_id')
    emp = js_str(qe) if js_truthy(qe) else None
    if not mine and not has(ctx, 'documents.templates') and not (emp and (emp == ctx.user_id or has(ctx, 'documents.employee_files'))):
        err(403, 'FORBIDDEN', 'You can see only your own documents.')
    lst = [d for d in rows(ctx, 'issued_documents') if can_see_issued(ctx, d)]
    if mine:
        lst = [d for d in lst if d.get('employee_id') == ctx.user_id and d.get('status') != 'draft']
    if emp:
        lst = [d for d in lst if d.get('employee_id') == emp]
    qc = req.q('category')
    if js_truthy(qc):
        lst = [d for d in lst if d.get('category') == qc]
    qs = req.q('status')
    if js_truthy(qs):
        lst = [d for d in lst if d.get('status') == qs]
    qr = req.q('run_id')
    if js_truthy(qr):
        lst = [d for d in lst if _g(d.get('source'), 'kind') == 'payroll' and _g(d.get('source'), 'ref_id') == qr]

    def day(d: dict) -> str:
        return coalesce(d.get('issued_on', UNDEF), d['created_at'][:10])

    qf = req.q('from')
    if js_truthy(qf):
        f = js_str(qf)
        lst = [d for d in lst if day(d) >= f]
    qt = req.q('to')
    if js_truthy(qt):
        t = js_str(qt)
        lst = [d for d in lst if day(d) <= t]
    users = {u['id']: u for u in rows(ctx, 'users')}
    tpls = {t['id']: t for t in rows(ctx, 'doc_templates')}
    out = [_issued_row(ctx, d, users, tpls) for d in lst]
    q = js_lower(_s(req.q('q')))
    if q:
        out = [
            d for d in out if q in js_lower(js_join([d['title'], d['employee_name'], d['employee_code'], d['ref_no'], coalesce(d['template_name'], '')], ' '))
        ]
    return sorted(out, key=lambda a: (locale_key(coalesce(a['issued_on'], a['created_at'])), locale_key(a['created_at'])), reverse=True)


def get_issued(req: Req) -> Any:
    ctx = req.ctx
    return _issued_detail(ctx, _load_issued(ctx, req.params['id']))


def _tpl_of(ctx: Ctx, d: dict) -> dict | None:
    tid = d.get('template_id')
    return _first(rows(ctx, 'doc_templates'), lambda t: t['id'] == tid) if js_truthy(tid) else None


def patch_issued(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    require_action(ctx, 'documents.templates', 'Only HR can edit documents.')
    d = _load_issued(ctx, req.params['id'])
    if d.get('status') != 'draft':
        err(409, 'NOT_DRAFT', 'Only drafts can be edited. Re-issue the document to correct it.')
    tpl = _tpl_of(ctx, d)
    if not tpl:
        err(409, 'TEMPLATE_MISSING', 'The template of this draft no longer exists.')
    vars_ = js_object({**thaw(coalesce(d.get('vars', UNDEF), {})), **_clean_vars(_g(body, 'vars'))})
    bt = _g(body, 'title')
    title = _u16slice(_s(bt), 0, 140) if bt is not UNDEF else d.get('title')
    if js_len(title) < 3:
        err(422, 'VALIDATION', 'Give the document a title.', 'title')
    rb = render_body(tpl['body'], {**standard_vars(ctx, d['employee_id']), **vars_})
    u = update(ctx, 'issued_documents', d['id'], {'vars': vars_, 'title': title, 'body_md': rb['body_md'], 'body_html': rb['body_html']}, req.if_match)
    log_activity(ctx, 'issued_document', d['id'], 'updated', f'Edited the draft “{title}”')
    return _issued_detail(ctx, u)


def issue_draft(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can issue documents.')
    d = _load_issued(ctx, req.params['id'])
    if d.get('status') != 'draft':
        err(409, 'NOT_DRAFT', 'This document is already issued.')
    tpl = _tpl_of(ctx, d)
    if not tpl:
        err(409, 'TEMPLATE_MISSING', 'The template of this draft no longer exists.')
    dv = d.get('vars')
    missing = [
        f
        for f in process_placeholders(tpl['body'], tpl['category'], coalesce(tpl.get('custom_placeholders', UNDEF), []))
        if not js_truthy(_obj_get(dv, f['key']) if isinstance(dv, dict) else UNDEF)
    ]
    if missing:
        err(422, 'VALIDATION', f'Fill {", ".join(js_lower(f["label"]) for f in missing)} before issuing.', missing[0]['key'])
    rb = render_body(tpl['body'], {**standard_vars(ctx, d['employee_id']), **(dv if isinstance(dv, dict) else {})})
    u = update(
        ctx,
        'issued_documents',
        d['id'],
        {'status': 'issued', 'issued_on': ctx.today, 'issued_by': ctx.user_id, 'body_md': rb['body_md'], 'body_html': rb['body_html']},
    )
    log_activity(
        ctx,
        'issued_document',
        d['id'],
        'issued',
        f'Issued “{js_str(d.get("title", UNDEF))}” ({js_str(d.get("ref_no", UNDEF))}) to {user_name(ctx, d.get("employee_id"))}',
    )
    announce_issued(ctx, u)
    persist()
    return _issued_detail(ctx, u)


def revoke_issued(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can revoke documents.')
    d = _load_issued(ctx, req.params['id'])
    if d.get('status') == 'draft':
        err(409, 'NOT_ISSUED', 'Drafts are deleted, not revoked.')
    if d.get('status') == 'revoked':
        err(409, 'REVOKED', 'This document is already revoked.')
    reason = _s(_g(req.body, 'reason'))
    if js_len(reason) < 5:
        err(422, 'VALIDATION', 'Say why the document is revoked (at least 5 characters).', 'reason')
    u = update(
        ctx, 'issued_documents', d['id'], {'status': 'revoked', 'revoked_on': ctx.today, 'revoked_by': ctx.user_id, 'revoke_reason': _u16slice(reason, 0, 500)}
    )
    title = js_str(d.get('title', UNDEF))
    notify(
        ctx,
        d['employee_id'],
        'NTF-06',
        'Document revoked',
        f'“{title}” was revoked by {js_str(ctx.user.get("name", UNDEF))}: {reason}',
        f'/documents/issued/{d["id"]}',
    )
    log_activity(ctx, 'issued_document', d['id'], 'revoked', f'Revoked “{title}” ({js_str(d.get("ref_no", UNDEF))}): {reason}')
    return _issued_detail(ctx, u)


def reissue(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can issue documents.')
    d = _load_issued(ctx, req.params['id'])
    if d.get('status') == 'draft':
        err(409, 'NOT_ISSUED', 'Issue the draft instead.')
    if js_truthy(d.get('replaced_by_id')):
        err(409, 'ALREADY_REISSUED', 'This document was already re-issued. Open the newer copy.')
    tid = d.get('template_id')
    tpl = _first(rows(ctx, 'doc_templates'), lambda t: t['id'] == tid and js_truthy(t.get('active'))) if js_truthy(tid) else None
    if not tpl:
        err(409, 'TEMPLATE_INACTIVE', 'The template is switched off or deleted. Turn it on to re-issue.')
    vars_ = {k: v for k, v in thaw(coalesce(d.get('vars', UNDEF), {})).items() if k != 'ref_no'}
    n = issue_document(
        ctx,
        {
            'template_id': tpl['id'],
            'employee_id': d['employee_id'],
            'vars': vars_,
            'title': d.get('title', UNDEF),
            'period': d.get('period', UNDEF),
            'source': thaw(d.get('source', UNDEF)),
        },
    )
    n['replaces_id'] = d['id']  # an owned (inserted) row: changed in place like the TS
    ch: dict[str, Any] = {'replaced_by_id': n['id']}
    if d.get('status') != 'revoked':
        ch.update({'status': 'revoked', 'revoked_on': ctx.today, 'revoked_by': ctx.user_id, 'revoke_reason': f'Replaced by {js_str(n.get("ref_no", UNDEF))}'})
    update(ctx, 'issued_documents', d['id'], ch)
    persist()
    log_activity(
        ctx,
        'issued_document',
        n['id'],
        'reissued',
        f'Re-issued “{js_str(d.get("title", UNDEF))}” as {js_str(n.get("ref_no", UNDEF))} (replaces {js_str(d.get("ref_no", UNDEF))})',
    )
    return _issued_detail(ctx, n)


def ack_issued(req: Req) -> Any:
    ctx = req.ctx
    d = _load_issued(ctx, req.params['id'])
    if d.get('employee_id') != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the employee it was issued to can acknowledge a document.')
    if d.get('status') == 'acknowledged':
        return _issued_detail(ctx, d)
    if d.get('status') != 'issued':
        err(
            409,
            'NOT_ISSUED',
            'This document was revoked and no longer needs acknowledgement.' if d.get('status') == 'revoked' else 'This document has not been issued yet.',
        )
    u = update(ctx, 'issued_documents', d['id'], {'status': 'acknowledged', 'acknowledged_on': ctx.today})
    name = js_str(ctx.user.get('name', UNDEF))
    title = js_str(d.get('title', UNDEF))
    log_activity(ctx, 'issued_document', d['id'], 'acknowledged', f'{name} acknowledged “{title}” ({js_str(d.get("ref_no", UNDEF))})')
    if d.get('issued_by') != ctx.user_id:
        notify(ctx, d.get('issued_by'), 'NTF-05', 'Document acknowledged', f'{name} acknowledged “{title}”.', f'/documents/issued/{d["id"]}')
    return _issued_detail(ctx, u)


def delete_issued(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.templates', 'Only HR can delete drafts.')
    d = _load_issued(ctx, req.params['id'])
    if d.get('status') != 'draft':
        err(409, 'NOT_DRAFT', 'Issued documents can’t be deleted. Revoke it instead.')
    remove(ctx, 'issued_documents', d['id'])
    log_activity(ctx, 'issued_document', d['id'], 'deleted', f'Deleted the draft “{js_str(d.get("title", UNDEF))}”')
    return {'ok': True}


# ---------------------------------------------------------------------------------------------------------------------
# routes: payslips from payroll
# ---------------------------------------------------------------------------------------------------------------------
def payslip_status(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['payroll.run', 'documents.templates'], 'Payslips are for payroll and HR.')
    _boot(ctx)
    run = find(ctx, 'payroll_runs', req.params['id'], 'Payroll run')
    lines = [ln for ln in rows(ctx, 'payroll_lines') if ln.get('run_id') == run['id'] and _gt0(ln.get('monthly_ctc_paise'))]
    docs = [
        d
        for d in rows(ctx, 'issued_documents')
        if _g(d.get('source'), 'kind') == 'payroll' and _g(d.get('source'), 'ref_id') == run['id'] and d.get('status') != 'revoked'
    ]
    return {
        'run_id': run['id'],
        'month': prop(run, 'month'),
        'run_status': prop(run, 'status'),
        'eligible': len(lines),
        'issued': len(docs),
        'acknowledged': sum(1 for d in docs if d.get('status') == 'acknowledged'),
        'can_generate': run.get('status') == 'final' and has_any(ctx, 'payroll.run', 'documents.templates'),
    }


def payslip_generate(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['payroll.run', 'documents.templates'], 'Payslips are for payroll and HR.')
    res = generate_payslips(ctx, req.params['id'])
    if res['skipped_no_ctc']:
        warn(ctx, 'NO_CTC', f'No payslip for {", ".join(js_str(x) for x in res["skipped_no_ctc"])}: no CTC on record for the month.')
    return res


# ---------------------------------------------------------------------------------------------------------------------
# routes: employee documents
# ---------------------------------------------------------------------------------------------------------------------
def emp_people(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'documents.employee_files', 'Employee documents are for HR.')
    _boot(ctx)
    by_emp: dict[Any, list] = {}
    for d in rows(ctx, 'employee_documents'):
        by_emp.setdefault(d.get('employee_id'), []).append(d)
    issued_n: dict[Any, int] = {}
    for d in rows(ctx, 'issued_documents'):
        if d.get('status') != 'draft' and d.get('status') != 'revoked':
            issued_n[d.get('employee_id')] = issued_n.get(d.get('employee_id'), 0) + 1
    all_ = js_truthy(req.q('all'))
    out = []
    for u in rows(ctx, 'users'):
        if js_truthy(u.get('is_super_admin')) or not (all_ or u.get('status') == 'active'):
            continue
        mine = by_emp.get(u['id'], [])
        out.append(
            {
                'user_id': u['id'],
                'name': prop(u, 'name'),
                'code': coalesce(prop(u, 'employee_code'), ''),
                'department': _master_name(ctx, 'departments', u.get('department_id')),
                'designation': js_or(_master_name(ctx, 'designations', u.get('designation_id')), prop(u, 'title')),
                'avatar_hue': prop(u, 'avatar_hue'),
                'docs': len(mine),
                'verified': sum(1 for d in mine if d.get('status') == 'verified'),
                'pending': sum(1 for d in mine if d.get('status') == 'pending'),
                'rejected': sum(1 for d in mine if d.get('status') == 'rejected'),
                'expiring': sum(1 for d in mine if _expiry_state(d, ctx.today) in ('expiring', 'expired')),
                'missing': _missing_of(mine),
                'issued': issued_n.get(u['id'], 0),
            }
        )
    return sorted(out, key=lambda a: (-a['pending'], -len(a['missing']), locale_key(a['name'])))


def emp_person(req: Req) -> Any:
    ctx = req.ctx
    _boot(ctx)
    pid = req.params['userId']
    self_ = pid == ctx.user_id or pid == 'me'
    id_ = ctx.user_id if pid == 'me' else pid
    if not self_ and not has(ctx, 'documents.employee_files'):
        err(403, 'FORBIDDEN', 'You can see only your own documents.')
    u = find(ctx, 'users', id_, 'Employee')
    if js_truthy(u.get('is_super_admin')):
        err(404, 'NOT_FOUND', 'Employee not found.')
    docs = [d for d in rows(ctx, 'employee_documents') if d.get('employee_id') == u['id']]
    users = {x['id']: x for x in rows(ctx, 'users')}
    can_tpl = has(ctx, 'documents.templates')
    issued = [
        d for d in rows(ctx, 'issued_documents') if d.get('employee_id') == u['id'] and can_see_issued(ctx, d) and (d.get('status') != 'draft' or can_tpl)
    ]
    rt = u.get('reports_to_id')
    return {
        'user': {
            'id': u['id'],
            'name': prop(u, 'name'),
            'code': coalesce(prop(u, 'employee_code'), ''),
            'department': _master_name(ctx, 'departments', u.get('department_id')),
            'designation': js_or(_master_name(ctx, 'designations', u.get('designation_id')), prop(u, 'title')),
            'email': prop(u, 'email'),
            'joined_on': prop(u, 'joined_on'),
            'manager_name': user_name(ctx, rt) if js_truthy(rt) else '',
            'avatar_hue': prop(u, 'avatar_hue'),
            'status': prop(u, 'status'),
        },
        'docs': sorted((_emp_doc_row(ctx, d) for d in docs), key=lambda a: locale_key(coalesce(a.get('uploaded_on', UNDEF), a['created_at'])), reverse=True),
        'issued': sorted((_issued_row(ctx, d, users) for d in issued), key=lambda a: locale_key(coalesce(a['issued_on'], a['created_at'])), reverse=True),
        'missing': _missing_of(docs),
        'can_verify': has(ctx, 'documents.employee_files') and u['id'] != ctx.user_id,
        'can_upload': self_ or has(ctx, 'documents.employee_files'),
        'can_issue': can_tpl,
    }


def emp_upload(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    be = _g(body, 'employee_id')
    eid = js_str(be) if js_truthy(be) else ctx.user_id
    on_behalf = eid != ctx.user_id
    if on_behalf:
        require_action(ctx, 'documents.employee_files', 'You can upload only your own documents.')
    emp = find(ctx, 'users', eid, 'Employee')
    kind = js_str(coalesce(_g(body, 'kind'), ''))
    if kind not in KINDS:
        err(422, 'VALIDATION', 'Choose what kind of document this is.', 'kind')
    title = _s(_g(body, 'title'))
    if js_len(title) < 3 or js_len(title) > 120:
        err(422, 'VALIDATION', 'Give the document a title of 3 to 120 characters.', 'title')
    f = _g(body, 'file')
    if not js_truthy(f):
        err(422, 'VALIDATION', 'Attach the scanned document.', 'file')
    name = _s(_g(f, 'name'))
    ext = js_lower(name.split('.')[-1]) if '.' in name else ''
    if not EXT_MIME.get(ext):
        err(422, 'FILE_REJECTED', 'Use a PDF, PNG, JPG or DOCX file.', 'file')
    size = js_number(coalesce(_g(f, 'size_bytes'), 0))
    if not (size > 0):
        err(422, 'FILE_REJECTED', 'The file is empty. Choose another file.', 'file')
    if size > MAX_EMP_BYTES:
        err(422, 'FILE_REJECTED', 'The file is larger than 5 MB. Scan at a lower resolution and try again.', 'file')
    du = _g(f, 'data_url')
    if not isinstance(du, str) or not du.startswith('data:'):
        err(422, 'FILE_REJECTED', 'The file could not be read. Choose it again.', 'file')
    bx = _g(body, 'expires_on')
    expires = js_str(bx) if js_truthy(bx) else None
    if expires and not _DATE_RE.match(expires):
        err(422, 'VALIDATION', 'Enter a valid expiry date.', 'expires_on')
    if kind == 'passport' and not expires:
        err(422, 'VALIDATION', 'Enter the passport’s expiry date.', 'expires_on')
    if expires and expires < ctx.today:
        warn(ctx, 'EXPIRED', 'This document has already expired. Upload the renewed one when you have it.')
    verified = on_behalf  # HR uploading on the employee's behalf has checked the original
    d = insert(
        ctx,
        'employee_documents',
        {
            'employee_id': emp['id'],
            'kind': kind,
            'title': title,
            'file_id': None,
            'uploaded_by': ctx.user_id,
            'verified_by': ctx.user_id if verified else None,
            'verified_on': ctx.today if verified else None,
            'status': 'verified' if verified else 'pending',
            'note': _u16slice(_s(_g(body, 'note')), 0, 500),
            'expires_on': expires,
            'doc_number': _u16slice(_s(_g(body, 'doc_number')), 0, 40) or None,
            'file_name': name,
            'mime': EXT_MIME[ext],
            'size_bytes': size,
            'uploaded_on': ctx.today,
            'review_note': None,
        },
    )
    file = insert(ctx, 'files', {'name': name, 'mime': EXT_MIME[ext], 'size_bytes': size, 'entity': 'employee_document', 'entity_id': d['id'], 'data_url': du})
    d['file_id'] = file['id']  # an owned (inserted) row: changed in place like the TS
    persist()
    log_activity(
        ctx,
        'employee_document',
        d['id'],
        'uploaded',
        f'Uploaded “{title}” for {js_str(emp.get("name", UNDEF))}' if on_behalf else f'Uploaded “{title}” for verification',
    )
    me_name = js_str(ctx.user.get('name', UNDEF))
    if on_behalf:
        notify(ctx, emp['id'], 'NTF-14', 'Document added to your file', f'{me_name} added “{title}” to your employee documents.', '/documents?tab=mine')
    else:
        for hr in users_with(ctx, 'documents.employee_files'):
            if hr['id'] != ctx.user_id and hr.get('role_key') != 'administrator' and hr.get('role_key') != 'ceo':
                notify(
                    ctx,
                    hr['id'],
                    'NTF-14',
                    'Document to verify',
                    f'{me_name} uploaded “{title}” ({kind.replace("_", " ", 1)}).',
                    f'/documents/employees/{ctx.user_id}',
                )
    return _emp_doc_row(ctx, d)


def _review_handler(action: str) -> Any:
    def h(req: Req) -> Any:
        ctx = req.ctx
        require_action(ctx, 'documents.employee_files', 'Only HR can verify employee documents.')
        d = find(ctx, 'employee_documents', req.params['id'], 'Document')
        if d.get('employee_id') == ctx.user_id:
            err(403, 'SELF_VERIFY', 'Someone else in HR must verify your own documents.')
        if d.get('status') != 'pending':
            err(409, 'NOT_PENDING', f'This document is already {js_str(d.get("status", UNDEF))}.')
        note = _s(_g(req.body, 'note'))
        verify = action == 'verify'
        if not verify and js_len(note) < 10:
            err(422, 'VALIDATION', 'Tell the employee what is wrong and what to upload instead (at least 10 characters).', 'note')
        u = update(
            ctx,
            'employee_documents',
            d['id'],
            {'status': 'verified' if verify else 'rejected', 'verified_by': ctx.user_id, 'verified_on': ctx.today, 'review_note': note or None},
        )
        name = js_str(ctx.user.get('name', UNDEF))
        title = js_str(d.get('title', UNDEF))
        notify(
            ctx,
            d['employee_id'],
            'NTF-05' if verify else 'NTF-06',
            'Document verified' if verify else 'Document rejected',
            f'{name} verified “{title}”.' if verify else f'{name} rejected “{title}”: {note}',
            '/documents?tab=mine',
        )
        log_activity(
            ctx,
            'employee_document',
            d['id'],
            'verified' if verify else 'rejected',
            f'{"Verified" if verify else "Rejected"} “{title}” of {user_name(ctx, d.get("employee_id"))}{f": {note}" if note else ""}',
        )
        return _emp_doc_row(ctx, u)

    return h


def emp_delete(req: Req) -> Any:
    ctx = req.ctx
    d = find(ctx, 'employee_documents', req.params['id'], 'Document')
    own = d.get('employee_id') == ctx.user_id
    if not own and not has(ctx, 'documents.employee_files'):
        err(404, 'NOT_FOUND', 'Document not found.')
    if own and not has(ctx, 'documents.employee_files') and d.get('status') == 'verified':
        err(409, 'VERIFIED', 'Verified documents can be removed only by HR. Upload a newer copy instead.')
    remove(ctx, 'employee_documents', d['id'])
    tid = ctx.tenant_id
    set_rows(
        'files', [f for f in get_db().files if not (f.get('tenant_id') == tid and f.get('entity') == 'employee_document' and f.get('entity_id') == d['id'])]
    )
    persist()
    log_activity(ctx, 'employee_document', d['id'], 'deleted', f'Removed “{js_str(d.get("title", UNDEF))}” of {user_name(ctx, d.get("employee_id"))}')
    return {'ok': True}


def emp_file(req: Req) -> Any:
    ctx = req.ctx
    d = find(ctx, 'employee_documents', req.params['id'], 'Document')
    if d.get('employee_id') != ctx.user_id and not has(ctx, 'documents.employee_files'):
        err(404, 'NOT_FOUND', 'Document not found.')
    fid = d.get('file_id')
    f = _first(get_db().files, lambda x: x.get('id') == fid and x.get('tenant_id') == ctx.tenant_id) if js_truthy(fid) else None
    return {
        'file_name': coalesce(prop(d, 'file_name'), prop(f, 'name'), 'document'),
        'mime': coalesce(prop(d, 'mime'), prop(f, 'mime'), 'application/octet-stream'),
        'data_url': coalesce(prop(f, 'data_url'), None),
    }


def register(r: Any) -> None:
    # templates
    r.get('/doc-templates', list_templates)
    r.get('/doc-templates/sample/:employeeId', template_sample)
    r.get('/doc-templates/suggest/:employeeId', template_suggest)
    r.get('/doc-templates/:id', get_template)
    r.post('/doc-templates', create_template)
    r.patch('/doc-templates/:id', patch_template)
    r.post('/doc-templates/:id/duplicate', duplicate_template)
    r.delete('/doc-templates/:id', delete_template)
    r.post('/doc-templates/:id/preview', preview_template, allow_view_as_write=True)
    r.post('/doc-templates/:id/issue', issue_template)
    # issued documents
    r.get('/issued-docs', list_issued)
    r.get('/issued-docs/:id', get_issued)
    r.patch('/issued-docs/:id', patch_issued)
    r.post('/issued-docs/:id/issue', issue_draft)
    r.post('/issued-docs/:id/revoke', revoke_issued)
    r.post('/issued-docs/:id/reissue', reissue)
    r.post('/issued-docs/:id/ack', ack_issued)
    r.delete('/issued-docs/:id', delete_issued)
    # payslips from payroll
    r.get('/payroll/:id/payslips', payslip_status)
    r.post('/payroll/:id/payslips', payslip_generate)
    # employee documents
    r.get('/employee-docs/people', emp_people)
    r.get('/employee-docs/person/:userId', emp_person)
    r.post('/employee-docs', emp_upload)
    for action in ('verify', 'reject'):
        r.post(f'/employee-docs/:id/{action}', _review_handler(action))
    r.delete('/employee-docs/:id', emp_delete)
    r.get('/employee-docs/:id/file', emp_file)
