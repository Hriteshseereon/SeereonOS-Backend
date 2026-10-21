"""E-mail delivery queue (worker): outbox messages are queued in the flush transaction when EMAIL_DELIVERY=smtp;
claims are exclusive (FOR UPDATE SKIP LOCKED + lease, fenced by worker id), failures back off exponentially, the
N-th failure (or a permanent 5xx) dead-letters; platform messages are sealed at rest and scrubbed once settled."""

from __future__ import annotations

import smtplib
import threading

import pytest

from app.ctx import build_ctx
from app.engine.email import render_template, send_email
from app.workers.email import EmailDelivery
from tests.conftest import owner_sql

pytestmark = pytest.mark.usefixtures('fresh')


class FakeMailer:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail
        self.lock = threading.Lock()

    def send(self, to: str, subject: str, text: str) -> None:
        if self.fail:
            raise self.fail
        with self.lock:
            self.sent.append((to, subject, text))


def test_render_template():
    assert render_template('Hi {{ name }}, {{x}}{{missing}}!', {'name': 'Asha', 'x': 0, 'missing': None}) == 'Hi Asha, 0!'


def _queue_task_mail(rt, n: int = 1) -> list[dict]:  # type: ignore[no-untyped-def]
    rt.backend.queue_emails = True
    try:

        def fn(s):  # type: ignore[no-untyped-def]
            ctx = build_ctx(s.find_by('users', 'id', 'u_santosh'), 'tn_seereon', {})
            return [
                send_email(
                    ctx,
                    'task_assigned',
                    {'email': 'hritesh@seereon.co.in', 'user_id': 'u_hritesh'},
                    {'employee_name': 'Hritesh', 'manager_name': 'Santosh', 'task_title': f'Ship it {i}', 'due_date': '2026-10-20'},
                    {'entity': 'task', 'id': f't{i}'},
                )
                for i in range(n)
            ]

        return rt.run_scoped('tenant', 'tn_seereon', fn, is_write=True)
    finally:
        rt.backend.queue_emails = False


def _status(mid: str) -> tuple:
    return owner_sql('SELECT status, attempts, last_error, locked_by FROM email_deliveries WHERE message_id = %s', mid)[0]


def test_outbox_delivery_retry_backoff_and_success(app):  # type: ignore[no-untyped-def]
    rt = app.state.rt
    (msg,) = _queue_task_mail(rt)
    assert msg['subject'] == 'New task: Ship it 0' and 'https://app.seereonos.com/login' in msg['body']
    assert (msg['status'], msg['sent_at']) == ('queued', None)  # the sent log tells the truth: not sent yet
    assert _status(msg['id'])[:2] == ('pending', 0)
    pool = rt.backend.pools.app
    out = EmailDelivery(pool, FakeMailer(fail=OSError('smtp down')), 'w1', max_attempts=3).tick()
    assert out['retry'] == 1
    st, attempts, last, locked = _status(msg['id'])
    assert (st, attempts, locked) == ('pending', 1, None) and 'smtp down' in last
    # backed off: not due yet
    assert EmailDelivery(pool, FakeMailer(), 'w2').tick()['claimed'] == 0
    owner_sql("UPDATE email_deliveries SET next_attempt_at = now() - interval '1 second' WHERE message_id = %s", msg['id'])
    ok = FakeMailer()
    assert EmailDelivery(pool, ok, 'w2').tick()['sent'] == 1
    assert ok.sent == [('hritesh@seereon.co.in', 'New task: Ship it 0', msg['body'])]
    assert _status(msg['id'])[0] == 'sent'
    st, sent_at, ver = owner_sql('SELECT status, sent_at, version FROM email_outbox WHERE id = %s', msg['id'])[0]
    assert st == 'sent' and sent_at is not None and ver == 2


def test_dead_letter_after_max_attempts_and_permanent_errors(app):  # type: ignore[no-untyped-def]
    rt = app.state.rt
    a, b = _queue_task_mail(rt, 2)
    pool = rt.backend.pools.app
    for _ in range(3):
        owner_sql("UPDATE email_deliveries SET next_attempt_at = now() - interval '1 second' WHERE message_id = %s", a['id'])
        owner_sql("UPDATE email_deliveries SET status = 'skipped' WHERE message_id = %s", b['id'])  # only `a` is in play
        EmailDelivery(pool, FakeMailer(fail=OSError('still down')), 'w1', max_attempts=3).tick()
    assert _status(a['id'])[:2] == ('dead', 3)
    assert owner_sql('SELECT status FROM email_outbox WHERE id = %s', a['id'])[0][0] == 'failed'
    owner_sql("UPDATE email_deliveries SET status = 'pending', attempts = 0, next_attempt_at = now() WHERE message_id = %s", b['id'])
    perm = smtplib.SMTPRecipientsRefused({'hritesh@seereon.co.in': (550, b'no such mailbox')})
    out = EmailDelivery(pool, FakeMailer(fail=perm), 'w1', max_attempts=8).tick()
    assert out['dead'] == 1 and _status(b['id'])[:2] == ('dead', 1)  # a permanent rejection is not retried


def test_two_workers_never_send_the_same_message(app):  # type: ignore[no-untyped-def]
    rt = app.state.rt
    msgs = _queue_task_mail(rt, 30)
    pool = rt.backend.pools.app
    mailer = FakeMailer()
    workers = [EmailDelivery(pool, mailer, f'w{i}', batch=4) for i in range(2)]
    barrier = threading.Barrier(2)

    def run(w):  # type: ignore[no-untyped-def]
        barrier.wait()
        while w.tick()['claimed']:
            pass

    ts = [threading.Thread(target=run, args=(w,)) for w in workers]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    subjects = [s for _, s, _ in mailer.sent]
    assert len(subjects) == 30 and len(set(subjects)) == 30  # every message exactly once
    ids = [m['id'] for m in msgs]
    assert owner_sql("SELECT count(*) FROM email_deliveries WHERE message_id = ANY(%s) AND status = 'sent'", ids)[0][0] == 30


def test_expired_claim_is_taken_over_and_fenced(app):  # type: ignore[no-untyped-def]
    rt = app.state.rt
    (msg,) = _queue_task_mail(rt)
    pool = rt.backend.pools.app
    with pool.connection() as c:
        claimed = c.execute('SELECT message_id FROM seereon_email_claim(%s, %s, %s)', ['dead-worker', 10, 300]).fetchall()
    assert [r[0] for r in claimed] == [msg['id']]
    assert EmailDelivery(pool, FakeMailer(), 'w2').tick()['claimed'] == 0  # leased to the dead worker
    owner_sql("UPDATE email_deliveries SET locked_until = now() - interval '1 second' WHERE message_id = %s", msg['id'])
    ok = FakeMailer()
    assert EmailDelivery(pool, ok, 'w2').tick()['sent'] == 1
    with pool.connection() as c:  # the dead worker coming back cannot settle a message it no longer holds
        assert c.execute('SELECT seereon_email_sent(%s, %s)', [msg['id'], 'dead-worker']).fetchone()[0] is False


def test_platform_mail_sealed_scrubbed_and_expired(app, api):  # type: ignore[no-untyped-def]
    from tests.conftest import platform_mails

    rt = app.state.rt
    api.call('POST', '/auth/reset-request', {'email': 'bijaya@seereon.co.in'})
    m = platform_mails('bijaya@seereon.co.in')[0]
    assert m['status'] == 'pending' and m['sealed'].startswith('v1.') and 'reset-password?token=' in m['body']
    ok = FakeMailer()
    EmailDelivery(rt.backend.pools.app, ok, 'w1').tick()
    assert ok.sent and ok.sent[0][0] == 'bijaya@seereon.co.in' and 'reset-password?token=' in ok.sent[0][2]
    m2 = platform_mails('bijaya@seereon.co.in')[0]
    assert m2['status'] == 'sent' and m2['sealed'] is None  # the link is gone from the database once delivered
    api.call('POST', '/auth/reset-request', {'email': 'bijaya@seereon.co.in'})
    owner_sql("UPDATE email_deliveries SET expires_at = now() - interval '1 minute' WHERE kind = 'platform' AND status = 'pending'")
    assert EmailDelivery(rt.backend.pools.app, FakeMailer(), 'w1').tick()['claimed'] == 0
    assert platform_mails('bijaya@seereon.co.in')[0]['status'] == 'expired' and platform_mails('bijaya@seereon.co.in')[0]['sealed'] is None


def test_production_with_email_disabled_queues_no_platform_mail(app, api):  # type: ignore[no-untyped-def]
    """nothing would ever drain them: no sealed reset links pile up (development still queues them for the mailbox)"""
    from tests.conftest import platform_mails

    backend = app.state.rt.backend
    assert backend.queue_platform_mails  # the test app (development) queues them
    before = len(platform_mails('bijaya@seereon.co.in'))
    backend.queue_platform_mails = False  # = create_app(production config, EMAIL_DELIVERY=disabled)
    try:
        r = api.call('POST', '/auth/reset-request', {'email': 'bijaya@seereon.co.in'})
        assert r.status_code == 200 and r.json()['data'] == {'accepted': True}
        assert len(platform_mails('bijaya@seereon.co.in')) == before
    finally:
        backend.queue_platform_mails = True


def test_runtime_role_cannot_touch_the_queue_directly(app):  # type: ignore[no-untyped-def]
    import psycopg

    with app.state.rt.backend.pools.app.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('SELECT * FROM email_deliveries LIMIT 1')
