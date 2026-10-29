"""Recruiting — openings (UI-35) and the public careers page (v9, decision D7).

The openings handlers were moved out of app/routes/projects.py in v9 with identical patterns and behaviour; the module
is registered right after `projects` (app/router.py MODULES) so first-match order is unchanged. Candidates / interviews
stay in app/routes/hr.py.

v9 additions (all optional fields — openings without them answer exactly as before):
* job description on an opening: about, responsibilities, requirements, nice_to_have, location, work_mode,
  employment_type, experience_min/max, salary_min/max_paise + salary_visible, accepting_applications;
* a public apply link per opening: `POST /openings/:id/public-link {regenerate?}` creates (or replaces) the random
  token; the opening view then carries `public_url` (APP_URL + /careers/<token>), `accepting_now` and `applications`;
* the public API (no session): `GET /public/openings/:token` (company + job description; closed links say so) and
  `POST /public/openings/:token` (apply: candidate with source "careers" + resume, honeypot, file checks; refused when
  the opening is not accepting). The tenant is found through the SECURITY DEFINER lookup seereon_opening_tenant (V012)
  and the handler runs in that tenant's unit of work. Rate limits: app/main.py (DB-backed, per IP).
"""

from __future__ import annotations

import base64
import re
import secrets
from typing import Any

from app.ctx import (
    Ctx,
    Req,
    err,
    find,
    has,
    has_any,
    insert,
    log_activity,
    notify,
    now_iso,
    permissions_of,
    remove,
    rows,
    scope,
    update,
    users_with,
)
from app.domain.calendar import is_real_iso_date
from app.domain.file_safety import check_data_url, sniff
from app.js import (
    UNDEF,
    coalesce,
    is_finite,
    is_integer,
    js_len,
    js_number,
    js_or,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    prop,
)
from app.routes import tasks as _tasks


# ---------- small helpers (same semantics as the ones in routes/projects.py) ----------
def _iso_test(s: Any) -> bool:
    return is_real_iso_date(s)


def _in(v: Any, lst: list) -> bool:
    """lst.includes(v) for a list of strings (non-strings never match)"""
    return isinstance(v, str) and v in lst


def _intify(n: Any) -> Any:
    return int(n) if isinstance(n, float) and n.is_integer() and abs(n) < 2**53 else n


def _need(v: Any, field: str, label: str) -> str:
    s = js_trim(v) if isinstance(v, str) else ''
    if not s:
        err(422, 'VALIDATION', f'{label} is required.', field)
    return s


def _active_user(ctx: Ctx, id_: Any, field: str, must_manage_projects: bool = False) -> dict | None:
    if not js_truthy(id_):
        return None
    u = _tasks._user(ctx, id_)
    if u is None or u.get('status') != 'active':
        err(422, 'VALIDATION', 'Choose an active person from your team.', field)
    if must_manage_projects and 'projects.edit' not in permissions_of(u):
        err(422, 'VALIDATION', f'{js_str(u["name"])} can’t manage projects. Choose a project manager or an admin.', field)
    return u


# ---------- openings (UI-35) ----------
_OPEN_STATUS = ['open', 'interviewing', 'offered', 'filled', 'on_hold']
_OPEN_RANK = {'open': 0, 'interviewing': 1, 'offered': 2, 'on_hold': 3, 'filled': 4}


def _opening_guard(ctx: Ctx, o: dict | None = None) -> None:
    if not has(ctx, 'openings.manage'):
        err(403, 'FORBIDDEN', 'Only admins, project managers and operations manage openings.')
    if o is not None and not has_any(ctx, 'projects.manage_all', 'people.view_all'):
        pid = o.get('project_id')
        pr = next((x for x in rows(ctx, 'projects') if x['id'] == pid), None) if js_truthy(pid) else None
        me = ctx.user['id']
        if o.get('owner_id') != me and prop(pr, 'pm_id') != me and o.get('created_by') != me:
            err(403, 'FORBIDDEN', 'You can change openings you own or that are on your projects.')


def _validate_opening(ctx: Ctx, b: Any, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    bb = b if isinstance(b, dict) else {}
    if not partial or 'role' in bb:
        out['role'] = _need(bb.get('role', UNDEF), 'role', 'Role')
    if not partial or 'count' in bb:
        n = js_number(bb.get('count', UNDEF))
        if not is_integer(n) or n < 1 or n > 50:
            err(422, 'VALIDATION', 'Count must be between 1 and 50.', 'count')
        out['count'] = _intify(n)
    if not partial or 'status' in bb:
        s = coalesce(bb.get('status', UNDEF), 'open')
        if not _in(s, _OPEN_STATUS):
            err(422, 'VALIDATION', 'Choose a valid status.', 'status')
        out['status'] = s
    if not partial or 'owner_id' in bb:
        if not js_truthy(bb.get('owner_id', UNDEF)):
            err(422, 'VALIDATION', 'Choose who owns this opening.', 'owner_id')
        out['owner_id'] = _active_user(ctx, bb['owner_id'], 'owner_id')['id']  # type: ignore[index]
    if not partial or 'project_id' in bb:
        pid = js_or(bb.get('project_id', UNDEF), None)
        if js_truthy(pid):
            find(ctx, 'projects', pid, 'Project')
        out['project_id'] = pid
    if not partial or 'needed_by' in bb:
        d = js_or(bb.get('needed_by', UNDEF), None)
        if js_truthy(d) and not _iso_test(d):
            err(422, 'VALIDATION', 'Needed by must be a date.', 'needed_by')
        out['needed_by'] = d
    if not partial or 'notes' in bb:
        out['notes'] = js_trim(js_str(coalesce(bb.get('notes', UNDEF), '')))
    out.update(_validate_jd(bb))
    return out


# ---------- v9: job description (every key optional; only the keys sent are validated and stored) ----------
WORK_MODES = ['onsite', 'hybrid', 'remote']
EMPLOYMENT_TYPES = ['full_time', 'part_time', 'contract', 'internship']
_JD_TEXT = {
    'about': ('About the role', 4000),
    'responsibilities': ('Responsibilities', 4000),
    'requirements': ('Requirements', 4000),
    'nice_to_have': ('Nice to have', 3000),
}
# accepting applications on the public link: these statuses (and accepting_applications not switched off)
ACCEPTING_STATUSES = ('open', 'interviewing')


def _opt_number(v: Any, field: str, label: str, lo: float, hi: float, integer: bool = False) -> Any:
    if v is None or v == '':
        return None
    n = js_number(v)
    if not is_finite(n) or n < lo or n > hi or (integer and not is_integer(n)):
        err(422, 'VALIDATION', label, field)
    return _intify(n)


def _validate_jd(bb: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, (label, mx) in _JD_TEXT.items():
        if k in bb:
            v = bb[k]
            t = js_trim(v) if isinstance(v, str) else ''
            if v is not None and not isinstance(v, str):
                err(422, 'VALIDATION', f'{label} must be text.', k)
            if js_len(t) > mx:
                err(422, 'VALIDATION', f'{label} is limited to {mx} characters.', k)
            out[k] = t
    if 'location' in bb:
        v = bb['location']
        t = js_trim(v) if isinstance(v, str) else ''
        if js_len(t) > 120:
            err(422, 'VALIDATION', 'Keep the location under 120 characters.', 'location')
        out['location'] = t
    for k, lst, label in (
        ('work_mode', WORK_MODES, 'Choose on-site, hybrid or remote.'),
        ('employment_type', EMPLOYMENT_TYPES, 'Choose a valid employment type.'),
    ):
        if k in bb:
            v = bb[k]
            if v is None or v == '':
                out[k] = None
            elif not _in(v, lst):
                err(422, 'VALIDATION', label, k)
            else:
                out[k] = v
    if 'experience_min' in bb:
        out['experience_min'] = _opt_number(bb['experience_min'], 'experience_min', 'Experience is between 0 and 50 years.', 0, 50)
    if 'experience_max' in bb:
        out['experience_max'] = _opt_number(bb['experience_max'], 'experience_max', 'Experience is between 0 and 50 years.', 0, 50)
    if 'salary_min_paise' in bb:
        out['salary_min_paise'] = _opt_number(bb['salary_min_paise'], 'salary_min_paise', 'Enter a valid amount.', 0, 1e14, True)
    if 'salary_max_paise' in bb:
        out['salary_max_paise'] = _opt_number(bb['salary_max_paise'], 'salary_max_paise', 'Enter a valid amount.', 0, 1e14, True)
    for k in ('salary_visible', 'accepting_applications'):
        if k in bb:
            if not isinstance(bb[k], bool):
                err(422, 'VALIDATION', 'Choose yes or no.', k)
            out[k] = bb[k]
    return out


def _check_ranges(o: dict) -> None:
    a, b = o.get('experience_min'), o.get('experience_max')
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > b:
        err(422, 'VALIDATION', 'Minimum experience can’t be more than the maximum.', 'experience_min')
    a, b = o.get('salary_min_paise'), o.get('salary_max_paise')
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > b:
        err(422, 'VALIDATION', 'The lower salary can’t be more than the upper one.', 'salary_min_paise')
    if o.get('salary_visible') is True and not (isinstance(a, (int, float)) or isinstance(b, (int, float))):
        err(422, 'VALIDATION', 'Enter a salary range to show it on the job page.', 'salary_min_paise')


def accepting_now(ctx: Ctx, o: dict) -> bool:
    """does the public link take applications right now (status, the manual switch, the workspace)"""
    t = ctx.tenant
    return (
        js_truthy(o.get('public_token'))
        and o.get('status') in ACCEPTING_STATUSES
        and o.get('accepting_applications', UNDEF) is not False
        and prop(t, 'status') not in ('suspended', 'read_only')
    )


def _public_url(token: str) -> str:
    from app.config import settings

    base = (settings().app_url or '').rstrip('/')
    return f'{base}/careers/{token}'


def _new_token() -> str:
    return secrets.token_urlsafe(32)  # 43 URL-safe characters (256 bits)


def _opening_view(ctx: Ctx, o: dict) -> dict[str, Any]:
    pid = o.get('project_id')
    pr = next((x for x in rows(ctx, 'projects') if x['id'] == pid), None) if js_truthy(pid) else None
    out = {**o, 'project_code': coalesce(prop(pr, 'code'), None), 'project_name': coalesce(prop(pr, 'name'), None)}
    tok = o.get('public_token')
    if js_truthy(tok):  # link facts only once a link exists (openings without one answer exactly as before v9)
        out['public_url'] = _public_url(tok)
        out['accepting_now'] = accepting_now(ctx, o)
        out['applications'] = sum(1 for c in rows(ctx, 'candidates') if c.get('opening_id') == o['id'] and c.get('source') == 'careers')
    return out


def list_openings(req: Req) -> Any:
    ctx = req.ctx
    _opening_guard(ctx)
    return sorted(
        (_opening_view(ctx, o) for o in rows(ctx, 'openings')),
        key=lambda o: (_OPEN_RANK[o['status']], locale_key(coalesce(prop(o, 'needed_by'), '9999'))),
    )


def add_opening(req: Req) -> Any:
    ctx = req.ctx
    _opening_guard(ctx)
    v = _validate_opening(ctx, req.body, False)
    _check_ranges(v)
    if req.b('publish_link') is True:  # v9: create the public apply link with the opening
        v['public_token'] = _new_token()
    o = insert(ctx, 'openings', v)
    log_activity(ctx, 'opening', o['id'], 'opening.created', f'Opening: {js_str(o["count"])} × {js_str(o["role"])}')
    if o['owner_id'] != ctx.user['id']:
        notify(ctx, o['owner_id'], 'NTF-14', 'Opening assigned to you', f'{js_str(o["count"])} × {js_str(o["role"])}', '/openings')
    return _opening_view(ctx, o) if js_truthy(o.get('public_token')) else o


def patch_opening(req: Req) -> Any:
    ctx = req.ctx
    o = find(ctx, 'openings', req.params['id'], 'Opening')
    _opening_guard(ctx, o)
    v = _validate_opening(ctx, req.body, True)
    _check_ranges({**o, **v})
    out = update(ctx, 'openings', o['id'], v, req.if_match)
    log_activity(ctx, 'opening', o['id'], 'opening.updated', f'Opening {js_str(out["role"])}: {js_str(out["status"]).replace("_", " ", 1)}')
    return _opening_view(ctx, out) if js_truthy(out.get('public_token')) else out


def opening_link(req: Req) -> Any:
    """POST /openings/:id/public-link {regenerate?} — create the public apply link, or replace it (the old one stops)"""
    ctx = req.ctx
    o = find(ctx, 'openings', req.params['id'], 'Opening')
    _opening_guard(ctx, o)
    had = js_truthy(o.get('public_token'))
    if had and req.b('regenerate') is not True:
        return _opening_view(ctx, o)
    out = update(ctx, 'openings', o['id'], {'public_token': _new_token()}, req.if_match)
    log_activity(
        ctx,
        'opening',
        o['id'],
        'opening.link_regenerated' if had else 'opening.link_created',
        f'{"New public link for" if had else "Public apply link created for"} {js_str(out["role"])}{" (the old link no longer works)" if had else ""}',
    )
    return _opening_view(ctx, out)


def delete_opening(req: Req) -> Any:
    ctx = req.ctx
    o = find(ctx, 'openings', req.params['id'], 'Opening')
    _opening_guard(ctx, o)
    remove(ctx, 'openings', o['id'])
    log_activity(ctx, 'opening', o['id'], 'opening.deleted', f'Removed opening {js_str(o["role"])}')
    return None


# =====================================================================================================================
# Public careers page (no session) — D7
# =====================================================================================================================
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{32,64}\Z')
RESUME_MAX_BYTES = 5 * 1024 * 1024
# extension -> (accepted declared types, the signature the bytes must have — app.domain.file_safety.sniff)
RESUME_TYPES = {
    'pdf': (('application/pdf',), 'pdf', 'application/pdf'),
    'doc': (('application/msword',), 'ole', 'application/msword'),
    'docx': (
        ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',),
        'zip',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    ),
}
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+\Z')
_PHONE_RE = re.compile(r'^\+?[0-9][0-9 ()-]{6,19}\Z')
_LINKEDIN_RE = re.compile(r'^https://([a-z]{2,3}\.)?linkedin\.com/[^\s<>"]{2,250}\Z', re.I)
HONEYPOT = 'website'  # a field people never see; bots fill it
NOT_FOUND_MSG = 'This job link is not valid. Check the address, or ask the company for a new link.'


def _lines(t: Any) -> list[str]:
    return [x.strip(' \t-•*') for x in js_str(coalesce(t, '')).split('\n') if x.strip(' \t-•*')]


def _in_tenant_of(token: str, fn: Any, *, write: bool) -> Any:
    """run fn(ctx) in the unit of work of the tenant that owns the apply token (404 for an unknown token)"""
    if not TOKEN_RE.match(token):
        err(404, 'NOT_FOUND', NOT_FOUND_MSG)
    rt = getattr(scope(), 'rt', None)  # the runtime serving this request (its outer, session-less unit of work)
    tid = rt.backend.opening_tenant(token) if rt is not None else None
    if not tid:
        err(404, 'NOT_FOUND', NOT_FOUND_MSG)

    def run(_s: Any) -> Any:
        tenant = scope().find_by('tenants', 'id', tid)
        if tenant is None or prop(tenant, 'status') == 'suspended':
            err(404, 'NOT_FOUND', NOT_FOUND_MSG)
        ctx = Ctx.synthetic_ctx(tenant, {'id': 'system', 'name': 'Careers page'})
        o = next((x for x in rows(ctx, 'openings') if x.get('public_token') == token), None)
        if o is None:
            err(404, 'NOT_FOUND', NOT_FOUND_MSG)
        return fn(ctx, o)

    return rt.run_scoped('tenant', tid, run, is_write=write)


def public_view(ctx: Ctx, o: dict) -> dict[str, Any]:
    """what the public careers page shows: the company and the job description — never internal notes, owners,
    projects or clients"""
    t = ctx.tenant
    org = prop(t, 'org') if isinstance(prop(t, 'org'), dict) else {}
    accepting = accepting_now(ctx, o)
    show_salary = o.get('salary_visible') is True
    st = o.get('status')
    return {
        'company': {
            'name': coalesce(prop(org, 'display_name'), prop(t, 'name'), ''),
            'logo_data_url': coalesce(prop(t, 'logo_data_url'), None),
            'website': coalesce(prop(org, 'website'), None),
            'primary': coalesce(prop(prop(t, 'theme'), 'primary'), None),
        },
        'opening': {
            'role': o['role'],
            'count': o['count'],
            'about': coalesce(o.get('about', UNDEF), ''),
            'responsibilities': _lines(o.get('responsibilities')),
            'requirements': _lines(o.get('requirements')),
            'nice_to_have': _lines(o.get('nice_to_have')),
            'location': coalesce(o.get('location', UNDEF), '') or '',
            'work_mode': coalesce(o.get('work_mode', UNDEF), None),
            'employment_type': coalesce(o.get('employment_type', UNDEF), None),
            'experience_min': coalesce(o.get('experience_min', UNDEF), None),
            'experience_max': coalesce(o.get('experience_max', UNDEF), None),
            'salary_min_paise': coalesce(o.get('salary_min_paise', UNDEF), None) if show_salary else None,
            'salary_max_paise': coalesce(o.get('salary_max_paise', UNDEF), None) if show_salary else None,
            'posted_on': o['created_at'][:10],
        },
        'accepting': accepting,
        'closed_reason': None
        if accepting
        else ('filled' if st == 'filled' else 'paused' if st == 'on_hold' or o.get('accepting_applications', UNDEF) is False else 'closed'),
        'max_resume_mb': RESUME_MAX_BYTES // (1024 * 1024),
    }


def public_opening(req: Req) -> Any:
    token = js_str(coalesce(req.params.get('token'), ''))
    return _in_tenant_of(token, public_view, write=False)


def _txt(b: dict, k: str, label: str, mn: int, mx: int, required: bool) -> str:
    v = b.get(k)
    t = js_trim(v) if isinstance(v, str) else ''
    if required and js_len(t) < mn:
        err(422, 'VALIDATION', label, k)
    if js_len(t) > mx:
        err(422, 'VALIDATION', f'Keep this under {mx} characters.', k)
    return t


def _resume(b: dict) -> dict[str, Any]:
    f = b.get('resume')
    if not isinstance(f, dict):
        err(422, 'VALIDATION', 'Attach your resume (PDF, DOC or DOCX, up to 5 MB).', 'resume')
    name = js_trim(js_str(coalesce(f.get('name'), '')))[:160]
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    spec = RESUME_TYPES.get(ext)
    if not name or spec is None:
        err(422, 'FILE_REJECTED', 'Upload your resume as a PDF, DOC or DOCX file.', 'resume')
    du = f.get('data_url')
    if not isinstance(du, str) or not du.startswith('data:') or ',' not in du:
        err(422, 'FILE_REJECTED', 'The file could not be read. Choose it again.', 'resume')
    head, b64 = du[5:].split(',', 1)
    declared = head.split(';', 1)[0].strip().lower()
    if declared and declared not in spec[0] and declared != 'application/octet-stream':
        err(422, 'FILE_REJECTED', 'Upload your resume as a PDF, DOC or DOCX file.', 'resume')
    size = (len(b64) * 3) // 4 - (2 if b64.endswith('==') else 1 if b64.endswith('=') else 0)
    if size <= 0:
        err(422, 'FILE_REJECTED', 'The file is empty. Choose another file.', 'resume')
    if size > RESUME_MAX_BYTES:
        err(422, 'FILE_TOO_LARGE', 'The resume is larger than 5 MB. Save a smaller PDF and try again.', 'resume')
    bad = check_data_url(du, f.get('size_bytes'))
    if bad:
        err(422, 'FILE_REJECTED', bad['message'], 'resume')
    chunk = re.sub(r'[^A-Za-z0-9+/]', '', b64[:4096])
    chunk = chunk[: len(chunk) - len(chunk) % 4]
    try:
        first = base64.b64decode(chunk)
    except Exception:  # noqa: BLE001
        first = b''
    if sniff(first) != spec[1]:
        err(422, 'FILE_REJECTED', 'This file isn’t a real PDF or Word document. Export your resume again and upload it.', 'resume')
    return {'name': name, 'mime': spec[2], 'size_bytes': size, 'data_url': du}


def public_apply(req: Req) -> Any:
    token = js_str(coalesce(req.params.get('token'), ''))
    b = req.body if isinstance(req.body, dict) else {}

    def apply(ctx: Ctx, o: dict) -> Any:
        # a filled honeypot looks like success to the bot and stores nothing
        hp = b.get(HONEYPOT)
        if isinstance(hp, str) and hp.strip():
            return {'received': True, 'role': o['role']}
        if not accepting_now(ctx, o):
            err(409, 'OPENING_CLOSED', 'This opening is no longer accepting applications.')
        name = _txt(b, 'name', 'Enter your full name.', 2, 100, True)
        email = _txt(b, 'email', 'Enter a valid email address.', 3, 254, True).lower()
        if not _EMAIL_RE.search(email):
            err(422, 'VALIDATION', 'Enter a valid email address.', 'email')
        phone = _txt(b, 'phone', 'Enter your phone number.', 7, 20, True)
        if not _PHONE_RE.match(phone):
            err(422, 'VALIDATION', 'Enter a phone number with 7 to 15 digits (you may start with +).', 'phone')
        linkedin = _txt(b, 'linkedin_url', '', 0, 300, False)
        if linkedin and not _LINKEDIN_RE.match(linkedin):
            err(422, 'VALIDATION', 'Paste the link to your LinkedIn profile (https://www.linkedin.com/in/…).', 'linkedin_url')
        company = _txt(b, 'current_company', '', 0, 120, False)
        title = _txt(b, 'current_title', '', 0, 120, False)
        location = _txt(b, 'location', 'Tell us where you are based.', 2, 120, True)
        exp = js_number(b.get('experience_years', UNDEF))
        if b.get('experience_years') in (None, '') or not is_finite(exp) or exp < 0 or exp > 50:
            err(422, 'VALIDATION', 'Experience is between 0 and 50 years.', 'experience_years')
        nd = js_number(b.get('notice_days', UNDEF))
        if b.get('notice_days') in (None, '') or not is_integer(nd) or nd < 0 or nd > 180:
            err(422, 'VALIDATION', 'Choose your notice period.', 'notice_days')
        ectc_raw = b.get('expected_ctc_paise')
        ectc = None
        if ectc_raw not in (None, ''):
            ectc = js_number(ectc_raw)
            if not is_integer(ectc) or ectc < 0 or ectc > 1e13:
                err(422, 'VALIDATION', 'Enter your expected annual CTC as an amount, or leave it empty.', 'expected_ctc_paise')
        cover = _txt(b, 'cover_note', '', 0, 3000, False)
        if b.get('consent') is not True:
            err(422, 'VALIDATION', 'Please agree to the use of your details for this application.', 'consent')
        file = _resume(b)
        if any(c.get('opening_id') == o['id'] and c.get('email') == email for c in rows(ctx, 'candidates')):
            err(409, 'ALREADY_APPLIED', 'You have already applied for this role with this email address. The team will be in touch.', 'email')
        c = insert(
            ctx,
            'candidates',
            {
                'opening_id': o['id'],
                'name': name,
                'email': email,
                'phone': phone,
                'source': 'careers',
                'stage': 'applied',
                'experience_years': _intify(exp),
                'current_ctc_paise': None,
                'expected_ctc_paise': _intify(ectc) if ectc is not None else None,
                'offer_ctc_paise': None,
                'notice_days': _intify(nd),
                'resume_file_id': None,
                'rating': None,
                'notes': '',
                'rejected_reason': None,
                'joining_on': None,
                'user_id': None,
                'linkedin_url': linkedin or None,
                'current_company': company or None,
                'current_title': title or None,
                'location': location,
                'cover_note': cover or None,
                'consent_at': now_iso(),
            },
        )
        f = insert(ctx, 'files', {**file, 'entity': 'candidate', 'entity_id': c['id']})
        update(ctx, 'candidates', c['id'], {'resume_file_id': f['id']})
        log_activity(ctx, 'candidate', c['id'], 'created', f'{name} applied for {o["role"]} through the careers link')
        notified: set = set()
        to = [o.get('owner_id')] + [u['id'] for u in users_with(ctx, 'recruitment.manage') if u.get('role') == 'ops']
        for uid_ in to:
            if js_truthy(uid_) and uid_ not in notified:
                notified.add(uid_)
                notify(ctx, uid_, 'NTF-14', 'New application', f'{name} applied for {o["role"]} through the careers link.', '/recruitment')
        return {'received': True, 'role': o['role']}

    return _in_tenant_of(token, apply, write=True)


def register(r: Any) -> None:
    r.get('/openings', list_openings)
    r.post('/openings', add_opening)
    r.patch('/openings/:id', patch_opening)
    r.delete('/openings/:id', delete_opening)
    # v9 (new patterns; no earlier route matches them)
    r.post('/openings/:id/public-link', opening_link)
    r.get('/public/openings/:token', public_opening, public=True)
    r.post('/public/openings/:token', public_apply, public=True)
