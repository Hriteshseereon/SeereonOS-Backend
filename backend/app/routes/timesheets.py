"""Port of src/mock/routes/timesheets.ts — Module C: timesheets (UI-15), approvals (UI-16), team grid (UI-37), daily
updates (UI-40), leave. Enforces BR-01..BR-11, VAL-F04..F10, F28 and VAL-C01..C09 on the "server".

Identity semantics: the TS mutates the week row in place (applyLock, saveRows, update() of the same object) and keeps
using it. Here the write handlers take a private copy with `edit()` first, so every helper (apply_lock, update(),
patch()) changes that same dict, exactly like the JS object identity.
"""

from __future__ import annotations

import re
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    edit,
    err,
    find,
    find_by,
    get_db,
    group,
    has,
    has_any,
    insert,
    log_activity,
    notify,
    now_iso,
    patch,
    remove,
    rows,
    set_rows,
    update,
    user_name,
    users_with,
    warn,
)
from app.domain.calendar import (
    CalendarCtx,
    add_days,
    day_kind,
    diff_days,
    fmt_date,
    fy_label,
    fy_range,
    fy_start_year,
    is_working_day,
    parse_iso,
    week_days,
    week_label,
    week_start,
)
from app.domain.labels import INTERNAL_CODES, LEAVE_TYPE
from app.engine.analytics import cal, sla_due, task_actuals
from app.engine.approval_kpi import approval_kpi, approval_kpis
from app.engine.workflows import advance_approval, ensure_wf, start_approval, step_label, wf_clear, wf_patch
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_div,
    js_len,
    js_lower,
    js_max,
    js_min,
    js_number,
    js_or,
    js_round,
    js_slice,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    numeric_key,
    prop,
)

__all__ = [
    'leave_label',
    'optional_holiday_on',
    'resolve_approver',
    'timesheet_override',
    'can_decide',
    'apply_lock',
    'sweep_locks',
    'find_week',
    'demo_date_of',
    'register',
]


# ==================================================================
# Shared helpers
# ==================================================================
def S(ctx: Ctx) -> Any:
    return ctx.tenant['settings']


def r2(n: Any) -> Any:
    return js_round(n * 100) / 100


LEAVE_TYPES = ['casual', 'sick', 'earned', 'compensatory', 'lwp', 'optional_holiday']
# Internal activity that counts toward the daily minimum only when the company says breaks count.
BREAKS = 'breaks'
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$', re.ASCII)
_PROOF_RE = re.compile(r'^https?://\S+\.\S+')


def _counts_to_min(ctx: Ctx, e: dict) -> bool:
    return not (e.get('kind') == 'internal' and e.get('internal_code') == BREAKS and not js_truthy(prop(S(ctx), 'calendar.breaks_count')))


def _g(o: Any, k: str) -> Any:
    """o.k for a value coming from a request body (null/undefined throw like JS; non-objects have no such property)"""
    if o is None or o is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(o)} (reading '{k}')")
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    return UNDEF


def _hashable_str(v: Any) -> bool:
    return isinstance(v, str)


_INT_KEY = re.compile(r'^(?:0|[1-9][0-9]*)$', re.ASCII)


def _entries(o: Any) -> list[tuple[str, Any]]:
    """Object.entries(o): integer-like keys first (ascending), then the other keys in insertion order; arrays by index"""
    if isinstance(o, list):
        return [(str(i), v) for i, v in enumerate(o)]
    if isinstance(o, str):
        return [(str(i), c) for i, c in enumerate(o)]
    if not isinstance(o, dict):
        return []
    ints = [(k, v) for k, v in o.items() if _INT_KEY.match(k) and int(k) < 4294967295]
    rest = [(k, v) for k, v in o.items() if not (_INT_KEY.match(k) and int(k) < 4294967295)]
    ints.sort(key=lambda kv: int(kv[0]))
    return ints + rest


def _u16slice(s: str, a: int, b: int) -> str:
    """String.prototype.slice in UTF-16 code units"""
    if all(ord(c) <= 0xFFFF for c in s):
        return js_slice(s, a, b)
    units = s.encode('utf-16-le', 'surrogatepass')
    n = len(units) // 2
    a = max(0, min(a, n))
    b = max(0, min(b, n))
    return units[a * 2 : b * 2].decode('utf-16-le', 'surrogatepass') if b > a else ''


def _fmt_date(s: Any, opts: str = 'short') -> str:
    """fmtDate() also for strings that are not dates (JS prints 'Invalid Date')"""
    if not js_truthy(s):
        return '—'
    if parse_iso(s) is None:
        return 'Invalid Date'
    return fmt_date(s, opts)


def leave_label(t: Any) -> str:
    """leaveLabel(t)"""
    if t in ('lwp', 'optional_holiday'):
        return LEAVE_TYPE[t]
    return f'{js_str(LEAVE_TYPE.get(t, UNDEF) if isinstance(t, str) else UNDEF)} leave'


def optional_holiday_on(ctx: Ctx, date: str) -> dict | None:
    """Optional holidays (the tenant's list) — an optional-holiday leave day must be one of them."""
    return next((h for h in rows(ctx, 'holidays') if js_truthy(h.get('optional')) and h['date'] == date), None)


def _assert_monday(week: str) -> None:
    if not _DATE_RE.match(week) or parse_iso(week) is None or week_start(week) != week:
        err(422, 'TS_BAD_WEEK', 'A timesheet week starts on a Monday. Pick the Monday of the week you want.', 'week')


def _active_user(ctx: Ctx, id_: Any) -> dict | None:
    if not js_truthy(id_):
        return None
    u = find_by('users', 'id', id_)
    if u is None or u.get('tenant_id') != ctx.tenant_id:
        u = next((x for x in rows(ctx, 'users') if x['id'] == id_), None)
    return u if u is not None and u.get('status') == 'active' else None


def resolve_approver(ctx: Ctx, user: dict) -> str | None:
    """BR-07: approver = reports_to (active, not self). Deactivated manager → first admin. Top person → null (auto-approved)."""
    rt = _active_user(ctx, user.get('reports_to_id'))
    if rt is not None and rt['id'] != user['id']:
        return rt['id']
    if user.get('reports_to_id', UNDEF) is None:
        return None
    admin = next((u for u in _top_approvers(ctx) if u['id'] != user['id']), None)
    return admin['id'] if admin is not None else None


def timesheet_override(ctx: Ctx) -> bool:
    """Timesheet override: may decide any submitted week (projects.manage_all — CEO / Administrator / CTO by default)."""
    return has(ctx, 'projects.manage_all')


def _top_approvers(ctx: Ctx) -> list[dict]:
    """People who hold the timesheet override, top of the reporting line first (fallback approvers)."""
    return sorted(users_with(ctx, 'projects.manage_all'), key=lambda u: 1 if js_truthy(u.get('reports_to_id')) else 0)


def _delegators_of(ctx: Ctx, me: str, on: Any = None) -> set:
    """People who have delegated their approvals to me today."""
    on = ctx.today if on is None else on
    return {
        d['from_user_id']
        for d in rows(ctx, 'delegations')
        if d['to_user_id'] == me and d['from_on'] <= on and d['to_on'] >= on and not js_truthy(d.get('project_id'))
    }


def _active_delegates(ctx: Ctx, approver_id: str, on: Any = None) -> list[str]:
    """Delegates covering an approver today (all-project delegations only)."""
    on = ctx.today if on is None else on
    return [
        d['to_user_id']
        for d in rows(ctx, 'delegations')
        if d['from_user_id'] == approver_id and d['from_on'] <= on and d['to_on'] >= on and not js_truthy(d.get('project_id'))
    ]


def _approver_for(ctx: Ctx, w: dict) -> str | None:
    if js_truthy(w.get('approver_id')):
        return w['approver_id']
    u = next((x for x in rows(ctx, 'users') if x['id'] == w['user_id']), None)
    return resolve_approver(ctx, u) if u is not None else None


def can_decide(ctx: Ctx, w: dict) -> dict[str, bool]:
    """Can the viewer approve / reject / unlock this week? (F08: never your own) → {'ok', 'viaDelegation'}"""
    if w['user_id'] == ctx.user_id:
        return {'ok': False, 'viaDelegation': False}
    if timesheet_override(ctx):
        return {'ok': True, 'viaDelegation': False}
    ap = _approver_for(ctx, w)
    if ap == ctx.user_id:
        return {'ok': True, 'viaDelegation': False}
    if js_truthy(ap) and ap in _delegators_of(ctx, ctx.user_id):
        return {'ok': True, 'viaDelegation': True}
    return {'ok': False, 'viaDelegation': False}


def apply_lock(ctx: Ctx, w: dict) -> dict:
    """BR-06 lazy lock: an open week whose Sunday has passed locks (unless an unlock window is live).
    Returns the CURRENT row (the TS mutates `w` in place — use the return value)."""
    if w['status'] != 'open' or not js_truthy(prop(S(ctx), 'timesheet.weekly_lock')):
        return w
    sunday = add_days(w['week_start'], 6)
    if sunday >= ctx.today:
        return w
    if js_truthy(w.get('unlock_until')) and w['unlock_until'] >= ctx.today:
        return w
    cur = find_by('timesheet_weeks', 'id', w['id'])
    if cur is not None and cur is not w:
        # a stale reference (the row was already changed in this request): JS would see the change through identity
        return apply_lock(ctx, cur)
    return patch('timesheet_weeks', w, {'status': 'locked', 'version': w['version'] + 1, 'updated_at': now_iso()})


def sweep_locks(ctx: Ctx) -> None:
    for w in list(rows(ctx, 'timesheet_weeks')):
        if w['status'] == 'open':
            apply_lock(ctx, w)


def _week_of(ctx: Ctx, user_id: Any, monday: str) -> dict | None:
    """rows(ctx, 'timesheet_weeks').find((x) => x.user_id === userId && x.week_start === monday)"""
    tid = ctx.tenant_id
    for x in group('timesheet_weeks', 'user_id').get(user_id, ()) if isinstance(user_id, str) else ():
        if x['week_start'] == monday and x.get('tenant_id') == tid:
            return x
    return None


def find_week(ctx: Ctx, user_id: str, monday: str) -> dict | None:
    w = _week_of(ctx, user_id, monday)
    return apply_lock(ctx, w) if w is not None else None


def _ensure_week(ctx: Ctx, user_id: str, monday: str) -> dict:
    w = find_week(ctx, user_id, monday)
    if w is not None:
        return w
    uid_ = user_id[2:] if user_id.startswith('u_') else user_id
    row = insert(
        ctx,
        'timesheet_weeks',
        {
            'id': f'wk_{uid_}_{monday}',
            'user_id': user_id,
            'week_start': monday,
            'status': 'open',
            'submitted_at': None,
            'approver_id': None,
            'decided_by': None,
            'decided_at': None,
            'late': False,
            'reject_reason': None,
            'unlock_requested_at': None,
            'unlock_reason': None,
            'unlock_until': None,
        },
    )
    return apply_lock(ctx, row)


def _is_editable(w: dict) -> bool:
    return w['status'] in ('open', 'rejected')


def _assert_editable(w: dict) -> None:
    if w['status'] == 'locked':
        err(409, 'TS_LOCKED', 'This week ended without being submitted, so it is locked. Ask your manager to unlock it.')
    if not _is_editable(w):
        err(
            409,
            'TS_NOT_EDITABLE',
            'This week is approved and can no longer change.'
            if w['status'] == 'approved'
            else 'This week is submitted and waiting for approval. It can’t be edited now.',
        )


def _entries_of(ctx: Ctx, w: dict) -> list[dict]:
    tid = ctx.tenant_id
    return [e for e in group('timesheet_entries', 'week_id').get(w['id'], ()) if e.get('tenant_id') == tid]


def _leave_of(ctx: Ctx, user_id: Any) -> list[dict]:
    tid = ctx.tenant_id
    if not isinstance(user_id, str):
        return []
    return [lv for lv in group('leave', 'user_id').get(user_id, ()) if lv.get('tenant_id') == tid]


def _user_cal(ctx: Ctx, user_id: Any) -> CalendarCtx:
    base = cal(ctx)
    full = {lv['date'] for lv in _leave_of(ctx, user_id) if lv.get('portion') == 'full'}
    c = CalendarCtx(base.settings, base.holidays, full)
    c._hs = base._hs
    return c


def _no_leave(c: CalendarCtx) -> CalendarCtx:
    """{ ...c, leaveDates: undefined }"""
    return c.without_leave()


def _day_totals(ctx: Ctx, w: dict, entries: list[dict] | None = None) -> list[dict]:
    if entries is None:
        entries = _entries_of(ctx, w)
    mn = prop(S(ctx), 'calendar.min_hours_per_day')
    c = _user_cal(ctx, w['user_id'])
    by_date: dict[str, list[dict]] = {}
    for e in entries:
        by_date.setdefault(e['work_date'], []).append(e)
    decided = not _is_editable(w)
    out = []
    for date in week_days(w['week_start']):
        kind = day_kind(date, c)
        day = by_date.get(date, [])
        tot: Any = 0
        for e in day:
            tot = tot + e['hours']
        cnt: Any = 0
        for e in day:
            if _counts_to_min(ctx, e):
                cnt = cnt + e['hours']
        total = r2(tot)
        counted = r2(cnt)
        # a half-day leave keeps the day a working day: the leave hours count toward the minimum
        min_req = mn if kind == 'working' else 0
        below = min_req > 0 and counted < min_req and (date < ctx.today or (decided and date <= ctx.today))
        out.append({'date': date, 'kind': kind, 'total': total, 'counted': counted, 'min_required': min_req, 'below_min': below})
    return out


def _task_allowed(ctx: Ctx, t: dict, user_id: str, monday: str, cover: dict | None = None) -> bool:
    """Task access for time entry (VAL-F09): owner, backup, or allocated to the task's milestone during the week.
    `cover` caches coveringFor() for a loop over many tasks with no write in between (same result)."""
    from app.routes.tasks import covering_for

    if t.get('owner_id') == user_id or t.get('backup_id') == user_id:
        return True
    sun = add_days(monday, 6)
    if cover is not None:
        k = (user_id, monday)
        cv = cover.get(k)
        if cv is None:
            cv = cover[k] = covering_for(ctx, user_id, monday, sun)
    else:
        cv = covering_for(ctx, user_id, monday, sun)
    if t['id'] in cv:  # covering via delegation
        return True
    for a in _allocations_of(ctx, user_id):
        if (
            a['user_id'] == user_id
            and a['project_id'] == t['project_id']
            and (not js_truthy(t.get('milestone_id')) or not js_truthy(a.get('milestone_id')) or a.get('milestone_id') == t.get('milestone_id'))
            and a['from_on'] <= sun
            and a['to_on'] >= monday
        ):
            return True
    return False


def _allocations_of(ctx: Ctx, user_id: Any) -> list:
    """rows(ctx, 'allocations').filter((a) => a.user_id === userId) — table order"""
    tid = ctx.tenant_id
    return [a for a in group('allocations', 'user_id').get(user_id, ()) if a.get('tenant_id') == tid] if isinstance(user_id, str) else []


def _project_closed_on(ctx: Ctx, p: dict) -> str | None:
    """VAL-F10: completed / closed projects accept no hours after their status date."""
    if p['status'] not in ('completed', 'closed'):
        return None
    hs = [x for x in rows(ctx, 'project_status_history') if x['project_id'] == p['id'] and x['to_status'] == p['status']]
    hs = sorted(hs, key=lambda x: locale_key(x['changed_at']), reverse=True)
    return hs[0]['changed_at'][:10] if hs else p['updated_at'][:10]


def _row_key(e: dict) -> str:
    kind = e.get('kind')
    if kind == 'task':
        return f'task:{js_str(e.get("task_id"))}'
    if kind == 'leave':
        return f'leave:{js_str(e.get("leave_type"))}'
    note = coalesce(e.get('note', UNDEF), '')
    return f'internal:{js_str(e.get("internal_code"))}:{js_str(coalesce(e.get("project_id", UNDEF), ""))}:{js_lower(js_trim(js_str(note)))}'


def _activity_projects(ctx: Ctx, user_id: Any) -> list[dict]:
    """Projects a person can book activity time to (No project is always allowed)."""
    mine = set()
    for a in rows(ctx, 'allocations'):
        if a['user_id'] == user_id:
            mine.add(a['project_id'])
    for t in rows(ctx, 'tasks'):
        if t.get('owner_id') == user_id or t.get('backup_id') == user_id:
            mine.add(t['project_id'])
    for p in rows(ctx, 'projects'):
        if p.get('pm_id') == user_id:
            mine.add(p['id'])
    ps = [p for p in rows(ctx, 'projects') if p['status'] not in ('completed', 'closed') and (p['id'] in mine or p['code'] == 'GEN')]
    ps = sorted(ps, key=lambda p: locale_key(p['code']))
    return [{'id': p['id'], 'code': p['code'], 'name': p['name']} for p in ps]


def demo_date_of(ctx: Ctx, monday: str, user_id: Any = UNDEF) -> str:
    """Day the week's Friday demo is recorded on: Friday, or the nearest working day before it (else the last working day)."""
    c = _no_leave(_user_cal(ctx, user_id) if js_truthy(user_id) else cal(ctx))
    fri = add_days(monday, 4)
    d = fri
    while d >= monday:
        if is_working_day(d, c):
            return d
        d = add_days(d, -1)
    d = add_days(monday, 6)
    while d > fri:
        if is_working_day(d, c):
            return d
        d = add_days(d, -1)
    return fri


def _dow(d: str) -> int:
    x = parse_iso(d)
    return (x.weekday() + 1) % 7 if x is not None else -1


# ------------------------------------------------------------------
# Week view
# ------------------------------------------------------------------
def _build_week_view(ctx: Ctx, w: dict) -> dict:
    from app.routes.tasks import milestone_map, wbs_of

    st = S(ctx)
    user = find(ctx, 'users', w['user_id'], 'Person')
    entries = _entries_of(ctx, w)
    totals = _day_totals(ctx, w, entries)
    holidays = {h['date']: h['name'] for h in rows(ctx, 'holidays') if not js_truthy(h.get('optional'))}
    leaves = {lv['date']: lv for lv in _leave_of(ctx, w['user_id'])}
    editable = _is_editable(w)
    base_no_leave = _no_leave(cal(ctx))
    days = []
    for d in totals:
        lv = leaves.get(d['date'])
        future = d['date'] > ctx.today
        hol = holidays.get(d['date'])
        off = d['kind'] == 'weekly_off' or (d['kind'] == 'leave' and day_kind(d['date'], base_no_leave) == 'weekly_off')
        days.append(
            {
                'date': d['date'],
                'kind': d['kind'],
                'holiday': hol,
                # Mon–Sat always; Sunday only when the company calendar makes it a working day (or hours were logged on it)
                'hidden': _dow(d['date']) == 0 and off and d['total'] == 0,
                'label': 'Holiday' if js_truthy(hol) else 'Weekly off' if off else ('Half day' if lv['portion'] == 'half' else 'Leave') if lv is not None else None,
                'counted': d['counted'],
                'leave': {'id': lv['id'], 'type': lv['type'], 'portion': lv['portion'], 'source': lv['source']} if lv is not None else None,
                'total': d['total'],
                'min_required': d['min_required'],
                'below_min': d['below_min'],
                'future': future,
                'entry_allowed': js_or(not future, st['timesheet.allow_future_entries']),
            }
        )
    ms = milestone_map(ctx)
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    act = task_actuals(ctx)
    hr_dates = {lv['date'] for lv in leaves.values() if lv.get('source') == 'hr'}
    grouped: dict[str, dict] = {}
    for e in sorted(entries, key=lambda e: locale_key(e['created_at'])):
        key = _row_key(e)
        row = grouped.get(key)
        if row is None:
            t = tasks.get(e['task_id']) if js_truthy(e.get('task_id')) else None
            p = projects.get(t['project_id']) if t is not None else None
            m = ms.get(t['milestone_id']) if t is not None and js_truthy(t.get('milestone_id')) else None
            kind = e['kind']
            if kind == 'task':
                label = t['title'] if t is not None else 'Removed task'
            elif kind == 'leave':
                label = leave_label(coalesce(e.get('leave_type', UNDEF), 'casual'))
            else:
                ic = e.get('internal_code')
                label = coalesce(INTERNAL_CODES.get(coalesce(ic, '')) if isinstance(coalesce(ic, ''), str) else None, ic, 'Internal')
            if kind == 'task':
                parts = [p.get('code', UNDEF) if p is not None else UNDEF, p.get('name', UNDEF) if p is not None else UNDEF, f'M{js_str(m["no"])} {js_str(m["name"])}' if m is not None else None]
                sublabel = ' · '.join(js_str(x) for x in parts if js_truthy(x))
            elif kind == 'leave':
                sublabel = 'Leave'
            elif js_truthy(e.get('project_id')):
                pp = projects.get(e['project_id'])
                sublabel = js_trim(f'Activity · {js_str(coalesce(pp.get("code", UNDEF) if pp else UNDEF, ""))} {js_str(coalesce(pp.get("name", UNDEF) if pp else UNDEF, ""))}')
            else:
                sublabel = 'Activity · no project'
            row = {
                'key': key,
                'kind': kind,
                'task_id': e.get('task_id', UNDEF),
                'internal_code': e.get('internal_code', UNDEF),
                'leave_type': e.get('leave_type', UNDEF),
                'note': coalesce(e.get('note', UNDEF), ''),
                'label': label,
                'sublabel': sublabel,
                'project_id': coalesce(e.get('project_id', UNDEF), None) if kind == 'internal' else (coalesce(t.get('project_id', UNDEF), None) if t is not None else None),
                'wbs': wbs_of(t, ms) if t is not None else None,
                'project_code': coalesce(p.get('code', UNDEF), None) if p is not None else None,
                'project_hue': coalesce(p.get('color_hue', UNDEF), None) if p is not None else None,
                'task_status': coalesce(t.get('status', UNDEF), None) if t is not None else None,
                'est_hours': coalesce(t.get('est_hours', UNDEF), None) if t is not None else None,
                'logged_hours': r2(coalesce(act[t['id']]['hours'] if t['id'] in act else UNDEF, 0)) if t is not None else None,
                'hours': {},
                'total': 0,
                'locked_reason': None,
            }
            grouped[key] = row
        if e['hours'] > 0:
            row['hours'][e['work_date']] = r2(coalesce(row['hours'].get(e['work_date'], UNDEF), 0) + e['hours'])
        row['total'] = r2(row['total'] + e['hours'])
        if e['kind'] == 'leave' and e['work_date'] in hr_dates:
            row['locked_reason'] = 'Leave entered by HR. Ask HR to change it.'
    order = {'task': 0, 'internal': 1, 'leave': 2}
    out_rows = sorted(grouped.values(), key=lambda r: (order[r['kind']], numeric_key(coalesce(r['wbs'], ''))))
    approver_id = coalesce(w.get('approver_id', UNDEF), resolve_approver(ctx, user))
    total: Any = 0
    for d in totals:
        total = total + d['total']
    total = r2(total)
    is_self = w['user_id'] == ctx.user_id
    wk_end = add_days(w['week_start'], 6)
    optional = [{'date': h['date'], 'name': h['name']} for h in rows(ctx, 'holidays') if js_truthy(h.get('optional')) and h['date'] >= w['week_start'] and h['date'] <= wk_end]
    target: Any = 0
    for d in totals:
        target = target + d['min_required']
    target_to_date: Any = 0
    for d in totals:
        if d['date'] <= ctx.today:
            target_to_date = target_to_date + d['min_required']
    counted: Any = 0
    for d in totals:
        counted = counted + d['counted']
    uu = w.get('unlock_until')
    return {
        'id': w['id'],
        'version': w['version'],
        'user': {'id': user['id'], 'name': user.get('name', UNDEF), 'title': user.get('title', UNDEF)},
        'is_self': is_self,
        'week_start': w['week_start'],
        'week_end': wk_end,
        'status': w['status'],
        'editable': editable and is_self and not ctx.view_as,
        'unlocked_until': uu if js_truthy(uu) and uu >= ctx.today and w['status'] == 'open' else None,
        'submitted_at': w.get('submitted_at', UNDEF),
        'decided_at': w.get('decided_at', UNDEF),
        'decided_by': {'id': w['decided_by'], 'name': user_name(ctx, w['decided_by'])} if js_truthy(w.get('decided_by')) else None,
        'approver': {'id': approver_id, 'name': user_name(ctx, approver_id)} if js_truthy(approver_id) else None,
        'auto_approved': w['status'] == 'approved' and w.get('decided_by') == w['user_id'],
        'reject_reason': w.get('reject_reason', UNDEF),
        'unlock_requested_at': w.get('unlock_requested_at', UNDEF),
        'unlock_reason': w.get('unlock_reason', UNDEF),
        'sla_due': sla_due(ctx, w['submitted_at']) if w['status'] == 'submitted' and js_truthy(w.get('submitted_at')) else None,
        'late': w.get('late', UNDEF),
        'days': days,
        'rows': out_rows,
        'total': total,
        'target': target,
        'target_to_date': target_to_date,
        'counted': r2(counted),
        'submit_by': wk_end,
        'below_min_days': sum(1 for d in totals if d['below_min']),
        'activity_projects': _activity_projects(ctx, w['user_id']) if is_self else [],
        'optional_holidays': optional,
        'rules': {
            'min_hours': st['calendar.min_hours_per_day'],
            'max_hours': st['calendar.max_hours_per_day'],
            'step': st['timesheet.hours_step'],
            'allow_future': st['timesheet.allow_future_entries'],
            'note_required': st['timesheet.internal_note_required'],
            'unlock_max_days': st['timesheet.unlock_max_days'],
            'bulk_approve': st['approval.bulk_approve'],
            'breaks_count': st['calendar.breaks_count'],
        },
        'can_submit': editable and is_self and w['week_start'] <= ctx.today,
        'can_request_unlock': is_self and w['status'] == 'locked' and not js_truthy(w.get('unlock_requested_at')),
    }


def _can_view(ctx: Ctx, user_id: Any) -> bool:
    if user_id == ctx.user_id:
        return True
    if has_any(ctx, 'people.view_all', 'projects.view_all'):
        return True
    u = next((x for x in rows(ctx, 'users') if x['id'] == user_id), None)
    if u is None:
        return False
    ap = resolve_approver(ctx, u)
    return ap == ctx.user_id or (js_truthy(ap) and ap in _delegators_of(ctx, ctx.user_id))


# ------------------------------------------------------------------
# Save (replace entry set) with all validations
# ------------------------------------------------------------------
def _save_rows(ctx: Ctx, w: dict, inp: Any, keep_empty: bool = False) -> None:
    """Replace the week's entries (w must be a private row: it is changed in place like the TS)."""
    st = S(ctx)
    if not isinstance(inp, list):
        err(422, 'VALIDATION', 'Send the rows of the week.', 'rows')
    days = week_days(w['week_start'])
    days_set = set(days)
    c = _user_cal(ctx, w['user_id'])
    step = st['timesheet.hours_step']
    max_h = st['calendar.max_hours_per_day']
    min_h = st['calendar.min_hours_per_day']
    existing = _entries_of(ctx, w)
    had_task = {e.get('task_id') for e in existing if e['kind'] == 'task'}
    leaves = _leave_of(ctx, w['user_id'])
    hr_leave_dates = {lv['date'] for lv in leaves if lv.get('source') == 'hr'}
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    allowed_projects = {p['id'] for p in _activity_projects(ctx, w['user_id'])}
    out: list[dict] = []
    seen: set[str] = set()
    cover: dict = {}
    for i, row in enumerate(inp):
        kind = _g(row, 'kind')
        if not (isinstance(kind, str) and kind in ('task', 'leave', 'internal')):
            err(422, 'VALIDATION', f'Row {i + 1}: choose a task, leave type or internal activity.', f'rows.{i}')
        note = _u16slice(js_trim(js_str(coalesce(_g(row, 'note'), ''))), 0, 500)
        rh = _g(row, 'hours')
        hours = rh if js_truthy(rh) and isinstance(rh, (dict, list)) else {}
        hentries = _entries(hours)
        any_ = any(js_number(h) > 0 for _, h in hentries)
        label: Any = ''
        task_id = _g(row, 'task_id')
        internal_code = _g(row, 'internal_code')
        leave_type = _g(row, 'leave_type')
        project_id_in = _g(row, 'project_id')
        if kind == 'task':
            t = tasks.get(task_id) if js_truthy(task_id) and _hashable_str(task_id) else None
            if t is None:
                err(422, 'VALIDATION', f'Row {i + 1}: that task no longer exists. Remove the row.', f'rows.{i}')
            label = t['title']
            if any_ and t['id'] not in had_task and not _task_allowed(ctx, t, w['user_id'], w['week_start'], cover):
                err(
                    403,
                    'TS_TASK_NOT_ALLOWED',
                    f'You can log time only on tasks you own, back up or are allocated to. “{js_str(t["title"])}” isn’t one of them.',
                    f'rows.{i}',
                )
            p = projects.get(t['project_id'])
            closed_on = _project_closed_on(ctx, p) if p is not None else None
            if js_truthy(closed_on) and any_ and any(js_number(h) > 0 and d > closed_on for d, h in hentries):
                err(
                    409,
                    'TS_PROJECT_CLOSED',
                    f'{js_str(p["name"])} was {js_str(p["status"])} on {_fmt_date(closed_on)}. Hours after that date can’t be booked to it.',
                    f'rows.{i}',
                )
        elif kind == 'internal':
            if not js_truthy(internal_code) or not _hashable_str(internal_code) or not js_truthy(INTERNAL_CODES.get(internal_code)):
                err(422, 'VALIDATION', f'Row {i + 1}: choose an activity.', f'rows.{i}')
            label = INTERNAL_CODES[internal_code]
            if (
                js_truthy(project_id_in)
                and not (_hashable_str(project_id_in) and project_id_in in allowed_projects)
                and not (_hashable_str(project_id_in) and project_id_in in projects)
            ):
                err(422, 'VALIDATION', f'Row {i + 1}: that project no longer exists. Pick another or “No project”.', f'rows.{i}.project_id')
            if any_ and js_truthy(st['timesheet.internal_note_required']) and internal_code != BREAKS and js_len(note) < 3:
                err(422, 'TS_NOTE_REQUIRED', f'Add a short note to “{js_str(label)}” saying what the time was for.', f'rows.{i}.note')
        else:
            if not js_truthy(leave_type) or not (isinstance(leave_type, str) and leave_type in LEAVE_TYPES):
                err(422, 'VALIDATION', f'Row {i + 1}: choose a leave type.', f'rows.{i}')
            label = leave_label(leave_type)
        project_id = js_or(project_id_in, None) if kind == 'internal' else None
        # Feature Guide §8: "Rows with no hours disappear" — on every save, whatever the note says.
        if not any_ and not keep_empty:
            continue
        key = _row_key(
            {
                'kind': kind,
                'task_id': coalesce(task_id, None),
                'internal_code': coalesce(internal_code, None),
                'leave_type': coalesce(leave_type, None),
                'note': note,
                'project_id': project_id,
            }
        )
        if key in seen:
            err(422, 'VALIDATION', f'“{js_str(label)}” appears twice. Merge the rows into one.', f'rows.{i}')
        seen.add(key)
        wrote = False
        for date, raw in hentries:
            h = js_number(raw)
            if not js_truthy(h):
                continue
            if date not in days_set:
                err(422, 'TS_BAD_HOURS', f'{_fmt_date(date)} is outside this week.', f'rows.{i}')
            if not is_finite(h) or h < 0 or h > 24:
                err(422, 'TS_BAD_HOURS', f'Hours must be between 0 and 24 ({js_str(label)}, {_fmt_date(date, "day")}).', f'rows.{i}')
            q = js_div(h, step)
            if abs(q - js_round(q)) > 1e-9:
                err(
                    422,
                    'TS_BAD_STEP',
                    f'Enter hours in steps of {js_str(step)}h. {js_str(h)}h on {_fmt_date(date, "day")} for “{js_str(label)}” isn’t one.',
                    f'rows.{i}',
                )
            if kind != 'leave' and date > ctx.today and not js_truthy(st['timesheet.allow_future_entries']):
                err(422, 'TS_FUTURE', f'{_fmt_date(date, "day")} hasn’t happened yet. Log hours on or after the day you worked them.', f'rows.{i}')
            if kind == 'leave':
                if date in hr_leave_dates:
                    continue  # HR-entered leave is kept as is
                k = day_kind(date, _no_leave(c))
                if k != 'working':
                    err(
                        422,
                        'LEAVE_INVALID',
                        f'{_fmt_date(date, "day")} is {"a holiday" if k == "holiday" else "a weekly off"}, so it can’t be a leave day.',
                        f'rows.{i}',
                    )
                if leave_type == 'optional_holiday' and optional_holiday_on(ctx, date) is None:
                    err(
                        422,
                        'LEAVE_INVALID',
                        f'{_fmt_date(date, "day")} isn’t on the optional holiday list. Pick one of the optional holidays in the holiday calendar, or another leave type.',
                        f'rows.{i}',
                    )
            out.append(
                {
                    'work_date': date,
                    'kind': kind,
                    'task_id': task_id if kind == 'task' else None,
                    'internal_code': internal_code if kind == 'internal' else None,
                    'leave_type': leave_type if kind == 'leave' else None,
                    'hours': h,
                    'note': note,
                    'project_id': project_id,
                }
            )
            wrote = True
        # keep empty rows pinned (e.g. copied from last week) with a zero-hour marker
        if not wrote:
            out.append(
                {
                    'work_date': w['week_start'],
                    'kind': kind,
                    'task_id': task_id if kind == 'task' else None,
                    'internal_code': internal_code if kind == 'internal' else None,
                    'leave_type': leave_type if kind == 'leave' else None,
                    'hours': 0,
                    'note': note,
                    'project_id': project_id,
                }
            )
    # HR leave entries survive the replace
    keep_hr = [e for e in existing if e['kind'] == 'leave' and e['work_date'] in hr_leave_dates]
    # VAL-C02: per-day maximum
    for d in days:
        tot: Any = 0
        for e in out:
            if e['work_date'] == d:
                tot = tot + e['hours']
        tot2: Any = 0
        for e in keep_hr:
            if e['work_date'] == d:
                tot2 = tot2 + e['hours']
        tot = tot + tot2
        if tot > max_h:
            err(422, 'TS_OVER_MAX', f'{_fmt_date(d, "day")} adds up to {js_str(r2(tot))}h. The most you can log in a day is {js_str(max_h)}h.')
    # Replace the entry set
    keep_ids = {id(e) for e in keep_hr}
    tid = ctx.tenant_id
    wid = w['id']
    set_rows(
        'timesheet_entries',
        [e for e in get_db().timesheet_entries if not (e.get('tenant_id') == tid and e.get('week_id') == wid and id(e) not in keep_ids)],
    )
    for e in out:
        insert(ctx, 'timesheet_entries', {'week_id': w['id'], 'user_id': w['user_id'], **e})
    # BR-09: leave rows ⇄ leave records (timesheet-sourced only)
    for d in days:
        if d in hr_leave_dates:
            continue
        lv = [e for e in out if e['kind'] == 'leave' and e['work_date'] == d and e['hours'] > 0]
        cur = next((x for x in leaves if x['date'] == d and x.get('source') == 'timesheet'), None)
        if not lv:
            if cur is not None:
                remove(ctx, 'leave', cur['id'])
            continue
        hrs: Any = 0
        for e in lv:
            hrs = hrs + e['hours']
        typ = sorted(lv, key=lambda e: -e['hours'])[0]['leave_type']
        portion = 'full' if hrs >= min_h else 'half'
        if cur is not None:
            if cur['type'] != typ or cur['portion'] != portion:
                update(ctx, 'leave', cur['id'], {'type': typ, 'portion': portion})
        else:
            insert(ctx, 'leave', {'user_id': w['user_id'], 'date': d, 'type': typ, 'portion': portion, 'source': 'timesheet', 'approved_by': None})
    patch('timesheet_weeks', w, {'version': w['version'] + 1, 'updated_at': now_iso()})


def _submit_week(ctx: Ctx, w: dict) -> dict:
    _assert_editable(w)
    if w['week_start'] > ctx.today:
        err(422, 'TS_FUTURE', 'This week hasn’t started yet. Submit it once you’ve worked it.')
    totals = _day_totals(ctx, w)
    total: Any = 0
    for d in totals:
        total = total + d['total']
    if total <= 0:
        err(422, 'TS_BAD_HOURS', 'Log your hours before submitting the week.')
    st = S(ctx)
    user = find(ctx, 'users', w['user_id'], 'Person')
    # Timesheet workflow (default: reporting manager); the v3 rule applies when it names nobody.
    wf = start_approval(ctx, 'timesheet', {'requester_id': user['id']}, resolve_approver(ctx, user))
    approver = wf['approver_id']
    below = [d for d in totals if d['min_required'] > 0 and d['counted'] < d['min_required'] and d['date'] <= ctx.today]
    if below:
        lst = ', '.join(f'{_fmt_date(d["date"], "day")}: {js_str(d["total"])}h' for d in below)
        warn(
            ctx,
            'TS_BELOW_MIN',
            f'{lst} {"is" if len(below) == 1 else "are"} below {js_str(st["calendar.min_hours_per_day"])}h. Short days may not be paid in full.',
        )
    was_rejected = w['status'] == 'rejected'
    # rows still at 0h (e.g. copied from last week and never filled) don't travel with the submitted week
    tid = ctx.tenant_id
    wid = w['id']
    set_rows('timesheet_entries', [e for e in get_db().timesheet_entries if not (e.get('tenant_id') == tid and e.get('week_id') == wid and not (e['hours'] > 0))])
    if not js_truthy(approver):
        # Top person: auto-approved (BR-07)
        update(
            ctx,
            'timesheet_weeks',
            w['id'],
            {
                'status': 'approved',
                'submitted_at': now_iso(),
                'approver_id': None,
                'decided_by': user['id'],
                'decided_at': now_iso(),
                'late': False,
                'reject_reason': None,
                'unlock_until': None,
                'unlock_requested_at': None,
                'unlock_reason': None,
            },
        )
        log_activity(ctx, 'timesheet_week', w['id'], 'auto_approved', f'Submitted {week_label(w["week_start"])} (auto-approved)')
        return {'approverName': None}
    update(
        ctx,
        'timesheet_weeks',
        w['id'],
        {
            'status': 'submitted',
            'submitted_at': now_iso(),
            'approver_id': approver,
            'decided_by': None,
            'decided_at': None,
            'late': False,
            'reject_reason': None,
            'unlock_until': None,
            'unlock_requested_at': None,
            'unlock_reason': None,
            **wf_patch(wf),
        },
    )
    short = f', {len(below)} short day{"s" if len(below) > 1 else ""}' if below else ''
    body = f'{js_str(user.get("name", UNDEF))} {"resubmitted" if was_rejected else "submitted"} {week_label(w["week_start"])} ({js_str(r2(total))}h{short}).'
    for to in [approver, *_active_delegates(ctx, approver)]:
        notify(ctx, to, 'NTF-04', 'Timesheet submitted', body, '/approvals')
    log_activity(ctx, 'timesheet_week', w['id'], 'submitted', f'Submitted {week_label(w["week_start"])} to {user_name(ctx, approver)}')
    return {'approverName': user_name(ctx, approver)}


# ------------------------------------------------------------------
# Approvals
# ------------------------------------------------------------------
def _approval_item(ctx: Ctx, w: dict, via_delegation: bool) -> dict:
    u = find(ctx, 'users', w['user_id'], 'Person')
    totals = _day_totals(ctx, w)
    sub = w.get('submitted_at')
    due = sla_due(ctx, sub) if js_truthy(sub) else None
    total: Any = 0
    for d in totals:
        total = total + d['total']
    ura = w.get('unlock_requested_at')
    if js_truthy(sub):
        waiting = js_max(0, diff_days(ctx.today, sub[:10]))
    elif js_truthy(ura):
        waiting = js_max(0, diff_days(ctx.today, ura[:10]))
    else:
        waiting = 0
    return {
        'week_id': w['id'],
        'version': w['version'],
        'user': {'id': u['id'], 'name': u.get('name', UNDEF), 'title': u.get('title', UNDEF), 'avatar_hue': u.get('avatar_hue', UNDEF)},
        'week_start': w['week_start'],
        'status': w['status'],
        'total': r2(total),
        'day_totals': [
            {
                'date': d['date'],
                'total': d['total'],
                'below_min': d['min_required'] > 0 and d['counted'] < d['min_required'],
                'kind': d['kind'],
                'hidden': _dow(d['date']) == 0 and d['kind'] != 'working' and d['total'] == 0,
            }
            for d in totals
        ],
        'days_below_min': sum(1 for d in totals if d['min_required'] > 0 and d['counted'] < d['min_required']),
        'submitted_at': w.get('submitted_at', UNDEF),
        'days_waiting': waiting,
        'sla_due': due,
        'overdue': w['status'] == 'submitted' and js_truthy(due) and ctx.today > due,
        'via_delegation': via_delegation,
        'unlock_requested_at': w.get('unlock_requested_at', UNDEF),
        'unlock_reason': w.get('unlock_reason', UNDEF),
        # v4 workflow extra (multi-step timesheet chains); not in the shared ApprovalItem type
        'step_label': step_label(w) if w['status'] == 'submitted' else None,
    }


def _decidable(ctx: Ctx, week_id: str) -> dict:
    """find + lazy lock + checks; returns a PRIVATE copy of the week (the TS keeps using the mutated db row)."""
    w = edit('timesheet_weeks', find(ctx, 'timesheet_weeks', week_id, 'Timesheet week'))
    apply_lock(ctx, w)
    if w['user_id'] == ctx.user_id:
        err(403, 'TS_SELF_APPROVAL', 'You can’t approve or unlock your own timesheet. Your manager does that.')
    if not can_decide(ctx, w)['ok']:
        err(403, 'FORBIDDEN', 'Only this person’s approver (or their delegate) can do this.')
    return w


def _do_approve(ctx: Ctx, w: dict, if_match: Any = UNDEF) -> bool:
    """w must be a private (edited) row"""
    if w['status'] != 'submitted':
        err(409, 'TS_NOT_EDITABLE', 'Only submitted weeks can be approved. Refresh to see its current state.')
    late = js_truthy(w.get('submitted_at')) and ctx.today > sla_due(ctx, w['submitted_at'])
    owner = next((u for u in rows(ctx, 'users') if u['id'] == w['user_id']), None)
    st = ensure_wf(ctx, w, 'timesheet', {'requester_id': w['user_id']}, resolve_approver(ctx, owner) if owner is not None else None)
    adv = advance_approval(ctx, st, ctx.user_id)
    me = js_str(ctx.user.get('name', UNDEF))
    if adv['next'] is not None:
        # Multi-step timesheet workflow: the week stays submitted and moves to the next approver.
        nx = adv['next']
        update(ctx, 'timesheet_weeks', w['id'], {'approver_id': nx['approver_id'], **wf_patch(adv['state'])}, if_match)
        for to in [nx['approver_id'], *_active_delegates(ctx, nx['approver_id'])]:
            notify(
                ctx,
                to,
                'NTF-04',
                'Timesheet to approve',
                f'{user_name(ctx, w["user_id"])}’s {week_label(w["week_start"])} was approved by {me} and needs your approval ({js_str(nx.get("name", UNDEF))}).',
                '/approvals',
            )
        done = adv['done']
        done_name = coalesce(done.get('name', UNDEF) if done is not None else UNDEF, 'approval')
        log_activity(
            ctx,
            'timesheet_week',
            w['id'],
            'approved_step',
            f'Step {js_str(st["wf_step"] + 1)} of {len(st["wf_chain"])} ({js_str(done_name)}) of {user_name(ctx, w["user_id"])}’s {week_label(w["week_start"])} approved by {me}; sent to {user_name(ctx, nx["approver_id"])}',
        )
        warn(ctx, 'NEXT_APPROVAL', f'Step {js_str(st["wf_step"] + 1)} of {len(st["wf_chain"])} approved — sent to {user_name(ctx, nx["approver_id"])}.')
        return False
    update(
        ctx,
        'timesheet_weeks',
        w['id'],
        {'status': 'approved', 'decided_by': ctx.user_id, 'decided_at': now_iso(), 'late': late, 'reject_reason': None, **wf_patch(adv['state'])},
        if_match,
    )
    notify(ctx, w['user_id'], 'NTF-05', 'Timesheet approved', f'{me} approved {week_label(w["week_start"])}. It counts for salary.', f'/timesheet/{w["week_start"]}')
    log_activity(ctx, 'timesheet_week', w['id'], 'approved', f'Approved {user_name(ctx, w["user_id"])}’s {week_label(w["week_start"])}{" (late)" if late else ""}')
    return late


# ------------------------------------------------------------------
# Work item picker
# ------------------------------------------------------------------
def _work_items(ctx: Ctx, user_id: str, monday: str) -> list[dict]:
    from app.routes.tasks import milestone_map, wbs_of

    ms = milestone_map(ctx)
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    out: list[dict] = []
    tasks = []
    cover: dict = {}
    for t in rows(ctx, 'tasks'):
        p = projects.get(t['project_id'])
        if p is None or p['status'] in ('completed', 'closed'):
            continue
        if t['status'] == 'done' and (not js_truthy(t.get('done_on')) or t['done_on'] < monday):
            continue
        if _task_allowed(ctx, t, user_id, monday, cover):
            tasks.append(t)

    def kind_of(t: dict) -> str:
        return 'mine' if t.get('owner_id') == user_id else 'backup' if t.get('backup_id') == user_id else 'allocated'

    tasks = sorted(tasks, key=lambda t: numeric_key(js_str(projects[t['project_id']]['code']) + wbs_of(t, ms)))
    for t in tasks:
        p = projects[t['project_id']]
        m = ms.get(t['milestone_id']) if js_truthy(t.get('milestone_id')) else None
        k = kind_of(t)
        parts = [
            p['code'],
            f'M{js_str(m["no"])} {js_str(m["name"])}' if m is not None else None,
            f'covering {user_name(ctx, t.get("owner_id"))}' if k == 'backup' else f'owner {user_name(ctx, t.get("owner_id"))}' if k == 'allocated' else None,
        ]
        out.append(
            {
                'key': f'task:{t["id"]}',
                'kind': 'task',
                'task_id': t['id'],
                'internal_code': None,
                'leave_type': None,
                'label': f'{wbs_of(t, ms)} {js_str(t["title"])}',
                'hint': ' · '.join(js_str(x) for x in parts if js_truthy(x)),
                'group': f'My tasks · {js_str(p["name"])}' if k == 'mine' else 'Tasks I’m covering' if k == 'backup' else f'Allocated · {js_str(p["name"])}',
                'wbs': wbs_of(t, ms),
                'project_code': p['code'],
            }
        )
    st = S(ctx)
    for code, label in _entries(dict(INTERNAL_CODES)):
        if code == BREAKS:
            hint = 'Counts toward the minimum' if js_truthy(st['calendar.breaks_count']) else 'Doesn’t count toward the minimum'
        else:
            hint = 'Needs a note' if js_truthy(st['timesheet.internal_note_required']) else 'Activity'
        out.append(
            {
                'key': f'internal:{code}',
                'kind': 'internal',
                'task_id': None,
                'internal_code': code,
                'leave_type': None,
                'label': label,
                'hint': hint,
                'group': 'Activities',
                'wbs': None,
                'project_code': None,
            }
        )
    for lt in LEAVE_TYPES:
        out.append(
            {
                'key': f'leave:{lt}',
                'kind': 'leave',
                'task_id': None,
                'internal_code': None,
                'leave_type': lt,
                'label': leave_label(lt),
                'hint': 'Unpaid — deducted in payroll'
                if lt == 'lwp'
                else 'Only on a listed optional holiday'
                if lt == 'optional_holiday'
                else 'Full day = minimum hours; less = half day',
                'group': 'Leave',
                'wbs': None,
                'project_code': None,
            }
        )
    return out


# ------------------------------------------------------------------
# Leave helpers
# ------------------------------------------------------------------
def _leave_entry_for(ctx: Ctx, lv: dict) -> None:
    w = _ensure_week(ctx, lv['user_id'], week_start(lv['date']))
    min_h = prop(S(ctx), 'calendar.min_hours_per_day')
    hours = min_h if lv['portion'] == 'full' else min_h / 2
    tid = ctx.tenant_id
    wid = w['id']
    d = lv['date']
    set_rows(
        'timesheet_entries',
        [e for e in get_db().timesheet_entries if not (e.get('tenant_id') == tid and e.get('week_id') == wid and e.get('kind') == 'leave' and e.get('work_date') == d)],
    )
    insert(
        ctx,
        'timesheet_entries',
        {
            'week_id': w['id'],
            'user_id': lv['user_id'],
            'work_date': lv['date'],
            'kind': 'leave',
            'task_id': None,
            'internal_code': None,
            'leave_type': lv['type'],
            'hours': hours,
            'note': 'Entered by HR' if lv['source'] == 'hr' else '',
        },
    )
    patch('timesheet_weeks', w, {'version': w['version'] + 1})


def _remove_leave_entry(ctx: Ctx, lv: dict) -> None:
    w = find_week(ctx, lv['user_id'], week_start(lv['date']))
    if w is None:
        return
    tid = ctx.tenant_id
    wid = w['id']
    d = lv['date']
    set_rows(
        'timesheet_entries',
        [e for e in get_db().timesheet_entries if not (e.get('tenant_id') == tid and e.get('week_id') == wid and e.get('kind') == 'leave' and e.get('work_date') == d)],
    )
    patch('timesheet_weeks', w, {'version': w['version'] + 1})


def _direct_reports(ctx: Ctx, me: str) -> list[dict]:
    dl = _delegators_of(ctx, me)
    return [
        u
        for u in rows(ctx, 'users')
        if u.get('status') == 'active'
        and u['id'] != me
        and (u.get('reports_to_id') == me or (js_truthy(u.get('reports_to_id')) and u['reports_to_id'] in dl))
    ]


def _stale(ctx: Ctx, w: dict, if_match: Any) -> None:
    if if_match is not UNDEF and if_match != w['version']:
        err(409, 'STALE', 'This week changed in another tab or device. Reload to see the latest version.')


# ==================================================================
# Routes
# ==================================================================
def get_week(req: Req) -> Any:
    ctx = req.ctx
    week = req.params['week']
    _assert_monday(week)
    user_id = js_or(req.q('user'), ctx.user_id)
    if not _can_view(ctx, user_id):
        err(403, 'FORBIDDEN', 'You can see only your own timesheet and those of people who report to you.')
    find(ctx, 'users', user_id, 'Person')
    if user_id == ctx.user_id:
        w = _ensure_week(ctx, user_id, week)
    else:
        w = find_week(ctx, user_id, week)
        if w is None:
            w = _ensure_week(ctx, user_id, week)
    return _build_week_view(ctx, w)


def put_week(req: Req) -> Any:
    ctx = req.ctx
    week = req.params['week']
    _assert_monday(week)
    w = edit('timesheet_weeks', _ensure_week(ctx, ctx.user_id, week))
    _assert_editable(w)
    _stale(ctx, w, req.if_match)
    _save_rows(ctx, w, req.b('rows'))
    log_activity(ctx, 'timesheet_week', w['id'], 'saved', f'Saved draft for {week_label(w["week_start"])}')
    return _build_week_view(ctx, w)


def submit(req: Req) -> Any:
    ctx = req.ctx
    week = req.params['week']
    _assert_monday(week)
    w = edit('timesheet_weeks', _ensure_week(ctx, ctx.user_id, week))
    _assert_editable(w)
    _stale(ctx, w, req.if_match)
    rws = req.b('rows')
    if isinstance(rws, list):
        _save_rows(ctx, w, rws)
    res = _submit_week(ctx, w)
    return {**_build_week_view(ctx, w), 'approver_name': res['approverName']}


def unlock_request(req: Req) -> Any:
    ctx = req.ctx
    week = req.params['week']
    _assert_monday(week)
    w = edit('timesheet_weeks', _ensure_week(ctx, ctx.user_id, week))
    if w['status'] != 'locked':
        err(409, 'TS_NOT_EDITABLE', 'Only a locked week needs unlocking.')
    reason = js_trim(js_str(coalesce(req.b('reason'), '')))
    if js_len(reason) < 5:
        err(422, 'TS_REASON_REQUIRED', 'Say why you need the week unlocked (at least 5 characters).', 'reason')
    update(ctx, 'timesheet_weeks', w['id'], {'unlock_requested_at': now_iso(), 'unlock_reason': reason})
    ap = resolve_approver(ctx, ctx.user)
    if js_truthy(ap):
        to = [ap, *_active_delegates(ctx, ap)]
    else:
        to = [u['id'] for u in _top_approvers(ctx) if u['id'] != ctx.user_id]
    snippet = _u16slice(reason, 0, 68) + '…' if js_len(reason) > 70 else reason
    me = js_str(ctx.user.get('name', UNDEF))
    for id_ in to:
        notify(ctx, id_, 'NTF-07', 'Unlock requested', f'{me} asked to unlock {week_label(w["week_start"])}: “{snippet}”', '/approvals')
    log_activity(ctx, 'timesheet_week', w['id'], 'unlock_requested', f'Requested unlock of {week_label(w["week_start"])}')
    return {**_build_week_view(ctx, w), 'approver_name': user_name(ctx, ap) if js_truthy(ap) else 'an admin'}


def copy_previous(req: Req) -> Any:
    ctx = req.ctx
    week = req.params['week']
    _assert_monday(week)
    w = edit('timesheet_weeks', _ensure_week(ctx, ctx.user_id, week))
    _assert_editable(w)
    prev_mon = add_days(week, -7)
    prev = find_week(ctx, ctx.user_id, prev_mon)
    prev_entries = [e for e in _entries_of(ctx, prev) if e['kind'] != 'leave'] if prev is not None else []
    if not prev_entries:
        err(422, 'VALIDATION', 'Last week has no task or internal rows to copy.')
    cur = _entries_of(ctx, w)
    current: list[dict] = []
    by_key: dict[str, dict] = {}
    for e in cur:
        k = _row_key(e)
        row = by_key.get(k)
        if row is None:
            row = {
                'kind': e['kind'],
                'task_id': e.get('task_id', UNDEF),
                'internal_code': e.get('internal_code', UNDEF),
                'leave_type': e.get('leave_type', UNDEF),
                'note': e.get('note', UNDEF),
                'hours': {},
                'project_id': coalesce(e.get('project_id', UNDEF), None),
            }
            by_key[k] = row
            current.append(row)
        if e['hours'] > 0:
            row['hours'][e['work_date']] = coalesce(row['hours'].get(e['work_date'], UNDEF), 0) + e['hours']
    tasks = {t['id']: t for t in rows(ctx, 'tasks')}
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    c = _user_cal(ctx, ctx.user_id)
    allow_future = prop(S(ctx), 'timesheet.allow_future_entries')
    with_hours = req.b('with_hours')
    added = 0
    skipped = 0
    cover: dict = {}
    for e in prev_entries:
        if e['kind'] == 'task':
            t = tasks.get(e['task_id']) if js_truthy(e.get('task_id')) else None
            p = projects.get(t['project_id']) if t is not None else None
            if t is None or p is None or t['status'] == 'done' or p['status'] in ('completed', 'closed') or not _task_allowed(ctx, t, ctx.user_id, week, cover):
                skipped += 1
                continue
        k = _row_key(e)
        row = by_key.get(k)
        if row is None:
            row = {
                'kind': e['kind'],
                'task_id': e.get('task_id', UNDEF),
                'internal_code': e.get('internal_code', UNDEF),
                'leave_type': None,
                'note': e.get('note', UNDEF),
                'hours': {},
                'project_id': coalesce(e.get('project_id', UNDEF), None),
            }
            by_key[k] = row
            current.append(row)
            added += 1
        if js_truthy(with_hours) and e['hours'] > 0:
            d = add_days(e['work_date'], 7)
            if (js_truthy(allow_future) or d <= ctx.today) and is_working_day(d, c) and not js_truthy(row['hours'].get(d)):
                row['hours'][d] = e['hours']
    if not added and not js_truthy(with_hours):
        err(422, 'VALIDATION', 'All of last week’s rows are already on this week.')
    _save_rows(ctx, w, current, True)
    if skipped:
        warn(ctx, 'COPY_SKIPPED', f'{skipped} entr{"y was" if skipped == 1 else "ies were"} skipped: the task is done, reassigned or its project is closed.')
    return {**_build_week_view(ctx, w), 'copied': added}


def work_items(req: Req) -> Any:
    ctx = req.ctx
    wk = js_str(coalesce(req.q('week'), week_start(ctx.today)))
    _assert_monday(wk)
    return _work_items(ctx, ctx.user_id, wk)


# ---------- approvals ----------
def list_approvals(req: Req) -> Any:
    ctx = req.ctx
    sweep_locks(ctx)
    all_ = rows(ctx, 'timesheet_weeks')
    pending: list[dict] = []
    unlocks: list[dict] = []
    for w in all_:
        if w['status'] != 'submitted' and not (w['status'] == 'locked' and js_truthy(w.get('unlock_requested_at'))):
            continue
        d = can_decide(ctx, w)
        if not d['ok']:
            continue
        if w['status'] == 'submitted':
            pending.append(_approval_item(ctx, w, d['viaDelegation']))
        else:
            unlocks.append(_approval_item(ctx, w, d['viaDelegation']))
    pending = sorted(pending, key=lambda a: (-(1 if a['overdue'] else 0), locale_key(coalesce(a['submitted_at'], ''))))
    unlocks = sorted(unlocks, key=lambda a: locale_key(coalesce(a['unlock_requested_at'], '')))
    since = add_days(ctx.today, -14)
    rec = [
        w
        for w in all_
        if w.get('decided_by') == ctx.user_id and w['user_id'] != ctx.user_id and js_truthy(w.get('decided_at')) and w['decided_at'][:10] >= since
    ]
    rec = sorted(rec, key=lambda w: locale_key(coalesce(w.get('decided_at', UNDEF), '')), reverse=True)[:20]
    recent = [{**_approval_item(ctx, w, False), 'decided_at': w.get('decided_at', UNDEF), 'late': w.get('late', UNDEF)} for w in rec]
    st = S(ctx)
    return {
        'pending': pending,
        'unlock_requests': unlocks,
        'recent': recent,
        'bulk_enabled': st['approval.bulk_approve'],
        'unlock_max_days': st['timesheet.unlock_max_days'],
        'sla_days': st['approval.sla_working_days'],
        'kpi': approval_kpi(ctx, ctx.user_id, 30),
        'can_unlock_any': timesheet_override(ctx) or len(_direct_reports(ctx, ctx.user_id)) > 0,
    }


def approvals_summary(req: Req) -> Any:
    ctx = req.ctx
    if ctx.is_super:
        return {'pending': 0, 'overdue': 0}
    pending = 0
    overdue = 0
    for w in rows(ctx, 'timesheet_weeks'):
        if w['status'] != 'submitted' and not (w['status'] == 'locked' and js_truthy(w.get('unlock_requested_at'))):
            continue
        if not can_decide(ctx, w)['ok']:
            continue
        pending += 1
        if w['status'] == 'submitted' and js_truthy(w.get('submitted_at')) and ctx.today > sla_due(ctx, w['submitted_at']):
            overdue += 1
    return {'pending': pending, 'overdue': overdue}


def approve(req: Req) -> Any:
    ctx = req.ctx
    w = _decidable(ctx, req.params['weekId'])
    late = _do_approve(ctx, w, req.if_match)
    if late:
        warn(ctx, 'APPROVAL_LATE', f'Approved after the {js_str(prop(S(ctx), "approval.sla_working_days"))}-working-day deadline. It’s recorded as a late approval.')
    return {'id': w['id'], 'late': late, 'user_name': user_name(ctx, w['user_id'])}


def reject(req: Req) -> Any:
    ctx = req.ctx
    w = _decidable(ctx, req.params['weekId'])
    if w['status'] != 'submitted':
        err(409, 'TS_NOT_EDITABLE', 'Only submitted weeks can be sent back.')
    reason = js_trim(js_str(coalesce(req.b('reason'), '')))
    if js_len(reason) < 5:
        err(422, 'TS_REASON_REQUIRED', 'Give a reason of at least 5 characters so they know what to fix.', 'reason')
    late = js_truthy(w.get('submitted_at')) and ctx.today > sla_due(ctx, w['submitted_at'])
    update(
        ctx,
        'timesheet_weeks',
        w['id'],
        {'status': 'rejected', 'decided_by': ctx.user_id, 'decided_at': now_iso(), 'late': late, 'reject_reason': reason, **wf_clear()},
        req.if_match,
    )
    me = js_str(ctx.user.get('name', UNDEF))
    notify(ctx, w['user_id'], 'NTF-06', 'Timesheet sent back', f'{me} sent back {week_label(w["week_start"])}: “{reason}”', f'/timesheet/{w["week_start"]}')
    log_activity(ctx, 'timesheet_week', w['id'], 'rejected', f'Sent back {user_name(ctx, w["user_id"])}’s {week_label(w["week_start"])}')
    return {'id': w['id'], 'late': late, 'user_name': user_name(ctx, w['user_id'])}


def bulk_approve(req: Req) -> Any:
    ctx = req.ctx
    if not js_truthy(prop(S(ctx), 'approval.bulk_approve')):
        err(403, 'TS_BULK_OFF', 'Bulk approval is switched off for this workspace. Approve weeks one at a time.')
    ids_in = req.b('ids')
    ids = ids_in if isinstance(ids_in, list) else []
    if not ids:
        err(422, 'VALIDATION', 'Select at least one week.')
    approved: list = []
    skipped: list[dict] = []
    late_count = 0
    for id_ in ids:
        w = None
        if isinstance(id_, str):
            w = find_by('timesheet_weeks', 'id', id_)
            if w is not None and w.get('tenant_id') != ctx.tenant_id:
                w = next((x for x in rows(ctx, 'timesheet_weeks') if x['id'] == id_), None)
        if w is None:
            skipped.append({'id': id_, 'reason': 'Not found'})
            continue
        if w['user_id'] == ctx.user_id or not can_decide(ctx, w)['ok']:
            skipped.append({'id': id_, 'reason': 'Not yours to approve'})
            continue
        if w['status'] != 'submitted':
            skipped.append({'id': id_, 'reason': 'No longer waiting'})
            continue
        below = sum(1 for d in _day_totals(ctx, w) if d['min_required'] > 0 and d['counted'] < d['min_required'])
        if below:
            skipped.append({'id': id_, 'reason': f'{user_name(ctx, w["user_id"])}: {below} short day{"s" if below > 1 else ""} — review it individually'})
            continue
        w = edit('timesheet_weeks', w)
        if _do_approve(ctx, w):
            late_count += 1
        approved.append(id_)
    if skipped:
        n = len(skipped)
        warn(ctx, 'BULK_SKIPPED', f'{n} week{"s were" if n > 1 else " was"} skipped. {"; ".join(js_str(s["reason"]) for s in skipped)}.')
    if late_count:
        warn(ctx, 'APPROVAL_LATE', f'{late_count} approval{"s were" if late_count > 1 else " was"} past the deadline and recorded as late.')
    return {'approved': approved, 'skipped': skipped}


def _unlock_week(ctx: Ctx, w: dict, body: Any) -> dict:
    """Unlock a locked week for N days. Works on the person's request or on the manager's own initiative
    (then a reason is required — it goes to the person and the audit log)."""
    if w['status'] != 'locked':
        err(409, 'TS_NOT_EDITABLE', 'Only locked weeks can be unlocked.')
    mx = prop(S(ctx), 'timesheet.unlock_max_days')
    days = js_number(body.get('days', UNDEF) if isinstance(body, dict) else UNDEF)
    if not is_integer(days) or days < 1 or days > mx:
        err(422, 'TS_UNLOCK_RANGE', f'Unlock for 1 to {js_str(mx)} days.', 'days')
    reason = js_trim(js_str(coalesce(body.get('reason', UNDEF) if isinstance(body, dict) else UNDEF, '')))
    if not js_truthy(w.get('unlock_requested_at')) and js_len(reason) < 5:
        err(422, 'TS_REASON_REQUIRED', 'Say why you are reopening this week (at least 5 characters). They’ll see it.', 'reason')
    until = add_days(ctx.today, int(days) - 1)
    why = js_or(reason, w.get('unlock_reason'), '')
    update(ctx, 'timesheet_weeks', w['id'], {'status': 'open', 'unlock_until': until, 'unlock_requested_at': None, 'unlock_reason': None})
    me = js_str(ctx.user.get('name', UNDEF))
    quote = f': “{_u16slice(reason, 0, 80)}”' if reason else ''
    notify(
        ctx,
        w['user_id'],
        'NTF-08',
        'Timesheet unlocked',
        f'{me} unlocked {week_label(w["week_start"])} until {_fmt_date(until)}{quote}. Fill it in and submit before then.',
        f'/timesheet/{w["week_start"]}',
    )
    log_activity(
        ctx,
        'timesheet_week',
        w['id'],
        'unlocked',
        f'Unlocked {user_name(ctx, w["user_id"])}’s {week_label(w["week_start"])} until {until}{f" — {js_str(why)}" if js_truthy(why) else ""}',
    )
    return {'id': w['id'], 'unlock_until': until, 'user_name': user_name(ctx, w['user_id'])}


def unlock(req: Req) -> Any:
    ctx = req.ctx
    return _unlock_week(ctx, _decidable(ctx, req.params['weekId']), req.body)


def unlock_week_route(req: Req) -> Any:
    """A week that ended without a timesheet at all: create it (it locks immediately) and unlock it."""
    ctx = req.ctx
    user_id = js_str(coalesce(req.b('user_id'), ''))
    week = js_str(coalesce(req.b('week_start'), ''))
    _assert_monday(week)
    person = find(ctx, 'users', user_id, 'Person')
    if person['id'] == ctx.user_id:
        err(403, 'TS_SELF_APPROVAL', 'You can’t approve or unlock your own timesheet. Your manager does that.')
    if add_days(week, 6) >= ctx.today:
        err(409, 'TS_NOT_EDITABLE', 'This week hasn’t ended, so it isn’t locked.')
    w = _ensure_week(ctx, person['id'], week)
    return _unlock_week(ctx, _decidable(ctx, w['id']), req.body)


def _n_weeks(raw: Any, default: int, lo: int, hi: int) -> Any:
    return js_min(hi, js_max(lo, js_number(coalesce(raw, default))))


def _week_list(this_mon: str, n: Any) -> list[str]:
    """Array.from({ length: n }, (_, i) => addDays(thisMon, -7 * (n - 1 - i)))"""
    if not is_finite(n) or n <= 0:
        return []
    ln = int(n)
    return [add_days(this_mon, -7 * (n - 1 - i)) for i in range(ln)]


def team_approvals(req: Req) -> Any:
    ctx = req.ctx
    sweep_locks(ctx)
    n = _n_weeks(req.q('weeks'), 4, 4, 8)
    this_mon = week_start(ctx.today)
    weeks = _week_list(this_mon, n)
    override = timesheet_override(ctx)
    dl = _delegators_of(ctx, ctx.user_id)
    people = []
    for u in rows(ctx, 'users'):
        if u.get('status') != 'active' or u['id'] == ctx.user_id or js_truthy(u.get('is_super_admin')):
            continue
        ap = resolve_approver(ctx, u)
        if ap == ctx.user_id or (js_truthy(ap) and ap in dl) or (override and (u.get('reports_to_id', UNDEF) is not None or ap is not None)):
            people.append((u, ap))
    by_key: dict[tuple, dict] = {}
    for x in rows(ctx, 'timesheet_weeks'):
        by_key.setdefault((x['user_id'], x['week_start']), x)
    hours: dict[str, Any] = {}
    for e in rows(ctx, 'timesheet_entries'):
        hours[e['week_id']] = coalesce(hours.get(e['week_id'], UNDEF), 0) + e['hours']
    lock_on = prop(S(ctx), 'timesheet.weekly_lock')
    people = sorted(people, key=lambda p: (0 if p[1] == ctx.user_id else 1, locale_key(p[0]['name'])))
    out = []
    for u, ap in people:
        cells = []
        for wk in weeks:
            w = by_key.get((u['id'], wk))
            ended = add_days(wk, 6) < ctx.today
            if w is not None:
                status = w['status']
            elif ended:
                status = 'locked' if js_truthy(lock_on) else 'open'
            else:
                status = 'upcoming' if wk > ctx.today else 'open'
            uu = w.get('unlock_until') if w is not None else None
            cells.append(
                {
                    'week_start': wk,
                    'status': status,
                    'week_id': w['id'] if w is not None else None,
                    'hours': r2(coalesce(hours.get(w['id'], UNDEF), 0) if w is not None else 0),
                    'unlock_requested': js_truthy(w.get('unlock_requested_at')) if w is not None else False,
                    'unlock_reason': coalesce(w.get('unlock_reason', UNDEF), None) if w is not None else None,
                    'unlocked_until': uu if w is not None and w['status'] == 'open' and js_truthy(uu) and uu >= ctx.today else None,
                    'can_unlock': status == 'locked' and not ctx.view_as,
                }
            )
        out.append(
            {
                'user': {'id': u['id'], 'name': u.get('name', UNDEF), 'title': u.get('title', UNDEF), 'avatar_hue': u.get('avatar_hue', UNDEF)},
                'approver_name': user_name(ctx, ap) if js_truthy(ap) else None,
                'via_delegation': js_truthy(ap) and ap != ctx.user_id and ap in dl,
                'cells': cells,
            }
        )
    return {'weeks': weeks, 'people': out, 'unlock_max_days': prop(S(ctx), 'timesheet.unlock_max_days')}


def deny_unlock(req: Req) -> Any:
    ctx = req.ctx
    w = _decidable(ctx, req.params['weekId'])
    if w['status'] != 'locked' or not js_truthy(w.get('unlock_requested_at')):
        err(409, 'TS_NOT_EDITABLE', 'There is no open unlock request on this week.')
    reason = js_trim(js_str(coalesce(req.b('reason'), '')))
    if js_len(reason) < 5:
        err(422, 'TS_REASON_REQUIRED', 'Give a reason of at least 5 characters.', 'reason')
    update(ctx, 'timesheet_weeks', w['id'], {'unlock_requested_at': None, 'unlock_reason': None})
    me = js_str(ctx.user.get('name', UNDEF))
    notify(ctx, w['user_id'], 'NTF-08', 'Unlock request declined', f'{me} declined unlocking {week_label(w["week_start"])}: “{reason}”', f'/timesheet/{w["week_start"]}')
    log_activity(ctx, 'timesheet_week', w['id'], 'unlock_denied', f'Declined unlock for {user_name(ctx, w["user_id"])}’s {week_label(w["week_start"])}')
    return {'id': w['id'], 'user_name': user_name(ctx, w['user_id'])}


# ---------- team grid (UI-37) ----------
def team_grid(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'team.view'):
        err(403, 'FORBIDDEN', 'The team view is for admins and project managers.')
    sweep_locks(ctx)
    n = _n_weeks(req.q('weeks'), 8, 4, 12)
    this_mon = week_start(ctx.today)
    weeks = _week_list(this_mon, n)
    users = [u for u in rows(ctx, 'users') if u.get('status') == 'active']
    me = ctx.user_id
    if has(ctx, 'people.view_all'):
        scope_ = users
    else:
        my_projects = {p['id'] for p in rows(ctx, 'projects') if p.get('pm_id') == me}
        owners = {t.get('owner_id') for t in rows(ctx, 'tasks') if t['project_id'] in my_projects}
        scope_ = [u for u in users if u['id'] == me or u.get('reports_to_id') == me or u['id'] in owners]
    by_key: dict[tuple, dict] = {}
    for x in rows(ctx, 'timesheet_weeks'):
        by_key.setdefault((x['user_id'], x['week_start']), x)
    hours_by_week: dict[str, Any] = {}
    task_hours_by_week: dict[str, Any] = {}
    for e in rows(ctx, 'timesheet_entries'):
        hours_by_week[e['week_id']] = coalesce(hours_by_week.get(e['week_id'], UNDEF), 0) + e['hours']
        if e['kind'] == 'task':
            task_hours_by_week[e['week_id']] = coalesce(task_hours_by_week.get(e['week_id'], UNDEF), 0) + e['hours']
    min_h = prop(S(ctx), 'calendar.min_hours_per_day')
    c = cal(ctx)
    worked_days = sum(1 for d in week_days(this_mon) if d <= ctx.today and is_working_day(d, c))
    scope_ = sorted(scope_, key=lambda u: locale_key(u['name']))
    people = []
    for u in scope_:
        cells = []
        for wk in weeks:
            w = by_key.get((u['id'], wk))
            ended = add_days(wk, 6) < ctx.today
            status = w['status'] if w is not None else 'missing' if ended else 'upcoming'
            cells.append(
                {
                    'week_start': wk,
                    'status': status,
                    'hours': r2(coalesce(hours_by_week.get(w['id'], UNDEF), 0) if w is not None else 0),
                    'week_id': w['id'] if w is not None else None,
                    'late': js_truthy(w.get('late')) if w is not None else False,
                }
            )
        cur = by_key.get((u['id'], this_mon))
        leave_days: Any = 0
        for lv in _leave_of(ctx, u['id']):
            if lv['date'] >= this_mon and lv['date'] <= ctx.today and is_working_day(lv['date'], c):
                leave_days = leave_days + (1 if lv['portion'] == 'full' else 0.5)
        capacity = js_max(0, worked_days - leave_days) * min_h
        task_h = coalesce(task_hours_by_week.get(cur['id'], UNDEF), 0) if cur is not None else 0
        people.append(
            {
                'user': {
                    'id': u['id'],
                    'name': u.get('name', UNDEF),
                    'title': u.get('title', UNDEF),
                    'avatar_hue': u.get('avatar_hue', UNDEF),
                    'role': u.get('role', UNDEF),
                    'reports_to_id': u.get('reports_to_id', UNDEF),
                },
                'cells': cells,
                'utilisation': r2(task_h / capacity) if capacity > 0 else None,
                'hours_this_week': r2(coalesce(hours_by_week.get(cur['id'], UNDEF), 0) if cur is not None else 0),
            }
        )
    # Approval KPI per approver (BR-11) over the grid's window; credited to whoever decided (delegates included).
    managers = []
    wide = has(ctx, 'people.view_all')
    scope_ids = {s['id'] for s in scope_}
    users_by_id: dict[str, dict] = {}
    for u in users:
        users_by_id.setdefault(u['id'], u)
    for k in approval_kpis(ctx, diff_days(ctx.today, weeks[0])).values():
        if not wide and k['user_id'] != me and k['user_id'] not in scope_ids:
            continue
        ap = users_by_id.get(k['user_id'])
        if ap is None:
            continue
        managers.append(
            {
                'user': {'id': ap['id'], 'name': ap.get('name', UNDEF), 'avatar_hue': ap.get('avatar_hue', UNDEF)},
                'decided': k['decided'],
                'on_time': k['on_time'],
                'late': k['late'],
                'overdue': k['overdue_now'],
                'pending': k['overdue_now'] + k['waiting_within'],
                'score': -k['lapses'],
            }
        )
    mgr = sorted(managers, key=lambda m: (-m['score'], -m['decided']))
    return {'weeks': weeks, 'people': people, 'managers': mgr}


# ---------- daily updates (UI-40) ----------
def get_daily(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user_id
    date = js_str(coalesce(req.q('date'), ctx.today))
    every = rows(ctx, 'daily_updates')
    # a demo-only row (no end-of-day text yet) isn't an update
    all_ = [d for d in every if js_truthy(d.get('done'))]
    mine = next((d for d in all_ if d['user_id'] == me and d['date'] == date), None)
    recent = sorted((d for d in all_ if d['user_id'] == me), key=lambda d: locale_key(d['date']), reverse=True)[:10]
    team = None
    reports = _direct_reports(ctx, me)
    wide = has(ctx, 'people.view_all')
    if wide or has(ctx, 'team.view') or reports:
        people = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and u['id'] != me] if wide else reports
        leave_on = {lv['user_id'] for lv in rows(ctx, 'leave') if lv['date'] == date and lv['portion'] == 'full'}
        team = [
            {
                'user': {'id': u['id'], 'name': u.get('name', UNDEF), 'title': u.get('title', UNDEF), 'avatar_hue': u.get('avatar_hue', UNDEF)},
                'update': next((d for d in all_ if d['user_id'] == u['id'] and d['date'] == date), None),
                'on_leave': u['id'] in leave_on,
            }
            for u in sorted(people, key=lambda u: locale_key(u['name']))
        ]
    ws = week_start(date)
    demo_date = demo_date_of(ctx, ws, me)
    demo_row = next((d for d in every if d['user_id'] == me and d['date'] == demo_date), None)
    demo = {
        'date': demo_date,
        'week_start': ws,
        'shown': js_truthy(demo_row.get('demo_shown')) if demo_row is not None else False,
        'note': coalesce(demo_row.get('demo_note', UNDEF), '') if demo_row is not None else '',
        'editable': not ctx.view_as and ws == week_start(ctx.today) and demo_date <= ctx.today,
    }
    return {'date': date, 'mine': mine, 'recent': recent, 'proof_required': prop(S(ctx), 'daily.proof_required'), 'team': team, 'demo': demo}


def post_demo(req: Req) -> Any:
    """Friday demo (Feature Guide §8): one per week, stored on the week's demo day (Friday, or the working day before it)."""
    ctx = req.ctx
    me = ctx.user_id
    anchor = js_str(coalesce(req.b('date'), ctx.today))
    if week_start(anchor) != week_start(ctx.today):
        err(422, 'VALIDATION', 'The demo can be recorded for this week only.', 'date')
    date = demo_date_of(ctx, week_start(anchor), me)
    if date > ctx.today:
        err(422, 'VALIDATION', f'Record the demo on {_fmt_date(date, "long")}, the week’s demo day.', 'date')
    shown = js_truthy(req.b('demo_shown'))
    note = _u16slice(js_trim(js_str(coalesce(req.b('demo_note'), ''))), 0, 300)
    if shown and js_len(note) < 5:
        err(422, 'VALIDATION', 'Say what was demoed (at least a few words).', 'demo_note')
    cur = next((d for d in rows(ctx, 'daily_updates') if d['user_id'] == me and d['date'] == date), None)
    pt = {'demo_shown': shown, 'demo_note': note if shown else ''}
    if cur is not None:
        saved = update(ctx, 'daily_updates', cur['id'], pt)
    else:
        saved = insert(ctx, 'daily_updates', {'user_id': me, 'date': date, 'done': '', 'proof_url': '', 'blocker': '', **pt})
    nm = js_str(ctx.user.get('name', UNDEF))
    log_activity(
        ctx,
        'daily_update',
        saved['id'],
        'demo',
        f'{nm} showed working software: {note}' if shown else f'{nm} recorded no demo for the week of {_fmt_date(week_start(date))}',
    )
    return saved


def post_daily(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user_id
    date = js_str(coalesce(req.b('date'), ctx.today))
    if date > ctx.today:
        err(422, 'VALIDATION', 'You can’t post an update for a future day.', 'date')
    if date < week_start(ctx.today):
        err(422, 'VALIDATION', 'Updates can be posted or edited for this week only.', 'date')
    done = js_trim(js_str(coalesce(req.b('done'), '')))
    if js_len(done) < 5:
        err(422, 'VALIDATION', 'Write what you finished today (at least a sentence).', 'done')
    proof = js_trim(js_str(coalesce(req.b('proof_url'), '')))
    if js_truthy(prop(S(ctx), 'daily.proof_required')) and not proof:
        err(422, 'DU_PROOF_REQUIRED', 'Add a proof link — a commit, screenshot or demo URL. No proof, no progress.', 'proof_url')
    if proof and not _PROOF_RE.search(proof):
        err(422, 'VALIDATION', 'Links must start with http:// or https://', 'proof_url')
    cur = next((d for d in rows(ctx, 'daily_updates') if d['user_id'] == me and d['date'] == date), None)
    ds = req.b('demo_shown')
    pt = {
        'done': done,
        'proof_url': proof,
        'blocker': js_trim(js_str(coalesce(req.b('blocker'), ''))),
        'demo_shown': (coalesce(cur.get('demo_shown', UNDEF), False) if cur is not None else False) if ds is UNDEF else js_truthy(ds),
    }
    if cur is not None:
        saved = update(ctx, 'daily_updates', cur['id'], pt)
    else:
        saved = insert(ctx, 'daily_updates', {'user_id': me, 'date': date, **pt})
    if pt['blocker'] and not (cur is not None and js_truthy(cur.get('blocker'))):
        ap = resolve_approver(ctx, ctx.user)
        if js_truthy(ap):
            log_activity(ctx, 'daily_update', saved['id'], 'blocker', f'{js_str(ctx.user.get("name", UNDEF))} flagged a blocker: {pt["blocker"]}')
    return saved


# ---------- leave ----------
def list_leave(req: Req) -> Any:
    ctx = req.ctx
    me = ctx.user_id
    hr = has(ctx, 'leave.hr_entry')
    reports = _direct_reports(ctx, me)
    scope_ = js_or(req.q('scope'), 'mine')
    see_all = hr or has(ctx, 'people.view_all')
    if scope_ == 'all' and not see_all:
        scope_ = 'team' if reports else 'mine'
    if scope_ == 'team' and not reports and not hr:
        scope_ = 'mine'
    frm = js_str(coalesce(req.q('from'), add_days(ctx.today, -120)))
    to = js_str(coalesce(req.q('to'), add_days(ctx.today, 180)))
    ids = {me} if scope_ == 'mine' else {me, *(u['id'] for u in reports)} if scope_ == 'team' else None
    qu = req.q('user')
    weeks_by: dict[tuple, dict] = {}
    for x in rows(ctx, 'timesheet_weeks'):
        weeks_by.setdefault((x['user_id'], x['week_start']), x)
    sel = [
        lv
        for lv in rows(ctx, 'leave')
        if (ids is None or lv['user_id'] in ids) and lv['date'] >= frm and lv['date'] <= to and (not js_truthy(qu) or lv['user_id'] == qu)
    ]
    sel = sorted(sel, key=lambda lv: locale_key(lv['date']), reverse=True)
    items = []
    for lv in sel:
        w = weeks_by.get((lv['user_id'], week_start(lv['date'])))
        stt = apply_lock(ctx, w)['status'] if w is not None else None
        own = lv['user_id'] == me
        can_del = stt != 'approved' and (hr or (own and (not stt or stt == 'open' or stt == 'rejected')))
        items.append(
            {
                'id': lv['id'],
                'user_id': lv['user_id'],
                'user_name': user_name(ctx, lv['user_id']),
                'date': lv['date'],
                'type': lv['type'],
                'portion': lv['portion'],
                'source': lv['source'],
                'week_status': stt,
                'can_delete': can_del and not ctx.view_as,
            }
        )
    holidays = [{'date': h['date'], 'name': h['name'], 'optional': h.get('optional', UNDEF)} for h in rows(ctx, 'holidays') if h['date'] >= frm and h['date'] <= to]
    return {'items': items, 'holidays': holidays, 'can_hr_entry': hr, 'can_see_all': see_all, 'scope': scope_}


def post_leave(req: Req) -> Any:
    ctx = req.ctx
    hr = has(ctx, 'leave.hr_entry')
    user_id = js_str(js_or(req.b('user_id'), ctx.user_id))
    for_other = user_id != ctx.user_id
    if for_other and not hr:
        err(403, 'FORBIDDEN', 'Only admins and operations can enter leave for someone else.')
    person = find(ctx, 'users', user_id, 'Person')
    pname = js_str(person.get('name', UNDEF))
    if person.get('status') != 'active':
        err(422, 'VALIDATION', f'{pname} is not an active user.')
    typ = req.b('type')
    if not (isinstance(typ, str) and typ in LEAVE_TYPES):
        err(422, 'VALIDATION', 'Choose a leave type.', 'type')
    portion = 'half' if req.b('portion') == 'half' else 'full'
    frm = js_str(coalesce(req.b('from'), ''))
    to = js_str(js_or(req.b('to'), frm))
    if not _DATE_RE.match(frm) or not _DATE_RE.match(to):
        err(422, 'VALIDATION', 'Pick the leave dates.', 'from')
    if to < frm:
        err(422, 'DATE_RANGE', 'The end date is before the start date.', 'to')
    if diff_days(to, frm) > 60:
        err(422, 'DATE_RANGE', 'Enter at most 60 days at a time.', 'to')
    if portion == 'half' and to != frm:
        err(422, 'LEAVE_INVALID', 'A half day covers one date. Pick a single day.', 'to')
    c = _no_leave(cal(ctx))
    existing = {lv['date'] for lv in _leave_of(ctx, user_id)}
    dates: list[str] = []
    offs = 0
    d = frm
    while d <= to:
        if not is_working_day(d, c):
            offs += 1
            d = add_days(d, 1)
            continue
        if d in existing:
            err(422, 'LEAVE_INVALID', f'{pname} already has leave on {_fmt_date(d, "long")}.', 'from')
        if typ == 'optional_holiday' and optional_holiday_on(ctx, d) is None:
            err(
                422,
                'LEAVE_INVALID',
                f'{_fmt_date(d, "long")} isn’t an optional holiday. Pick a date from the optional holiday list, or another leave type.',
                'from',
            )
        w = find_week(ctx, user_id, week_start(d))
        if w is not None and w['status'] == 'approved':
            err(409, 'TS_NOT_EDITABLE', f'The week of {_fmt_date(week_start(d))} is already approved. Ask the approver to send it back first.', 'from')
        if not for_other and w is not None and not _is_editable(w):
            err(
                409,
                'TS_LOCKED' if w['status'] == 'locked' else 'TS_NOT_EDITABLE',
                f'The week of {_fmt_date(week_start(d))} is {w["status"]}. {"Ask your manager to unlock it first." if w["status"] == "locked" else "Leave can’t be added to it now."}',
                'from',
            )
        dates.append(d)
        d = add_days(d, 1)
    if not dates:
        err(
            422,
            'LEAVE_INVALID',
            f'{_fmt_date(frm, "long")} is not a working day, so it can’t be a leave day.' if frm == to else 'Every day in that range is a weekly off or holiday.',
            'from',
        )
    for d in dates:
        lv = insert(
            ctx,
            'leave',
            {'user_id': user_id, 'date': d, 'type': typ, 'portion': portion, 'source': 'hr' if for_other else 'timesheet', 'approved_by': ctx.user_id if for_other else None},
        )
        _leave_entry_for(ctx, lv)
    if offs:
        warn(ctx, 'LEAVE_SKIPPED_OFFS', f'{offs} weekly off or holiday day{"s were" if offs > 1 else " was"} skipped.')
    if typ == 'optional_holiday':
        pol = next((p for p in rows(ctx, 'leave_policies') if p['type'] == 'optional_holiday'), None)
        if pol is not None:
            fm = prop(S(ctx), 'company.fy_start_month')
            fy = fy_range(fy_start_year(dates[0], fm), fm)
            used = sum(1 for lv in _leave_of(ctx, user_id) if lv['type'] == 'optional_holiday' and lv['date'] >= fy['from'] and lv['date'] <= fy['to'])
            if used + len(dates) > pol['annual_days']:
                ad = pol['annual_days']
                err(
                    422,
                    'LEAVE_BALANCE',
                    f'{pname} can take {js_str(ad)} optional holiday{"" if ad == 1 else "s"} a year and has used {used}.',
                    'type',
                )
    n = len(dates)
    log_activity(ctx, 'leave', user_id, 'created', f'{"HR entered" if for_other else "Entered"} {n} day{"s" if n > 1 else ""} of {leave_label(typ)} for {pname}')
    return {'created': n}


def leave_summary(req: Req) -> Any:
    """Leave taken this financial year by person × type (Feature Guide §16). Days from timesheet = leave hours ÷ minimum hours."""
    ctx = req.ctx
    hr = has(ctx, 'leave.hr_entry')
    reports = _direct_reports(ctx, ctx.user_id)
    see_all = hr or has(ctx, 'people.view_all')
    if not see_all and not reports:
        err(403, 'FORBIDDEN', 'The leave summary is for HR and managers.')
    fm = prop(S(ctx), 'company.fy_start_month')
    qfy = req.q('fy')
    fy_start = js_number(qfy) if js_truthy(qfy) else fy_start_year(ctx.today, fm)
    rng = fy_range(fy_start, fm)
    frm, to = rng['from'], rng['to']
    min_h = prop(S(ctx), 'calendar.min_hours_per_day')
    if see_all:
        people = [u for u in rows(ctx, 'users') if u.get('status') != 'invited' and not js_truthy(u.get('is_super_admin'))]
    else:
        people = [ctx.user, *reports]
    people = sorted(people, key=lambda u: locale_key(u['name']))
    leave = [lv for lv in rows(ctx, 'leave') if lv['date'] >= frm and lv['date'] <= to]
    entries = [e for e in rows(ctx, 'timesheet_entries') if e['kind'] == 'leave' and e['work_date'] >= frm and e['work_date'] <= to]
    types = ['casual', 'sick', 'earned', 'compensatory', 'lwp', 'optional_holiday']
    leave_by: dict[str, list] = {}
    for lv in leave:
        leave_by.setdefault(lv['user_id'], []).append(lv)
    ent_by: dict[str, list] = {}
    for e in entries:
        ent_by.setdefault(e['user_id'], []).append(e)
    out = []
    for u in people:
        mine = leave_by.get(u['id'], [])
        by_type = {t: sum(1 for lv in mine if lv['type'] == t and lv['portion'] == 'full') for t in types}
        half_days = sum(1 for lv in mine if lv['portion'] == 'half') * 0.5
        hs: Any = 0
        for e in ent_by.get(u['id'], []):
            hs = hs + e['hours']
        timesheet_days = r2(hs / min_h)
        tot: Any = 0
        for lv in mine:
            tot = tot + (0.5 if lv['portion'] == 'half' else 1)
        total = r2(tot)
        if total > 0 or timesheet_days > 0:
            out.append(
                {
                    'user': {'id': u['id'], 'name': u.get('name', UNDEF), 'title': u.get('title', UNDEF), 'avatar_hue': u.get('avatar_hue', UNDEF)},
                    'by_type': by_type,
                    'half_days': half_days,
                    'timesheet_days': timesheet_days,
                    'total': total,
                }
            )
    return {'fy_label': fy_label(fy_start), 'from': frm, 'to': to, 'min_hours': min_h, 'types': types, 'rows': out}


def delete_leave(req: Req) -> Any:
    ctx = req.ctx
    lv = find(ctx, 'leave', req.params['id'], 'Leave')
    hr = has(ctx, 'leave.hr_entry')
    own = lv['user_id'] == ctx.user_id
    if not own and not hr:
        err(403, 'FORBIDDEN', 'Only admins and operations can remove someone else’s leave.')
    w = find_week(ctx, lv['user_id'], week_start(lv['date']))
    if w is not None and w['status'] == 'approved':
        err(409, 'TS_NOT_EDITABLE', 'That week is approved, so its leave can’t change.')
    if not hr and w is not None and not _is_editable(w):
        err(409, 'TS_LOCKED' if w['status'] == 'locked' else 'TS_NOT_EDITABLE', f'That week is {w["status"]}; its leave can’t change now.')
    if not hr and lv.get('source') == 'hr':
        err(403, 'FORBIDDEN', 'This leave was entered by HR. Ask them to change it.')
    _remove_leave_entry(ctx, lv)
    remove(ctx, 'leave', lv['id'])
    log_activity(ctx, 'leave', lv['user_id'], 'deleted', f'Removed leave on {lv["date"]} for {user_name(ctx, lv["user_id"])}')
    return {'ok': True}


def holidays_lite(req: Req) -> Any:
    ctx = req.ctx
    frm = js_str(coalesce(req.q('from'), f'{ctx.today[:4]}-01-01'))
    to = js_str(coalesce(req.q('to'), f'{ctx.today[:4]}-12-31'))
    hs = sorted((h for h in rows(ctx, 'holidays') if h['date'] >= frm and h['date'] <= to), key=lambda h: locale_key(h['date']))
    return [{'date': h['date'], 'name': h['name'], 'optional': h.get('optional', UNDEF)} for h in hs]


def register(r: Any) -> None:
    # ---------- timesheet week ----------
    r.get('/timesheets/:week', get_week)
    r.put('/timesheets/:week', put_week)
    r.post('/timesheets/:week/submit', submit)
    r.post('/timesheets/:week/unlock-request', unlock_request)
    r.post('/timesheets/:week/copy-previous', copy_previous)
    r.get('/timesheet/work-items', work_items)
    # ---------- approvals ----------
    r.get('/approvals', list_approvals)
    r.get('/approvals/summary', approvals_summary)
    r.post('/approvals/:weekId/approve', approve)
    r.post('/approvals/:weekId/reject', reject)
    r.post('/approvals/bulk-approve', bulk_approve)
    r.post('/approvals/:weekId/unlock', unlock)
    r.post('/approvals/unlock-week', unlock_week_route)
    r.get('/approvals/team', team_approvals)
    r.post('/approvals/:weekId/deny-unlock', deny_unlock)
    # ---------- team grid ----------
    r.get('/team/grid', team_grid)
    # ---------- daily updates ----------
    r.get('/daily', get_daily)
    r.post('/daily/demo', post_demo)
    r.post('/daily', post_daily)
    # ---------- leave ----------
    r.get('/leave', list_leave)
    r.post('/leave', post_leave)
    r.get('/leave/summary', leave_summary)
    r.delete('/leave/:id', delete_leave)
    r.get('/holidays-lite', holidays_lite)

