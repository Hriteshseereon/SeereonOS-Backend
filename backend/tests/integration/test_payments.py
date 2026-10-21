"""Razorpay payments end to end against the API (the Orders API is an httpx MockTransport — no network):
the amount always comes from the server-side invoice, the checkout signature is verified before anything is applied,
verify and webhook are idempotent (a payment is applied once, an event id is stored once), and with no keys every
payment endpoint answers 409 PAYMENTS_NOT_CONFIGURED — no simulated success anywhere."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from tests.conftest import PASSWORD, env_for, owner_sql

KEY_ID, KEY_SECRET, WH_SECRET = 'rzp_test_key_1', 'rzp_test_secret_1', 'whsec_test_1'
pytestmark = pytest.mark.usefixtures('fresh')


class FakeRazorpay:
    def __init__(self) -> None:
        self.orders: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/v1/orders' and request.method == 'POST'
        assert request.headers['authorization'].startswith('Basic ')
        body = json.loads(request.content)
        oid = f'order_TEST{len(self.orders) + 1:04d}'
        self.orders.append({**body, 'id': oid})
        return httpx.Response(200, json={'id': oid, 'entity': 'order', 'amount': body['amount'], 'currency': body['currency'], 'status': 'created'})


@pytest.fixture()
def pay(app):  # type: ignore[no-untyped-def]
    """an API instance with Razorpay keys (the session app has none)"""
    from starlette.testclient import TestClient

    from app.config import load_config, set_settings
    from app.main import create_app
    from app.payments.razorpay import set_transport

    fake = FakeRazorpay()
    set_transport(httpx.MockTransport(fake))
    a = create_app(load_config(env_for(RAZORPAY_KEY_ID=KEY_ID, RAZORPAY_KEY_SECRET=KEY_SECRET, RAZORPAY_WEBHOOK_SECRET=WH_SECRET)))
    try:
        with TestClient(a) as c:
            tok = c.post('/api/v1/auth/login', json={'email': 'santosh@seereon.co.in', 'password': PASSWORD}).json()['data']['token']
            yield {'c': c, 'tok': tok, 'fake': fake, 'app': a}
    finally:
        set_transport(None)
        a.state.rt.backend.pools.close()
        set_settings(app.state.cfg)


def call(p: dict, method: str, path: str, body=None):  # type: ignore[no-untyped-def]
    return p['c'].request(method, '/api/v1' + path, json=body, headers={'Authorization': 'Bearer ' + p['tok']})


def sign(order_id: str, payment_id: str) -> str:
    return hmac.new(KEY_SECRET.encode(), f'{order_id}|{payment_id}'.encode(), hashlib.sha256).hexdigest()


def webhook(p: dict, event: str, order_id: str, payment_id: str, amount: int, event_id: str, secret: str = WH_SECRET):  # type: ignore[no-untyped-def]
    raw = json.dumps(
        {'entity': 'event', 'event': event, 'payload': {'payment': {'entity': {'id': payment_id, 'order_id': order_id, 'amount': amount, 'status': 'captured', 'card': {'last4': '1111'}}}}}
    ).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return p['c'].post(
        '/api/v1/billing/webhooks/razorpay', content=raw, headers={'content-type': 'application/json', 'x-razorpay-signature': sig, 'x-razorpay-event-id': event_id}
    )


def test_order_amount_from_the_server_and_verified_payment_applied_once(pay):  # type: ignore[no-untyped-def]
    b = call(pay, 'GET', '/billing').json()['data']
    assert b['payments'] == {'enabled': True, 'provider': 'razorpay', 'key_id': KEY_ID, 'support_email': None}
    seats = b['seats_used'] + 2
    initial = b['tenant']
    r = call(pay, 'POST', '/billing/payments/order', {'plan_code': 'scale', 'seats': seats, 'amount_paise': 100, 'amount': 1})
    assert r.status_code == 200, r.text
    o = r.json()['data']
    expected = 69900 * seats + round(69900 * seats * 18 / 100)
    assert o['amount_paise'] == expected and o['invoice']['amount_paise'] == expected and o['invoice']['status'] == 'due'
    assert pay['fake'].orders[-1]['amount'] == expected and pay['fake'].orders[-1]['currency'] == 'INR'  # what Razorpay was asked
    assert o['key_id'] == KEY_ID and o['order_id'] == 'order_TEST0001'
    # Pay clicked twice: the same invoice and order, no second order at Razorpay
    again = call(pay, 'POST', '/billing/payments/order', {'plan_code': 'scale', 'seats': seats}).json()['data']
    assert again['order_id'] == o['order_id'] and again['invoice']['id'] == o['invoice']['id'] and len(pay['fake'].orders) == 1
    # a forged signature changes nothing
    bad = call(pay, 'POST', '/billing/payments/verify', {'razorpay_order_id': o['order_id'], 'razorpay_payment_id': 'pay_X', 'razorpay_signature': sign(o['order_id'], 'pay_Y')})
    assert bad.status_code == 400 and bad.json()['error']['code'] == 'PAYMENT_SIGNATURE_INVALID'
    assert call(pay, 'GET', '/billing').json()['data']['tenant'] == initial
    good = {'razorpay_order_id': o['order_id'], 'razorpay_payment_id': 'pay_GOOD1', 'razorpay_signature': sign(o['order_id'], 'pay_GOOD1')}
    r = call(pay, 'POST', '/billing/payments/verify', good)
    assert r.status_code == 200, r.text
    d = r.json()['data']
    assert d['status'] == 'paid' and d['already_paid'] is False and d['invoice']['status'] == 'paid'
    assert d['tenant']['status'] == 'active' and d['tenant']['plan_code'] == 'scale' and d['tenant']['seats'] == seats
    # verify again (and the webhook for the same payment): applied once
    d2 = call(pay, 'POST', '/billing/payments/verify', good).json()['data']
    assert d2['already_paid'] is True and d2['tenant']['version'] == d['tenant']['version']
    w = webhook(pay, 'payment.captured', o['order_id'], 'pay_GOOD1', expected, 'evt_001')
    assert w.status_code == 200 and w.json()['data']['outcome'] == 'duplicate'
    w = webhook(pay, 'payment.captured', o['order_id'], 'pay_GOOD1', expected, 'evt_001')  # Razorpay retries the event
    assert w.json()['data']['outcome'] == 'duplicate'
    events = owner_sql("SELECT id, outcome, payload FROM payment_events WHERE provider_order_id = %s", o['order_id'])
    assert [(e[0], e[1]) for e in events] == [('evt_001', 'duplicate')]
    assert 'card' not in json.dumps(events[0][2])  # the stored payload keeps ids and amounts only
    assert owner_sql("SELECT count(*) FROM billing_invoices WHERE tenant_id = 'tn_seereon' AND status = 'paid' AND id = %s", o['invoice']['id'])[0][0] == 1


def test_webhook_applies_a_payment_the_browser_never_confirmed(pay):  # type: ignore[no-untyped-def]
    b = call(pay, 'GET', '/billing').json()['data']
    seats = b['seats_used'] + 1
    o = call(pay, 'POST', '/billing/payments/order', {'plan_code': 'scale', 'seats': seats}).json()['data']
    assert webhook(pay, 'payment.captured', o['order_id'], 'pay_W1', o['amount_paise'], 'evt_x', secret='wrong').status_code == 400
    assert owner_sql('SELECT count(*) FROM payment_events')[0][0] == 0
    assert webhook(pay, 'payment.captured', o['order_id'], 'pay_W1', o['amount_paise'] - 1, 'evt_amt').json()['data']['outcome'] == 'failed'  # amount mismatch
    assert call(pay, 'GET', '/billing').json()['data']['tenant'] == b['tenant']
    r = webhook(pay, 'order.paid', o['order_id'], 'pay_W1', o['amount_paise'], 'evt_002')
    assert r.status_code == 200 and r.json()['data']['outcome'] == 'applied'
    t = call(pay, 'GET', '/billing').json()['data']['tenant']
    assert t['plan_code'] == 'scale' and t['status'] == 'active' and t['seats'] == seats
    assert webhook(pay, 'payment.captured', 'order_UNKNOWN', 'pay_Z', 100, 'evt_003').json()['data']['outcome'] == 'ignored'


def test_failed_payment_and_seat_top_up(pay):  # type: ignore[no-untyped-def]
    b = call(pay, 'GET', '/billing').json()['data']
    seats = b['seats_used']
    o = call(pay, 'POST', '/billing/payments/order', {'plan_code': 'growth', 'seats': seats}).json()['data']
    raw_fail = webhook(pay, 'payment.failed', o['order_id'], 'pay_F1', o['amount_paise'], 'evt_f1')
    assert raw_fail.json()['data']['outcome'] == 'applied'
    inv = owner_sql('SELECT status FROM billing_invoices WHERE id = %s', o['invoice']['id'])[0][0]
    assert inv == 'failed'
    # retry the same purchase: a new order for the same (now due again) invoice
    o2 = call(pay, 'POST', '/billing/payments/order', {'invoice_id': o['invoice']['id']}).json()['data']
    assert o2['order_id'] != o['order_id'] and o2['invoice']['status'] == 'due'
    call(pay, 'POST', '/billing/payments/verify', {'razorpay_order_id': o2['order_id'], 'razorpay_payment_id': 'pay_OK2', 'razorpay_signature': sign(o2['order_id'], 'pay_OK2')})
    # more seats: an invoice to pay first; nothing changes until it is paid
    r = call(pay, 'POST', '/billing/seats', {'seats': seats + 3}).json()['data']
    assert r['payment_required'] is True and r['invoice']['status'] == 'due' and r['tenant']['seats'] == seats
    o3 = call(pay, 'POST', '/billing/payments/order', {'invoice_id': r['invoice']['id']}).json()['data']
    assert o3['amount_paise'] == r['invoice']['amount_paise'] and o3['purpose'] == 'seats'
    d = call(pay, 'POST', '/billing/payments/verify', {'razorpay_order_id': o3['order_id'], 'razorpay_payment_id': 'pay_S1', 'razorpay_signature': sign(o3['order_id'], 'pay_S1')}).json()['data']
    assert d['tenant']['seats'] == seats + 3
    # fewer seats: no charge
    r = call(pay, 'POST', '/billing/seats', {'seats': seats + 1}).json()['data']
    assert r['payment_required'] is False and r['tenant']['seats'] == seats + 1


def test_payments_off_means_409_never_a_fake_success(api):  # type: ignore[no-untyped-def]
    tok = api.login('santosh@seereon.co.in')
    b = api.call('GET', '/billing', token=tok).json()['data']
    assert b['payments'] == {'enabled': False, 'provider': None, 'key_id': None, 'support_email': None}
    for path, body in (('/billing/payments/order', {'plan_code': 'growth', 'seats': 30}), ('/billing/payments/verify', {'razorpay_order_id': 'o', 'razorpay_payment_id': 'p', 'razorpay_signature': 's'})):
        r = api.call('POST', path, body, token=tok)
        assert r.status_code == 409 and r.json()['error']['code'] == 'PAYMENTS_NOT_CONFIGURED', (path, r.text)
    assert api.call('POST', '/billing/checkout', {'plan_code': 'growth', 'seats': 30, 'method': 'card'}, token=tok).status_code == 404  # the simulation is gone
    r = api.call('POST', '/billing/seats', {'seats': 999}, token=tok)
    assert r.status_code in (409, 422)
    r = api.c.post('/api/v1/billing/webhooks/razorpay', content=b'{}', headers={'content-type': 'application/json', 'x-razorpay-signature': 'x'})
    assert r.status_code == 409 and r.json()['error']['code'] == 'PAYMENTS_NOT_CONFIGURED'
