"""Workstream A — project costing engine (Feature Guide §3 "Create project", §6 "Costing, allocation and margins").

Port of src/mock/costing.ts. Server-side only: the project cost calculator (preview + save), automatic milestone date
spreading, planned / projected / actual margins, "what moved the margin", milestone variance, resources allocated vs
logged, effort ratios and the CEO portfolio. Screens only format what these functions return.

Routes: `register_costing_routes(r, deps)` (the TS registerCostingRoutes, called by routes/projects.ts BEFORE its own
/projects/:id routes) — `register(r)` does the same with the deps of app.routes.projects (can_view / can_edit).

Per-request memo: the TS `WeakMap<Ctx, Map>` is `scope().memo[('mock/costing', ctx)]`; `memo.delete(ctx)` pops it.
The analytics metrics override is installed at import (like the TS module); its recursion guard lives in the request
memo (the TS module-level Set would be shared between threads here).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    can_see_margins,
    can_see_salaries,
    err,
    find,
    get_db,
    has,
    has_any,
    insert,
    log_activity,
    now_iso,
    remove,
    rows,
    rows_where,
    rows_where_ro,
    scope,
    update,
    user_name,
    warn,
)
from app.domain.calendar import (
    CalendarCtx,
    add_days,
    fmt_date,
    fy_label,
    fy_range,
    fy_start_year,
    is_real_iso_date,
    is_weekly_off,
    is_working_day,
    max_iso,
    min_iso,
    month_of,
    month_range,
    working_days_between,
    working_days_in_month,
)
from app.domain.costing import ADDITIONAL_COST_CATEGORIES, cost_per_hour, margin_pct, markup_pct, project_health, rate_on
from app.domain.delivery import progress_of
from app.domain.labels import BILLING_MODEL, PROJECT_TYPE
from app.domain.money import fmt_money
from app.engine import analytics as an
from app.js import (
    UNDEF,
    coalesce,
    compare_numeric,
    is_finite,
    is_integer,
    js_div,
    js_lower,
    js_number,
    js_or,
    js_round,
    js_sort,
    js_str,
    js_trim,
    js_truthy,
    locale_compare,
    nullish,
    pad_start,
    prop,
    to_fixed,
)

CREATE_DOC_MAX_BYTES = cat('mock/costing')['CREATE_DOC_MAX_BYTES']


def _iso(s: Any) -> bool:
    return is_real_iso_date(s)


def r1(n: Any) -> Any:
    return js_round(n * 10) / 10


def r2(n: Any) -> Any:
    return js_round(n * 100) / 100


def _g(o: Any, k: str) -> Any:
    """o.k / o?.k on any JS value: dict -> own value or UNDEF; null/undefined -> UNDEF (optional chaining); other
    primitives / arrays -> UNDEF (no such property)"""
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    return UNDEF


def _strict_prop(o: Any, k: str) -> Any:
    """o.k WITHOUT optional chaining: TypeError on null/undefined (JS 500)"""
    if o is None or o is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(o)} (reading '{k}')")
    return _g(o, k)


def _is_num(v: Any) -> bool:
    """typeof v === 'number'"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num(v: Any, d: Any = 0) -> Any:
    n = js_number(v)
    return n if is_finite(n) else d


def _or_cmp(*ds: Any) -> Any:
    """a || b || c for comparator results (NaN / 0 fall through)"""
    for d in ds[:-1]:
        if js_truthy(d):
            return d
    return ds[-1]


def _slice16(s: str, n: int) -> str:
    """s.slice(0, n) in UTF-16 code units"""
    if len(s) <= n // 2 or all(ord(c) <= 0xFFFF for c in s):
        return s[:n]
    out = []
    units = 0
    for c in s:
        w = 2 if ord(c) > 0xFFFF else 1
        if units + w > n:
            if units < n:  # half a surrogate pair: JS keeps the high surrogate
                o = ord(c) - 0x10000
                out.append(chr(0xD800 + (o >> 10)))
            break
        out.append(c)
        units += w
    return ''.join(out)


# ---------------------------------------------------------------------------------------------------------------------
# per-request memo
# ---------------------------------------------------------------------------------------------------------------------
_MEMO = 'mock/costing'


def _memo(ctx: Ctx) -> dict:
    m = scope().memo
    k = (_MEMO, ctx)
    d = m.get(k)
    if d is None:
        d = {}
        m[k] = d
    return d


def _cached(ctx: Ctx, key: str, f: Callable[[], Any]) -> Any:
    m = _memo(ctx)
    if key not in m:
        m[key] = f()
    return m[key]


def _memo_delete(ctx: Ctx) -> None:
    scope().memo.pop((_MEMO, ctx), None)


# ---------------------------------------------------------------------------------------------------------------------
# schedule (analytics owns the CPM / forecast)
# ---------------------------------------------------------------------------------------------------------------------
def _schedule_for(ctx: Ctx, p: dict) -> dict | None:
    try:
        return coalesce(an.schedule_of(ctx, p['id']).get('schedule'), None)
    except Exception:  # noqa: BLE001 — the TS catches everything
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------------------------------------------------
def _calc(ctx: Ctx) -> CalendarCtx:
    return an.cal(ctx).without_leave()


def _holiday_map(ctx: Ctx) -> dict:
    from app.engine.holiday_cal import holidays_default  # v9 (A5): the company (default) holiday calendar

    return _cached(ctx, 'holmap', lambda: {h['date']: h['name'] for h in holidays_default(ctx.tenant_id) if not js_truthy(h.get('optional'))})


def wd(ctx: Ctx, frm: Any, to: Any) -> int:
    """Working days in [from, to] on the company calendar (weekly offs + holidays; no personal leave)."""
    if not js_truthy(frm) or not js_truthy(to) or to < frm:
        return 0
    return _cached(ctx, f'wd:{frm}:{to}', lambda: working_days_between(frm, to, _calc(ctx)))


def wd_person(ctx: Ctx, user_id: Any, frm: Any, to: Any) -> int:
    """V9-FIX (item 2): working days in [from, to] on the person's calendar (their location's holidays; no leave) —
    the days an allocation of that person costs. The company calendar when the tenant has no holiday calendars."""
    from app.engine.holiday_cal import has_calendars

    if not isinstance(user_id, str) or not user_id or not has_calendars(ctx.tenant_id):
        return wd(ctx, frm, to)
    if not js_truthy(frm) or not js_truthy(to) or to < frm:
        return 0
    c = an.person_cal(ctx, user_id)
    return _cached(ctx, f'wdp:{id(c)}:{frm}:{to}', lambda: working_days_between(frm, to, c))


def slip_days(ctx: Ctx, deadline: str, end: str) -> int:
    """Signed slip in working days: + when `end` is after `deadline`, − when before."""
    if end > deadline:
        return wd(ctx, add_days(deadline, 1), end)
    if end < deadline:
        return -wd(ctx, add_days(end, 1), deadline)
    return 0


def calendar_summary(ctx: Ctx, frm: Any, to: Any) -> dict | None:
    if not _iso(frm) or not _iso(to) or to < frm:
        return None
    c = _calc(ctx)
    hm = _holiday_map(ctx)
    cd = we = hol = work = 0
    lst: list[dict] = []
    stop = add_days(frm, 366 * 4)
    d = frm
    while d <= to and d <= stop:
        cd += 1
        if is_weekly_off(d, c):
            we += 1
        elif d in hm:
            hol += 1
            lst.append({'date': d, 'name': hm[d]})
        else:
            work += 1
        d = add_days(d, 1)
    return {'from': frm, 'to': to, 'calendar_days': cd, 'weekend_days': we, 'holidays': hol, 'working_days': work, 'holiday_list': lst}


# ---------------------------------------------------------------------------------------------------------------------
# Cost per hour (Feature Guide §6, BR-23): cost per hour = monthly CTC ÷ (working days in the month × minimum hours
# per day). An allocation costs its paid hours: working days × allocation % × minimum hours, each at its month's rate.
# ---------------------------------------------------------------------------------------------------------------------
def loading_pct(_ctx: Ctx) -> int:
    """Legacy: cost loading is no longer applied."""
    return 0


def min_hours(ctx: Ctx) -> Any:
    return js_or(ctx.tenant['settings'].get('calendar.min_hours_per_day', UNDEF), 9)


def avg_working_days_per_month(ctx: Ctx, date: str) -> Any:
    """Average working days per month = working days in the financial year ÷ 12."""
    fsm = js_or(ctx.tenant['settings'].get('company.fy_start_month', UNDEF), 4)
    y = fy_start_year(date, fsm)

    def f() -> Any:
        r = fy_range(y, fsm)
        return js_round((wd(ctx, r['from'], r['to']) / 12) * 10) / 10

    return _cached(ctx, 'avgwd:' + js_str(y), f)


def wd_month(ctx: Ctx, ym: str) -> int:
    """Working days in a calendar month (company calendar)."""
    return _cached(ctx, 'wdm:' + ym, lambda: working_days_in_month(ym, _calc(ctx)))


def _rate_for(ctx: Ctx, user_id: Any, date: str) -> dict | None:
    rates = _cached(ctx, 'rates', lambda: rows(ctx, 'cost_rates'))
    r = rate_on(rates, user_id, date)
    if r:
        return r
    # a plan that starts before the person's first rate uses their first rate
    mine = sorted((x for x in rates if x['user_id'] == user_id), key=lambda x: _lk(x['effective_from']))
    return mine[0] if mine else None


def _lk(s: str) -> Any:
    from app.js import locale_key

    return locale_key(s)


def cost_per_hour_for(ctx: Ctx, user_id: Any, date: str) -> Any:
    """Cost per hour (paise, float) on a date = CTC ÷ (working days in that month × minimum hours). None when no rate."""

    def f() -> Any:
        r = _rate_for(ctx, user_id, date)
        if not r:
            return None
        # V9-FIX (item 2): working days of the month on the person's own (location) calendar
        return cost_per_hour(r['monthly_ctc_paise'], an.person_wdm(ctx, user_id, month_of(date)), min_hours(ctx))

    return _cached(ctx, f'cph:{js_str(user_id)}:{month_of(date)}', f)


def cost_per_day_for(ctx: Ctx, user_id: Any, date: str) -> Any:
    """Cost of a full working day (paise) = minimum hours × cost per hour = CTC ÷ working days in the month."""
    ph = cost_per_hour_for(ctx, user_id, date)
    return None if ph is None else js_round(ph * min_hours(ctx))


def range_cost(ctx: Ctx, user_id: Any, pct: Any, frm: Any, to: Any) -> dict[str, Any]:
    """Paid hours and cost of `pct`% of a person's time over [from, to], month by month at each month's rate."""
    if not js_truthy(frm) or not js_truthy(to) or to < frm or not js_truthy(pct):
        return {'days': 0, 'hours': 0, 'cost': 0, 'rated': cost_per_hour_for(ctx, user_id, js_or(frm, ctx.today)) is not None}

    def f() -> dict[str, Any]:
        mh = min_hours(ctx)
        days: Any = 0
        cost: Any = 0
        rated = True
        a = frm
        while a <= to:
            ym = month_of(a)
            b = min_iso(month_range(ym)['to'], to)
            d = wd_person(ctx, user_id, a, b)
            ph = cost_per_hour_for(ctx, user_id, a)
            if ph is None:
                rated = False
            days += d
            cost += (d * pct * mh * (0 if ph is None else ph)) / 100
            a = add_days(b, 1)
        return {'days': days, 'hours': r1((days * pct * mh) / 100), 'cost': js_round(cost), 'rated': rated}

    return _cached(ctx, f'rc:{js_str(user_id)}:{js_str(pct)}:{frm}:{to}', f)


def _resource_row_cost(ctx: Ctx, user_id: Any, pct: Any, frm: str, to: str) -> dict[str, Any]:
    c = range_cost(ctx, user_id, pct, frm, to)
    return {
        'days': c['days'],
        'manDays': r1((c['days'] * pct) / 100),
        'hours': c['hours'],
        'cpd': cost_per_day_for(ctx, user_id, frm) if c['rated'] else None,
        'cph': cost_per_hour_for(ctx, user_id, frm),
        'cost': c['cost'],
    }


# ---------------- v7 standalone estimates (used by routes/estimates.ts) ----------------
def range_cost_at_ctc(ctx: Ctx, monthly_ctc_paise: Any, pct: Any, frm: Any, to: Any) -> dict[str, Any]:
    """Cost of `pct`% of an ASSUMED monthly CTC (a role not hired yet) over [from, to], month by month."""
    if not js_truthy(frm) or not js_truthy(to) or to < frm or not js_truthy(pct) or not (monthly_ctc_paise > 0):
        return {'days': 0, 'manDays': 0, 'hours': 0, 'cost': 0, 'cpd': None, 'cph': None}
    mh = min_hours(ctx)
    days: Any = 0
    cost: Any = 0
    a = frm
    while a <= to:
        ym = month_of(a)
        b = min_iso(month_range(ym)['to'], to)
        d = wd(ctx, a, b)
        days += d
        cost += (d * pct * mh * cost_per_hour(monthly_ctc_paise, wd_month(ctx, ym), mh)) / 100
        a = add_days(b, 1)
    cph = cost_per_hour(monthly_ctc_paise, wd_month(ctx, month_of(frm)), mh)
    return {
        'days': days,
        'manDays': r1((days * pct) / 100),
        'hours': r1((days * pct * mh) / 100),
        'cost': js_round(cost),
        'cpd': js_round(cph * mh),
        'cph': cph,
    }


def person_range_cost(ctx: Ctx, user_id: Any, pct: Any, frm: str, to: str) -> dict[str, Any]:
    """Named-person row cost (same as the project calculator's resource rows)."""
    return _resource_row_cost(ctx, user_id, pct, frm, to)


def cost_chain(resources: Any, additional: Any, maintenance: Any, contingency_pct: Any) -> dict[str, Any]:
    """The CEO's costing chain (costChain of the TS spec), shared by the project calculator and the estimates:
    total cost = resources + additional + maintenance; contingency = total cost × contingency %;
    actual cost = total cost + contingency; price = actual cost + actual cost × markup % (to the paisa);
    profit = price − actual cost; margin = profit ÷ price; markup = profit ÷ actual cost."""
    total = resources + additional + maintenance
    contingency = js_round((total * contingency_pct) / 100)
    return {'total': total, 'contingency': contingency, 'actual': total + contingency}


# v9 (D1) Commercials categories: every additional-cost line belongs to one group of the chain. The v8 categories map
# onto them (saved plans keep loading): software / hardware -> office & hardware, travel / training / third-party ->
# expenses, other -> miscellaneous. Maintenance / support stays its own amount.
COST_GROUPS = (
    ('resources', 'Resource cost'),
    ('office_hardware', 'Office & hardware'),
    ('expenses', 'Expenses'),
    ('misc', 'Miscellaneous'),
    ('maintenance', 'Maintenance / support'),
)
CATEGORY_GROUP = {
    'office_hardware': 'office_hardware',
    'software': 'office_hardware',
    'hardware': 'office_hardware',
    'expenses': 'expenses',
    'travel': 'expenses',
    'training': 'expenses',
    'third_party': 'expenses',
    'misc': 'misc',
    'other': 'misc',
}


def group_of(category: Any) -> str:
    return CATEGORY_GROUP.get(category, 'misc') if isinstance(category, str) else 'misc'


def cost_groups(resources: Any, additional: list[dict], maintenance: Any, see_money: bool = True) -> list[dict]:
    """Total cost by Commercials category (resource cost, office & hardware, expenses, miscellaneous, maintenance):
    the lines of the chain's Total cost. Amounts are None for a reader who can't see money."""
    sums: dict[str, Any] = {k: 0 for k, _ in COST_GROUPS}
    sums['resources'] = resources
    sums['maintenance'] = maintenance
    lines: dict[str, int] = {k: 0 for k, _ in COST_GROUPS}
    for x in additional:
        g = group_of(x.get('category'))
        sums[g] = sums[g] + x['amount_paise']
        lines[g] += 1
    return [{'key': k, 'label': lbl, 'amount': sums[k] if see_money else None, 'lines': lines[k]} for k, lbl in COST_GROUPS]


def price_and_markup(actual: Any, mode: str, markup_pct_: Any, price_paise: Any) -> dict[str, Any]:
    """Price ⇄ markup on the ACTUAL cost (total cost + contingency): price mode keeps the typed price and derives
    markup % = (price − actual) ÷ actual; markup mode prices to the paisa."""
    if mode == 'price' and price_paise is not None and price_paise >= 0:
        return {'price': js_round(price_paise), 'markup': r2(((js_round(price_paise) - actual) / actual) * 100) if actual > 0 else None}
    markup = 0 if markup_pct_ is None else r2(markup_pct_)
    return {'price': actual + js_round((actual * markup) / 100), 'markup': markup}


# ---------------------------------------------------------------------------------------------------------------------
# AUTO MILESTONE SETUP: provisional dates across the project range
# ---------------------------------------------------------------------------------------------------------------------
def spread_milestones(ctx: Ctx, start: Any, end: Any, lst: list[dict]) -> list[dict]:
    """Spread milestones across the project's working days, proportional to billing when every milestone bills."""
    n = len(lst)
    if not n or not _iso(start) or not _iso(end) or end < start:
        return []
    c = _calc(ctx)
    days: list[str] = []
    d = start
    while d <= end and len(days) < 2000:
        if is_working_day(d, c):
            days.append(d)
        d = add_days(d, 1)
    if not days:
        days = [start, end]
    D = len(days)
    proportional = all(m['billing_amount_paise'] > 0 for m in lst)
    weights = [m['billing_amount_paise'] if proportional else 1 for m in lst]
    W: Any = 0
    for w in weights:
        W += w
    cum: Any = 0
    prev = -1
    out = []
    for i, m in enumerate(lst):
        cum += weights[i]
        idx = D - 1 if i == n - 1 else js_round((cum / W) * D) - 1
        if D >= n:
            idx = min(max(idx, prev + 1), D - 1 - (n - 1 - i))
        else:
            idx = min(max(idx, prev), D - 1)
        s = min(prev + 1, D - 1)
        prev = idx
        out.append(
            {
                'index': i,
                'id': None,
                'code': f'M{i + 1}',
                'name': m['name'],
                'billing_amount_paise': m['billing_amount_paise'],
                'planned_start': start if i == 0 else days[s],
                'planned_end': end if i == n - 1 else (days[idx] if 0 <= idx < D else UNDEF),  # days[-1] is undefined in JS
            }
        )
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Cost calculator — preview / validation
# ---------------------------------------------------------------------------------------------------------------------
def _ms_at(milestones: list[dict], idx: Any) -> dict | None:
    """milestones[idx] ?? null for a JS number index"""
    if isinstance(idx, float):
        if not idx.is_integer():
            return None
        idx = int(idx)
    if isinstance(idx, int) and 0 <= idx < len(milestones):
        return milestones[idx]
    return None


def _resolve_rows(ctx: Ctx, rng: dict, milestones: list[dict], resources: list) -> list[dict]:
    users = _cached(ctx, 'usersById', lambda: {u['id']: u for u in rows(ctx, 'users')})
    out = []
    for i, r in enumerate(resources):
        milestone = None
        missing = False
        none = False
        mi = _strict_prop(r, 'milestone_index')
        if _is_num(mi):
            milestone = _ms_at(milestones, mi)
            missing = not milestone
        elif js_truthy(_g(r, 'milestone_id')):
            mid = _g(r, 'milestone_id')
            milestone = next((m for m in milestones if m['id'] == mid and not isinstance(mid, bool)), None)
            missing = not milestone
        else:
            none = True
        fo, to_ = _g(r, 'from_on'), _g(r, 'to_on')
        frm = (
            fo
            if js_truthy(fo) and _iso(fo)
            else max_iso(milestone['planned_start'], rng['start'])
            if milestone
            else rng['start']
            if _iso(rng['start'])
            else None
        )
        to = to_ if js_truthy(to_) and _iso(to_) else min_iso(milestone['planned_end'], rng['end']) if milestone else rng['end'] if _iso(rng['end']) else None
        rp = _g(r, 'pct')
        pct = None if rp is None or rp is UNDEF or rp == '' else js_number(rp)
        uid_ = _g(r, 'user_id')
        u = users.get(uid_) if js_truthy(uid_) and isinstance(uid_, str) else None
        who = u['name'] if u else f'Row {i + 1}'
        st = {'error': None, 'field': None}

        def fail(f: str, msg: str, st: dict = st, i: int = i) -> None:
            if not js_truthy(st['error']):
                st['error'] = msg
                st['field'] = f'resources.{i}.{f}'

        role_lbl = _g(r, 'role_label')
        if not js_truthy(uid_) and isinstance(role_lbl, str) and role_lbl.strip():
            # V9-FIX (item 5): a role placeholder from a converted estimate keeps its role name until a person is chosen
            fail('user_id', f'Role: {role_lbl.strip()[:80]} — assign a person (row {i + 1}).')
        elif not js_truthy(uid_):
            fail('user_id', f'Row {i + 1}: choose the person.')
        elif not u or u.get('status') != 'active':
            fail('user_id', f'Row {i + 1}: choose an active person from your team.')
        # Feature Guide G3.10 / G6.01: person, milestone OR "All milestones", % and both dates. A row without a milestone
        # is an "All milestones" row: it spans the project (or its own dates), so a project can be costed before its WBS
        # exists (v8)
        _ = none
        if missing:
            fail('milestone_id', f'{js_str(who)}: that milestone no longer exists. Choose another milestone.')
        if pct is None or not is_finite(pct) or pct < 1 or pct > 100:
            fail('pct', f'{js_str(who)}: set an allocation between 1% and 100%.')
        if not js_truthy(frm):
            fail('from_on', f'{js_str(who)}: the From date is required.')
        if not js_truthy(to):
            fail('to_on', f'{js_str(who)}: the To date is required.')
        if js_truthy(frm) and js_truthy(to) and frm > to:
            fail('to_on', f'{js_str(who)}: From is after To.')
        if js_truthy(frm) and js_truthy(to) and _iso(rng['start']) and _iso(rng['end']) and (frm < rng['start'] or to > rng['end']):
            fail(
                'from_on' if frm < rng['start'] else 'to_on',
                f'{js_str(who)}: dates must sit inside the project ({fmt_date(rng["start"])} – {fmt_date(rng["end"])}).',
            )
        out.append(
            {
                'input': r,
                'key': coalesce(_g(r, 'key'), _g(r, 'id'), f'r{i}'),
                'milestone': milestone,
                'missingMilestone': missing,
                'from': frm,
                'to': to,
                'pct': js_round(pct) if pct is not None and is_finite(pct) else None,
                'error': st['error'],
                'field': st['field'],
            }
        )
    return out


def clean_additional(lst: Any) -> list[dict]:
    if not isinstance(lst, list):
        return []
    out = []
    for i, x in enumerate(lst):
        o = x if isinstance(x, dict) else {}
        oc = o.get('category', UNDEF)
        cat_ = js_str(oc) if any(c['value'] == oc for c in ADDITIONAL_COST_CATEGORIES) and isinstance(oc, str) else 'other'
        amt = js_round(js_number(coalesce(o.get('amount_paise', UNDEF), 0)))
        oid = o.get('id', UNDEF)
        out.append(
            {
                'id': oid if isinstance(oid, str) and oid else f'ac{i + 1}',
                'category': cat_,
                'description': _slice16(js_trim(js_str(coalesce(o.get('description', UNDEF), ''))), 200),
                'amount_paise': amt if is_finite(amt) and amt > 0 else 0,
            }
        )
    return out


def validate_additional(lst: Any) -> None:
    if not isinstance(lst, list):
        return
    for i, x in enumerate(lst):
        o = x if isinstance(x, dict) else {}
        amt = js_number(coalesce(o.get('amount_paise', UNDEF), 0))
        if not is_finite(amt) or amt < 0 or not is_integer(amt):
            err(422, 'MONEY_INVALID', f'Additional cost {i + 1}: the amount must be zero or more.', f'additional_costs.{i}.amount_paise')
        if amt > 0 and not js_trim(js_str(coalesce(o.get('description', UNDEF), ''))):
            err(422, 'VALIDATION', f'Additional cost {i + 1}: describe what it is for.', f'additional_costs.{i}.description')


def _compute_preview(ctx: Ctx, a: dict) -> dict[str, Any]:
    see_rates = can_see_salaries(ctx)
    see_money = see_rates or can_see_margins(ctx)
    resolved = _resolve_rows(ctx, a['range'], a['milestones'], a['resources'])
    res_total: Any = 0
    man_days: Any = 0
    people: dict[Any, None] = {}
    out_rows = []
    for r in resolved:
        days: Any = 0
        md: Any = 0
        cpd: Any = None
        cost: Any = 0
        hours: Any = 0
        cph: Any = None
        inp = r['input']
        uid_ = _g(inp, 'user_id')
        if js_truthy(uid_) and js_truthy(r['from']) and js_truthy(r['to']) and r['to'] >= r['from'] and js_truthy(r['pct']):
            c = _resource_row_cost(ctx, uid_, r['pct'], r['from'], r['to'])
            days = c['days']
            md = c['manDays']
            cpd = c['cpd']
            cph = c['cph']
            hours = c['hours']
            cost = c['cost']
            res_total += cost
            man_days += md
            people[uid_] = None
        no_rate = (
            f'{user_name(ctx, uid_)} has no cost rate yet, so this row costs nothing.'
            if js_truthy(uid_) and cpd is None and not js_truthy(r['error'])
            else None
        )
        mi = _g(inp, 'milestone_index')
        ms = r['milestone']
        out_rows.append(
            {
                'key': r['key'],
                'id': coalesce(_g(inp, 'id'), None),
                'user_id': coalesce(uid_, None),
                'milestone_id': coalesce(ms['id'] if ms else UNDEF, None if _is_num(mi) else coalesce(_g(inp, 'milestone_id'), None)),
                'milestone_index': ms['index'] if ms else mi if _is_num(mi) else None,
                'ms_code': ms['code'] if ms else '—',
                'pct': r['pct'],
                'from_on': r['from'],
                'to_on': r['to'],
                'working_days': days,
                'man_days': md,
                'hours': hours,
                'cost_per_hour': js_round(cph) if see_rates and cph is not None else None,
                'cost_per_day': cpd if see_rates else None,
                'cost': cost if see_rates else None,
                'error': coalesce(r['error'], no_rate),
                '_cost_raw': cost,
            }
        )
    add: Any = 0
    for x in a['additional']:
        add += x['amount_paise']
    maint = max(0, js_round(a['maintenance'])) if a['maintenance'] == a['maintenance'] else a['maintenance']
    cp = a['contingencyPct']
    cont_pct = min(100, max(0, cp)) if cp == cp else cp
    chain = cost_chain(res_total, add, maint, cont_pct)
    base, cont, total = chain['total'], chain['contingency'], chain['actual']  # total = the ACTUAL cost
    pm = price_and_markup(total, a['priceMode'], a['markupPct'], a['pricePaise'])
    price, markup = pm['price'], pm['markup']
    profit = price - total
    margin = profit / price if price > 0 else None
    floor = prop(ctx.tenant['settings'], 'projects.margin_floor_pct')
    totals = {
        'resources': res_total if see_money else None,
        'additional': add if see_money else None,
        'maintenance': maint if see_money else None,
        'contingency': cont if see_money else None,
        'total_cost': base if see_money else None,
        'actual_cost': total if see_money else None,
        'price': price if see_money else None,
        'profit': profit if see_money else None,
        'margin': margin if see_money else None,
        'markup': (profit / total if total > 0 else None) if see_money else None,
        'man_days': r1(man_days),
        'resource_rows': len(out_rows),
        'people': len(people),
    }
    totals['groups'] = cost_groups(res_total, a['additional'], maint, see_money)
    totals['people_share'] = _people_share(ctx, out_rows, res_total, see_rates, see_money)
    warnings: list[str] = []
    below = bool(see_money and margin is not None and total > 0 and margin < floor / 100)
    if below:
        warnings.append(f'Planned margin {to_fixed(margin * 100, 1)}% is below the {js_str(floor)}% floor.')
    return {
        'summary': calendar_summary(ctx, a['range']['start'], a['range']['end']),
        'milestones': a['milestones'],
        'rows': out_rows,
        'totals': totals,
        'additional_costs': a['additional'] if see_money else [],
        'maintenance_paise': maint if see_money else 0,
        'contingency_pct': cont_pct,
        'markup_pct': markup if see_money else None,
        'price_paise': price if see_money else None,
        'floor_pct': floor,
        'below_floor': below,
        'can_see_rates': see_rates,
        'can_see_money': see_money,
        'loading_pct': 0,
        'avg_working_days_per_month': r2(avg_working_days_per_month(ctx, a['range']['start'])) if see_rates and _iso(a['range']['start']) else None,
        'min_hours': min_hours(ctx),
        'saved_at': coalesce(a.get('saved_at', UNDEF), None),
        'saved_by': coalesce(a.get('saved_by', UNDEF), None),
        'warnings': warnings,
        '_resolved': resolved,
        '_internal': {'resources': res_total, 'total': total, 'price': price},
    }


def _people_share(ctx: Ctx, out_rows: list[dict], res_total: Any, see_rates: bool, see_money: bool) -> list[dict]:
    """Per allocated person: man-days, the resource cost their rows add and their % share of the resource cost
    (wizard step 3). The cost needs salary access; the share is shown with money access."""
    by: dict[Any, dict] = {}
    for r in out_rows:
        u = r['user_id']
        if not js_truthy(u):
            continue
        x = by.setdefault(u, {'user_id': u, 'name': user_name(ctx, u), 'rows': 0, 'pct': [], 'man_days': 0, 'hours': 0, 'cost': 0})
        x['rows'] += 1
        if r['pct'] is not None:
            x['pct'].append(r['pct'])
        x['man_days'] = x['man_days'] + r['man_days']
        x['hours'] = x['hours'] + r['hours']
        x['cost'] = x['cost'] + (r['cost'] or 0) if see_rates else 0
        x['_raw'] = x.get('_raw', 0) + (r.get('_cost_raw') or 0)
    out = []
    for x in by.values():
        raw = x.pop('_raw', 0)
        out.append(
            {
                **x,
                'man_days': r1(x['man_days']),
                'hours': r1(x['hours']),
                'cost': x['cost'] if see_rates else None,
                'share': (raw / res_total if res_total > 0 else 0) if see_money else None,
            }
        )
    return out


def _public_preview(p: dict) -> dict:
    return {
        **{k: v for k, v in p.items() if k not in ('_resolved', '_internal', 'rows')},
        'rows': [{k: v for k, v in r.items() if k != '_cost_raw'} for r in p['rows']],
    }


def _live_milestones_of(ctx: Ctx, pid: Any) -> list[dict]:
    return sorted((m for m in rows_where_ro(ctx, 'milestones', 'project_id', pid) if not js_truthy(m.get('deleted_at'))), key=lambda m: m['no'])


def _planned_milestones_of(ctx: Ctx, p: dict) -> list[dict]:
    return [
        {
            'index': i,
            'id': m['id'],
            'code': f'M{js_str(m["no"])}',
            'name': m['name'],
            'billing_amount_paise': m['billing_amount_paise'],
            'planned_start': m['planned_start'],
            'planned_end': m['planned_end'],
        }
        for i, m in enumerate(_live_milestones_of(ctx, p['id']))
    ]


def _args_from_body(b: dict) -> dict[str, Any]:
    mode = 'price' if b.get('price_mode', UNDEF) == 'price' else 'markup'
    mp = b.get('markup_pct', UNDEF)
    pp = b.get('price_paise', UNDEF)
    res = b.get('resources', UNDEF)
    return {
        'resources': res if isinstance(res, list) else [],
        'additional': clean_additional(b.get('additional_costs', UNDEF)),
        'maintenance': max(0, js_round(_num(b.get('maintenance_paise', UNDEF)))),
        'contingencyPct': min(100, max(0, _num(b.get('contingency_pct', UNDEF)))),
        'markupPct': None if mp is None or mp is UNDEF or mp == '' else _num(mp),
        'pricePaise': None if pp is None or pp is UNDEF or pp == '' else js_round(_num(pp)),
        'priceMode': mode,
    }


def saved_plan(ctx: Ctx, p: dict) -> dict[str, Any]:
    """The project's saved plan as an editable preview (allocations are the resource rows)."""

    def f() -> dict[str, Any]:
        c = coalesce(p.get('costing', UNDEF), None)
        allocs = rows_where(ctx, 'allocations', 'project_id', p['id'])
        ms = _planned_milestones_of(ctx, p)
        resources = [
            {
                'key': a['id'],
                'id': a['id'],
                'user_id': a['user_id'],
                'milestone_id': a['milestone_id'],
                'pct': a['pct'],
                'from_on': a['from_on'],
                'to_on': a['to_on'],
            }
            for a in allocs
        ]

        def ms_ix(x: dict) -> Any:
            if not js_truthy(x['milestone_id']):
                return -1
            m = next((m for m in ms if m['id'] == x['milestone_id']), None)
            return coalesce(m['index'] if m else UNDEF, 99)

        def cmp(x: dict, y: dict) -> Any:
            return _or_cmp(
                ms_ix(x) - ms_ix(y),
                locale_compare(user_name(ctx, x['user_id']), user_name(ctx, y['user_id'])),
                locale_compare(coalesce(x['from_on'], ''), coalesce(y['from_on'], '')),
            )

        resources = js_sort(resources, cmp)
        return _compute_preview(
            ctx,
            {
                'range': {'start': p['start_on'], 'end': p['end_on']},
                'milestones': ms,
                'resources': resources,
                'additional': coalesce(_g(c, 'additional_costs'), []),
                'maintenance': coalesce(_g(c, 'maintenance_paise'), 0),
                'contingencyPct': coalesce(_g(c, 'contingency_pct'), 0),
                'markupPct': _g(c, 'markup_pct') if js_truthy(c) else None,
                'pricePaise': _g(c, 'price_paise') if js_truthy(c) else p['contract_value_paise'],
                # v8: a plan never saved on a project without a contract value starts in markup mode at 0% (price =
                # actual cost), not at price 0 / markup −100%
                'priceMode': 'price' if js_truthy(c) or p['contract_value_paise'] > 0 else 'markup',
                'saved_at': coalesce(_g(c, 'saved_at'), None),
                'saved_by': coalesce(_g(c, 'saved_by'), None),
            },
        )

    return _cached(ctx, 'plan:' + js_str(p['id']), f)


# ---------------------------------------------------------------------------------------------------------------------
# Over-allocation across projects (per person per working day)
# ---------------------------------------------------------------------------------------------------------------------
def _over_allocation(ctx: Ctx, project_id: Any, resolved: list[dict]) -> dict[str, Any]:
    mx = prop(ctx.tenant['settings'], 'allocation.max_pct_per_person')
    mine = rows_where(ctx, 'allocations', 'project_id', project_id) if js_truthy(project_id) else []
    by_user: dict[Any, list[dict]] = {}
    for r in resolved:
        uid_ = _g(r['input'], 'user_id')
        if not js_truthy(r['error']) and js_truthy(uid_) and js_truthy(r['from']) and js_truthy(r['to']) and js_truthy(r['pct']):
            by_user.setdefault(uid_, []).append(r)
    out = []
    c = _calc(ctx)
    for u, lst in by_user.items():
        frm = lst[0]['from']
        to = lst[0]['to']
        for r in lst:
            frm = min_iso(frm, r['from'])
            to = max_iso(to, r['to'])
        mine_u = [a for a in mine if a['user_id'] == u]
        over: list[str] = []
        peak: Any = 0
        d = frm
        while d <= to:
            if is_working_day(d, c):
                held: Any = 0
                for a in mine_u:
                    if a['from_on'] <= d and a['to_on'] >= d:
                        held += a['pct']
                others = an.allocation_on(ctx, u, d) - held
                here: Any = 0
                for r in lst:
                    if r['from'] <= d and r['to'] >= d:
                        here += r['pct']
                if others + here > mx:
                    over.append(d)
                    peak = max(peak, others + here)
            d = add_days(d, 1)
        if over:
            out.append({'user_id': u, 'index': next(i for i, x in enumerate(resolved) if x is lst[0]), 'days': over, 'peak': peak})
    return {'max': mx, 'over': out}


def _days_word(n: int) -> str:
    return 'working day' if n == 1 else 'working days'


def _check_over_allocation(ctx: Ctx, project_id: Any, resolved: list[dict]) -> None:
    oa = _over_allocation(ctx, project_id, resolved)
    mx = oa['max']
    for o in oa['over']:
        days = o['days']
        rng = f'{fmt_date(days[0])}{f" – {fmt_date(days[-1])}" if len(days) > 1 else ""}'
        msg = f'{user_name(ctx, o["user_id"])} would be over {js_str(mx)}% on {len(days)} {_days_word(len(days))} ({rng}, peak {js_str(o["peak"])}%).'
        if js_truthy(ctx.tenant['settings'].get('allocation.block_over', UNDEF)):
            err(409, 'ALLOC_OVER', msg + ' Lower the percentage or shorten the dates.', f'resources.{o["index"]}.pct')
        warn(ctx, 'ALLOC_OVER', msg)


# ---------------------------------------------------------------------------------------------------------------------
# Validation used by create + save
# ---------------------------------------------------------------------------------------------------------------------
def _present(v: Any) -> bool:
    return v is not None and v is not UNDEF


def validate_costing(ctx: Ctx, project_id: Any, rng: dict, milestones: list[dict], body: dict) -> dict[str, Any]:
    """-> ValidCosting {resolved, priceInternal, totalInternal, costing}"""
    validate_additional(body.get('additional_costs', UNDEF))
    cpv = body.get('contingency_pct', UNDEF)
    cp = _num(cpv, float('nan'))
    if _present(cpv) and (not is_finite(cp) or cp < 0 or cp > 100):
        err(422, 'VALIDATION', 'Contingency must be between 0% and 100%.', 'costing.contingency_pct')
    mpv = body.get('maintenance_paise', UNDEF)
    mp = _num(mpv, float('nan'))
    if _present(mpv) and (not is_finite(mp) or mp < 0):
        err(422, 'MONEY_INVALID', 'Maintenance / support cost must be zero or more.', 'costing.maintenance_paise')
    mode = body.get('price_mode', UNDEF)
    pp = body.get('price_paise', UNDEF)
    if mode == 'price' and _present(pp) and _num(pp, -1) < 0:
        err(422, 'MONEY_INVALID', 'The project price must be zero or more.', 'costing.price_paise')
    mk = body.get('markup_pct', UNDEF)
    if mode != 'price' and _present(mk) and (_num(mk, float('nan')) < -100 or not is_finite(_num(mk, float('nan')))):
        err(422, 'VALIDATION', 'Markup must be a number above −100%.', 'costing.markup_pct')
    args = _args_from_body(body)
    pv = _compute_preview(ctx, {**args, 'range': rng, 'milestones': milestones})
    bad = next((r for r in pv['_resolved'] if js_truthy(r['error'])), None)
    if bad:
        err(422, 'ALLOC_INVALID', bad['error'], bad['field'] if bad['field'] is not None else UNDEF)
    _check_over_allocation(ctx, project_id, pv['_resolved'])
    total = pv['_internal']['total']
    price = pv['_internal']['price']
    return {
        'resolved': pv['_resolved'],
        'priceInternal': price,
        'totalInternal': total,
        'costing': {
            'additional_costs': [x for x in args['additional'] if x['amount_paise'] > 0 or js_truthy(x['description'])],
            'maintenance_paise': args['maintenance'],
            'contingency_pct': args['contingencyPct'],
            'markup_pct': r2(((price - total) / total) * 100) if total > 0 else coalesce(args['markupPct'], 0),
            'price_paise': price,
        },
    }


# ---------------------------------------------------------------------------------------------------------------------
# Analysis: planned / projected / actual
# ---------------------------------------------------------------------------------------------------------------------
def _col(revenue: Any, cost: Any) -> dict[str, Any]:
    return {'revenue': revenue, 'cost': cost, 'profit': revenue - cost, 'margin': margin_pct(revenue, cost), 'markup': markup_pct(revenue, cost)}


def approved_cr_value(ctx: Ctx, pid: Any) -> Any:
    s: Any = 0
    for c in rows(ctx, 'change_requests'):
        if c['project_id'] == pid and c['status'] in ('approved', 'done'):
            s += c['value_paise']
    return s


def pct_complete(tasks: list[dict], p: dict) -> Any:
    """% complete = earned hours ÷ total estimated hours (earned = estimate × progress)."""
    est: Any = 0
    for t in tasks:
        est += t['est_hours']
    if not js_truthy(est):
        return 1 if p['status'] in ('completed', 'closed') else 0
    s: Any = 0
    for t in tasks:
        s += (t['est_hours'] * progress_of(t)) / 100
    return s / est


def _project_forecast_end(ctx: Ctx, p: dict) -> Any:
    s = _schedule_for(ctx, p)
    return coalesce(s.get('forecast_finish', UNDEF) if s else UNDEF, None)


_APPROVED = an.APPROVED_WEEK_STATUSES  # V9-FIX (item 8): the one definition (approved or locked weeks)


def _sum(xs: Any) -> Any:
    s: Any = 0
    for x in xs:
        s += x
    return s


def _sweep_first(ctx: Ctx) -> None:
    an.ensure_swept(ctx)


def analyse(ctx: Ctx, p: dict) -> dict[str, Any]:
    """Feature Guide §6 — three margins:
    Planned   = contract value (original, before CRs) vs allocation cost (estimates × rate where a milestone has no
                allocation) + plan extras
    Projected = contract value incl. approved CRs vs actual so far (all logged hours × rate) + what's left + extras
    Actual    = invoiced amount vs approved hours only × cost per hour
    The result carries `_perPersonPlanned` (internal; publicAnalysis drops it)."""
    # V9-FIX (item 7): milestone deadlines follow their tasks BEFORE anything is computed — the same once-per-request
    # sweep as project_metrics, so a list read (Finance › Profit margins, Insights, Reports) never sees stale deadlines
    _sweep_first(ctx)

    def f() -> dict[str, Any]:
        pid = p['id']
        # shared across requests while every input table is unchanged — only for the stored row itself (a caller may
        # pass an edited copy, e.g. a preview); the value does not depend on the viewer (public_analysis redacts)
        if type(pid) is str and scope().find_by('projects', 'id', pid) is p:
            return scope().shared(('cost:an', pid, ctx.today), an.METRICS_TABLES, lambda: _analyse(ctx, p))
        return _analyse(ctx, p)

    return _cached(ctx, 'an:' + js_str(p['id']), f)


def _analyse(ctx: Ctx, p: dict) -> dict[str, Any]:
    today = ctx.today
    mh = min_hours(ctx)
    settings = ctx.tenant['settings']
    pid = p['id']
    tasks = rows_where(ctx, 'tasks', 'project_id', pid)
    task_by_id = {t['id']: t for t in tasks}
    ms = _live_milestones_of(ctx, pid)
    ms_by_id = {m['id']: m for m in ms}
    sched = _schedule_for(ctx, p)
    ms_forecast = {m['id']: m['forecast_end'] for m in (coalesce(sched.get('milestones', UNDEF), None) if sched else None) or []}
    forecast_end = coalesce(sched.get('forecast_finish', UNDEF) if sched else UNDEF, None)
    proj_end = coalesce(forecast_end, p['end_on'])
    allocs = rows_where(ctx, 'allocations', 'project_id', pid)
    costing = coalesce(p.get('costing', UNDEF), None)
    act = an.task_actuals(ctx)
    crs = approved_cr_value(ctx, pid)
    contract = p['contract_value_paise']
    planned_revenue = max(0, contract - crs)
    projected_revenue = contract
    pct = pct_complete(tasks, p)
    basis = 'costing' if js_truthy(costing) else 'allocations' if allocs else 'estimates'
    alert = coalesce(settings.get('projects.effort_ratio_alert', UNDEF), 1.2)
    add = _sum(x['amount_paise'] for x in (coalesce(costing.get('additional_costs', UNDEF), None) if costing else None) or [])
    maint = coalesce(costing.get('maintenance_paise', UNDEF) if costing else UNDEF, 0)
    cont_pct = coalesce(costing.get('contingency_pct', UNDEF) if costing else UNDEF, 0)
    closed = p['status'] in ('completed', 'closed')

    def rate_now(uid_: Any) -> Any:
        return an.current_rate_ph(ctx, uid_) if js_truthy(uid_) else 0

    # costedEntries(ctx).filter(kind task, a task id of this project, work_date <= today) — via the per-project index
    entries = [e for e in an.costed_task_entries_by_project(ctx).get(pid, ()) if e['work_date'] <= today]

    def ms_of_task(tid: Any) -> Any:
        t = task_by_id.get(tid)
        return coalesce(t.get('milestone_id', UNDEF) if t else UNDEF, None)

    tasks_by_ms: dict[Any, list[dict]] = {}
    for t in tasks:
        tasks_by_ms.setdefault(t.get('milestone_id'), []).append(t)

    # ---- milestone end dates (actual, forecast or deadline)
    ms_end: dict[Any, dict] = {}
    for m in ms:
        mine = tasks_by_ms.get(m['id'], [])
        all_done = len(mine) > 0 and all(t['status'] == 'done' for t in mine)
        if js_truthy(m.get('achieved_on')):
            ms_end[m['id']] = {'end': m['achieved_on'], 'kind': 'actual', 'done': True}
        elif all_done:
            e = '0000-01-01'
            for t in mine:
                e = max_iso(e, coalesce(t.get('done_on', UNDEF), t['due_on']))
            ms_end[m['id']] = {'end': e, 'kind': 'actual', 'done': True}
        elif js_truthy(ms_forecast.get(m['id'])):
            ms_end[m['id']] = {'end': ms_forecast[m['id']], 'kind': 'forecast', 'done': False}
        else:
            ms_end[m['id']] = {'end': None, 'kind': 'deadline', 'done': closed}

    def slip_of(m: dict) -> int:
        e = ms_end[m['id']]
        return slip_days(ctx, m['planned_end'], coalesce(e['end'], m['planned_end']))

    # ---- allocations: planned cost (full range), split at today, and what's left with the milestone's real end
    tomorrow = add_days(today, 1)
    grp: dict[Any, dict] = {}
    person: dict[Any, dict] = {}
    breach: Any = 0
    early: Any = 0
    alloc_rows: list[dict] = []
    for a in allocs:
        m = ms_by_id.get(a['milestone_id']) if js_truthy(a['milestone_id']) else None
        full = range_cost(ctx, a['user_id'], a['pct'], a['from_on'], a['to_on'])
        past = (
            range_cost(ctx, a['user_id'], a['pct'], a['from_on'], min_iso(a['to_on'], today)) if a['from_on'] <= today else {'days': 0, 'hours': 0, 'cost': 0}
        )
        future = full['cost'] - past['cost']
        eff_to = a['to_on']
        done = False
        if m:
            e = ms_end[m['id']]
            done = e['done']
            if e['kind'] == 'actual':
                eff_to = min_iso(a['to_on'], e['end'])
            else:
                eff_to = max_iso(coalesce(e['end'], m['planned_end']), m['planned_end']) if a['to_on'] >= m['planned_end'] else a['to_on']
        else:
            done = closed
            eff_to = max_iso(proj_end, p['end_on']) if a['to_on'] >= p['end_on'] else a['to_on']
        future_eff = 0 if done else range_cost(ctx, a['user_id'], a['pct'], max_iso(a['from_on'], tomorrow), eff_to)['cost'] if eff_to >= tomorrow else 0
        if future_eff > future:
            breach += future_eff - future
        else:
            early += future - future_eff
        k = m['id'] if js_truthy(a['milestone_id']) and m else '_all'
        x = grp.get(k)
        if x is None:
            x = grp[k] = {'past': 0, 'future': 0, 'futureEff': 0, 'plannedHours': 0, 'alloc': False}
        x['past'] += past['cost']
        x['future'] += future
        x['futureEff'] += future_eff
        x['plannedHours'] += full['hours']
        x['alloc'] = True
        pr = person.get(a['user_id'])
        if pr is None:
            pr = {'hours': 0, 'hoursToDate': 0, 'planned': 0}
        pr['hours'] += full['hours']
        pr['hoursToDate'] += past['hours']
        pr['planned'] += full['cost']
        person[a['user_id']] = pr
        alloc_rows.append(
            {
                'id': a['id'],
                'version': a['version'],
                'user_id': a['user_id'],
                'milestone_id': a['milestone_id'],
                'ms_code': f'M{js_str(m["no"])}' if m else '—' if js_truthy(a['milestone_id']) else 'All',
                'ms_name': m['name'] if m else 'Removed milestone' if js_truthy(a['milestone_id']) else 'All milestones',
                'pct': a['pct'],
                'from_on': a['from_on'],
                'to_on': a['to_on'],
                'working_days': full['days'],
                'hours': full['hours'],
                'cost': full['cost'],
            }
        )

    def ms_no(x: dict) -> Any:
        if not js_truthy(x['milestone_id']):
            return 0
        m = ms_by_id.get(x['milestone_id'])
        return coalesce(m['no'] if m else UNDEF, 99)

    alloc_rows = js_sort(
        alloc_rows,
        lambda x, y: _or_cmp(
            ms_no(x) - ms_no(y), locale_compare(x['from_on'], y['from_on']), locale_compare(user_name(ctx, x['user_id']), user_name(ctx, y['user_id']))
        ),
    )

    # ---- actual so far: every logged hour (approved or not) at its month's rate; actual: approved hours only
    spent: dict[Any, Any] = {}
    spent_hours: dict[Any, Any] = {}
    approved_hours: Any = 0
    approved_cost: Any = 0
    pending_hours: Any = 0
    pending_cost: Any = 0
    logged_hours: Any = 0
    logged_cost: Any = 0
    logged: dict[Any, dict] = {}
    for e in entries:
        k = coalesce(ms_of_task(e['task_id']), '_none')
        spent[k] = spent.get(k, 0) + e['cost']
        spent_hours[k] = spent_hours.get(k, 0) + e['hours']
        logged_hours += e['hours']
        logged_cost += e['cost']
        ok = e['week_status'] in _APPROVED
        if ok:
            approved_hours += e['hours']
            approved_cost += e['cost']
        else:
            pending_hours += e['hours']
            pending_cost += e['cost']
        lg = logged.get(e['user_id'])
        if lg is None:
            lg = {'hours': 0, 'approved': 0}
        lg['hours'] += e['hours']
        if ok:
            lg['approved'] += e['hours']
        logged[e['user_id']] = lg

    def act_hours(t: dict) -> Any:
        x = act.get(t['id'])
        return coalesce(x.get('hours', UNDEF) if x else UNDEF, 0)

    # ---- per milestone: planned vs forecast (= spent so far + what's left)
    def est_left_of(ts: list[dict]) -> Any:
        s: Any = 0
        for t in ts:
            s += 0 if (t['status'] == 'done' or closed) else max(0, t['est_hours'] - act_hours(t)) * rate_now(t.get('owner_id'))
        return s

    def est_cost_of(ts: list[dict]) -> Any:
        s: Any = 0
        for t in ts:
            s += t['est_hours'] * rate_now(t.get('owner_id'))
        return s

    res_plan: Any = 0
    estimate_plan: Any = 0
    forecast_total: Any = 0
    b_logged: Any = 0
    b_late: Any = 0
    b_extra: Any = 0
    b_est: Any = 0
    groups = [{'key': m['id'], 'm': m, 'tasks': tasks_by_ms.get(m['id'], [])} for m in ms]
    groups.append({'key': '_none', 'm': None, 'tasks': [t for t in tasks if not js_truthy(t.get('milestone_id')) or t['milestone_id'] not in ms_by_id]})
    ms_plan: dict[Any, Any] = {}
    ms_proj: dict[Any, Any] = {}
    ms_spent: dict[Any, Any] = {}
    ms_left: dict[Any, Any] = {}
    # v9 (D3): "All milestones" rows (the wizard / cost calculator allow them) cover every milestone that has no
    # allocation of its own — their cost is the plan there, never task estimates on top (the wizard priced it so)
    all_rows = grp.get('_all')
    covered = all_rows is not None and bool(all_rows.get('alloc'))
    covered_spent: Any = 0  # logged on milestones the All-milestones rows cover
    for gr in groups:
        x = grp.get(gr['key'])
        sp = spent.get(gr['key'], 0)
        done = ms_end[gr['m']['id']]['done'] if gr['m'] else (all(t['status'] == 'done' for t in gr['tasks']) or closed)
        if (x is None or not x['alloc']) and covered:
            # planned and forecast are the All-milestones rows' (added below); what was logged here is in their past
            ms_plan[gr['key']] = 0
            ms_proj[gr['key']] = js_round(sp)
            ms_spent[gr['key']] = js_round(sp)
            ms_left[gr['key']] = 0
            covered_spent += sp
            continue
        if x is not None and x['alloc']:
            plan = x['past'] + x['future']
            est_left = 0 if done else est_left_of(gr['tasks'])
            left = 0 if done else max(x['futureEff'], est_left)
            late_ms = slip_of(gr['m']) > 0 if gr['m'] else False
            d1 = x['past'] - sp  # allocated to date vs logged to date
            if late_ms and d1 < 0:
                b_late += d1
            else:
                b_logged += d1
            b_extra += x['futureEff'] - left  # ≤ 0: remaining work above the remaining allocation
            res_plan += plan
        else:
            plan = est_cost_of(gr['tasks'])
            left = 0 if done else est_left_of(gr['tasks'])
            b_est += plan - sp - left
            estimate_plan += plan
        fc = sp + left
        forecast_total += fc
        ms_plan[gr['key']] = js_round(plan)
        ms_proj[gr['key']] = js_round(fc)
        ms_spent[gr['key']] = js_round(sp)
        ms_left[gr['key']] = js_round(left)
    # "All milestones" rows: planned at full range, what's left follows the project's forecast end
    all_ = grp.get('_all')
    if all_ is not None:
        res_plan += all_['past'] + all_['future']
        # what was logged on the covered milestones so far + the rows' remaining allocation (like a milestone row)
        forecast_total += (covered_spent if covered else all_['past']) + all_['futureEff']
    res_plan = js_round(res_plan)
    estimate_plan = js_round(estimate_plan)
    planned_people = res_plan + estimate_plan
    # the costing chain: contingency on the whole total cost (people + additional + maintenance)
    cont = js_round(((planned_people + add + maint) * cont_pct) / 100)
    cont_held = 0 if closed else cont
    planned_cost = planned_people + add + maint + cont  # the planned ACTUAL cost
    projected_cost = js_round(forecast_total) + add + maint + cont_held

    planned = _col(planned_revenue, planned_cost)
    projected = _col(projected_revenue, projected_cost)
    invoices = [i for i in rows_where_ro(ctx, 'invoices', 'project_id', pid) if i['status'] != 'draft' and i['status'] != 'cancelled']
    invoiced = _sum(i['amount_paise'] for i in invoices)
    actual = _col(invoiced, js_round(approved_cost))

    # ---- what moved the margin (planned profit → projected profit; the lines add up exactly)
    bridge: list[dict] = [{'key': 'planned', 'label': 'Planned profit', 'amount': planned['profit']}]
    if js_truthy(crs):
        bridge.append({'key': 'crs', 'label': 'Approved change requests', 'amount': crs})
    lines = [
        {
            'key': 'logged',
            'label': 'Hours logged below allocation to date' if b_logged >= 0 else 'Extra hours logged above allocation to date',
            'amount': js_round(b_logged),
        },
        {'key': 'late_hours', 'label': 'Extra hours on late milestones', 'amount': js_round(b_late)},
        {'key': 'breach', 'label': 'Breach cost: people held on late milestones', 'amount': -js_round(breach)},
        {'key': 'early', 'label': 'Early finish saving: milestones ahead of deadline', 'amount': js_round(early)},
        {'key': 'extra', 'label': 'Extra hours: remaining work above the allocation', 'amount': js_round(b_extra)},
        {'key': 'estimates', 'label': 'Effort vs estimate on milestones without an allocation', 'amount': js_round(b_est)},
    ]
    for ln in lines:
        ln['amount'] = 0 if abs(ln['amount']) < 100 else ln['amount']  # under a rupee is noise (and no −0)
        if ln['amount'] != 0 or ln['key'] in ('breach', 'early'):
            bridge.append(ln)
    if cont != cont_held:
        bridge.append({'key': 'contingency', 'label': 'Contingency released at close', 'amount': cont - cont_held})
    # rounding: make the bridge add up to the projected profit to the rupee
    total_b = _sum(b['amount'] for b in bridge)
    diff = projected['profit'] - total_b
    if diff != 0:
        tgt = next((b for b in bridge if b['key'] == 'logged'), None) or next(b for b in bridge if b['key'] == 'early')
        tgt['amount'] += diff
    bridge.append({'key': 'projected', 'label': 'Projected profit', 'amount': projected['profit']})

    # ---- effort ratio = actual hours ÷ estimated hours (per task, milestone, project)
    def ratio_of(ts: list[dict]) -> Any:
        st = [t for t in ts if t['status'] == 'done' and t['est_hours'] > 0 and act_hours(t) > 0]
        e = _sum(t['est_hours'] for t in st)
        a_ = _sum(act_hours(t) for t in st)
        return a_ / e if e > 0 and a_ > 0 else None

    milestones = []
    for m in ms:
        e = ms_end[m['id']]
        mine = tasks_by_ms.get(m['id'], [])
        plan = ms_plan.get(m['id'], 0)
        proj = ms_proj.get(m['id'], 0)
        g = grp.get(m['id'])
        milestones.append(
            {
                'id': m['id'],
                'code': f'M{js_str(m["no"])}',
                'name': m['name'],
                'done': e['done'],
                'deadline': m['planned_end'],
                'end': coalesce(e['end'], m['planned_end']),
                'end_kind': e['kind'] if js_truthy(e['end']) else 'deadline',
                'slip_days': slip_of(m),
                'planned_cost': plan,
                'projected_cost': proj,
                'change': proj - plan,
                'est_hours': _sum(t['est_hours'] for t in mine),
                'actual_hours': r2(_sum(act_hours(t) for t in mine)),
                'effort_ratio': ratio_of(mine),
                'planned_hours': r1(g['plannedHours'] if g is not None else 0),
                'logged_hours': r2(spent_hours.get(m['id'], 0)),
                'allocated': bool(g is not None and g['alloc']),
                'spent_to_date': ms_spent.get(m['id'], 0),
                'left': ms_left.get(m['id'], 0),
            }
        )

    # ---- resources: allocated vs logged, in hours
    res_ids = list(dict.fromkeys([*person.keys(), *logged.keys()]))
    resources = []
    for uid_ in res_ids:
        pr = person.get(uid_)
        lg = logged.get(uid_)
        lh = lg['hours'] if lg is not None else 0
        resources.append(
            {
                'user_id': uid_,
                'allocated_man_days': r1((pr['hours'] if pr else 0) / mh),
                'allocated_to_date': r1((pr['hoursToDate'] if pr else 0) / mh),
                'logged_hours': r2(lh),
                'logged_days': r1(lh / mh),
                'used': lh / pr['hoursToDate'] if pr and pr['hoursToDate'] > 0 else None,
                'unplanned': not pr and lh > 0,
                'planned_cost': js_round(pr['planned']) if pr else 0,
                'allocated_hours': r1(pr['hours'] if pr else 0),
                'allocated_hours_to_date': r1(pr['hoursToDate'] if pr else 0),
                'logged_hours_to_date': r2(lh),
                'approved_hours': r2(lg['approved'] if lg is not None else 0),
            }
        )
    resources = js_sort(
        resources,
        lambda a, b: _or_cmp(
            int(a['unplanned']) - int(b['unplanned']),
            coalesce(b['allocated_hours'], 0) - coalesce(a['allocated_hours'], 0),
            locale_compare(user_name(ctx, a['user_id']), user_name(ctx, b['user_id'])),
        ),
    )

    # ---- effort ratio per task (every task with an estimate and logged hours)
    ms_all = _ms_map_all(ctx)
    eff_tasks = []
    for t in tasks:
        ah = act_hours(t)
        if not (t['est_hours'] > 0 and ah > 0):
            continue
        m = ms_by_id.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
        eff_tasks.append(
            {
                'id': t['id'],
                'wbs': an.wbs_code(t, ms_all),
                'title': t['title'],
                'owner_id': t['owner_id'],
                'est_hours': t['est_hours'],
                'actual_hours': r2(ah),
                'earned_hours': r2((t['est_hours'] * progress_of(t)) / 100),
                'ratio': ah / t['est_hours'],
                'milestone_code': f'M{js_str(m["no"])}' if m else None,
                'done': t['status'] == 'done',
                'progress': progress_of(t),
            }
        )
    eff_tasks = js_sort(eff_tasks, lambda a, b: _or_cmp(coalesce(b['ratio'], 0) - coalesce(a['ratio'], 0), compare_numeric(a['wbs'], b['wbs'])))
    flagged = [t for t in eff_tasks if coalesce(t['ratio'], 0) > alert]

    type_label = js_str(PROJECT_TYPE.get(p['type'], UNDEF))
    model_label = js_str(BILLING_MODEL.get(p['billing_model'], UNDEF))
    description = (
        f'{model_label} {type_label} project. Planned is the contract value against the allocation plan'
        f'{" (task estimates where a milestone has no allocation)" if js_truthy(estimate_plan) else ""}'
        f'{" and the saved costing extras" if js_truthy(costing) else ""}'
        '; projected is the contract incl. approved CRs against cost so far plus what’s left; actual is the invoiced amount against approved hours only.'
    )

    # ---- cost per hour (guide formula) with this month's numbers
    ym = month_of(today)
    wdm = wd_month(ctx, ym)
    team = list(dict.fromkeys([*(a['user_id'] for a in allocs), *logged.keys()]))
    people = []
    for uid_ in team:
        r = _rate_for(ctx, uid_, today)
        if r:
            people.append({'user_id': uid_, 'monthly_ctc': r['monthly_ctc_paise'], 'cost_per_hour': js_round(cost_per_hour(r['monthly_ctc_paise'], an.person_wdm(ctx, uid_, ym), mh))})
    rate = {
        'min_hours': mh,
        'month': ym,
        'working_days': wdm,
        'example': {'ctc': 6_000_000, 'working_days': 22, 'min_hours': 9, 'per_hour': js_round(cost_per_hour(6_000_000, 22, 9))},
        'people': people,
    }

    nb = [x for x in ms if x['billing_status'] == 'none' and x['billing_amount_paise'] > 0]
    nb = sorted(nb, key=lambda x: _lk(x['planned_end']))
    next_billing = (
        {'code': f'M{js_str(nb[0]["no"])}', 'name': nb[0]['name'], 'amount': nb[0]['billing_amount_paise'], 'due': nb[0]['planned_end']} if nb else None
    )

    return {
        'basis': basis,
        'description': description,
        'pct_complete': pct,
        'approved_crs': crs,
        'planned': planned,
        'projected': projected,
        'actual': actual,
        'bridge': bridge,
        'contingency_held': cont_held,
        'profit_if_released': projected['profit'] + cont_held,
        'forecast_end': forecast_end,
        'milestones': milestones,
        'resources': resources,
        'effort': {
            'alert': alert,
            'project': ratio_of(tasks),
            'flagged': len(flagged),
            'tasks': eff_tasks,
            'est_hours': _sum(t['est_hours'] for t in tasks),
            'actual_hours': r2(_sum(act_hours(t) for t in tasks)),
        },
        'floor_pct': settings['projects.margin_floor_pct'],
        'planned_basis': {
            'allocation_cost': res_plan,
            'estimate_cost': estimate_plan,
            'additional': add,
            'maintenance': maint,
            'contingency': cont,
            'total_cost': planned_people + add + maint,
            'actual_cost': planned_cost,
        },
        'projected_basis': {
            'cost_so_far': js_round(logged_cost),
            'hours_so_far': r2(logged_hours),
            'whats_left': projected_cost - js_round(logged_cost) - add - maint - cont_held,
        },
        'actual_basis': {
            'invoiced': invoiced,
            'invoices': len(invoices),
            'contract': contract,
            'approved_hours': r2(approved_hours),
            'approved_cost': js_round(approved_cost),
            'pending_hours': r2(pending_hours),
            'pending_cost': js_round(pending_cost),
            'earned_value': js_round(projected_revenue * pct),
            'next_billing': next_billing,
            'additional_excluded': add + maint,
        },
        'allocations': alloc_rows,
        'rate': rate,
        '_perPersonPlanned': {k: v['planned'] for k, v in person.items()},
    }


def public_analysis(ctx: Ctx, a: dict) -> dict[str, Any]:
    """Per-person money is salary-derived: only for cost_rates.view."""
    rest = {k: v for k, v in a.items() if k != '_perPersonPlanned'}
    if can_see_salaries(ctx):
        return rest
    rate = rest.get('rate', UNDEF)
    return {
        **rest,
        'resources': [{**r, 'planned_cost': None} for r in rest['resources']],
        'allocations': [{**x, 'cost': None} for x in (coalesce(rest.get('allocations', UNDEF), None) or [])],
        'rate': {**rate, 'people': []} if js_truthy(rate) else rate,
    }


# ---------------- metrics override: list / dashboard margins agree with the Costing & margin tab ----------------
def _metrics_override(ctx: Ctx, p: dict, base: dict) -> dict | None:
    guard = scope().memo.setdefault(('mock/costing', 'inOverride'), set())
    if p['id'] in guard:
        return None
    guard.add(p['id'])
    try:
        a = analyse(ctx, p)
        contract = p['contract_value_paise']
        projected_margin = a['projected']['margin'] if js_truthy(contract) else None
        er = a['effort']['project']
        settings = ctx.tenant['settings']
        if p['status'] in ('completed', 'closed'):
            health = 'on_track'
        else:
            health = project_health(
                projected_margin, er, settings['projects.margin_floor_pct'] / 100, settings.get('projects.effort_ratio_alert'), base.get('overdue_milestones')
            )
        spi = base.get('spi')
        adj = 'at_risk' if health == 'on_track' and spi is not None and spi < 0.8 and p['status'] == 'active' else health
        ab = a.get('actual_basis')
        return {
            'planned_cost': a['planned']['cost'],
            'projected_cost': a['projected']['cost'],
            'actual_cost': a['actual']['cost'],
            'planned_margin': a['planned']['margin'] if js_truthy(contract) else None,
            'projected_margin': projected_margin,
            'actual_margin': a['actual']['margin'],
            'planned_markup': a['planned']['markup'],
            'projected_markup': a['projected']['markup'],
            'actual_markup': a['actual']['markup'],
            'approved_hours': coalesce(ab.get('approved_hours', UNDEF) if ab else UNDEF, base.get('approved_hours', UNDEF)),
            'effort_ratio': er,
            'health': adj,
        }
    finally:
        guard.discard(p['id'])


an.set_project_metrics_override(_metrics_override)


# ---------------------------------------------------------------------------------------------------------------------
# List row extras, expand summary, portfolio
# ---------------------------------------------------------------------------------------------------------------------
def project_row_info(ctx: Ctx, p: dict) -> dict[str, Any]:
    tasks = rows_where(ctx, 'tasks', 'project_id', p['id'])
    f_end = _project_forecast_end(ctx, p)
    see_contract = has_any(ctx, 'finance.view', 'margins.view', 'projects.edit')
    return {
        'pct_complete': pct_complete(tasks, p),
        'tasks_total': len(tasks),
        'tasks_done': len([t for t in tasks if t['status'] == 'done']),
        'forecast_end': f_end,
        'slip_days': slip_days(ctx, p['end_on'], f_end) if js_truthy(f_end) else None,
        'contract_incl_crs': p['contract_value_paise'] if see_contract else None,
        'projected_margin': analyse(ctx, p)['projected']['margin'] if can_see_margins(ctx) and js_truthy(p['contract_value_paise']) else None,
    }


def project_summary(ctx: Ctx, p: dict) -> dict[str, Any]:
    tasks = rows_where(ctx, 'tasks', 'project_id', p['id'])
    ms = _live_milestones_of(ctx, p['id'])
    ms_by_id = {m['id']: m for m in ms}
    sched = _schedule_for(ctx, p)
    fc = {m['id']: m for m in ((sched.get('milestones') if sched else None) or [])}
    see_money = has_any(ctx, 'finance.view', 'margins.view', 'projects.edit')
    open_ = sorted((t for t in tasks if t['status'] != 'done'), key=lambda t: _lk(t['due_on']))
    risks = [
        t
        for t in ((sched.get('tasks') if sched else None) or [])
        if js_truthy(t.get('breach')) and (t['critical'] or t['breach']['kind'] in ('overdue', 'blocked'))
    ]
    risks = sorted(risks, key=lambda t: -int(bool(t['critical'])))[:5]
    out_ms = []
    for m in ms:
        s = fc.get(m['id'])
        s_fe = s.get('forecast_end') if s else None
        end = coalesce(m.get('achieved_on'), s_fe, None)
        out_ms.append(
            {
                'id': m['id'],
                'code': f'M{js_str(m["no"])}',
                'name': m['name'],
                'deadline': m['planned_end'],
                'end': end,
                'end_kind': 'actual' if js_truthy(m.get('achieved_on')) else 'forecast' if js_truthy(s_fe) else 'deadline',
                'state': s['state'] if s and not nullish(s.get('state')) else an.milestone_state(ctx, m, tasks)['state'],
                'billing_amount_paise': m['billing_amount_paise'] if see_money else None,
                'billing_status': m['billing_status'],
            }
        )
    team = [t.get('owner_id') for t in tasks] + [a['user_id'] for a in rows(ctx, 'allocations') if a['project_id'] == p['id']]
    return {
        'project_id': p['id'],
        'pm_id': p['pm_id'],
        'start_on': p['start_on'],
        'end_on': p['end_on'],
        'forecast_end': coalesce(sched.get('forecast_finish', UNDEF) if sched else UNDEF, None),
        'milestones': out_ms,
        'open_tasks': [
            {
                'id': t['id'],
                'wbs': an.wbs_code(t, ms_by_id),
                'title': t['title'],
                'owner_id': t['owner_id'],
                'due_on': t['due_on'],
                'overdue': t['due_on'] < ctx.today,
            }
            for t in open_[:5]
        ],
        'open_count': len(open_),
        'risks': [{'id': t['id'], 'wbs': t['wbs'], 'title': t['title'], 'message': t['breach']['message'], 'critical': t['critical']} for t in risks],
        'team_ids': list(dict.fromkeys(x for x in team if js_truthy(x))),
    }


def portfolio(ctx: Ctx, visible: list[dict], scope_: str) -> dict[str, Any]:
    def in_scope(p: dict) -> bool:
        if p['id'].startswith('p_general') or p['code'] == 'GEN':
            return False
        if scope_ == 'all':
            return True
        return p['status'] == 'active' if scope_ == 'active' else p['status'] not in ('completed', 'closed')

    in_sc = [p for p in visible if in_scope(p)]
    lst = []
    for p in in_sc:
        a = analyse(ctx, p)
        pm, jm = a['planned']['margin'], a['projected']['margin']
        ab = a.get('actual_basis')
        lst.append(
            {
                'project_id': p['id'],
                'code': p['code'],
                'name': p['name'],
                'client': p.get('client', UNDEF),
                'status': p['status'],
                'type': p['type'],
                'billing_model': p['billing_model'],
                'pct_complete': a['pct_complete'],
                'contract': p['contract_value_paise'],
                'planned_cost': a['planned']['cost'],
                'planned_margin': pm,
                'projected_margin': jm,
                'margin_delta_pts': r1((jm - pm) * 100) if jm is not None and pm is not None else None,
                'actual_margin': a['actual']['margin'],
                'projected_profit': a['projected']['profit'],
                'cost_to_date': a['actual']['cost'],
                'projected_cost': a['projected']['cost'],
                'earned': coalesce(ab.get('earned_value', UNDEF) if ab else UNDEF, 0),
                'basis': a['basis'],
                'invoiced': a['actual']['revenue'],
                'effort_ratio': a['effort']['project'],
            }
        )
    lst = js_sort(lst, lambda x, y: _or_cmp(y['contract'] - x['contract'], locale_compare(x['name'], y['name'])))
    paid = [r for r in lst if r['contract'] > 0]
    contract = _sum(r['contract'] for r in paid)
    profit = _sum(r['projected_profit'] for r in paid)
    earned = _sum(r['earned'] for r in paid)
    invoiced = _sum(coalesce(r['invoiced'], 0) for r in paid)
    ctd = _sum(r['cost_to_date'] for r in paid)
    pcost = _sum(r['projected_cost'] for r in paid)
    planned_cost = _sum(r['planned_cost'] for r in paid)
    planned_rev = _sum(js_div(r['planned_cost'], 1 - r['planned_margin']) if r['planned_margin'] is not None else 0 for r in paid)
    # milestone cash (G2.07)
    ids = {p['id'] for p in in_sc}
    ms = [m for m in rows(ctx, 'milestones') if m['project_id'] in ids and not js_truthy(m.get('deleted_at'))]
    tasks = [t for t in rows(ctx, 'tasks') if t['project_id'] in ids]
    achieved_not_invoiced = _sum(
        m['billing_amount_paise'] for m in ms if m['billing_status'] == 'none' and an.milestone_state(ctx, m, tasks)['state'].startswith('achieved')
    )
    awaiting = _sum(max(0, i['amount_paise'] - i['paid_amount_paise']) for i in rows(ctx, 'invoices') if i['project_id'] in ids)
    in30 = add_days(ctx.today, 30)
    next30 = _sum(
        m['billing_amount_paise']
        for m in ms
        if m['billing_status'] == 'none' and not js_truthy(m.get('achieved_on')) and m['planned_end'] >= ctx.today and m['planned_end'] <= in30
    )
    return {
        'scope': scope_,
        'floor_pct': prop(ctx.tenant['settings'], 'projects.margin_floor_pct'),
        'kpis': {
            'contract_value': contract,
            'projects': len(paid),
            'projected_profit': profit,
            'projected_margin': profit / contract if js_truthy(contract) else None,
            # actual = invoiced amount against approved hours only (same as each project's Actual column)
            'actual_margin': (invoiced - ctd) / invoiced if js_truthy(invoiced) else None,
            'earned_to_date': earned,
            'cost_to_date': ctd,
            'planned_margin': (planned_rev - planned_cost) / planned_rev if planned_rev > 0 else None,
            'invoiced': invoiced,
            'projected_cost': pcost,
        },
        'cash': {'achieved_not_invoiced': achieved_not_invoiced, 'invoiced_awaiting_payment': awaiting, 'billable_next_30': next30},
        'rows': lst,
    }


def cost_rate_days(ctx: Ctx) -> dict[str, Any]:
    settings = ctx.tenant['settings']
    fsm = js_or(settings.get('company.fy_start_month', UNDEF), 4)
    y = fy_start_year(ctx.today, fsm)
    r = fy_range(y, fsm)
    fy_wd = wd(ctx, r['from'], r['to'])
    mh = min_hours(ctx)
    ym = month_of(ctx.today)
    wdm = wd_month(ctx, ym)
    rates = rows(ctx, 'cost_rates')
    people = []
    for u in rows(ctx, 'users'):
        if js_truthy(u.get('is_super_admin')):
            continue
        rate = rate_on(rates, u['id'], ctx.today)
        ph = cost_per_hour(rate['monthly_ctc_paise'], an.person_wdm(ctx, u['id'], ym), mh) if rate else None  # V9-FIX: their calendar
        people.append(
            {
                'user_id': u['id'],
                'monthly_ctc_paise': coalesce(rate.get('monthly_ctc_paise', UNDEF) if rate else UNDEF, None),
                'cost_per_day_paise': None if ph is None else js_round(ph * mh),
                'cost_per_hour_paise': None if ph is None else js_round(ph),
            }
        )
    return {
        'loading_pct': 0,
        'fy_label': fy_label(y),
        'fy_working_days': fy_wd,
        'avg_working_days_per_month': r2(fy_wd / 12),
        'people': people,
        'min_hours': mh,
        'month': ym,
        'month_working_days': wdm,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------------------------------------------------
def insert_plan_allocations(ctx: Ctx, project_id: str, v: dict, ms_ids: list) -> None:
    """Insert allocations for a new project (create form). Milestone ids come from the created milestones."""
    for r in v['resolved']:
        mi = _g(r['input'], 'milestone_index')
        ms_id = coalesce(_ms_at(ms_ids, mi), None) if _is_num(mi) else None
        insert(
            ctx,
            'allocations',
            {'project_id': project_id, 'milestone_id': ms_id, 'user_id': _g(r['input'], 'user_id'), 'pct': r['pct'], 'from_on': r['from'], 'to_on': r['to']},
        )


def _replace_allocations(ctx: Ctx, p: dict, v: dict) -> dict[str, int]:
    """Replace a project's allocations with the editor rows, keeping ids where rows came from an allocation."""
    current = rows_where(ctx, 'allocations', 'project_id', p['id'])
    keep: set[Any] = set()
    added = changed = 0
    s = scope()
    for r in v['resolved']:
        ms = r['milestone']
        data = {
            'project_id': p['id'],
            'milestone_id': coalesce(ms['id'] if ms else UNDEF, None),
            'user_id': _g(r['input'], 'user_id'),
            'pct': r['pct'],
            'from_on': r['from'],
            'to_on': r['to'],
        }
        rid = _g(r['input'], 'id')
        existing = next((a for a in current if a['id'] == rid), None) if js_truthy(rid) else None
        if existing is not None:
            keep.add(existing['id'])
            cur = s.find_by('allocations', 'id', existing['id']) or existing  # the TS object sees earlier updates
            if (
                cur['milestone_id'] != data['milestone_id']
                or cur['user_id'] != data['user_id']
                or cur['pct'] != data['pct']
                or cur['from_on'] != data['from_on']
                or cur['to_on'] != data['to_on']
            ):
                update(ctx, 'allocations', existing['id'], data)
                changed += 1
        else:
            insert(ctx, 'allocations', data)
            added += 1
    removed = [a for a in current if a['id'] not in keep]
    for a in removed:
        remove(ctx, 'allocations', a['id'])
    return {'added': added, 'changed': changed, 'removed': len(removed)}


# ---------------------------------------------------------------------------------------------------------------------
# Routes (registered from routes/projects.py before /projects/:id)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class CostingDeps:
    can_view: Callable[[Ctx, dict], bool]
    can_edit: Callable[[Ctx, dict], bool]


def can_plan_cost(ctx: Ctx) -> bool:
    return has_any(ctx, 'margins.view', 'cost_rates.view')


def register_costing_routes(r: Any, deps: CostingDeps) -> None:
    def get_project(ctx: Ctx, id_: Any) -> dict:
        p = find(ctx, 'projects', id_, 'Project')
        if not deps.can_view(ctx, p):
            err(403, 'FORBIDDEN', 'You don’t have access to this project.')
        return p

    def next_code(req: Any) -> Any:
        ctx = req.ctx
        if not has(ctx, 'projects.create'):
            err(403, 'FORBIDDEN', 'Only admins and project managers can create projects.')
        prefix = js_str(js_or(ctx.tenant['settings'].get('projects.code_prefix', UNDEF), 'PRJ'))
        seq = next((x for x in get_db().number_sequences if x.get('tenant_id') == ctx.tenant_id and x.get('scope') == 'project'), None)
        taken = {p.get('code') for p in rows(ctx, 'projects')}
        n = coalesce(seq.get('next_value', UNDEF) if seq else UNDEF, 1)
        while f'{prefix}-{pad_start(js_str(n), 3, "0")}' in taken:
            n += 1
        return {'code': f'{prefix}-{pad_start(js_str(n), 3, "0")}'}

    def cal_summary(req: Any) -> Any:
        ctx = req.ctx
        s = calendar_summary(ctx, js_str(coalesce(req.q('from'), '')), js_str(coalesce(req.q('to'), '')))
        if not s:
            err(422, 'DATE_RANGE', 'Pick a start date and an end date on or after it.', 'to')
        return s

    def preview(req: Any) -> Any:
        """Live cost calculator for the create form and the project's planned costing editor."""
        ctx = req.ctx
        if not has_any(ctx, 'projects.create', 'projects.edit', 'projects.manage_all'):
            err(403, 'FORBIDDEN', 'Only project editors can use the cost calculator.')
        b = req.body if isinstance(req.body, dict) else {}
        pid = b.get('project_id', UNDEF)
        if isinstance(pid, str) and pid:
            p = get_project(ctx, pid)
            rng = {'start': p['start_on'], 'end': p['end_on']}
            milestones = _planned_milestones_of(ctx, p)
        else:
            rng = {'start': js_str(coalesce(b.get('start_on', UNDEF), '')), 'end': js_str(coalesce(b.get('end_on', UNDEF), ''))}
            raw = b.get('milestones', UNDEF)
            lst = []
            if isinstance(raw, list):
                for m in raw:
                    if js_trim(js_str(coalesce(_g(m, 'name'), ''))):
                        lst.append({'name': js_trim(js_str(m['name'])), 'billing_amount_paise': max(0, js_round(_num(m.get('billing_amount_paise', UNDEF))))})
            milestones = spread_milestones(ctx, rng['start'], rng['end'], lst)
        pv = _compute_preview(ctx, {**_args_from_body(b), 'range': rng, 'milestones': milestones})
        if pv['can_see_money']:
            oa = _over_allocation(ctx, pid if isinstance(pid, str) else None, pv['_resolved'])
            for o in oa['over']:
                n = len(o['days'])
                pv['warnings'].append(f'{user_name(ctx, o["user_id"])} would be over {js_str(oa["max"])}% on {n} {_days_word(n)} (peak {js_str(o["peak"])}%).')
        return _public_preview(pv)

    def get_costing(req: Any) -> Any:
        ctx = req.ctx
        p = get_project(ctx, req.params['id'])
        if not can_plan_cost(ctx):
            err(403, 'FORBIDDEN', 'Costing is visible to people with the margins or salaries permission.')
        return {'can_edit': deps.can_edit(ctx, p), 'preview': _public_preview(saved_plan(ctx, p))}

    def put_costing(req: Any) -> Any:
        ctx = req.ctx
        p = get_project(ctx, req.params['id'])
        if not deps.can_edit(ctx, p):
            err(403, 'FORBIDDEN', 'Only the project manager of this project (or an admin) can change its costing.')
        if not can_plan_cost(ctx):
            err(403, 'FORBIDDEN', 'Costing needs the margins or salaries permission.')
        b = req.body if isinstance(req.body, dict) else {}
        v = validate_costing(ctx, p['id'], {'start': p['start_on'], 'end': p['end_on']}, _planned_milestones_of(ctx, p), b)
        res = _replace_allocations(ctx, p, v)
        costing = {**v['costing'], 'saved_at': now_iso(), 'saved_by': ctx.user.get('id')}
        out = update(ctx, 'projects', p['id'], {'costing': costing})
        floor = prop(ctx.tenant['settings'], 'projects.margin_floor_pct')
        m = margin_pct(v['priceInternal'], v['totalInternal']) if v['totalInternal'] > 0 else None
        if m is not None and m < floor / 100:
            warn(ctx, 'MARGIN_BELOW_FLOOR', f'Planned margin is {to_fixed(m * 100, 1)}%, below the {js_str(floor)}% floor.')
        cv = out['contract_value_paise']
        if js_truthy(cv) and abs(cv - approved_cr_value(ctx, p['id']) - v['priceInternal']) > 100:
            warn(
                ctx,
                'PRICE_NOT_CONTRACT',
                f'The planned price {fmt_money(v["priceInternal"])} differs from the contract ({fmt_money(cv)}). Revenue uses the contract value.',
            )
        n = len(v['resolved'])
        log_activity(
            ctx,
            'project',
            p['id'],
            'costing.saved',
            f'Saved planned costing: {n} resource row{"" if n == 1 else "s"} ({res["added"]} added, {res["changed"]} changed, {res["removed"]} removed), price {fmt_money(v["priceInternal"])}',
        )
        _memo_delete(ctx)
        return _public_preview(saved_plan(ctx, out))

    def summary(req: Any) -> Any:
        return project_summary(req.ctx, get_project(req.ctx, req.params['id']))

    def margins(req: Any) -> Any:
        ctx = req.ctx
        if not has(ctx, 'margins.view'):
            err(403, 'FORBIDDEN', 'Profit margins are visible to the CEO and people with the margins permission.')
        q = req.q('scope')
        scope_ = q if isinstance(q, str) and q in ('active', 'open', 'all') else 'active'
        return portfolio(ctx, [p for p in rows(ctx, 'projects') if deps.can_view(ctx, p)], scope_)

    def rates(req: Any) -> Any:
        ctx = req.ctx
        if not has(ctx, 'cost_rates.view'):
            err(403, 'FORBIDDEN', 'Cost per day is visible to people with the salaries permission.')
        return cost_rate_days(ctx)

    r.get('/projects/next-code', next_code)
    r.get('/projects/calendar-summary', cal_summary)
    r.post('/projects/costing/preview', preview)
    r.get('/projects/:id/costing', get_costing)
    r.put('/projects/:id/costing', put_costing)
    r.get('/projects/:id/summary', summary)
    r.get('/finance/margins', margins)
    r.get('/costing/rates', rates)


def register(r: Any) -> None:
    """registerCostingRoutes(r, { canView, canEdit }) with the access rules of app.routes.projects"""
    from app.routes.projects import can_edit, can_view

    register_costing_routes(r, CostingDeps(can_view, can_edit))


# ---------------------------------------------------------------------------------------------------------------------
# Documents uploaded with the create form (G3.09): documents → versions → files
# ---------------------------------------------------------------------------------------------------------------------
_CREATE_DOC_MIME = {
    'pdf': 'application/pdf',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'gif': 'image/gif',
    'webp': 'image/webp',
    'svg': 'image/svg+xml',
    'txt': 'text/plain',
    'md': 'text/markdown',
    'csv': 'text/csv',
}
# CREATE_DOC_MIME[ext] on a plain object also "finds" Object.prototype members (lower-cased extensions that match):
# the type check passes and the mime is a function / the prototype (dropped by JSON) — mirrored as UNDEF
_PROTO_EXT = frozenset({'constructor', '__proto__'})


def validate_create_documents(lst: Any) -> list[dict]:
    if lst is UNDEF or lst is None:
        return []
    if not isinstance(lst, list):
        err(422, 'VALIDATION', 'Documents must be a list of files.', 'documents')
    if len(lst) > 20:
        err(422, 'FILE_REJECTED', 'Attach up to 20 documents at a time.', 'documents')
    out = []
    for i, f in enumerate(lst):
        o = f if isinstance(f, dict) else {}
        name = _slice16(js_trim(js_str(coalesce(o.get('name', UNDEF), ''))), 160)
        ext = js_lower(name.split('.')[-1]) if '.' in name else ''
        if ext not in _CREATE_DOC_MIME and ext not in _PROTO_EXT:
            err(422, 'FILE_REJECTED', f'{name or f"File {i + 1}"}: use PDF, images (PNG, JPG, GIF, WebP, SVG), text (TXT, MD) or CSV.', f'documents.{i}')
        size = js_number(coalesce(o.get('size_bytes', UNDEF), 0))
        if not (size > 0):
            err(422, 'FILE_REJECTED', f'{name} is empty. Choose another file.', f'documents.{i}')
        if size > CREATE_DOC_MAX_BYTES:
            err(422, 'FILE_REJECTED', f'{name} is larger than 20 MB. Compress it or upload it later as a link.', f'documents.{i}')
        du = o.get('data_url', UNDEF)
        if not isinstance(du, str) or not du.startswith('data:'):
            err(422, 'FILE_REJECTED', f'{name} could not be read. Choose it again.', f'documents.{i}')
        out.append({'name': name, 'mime': _CREATE_DOC_MIME.get(ext, UNDEF), 'size_bytes': size, 'data_url': du})
    return out


def insert_project_documents(ctx: Ctx, project_id: str, docs: list[dict]) -> None:
    for f in docs:
        ext = js_lower(f['name'].split('.')[-1])
        category = 'technical' if ext == 'csv' else 'contract' if ext == 'pdf' else 'other'
        title = js_trim(re.sub(r'[_-]+', ' ', re.sub(r'\.[^.]+\Z', '', f['name'], count=1))) or f['name']
        d = insert(
            ctx,
            'documents',
            {
                'project_id': project_id,
                'title': title,
                'description': 'Uploaded when the project was created.',
                'category': category,
                'visibility': 'project',
                'must_read': False,
                'tags': ['kick-off'],
                'owner_id': ctx.user.get('id'),
                'current_version': 1,
                'archived': False,
            },
        )
        ver = insert(
            ctx,
            'document_versions',
            {
                'document_id': d['id'],
                'version': 1,
                'file_id': None,
                'url': None,
                'file_name': f['name'],
                'size_bytes': f['size_bytes'],
                'mime': f['mime'],
                'note': 'First version',
                'uploaded_by': ctx.user.get('id'),
                'uploaded_at': now_iso(),
            },
        )
        fr = insert(
            ctx,
            'files',
            {
                'name': f['name'],
                'mime': f['mime'],
                'size_bytes': f['size_bytes'],
                'entity': 'document_version',
                'entity_id': ver['id'],
                'data_url': f['data_url'],
            },
        )
        ver['file_id'] = fr['id']  # owned row (from insert): changed in place like the TS
        log_activity(ctx, 'document', d['id'], 'created', f'Uploaded “{js_str(d["title"])}” (v1) with the new project')


def _ms_map_all(ctx: Ctx) -> dict:
    return {m['id']: m for m in rows(ctx, 'milestones')}
