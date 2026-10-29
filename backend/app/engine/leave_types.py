"""Leave types & policies (v9, D10) — the tenant's leave-type master on top of `leave_policies`.

A policy row is one leave TYPE (`type` = its code). Built-in codes keep their labels (casual, sick, earned,
compensatory, optional_holiday; `lwp` needs no policy and is always available); tenants add custom codes
(`is_custom`). Optional fields on a policy (absent on v8 rows = v8 behaviour):

    name, description, active (absent = true), encashable, encash_max_days,
    applicable_role_keys / applicable_location_ids / applicable_employment_types (empty or absent = everyone),
    attachment_after_days (None = never; 0 = always; N = when a request is MORE than N days), sort_order

Accrual: 'monthly' (1/12 of the quota per month worked), 'upfront' (the year's quota at the start of the FY, pro-rated
for joiners — v8), 'annual' (the full quota at the start of every FY, also in the year someone joins).

Leave-request contract (A2's form and POST /leave-requests): `check_leave_application(ctx, user, typ, days,
has_attachment)` raises 422 LEAVE_TYPE_INACTIVE / LEAVE_NOT_APPLICABLE / LEAVE_ATTACHMENT_REQUIRED (field
'attachment'); `attachment_required(policy, days)` answers the same question for the form.
"""

from __future__ import annotations

import re
from typing import Any

from app.db.uow import current_scope
from app.errors import err

__all__ = [
    'BUILTIN_TYPES',
    'BUILTIN_LABEL',
    'ACCRUALS',
    'EMPLOYMENT_TYPES',
    'CODE_RE',
    'policies_of',
    'policy_of',
    'leave_type_codes',
    'leave_type_name',
    'is_active',
    'applies_to',
    'attachment_required',
    'check_leave_application',
    'custom_leave_name',
]

# v8 order of the built-in types (LEAVE_TYPES of hr.py / timesheets.py)
BUILTIN_TYPES = ['casual', 'sick', 'earned', 'compensatory', 'lwp', 'optional_holiday']
BUILTIN_LABEL = {
    'casual': 'Casual',
    'sick': 'Sick',
    'earned': 'Earned',
    'compensatory': 'Comp-off',
    'lwp': 'Leave without pay',
    'optional_holiday': 'Optional holiday',
}
ACCRUALS = ('monthly', 'upfront', 'annual')
EMPLOYMENT_TYPES = ('full_time', 'part_time', 'contract', 'intern')
CODE_RE = re.compile(r'^[a-z][a-z0-9_]{1,39}\Z')


def _tid(ctx_or_tid: Any) -> str:
    return ctx_or_tid if isinstance(ctx_or_tid, str) else ctx_or_tid.tenant_id


def policies_of(ctx_or_tid: Any) -> list:
    return current_scope().tenant_rows('leave_policies', _tid(ctx_or_tid))


def policy_of(ctx_or_tid: Any, code: Any) -> dict | None:
    return next((p for p in policies_of(ctx_or_tid) if p.get('type') == code), None)


def is_active(p: dict | None) -> bool:
    return p is not None and p.get('active') is not False


def _order_key(p: dict) -> tuple:
    t = p.get('type')
    so = p.get('sort_order')
    builtin_rank = BUILTIN_TYPES.index(t) if t in BUILTIN_TYPES else 100
    return (so if isinstance(so, int) and not isinstance(so, bool) else 1000 + builtin_rank, builtin_rank, (p.get('name') or t or '').lower())


def leave_type_codes(ctx_or_tid: Any) -> list[str]:
    """every valid leave code of the tenant: the built-ins (v8 order) + its custom codes (active or not — history)"""
    custom = [p['type'] for p in sorted(policies_of(ctx_or_tid), key=_order_key) if p.get('type') not in BUILTIN_TYPES and isinstance(p.get('type'), str)]
    return [*BUILTIN_TYPES, *dict.fromkeys(custom)]


def leave_type_name(ctx_or_tid: Any, code: Any) -> str:
    p = policy_of(ctx_or_tid, code)
    n = p.get('name') if p else None
    if isinstance(n, str) and n.strip():
        return n.strip()
    if code in BUILTIN_LABEL:
        return BUILTIN_LABEL[code]
    return str(code).replace('_', ' ').capitalize() if isinstance(code, str) else 'Leave'


def custom_leave_name(code: Any, default: Any = None) -> Any:
    """label of a code that is not a built-in (used by app.domain.labels.LEAVE_TYPE for custom codes)"""
    try:
        s = current_scope()
    except RuntimeError:
        return default
    tid = getattr(s, 'tenant_id', None)
    if not isinstance(tid, str) or not isinstance(code, str):
        return default
    p = policy_of(tid, code)
    if p is None:
        return default
    n = p.get('name')
    return n.strip() if isinstance(n, str) and n.strip() else code.replace('_', ' ').capitalize()


def _lst(v: Any) -> list:
    return list(v) if isinstance(v, (list, tuple)) else []


def applies_to(p: dict, user: dict | None) -> bool:
    """is this person eligible for the leave type (roles / locations / employment types; empty = everyone)"""
    if user is None:
        return False
    roles = _lst(p.get('applicable_role_keys'))
    if roles and user.get('role_key') not in roles:
        return False
    locs = _lst(p.get('applicable_location_ids'))
    if locs and user.get('location_id') not in locs:
        return False
    ets = _lst(p.get('applicable_employment_types'))
    if ets and (user.get('employment_type') or 'full_time') not in ets:
        return False
    return True


def attachment_required(p: dict | None, days: Any) -> bool:
    """a supporting document is needed: attachment_after_days None = never, 0 = always, N = more than N days"""
    if p is None:
        return False
    n = p.get('attachment_after_days')
    if n is None or isinstance(n, bool) or not isinstance(n, (int, float)):
        return False
    try:
        return float(days) > float(n) or n == 0
    except (TypeError, ValueError):
        return n == 0


def check_leave_application(ctx: Any, user: dict, typ: Any, days: Any, has_attachment: bool) -> None:
    """policy rules for a new leave application (raises 422)"""
    if typ == 'lwp':
        return
    p = policy_of(ctx, typ)
    if p is None:
        if typ in BUILTIN_TYPES:
            return  # v8: a built-in type without a policy has no quota and no rules
        err(422, 'VALIDATION', 'Pick a leave type.', 'type')
    name = leave_type_name(ctx, typ)
    if not is_active(p):
        err(422, 'LEAVE_TYPE_INACTIVE', f'{name} leave is no longer offered. Pick another leave type.', 'type')
    if not applies_to(p, user):  # type: ignore[arg-type]
        err(
            422,
            'LEAVE_NOT_APPLICABLE',
            f'{name} leave doesn’t apply to you (it is limited by role, location or employment type). Pick another leave type.',
            'type',
        )
    if attachment_required(p, days) and not has_attachment:
        n = p.get('attachment_after_days')  # type: ignore[union-attr]
        when = 'for every request' if n == 0 else f'for more than {n:g} day{"" if n == 1 else "s"}'
        err(422, 'LEAVE_ATTACHMENT_REQUIRED', f'{name} leave needs a supporting document {when}. Attach it and apply again.', 'attachment')


def offered_leave_codes(ctx_or_tid: Any, user: dict | None) -> list[str]:
    """leave codes offered to a person on the timesheet / HR entry: the built-ins (v8) + the custom types that are
    switched on and apply to them"""
    custom = [
        p['type']
        for p in sorted(policies_of(ctx_or_tid), key=_order_key)
        if p.get('type') not in BUILTIN_TYPES and isinstance(p.get('type'), str) and is_active(p) and applies_to(p, user)
    ]
    return [*BUILTIN_TYPES, *dict.fromkeys(custom)]
