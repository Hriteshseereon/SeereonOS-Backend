"""Port of src/mock/routes/workflowsV7.ts — v7 workflow management: configurable multi-stage workflows (e.g. Employee
self-appraisal → Manager / TL review → Unit head review → HR review → Finance approval → letter + CTC change).

  • templates are versioned (rev); an instance runs on the snapshot it started with
  • assignees resolve per stage (subject, reporting manager, skip level, department head, role, person, initiator)
  • only the current assignee (role pool members, or an active delegate) can act; fields are validated here
  • every response is filtered per viewer: a field's value is sent only to the stages listed in `visible_to`
    (the owning stage always sees it; the subject sees "subject"/"all" fields only once the workflow completes)
  • SLA: an overdue stage escalates once to the assignee's manager (lazily, when workflows are read)
  • completion actions: issue a document (docgen), send an email (outbox), propose a CTC change through
    maker-checker (entity cost_rate — the same applier as Settings → Cost rates), notify, create a task.

Instances are mutated in place like the TS: `edit()` gives the request a private copy (JS identity semantics), the
helpers (enter_stage, save, run_actions…) change that copy, and the flush writes it.

Exported helpers: can_view (GET /search), start_workflow / appraisal_template / check_routable /
set_appraisal_sync_hook / wf_linked_appraisals / assert_not_workflow_managed (hr.py, people.py), wf_inbox_rows
(company.py), snap_of, display_of, sync_appraisal.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app import ctx as _ctx_mod  # uid() looked up at call time (patchable like insert()'s)
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
    notify,
    now_iso,
    persist,
    require_action,
    rows,
    scope,
    update,
    user_name,
    warn,
)
from app.domain.calendar import add_working_days, fmt_date, fy_label, fy_start_year, is_real_iso_date, month_of, month_range, parse_iso
from app.domain.money import fmt_money
from app.engine.analytics import cal
from app.engine.doc_markup import js_object
from app.engine.docgen import ensure_doc_defaults, issue_document, long_date, standard_vars
from app.engine.email import send_email
from app.engine.maker_checker import eligible_checkers, needs_checker, pending_change_for, propose
from app.frozen import thaw
from app.js import (
    INF,
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_and,
    js_len,
    js_lower,
    js_max,
    js_number,
    js_round,
    js_slice,
    js_sort,
    js_spread,
    js_str,
    js_trim,
    js_truthy,
    js_upper,
    locale_key,
    nullish,
    prop,
    to_fixed,
)
from app.seeds.workflows import ensure_wf_letters, provision_wf_templates

PROCESS_LABEL: Any = cat('mock/routes/workflowsV7')['PROCESS_LABEL']
PROCESSES = list(PROCESS_LABEL.keys())
ASSIGNEES = ['subject', 'reports_to', 'skip_level', 'department_head', 'role', 'user', 'initiator']
ASSIGNEE_LABEL = {
    'subject': 'The employee',
    'reports_to': 'Reporting manager / TL',
    'skip_level': 'Skip-level manager',
    'department_head': 'Unit / department head',
    'role': 'Role',
    'user': 'Specific person',
    'initiator': 'Whoever started it',
}
FIELD_TYPES = ['rating', 'text', 'textarea', 'number', 'percent', 'money', 'select', 'date', 'checkbox', 'file']
ACTION_TYPES = ['generate_document', 'send_email', 'update_ctc', 'notify', 'create_task']
TARGETS = ['subject', 'initiator', 'reports_to', 'participants']
KEY_RE = re.compile(r'^[a-z][a-z0-9_]{1,31}\Z')
_NUMERIC_TYPES = ['number', 'percent', 'money', 'rating']


# =====================================================================================================================
# small JS helpers
# =====================================================================================================================
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
    return o.get(k, UNDEF) if isinstance(o, dict) else UNDEF


def _str(v: Any) -> str:
    """String(v ?? '').trim()"""
    return js_trim(js_str(coalesce(v, '')))


def _arr(v: Any) -> list:
    return v if isinstance(v, list) else []


def _at(lst: Any, i: Any) -> Any:
    """lst[i] (undefined when out of range; never Python's negative indexing)"""
    if isinstance(lst, list) and isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(lst):
        return lst[i]
    return UNDEF


def _u16slice(s: str, a: int, b: int) -> str:
    if all(ord(c) <= 0xFFFF for c in s):
        return js_slice(s, a, b)
    units = s.encode('utf-16-le', 'surrogatepass')
    return units[a * 2 : b * 2].decode('utf-16-le', 'surrogatepass')


def _first(lst: Any, pred: Callable[[Any], Any]) -> Any:
    return next((x for x in lst if pred(x)), None)


def _includes(lst: list, v: Any) -> bool:
    """Array.prototype.includes for a list of strings"""
    return isinstance(v, str) and v in lst


def _canon(v: Any) -> Any:
    """a comparable form of JSON.stringify(v) (object key order ignored: jsonb does not keep it)"""
    if isinstance(v, dict):
        return tuple(sorted((k, _canon(x)) for k, x in v.items() if x is not UNDEF))
    if isinstance(v, (list, tuple)):
        return tuple(None if x is UNDEF else _canon(x) for x in v)
    if isinstance(v, bool):
        return ('b', v)
    if isinstance(v, float) and v != v or v in (INF, -INF):
        return None
    return v


def _split2(s: str) -> tuple[str, Any]:
    """const [a, b] = s.split('.')"""
    parts = s.split('.')
    return parts[0], (parts[1] if len(parts) > 1 else UNDEF)


# =====================================================================================================================
# People
# =====================================================================================================================
def _by_id(ctx: Ctx, table: str, id_: Any) -> dict | None:
    """rows(ctx, table).find(x => x.id === id) (None when id is falsy)"""
    if not js_truthy(id_):
        return None
    if isinstance(id_, (str, int, float)):
        r = find_by(table, 'id', id_)
        if r is not None and r.get('tenant_id') == ctx.tenant_id:
            return r
        if r is None:
            return None
    return _first(rows(ctx, table), lambda x: x.get('id') == id_)


def _user_of(ctx: Ctx, id_: Any) -> dict | None:
    return _by_id(ctx, 'users', id_)


def _invite_of(ctx: Ctx, id_: Any) -> dict | None:
    return _by_id(ctx, 'invites', id_)


def _person(ctx: Ctx, id_: Any) -> dict | None:
    if not js_truthy(id_):
        return None
    u = _user_of(ctx, id_)
    if u:
        return {'id': u['id'], 'name': prop(u, 'name'), 'title': prop(u, 'title'), 'hue': prop(u, 'avatar_hue')}
    inv = _invite_of(ctx, id_)
    if inv:
        return {'id': inv['id'], 'name': prop(inv, 'name'), 'title': 'Joining soon (invited)', 'hue': 200, 'invited': True}
    return None


def _is_active(ctx: Ctx, id_: Any) -> bool:
    if not js_truthy(id_):
        return False
    u = _user_of(ctx, id_)
    return u is not None and u.get('status') == 'active'


def _manager_of(ctx: Ctx, id_: Any) -> Any:
    return coalesce(_q(_user_of(ctx, id_), 'reports_to_id'), _q(_invite_of(ctx, id_), 'reports_to_id'), None)


def _subject_account(ctx: Ctx, id_: Any) -> dict | None:
    """The account behind a subject: the user, or the user an invite turned into once accepted."""
    u = _user_of(ctx, id_)
    if u:
        return u
    inv = _invite_of(ctx, id_)
    if not inv:
        return None
    em = js_lower(inv['email'])
    return _first(rows(ctx, 'users'), lambda x: js_lower(x['email']) == em)


def _base_for(ctx: Ctx) -> Callable[[], dict]:
    def base() -> dict:
        return {'id': _ctx_mod.uid(), 'tenant_id': ctx.tenant_id, 'created_at': now_iso(), 'updated_at': now_iso(), 'version': 1, 'created_by': None}

    return base


def _ensure_provisioned(ctx: Ctx) -> None:
    """New workspaces get the built-in templates on first use (idempotent by key)."""
    if not rows(ctx, 'wf_templates') and ctx.tenant_id != '__none__':
        ensure_doc_defaults(ctx)
        provision_wf_templates(scope(), ctx.tenant_id, _base_for(ctx))
        persist()


# =====================================================================================================================
# Engine
# =====================================================================================================================
def snap_of(ctx: Ctx, i: dict) -> dict:
    s = i.get('snapshot')
    if js_truthy(s):
        return s
    t = _first(rows(ctx, 'wf_templates'), lambda x: x['id'] == i.get('template_id'))
    if t:
        return {'name': t['name'], 'process': t['process'], 'stages': t['stages'], 'on_complete': t['on_complete'], 'on_reject': t['on_reject']}
    return {'name': i.get('title'), 'process': 'general', 'stages': [], 'on_complete': [], 'on_reject': []}


def _snapshot_of(t: dict) -> dict:
    return thaw({'name': t['name'], 'process': t['process'], 'stages': t['stages'], 'on_complete': t['on_complete'], 'on_reject': t['on_reject']})


def _role_holders(ctx: Ctx, key: Any) -> list[dict]:
    tid = ctx.tenant_id
    if isinstance(key, (str, int, float)) or key is None:
        cand = scope().group('users', 'role_key').get(key, [])
    else:
        cand = rows(ctx, 'users')
    hs = [u for u in cand if u.get('tenant_id') == tid and u.get('status') == 'active' and not js_truthy(u.get('is_super_admin')) and u.get('role_key') == key]
    return sorted(hs, key=lambda u: locale_key(u['name']))


def _dept_head(ctx: Ctx, subject_id: Any, exclude: set) -> Any:
    deps = rows(ctx, 'departments')
    u = _user_of(ctx, subject_id) or _user_of(ctx, _manager_of(ctx, subject_id))
    did = _q(u, 'department_id')
    d = _first(deps, lambda x: x.get('id') == did) if u is not None else None
    n = 0
    while d is not None and n < 10:
        h = d.get('head_id')
        if js_truthy(h) and h not in exclude and _is_active(ctx, h):
            return h
        pid = d.get('parent_id')
        d = _first(deps, lambda x, p=pid: x.get('id') == p) if js_truthy(pid) else None
        n += 1
    return None


def _resolve_stage(ctx: Ctx, st: dict, subject_id: Any, initiator_id: Any, prior: list) -> dict | None:
    subj = _person(ctx, subject_id)
    s_name = js_str(coalesce(_q(subj, 'name'), 'the employee'))

    def one(id_: Any, why: str) -> dict:
        return {'primary': id_, 'pool': [id_], 'why': ''} if js_truthy(id_) and _is_active(ctx, id_) else {'primary': None, 'pool': [], 'why': why}

    a = st.get('assignee')
    if a == 'subject':
        if not js_truthy(subject_id):
            return one(None, 'the workflow has no employee')
        if subj is not None and subj.get('invited'):
            return one(None, f'{s_name} has not accepted the invite yet')
        return one(subject_id, f'{s_name} is not active')
    if a == 'initiator':
        return one(initiator_id, 'the person who started it is no longer active')
    if a == 'reports_to':
        m = _manager_of(ctx, subject_id)
        return one(m if js_truthy(m) and m != subject_id else None, f'{s_name} has no reporting manager')
    if a == 'skip_level':
        m = _manager_of(ctx, subject_id)
        s = _manager_of(ctx, m)
        return one(s if js_truthy(s) and s != subject_id else None, f'{s_name}’s manager has no manager')
    if a == 'department_head':
        # the unit's head; skip the employee and anyone who already reviewed (climb to the parent unit); if no one is
        # left, accept a head who already reviewed, then the employee's own manager (people who head their own unit)
        self_ = [subject_id] if js_truthy(subject_id) else []
        m = _manager_of(ctx, subject_id)
        h = _dept_head(ctx, subject_id, {*self_, *prior})
        if h is None:
            h = _dept_head(ctx, subject_id, set(self_))
        if h is None:
            h = m if js_truthy(m) and m != subject_id and _is_active(ctx, m) else None
        return one(h, f'no department head is set for {s_name}’s unit (or its parent units) and they have no manager')
    if a == 'role':
        rk = st.get('role_key')
        role = _first(rows(ctx, 'roles'), lambda r: r.get('key') == rk)
        if not role or not js_truthy(role.get('active')):
            return one(None, f'the role “{js_str(coalesce(rk, "—"))}” does not exist')
        holders = _role_holders(ctx, rk)
        pool = [u['id'] for u in holders if u['id'] != subject_id]
        if not pool:
            m = _manager_of(ctx, subject_id)
            if holders and js_truthy(m) and _is_active(ctx, m):
                return {'primary': m, 'pool': [m], 'why': ''}
            return one(None, f'nobody holds the {js_str(role.get("name", UNDEF))} role')
        return {'primary': pool[0], 'pool': pool, 'why': ''}
    if a == 'user':
        u_id = st.get('user_id')
        return one(
            u_id if js_truthy(u_id) and u_id != subject_id else None,
            f'{s_name} can’t review their own workflow' if u_id == subject_id else 'the chosen person is not active',
        )
    return None  # (no case matched: undefined in the TS)


def _last_actor(i: dict, stage_key: Any) -> Any:
    for h in reversed(i['history']):
        if h.get('stage_key') == stage_key and h.get('action') in ('submitted', 'approved'):
            return h.get('by')
    return None


def _chain_for(ctx: Ctx, stages: list, subject_id: Any, initiator_id: Any, i: dict | None = None) -> list:
    """Resolve every stage in order. Done stages use their real actor; a unit head who already reviewed is skipped upward."""
    prior: list = []
    out = []
    for idx, st in enumerate(stages):
        w = _resolve_stage(ctx, st, subject_id, initiator_id, prior)
        actual = _last_actor(i, st.get('key')) if i is not None and idx < i['current_stage'] else None
        if actual is not None:
            used = actual
        elif i is not None and idx == i['current_stage']:
            used = i.get('current_assignee_id')
        else:
            used = _g(w, 'primary')
        if js_truthy(used):
            prior.append(used)
        out.append(w)
    return out


def _delegators_of(ctx: Ctx, me: str) -> set:
    t = ctx.today
    return {
        d.get('from_user_id')
        for d in rows(ctx, 'delegations')
        if d.get('to_user_id') == me and d.get('from_on') <= t and d.get('to_on') >= t and not js_truthy(d.get('project_id'))
    }


def _acting_as(ctx: Ctx, i: dict) -> str | None:
    if i.get('status') != 'in_progress' or ctx.view_as:
        return None
    me = ctx.user_id
    cur = i.get('current_assignee_id')
    if cur == me:
        return 'assignee'
    s = snap_of(ctx, i)
    st = _at(s['stages'], i.get('current_stage'))
    if (
        _q(st, 'assignee') == 'role'
        and me != i.get('subject_user_id')
        and any(u['id'] == me for u in _role_holders(ctx, _q(st, 'role_key') if st is not UNDEF else UNDEF))
    ):
        return 'pool'
    if js_truthy(cur) and cur in _delegators_of(ctx, me) and me != i.get('subject_user_id'):
        return 'delegate'
    return None


def _is_participant(ctx: Ctx, i: dict) -> bool:
    me = ctx.user_id
    return (
        i.get('subject_user_id') == me
        or i.get('initiated_by') == me
        or i.get('current_assignee_id') == me
        or any(h.get('by') == me or h.get('to', UNDEF) == me for h in i['history'])
        or js_truthy(_acting_as(ctx, i))
    )


def can_view(ctx: Ctx, i: dict) -> bool:
    return _is_participant(ctx, i) or has_any(ctx, 'workflows.design', 'workflows.start')


def _viewer_stages(ctx: Ctx, i: dict) -> set:
    me = ctx.user_id
    s = set()
    for h in i['history']:
        if h.get('by') == me and h.get('action') in ('submitted', 'approved'):
            s.add(h.get('stage_key'))
    snap = snap_of(ctx, i)
    if _acting_as(ctx, i):
        s.add(_q(_at(snap['stages'], i.get('current_stage')), 'key'))
    return s


def _field_visible(f: dict, stage_key: Any, vs: set, is_subject: bool, completed: bool) -> bool:
    if stage_key in vs:
        return True
    vt = f.get('visible_to') or []
    if 'all' in vt:
        return True
    if is_subject:
        return completed and 'subject' in vt
    return any(k in vs for k in vt)


def _ref_value(values: Any, path: Any) -> Any:
    p = _str(path)
    if '.' not in p:
        return UNDEF
    s, k = _split2(p)
    v = values.get(s, UNDEF) if isinstance(values, dict) else UNDEF
    return v.get(k, UNDEF) if isinstance(v, dict) else UNDEF


def _num_or_null(v: Any) -> Any:
    if v is None or v is UNDEF or v == '' and isinstance(v, str):
        return None
    n = js_number(v)
    return n if is_finite(n) else None


def _field_of(snap: dict, path: Any) -> dict | None:
    s, k = _split2(_str(path))
    st = _first(snap['stages'], lambda x: x.get('key') == s)
    if not st:
        return None
    return _first(st.get('fields') or [], lambda x: x.get('key') == k)


def _fmt_date_long(s: str) -> str:
    if not s:
        return '—'
    return fmt_date(s, 'long') if parse_iso(s) is not None else 'Invalid Date'


def display_of(f: dict, v: Any) -> str:
    if v is None or v is UNDEF or v == '' and isinstance(v, str):
        return '—'
    t = f.get('type')
    if t == 'rating':
        return f'{js_str(v)} / 5'
    if t == 'percent':
        return f'{js_str(v)}%'
    if t == 'money':
        return fmt_money(js_number(v))
    if t == 'date':
        return _fmt_date_long(js_str(v))
    if t == 'checkbox':
        return 'Yes' if js_truthy(v) else 'No'
    if t == 'file':
        return js_str(coalesce(_q(v, 'name'), 'File'))
    return js_str(v)


def _label_lower(s: Any) -> str:
    return js_lower(js_str(s))


# ---------------- validation of a stage's submission ----------------
def _clean_values(snap: dict, st: dict, raw: Any, values: dict) -> dict:
    out: dict[str, Any] = {}
    for f in st['fields']:
        key = f.get('key')
        ftype = f.get('type')
        label = js_str(f.get('label', UNDEF))
        v = raw.get(key, UNDEF) if isinstance(raw, dict) else UNDEF
        empty = (
            v is UNDEF
            or v is None
            or (isinstance(v, str) and v == '')
            or (ftype == 'checkbox' and v is not True)
            or (ftype == 'file' and not js_truthy(_q(v, 'name')))
        )
        if js_truthy(f.get('required')) and empty:
            msg = f'Tick “{label}” before you submit.' if ftype == 'checkbox' else f'Attach “{label}”.' if ftype == 'file' else f'Fill in “{label}”.'
            err(422, 'VALIDATION', msg, key)
        if empty:
            out[key] = False if ftype == 'checkbox' else None
            continue
        fmin = f.get('min', UNDEF)
        fmax = f.get('max', UNDEF)
        if ftype == 'rating':
            n = js_number(v)
            lo = coalesce(fmin, 1)
            hi = coalesce(fmax, 5)
            if not is_finite(n) or n < lo or n > hi or js_round(n * 2) / 2 != n:
                err(422, 'VALIDATION', f'{label}: pick a rating from {js_str(lo)} to {js_str(hi)}.', key)
            out[key] = n
        elif ftype in ('number', 'percent'):
            n = js_number(v)
            lo = coalesce(fmin, 0 if ftype == 'percent' else -INF)
            hi = coalesce(fmax, 100 if ftype == 'percent' else INF)
            if not is_finite(n):
                err(422, 'VALIDATION', f'{label} must be a number.', key)
            if n < lo or n > hi:
                err(422, 'VALIDATION', f'{label} must be between {js_str(lo)} and {js_str(hi)}{"%" if ftype == "percent" else ""}.', key)
            out[key] = js_round(n * 100) / 100
        elif ftype == 'money':
            n = js_number(v)
            if not is_integer(n) or n < 0:
                err(422, 'MONEY_INVALID', f'{label}: enter an amount of zero or more.', key)
            if not nullish(fmin) and n < fmin:
                err(422, 'VALIDATION', f'{label} must be at least {fmt_money(fmin)}.', key)
            if not nullish(fmax) and n > fmax:
                err(422, 'VALIDATION', f'{label} must be at most {fmt_money(fmax)}.', key)
            out[key] = n
        elif ftype == 'select':
            sv = js_str(v)
            if sv not in (coalesce(f.get('options', UNDEF), []) or []):
                err(422, 'VALIDATION', f'{label}: choose one of the listed options.', key)
            out[key] = sv
        elif ftype == 'date':
            sv = js_str(v)
            if not is_real_iso_date(sv):
                err(422, 'VALIDATION', f'{label}: choose a valid date.', key)
            out[key] = sv
        elif ftype == 'checkbox':
            out[key] = True
        elif ftype == 'file':
            size = js_number(_q(v, 'size'))
            out[key] = {'name': _u16slice(_str(_q(v, 'name')), 0, 160), 'size': size if js_truthy(size) else 0}
        else:
            s = _str(v)
            max_len = 5000 if ftype == 'textarea' else 300
            if js_truthy(fmin) and js_len(s) < fmin:
                err(422, 'VALIDATION', f'{label}: write at least {js_str(fmin)} characters.', key)
            if js_len(s) > max_len:
                err(422, 'VALIDATION', f'{label} is too long (max {max_len} characters).', key)
            out[key] = s
    # caps from earlier stages, overridable with a written reason
    for f in st['fields']:
        key = f.get('key')
        mf = f.get('max_from')
        ov = out.get(key, UNDEF)
        if not js_truthy(mf) or ov is None or ov is UNDEF:
            continue
        cap = _num_or_null(_ref_value(values, mf))
        if cap is None or js_number(ov) <= cap:
            continue
        ofk = f.get('override_field')
        reason_field = _first(st['fields'], lambda x, ofk=ofk: x.get('key') == ofk) if js_truthy(ofk) else None
        reason = _str(out.get(reason_field['key'], UNDEF)) if reason_field else ''
        cf = _field_of(snap, mf)
        cap_label = js_str(coalesce(_q(cf, 'label'), mf))
        label = js_str(f.get('label', UNDEF))
        if not reason_field:
            err(422, 'OVER_CAP', f'{label} ({js_str(ov)}) can’t exceed {js_lower(cap_label)} ({js_str(cap)}).', key)
        if js_len(reason) < 10:
            err(
                422,
                'OVERRIDE_REASON',
                f'{label} ({js_str(ov)}%) is above the {js_lower(cap_label)} ({js_str(cap)}%). Explain why in “{js_str(reason_field.get("label", UNDEF))}” (at least 10 characters).',
                reason_field['key'],
            )
    return out


# ---------------- compensation facts (server-computed) ----------------
def _rate_on(ctx: Ctx, user_id: Any, d: str) -> dict | None:
    best = None
    for r in rows(ctx, 'cost_rates'):
        if r.get('user_id') == user_id and r['effective_from'] <= d and (best is None or r['effective_from'] > best['effective_from']):
            best = r
    return best


def _ctc_action(snap: dict) -> dict | None:
    a = _first(snap['on_complete'], lambda x: x.get('type') == 'update_ctc')
    if not a:
        return None
    c = coalesce(a.get('config', UNDEF), {})
    return c if isinstance(c, dict) else {}


def _gt(a: Any, b: Any) -> bool:
    return isinstance(b, (int, float)) and not isinstance(b, bool) and a > b


def _lt(a: Any, b: Any) -> bool:
    return isinstance(b, (int, float)) and not isinstance(b, bool) and a < b


def _compute_facts(ctx: Ctx, i: dict, values: dict, limited: bool) -> dict | None:
    snap = snap_of(ctx, i)
    cfg = _ctc_action(snap)
    u = _subject_account(ctx, i.get('subject_user_id'))
    if cfg is None or not u:
        return None
    cur = _rate_on(ctx, u['id'], ctx.today)
    rec = _num_or_null(_ref_value(values, cfg.get('recommended_field', UNDEF)))
    endo = _num_or_null(_ref_value(values, cfg.get('endorsed_field', UNDEF)))
    norm = _num_or_null(_ref_value(values, cfg.get('normalised_field', UNDEF)))
    appr = _num_or_null(_ref_value(values, cfg.get('hike_field', UNDEF)))
    pick = [('approved', appr), ('normalised', norm), ('endorsed', endo), ('recommended', rec)]
    basis = next((p for p in pick if p[1] is not None), None)
    new_ctc = js_round(cur['monthly_ctc_paise'] * (1 + basis[1] / 100)) if cur and basis else None
    eff = _ref_value(values, cfg.get('effective_field', UNDEF))
    g = _first(rows(ctx, 'grades'), lambda x: x['id'] == u.get('grade_id'))
    annual = new_ctc * 12 if new_ctc is not None else None
    band = None
    if g and annual is not None:
        mx = g.get('max_ctc_paise', UNDEF)
        mn = g.get('min_ctc_paise', UNDEF)
        status = 'above' if (mx is not None and _gt(annual, mx)) else 'below' if (mn is not None and _lt(annual, mn)) else 'within'
        band = {'grade': f'{js_str(g.get("code", UNDEF))} · {js_str(g.get("name", UNDEF))}', 'min_paise': mn, 'max_paise': mx, 'status': status}
    cur_m = cur['monthly_ctc_paise'] if cur else None
    f = {
        'current_ctc_paise': coalesce(cur_m, None) if cur else None,
        'current_annual_paise': cur_m * 12 if cur else None,
        'current_from': coalesce(prop(cur, 'effective_from'), None) if cur else None,
        'recommended_pct': rec,
        'endorsed_pct': endo,
        'normalised_pct': norm,
        'approved_pct': appr,
        'basis': basis[0] if basis else None,
        'basis_pct': basis[1] if basis else None,
        'new_ctc_paise': new_ctc,
        'new_annual_paise': annual,
        'monthly_increase_paise': new_ctc - cur_m if new_ctc is not None and cur else None,
        'annual_impact_paise': (new_ctc - cur_m) * 12 if new_ctc is not None and cur else None,
        'effective_from': eff if isinstance(eff, str) else None,
        'band': band,
        'limited': limited,
    }
    if limited:
        f.update(
            {
                'recommended_pct': None,
                'endorsed_pct': None,
                'normalised_pct': None,
                'monthly_increase_paise': None,
                'annual_impact_paise': None,
                'band': None,
                'current_ctc_paise': None,
                'current_annual_paise': None,
                'current_from': None,
            }
        )
    return f


def _facts_for(ctx: Ctx, i: dict, vs: set, values: dict | None = None) -> dict | None:
    if values is None:
        values = i['values']
    snap = snap_of(ctx, i)
    comp = any(js_truthy(s.get('sees_compensation')) and s.get('key') in vs for s in snap['stages'])
    if comp:
        return _compute_facts(ctx, i, values, False)
    if i.get('subject_user_id') == ctx.user_id and i.get('status') == 'completed':
        return _compute_facts(ctx, i, values, True)
    return None


# ---------------- variables for letters and emails ----------------
def _vars_for(ctx: Ctx, i: dict, mapping: Any) -> dict[str, Any]:
    snap = snap_of(ctx, i)
    ictx = i.get('context') or {}
    out: dict[str, Any] = {'workflow_title': i.get('title'), 'cycle_name': _str(_q(ictx, 'cycle_name'))}
    values = i['values']
    for st in snap['stages']:
        sv = values.get(st['key'], UNDEF) if isinstance(values, dict) else UNDEF
        for f in st['fields']:
            out[f'{js_str(st["key"])}_{js_str(f["key"])}'] = display_of(f, _q(sv, f['key']))
    for k, path in js_spread(coalesce(mapping, {})).items():
        f = _field_of(snap, path)
        v = _ref_value(values, path)
        empty = v is None or v is UNDEF or (isinstance(v, str) and v == '')
        if not f or empty:
            out[k] = ''
        elif f.get('type') in ('percent', 'number'):
            out[k] = js_str(v)
        elif f.get('type') == 'date':
            out[k] = long_date(js_str(v))
        else:
            out[k] = display_of(f, v)
    if js_truthy(out.get('cycle_name')) and not js_truthy(out.get('appraisal_period', UNDEF)):
        out['appraisal_period'] = out['cycle_name']
    # started without an appraisal cycle: name the period after the financial year it was started in
    if not js_truthy(out.get('appraisal_period', UNDEF)):
        settings = ctx.tenant.get('settings') or {}
        out['appraisal_period'] = fy_label(fy_start_year(i['created_at'][:10], coalesce(settings.get('company.fy_start_month', UNDEF), 4)))
    acc = _subject_account(ctx, i.get('subject_user_id'))
    if acc:
        std = standard_vars(ctx, acc['id'])
        if not js_truthy(out.get('new_designation', UNDEF)):
            out['new_designation'] = std['designation']
        out['last_designation'] = std['designation']
        rate = _rate_on(ctx, acc['id'], ctx.today)
        if rate:
            out['ctc_annual'] = fmt_money(rate['monthly_ctc_paise'] * 12)
            out['ctc_monthly'] = fmt_money(rate['monthly_ctc_paise'])
        shifts = rows(ctx, 'shifts')
        shift = _first(shifts, lambda x: x['id'] == acc.get('shift_id')) or _first(
            shifts, lambda x: js_truthy(x.get('is_default')) and js_truthy(x.get('active'))
        )
        if shift:
            out['working_hours'] = f'{js_str(shift.get("start_time", UNDEF))} – {js_str(shift.get("end_time", UNDEF))}'
            out['reporting_time'] = shift.get('start_time', UNDEF)
        facts = _compute_facts(ctx, i, values, False)
        if facts:
            out['old_ctc'] = fmt_money(facts['current_annual_paise'])
            out['current_monthly_ctc'] = fmt_money(facts['current_ctc_paise'])
            if facts['new_ctc_paise'] is not None:
                out['new_ctc'] = fmt_money(facts['new_annual_paise'])
                out['new_monthly_ctc'] = fmt_money(facts['new_ctc_paise'])
            if not js_truthy(out.get('hike_pct', UNDEF)) and facts['basis_pct'] is not None:
                out['hike_pct'] = js_str(facts['basis_pct'])
            if not js_truthy(out.get('effective_from', UNDEF)) and js_truthy(facts['effective_from']):
                out['effective_from'] = long_date(facts['effective_from'])
    else:
        inv = _invite_of(ctx, i.get('subject_user_id'))
        if inv:
            out['employee_name'] = inv.get('name', UNDEF)
            out['employee_email'] = inv.get('email', UNDEF)
    return js_object(out)


def _targets_of(ctx: Ctx, i: dict, to: Any) -> list:
    t = _str(to) or 'subject'
    acc_u = _subject_account(ctx, i.get('subject_user_id'))
    acc = acc_u['id'] if acc_u else None
    if t == 'subject':
        return [acc] if acc else []
    if t == 'initiator':
        return [i.get('initiated_by')]
    if t == 'reports_to':
        m = _manager_of(ctx, i.get('subject_user_id'))
        return [m] if js_truthy(m) else []
    if t == 'participants':
        seen: list = []
        for x in [i.get('initiated_by'), *(h.get('by') for h in i['history']), *([acc] if acc else [])]:
            if x not in seen:
                seen.append(x)
        return [x for x in seen if _is_active(ctx, x)]
    if t.startswith('role:'):
        return [u['id'] for u in _role_holders(ctx, t[5:])]
    return []


def _when_ok(i: dict, a: dict) -> bool:
    w = _q(a.get('config', UNDEF), 'when')
    if not js_truthy(_q(w, 'field')):
        return True
    return js_str(coalesce(_ref_value(i['values'], w['field']), '')) == js_str(coalesce(_q(w, 'equals'), ''))


# ---------------- completion actions ----------------
def _locked_months(ctx: Ctx) -> set:
    return {p.get('month') for p in rows(ctx, 'payroll_runs') if p.get('status') == 'final'}


def _ctc_plan(ctx: Ctx, i: dict, a: dict) -> dict[str, Any]:
    """Mirrors the Settings → Cost rates rules so a completion never half-applies (the applier re-checks on approval)."""
    snap = snap_of(ctx, i)
    cfg = coalesce(a.get('config', UNDEF), {})
    if not isinstance(cfg, dict):
        cfg = {}
    u = _subject_account(ctx, i.get('subject_user_id'))
    if not u:
        err(409, 'WF_NO_ACCOUNT', 'The employee has no SeereonOS account yet, so their CTC can’t be changed.')
    hf = cfg.get('hike_field', UNDEF)
    ef = cfg.get('effective_field', UNDEF)
    hike = _num_or_null(_ref_value(i['values'], hf))
    eff = _str(_ref_value(i['values'], ef))
    if hike is None:
        err(409, 'WF_CTC_INPUT', f'The CTC change needs “{js_str(coalesce(_q(_field_of(snap, hf), "label"), hf))}”.')
    if not is_real_iso_date(eff):
        err(409, 'WF_CTC_INPUT', f'The CTC change needs “{js_str(coalesce(_q(_field_of(snap, ef), "label"), ef))}”.')
    hist = [x for x in rows(ctx, 'cost_rates') if x.get('user_id') == u['id']]
    cur = _rate_on(ctx, u['id'], ctx.today)
    if cur is None:
        srt = sorted(hist, key=lambda x: locale_key(x['effective_from']))
        cur = srt[0] if srt else None
    name = js_str(u.get('name', UNDEF))
    if not cur:
        err(409, 'WF_NO_RATE', f'{name} has no current cost rate. Add one under Settings → Cost rates first.')
    locked = _locked_months(ctx)
    srt_locked = js_sort(list(locked))
    last_locked = srt_locked[-1] if srt_locked else UNDEF
    if month_of(eff) in locked or (js_truthy(last_locked) and eff <= month_range(last_locked)['to']):
        err(
            409,
            'RATE_PERIOD_LOCKED',
            f'Payroll up to {fmt_date(month_range(last_locked)["to"])} is final. Send back to HR to choose a later effective date.',
            'effective_date',
        )
    if any(x.get('effective_from') == eff for x in hist):
        err(409, 'RATE_EXISTS', f'{name} already has a rate starting {fmt_date(eff)}. Send back to HR to pick another effective date.')
    efs = js_sort([x.get('effective_from') for x in hist])
    latest = efs[-1] if efs else UNDEF
    if js_truthy(latest) and eff < latest:
        err(409, 'RATE_PERIOD_LOCKED', f'{name}’s latest rate starts {fmt_date(latest)}; the effective date must be later.')
    if pending_change_for(ctx, 'cost_rate', u['id']):
        err(409, 'CHANGE_PENDING', f'A salary change for {name} is already waiting for a checker. Decide it under Change approvals first.')
    if needs_checker(ctx, 'cost_rate') and not eligible_checkers(ctx, 'cost_rates.edit', ctx.user_id):
        err(
            409,
            'NO_CHECKER',
            'Maker-checker is on for salaries, but no one else can approve this CTC change. Give a second person “Check changes” and “Change salaries & cost rates”.',
        )
    ctc = js_round(cur['monthly_ctc_paise'] * (1 + hike / 100))
    return {'u': u, 'cur': cur, 'hike': hike, 'eff': eff, 'ctc': ctc, 'base_version': len(hist)}


def _preflight(ctx: Ctx, i: dict, actions: list) -> None:
    for a in actions:
        if not _when_ok(i, a):
            continue
        t = a.get('type')
        if t == 'update_ctc':
            _ctc_plan(ctx, i, a)
        if t == 'generate_document':
            if not _subject_account(ctx, i.get('subject_user_id')):
                who = js_str(coalesce(_q(_person(ctx, i.get('subject_user_id')), 'name'), 'The employee'))
                err(409, 'WF_NO_ACCOUNT', f'{who} has not accepted their invite yet, so the letter can’t be issued. Complete this step after they sign in.')
            ensure_doc_defaults(ctx)
            tk = a.get('template_key', UNDEF)
            if not any(x.get('key') == tk and js_truthy(x.get('active')) for x in rows(ctx, 'doc_templates')):
                ensure_wf_letters(scope(), ctx.tenant_id, _base_for(ctx))
                if not any(x.get('key') == tk and js_truthy(x.get('active')) for x in rows(ctx, 'doc_templates')):
                    err(409, 'TEMPLATE_MISSING', f'The document template “{js_str(tk)}” is missing or inactive. Restore it under Documents → Templates.')
        if t == 'send_email' and not any(x.get('key') == a.get('template_key', UNDEF) for x in rows(ctx, 'email_templates')):
            ensure_wf_letters(scope(), ctx.tenant_id, _base_for(ctx))


def _run_actions(ctx: Ctx, i: dict, actions: list) -> None:
    snap = snap_of(ctx, i)
    link = f'/workflows/run/{i["id"]}'
    ictx = i.get('context') or {}
    cyc = _q(ictx, 'cycle_name')
    for a in actions:
        if not _when_ok(i, a):
            continue
        cfg = coalesce(a.get('config', UNDEF), {})
        cfg = cfg if isinstance(cfg, dict) else {}
        t = a.get('type')
        if t == 'update_ctc':
            p = _ctc_plan(ctx, i, a)
            vp = js_str(p['cur'].get('variable_pct', UNDEF))

            def show(c: Any, frm: Any, vp: str = vp) -> dict:
                return {'Monthly CTC': fmt_money(c), 'Variable pay': f'{vp}%', 'Effective from': frm}

            uname = js_str(p['u'].get('name', UNDEF))
            res = propose(
                ctx,
                {
                    'entity': 'cost_rate',
                    'entity_id': p['u']['id'],
                    'action': 'create',
                    'required_permission': 'cost_rates.edit',
                    'base_version': p['base_version'],
                    'summary': f'New rate for {uname}: {fmt_money(p["ctc"])}/month from {p["eff"]} (+{to_fixed(p["hike"], 1)}%) — {js_str(snap["name"])}{" " + _str(cyc) if js_truthy(cyc) else ""}',
                    'before': show(p['cur']['monthly_ctc_paise'], p['cur'].get('effective_from', UNDEF)),
                    'after': show(p['ctc'], p['eff']),
                    'payload': {
                        'op': 'add',
                        'user_id': p['u']['id'],
                        'monthly_ctc_paise': p['ctc'],
                        'variable_pct': p['cur'].get('variable_pct', UNDEF),
                        'sales_target_paise': None,
                        'effective_from': p['eff'],
                        'source': 'workflow',
                        'wf_instance_id': i['id'],
                    },
                },
            )
            if res['pending']:
                i['outputs'].append(
                    {'kind': 'cost_rate_change', 'ref_id': res['change']['id'], 'label': f'New CTC {fmt_money(p["ctc"])}/month from {fmt_date(p["eff"])}'}
                )
            else:
                i['outputs'].append(
                    {
                        'kind': 'cost_rate',
                        'ref_id': coalesce(res.get('result_id', UNDEF), ''),
                        'label': f'New CTC {fmt_money(p["ctc"])}/month from {fmt_date(p["eff"])} (applied)',
                    }
                )
        elif t == 'generate_document':
            acc = _subject_account(ctx, i.get('subject_user_id'))
            vars_ = _vars_for(ctx, i, cfg.get('vars', UNDEF))
            tk = a.get('template_key', UNDEF)
            tpl = _first(rows(ctx, 'doc_templates'), lambda x, tk=tk: x.get('key') == tk and js_truthy(x.get('active')))
            title = f'{js_str(_g(tpl, "name"))}{" " + _str(cyc) if js_truthy(cyc) else ""} — {js_str(_g(acc, "name"))}'
            # when the workflow also emails the employee itself, skip the generic "document issued" email (one message, not two)
            own_email = any(
                x.get('type') == 'send_email' and _when_ok(i, x) and acc['id'] in _targets_of(ctx, i, _q(x.get('config', UNDEF), 'to')) for x in actions
            )
            d = issue_document(
                ctx,
                {
                    'template_key': tk,
                    'employee_id': acc['id'],
                    'vars': vars_,
                    'title': title,
                    'period': coalesce(cyc, None),
                    'source': {'kind': 'workflow', 'ref_id': i['id']},
                    'notify': not own_email,
                },
            )
            i['outputs'].append({'kind': 'document', 'ref_id': d['id'], 'label': d['title']})
            if own_email:
                notify(
                    ctx,
                    acc['id'],
                    'NTF-14',
                    f'{js_str(tpl["name"])} issued',
                    f'{js_str(d["title"])} is in Documents → My documents. Open it and acknowledge.',
                    f'/documents/issued/{d["id"]}',
                )
        elif t == 'send_email':
            vars_ = _vars_for(ctx, i, cfg.get('vars', UNDEF))
            for uid2 in _targets_of(ctx, i, cfg.get('to', UNDEF)):
                u = _user_of(ctx, uid2)
                if not u:
                    continue
                tk = a.get('template_key', UNDEF)
                m = send_email(
                    ctx,
                    tk,
                    {'email': u.get('email', UNDEF), 'user_id': u['id']},
                    {**standard_vars(ctx, u['id']), **vars_, 'employee_name': coalesce(vars_.get('employee_name', UNDEF), u.get('name', UNDEF))},
                    {'entity': 'wf_instance', 'id': i['id']},
                )
                if m:
                    i['outputs'].append({'kind': 'email', 'ref_id': m['id'], 'label': f'{js_str(m["subject"])} → {js_str(u.get("email", UNDEF))}'})
                else:
                    warn(ctx, 'EMAIL_TEMPLATE_MISSING', f'No active email template “{js_str(tk)}”, so no email was sent.')
        elif t == 'notify':
            for tg in _targets_of(ctx, i, cfg.get('to', UNDEF)):
                msg = _str(cfg.get('message', UNDEF)) or f'{js_str(snap["name"])} is {"rejected" if i.get("status") == "rejected" else "complete"}.'
                notify(ctx, tg, 'NTF-05', i['title'], msg, link)
        elif t == 'create_task':
            project = f'p_general_{js_str(ctx.tenant.get("slug", UNDEF))}'
            if not any(p['id'] == project for p in rows(ctx, 'projects')):
                continue
            dd = js_number(cfg.get('due_days', UNDEF))
            due = add_working_days(ctx.today, js_max(1, dd if js_truthy(dd) else 3), cal(ctx))
            for owner in _targets_of(ctx, i, cfg.get('to', UNDEF)):
                nos = [x['no'] for x in rows(ctx, 'tasks') if x.get('project_id') == project and not js_truthy(x.get('milestone_id'))]
                no = js_max(0, *nos) + 1
                title = _str(cfg.get('title', UNDEF)) or f'Follow up: {js_str(i["title"])}'
                tk_ = insert(
                    ctx,
                    'tasks',
                    {
                        'project_id': project,
                        'milestone_id': None,
                        'no': no,
                        'wbs_code': f'0.{js_str(no)}',
                        'title': title,
                        'owner_id': owner,
                        'backup_id': None,
                        'est_hours': 2,
                        'start_on': ctx.today,
                        'due_on': due,
                        'done_on': None,
                        'complexity': 'low',
                        'status': 'todo',
                        'description': f'Created by the workflow “{js_str(i["title"])}”.',
                        'blocked_reason': None,
                        'kind': 'general',
                        'assigned_by': ctx.user_id,
                        'reviewer_id': i.get('initiated_by') if i.get('initiated_by') != owner else _manager_of(ctx, owner),
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
                    },
                )
                i['outputs'].append({'kind': 'task', 'ref_id': tk_['id'], 'label': f'Task “{title}” for {user_name(ctx, owner)}'})
                notify(ctx, owner, 'NTF-14', 'Task assigned to you', f'{title} · due {fmt_date(due)}', f'/my-tasks?task={tk_["id"]}')


# ---------------- transitions ----------------
def _enter_stage(ctx: Ctx, i: dict, idx: int, who: Any) -> None:
    snap = snap_of(ctx, i)
    st = snap['stages'][idx]
    i['current_stage'] = idx
    i['current_assignee_id'] = who
    i['stage_due_on'] = add_working_days(ctx.today, js_max(1, st['sla_days']), cal(ctx))
    i['stage_started_at'] = now_iso()
    i['escalated_stage'] = None
    if who != ctx.user_id:
        notify(
            ctx,
            who,
            'NTF-04',
            f'{js_str(st["name"])} waiting for you',
            f'{js_str(i["title"])} · due {fmt_date(i["stage_due_on"])}',
            f'/workflows/run/{i["id"]}',
        )


_appraisal_hook: Callable[[Ctx, Any], None] | None = None


def set_appraisal_sync_hook(fn: Callable[[Ctx, Any], None]) -> None:
    """hr.py registers its cycle-status sync here (avoids a circular import)."""
    global _appraisal_hook
    _appraisal_hook = fn


def sync_appraisal(ctx: Ctx, i: dict) -> None:
    """Mirror an appraisal workflow onto the linked appraisal record so the Appraisals screens stay correct."""
    aid = _str(_q(i.get('context'), 'appraisal_id'))
    if not aid:
        return
    a = _first(rows(ctx, 'appraisals'), lambda x: x['id'] == aid)
    if not a:
        return
    snap = snap_of(ctx, i)
    stages = snap['stages']
    values = i['values']

    def stage_by(pred: Callable[[dict], bool]) -> dict | None:
        return _first(stages, pred)

    self_st = stage_by(lambda s: s.get('assignee') == 'subject')
    mgr_st = stage_by(lambda s: s.get('assignee') in ('reports_to', 'skip_level'))
    hr_st = stage_by(lambda s: s.get('assignee') == 'role' and isinstance(s.get('role_key'), str) and s['role_key'].startswith('hr')) or stage_by(
        lambda s: s.get('sees_compensation') is True
    )

    def vals_of(st: dict) -> Any:
        v = values.get(st['key'], UNDEF) if isinstance(values, dict) else UNDEF
        return coalesce(v, {})

    def rating(st: dict | None, prefer: re.Pattern[str]) -> Any:
        if not st:
            return None
        vals = vals_of(st)
        rs = [f for f in st['fields'] if f.get('type') == 'rating' and _num_or_null(_q(vals, f['key'])) is not None]
        p = _first(rs, lambda f: prefer.search(f['key']))
        if p:
            return js_number(_q(vals, p['key']))
        if not rs:
            return None
        total = 0
        for f in rs:
            total = total + js_number(_q(vals, f['key']))
        return js_round((total / len(rs)) * 2) / 2

    def text(st: dict | None) -> str:
        if not st:
            return ''
        f = _first(st['fields'], lambda x: x.get('type') == 'textarea' and js_truthy(x.get('required'))) or _first(
            st['fields'], lambda x: x.get('type') == 'textarea' and 'private' not in js_str(x.get('key'))
        )
        return _str(_q(_q(values, st['key']), f['key'])) if f else ''

    def at(st: dict | None) -> Any:
        if not st:
            return None
        for h in reversed(i['history']):
            if h.get('stage_key') == st['key'] and h.get('action') in ('submitted', 'approved'):
                return h.get('at')
        return None

    status = a.get('status')
    if i.get('status') == 'completed':
        status = 'completed'
    elif i.get('status') == 'in_progress':
        cur = _at(stages, i.get('current_stage'))
        ca = _q(cur, 'assignee')
        status = (
            'self_pending'
            if ca == 'subject'
            else 'manager_pending'
            if cur is not UNDEF and ca in ('reports_to', 'skip_level', 'department_head')
            else 'hr_pending'
        )
    cfg = _ctc_action(snap)
    completed = i.get('status') == 'completed'
    hike = _num_or_null(_ref_value(values, _q(cfg, 'hike_field'))) if completed else None
    desig = _str(_q(_q(values, hr_st['key']), 'new_designation')) if hr_st else ''

    def done(st: dict | None) -> bool:
        return js_truthy(at(st))

    _re_overall = re.compile('overall')
    _re_final = re.compile('final')
    update(
        ctx,
        'appraisals',
        a['id'],
        {
            'status': status,
            'self_rating': rating(self_st, _re_overall) if done(self_st) else None,
            'self_comments': text(self_st) if done(self_st) else '',
            'submitted_self_at': at(self_st),
            'manager_id': coalesce(a.get('manager_id', UNDEF), _manager_of(ctx, a.get('user_id'))),
            'manager_rating': rating(mgr_st, _re_overall) if done(mgr_st) else None,
            'manager_comments': text(mgr_st) if done(mgr_st) else '',
            'submitted_manager_at': at(mgr_st),
            'hr_rating': rating(hr_st, _re_final) if done(hr_st) else None,
            'hr_comments': text(hr_st) if done(hr_st) else '',
            'final_rating': rating(hr_st, _re_final) if completed else None,
            'increment_pct': hike,
            'promotion_to': desig if completed and desig else None,
            'closed_at': coalesce(i.get('completed_at', UNDEF), now_iso()) if completed else None,
        },
    )
    if _appraisal_hook is not None:
        _appraisal_hook(ctx, a.get('cycle_id'))


def _save(ctx: Ctx, i: dict) -> None:
    update(ctx, 'wf_instances', i['id'], {})
    sync_appraisal(ctx, i)


def _sweep(ctx: Ctx) -> None:
    """Lazy SLA escalation: an overdue stage moves once to the assignee's manager, and both are told."""
    if ctx.view_as:
        return
    today = ctx.today
    for i0 in list(rows(ctx, 'wf_instances')):
        due0 = i0.get('stage_due_on')
        if i0.get('status') != 'in_progress' or not js_truthy(due0) or due0 >= today or i0.get('escalated_stage') == i0.get('current_stage'):
            continue
        i = edit('wf_instances', i0)
        cur = i.get('current_assignee_id')
        mgr = _manager_of(ctx, cur)
        st = _at(snap_of(ctx, i)['stages'], i.get('current_stage'))
        st_name = _q(st, 'name')
        i['escalated_stage'] = i.get('current_stage')
        link = f'/workflows/run/{i["id"]}'
        if js_truthy(mgr) and mgr != i.get('subject_user_id') and _is_active(ctx, mgr):
            i['history'].append(
                {
                    'stage_key': coalesce(_q(st, 'key'), ''),
                    'by': 'system',
                    'action': 'escalated',
                    'note': f'Overdue since {fmt_date(i["stage_due_on"])} — escalated from {user_name(ctx, cur)}',
                    'at': now_iso(),
                    'to': mgr,
                }
            )
            i['current_assignee_id'] = mgr
            i['stage_due_on'] = add_working_days(today, 1, cal(ctx))
            notify(
                ctx,
                mgr,
                'NTF-04',
                f'Escalated: {js_str(coalesce(st_name, "workflow step"))}',
                f'{js_str(i["title"])} was overdue with {user_name(ctx, cur)} and is now with you.',
                link,
            )
            if js_truthy(cur):
                notify(ctx, cur, 'NTF-06', 'Workflow step escalated', f'{js_str(i["title"])} passed its deadline and moved to {user_name(ctx, mgr)}.', link)
        else:
            i['history'].append(
                {
                    'stage_key': coalesce(_q(st, 'key'), ''),
                    'by': 'system',
                    'action': 'escalated',
                    'note': f'Overdue since {fmt_date(i["stage_due_on"])} — nobody above {user_name(ctx, cur)} to escalate to',
                    'at': now_iso(),
                    'to': None,
                }
            )
            notify(
                ctx,
                i.get('initiated_by'),
                'NTF-06',
                'Workflow step overdue',
                f'{js_str(i["title"])}: {js_str(coalesce(st_name, "a step"))} is overdue with {user_name(ctx, cur)}.',
                link,
            )
        i['version'] = i['version'] + 1
        i['updated_at'] = now_iso()
        persist()


# =====================================================================================================================
# Start
# =====================================================================================================================
def start_workflow(ctx: Ctx, t: dict, subject_id: str, context: dict | None = None, title: Any = UNDEF) -> dict:
    if context is None:
        context = {}
    if not js_truthy(t.get('active')):
        err(409, 'WF_INACTIVE', f'“{js_str(t["name"])}” is inactive. Activate it in the designer first.')
    subj = _person(ctx, subject_id)
    if not subj:
        err(404, 'NOT_FOUND', 'Employee not found.')
    u = _user_of(ctx, subject_id)
    if u and u.get('status') != 'active':
        err(409, 'WF_SUBJECT_INACTIVE', f'{js_str(u.get("name", UNDEF))} is not active.')
    if not u and js_truthy(_q(_invite_of(ctx, subject_id), 'accepted_at')):
        err(409, 'WF_SUBJECT_INACTIVE', f'{js_str(subj["name"])} already accepted the invite — pick their account instead.')
    if any(x.get('template_id') == t['id'] and x.get('subject_user_id') == subject_id and x.get('status') == 'in_progress' for x in rows(ctx, 'wf_instances')):
        err(409, 'WF_RUNNING', f'A “{js_str(t["name"])}” workflow for {js_str(subj["name"])} is already running. Finish or cancel it first.')
    snap = _snapshot_of(t)
    chain = _chain_for(ctx, snap['stages'], subject_id, ctx.user_id)
    for idx, w in enumerate(chain):
        if not js_truthy(_g(w, 'primary')) and not (snap['stages'][idx].get('assignee') == 'subject' and subj.get('invited') and idx > 0):
            err(
                409, 'WF_NO_ASSIGNEE', f'Can’t start for {js_str(subj["name"])}: nobody can do “{js_str(snap["stages"][idx].get("name", UNDEF))}” — {w["why"]}.'
            )
    ctx_out: dict[str, Any] = dict(context)
    if u:
        r = _rate_on(ctx, u['id'], ctx.today)
        if r:
            ctx_out['current_ctc_paise'] = r['monthly_ctc_paise']
    i = insert(
        ctx,
        'wf_instances',
        {
            'template_id': t['id'],
            'title': title if js_truthy(title) else f'{js_str(t["name"])} — {js_str(subj["name"])}',
            'subject_user_id': subject_id,
            'initiated_by': ctx.user_id,
            'status': 'in_progress',
            'current_stage': 0,
            'current_assignee_id': None,
            'stage_due_on': None,
            'values': {},
            'history': [
                {'stage_key': snap['stages'][0]['key'], 'by': ctx.user_id, 'action': 'started', 'note': _str(context.get('note', UNDEF)), 'at': now_iso()}
            ],
            'context': ctx_out,
            'outputs': [],
            'template_rev': coalesce(t.get('rev', UNDEF), 1),
            'snapshot': snap,
            'stage_started_at': now_iso(),
            'escalated_stage': None,
            'comments': [],
            'completed_at': None,
        },
    )
    _enter_stage(ctx, i, 0, chain[0]['primary'])
    log_activity(ctx, 'wf_instance', i['id'], 'started', f'{js_str(ctx.user.get("name", UNDEF))} started “{js_str(t["name"])}” for {js_str(subj["name"])}')
    _save(ctx, i)
    return i


def appraisal_template(ctx: Ctx, id_: Any) -> dict:
    _ensure_provisioned(ctx)
    if js_truthy(id_):
        t = _first(rows(ctx, 'wf_templates'), lambda x: x['id'] == id_)
    else:
        t = _first(rows(ctx, 'wf_templates'), lambda x: x.get('key') == 'annual_appraisal' and js_truthy(x.get('active')))
    if not t:
        err(404, 'NOT_FOUND', 'Workflow template not found.')
    if t.get('process') != 'appraisal':
        err(422, 'VALIDATION', 'Choose an appraisal workflow.', 'workflow_template_id')
    if not js_truthy(t.get('active')):
        err(409, 'WF_INACTIVE', f'“{js_str(t["name"])}” is inactive.')
    return t


def check_routable(ctx: Ctx, t: dict, subject_ids: list) -> None:
    """Can every person be routed through this template? (used before launching a cycle)"""
    for s in subject_ids:
        chain = _chain_for(ctx, t['stages'], s, ctx.user_id)
        bad = next((k for k, w in enumerate(chain) if not js_truthy(_g(w, 'primary'))), -1)
        if bad >= 0:
            err(
                409,
                'WF_NO_ASSIGNEE',
                f'Can’t route {user_name(ctx, s)} through “{js_str(t["name"])}”: nobody can do “{js_str(t["stages"][bad].get("name", UNDEF))}” — {chain[bad]["why"]}.',
            )


def wf_linked_appraisals(ctx: Ctx) -> Any:
    """Appraisals that run through a workflow (their legacy self/manager/HR forms are replaced by the workflow).
    A JS Set: membership + insertion-ordered iteration (a dict keys view, which also supports set operators)."""
    return dict.fromkeys(
        js_str(i['context']['appraisal_id'])
        for i in rows(ctx, 'wf_instances')
        if i.get('status') != 'cancelled' and js_truthy(_q(i.get('context'), 'appraisal_id'))
    ).keys()


def assert_not_workflow_managed(ctx: Ctx, appraisal_id: Any) -> None:
    i = _first(rows(ctx, 'wf_instances'), lambda x: x.get('status') != 'cancelled' and _q(x.get('context'), 'appraisal_id') == appraisal_id)
    if i:
        err(409, 'WORKFLOW_MANAGED', f'This appraisal runs through the workflow “{js_str(i["title"])}”. Act on it from Workflows.')


# =====================================================================================================================
# Views
# =====================================================================================================================
def _row_of(ctx: Ctx, i: dict) -> dict[str, Any]:
    snap = snap_of(ctx, i)
    st = _at(snap['stages'], i.get('current_stage'))
    live = i.get('status') == 'in_progress'
    due = i.get('stage_due_on')
    return {
        'id': i['id'],
        'title': prop(i, 'title'),
        'template_id': prop(i, 'template_id'),
        'template_name': snap.get('name', UNDEF),
        'process': snap.get('process', UNDEF),
        'subject': _person(ctx, i.get('subject_user_id')),
        'initiator': _person(ctx, i.get('initiated_by')),
        'status': prop(i, 'status'),
        'stage_index': prop(i, 'current_stage'),
        'stage_count': len(snap['stages']),
        'stage_name': coalesce(_q(st, 'name'), None) if live else None,
        'assignee': _person(ctx, i.get('current_assignee_id')) if live else None,
        'due_on': prop(i, 'stage_due_on') if live else None,
        'overdue': live and js_truthy(due) and due < ctx.today,
        'created_at': prop(i, 'created_at'),
        'updated_at': prop(i, 'updated_at'),
        'completed_at': coalesce(prop(i, 'completed_at'), None),
        'can_act': js_truthy(_acting_as(ctx, i)),
        'version': prop(i, 'version'),
    }


def _cap_first(s: str) -> str:
    """c.status[0].toUpperCase() + c.status.slice(1)"""
    if not s:
        return 'undefined'
    u = js_str(s).encode('utf-16-le', 'surrogatepass')
    first = u[:2].decode('utf-16-le', 'surrogatepass')
    return js_upper(first) + u[2:].decode('utf-16-le', 'surrogatepass')


def _outputs_for(ctx: Ctx, i: dict, vs: set) -> list[dict]:
    snap = snap_of(ctx, i)
    acc = _subject_account(ctx, i.get('subject_user_id'))
    is_subject = acc is not None and acc['id'] == ctx.user_id
    comp = _ctc_action(snap) is not None
    privileged = any(js_truthy(s.get('sees_compensation')) and s.get('key') in vs for s in snap['stages']) or has_any(
        ctx, 'documents.templates', 'cost_rates.view'
    )
    out = []
    for o in i.get('outputs') or []:
        if not (o.get('kind') in ('document', 'email', 'task') if is_subject else (not comp or privileged)):
            continue
        k = o.get('kind')
        ref = o.get('ref_id')
        if k == 'cost_rate_change':
            c = _first(rows(ctx, 'change_sets'), lambda x, ref=ref: x['id'] == ref)
            can_see = has_any(ctx, 'makerchecker.check', 'audit.view', 'makerchecker.configure') or (c is not None and c.get('maker_id') == ctx.user_id)
            if c:
                cs = c.get('status')
                status = 'Waiting for a checker' if cs == 'pending' else 'Approved and applied' if cs == 'approved' else _cap_first(cs)
            else:
                status = None
            out.append({**o, 'status': status, 'link': f'/changes?id={js_str(ref)}' if can_see else None})
        elif k == 'document':
            d = _first(rows(ctx, 'issued_documents'), lambda x, ref=ref: x['id'] == ref)
            ds = d.get('status') if d else None
            out.append(
                {
                    **o,
                    'status': ('Acknowledged' if ds == 'acknowledged' else 'Revoked' if ds == 'revoked' else 'Issued') if d else None,
                    'link': f'/documents/issued/{js_str(ref)}',
                }
            )
        elif k == 'cost_rate':
            out.append({**o, 'status': 'Applied', 'link': '/settings/cost-rates' if has(ctx, 'cost_rates.view') else None})
        elif k == 'task':
            tk = _first(rows(ctx, 'tasks'), lambda x, ref=ref: x['id'] == ref)
            out.append({**o, 'status': 'Done' if tk is not None and tk.get('status') == 'done' else 'Open', 'link': f'/tasks/{js_str(ref)}'})
        elif k == 'email':
            out.append({**o, 'status': 'Sent', 'link': '/setup/emails' if has(ctx, 'emails.view') else None})
        else:
            out.append({**o, 'status': None, 'link': None})
    return out


def _detail_of(ctx: Ctx, i: dict) -> dict[str, Any]:
    snap = snap_of(ctx, i)
    vs = _viewer_stages(ctx, i)
    acting = _acting_as(ctx, i)
    acc = _subject_account(ctx, i.get('subject_user_id'))
    me = ctx.user_id
    is_subject = (acc is not None and acc['id'] == me) or i.get('subject_user_id') == me
    status_i = i.get('status')
    completed = status_i == 'completed'
    in_progress = status_i == 'in_progress'
    cur_stage = i.get('current_stage')
    chain = _chain_for(ctx, snap['stages'], i.get('subject_user_id'), i.get('initiated_by'), i)
    t = _first(rows(ctx, 'wf_templates'), lambda x: x['id'] == i.get('template_id'))
    roles = rows(ctx, 'roles')
    values = i['values']
    due = i.get('stage_due_on')
    stages_out = []
    for idx, st in enumerate(snap['stages']):
        done = idx < cur_stage or completed or (not in_progress and idx < cur_stage)
        current = in_progress and idx == cur_stage
        status = 'current' if current else 'done' if done else 'upcoming' if in_progress else 'skipped'
        actor = _last_actor(i, st['key'])
        at = None
        if js_truthy(actor):
            hh = next(
                (
                    h
                    for h in reversed(i['history'])
                    if h.get('stage_key') == st['key'] and h.get('by') == actor and h.get('action') in ('submitted', 'approved')
                ),
                None,
            )
            at = coalesce(prop(hh, 'at'), None)
        vals = coalesce(values.get(st['key'], UNDEF) if isinstance(values, dict) else UNDEF, {})
        shown = [] if status == 'current' else [f for f in st['fields'] if _field_visible(f, st['key'], vs, is_subject, completed)]
        has_values = isinstance(vals, dict) and len(vals) > 0
        if st.get('assignee') == 'role':
            rk = st.get('role_key', UNDEF)
            r = _first(roles, lambda x, rk=rk: x.get('key') == rk)
            kind = f'{ASSIGNEE_LABEL["role"]}: {js_str(coalesce(prop(r, "name"), rk))}'
        else:
            a = st.get('assignee')
            kind = ASSIGNEE_LABEL.get(a, UNDEF) if isinstance(a, str) else UNDEF
        if status == 'done':
            pid = actor
        elif current:
            pid = i.get('current_assignee_id')
        else:
            w = _at(chain, idx)
            pid = coalesce(_q(w, 'primary'), None)
        stages_out.append(
            {
                'key': st['key'],
                'name': prop(st, 'name'),
                'index': idx,
                'assignee_kind': kind,
                'status': status,
                'person': _person(ctx, pid),
                'acted_at': at if status == 'done' else None,
                'due_on': prop(i, 'stage_due_on') if current else None,
                'overdue': current and js_truthy(due) and due < ctx.today,
                'sla_days': prop(st, 'sla_days'),
                'fields': [{**f, 'value': coalesce(_q(vals, f['key']), None), 'display': display_of(f, _q(vals, f['key']))} for f in shown]
                if has_values
                else [],
                'hidden_count': len(st['fields']) - len(shown) if has_values and status != 'current' else 0,
                'instructions': coalesce(prop(st, 'instructions'), ''),
                'sees_compensation': js_truthy(st.get('sees_compensation')),
            }
        )
    cur = _at(snap['stages'], cur_stage)
    form = None
    if acting and cur is not UNDEF and js_truthy(cur):
        cv = values.get(cur['key'], UNDEF) if isinstance(values, dict) else UNDEF
        form = {
            'stage_key': cur['key'],
            'stage_name': prop(cur, 'name'),
            'fields': cur['fields'],
            'values': dict(coalesce(cv, {})) if isinstance(coalesce(cv, {}), dict) else {},
            'instructions': coalesce(prop(cur, 'instructions'), ''),
            'can_send_back': js_and(cur.get('can_send_back', UNDEF), cur_stage > 0),
            'can_reject': cur.get('assignee') != 'subject' and cur.get('assignee') != 'initiator',
            'sees_compensation': js_truthy(cur.get('sees_compensation')),
        }

    def stage_name(k: Any) -> Any:
        s = _first(snap['stages'], lambda x: x.get('key') == k)
        return coalesce(prop(s, 'name'), k)

    ictx = i.get('context') or {}
    h0 = i['history'][0] if i['history'] else None
    view_as = bool(ctx.view_as)
    return {
        **_row_of(ctx, i),
        'description': coalesce(prop(t, 'description'), ''),
        'template_rev': coalesce(prop(i, 'template_rev'), 1),
        'latest_rev': coalesce(prop(t, 'rev'), 1),
        'stages': stages_out,
        'form': form,
        'facts': _facts_for(ctx, i, vs),
        'outputs': _outputs_for(ctx, i, vs),
        'history': [
            {
                'stage_key': prop(h, 'stage_key'),
                'stage_name': stage_name(h.get('stage_key')),
                'by': _person(ctx, h.get('by')),
                'action': prop(h, 'action'),
                'note': prop(h, 'note'),
                'at': prop(h, 'at'),
                'to': _person(ctx, h.get('to')),
            }
            for h in i['history']
        ],
        'comments': [
            {'id': prop(c, 'id'), 'by': _person(ctx, c.get('by')), 'at': prop(c, 'at'), 'body': prop(c, 'body')} for c in coalesce(i.get('comments', UNDEF), [])
        ],
        'context': {
            'cycle_id': coalesce(_q(ictx, 'cycle_id'), None),
            'cycle_name': coalesce(_q(ictx, 'cycle_name'), None),
            'appraisal_id': coalesce(_q(ictx, 'appraisal_id'), None),
            'note': _str(_q(h0, 'note')) or None,
        },
        'perms': {
            'can_act': js_truthy(acting),
            'acting_as': acting,
            'can_reassign': in_progress and not view_as and has(ctx, 'workflows.design'),
            'can_cancel': in_progress and not view_as and (i.get('initiated_by') == me or has(ctx, 'workflows.design')),
            'can_comment': not view_as,
        },
        'escalated': i.get('escalated_stage') == cur_stage and in_progress,
    }


def _get_visible(ctx: Ctx, id_: str) -> dict:
    i = find(ctx, 'wf_instances', id_, 'Workflow')
    if not can_view(ctx, i):
        err(404, 'NOT_FOUND', 'Workflow not found.')
    return i


# ---------------- approvals inbox feed (used by routes/company.py) ----------------
def wf_inbox_rows(ctx: Ctx) -> list[dict]:
    _sweep(ctx)
    out = []
    for i in rows(ctx, 'wf_instances'):
        how = _acting_as(ctx, i)
        if not how:
            continue
        snap = snap_of(ctx, i)
        st = _at(snap['stages'], i.get('current_stage'))
        st_name = js_str(_g(st, 'name'))
        mgr = _manager_of(ctx, i.get('current_assignee_id'))
        out.append(
            {
                'id': i['id'],
                'version': prop(i, 'version'),
                'title': f'{st_name} · {js_str(coalesce(_q(_person(ctx, i.get("subject_user_id")), "name"), i.get("title")))}',
                'template_name': snap.get('name', UNDEF),
                'process': snap.get('process', UNDEF),
                'stage_name': st.get('name', UNDEF),
                'step_label': f'Step {i["current_stage"] + 1} of {len(snap["stages"])} · {st_name}',
                'subject_id': prop(i, 'subject_user_id'),
                'started_at': coalesce(prop(i, 'stage_started_at'), prop(i, 'created_at')),
                'due_on': prop(i, 'stage_due_on'),
                'escalates_to': None
                if i.get('escalated_stage') == i.get('current_stage')
                else (user_name(ctx, mgr) if js_truthy(mgr) and _is_active(ctx, mgr) else None),
                'via_delegation': how == 'delegate',
                'note': coalesce(st.get('instructions', UNDEF), None),
            }
        )
    return out


# =====================================================================================================================
# Template validation
# =====================================================================================================================
def _opt_num(v: Any) -> Any:
    return UNDEF if (v is None or v is UNDEF or (isinstance(v, str) and v == '')) else js_number(v)


def _normalise(body: Any) -> dict[str, Any]:
    stages = []
    for idx, s in enumerate(_arr(_g(body, 'stages'))):
        assignee = _g(s, 'assignee')
        fields = []
        for f in _arr(_g(s, 'fields')):
            ftype = _g(f, 'type')
            vt: list = []
            for x in _arr(_g(f, 'visible_to')):
                sx = _str(x)
                if sx and sx not in vt:
                    vt.append(sx)
            fields.append(
                {
                    'key': _str(_g(f, 'key')),
                    'label': _str(_g(f, 'label')),
                    'type': ftype,
                    'required': js_truthy(_g(f, 'required')),
                    'help': _str(_g(f, 'help')) or UNDEF,
                    'options': [o for o in (_str(x) for x in _arr(_g(f, 'options'))) if o] if ftype == 'select' else UNDEF,
                    'min': _opt_num(_g(f, 'min')),
                    'max': _opt_num(_g(f, 'max')),
                    'visible_to': vt,
                    'max_from': _str(_g(f, 'max_from')) or None,
                    'override_field': _str(_g(f, 'override_field')) or None,
                }
            )
        stages.append(
            {
                'key': _str(_g(s, 'key')),
                'name': _str(_g(s, 'name')),
                'assignee': assignee,
                'role_key': (_str(_g(s, 'role_key')) or None) if assignee == 'role' else None,
                'user_id': (_str(_g(s, 'user_id')) or None) if assignee == 'user' else None,
                'sla_days': js_number(_g(s, 'sla_days')),
                'can_send_back': idx > 0 and js_truthy(_g(s, 'can_send_back')),
                'instructions': _str(_g(s, 'instructions')),
                'sees_compensation': js_truthy(_g(s, 'sees_compensation')),
                'fields': fields,
            }
        )

    def actions(v: Any) -> list:
        out = []
        for a in _arr(v):
            c = _g(a, 'config')
            out.append(
                {
                    'type': _g(a, 'type'),
                    'template_key': _str(_g(a, 'template_key')) or None,
                    'config': c if js_truthy(c) and isinstance(c, (dict, list)) else {},
                }
            )
        return out

    ba = _g(body, 'active')
    return {
        'name': _str(_g(body, 'name')),
        'process': _str(_g(body, 'process')),
        'description': _str(_g(body, 'description')),
        'active': ba is not False,
        'self_service': js_truthy(_g(body, 'self_service')),
        'stages': stages,
        'on_complete': actions(_g(body, 'on_complete')),
        'on_reject': actions(_g(body, 'on_reject')),
    }


def _drop_undef(v: Any) -> Any:
    """the value as JSON.stringify keeps it (undefined object values dropped) — what the database stores"""
    if isinstance(v, dict):
        return {k: _drop_undef(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else _drop_undef(x) for x in v]
    return v


def _issues_of(ctx: Ctx, t: dict, self_id: Any = UNDEF) -> list[dict]:
    out: list[dict] = []

    def add(path: str, message: str) -> None:
        out.append({'path': path, 'message': message})

    name = t['name']
    if js_len(name) < 3:
        add('name', 'Give the workflow a name (at least 3 characters).')
    else:
        low = js_lower(name)
        if any(x['id'] != self_id and js_lower(x['name']) == low for x in rows(ctx, 'wf_templates')):
            add('name', f'Another workflow is already called “{name}”.')
    if not _includes(PROCESSES, t['process']):
        add('process', 'Choose a process type.')
    stages = t['stages']
    if not stages:
        add('stages', 'Add at least one stage.')
    if len(stages) > 12:
        add('stages', 'A workflow can have at most 12 stages.')
    keys: set = set()
    roles = rows(ctx, 'roles')
    stage_keys = [x['key'] for x in stages]
    for si, s in enumerate(stages):
        p = f'stages.{si}'
        label = s['name'] or f'Stage {si + 1}'
        if not KEY_RE.match(s['key']):
            add(f'{p}.key', f'{label}: the key must start with a letter and use only a–z, 0–9 and _ (2–32 characters).')
        elif s['key'] in keys:
            add(f'{p}.key', f'{label}: another stage already uses the key “{s["key"]}”.')
        keys.add(s['key'])
        if js_len(s['name']) < 2:
            add(f'{p}.name', f'Stage {si + 1}: give it a name.')
        sla = s['sla_days']
        if not is_integer(sla) or sla < 1 or sla > 30:
            add(f'{p}.sla_days', f'{label}: the SLA must be 1 to 30 working days.')
        if not _includes(ASSIGNEES, s['assignee']):
            add(f'{p}.assignee', f'{label}: choose who does this stage.')
        if s['assignee'] == 'role':
            r = _first(roles, lambda x, rk=s['role_key']: x.get('key') == rk)
            if not r:
                add(f'{p}.role_key', f'{label}: choose a role.')
            elif not js_truthy(r.get('active')):
                add(f'{p}.role_key', f'{label}: the role {js_str(r.get("name", UNDEF))} is inactive.')
            elif not _role_holders(ctx, r['key']):
                add(f'{p}.role_key', f'{label}: nobody holds the {js_str(r.get("name", UNDEF))} role, so this stage could never be assigned.')
        if s['assignee'] == 'user' and not _is_active(ctx, s['user_id']):
            add(f'{p}.user_id', f'{label}: choose an active person.')
        if s['assignee'] == 'subject' and si > 0 and any(x['assignee'] == 'subject' for x in stages[:si]):
            add(f'{p}.assignee', f'{label}: the employee already has a stage; use one stage per person type.')
        fkeys: set = set()
        for fi, f in enumerate(s['fields']):
            fp = f'{p}.fields.{fi}'
            fl = f['label'] or f'Field {fi + 1}'
            if not KEY_RE.match(f['key']):
                add(f'{fp}.key', f'{label} › {fl}: the key must start with a letter and use only a–z, 0–9 and _.')
            elif f['key'] in fkeys:
                add(f'{fp}.key', f'{label} › {fl}: the key “{f["key"]}” is used twice in this stage.')
            fkeys.add(f['key'])
            if js_len(f['label']) < 2:
                add(f'{fp}.label', f'{label} › field {fi + 1}: give it a label.')
            if not _includes(FIELD_TYPES, f['type']):
                add(f'{fp}.type', f'{label} › {fl}: choose a field type.')
            if f['type'] == 'select':
                opts = coalesce(f['options'], [])
                if len(opts) < 2:
                    add(f'{fp}.options', f'{label} › {fl}: a select needs at least two options.')
                if len({js_lower(o) for o in opts}) != len(opts):
                    add(f'{fp}.options', f'{label} › {fl}: options must be unique.')
            fmin, fmax = f['min'], f['max']
            if fmin is not UNDEF and not is_finite(fmin):
                add(f'{fp}.min', f'{label} › {fl}: the minimum must be a number.')
            if fmax is not UNDEF and not is_finite(fmax):
                add(f'{fp}.max', f'{label} › {fl}: the maximum must be a number.')
            if fmin is not UNDEF and fmax is not UNDEF and fmin > fmax:
                add(f'{fp}.min', f'{label} › {fl}: the minimum is above the maximum.')
            if f['type'] == 'rating' and ((fmin is not UNDEF and (fmin < 1 or fmin > 5)) or (fmax is not UNDEF and (fmax < 1 or fmax > 5))):
                add(f'{fp}.min', f'{label} › {fl}: ratings run from 1 to 5.')
            allowed = {*stage_keys, 'all', 'subject'}
            bad = [v for v in f['visible_to'] if v not in allowed]
            if bad:
                add(f'{fp}.visible_to', f'{label} › {fl}: “{", ".join(bad)}” is not a stage of this workflow.')
            if f['max_from']:
                rs, rk = _split2(f['max_from'])
                ri = next((k for k, x in enumerate(stages) if x['key'] == rs), -1)
                rf = _first(stages[ri]['fields'], lambda x, rk=rk: x['key'] == rk) if ri >= 0 else None
                if ri < 0 or ri >= si or not rf or rf['type'] not in _NUMERIC_TYPES:
                    add(f'{fp}.max_from', f'{label} › {fl}: the cap must be a number field of an earlier stage.')
                if f['type'] not in _NUMERIC_TYPES:
                    add(f'{fp}.max_from', f'{label} › {fl}: only number fields can be capped.')
            if f['override_field']:
                of = _first(s['fields'], lambda x, k=f['override_field']: x['key'] == k)
                if not of or of['type'] not in ('text', 'textarea') or of['key'] == f['key']:
                    add(f'{fp}.override_field', f'{label} › {fl}: the override reason must be a text field of the same stage.')
    docs = rows(ctx, 'doc_templates')
    emails = rows(ctx, 'email_templates')

    def any_field(path: Any, types: list) -> bool:
        s_, k_ = _split2(_str(path))
        st = _first(stages, lambda x: x['key'] == s_)
        f = _first(st['fields'], lambda x: x['key'] == k_) if st else None
        return bool(f) and f['type'] in types

    def check_actions(lst: list, where: str) -> None:
        for ai, a in enumerate(lst):
            p = f'{where}.{ai}'
            what = f'On completion, action {ai + 1}' if where == 'on_complete' else f'On rejection, action {ai + 1}'
            cfg = coalesce(a.get('config', UNDEF), {})
            cg = cfg if isinstance(cfg, dict) else {}
            at = a.get('type')
            if not _includes(ACTION_TYPES, at):
                add(p, f'{what}: choose what it does.')
                continue
            if at == 'generate_document':
                d = _first(docs, lambda x, a=a: x.get('key') == a.get('template_key'))
                if not d:
                    add(f'{p}.template_key', f'{what}: choose a document template.')
                elif not js_truthy(d.get('active')):
                    add(f'{p}.template_key', f'{what}: the template “{js_str(d.get("name", UNDEF))}” is inactive.')
            if at == 'send_email' and not any(x.get('key') == a.get('template_key') for x in emails):
                add(f'{p}.template_key', f'{what}: choose an email template.')
            if at == 'update_ctc':
                if where == 'on_reject':
                    add(p, f'{what}: a CTC change can only run on completion.')
                if not any_field(cg.get('hike_field', UNDEF), ['percent', 'number']):
                    add(f'{p}.hike_field', f'{what}: choose the field that holds the approved hike %.')
                if not any_field(cg.get('effective_field', UNDEF), ['date']):
                    add(f'{p}.effective_field', f'{what}: choose the date field for the effective date.')
            to = cg.get('to', UNDEF)
            if at in ('notify', 'send_email', 'create_task') and to is not UNDEF and not _includes(TARGETS, _str(to)) and not _str(to).startswith('role:'):
                add(f'{p}.to', f'{what}: choose who receives it.')
            if at == 'create_task' and js_len(_str(cg.get('title', UNDEF))) < 3:
                add(f'{p}.title', f'{what}: give the task a title.')
            w = cg.get('when', UNDEF)
            wf = _q(w, 'field')
            if js_truthy(wf) and not any_field(wf, FIELD_TYPES):
                add(f'{p}.when', f'{what}: the condition refers to a field that doesn’t exist.')

    check_actions(t['on_complete'], 'on_complete')
    check_actions(t['on_reject'], 'on_reject')
    return out


def _name_process_issues(ctx: Ctx, t: dict, self_id: Any = UNDEF) -> list[dict]:
    """issuesOf(ctx, t).filter(x => x.path === 'name' || x.path === 'process') — the other checks only add issues"""
    out: list[dict] = []
    name = t['name']
    if js_len(name) < 3:
        out.append({'path': 'name', 'message': 'Give the workflow a name (at least 3 characters).'})
    else:
        low = js_lower(name)
        if any(x['id'] != self_id and js_lower(x['name']) == low for x in rows(ctx, 'wf_templates')):
            out.append({'path': 'name', 'message': f'Another workflow is already called “{name}”.'})
    if not _includes(PROCESSES, t['process']):
        out.append({'path': 'process', 'message': 'Choose a process type.'})
    return out


def _template_row(ctx: Ctx, t: dict) -> dict[str, Any]:
    inst = [i for i in rows(ctx, 'wf_instances') if i.get('template_id') == t['id']]
    return {
        'id': t['id'],
        'key': coalesce(prop(t, 'key'), None),
        'name': prop(t, 'name'),
        'process': prop(t, 'process'),
        'description': prop(t, 'description'),
        'active': prop(t, 'active'),
        'self_service': js_truthy(t.get('self_service')),
        'stage_names': [prop(s, 'name') for s in t['stages']],
        'rev': coalesce(prop(t, 'rev'), 1),
        'running': sum(1 for i in inst if i.get('status') == 'in_progress'),
        'completed': sum(1 for i in inst if i.get('status') == 'completed'),
        'updated_at': prop(t, 'updated_at'),
        'version': prop(t, 'version'),
    }


# =====================================================================================================================
# Routes
# =====================================================================================================================
def list_templates(req: Req) -> Any:
    ctx = req.ctx
    _ensure_provisioned(ctx)
    all_ = rows(ctx, 'wf_templates')
    lst = all_ if has_any(ctx, 'workflows.design', 'workflows.start') else [t for t in all_ if js_truthy(t.get('active')) and js_truthy(t.get('self_service'))]
    out = [_template_row(ctx, t) for t in lst]
    return sorted(out, key=lambda r: (-js_number(r['active']), locale_key(r['name'])))


def meta(req: Req) -> Any:
    ctx = req.ctx
    _ensure_provisioned(ctx)
    return {
        'roles': sorted(
            (
                {'key': x['key'], 'name': prop(x, 'name'), 'holders': [prop(u, 'name') for u in _role_holders(ctx, x['key'])]}
                for x in rows(ctx, 'roles')
                if js_truthy(x.get('active'))
            ),
            key=lambda r: locale_key(r['name']),
        ),
        'doc_templates': sorted(
            ({'key': prop(d, 'key'), 'name': prop(d, 'name'), 'active': prop(d, 'active')} for d in rows(ctx, 'doc_templates')),
            key=lambda r: locale_key(r['name']),
        ),
        'email_templates': sorted(
            ({'key': prop(e, 'key'), 'name': prop(e, 'name')} for e in rows(ctx, 'email_templates') if js_truthy(e.get('active'))),
            key=lambda r: locale_key(r['name']),
        ),
        'departments_without_head': [prop(d, 'name') for d in rows(ctx, 'departments') if js_truthy(d.get('active')) and not js_truthy(d.get('head_id'))],
        'joiners': [
            {
                'id': x['id'],
                'name': prop(x, 'name'),
                'email': prop(x, 'email'),
                'manager': user_name(ctx, x['reports_to_id']) if js_truthy(x.get('reports_to_id')) else None,
            }
            for x in rows(ctx, 'invites')
            if not js_truthy(x.get('accepted_at'))
        ]
        if has(ctx, 'workflows.start')
        else [],
        'cycles': [
            {'id': c['id'], 'name': prop(c, 'name'), 'status': prop(c, 'status')}
            for c in rows(ctx, 'appraisal_cycles')
            if c.get('status') not in ('closed', 'draft')
        ],
    }


def get_template(req: Req) -> Any:
    ctx = req.ctx
    t = find(ctx, 'wf_templates', req.params['id'], 'Workflow template')
    if not has_any(ctx, 'workflows.design', 'workflows.start') and not (js_truthy(t.get('active')) and js_truthy(t.get('self_service'))):
        err(404, 'NOT_FOUND', 'Workflow template not found.')
    return {**t, 'running': sum(1 for i in rows(ctx, 'wf_instances') if i.get('template_id') == t['id'] and i.get('status') == 'in_progress')}


def check_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'workflows.design', 'Only workflow designers can edit workflows.')
    sid = _str(req.b('id'))
    return {'issues': _issues_of(ctx, _normalise(coalesce(req.b('template'), {})), sid or UNDEF)}


def create_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'workflows.design', 'Only workflow designers can create workflows.')
    _ensure_provisioned(ctx)
    name = _str(req.b('name'))
    copy = find(ctx, 'wf_templates', _str(req.b('copy_of')), 'Workflow template') if js_truthy(req.b('copy_of')) else None
    process = _str(req.b('process')) or (copy.get('process') if copy and js_truthy(copy.get('process')) else None) or 'general'
    if copy:
        inp = {
            'name': name,
            'process': process,
            'description': _str(req.b('description')) or copy.get('description', UNDEF),
            'active': False,
            'self_service': js_truthy(copy.get('self_service')),
            'stages': thaw(copy['stages']),
            'on_complete': thaw(copy['on_complete']),
            'on_reject': thaw(copy['on_reject']),
        }
    else:
        inp = {
            'name': name,
            'process': process,
            'description': _str(req.b('description')),
            'active': False,
            'self_service': False,
            'stages': [
                {
                    'key': 'review',
                    'name': 'Manager review',
                    'assignee': 'reports_to',
                    'sla_days': 3,
                    'can_send_back': False,
                    'instructions': '',
                    'fields': [{'key': 'comments', 'label': 'Comments', 'type': 'textarea', 'required': True, 'visible_to': ['all']}],
                }
            ],
            'on_complete': [{'type': 'notify', 'config': {'to': 'subject', 'message': 'Your request is complete.'}}],
            'on_reject': [{'type': 'notify', 'config': {'to': 'initiator', 'message': 'The workflow was rejected.'}}],
        }
    issues = _name_process_issues(ctx, inp)
    if issues:
        err(422, 'VALIDATION', issues[0]['message'], issues[0]['path'])
    t = insert(ctx, 'wf_templates', {**inp, 'rev': 1, 'key': None, 'updated_by': ctx.user_id})
    src = f' from “{js_str(copy["name"])}”' if copy else ''
    log_activity(ctx, 'wf_template', t['id'], 'created', f'{js_str(ctx.user.get("name", UNDEF))} created the workflow “{js_str(t["name"])}”{src}')
    return t


def put_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'workflows.design', 'Only workflow designers can edit workflows.')
    t = find(ctx, 'wf_templates', req.params['id'], 'Workflow template')
    inp = _normalise(req.body)
    issues = _issues_of(ctx, inp, t['id'])
    if issues:
        msg = issues[0]['message'] if len(issues) == 1 else f'{len(issues)} problems — first: {issues[0]["message"]}'
        err(422, 'WF_INVALID', msg, issues[0]['path'])
    stored = _drop_undef(inp)
    structural = _canon([t.get('stages'), t.get('on_complete'), t.get('on_reject'), t.get('process')]) != _canon(
        [stored['stages'], stored['on_complete'], stored['on_reject'], stored['process']]
    )
    rev0 = coalesce(t.get('rev', UNDEF), 1)
    out = update(ctx, 'wf_templates', t['id'], {**stored, 'rev': rev0 + (1 if structural else 0), 'updated_by': ctx.user_id}, req.if_match)
    # the TS reads t.rev AFTER update() changed t in place: the messages quote the NEW revision (mirrored)
    rev = coalesce(out.get('rev', UNDEF), 1)
    running = sum(1 for i in rows(ctx, 'wf_instances') if i.get('template_id') == t['id'] and i.get('status') == 'in_progress')
    if structural and running:
        warn(
            ctx,
            'WF_RUNNING_KEEP_VERSION',
            f'{running} running workflow{"" if running == 1 else "s"} keep revision {js_str(rev)}; new starts use revision {js_str(rev + 1)}.',
        )
    log_activity(
        ctx,
        'wf_template',
        t['id'],
        'updated',
        f'{js_str(ctx.user.get("name", UNDEF))} updated the workflow “{inp["name"]}”{f" (revision {js_str(rev + 1)})" if structural else ""}',
    )
    return out


def duplicate_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'workflows.design', 'Only workflow designers can copy workflows.')
    t = find(ctx, 'wf_templates', req.params['id'], 'Workflow template')
    names = {js_lower(x['name']) for x in rows(ctx, 'wf_templates')}
    name = f'{js_str(t["name"])} (copy)'
    n = 2
    while js_lower(name) in names:
        name = f'{js_str(t["name"])} (copy {n})'
        n += 1
    c = insert(
        ctx,
        'wf_templates',
        {
            'name': name,
            'process': t['process'],
            'description': t.get('description', UNDEF),
            'active': False,
            'self_service': js_truthy(t.get('self_service')),
            'stages': thaw(t['stages']),
            'on_complete': thaw(t['on_complete']),
            'on_reject': thaw(t['on_reject']),
            'rev': 1,
            'key': None,
            'updated_by': ctx.user_id,
        },
    )
    log_activity(ctx, 'wf_template', c['id'], 'created', f'{js_str(ctx.user.get("name", UNDEF))} copied the workflow “{js_str(t["name"])}”')
    return c


def list_instances(req: Req) -> Any:
    ctx = req.ctx
    _sweep(ctx)
    me = ctx.user_id
    all_ = rows(ctx, 'wf_instances')
    can_all = has_any(ctx, 'workflows.design', 'workflows.start')
    tab = _str(req.q('tab')) or 'waiting'

    def pick(t: str) -> list:
        if t == 'waiting':
            return [i for i in all_ if js_truthy(_acting_as(ctx, i))]
        if t == 'started':
            return [i for i in all_ if i.get('initiated_by') == me]
        if t == 'about':
            return [
                i
                for i in all_
                if i.get('subject_user_id') == me or (lambda a: a is not None and a['id'] == me)(_subject_account(ctx, i.get('subject_user_id')))
            ]
        return list(all_) if can_all else []

    if tab == 'all' and not can_all:
        err(403, 'FORBIDDEN', 'Only HR and workflow designers see every workflow.')
    lst = pick(tab)
    qs = req.q('status')
    if js_truthy(qs):
        lst = [i for i in lst if i.get('status') == qs]
    qt = req.q('template_id')
    if js_truthy(qt):
        lst = [i for i in lst if i.get('template_id') == qt]
    rws = [_row_of(ctx, i) for i in lst]
    rws = sorted(rws, key=lambda a: locale_key(a['updated_at']), reverse=True)
    rws = sorted(rws, key=lambda a: (0 if a['status'] == 'in_progress' else 1, locale_key(coalesce(a['due_on'], '9999'))))
    return {
        'rows': rws,
        'counts': {
            'waiting': len(pick('waiting')),
            'started': sum(1 for i in pick('started') if i.get('status') == 'in_progress'),
            'about': sum(1 for i in pick('about') if i.get('status') == 'in_progress'),
            'all': sum(1 for i in all_ if i.get('status') == 'in_progress') if can_all else None,
        },
        'can_start': has(ctx, 'workflows.start') or any(js_truthy(t.get('active')) and js_truthy(t.get('self_service')) for t in rows(ctx, 'wf_templates')),
        'can_design': has(ctx, 'workflows.design'),
    }


def get_instance(req: Req) -> Any:
    ctx = req.ctx
    _sweep(ctx)
    return _detail_of(ctx, _get_visible(ctx, req.params['id']))


def appraisal_brief(req: Req) -> Any:
    ctx = req.ctx
    _sweep(ctx)
    a = find(ctx, 'appraisals', req.params['appraisalId'], 'Appraisal')
    cands = sorted(
        (x for x in rows(ctx, 'wf_instances') if x.get('status') != 'cancelled' and _q(x.get('context'), 'appraisal_id') == a['id']),
        key=lambda x: locale_key(x['created_at']),
        reverse=True,
    )
    if not cands:
        return None
    d = _detail_of(ctx, cands[0])
    return {
        'id': d['id'],
        'title': d['title'],
        'status': d['status'],
        'stage_index': d['stage_index'],
        'stage_count': d['stage_count'],
        'stage_name': d['stage_name'],
        'assignee': d['assignee'],
        'due_on': d['due_on'],
        'overdue': d['overdue'],
        'can_act': d['can_act'],
        'stages': [{'name': s['name'], 'status': s['status'], 'person': s['person'], 'acted_at': s['acted_at']} for s in d['stages']],
        'outputs': [{'kind': o.get('kind', UNDEF), 'label': o.get('label', UNDEF)} for o in d['outputs']],
    }


def start_instances(req: Req) -> Any:
    ctx = req.ctx
    _ensure_provisioned(ctx)
    t = find(ctx, 'wf_templates', _str(req.b('template_id')), 'Workflow template')
    bs = req.b('subject_ids')
    raw = bs if isinstance(bs, list) else [req.b('subject_id')]
    ids: list[str] = []
    for x in raw:
        s = _str(x)
        if s and s not in ids:
            ids.append(s)
    if not ids:
        err(422, 'VALIDATION', 'Choose at least one employee.', 'subject_ids')
    if len(ids) > 100:
        err(422, 'VALIDATION', 'Start at most 100 at a time.', 'subject_ids')
    tname = js_str(t['name'])
    for s in ids:
        if (not (js_truthy(t.get('self_service')) or has(ctx, 'workflows.start'))) if s == ctx.user_id else not has(ctx, 'workflows.start'):
            err(
                403,
                'FORBIDDEN',
                f'“{tname}” can only be started by HR.' if s == ctx.user_id else 'Starting workflows for other people needs “Start workflows for others”.',
            )
        if not _user_of(ctx, s) and not (_invite_of(ctx, s) and t.get('process') == 'onboarding'):
            err(422, 'VALIDATION', 'Choose active employees (invited joiners only for onboarding).', 'subject_ids')
    note = _u16slice(_str(req.b('note')), 0, 1000)
    cycle = find(ctx, 'appraisal_cycles', _str(req.b('cycle_id')), 'Appraisal cycle') if js_truthy(req.b('cycle_id')) else None
    if cycle and t.get('process') != 'appraisal':
        err(422, 'VALIDATION', 'Only appraisal workflows link to an appraisal cycle.', 'cycle_id')
    if cycle and cycle.get('status') == 'closed':
        err(409, 'CYCLE_CLOSED', f'{js_str(cycle["name"])} is closed.', 'cycle_id')
    # validate every person first (all-or-nothing)
    linked = wf_linked_appraisals(ctx)

    def pname(s: str) -> str:
        return js_str(_q(_person(ctx, s), 'name'))

    for s in ids:
        if any(x.get('template_id') == t['id'] and x.get('subject_user_id') == s and x.get('status') == 'in_progress' for x in rows(ctx, 'wf_instances')):
            err(409, 'WF_RUNNING', f'A “{tname}” workflow for {pname(s)} is already running.')
        if cycle:
            a = _first(rows(ctx, 'appraisals'), lambda x, s=s: x.get('cycle_id') == cycle['id'] and x.get('user_id') == s)
            if a and a['id'] in linked:
                err(409, 'WF_RUNNING', f'{pname(s)}’s {js_str(cycle["name"])} appraisal already runs through a workflow.')
            if a and a.get('status') == 'completed':
                err(409, 'APPRAISAL_DONE', f'{pname(s)}’s {js_str(cycle["name"])} appraisal is already completed.')
    if not js_truthy(t.get('active')):
        err(409, 'WF_INACTIVE', f'“{tname}” is inactive. Activate it in the designer first.')
    check_routable(ctx, t, [s for s in ids if _user_of(ctx, s)])
    created = []
    btitle = _str(req.b('title'))
    for s in ids:
        a = _first(rows(ctx, 'appraisals'), lambda x, s=s: x.get('cycle_id') == cycle['id'] and x.get('user_id') == s) if cycle else None
        context: dict[str, Any] = {'note': note}
        if cycle:
            context.update({'cycle_id': cycle['id'], 'cycle_name': cycle.get('name', UNDEF), 'appraisal_id': a['id'] if a else None})
        title = btitle if btitle and len(ids) == 1 else f'{tname}{" " + js_str(cycle["name"]) if cycle else ""} — {pname(s)}'
        created.append(start_workflow(ctx, t, s, context, title))
    return {'created': [_row_of(ctx, i) for i in created]}


def preview_instance(req: Req) -> Any:
    ctx = req.ctx
    i = _get_visible(ctx, req.params['id'])
    if not _acting_as(ctx, i):
        err(403, 'FORBIDDEN', 'Only the current reviewer can preview.')
    snap = snap_of(ctx, i)
    st = snap['stages'][i['current_stage']]
    bv = coalesce(req.b('values'), {})
    draft: dict[str, Any] = {}
    for f in st['fields']:
        if f.get('type') in ('number', 'percent', 'money', 'rating', 'date'):
            draft[f['key']] = bv.get(f['key'], UNDEF) if isinstance(bv, dict) else UNDEF
    values = i['values']
    curv = values.get(st['key'], UNDEF) if isinstance(values, dict) else UNDEF
    merged = {**values, st['key']: {**(coalesce(curv, {}) if isinstance(coalesce(curv, {}), dict) else {}), **draft}}
    return {'facts': _facts_for(ctx, i, _viewer_stages(ctx, i), merged)}


def act(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    i0 = _get_visible(ctx, req.params['id'])
    if req.if_match is not UNDEF and req.if_match != i0.get('version'):
        err(409, 'STALE', 'This workflow changed while you were working. Reload to see the latest.')
    if i0.get('status') != 'in_progress':
        err(409, 'WF_CLOSED', f'This workflow is {js_str(i0.get("status", UNDEF)).replace("_", " ", 1)}.')
    how = _acting_as(ctx, i0)
    if not how:
        err(403, 'FORBIDDEN', f'This step is waiting for {user_name(ctx, i0.get("current_assignee_id"))}.')
    i = edit('wf_instances', i0)
    snap = snap_of(ctx, i)
    idx = i['current_stage']
    st = snap['stages'][idx]
    action = _str(_g(body, 'action'))
    me = ctx.user_id
    me_name = js_str(ctx.user.get('name', UNDEF))
    by = f' (for {user_name(ctx, i.get("current_assignee_id"))})' if how == 'delegate' else ''
    link = f'/workflows/run/{i["id"]}'
    if action == 'submit':
        clean = _clean_values(snap, st, coalesce(_g(body, 'values'), {}), i['values'])
        nxt = {**i['values'], st['key']: clean}
        last = idx == len(snap['stages']) - 1
        probe = {**i, 'values': nxt}
        next_who = None
        if not last:
            chain = _chain_for(
                ctx,
                snap['stages'],
                i.get('subject_user_id'),
                i.get('initiated_by'),
                {
                    **probe,
                    'current_stage': idx + 1,
                    'current_assignee_id': None,
                    'history': [*i['history'], {'stage_key': st['key'], 'by': me, 'action': 'approved', 'note': '', 'at': now_iso()}],
                },
            )
            next_who = _g(chain[idx + 1], 'primary')
            if not js_truthy(next_who):
                err(
                    409,
                    'WF_NO_ASSIGNEE',
                    f'Nobody can do the next stage “{js_str(snap["stages"][idx + 1].get("name", UNDEF))}” — {chain[idx + 1]["why"]}. Ask a workflow designer to fix the workflow.',
                )
        else:
            _preflight(ctx, probe, snap['on_complete'])
        # commit
        i['values'] = nxt
        i['history'].append(
            {
                'stage_key': st['key'],
                'by': me,
                'action': 'submitted' if st.get('assignee') == 'subject' or idx == 0 else 'approved',
                'note': _u16slice(_str(_g(body, 'note')), 0, 1000) + by,
                'at': now_iso(),
            }
        )
        if last:
            i['status'] = 'completed'
            i['current_assignee_id'] = None
            i['stage_due_on'] = None
            i['completed_at'] = now_iso()
            _run_actions(ctx, i, snap['on_complete'])
            kinds = [o.get('kind') for o in i['outputs']]
            parts = [
                'letter issued' if 'document' in kinds else '',
                'emailed' if 'email' in kinds else '',
                'CTC change sent for approval' if 'cost_rate_change' in kinds else 'CTC updated' if 'cost_rate' in kinds else '',
            ]
            i['history'].append({'stage_key': st['key'], 'by': me, 'action': 'completed', 'note': ', '.join(p for p in parts if p), 'at': now_iso()})
            if i.get('initiated_by') != me:
                notify(ctx, i.get('initiated_by'), 'NTF-05', 'Workflow completed', i['title'], link)
            log_activity(ctx, 'wf_instance', i['id'], 'completed', f'{me_name}{by} completed “{js_str(i["title"])}”')
        else:
            _enter_stage(ctx, i, idx + 1, next_who)
            log_activity(ctx, 'wf_instance', i['id'], 'advanced', f'{me_name}{by} completed {js_str(st.get("name", UNDEF))} on “{js_str(i["title"])}”')
        _save(ctx, i)
        return _detail_of(ctx, i)
    if action == 'send_back':
        if not js_truthy(st.get('can_send_back')) or idx == 0:
            err(409, 'WF_NO_SEND_BACK', f'{js_str(st.get("name", UNDEF))} can’t send back.')
        reason = _str(_g(body, 'reason'))
        if js_len(reason) < 10:
            err(422, 'REASON_REQUIRED', 'Say what should change (at least 10 characters).', 'reason')
        prev = snap['stages'][idx - 1]
        who = _last_actor(i, prev['key'])
        if who is None:
            who = _g(_chain_for(ctx, snap['stages'], i.get('subject_user_id'), i.get('initiated_by'))[idx - 1], 'primary')
        if not js_truthy(who) or not _is_active(ctx, who):
            err(409, 'WF_NO_ASSIGNEE', f'Nobody is available to redo “{js_str(prev.get("name", UNDEF))}”.')
        i['history'].append({'stage_key': st['key'], 'by': me, 'action': 'sent_back', 'note': reason + by, 'at': now_iso(), 'to': who})
        _enter_stage(ctx, i, idx - 1, who)
        notify(ctx, who, 'NTF-06', f'Sent back: {js_str(prev.get("name", UNDEF))}', f'{me_name}: “{reason}”', link)
        log_activity(ctx, 'wf_instance', i['id'], 'sent_back', f'{me_name}{by} sent “{js_str(i["title"])}” back to {js_str(prev.get("name", UNDEF))}: {reason}')
        _save(ctx, i)
        return _detail_of(ctx, i)
    if action == 'reject':
        if st.get('assignee') in ('subject', 'initiator'):
            err(409, 'WF_NO_REJECT', 'This stage can submit but not reject. Ask HR to cancel the workflow instead.')
        reason = _str(_g(body, 'reason'))
        if js_len(reason) < 10:
            err(422, 'REASON_REQUIRED', 'Say why it is rejected (at least 10 characters).', 'reason')
        i['history'].append({'stage_key': st['key'], 'by': me, 'action': 'rejected', 'note': reason + by, 'at': now_iso()})
        i['status'] = 'rejected'
        i['current_assignee_id'] = None
        i['stage_due_on'] = None
        i['completed_at'] = now_iso()
        _run_actions(ctx, i, [a for a in snap['on_reject'] if a.get('type') != 'update_ctc'])
        log_activity(ctx, 'wf_instance', i['id'], 'rejected', f'{me_name}{by} rejected “{js_str(i["title"])}”: {reason}')
        _save(ctx, i)
        return _detail_of(ctx, i)
    err(422, 'VALIDATION', 'Unknown action.', 'action')
    return UNDEF


def reassign(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'workflows.design', 'Only workflow designers can reassign a step.')
    i0 = _get_visible(ctx, req.params['id'])
    if i0.get('status') != 'in_progress':
        err(409, 'WF_CLOSED', 'Only running workflows can be reassigned.')
    to = _str(req.b('user_id'))
    if not _is_active(ctx, to):
        err(422, 'VALIDATION', 'Choose an active person.', 'user_id')
    if to == i0.get('current_assignee_id'):
        err(422, 'VALIDATION', 'That person already has this step.', 'user_id')
    snap = snap_of(ctx, i0)
    st = snap['stages'][i0['current_stage']]
    if to == i0.get('subject_user_id') and st.get('assignee') != 'subject':
        err(422, 'VALIDATION', 'The employee can’t review their own workflow.', 'user_id')
    note = _str(req.b('note'))
    if js_len(note) < 5:
        err(422, 'REASON_REQUIRED', 'Say why you are reassigning (at least 5 characters).', 'note')
    i = edit('wf_instances', i0)
    frm = i.get('current_assignee_id')
    i['history'].append(
        {
            'stage_key': st['key'],
            'by': ctx.user_id,
            'action': 'reassigned',
            'note': f'{user_name(ctx, frm)} → {user_name(ctx, to)}: {note}',
            'at': now_iso(),
            'to': to,
        }
    )
    _enter_stage(ctx, i, i['current_stage'], to)
    if js_truthy(frm) and frm != ctx.user_id:
        notify(
            ctx,
            frm,
            'NTF-06',
            'Workflow step reassigned',
            f'{js_str(i["title"])}: {js_str(st.get("name", UNDEF))} moved to {user_name(ctx, to)}.',
            f'/workflows/run/{i["id"]}',
        )
    log_activity(
        ctx,
        'wf_instance',
        i['id'],
        'reassigned',
        f'{js_str(ctx.user.get("name", UNDEF))} reassigned {js_str(st.get("name", UNDEF))} on “{js_str(i["title"])}” to {user_name(ctx, to)}',
    )
    _save(ctx, i)
    return _detail_of(ctx, i)


def cancel(req: Req) -> Any:
    ctx = req.ctx
    i0 = _get_visible(ctx, req.params['id'])
    if i0.get('initiated_by') != ctx.user_id and not has(ctx, 'workflows.design'):
        err(403, 'FORBIDDEN', 'Only the person who started it or a workflow designer can cancel it.')
    if i0.get('status') != 'in_progress':
        err(409, 'WF_CLOSED', f'This workflow is already {js_str(i0.get("status", UNDEF)).replace("_", " ", 1)}.')
    reason = _str(req.b('reason'))
    if js_len(reason) < 10:
        err(422, 'REASON_REQUIRED', 'Say why you are cancelling (at least 10 characters).', 'reason')
    i = edit('wf_instances', i0)
    snap = snap_of(ctx, i)
    st = _at(snap['stages'], i.get('current_stage'))
    i['history'].append({'stage_key': coalesce(_q(st, 'key'), ''), 'by': ctx.user_id, 'action': 'cancelled', 'note': reason, 'at': now_iso()})
    was = i.get('current_assignee_id')
    i['status'] = 'cancelled'
    i['current_assignee_id'] = None
    i['stage_due_on'] = None
    if js_truthy(was) and was != ctx.user_id:
        notify(ctx, was, 'NTF-06', 'Workflow cancelled', f'{js_str(i["title"])}: {reason}', f'/workflows/run/{i["id"]}')
    log_activity(ctx, 'wf_instance', i['id'], 'cancelled', f'{js_str(ctx.user.get("name", UNDEF))} cancelled “{js_str(i["title"])}”: {reason}')
    update(ctx, 'wf_instances', i['id'], {})
    return _detail_of(ctx, i)


def comment(req: Req) -> Any:
    ctx = req.ctx
    i0 = _get_visible(ctx, req.params['id'])
    text = _str(req.b('body'))
    if js_len(text) < 2:
        err(422, 'VALIDATION', 'Write a comment first.', 'body')
    if js_len(text) > 2000:
        err(422, 'VALIDATION', 'Keep comments under 2,000 characters.', 'body')
    i = edit('wf_instances', i0)
    me = ctx.user_id
    i['comments'] = [*coalesce(i.get('comments', UNDEF), []), {'id': _ctx_mod.uid('c'), 'by': me, 'at': now_iso(), 'body': text}]
    others: list = []
    for x in [i.get('initiated_by'), i.get('current_assignee_id'), *(h.get('by') for h in i['history'])]:
        if js_truthy(x) and x != me and x != 'system' and _is_active(ctx, x) and x not in others:
            others.append(x)
    me_name = js_str(ctx.user.get('name', UNDEF))
    for o in others:
        notify(ctx, o, 'NTF-03', f'Comment on {js_str(i["title"])}', f'{me_name}: “{_u16slice(text, 0, 90)}”', f'/workflows/run/{i["id"]}')
    update(ctx, 'wf_instances', i['id'], {})
    return _detail_of(ctx, i)


def register(r: Any) -> None:
    # templates
    r.get('/wf/templates', list_templates)
    r.get('/wf/meta', meta)
    r.get('/wf/templates/:id', get_template)
    r.post('/wf/templates/check', check_template)
    r.post('/wf/templates', create_template)
    r.put('/wf/templates/:id', put_template)
    r.post('/wf/templates/:id/duplicate', duplicate_template)
    # instances
    r.get('/wf/instances', list_instances)
    r.get('/wf/instances/:id', get_instance)
    r.get('/wf/appraisal/:appraisalId', appraisal_brief)
    r.post('/wf/instances', start_instances)
    r.post('/wf/instances/:id/preview', preview_instance)
    r.post('/wf/instances/:id/act', act)
    r.post('/wf/instances/:id/reassign', reassign)
    r.post('/wf/instances/:id/cancel', cancel)
    r.post('/wf/instances/:id/comments', comment)
