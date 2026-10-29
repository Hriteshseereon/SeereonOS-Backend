"""Teams (v9, decision D6) — a master entity (name, description, lead, optional department, members) and the
People › Team overview built on it.

Routes
* GET    /teams                 the list (master page, pickers, team documents) — master.view | team.view |
                                people.view_all | documents.employee_files
* POST   /teams                 create — master.edit
* PATCH  /teams/:id             change (incl. active on/off) — master.edit
* DELETE /teams/:id             remove for good — master.edit (no record references a team)
* GET    /teams/overview?week=  People › Team: team cards → members → info → this week's timesheet status and hours
                                vs target, team totals. Falls back to departments when the company has no active team.
                                people.view_all sees every team and every person's week; team.view sees the teams they
                                lead or belong to, and the week of the people they lead or manage (others: status hidden).

Timesheet facts follow the Feature Guide rules used by the team grid (routes/timesheets.py): the week's status is the
timesheet_weeks row (missing once the week has ended without one); hours are the logged non-leave hours; the target is
the minimum hours per day (settings calendar.min_hours_per_day) on every working day of the person's calendar, less
leave (half days count half); a short day is a working day before today with less than its target logged.
"""

from __future__ import annotations

from typing import Any

from app.ctx import (
    Ctx,
    Req,
    err,
    find,
    has,
    has_any,
    insert,
    log_activity,
    remove,
    require_action,
    rows,
    update,
    user_name,
)
from app.domain.calendar import CalendarCtx, add_days, is_real_iso_date, is_working_day, week_days, week_start
from app.engine.holiday_cal import holidays_for_user
from app.js import coalesce, js_len, js_round, js_str, js_trim, js_truthy, locale_key, prop

LIST_ACTIONS = ('master.view', 'team.view', 'people.view_all', 'documents.employee_files')


def _r2(n: Any) -> Any:
    return js_round(n * 100) / 100


def _active_users(ctx: Ctx) -> list[dict]:
    return [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin'))]


def _name_of(ctx: Ctx, table: str, id_: Any) -> str:
    if not js_truthy(id_):
        return ''
    x = next((r for r in rows(ctx, table) if r['id'] == id_), None)
    return js_str(coalesce(prop(x, 'name'), ''))


def team_view(ctx: Ctx, t: dict) -> dict[str, Any]:
    users = {u['id']: u for u in rows(ctx, 'users')}
    members = [m for m in t.get('member_ids') or [] if m in users]
    return {
        **t,
        'lead_name': user_name(ctx, t.get('lead_id')) if js_truthy(t.get('lead_id')) else None,
        'department_name': _name_of(ctx, 'departments', t.get('department_id')) or None,
        'member_count': len(members),
        'active_member_count': sum(1 for m in members if users[m].get('status') == 'active'),
    }


def _validate(ctx: Ctx, b: Any, cur: dict | None) -> dict[str, Any]:
    bb = b if isinstance(b, dict) else {}
    out: dict[str, Any] = {}
    partial = cur is not None
    if not partial or 'name' in bb:
        n = js_trim(bb.get('name')) if isinstance(bb.get('name'), str) else ''
        if js_len(n) < 2 or js_len(n) > 80:
            err(422, 'VALIDATION', 'Give the team a name of 2 to 80 characters.', 'name')
        low = n.lower()
        if any(x['name'].lower() == low and (cur is None or x['id'] != cur['id']) for x in rows(ctx, 'teams')):
            err(409, 'DUPLICATE', f'There is already a team called {n}.', 'name')
        out['name'] = n
    if not partial or 'description' in bb:
        d = js_trim(bb.get('description')) if isinstance(bb.get('description'), str) else ''
        if js_len(d) > 500:
            err(422, 'VALIDATION', 'Keep the description under 500 characters.', 'description')
        out['description'] = d
    active_ids = {u['id'] for u in _active_users(ctx)}
    if not partial or 'lead_id' in bb:
        lid = bb.get('lead_id') or None
        if lid is not None and lid not in active_ids:
            err(422, 'VALIDATION', 'Choose an active person as the team lead.', 'lead_id')
        out['lead_id'] = lid
    if not partial or 'department_id' in bb:
        did = bb.get('department_id') or None
        if did is not None:
            dep = next((x for x in rows(ctx, 'departments') if x['id'] == did), None)
            if dep is None or (not dep.get('active') and (cur is None or cur.get('department_id') != did)):
                err(422, 'VALIDATION', 'Choose an active department.', 'department_id')
        out['department_id'] = did
    if not partial or 'member_ids' in bb:
        ms = bb.get('member_ids', [])
        if not isinstance(ms, list) or any(not isinstance(m, str) for m in ms):
            err(422, 'VALIDATION', 'Choose the members of the team.', 'member_ids')
        keep_old = set(cur.get('member_ids') or []) if cur else set()
        seen: list[str] = []
        for m in ms:
            if m in seen:
                continue
            if m not in active_ids and m not in keep_old:  # someone deactivated later may stay on the list
                err(422, 'VALIDATION', 'Members must be active people of the company.', 'member_ids')
            seen.append(m)
        if len(seen) > 200:
            err(422, 'VALIDATION', 'A team has at most 200 members.', 'member_ids')
        out['member_ids'] = seen
    if 'active' in bb:
        if not isinstance(bb['active'], bool):
            err(422, 'VALIDATION', 'Choose active or inactive.', 'active')
        out['active'] = bb['active']
    elif not partial:
        out['active'] = True
    # the lead is part of the team: add them to the members when they are not there yet
    lead = out.get('lead_id', cur.get('lead_id') if cur else None)
    members = out.get('member_ids', list(cur.get('member_ids') or []) if cur else [])
    if lead and lead not in members:
        out['member_ids'] = [lead, *members]
    return out


def list_teams(req: Req) -> Any:
    ctx = req.ctx
    if not has_any(ctx, *LIST_ACTIONS):
        err(403, 'FORBIDDEN', 'Teams are visible to managers, HR and admins.')
    return sorted((team_view(ctx, t) for t in rows(ctx, 'teams')), key=lambda t: (not t['active'], locale_key(t['name'])))


def create_team(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'master.edit', 'Only admins and HR manage teams.')
    t = insert(ctx, 'teams', _validate(ctx, req.body, None))
    log_activity(ctx, 'team', t['id'], 'team.created', f'Team {t["name"]} created ({len(t["member_ids"])} members)')
    return team_view(ctx, t)


def patch_team(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'master.edit', 'Only admins and HR manage teams.')
    t = find(ctx, 'teams', req.params['id'], 'Team')
    out = update(ctx, 'teams', t['id'], _validate(ctx, req.body, t), req.if_match)
    what = 'activated' if req.b('active') is True and not t.get('active') else 'deactivated' if req.b('active') is False and t.get('active') else 'updated'
    log_activity(ctx, 'team', t['id'], f'team.{what}', f'Team {out["name"]} {what}')
    return team_view(ctx, out)


def delete_team(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'master.edit', 'Only admins and HR manage teams.')
    t = find(ctx, 'teams', req.params['id'], 'Team')
    remove(ctx, 'teams', t['id'], req.if_match)
    log_activity(ctx, 'team', t['id'], 'team.deleted', f'Team {t["name"]} deleted')
    return None


# ---------------------------------------------------------------------------------------------------------------------
# People › Team overview
# ---------------------------------------------------------------------------------------------------------------------
def _week_facts(ctx: Ctx, u: dict, monday: str, weeks: dict, hours: dict, leave_by_user: dict, min_h: Any, leave_hours: dict | None = None) -> dict[str, Any]:
    # the person's own holiday calendar (their location, v9 A5) — the same days their timesheet expects; leave is
    # handled below (portion), so no leave dates in the calendar
    c = CalendarCtx(ctx.tenant.get('settings'), holidays_for_user(ctx.tenant_id, u['id']))
    w = weeks.get((u['id'], monday))
    ended = add_days(monday, 6) < ctx.today
    status = w['status'] if w is not None else 'missing' if ended else 'not_started'
    lv = {x['date']: x['portion'] for x in leave_by_user.get(u['id'], ())}
    days = []
    target = 0.0
    target_to_date = 0.0
    logged = 0.0
    short = 0
    for d in week_days(monday):
        h = hours.get((u['id'], d), 0)  # work hours: what a short day is judged on (leave already lowers the need)
        lh = (leave_hours or {}).get((u['id'], d), 0)  # leave rows: counted in the logged total like the timesheet's
        logged += h + lh
        working = is_working_day(d, c) and d >= js_str(coalesce(u.get('joined_on'), '0000'))
        portion = lv.get(d)
        need = 0.0
        if working:
            need = 0.0 if portion == 'full' else min_h * (0.5 if portion == 'half' else 1)
        target += need
        past = d < ctx.today
        if past:
            target_to_date += need
        is_short = bool(working and past and need > 0 and h < need)
        short += 1 if is_short else 0
        days.append({'date': d, 'hours': _r2(h + lh), 'working': working, 'leave': portion, 'target': _r2(need), 'short': is_short})
    if w is None and target == 0:
        status = 'not_due'  # no working day for them this week (before joining, all leave or holidays): nothing to submit
    return {
        'week_start': monday,
        'status': status,
        'week_id': w['id'] if w is not None else None,
        'submitted_at': coalesce(prop(w, 'submitted_at'), None) if w is not None else None,
        'late': js_truthy(w.get('late')) if w is not None else False,
        'hours': _r2(logged),
        'target': _r2(target),
        'target_to_date': _r2(target_to_date),
        'short_days': short,
        'days': days,
    }


def overview(req: Req) -> Any:
    ctx = req.ctx
    wide = has(ctx, 'people.view_all')
    if not wide and not has(ctx, 'team.view'):
        err(403, 'FORBIDDEN', 'The team overview is for managers, HR and admins.')
    qw = req.q('week')
    monday = week_start(qw if isinstance(qw, str) and is_real_iso_date(qw) else ctx.today)
    if monday > week_start(ctx.today):
        err(422, 'VALIDATION', 'Pick this week or an earlier one.', 'week')
    me = ctx.user_id
    users = _active_users(ctx)
    by_id = {u['id']: u for u in users}
    min_h = coalesce(prop(ctx.tenant['settings'], 'calendar.min_hours_per_day'), 8)
    # one pass over the week's rows
    days = set(week_days(monday))
    weeks = {(w['user_id'], w['week_start']): w for w in rows(ctx, 'timesheet_weeks') if w.get('week_start') == monday}
    hours: dict = {}
    leave_hours: dict = {}  # leave rows: in the week total like the timesheet and Timesheet approvals show it
    for e in rows(ctx, 'timesheet_entries'):
        if e.get('work_date') in days:
            k = (e['user_id'], e['work_date'])
            bucket = leave_hours if e.get('kind') == 'leave' else hours
            bucket[k] = bucket.get(k, 0) + e['hours']
    leave_by_user: dict = {}
    for lv in rows(ctx, 'leave'):
        if lv.get('date') in days:
            leave_by_user.setdefault(lv['user_id'], []).append(lv)
    teams = [t for t in rows(ctx, 'teams') if t.get('active')]
    mode = 'teams' if teams else 'departments'
    groups: list[dict[str, Any]] = []
    if teams:
        for t in sorted(teams, key=lambda x: locale_key(x['name'])):
            ids = [m for m in t.get('member_ids') or [] if m in by_id]
            groups.append(
                {
                    'id': t['id'],
                    'kind': 'team',
                    'name': t['name'],
                    'description': t.get('description') or '',
                    'lead_id': t.get('lead_id'),
                    'department_name': _name_of(ctx, 'departments', t.get('department_id')) or None,
                    'member_ids': ids,
                }
            )
        in_team = {m for g in groups for m in g['member_ids']}
        rest = [u['id'] for u in users if u['id'] not in in_team]
        if rest and wide:
            groups.append(
                {
                    'id': '_none',
                    'kind': 'unassigned',
                    'name': 'Not in a team',
                    'description': 'Active people who are not a member of any team.',
                    'lead_id': None,
                    'department_name': None,
                    'member_ids': rest,
                }
            )
    else:
        deps = {d['id']: d for d in rows(ctx, 'departments')}
        by_dep: dict = {}
        for u in users:
            by_dep.setdefault(u.get('department_id') if u.get('department_id') in deps else None, []).append(u['id'])
        for did, ids in sorted(by_dep.items(), key=lambda kv: (kv[0] is None, locale_key(deps[kv[0]]['name']) if kv[0] else '')):
            d = deps.get(did) if did else None
            groups.append(
                {
                    'id': did or '_none',
                    'kind': 'department' if d else 'unassigned',
                    'name': d['name'] if d else 'No department',
                    'description': '',
                    'lead_id': d.get('head_id') if d else None,
                    'department_name': None,
                    'member_ids': ids,
                }
            )
    # scope: wide sees all; otherwise the groups I lead or belong to, and the people I lead or manage
    reports = {u['id'] for u in users if u.get('reports_to_id') == me}
    out_groups = []
    for g in groups:
        mine = wide or g['lead_id'] == me or me in g['member_ids'] or any(m in reports for m in g['member_ids'])
        if not mine:
            continue
        can_see_week = lambda uid_, g=g: wide or g['lead_id'] == me or uid_ in reports or uid_ == me  # noqa: E731
        members = []
        for uid_ in g['member_ids']:
            u = by_id[uid_]
            rt = u.get('reports_to_id')
            members.append(
                {
                    'id': u['id'],
                    'name': prop(u, 'name'),
                    'title': js_str(coalesce(_name_of(ctx, 'designations', u.get('designation_id')) or None, prop(u, 'title'), '')),
                    'employee_code': coalesce(prop(u, 'employee_code'), None),
                    'email': prop(u, 'email'),
                    'phone': coalesce(prop(u, 'phone'), None),
                    'avatar_hue': prop(u, 'avatar_hue'),
                    'manager_id': rt if js_truthy(rt) else None,
                    'manager_name': user_name(ctx, rt) if js_truthy(rt) else None,
                    'location': _name_of(ctx, 'locations', u.get('location_id')) or None,
                    'department': _name_of(ctx, 'departments', u.get('department_id')) or None,
                    'joined_on': prop(u, 'joined_on'),
                    'is_lead': uid_ == g['lead_id'],
                    'week': _week_facts(ctx, u, monday, weeks, hours, leave_by_user, min_h, leave_hours) if can_see_week(uid_) else None,
                }
            )
        members.sort(key=lambda m: (not m['is_lead'], locale_key(js_str(m['name']))))
        seen = [m['week'] for m in members if m['week'] is not None]
        out_groups.append(
            {
                **{k: v for k, v in g.items() if k != 'member_ids'},
                'lead_name': user_name(ctx, g['lead_id']) if js_truthy(g['lead_id']) else None,
                'members': members,
                'totals': {
                    'members': len(members),
                    'visible': len(seen),
                    'approved': sum(1 for w in seen if w['status'] == 'approved'),
                    'submitted': sum(1 for w in seen if w['status'] == 'submitted'),
                    'draft': sum(1 for w in seen if w['status'] == 'open'),
                    'not_started': sum(1 for w in seen if w['status'] == 'not_started'),
                    'missing': sum(1 for w in seen if w['status'] in ('missing', 'locked', 'rejected')),
                    'short_people': sum(1 for w in seen if w['short_days'] > 0),
                    'hours': _r2(sum(w['hours'] for w in seen)),
                    'target': _r2(sum(w['target'] for w in seen)),
                    'target_to_date': _r2(sum(w['target_to_date'] for w in seen)),
                },
            }
        )
    allw = [m['week'] for g in out_groups for m in g['members'] if m['week'] is not None]
    uniq = {m['id']: m['week'] for g in out_groups for m in g['members'] if m['week'] is not None}
    return {
        'week_start': monday,
        'this_week': week_start(ctx.today),
        'mode': mode,
        'min_hours_per_day': min_h,
        'can_manage': has(ctx, 'master.edit'),
        'groups': out_groups,
        'totals': {
            'groups': len(out_groups),
            'people': len(uniq),
            'hours': _r2(sum(w['hours'] for w in uniq.values())),
            'target_to_date': _r2(sum(w['target_to_date'] for w in uniq.values())),
            'missing': sum(1 for w in uniq.values() if w['status'] in ('missing', 'locked', 'rejected')),
            'awaiting_approval': sum(1 for w in uniq.values() if w['status'] == 'submitted'),
            'short_people': sum(1 for w in uniq.values() if w['short_days'] > 0),
        }
        if allw
        else {'groups': len(out_groups), 'people': 0, 'hours': 0, 'target_to_date': 0, 'missing': 0, 'awaiting_approval': 0, 'short_people': 0},
    }


def register(r: Any) -> None:
    r.get('/teams', list_teams)
    r.get('/teams/overview', overview)  # before /teams/:id patterns of other methods (GET has no /teams/:id)
    r.post('/teams', create_team)
    r.patch('/teams/:id', patch_team)
    r.delete('/teams/:id', delete_team)
