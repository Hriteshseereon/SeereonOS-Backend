"""Port of src/mock/routes/admin.ts — platform super admin (UI-24). Sees tenants, never tenant business data.
These routes run in GLOBAL mode (all tenants' rows; super-admin sessions only)."""

from __future__ import annotations

from typing import Any

from app.ctx import Ctx, Req, err, get_db, now_iso, patch, push, uid
from app.domain.calendar import add_days, diff_days, today_iso
from app.js import UNDEF, coalesce, js_str, js_trim, locale_key
from app.routes.billing import GST_PCT, PLANS, seat_limit


def tenant_row(t: dict) -> dict[str, Any]:
    d = get_db()
    users = [u for u in d.users if u.get('tenant_id') == t['id']]
    sub = next((s for s in d.subscriptions if s.get('tenant_id') == t['id']), None)
    p = next((x for x in PLANS if x['code'] == t.get('plan_code')), None)
    mrr = p['price_per_user_paise'] * t['seats'] if t.get('status') == 'active' and p else 0
    logins = sorted(u['last_login_at'] for u in users if u.get('last_login_at'))  # .filter(Boolean).sort() (code-unit order)
    last_login = logins[-1] if logins else None
    return {
        'id': t['id'],
        'name': t.get('name', UNDEF),
        'slug': t.get('slug', UNDEF),
        'status': t.get('status', UNDEF),
        'industry': t.get('industry', UNDEF),
        'plan_code': t.get('plan_code', UNDEF),
        'plan_name': p['name'] if p else t.get('plan_code', UNDEF),
        'seats': t.get('seats', UNDEF),
        'seat_limit': seat_limit(t),
        'users_active': sum(1 for u in users if u.get('status') == 'active'),
        'users_total': len(users),
        'created_at': t.get('created_at', UNDEF),
        'trial_ends_on': t.get('trial_ends_on', UNDEF),
        'trial_days_left': diff_days(t['trial_ends_on'], today_iso()) if t.get('trial_ends_on') else None,
        'subscription_status': sub['status'] if sub else None,
        'period_end': sub['current_period_end'] if sub else None,
        'mrr_paise': mrr,
        'last_activity_at': last_login,
    }


def audit(ctx: Ctx, tenant_id: str, action: str, reason: str) -> None:
    push('admin_audit', {'id': 'aud_' + uid(), 'actor_id': ctx.user_id, 'tenant_id': tenant_id, 'action': action, 'reason': reason, 'at': now_iso()})


def tenant_by_id(id_: Any) -> dict:
    t = next((x for x in get_db().tenants if x['id'] == id_), None)
    if t is None:
        err(404, 'NOT_FOUND', 'Tenant not found.')
    return t  # type: ignore[return-value]


def reason_of(req: Req) -> str:
    r = js_trim(js_str(coalesce(req.b('reason'), '')))
    if len(r) < 5:
        err(422, 'VALIDATION', 'Give a reason of at least 5 characters. It goes into the audit log.', 'reason')
    return r


def list_tenants(req: Req) -> Any:
    lst = sorted((tenant_row(t) for t in get_db().tenants), key=lambda x: locale_key(x['created_at']), reverse=True)

    def by(s: str) -> int:
        return sum(1 for t in lst if t['status'] == s)

    return {
        'tenants': lst,
        'kpis': {
            'total': len(lst),
            'trial': by('trial'),
            'active': by('active'),
            'read_only': by('read_only'),
            'suspended': by('suspended'),
            'mrr_paise': sum(t['mrr_paise'] for t in lst),
            'gst_pct': GST_PCT,
            'seats_paid': sum(t['seats'] for t in lst if t['status'] == 'active'),
            'trials_ending_7d': sum(1 for t in lst if t['status'] == 'trial' and t['trial_days_left'] is not None and t['trial_days_left'] <= 7),
        },
    }


def extend(req: Req) -> Any:
    from app.js import is_integer, js_number

    t = tenant_by_id(req.params['id'])
    days = js_number(req.b('days'))
    if not is_integer(days) or days < 1 or days > 30:
        err(422, 'VALIDATION', 'Extend by 1 to 30 days.', 'days')
    reason = reason_of(req)
    if t['status'] == 'active':
        err(409, 'TENANT_ACTIVE', 'This tenant is on a paid plan; there is no trial to extend.')
    if t['status'] == 'suspended':
        err(409, 'TENANT_SUSPENDED', 'Reactivate the tenant before extending its trial.')
    today = today_iso()
    base_date = t['trial_ends_on'] if t.get('trial_ends_on') and t['trial_ends_on'] > today else today
    t = patch('tenants', t, {'trial_ends_on': add_days(base_date, int(days)), 'status': 'trial', 'version': t['version'] + 1})
    sub = next((s for s in get_db().subscriptions if s.get('tenant_id') == t['id']), None)
    if sub:
        patch('subscriptions', sub, {'status': 'trialing', 'current_period_end': t['trial_ends_on']})
    audit(req.ctx, t['id'], f'extend_trial:+{js_str(days)}d', reason)
    return tenant_row(t)


def suspend(req: Req) -> Any:
    t = tenant_by_id(req.params['id'])
    if t['status'] == 'suspended':
        err(409, 'TENANT_SUSPENDED', 'This tenant is already suspended.')
    reason = reason_of(req)
    t = patch('tenants', t, {'status': 'suspended', 'version': t['version'] + 1})
    d = get_db()
    ids = {u['id'] for u in d.users if u.get('tenant_id') == t['id']}
    d.sessions = [s for s in d.sessions if s['user_id'] not in ids]
    audit(req.ctx, t['id'], 'suspend', reason)
    return tenant_row(t)


def reactivate(req: Req) -> Any:
    t = tenant_by_id(req.params['id'])
    if t['status'] not in ('suspended', 'read_only'):
        err(409, 'TENANT_ACTIVE', 'Only suspended or read-only tenants can be reactivated.')
    reason = reason_of(req)
    sub = next((s for s in get_db().subscriptions if s.get('tenant_id') == t['id']), None)
    today = today_iso()
    paid = t.get('plan_code') != 'trial' and sub is not None and sub['status'] == 'active' and sub['current_period_end'] >= today
    ch: dict[str, Any] = {}
    if paid:
        ch['status'] = 'active'
    else:
        ch['status'] = 'trial'
        if not t.get('trial_ends_on') or t['trial_ends_on'] < today:
            ch['trial_ends_on'] = add_days(today, 7)
        if sub:
            patch('subscriptions', sub, {'status': 'trialing', 'current_period_end': ch.get('trial_ends_on', t.get('trial_ends_on'))})
    ch['version'] = t['version'] + 1
    t = patch('tenants', t, ch)
    audit(req.ctx, t['id'], 'reactivate', reason)
    return tenant_row(t)


def audit_log(req: Req) -> Any:
    d = get_db()
    tenants = {t['id']: t for t in reversed(d.tenants)}
    users = {u['id']: u for u in reversed(d.users)}
    out = []
    for a in d.admin_audit:
        tn = tenants.get(a.get('tenant_id'))
        us = users.get(a.get('actor_id'))
        out.append(
            {
                **a,
                'tenant_name': coalesce(tn.get('name', UNDEF) if tn else UNDEF, a.get('tenant_id', UNDEF)),
                'actor_name': coalesce(us.get('name', UNDEF) if us else UNDEF, 'Platform'),
            }
        )
    return sorted(out, key=lambda x: locale_key(x['at']), reverse=True)


def register(r: Any) -> None:
    r.get('/admin/tenants', list_tenants, super_admin=True)
    r.post('/admin/tenants/:id/extend', extend, super_admin=True)
    r.post('/admin/tenants/:id/suspend', suspend, super_admin=True)
    r.post('/admin/tenants/:id/reactivate', reactivate, super_admin=True)
    r.get('/admin/audit', audit_log, super_admin=True)
