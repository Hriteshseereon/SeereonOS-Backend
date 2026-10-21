"""Billing (UI-23): plans, seats, invoices — and real payments with Razorpay (no simulated success anywhere).

Paying (plan purchase or extra seats) is three steps; the amount always comes from a server-side billing invoice:
  1. POST /billing/payments/order {plan_code, seats} | {invoice_id} -> a 'due' billing invoice + a Razorpay order
     created by the server (Orders API) -> {order_id, amount_paise, currency, key_id, …} for Razorpay Checkout;
  2. the browser runs Checkout; POST /billing/payments/verify {razorpay_order_id, razorpay_payment_id,
     razorpay_signature} -> the HMAC-SHA256 signature is checked with the key secret, then the invoice is marked paid
     and the plan / seats applied — idempotently (a second verify or the webhook finds the order paid);
  3. POST /billing/webhooks/razorpay (app/payments/webhook.py) confirms the same payment server-to-server.
Without RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET every payment endpoint answers 409 PAYMENTS_NOT_CONFIGURED. A read-only
tenant may call these (the router allows /billing)."""

from __future__ import annotations

from typing import Any

from app.catalog import cat
from app.ctx import Ctx, Req, err, find, find_by, get_db, insert, log_activity, notify, now_iso, patch, require_action, rows, update, users_with
from app.domain.calendar import add_days, add_months, month_of, today_iso
from app.js import date_parse, is_integer, js_max, js_min, js_number, js_round, js_str, js_truthy, locale_key, mark_volatile, now_ms
from app.routes.auth import tenant_of

GST_PCT = cat('mock/routes/billing')['GST_PCT']
PLANS = cat('mock/routes/billing')['PLANS']


def plan(code: Any) -> dict | None:
    return next((p for p in PLANS if p['code'] == code), None)


def sub_of(ctx: Ctx) -> dict:
    s = next(iter(rows(ctx, 'subscriptions')), None)
    if s is None:
        s = insert(
            ctx,
            'subscriptions',
            {'plan_code': 'trial', 'status': 'trialing', 'current_period_end': add_days(today_iso(), 14), 'seats': 5, 'razorpay_subscription_id': None},
        )
    return s


def quote(p: dict, seats: Any) -> dict[str, Any]:
    subtotal = p['price_per_user_paise'] * seats
    gst = js_round((subtotal * GST_PCT) / 100)
    return {
        'plan_code': p['code'],
        'seats': seats,
        'unit_paise': p['price_per_user_paise'],
        'subtotal_paise': subtotal,
        'gst_paise': gst,
        'total_paise': subtotal + gst,
    }


def seat_limit(t: dict) -> int:
    """THE seat limit of a workspace (adding people, the billing page, the platform console): its purchased seats; a
    trial includes at least the trial plan's limit (25 people)."""
    trial = plan('trial')
    cap = trial['max_users'] if trial and trial.get('max_users') else 25
    return max(cap, t['seats']) if t.get('status') == 'trial' else t['seats']


def seats_used(ctx: Ctx) -> int:
    now = now_iso()
    return sum(1 for u in rows(ctx, 'users') if u.get('status') == 'active') + sum(
        1 for i in rows(ctx, 'invites') if not i.get('accepted_at') and i['expires_at'] > now
    )


def get_billing(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'billing.manage', 'Only admins can see billing.')
    t = tenant_of(ctx)
    s = sub_of(ctx)
    p = plan(s.get('plan_code')) or PLANS[0]
    return {
        'tenant': {
            'id': t['id'],
            'name': t['name'],
            'status': t['status'],
            'trial_ends_on': t.get('trial_ends_on'),
            'plan_code': t.get('plan_code'),
            'seats': t.get('seats'),
        },
        'subscription': s,
        'plan': p,
        'plans': PLANS,
        'gst_pct': GST_PCT,
        'seats_used': seats_used(ctx),
        'seat_limit': seat_limit(t),
        'next_charge': None if s['status'] == 'trialing' else quote(p, s['seats']),
        'invoices': sorted(rows(ctx, 'billing_invoices'), key=lambda x: locale_key(x['issued_on']), reverse=True),
        'payments': payments_info(),
    }


def payments_info() -> dict[str, Any]:
    """{enabled, provider, key_id, support_email}: the public key id for Razorpay Checkout (never the secret), and whom
    to contact (SUPPORT_EMAIL) — the billing page shows it when online payments are off"""
    from app.config import settings

    cfg = settings()
    return {
        'enabled': cfg.payments_enabled,
        'provider': 'razorpay' if cfg.payments_enabled else None,
        'key_id': cfg.razorpay_key_id if cfg.payments_enabled else None,
        'support_email': cfg.support_email,
    }


def post_quote(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'billing.manage')
    p = plan(req.b('plan_code'))
    if not p or p['code'] == 'trial':
        err(422, 'VALIDATION', 'Choose a paid plan.', 'plan_code')
    assert p is not None
    n = js_number(req.b('seats'))
    return quote(p, js_max(1, n if js_truthy(n) else 1))


def _validate_plan_seats(ctx: Ctx, plan_code: Any, seats_raw: Any) -> tuple[dict, int]:
    p = plan(plan_code)
    if not p or p['code'] == 'trial':
        err(422, 'VALIDATION', 'Choose a paid plan.', 'plan_code')
    assert p is not None
    seats = js_number(seats_raw)
    used = seats_used(ctx)
    if not is_integer(seats) or seats < 1:
        err(422, 'VALIDATION', 'Enter the number of seats.', 'seats')
    if seats < used:
        err(409, 'SEATS_BELOW_USAGE', f'You have {used} people and pending invites. Buy at least {used} seats, or deactivate people first.', 'seats')
    if p.get('max_users') and seats > p['max_users']:
        err(409, 'PLAN_LIMIT', f'{p["name"]} supports up to {js_str(p["max_users"])} people. Choose a bigger plan.', 'seats')
    return p, int(seats)


def _new_invoice(ctx: Ctx, amount_paise: int) -> dict:
    today = today_iso()
    seq_no = len(get_db().billing_invoices) + 101
    return insert(
        ctx,
        'billing_invoices',
        {'number': f'SOS-{today[:4]}-{seq_no}', 'period': month_of(today), 'amount_paise': amount_paise, 'status': 'due', 'issued_on': today},
    )


def _order_view(o: dict, inv: dict, ctx: Ctx) -> dict[str, Any]:
    from app.config import settings

    p = plan(o['plan_code'])
    return {
        'order_id': o['provider_order_id'],
        'provider': o['provider'],
        'key_id': settings().razorpay_key_id,
        'amount_paise': o['amount_paise'],
        'currency': o['currency'],
        'invoice': inv,
        'purpose': o['purpose'],
        'plan_code': o['plan_code'],
        'seats': o['seats'],
        'name': 'SeereonOS',
        'description': f'{p["name"] if p else o["plan_code"]} × {o["seats"]} seats' if o['purpose'] == 'plan' else f'{o["seats"]} seats',
        'prefill': {'name': ctx.user.get('name'), 'email': ctx.user.get('email')},
        'notes': {'tenant_id': ctx.tenant_id, 'invoice_id': inv['id']},
    }


def create_order(req: Req) -> Any:
    """POST /billing/payments/order — {plan_code, seats} buys a plan; {invoice_id} pays an existing due invoice (e.g. the
    prorated extra seats from POST /billing/seats). The amount is the invoice's, computed here."""
    from app.payments.razorpay import PROVIDER, client_from_settings

    ctx = req.ctx
    require_action(ctx, 'billing.manage', 'Only admins can pay for the plan.')
    client = client_from_settings()  # 409 PAYMENTS_NOT_CONFIGURED when keys are not set
    sub_of(ctx)  # the subscription row exists (created lazily, like GET /billing)
    inv_id = req.b('invoice_id')
    if js_truthy(inv_id):
        inv = find(ctx, 'billing_invoices', inv_id, 'Invoice')
        if inv['status'] == 'paid':
            err(409, 'INVOICE_PAID', 'This invoice is already paid.')
        prev = next((o for o in rows(ctx, 'payment_orders') if o.get('invoice_id') == inv['id']), None)
        if prev is None:
            err(409, 'INVOICE_NOT_PAYABLE', 'This invoice cannot be paid online. Contact SeereonOS support.')
        assert prev is not None
        purpose, plan_code, seats = prev['purpose'], prev['plan_code'], prev['seats']
    else:
        p, seats = _validate_plan_seats(ctx, req.b('plan_code'), req.b('seats'))
        purpose, plan_code = 'plan', p['code']
        amount = quote(p, seats)['total_paise']
        # the same unpaid purchase again (Pay clicked twice, Checkout closed): reuse its invoice and order
        inv = None
        for o in rows(ctx, 'payment_orders'):
            if o.get('status') == 'created' and o.get('purpose') == 'plan' and o.get('plan_code') == plan_code and o.get('seats') == seats:
                cand = find_by('billing_invoices', 'id', o.get('invoice_id'))
                if cand is not None and cand.get('status') == 'due' and cand.get('amount_paise') == amount:
                    inv = cand
                    break
        if inv is None:
            inv = _new_invoice(ctx, amount)
    open_order = next(
        (
            o
            for o in rows(ctx, 'payment_orders')
            if o.get('invoice_id') == inv['id'] and o.get('status') == 'created' and o.get('amount_paise') == inv['amount_paise']
        ),
        None,
    )
    if open_order is not None and open_order.get('provider_order_id'):
        return _order_view(open_order, inv, ctx)
    if inv['status'] == 'failed':
        inv = update(ctx, 'billing_invoices', inv['id'], {'status': 'due'})
    mark_volatile()
    ro = client.create_order(int(inv['amount_paise']), 'INR', inv['number'], {'tenant_id': str(ctx.tenant_id), 'invoice_id': str(inv['id'])})
    o = insert(
        ctx,
        'payment_orders',
        {
            'provider': PROVIDER,
            'provider_order_id': ro.id,
            'provider_payment_id': None,
            'invoice_id': inv['id'],
            'purpose': purpose,
            'plan_code': plan_code,
            'seats': seats,
            'amount_paise': int(inv['amount_paise']),
            'currency': 'INR',
            'status': 'created',
            'paid_at': None,
            'failure_reason': None,
        },
    )
    log_activity(ctx, 'billing', inv['id'], 'payment_order', f'Payment started for invoice {inv["number"]}')
    return _order_view(o, inv, ctx)


def apply_paid(ctx: Ctx, order: dict, payment_id: str | None) -> dict[str, Any]:
    """mark the order and its invoice paid and apply what was bought — once: an order already paid is returned as is
    (verify and webhook may both arrive; the unit of work serialises them per tenant)"""
    inv = find(ctx, 'billing_invoices', order['invoice_id'], 'Invoice')
    t = tenant_of(ctx)
    if order['status'] == 'paid':
        return {
            'invoice': inv,
            'tenant': t,
            'subscription': sub_of(ctx),
            'payment_id': order.get('provider_payment_id'),
            'status': 'paid',
            'already_paid': True,
        }
    order = update(ctx, 'payment_orders', order['id'], {'status': 'paid', 'provider_payment_id': payment_id, 'paid_at': now_iso(), 'failure_reason': None})
    inv = update(ctx, 'billing_invoices', inv['id'], {'status': 'paid'})
    s = sub_of(ctx)
    p = plan(order['plan_code'])
    seats = order['seats']
    today = today_iso()
    if order['purpose'] == 'plan':
        base = s['current_period_end'] if s['current_period_end'] > today and s['status'] == 'active' else today
        s = update(
            ctx,
            'subscriptions',
            s['id'],
            {
                'plan_code': order['plan_code'],
                'status': 'active',
                'seats': seats,
                'current_period_end': add_months(base, 1),
                'razorpay_subscription_id': s.get('razorpay_subscription_id'),
            },
        )
        t = patch('tenants', t, {'status': 'active', 'plan_code': order['plan_code'], 'seats': seats, 'trial_ends_on': None, 'version': t['version'] + 1})
        log_activity(ctx, 'billing', inv['id'], 'checkout', f'Paid {p["name"] if p else order["plan_code"]} × {js_str(seats)} seats')
    else:
        old = s['seats']
        s = update(ctx, 'subscriptions', s['id'], {'seats': seats})
        t = patch('tenants', t, {'seats': seats, 'version': t['version'] + 1})
        log_activity(ctx, 'billing', s['id'], 'seats', f'Seats changed {js_str(old)} → {js_str(seats)}')
    for a in users_with(ctx, 'billing.manage'):
        notify(ctx, a['id'], 'NTF-16', 'Payment received', f'Invoice {inv["number"]} is paid. Thank you.', '/billing')
    return {'invoice': inv, 'tenant': t, 'subscription': s, 'payment_id': payment_id, 'status': 'paid', 'already_paid': False}


def mark_failed(ctx: Ctx, order: dict, reason: str) -> None:
    if order['status'] != 'created':
        return
    update(ctx, 'payment_orders', order['id'], {'status': 'failed', 'failure_reason': reason[:300]})
    inv = find_by('billing_invoices', 'id', order['invoice_id'])
    if inv is not None and inv.get('status') == 'due':
        update(ctx, 'billing_invoices', inv['id'], {'status': 'failed'})
    for a in users_with(ctx, 'billing.manage'):
        notify(ctx, a['id'], 'NTF-16', 'Payment failed', 'The payment did not go through. Nothing was charged; try again from Billing.', '/billing')


def verify_payment(req: Req) -> Any:
    """POST /billing/payments/verify — Razorpay Checkout's handler response; the signature proves the payment"""
    from app.config import settings
    from app.payments.razorpay import client_from_settings, verify_checkout_signature

    ctx = req.ctx
    require_action(ctx, 'billing.manage', 'Only admins can pay for the plan.')
    client_from_settings()  # 409 when not configured
    oid, pid, sig = req.b('razorpay_order_id'), req.b('razorpay_payment_id'), req.b('razorpay_signature')
    if not all(isinstance(x, str) and x for x in (oid, pid, sig)):
        err(422, 'VALIDATION', 'razorpay_order_id, razorpay_payment_id and razorpay_signature are required.')
    order = next((o for o in rows(ctx, 'payment_orders') if o.get('provider_order_id') == oid), None)
    if order is None:
        err(404, 'NOT_FOUND', 'Payment order not found.')
    assert order is not None
    if not verify_checkout_signature(settings().razorpay_key_secret or '', oid, pid, sig):
        err(400, 'PAYMENT_SIGNATURE_INVALID', 'The payment could not be verified. Nothing was applied; contact support if you were charged.')
    if order['status'] == 'paid' and order.get('provider_payment_id') not in (None, pid):
        err(409, 'ORDER_ALREADY_PAID', 'This order was already paid with another payment.')
    return apply_paid(ctx, order, pid)


def change_seats(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'billing.manage', 'Only admins can change seats.')
    t = tenant_of(ctx)
    s = sub_of(ctx)
    if s['status'] == 'trialing' or t['status'] != 'active':
        err(409, 'PLAN_REQUIRED', 'Choose a paid plan first; seats are set during checkout.')
    seats = js_number(req.b('seats'))
    used = seats_used(ctx)
    p = plan(s.get('plan_code'))
    assert p is not None
    if not is_integer(seats) or seats < 1:
        err(422, 'VALIDATION', 'Enter the number of seats.', 'seats')
    if seats < used:
        err(409, 'SEATS_BELOW_USAGE', f'You have {used} people and pending invites. Keep at least {used} seats.', 'seats')
    if p.get('max_users') and seats > p['max_users']:
        err(409, 'PLAN_LIMIT', f'{p["name"]} supports up to {js_str(p["max_users"])} people. Upgrade the plan.', 'seats')
    added = seats - s['seats']
    if added > 0:
        # extra seats are paid first (prorated for the rest of the period); they apply when the payment is verified
        from app.payments.razorpay import client_from_settings

        client_from_settings()  # 409 PAYMENTS_NOT_CONFIGURED
        days_left = js_max(1, js_round((date_parse(s['current_period_end']) - now_ms()) / 86400000))
        sub = js_round((p['price_per_user_paise'] * added * js_min(30, days_left)) / 30)
        invoice = _new_invoice(ctx, sub + js_round((sub * GST_PCT) / 100))
        insert(
            ctx,
            'payment_orders',
            {
                'provider': 'razorpay',
                'provider_order_id': None,
                'provider_payment_id': None,
                'invoice_id': invoice['id'],
                'purpose': 'seats',
                'plan_code': p['code'],
                'seats': seats,
                'amount_paise': invoice['amount_paise'],
                'currency': 'INR',
                'status': 'created',
                'paid_at': None,
                'failure_reason': None,
            },
        )
        log_activity(ctx, 'billing', invoice['id'], 'seats_invoice', f'Invoice for {js_str(added)} more seats ({js_str(s["seats"])} → {js_str(seats)})')
        return {'tenant': t, 'invoice': invoice, 'payment_required': True}
    update(ctx, 'subscriptions', s['id'], {'seats': seats})
    t = patch('tenants', tenant_of(ctx), {'seats': seats, 'version': t['version'] + 1})
    log_activity(ctx, 'billing', s['id'], 'seats', f'Seats changed {js_str(s["seats"])} → {js_str(seats)}')
    return {'tenant': t, 'invoice': None, 'payment_required': False}


def get_invoice(req: Req) -> Any:
    require_action(req.ctx, 'billing.manage')
    return find(req.ctx, 'billing_invoices', req.params['id'], 'Invoice')


def register(r: Any) -> None:
    r.get('/billing', get_billing)
    r.post('/billing/quote', post_quote)
    r.post('/billing/payments/order', create_order)
    r.post('/billing/payments/verify', verify_payment)
    r.post('/billing/seats', change_seats)
    r.get('/billing/invoices/:id', get_invoice)
