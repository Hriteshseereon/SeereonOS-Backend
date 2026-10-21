"""E-mail delivery: the SMTP mailer and the queue consumer run by the worker process (python -m app.worker).

What is queued (always, whatever EMAIL_DELIVERY says — the queue is the record):
  * every email_outbox message the shared code writes (welcome e-mail, approvals, payslips…): the flush queues an
    `outbox` delivery in the same transaction (seereon_email_enqueue_outbox);
  * platform messages that must not sit in a tenant's sent log — password-reset links and invitations: the body is
    sealed with AES-256-GCM (MFA_ENCRYPTION_KEY) by the API, opened here, and scrubbed from the database once sent,
    expired or dead (seereon_email_enqueue_platform).

Delivery (EMAIL_DELIVERY=smtp): seereon_email_claim takes due messages with FOR UPDATE SKIP LOCKED and a lease, so N
workers never send the same message at the same time; only the claim holder can settle it (fencing by worker id).
A failure goes back to pending with exponential back-off (1, 2, 4 … minutes, capped at 6 h); after EMAIL_MAX_ATTEMPTS
the message is dead-lettered (status 'dead', last_error kept). Permanent SMTP rejections (5xx) are dead at once. A
worker that dies mid-send loses its lease and the message is claimed again (at-least-once across crashes: the only
window for a duplicate is a crash between the SMTP server accepting the message and the 'sent' update).
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger('seereonos.email')

MAX_BACKOFF_S = 6 * 3600


class Mailer:
    def __init__(self, smtp_url: str, mail_from: str, timeout_s: float = 30.0) -> None:
        self.url = urlparse(smtp_url)
        self.mail_from = mail_from
        self.timeout_s = timeout_s
        q = parse_qs(self.url.query)
        # smtp://… uses STARTTLS when the server offers it; credentials are never sent over a plaintext connection
        # (except to localhost); ?tls=required refuses servers without STARTTLS
        self.tls_required = (q.get('tls') or [''])[0].lower() == 'required'

    def send(self, to: str, subject: str, text: str) -> None:
        u = self.url
        secure = u.scheme == 'smtps'
        port = u.port or (465 if secure else 587)
        host = u.hostname or 'localhost'
        msg = EmailMessage()
        msg['From'] = self.mail_from
        msg['To'] = to
        msg['Subject'] = subject
        msg['Date'] = formatdate(localtime=False)
        domain = (parseaddr(self.mail_from)[1].rpartition('@')[2] or 'seereonos.local').strip('>')
        msg['Message-ID'] = make_msgid(domain=domain)
        msg.set_content(text)
        ctx = ssl.create_default_context()
        if secure:
            s: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=ctx, timeout=self.timeout_s)
        else:
            s = smtplib.SMTP(host, port, timeout=self.timeout_s)
        try:
            s.ehlo()
            tls = secure
            if not secure and s.has_extn('starttls'):
                s.starttls(context=ctx)
                s.ehlo()
                tls = True
            if not tls and (self.tls_required or (u.username and host not in ('localhost', '127.0.0.1', '::1'))):
                raise smtplib.SMTPNotSupportedError('the SMTP server does not offer STARTTLS; refusing to send credentials in clear text')
            if u.username:
                s.login(unquote(u.username), unquote(u.password or ''))
            s.send_message(msg, from_addr=parseaddr(self.mail_from)[1] or None)
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass


def create_mailer(smtp_url: str | None, mail_from: str) -> Mailer | None:
    return Mailer(smtp_url, mail_from) if smtp_url else None


def _permanent(e: BaseException) -> bool:
    """an SMTP answer that will not change on retry (5xx except 421/450-type transient codes)"""
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        codes = [c for c, _ in e.recipients.values()]
        return bool(codes) and all(500 <= c < 600 for c in codes)
    if isinstance(e, smtplib.SMTPResponseException):
        return 500 <= e.smtp_code < 600
    return False


def _error_text(e: BaseException) -> str:
    return re.sub(r'//[^@/\s]*@', '//***@', f'{type(e).__name__}: {e}')[:500]


class EmailDelivery:
    """one pass over the due queue (called by the worker's email job; safe to run in N processes at once)"""

    def __init__(
        self,
        pool: Any,
        mailer: Any,
        worker_id: str,
        max_attempts: int = 8,
        batch: int = 20,
        lease_s: int = 300,
        base_backoff_s: int = 60,
    ) -> None:
        self.pool = pool
        self.mailer = mailer
        self.worker_id = worker_id
        self.max_attempts = max_attempts
        self.batch = batch
        self.lease_s = lease_s
        self.base_backoff_s = base_backoff_s

    def backoff(self, attempts: int) -> int:
        return int(min(MAX_BACKOFF_S, self.base_backoff_s * (2 ** max(0, attempts - 1))))

    def tick(self) -> dict[str, int]:
        from app.engine.secrets import open_secret

        out = {'claimed': 0, 'sent': 0, 'retry': 0, 'dead': 0, 'skipped': 0}
        with self.pool.connection() as c:
            claimed = c.execute(
                'SELECT message_id, tenant_id, kind, to_addr, subject, body, attempts FROM seereon_email_claim(%s, %s, %s)',
                [self.worker_id, self.batch, self.lease_s],
            ).fetchall()
        out['claimed'] = len(claimed)
        for message_id, _tenant, kind, to, subject, body, attempts in claimed:
            if to is None or subject is None or body is None:
                self._failed(message_id, 'message deleted', attempts, skip=True)
                out['skipped'] += 1
                continue
            text = body if kind == 'outbox' else open_secret(body)
            if text is None:  # sealed with a key this process does not hold (rotation without MFA_ENCRYPTION_KEY_PREVIOUS)
                self._failed(message_id, 'cannot open the sealed message (MFA_ENCRYPTION_KEY)', attempts, skip=True)
                out['skipped'] += 1
                continue
            try:
                self.mailer.send(to, subject, text)
            except Exception as e:  # noqa: BLE001
                st = self._failed(message_id, _error_text(e), attempts, skip=False, permanent=_permanent(e))
                out['dead' if st == 'dead' else 'retry'] += 1
                log.warning('email delivery failed', extra={'message_id': message_id, 'attempts': attempts, 'status': st, 'error': type(e).__name__})
                continue
            with self.pool.connection() as c:
                ok = c.execute('SELECT seereon_email_sent(%s, %s)', [message_id, self.worker_id]).fetchone()[0]  # type: ignore[index]
            if ok:
                out['sent'] += 1
            else:  # the lease ran out and another worker took the message over (slow SMTP): it settles it
                log.warning('email sent after its claim expired', extra={'message_id': message_id})
        return out

    def _failed(self, message_id: str, error: str, attempts: int, skip: bool, permanent: bool = False) -> str | None:
        """back to pending (back-off), dead-letter (attempts used up, or a permanent SMTP rejection) or skipped"""
        with self.pool.connection() as c:
            r = c.execute(
                'SELECT seereon_email_failed(%s, %s, %s, %s, %s, %s)',
                [message_id, self.worker_id, error, attempts if permanent else self.max_attempts, self.backoff(attempts), skip],
            ).fetchone()
        return r[0] if r else None
