"""Maker-checker — port of src/mock/makerChecker.ts (engine + its routes; OpenAPI tag "makerChecker").

Sensitive changes are proposed by a maker and applied only after a different person (the checker) approves. Route
contract (every guarded write): validate input -> build a proposal -> `return propose(ctx, proposal)`. propose()
either applies immediately (maker-checker off for that entity) and returns {pending: False, result, result_id} or
stores a change set and returns {pending: True, change}. The applier registered for the entity performs the real
write in both cases.

    from app.engine.maker_checker import register_applier, propose, assert_base_version, pending_brief

    def apply_role(ctx, cs):            # cs = the change set row (dict); cs['id'] == 'cs_direct' for a direct apply
        ...
        return {'result': role, 'result_id': role['id']}
    register_applier('role', apply_role)   # at import time of the route module (like the TS)

Integrity rules: the maker never checks their own change (409 MAKER_CANNOT_CHECK); the checker needs
makerchecker.check AND the change's required_permission; one pending change per record (409 CHANGE_PENDING); a record
changed after the proposal fails on approval (409 STALE); rejection needs a reason (>= 10 chars); nobody eligible to
check -> 409 NO_CHECKER.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    calendar_for,
    err,
    find,
    has,
    insert,
    log_activity,
    notify,
    now_iso,
    patch,
    permissions_of,
    require_action,
    rows,
    scope,
    update,
    user_name,
    warn,
)
from app.domain.calendar import working_days_between
from app.domain.permission_catalog import PERMISSION_BY_KEY
from app.errors import ApiError
from app.js import UNDEF, coalesce, is_integer, js_join, js_max, js_min, js_number, js_or, js_str, js_trim, js_truthy, locale_compare, locale_key, nullish, prop

CHANGE_ENTITY_LABEL = cat('types/access')['CHANGE_ENTITY_LABEL']
_MC = cat('mock/makerChecker')
DEFAULT_MC = _MC['DEFAULT_MC']
RECOMMENDED_MC_ENTITIES = _MC['RECOMMENDED_MC_ENTITIES']
REQUIRED_PERMISSION_OF = _MC['REQUIRED_PERMISSION_OF']
MC_ENTITY_GROUPS = _MC['MC_ENTITY_GROUPS']

Applier = Callable[[Ctx, dict], dict]
_appliers: dict[str, Applier] = {}


def register_applier(entity: str, fn: Applier) -> None:
    """register the function that performs a change of this entity (called on direct apply and on approval)"""
    _appliers[entity] = fn


def mc_config(ctx: Ctx) -> Any:
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    return coalesce(prop(t, 'maker_checker'), DEFAULT_MC)


def needs_checker(ctx: Ctx, entity: str) -> bool:
    c = mc_config(ctx)
    return bool(c.get('enabled')) and (entity == 'makerchecker_config' or entity in c.get('entities', []))


def _perm_label(key: Any) -> str:
    p = PERMISSION_BY_KEY.get(key)
    return p['label'] if p and p.get('label') is not None else js_str(key)


def eligible_checkers(ctx: Ctx, perm: str, maker_id: str) -> list[dict]:
    """people (other than the maker) who could approve a change needing `perm`"""
    out = []
    for u in rows(ctx, 'users'):
        if u.get('status') == 'active' and not u.get('is_super_admin') and u['id'] != maker_id:
            p = permissions_of(u)
            if 'makerchecker.check' in p and perm in p:
                out.append(u)
    return out


def run_applier(ctx: Ctx, cs: dict) -> dict:
    fn = _appliers.get(cs['entity'])
    if fn is None:
        err(500, 'NO_APPLIER', f'No handler is registered for {cs["entity"]} changes.')
    return fn(ctx, cs)  # type: ignore[misc]


def propose(ctx: Ctx, p: dict[str, Any]) -> dict[str, Any]:
    """p = {entity, entity_id, action, summary, before?, after?, payload, required_permission, base_version?}"""
    draft = {
        'id': 'cs_direct',
        'tenant_id': ctx.tenant_id,
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'version': 1,
        'entity': p['entity'],
        'entity_id': p['entity_id'],
        'action': p['action'],
        'summary': p['summary'],
        'before': coalesce(p.get('before', UNDEF), None),
        'after': coalesce(p.get('after', UNDEF), None),
        'payload': p['payload'],
        'maker_id': ctx.user_id,
        'required_permission': p['required_permission'],
        'base_version': coalesce(p.get('base_version', UNDEF), None),
        'status': 'approved',
        'checker_id': None,
        'decided_at': None,
        'reason': None,
        'applied_at': None,
        'result_id': None,
        'error': None,
    }
    if not needs_checker(ctx, p['entity']):
        r = run_applier(ctx, draft)
        return {'pending': False, 'result': r.get('result', UNDEF), 'result_id': coalesce(r.get('result_id', UNDEF), None)}
    if p['entity_id'] and any(
        c.get('status') == 'pending' and c.get('entity') == p['entity'] and c.get('entity_id') == p['entity_id'] for c in rows(ctx, 'change_sets')
    ):
        err(409, 'CHANGE_PENDING', 'A change to this record is already waiting for approval. Withdraw it or wait for the checker.')
    checkers = eligible_checkers(ctx, p['required_permission'], ctx.user_id)
    if not checkers:
        err(
            409,
            'NO_CHECKER',
            f'Maker-checker is on for {CHANGE_ENTITY_LABEL[p["entity"]].lower()}, but no one else can approve it. Give a second person “Check changes” and “{_perm_label(p["required_permission"])}”, or change the maker-checker settings.',
        )
    rest = {k: v for k, v in draft.items() if k not in ('id', 'tenant_id', 'created_at', 'updated_at', 'version')}
    cs = insert(ctx, 'change_sets', {**rest, 'status': 'pending'})
    for u in checkers:
        notify(ctx, u['id'], 'NTF-04', 'Change to approve', f'{js_str(ctx.user.get("name", UNDEF))}: {p["summary"]}', '/changes')
    log_activity(ctx, 'change_set', cs['id'], 'proposed', f'{js_str(ctx.user.get("name", UNDEF))} proposed: {p["summary"]}')
    return {'pending': True, 'change': view(ctx, cs)}


def assert_base_version(cs: dict, current_version: Any) -> None:
    """throws STALE when the record moved on since the proposal (use inside appliers)"""
    if cs['id'] != 'cs_direct' and not nullish(cs.get('base_version')) and not nullish(current_version) and current_version != cs['base_version']:
        err(409, 'STALE', 'The record changed after this was proposed. Reject it and ask the maker to propose again.')


def view(ctx: Ctx, c: dict) -> dict[str, Any]:
    pending = c['status'] == 'pending'
    age = js_max(0, working_days_between(c['created_at'][:10], ctx.today, calendar_for(ctx)) - 1) if pending else 0
    maker = next((u for u in rows(ctx, 'users') if u['id'] == c['maker_id']), None)
    return {
        **c,
        'entity_label': coalesce(CHANGE_ENTITY_LABEL.get(c['entity'], UNDEF), c['entity']),
        'maker_name': user_name(ctx, c['maker_id']),
        'maker_hue': coalesce(maker.get('avatar_hue', UNDEF) if maker else UNDEF, 150),
        'maker_title': coalesce(maker.get('title', UNDEF) if maker else UNDEF, ''),
        'checker_name': user_name(ctx, c['checker_id']) if c.get('checker_id') else None,
        'required_permission_label': _perm_label(c['required_permission']),
        'can_check': pending and not ctx.view_as and c['maker_id'] != ctx.user_id and has(ctx, 'makerchecker.check') and c['required_permission'] in ctx.perms,
        'can_withdraw': pending and not ctx.view_as and c['maker_id'] == ctx.user_id,
        'overdue': pending and age > mc_config(ctx)['sla_days'],
        'age_days': age,
    }


def pending_change_for(ctx: Ctx, entity: str, entity_id: Any) -> dict | None:
    """the pending change waiting on a record"""
    if not entity_id:
        return None
    return next((c for c in rows(ctx, 'change_sets') if c.get('status') == 'pending' and c.get('entity') == entity and c.get('entity_id') == entity_id), None)


def pending_brief(ctx: Ctx, entity: str, entity_id: Any) -> dict | None:
    """pending change in a light shape for record screens"""
    c = pending_change_for(ctx, entity, entity_id)
    if not c:
        return None
    checkers = eligible_checkers(ctx, c['required_permission'], c['maker_id'])
    return {
        'id': c['id'],
        'summary': c['summary'],
        'maker_id': c['maker_id'],
        'maker_name': user_name(ctx, c['maker_id']),
        'created_at': c['created_at'],
        'checker_names': [u.get('name', UNDEF) for u in checkers],
        'mine': c['maker_id'] == ctx.user_id,
        'can_check': not ctx.view_as and c['maker_id'] != ctx.user_id and has(ctx, 'makerchecker.check') and c['required_permission'] in ctx.perms,
    }


def can_see_all(ctx: Ctx) -> bool:
    return has(ctx, 'makerchecker.check') or has(ctx, 'audit.view') or has(ctx, 'makerchecker.configure')


def changes_to_check(ctx: Ctx) -> list[dict]:
    return [v for v in (view(ctx, c) for c in rows(ctx, 'change_sets') if c.get('status') == 'pending') if v['can_check']]


# ---------------------------------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------------------------------
def list_changes(req: Req) -> Any:
    ctx = req.ctx
    status = js_str(coalesce(req.q('status'), 'pending'))
    scope_ = js_str(coalesce(req.q('scope'), 'all' if can_see_all(ctx) else 'mine'))
    lst = list(rows(ctx, 'change_sets'))
    if not can_see_all(ctx) or scope_ == 'mine':
        lst = [c for c in lst if c['maker_id'] == ctx.user_id]
    if status != 'all':
        lst = [c for c in lst if (c['status'] != 'pending' if status == 'decided' else c['status'] == status)]
    ent = req.q('entity')
    if js_truthy(ent):
        lst = [c for c in lst if c['entity'] == ent]
    out = [view(ctx, c) for c in lst]
    if scope_ == 'to_check':
        out = [c for c in out if c['can_check']]
    return sorted(out, key=lambda c: locale_key(c['created_at']), reverse=True)


def summary(req: Req) -> Any:
    ctx = req.ctx
    return {
        'to_check': len(changes_to_check(ctx)),
        'mine_pending': sum(1 for c in rows(ctx, 'change_sets') if c.get('status') == 'pending' and c.get('maker_id') == ctx.user_id),
        'enabled': mc_config(ctx)['enabled'],
    }


def get_change(req: Req) -> Any:
    ctx = req.ctx
    c = find(ctx, 'change_sets', req.params['id'], 'Change')
    if c['maker_id'] != ctx.user_id and not can_see_all(ctx):
        err(404, 'NOT_FOUND', 'Change not found.')
    return view(ctx, c)


def approve(req: Req) -> Any:
    ctx = req.ctx
    c = find(ctx, 'change_sets', req.params['id'], 'Change')
    if c['status'] != 'pending':
        err(409, 'INVALID_STATE', f'This change is already {js_str(c["status"])}.')
    if c['maker_id'] == ctx.user_id:
        err(409, 'MAKER_CANNOT_CHECK', 'You proposed this change, so someone else must approve it.')
    require_action(ctx, 'makerchecker.check', 'You can’t approve changes.')
    if c['required_permission'] not in ctx.perms:
        err(403, 'FORBIDDEN', f'Approving this needs “{_perm_label(c["required_permission"])}”.')
    note = js_trim(js_str(coalesce(prop(req.body, 'note') if isinstance(req.body, dict) else UNDEF, ''))) or None
    try:
        res = run_applier(ctx, c)
        update(
            ctx,
            'change_sets',
            c['id'],
            {
                'status': 'approved',
                'checker_id': ctx.user_id,
                'decided_at': now_iso(),
                'reason': note,
                'applied_at': now_iso(),
                'result_id': coalesce(res.get('result_id', UNDEF), c.get('entity_id', UNDEF)),
                'error': None,
            },
        )
    except Exception as e:  # noqa: BLE001 — the TS catches everything, records the failure and rethrows
        msg = e.message if isinstance(e, ApiError) else str(e)
        update(ctx, 'change_sets', c['id'], {'status': 'failed', 'checker_id': ctx.user_id, 'decided_at': now_iso(), 'error': msg})
        notify(ctx, c['maker_id'], 'NTF-05', 'Change could not be applied', f'{c["summary"]} — {msg}', '/changes')
        raise
    notify(ctx, c['maker_id'], 'NTF-05', 'Change approved', f'{js_str(ctx.user.get("name", UNDEF))} approved: {c["summary"]}', '/changes')
    log_activity(
        ctx,
        'change_set',
        c['id'],
        'approved',
        f'{js_str(ctx.user.get("name", UNDEF))} approved {user_name(ctx, c["maker_id"])}’s change: {c["summary"]}{" — " + note if note else ""}',
    )
    return view(ctx, find(ctx, 'change_sets', c['id']))


def reject(req: Req) -> Any:
    ctx = req.ctx
    c = find(ctx, 'change_sets', req.params['id'], 'Change')
    if c['status'] != 'pending':
        err(409, 'INVALID_STATE', f'This change is already {js_str(c["status"])}.')
    if c['maker_id'] == ctx.user_id:
        err(409, 'MAKER_CANNOT_CHECK', 'You proposed this change. Withdraw it instead.')
    require_action(ctx, 'makerchecker.check', 'You can’t decide changes.')
    if c['required_permission'] not in ctx.perms:
        err(403, 'FORBIDDEN', f'Deciding this needs “{_perm_label(c["required_permission"])}”.')
    reason = js_trim(js_str(coalesce(prop(req.body, 'reason') if isinstance(req.body, dict) else UNDEF, '')))
    if len(reason) < 10:
        err(422, 'REASON_REQUIRED', 'Tell the maker why (at least 10 characters).', 'reason')
    out = update(ctx, 'change_sets', c['id'], {'status': 'rejected', 'checker_id': ctx.user_id, 'decided_at': now_iso(), 'reason': reason})
    notify(ctx, c['maker_id'], 'NTF-05', 'Change rejected', f'{js_str(ctx.user.get("name", UNDEF))} rejected: {c["summary"]} — {reason}', '/changes')
    log_activity(
        ctx,
        'change_set',
        c['id'],
        'rejected',
        f'{js_str(ctx.user.get("name", UNDEF))} rejected {user_name(ctx, c["maker_id"])}’s change: {c["summary"]} — {reason}',
    )
    return view(ctx, out)


def withdraw(req: Req) -> Any:
    ctx = req.ctx
    c = find(ctx, 'change_sets', req.params['id'], 'Change')
    if c['maker_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person who proposed a change can withdraw it.')
    if c['status'] != 'pending':
        err(409, 'INVALID_STATE', f'This change is already {js_str(c["status"])}.')
    out = update(ctx, 'change_sets', c['id'], {'status': 'withdrawn', 'decided_at': now_iso()})
    log_activity(ctx, 'change_set', c['id'], 'withdrawn', f'{js_str(ctx.user.get("name", UNDEF))} withdrew: {c["summary"]}')
    return view(ctx, out)


def checkers(req: Req) -> Any:
    ctx = req.ctx
    if not has(ctx, 'makerchecker.configure') and not has(ctx, 'makerchecker.check'):
        err(403, 'FORBIDDEN', 'You can’t see maker-checker settings.')
    cfg = mc_config(ctx)
    all_users = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not u.get('is_super_admin')]
    ents = [e for g in MC_ENTITY_GROUPS for e in g['entities']] + ['makerchecker_config']
    import functools

    out = []
    for entity in ents:
        perm = REQUIRED_PERMISSION_OF[entity]
        holders = [u for u in all_users if (lambda p: 'makerchecker.check' in p and perm in p)(permissions_of(u))]
        holders = sorted(holders, key=functools.cmp_to_key(lambda a, b: locale_compare(a['name'], b['name'])))
        out.append(
            {
                'entity': entity,
                'label': CHANGE_ENTITY_LABEL[entity],
                'required_permission': perm,
                'required_label': _perm_label(perm),
                'enabled': bool(cfg['enabled']) and (entity == 'makerchecker_config' or entity in cfg['entities']),
                'recommended': entity in RECOMMENDED_MC_ENTITIES,
                'checkers': [{'id': u['id'], 'name': u['name'], 'title': u.get('title', UNDEF), 'hue': u.get('avatar_hue', UNDEF)} for u in holders],
                'others': sum(1 for u in holders if u['id'] != ctx.user_id),
            }
        )
    return out


def get_config(req: Req) -> Any:
    return {**mc_config(req.ctx), 'recommended': RECOMMENDED_MC_ENTITIES, 'entity_labels': CHANGE_ENTITY_LABEL}


def put_config(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'makerchecker.configure', 'You can’t change maker-checker settings.')
    cur = mc_config(ctx)
    sla_in = js_number(coalesce(req.b('sla_days'), cur['sla_days']))
    if not is_integer(sla_in) or sla_in < 1 or sla_in > 10:
        err(422, 'VALIDATION', 'The checking deadline must be 1 to 10 working days.', 'sla_days')
    ents_in = req.b('entities')
    ents_list = list(dict.fromkeys(ents_in if isinstance(ents_in, list) else ([] if nullish(ents_in) else list(ents_in) if isinstance(ents_in, str) else [])))
    nxt = {
        'enabled': js_truthy(req.b('enabled')),
        'entities': [e for e in ents_list if isinstance(e, str) and e in CHANGE_ENTITY_LABEL],
        'sla_days': js_max(1, js_min(10, js_or(js_number(coalesce(req.b('sla_days'), cur['sla_days'])), 2))),
    }
    if nxt['enabled'] and not eligible_checkers(ctx, 'makerchecker.check', ctx.user_id):
        err(409, 'NO_CHECKER', 'Turn this on only when at least one other person has “Check changes”. Otherwise no one could approve your changes.')
    parts: list[str] = []
    if bool(cur['enabled']) != nxt['enabled']:
        parts.append('turn maker-checker on' if nxt['enabled'] else 'turn maker-checker off')
    added = [CHANGE_ENTITY_LABEL[e] for e in nxt['entities'] if e not in cur['entities']]
    removed = [CHANGE_ENTITY_LABEL.get(e, UNDEF) for e in cur['entities'] if e not in nxt['entities']]
    if added:
        parts.append('check ' + ', '.join(added))
    if removed:
        parts.append('stop checking ' + js_join(removed, ', '))
    if cur['sla_days'] != nxt['sla_days']:
        parts.append(f'SLA {js_str(cur["sla_days"])} → {js_str(nxt["sla_days"])} days')
    if not parts:
        err(422, 'NO_CHANGE', 'Nothing changed.')
    if nxt['enabled']:
        lonely = [e for e in nxt['entities'] if not eligible_checkers(ctx, REQUIRED_PERMISSION_OF[e], ctx.user_id)]
        if lonely:
            warn(
                ctx,
                'NO_CHECKER_FOR',
                f'Nobody else can check {", ".join(CHANGE_ENTITY_LABEL[e].lower() for e in lonely)} yet — those changes will be refused until someone has the permission.',
            )
    return propose(
        ctx,
        {
            'entity': 'makerchecker_config',
            'entity_id': ctx.tenant_id,
            'action': 'update',
            'summary': 'Maker-checker: ' + '; '.join(parts),
            'before': dict(cur),
            'after': dict(nxt),
            'payload': {'config': nxt},
            'required_permission': 'makerchecker.configure',
        },
    )


def _apply_mc_config(ctx: Ctx, cs: dict) -> dict:
    t = scope().find_by('tenants', 'id', ctx.tenant_id)
    t = patch('tenants', t, {'maker_checker': cs['payload']['config'], 'version': t['version'] + 1})  # type: ignore[index, arg-type]
    by = f' (proposed by {user_name(ctx, cs["maker_id"])})' if cs['id'] != 'cs_direct' else ''
    log_activity(ctx, 'settings', t['id'], 'maker_checker', f'Maker-checker settings updated{by}')
    return {'result': t['maker_checker'], 'result_id': t['id']}


register_applier('makerchecker_config', _apply_mc_config)


def register(r: Any) -> None:
    r.get('/changes', list_changes)
    r.get('/changes/summary', summary)
    r.get('/changes/:id', get_change)
    r.post('/changes/:id/approve', approve)
    r.post('/changes/:id/reject', reject)
    r.post('/changes/:id/withdraw', withdraw)
    r.get('/maker-checker/checkers', checkers)
    r.get('/maker-checker/config', get_config)
    r.put('/maker-checker/config', put_config)
