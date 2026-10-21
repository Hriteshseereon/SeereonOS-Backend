"""Access provisioning — port of provisionTenant() of src/mock/seeds/access.ts: default roles, system email
templates, approval workflows, maker-checker defaults, master data and (v7) letter + workflow templates, for every
new workspace (signup).

The data-heavy parts (roles, email/doc/workflow templates, approval workflows) are replayed from the provisioning
template exported from the TypeScript (app/catalog/data/provision/tenant.json, built by running provisionTenant() in
Node), in the TS call order and with the TS "only if missing" keys; master data is ported as code (seeds/master.py).
Golden-tested: tests/golden/test_provision.py."""

from __future__ import annotations

from collections.abc import Callable

from app.catalog import cat
from app.db.uow import Scope
from app.frozen import thaw
from app.seeds.master import provision_master

SYSTEM_EMAIL_TEMPLATES = cat('mock/seeds/access')['SYSTEM_EMAIL_TEMPLATES']
NEW_TENANT_MC = cat('mock/seeds/access')['NEW_TENANT_MC']
_T = cat('provision/tenant')['tables']
_DOC_KEYS = [t['key'] for t in cat('mock/seeds/documentsV7')['DEFAULT_DOC_TEMPLATES']]
_WF_DOC_KEYS = [t['key'] for t in cat('mock/seeds/workflows')['WF_DOC_TEMPLATES']]
_WF_EMAIL_KEYS = [t['key'] for t in cat('mock/seeds/workflows')['WF_EMAIL_TEMPLATES']]
_SYSTEM_KEYS = [t['key'] for t in SYSTEM_EMAIL_TEMPLATES]
_BASE_KEYS = ('id', 'tenant_id', 'created_at', 'updated_at', 'created_by', 'version')


def _replay(s: Scope, table: str, tenant_id: str, base: Callable[[], dict], keys: list[str] | None = None, key_field: str = 'key') -> None:
    """append the template rows of `table` (optionally only those whose key is in `keys`, in template order) that the
    tenant does not have yet (by key_field)"""
    for tpl in _T.get(table, ()):
        k = tpl.get(key_field)
        if keys is not None and k not in keys:
            continue
        if any(x.get('tenant_id') == tenant_id and x.get(key_field) == k for x in s.view(table)):
            continue
        s.append(table, {**base(), **{f: thaw(v) for f, v in tpl.items() if f not in _BASE_KEYS}})


def provision_tenant(s: Scope, tenant: dict, base: Callable[[], dict], v7_defaults: bool = True) -> dict:
    """provision a tenant in the current scope; returns the updated tenant row (maker_checker / org set when missing)
    — the caller replaces the stored row with it"""
    tid = tenant['id']
    _replay(s, 'roles', tid, base)
    _replay(s, 'email_templates', tid, base, _SYSTEM_KEYS)
    _replay(s, 'approval_workflows', tid, base, key_field='process')  # provisionWorkflows (src/mock/workflows.ts)
    if not tenant.get('maker_checker'):
        tenant = {**tenant, 'maker_checker': {**thaw(NEW_TENANT_MC), 'entities': list(NEW_TENANT_MC['entities'])}}
    tenant = provision_master(s, tenant, base)
    if v7_defaults:
        _replay(s, 'doc_templates', tid, base, _DOC_KEYS)  # provisionDocDefaults (src/mock/docgen.ts)
        _replay(s, 'email_templates', tid, base, ['payslip', 'custom'])
        _replay(s, 'doc_templates', tid, base, _WF_DOC_KEYS)  # provisionWfTemplates: ensureWfLetters, then the templates
        _replay(s, 'email_templates', tid, base, _WF_EMAIL_KEYS)
        _replay(s, 'wf_templates', tid, base)
    return tenant
