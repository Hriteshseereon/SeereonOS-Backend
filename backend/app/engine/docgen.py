"""v7 document generation contract — port of src/mock/docgen.ts.

Renders a DocTemplate (by key or id) for an employee with {{placeholders}} and stores an IssuedDocument the employee
can open under Documents → My documents. Standard placeholders are filled automatically (employee_name, employee_code,
designation, department, joining_date, company, company_address, today…); `vars` adds/overrides process-specific ones.
When the document is issued (not a draft) the employee is notified in the app and by email.

`db` parameters of the TS (getDb()) are the request Scope here (`app.ctx.scope()`), like app/seeds/access.py.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, err, insert, log_activity, next_number, notify, patch, rows, scope
from app.db.uow import Scope
from app.engine.doc_markup import CATEGORY_LABEL, STANDARD_KEYS, _obj_get, fill_placeholders, js_object, markup_to_html, placeholders_in
from app.engine.email import send_email
from app.js import UNDEF, coalesce, js_or, js_str, js_trim, js_truthy, now_iso, now_ms, nullish, pad_start, prop

DEFAULT_DOC_TEMPLATES = cat('mock/seeds/documentsV7')['DEFAULT_DOC_TEMPLATES']

_EMPLOYMENT = {'full_time': 'Full-time', 'part_time': 'Part-time', 'contract': 'Contract', 'intern': 'Intern'}
_MONTH_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
_DATE_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}')


def _leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def long_date(iso: Any) -> str:
    """new Date(iso.slice(0, 10) + 'T00:00:00').toLocaleDateString('en-IN', { day, month: 'long', year }) — V8 accepts
    days up to 31 in every month (rolling over: 30 Feb -> 2 March) and rejects month 0/13+ and day 0/32+."""
    if not (isinstance(iso, str) and iso and _DATE_RE.match(iso)):
        return ''
    y, m, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return 'Invalid Date'
    dim = [31, 29 if _leap(y) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    if d > dim:
        d -= dim
        m += 1
    return f'{d} {_MONTH_LONG[m - 1]} {1 if y == 0 else y}'  # year 0 = 1 BC, shown as "1" by Intl


def _by_id(lst: list, id_: Any, tenant_id: str) -> dict | None:
    if not js_truthy(id_):
        return None
    return next((x for x in lst if x.get('id') == id_ and x.get('tenant_id') == tenant_id), None)


def _g(o: Any, k: str) -> Any:
    """o?.k"""
    return prop(o, k) if isinstance(o, dict) else UNDEF


def standard_vars_for(db: Scope, tenant_id: str, employee_id: str, issuer_id: str | None, today: str) -> dict[str, Any]:
    """Standard placeholders straight from the DB (also used by the seed, which has no request context)."""
    users = [x for x in db.view('users') if x.get('tenant_id') == tenant_id]
    u = next((x for x in users if x.get('id') == employee_id), None)
    issuer = next((x for x in users if x.get('id') == issuer_id), None) if js_truthy(issuer_id) else None
    t = next((x for x in db.view('tenants') if x.get('id') == tenant_id), None)
    o = _g(t, 'org')

    def desig(x: Any) -> Any:
        d = _by_id(db.view('designations'), _g(x, 'designation_id'), tenant_id)
        return js_or(_g(d, 'name'), _g(x, 'title'), '')

    grade = _by_id(db.view('grades'), _g(u, 'grade_id'), tenant_id)
    state_pin = ' '.join(js_str(v) for v in (_g(o, 'state'), _g(o, 'pincode')) if js_truthy(v))
    address = ', '.join(js_str(v) for v in (_g(o, 'registered_address'), _g(o, 'city'), state_pin) if js_truthy(v))
    name = coalesce(_g(u, 'name'), '')
    reports_to = _g(u, 'reports_to_id')
    manager = ''
    if js_truthy(reports_to):
        mgr = next((x for x in users if x.get('id') == reports_to), None)
        manager = coalesce(_g(mgr, 'name'), '')
    dept = _by_id(db.view('departments'), _g(u, 'department_id'), tenant_id)
    loc = _by_id(db.view('locations'), _g(u, 'location_id'), tenant_id)
    et = coalesce(_g(u, 'employment_type'), 'full_time')
    return {
        'employee_name': name,
        'employee_first_name': coalesce(js_str(name).split(' ')[0], ''),
        'employee_code': coalesce(_g(u, 'employee_code'), ''),
        'employee_email': coalesce(_g(u, 'email'), ''),
        'employee_phone': coalesce(_g(u, 'phone'), ''),
        'designation': desig(u),
        'department': coalesce(_g(dept, 'name'), ''),
        'grade': f'{js_str(grade.get("code", UNDEF))} · {js_str(grade.get("name", UNDEF))}' if grade else '',
        'location': coalesce(_g(loc, 'name'), ''),
        'employment_type': coalesce(_EMPLOYMENT.get(et, UNDEF) if isinstance(et, str) else UNDEF, ''),
        'joining_date': long_date(_g(u, 'joined_on')),
        'manager_name': manager,
        'company': js_or(_g(o, 'legal_name'), _g(t, 'name'), ''),
        'company_brand': js_or(_g(o, 'brand_name'), _g(t, 'name'), ''),
        'company_address': address,
        'company_city': coalesce(_g(o, 'city'), ''),
        'company_cin': coalesce(_g(o, 'cin'), ''),
        'company_gstin': coalesce(_g(o, 'gstin'), ''),
        'company_email': coalesce(_g(o, 'email'), ''),
        'company_phone': coalesce(_g(o, 'phone'), ''),
        'company_website': coalesce(_g(o, 'website'), ''),
        'signatory_name': coalesce(_g(issuer, 'name'), ''),
        'signatory_designation': desig(issuer),
        'today': long_date(today),
    }


def standard_vars(ctx: Ctx, employee_id: str) -> dict[str, Any]:
    return standard_vars_for(scope(), ctx.tenant_id, employee_id, ctx.user.get('id'), ctx.today)


def render_body(body: str, vars: dict[str, Any]) -> dict[str, Any]:
    """Render a template body for an employee: fill placeholders, then lay out the markup as HTML."""
    body_md = fill_placeholders(body, vars)
    missing = [k for k in placeholders_in(body) if not js_trim(js_str(coalesce(_obj_get(vars, k), '')))]
    return {'body_md': body_md, 'body_html': markup_to_html(body_md), 'missing': missing}


def _str(v: Any = UNDEF) -> dict[str, str]:
    if nullish(v) or not isinstance(v, dict):
        return {}
    return js_object({k: js_str(x) for k, x in js_object(v).items() if not nullish(x)})


def next_ref(ctx: Ctx, category: str, period: Any, employee_code: str) -> str:
    """Document reference number: <prefix>/HR/<year>/<seq> — or <prefix>/PAY/<period>/<code> for payslips."""
    t = next((x for x in scope().view('tenants') if x.get('id') == ctx.tenant_id), None)
    prefix = js_or(_g(_g(t, 'org'), 'employee_code_prefix'), 'DOC')
    if category == 'payslip' and js_truthy(period):
        return f'{js_str(prefix)}/PAY/{js_str(period)}/{js_str(js_or(employee_code, "EMP"))}'
    return f'{js_str(prefix)}/HR/{ctx.today[:4]}/{pad_start(js_str(next_number(ctx, "doc_ref", 1)), 4, "0")}'


_B36 = '0123456789abcdefghijklmnopqrstuvwxyz'


def _b36(n: int) -> str:
    s = ''
    while True:
        n, r = divmod(n, 36)
        s = _B36[r] + s
        if not n:
            return s


def ensure_doc_defaults(ctx: Ctx) -> None:
    """Seed the default letter templates (and the two emails documents send) for a workspace that has none yet."""
    s = scope()
    tid = ctx.tenant_id
    if any(x.get('tenant_id') == tid and x.get('scope') == 'docs_v7_defaults' for x in s.view('number_sequences')):
        return
    s.append('number_sequences', {'tenant_id': tid, 'scope': 'docs_v7_defaults', 'next_value': 1})

    def base() -> dict[str, Any]:
        t = now_iso()
        rnd = ''.join(_B36[secrets.randbelow(36)] for _ in range(6))  # Math.random().toString(36).slice(2, 8)
        return {'id': f'{_b36(now_ms())}{rnd}', 'tenant_id': tid, 'created_at': t, 'updated_at': t, 'version': 1}

    provision_doc_defaults(s, tid, base)  # (the TS pushes without persist(); appending here also marks write intent)


def provision_doc_defaults(db: Scope, tenant_id: str, base: Callable[[], dict]) -> None:
    """Default templates + document emails for a tenant (idempotent by key). Call from tenant provisioning."""
    from app.frozen import thaw

    for t in DEFAULT_DOC_TEMPLATES:
        if not any(x.get('tenant_id') == tenant_id and x.get('key') == t['key'] for x in db.view('doc_templates')):
            db.append(
                'doc_templates',
                {
                    **base(),
                    **thaw(t),
                    'placeholders': placeholders_in(t['body']),
                    'custom_placeholders': [],
                    'letterhead': True,
                    'active': True,
                    'updated_by': None,
                },
            )

    def mail(key: str, name: str, subject: str, body: str) -> None:
        if not any(x.get('tenant_id') == tenant_id and x.get('key') == key for x in db.view('email_templates')):
            db.append('email_templates', {**base(), 'key': key, 'name': name, 'subject': subject, 'body': body, 'active': True})

    mail(
        'payslip',
        'Payslip issued',
        'Your payslip for {{month}} is ready',
        'Hi {{employee_name}},\n\nYour payslip for {{month}} is ready. Net pay: {{net_pay}}.\n\nOpen it under Documents → My documents: {{login_url}}\n\n{{company}} HR',
    )
    mail(
        'custom',
        'Document issued',
        'A new document from {{company}}',
        'Hi {{employee_name}},\n\n{{company}} HR has issued a document to you in SeereonOS. Open Documents → My documents to read it, download a PDF copy and acknowledge it:\n{{login_url}}\n\n{{company}} HR',
    )


def announce_issued(ctx: Ctx, doc: dict) -> dict | None:
    """In-app notification + email to the employee for an issued document. Sets doc.emailed_to like the TS (in place
    on an owned row — pass the row returned by insert()/update(); a read row is copied on write)."""
    u = next((x for x in rows(ctx, 'users') if x.get('id') == doc.get('employee_id')), None)
    if not u:
        return None
    cat_ = doc.get('category')
    is_payslip = cat_ == 'payslip'
    if is_payslip:
        title = 'Payslip issued'
    elif cat_ == 'other':
        title = 'New document issued'
    else:
        label = _obj_get(CATEGORY_LABEL, cat_) if isinstance(cat_, str) else UNDEF
        if not isinstance(label, str):
            raise TypeError("Cannot read properties of undefined (reading 'toLowerCase')")
        title = f'New {label.lower()}'
    notify(
        ctx,
        u['id'],
        'NTF-14',
        title,
        f'{js_str(ctx.user.get("name", UNDEF))} issued “{js_str(doc.get("title", UNDEF))}”. Open it and acknowledge.',
        f'/documents/issued/{js_str(doc.get("id", UNDEF))}',
    )
    to = {'email': u.get('email', UNDEF), 'user_id': u['id']}
    related = {'entity': 'issued_document', 'id': doc.get('id')}
    if is_payslip:
        v = _g(doc, 'vars')
        email = send_email(
            ctx,
            'payslip',
            to,
            {
                'employee_name': u.get('name', UNDEF),
                'month': coalesce(_g(v, 'month_label'), _g(doc, 'period'), ''),
                'net_pay': coalesce(_g(v, 'net_pay'), ''),
            },
            related,
        )
    else:
        email = send_email(ctx, 'custom', to, {'employee_name': u.get('name', UNDEF)}, related)
    if js_truthy(email):
        patch('issued_documents', doc, {'emailed_to': u.get('email', UNDEF)})
    return email


def issue_document(ctx: Ctx, input: dict[str, Any]) -> dict:
    """input = IssueInput: template_key | template_id, employee_id, vars?, title?, period?, status?, source?, notify?,
    issued_on?"""
    ensure_doc_defaults(ctx)
    tid_in = input.get('template_id', UNDEF)
    tkey_in = input.get('template_key', UNDEF)
    tpl = next(
        (
            x
            for x in rows(ctx, 'doc_templates')
            if (x.get('id') == tid_in if js_truthy(tid_in) else x.get('key', UNDEF) == tkey_in) and js_truthy(x.get('active'))
        ),
        None,
    )
    if not tpl:
        err(422, 'TEMPLATE_MISSING', f'No active document template "{js_str(coalesce(tkey_in, tid_in))}". Create it under Documents → Templates.')
    emp = next((u for u in rows(ctx, 'users') if u.get('id') == input.get('employee_id')), None)
    if not emp:
        err(404, 'NOT_FOUND', 'Employee not found.')
    status = coalesce(input.get('status', UNDEF), 'issued')
    std = standard_vars(ctx, input['employee_id'])
    extra = _str(input.get('vars', UNDEF))
    period = coalesce(input.get('period', UNDEF), None)
    ref_no = js_or(extra.get('ref_no', UNDEF), UNDEF)
    if not js_truthy(ref_no):
        ref_no = next_ref(ctx, tpl['category'], period, std['employee_code'])
    vars_ = js_object({**std, 'ref_no': ref_no, **extra})
    rb = render_body(tpl['body'], vars_)
    proc_vars = {k: v for k, v in vars_.items() if k not in STANDARD_KEYS or k == 'ref_no'}
    doc = insert(
        ctx,
        'issued_documents',
        {
            'template_id': tpl['id'],
            'category': tpl['category'],
            'title': coalesce(input.get('title', UNDEF), f'{js_str(tpl.get("name", UNDEF))} — {js_str(vars_.get("employee_name", UNDEF))}'),
            'employee_id': input['employee_id'],
            'body_html': rb['body_html'],
            'body_md': rb['body_md'],
            'vars': proc_vars,
            'letterhead': tpl.get('letterhead', UNDEF),
            'ref_no': ref_no,
            'period': period,
            'status': status,
            'issued_by': ctx.user.get('id'),
            'issued_on': coalesce(input.get('issued_on', UNDEF), ctx.today) if status == 'issued' else None,
            'acknowledged_on': None,
            'source': coalesce(input.get('source', UNDEF), {'kind': 'manual', 'ref_id': None}),
            'revoked_on': None,
            'revoked_by': None,
            'revoke_reason': None,
            'replaces_id': None,
            'replaced_by_id': None,
            'emailed_to': None,
        },
    )
    if status == 'issued':
        log_activity(ctx, 'issued_document', doc['id'], 'issued', f'Issued “{js_str(doc["title"])}” ({js_str(ref_no)}) to {js_str(emp.get("name", UNDEF))}')
        if input.get('notify', UNDEF) is not False:
            announce_issued(ctx, doc)
    else:
        log_activity(ctx, 'issued_document', doc['id'], 'drafted', f'Saved a draft of “{js_str(doc["title"])}” for {js_str(emp.get("name", UNDEF))}')
    return doc


now_stamp = now_iso
