"""v4 organisation master data + email templates/outbox — port of src/mock/routes/master.ts.

Reads: GET /master/<type> (everyone in the tenant — pickers on employee forms), /master/org, /master/usage,
/master/people, /master/shifts/:id/members.
Writes go through maker-checker (`propose`):
  * org profile            -> entity 'org_profile'   (master.edit, base_version = tenant.version)
  * locations, departments, designations, grades, cost centres -> entity 'master_record' (master.edit)
  * shifts + assignment    -> entity 'shift'         (shifts.manage)
The appliers registered at the bottom perform the real write (direct apply or on approval) and re-check the integrity
rules that could have changed in between.
"""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, Req, err, find, insert, log_activity, now_iso, patch, remove, require_action, role_def, rows, scope, update, user_name
from app.engine.email import login_url, render_template
from app.engine.maker_checker import assert_base_version, propose, register_applier
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_json_dumps,
    js_len,
    js_max,
    js_number,
    js_or,
    js_round,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    nullish,
    pad_start,
    prop,
    to_fixed,
)

_C = cat('mock/routes/master')
MASTER_TABLES = _C['MASTER_TABLES']
ORG_FIELD_LABEL = _C['ORG_FIELD_LABEL']
TEMPLATE_PLACEHOLDERS = _C['TEMPLATE_PLACEHOLDERS']
PLACEHOLDER_LABEL = _C['PLACEHOLDER_LABEL']
SYSTEM_EMAIL_TEMPLATES = cat('mock/seeds/access')['SYSTEM_EMAIL_TEMPLATES']

LABEL = {
    'locations': 'location',
    'departments': 'department',
    'designations': 'designation',
    'grades': 'grade',
    'cost-centres': 'cost centre',
    'shifts': 'shift',
}
USER_FIELD = {
    'locations': 'location_id',
    'departments': 'department_id',
    'designations': 'designation_id',
    'grades': 'grade_id',
    'cost-centres': 'cost_centre_id',
    'shifts': 'shift_id',
}


def _perm_of(p: str) -> str:
    return 'shifts.manage' if p == 'shifts' else 'master.edit'


def _entity_of(p: str) -> str:
    return 'shift' if p == 'shifts' else 'master_record'


def _name_of(r: dict) -> Any:
    a = r.get('name', UNDEF) if 'name' in r else ''
    return a if js_truthy(a) else (r.get('code', UNDEF) if 'code' in r else '')


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# ---------------------------------------------------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------------------------------------------------
def _str(v: Any) -> str:
    return js_trim('' if nullish(v) else js_str(v))


def _opt_id(v: Any) -> Any:
    return None if (nullish(v) or v == '') and not isinstance(v, bool) else js_str(v)


def _need(v: str, field: str, label: str, mx: int = 120) -> str:
    if not v:
        err(422, 'VALIDATION', f'Enter the {label}.', field)
    if js_len(v) > mx:
        err(422, 'VALIDATION', f'Keep the {label} under {mx} characters.', field)
    return v


_CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9_-]*\Z')


def _code(v: Any, field: str = 'code', mx: int = 12) -> str:
    c = _str(v).upper()
    if not c:
        err(422, 'VALIDATION', 'Enter a short code.', field)
    if not _CODE_RE.search(c) or js_len(c) > mx:
        err(422, 'VALIDATION', f'Use letters, digits, - or _ (up to {mx} characters).', field)
    return c


def _unique(ctx: Ctx, table: str, field: str, value: str, self_id: Any, label: str) -> None:
    lv = value.lower()
    for r in rows(ctx, table):
        if r['id'] != self_id and js_str(coalesce(r.get(field, UNDEF), '')).lower() == lv:
            err(409, 'DUPLICATE', f'Another {label} already uses {field} “{value}”. Pick a different {field}.', field)


def _active_user(ctx: Ctx, id_: Any, field: str, what: str) -> Any:
    if not js_truthy(id_):
        return None
    u = next((x for x in rows(ctx, 'users') if x['id'] == id_), None)
    if not u or u.get('is_super_admin'):
        err(422, 'VALIDATION', f'Choose a person from your organisation as {what}.', field)
    if u.get('status') != 'active':  # type: ignore[union-attr]
        err(422, 'VALIDATION', f'{js_str(prop(u, "name"))} is not active. Choose an active person as {what}.', field)
    return id_


def _ref(ctx: Ctx, table: str, id_: Any, field: str, label: str) -> Any:
    if not js_truthy(id_):
        return None
    r = next((x for x in rows(ctx, table) if x['id'] == id_), None)
    if not r:
        err(422, 'VALIDATION', f'Choose a valid {label}.', field)
    if not r.get('active'):  # type: ignore[union-attr]
        err(422, 'VALIDATION', f'{js_str(_name_of(r))} is inactive. Choose an active {label}.', field)  # type: ignore[arg-type]
    return id_


_HHMM = re.compile(r'^([01][0-9]|2[0-3]):([0-5][0-9])\Z')


def _minutes(t: str) -> Any:
    return js_number(t[0:2]) * 60 + js_number(t[3:5])


def _int(v: Any, field: str, label: str, mn: int, mx: int) -> Any:
    n = js_number(v)
    if v == '' or nullish(v) or not is_finite(n) or js_round(n) != n:
        err(422, 'VALIDATION', f'Enter the {label} as a whole number.', field)
    if n < mn or n > mx:
        err(422, 'VALIDATION', f'The {label} must be between {mn} and {mx}.', field)
    return n


def _jss(v: Any) -> Any:
    """JSON.stringify(v) (None for undefined, which JSON.stringify returns as undefined)"""
    return None if v is UNDEF else js_json_dumps(v)


# ---------------------------------------------------------------------------------------------------------------------
# Normalise + validate a full record of each type
# ---------------------------------------------------------------------------------------------------------------------
def _normalise(ctx: Ctx, typ: str, b: Any, cur: dict | None) -> dict[str, Any]:
    self_id = cur['id'] if cur else None
    table = MASTER_TABLES[typ]

    def v(k: str) -> Any:
        x = b.get(k, UNDEF) if isinstance(b, dict) else UNDEF
        return x if x is not UNDEF else (cur.get(k, UNDEF) if cur else UNDEF)

    if typ == 'locations':
        out = {
            'name': _need(_str(v('name')), 'name', 'location name', 80),
            'code': _code(v('code')),
            'address': _str(v('address'))[:300],
            'city': _str(v('city'))[:60],
            'state': _str(v('state'))[:60],
            'timezone': _str(v('timezone')) or 'Asia/Kolkata',
            'holiday_calendar': _str(v('holiday_calendar'))[:60] or 'Company holidays',
            'is_head_office': js_truthy(v('is_head_office')),
        }
        _unique(ctx, table, 'code', out['code'], self_id, 'location')
        _unique(ctx, table, 'name', out['name'], self_id, 'location')
        if cur and cur.get('is_head_office') and not out['is_head_office']:
            err(409, 'HEAD_OFFICE_REQUIRED', 'One location must be the head office. Mark another location as head office instead.', 'is_head_office')
        return out
    if typ == 'departments':
        kind = _str(v('kind')) or 'delivery'
        if kind not in ('management', 'delivery', 'sales', 'operations'):
            err(422, 'VALIDATION', 'Choose what kind of department this is.', 'kind')
        out = {
            'name': _need(_str(v('name')), 'name', 'department name', 80),
            'code': _code(v('code')),
            'kind': kind,
            'head_id': _active_user(ctx, _opt_id(v('head_id')), 'head_id', 'department head'),
            'parent_id': _opt_id(v('parent_id')),
            'cost_centre_id': _opt_id(v('cost_centre_id')),
        }
        _unique(ctx, table, 'code', out['code'], self_id, 'department')
        _unique(ctx, table, 'name', out['name'], self_id, 'department')
        if out['parent_id']:
            if out['parent_id'] == self_id:
                err(409, 'DEPARTMENT_CYCLE', 'A department cannot sit under itself.', 'parent_id')
            all_ = rows(ctx, 'departments')
            parent = next((d for d in all_ if d['id'] == out['parent_id']), None)
            if not parent:
                err(422, 'VALIDATION', 'Choose a valid parent department.', 'parent_id')
            # walk up from the new parent: reaching this department means a loop
            p = parent
            i = 0
            while p and i < 50:
                if p['id'] == self_id:
                    err(409, 'DEPARTMENT_CYCLE', f'{parent["name"]} sits under this department, so it can’t also be its parent.', 'parent_id')  # type: ignore[index]
                pp = p.get('parent_id')
                p = next((d for d in all_ if d['id'] == pp), None)
                i += 1
        if out['cost_centre_id']:
            cc = next((c for c in rows(ctx, 'cost_centres') if c['id'] == out['cost_centre_id']), None)
            if not cc:
                err(422, 'VALIDATION', 'Choose a valid cost centre.', 'cost_centre_id')
            if not cc.get('active') and (cur.get('cost_centre_id', UNDEF) if cur else UNDEF) != cc['id']:  # type: ignore[union-attr]
                err(422, 'VALIDATION', f'{cc["name"]} is inactive. Choose an active cost centre.', 'cost_centre_id')  # type: ignore[index]
        return out
    if typ == 'designations':
        role = _opt_id(v('default_role_id'))
        if role:
            rd = role_def(ctx.tenant_id, role)
            if not rd:
                err(422, 'VALIDATION', 'Choose a valid default role.', 'default_role_id')
            if not rd.get('active'):  # type: ignore[union-attr]
                err(422, 'VALIDATION', f'The role {rd["name"]} is inactive. Choose an active role.', 'default_role_id')  # type: ignore[index]
        out = {
            'name': _need(_str(v('name')), 'name', 'designation', 80),
            'department_id': _opt_id(v('department_id')),
            'grade_id': _opt_id(v('grade_id')),
            'default_role_id': role,
        }
        if out['department_id'] != (cur.get('department_id', UNDEF) if cur else UNDEF):
            _ref(ctx, 'departments', out['department_id'], 'department_id', 'department')
        if out['grade_id'] != (cur.get('grade_id', UNDEF) if cur else UNDEF):
            _ref(ctx, 'grades', out['grade_id'], 'grade_id', 'grade')
        _unique(ctx, table, 'name', out['name'], self_id, 'designation')
        return out
    if typ == 'grades':

        def money(k: str, label: str) -> Any:
            x = v(k)
            if nullish(x) or x == '' or (x == 0 and not isinstance(x, bool)):
                return None
            n = js_number(x)
            if not is_finite(n) or n < 0 or js_round(n) != n:
                err(422, 'VALIDATION', f'Enter the {label} in rupees.', k)
            return n

        lm = js_number(coalesce(v('leave_multiplier'), 1))
        out = {
            'code': _code(v('code'), 'code', 8),
            'name': _need(_str(v('name')), 'name', 'grade name', 60),
            'min_ctc_paise': money('min_ctc_paise', 'minimum CTC'),
            'max_ctc_paise': money('max_ctc_paise', 'maximum CTC'),
            'leave_multiplier': lm,
        }
        if not is_finite(lm) or lm < 0.5 or lm > 3:
            err(422, 'VALIDATION', 'Leave multiplier must be between 0.5 and 3 (1 = standard leave).', 'leave_multiplier')
        if out['min_ctc_paise'] is not None and out['max_ctc_paise'] is not None and out['min_ctc_paise'] > out['max_ctc_paise']:
            err(422, 'GRADE_BAND', 'The minimum CTC is higher than the maximum. Swap them or correct one.', 'max_ctc_paise')
        _unique(ctx, table, 'code', out['code'], self_id, 'grade')
        return out
    if typ == 'cost-centres':
        out = {
            'code': _code(v('code'), 'code', 16),
            'name': _need(_str(v('name')), 'name', 'cost centre name', 80),
            'owner_id': _active_user(ctx, _opt_id(v('owner_id')), 'owner_id', 'owner'),
        }
        _unique(ctx, table, 'code', out['code'], self_id, 'cost centre')
        return out
    # shifts
    start = _str(v('start_time'))
    end = _str(v('end_time'))
    if not _HHMM.search(start):
        err(422, 'VALIDATION', 'Enter the start time as HH:MM (24-hour).', 'start_time')
    if not _HHMM.search(end):
        err(422, 'VALIDATION', 'Enter the end time as HH:MM (24-hour).', 'end_time')
    ln = (_minutes(end) - _minutes(start) + 1440) % 1440
    if ln == 0:
        err(422, 'VALIDATION', 'Start and end time are the same. A shift needs some length.', 'end_time')
    brk = _int(coalesce(v('break_minutes'), 0), 'break_minutes', 'break', 0, 240)
    if brk >= ln:
        err(422, 'SHIFT_BREAK', f'The break ({js_str(brk)} min) must be shorter than the shift ({js_str(ln)} min).', 'break_minutes')
    grace = _int(coalesce(v('grace_minutes'), 0), 'grace_minutes', 'grace period', 0, 120)
    wd = coalesce(v('working_days'), [])
    if not isinstance(wd, list):
        raise TypeError('working_days.map is not a function')
    days = sorted((d for d in dict.fromkeys(js_number(x) for x in wd) if is_integer(d) and 0 <= d <= 6), key=js_str)
    if not days:
        err(422, 'VALIDATION', 'Pick at least one working day.', 'working_days')
    settings = prop(ctx.tenant, 'settings')
    min_h = js_number(coalesce(v('min_hours'), prop(settings, 'calendar.min_hours_per_day') if isinstance(settings, dict) else UNDEF, 8))
    net = (ln - brk) / 60
    if not is_finite(min_h) or min_h <= 0:
        err(422, 'VALIDATION', 'Enter the minimum hours per day.', 'min_hours')
    if min_h > net + 1e-9:
        err(
            422,
            'SHIFT_MIN_HOURS',
            f'Minimum hours ({js_str(min_h)}h) can’t exceed the working time of the shift ({js_str(js_number(to_fixed(net, 2)))}h after break).',
            'min_hours',
        )
    out = {
        'name': _need(_str(v('name')), 'name', 'shift name', 60),
        'code': _code(v('code'), 'code', 8),
        'start_time': start,
        'end_time': end,
        'break_minutes': brk,
        'grace_minutes': grace,
        'working_days': days,
        'night_shift': _minutes(end) <= _minutes(start),
        'min_hours': min_h,
        'is_default': js_truthy(v('is_default')),
    }
    _unique(ctx, table, 'code', out['code'], self_id, 'shift')
    _unique(ctx, table, 'name', out['name'], self_id, 'shift')
    if cur and cur.get('is_default') and not out['is_default']:
        err(409, 'DEFAULT_SHIFT_REQUIRED', 'One shift must be the default for new joiners. Make another shift the default instead.', 'is_default')
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------------------------------------------------
def _default_shift_id(ctx: Ctx) -> Any:
    s = next((x for x in rows(ctx, 'shifts') if x.get('is_default')), None)
    return coalesce(s.get('id', UNDEF) if s else UNDEF, None)


def _shift_of(u: dict, default: Any) -> Any:
    """Effective shift of a user: their own, else the tenant default."""
    return coalesce(u.get('shift_id', UNDEF), default)


def _people_of(ctx: Ctx, typ: str, id_: Any, include_inactive: bool = False) -> list[dict]:
    d = _default_shift_id(ctx) if typ == 'shifts' else None
    out = []
    for u in rows(ctx, 'users'):
        if u.get('is_super_admin') or (not include_inactive and u.get('status') == 'deactivated'):
            continue
        if (_shift_of(u, d) == id_) if typ == 'shifts' else (u.get(USER_FIELD[typ], UNDEF) == id_):
            out.append(u)
    return out


def _pl(n: int, one: str, many: str | None = None) -> str:
    return f'{n} {one if n == 1 else (many if many is not None else one + "s")}'


def _used_by(ctx: Ctx, typ: str, id_: Any) -> list[str]:
    """Where a record is referenced (people + other master rows). Empty = safe to delete."""
    out: list[str] = []
    people = _people_of(ctx, typ, id_, True)
    if people:
        out.append(_pl(len(people), 'person', 'people') + (f' ({", ".join(js_str(p.get("name", UNDEF)) for p in people)})' if len(people) <= 3 else ''))
    if typ == 'departments':
        kids = [d for d in rows(ctx, 'departments') if d.get('parent_id') == id_]
        if kids:
            out.append(_pl(len(kids), 'sub-department'))
        des = [d for d in rows(ctx, 'designations') if d.get('department_id') == id_]
        if des:
            out.append(_pl(len(des), 'designation'))
    if typ == 'grades':
        des = [d for d in rows(ctx, 'designations') if d.get('grade_id') == id_]
        if des:
            out.append(_pl(len(des), 'designation'))
    if typ == 'cost-centres':
        deps = [d for d in rows(ctx, 'departments') if d.get('cost_centre_id') == id_]
        if deps:
            out.append(_pl(len(deps), 'department'))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Org profile validation
# ---------------------------------------------------------------------------------------------------------------------
ORG_FIELDS = [
    'legal_name',
    'brand_name',
    'cin',
    'gstin',
    'pan',
    'tan',
    'registered_address',
    'corporate_address',
    'city',
    'state',
    'pincode',
    'country',
    'phone',
    'email',
    'website',
    'incorporated_on',
    'employee_code_prefix',
    'employee_code_next',
    'bank_name',
    'bank_account_masked',
    'ifsc',
]
_RX = {
    'pan': re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]\Z'),
    'tan': re.compile(r'^[A-Z]{4}[0-9]{5}[A-Z]\Z'),
    'cin': re.compile(r'^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}\Z'),
    'gstin': re.compile(r'^([0-3][0-9])([A-Z]{5}[0-9]{4}[A-Z])[1-9A-Z]Z[0-9A-Z]\Z'),
    'ifsc': re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}\Z'),
    'pincode': re.compile(r'^[1-9][0-9]{5}\Z'),
    'email': re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}\Z'),
    # JS /i on ASCII classes: no Unicode case folding (Python re.I would also match U+017F / U+212A)
    'website': re.compile(r'^([hH][tT][tT][pP][sS]?://)?([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(/\S*)?\Z'),
    'phone': re.compile(r'^\+?[0-9 ()-]{8,20}\Z'),
}


def _test(rx: re.Pattern[str], v: Any) -> bool:
    """RegExp.prototype.test (coerces the value to a string)"""
    return rx.search(js_str(v)) is not None


def format_employee_code(prefix: Any, n: Any) -> str:
    """PREFIX-NNN — the employee number format used across the product."""
    return f'{js_str(prefix)}-{pad_start(js_str(n), 3, "0")}'


def _validate_org(ctx: Ctx, b: Any, cur: Any) -> dict[str, Any]:
    nxt: dict[str, Any] = {}

    def bk(k: str) -> Any:
        return b.get(k, UNDEF) if isinstance(b, dict) else UNDEF

    def up(k: str) -> str:
        return re.sub(r'\s+', '', _str(bk(k)).upper())

    for k in ORG_FIELDS:
        if bk(k) is UNDEF or k == 'bank_account_masked':
            continue
        if k in ('pan', 'tan', 'cin', 'gstin', 'ifsc', 'employee_code_prefix'):
            nxt[k] = up(k)
        elif k == 'employee_code_next':
            nxt[k] = js_number(bk(k))
        elif k == 'incorporated_on':
            nxt[k] = _str(bk(k)) or None
        else:
            nxt[k] = _str(bk(k))
    f = {**(cur if isinstance(cur, dict) else {}), **nxt}

    def fv(k: str) -> Any:
        return f.get(k, UNDEF)

    if not js_truthy(fv('legal_name')):
        err(422, 'VALIDATION', 'Enter the legal name as registered.', 'legal_name')
    if not js_truthy(fv('brand_name')):
        err(422, 'VALIDATION', 'Enter the name people know you by (used in emails).', 'brand_name')
    if js_truthy(fv('cin')) and not _test(_RX['cin'], fv('cin')):
        err(422, 'VALIDATION', 'CIN has 21 characters, like U72900OR2025PTC048213.', 'cin')
    if js_truthy(fv('pan')) and not _test(_RX['pan'], fv('pan')):
        err(422, 'VALIDATION', 'PAN looks like AAAAA9999A (5 letters, 4 digits, 1 letter).', 'pan')
    if js_truthy(fv('tan')) and not _test(_RX['tan'], fv('tan')):
        err(422, 'VALIDATION', 'TAN looks like AAAA99999A (4 letters, 5 digits, 1 letter).', 'tan')
    if js_truthy(fv('gstin')):
        m = _RX['gstin'].search(js_str(fv('gstin')))
        if not m or js_number(m.group(1)) < 1 or js_number(m.group(1)) > 38:
            err(422, 'VALIDATION', 'GSTIN has 15 characters: 2-digit state code, your PAN, entity number, Z and a check character.', 'gstin')
        assert m is not None
        if js_truthy(fv('pan')) and m.group(2) != fv('pan'):
            err(422, 'GSTIN_PAN_MISMATCH', f'Characters 3–12 of the GSTIN ({m.group(2)}) must match your PAN ({js_str(fv("pan"))}).', 'gstin')
    if js_truthy(fv('pincode')) and not _test(_RX['pincode'], fv('pincode')):
        err(422, 'VALIDATION', 'Pincode is 6 digits and doesn’t start with 0.', 'pincode')
    if js_truthy(fv('email')) and not _test(_RX['email'], fv('email')):
        err(422, 'VALIDATION', 'Enter a valid email address, like hr@company.in.', 'email')
    if js_truthy(fv('website')) and not _test(_RX['website'], fv('website')):
        err(422, 'VALIDATION', 'Enter a website like company.in or https://company.in.', 'website')
    if js_truthy(fv('phone')) and not _test(_RX['phone'], fv('phone')):
        err(422, 'VALIDATION', 'Enter a phone number with country code, like +91 90000 00000.', 'phone')
    if js_truthy(fv('ifsc')) and not _test(_RX['ifsc'], fv('ifsc')):
        err(422, 'VALIDATION', 'IFSC is 11 characters: 4 letters, 0, then 6 letters or digits (e.g. HDFC0001234).', 'ifsc')
    inc = fv('incorporated_on')
    if js_truthy(inc) and (not re.search(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z', js_str(inc)) or js_str(inc) > ctx.today):
        err(422, 'VALIDATION', 'Enter a past incorporation date.', 'incorporated_on')
    if not _test(re.compile(r'^[A-Z][A-Z0-9]{0,5}\Z'), fv('employee_code_prefix')):
        err(422, 'VALIDATION', 'Use 1–6 letters or digits, starting with a letter (e.g. SCI).', 'employee_code_prefix')
    ecn = fv('employee_code_next')
    if not is_integer(ecn) or ecn < 1 or ecn > 999999:
        err(422, 'VALIDATION', 'The next number must be a whole number from 1.', 'employee_code_next')
    # Never reuse an employee number that is already taken.
    if 'employee_code_prefix' in nxt or 'employee_code_next' in nxt:
        pfx = js_str(fv('employee_code_prefix'))
        taken = []
        for u in rows(ctx, 'users'):
            c = u.get('employee_code')
            if js_truthy(c) and isinstance(c, str) and c.startswith(pfx + '-'):
                n = js_number(c[len(pfx) + 1 :])
                if is_finite(n):
                    taken.append(n)
        mx = js_max(*taken) if taken else 0
        if ecn <= mx:
            err(409, 'EMPLOYEE_CODE_TAKEN', f'{format_employee_code(pfx, mx)} is already in use. Start from {js_str(mx + 1)} or later.', 'employee_code_next')
    # Bank account: the full number is accepted, only the last 4 are stored.
    ba = bk('bank_account')
    if ba is not UNDEF and _str(ba) != '':
        acc = re.sub(r'\s+', '', _str(ba))
        if not re.search(r'^[0-9]{9,18}\Z', acc):
            err(422, 'VALIDATION', 'Bank account numbers are 9–18 digits.', 'bank_account')
        nxt['bank_account_masked'] = 'X' * (len(acc) - 4) + acc[-4:]
    if (js_truthy(fv('bank_account_masked')) or js_truthy(nxt.get('bank_account_masked', UNDEF))) and js_truthy(fv('bank_name')) and not js_truthy(fv('ifsc')):
        err(422, 'VALIDATION', 'Add the IFSC of the branch.', 'ifsc')
    return nxt


def take_employee_code(ctx: Ctx) -> str:
    """Take the next employee code for this tenant (increments org.employee_code_next). For Create Employee."""
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    org = t['org']  # type: ignore[index]
    used = {u.get('employee_code') for u in rows(ctx, 'users')}
    n = org['employee_code_next']
    while format_employee_code(org['employee_code_prefix'], n) in used:
        n += 1
    patch('tenants', t, {'org': {**org, 'employee_code_next': n + 1}})  # type: ignore[arg-type]
    return format_employee_code(org['employee_code_prefix'], n)


# ---------------------------------------------------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------------------------------------------------
REQUIRED_PLACEHOLDERS = {'welcome': ['username', 'temp_password', 'login_url'], 'password_reset': ['username', 'temp_password', 'login_url']}


def _sample_vars(ctx: Ctx) -> dict[str, Any]:
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    org = prop(t, 'org')
    og = (lambda k: org.get(k, UNDEF)) if isinstance(org, dict) else (lambda k: UNDEF)
    company = js_or(og('brand_name'), prop(t, 'name'), 'Your company')
    domain = re.sub(r'/.*\Z', '', re.sub(r'^https?://', '', js_str(js_or(og('website'), 'company.in'))))
    return {
        'employee_name': 'Ananya Rath',
        'company': company,
        'username': f'ananya@{domain}',
        'temp_password': 'Kx7-pq2m-Rv',
        'login_url': login_url(),
        'role_name': 'Employee',
        'manager_name': js_or(ctx.user.get('name', UNDEF), 'Your manager'),
        'joining_date': '3 Nov 2026',
        'employee_code': format_employee_code(js_or(og('employee_code_prefix'), 'EMP'), js_or(og('employee_code_next'), 1)),
        'designation': 'MERN Developer',
        'department': 'Delivery',
        'decision': 'approved',
        'from_date': '12 Nov 2026',
        'to_date': '14 Nov 2026',
        'reason': 'Enjoy the break.',
        'amount': '₹2,450',
        'description': 'Client visit — cab fare',
        'approver_name': 'Subhankar Rout',
        'task_title': 'Payment gateway integration',
        'due_date': '20 Nov 2026',
        'month': 'October 2026',
        'net_pay': '₹48,250',
        'cycle_name': 'H1 FY26-27',
        'rating': '4 — Exceeds expectations',
    }


_PH = re.compile(r'\{\{\s*([A-Za-z0-9_]+)\s*\}\}')


def _placeholders_in(s: str) -> list[str]:
    return [m.group(1) for m in _PH.finditer(s)]


def _template_view(t: dict) -> dict[str, Any]:
    return {
        **t,
        'placeholders': coalesce(TEMPLATE_PLACEHOLDERS.get(t.get('key'), UNDEF), TEMPLATE_PLACEHOLDERS['custom']),
        'required_placeholders': REQUIRED_PLACEHOLDERS.get(t.get('key'), []),
        'is_system': any(s['key'] == t.get('key') for s in SYSTEM_EMAIL_TEMPLATES),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------------------------------
def _list_master(table: str) -> Any:
    def h(req: Req) -> Any:
        return rows(req.ctx, table)

    h.__name__ = f'list_{table}'
    return h


def get_org(req: Req) -> Any:
    t = scope().find_by('tenants', 'id', req.ctx.tenant_id)
    return coalesce(prop(t, 'org'), None)


def org_meta(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'master.view')
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    mc = prop(t, 'maker_checker')
    on = js_truthy(mc.get('enabled', UNDEF)) if isinstance(mc, dict) else False
    return {'version': t['version'], 'maker_checker': on and 'org_profile' in coalesce(mc.get('entities', UNDEF), [])}  # type: ignore[index, union-attr]


def put_org(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'master.edit', 'You can’t change the organisation profile.')
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    if req.if_match is not UNDEF and req.if_match != t['version']:  # type: ignore[index]
        err(409, 'STALE', 'Someone else changed the organisation profile. Reload to see the latest version.')
    cur = t['org']  # type: ignore[index]
    nxt = _validate_org(ctx, coalesce(req.body, {}), cur)
    changed = [k for k in nxt if not _strict_eq(nxt[k], cur.get(k, UNDEF))]
    if not changed:
        return {'pending': False, 'result': cur, 'result_id': t['id']}  # type: ignore[index]
    before = {k: cur.get(k, UNDEF) for k in changed}
    after = {k: nxt[k] for k in changed}
    lst = [ORG_FIELD_LABEL[k] for k in changed]
    what = ', '.join(lst[:4]) + f' and {len(lst) - 4} more' if len(lst) > 4 else ', '.join(lst)
    return propose(
        ctx,
        {
            'entity': 'org_profile',
            'entity_id': t['id'],  # type: ignore[index]
            'action': 'update',
            'summary': f'Organisation profile: change {what}',
            'before': before,
            'after': after,
            'payload': {'patch': after},
            'required_permission': 'master.edit',
            'base_version': t['version'],  # type: ignore[index]
        },
    )


def _strict_eq(a: Any, b: Any) -> bool:
    """a === b for JSON scalars (objects compare by identity in JS: never equal here)"""
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def master_usage(req: Req) -> Any:
    ctx = req.ctx
    out: dict[str, dict] = {'locations': {}, 'departments': {}, 'designations': {}, 'grades': {}, 'cost-centres': {}, 'shifts': {}}
    d = _default_shift_id(ctx)
    for u in rows(ctx, 'users'):
        if u.get('is_super_admin') or u.get('status') == 'deactivated':
            continue

        def bump(k: str, id_: Any) -> None:
            if js_truthy(id_):
                out[k][id_] = coalesce(out[k].get(id_, UNDEF), 0) + 1

        bump('locations', u.get('location_id'))
        bump('departments', u.get('department_id'))
        bump('designations', u.get('designation_id'))
        bump('grades', u.get('grade_id'))
        bump('cost-centres', u.get('cost_centre_id'))
        bump('shifts', _shift_of(u, d))
    return out


def master_people(req: Req) -> Any:
    """Light people list for master-data screens (assign shift, heads)."""
    ctx = req.ctx
    require_action(ctx, 'master.view')
    d = _default_shift_id(ctx)
    out = [
        {
            'id': u['id'],
            'name': prop(u, 'name'),
            'title': prop(u, 'title'),
            'avatar_hue': prop(u, 'avatar_hue'),
            'status': prop(u, 'status'),
            'employee_code': coalesce(prop(u, 'employee_code'), None),
            'department_id': coalesce(prop(u, 'department_id'), None),
            'location_id': coalesce(prop(u, 'location_id'), None),
            'shift_id': _shift_of(u, d),
        }
        for u in rows(ctx, 'users')
        if not u.get('is_super_admin') and u.get('status') != 'deactivated'
    ]
    return sorted(out, key=lambda x: locale_key(x['name']))


def _assert_deletable(ctx: Ctx, path: str, cur: dict) -> None:
    if path == 'locations' and cur.get('is_head_office'):
        err(409, 'HEAD_OFFICE_REQUIRED', f'{js_str(_name_of(cur))} is the head office. Make another location the head office before deleting it.')
    if path == 'shifts' and cur.get('is_default'):
        err(409, 'DEFAULT_SHIFT_REQUIRED', f'{js_str(_name_of(cur))} is the default shift. Make another shift the default before deleting it.')
    used = _used_by(ctx, path, cur['id'])
    if used:
        where = ', '.join(used[:-1]) + ' and ' + used[-1] if len(used) > 1 else used[0]
        err(
            409,
            'IN_USE',
            f'{js_str(_name_of(cur))} is used by {where}. Move them first, or deactivate it instead — deactivated {LABEL[path]}s stay on past records but can’t be picked for new ones.',
        )


def _assert_deactivatable(ctx: Ctx, path: str, cur: dict) -> None:
    if path == 'locations' and cur.get('is_head_office'):
        err(409, 'HEAD_OFFICE_REQUIRED', f'{js_str(_name_of(cur))} is the head office. Make another location the head office first.')
    if path == 'shifts' and cur.get('is_default'):
        err(409, 'DEFAULT_SHIFT_REQUIRED', f'{js_str(_name_of(cur))} is the default shift. Make another shift the default first.')


def _user_shift_name(ctx: Ctx, u: dict, d: Any) -> Any:
    id_ = _shift_of(u, d)
    s = next((x for x in rows(ctx, 'shifts') if x['id'] == id_), None)
    return coalesce(s.get('name', UNDEF) if s else UNDEF, '—')


def _master_routes(path: str) -> dict[str, Any]:
    table = MASTER_TABLES[path]
    label = LABEL[path]
    perm = _perm_of(path)
    entity = _entity_of(path)
    deny = f'You can’t change {"shifts" if label == "shift" else "master data"}.'

    def create(req: Req) -> Any:
        ctx = req.ctx
        require_action(ctx, perm, deny)
        data = _normalise(ctx, path, coalesce(req.body, {}), None)
        return propose(
            ctx,
            {
                'entity': entity,
                'entity_id': None,
                'action': 'create',
                'summary': f'Add {label} “{js_str(coalesce(data.get("name", UNDEF), data.get("code", UNDEF)))}”',
                'before': None,
                'after': data,
                'payload': {'op': 'create', 'type': path, 'data': data},
                'required_permission': perm,
            },
        )

    def edit(req: Req) -> Any:
        ctx = req.ctx
        require_action(ctx, perm, deny)
        cur = find(ctx, table, req.params['id'], _cap(label))
        if req.if_match is not UNDEF and req.if_match != cur['version']:
            err(409, 'STALE', 'Someone else changed this record. Reload to see the latest version.')
        data = _normalise(ctx, path, coalesce(req.body, {}), cur)
        changed = [k for k in data if _jss(data[k]) != _jss(cur.get(k, UNDEF))]
        if not changed:
            return {'pending': False, 'result': cur, 'result_id': cur['id']}
        before = {k: cur.get(k, UNDEF) for k in changed}
        after = {k: data[k] for k in changed}
        fields = ', '.join(re.sub(r'_id\Z|_paise\Z', '', k, count=1).replace('_', ' ') for k in changed)
        return propose(
            ctx,
            {
                'entity': entity,
                'entity_id': cur['id'],
                'action': 'update',
                'summary': f'Edit {label} “{js_str(_name_of(cur))}”: {fields}',
                'before': before,
                'after': after,
                'payload': {'op': 'update', 'type': path, 'id': cur['id'], 'data': after},
                'required_permission': perm,
                'base_version': cur['version'],
            },
        )

    def delete(req: Req) -> Any:
        ctx = req.ctx
        require_action(ctx, perm, deny)
        cur = find(ctx, table, req.params['id'], _cap(label))
        if req.if_match is not UNDEF and req.if_match != cur['version']:
            err(409, 'STALE', 'Someone else changed this record. Reload to see the latest version.')
        _assert_deletable(ctx, path, cur)
        return propose(
            ctx,
            {
                'entity': entity,
                'entity_id': cur['id'],
                'action': 'delete',
                'summary': f'Delete {label} “{js_str(_name_of(cur))}”',
                'before': {'name': _name_of(cur)},
                'after': None,
                'payload': {'op': 'delete', 'type': path, 'id': cur['id']},
                'required_permission': perm,
                'base_version': cur['version'],
            },
        )

    def toggle(op: str) -> Any:
        def h(req: Req) -> Any:
            ctx = req.ctx
            require_action(ctx, perm, deny)
            cur = find(ctx, table, req.params['id'], _cap(label))
            if cur.get('active', UNDEF) is (op == 'activate'):
                return {'pending': False, 'result': cur, 'result_id': cur['id']}
            if op == 'deactivate':
                _assert_deactivatable(ctx, path, cur)
            return propose(
                ctx,
                {
                    'entity': entity,
                    'entity_id': cur['id'],
                    'action': 'update',
                    'summary': f'{_cap(op)} {label} “{js_str(_name_of(cur))}”',
                    'before': {'active': cur.get('active', UNDEF)},
                    'after': {'active': op == 'activate'},
                    'payload': {'op': op, 'type': path, 'id': cur['id']},
                    'required_permission': perm,
                    'base_version': cur['version'],
                },
            )

        h.__name__ = f'{op}_{table}'
        return h

    return {'create': create, 'edit': edit, 'delete': delete, 'activate': toggle('activate'), 'deactivate': toggle('deactivate')}


def shift_members(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'master.view')
    find(ctx, 'shifts', req.params['id'], 'Shift')
    return [
        {
            'id': u['id'],
            'name': prop(u, 'name'),
            'title': prop(u, 'title'),
            'avatar_hue': prop(u, 'avatar_hue'),
            'employee_code': coalesce(prop(u, 'employee_code'), None),
        }
        for u in _people_of(ctx, 'shifts', req.params['id'])
    ]


def shift_assign(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'shifts.manage', 'You can’t assign shifts.')
    s = find(ctx, 'shifts', req.params['id'], 'Shift')
    if not s.get('active'):
        err(409, 'INACTIVE', f'{s["name"]} is inactive. Activate it before assigning people.')
    raw = coalesce(req.b('user_ids'), [])
    if not isinstance(raw, list):
        raise TypeError('user_ids.map is not a function')
    ids = list(dict.fromkeys(js_str(x) for x in raw))
    if not ids:
        err(422, 'VALIDATION', 'Pick at least one person to move to this shift.', 'user_ids')
    d = _default_shift_id(ctx)
    picked = []
    for id_ in ids:
        u = next((x for x in rows(ctx, 'users') if x['id'] == id_ and not x.get('is_super_admin')), None)
        if not u:
            err(404, 'NOT_FOUND', 'Person not found.')
        if u.get('status') == 'deactivated':  # type: ignore[union-attr]
            err(422, 'VALIDATION', f'{u["name"]} has left. Only active people can be assigned a shift.', 'user_ids')  # type: ignore[index]
        picked.append(u)
    people = [u for u in picked if _shift_of(u, d) != s['id'] or not js_truthy(u.get('shift_id'))]
    if not people:
        return {'pending': False, 'result': {'moved': 0}, 'result_id': s['id']}
    names = [u['name'] for u in people]
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for u in people:
        before[js_str(u['name'])] = _user_shift_name(ctx, u, d)
    for u in people:
        after[js_str(u['name'])] = s['name']
    return propose(
        ctx,
        {
            'entity': 'shift',
            'entity_id': s['id'],
            'action': 'execute',
            'summary': f'Move {", ".join(names) if len(names) <= 3 else _pl(len(names), "person", "people")} to {s["name"]}',
            'before': before,
            'after': after,
            'payload': {'op': 'assign', 'type': 'shifts', 'id': s['id'], 'user_ids': [u['id'] for u in people]},
            'required_permission': 'shifts.manage',
        },
    )


def list_templates(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'emails.view')
    order = [t['key'] for t in SYSTEM_EMAIL_TEMPLATES]

    def rank(t: dict) -> int:
        k = t.get('key')
        return (order.index(k) + 1) if k in order else 99

    return sorted((_template_view(t) for t in rows(ctx, 'email_templates')), key=rank)


def get_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'emails.view')
    return _template_view(find(ctx, 'email_templates', req.params['id'], 'Template'))


def put_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'emails.view', 'You can’t edit email templates.')
    t = find(ctx, 'email_templates', req.params['id'], 'Template')
    subject = _str(req.b('subject')) if req.b('subject') is not UNDEF else t['subject']
    text = js_trim(js_str(coalesce(req.b('body'), '')).replace('\r\n', '\n')) if req.b('body') is not UNDEF else t['body']
    if not subject:
        err(422, 'VALIDATION', 'Enter a subject line.', 'subject')
    if js_len(subject) > 200:
        err(422, 'VALIDATION', 'Keep the subject under 200 characters.', 'subject')
    if not text:
        err(422, 'VALIDATION', 'The email body can’t be empty.', 'body')
    if js_len(text) > 10000:
        err(422, 'VALIDATION', 'Keep the body under 10,000 characters.', 'body')
    allowed = coalesce(TEMPLATE_PLACEHOLDERS.get(t.get('key'), UNDEF), TEMPLATE_PLACEHOLDERS['custom'])
    for field, s in (('subject', subject), ('body', text)):
        bad = list(dict.fromkeys(p for p in _placeholders_in(s) if p not in allowed))
        if bad:
            err(
                422,
                'UNKNOWN_PLACEHOLDER',
                f'{", ".join("{{" + b + "}}" for b in bad)} {"isn’t a" if len(bad) == 1 else "aren’t"} known placeholder{"" if len(bad) == 1 else "s"} for this email. Use one of the chips below the editor.',
                field,
            )
    used_all = _placeholders_in(subject + '\n' + text)
    missing = [p for p in REQUIRED_PLACEHOLDERS.get(t.get('key'), []) if p not in used_all]
    if missing:
        err(
            422,
            'REQUIRED_PLACEHOLDER',
            f'This email must include {", ".join("{{" + m + "}}" for m in missing)} — without {"it" if len(missing) == 1 else "them"} the new joiner can’t sign in.',
            'body',
        )
    active = t.get('active', UNDEF) if req.b('active') is UNDEF else js_truthy(req.b('active'))
    if not js_truthy(active) and t.get('key') in REQUIRED_PLACEHOLDERS:
        err(409, 'TEMPLATE_REQUIRED', 'This email delivers login details, so it can’t be switched off.', 'active')
    out = update(ctx, 'email_templates', t['id'], {'subject': subject, 'body': text, 'active': active}, req.if_match)
    log_activity(ctx, 'email_template', t['id'], 'updated', f'{js_str(ctx.user.get("name", UNDEF))} edited the “{t["name"]}” email template')
    return _template_view(out)


def reset_template(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'emails.view', 'You can’t edit email templates.')
    t = find(ctx, 'email_templates', req.params['id'], 'Template')
    d = next((s for s in SYSTEM_EMAIL_TEMPLATES if s['key'] == t.get('key')), None)
    if not d:
        err(409, 'NO_DEFAULT', 'This template has no built-in default to restore.')
    out = update(ctx, 'email_templates', t['id'], {'subject': d['subject'], 'body': d['body'], 'active': True}, req.if_match)  # type: ignore[index]
    log_activity(ctx, 'email_template', t['id'], 'reset', f'{js_str(ctx.user.get("name", UNDEF))} restored the default “{t["name"]}” email template')
    return _template_view(out)


def preview_template(req: Req) -> Any:
    """Preview never writes, so it is allowed while viewing as another role."""
    ctx = req.ctx
    require_action(ctx, 'emails.view')
    t = find(ctx, 'email_templates', req.params['id'], 'Template')
    subject = js_str(req.b('subject')) if req.b('subject') is not UNDEF else t['subject']
    text = js_str(req.b('body')) if req.b('body') is not UNDEF else t['body']
    allowed = coalesce(TEMPLATE_PLACEHOLDERS.get(t.get('key'), UNDEF), TEMPLATE_PLACEHOLDERS['custom'])
    used = list(dict.fromkeys(_placeholders_in(subject + '\n' + text)))
    vs = _sample_vars(ctx)
    # unknown placeholders render visibly so the editor can spot them
    shown = {p: (coalesce(vs.get(p, UNDEF), '') if p in allowed else '{{' + p + '}}') for p in used}
    return {
        'subject': render_template(subject, shown),
        'body': render_template(text, shown),
        'to': vs['username'],
        'unknown': [p for p in used if p not in allowed],
        'missing': [p for p in REQUIRED_PLACEHOLDERS.get(t.get('key'), []) if p not in used],
    }


def list_outbox(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'emails.view')
    key = _str(req.q('template_key'))
    q = _str(req.q('search')).lower()
    out = [
        m
        for m in rows(ctx, 'email_outbox')
        if (not key or m.get('template_key') == key)
        and (not q or q in m['to'].lower() or q in m['subject'].lower() or q in user_name(ctx, m.get('to_user_id')).lower())
    ]
    out = sorted(out, key=lambda m: locale_key(coalesce(m.get('sent_at'), m['created_at'])), reverse=True)[:300]
    return [{**m, 'to_name': user_name(ctx, m['to_user_id']) if js_truthy(m.get('to_user_id')) else None} for m in out]


def get_outbox(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'emails.view')
    m = find(ctx, 'email_outbox', req.params['id'], 'Email')
    return {**m, 'to_name': user_name(ctx, m['to_user_id']) if js_truthy(m.get('to_user_id')) else None}


def register(r: Any) -> None:
    # Readable by everyone in the tenant (pickers on employee forms, profile pages). Writes need master.edit / shifts.manage.
    for path, table in MASTER_TABLES.items():
        r.get(f'/master/{path}', _list_master(table))
    r.get('/master/org', get_org)
    r.get('/master/org/meta', org_meta)
    r.put('/master/org', put_org)
    r.get('/master/usage', master_usage)
    r.get('/master/people', master_people)
    for path in MASTER_TABLES:
        h = _master_routes(path)
        r.post(f'/master/{path}', h['create'])
        r.patch(f'/master/{path}/:id', h['edit'])
        r.delete(f'/master/{path}/:id', h['delete'])
        r.post(f'/master/{path}/:id/activate', h['activate'])
        r.post(f'/master/{path}/:id/deactivate', h['deactivate'])
    r.get('/master/shifts/:id/members', shift_members)
    r.post('/master/shifts/:id/assign', shift_assign)
    # Email templates & outbox
    r.get('/emails/templates', list_templates)
    r.get('/emails/templates/:id', get_template)
    r.put('/emails/templates/:id', put_template)
    r.post('/emails/templates/:id/reset', reset_template)
    r.post('/emails/templates/:id/preview', preview_template, allow_view_as_write=True)
    r.get('/emails/outbox', list_outbox)
    r.get('/emails/outbox/:id', get_outbox)


# ---------------------------------------------------------------------------------------------------------------------
# Appliers (direct apply and on approval)
# ---------------------------------------------------------------------------------------------------------------------
def _apply_org(ctx: Ctx, cs: dict) -> dict[str, Any]:
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    assert_base_version(cs, t['version'])  # type: ignore[index]
    pt = cs['payload']['patch']
    t = patch('tenants', t, {'org': {**coalesce(prop(t, 'org'), {}), **pt}, 'version': t['version'] + 1})  # type: ignore[arg-type, index]
    by = f' (proposed by {user_name(ctx, cs["maker_id"])}, approved by {js_str(ctx.user.get("name", UNDEF))})' if cs['id'] != 'cs_direct' else ''
    log_activity(
        ctx, 'org_profile', t['id'], 'updated', f'Organisation profile updated: {", ".join(js_str(coalesce(ORG_FIELD_LABEL.get(k, UNDEF), k)) for k in pt)}{by}'
    )
    return {'result': t['org'], 'result_id': t['id']}


register_applier('org_profile', _apply_org)


def _apply_master(ctx: Ctx, cs: dict) -> dict[str, Any]:
    p = cs['payload']
    if p.get('type') not in MASTER_TABLES:
        err(422, 'VALIDATION', 'Unknown master data type.')
    path = p['type']
    table = MASTER_TABLES[path]
    label = LABEL[path]
    by = f' (proposed by {user_name(ctx, cs["maker_id"])})' if cs['id'] != 'cs_direct' else ''

    def exclusive(flag: str, keep_id: Any) -> None:
        for x in list(rows(ctx, table)):
            if x['id'] != keep_id and js_truthy(x.get(flag)):
                update(ctx, table, x['id'], {flag: False})

    op = p.get('op')
    if op == 'create':
        data = _normalise(ctx, path, coalesce(p.get('data', UNDEF), {}), None)  # re-check uniqueness at apply time
        row = insert(ctx, table, {**data, 'active': True})
        if path == 'locations' and data.get('is_head_office'):
            exclusive('is_head_office', row['id'])
        if path == 'shifts' and data.get('is_default'):
            exclusive('is_default', row['id'])
        if path == 'locations' and not any(x.get('is_head_office') for x in rows(ctx, 'locations')):
            update(ctx, 'locations', row['id'], {'is_head_office': True})
        if path == 'shifts' and not any(x.get('is_default') for x in rows(ctx, 'shifts')):
            update(ctx, 'shifts', row['id'], {'is_default': True})
        log_activity(ctx, path, row['id'], 'created', f'{_cap(label)} “{js_str(_name_of(row))}” added{by}')
        return {'result': find(ctx, table, row['id']), 'result_id': row['id']}
    cur = find(ctx, table, p['id'], _cap(label))
    if op != 'assign':
        assert_base_version(cs, cur['version'])
    if op == 'update':
        pdata = coalesce(p.get('data', UNDEF), {})
        data = _normalise(ctx, path, pdata, cur)
        pt = {k: data.get(k, UNDEF) for k in pdata}
        row = update(ctx, table, cur['id'], pt)
        if path == 'locations' and js_truthy(pt.get('is_head_office')):
            exclusive('is_head_office', row['id'])
        if path == 'shifts' and js_truthy(pt.get('is_default')):
            exclusive('is_default', row['id'])
        row = find(ctx, table, cur['id'])
        log_activity(ctx, path, row['id'], 'updated', f'{_cap(label)} “{js_str(_name_of(row))}” edited{by}')
        return {'result': row, 'result_id': row['id']}
    if op == 'delete':
        _assert_deletable(ctx, path, cur)
        remove(ctx, table, cur['id'])
        log_activity(ctx, path, cur['id'], 'deleted', f'{_cap(label)} “{js_str(_name_of(cur))}” deleted{by}')
        return {'result': {'id': cur['id'], 'deleted': True}, 'result_id': cur['id']}
    if op in ('activate', 'deactivate'):
        if op == 'deactivate':
            _assert_deactivatable(ctx, path, cur)
        row = update(ctx, table, cur['id'], {'active': op == 'activate'})
        log_activity(ctx, path, row['id'], 'activated' if op == 'activate' else 'deactivated', f'{_cap(label)} “{js_str(_name_of(row))}” {op}d{by}')
        return {'result': row, 'result_id': row['id']}
    # assign people to a shift
    s = cur
    if not s.get('active'):
        err(409, 'INACTIVE', f'{s["name"]} is inactive. Activate it before assigning people.')
    moved = 0
    for id_ in coalesce(p.get('user_ids', UNDEF), []):
        u = next((x for x in rows(ctx, 'users') if x['id'] == id_), None)
        if not u or u.get('status') == 'deactivated':
            continue
        update(ctx, 'users', u['id'], {'shift_id': s['id']})
        moved += 1
    log_activity(ctx, 'shifts', s['id'], 'assigned', f'{_pl(moved, "person", "people")} moved to {s["name"]}{by}')
    return {'result': {'moved': moved, 'at': now_iso()}, 'result_id': s['id']}


register_applier('master_record', _apply_master)
register_applier('shift', _apply_master)

__all__ = ['register', 'format_employee_code', 'take_employee_code', 'MASTER_TABLES', 'ORG_FIELD_LABEL', 'TEMPLATE_PLACEHOLDERS', 'PLACEHOLDER_LABEL']
