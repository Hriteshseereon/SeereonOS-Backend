"""v9 (A2, CEO request 5) — supporting documents on a leave application (medical certificate, invitation, …).

Optional when applying (POST /leave-requests body `attachments`), or added afterwards by the applicant:

  POST   /leave-requests/:id/attachments        {name, size_bytes, mime?, data_url}   applicant; pending or approved
  DELETE /leave-requests/:id/attachments/:aid                                          applicant; pending only
  GET    /leave-requests/:id/attachments/:aid   -> {file_name, mime, size_bytes, data_url}

Storage: the content is a `files` row (entity 'leave_request', entity_id = the request id); the request keeps only
metadata in leave_requests.attachments (V011) so leave lists stay small and never carry file content.

Who may open a document (server-side, can_open_docs): the applicant, the request's approvers (current approver, every
approver on its workflow route, whoever decided it) and their active approval delegates, and HR (people.manage). A
manager who only sees the request in the team list does not see the document (it may be medical).

Rules: up to MAX_FILES files, PDF / PNG / JPG / DOCX only, at most 5 MB each, non-empty, readable (a data: URL).
"""

from __future__ import annotations

from typing import Any

from app.ctx import Ctx, Req, err, find, get_db, has, insert, log_activity, notify, now_iso, patch, rows, scope, set_rows, uid
from app.js import UNDEF, coalesce, js_len, js_lower, js_number, js_str, js_trim, js_truthy, prop

MAX_FILES = 3
MAX_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024  # one request body (the upload limit is 15 MB of base64)
EXT_MIME = {
    'pdf': 'application/pdf',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
_ENTITY = 'leave_request'


def _g(b: Any, k: str) -> Any:
    return b.get(k, UNDEF) if isinstance(b, dict) else UNDEF


def _one(f: Any, field: str) -> dict[str, Any]:
    """Validate one uploaded file {name, size_bytes (or size), mime?, data_url} -> a clean dict (content included)."""
    if not isinstance(f, dict):
        err(422, 'FILE_REJECTED', 'Attach the document again.', field)
    name = js_trim(js_str(coalesce(_g(f, 'name'), '')))
    if not name:
        err(422, 'FILE_REJECTED', 'The document needs a file name.', field)
    if js_len(name) > 160:
        err(422, 'FILE_REJECTED', 'Keep the file name under 160 characters.', field)
    ext = js_lower(name.rsplit('.', 1)[-1]) if '.' in name else ''
    mime = EXT_MIME.get(ext)
    if mime is None:
        err(422, 'FILE_REJECTED', f'{name}: use a PDF, PNG, JPG or DOCX file.', field)
    size = js_number(coalesce(_g(f, 'size_bytes'), _g(f, 'size'), 0))
    if not (size > 0):
        err(422, 'FILE_REJECTED', f'{name} is empty. Choose another file.', field)
    if size > MAX_BYTES:
        err(422, 'FILE_REJECTED', f'{name} is larger than 5 MB. Scan it at a lower resolution or upload a PDF.', field)
    du = _g(f, 'data_url')
    if not isinstance(du, str) or not du.startswith('data:') or ';base64,' not in du[:120]:
        err(422, 'FILE_REJECTED', f'{name} could not be read. Choose it again.', field)
    # the decoded size of the content may not exceed the limit either (the declared size is the browser's)
    if (len(du) - du.index(',') - 1) * 3 // 4 > MAX_BYTES + 3:
        err(422, 'FILE_REJECTED', f'{name} is larger than 5 MB. Scan it at a lower resolution or upload a PDF.', field)
    return {'name': name, 'mime': mime, 'size_bytes': int(size), 'data_url': du}


def validate_new(ctx: Ctx, raw: Any, have: int = 0) -> list[dict[str, Any]]:
    """The `attachments` of a request body (absent / null = none)."""
    if raw is UNDEF or raw is None:
        return []
    if not isinstance(raw, list):
        err(422, 'FILE_REJECTED', 'Attach documents as a list of files.', 'attachments')
    if have + len(raw) > MAX_FILES:
        err(422, 'FILE_REJECTED', f'Attach up to {MAX_FILES} documents to a leave request.', 'attachments')
    out = [_one(f, 'attachments') for f in raw]
    if sum(f['size_bytes'] for f in out) > MAX_TOTAL_BYTES:
        err(422, 'FILE_REJECTED', 'Attach up to 10 MB in one go. Add the other documents to the request after applying.', 'attachments')
    return out


def store(ctx: Ctx, req_row: dict, files: list[dict[str, Any]]) -> dict:
    """Save validated files for a leave request; returns the request row with its new metadata list."""
    if not files:
        return req_row
    cur = list(coalesce(prop(req_row, 'attachments'), []))
    at = now_iso()
    for f in files:
        fr = insert(
            ctx,
            'files',
            {'name': f['name'], 'mime': f['mime'], 'size_bytes': f['size_bytes'], 'entity': _ENTITY, 'entity_id': req_row['id'], 'data_url': f['data_url']},
        )
        cur.append(
            {
                'id': uid('lda'),
                'file_id': fr['id'],
                'name': f['name'],
                'mime': f['mime'],
                'size_bytes': f['size_bytes'],
                'uploaded_by': ctx.user_id,
                'uploaded_at': at,
            }
        )
    return patch('leave_requests', req_row, {'attachments': cur})


def can_open_docs(ctx: Ctx, r: dict) -> bool:
    me = ctx.user_id
    if r.get('user_id') == me or has(ctx, 'people.manage'):
        return True
    ids = {r.get('approver_id'), r.get('decided_by')}
    for st in coalesce(prop(r, 'wf_chain'), []) or []:
        if isinstance(st, dict):
            ids.add(st.get('approver_id'))
    ids.discard(None)
    if me in ids:
        return True
    today = ctx.today
    return any(
        d.get('to_user_id') == me and d.get('from_user_id') in ids and d['from_on'] <= today and d['to_on'] >= today and not js_truthy(d.get('project_id'))
        for d in rows(ctx, 'delegations')
    )


def view_extra(ctx: Ctx, r: dict) -> dict[str, Any]:
    """Fields the leave lists add for a request with documents (absent when it has none)."""
    if not coalesce(prop(r, 'attachments'), []):
        return {}
    return {'can_open_docs': can_open_docs(ctx, r)}


def _request(ctx: Ctx, id_: Any) -> dict:
    return find(ctx, 'leave_requests', id_, 'Leave request')


def add_route(req: Req) -> Any:
    ctx = req.ctx
    r = _request(ctx, req.params['id'])
    if r['user_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person who applied can add documents to a leave request.')
    if r['status'] not in ('pending', 'approved'):
        err(409, 'NOT_PENDING', f'This request is {r["status"]}. Apply again to attach documents.')
    have = len(coalesce(prop(r, 'attachments'), []))
    if have >= MAX_FILES:
        err(422, 'FILE_REJECTED', f'A leave request holds up to {MAX_FILES} documents. Remove one first.', 'attachments')
    files = [_one(req.body, 'attachments')]
    out = store(ctx, r, files)
    log_activity(ctx, 'leave_request', r['id'], 'document', f'Attached {files[0]["name"]} to the leave request')
    ap = r.get('approver_id') if r['status'] == 'pending' else None
    if js_truthy(ap) and ap != ctx.user_id:
        notify(
            ctx,
            ap,
            'NTF-04',
            'Document added to a leave request',
            f'{js_str(ctx.user.get("name", UNDEF))} attached {files[0]["name"]} to their leave request.',
            '/leave',
        )
    return scope().find_by('leave_requests', 'id', out['id']) or out


def remove_route(req: Req) -> Any:
    ctx = req.ctx
    r = _request(ctx, req.params['id'])
    if r['user_id'] != ctx.user_id:
        err(403, 'FORBIDDEN', 'Only the person who applied can remove their documents.')
    if r['status'] != 'pending':
        err(409, 'NOT_PENDING', 'The request is decided: its documents stay on record.')
    cur = list(coalesce(prop(r, 'attachments'), []))
    a = next((x for x in cur if isinstance(x, dict) and x.get('id') == req.params['aid']), None)
    if a is None:
        err(404, 'NOT_FOUND', 'That document was already removed.')
    tid = ctx.tenant_id
    fid = a.get('file_id')
    set_rows('files', [f for f in get_db().files if not (f.get('tenant_id') == tid and f.get('id') == fid)])
    out = patch('leave_requests', r, {'attachments': [x for x in cur if x is not a]})
    log_activity(ctx, 'leave_request', r['id'], 'document', f'Removed {js_str(a.get("name"))} from the leave request')
    return scope().find_by('leave_requests', 'id', out['id']) or out


def file_route(req: Req) -> Any:
    ctx = req.ctx
    r = _request(ctx, req.params['id'])
    if not can_open_docs(ctx, r):
        err(403, 'FORBIDDEN', 'Only the applicant, their approvers and HR can open leave documents.')
    a = next((x for x in coalesce(prop(r, 'attachments'), []) if isinstance(x, dict) and x.get('id') == req.params['aid']), None)
    if a is None:
        err(404, 'NOT_FOUND', 'That document is no longer attached.')
    fid = a.get('file_id')
    f = next((x for x in rows(ctx, 'files') if x.get('id') == fid), None)
    if f is None:
        err(404, 'NOT_FOUND', 'The file is missing. Ask the applicant to attach it again.')
    return {
        'file_name': a.get('name'),
        'mime': coalesce(a.get('mime'), prop(f, 'mime'), 'application/octet-stream'),
        'size_bytes': a.get('size_bytes'),
        'data_url': prop(f, 'data_url'),
    }


def register(r: Any) -> None:
    r.post('/leave-requests/:id/attachments', add_route)
    r.delete('/leave-requests/:id/attachments/:aid', remove_route)
    r.get('/leave-requests/:id/attachments/:aid', file_route)
