"""Razorpay signature verification with fixed vectors (no network): checkout (order|payment) and webhook (raw body)."""

from __future__ import annotations

from app.payments.razorpay import checkout_signature, verify_checkout_signature, verify_webhook_signature, webhook_signature

KEY_SECRET = 'rzp_test_secret_123'
ORDER = 'order_IluGWxBm9U8zJ8'
PAYMENT = 'pay_IluHnLQaU0jcdK'
CHECKOUT_SIG = 'eaca9315e92168cef95e38886d81b026804d2082d6029f845371ddab2e536523'
WEBHOOK_SECRET = 'whsec_test_456'
WEBHOOK_BODY = (
    b'{"entity":"event","event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_IluHnLQaU0jcdK","order_id":"order_IluGWxBm9U8zJ8","amount":118000}}}}'
)
WEBHOOK_SIG = 'aad9bd90c33cdf353ad7f09b349832a71b3a816878e14c399125d4650fb80e7d'


def test_checkout_signature_vector():
    assert checkout_signature(KEY_SECRET, ORDER, PAYMENT) == CHECKOUT_SIG
    assert verify_checkout_signature(KEY_SECRET, ORDER, PAYMENT, CHECKOUT_SIG)
    assert verify_checkout_signature(KEY_SECRET, ORDER, PAYMENT, CHECKOUT_SIG.upper())  # hex case-insensitive


def test_checkout_signature_rejects_tampering():
    assert not verify_checkout_signature(KEY_SECRET, ORDER, 'pay_other', CHECKOUT_SIG)
    assert not verify_checkout_signature(KEY_SECRET, 'order_other', PAYMENT, CHECKOUT_SIG)
    assert not verify_checkout_signature('another-secret', ORDER, PAYMENT, CHECKOUT_SIG)
    assert not verify_checkout_signature(KEY_SECRET, ORDER, PAYMENT, CHECKOUT_SIG[:-1] + '0')
    for bad in (None, '', 123, ['x']):
        assert not verify_checkout_signature(KEY_SECRET, ORDER, PAYMENT, bad)  # type: ignore[arg-type]
        assert not verify_checkout_signature(KEY_SECRET, bad, PAYMENT, CHECKOUT_SIG)  # type: ignore[arg-type]
    assert not verify_checkout_signature('', ORDER, PAYMENT, CHECKOUT_SIG)


def test_webhook_signature_vector_and_tampering():
    assert webhook_signature(WEBHOOK_SECRET, WEBHOOK_BODY) == WEBHOOK_SIG
    assert verify_webhook_signature(WEBHOOK_SECRET, WEBHOOK_BODY, WEBHOOK_SIG)
    assert not verify_webhook_signature(WEBHOOK_SECRET, WEBHOOK_BODY.replace(b'118000', b'100'), WEBHOOK_SIG)  # amount changed
    assert not verify_webhook_signature(WEBHOOK_SECRET, WEBHOOK_BODY + b' ', WEBHOOK_SIG)  # the RAW body is signed
    assert not verify_webhook_signature(None, WEBHOOK_BODY, WEBHOOK_SIG)
    assert not verify_webhook_signature(WEBHOOK_SECRET, WEBHOOK_BODY, None)
    assert not verify_webhook_signature('other', WEBHOOK_BODY, WEBHOOK_SIG)
