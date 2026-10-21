"""Runtime part of src/mock/seeds/workflows.ts: ensureWfLetters / provisionWfTemplates (the demo seed itself is exported
as JSON). The template data comes from the generated catalogue (WF_DOC_TEMPLATES, WF_EMAIL_TEMPLATES and the
defaultWfTemplates() factory). `db` is the request Scope (like app/seeds/access.py)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.catalog import cat
from app.db.uow import Scope
from app.frozen import thaw

_C = cat('mock/seeds/workflows')
WF_DOC_TEMPLATES: Any = _C['WF_DOC_TEMPLATES']
WF_EMAIL_TEMPLATES: Any = _C['WF_EMAIL_TEMPLATES']


def default_wf_templates() -> list[dict]:
    return thaw(_C['defaultWfTemplates()'])


def _has_key(db: Scope, table: str, tenant_id: str, key: Any) -> bool:
    return any(x.get('tenant_id') == tenant_id and x.get('key') == key for x in db.view(table))


def ensure_wf_letters(db: Scope, tenant_id: str, base: Callable[[], dict]) -> None:
    """Adds the workflow letter / email templates a tenant is missing (never duplicates a key)."""
    for t in WF_DOC_TEMPLATES:
        if not _has_key(db, 'doc_templates', tenant_id, t['key']):
            db.append('doc_templates', {**base(), **thaw(t), 'custom_placeholders': [], 'active': True, 'updated_by': None})
    for t in WF_EMAIL_TEMPLATES:
        if not _has_key(db, 'email_templates', tenant_id, t['key']):
            db.append('email_templates', {**base(), **thaw(t), 'active': True})


def provision_wf_templates(db: Scope, tenant_id: str, base: Callable[[], dict]) -> None:
    """Workflow templates + letters for a tenant (idempotent by template key)."""
    ensure_wf_letters(db, tenant_id, base)
    for t in default_wf_templates():
        if not _has_key(db, 'wf_templates', tenant_id, t['key']):
            db.append('wf_templates', {**base(), **t})
