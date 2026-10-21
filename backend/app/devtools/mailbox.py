"""DEVELOPMENT ONLY (refused when APP_ENV=production): read the e-mail queue of a local database, e.g. to follow a
password-reset or invitation link without an SMTP server (EMAIL_DELIVERY=disabled).

    python -m app.devtools.mailbox [--to someone@example.com] [--last 10] [--json]

Platform messages (reset links, invitations) are sealed at rest; they are opened here with MFA_ENCRYPTION_KEY (the
development key when unset). Connection: the owner role (MIGRATION_DATABASE_URL, else DATABASE_URL) — the runtime role
cannot read the queue.
"""

from __future__ import annotations

import argparse
import os
import sys

import orjson


def read(conn: object, to: str | None, last: int) -> list[dict]:
    from app.engine.secrets import open_secret

    sql = """SELECT d.message_id, d.kind, d.status, d.created_at::text, coalesce(d.to_addr, o."to"), coalesce(d.subject, o.subject),
                    CASE WHEN d.kind = 'platform' THEN d.body ELSE o.body END
               FROM email_deliveries d LEFT JOIN email_outbox o ON d.kind = 'outbox' AND o.id = d.message_id
              WHERE (%s::text IS NULL OR lower(coalesce(d.to_addr, o."to")) = lower(%s::text))
              ORDER BY d.created_at DESC LIMIT %s"""
    rows = conn.execute(sql, [to, to, last]).fetchall()  # type: ignore[attr-defined]
    out = []
    for mid, kind, status, at, addr, subject, body in rows:
        text = open_secret(body) if kind == 'platform' and body else body
        out.append({'id': mid, 'kind': kind, 'status': status, 'at': at, 'to': addr, 'subject': subject, 'body': text if text is not None else '(scrubbed after delivery / expiry)'})
    return out


def main(argv: list[str] | None = None) -> int:
    from app.auth.secret_box import create_secret_box
    from app.config import DEV_MFA_KEY
    from app.db.migrate import migration_url
    from app.db.pool import connect
    from app.engine.secrets import set_secret_box

    p = argparse.ArgumentParser(prog='python -m app.devtools.mailbox', description='Local e-mail queue (development only)')
    p.add_argument('--to')
    p.add_argument('--last', type=int, default=10)
    p.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    if (os.environ.get('APP_ENV') or os.environ.get('NODE_ENV')) == 'production':
        print('The mailbox is a development tool and is refused when APP_ENV=production.', file=sys.stderr)
        return 2
    prev = [s.strip() for s in (os.environ.get('MFA_ENCRYPTION_KEY_PREVIOUS') or '').split(',') if s.strip()]
    set_secret_box(create_secret_box(os.environ.get('MFA_ENCRYPTION_KEY') or DEV_MFA_KEY, prev, allow_plain=True))
    with connect(migration_url()) as c:
        mails = read(c, a.to, max(1, min(a.last, 500)))
    if a.json:
        print(orjson.dumps(mails).decode())
        return 0
    for m in mails:
        print(f'--- {m["at"]}  {m["status"]}  {m["kind"]}  to {m["to"]}\nSubject: {m["subject"]}\n\n{m["body"]}\n')
    if not mails:
        print('(no messages)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
