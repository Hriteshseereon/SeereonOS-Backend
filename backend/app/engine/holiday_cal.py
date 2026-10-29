"""Holiday calendars (v9, D10) — which holidays apply to whom.

A tenant has one or more holiday calendars (table holiday_calendars). Every holiday row belongs to one calendar:
`holidays.calendar_id`, ABSENT = the tenant's BASE calendar, whose id is always ``base_calendar_id(tenant_id)``
('hcal_<tenant id>'). The base row is materialised on the first calendar write (ensure_base); until then the tenant
has exactly one (virtual) calendar and every holiday belongs to it — the v8 behaviour.

Exactly one calendar is the DEFAULT: the calendar of locations without `holiday_calendar_id` and of people without a
location. A person's calendar = the calendar of their location, else the default. Working-day maths for a person
(leave days, timesheet targets, costing per person) uses that calendar; company-wide maths uses the default calendar.

Kept free of app.ctx imports (ctx.calendar_for imports this module).
"""

from __future__ import annotations

from typing import Any

from app.db.uow import current_scope

__all__ = [
    'BASE_NAME',
    'base_calendar_id',
    'calendars_of',
    'default_calendar_id',
    'calendar_of_holiday',
    'calendar_id_for_location',
    'calendar_id_for_user',
    'holidays_in',
    'holidays_for_user',
    'holidays_default',
    'has_calendars',
    'virtual_base',
]

BASE_NAME = 'Company holidays'


def base_calendar_id(tenant_id: str) -> str:
    return f'hcal_{tenant_id}'


def _tenant_rows(table: str, tenant_id: str) -> list:
    return current_scope().tenant_rows(table, tenant_id)


def has_calendars(tenant_id: str) -> bool:
    """the tenant has materialised calendar rows (else: one virtual base calendar, every holiday in it)"""
    return bool(_tenant_rows('holiday_calendars', tenant_id))


def virtual_base(tenant_id: str) -> dict[str, Any]:
    return {
        'id': base_calendar_id(tenant_id),
        'tenant_id': tenant_id,
        'name': BASE_NAME,
        'description': '',
        'is_default': True,
        'active': True,
        'version': 0,
        'virtual': True,
    }


def calendars_of(tenant_id: str) -> list[dict]:
    """the tenant's calendars (the virtual base one when none were created yet), default first"""
    lst = list(_tenant_rows('holiday_calendars', tenant_id))
    base = base_calendar_id(tenant_id)
    if not any(c['id'] == base for c in lst):
        # the base calendar always exists logically (it holds every holiday without calendar_id)
        lst.insert(0, {**virtual_base(tenant_id), 'is_default': not any(c.get('is_default') for c in lst)})
    return sorted(lst, key=lambda c: (0 if c.get('is_default') else 1, (c.get('name') or '').lower()))


def default_calendar_id(tenant_id: str) -> str:
    for c in _tenant_rows('holiday_calendars', tenant_id):
        if c.get('is_default'):
            return c['id']
    return base_calendar_id(tenant_id)


def calendar_of_holiday(h: dict, tenant_id: str) -> str:
    cid = h.get('calendar_id')
    return cid if isinstance(cid, str) and cid else base_calendar_id(tenant_id)


def _known(tenant_id: str, cid: Any) -> bool:
    if not isinstance(cid, str) or not cid:
        return False
    if cid == base_calendar_id(tenant_id):
        return True
    return any(c['id'] == cid for c in _tenant_rows('holiday_calendars', tenant_id))


def calendar_id_for_location(tenant_id: str, loc: dict | None) -> str:
    cid = loc.get('holiday_calendar_id') if loc else None
    if _known(tenant_id, cid):
        c = next((x for x in _tenant_rows('holiday_calendars', tenant_id) if x['id'] == cid), None)
        if c is None or c.get('active', True):
            return cid  # type: ignore[return-value]
    return default_calendar_id(tenant_id)


def calendar_id_for_user(tenant_id: str, user: dict | None) -> str:
    if not has_calendars(tenant_id):
        return base_calendar_id(tenant_id)
    loc_id = user.get('location_id') if user else None
    loc = current_scope().find_by('locations', 'id', loc_id) if isinstance(loc_id, str) and loc_id else None
    if loc is not None and loc.get('tenant_id') != tenant_id:
        loc = None
    return calendar_id_for_location(tenant_id, loc)


def holidays_in(tenant_id: str, calendar_id: str) -> list:
    """holiday rows of one calendar (the very same list object as rows(ctx, 'holidays') while the tenant has no
    calendars — keeps the cache keys of the analytics stable)"""
    hol = _tenant_rows('holidays', tenant_id)
    if not has_calendars(tenant_id) and calendar_id == base_calendar_id(tenant_id):
        return hol
    return [h for h in hol if calendar_of_holiday(h, tenant_id) == calendar_id]


def holidays_default(tenant_id: str) -> list:
    return holidays_in(tenant_id, default_calendar_id(tenant_id))


def holidays_for_user(tenant_id: str, user_or_id: Any) -> list:
    """holiday rows that apply to a person (their location's calendar)"""
    if not has_calendars(tenant_id):
        return _tenant_rows('holidays', tenant_id)
    u = user_or_id
    if isinstance(user_or_id, str):
        u = current_scope().find_by('users', 'id', user_or_id)
    return holidays_in(tenant_id, calendar_id_for_user(tenant_id, u if isinstance(u, dict) else None))
