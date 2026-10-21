"""Email outbox — port of src/mock/email.ts. Renders the tenant's template and stores the message in email_outbox
(the worker process delivers it when EMAIL_DELIVERY=smtp). Links point at APP_URL."""

from __future__ import annotations

import re
from typing import Any

from app.ctx import Ctx, find_by, insert, rows
from app.js import UNDEF, js_or, js_str, nullish

DEFAULT_LOGIN_URL = 'https://app.seereonos.com/login'  # the TypeScript reference's constant (golden vectors, parity)


def login_url() -> str:
    """the sign-in page of THIS installation: APP_URL + /login (links in every e-mail)"""
    from app.config import settings

    base = settings().app_url
    return f'{base}/login' if base else DEFAULT_LOGIN_URL


def render_template(text: str, vars: dict[str, Any]) -> str:
    def rep(m: re.Match[str]) -> str:
        v = vars.get(m.group(1), UNDEF)
        return '' if nullish(v) else js_str(v)

    return re.sub(r'\{\{\s*(\w+)\s*\}\}', rep, text, flags=re.ASCII)


def template_for(ctx: Ctx, key: str) -> dict | None:
    return next((t for t in rows(ctx, 'email_templates') if t.get('key') == key and t.get('active')), None)


def send_email(ctx: Ctx, key: str, to: dict[str, Any], vars: dict[str, Any], related: dict[str, str] | None = None) -> dict | None:
    """to = {'email', 'user_id'?}; related = {'entity', 'id'}"""
    t = template_for(ctx, key)
    if not t:
        return None
    tenant = find_by('tenants', 'id', ctx.tenant_id)
    org = tenant.get('org') if tenant else None
    company = js_or(org.get('brand_name') if org else UNDEF, tenant.get('name') if tenant else UNDEF, '')
    all_vars = {'company': company, 'login_url': login_url(), **vars}
    uid = to.get('user_id', UNDEF)
    return insert(
        ctx,
        'email_outbox',
        {
            'to': to['email'],
            'to_user_id': None if nullish(uid) else uid,
            'subject': render_template(t['subject'], all_vars),
            'body': render_template(t['body'], all_vars),
            'template_key': key,
            'related_entity': related['entity'] if related else None,
            'related_id': related['id'] if related else None,
            'status': 'queued',  # v8: the worker delivers it (sent / failed); sent_at is set then
            'sent_at': None,
        },
    )
