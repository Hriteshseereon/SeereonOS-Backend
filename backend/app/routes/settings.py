"""Module A — tenant settings (UI-06), holidays (UI-07), cost rates (UI-17), data export (UI-26), onboarding checklist
(UI-25) — port of src/mock/routes/settings.ts."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    calendar_for,
    can_see_salaries,
    err,
    find,
    get_db,
    has,
    insert,
    log_activity,
    next_number,
    notify,
    now_iso,
    patch,
    permissions_of,
    remove,
    require_action,
    rows,
    update,
    user_name,
    warn,
)
from app.domain.calendar import (
    add_days,
    avg_working_days_per_month,
    fy_label,
    fy_start_year,
    is_real_iso_date,
    month_of,
    month_range,
    parse_iso,
    today_iso,
    working_days_in_month,
)
from app.domain.money import fmt_money
from app.engine.maker_checker import assert_base_version, pending_brief, propose, register_applier
from app.js import (
    UNDEF,
    coalesce,
    date_parse,
    is_finite,
    is_integer,
    is_nan,
    iso_from_ms,
    js_json_dumps,
    js_len,
    js_max,
    js_number,
    js_round,
    js_spread,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    now_ms,
    nullish,
    pad_start,
    prop,
    to_fixed,
)
from app.routes.auth import tenant_of

SETTING_RULES = cat('mock/routes/settings')['SETTING_RULES']
DEFAULT_SETTINGS = cat('mock/seed')['DEFAULT_SETTINGS']


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _same_value_zero(a: Any, b: Any) -> bool:
    """SameValueZero (Array.prototype.includes, Set membership) for JSON values"""
    if _is_num(a) and _is_num(b):
        return a == b or (a != a and b != b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if a is None or b is None:
        return a is b
    return a is b


def _includes(lst: Any, v: Any) -> bool:
    return any(_same_value_zero(x, v) for x in lst)


def _set_items(v: Any) -> list:
    """[...new Set(v ?? [])]"""
    if nullish(v):
        return []
    if isinstance(v, list):
        items = v
    elif isinstance(v, str):
        items = list(v)
    else:
        raise TypeError('object is not iterable')
    out: list = []
    for x in items:
        if not _includes(out, x):
            out.append(x)
    return out


def _jss(v: Any) -> Any:
    """JSON.stringify(v) (None stands for JS undefined)"""
    return None if v is UNDEF else js_json_dumps(v)


_TIME_RE = re.compile(r'^([01][0-9]|2[0-3]):[0-5][0-9]\Z')
_PREFIX_RE = re.compile(r'^[A-Z][A-Z0-9]{1,5}\Z')


def _validate_setting(key: str, v: Any) -> Any:
    r = SETTING_RULES.get(key)
    if not r:
        err(422, 'VALIDATION', f'Unknown setting {key}.', key)
    kind = r['kind']  # type: ignore[index]
    label = r['label']  # type: ignore[index]
    if kind == 'fixed':
        err(422, 'VALIDATION', f'{label} cannot be changed.', key)
    if kind == 'num':
        n = js_number(v)
        if not is_finite(n) or n < r['min'] or n > r['max']:  # type: ignore[index]
            err(422, 'VALIDATION', f'{label} must be between {js_str(r["min"])} and {js_str(r["max"])}.', key)  # type: ignore[index]
        step = r.get('step', UNDEF)  # type: ignore[union-attr]
        if js_truthy(step) and abs(n / step - js_round(n / step)) > 1e-9:
            err(422, 'VALIDATION', f'{label} must be in steps of {js_str(step)}.', key)
        if not js_truthy(step) and not key.startswith('finance.') and not is_integer(n):
            err(422, 'VALIDATION', f'{label} must be a whole number.', key)
        return n
    if kind == 'bool':
        if not isinstance(v, bool):
            err(422, 'VALIDATION', f'{label} must be on or off.', key)
        return v
    if kind == 'enum':
        if not _includes(r['values'], v):  # type: ignore[index]
            err(422, 'VALIDATION', f'Choose a valid {label.lower()}.', key)
        return v
    if kind == 'time':
        if not _TIME_RE.search(js_str(v)):
            err(422, 'VALIDATION', f'{label} must be a time like 16:00.', key)
        return v
    if kind == 'text':
        s = js_trim(js_str(coalesce(v, ''))).upper()
        if not _PREFIX_RE.search(s) or js_len(s) > r['max']:  # type: ignore[index]
            err(422, 'VALIDATION', f'{label} must be 2–{js_str(r["max"])} letters or digits, starting with a letter.', key)  # type: ignore[index]
        return s
    if kind == 'days':
        a = sorted((js_number(x) for x in _set_items(v)), key=js_str)
        if any(not is_integer(d) or d < 0 or d > 5 for d in a):
            err(422, 'VALIDATION', 'Weekly offs are Sunday to Friday; Saturdays use the Saturday rule.', key)
        if len(a) > 3:
            err(422, 'VALIDATION', 'Choose at most three weekly off days.', key)
        return a
    if kind == 'weeks':
        a = sorted((js_number(x) for x in _set_items(v)), key=js_str)
        if any(not is_integer(d) or d < 1 or d > 5 for d in a):
            err(422, 'VALIDATION', 'Saturdays off must be 1st to 5th.', key)
        return a
    return UNDEF


TIME_ZONES = ['Asia/Kolkata', 'Asia/Dubai', 'Asia/Riyadh', 'Asia/Singapore', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'Australia/Sydney']


def _settings_touched(ctx: Ctx) -> bool:
    """Admin touched workspace settings? (derived — activity log, non-default values, or a logo)"""
    t = tenant_of(ctx)
    if any(a.get('entity') == 'tenant_settings' for a in rows(ctx, 'activity_log')):
        return True
    if js_truthy(t.get('logo_data_url')):
        return True
    st = t['settings']
    return any(_jss(st.get(k, UNDEF)) != _jss(DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS)


# ---------------------------------------------------------------------------------------------------------------------
# Cost rates
# ---------------------------------------------------------------------------------------------------------------------
def _sort_desc_eff(lst: list) -> list:
    return sorted(lst, key=lambda r: locale_key(r['effective_from']), reverse=True)


def _rate_on(lst: list, d: str) -> dict | None:
    xs = _sort_desc_eff([r for r in lst if r['effective_from'] <= d])
    return xs[0] if xs else None


def _locked_months(ctx: Ctx) -> list[str]:
    """new Set(final payroll months) — insertion order kept"""
    return list(dict.fromkeys(p['month'] for p in rows(ctx, 'payroll_runs') if p.get('status') == 'final'))


# ---------------------------------------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------------------------------------
EXPORT_SKIP = frozenset(['passwords', 'login_attempts', 'sessions', 'reset_tokens', 'admin_audit', 'number_sequences', 'tenants', 'pending_tokens'])


def _tenant_dump(ctx: Ctx) -> dict[str, Any]:
    d = get_db()
    tid = ctx.tenant_id
    tables: dict[str, Any] = {}
    for name in d.keys():
        if name in EXPORT_SKIP:
            continue
        lst = [r for r in d[name] if r.get('tenant_id') == tid]
        if name == 'invites':
            tables[name] = [{**r, 'token': '[redacted]'} for r in lst]
        elif name == 'files':
            tables[name] = [{**r, 'data_url': '[binary omitted]' if js_truthy(r.get('data_url')) else None} for r in lst]
        else:
            tables[name] = lst
    t = tenant_of(ctx)
    return {
        'tenant': {
            'id': t['id'],
            'name': prop(t, 'name'),
            'slug': prop(t, 'slug'),
            'industry': prop(t, 'industry'),
            'time_zone': prop(t, 'time_zone'),
            'settings': prop(t, 'settings'),
            'theme': prop(t, 'theme'),
        },
        'tables': tables,
    }


# ---------------------------------------------------------------------------------------------------------------------
# settings (UI-06)
# ---------------------------------------------------------------------------------------------------------------------
def get_settings(req: Req) -> Any:
    ctx = req.ctx
    t = tenant_of(ctx)
    if t is None:
        err(404, 'NOT_FOUND', 'Workspace not found.')  # the platform admin has no workspace
    fm = prop(t['settings'], 'company.fy_start_month')
    fy = fy_start_year(today_iso(), fm)
    # Feature Guide §16: average working days a month this FY, after weekly offs and holidays
    calendar_stats = {'fy_label': fy_label(fy), 'avg_working_days': avg_working_days_per_month(fy, calendar_for(ctx), fm)}
    return {'tenant': t, 'time_zones': TIME_ZONES, 'rules': SETTING_RULES, 'defaults': DEFAULT_SETTINGS, 'calendar_stats': calendar_stats}


def _g(b: Any, k: str) -> Any:
    return b.get(k, UNDEF) if isinstance(b, dict) else UNDEF


def put_settings(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'settings.edit', 'Only admins can change workspace settings.')
    t = tenant_of(ctx)
    if req.if_match is not UNDEF and req.if_match != t['version']:
        err(409, 'STALE', 'Settings were changed by someone else. Reload to see the latest, then make your change again.')
    changed: list[str] = []
    cur_s = t['settings']
    nxt: dict[str, Any] = dict(cur_s)
    for key, v in js_spread(coalesce(req.b('settings'), {})).items():
        if _jss(cur_s.get(key, UNDEF)) == _jss(v):
            continue
        nxt[key] = _validate_setting(key, v)
        changed.append(SETTING_RULES[key]['label'])
    s = nxt
    mx, mn = s.get('calendar.max_hours_per_day'), s.get('calendar.min_hours_per_day')
    if _is_num(mx) and _is_num(mn) and mx < mn:
        err(422, 'VALIDATION', 'Maximum hours must be at least the minimum hours.', 'calendar.max_hours_per_day')
    if s.get('calendar.saturday_rule') == 'some_off' and len(s.get('calendar.saturday_off_weeks') or []) == 0:
        err(422, 'VALIDATION', 'Pick which Saturdays are off, or choose "All Saturdays on".', 'calendar.saturday_off_weeks')
    tb = coalesce(req.b('tenant'), {})
    tenant_patch: dict[str, Any] = {}
    tn = _g(tb, 'name')
    if tn is not UNDEF and not (isinstance(tn, str) and tn == t.get('name')):
        if js_len(js_trim(js_str(tn))) < 2:
            err(422, 'VALIDATION', 'Enter the company name.', 'name')
        tenant_patch['name'] = js_trim(js_str(tn))[:80]
        changed.append('Company name')
    tz = _g(tb, 'time_zone')
    if tz is not UNDEF and not (isinstance(tz, str) and tz == t.get('time_zone')):
        if not (isinstance(tz, str) and tz in TIME_ZONES):
            err(422, 'VALIDATION', 'Choose a supported time zone.', 'time_zone')
        tenant_patch['time_zone'] = tz
        changed.append('Time zone')
    ind = _g(tb, 'industry')
    if ind is not UNDEF and not (isinstance(ind, str) and ind == t.get('industry')):
        if not (isinstance(ind, str) and ind in ('it_services', 'sap_consulting', 'product', 'other')):
            err(422, 'VALIDATION', 'Choose an industry.', 'industry')
        tenant_patch['industry'] = ind
        changed.append('Industry')
    if not changed:
        return {'pending': False, 'result': t, 'result_id': t['id']}
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for k, v in tenant_patch.items():
        before[k] = t.get(k, UNDEF)
        after[k] = v
    for k in nxt:
        if _jss(cur_s.get(k, UNDEF)) != _jss(nxt[k]):
            rule = SETTING_RULES.get(k)
            lbl = rule['label'] if rule else k
            before[lbl] = cur_s.get(k, UNDEF)
            after[lbl] = nxt[k]
    return propose(
        ctx,
        {
            'entity': 'settings',
            'entity_id': t['id'],
            'action': 'update',
            'summary': f'Company settings: {", ".join(changed)}',
            'before': before,
            'after': after,
            'payload': {'settings': s, 'tenant': tenant_patch, 'changed': changed},
            'required_permission': 'settings.edit',
            'base_version': t['version'],
        },
    )


# ---------------------------------------------------------------------------------------------------------------------
# holidays (UI-07)
# ---------------------------------------------------------------------------------------------------------------------
def list_holidays(req: Req) -> Any:
    ctx = req.ctx
    year = js_str(coalesce(req.q('year'), today_iso()[:4]))
    cal = calendar_for(ctx)
    hol = rows(ctx, 'holidays')
    lst = sorted((h for h in hol if h['date'].startswith(year)), key=lambda h: locale_key(h['date']))
    months = []
    for i in range(12):
        ym = f'{year}-{pad_start(js_str(i + 1), 2, "0")}'
        months.append({'month': ym, 'working_days': working_days_in_month(ym, cal)})
    years = list(dict.fromkeys(h['date'][:4] for h in hol))
    return {'year': js_number(year), 'holidays': lst, 'months': months, 'years': years}


_DATE_SHAPE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')


def _holiday_input(ctx: Ctx, body: dict, except_id: Any = UNDEF) -> dict[str, Any]:
    date = js_str(coalesce(body.get('date', UNDEF), ''))
    name = js_trim(js_str(coalesce(body.get('name', UNDEF), '')))
    if not _DATE_SHAPE.search(date) or parse_iso(date) is None:
        err(422, 'VALIDATION', 'Choose a date.', 'date')
    if js_len(name) < 2:
        err(422, 'VALIDATION', 'Enter the holiday name.', 'name')
    if js_len(name) > 60:
        err(422, 'VALIDATION', 'Keep the name under 60 characters.', 'name')
    dup = next((h for h in rows(ctx, 'holidays') if h['date'] == date and h['id'] != except_id), None)
    if dup:
        err(409, 'HOLIDAY_EXISTS', f'{dup["name"]} is already on that date.', 'date')
    return {'date': date, 'name': name, 'optional': js_truthy(body.get('optional', UNDEF))}


def create_holiday(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holidays.')
    v = _holiday_input(ctx, req.body if isinstance(req.body, dict) else {})
    out = insert(ctx, 'holidays', v)
    if month_of(v['date']) in _locked_months(ctx):
        warn(ctx, 'PAYROLL_FINAL', f'Payroll for {month_of(v["date"])} is already final; this holiday will not change it.')
    log_activity(ctx, 'holiday', out['id'], 'create', f'Added holiday {v["name"]} on {v["date"]}')
    return out


def patch_holiday(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holidays.')
    h = find(ctx, 'holidays', req.params['id'], 'Holiday')
    v = _holiday_input(
        ctx,
        {
            'date': coalesce(req.b('date'), h.get('date', UNDEF)),
            'name': coalesce(req.b('name'), h.get('name', UNDEF)),
            'optional': coalesce(req.b('optional'), h.get('optional', UNDEF)),
        },
        h['id'],
    )
    out = update(ctx, 'holidays', h['id'], v, req.if_match)
    log_activity(ctx, 'holiday', h['id'], 'update', f'Updated holiday {v["name"]}')
    return out


def delete_holiday(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can change holidays.')
    h = find(ctx, 'holidays', req.params['id'], 'Holiday')
    remove(ctx, 'holidays', h['id'])
    log_activity(ctx, 'holiday', h['id'], 'delete', f'Deleted holiday {h["name"]}')
    return {'ok': True}


_DMY = re.compile(r'^([0-9]{1,2})[/.-]([0-9]{1,2})[/.-]([0-9]{4})\Z')


def import_holidays(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'holidays.edit', 'Only admins can import holidays.')
    inp = coalesce(req.b('rows'), [])
    ln = len(inp) if isinstance(inp, (list, str)) else UNDEF
    if not js_truthy(ln):
        err(422, 'VALIDATION', 'Paste at least one row: date,name[,optional].')
    if not isinstance(inp, list):
        raise TypeError('input.map is not a function')
    existing: dict[str, Any] = {}
    for h in rows(ctx, 'holidays'):
        existing[h['date']] = h.get('name', UNDEF)  # new Map(...): the LAST duplicate wins
    seen: set[str] = set()
    out = []
    for i, row in enumerate(inp):
        if nullish(row):
            raise TypeError("Cannot read properties of null (reading 'date')")
        errors: list[str] = []
        date = js_trim(js_str(coalesce(_g(row, 'date'), '')))
        dm = _DMY.search(date)
        if dm:
            date = f'{dm.group(3)}-{pad_start(dm.group(2), 2, "0")}-{pad_start(dm.group(1), 2, "0")}'
        name = js_trim(js_str(coalesce(_g(row, 'name'), '')))
        opt = _g(row, 'optional')
        opt_raw = js_trim(js_str(coalesce(opt, ''))).lower()
        optional = opt is True or opt_raw in ('yes', 'y', 'true', '1', 'optional')
        pd = parse_iso(date) if _DATE_SHAPE.search(date) else None
        if pd is None or pd.day != js_number(date[8:10]):
            errors.append('Date must be YYYY-MM-DD or DD/MM/YYYY')
        if js_len(name) < 2:
            errors.append('Name is missing')
        if date in existing:
            errors.append(f'Already a holiday: {js_str(existing[date])}')
        if date in seen:
            errors.append('Date appears twice in this file')
        seen.add(date)
        out.append({'line': i + 1, 'date': date, 'name': name, 'optional': optional, 'errors': errors})
    bad = sum(1 for x in out if x['errors'])
    if req.b('mode') != 'commit':
        return {'rows': out, 'valid': len(out) - bad, 'invalid': bad, 'committed': False}
    if bad:
        err(422, 'VALIDATION', f'{bad} row{" has" if bad == 1 else "s have"} errors. Fix them and import again — nothing was added.')
    for x in out:
        insert(ctx, 'holidays', {'date': x['date'], 'name': x['name'], 'optional': x['optional']})
    log_activity(ctx, 'holiday', 'import', 'create', f'Imported {len(out)} holidays')
    return {'rows': out, 'valid': len(out), 'invalid': 0, 'committed': True}


# ---------------------------------------------------------------------------------------------------------------------
# cost rates (UI-17)
# ---------------------------------------------------------------------------------------------------------------------
def list_cost_rates(req: Req) -> Any:
    ctx = req.ctx
    if not can_see_salaries(ctx):
        err(403, 'FORBIDDEN', 'Cost rates need the salaries grant. Ask an admin.')
    today = today_iso()
    ym = month_of(today)
    cal = calendar_for(ctx)
    wd = working_days_in_month(ym, cal)
    min_h = ctx.tenant['settings'].get('calendar.min_hours_per_day', UNDEF)
    locked = _locked_months(ctx)
    locked_to = [month_range(m)['to'] for m in locked]
    by_user: dict = {}
    for x in rows(ctx, 'cost_rates'):
        by_user.setdefault(x.get('user_id'), []).append(x)
    people = []
    for u in rows(ctx, 'users'):
        if u.get('status') == 'invited':
            continue
        hist = _sort_desc_eff(by_user.get(u['id'], []))
        cur = _rate_on(hist, today)
        nxt = next((x for x in hist if x['effective_from'] > today), None)
        cph = js_round(cur['monthly_ctc_paise'] / (wd * min_h)) if cur and wd > 0 else None
        people.append(
            {
                'user_id': u['id'],
                'status': prop(u, 'status'),
                'current': cur,
                'upcoming': nxt,
                'cost_per_hour_paise': cph,
                'variable_paise': js_round((cur['monthly_ctc_paise'] * cur['variable_pct']) / 100) if cur else None,
                'history': [
                    {
                        **h,
                        'locked': any(h['effective_from'] <= to for to in locked_to),
                        'cost_per_hour_paise': js_round(h['monthly_ctc_paise'] / (wd * min_h)) if wd > 0 else None,
                    }
                    for h in hist
                ],
                # v4: a salary change waiting for a checker (maker-checker entity `cost_rate`, keyed by person)
                'pending_change': pending_brief(ctx, 'cost_rate', u['id']),
            }
        )
    last_final = sorted(locked)[-1] if locked else None
    total = 0
    for p in people:
        if p['status'] == 'active':
            total += coalesce(p['current'].get('monthly_ctc_paise', UNDEF) if p['current'] else UNDEF, 0)
        else:
            total += 0
    return {
        'month': ym,
        'working_days': wd,
        'min_hours': min_h,
        'locked_through': month_range(last_final)['to'] if last_final else None,
        'can_edit': has(ctx, 'cost_rates.edit'),
        'people': people,
        'total_ctc_paise': total,
    }


def _show(r: dict | None, is_sales: bool) -> Any:
    if not r:
        return None
    out: dict[str, Any] = {'Monthly CTC': fmt_money(r['monthly_ctc_paise']), 'Variable pay': f'{js_str(r["variable_pct"])}%'}
    if is_sales:
        out['Sales target'] = fmt_money(r['sales_target_paise'])
    out['Effective from'] = r['effective_from']
    return out


def create_cost_rate(req: Req) -> Any:
    """Salary changes are maker-checked (entity `cost_rate`, one pending change per person)."""
    ctx = req.ctx
    require_action(ctx, 'cost_rates.edit', 'Only admins can change cost rates.')
    v = _validate_rate(ctx, req.body)
    hist = _sort_desc_eff([x for x in rows(ctx, 'cost_rates') if x.get('user_id') == v['user']['id']])
    cur = _rate_on(hist, today_iso())
    is_sales = v['user'].get('role') == 'sales'
    delta = ''
    if cur:
        cm = cur['monthly_ctc_paise']
        delta = f' ({"+" if v["ctc"] >= cm else ""}{to_fixed(((v["ctc"] - cm) / cm) * 100, 1)}%)'
    return propose(
        ctx,
        {
            'entity': 'cost_rate',
            'entity_id': v['user']['id'],
            'action': 'create',
            'required_permission': 'cost_rates.edit',
            'base_version': len(hist),
            'summary': f'New rate for {v["user"]["name"]}: {fmt_money(v["ctc"])}/month from {v["eff"]}{delta}',
            'before': _show(cur, is_sales),
            'after': _show({'monthly_ctc_paise': v['ctc'], 'variable_pct': v['vp'], 'sales_target_paise': v['target'], 'effective_from': v['eff']}, is_sales),
            'payload': {
                'op': 'add',
                'user_id': v['user']['id'],
                'monthly_ctc_paise': v['ctc'],
                'variable_pct': v['vp'],
                'sales_target_paise': v['target'],
                'effective_from': v['eff'],
            },
        },
    )


def delete_cost_rate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'cost_rates.edit', 'Only admins can change cost rates.')
    cr = find(ctx, 'cost_rates', req.params['id'], 'Rate')
    if cr['effective_from'] <= today_iso():
        err(409, 'RATE_PERIOD_LOCKED', 'Only future rates can be removed. Add a new row instead.')
    u = find(ctx, 'users', cr['user_id'], 'Person')
    return propose(
        ctx,
        {
            'entity': 'cost_rate',
            'entity_id': u['id'],
            'action': 'delete',
            'required_permission': 'cost_rates.edit',
            'base_version': sum(1 for x in rows(ctx, 'cost_rates') if x.get('user_id') == u['id']),
            'summary': f'Remove {u["name"]}’s future rate of {fmt_money(cr["monthly_ctc_paise"])} from {cr["effective_from"]}',
            'before': {
                'Monthly CTC': fmt_money(cr['monthly_ctc_paise']),
                'Variable pay': f'{js_str(cr["variable_pct"])}%',
                'Effective from': cr['effective_from'],
            },
            'after': None,
            'payload': {'op': 'remove', 'rate_id': cr['id'], 'user_id': u['id']},
        },
    )


def _validate_rate(ctx: Ctx, body: Any, quiet: bool = False) -> dict[str, Any]:
    """Validation shared by the proposal and the applier (rules are re-checked at approval time)."""
    u = find(ctx, 'users', js_str(coalesce(_g(body, 'user_id'), '')), 'Person')
    ctc = js_number(_g(body, 'monthly_ctc_paise'))
    vp = js_number(coalesce(_g(body, 'variable_pct'), 0))
    eff = js_str(coalesce(_g(body, 'effective_from'), ''))
    if not is_integer(ctc) or ctc <= 0:
        err(422, 'MONEY_INVALID', 'Enter a monthly CTC above zero.', 'monthly_ctc_paise')
    if ctc > 1e10:
        err(422, 'MONEY_INVALID', 'That CTC looks too large. Enter the monthly amount in rupees.', 'monthly_ctc_paise')
    if not is_finite(vp) or vp < 0 or vp > 50:
        err(422, 'VALIDATION', 'Variable pay must be between 0% and 50%.', 'variable_pct')
    if not is_real_iso_date(eff):
        err(422, 'VALIDATION', 'Choose the effective-from date.', 'effective_from')
    locked = _locked_months(ctx)
    if month_of(eff) in locked:
        err(409, 'RATE_PERIOD_LOCKED', f'Payroll for {month_of(eff)} is final. Choose a date after {month_range(sorted(locked)[-1])["to"]}.', 'effective_from')
    last_locked = sorted(locked)[-1] if locked else None
    if last_locked and eff <= month_range(last_locked)['to']:
        err(409, 'RATE_PERIOD_LOCKED', f'Rates before {month_range(last_locked)["to"]} are locked by a final payroll run.', 'effective_from')
    hist = [x for x in rows(ctx, 'cost_rates') if x.get('user_id') == u['id']]
    if any(x['effective_from'] == eff for x in hist):
        err(409, 'RATE_EXISTS', 'A rate already starts on that date. Rates are never edited — pick a new date.', 'effective_from')
    latest = sorted(x['effective_from'] for x in hist)[-1] if hist else None
    if latest and eff < latest:
        err(409, 'RATE_PERIOD_LOCKED', f'History is append-only. The latest rate starts {latest}; choose a later date.', 'effective_from')
    target: Any = None
    if u.get('role') == 'sales':
        st = _g(body, 'sales_target_paise')
        if nullish(st) or st == '':
            target = ctc if prop(prop(ctx.tenant, 'settings'), 'pay.target_default') == 'ctc' else None
        else:
            target = js_number(st)
            if not is_integer(target) or target < 0:
                err(422, 'MONEY_INVALID', 'Enter a valid sales target.', 'sales_target_paise')
    if eff < today_iso() and not quiet:
        warn(ctx, 'RATE_BACKDATED', 'Backdated rate: project costs from that date will be recomputed with it.')
    return {'user': u, 'ctc': ctc, 'vp': vp, 'eff': eff, 'target': target}


def _apply_cost_rate(ctx: Ctx, cs: dict) -> dict[str, Any]:
    p = cs['payload']
    assert_base_version(cs, sum(1 for x in rows(ctx, 'cost_rates') if x.get('user_id') == p['user_id']))
    by = f' (proposed by {user_name(ctx, cs["maker_id"])})' if cs['id'] != 'cs_direct' else ''
    if p['op'] == 'remove':
        cr = find(ctx, 'cost_rates', p['rate_id'], 'Rate')
        if cr['effective_from'] <= today_iso():
            err(409, 'RATE_PERIOD_LOCKED', 'That rate has already started, so it can no longer be removed.')
        remove(ctx, 'cost_rates', cr['id'])
        log_activity(ctx, 'cost_rate', cr['id'], 'delete', f'Removed {user_name(ctx, p["user_id"])}’s future rate from {cr["effective_from"]}{by}')
        return {'result': {'ok': True}, 'result_id': cr['id']}
    v = _validate_rate(ctx, p, True)
    out = insert(
        ctx,
        'cost_rates',
        {'user_id': v['user']['id'], 'monthly_ctc_paise': v['ctc'], 'variable_pct': v['vp'], 'sales_target_paise': v['target'], 'effective_from': v['eff']},
    )
    log_activity(ctx, 'cost_rate', out['id'], 'create', f'New rate for {v["user"]["name"]} from {v["eff"]}: {fmt_money(v["ctc"])}/month{by}')
    return {'result': out, 'result_id': out['id']}


register_applier('cost_rate', _apply_cost_rate)


# ---------------------------------------------------------------------------------------------------------------------
# data export (UI-26)
# ---------------------------------------------------------------------------------------------------------------------
def list_exports(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'data.export', 'Only admins can export workspace data.')
    now = now_iso()
    out = [
        {**x, 'status': 'expired'} if x.get('status') == 'ready' and js_truthy(x.get('expires_at')) and x['expires_at'] < now else x
        for x in rows(ctx, 'data_exports')
    ]
    return sorted(out, key=lambda x: locale_key(x['created_at']), reverse=True)


def create_export(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'data.export', 'Only admins can export workspace data.')
    now = now_ms()
    recent = [x for x in rows(ctx, 'data_exports') if now - date_parse(x['created_at']) < 60_000]
    if len(recent) >= 3:
        err(429, 'RATE_LIMITED', 'You just requested several exports. Wait a minute and try again.')
    dump = _tenant_dump(ctx)
    size = js_len(js_json_dumps(dump).decode('utf-8'))
    ready = now_iso()
    out = insert(
        ctx,
        'data_exports',
        {'requested_by': ctx.user_id, 'status': 'ready', 'ready_at': ready, 'expires_at': iso_from_ms(now_ms() + 24 * 3600_000), 'size_bytes': size},
    )
    notify(
        ctx,
        ctx.user_id,
        'NTF-17',
        'Your data export is ready',
        f'Download it within 24 hours ({js_str(js_max(1, js_round(size / 1024)))} KB).',
        '/settings/data',
    )
    log_activity(ctx, 'data_export', out['id'], 'create', 'Requested a full data export')
    return out


def download_export(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'data.export', 'Only admins can export workspace data.')
    x = find(ctx, 'data_exports', req.params['id'], 'Export')
    if x.get('status') != 'ready' or (js_truthy(x.get('expires_at')) and x['expires_at'] < now_iso()):
        err(410, 'EXPORT_EXPIRED', 'This export link has expired. Request a new export.')
    log_activity(ctx, 'data_export', x['id'], 'download', 'Downloaded a data export')
    return {'generated_at': prop(x, 'ready_at'), **_tenant_dump(ctx)}


# ---------------------------------------------------------------------------------------------------------------------
# onboarding (UI-25)
# ---------------------------------------------------------------------------------------------------------------------
def get_onboarding(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'settings.edit')
    users = rows(ctx, 'users')
    steps = [
        {
            'key': 'settings',
            'title': 'Set company rules',
            'body': 'Working days, hours, approval SLA and pay rules.',
            'to': '/settings/company',
            'done': _settings_touched(ctx),
        },
        {
            'key': 'holidays',
            'title': 'Add this year’s holidays',
            'body': 'Holidays drive working days, SLA and payroll.',
            'to': '/settings/holidays',
            'done': len(rows(ctx, 'holidays')) > 0,
        },
        {
            'key': 'people',
            'title': 'Invite your team',
            'body': 'Set roles and who approves whose timesheet.',
            'to': '/people',
            'done': len(users) > 1 or len(rows(ctx, 'invites')) > 0,
        },
        {
            'key': 'project',
            'title': 'Create your first project',
            'body': 'Milestones, tasks and estimates tie hours to money.',
            'to': '/projects',
            'done': len(rows(ctx, 'projects')) > 0,
        },
        {
            'key': 'timesheet',
            'title': 'Submit a first timesheet week',
            'body': 'Log hours against a task and submit for approval.',
            'to': '/timesheet',
            'done': any(w.get('status') not in ('open', 'locked') for w in rows(ctx, 'timesheet_weeks')),
        },
    ]
    dismissed = any(a.get('entity') == 'onboarding' and a.get('action') == 'dismiss' for a in rows(ctx, 'activity_log'))
    created = date_parse(tenant_of(ctx)['created_at'])
    return {
        'steps': steps,
        'done': sum(1 for s in steps if s['done']),
        'total': len(steps),
        'complete': all(s['done'] for s in steps),
        'dismissed': dismissed,
        'can_load_sample': len(rows(ctx, 'projects')) == 0 and len(users) <= 1,
        'tenant_age_days': js_max(0, js_round((now_ms() - created) / 86400000)) if not is_nan(created) else None,
    }


def dismiss_onboarding(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'settings.edit')
    log_activity(ctx, 'onboarding', ctx.tenant_id, 'dismiss', 'Dismissed the getting-started checklist')
    return {'ok': True}


def restore_onboarding(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'settings.edit')
    d = get_db()
    tid = ctx.tenant_id
    d.activity_log = [a for a in d.activity_log if not (a.get('tenant_id') == tid and a.get('entity') == 'onboarding' and a.get('action') == 'dismiss')]
    return {'ok': True}


def sample_onboarding(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'settings.edit', 'Only admins can load sample data.')
    if len(rows(ctx, 'projects')) > 0 or len(rows(ctx, 'users')) > 1:
        err(409, 'SAMPLE_NOT_EMPTY', 'Sample data can only be loaded into an empty workspace.')
    _load_sample(ctx)
    log_activity(ctx, 'onboarding', ctx.tenant_id, 'sample', 'Loaded sample data')
    return {'ok': True}


# ---------------------------------------------------------------------------------------------------------------------
# Sample data for a brand-new tenant (stays inside that tenant)
# ---------------------------------------------------------------------------------------------------------------------
def _load_sample(ctx: Ctx) -> None:
    today = today_iso()
    y = today[:4]
    t = tenant_of(ctx)
    dom = re.sub(r'[^a-z0-9]', '', t['slug']) + '.example'
    me = ctx.user

    def mk(name: str, title: str, role: str, reports: Any, hue: int, ctc_rupees: int, dept: str) -> dict:
        u = insert(
            ctx,
            'users',
            {
                'email': f'{name.split(" ")[0].lower()}@{dom}',
                'name': name,
                'title': title,
                'role': role,
                'reports_to_id': reports,
                'grants': [],
                'status': 'active',
                'last_login_at': None,
                'avatar_hue': hue,
                'phone': None,
                'joined_on': add_days(today, -30),
                'department': dept,
                'two_factor': False,
                'prefs': {},
            },
        )
        insert(
            ctx,
            'cost_rates',
            {
                'user_id': u['id'],
                'monthly_ctc_paise': ctc_rupees * 100,
                'variable_pct': 20 if role == 'sales' else 0,
                'sales_target_paise': ctc_rupees * 100 if role == 'sales' else None,
                'effective_from': add_days(today, -30),
            },
        )
        return u

    if not any(c.get('user_id') == me['id'] for c in rows(ctx, 'cost_rates')):
        insert(
            ctx,
            'cost_rates',
            {'user_id': me['id'], 'monthly_ctc_paise': 150000_00, 'variable_pct': 0, 'sales_target_paise': None, 'effective_from': add_days(today, -30)},
        )
    pm = mk('Aarav Mishra (sample)', 'Project Manager', 'pm', me['id'], 210, 85000, 'delivery')
    dev1 = mk('Diya Sahoo (sample)', 'React Developer', 'employee', pm['id'], 18, 45000, 'delivery')
    dev2 = mk('Kabir Das (sample)', 'Python Developer', 'employee', pm['id'], 265, 50000, 'delivery')
    mk('Isha Patra (sample)', 'Business Development', 'sales', me['id'], 330, 40000, 'sales')

    if not rows(ctx, 'holidays'):
        for d, n, o in [
            (f'{y}-01-26', 'Republic Day', False),
            (f'{y}-03-14', 'Holi', False),
            (f'{y}-08-15', 'Independence Day', False),
            (f'{y}-10-02', 'Gandhi Jayanti', False),
            (f'{y}-10-20', 'Diwali', False),
            (f'{y}-12-25', 'Christmas', True),
        ]:
            insert(ctx, 'holidays', {'date': d, 'name': n, 'optional': o})

    prefix = prop(t['settings'], 'projects.code_prefix')
    no = next_number(ctx, 'project', 1)
    p = insert(
        ctx,
        'projects',
        {
            'code': f'{js_str(prefix)}-{pad_start(js_str(no), 3, "0")}',
            'name': 'Customer portal (sample)',
            'client': 'Sample Client Pvt Ltd',
            'type': 'service',
            'status': 'active',
            'pm_id': pm['id'],
            'start_on': add_days(today, -14),
            'end_on': add_days(today, 60),
            'contract_value_paise': 6_00_000_00,
            'billing_model': 'fixed',
            'stream': 'services',
            'description': 'A sample project to explore milestones, tasks and margins. Delete it any time.',
            'priority': 2,
            'deal_id': None,
            'baseline_end_on': add_days(today, 60),
            'color_hue': 200,
        },
    )
    insert(
        ctx,
        'project_status_history',
        {'project_id': p['id'], 'from_status': None, 'to_status': 'active', 'reason': None, 'changed_by': me['id'], 'changed_at': now_iso()},
    )
    ms = [
        insert(
            ctx,
            'milestones',
            {
                'project_id': p['id'],
                'no': m['no'],
                'name': m['name'],
                'planned_start': add_days(today, m['s']),
                'planned_end': add_days(today, m['e']),
                'baseline_end': add_days(today, m['e']),
                'billing_amount_paise': m['amt'],
                'billing_status': 'none',
                'achieved_on': None,
                'empty_since': None,
                'deleted_at': None,
            },
        )
        for m in [
            {'no': 1, 'name': 'Discovery & design', 's': -14, 'e': 10, 'amt': 1_50_000_00},
            {'no': 2, 'name': 'Build & integrate', 's': 11, 'e': 45, 'amt': 3_00_000_00},
            {'no': 3, 'name': 'UAT & go-live', 's': 46, 'e': 60, 'amt': 1_50_000_00},
        ]
    ]
    tasks = [
        (0, 'Requirement workshops', pm, 16, -14, -8, 'done'),
        (0, 'Wireframes and clickable prototype', dev1, 32, -7, 6, 'in_progress'),
        (0, 'API contract', dev2, 12, -3, 8, 'todo'),
        (1, 'Login and profile screens', dev1, 24, 11, 22, 'todo'),
        (1, 'Orders API', dev2, 40, 11, 30, 'todo'),
        (2, 'UAT support', pm, 20, 46, 58, 'todo'),
    ]
    counters = [0, 0, 0]
    for mi, title, owner, est, s, e, status in tasks:
        counters[mi] += 1
        insert(
            ctx,
            'tasks',
            {
                'project_id': p['id'],
                'milestone_id': ms[mi]['id'],
                'no': counters[mi],
                'title': title,
                'owner_id': owner['id'],
                'backup_id': None,
                'est_hours': est,
                'start_on': add_days(today, s),
                'due_on': add_days(today, e),
                'done_on': add_days(today, e) if status == 'done' else None,
                'complexity': 'medium',
                'status': status,
                'description': '',
                'blocked_reason': None,
            },
        )
    for u in (pm, dev1, dev2):
        insert(
            ctx,
            'allocations',
            {
                'project_id': p['id'],
                'milestone_id': ms[0]['id'],
                'user_id': u['id'],
                'pct': 40 if u['id'] == pm['id'] else 80,
                'from_on': add_days(today, -14),
                'to_on': add_days(today, 10),
            },
        )


def _apply_settings(ctx: Ctx, cs: dict) -> dict[str, Any]:
    t = tenant_of(ctx)
    assert_base_version(cs, t['version'])
    s = cs['payload']['settings']
    t = patch('tenants', t, {**js_spread(cs['payload'].get('tenant', UNDEF)), 'settings': s, 'version': t['version'] + 1})
    if js_truthy(s.get('security.require_2fa_admins', UNDEF)):
        without = []
        for u in rows(ctx, 'users'):
            if u.get('status') == 'active' and not js_truthy(u.get('two_factor')) and not u.get('is_super_admin'):
                pp = permissions_of(u)
                if 'roles.manage' in pp or 'users.manage' in pp:
                    without.append(u)
        if without:
            warn(ctx, 'TWO_FACTOR_PENDING', f'{", ".join(js_str(u.get("name", UNDEF)) for u in without)} must set up two-step verification at next sign-in.')
    by = f' (proposed by {user_name(ctx, cs["maker_id"])})' if cs['id'] != 'cs_direct' else ''
    log_activity(ctx, 'tenant_settings', t['id'], 'update', f'Changed {", ".join(js_str(x) for x in cs["payload"]["changed"])}{by}')
    return {'result': t, 'result_id': t['id']}


register_applier('settings', _apply_settings)


def register(r: Any) -> None:
    # settings (UI-06)
    r.get('/settings', get_settings)
    r.put('/settings', put_settings)
    # holidays (UI-07)
    r.get('/holidays', list_holidays)
    r.post('/holidays', create_holiday)
    r.patch('/holidays/:id', patch_holiday)
    r.delete('/holidays/:id', delete_holiday)
    r.post('/holidays/import', import_holidays)
    # cost rates (UI-17)
    r.get('/cost-rates', list_cost_rates)
    r.post('/cost-rates', create_cost_rate)
    r.delete('/cost-rates/:id', delete_cost_rate)
    # data export (UI-26)
    r.get('/exports', list_exports)
    r.post('/exports', create_export)
    r.get('/exports/:id/download', download_export)
    # onboarding (UI-25)
    r.get('/onboarding', get_onboarding)
    r.post('/onboarding/dismiss', dismiss_onboarding)
    r.post('/onboarding/restore', restore_onboarding)
    r.post('/onboarding/sample', sample_onboarding)


__all__ = ['register', 'SETTING_RULES']
