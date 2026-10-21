"""Log redaction (SECURITY.md V3.1.1 / V7.1): secrets in URLs and structured fields never reach the logs."""

from __future__ import annotations

import logging

import orjson

from app.logging_setup import JsonFormatter, redact, redact_url


def test_redact_url_tokens() -> None:
    assert redact_url('/api/v1/auth/reset-info/abc123?x=1') == '/api/v1/auth/reset-info/[redacted]?x=1'
    assert redact_url('/api/v1/invites/inv_secret/accept') == '/api/v1/invites/[redacted]/accept'
    assert redact_url('/x?token=s3cr3t&a=1&MFA_TOKEN=zz') == '/x?token=[redacted]&a=1&MFA_TOKEN=[redacted]'
    assert redact_url('/api/v1/projects?status=active') == '/api/v1/projects?status=active'


def test_redact_nested_fields() -> None:
    v = {'Authorization': 'Bearer abc', 'body': {'password': 'pw', 'next': 'pw2', 'list': [{'token': 't', 'data_url': 'data:x'}]}, 'ok': 1}
    assert redact(v) == {'Authorization': '[redacted]', 'body': {'password': '[redacted]', 'next': '[redacted]', 'list': [{'token': '[redacted]', 'data_url': '[redacted]'}]}, 'ok': 1}


def test_formatter_redacts_extra_and_keeps_pino_levels() -> None:
    rec = logging.LogRecord('seereonos', logging.ERROR, __file__, 1, 'boom', None, None)
    rec.req = {'headers': {'authorization': 'Bearer abc', 'idempotency-key': 'k'}, 'url': '/a'}  # type: ignore[attr-defined]
    out = orjson.loads(JsonFormatter().format(rec))
    assert out['level'] == 50 and out['msg'] == 'boom'
    assert out['req']['headers'] == {'authorization': '[redacted]', 'idempotency-key': '[redacted]'}
