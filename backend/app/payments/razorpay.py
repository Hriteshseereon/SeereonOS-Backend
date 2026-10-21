"""Razorpay: server-side order creation (Orders API over HTTPS with httpx) and signature verification.

* Checkout signature (POST /billing/payments/verify): HMAC-SHA256(key_secret, f"{order_id}|{payment_id}") as hex.
* Webhook signature (POST /billing/webhooks/razorpay): HMAC-SHA256(webhook_secret, raw request body) as hex, sent in
  X-Razorpay-Signature.
Comparisons are constant-time. Nothing here trusts an amount from the client: orders are created for the amount of a
server-side billing invoice.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from app.errors import ApiError

PROVIDER = 'razorpay'


def _hex_hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()


def checkout_signature(key_secret: str, order_id: str, payment_id: str) -> str:
    return _hex_hmac(key_secret, f'{order_id}|{payment_id}'.encode())


def verify_checkout_signature(key_secret: str, order_id: Any, payment_id: Any, signature: Any) -> bool:
    if not all(isinstance(x, str) and x for x in (key_secret, order_id, payment_id, signature)):
        return False
    return hmac.compare_digest(checkout_signature(key_secret, order_id, payment_id), signature.strip().lower())


def webhook_signature(webhook_secret: str, raw_body: bytes) -> str:
    return _hex_hmac(webhook_secret, raw_body)


def verify_webhook_signature(webhook_secret: str | None, raw_body: bytes, signature: str | None) -> bool:
    if not webhook_secret or not signature or not isinstance(raw_body, (bytes, bytearray)):
        return False
    return hmac.compare_digest(webhook_signature(webhook_secret, bytes(raw_body)), signature.strip().lower())


@dataclass
class RazorpayOrder:
    id: str
    amount: int
    currency: str
    status: str


class RazorpayClient:
    def __init__(self, key_id: str, key_secret: str, api_url: str = 'https://api.razorpay.com', timeout_s: float = 15.0, transport: Any = None) -> None:
        self.key_id = key_id
        self.key_secret = key_secret
        self.api_url = api_url.rstrip('/')
        self.timeout_s = timeout_s
        self.transport = transport  # tests: httpx.MockTransport

    def create_order(self, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]) -> RazorpayOrder:
        import httpx

        if not isinstance(amount_paise, int) or amount_paise < 100:
            raise ApiError(422, 'VALIDATION', 'The amount to pay must be at least ₹1.')
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self.transport) as c:
                r = c.post(
                    f'{self.api_url}/v1/orders',
                    auth=(self.key_id, self.key_secret),
                    json={'amount': amount_paise, 'currency': currency, 'receipt': receipt[:40], 'notes': notes, 'payment_capture': 1},
                )
        except httpx.HTTPError as e:
            raise ApiError(502, 'PAYMENT_PROVIDER_UNAVAILABLE', 'The payment provider could not be reached. Try again in a minute.') from e
        if r.status_code >= 400:
            raise ApiError(502, 'PAYMENT_PROVIDER_ERROR', 'The payment provider refused to create the order. Try again or contact support.')
        d = r.json()
        if not isinstance(d, dict) or not isinstance(d.get('id'), str) or d.get('amount') != amount_paise:
            raise ApiError(502, 'PAYMENT_PROVIDER_ERROR', 'The payment provider answered unexpectedly. Nothing was charged.')
        return RazorpayOrder(d['id'], int(d['amount']), str(d.get('currency') or currency), str(d.get('status') or 'created'))


def client_from_settings() -> RazorpayClient:
    from app.config import settings

    cfg = settings()
    if not cfg.payments_enabled:
        raise ApiError(409, 'PAYMENTS_NOT_CONFIGURED', 'Online payments are not set up for this installation. Contact SeereonOS support to change your plan.')
    return RazorpayClient(cfg.razorpay_key_id or '', cfg.razorpay_key_secret or '', cfg.razorpay_api_url, transport=_TRANSPORT)


_TRANSPORT: Any = None  # tests install an httpx.MockTransport here


def set_transport(t: Any) -> None:
    global _TRANSPORT
    _TRANSPORT = t
