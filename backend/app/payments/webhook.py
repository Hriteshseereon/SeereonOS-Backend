"""POST /api/v1/billing/webhooks/razorpay — public, authenticated by its signature.

1. payments must be configured (409 PAYMENTS_NOT_CONFIGURED otherwise) and RAZORPAY_WEBHOOK_SECRET set;
2. X-Razorpay-Signature must equal HMAC-SHA256(RAZORPAY_WEBHOOK_SECRET, raw body) (400 SIGNATURE_INVALID, audited);
3. the order is looked up by its Razorpay order id (seereon_payment_tenant): unknown orders are acknowledged (200,
   outcome "ignored") so Razorpay stops retrying;
4. inside ONE unit of work of that tenant: an event id already stored -> "duplicate" (nothing applied again);
   payment.captured / order.paid -> the order is paid (app.routes.billing.apply_paid, itself idempotent);
   payment.failed -> the order/invoice are marked failed (unless already paid); the event is stored in
   payment_events with a payload reduced to ids, amounts and status (no card / contact details).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import orjson

from app.errors import error_body

log = logging.getLogger('seereonos')

PAID_EVENTS = frozenset({'payment.captured', 'order.paid'})
FAILED_EVENTS = frozenset({'payment.failed'})


def _entity(payload: dict, kind: str) -> dict:
    p = payload.get('payload')
    e = p.get(kind) if isinstance(p, dict) else None
    ent = e.get('entity') if isinstance(e, dict) else None
    return ent if isinstance(ent, dict) else {}


def reduced(payload: dict) -> dict[str, Any]:
    pay = _entity(payload, 'payment')
    order = _entity(payload, 'order')
    keep = ('id', 'order_id', 'amount', 'amount_paid', 'currency', 'status', 'method', 'error_code', 'error_reason', 'captured')
    return {
        'event': payload.get('event'),
        'created_at': payload.get('created_at'),
        'payment': {k: pay[k] for k in keep if k in pay},
        'order': {k: order[k] for k in keep if k in order},
    }


def handle_webhook(rt: Any, cfg: Any, raw: bytes, signature: str | None, event_id: str | None, request_id: str) -> tuple[int, bytes]:
    from app.payments.razorpay import PROVIDER, verify_webhook_signature

    if not cfg.payments_enabled or not cfg.razorpay_webhook_secret:
        return 409, error_body('PAYMENTS_NOT_CONFIGURED', 'Online payments are not set up for this installation.', request_id)
    if not verify_webhook_signature(cfg.razorpay_webhook_secret, raw, signature):
        log.warning('razorpay webhook: invalid signature', extra={'reqId': request_id})
        _audit(rt, 'payment.webhook_rejected', 'blocked', None, request_id, {'reason': 'signature'})
        return 400, error_body('SIGNATURE_INVALID', 'Invalid webhook signature.', request_id)
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return 400, error_body('INVALID_JSON', 'The request body is not valid JSON.', request_id)
    if not isinstance(payload, dict):
        return 400, error_body('INVALID_JSON', 'The request body is not a JSON object.', request_id)
    event = str(payload.get('event') or '')
    pay = _entity(payload, 'payment')
    order = _entity(payload, 'order')
    order_id = order.get('id') or pay.get('order_id')
    payment_id = pay.get('id')
    eid = (event_id or '').strip()[:200] or 'sha256:' + hashlib.sha256(raw).hexdigest()
    tenant_id = rt.backend.payment_tenant(PROVIDER, order_id) if isinstance(order_id, str) and order_id else None
    if tenant_id is None:
        return 200, orjson.dumps({'data': {'ok': True, 'outcome': 'ignored', 'reason': 'unknown order'}, 'warnings': []})

    def fn(_scope: Any) -> str:
        from app.ctx import Ctx, find_by, insert, rows
        from app.routes.billing import apply_paid, mark_failed

        tenant = find_by('tenants', 'id', tenant_id)
        if tenant is None:
            return 'ignored'
        ctx = Ctx.synthetic_ctx(tenant, {'id': 'system'})
        if find_by('payment_events', 'id', eid) is not None:
            return 'duplicate'
        o = next((x for x in rows(ctx, 'payment_orders') if x.get('provider_order_id') == order_id), None)
        outcome = 'ignored'
        if o is not None and event in PAID_EVENTS:
            amount = pay.get('amount', order.get('amount_paid', order.get('amount')))
            if isinstance(amount, int) and amount != o['amount_paise']:
                outcome = 'failed'  # never apply a payment of another amount than the invoice's
                log.error('razorpay webhook: amount mismatch', extra={'reqId': request_id, 'order': order_id})
            else:
                res = apply_paid(ctx, o, payment_id if isinstance(payment_id, str) else None)
                outcome = 'duplicate' if res.get('already_paid') else 'applied'
        elif o is not None and event in FAILED_EVENTS:
            mark_failed(ctx, o, str(pay.get('error_description') or pay.get('error_reason') or 'payment failed'))
            outcome = 'applied'
        insert(
            ctx,
            'payment_events',
            {
                'id': eid,
                'provider': PROVIDER,
                'event': event[:100] or 'unknown',
                'provider_order_id': order_id,
                'provider_payment_id': payment_id if isinstance(payment_id, str) else None,
                'payload': reduced(payload),
                'outcome': outcome,
            },
        )
        return outcome

    outcome = rt.run_scoped('tenant', tenant_id, fn, is_write=True)
    _audit(rt, 'payment.webhook', 'success' if outcome in ('applied', 'duplicate') else 'info', tenant_id, request_id, {'event': event, 'outcome': outcome})
    return 200, orjson.dumps({'data': {'ok': True, 'outcome': outcome}, 'warnings': []})


def _audit(rt: Any, event: str, outcome: str, tenant_id: str | None, request_id: str, detail: dict) -> None:
    try:
        from app.security.store import PgSecurityStore, SecurityEvent

        PgSecurityStore(rt.backend.pools.app).log(SecurityEvent(event, outcome, tenant_id=tenant_id, request_id=request_id, detail=detail))
    except Exception:  # noqa: BLE001
        pass


def openapi_paths() -> dict[str, Any]:
    return {
        '/billing/webhooks/razorpay': {
            'post': {
                'tags': ['billing'],
                'summary': 'Razorpay webhook (payment.captured, order.paid, payment.failed)',
                'description': 'No session required. Authenticated by X-Razorpay-Signature = HMAC-SHA256(RAZORPAY_WEBHOOK_SECRET, raw body). '
                'Idempotent by X-Razorpay-Event-Id. 409 PAYMENTS_NOT_CONFIGURED when payments are off.',
                'parameters': [
                    {'name': 'X-Razorpay-Signature', 'in': 'header', 'required': True, 'schema': {'type': 'string'}},
                    {'name': 'X-Razorpay-Event-Id', 'in': 'header', 'required': False, 'schema': {'type': 'string'}},
                ],
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {'type': 'object'}}}},
                'responses': {
                    '200': {'description': 'Processed (outcome applied | duplicate | ignored)'},
                    '400': {'description': 'Invalid signature or body'},
                    '409': {'description': 'Payments not configured'},
                },
            }
        }
    }
