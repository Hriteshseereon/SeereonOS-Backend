"""v9 admin settings (A5, decision D10): leave types & policies, holiday calendars, financial-year opening balances.

Endpoints (all server-validated, audit-logged with log_activity, permission-gated):

  Leave types & policies (people.manage to change; people.view_all / master.view / people.manage to read)
    GET    /admin/leave-types                 the tenant's leave types (built-in + custom) with usage and options
    POST   /admin/leave-types                 configure a built-in type (body.code) or add a custom one
    PATCH  /admin/leave-types/:code           change a type's policy (If-Match = version)
    DELETE /admin/leave-types/:code           remove an unused custom type (others: switch it off)
    GET    /leave/options                     (everyone) the types the signed-in person may apply for, their balance
                                              and the attachment rule (attachment_after_days) — the leave form uses it

  Holiday calendars (holidays.edit to change; everyone reads)
    GET    /holiday-calendars                 calendars, holiday counts, assigned locations
    POST   /holiday-calendars                 new calendar (optionally copying another calendar's holidays)
    PATCH  /holiday-calendars/:id             rename / describe / (de)activate
    DELETE /holiday-calendars/:id             delete an unassigned, non-default calendar and its holidays
    POST   /holiday-calendars/:id/default     make it the default calendar
    PUT    /holiday-calendars/:id/locations   {location_ids}: the locations that use this calendar
  (holidays themselves: /holidays in settings.py, now with calendar_id)

  Financial-year opening balances (finance.view to read, finance.edit to change)
    GET    /finance/openings                  ?fy=2026 (default: current FY): lines, totals, lock state, FYs
    PUT    /finance/openings/:fy              {lines, notes} (If-Match = version) — refused while locked
    POST   /finance/openings/:fy/lock         lock by hand
    POST   /finance/openings/:fy/reopen       {reason ≥ 10 chars} — maker-checker entity 'opening_balance'
"""

from __future__ import annotations

import re
from typing import Any

from app.ctx import Ctx, Req, err, has, insert, log_activity, now_iso, remove, require_action, require_any, rows, uid, update, user_name
from app.domain.calendar import fy_label, fy_range, fy_start_year, is_real_iso_date, parse_iso, working_days_in_month
from app.domain.money import fmt_money
from app.engine import holiday_cal as hc
from app.engine.leave_types import (
    ACCRUALS,
    BUILTIN_LABEL,
    BUILTIN_TYPES,
    CODE_RE,
    EMPLOYMENT_TYPES,
    applies_to,
    attachment_required,
    is_active,
    leave_type_name,
    policy_of,
)
from app.engine.maker_checker import pending_brief, propose, register_applier
from app.engine.openings import KIND_LABEL, KINDS, current_fy, fy_month, opening_row, state_of, totals_of
from app.js import UNDEF, is_finite, js_number, js_round, js_str, js_trim, js_truthy, locale_key

__all__ = ['register']

_ACCRUAL_LABEL = {
    'monthly': 'Monthly — 1/12 of the quota each month',
    'upfront': 'Upfront — the year’s quota at the start of the FY, pro-rated for joiners',
    'annual': 'Annual — the full quota at the start of every FY, also in the year someone joins',
}
_EMPLOYMENT_LABEL = {'full_time': 'Full-time', 'part_time': 'Part-time', 'contract': 'Contract', 'intern': 'Intern'}
_CONFIGURABLE_BUILTINS = [t for t in BUILTIN_TYPES if t != 'lwp']


def _body(req: Req) -> dict:
    return req.body if isinstance(req.body, dict) else {}


def _s(v: Any) -> str:
    return js_trim(v) if isinstance(v, str) else ''


def _name(ctx: Ctx) -> str:
    return js_str(ctx.user.get('name', UNDEF))


# =====================================================================================================================
# Leave types & policies
# =====================================================================================================================
def _num(v: Any, field: str, label: str, lo: float, hi: float, half_ok: bool = True) -> float | int:
    if isinstance(v, bool) or v is None or v == '':
        err(422, 'VALIDATION', f'Enter {label}.', field)
    n = js_number(v)
    if not is_finite(n) or n < lo or n > hi:
        err(422, 'VALIDATION', f'{label[0].upper() + label[1:]} must be between {lo:g} and {hi:g}.', field)
    if half_ok and n * 2 != js_round(n * 2):
        err(422, 'VALIDATION', f'{label[0].upper() + label[1:]} can be whole or half days only.', field)
    if not half_ok and n != js_round(n):
        err(422, 'VALIDATION', f'{label[0].upper() + label[1:]} must be a whole number.', field)
    return int(n) if n == int(n) else n


def _id_list(v: Any, field: str, allowed: set[str], what: str) -> list[str]:
    if v is None or v is UNDEF:
        return []
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        err(422, 'VALIDATION', f'Send the {what} as a list.', field)
    bad = [x for x in v if x not in allowed]
    if bad:
        err(422, 'VALIDATION', f'Unknown {what}: {", ".join(bad[:3])}.', field)
    return list(dict.fromkeys(v))


def _slug(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    if not s or not s[0].isalpha():
        s = 'leave_' + s
    return s[:40]


def _leave_input(ctx: Ctx, b: dict, cur: dict | None, code: str) -> dict[str, Any]:
    def v(k: str) -> Any:
        return b[k] if k in b else (cur.get(k, UNDEF) if cur else UNDEF)

    out: dict[str, Any] = {}
    nm = _s(v('name')) if v('name') is not UNDEF else ''
    if not nm:
        nm = BUILTIN_LABEL.get(code, '')
    if len(nm) < 2:
        err(422, 'VALIDATION', 'Give the leave type a name (at least 2 characters).', 'name')
    if len(nm) > 40:
        err(422, 'VALIDATION', 'Keep the name under 40 characters.', 'name')
    for p in rows(ctx, 'leave_policies'):
        if p.get('type') != code and leave_type_name(ctx, p.get('type')).lower() == nm.lower():
            err(409, 'LEAVE_TYPE_EXISTS', f'A leave type called “{nm}” already exists.', 'name')
    for bc, bl in BUILTIN_LABEL.items():
        if bc != code and bl.lower() == nm.lower():
            err(409, 'LEAVE_TYPE_EXISTS', f'“{nm}” is a built-in leave type. Configure it instead of adding a copy.', 'name')
    out['name'] = nm
    desc = _s(v('description')) if v('description') is not UNDEF else ''
    if len(desc) > 400:
        err(422, 'VALIDATION', 'Keep the description under 400 characters.', 'description')
    out['description'] = desc
    ad = v('annual_days')
    out['annual_days'] = _num(0 if ad is UNDEF else ad, 'annual_days', 'the yearly quota', 0, 365)
    cf = v('carry_forward_max')
    out['carry_forward_max'] = _num(0 if cf is UNDEF else cf, 'carry_forward_max', 'the carry-forward cap', 0, 365)
    acc = v('accrual')
    acc = 'monthly' if acc is UNDEF else acc
    if acc not in ACCRUALS:
        err(422, 'VALIDATION', 'Choose how the quota accrues: monthly, upfront or annual.', 'accrual')
    out['accrual'] = acc
    out['encashable'] = js_truthy(v('encashable')) if v('encashable') is not UNDEF else False
    em = v('encash_max_days')
    out['encash_max_days'] = (
        None if (em is UNDEF or em is None or em == '' or not out['encashable']) else _num(em, 'encash_max_days', 'the encashment cap', 0, 365)
    )
    roles = {r['key'] for r in rows(ctx, 'roles')}
    locs = {loc['id'] for loc in rows(ctx, 'locations')}
    out['applicable_role_keys'] = _id_list(v('applicable_role_keys'), 'applicable_role_keys', roles, 'roles')
    out['applicable_location_ids'] = _id_list(v('applicable_location_ids'), 'applicable_location_ids', locs, 'locations')
    out['applicable_employment_types'] = _id_list(v('applicable_employment_types'), 'applicable_employment_types', set(EMPLOYMENT_TYPES), 'employment types')
    at = v('attachment_after_days')
    out['attachment_after_days'] = (
        None if (at is UNDEF or at is None or at == '') else int(_num(at, 'attachment_after_days', 'the attachment threshold', 0, 365, half_ok=False))
    )
    act = v('active')
    out['active'] = True if act is UNDEF else js_truthy(act)
    so = v('sort_order')
    if so is not UNDEF and so is not None and so != '':
        out['sort_order'] = int(_num(so, 'sort_order', 'the order', 0, 999, half_ok=False))
    if code == 'optional_holiday' and out['carry_forward_max']:
        err(422, 'VALIDATION', 'Optional holidays can’t be carried forward.', 'carry_forward_max')
    return out


def _usage(ctx: Ctx) -> dict[str, dict[str, Any]]:
    fm = fy_month(ctx)
    rg = fy_range(fy_start_year(ctx.today, fm), fm)
    u: dict[str, dict[str, Any]] = {}
    for r in rows(ctx, 'leave_requests'):
        x = u.setdefault(r.get('type'), {'requests': 0, 'pending': 0, 'days_fy': 0, 'records': 0})
        x['requests'] += 1
        if r.get('status') == 'pending':
            x['pending'] += 1
    for lv in rows(ctx, 'leave'):
        x = u.setdefault(lv.get('type'), {'requests': 0, 'pending': 0, 'days_fy': 0, 'records': 0})
        x['records'] += 1
        if rg['from'] <= lv['date'] <= rg['to']:
            x['days_fy'] += 0.5 if lv.get('portion') == 'half' else 1
    return u


def _people(ctx: Ctx) -> list[dict]:
    return [u for u in rows(ctx, 'users') if not u.get('is_super_admin') and u.get('status') == 'active']


def _type_view(ctx: Ctx, code: str, p: dict | None, usage: dict, people: list[dict]) -> dict[str, Any]:
    us = usage.get(code, {'requests': 0, 'pending': 0, 'days_fy': 0, 'records': 0})
    base = {
        'code': code,
        'name': leave_type_name(ctx, code),
        'builtin': code in BUILTIN_TYPES,
        'is_custom': code not in BUILTIN_TYPES,
        'configured': p is not None,
        'configurable': code != 'lwp',
        'usage': us,
        'in_use': us['requests'] + us['records'] > 0,
    }
    if p is None:
        return {
            **base,
            'id': None,
            'version': None,
            'active': True,
            'description': 'Always available, without a quota (salary is cut for the days).' if code == 'lwp' else '',
            'annual_days': None,
            'accrual': None,
            'carry_forward_max': None,
            'encashable': False,
            'encash_max_days': None,
            'applicable_role_keys': [],
            'applicable_location_ids': [],
            'applicable_employment_types': [],
            'attachment_after_days': None,
            'sort_order': None,
            'eligible_people': len(people),
        }
    return {
        **base,
        'id': p['id'],
        'version': p['version'],
        'active': is_active(p),
        'description': p.get('description') or '',
        'annual_days': p.get('annual_days'),
        'accrual': p.get('accrual'),
        'carry_forward_max': p.get('carry_forward_max'),
        'encashable': bool(p.get('encashable')),
        'encash_max_days': p.get('encash_max_days'),
        'applicable_role_keys': list(p.get('applicable_role_keys') or []),
        'applicable_location_ids': list(p.get('applicable_location_ids') or []),
        'applicable_employment_types': list(p.get('applicable_employment_types') or []),
        'attachment_after_days': p.get('attachment_after_days'),
        'sort_order': p.get('sort_order'),
        'eligible_people': sum(1 for u in people if applies_to(p, u)),
        'updated_at': p.get('updated_at'),
    }


def _all_types(ctx: Ctx) -> list[dict]:
    usage = _usage(ctx)
    people = _people(ctx)
    pol = {p['type']: p for p in rows(ctx, 'leave_policies')}
    out = [_type_view(ctx, c, pol.get(c), usage, people) for c in BUILTIN_TYPES]
    out += [_type_view(ctx, c, p, usage, people) for c, p in pol.items() if c not in BUILTIN_TYPES]

    def key(t: dict) -> tuple:
        so = t['sort_order']
        rank = BUILTIN_TYPES.index(t['code']) if t['code'] in BUILTIN_TYPES else 50
        return (so if isinstance(so, int) else 1000 + rank, rank, t['name'].lower())

    return sorted(out, key=key)


def list_leave_types(req: Req) -> Any:
    ctx = req.ctx
    require_any(ctx, ['people.manage', 'people.view_all', 'master.view'], 'Leave policies are visible to HR and admins.')
    fm = fy_month(ctx)
    return {
        'types': _all_types(ctx),
        'accruals': [{'value': a, 'label': _ACCRUAL_LABEL[a]} for a in ACCRUALS],
        'employment_types': [{'value': e, 'label': _EMPLOYMENT_LABEL[e]} for e in EMPLOYMENT_TYPES],
        'roles': [{'key': r['key'], 'name': r['name']} for r in sorted(rows(ctx, 'roles'), key=lambda r: locale_key(r['name'])) if r.get('active')],
        'locations': [
            {'id': loc['id'], 'name': loc['name']} for loc in sorted(rows(ctx, 'locations'), key=lambda x: locale_key(x['name'])) if loc.get('active')
        ],
        'people': len(_people(ctx)),
        'fy_label': fy_label(fy_start_year(ctx.today, fm)),
        'can_edit': has(ctx, 'people.manage') and not ctx.view_as,
    }


def _summary(ctx: Ctx, code: str, d: dict) -> str:
    bits = [f'{d["annual_days"]:g} days/year', d['accrual']]
    if d['carry_forward_max']:
        bits.append(f'carry forward up to {d["carry_forward_max"]:g}')
    if d['encashable']:
        bits.append('encashable')
    if d['attachment_after_days'] is not None:
        bits.append('document always' if d['attachment_after_days'] == 0 else f'document after {d["attachment_after_days"]} days')
    lim = [k for k in ('applicable_role_keys', 'applicable_location_ids', 'applicable_employment_types') if d[k]]
    if lim:
        bits.append('limited to some people')
    if not d['active']:
        bits.append('switched off')
    return f'{d["name"]} leave policy: {", ".join(bits)}'


def create_leave_type(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins can change leave types.')
    b = _body(req)
    code_in = b.get('code')
    if code_in is not None and code_in != '':
        if not isinstance(code_in, str) or not CODE_RE.match(code_in):
            err(422, 'VALIDATION', 'Use a short code of lower-case letters, digits and _ (e.g. maternity).', 'code')
        code = code_in
    else:
        nm = _s(b.get('name'))
        if len(nm) < 2:
            err(422, 'VALIDATION', 'Give the leave type a name (at least 2 characters).', 'name')
        code = _slug(nm)
    if code == 'lwp':
        err(422, 'VALIDATION', 'Leave without pay is always available and has no policy.', 'code')
    if policy_of(ctx, code) is not None:
        err(409, 'LEAVE_TYPE_EXISTS', f'{leave_type_name(ctx, code)} is already set up. Edit it instead.', 'code' if code_in else 'name')
    d = _leave_input(ctx, b, None, code)
    row = insert(ctx, 'leave_policies', {'type': code, **d, 'is_custom': code not in BUILTIN_TYPES})
    log_activity(ctx, 'leave_policy', row['id'], 'created', f'{_name(ctx)} set up {_summary(ctx, code, d)}')
    return _type_view(ctx, code, row, _usage(ctx), _people(ctx))


def patch_leave_type(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins can change leave types.')
    code = req.params['code']
    p = policy_of(ctx, code)
    if p is None:
        err(404, 'NOT_FOUND', 'Leave type not set up yet.')
    d = _leave_input(ctx, _body(req), p, code)
    row = update(ctx, 'leave_policies', p['id'], d, req.if_match)  # type: ignore[index]
    log_activity(ctx, 'leave_policy', row['id'], 'updated', f'{_name(ctx)} changed {_summary(ctx, code, d)}')
    return _type_view(ctx, code, row, _usage(ctx), _people(ctx))


def delete_leave_type(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'people.manage', 'Only HR and admins can change leave types.')
    code = req.params['code']
    p = policy_of(ctx, code)
    if p is None:
        err(404, 'NOT_FOUND', 'Leave type not found.')
    if code in BUILTIN_TYPES:
        err(409, 'LEAVE_TYPE_BUILTIN', f'{leave_type_name(ctx, code)} is a built-in type. Switch it off instead.')
    us = _usage(ctx).get(code)
    if us and us['requests'] + us['records'] > 0:
        err(409, 'LEAVE_TYPE_IN_USE', f'{leave_type_name(ctx, code)} leave has been applied for or recorded. Switch it off instead — the history stays.')
    name = leave_type_name(ctx, code)
    remove(ctx, 'leave_policies', p['id'], req.if_match)  # type: ignore[index]
    log_activity(ctx, 'leave_policy', p['id'], 'deleted', f'{_name(ctx)} deleted the {name} leave type')  # type: ignore[index]
    return {'ok': True}


def leave_options(req: Req) -> Any:
    """the leave types the signed-in person may apply for (form contract for the leave request)"""
    ctx = req.ctx
    from app.routes.hr import balance_for

    me = ctx.user
    out = []
    pol = {p['type']: p for p in rows(ctx, 'leave_policies')}
    codes = [c for c in BUILTIN_TYPES if c in pol or c == 'lwp' or not pol] + [c for c in pol if c not in BUILTIN_TYPES]
    for code in codes:
        p = pol.get(code)
        if p is not None and (not is_active(p) or not applies_to(p, me)):
            continue
        b = balance_for(ctx, me, code) if p is not None else None
        n = p.get('attachment_after_days') if p else None
        out.append(
            {
                'code': code,
                'name': leave_type_name(ctx, code),
                'description': (p.get('description') if p else '') or '',
                'has_quota': p is not None,
                'annual_days': p.get('annual_days') if p else None,
                'accrual': p.get('accrual') if p else None,
                'balance': b,
                'encashable': bool(p.get('encashable')) if p else False,
                'attachment_after_days': n,
                'attachment_rule': None
                if n is None
                else ('A supporting document is required.' if n == 0 else f'A supporting document is required for more than {n} day{"" if n == 1 else "s"}.'),
            }
        )
    order = {c: i for i, c in enumerate(BUILTIN_TYPES)}
    out.sort(
        key=lambda t: (
            pol.get(t['code'], {}).get('sort_order') if isinstance(pol.get(t['code'], {}).get('sort_order'), int) else 1000 + order.get(t['code'], 50),
            t['name'].lower(),
        )
    )
    hol = hc.holidays_for_user(ctx.tenant_id, me)
    cid = hc.calendar_id_for_user(ctx.tenant_id, me)
    cal = next((c for c in hc.calendars_of(ctx.tenant_id) if c['id'] == cid), None)
    return {
        'types': out,
        'calendar': {'id': cid, 'name': cal['name'] if cal else hc.BASE_NAME},
        'optional_holidays': sorted(
            ({'date': h['date'], 'name': h['name']} for h in hol if js_truthy(h.get('optional')) and h['date'] >= ctx.today), key=lambda h: h['date']
        ),
    }


def leave_attachment_required(ctx: Ctx, typ: Any, days: Any) -> bool:
    return attachment_required(policy_of(ctx, typ), days)


# =====================================================================================================================
# Holiday calendars
# =====================================================================================================================
def _cal_or_404(ctx: Ctx, cid: Any) -> dict:
    c = next((x for x in hc.calendars_of(ctx.tenant_id) if x['id'] == cid), None)
    if c is None:
        err(404, 'NOT_FOUND', 'Holiday calendar not found.')
    return c  # type: ignore[return-value]


def ensure_base(ctx: Ctx) -> dict:
    """materialise the base calendar row (holds every holiday without calendar_id)"""
    bid = hc.base_calendar_id(ctx.tenant_id)
    cur = next((c for c in rows(ctx, 'holiday_calendars') if c['id'] == bid), None)
    if cur is not None:
        return cur
    any_default = any(c.get('is_default') for c in rows(ctx, 'holiday_calendars'))
    return insert(ctx, 'holiday_calendars', {'id': bid, 'name': hc.BASE_NAME, 'description': '', 'is_default': not any_default, 'active': True})


def _cal_view(ctx: Ctx, c: dict, year: str, locs: list, people: list) -> dict[str, Any]:
    tid = ctx.tenant_id
    hol = [h for h in rows(ctx, 'holidays') if hc.calendar_of_holiday(h, tid) == c['id']]
    in_year = [h for h in hol if h['date'].startswith(year)]
    mine = [loc for loc in locs if hc.calendar_id_for_location(tid, loc) == c['id']]
    loc_ids = {loc['id'] for loc in mine}
    default = c.get('is_default')
    return {
        **{k: c.get(k) for k in ('id', 'name', 'description', 'is_default', 'active', 'version')},
        'description': c.get('description') or '',
        'is_base': c['id'] == hc.base_calendar_id(tid),
        'holiday_count': len([h for h in in_year if not h.get('optional')]),
        'optional_count': len([h for h in in_year if h.get('optional')]),
        'total_holidays': len(hol),
        'location_ids': [loc['id'] for loc in mine],
        'locations': [{'id': loc['id'], 'name': loc['name'], 'explicit': bool(loc.get('holiday_calendar_id'))} for loc in mine],
        'people_count': sum(1 for u in people if (u.get('location_id') in loc_ids) or (default and not u.get('location_id'))),
    }


def list_calendars(req: Req) -> Any:
    ctx = req.ctx
    year = js_str(req.q('year')) if isinstance(req.q('year'), str) and re.match(r'^\d{4}$', req.q('year')) else ctx.today[:4]
    locs = [loc for loc in rows(ctx, 'locations') if loc.get('active')]
    people = _people(ctx)
    cals = [_cal_view(ctx, c, year, locs, people) for c in hc.calendars_of(ctx.tenant_id)]
    return {
        'year': int(year),
        'calendars': cals,
        'default_id': hc.default_calendar_id(ctx.tenant_id),
        'my_calendar_id': hc.calendar_id_for_user(ctx.tenant_id, ctx.user),
        'locations': [
            {
                'id': loc['id'],
                'name': loc['name'],
                'code': loc.get('code'),
                'calendar_id': hc.calendar_id_for_location(ctx.tenant_id, loc),
                'explicit': bool(loc.get('holiday_calendar_id')),
            }
            for loc in sorted(locs, key=lambda x: locale_key(x['name']))
        ],
        'can_edit': has(ctx, 'holidays.edit') and not ctx.view_as,
    }


def _cal_input(ctx: Ctx, b: dict, cur: dict | None) -> dict[str, Any]:
    name = _s(b.get('name')) if 'name' in b else (cur or {}).get('name', '')
    if len(name) < 2:
        err(422, 'VALIDATION', 'Give the calendar a name (at least 2 characters).', 'name')
    if len(name) > 60:
        err(422, 'VALIDATION', 'Keep the name under 60 characters.', 'name')
    for c in hc.calendars_of(ctx.tenant_id):
        if c['id'] != (cur or {}).get('id') and c['name'].strip().lower() == name.lower():
            err(409, 'CALENDAR_EXISTS', f'A calendar called “{name}” already exists.', 'name')
    desc = _s(b.get('description')) if 'description' in b else ((cur or {}).get('description') or '')
    if len(desc) > 240:
        err(422, 'VALIDATION', 'Keep the description under 240 characters.', 'description')
    return {'name': name, 'description': desc}


def _copy_holidays(ctx: Ctx, src_id: str, dst_id: str, year: Any) -> int:
    tid = ctx.tenant_id
    base = hc.base_calendar_id(tid)
    have = {h['date'] for h in rows(ctx, 'holidays') if hc.calendar_of_holiday(h, tid) == dst_id}
    n = 0
    for h in list(rows(ctx, 'holidays')):
        if hc.calendar_of_holiday(h, tid) != src_id or h['date'] in have:
            continue
        if year and not h['date'].startswith(str(year)):
            continue
        row: dict[str, Any] = {'date': h['date'], 'name': h['name'], 'optional': bool(h.get('optional'))}
        if dst_id != base:
            row['calendar_id'] = dst_id
        insert(ctx, 'holidays', row)
        n += 1
    return n


def create_calendar(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holiday calendars.')
    b = _body(req)
    d = _cal_input(ctx, b, None)
    src = b.get('copy_from_id')
    if src:
        _cal_or_404(ctx, src)
    year = b.get('copy_year')
    if year not in (None, '', UNDEF) and not (isinstance(year, (int, str)) and re.match(r'^\d{4}$', str(year))):
        err(422, 'VALIDATION', 'Pick the year to copy.', 'copy_year')
    ensure_base(ctx)
    c = insert(ctx, 'holiday_calendars', {**d, 'is_default': False, 'active': True})
    copied = _copy_holidays(ctx, src, c['id'], year or None) if src else 0
    log_activity(
        ctx,
        'holiday_calendar',
        c['id'],
        'created',
        f'{_name(ctx)} created the holiday calendar {d["name"]}' + (f' ({copied} holidays copied)' if copied else ''),
    )
    return {**c, 'copied': copied}


def patch_calendar(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holiday calendars.')
    cur = _cal_or_404(ctx, req.params['id'])
    b = _body(req)
    d = _cal_input(ctx, b, cur)
    if 'active' in b:
        d['active'] = js_truthy(b['active'])
        if not d['active'] and cur.get('is_default'):
            err(409, 'CALENDAR_DEFAULT', 'The default calendar can’t be switched off. Make another calendar the default first.', 'active')
    row = ensure_base(ctx) if cur.get('virtual') else cur
    out = update(ctx, 'holiday_calendars', row['id'], d, UNDEF if cur.get('virtual') else req.if_match)
    if d['name'] != cur['name']:
        for loc in rows(ctx, 'locations'):
            if hc.calendar_id_for_location(ctx.tenant_id, loc) == out['id'] and loc.get('holiday_calendar') != d['name']:
                update(ctx, 'locations', loc['id'], {'holiday_calendar': d['name']})
    what = []
    if d['name'] != cur['name']:
        what.append(f'renamed to {d["name"]}')
    if 'active' in d and d['active'] != cur.get('active'):
        what.append('switched on' if d['active'] else 'switched off')
    log_activity(
        ctx, 'holiday_calendar', out['id'], 'updated', f'{_name(ctx)} updated the holiday calendar {cur["name"]}' + (f': {", ".join(what)}' if what else '')
    )
    return out


def delete_calendar(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holiday calendars.')
    cur = _cal_or_404(ctx, req.params['id'])
    if cur['id'] == hc.base_calendar_id(ctx.tenant_id):
        err(409, 'CALENDAR_BASE', 'The company calendar can’t be deleted. Rename it or remove its holidays instead.')
    if cur.get('is_default'):
        err(409, 'CALENDAR_DEFAULT', 'Make another calendar the default before deleting this one.')
    explicit = [loc for loc in rows(ctx, 'locations') if loc.get('holiday_calendar_id') == cur['id']]
    if explicit:
        err(
            409,
            'CALENDAR_IN_USE',
            f'{len(explicit)} location{" uses" if len(explicit) == 1 else "s use"} this calendar ({", ".join(x["name"] for x in explicit[:3])}). Move them to another calendar first.',
        )
    tid = ctx.tenant_id
    gone = [h for h in rows(ctx, 'holidays') if hc.calendar_of_holiday(h, tid) == cur['id']]
    for h in gone:
        remove(ctx, 'holidays', h['id'])
    remove(ctx, 'holiday_calendars', cur['id'], req.if_match)
    log_activity(ctx, 'holiday_calendar', cur['id'], 'deleted', f'{_name(ctx)} deleted the holiday calendar {cur["name"]} ({len(gone)} holidays)')
    return {'ok': True}


def default_calendar(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holiday calendars.')
    cur = _cal_or_404(ctx, req.params['id'])
    if not cur.get('active', True):
        err(409, 'CALENDAR_INACTIVE', 'Switch the calendar on before making it the default.')
    ensure_base(ctx)
    for c in list(rows(ctx, 'holiday_calendars')):
        want = c['id'] == cur['id']
        if bool(c.get('is_default')) != want:
            update(ctx, 'holiday_calendars', c['id'], {'is_default': want})
    for loc in rows(ctx, 'locations'):
        if not loc.get('holiday_calendar_id') and loc.get('holiday_calendar') != cur['name']:
            update(ctx, 'locations', loc['id'], {'holiday_calendar': cur['name']})
    log_activity(ctx, 'holiday_calendar', cur['id'], 'default', f'{_name(ctx)} made {cur["name"]} the default holiday calendar')
    return {'ok': True, 'default_id': cur['id']}


def assign_locations(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holiday calendars.')
    cur = _cal_or_404(ctx, req.params['id'])
    if not cur.get('active', True):
        err(409, 'CALENDAR_INACTIVE', 'Switch the calendar on before assigning locations to it.')
    all_locs = rows(ctx, 'locations')
    ids = _id_list(req.b('location_ids'), 'location_ids', {loc['id'] for loc in all_locs}, 'locations')
    ensure_base(ctx)
    default_id = hc.default_calendar_id(ctx.tenant_id)
    default_name = next((c['name'] for c in hc.calendars_of(ctx.tenant_id) if c['id'] == default_id), hc.BASE_NAME)
    added, removed = [], []
    for loc in all_locs:
        now = loc.get('holiday_calendar_id')
        if loc['id'] in ids and now != cur['id']:
            update(ctx, 'locations', loc['id'], {'holiday_calendar_id': cur['id'], 'holiday_calendar': cur['name']})
            added.append(loc['name'])
        elif loc['id'] not in ids and now == cur['id']:
            update(ctx, 'locations', loc['id'], {'holiday_calendar_id': None, 'holiday_calendar': default_name})
            removed.append(loc['name'])
    if added or removed:
        bits = ([f'added {", ".join(added)}'] if added else []) + ([f'moved {", ".join(removed)} back to {default_name}'] if removed else [])
        log_activity(ctx, 'holiday_calendar', cur['id'], 'locations', f'{_name(ctx)} changed the locations of {cur["name"]}: {"; ".join(bits)}')
    return {'ok': True, 'added': added, 'removed': removed}


def calendar_month_days(ctx: Ctx, holidays: list, year: str) -> list[dict]:
    from app.domain.calendar import CalendarCtx

    c = CalendarCtx(ctx.tenant.get('settings'), holidays)
    return [{'month': f'{year}-{i:02d}', 'working_days': working_days_in_month(f'{year}-{i:02d}', c)} for i in range(1, 13)]


# =====================================================================================================================
# Financial-year opening balances
# =====================================================================================================================
_MAX_PAISE = 10**15


def _fy_param(ctx: Ctx, v: Any) -> int:
    cur = current_fy(ctx)
    if v is UNDEF or v is None or v == '':
        return cur
    s = js_str(v)
    if not re.match(r'^\d{4}$', s):
        err(422, 'VALIDATION', 'Pick a financial year.', 'fy')
    y = int(s)
    if y < 2000 or y > cur + 1:
        err(422, 'VALIDATION', f'Pick a financial year between {fy_label(2000)} and {fy_label(cur + 1)}.', 'fy')
    return y


def _clean_lines(ctx: Ctx, raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        err(422, 'VALIDATION', 'Send the opening balances as a list of lines.', 'lines')
    if len(raw) > 500:
        err(422, 'VALIDATION', 'Keep it to 500 lines or fewer.', 'lines')
    clients = {c['id']: c for c in rows(ctx, 'clients')}
    vendors = {v['id']: v for v in rows(ctx, 'vendors')}
    seen: set[tuple] = set()
    out = []
    for i, ln in enumerate(raw):
        f = f'lines.{i}'
        if not isinstance(ln, dict):
            err(422, 'VALIDATION', f'Line {i + 1} is not valid.', f)
        kind = ln.get('kind')
        if kind not in KINDS:
            err(422, 'VALIDATION', f'Line {i + 1}: choose cash, bank, ledger account, receivable or payable.', f + '.kind')
        name = _s(ln.get('name'))
        party_id = ln.get('party_id') or None
        if party_id is not None:
            src = clients if kind == 'receivable' else vendors if kind == 'payable' else None
            if src is None or party_id not in src:
                err(422, 'VALIDATION', f'Line {i + 1}: pick a {"customer" if kind == "receivable" else "vendor"} from the list.', f + '.party_id')
            if not name:
                name = src[party_id].get('name') or ''
        if len(name) < 2:
            err(
                422,
                'VALIDATION',
                f'Line {i + 1}: name the {"account" if kind in ("cash", "bank", "ledger") else "party"} (at least 2 characters).',
                f + '.name',
            )
        if len(name) > 80:
            err(422, 'VALIDATION', f'Line {i + 1}: keep the name under 80 characters.', f + '.name')
        amt = ln.get('amount_paise')
        if isinstance(amt, bool) or not isinstance(amt, (int, float)) or amt != int(amt) or amt <= 0 or amt > _MAX_PAISE:
            err(422, 'MONEY_INVALID', f'Line {i + 1}: enter an amount above zero.', f + '.amount_paise')
        side = ln.get('side') or ('credit' if kind == 'payable' else 'debit')
        if side not in ('debit', 'credit'):
            err(422, 'VALIDATION', f'Line {i + 1}: choose debit or credit.', f + '.side')
        if kind == 'receivable' and side != 'debit':
            err(422, 'VALIDATION', f'Line {i + 1}: a receivable is a debit balance.', f + '.side')
        if kind == 'payable' and side != 'credit':
            err(422, 'VALIDATION', f'Line {i + 1}: a payable is a credit balance.', f + '.side')
        due = ln.get('due_on') or None
        if due is not None and (not isinstance(due, str) or not is_real_iso_date(due) or parse_iso(due) is None):
            err(422, 'VALIDATION', f'Line {i + 1}: pick a valid due date.', f + '.due_on')
        ref = _s(ln.get('reference'))[:60]
        note = _s(ln.get('note'))[:200]
        cat = _s(ln.get('category'))[:60]
        key = (kind, name.lower(), ref.lower(), party_id)
        if kind in ('cash', 'bank', 'ledger'):
            key = (kind, name.lower())
        if key in seen:
            err(422, 'VALIDATION', f'Line {i + 1}: “{name}” appears twice. Merge the two lines.', f + '.name')
        seen.add(key)
        lid = ln.get('id') if isinstance(ln.get('id'), str) and 0 < len(ln.get('id')) <= 64 else uid('obl_')
        row: dict[str, Any] = {'id': lid, 'kind': kind, 'name': name, 'side': side, 'amount_paise': int(amt)}
        if cat:
            row['category'] = cat
        if party_id:
            row['party_id'] = party_id
        if ref:
            row['reference'] = ref
        if due:
            row['due_on'] = due
        if note:
            row['note'] = note
        out.append(row)
    return out


def _opening_view(ctx: Ctx, fy: int) -> dict[str, Any]:
    r = opening_row(ctx, fy)
    st = state_of(ctx, r, fy)
    rg = fy_range(fy, fy_month(ctx))
    lines = list(r['lines']) if r else []
    clients = {c['id']: c.get('name') for c in rows(ctx, 'clients')}
    vendors = {v['id']: v.get('name') for v in rows(ctx, 'vendors')}
    view_lines = []
    for ln in lines:
        pid = ln.get('party_id')
        pname = (clients.get(pid) if ln.get('kind') == 'receivable' else vendors.get(pid)) if pid else None
        view_lines.append({**ln, 'party_name': pname})
    years = sorted({x['fy_start_year'] for x in rows(ctx, 'fy_openings')} | {current_fy(ctx)}, reverse=True)
    hist = []
    for h in (r.get('history') if r else None) or []:
        hist.append(
            {
                **h,
                'by_name': user_name(ctx, h.get('by')) if h.get('by') else None,
                'approved_by_name': user_name(ctx, h['approved_by']) if h.get('approved_by') else None,
            }
        )
    return {
        'fy_start_year': fy,
        'fy_label': fy_label(fy),
        'as_of': rg['from'],
        'fy_to': rg['to'],
        'id': r['id'] if r else None,
        'version': r['version'] if r else None,
        'exists': r is not None,
        'lines': view_lines,
        'notes': (r.get('notes') if r else None) or '',
        'totals': totals_of(lines),
        **st,
        'locked_at': r.get('locked_at') if r else None,
        'locked_by_name': user_name(ctx, r['locked_by']) if r and r.get('locked_by') else None,
        'reopened_at': r.get('reopened_at') if r else None,
        'reopened_by_name': user_name(ctx, r['reopened_by']) if r and r.get('reopened_by') else None,
        'reopen_reason': r.get('reopen_reason') if r else None,
        'history': list(reversed(hist))[:50],
        'pending': pending_brief(ctx, 'opening_balance', r['id']) if r else None,
        'kinds': [{'value': k, 'label': KIND_LABEL[k]} for k in KINDS],
        'years': [{'fy_start_year': y, 'fy_label': fy_label(y)} for y in years],
        'parties': {
            'customers': sorted(({'id': c['id'], 'name': c.get('name')} for c in rows(ctx, 'clients')), key=lambda x: locale_key(js_str(x['name']))),
            'vendors': sorted(({'id': v['id'], 'name': v.get('name')} for v in rows(ctx, 'vendors')), key=lambda x: locale_key(js_str(x['name']))),
        },
        'can_edit': has(ctx, 'finance.edit') and not ctx.view_as,
        'legacy_opening_cash_paise': ctx.tenant.get('settings', {}).get('finance.opening_cash_paise'),
    }


def get_openings(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.view', 'Opening balances are visible to finance and admins.')
    return _opening_view(ctx, _fy_param(ctx, req.q('fy')))


def _hist(row: dict | None, ctx: Ctx, action: str, **extra: Any) -> list:
    h = list((row.get('history') if row else None) or [])
    h.append({'at': now_iso(), 'by': ctx.user_id, 'action': action, **extra})
    return h[-200:]


def put_openings(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', 'Only finance and admins can change opening balances.')
    fy = _fy_param(ctx, req.params['fy'])
    r = opening_row(ctx, fy)
    st = state_of(ctx, r, fy)
    if st['locked']:
        err(409, 'OPENINGS_LOCKED', f'Opening balances for {fy_label(fy)} are locked because {st["lock_reason"]}. Reopen them with a reason to make changes.')
    if r and pending_brief(ctx, 'opening_balance', r['id']):
        err(409, 'CHANGE_PENDING', 'A reopen request for these balances is waiting for approval.')
    lines = _clean_lines(ctx, req.b('lines'))
    notes = _s(req.b('notes'))[:1000] if req.b('notes') is not UNDEF else ((r.get('notes') if r else '') or '')
    t = totals_of(lines)
    tot = {k: t[k] for k in ('cash_bank', 'receivables', 'payables', 'debits', 'credits')}
    if r:
        if req.if_match is not UNDEF and req.if_match != r['version']:
            err(409, 'STALE', 'Someone else changed these opening balances. Reload to see the latest version.')
        out = update(ctx, 'fy_openings', r['id'], {'lines': lines, 'notes': notes or None, 'history': _hist(r, ctx, 'saved', totals=tot)})
    else:
        out = insert(
            ctx,
            'fy_openings',
            {
                'fy_start_year': fy,
                'lines': lines,
                'locked': False,
                'locked_at': None,
                'locked_by': None,
                'reopened_at': None,
                'reopened_by': None,
                'reopen_reason': None,
                'history': _hist(None, ctx, 'saved', totals=tot),
                'notes': notes or None,
            },
        )
    log_activity(
        ctx,
        'fy_opening',
        out['id'],
        'saved',
        f'{_name(ctx)} saved {fy_label(fy)} opening balances: cash & bank {fmt_money(t["cash_bank"])}, receivables {fmt_money(t["receivables"])}, payables {fmt_money(t["payables"])} ({t["count"]} lines)',
    )
    return _opening_view(ctx, fy)


def lock_openings(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', 'Only finance and admins can lock opening balances.')
    fy = _fy_param(ctx, req.params['fy'])
    r = opening_row(ctx, fy)
    if r is None or not r.get('lines'):
        err(409, 'OPENINGS_EMPTY', f'Enter the {fy_label(fy)} opening balances before locking them.')
    if r.get('locked') and not r.get('reopened_at'):  # type: ignore[union-attr]
        return _opening_view(ctx, fy)
    out = update(
        ctx,
        'fy_openings',
        r['id'],  # type: ignore[index]
        {
            'locked': True,
            'locked_at': now_iso(),
            'locked_by': ctx.user_id,
            'reopened_at': None,
            'reopened_by': None,
            'reopen_reason': None,
            'history': _hist(r, ctx, 'locked'),
        },
        req.if_match,
    )
    log_activity(ctx, 'fy_opening', out['id'], 'locked', f'{_name(ctx)} locked the {fy_label(fy)} opening balances')
    return _opening_view(ctx, fy)


def reopen_openings(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'finance.edit', 'Only finance and admins can reopen opening balances.')
    fy = _fy_param(ctx, req.params['fy'])
    r = opening_row(ctx, fy)
    st = state_of(ctx, r, fy)
    if not st['locked']:
        err(409, 'OPENINGS_NOT_LOCKED', f'The {fy_label(fy)} opening balances are not locked — edit them directly.')
    reason = _s(req.b('reason'))
    if len(reason) < 10:
        err(422, 'VALIDATION', 'Give a reason of at least 10 characters (it goes to the audit trail).', 'reason')
    if len(reason) > 400:
        err(422, 'VALIDATION', 'Keep the reason under 400 characters.', 'reason')
    if r is None:  # the FY already has transactions but nobody entered openings yet: an empty record to reopen
        r = insert(
            ctx,
            'fy_openings',
            {
                'fy_start_year': fy,
                'lines': [],
                'locked': False,
                'locked_at': None,
                'locked_by': None,
                'reopened_at': None,
                'reopened_by': None,
                'reopen_reason': None,
                'history': [],
                'notes': None,
            },
        )
    res = propose(
        ctx,
        {
            'entity': 'opening_balance',
            'entity_id': r['id'],  # type: ignore[index]
            'action': 'execute',  # change_sets.action: create | update | delete | execute
            'summary': f'Reopen {fy_label(fy)} opening balances: “{reason[:120]}”',
            'before': {'locked': True},
            'after': {'locked': False, 'reason': reason},
            'payload': {'fy': fy, 'reason': reason},
            'required_permission': 'finance.edit',
            'base_version': r['version'],  # type: ignore[index]
        },
    )
    if res.get('pending'):
        return {'pending': True, 'change': res['change'], 'result': None}
    return {'pending': False, 'result': _opening_view(ctx, fy), 'result_id': r['id']}  # type: ignore[index]


def _apply_reopen(ctx: Ctx, cs: dict) -> dict:
    from app.engine.maker_checker import assert_base_version

    fy = cs['payload']['fy']
    r = opening_row(ctx, fy)
    if r is None:
        err(404, 'NOT_FOUND', 'Opening balances not found.')
    assert_base_version(cs, r['version'])  # type: ignore[index]
    reason = cs['payload']['reason']
    maker = cs.get('maker_id') or ctx.user_id
    by = f' (approved by {_name(ctx)})' if cs['id'] != 'cs_direct' else ''
    out = update(
        ctx,
        'fy_openings',
        r['id'],  # type: ignore[index]
        {
            'locked': False,
            'reopened_at': now_iso(),
            'reopened_by': maker,
            'reopen_reason': reason,
            'history': [
                *list((r.get('history') if r else None) or [])[-199:],  # type: ignore[union-attr]
                {'at': now_iso(), 'by': maker, 'action': 'reopened', 'reason': reason, **({'approved_by': ctx.user_id} if cs['id'] != 'cs_direct' else {})},
            ],
        },
    )
    log_activity(ctx, 'fy_opening', out['id'], 'reopened', f'{user_name(ctx, maker)} reopened the {fy_label(fy)} opening balances{by}: “{reason}”')
    return {'result': out, 'result_id': out['id']}


register_applier('opening_balance', _apply_reopen)


# =====================================================================================================================
def register(r: Any) -> None:
    r.get('/admin/leave-types', list_leave_types)
    r.post('/admin/leave-types', create_leave_type)
    r.patch('/admin/leave-types/:code', patch_leave_type)
    r.delete('/admin/leave-types/:code', delete_leave_type)
    r.get('/leave/options', leave_options)

    r.get('/holiday-calendars', list_calendars)
    r.post('/holiday-calendars', create_calendar)
    r.patch('/holiday-calendars/:id', patch_calendar)
    r.delete('/holiday-calendars/:id', delete_calendar)
    r.post('/holiday-calendars/:id/default', default_calendar)
    r.put('/holiday-calendars/:id/locations', assign_locations)

    r.get('/finance/openings', get_openings)
    r.put('/finance/openings/:fy', put_openings)
    r.post('/finance/openings/:fy/lock', lock_openings)
    r.post('/finance/openings/:fy/reopen', reopen_openings)
