"""v4 approval workflow engine — port of src/mock/workflows.ts (engine + its configuration routes).

A tenant configures, per business process, an ordered list of steps (who approves, from which amount, SLA,
escalation). Routes call `start_approval` on submit and `advance_approval` on every approval; the record keeps
`approver_id` = the current step's approver plus the frozen chain, so editing a workflow never re-routes requests that
are already waiting. Defaults reproduce the v3 behaviour exactly.

Stored fields (kept loosely on leave_requests, expense_claims, purchase_orders, vendor_bills, timesheet_weeks, tasks):
  wf_process, wf_chain (StoredStep[]), wf_step (index into wf_chain), wf_step_at (ISO datetime), wf_approver_id (tasks)

The routes (`register(r)`: GET /workflows, PUT /workflows/:process, POST /workflows/:process/reset,
POST /workflows/:process/simulate) are registered by app/routes/company.py, like company.ts calls registerWorkflows(r).
The maker-checker applier for entity 'workflow' is registered at import time.

Functions return NEW dicts where the TS returns new objects; `ensure_wf` returns the record itself when it already
carries a valid chain (like the TS).
"""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, Req, calendar_for, err, find, has_any, log_activity, notify, now_iso, persist, require_action, rows, scope, update, user_name
from app.domain.calendar import add_working_days
from app.domain.money import fmt_money
from app.engine.maker_checker import assert_base_version, pending_change_for, propose, register_applier
from app.frozen import thaw
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_json_dumps,
    js_len,
    js_number,
    js_slice,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    now_ms,
    nullish,
    prop,
)

__all__ = [
    'PROCESS_LABEL',
    'APPROVER_LABEL',
    'AMOUNT_PROCESSES',
    'PROJECT_PROCESSES',
    'PROCESS_ORDER',
    'DEFAULT_WORKFLOWS',
    'FALLBACK_STEP',
    'workflow_for',
    'holder_of_role',
    'top_approver',
    'resolve_approver',
    'resolve_chain',
    'next_in_chain',
    'provision_workflows',
    'start_approval',
    'wf_patch',
    'wf_clear',
    'ensure_wf',
    'advance_approval',
    'step_label',
    'step_due',
    'escalation_target',
    'approval_route',
    'escalate_if_due',
    'step_summary',
    'chain_summary',
    'validate_steps',
    'register',
]

_C = cat('mock/workflows')
PROCESS_LABEL = _C['PROCESS_LABEL']
APPROVER_LABEL = _C['APPROVER_LABEL']
AMOUNT_PROCESSES = _C['AMOUNT_PROCESSES']
PROJECT_PROCESSES = _C['PROJECT_PROCESSES']
PROCESS_ORDER = _C['PROCESS_ORDER']
DEFAULT_WORKFLOWS = _C['DEFAULT_WORKFLOWS']
FALLBACK_STEP = {'id': 'fallback', 'name': 'Final approval', 'approver': 'role', 'role_id': 'ceo', 'sla_days': 2}


# ---------------------------------------------------------------------------------------------------------------------
# JS value helpers (request bodies can hold any JSON value)
# ---------------------------------------------------------------------------------------------------------------------
def _get(obj: Any, key: str) -> Any:
    """`obj.key` for any JS value (undefined for non-objects / missing keys)"""
    if isinstance(obj, dict):
        return obj.get(key, UNDEF)
    return UNDEF


def _at(lst: Any, i: Any) -> Any:
    """`arr[i]` (undefined when out of range / not an integer index)"""
    if not isinstance(lst, (list, tuple)) or isinstance(i, bool) or not isinstance(i, (int, float)):
        return UNDEF
    if isinstance(i, float):
        if not i.is_integer():
            return UNDEF
        i = int(i)
    return lst[i] if 0 <= i < len(lst) else UNDEF


def _strict_eq(a: Any, b: Any) -> bool:
    """a === b on JSON values (objects by identity)"""
    if a is UNDEF or b is UNDEF:
        return a is b
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    return False


def _includes(lst: Any, v: Any) -> bool:
    """Array.prototype.includes (SameValueZero) for string lists"""
    return isinstance(v, str) and v in lst


def _b36(n: int) -> str:
    """Number.prototype.toString(36) for a non-negative integer"""
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    if n == 0:
        return '0'
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return ''.join(reversed(out))


# ---------------------------------------------------------------------------------------------------------------------
# chains
# ---------------------------------------------------------------------------------------------------------------------
def _user(ctx: Ctx, id_: Any) -> dict | None:
    """rows(ctx, 'users').find(u => u.id === id)"""
    if nullish(id_):
        return None
    u = scope().find_by('users', 'id', id_)
    return u if u is not None and u.get('tenant_id') == ctx.tenant_id else None


def workflow_for(ctx: Ctx, process: Any) -> dict | None:
    return next((w for w in rows(ctx, 'approval_workflows') if w.get('process') == process and js_truthy(w.get('active'))), None)


def _active_user(ctx: Ctx, id_: Any) -> dict | None:
    if not js_truthy(id_):
        return None
    u = _user(ctx, id_)
    return u if u is not None and u.get('status') == 'active' else None


def _id_of(u: dict | None) -> Any:
    """`activeUser(...)?.id ?? null`"""
    return coalesce(u.get('id', UNDEF), None) if u is not None else None


def holder_of_role(ctx: Ctx, role_key: Any, exclude: Any = UNDEF) -> Any:
    """First active person holding a role (stable: earliest joiner)."""
    if not js_truthy(role_key):
        return None
    c = [
        u
        for u in rows(ctx, 'users')
        if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin')) and u.get('role_key') == role_key and u['id'] != exclude
    ]
    c = sorted(c, key=lambda u: locale_key(u['joined_on']))
    return c[0]['id'] if c else None


def top_approver(ctx: Ctx, exclude: Any = UNDEF) -> Any:
    """Final fallback approver: the CEO, else anyone on the leadership persona."""
    h = holder_of_role(ctx, 'ceo', exclude)
    if h is not None:
        return h
    u = next(
        (
            u
            for u in rows(ctx, 'users')
            if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin')) and u.get('role') == 'admin' and u['id'] != exclude
        ),
        None,
    )
    return coalesce(u.get('id', UNDEF), None) if u is not None else None


def resolve_approver(ctx: Ctx, a: dict, input_: dict) -> Any:
    """the approver id a step resolves to for a request (None when nobody matches; UNDEF for an unknown type, like TS)"""
    req = _user(ctx, input_.get('requester_id'))

    def pm() -> Any:
        pid = input_.get('project_id')
        if not js_truthy(pid):
            return None
        p = next((x for x in rows(ctx, 'projects') if x['id'] == pid), None)
        return coalesce(p.get('pm_id', UNDEF), None) if p is not None else None

    req_reports_to = req.get('reports_to_id', UNDEF) if req is not None else UNDEF
    t = a.get('approver')
    if t == 'reports_to':
        return _id_of(_active_user(ctx, req_reports_to))
    if t == 'skip_level':
        m = _user(ctx, req_reports_to)
        return _id_of(_active_user(ctx, m.get('reports_to_id', UNDEF) if m is not None else UNDEF))
    if t == 'role':
        h = holder_of_role(ctx, a.get('role_id', UNDEF), input_.get('requester_id', UNDEF))
        return h if h is not None else holder_of_role(ctx, a.get('role_id', UNDEF))
    if t == 'user':
        return _id_of(_active_user(ctx, a.get('user_id', UNDEF)))
    if t == 'project_pm':
        return _id_of(_active_user(ctx, pm()))
    if t == 'project_pm_or_manager':
        p = _active_user(ctx, pm())
        if p is not None and p['id'] != input_.get('requester_id'):
            return p['id']
        return _id_of(_active_user(ctx, req_reports_to))
    if t == 'department_head':
        dep_id = req.get('department_id') if req is not None else None
        dep = next((d for d in rows(ctx, 'departments') if d['id'] == dep_id), None) if js_truthy(dep_id) else None
        h = _id_of(_active_user(ctx, dep.get('head_id', UNDEF) if dep is not None else UNDEF))
        return h if h is not None else _id_of(_active_user(ctx, req_reports_to))
    if t == 'task_reviewer':
        return _id_of(_active_user(ctx, input_.get('reviewer_id', UNDEF)))
    return UNDEF


def _applies(s: dict, amount: Any) -> bool:
    """Does a step apply to this amount? (threshold = strictly above)"""
    mn = s.get('min_amount_paise')
    return not js_truthy(mn) or coalesce(amount, 0) > mn


def resolve_chain(ctx: Ctx, process: Any, input_: dict, steps_override: list | None = None) -> list[dict]:
    """Concrete approver chain for a request: [{'step', 'index', 'approver_id'}] (see the TS doc)."""
    wf = None if steps_override is not None else workflow_for(ctx, process)
    if steps_override is not None:
        steps = steps_override
    elif wf is not None and not nullish(wf.get('steps')):
        steps = wf['steps']
    else:
        d = next((w for w in DEFAULT_WORKFLOWS if w['process'] == process), None)
        steps = d['steps'] if d is not None else []
    req_id = input_.get('requester_id')
    out: list[dict] = []
    for index, s in enumerate(steps):
        if not _applies(s, input_.get('amount_paise')):
            continue
        id_ = resolve_approver(ctx, s, input_)
        esc = s.get('escalate_to')
        if (not js_truthy(id_) or (id_ == req_id and not js_truthy(s.get('allow_self')))) and js_truthy(esc):
            alt = resolve_approver(ctx, esc, input_)
            if js_truthy(alt) and alt != req_id:
                id_ = alt
        if not js_truthy(id_):
            continue
        if id_ == req_id and not js_truthy(s.get('allow_self')):
            continue
        if out and out[-1]['approver_id'] == id_:
            continue
        out.append({'step': s, 'index': index, 'approver_id': id_})
    if not out:
        top = top_approver(ctx, req_id)
        if top is None:
            top = top_approver(ctx)
        if js_truthy(top):
            out.append({'step': FALLBACK_STEP, 'index': -1, 'approver_id': top})
    return out


def next_in_chain(chain: list[dict], current_approver_id: Any) -> dict | None:
    """The step after `current_approver_id` in the chain (None when the current one is the last)."""
    i = next((k for k, c in enumerate(chain) if c['approver_id'] == current_approver_id), -1)
    return chain[i + 1] if i >= 0 and i + 1 < len(chain) else None


def provision_workflows(d: Any, tenant_id: str, base: Any) -> None:
    """Seed the default workflows for a tenant (idempotent). `d` = get_db() (or a Scope)."""
    s = getattr(d, '_scope', d)
    for w in DEFAULT_WORKFLOWS:
        if not any(x.get('tenant_id') == tenant_id and x.get('process') == w['process'] for x in s.view('approval_workflows')):
            s.append('approval_workflows', {**base(), **thaw(w)})


# ---------------------------------------------------------------------------------------------------------------------
# approval state on records
# ---------------------------------------------------------------------------------------------------------------------
def _store_chain(ctx: Ctx, chain: list[dict], input_: dict) -> list[dict]:
    out = []
    for c in chain:
        st = c['step']
        esc_to = st.get('escalate_to')
        esc = resolve_approver(ctx, esc_to, input_) if js_truthy(esc_to) else None
        out.append(
            {
                'step_id': st.get('id', UNDEF),
                'index': c['index'],
                'name': st.get('name', UNDEF),
                'approver_id': c['approver_id'],
                'sla_days': st['sla_days'] if js_truthy(st.get('sla_days')) else 2,
                'escalate_to_id': esc if js_truthy(esc) and esc != c['approver_id'] and esc != input_.get('requester_id') else None,
            }
        )
    return out


def start_approval(ctx: Ctx, process: Any, input_: dict, legacy: Any) -> dict[str, Any]:
    """Resolve the chain for a new submission (legacy = the area's v3 rule when only the fallback would apply)."""
    chain = resolve_chain(ctx, process, input_)
    if len(chain) == 1 and chain[0]['index'] == -1:
        chain = [{'step': {**FALLBACK_STEP, 'name': 'Approval'}, 'index': -1, 'approver_id': legacy}] if js_truthy(legacy) else []
    stored = _store_chain(ctx, chain, input_)
    return {
        'wf_process': process,
        'wf_chain': stored,
        'wf_step': 0,
        'wf_step_at': now_iso(),
        'approver_id': coalesce(stored[0]['approver_id'], None) if stored else None,
    }


def wf_patch(s: dict) -> dict[str, Any]:
    return {
        'wf_process': s.get('wf_process', UNDEF),
        'wf_chain': s.get('wf_chain', UNDEF),
        'wf_step': s.get('wf_step', UNDEF),
        'wf_step_at': s.get('wf_step_at', UNDEF),
    }


def wf_clear() -> dict[str, Any]:
    """Clear the stored chain (after a rejection / send-back the next submission starts over)."""
    return {'wf_chain': [], 'wf_step': 0, 'wf_step_at': None}


def _legacy_step(cur: Any) -> dict:
    return {'step_id': 'legacy', 'index': -1, 'name': 'Approval', 'approver_id': cur, 'sla_days': 2, 'escalate_to_id': None}


def ensure_wf(ctx: Ctx, rec: dict, process: Any, input_: dict, legacy: Any, current_approver: Any = UNDEF) -> dict:
    """The stored state of a record; records submitted before workflows existed get one built now."""
    cur = current_approver if current_approver is not UNDEF else coalesce(rec.get('approver_id', UNDEF), None)
    ch = rec.get('wf_chain', UNDEF)
    step = rec.get('wf_step', UNDEF)
    if js_truthy(ch) and len(ch) and not nullish(step) and js_truthy(_at(ch, step)):
        return rec
    s = start_approval(ctx, process, input_, coalesce(legacy, cur))
    i = next((k for k, c in enumerate(s['wf_chain']) if c['approver_id'] == cur), -1) if js_truthy(cur) else 0
    if i < 0:
        if js_truthy(cur):
            s['wf_chain'] = [_legacy_step(cur), *s['wf_chain'][1:]]
        i = 0
    if not s['wf_chain'] and js_truthy(cur):
        s['wf_chain'] = [_legacy_step(cur)]
    return {**s, 'wf_step': i, 'wf_step_at': coalesce(rec.get('updated_at', UNDEF), now_iso())}


def advance_approval(ctx: Ctx, st: dict, actor_id: Any) -> dict[str, Any]:
    """Record the actor's approval of the current step and move on: {'state', 'next', 'done'}."""
    chain = [dict(c) for c in st['wf_chain']]
    at = now_iso()
    done = _at(chain, st['wf_step'])
    done = None if done is UNDEF or done is None else done
    if done is not None:
        done['decided_by'] = actor_id
        done['decided_at'] = at
    i = st['wf_step'] + 1
    while i < len(chain) and chain[i]['approver_id'] == actor_id:
        chain[i]['decided_by'] = actor_id
        chain[i]['decided_at'] = at
        i += 1
    nxt = chain[i] if i < len(chain) else None
    state = {**st, 'wf_chain': chain, 'wf_step': i if nxt is not None else st['wf_step'], 'wf_step_at': at if nxt is not None else st.get('wf_step_at', UNDEF)}
    return {'state': state, 'next': nxt, 'done': done}


def step_label(rec: Any) -> str | None:
    """ "Step 2 of 2 · CEO approval" — only for multi-step chains."""
    c = _get(rec, 'wf_chain')
    if not js_truthy(c) or len(c) < 2 or nullish(_get(rec, 'wf_step')):
        return None
    s = _at(c, rec['wf_step'])
    return f'Step {js_str(rec["wf_step"] + 1)} of {len(c)} · {js_str(_get(s, "name"))}' if js_truthy(s) else None


def _current(rec: Any) -> Any:
    """rec?.wf_chain?.[rec?.wf_step ?? -1]"""
    c = _get(rec, 'wf_chain')
    if nullish(c):
        return UNDEF
    return _at(c, coalesce(_get(rec, 'wf_step'), -1))


def step_due(ctx: Ctx, rec: Any) -> str | None:
    """Working-day SLA due date of the current step (None when not tracked)."""
    s = _current(rec)
    if not js_truthy(s) or not js_truthy(_get(rec, 'wf_step_at')):
        return None
    sla = s.get('sla_days')
    return add_working_days(rec['wf_step_at'][:10], sla if js_truthy(sla) else 2, calendar_for(ctx))


def escalation_target(ctx: Ctx, rec: Any) -> dict[str, Any] | None:
    s = _current(rec)
    if not js_truthy(_get(s, 'escalate_to_id')) or js_truthy(s.get('escalated_from')):
        return None
    return {'id': s['escalate_to_id'], 'name': user_name(ctx, s['escalate_to_id'])}


def approval_route(ctx: Ctx, rec: Any, pending: bool) -> list[dict] | None:
    """The stored chain as a readable route for detail screens (None for single-step chains)."""
    c = _get(rec, 'wf_chain')
    if not js_truthy(c) or len(c) < 2:
        return None
    out = []
    for i, s in enumerate(c):
        if js_truthy(s.get('decided_by')):
            state = 'done'
        elif pending and i == rec.get('wf_step'):
            state = 'current'
        elif pending:
            state = 'next'
        else:
            state = 'skipped'
        out.append(
            {
                'name': s.get('name', UNDEF),
                'approver_id': s.get('approver_id', UNDEF),
                'approver_name': user_name(ctx, s.get('approver_id')),
                'state': state,
                'decided_by_name': user_name(ctx, s['decided_by']) if js_truthy(s.get('decided_by')) else None,
                'escalated_from_name': user_name(ctx, s['escalated_from']) if js_truthy(s.get('escalated_from')) else None,
            }
        )
    return out


def escalate_if_due(ctx: Ctx, table: str, rec: dict, entity: str, title: str, link: str) -> bool:
    """Lazy escalation: past its SLA with an escalation target, hand the item to that person (once), notify and log it."""
    due = step_due(ctx, rec)
    tgt = escalation_target(ctx, rec)
    if not due or not tgt or ctx.today <= due:
        return False
    chain = [dict(c) for c in rec['wf_chain']]
    s = chain[rec['wf_step']]
    frm = s['approver_id']
    if frm == tgt['id']:
        return False
    s['escalated_from'] = frm
    s['approver_id'] = tgt['id']
    p: dict[str, Any] = {'wf_chain': chain, 'wf_step_at': now_iso()}
    if table == 'tasks':
        p['wf_approver_id'] = tgt['id']
    else:
        p['approver_id'] = tgt['id']
    update(ctx, table, rec['id'], p)
    sla = js_str(s['sla_days'])
    notify(
        ctx,
        tgt['id'],
        'NTF-04',
        'Escalated to you',
        f'{title} waited past its {sla}-working-day deadline with {user_name(ctx, frm)}, so it now needs your decision.',
        link,
    )
    notify(ctx, frm, 'NTF-04', 'Approval escalated', f'{title} passed its deadline and was escalated to {js_str(tgt["name"])}.', link)
    log_activity(
        ctx,
        entity,
        rec['id'],
        'escalated',
        f'Escalated from {user_name(ctx, frm)} to {js_str(tgt["name"])} after the {sla}-working-day deadline ({js_str(s["name"])})',
    )
    return True


# ---------------------------------------------------------------------------------------------------------------------
# configuration API
# ---------------------------------------------------------------------------------------------------------------------
def _role_name(ctx: Ctx, key: Any) -> Any:
    if not js_truthy(key):
        return 'a role'
    r = next((r for r in rows(ctx, 'roles') if r.get('key') == key), None)
    return coalesce(r.get('name', UNDEF), key) if r is not None else key


def _approver_text(ctx: Ctx, a: dict) -> Any:
    t = a.get('approver')
    if t == 'role':
        return _role_name(ctx, a.get('role_id', UNDEF))
    if t == 'user':
        return user_name(ctx, a.get('user_id', UNDEF))
    return (
        {
            'reports_to': 'Reporting manager',
            'skip_level': 'Manager’s manager',
            'project_pm': 'Project manager',
            'project_pm_or_manager': 'Project manager (else reporting manager)',
            'department_head': 'Department head',
            'task_reviewer': 'Task reviewer',
        }.get(t, UNDEF)
        if isinstance(t, str)
        else UNDEF
    )


def step_summary(ctx: Ctx, s: dict) -> str:
    mn = s.get('min_amount_paise')
    return f'{js_str(_approver_text(ctx, s))}{f" above {fmt_money(mn)}" if js_truthy(mn) else ""}'


def chain_summary(ctx: Ctx, steps: list[dict]) -> str:
    """ "Reporting manager → CEO above ₹25,000\""""
    return ' → '.join(step_summary(ctx, s) for s in steps) or 'No approval'


def _same_steps(a: list[dict], b: list[dict]) -> bool:
    def norm(s: list[dict]) -> bytes:
        return js_json_dumps(
            [
                {
                    'n': x.get('name', UNDEF),
                    'a': x.get('approver', UNDEF),
                    'r': coalesce(x.get('role_id', UNDEF), None),
                    'u': coalesce(x.get('user_id', UNDEF), None),
                    'm': coalesce(x.get('min_amount_paise', UNDEF), None),
                    'd': x.get('sla_days', UNDEF),
                    'e': coalesce(x.get('escalate_to', UNDEF), None),
                    's': js_truthy(x.get('allow_self')),
                }
                for x in s
            ]
        )

    return norm(a) == norm(b)


def _wf_view(ctx: Ctx, w: dict) -> dict[str, Any]:
    d = next((x for x in DEFAULT_WORKFLOWS if x['process'] == w['process']), None)
    pc = pending_change_for(ctx, 'workflow', w['id'])
    return {
        **w,
        'process_label': PROCESS_LABEL.get(w['process'], UNDEF),
        'summary': chain_summary(ctx, w['steps']),
        'step_summaries': [step_summary(ctx, s) for s in w['steps']],
        'uses_amount': _includes(AMOUNT_PROCESSES, w['process']),
        'uses_project': _includes(PROJECT_PROCESSES, w['process']),
        'is_default': d is not None and _same_steps(d['steps'], w['steps']),
        'pending_change': {
            'id': pc['id'],
            'summary': pc['summary'],
            'maker_name': user_name(ctx, pc['maker_id']),
            'maker_id': pc['maker_id'],
            'created_at': pc['created_at'],
        }
        if pc
        else None,
    }


def _get_workflow(ctx: Ctx, process: Any) -> dict:
    if not (isinstance(process, str) and process in PROCESS_LABEL):
        err(404, 'NOT_FOUND', 'Workflow not found.')
    w = next((x for x in rows(ctx, 'approval_workflows') if x.get('process') == process), None)
    if w is None:
        err(404, 'NOT_FOUND', 'Workflow not found.')
    return w  # type: ignore[return-value]


APPROVER_TYPES = list(APPROVER_LABEL.keys())
_step_seq = 0
_ID_RE = re.compile(r'[\w-]{1,40}', re.ASCII)


def _gen_step_id() -> str:
    global _step_seq
    n = _step_seq
    _step_seq += 1
    return f's{_b36(now_ms())}{_b36(n)}'


def validate_steps(ctx: Ctx, process: str, input_: Any, acknowledge_fallback: bool) -> list[dict]:
    """Validate an edited step list. Throws 422 with a field path like `steps.1.role_id`."""
    if not isinstance(input_, list) or len(input_) < 1:
        err(422, 'WF_STEPS', 'Add at least one approval step.', 'steps')
    if len(input_) > 5:
        err(422, 'WF_STEPS', 'A workflow can have at most 5 steps. Combine steps or raise the amount thresholds.', 'steps')
    roles = rows(ctx, 'roles')
    amounts = _includes(AMOUNT_PROCESSES, process)
    projects = _includes(PROJECT_PROCESSES, process)
    plabel = PROCESS_LABEL[process].lower()

    def check_target(a: Any, n: int, f: str, label: str) -> Any:
        t = _get(a, 'approver')
        if not _includes(APPROVER_TYPES, t):
            err(422, 'WF_APPROVER', f'Step {n}: choose who {label}.', f'{f}.approver')
        if t == 'task_reviewer' and process != 'task_review':
            err(422, 'WF_APPROVER', f'Step {n}: “Task reviewer” only applies to task reviews.', f'{f}.approver')
        if t in ('project_pm', 'project_pm_or_manager') and not projects:
            err(422, 'WF_APPROVER', f'Step {n}: {plabel} have no project, so a project manager can’t be resolved. Pick another approver.', f'{f}.approver')
        if t == 'role':
            rid = _get(a, 'role_id')
            r = next((x for x in roles if _strict_eq(x.get('key', UNDEF), rid)), None)
            if r is None:
                err(422, 'WF_ROLE', f'Step {n}: choose the role that {label}.', f'{f}.role_id')
            if not js_truthy(r.get('active')):  # type: ignore[union-attr]
                err(422, 'WF_ROLE', f'Step {n}: the role “{js_str(r.get("name", UNDEF))}” is inactive. Pick an active role.', f'{f}.role_id')  # type: ignore[union-attr]
        if t == 'user':
            uid_ = _get(a, 'user_id')
            u = next((x for x in rows(ctx, 'users') if _strict_eq(x.get('id', UNDEF), uid_)), None)
            if u is None:
                err(422, 'WF_USER', f'Step {n}: choose the person who {label}.', f'{f}.user_id')
            if u.get('status') != 'active':  # type: ignore[union-attr]
                err(422, 'WF_USER', f'Step {n}: {js_str(u.get("name", UNDEF))} is not active. Pick someone who can sign in.', f'{f}.user_id')  # type: ignore[union-attr]
        return t

    last_amount: Any = -1
    out: list[dict] = []
    for i, raw in enumerate(input_):
        s = {} if nullish(raw) else raw
        n = i + 1
        f = f'steps.{i}'
        name = js_trim(js_str(coalesce(_get(s, 'name'), '')))
        if js_len(name) < 2:
            err(422, 'WF_NAME', f'Step {n} needs a name (e.g. “Manager approval”).', f'{f}.name')
        if js_len(name) > 60:
            err(422, 'WF_NAME', f'Step {n}: keep the name under 60 characters.', f'{f}.name')
        approver = check_target(s, n, f, 'approves')
        mn: Any = None
        raw_min = _get(s, 'min_amount_paise')
        if raw_min is not None and raw_min is not UNDEF and raw_min != '' and js_number(raw_min) != 0:
            if not amounts:
                err(422, 'WF_AMOUNT', f'Step {n}: {plabel} have no amount, so remove the threshold.', f'{f}.min_amount_paise')
            mn = js_number(raw_min)
            if not is_integer(mn) or mn < 0:
                err(422, 'WF_AMOUNT', f'Step {n}: the amount must be zero or more.', f'{f}.min_amount_paise')
            if mn > 1_00_00_00_000_00:
                err(422, 'WF_AMOUNT', f'Step {n}: that amount looks too large. Check the number of zeros.', f'{f}.min_amount_paise')
            if mn <= last_amount:
                err(
                    422,
                    'WF_AMOUNT_ORDER',
                    f'Step {n}: thresholds must go up from step to step (the previous step starts above {fmt_money(last_amount)}).',
                    f'{f}.min_amount_paise',
                )
            last_amount = mn
        sla = js_number(coalesce(_get(s, 'sla_days'), 2))
        if not is_integer(sla) or sla < 1 or sla > 15:
            err(422, 'WF_SLA', f'Step {n}: the deadline must be 1 to 15 working days.', f'{f}.sla_days')
        esc: Any = None
        e = _get(s, 'escalate_to')
        if js_truthy(e) and js_truthy(_get(e, 'approver')):
            t = check_target(e, n, f'{f}.escalate_to', 'takes over after the deadline')
            if t == 'task_reviewer':
                err(422, 'WF_APPROVER', f'Step {n}: escalate to a person, a role or a manager — not the task reviewer.', f'{f}.escalate_to.approver')
            if (
                t == approver
                and (t != 'role' or _strict_eq(_get(e, 'role_id'), _get(s, 'role_id')))
                and (t != 'user' or _strict_eq(_get(e, 'user_id'), _get(s, 'user_id')))
            ):
                err(
                    422,
                    'WF_ESCALATION',
                    f'Step {n}: escalation goes to the same approver. Pick someone else, or turn escalation off.',
                    f'{f}.escalate_to.approver',
                )
            esc = {
                'approver': t,
                'role_id': js_str(_get(e, 'role_id')) if t == 'role' else None,
                'user_id': js_str(_get(e, 'user_id')) if t == 'user' else None,
            }
        sid = _get(s, 'id')
        id_ = sid if isinstance(sid, str) and _ID_RE.fullmatch(sid) else _gen_step_id()
        out.append(
            {
                'id': id_,
                'name': name,
                'approver': approver,
                'role_id': js_str(_get(s, 'role_id')) if approver == 'role' else None,
                'user_id': js_str(_get(s, 'user_id')) if approver == 'user' else None,
                'min_amount_paise': mn,
                'sla_days': sla,
                'escalate_to': esc,
                'allow_self': js_truthy(_get(s, 'allow_self')),
            }
        )
    if len({x['id'] for x in out}) != len(out):
        for i, x in enumerate(out):
            x['id'] = f's{i + 1}{_b36(now_ms())}'
    if not any(not js_truthy(x['min_amount_paise']) for x in out) and not acknowledge_fallback:
        err(
            422,
            'WF_NO_BASE_STEP',
            f'Every step has an amount threshold, so requests at or below {fmt_money(out[0]["min_amount_paise"])} would have no approver and go to the CEO. '
            'Add a step without an amount, or confirm that fallback.',
            'steps',
        )
    return out


# ---------------------------------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------------------------------
def list_workflows(req: Req) -> Any:
    ctx = req.ctx
    if not has_any(ctx, 'workflows.manage', 'master.view'):
        err(403, 'FORBIDDEN', 'You can’t see approval workflows.')
    all_ = rows(ctx, 'approval_workflows')
    users = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin'))]
    wfs = []
    for p in PROCESS_ORDER:
        w = next((x for x in all_ if x.get('process') == p), None)
        if w is not None:
            wfs.append(w)
    roles = [
        {
            'key': x.get('key', UNDEF),
            'name': x.get('name', UNDEF),
            'active': x.get('active', UNDEF),
            'holders': len([u for u in users if u.get('role_key') == x.get('key')]),
        }
        for x in rows(ctx, 'roles')
    ]
    mc = ctx.tenant.get('maker_checker')
    return {
        'workflows': [_wf_view(ctx, w) for w in wfs],
        'roles': sorted(roles, key=lambda r: locale_key(r['name'])),
        'approver_labels': APPROVER_LABEL,
        'maker_checker': js_truthy(prop(mc, 'enabled')) and 'workflow' in mc['entities'],
    }


def _save(ctx: Ctx, w: dict, steps: list[dict], meta: Any, reset: bool, if_match: Any = UNDEF) -> Any:
    if if_match is not UNDEF and not _strict_eq(if_match, w.get('version', UNDEF)):
        err(409, 'STALE', 'Someone else changed this workflow. Reload to see the latest version.')
    name = js_trim(js_str(coalesce(_get(meta, 'name'), w.get('name', UNDEF))))
    if js_len(name) < 3 or js_len(name) > 80:
        err(422, 'VALIDATION', 'Give the workflow a name of 3 to 80 characters.', 'name')
    desc = js_trim(js_str(coalesce(_get(meta, 'description'), w.get('description', UNDEF))))
    nxt = {
        'name': name,
        'description': _utf16_slice(desc, 0, 300),
        'require_reason_on_reject': coalesce(_get(meta, 'require_reason_on_reject'), w.get('require_reason_on_reject', UNDEF)),
        'notify_requester': coalesce(_get(meta, 'notify_requester'), w.get('notify_requester', UNDEF)),
        'steps': steps,
    }
    unchanged = (
        _same_steps(w['steps'], steps)
        and nxt['name'] == w.get('name')
        and _strict_eq(nxt['description'], w.get('description', UNDEF))
        and _strict_eq(nxt['require_reason_on_reject'], w.get('require_reason_on_reject', UNDEF))
        and _strict_eq(nxt['notify_requester'], w.get('notify_requester', UNDEF))
    )
    if unchanged:
        err(422, 'NO_CHANGE', 'Nothing changed. Edit a step before saving.')
    before = {'name': w.get('name', UNDEF), 'steps': w['steps'], 'summary': chain_summary(ctx, w['steps'])}
    after = {'name': nxt['name'], 'steps': steps, 'summary': chain_summary(ctx, steps)}
    label = PROCESS_LABEL[w['process']]
    return propose(
        ctx,
        {
            'entity': 'workflow',
            'entity_id': w['id'],
            'action': 'update',
            'required_permission': 'workflows.manage',
            'base_version': w['version'],
            'summary': f'Reset {label.lower()} workflow to the default: {after["summary"]}' if reset else f'{label} workflow: {after["summary"]}',
            'before': before,
            'after': after,
            'payload': {'workflow_id': w['id'], **nxt},
        },
    )


def _utf16_slice(s: str, a: int, b: int) -> str:
    """String.prototype.slice in UTF-16 code units"""
    if all(ord(c) <= 0xFFFF for c in s):
        return js_slice(s, a, b)
    units = s.encode('utf-16-le', 'surrogatepass')
    return units[a * 2 : b * 2].decode('utf-16-le', 'surrogatepass')


def put_workflow(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'workflows.manage', 'You can’t change approval workflows.')
    w = _get_workflow(ctx, req.params['process'])
    steps = validate_steps(ctx, w['process'], req.b('steps'), js_truthy(req.b('acknowledge_fallback')))
    return _save(ctx, w, steps, req.body, False, req.if_match)


def reset_workflow(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'workflows.manage', 'You can’t change approval workflows.')
    w = _get_workflow(ctx, req.params['process'])
    d = next(x for x in DEFAULT_WORKFLOWS if x['process'] == w['process'])
    if _same_steps(d['steps'], w['steps']):
        err(422, 'NO_CHANGE', 'This workflow already uses the default steps.')
    return _save(ctx, w, thaw(d['steps']), {'name': d['name'], 'description': d['description']}, True, req.if_match)


def simulate_workflow(req: Req) -> Any:
    ctx = req.ctx
    if not has_any(ctx, 'workflows.manage', 'master.view'):
        err(403, 'FORBIDDEN', 'You can’t test approval workflows.')
    w = _get_workflow(ctx, req.params['process'])
    requester = find(ctx, 'users', js_str(coalesce(req.b('requester_id'), '')), 'Requester')
    body_steps = req.b('steps')
    steps = validate_steps(ctx, w['process'], body_steps, True) if js_truthy(body_steps) else w['steps']
    ra = req.b('amount_paise')
    amount = None if (ra is None or ra is UNDEF or ra == '') else js_number(ra)
    if amount is not None and (not is_finite(amount) or amount < 0):
        err(422, 'VAL_AMOUNT', 'Enter an amount of zero or more.', 'amount_paise')
    project_id = js_str(req.b('project_id')) if js_truthy(req.b('project_id')) else None
    if project_id:
        find(ctx, 'projects', project_id, 'Project')
    if js_truthy(req.b('reviewer_id')):
        reviewer_id = js_str(req.b('reviewer_id'))
    elif project_id:
        p = next((x for x in rows(ctx, 'projects') if x['id'] == project_id), None)
        reviewer_id = coalesce(p.get('pm_id', UNDEF), None) if p is not None else None
    else:
        reviewer_id = requester.get('reports_to_id', UNDEF)
    input_ = {'requester_id': requester['id'], 'amount_paise': amount, 'project_id': project_id, 'reviewer_id': reviewer_id}
    chain = resolve_chain(ctx, w['process'], input_, steps)
    used = {c['index'] for c in chain}
    skipped = []
    for i, s in enumerate(steps):
        if i in used:
            continue
        if not _applies(s, amount):
            skipped.append({'name': s.get('name', UNDEF), 'reason': f'Only above {fmt_money(s.get("min_amount_paise", UNDEF))}'})
            continue
        id_ = resolve_approver(ctx, s, input_)
        if not js_truthy(id_):
            skipped.append({'name': s.get('name', UNDEF), 'reason': 'Nobody matches (no active person in that position)'})
        elif id_ == requester['id']:
            skipped.append({'name': s.get('name', UNDEF), 'reason': f'{js_str(requester.get("name", UNDEF))} can’t approve their own request'})
        else:
            skipped.append({'name': s.get('name', UNDEF), 'reason': f'Same person as the step before ({user_name(ctx, id_)})'})
    fallback = len(chain) == 1 and chain[0]['index'] == -1
    stored = _store_chain(ctx, chain, input_)
    auto = fallback and not js_truthy(requester.get('reports_to_id')) and w['process'] in ('leave', 'timesheet')
    out: list[dict] = []
    if not auto:
        for c in stored:
            u = _user(ctx, c['approver_id'])
            d = steps[c['index']] if c['index'] >= 0 else None
            out.append(
                {
                    'index': c['index'],
                    'name': c['name'],
                    'approver_id': c['approver_id'],
                    'approver_name': coalesce(u.get('name', UNDEF), '—') if u is not None else '—',
                    'approver_title': coalesce(u.get('title', UNDEF), '') if u is not None else '',
                    'sla_days': c['sla_days'],
                    'escalate_to_name': user_name(ctx, c['escalate_to_id']) if js_truthy(c['escalate_to_id']) else None,
                    'reason': _approver_text(ctx, d) if d is not None else 'Nobody in the workflow could approve, so the top approver decides',
                }
            )
    if auto:
        summary = f'{js_str(requester.get("name", UNDEF))} has no manager, so this is approved automatically.'
    elif out:
        summary = ' → '.join(js_str(s['approver_name']) for s in out)
    else:
        summary = 'No approver'
    return {'steps': out, 'skipped': skipped, 'fallback': fallback, 'auto_approved': auto, 'summary': summary}


def register(r: Any) -> None:
    """called by app/routes/company.py (company.ts: registerWorkflows(r))"""
    r.get('/workflows', list_workflows)
    r.put('/workflows/:process', put_workflow)
    r.post('/workflows/:process/reset', reset_workflow)
    r.post('/workflows/:process/simulate', simulate_workflow)


def _apply_workflow(ctx: Ctx, cs: dict) -> dict[str, Any]:
    p = cs['payload']
    w = find(ctx, 'approval_workflows', p['workflow_id'], 'Workflow')
    assert_base_version(cs, w['version'])
    out = update(
        ctx,
        'approval_workflows',
        w['id'],
        {
            'name': p.get('name', UNDEF),
            'description': p.get('description', UNDEF),
            'require_reason_on_reject': p.get('require_reason_on_reject', UNDEF),
            'notify_requester': p.get('notify_requester', UNDEF),
            'steps': p.get('steps', UNDEF),
        },
    )
    proposed = f' (proposed by {user_name(ctx, cs["maker_id"])})' if cs['id'] != 'cs_direct' else ''
    log_activity(
        ctx, 'workflow', w['id'], 'updated', f'{js_str(PROCESS_LABEL.get(w["process"], UNDEF))} workflow is now: {chain_summary(ctx, p["steps"])}{proposed}'
    )
    persist()
    return {'result': _wf_view(ctx, out), 'result_id': w['id']}


register_applier('workflow', _apply_workflow)
