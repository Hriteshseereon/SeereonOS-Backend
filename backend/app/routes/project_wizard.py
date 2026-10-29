"""v9 New Project wizard (items 1 + 2, decisions D1–D3) and the project escalation matrix.

Seven steps — (1) customer, (2) project + client details, (3) resource allocation, (4) commercials, (5) milestones &
WBS (optional), (6) escalation matrix, (7) overview — all validated here:

  POST /projects/wizard/preview   read-only: validates the steps sent, returns per-step errors and every computed
                                  number (calendar, provisional milestone dates, costing chain, per-person share)
  POST /projects/wizard           ONE atomic create: customer (new or picked) → project (the Feature Guide create
                                  rules of POST /projects: code, Initiated, status history, auto milestones, costing
                                  plan + allocations, documents, PM notification) → milestone details → tasks and
                                  subtasks → escalation matrix → optional estimate conversion. Idempotent on
                                  `request_key` (the same submission returns the project it created).
  GET  /projects/:id/escalation   the matrix (anyone who can open the project)
  PUT  /projects/:id/escalation   edit it later (the project's editors)

Every write happens in the request's unit of work, so a failure anywhere leaves nothing behind.
"""

from __future__ import annotations

import re
from typing import Any

from app.ctx import Ctx, Req, err, find, has, has_any, insert, log_activity, notify, now_iso, patch, rows, update, user_name, warn
from app.domain.calendar import add_working_days, fmt_date, is_real_iso_date, max_iso
from app.engine import costing as co
from app.engine.analytics import cal, sync_milestone_dates
from app.errors import ApiError
from app.js import UNDEF, coalesce, is_finite, js_number, js_round, js_str, js_trim

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+\Z')
STEPS = ('customer', 'details', 'resources', 'commercials', 'milestones', 'escalation')
ESC_UNITS = ('hours', 'days', 'working_days')
MAX_TASKS = 200


def _o(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list:
    return v if isinstance(v, list) else []


def _t(v: Any, n: int) -> str:
    return js_trim(js_str(coalesce(v if v is not UNDEF else None, '')))[:n]


def _iso(v: Any) -> bool:
    return isinstance(v, str) and is_real_iso_date(v)


def _pj() -> Any:
    from app.routes import projects

    return projects


# =====================================================================================================================
# Field validators (shared with PATCH /projects/:id and the escalation tab)
# =====================================================================================================================
def validate_client_details(v: Any, prefix: str = 'details.client_details') -> dict | None:
    """The client's people for THIS project: contact, PO / work-order reference, notes. None when empty."""
    if v is None or v is UNDEF or v == '':
        return None
    if not isinstance(v, dict):
        err(422, 'VALIDATION', 'Client details must be an object.', prefix)
    em = _t(v.get('contact_email'), 160)
    if em and not _EMAIL_RE.search(em):
        err(422, 'VAL_EMAIL', 'Client contact: enter a valid e-mail address.', f'{prefix}.contact_email')
    out = {
        'contact_name': _t(v.get('contact_name'), 120),
        'contact_role': _t(v.get('contact_role'), 80),
        'contact_email': em,
        'contact_phone': _t(v.get('contact_phone'), 40),
        'po_reference': _t(v.get('po_reference'), 80),
        'notes': _t(v.get('notes'), 1000),
    }
    return out if any(out.values()) else None


def validate_escalation(ctx: Ctx, v: Any, prefix: str = 'escalation') -> dict | None:
    """Escalation matrix: structured rows (level, person or external name, role, e-mail, phone, escalate after, scope)
    + notes. None when empty."""
    b = _o(v)
    raw = _l(b.get('rows'))
    if len(raw) > 20:
        err(422, 'VALIDATION', 'Keep the escalation matrix to 20 rows.', f'{prefix}.rows')
    out_rows = []
    for i, r0 in enumerate(raw):
        r = _o(r0)
        uid = r.get('user_id') if isinstance(r.get('user_id'), str) and r.get('user_id') else None
        name = _t(r.get('name'), 120)
        role = _t(r.get('role'), 80)
        email = _t(r.get('email'), 160)
        phone = _t(r.get('phone'), 40)
        scope = _t(r.get('scope'), 200)
        if not uid and not name and not role and not email and not phone and not scope:
            continue
        f = f'{prefix}.rows.{i}'
        lvl = js_number(coalesce(r.get('level'), i + 1))
        if not is_finite(lvl) or lvl != int(lvl) or lvl < 1 or lvl > 9:
            err(422, 'VALIDATION', f'Escalation row {i + 1}: the level is a whole number from 1 to 9.', f'{f}.level')
        u = None
        if uid:
            u = next((x for x in rows(ctx, 'users') if x['id'] == uid), None)
            if u is None or u.get('status') != 'active':
                err(422, 'VALIDATION', f'Escalation row {i + 1}: choose an active person, or type an external name.', f'{f}.user_id')
        if not u and not name:
            err(422, 'VAL_REQUIRED', f'Escalation row {i + 1}: choose a person or type the name.', f'{f}.name')
        if email and not _EMAIL_RE.search(email):
            err(422, 'VAL_EMAIL', f'Escalation row {i + 1}: enter a valid e-mail address.', f'{f}.email')
        av = r.get('after_value')
        after = None
        if av is not None and av != '' and av is not UNDEF:
            after = js_number(av)
            if not is_finite(after) or after < 0 or after > 999:
                err(422, 'VALIDATION', f'Escalation row {i + 1}: escalate after 0 to 999 hours or days.', f'{f}.after_value')
            after = js_round(after * 10) / 10
        unit = r.get('after_unit') if r.get('after_unit') in ESC_UNITS else 'hours'
        out_rows.append(
            {
                'level': int(lvl),
                'user_id': u['id'] if u else None,
                'name': u['name'] if u else name,
                'role': role,
                'email': email or (js_str(u.get('email')) if u and u.get('email') else ''),
                'phone': phone,
                'after_value': after,
                'after_unit': unit,
                'scope': scope,
            }
        )
    out_rows.sort(key=lambda x: x['level'])
    notes = js_trim(js_str(coalesce(b.get('notes'), '')))
    if len(notes) > 8000:
        err(422, 'VALIDATION', 'Keep the escalation notes under 8,000 characters.', f'{prefix}.notes')
    if not out_rows and not notes:
        return None
    return {'rows': out_rows, 'notes': notes}


# =====================================================================================================================
# Steps
# =====================================================================================================================
def _customer(ctx: Ctx, b: dict) -> dict:
    """-> {mode, client (name), client_id, row (existing), new (validated fields)}"""
    from app.routes.clients import validate_customer

    c = _o(b.get('customer'))
    mode = c.get('mode') if c.get('mode') in ('existing', 'new', 'internal') else None
    if mode is None:
        err(422, 'VAL_REQUIRED', 'Choose an existing customer, add a new one, or mark the project as internal.', 'customer.mode')
    if mode == 'existing':
        cid = c.get('client_id')
        if not isinstance(cid, str) or not cid:
            err(422, 'VAL_REQUIRED', 'Choose the customer.', 'customer.client_id')
        row = next((x for x in rows(ctx, 'clients') if x['id'] == cid), None)
        if row is None:
            err(422, 'VALIDATION', 'That customer no longer exists. Choose another one.', 'customer.client_id')
        return {'mode': mode, 'client': row['name'], 'client_id': row['id'], 'row': row, 'new': None}
    if mode == 'new':
        if not has_any(ctx, 'clients.manage', 'projects.create'):
            err(403, 'FORBIDDEN', 'You can’t add customers. Pick an existing one.', 'customer.mode')
        try:
            v = validate_customer(ctx, _o(c.get('new')))
        except ApiError as e:
            f = e.field if isinstance(e.field, str) else 'name'
            raise ApiError(e.status, e.code, e.message, f'customer.{f}') from None
        return {'mode': mode, 'client': v['name'], 'client_id': None, 'row': None, 'new': v}
    label = _t(c.get('internal_label'), 120) or 'Internal'
    return {'mode': mode, 'client': label, 'client_id': None, 'row': None, 'new': None}


def _details(ctx: Ctx, b: dict) -> dict:
    """The same rules as POST /projects (G3.07), with step-prefixed fields."""
    pj = _pj()
    d = _o(b.get('details'))
    f = 'details.'
    name = _t(d.get('name'), 200)
    if not name:
        err(422, 'VAL_REQUIRED', 'Give the project a name.', f + 'name')
    if d.get('type') not in pj.TYPES:
        err(422, 'VALIDATION', 'Choose a project type: Product, Service, SaaS or AMS.', f + 'type')
    if d.get('billing_model') not in pj.MODELS:
        err(422, 'VALIDATION', 'Choose a commercial model.', f + 'billing_model')
    if d.get('stream') not in pj.STREAMS:
        err(422, 'VALIDATION', 'Choose a revenue stream.', f + 'stream')
    if not _iso(d.get('start_on')):
        err(422, 'VALIDATION', 'Pick the start date.', f + 'start_on')
    if not _iso(d.get('end_on')):
        err(422, 'VALIDATION', 'Pick the planned end.', f + 'end_on')
    if d['end_on'] < d['start_on']:
        err(422, 'DATE_RANGE', 'The planned end is before the start date.', f + 'end_on')
    cv = d.get('contract_value_paise')
    contract = None
    if cv is not None and cv != '' and cv is not UNDEF:
        contract = js_number(cv)
        if not is_finite(contract) or contract < 0 or contract != int(contract):
            err(422, 'MONEY_INVALID', 'Contract value must be zero or more.', f + 'contract_value_paise')
    me = ctx.user['id']
    pm_id = d.get('pm_id') if isinstance(d.get('pm_id'), str) and d.get('pm_id') else (me if has(ctx, 'projects.edit') else None)
    if not pm_id:
        err(422, 'VAL_REQUIRED', 'Choose the project manager.', f + 'pm_id')
    try:
        pj._active_user(ctx, pm_id, f + 'pm_id', True)
    except ApiError as e:
        raise ApiError(e.status, e.code, e.message, f + 'pm_id') from None
    if not has(ctx, 'projects.manage_all') and pm_id != me:
        err(403, 'FORBIDDEN', 'Project managers create projects they manage. Ask an admin to assign someone else.', f + 'pm_id')
    pr = js_number(d.get('priority'))
    try:
        docs = co.validate_create_documents(d.get('documents', UNDEF))
    except ApiError as e:
        raise ApiError(e.status, e.code, e.message, f'details.{e.field}' if isinstance(e.field, str) else 'details.documents') from None
    deal = d.get('deal_id') if isinstance(d.get('deal_id'), str) and d.get('deal_id') else None
    if deal:
        find(ctx, 'deals', deal, 'Deal')
    return {
        'name': name,
        'type': d['type'],
        'billing_model': d['billing_model'],
        'stream': d['stream'],
        'start_on': d['start_on'],
        'end_on': d['end_on'],
        'contract_value_paise': contract,
        'pm_id': pm_id,
        'priority': int(pr) if pr in (1, 2, 3, 4, 5) else 3,
        'description': _t(d.get('description'), 4000),
        'client_details': validate_client_details(d.get('client_details')),
        'documents': d.get('documents', UNDEF),
        'documents_n': len(docs),
        'deal_id': deal,
    }


def _milestones(ctx: Ctx, b: dict, det: dict) -> dict:
    """Milestones & WBS (optional): planned milestones (auto spread or explicit dates, as in create_project), details,
    tasks and subtasks."""
    m0 = _o(b.get('milestones'))
    items = [x for x in _l(m0.get('items')) if _t(_o(x).get('name'), 120)]
    if len(items) > 50:
        err(422, 'VALIDATION', 'Keep it to 50 milestones; add more later on the WBS.', 'milestones.items')
    start, end = det['start_on'], det['end_on']
    seen: dict[str, int] = {}
    ms_in = []
    for i, x0 in enumerate(items):
        x = _o(x0)
        nm = _t(x.get('name'), 120)
        k = nm.lower()
        if k in seen:
            err(422, 'VALIDATION', f'Milestone {i + 1}: “{nm}” is already M{seen[k] + 1}. Give each milestone its own name.', f'milestones.items.{i}.name')
        seen[k] = i
        amt = js_number(coalesce(x.get('billing_amount_paise'), 0))
        if not is_finite(amt) or amt < 0 or amt != int(amt):
            err(422, 'MONEY_INVALID', f'Milestone {i + 1}: the billing amount must be zero or more.', f'milestones.items.{i}.billing_amount_paise')
        pe = x.get('planned_end')
        if pe not in (None, '', UNDEF) and not _iso(pe):
            err(422, 'VALIDATION', f'Milestone {i + 1}: the end date is not a valid date.', f'milestones.items.{i}.planned_end')
        ms_in.append({'name': nm, 'billing_amount_paise': int(amt), 'planned_end': pe if _iso(pe) else None, 'details': _t(x.get('details'), 2000)})
    explicit = bool(ms_in) and all(m['planned_end'] for m in ms_in)
    if explicit:
        prev = start
        planned = []
        for i, m in enumerate(ms_in):
            e = m['planned_end']
            if e < prev or e > end:
                err(
                    422,
                    'DATE_RANGE',
                    f'Milestone {i + 1}: the end date must fall between {fmt_date(prev)} and the project end {fmt_date(end)}, in delivery order.',
                    f'milestones.items.{i}.planned_end',
                )
            s = start if i == 0 else min(add_day(prev), e)
            prev = e
            planned.append({'index': i, 'id': None, 'code': f'M{i + 1}', 'name': m['name'], 'billing_amount_paise': m['billing_amount_paise'], 'planned_start': s, 'planned_end': e})
    else:
        planned = co.spread_milestones(ctx, start, end, [{'name': m['name'], 'billing_amount_paise': m['billing_amount_paise']} for m in ms_in])
    users = {u['id']: u for u in rows(ctx, 'users')}
    n_tasks = 0
    tasks_by_ms: list[list[dict]] = []
    for i, x0 in enumerate(items):
        tl = []
        for j, t0 in enumerate(_l(_o(x0).get('tasks'))):
            t = _o(t0)
            f = f'milestones.items.{i}.tasks.{j}'
            v = _task(t, f, users, start, end, f'M{i + 1} task {j + 1}')
            if v is None:
                continue
            subs = []
            for k, s0 in enumerate(_l(t.get('subtasks'))):
                sv = _task(_o(s0), f'{f}.subtasks.{k}', users, start, end, f'M{i + 1} task {j + 1} subtask {k + 1}')
                if sv is not None:
                    subs.append(sv)
            v['subtasks'] = subs
            n_tasks += 1 + len(subs)
            tl.append(v)
        tasks_by_ms.append(tl)
    if n_tasks > MAX_TASKS:
        err(422, 'VALIDATION', f'Add up to {MAX_TASKS} tasks here; import the rest on the WBS tab.', 'milestones.items')
    return {
        'items': ms_in,
        'planned': planned,
        'explicit': explicit,
        'tasks': tasks_by_ms,
        'task_count': n_tasks,
        'billing_total': sum(m['billing_amount_paise'] for m in ms_in),
    }


def add_day(d: str) -> str:
    from app.domain.calendar import add_days

    return add_days(d, 1)


def _task(t: dict, f: str, users: dict, start: str, end: str, label: str) -> dict | None:
    title = _t(t.get('title'), 200)
    if not title:
        if any(_t(t.get(k), 10) for k in ('description', 'owner_id')) or _l(t.get('subtasks')):
            err(422, 'VAL_REQUIRED', f'{label}: add the title.', f'{f}.title')
        return None
    owner = t.get('owner_id') if isinstance(t.get('owner_id'), str) and t.get('owner_id') else None
    if owner:
        u = users.get(owner)
        if u is None or u.get('status') != 'active':
            err(422, 'VALIDATION', f'{label}: choose an active owner.', f'{f}.owner_id')
    est_v = t.get('est_hours')
    est = 8 if est_v in (None, '', UNDEF) else js_number(est_v)
    if not is_finite(est) or est < 0 or est > 999:
        err(422, 'VALIDATION', f'{label}: estimated hours must be between 0 and 999.', f'{f}.est_hours')
    s = t.get('start_on') if _iso(t.get('start_on')) else None
    d = t.get('due_on') if _iso(t.get('due_on')) else None
    for k, v in (('start_on', s), ('due_on', d)):
        if v and (v < start or v > end):
            err(422, 'DATE_RANGE', f'{label}: dates must sit inside the project ({fmt_date(start)} – {fmt_date(end)}).', f'{f}.{k}')
    if s and d and d < s:
        err(422, 'DATE_RANGE', f'{label}: the due date is before the start date.', f'{f}.due_on')
    return {'title': title, 'owner_id': owner, 'est_hours': js_round(est * 4) / 4, 'start_on': s, 'due_on': d, 'description': _t(t.get('description'), 4000)}


def _can_plan(ctx: Ctx) -> bool:
    return co.can_plan_cost(ctx) and has(ctx, 'allocation.edit')


def _costing_body(b: dict) -> dict:
    """Steps 3 + 4 as the costing engine's body (resources + commercials)."""
    c = _o(b.get('commercials'))
    res = []
    for r0 in _l(b.get('resources')):
        r = _o(r0)
        mi = r.get('milestone_index')
        res.append(
            {
                'key': r.get('key'),
                'user_id': r.get('user_id'),
                'milestone_index': mi if isinstance(mi, int) and not isinstance(mi, bool) else None,
                'pct': r.get('pct'),
                'from_on': r.get('from_on') or None,
                'to_on': r.get('to_on') or None,
                # V9-FIX (item 5): the role a converted estimate's placeholder row stands for (message only)
                **({'role_label': _t(r.get('role_label'), 80)} if not r.get('user_id') and _t(r.get('role_label'), 80) else {}),
            }
        )
    return {
        'resources': res,
        'additional_costs': _l(c.get('additional_costs')),
        'maintenance_paise': coalesce(c.get('maintenance_paise'), 0),
        'contingency_pct': coalesce(c.get('contingency_pct'), 0),
        'markup_pct': c.get('markup_pct', 50),
        'price_paise': c.get('price_paise'),
        'price_mode': 'price' if c.get('price_mode') == 'price' else 'markup',
    }


def _has_costing(cb: dict) -> bool:
    return bool(cb['resources']) or any(_o(x).get('amount_paise') or _t(_o(x).get('description'), 5) for x in cb['additional_costs']) or js_number(cb['maintenance_paise']) > 0


def _validate_all(ctx: Ctx, b: dict, upto: str | None = None) -> dict:
    """Validate the steps in order (all, or up to `upto`); raises the first error. -> the resolved parts."""
    order = list(STEPS)
    last = order.index(upto) if upto in order else len(order) - 1
    out: dict[str, Any] = {}
    out['customer'] = _customer(ctx, b)
    if last >= 1:
        out['details'] = _details(ctx, b)
    if last >= 2:
        det = out['details']
        ms = _milestones(ctx, b, det)  # resources may land on the milestones of step 5
        out['milestones'] = ms
        cb = _costing_body(b)
        out['costing_body'] = cb
        if _has_costing(cb):
            if not co.can_plan_cost(ctx):
                err(403, 'FORBIDDEN', 'Costing needs the margins or salaries permission. Leave resources and commercials empty.', 'resources')
            if not has(ctx, 'allocation.edit'):
                err(403, 'FORBIDDEN', 'You can’t allocate people, so leave resource allocation empty.', 'resources')
            out['costing'] = co.validate_costing(ctx, None, {'start': det['start_on'], 'end': det['end_on']}, ms['planned'], cb)
        else:
            out['costing'] = None
    if last >= 5:
        out['escalation'] = validate_escalation(ctx, b.get('escalation'))
    return out


def _field_step(field: Any) -> str:
    f = js_str(field) if isinstance(field, str) else ''
    head = f.split('.')[0]
    if head in ('resources',):
        return 'resources'
    if head in ('additional_costs', 'costing'):
        return 'commercials'
    return head if head in STEPS else 'details'


# =====================================================================================================================
# Routes
# =====================================================================================================================
def preview(req: Req) -> Any:
    """POST /projects/wizard/preview {step?, ...wizard} — validation + numbers; nothing is stored."""
    ctx = req.ctx
    if not has(ctx, 'projects.create'):
        err(403, 'FORBIDDEN', 'Only admins and project managers can create projects.')
    b = _o(req.body)
    upto = b.get('step') if b.get('step') in STEPS else None
    steps: dict[str, Any] = {s: {'ok': None, 'error': None} for s in STEPS}
    resolved: dict[str, Any] = {}
    try:
        resolved = _validate_all(ctx, b, upto)
        for s in STEPS[: (STEPS.index(upto) + 1) if upto else len(STEPS)]:
            steps[s]['ok'] = True
    except ApiError as e:
        st = _field_step(e.field)
        steps[st] = {'ok': False, 'error': {'code': e.code, 'message': e.message, 'field': e.field if isinstance(e.field, str) else None, 'status': e.status}}
        for s in STEPS[: STEPS.index(st)]:
            steps[s]['ok'] = True
    out: dict[str, Any] = {'steps': steps, 'can_plan_cost': _can_plan(ctx), 'can_see_rates': co.can_see_salaries(ctx), 'can_create_customer': has_any(ctx, 'clients.manage', 'projects.create')}
    # numbers, whenever the dates allow (even while a later step has an error)
    d = _o(b.get('details'))
    if _iso(d.get('start_on')) and _iso(d.get('end_on')) and d['end_on'] >= d['start_on']:
        try:
            ms = resolved.get('milestones') or _milestones(ctx, b, {'start_on': d['start_on'], 'end_on': d['end_on']})
        except ApiError:
            ms = {'planned': co.spread_milestones(ctx, d['start_on'], d['end_on'], []), 'task_count': 0, 'billing_total': 0}
        cb = _costing_body(b)
        pv = co._compute_preview(ctx, {**co._args_from_body(cb), 'range': {'start': d['start_on'], 'end': d['end_on']}, 'milestones': ms['planned']})
        if pv['can_see_money']:
            oa = co._over_allocation(ctx, None, pv['_resolved'])
            for o in oa['over']:
                n = len(o['days'])
                pv['warnings'].append(f'{user_name(ctx, o["user_id"])} would be over {js_str(oa["max"])}% on {n} {co._days_word(n)} (peak {js_str(o["peak"])}%).')
        out['costing'] = co._public_preview(pv)
        out['milestones'] = ms['planned']
        out['task_count'] = ms.get('task_count', 0)
        out['billing_total'] = ms.get('billing_total', 0)
        cv = d.get('contract_value_paise')
        price = out['costing']['price_paise']
        out['contract_value_paise'] = js_number(cv) if cv not in (None, '', UNDEF) else (price if price is not None and _has_costing(cb) else 0)
    else:
        out['costing'] = None
        out['milestones'] = []
    cust = resolved.get('customer')
    out['customer'] = {'mode': cust['mode'], 'name': cust['client'], 'client_id': cust['client_id']} if cust else None
    out['next_code'] = _next_code(ctx)
    return out


def _next_code(ctx: Ctx) -> str:
    from app.ctx import get_db
    from app.js import js_or, pad_start

    prefix = js_str(js_or(ctx.tenant['settings'].get('projects.code_prefix', UNDEF), 'PRJ'))
    seq = next((x for x in get_db().number_sequences if x.get('tenant_id') == ctx.tenant_id and x.get('scope') == 'project'), None)
    taken = {p.get('code') for p in rows(ctx, 'projects')}
    n = coalesce(seq.get('next_value', UNDEF) if seq else UNDEF, 1)
    while f'{prefix}-{pad_start(js_str(n), 3, "0")}' in taken:
        n += 1
    return f'{prefix}-{pad_start(js_str(n), 3, "0")}'


def create(req: Req) -> Any:
    """POST /projects/wizard — everything in one unit of work."""
    ctx = req.ctx
    pj = _pj()
    if not has(ctx, 'projects.create'):
        err(403, 'FORBIDDEN', 'Only admins and project managers can create projects.')
    b = _o(req.body)
    key = _t(b.get('request_key'), 80) or None
    if key:
        dup = next((p for p in rows(ctx, 'projects') if p.get('create_key') == key), None)
        if dup is not None:
            return {'project': dup, 'duplicate': True, 'tasks': 0}
    v = _validate_all(ctx, b)
    cust, det, ms, esc = v['customer'], v['details'], v['milestones'], v['escalation']
    me = ctx.user['id']
    # (1) the customer
    client_id = cust['client_id']
    if cust['mode'] == 'new':
        from app.routes.clients import create_customer

        row = create_customer(ctx, _o(_o(b.get('customer')).get('new')))
        client_id = row['id']
    # (2) the project through the Feature Guide create rules (POST /projects)
    cb = v['costing_body']
    contract = det['contract_value_paise']
    if contract is None:
        contract = v['costing']['priceInternal'] if v['costing'] is not None else 0
    body = {
        'name': det['name'],
        'client': cust['client'],
        'client_id': client_id,
        'type': det['type'],
        'billing_model': det['billing_model'],
        'stream': det['stream'],
        'pm_id': det['pm_id'],
        'start_on': det['start_on'],
        'end_on': det['end_on'],
        'contract_value_paise': contract,
        'priority': det['priority'],
        'description': det['description'],
        'deal_id': det['deal_id'],
        'milestones': [{'name': m['name'], 'billing_amount_paise': m['billing_amount_paise'], 'planned_end': m['planned_end']} for m in ms['items']],
        'costing': cb if v['costing'] is not None else None,
        'documents': det['documents'],
    }
    if client_id is None:
        body.pop('client_id')
    try:
        p = pj.create_project(Req({}, {}, body, UNDEF, ctx))
    except ApiError as e:
        raise ApiError(e.status, e.code, e.message, f'details.{e.field}' if isinstance(e.field, str) and not e.field.startswith(('resources', 'costing', 'additional_costs')) else e.field) from None
    extra: dict[str, Any] = {}
    if cust['mode'] == 'internal' and 'client_id' in p:
        extra['client_id'] = None
    if det['client_details'] is not None:
        extra['client_details'] = det['client_details']
    if esc is not None:
        extra['escalation'] = {**esc, 'updated_at': now_iso(), 'updated_by': me}
    if key:
        extra['create_key'] = key
    if extra:
        p = update(ctx, 'projects', p['id'], extra)
    # (3) milestone details, tasks and subtasks
    live = sorted((m for m in rows(ctx, 'milestones') if m['project_id'] == p['id'] and not m.get('deleted_at')), key=lambda m: m['no'])
    for i, m in enumerate(live):
        if i < len(ms['items']) and ms['items'][i]['details']:
            patch('milestones', m, {'details': ms['items'][i]['details']})
    n_tasks = _insert_tasks(ctx, p, live, ms['tasks'])
    # (4) the people allocated hear about it
    for uid in dict.fromkeys(a['user_id'] for a in rows(ctx, 'allocations') if a['project_id'] == p['id']):
        if uid not in (me, p.get('pm_id')):
            notify(ctx, uid, 'NTF-03', f'You’re on {p["code"]}', f'{js_str(ctx.user.get("name", UNDEF))} allocated you to {p["name"]}.', f'/projects/{p["id"]}/team')
    bits = [x for x in [f'customer {cust["client"]}' if cust['mode'] != 'internal' else 'internal', f'{n_tasks} task{"" if n_tasks == 1 else "s"}' if n_tasks else '', 'an escalation matrix' if esc else ''] if x]
    log_activity(ctx, 'project', p['id'], 'project.wizard', f'Created with the New Project wizard: {", ".join(bits)}')
    # (5) an estimate that became this project
    est = b.get('estimate_id')
    if isinstance(est, str) and est:
        from app.routes import estimates

        estimates._convert(Req({'id': est}, {}, {'project_id': p['id']}, UNDEF, ctx))
    p = find(ctx, 'projects', p['id'], 'Project')
    # the costing plan is validated twice (the wizard's own check, then the create rules): one copy of each warning
    seen: set = set()
    ctx.warnings[:] = [w for w in ctx.warnings if not ((w['code'], w['message']) in seen or seen.add((w['code'], w['message'])))]
    return {'project': p, 'duplicate': False, 'tasks': n_tasks}


def _insert_tasks(ctx: Ctx, p: dict, live: list[dict], tasks_by_ms: list[list[dict]]) -> int:
    pj = _pj()
    c = cal(ctx)
    me = ctx.user['id']
    n = 0
    for i, tl in enumerate(tasks_by_ms):
        if i >= len(live) or not tl:
            continue
        m = live[i]

        def one(t: dict, parent: dict | None, m: dict = m) -> dict:
            start = t['start_on'] or (parent['start_on'] if parent else max_iso(ctx.today, m['planned_start']))
            start = min(start, p['end_on'])
            due = t['due_on'] or (parent['due_on'] if parent and parent['due_on'] >= start else add_working_days(start, 2, c))
            if not t['due_on'] and due > m['planned_end'] and m['planned_end'] >= start:
                due = m['planned_end']
            due = min(max(due, start), p['end_on'])
            code = pj._new_task_code(ctx, p['id'], m)
            row = insert(
                ctx,
                'tasks',
                {
                    'project_id': p['id'],
                    'milestone_id': m['id'],
                    'no': code['no'],
                    'wbs_code': code['wbs_code'],
                    'title': t['title'],
                    'owner_id': t['owner_id'],
                    'backup_id': None,
                    'est_hours': t['est_hours'],
                    'start_on': start,
                    'due_on': due,
                    'done_on': None,
                    'complexity': 'medium',
                    'status': 'todo',
                    'description': t['description'],
                    'blocked_reason': None,
                    'kind': 'project',
                    'assigned_by': me,
                    'review_round': 0,
                    'priority': 'normal',
                    'stage': 'new',
                    'progress': 0,
                    'tag': None,
                    'actual_start_on': None,
                    'baseline_due_on': due,
                    'target_revisions': [],
                    'slip_flagged_on': None,
                    'slip_note': None,
                    'parent_task_id': parent['id'] if parent else None,
                },
            )
            log_activity(ctx, 'task', row['id'], 'task.created', f'Added {code["wbs_code"]} {t["title"]} (New Project wizard)')
            if t['owner_id'] and t['owner_id'] != me:
                notify(ctx, t['owner_id'], 'NTF-14', 'Task assigned to you', f'{code["wbs_code"]} {t["title"]} · {js_str(p["name"])}', f'/tasks/{row["id"]}')
            return row

        for t in tl:
            parent = one(t, None)
            n += 1
            for s in t['subtasks']:
                one(s, parent)
                n += 1
        mm = next((x for x in rows(ctx, 'milestones') if x['id'] == m['id']), m)
        patch('milestones', mm, {'empty_since': None})
        sync_milestone_dates(ctx, m['id'], True)
    if n:
        last = max((t['due_on'] for t in rows(ctx, 'tasks') if t['project_id'] == p['id']), default=p['end_on'])
        if last > p['end_on']:
            warn(ctx, 'TASKS_PAST_END', f'Tasks run to {fmt_date(last)}, after the planned end {fmt_date(p["end_on"])}.')
    return n


def get_escalation(req: Req) -> Any:
    ctx = req.ctx
    pj = _pj()
    p = pj._get_project(ctx, req.params['id'])
    e = p.get('escalation') if isinstance(p.get('escalation'), dict) else None
    return {
        'project_id': p['id'],
        'version': p['version'],
        'matrix': e or {'rows': [], 'notes': ''},
        'updated_by_name': user_name(ctx, e.get('updated_by')) if e and e.get('updated_by') else None,
        'can_edit': pj.can_edit(ctx, p),
    }


def put_escalation(req: Req) -> Any:
    ctx = req.ctx
    pj = _pj()
    p = pj._editable_project(ctx, req.params['id'])
    v = validate_escalation(ctx, req.body, 'escalation')
    out = update(ctx, 'projects', p['id'], {'escalation': {**v, 'updated_at': now_iso(), 'updated_by': ctx.user['id']} if v else None}, req.if_match)
    n = len(v['rows']) if v else 0
    log_activity(ctx, 'project', p['id'], 'escalation.saved', f'Updated the escalation matrix ({n} level{"" if n == 1 else "s"})' if v else 'Cleared the escalation matrix')
    req2 = Req({'id': out['id']}, {}, None, UNDEF, ctx)
    return get_escalation(req2)


def register(r: Any) -> None:
    r.post('/projects/wizard/preview', preview)
    r.post('/projects/wizard', create)
    r.get('/projects/:id/escalation', get_escalation)
    r.put('/projects/:id/escalation', put_escalation)
