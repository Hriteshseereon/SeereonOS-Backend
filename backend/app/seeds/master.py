"""Organisation master data defaults — port of the provisioning part of src/mock/seeds/master.ts.

provision_master(scope, tenant, base) runs for every new tenant (signup). It only creates what is missing.
Returns the (possibly) updated tenant row (org profile added) — the caller stores it."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.db.uow import Scope
from app.js import coalesce, js_or, prop


def prefix_from_name(name: str) -> str:
    """'Kalinga Softworks' -> 'KS', 'Acme' -> 'ACM'"""
    words = [
        w for w in re.split(r'\s+', re.sub(r'[^A-Za-z0-9 ]+', ' ', name)) if w and not re.fullmatch(r'(pvt|ltd|private|limited|llp|inc|the|and|of)', w, re.I)
    ]
    p = ''.join(w[0] for w in words)[:4] if len(words) > 1 else (words[0] if words else 'EMP')[:3]
    return p.upper() or 'EMP'


def calendar_days(t: dict) -> list[int]:
    """working days a default shift covers from the tenant calendar (Sat included unless all Saturdays are off)"""
    settings = t.get('settings') or {}
    off = set(coalesce(prop(settings, 'calendar.weekly_off'), [0]))
    days = [d for d in (1, 2, 3, 4, 5) if d not in off]
    if settings.get('calendar.saturday_rule') != 'all_off' and 6 not in off:
        days.append(6)
    return days


def blank_org(t: dict) -> dict[str, Any]:
    return {
        'legal_name': t['name'],
        'brand_name': t['name'],
        'cin': '',
        'gstin': '',
        'pan': '',
        'tan': '',
        'registered_address': '',
        'corporate_address': '',
        'city': '',
        'state': '',
        'pincode': '',
        'country': 'India',
        'phone': '',
        'email': '',
        'website': '',
        'incorporated_on': None,
        'employee_code_prefix': prefix_from_name(t['name']),
        'employee_code_next': 1,
        'bank_name': '',
        'bank_account_masked': '',
        'ifsc': '',
    }


def provision_master(s: Scope, tenant: dict, base: Callable[[], dict]) -> dict:
    tid = tenant['id']

    def has(table: str) -> bool:
        return any(x.get('tenant_id') == tid for x in s.view(table))

    if not tenant.get('org'):
        tenant = {**tenant, 'org': blank_org(tenant)}
    if not has('locations'):
        s.append(
            'locations',
            {
                **base(),
                'name': 'Head office',
                'code': 'HO',
                'address': '',
                'city': '',
                'state': '',
                'timezone': js_or(tenant.get('time_zone'), 'Asia/Kolkata'),
                'holiday_calendar': 'Company holidays',
                'is_head_office': True,
                'active': True,
            },
        )
    if not has('grades'):
        for code, name, lm in (('L1', 'Trainee', 1.0), ('L2', 'Associate', 1.0), ('L3', 'Senior', 1.0), ('L4', 'Lead', 1.1), ('L5', 'Head', 1.2)):
            s.append(
                'grades',
                {
                    **base(),
                    'code': code,
                    'name': name,
                    'min_ctc_paise': None,
                    'max_ctc_paise': None,
                    'leave_multiplier': int(lm) if float(lm).is_integer() else lm,
                    'active': True,
                },
            )
    if not has('cost_centres') and not has('departments'):
        for name, code, kind in (
            ('Management', 'MGT', 'management'),
            ('Delivery', 'DEL', 'delivery'),
            ('Sales', 'SAL', 'sales'),
            ('Operations', 'OPS', 'operations'),
        ):
            cc = {**base(), 'code': f'CC-{code}', 'name': name, 'owner_id': None, 'active': True}
            s.append('cost_centres', cc)
            s.append(
                'departments',
                {**base(), 'name': name, 'code': code, 'head_id': None, 'parent_id': None, 'cost_centre_id': cc['id'], 'kind': kind, 'active': True},
            )
    if not has('designations'):

        def dep(kind: str) -> Any:
            return next((d['id'] for d in s.view('departments') if d.get('tenant_id') == tid and d.get('kind') == kind), None)

        def gr(code: str) -> Any:
            return next((g['id'] for g in s.view('grades') if g.get('tenant_id') == tid and g.get('code') == code), None)

        for name, kind, g, role in (
            ('Managing Director', 'management', 'L5', 'ceo'),
            ('Project Manager', 'delivery', 'L4', 'project_manager'),
            ('Software Engineer', 'delivery', 'L2', 'employee'),
            ('Sales Executive', 'sales', 'L2', 'sales_executive'),
            ('HR Executive', 'operations', 'L2', 'hr_executive'),
        ):
            s.append('designations', {**base(), 'name': name, 'department_id': dep(kind), 'grade_id': gr(g), 'default_role_id': role, 'active': True})
    if not has('shifts'):
        settings = tenant.get('settings') or {}
        s.append(
            'shifts',
            {
                **base(),
                'name': 'General shift',
                'code': 'GEN',
                'start_time': '09:30',
                'end_time': '18:30',
                'break_minutes': 60,
                'grace_minutes': 15,
                'working_days': calendar_days(tenant),
                'night_shift': False,
                'min_hours': coalesce(prop(settings, 'calendar.min_hours_per_day'), 8),
                'is_default': True,
                'active': True,
            },
        )
    return tenant
