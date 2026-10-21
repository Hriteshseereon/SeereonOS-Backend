"""Port of src/mock/routes/documents.ts — project libraries + company library, versions, must-read acknowledgements,
views.

Access rules (server-enforced):
 - company docs (project_id null): visibility 'company' → everyone in the tenant; 'managers' → admin, pm, ops.
 - project docs: 'company' → everyone; 'project' → admin, project PM, ops, task owners/backups, allocated people;
   'managers' → admin, project PM, ops.
 - manage: project docs → admin or the project's PM (documents.manage); company docs → documents.company (admin/ops).
 - archived docs are only visible to people who can manage them.

Exported helper (used by GET /search in auth.py): visible_documents(ctx).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from app.ctx import Ctx, Req, err, find, find_by, get_db, has, insert, log_activity, notify, now_iso, permissions_of, rows, update
from app.js import UNDEF, coalesce, js_join, js_len, js_lower, js_number, js_slice, js_str, js_trim, js_truthy, locale_key, prop

CATEGORIES = ['requirements', 'design', 'technical', 'test', 'contract', 'meeting_notes', 'process', 'policy', 'training', 'other']
VISIBILITIES = ['project', 'company', 'managers']
EXT_MIME: dict[str, list[str]] = {
    'pdf': ['application/pdf'],
    'docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    'xlsx': ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    'pptx': ['application/vnd.openxmlformats-officedocument.presentationml.presentation'],
    'png': ['image/png'],
    'jpg': ['image/jpeg'],
    'jpeg': ['image/jpeg'],
    'csv': ['text/csv', 'application/vnd.ms-excel'],
    'txt': ['text/plain'],
    'md': ['text/markdown', 'text/x-markdown', 'text/plain'],
}
MAX_BYTES = 10 * 1024 * 1024

# JS \s / \S (Unicode whitespace as ECMAScript defines it — Python's \s also matches \x1c-\x1f)
_WS = '\t\n\x0b\x0c\r \xa0  -     　﻿'
_NS = f'[^{_WS}]'
_URL_RE = re.compile(rf'^https?://{_NS}+\.{_NS}+')
_DATE_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')


# ---------------------------------------------------------------------------------------------------------------------
# small JS helpers
# ---------------------------------------------------------------------------------------------------------------------
def _g(o: Any, k: str) -> Any:
    """o.k for a JSON value (TypeError on null/undefined like JS; non-objects have no such property)"""
    if o is None or o is UNDEF:
        raise TypeError(f"Cannot read properties of {js_str(o)} (reading '{k}')")
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    return UNDEF


def _s(v: Any) -> str:
    """String(v ?? '').trim()"""
    return js_trim(js_str(coalesce(v, '')))


def u16slice(s: str, a: int, b: int | None = None) -> str:
    """String.prototype.slice in UTF-16 code units"""
    if all(ord(c) <= 0xFFFF for c in s):
        return js_slice(s, a, UNDEF if b is None else b)
    units = s.encode('utf-16-le', 'surrogatepass')
    n = len(units) // 2
    e = n if b is None else b
    return units[a * 2 : min(e, n) * 2].decode('utf-16-le', 'surrogatepass')


# ---------------------------------------------------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------------------------------------------------
class Members:
    """project_id → user ids (task owners/backups, allocations, PM) + project_id → pm_id"""

    __slots__ = ('m', 'pm')

    def __init__(self, m: dict[str, set], pm: dict[str, Any]) -> None:
        self.m = m
        self.pm = pm

    def has(self, project_id: Any, user_id: Any) -> bool:
        s = self.m.get(project_id)
        return s is not None and user_id in s


def members_of(ctx: Ctx) -> Members:
    m: dict[Any, set] = {}

    def add(p: Any, u: Any) -> None:
        if not js_truthy(u):
            return
        m.setdefault(p, set()).add(u)

    for t in rows(ctx, 'tasks'):
        add(t.get('project_id'), t.get('owner_id'))
        add(t.get('project_id'), t.get('backup_id'))
    for a in rows(ctx, 'allocations'):
        add(a.get('project_id'), a.get('user_id'))
    pm: dict[Any, Any] = {}
    for p in rows(ctx, 'projects'):
        add(p['id'], p.get('pm_id'))
        if p['id'] not in pm:  # .find(): the first row wins
            pm[p['id']] = coalesce(p.get('pm_id', UNDEF), None)
    return Members(m, pm)


class Viewer:
    __slots__ = ('id', 'perms')

    def __init__(self, id_: str, perms: Any) -> None:
        self.id = id_
        self.perms = perms


def _pm_of(mem: Members, project_id: Any) -> Any:
    if not js_truthy(project_id):
        return None
    return mem.pm.get(project_id, None)


def can_see_as(ctx: Ctx, v: Viewer, d: dict, mem: Members) -> bool:
    if js_truthy(d.get('archived')) and not can_manage_as(ctx, v, d, mem):
        return False
    if d.get('visibility') == 'company':
        return True
    if 'documents.view_all' in v.perms:
        return True
    pid = d.get('project_id')
    if not js_truthy(pid):
        return 'documents.manage' in v.perms or 'documents.company' in v.perms  # company doc, managers only
    if _pm_of(mem, pid) == v.id:
        return True
    if d.get('visibility') == 'managers':
        return False
    return mem.has(pid, v.id)


def can_manage_as(ctx: Ctx, v: Viewer, d: dict, mem: Members | None = None) -> bool:
    pid = d.get('project_id')
    if not js_truthy(pid):
        return 'documents.company' in v.perms
    if 'documents.manage' in v.perms and 'documents.view_all' in v.perms:
        return True
    if mem is None:
        mem = members_of(ctx)
    return 'documents.manage' in v.perms and _pm_of(mem, pid) == v.id


def _me(ctx: Ctx) -> Viewer:
    return Viewer(ctx.user_id, ctx.perms)


def _can_see(ctx: Ctx, d: dict, mem: Members) -> bool:
    return can_see_as(ctx, _me(ctx), d, mem)


def visible_documents(ctx: Ctx) -> list[dict]:
    """Library documents the caller may see (used by global search)."""
    mem = members_of(ctx)
    return [d for d in rows(ctx, 'documents') if not js_truthy(d.get('archived')) and _can_see(ctx, d, mem)]


def _can_manage(ctx: Ctx, d: dict, mem: Members) -> bool:
    return can_manage_as(ctx, _me(ctx), d, mem)


def _load_doc(ctx: Ctx, id_: str, mem: Members | None = None) -> dict:
    """Load a doc the caller may see; anything else is a 404 (never reveal existence)."""
    if mem is None:
        mem = members_of(ctx)
    d = find(ctx, 'documents', id_, 'Document')
    if not _can_see(ctx, d, mem):
        err(404, 'NOT_FOUND', 'Document not found.')
    return d


def _require_manage(ctx: Ctx, d: dict, mem: Members) -> None:
    if not _can_manage(ctx, d, mem):
        err(
            403,
            'FORBIDDEN',
            'Only admins and the project manager can change project documents.'
            if js_truthy(d.get('project_id'))
            else 'Only admins and HR & operations can change the company library.',
        )


def _is_required(ctx: Ctx, v: Viewer, d: dict, mem: Members) -> bool:
    """Who must read a must-read doc: the project team (PM + members) for project docs, everyone who can see it for
    company docs. Admins / HR who can see a project doc without being on the team may read it but are not required."""
    if not can_see_as(ctx, v, d, mem):
        return False
    pid = d.get('project_id')
    if not js_truthy(pid) or d.get('visibility') == 'company':
        return True
    if _pm_of(mem, pid) == v.id:
        return True
    if d.get('visibility') == 'managers':
        return 'documents.view_all' in v.perms
    return mem.has(pid, v.id)


def _audience_of(ctx: Ctx, d: dict, mem: Members) -> list[dict]:
    return [u for u in rows(ctx, 'users') if u.get('status') == 'active' and _is_required(ctx, Viewer(u['id'], set(permissions_of(u))), d, mem)]


def _ack_for(ctx: Ctx, d: dict, user_id: str) -> None:
    cv = d.get('current_version')
    if any(a.get('document_id') == d['id'] and a.get('user_id') == user_id and a.get('version') == cv for a in rows(ctx, 'document_acks')):
        return
    insert(ctx, 'document_acks', {'document_id': d['id'], 'version': cv, 'user_id': user_id, 'at': now_iso()})


# ---------------------------------------------------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------------------------------------------------
def _current_version(ctx: Ctx, d: dict) -> dict | None:
    cv = d.get('current_version')
    return next((v for v in rows(ctx, 'document_versions') if v.get('document_id') == d['id'] and v.get('version') == cv), None)


def _to_item(ctx: Ctx, d: dict, mem: Members) -> dict[str, Any]:
    v = _current_version(ctx, d)
    pid = d.get('project_id')
    p = next((x for x in rows(ctx, 'projects') if x['id'] == pid), None) if js_truthy(pid) else None
    cv = d.get('current_version')
    acks = [a for a in rows(ctx, 'document_acks') if a.get('document_id') == d['id'] and a.get('version') == cv]
    manage = _can_manage(ctx, d, mem)
    ack_stats: Any = None
    if manage and js_truthy(d.get('must_read')):
        aud = _audience_of(ctx, d, mem)
        ids = {u['id'] for u in aud}
        ack_stats = {'acked': len({a.get('user_id') for a in acks if a.get('user_id') in ids}), 'total': len(aud)}
    me = ctx.user_id
    if not js_truthy(d.get('must_read')):
        my_ack: Any = None
    elif any(a.get('user_id') == me for a in acks):
        my_ack = 'acknowledged'
    elif _is_required(ctx, _me(ctx), d, mem):
        my_ack = 'pending'
    else:
        my_ack = None
    return {
        'id': d['id'],
        'version': prop(d, 'version'),
        'project_id': prop(d, 'project_id'),
        'project_name': coalesce(prop(p, 'name'), None),
        'project_code': coalesce(prop(p, 'code'), None),
        'title': prop(d, 'title'),
        'description': prop(d, 'description'),
        'category': prop(d, 'category'),
        'visibility': prop(d, 'visibility'),
        'must_read': prop(d, 'must_read'),
        'tags': prop(d, 'tags'),
        'owner_id': prop(d, 'owner_id'),
        'current_version': prop(d, 'current_version'),
        'archived': prop(d, 'archived'),
        'file_name': coalesce(prop(v, 'file_name'), ''),
        'mime': coalesce(prop(v, 'mime'), ''),
        'size_bytes': coalesce(prop(v, 'size_bytes'), 0),
        'is_link': js_truthy(prop(v, 'url')),
        'updated_by': coalesce(prop(v, 'uploaded_by'), prop(d, 'owner_id')),
        'updated_at': coalesce(prop(v, 'uploaded_at'), prop(d, 'updated_at')),
        'effective_on': coalesce(prop(d, 'effective_on'), None),
        'my_ack': my_ack,
        'ack_stats': ack_stats,
        'can_manage': manage,
    }


# ---------------------------------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------------------------------
def _clean_effective(v: Any) -> str | None:
    """v7: company-library documents (policies) carry an effective date (an optional extra on the row)"""
    s = _s(v)
    if not s:
        return None
    if not _DATE_RE.match(s):
        err(422, 'VALIDATION', 'Enter a valid effective date.', 'effective_on')
    return s


def _clean_title(t: Any) -> str:
    s = _s(t)
    n = js_len(s)
    if n < 3 or n > 120:
        err(422, 'VALIDATION', 'Give the document a title of 3 to 120 characters.', 'title')
    return s


def _includes(lst: list, v: Any) -> bool:
    """Array.prototype.includes for a list of strings"""
    return isinstance(v, str) and v in lst


def _clean_category(c: Any) -> Any:
    if not _includes(CATEGORIES, c):
        err(422, 'VALIDATION', 'Choose a category.', 'category')
    return c


def _clean_visibility(v: Any, project_id: Any) -> Any:
    if not _includes(VISIBILITIES, v):
        err(422, 'VALIDATION', 'Choose who can see the document.', 'visibility')
    if not js_truthy(project_id) and v == 'project':
        return 'company'
    return v


def _clean_tags(t: Any) -> list[str]:
    if not isinstance(t, list):
        return []
    out: list[str] = []
    for x in t:
        s = js_lower(js_trim(js_str(x)))
        if s and s not in out:
            out.append(s)
    return out[:8]


# --- new URL(url): hostname + pathname (WHATWG URL parsing, the subset reachable from /^https?:\/\/\S+\.\S+/) -------
_PATH_ENC_SAFE = "!$&'()*+,-./0123456789:;=@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_abcdefghijklmnopqrstuvwxyz|~%"


class _BadUrl(ValueError):
    pass


def _whatwg_host_path(url: str) -> tuple[str, str]:
    m = re.match(r'^[\x00-\x20]*(https?):', url, re.I)
    if not m:
        raise _BadUrl(url)
    rest = url[m.end() :].replace('\\', '/')
    rest = re.sub(r'[\t\n\r]', '', rest)
    rest = rest.lstrip('/')
    end = len(rest)
    for ch in '/?#':
        i = rest.find(ch)
        if i != -1 and i < end:
            end = i
    authority, tail = rest[:end], rest[end:]
    if '@' in authority:
        authority = authority.rsplit('@', 1)[1]
    host = authority
    if host.startswith('['):
        raise _BadUrl(url)  # IPv6 literals: not handled (kept as the url, like a parse failure)
    if ':' in host:
        host, port = host.split(':', 1)
        if port and not port.isdigit():
            raise _BadUrl(url)
        if port and int(port) > 65535:
            raise _BadUrl(url)
    if not host:
        raise _BadUrl(url)
    try:
        from urllib.parse import unquote

        h = unquote(host)
        if any(ord(c) > 127 for c in h):
            h = h.encode('idna').decode('ascii')
    except Exception as e:  # noqa: BLE001
        raise _BadUrl(url) from e
    h = h.lower()
    if re.search(r'[\x00-\x20#%/:<>?@\[\\\]^|]', h):
        raise _BadUrl(url)
    # IPv4 shorthand (e.g. 1.2.3.4) passes unchanged in the common dotted-decimal form
    path = tail
    for ch in '?#':
        i = path.find(ch)
        if i != -1:
            path = path[:i]
    segs = path.split('/')[1:] if path.startswith('/') else ([] if not path else path.split('/'))
    out: list[str] = []
    for i, sgm in enumerate(segs):
        low = sgm.lower()
        last = i == len(segs) - 1
        if low in ('..', '.%2e', '%2e.', '%2e%2e'):
            if out:
                out.pop()
            if last:
                out.append('')
        elif low in ('.', '%2e'):
            if last:
                out.append('')
        else:
            out.append(quote(sgm, safe=_PATH_ENC_SAFE))
    pathname = '/' + '/'.join(out)
    return h, pathname


def _take_source(body: Any) -> dict[str, Any]:
    f = _g(body, 'file')
    url = _s(_g(body, 'url'))
    if js_truthy(f) and url:
        err(422, 'VALIDATION', 'Attach a file or paste a link — not both.', 'file')
    if js_truthy(f):
        name = _s(_g(f, 'name'))
        ext = js_lower(name.split('.')[-1]) if '.' in name else ''
        if not EXT_MIME.get(ext):
            err(422, 'FILE_REJECTED', 'Use PDF, DOCX, XLSX, PPTX, PNG, JPG, CSV, TXT or MD files.', 'file')
        size = js_number(coalesce(_g(f, 'size_bytes'), 0))
        if not (size > 0):
            err(422, 'FILE_REJECTED', 'The file is empty. Choose another file.', 'file')
        if size > MAX_BYTES:
            err(422, 'FILE_REJECTED', 'The file is larger than 10 MB. Compress it or share a link instead.', 'file')
        du = _g(f, 'data_url')
        if not isinstance(du, str) or not du.startswith('data:'):
            err(422, 'FILE_REJECTED', 'The file could not be read. Choose it again.', 'file')
        fm = _g(f, 'mime')
        mime = fm if js_truthy(fm) and _includes(EXT_MIME[ext], fm) else EXT_MIME[ext][0]
        file = {**(f if isinstance(f, dict) else {}), 'name': name, 'mime': mime}
        return {'url': None, 'file_name': name, 'mime': mime, 'size_bytes': size, 'file': file}
    if url:
        if not _URL_RE.search(url):
            err(422, 'VALIDATION', 'Paste a full link that starts with https://.', 'url')
        name = url
        try:
            host, pathname = _whatwg_host_path(url)
            name = host + (pathname if len(pathname) > 1 else '')
        except _BadUrl:
            pass  # keep url
        return {'url': url, 'file_name': u16slice(name, 0, 120), 'mime': 'text/uri-list', 'size_bytes': 0}
    err(422, 'VALIDATION', 'Attach a file or paste a link to the document.', 'file')
    raise AssertionError  # unreachable


def _add_version(ctx: Ctx, d: dict, version_no: int, src: dict, note: str) -> dict:
    at = now_iso()
    ver = insert(
        ctx,
        'document_versions',
        {
            'document_id': d['id'],
            'version': version_no,
            'file_id': None,
            'url': src['url'],
            'file_name': src['file_name'],
            'size_bytes': src['size_bytes'],
            'mime': src['mime'],
            'note': note,
            'uploaded_by': ctx.user_id,
            'uploaded_at': at,
        },
    )
    file = src.get('file')
    if file is not None:
        fr = insert(
            ctx,
            'files',
            {
                'name': file.get('name', UNDEF),
                'mime': file.get('mime', UNDEF),
                'size_bytes': src['size_bytes'],
                'entity': 'document_version',
                'entity_id': ver['id'],
                'data_url': file.get('data_url', UNDEF),
            },
        )
        ver['file_id'] = fr['id']  # an owned (inserted) row: changed in place like the TS
    return ver


def _notify_must_read(ctx: Ctx, d: dict, mem: Members, is_new_version: bool) -> None:
    name = js_str(ctx.user.get('name', UNDEF))
    for u in _audience_of(ctx, d, mem):
        if u['id'] == ctx.user_id:
            continue
        what = f'published v{js_str(d.get("current_version", UNDEF))} of' if is_new_version else 'shared'
        notify(
            ctx,
            u['id'],
            'NTF-14',
            'Must-read document updated' if is_new_version else 'New must-read document',
            f'{name} {what} “{js_str(d.get("title", UNDEF))}”. Read it and acknowledge.',
            f'/documents?doc={d["id"]}',
        )


# ---------------------------------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------------------------------
def list_documents(req: Req) -> Any:
    ctx = req.ctx
    mem = members_of(ctx)
    qpid = req.q('project_id')
    scope_ = js_str(coalesce(req.q('scope'), 'project' if js_truthy(qpid) else 'mine'))
    project_id = js_str(qpid) if js_truthy(qpid) else None
    q = js_lower(_s(req.q('q')))
    cat_ = js_str(coalesce(req.q('category'), ''))
    archived = js_str(coalesce(req.q('archived'), '')) == 'true'
    if project_id:
        find(ctx, 'projects', project_id, 'Project')
    me = ctx.user_id
    visible = [d for d in rows(ctx, 'documents') if _can_see(ctx, d, mem)]
    acks = rows(ctx, 'document_acks')
    acked = {(a.get('document_id'), a.get('version')) for a in acks if a.get('user_id') == me}
    meview = _me(ctx)
    pending_count = sum(
        1
        for d in visible
        if not js_truthy(d.get('archived'))
        and js_truthy(d.get('must_read'))
        and _is_required(ctx, meview, d, mem)
        and (d['id'], d.get('current_version')) not in acked
    )
    lst = [d for d in visible if d.get('archived', UNDEF) is archived]
    if scope_ == 'project':
        lst = [d for d in lst if (d.get('project_id') == project_id if project_id else prop(d, 'project_id') is not None)]
    elif scope_ == 'company':
        lst = [d for d in lst if prop(d, 'project_id') is None]
    if cat_:
        lst = [d for d in lst if d.get('category') == cat_]
    items = [_to_item(ctx, d, mem) for d in lst]
    if q:
        items = [
            d
            for d in items
            if q
            in js_lower(
                js_join(
                    [d['title'], d['description'], d['file_name'], coalesce(d['project_name'], ''), coalesce(d['project_code'], ''), *(d['tags'] or [])], ' '
                )
            )
        ]
    items = sorted(items, key=lambda d: locale_key(d['updated_at']), reverse=True)
    if scope_ == 'company':
        can_upload = _can_manage(ctx, {'project_id': None}, mem)
    elif project_id:
        can_upload = _can_manage(ctx, {'project_id': project_id}, mem)
    else:
        can_upload = False
    return {'items': items, 'pending_count': pending_count, 'can_upload': can_upload}


def list_projects(req: Req) -> Any:
    ctx = req.ctx
    mem = members_of(ctx)
    me = ctx.user_id
    visible_docs = [d for d in rows(ctx, 'documents') if js_truthy(d.get('project_id')) and not js_truthy(d.get('archived')) and _can_see(ctx, d, mem)]
    acked = {(a.get('document_id'), a.get('version')) for a in rows(ctx, 'document_acks') if a.get('user_id') == me}
    by_project: dict[Any, list] = {}
    for d in visible_docs:
        by_project.setdefault(d['project_id'], []).append(d)
    view_all = has(ctx, 'documents.view_all')
    meview = _me(ctx)
    out = []
    for p in rows(ctx, 'projects'):
        if not (view_all or p.get('pm_id') == me or mem.has(p['id'], me) or p['id'] in by_project):
            continue
        docs = by_project.get(p['id'], [])
        out.append(
            {
                'id': p['id'],
                'code': prop(p, 'code'),
                'name': prop(p, 'name'),
                'status': prop(p, 'status'),
                'doc_count': len(docs),
                'pending': sum(
                    1 for d in docs if js_truthy(d.get('must_read')) and _is_required(ctx, meview, d, mem) and (d['id'], d.get('current_version')) not in acked
                ),
                'can_manage': _can_manage(ctx, {'project_id': p['id']}, mem),
            }
        )
    return sorted(out, key=lambda x: (0 if x['status'] == 'active' else 1, locale_key(x['name'])))


def policy_status(req: Req) -> Any:
    ctx = req.ctx
    if not any(k in ctx.perms for k in ('documents.company', 'documents.employee_files', 'documents.view_all')):
        err(403, 'FORBIDDEN', 'Acknowledgement status is for HR and managers.')
    mem = members_of(ctx)
    policies = [
        d
        for d in rows(ctx, 'documents')
        if not js_truthy(d.get('project_id')) and not js_truthy(d.get('archived')) and js_truthy(d.get('must_read')) and _can_see(ctx, d, mem)
    ]
    acked_set = {(a.get('document_id'), a.get('user_id'), a.get('version')) for a in rows(ctx, 'document_acks')}
    people = [u for u in rows(ctx, 'users') if u.get('status') == 'active' and not js_truthy(u.get('is_super_admin'))]
    viewers = [(u, Viewer(u['id'], set(permissions_of(u)))) for u in people]

    def acked(d: dict, uid_: str) -> bool:
        return (d['id'], uid_, d.get('current_version')) in acked_set

    pol = []
    for d in policies:
        aud = [x for x in viewers if _is_required(ctx, x[1], d, mem)]
        pol.append(
            {
                'id': d['id'],
                'title': prop(d, 'title'),
                'current_version': prop(d, 'current_version'),
                'effective_on': coalesce(prop(d, 'effective_on'), None),
                'acked': sum(1 for x in aud if acked(d, x[0]['id'])),
                'total': len(aud),
            }
        )
    pol = sorted(pol, key=lambda a: (a['acked'] / (a['total'] or 1), locale_key(a['title'])))
    ppl = []
    for u, v in viewers:
        req_ = [d for d in policies if _is_required(ctx, v, d, mem)]
        pending = [d for d in req_ if not acked(d, u['id'])]
        ppl.append(
            {
                'user_id': u['id'],
                'name': prop(u, 'name'),
                'required': len(req_),
                'acked': len(req_) - len(pending),
                'pending': [{'id': d['id'], 'title': prop(d, 'title')} for d in pending],
            }
        )
    ppl = sorted(ppl, key=lambda a: (-len(a['pending']), locale_key(a['name'])))
    return {'policies': pol, 'people': ppl}


def get_document(req: Req) -> Any:
    ctx = req.ctx
    mem = members_of(ctx)
    d = _load_doc(ctx, req.params['id'], mem)
    versions = [
        {**v, 'is_link': js_truthy(v.get('url'))}
        for v in sorted((v for v in rows(ctx, 'document_versions') if v.get('document_id') == d['id']), key=lambda v: v['version'], reverse=True)
    ]
    acks = [a for a in rows(ctx, 'document_acks') if a.get('document_id') == d['id']]
    views = [v for v in rows(ctx, 'document_views') if v.get('document_id') == d['id']]
    cv = d.get('current_version')
    mine = next((a for a in acks if a.get('user_id') == ctx.user_id and a.get('version') == cv), None)
    audience: Any = None
    if _can_manage(ctx, d, mem):
        aud = []
        for u in _audience_of(ctx, d, mem):
            ua = sorted((a for a in acks if a.get('user_id') == u['id']), key=lambda a: a['version'], reverse=True)
            cur = next((a for a in ua if a.get('version') == cv), None)
            lvs = sorted((v for v in views if v.get('user_id') == u['id']), key=lambda v: locale_key(v['at']), reverse=True)
            lv = lvs[0] if lvs else None
            aud.append(
                {
                    'user_id': u['id'],
                    'acked_at': coalesce(prop(cur, 'at'), None),
                    'acked_version': coalesce(prop(ua[0], 'version') if ua else UNDEF, None),
                    'last_viewed_at': coalesce(prop(lv, 'at'), None),
                }
            )
        audience = sorted(aud, key=lambda a: 1 if js_truthy(a['acked_at']) else 0)
    return {'doc': _to_item(ctx, d, mem), 'versions': versions, 'my_ack_at': coalesce(prop(mine, 'at'), None), 'view_count': len(views), 'audience': audience}


def create_document(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    mem = members_of(ctx)
    bp = _g(body, 'project_id')
    project_id = js_str(bp) if js_truthy(bp) else None
    if project_id:
        p = find(ctx, 'projects', project_id, 'Project')
        if not _can_manage(ctx, {'project_id': p['id']}, mem):
            err(403, 'FORBIDDEN', 'Only admins and the project manager can upload project documents.')
    elif not _can_manage(ctx, {'project_id': None}, mem):
        err(403, 'FORBIDDEN', 'Only admins and HR & operations can add to the company library.')
    title = _clean_title(_g(body, 'title'))
    category = _clean_category(_g(body, 'category'))
    visibility = _clean_visibility(coalesce(_g(body, 'visibility'), 'project' if project_id else 'company'), project_id)
    src = _take_source(body)
    description = u16slice(_s(_g(body, 'description')), 0, 2000)
    must_read = js_truthy(_g(body, 'must_read'))
    tags = _clean_tags(_g(body, 'tags'))
    data: dict[str, Any] = {
        'project_id': project_id,
        'title': title,
        'description': description,
        'category': category,
        'visibility': visibility,
        'must_read': must_read,
        'tags': tags,
    }
    if not project_id:
        data['effective_on'] = _clean_effective(_g(body, 'effective_on'))
    data.update({'owner_id': ctx.user_id, 'current_version': 1, 'archived': False})
    d = insert(ctx, 'documents', data)
    _add_version(ctx, d, 1, src, _s(_g(body, 'note')) or 'First version')
    if d['must_read']:
        _ack_for(ctx, d, ctx.user_id)  # the uploader has read what they published
    log_activity(ctx, 'document', d['id'], 'created', f'Uploaded “{d["title"]}” (v1)')
    if d['must_read']:
        _notify_must_read(ctx, d, mem, False)
    return _to_item(ctx, d, mem)


def add_version(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    mem = members_of(ctx)
    d = _load_doc(ctx, req.params['id'], mem)
    _require_manage(ctx, d, mem)
    if js_truthy(d.get('archived')):
        err(409, 'ARCHIVED', 'Restore the document before uploading a new version.')
    note = _s(_g(body, 'note'))
    if js_len(note) < 5:
        err(422, 'VALIDATION', 'Say what changed in this version (at least 5 characters).', 'note')
    src = _take_source(body)
    nxt = d['current_version'] + 1
    _add_version(ctx, d, nxt, src, u16slice(note, 0, 500))
    u = update(ctx, 'documents', d['id'], {'current_version': nxt})
    log_activity(ctx, 'document', d['id'], 'version', f'Uploaded v{js_str(nxt)} of “{js_str(d.get("title", UNDEF))}”: {note}')
    if js_truthy(u.get('must_read')):
        _ack_for(ctx, u, ctx.user_id)
        _notify_must_read(ctx, u, mem, True)
    return _to_item(ctx, u, mem)


def patch_document(req: Req) -> Any:
    ctx = req.ctx
    body = req.body
    mem = members_of(ctx)
    d = _load_doc(ctx, req.params['id'], mem)
    _require_manage(ctx, d, mem)
    p: dict[str, Any] = {}
    if _g(body, 'title') is not UNDEF:
        p['title'] = _clean_title(_g(body, 'title'))
    if _g(body, 'description') is not UNDEF:
        p['description'] = u16slice(_s(_g(body, 'description')), 0, 2000)
    if _g(body, 'category') is not UNDEF:
        p['category'] = _clean_category(_g(body, 'category'))
    if _g(body, 'visibility') is not UNDEF:
        p['visibility'] = _clean_visibility(_g(body, 'visibility'), d.get('project_id'))
    if _g(body, 'must_read') is not UNDEF:
        p['must_read'] = js_truthy(_g(body, 'must_read'))
    if _g(body, 'tags') is not UNDEF:
        p['tags'] = _clean_tags(_g(body, 'tags'))
    if _g(body, 'effective_on') is not UNDEF and not js_truthy(d.get('project_id')):
        p['effective_on'] = _clean_effective(_g(body, 'effective_on'))
    became_must_read = p.get('must_read', UNDEF) is True and not js_truthy(d.get('must_read'))
    u = update(ctx, 'documents', d['id'], p, req.if_match)
    log_activity(ctx, 'document', d['id'], 'updated', f'Updated details of “{js_str(u.get("title", UNDEF))}”')
    if became_must_read:
        _ack_for(ctx, u, ctx.user_id)
        _notify_must_read(ctx, u, mem, False)
    return _to_item(ctx, u, mem)


def _archive_handler(action: str, archived: bool) -> Any:
    def h(req: Req) -> Any:
        ctx = req.ctx
        mem = members_of(ctx)
        d = find(ctx, 'documents', req.params['id'], 'Document')
        if not _can_manage(ctx, d, mem):
            if not _can_see(ctx, d, mem):
                err(404, 'NOT_FOUND', 'Document not found.')
            _require_manage(ctx, d, mem)
        if d.get('archived', UNDEF) is archived:
            return _to_item(ctx, d, mem)
        u = update(ctx, 'documents', d['id'], {'archived': archived})
        log_activity(
            ctx,
            'document',
            d['id'],
            'archived' if action == 'archive' else 'restored',
            f'{"Archived" if action == "archive" else "Restored"} “{js_str(d.get("title", UNDEF))}”',
        )
        return _to_item(ctx, u, mem)

    return h


def ack_document(req: Req) -> Any:
    ctx = req.ctx
    d = _load_doc(ctx, req.params['id'])
    if js_truthy(d.get('archived')):
        err(409, 'ARCHIVED', 'This document is archived and no longer needs acknowledgement.')
    cv = d.get('current_version')
    me = ctx.user_id
    existing = next((a for a in rows(ctx, 'document_acks') if a.get('document_id') == d['id'] and a.get('user_id') == me and a.get('version') == cv), None)
    if existing is not None:
        return {'version': prop(existing, 'version'), 'at': prop(existing, 'at')}
    _ack_for(ctx, d, me)
    at = next(a for a in rows(ctx, 'document_acks') if a.get('document_id') == d['id'] and a.get('user_id') == me and a.get('version') == cv)['at']
    log_activity(ctx, 'document', d['id'], 'acknowledged', f'Acknowledged v{js_str(cv)} of “{js_str(d.get("title", UNDEF))}”')
    return {'version': cv, 'at': at}


def view_document(req: Req) -> Any:
    ctx = req.ctx
    d = _load_doc(ctx, req.params['id'])
    insert(ctx, 'document_views', {'document_id': d['id'], 'user_id': ctx.user_id, 'at': now_iso()})
    return {'ok': True}


def download_document(req: Req) -> Any:
    ctx = req.ctx
    d = _load_doc(ctx, req.params['id'])
    qv = req.q('version')
    vno = js_number(qv) if js_truthy(qv) else d.get('current_version')
    v = next((x for x in rows(ctx, 'document_versions') if x.get('document_id') == d['id'] and x.get('version') == vno and vno == vno), None)
    if v is None:
        err(404, 'NOT_FOUND', f'Version {js_str(vno)} not found.')
    file = None
    fid = v.get('file_id')
    if js_truthy(fid):
        fr = find_by('files', 'id', fid)
        if fr is not None and fr.get('tenant_id') == ctx.tenant_id:
            file = fr
        elif fr is not None:
            file = next((f for f in get_db().files if f.get('id') == fid and f.get('tenant_id') == ctx.tenant_id), None)
    # GET handlers may still record a view (reads don't go through the write guard, so View-as doesn't log).
    if not ctx.view_as and not js_truthy(req.q('preview')):
        insert(ctx, 'document_views', {'document_id': d['id'], 'user_id': ctx.user_id, 'at': now_iso()})
    return {'file_name': prop(v, 'file_name'), 'mime': prop(v, 'mime'), 'data_url': coalesce(prop(file, 'data_url'), None), 'url': prop(v, 'url')}


def register(r: Any) -> None:
    r.get('/documents', list_documents)
    r.get('/documents/projects', list_projects)
    r.get('/documents/policy-status', policy_status)
    r.get('/documents/:id', get_document)
    r.post('/documents', create_document)
    r.post('/documents/:id/versions', add_version)
    r.patch('/documents/:id', patch_document)
    for action, archived in (('archive', True), ('restore', False)):
        r.post(f'/documents/:id/{action}', _archive_handler(action, archived))
    r.post('/documents/:id/ack', ack_document)
    r.post('/documents/:id/view', view_document)
    r.get('/documents/:id/download', download_document)
