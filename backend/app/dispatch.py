"""The router core — port of dispatch() of src/mock/server.ts as the API server runs it (server/src/runtime.ts).

route match -> ctx (session, View-as) -> platform-admin check -> write checks (View-as read-only 403, lapsed tenant
402) -> must-change-password guard -> file safety on non-GET bodies -> handler -> {data, warnings} serialised at once
(the reference clones the handler's return value at that moment).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import orjson

from app.ctx import Req, anon_ctx, assert_writable, build_ctx
from app.db.uow import current_scope
from app.domain.file_safety import find_file_problem
from app.errors import ApiError
from app.js import REQUEST_NOW, UNDEF, js_json_dumps, js_str, request_clock_now
from app.runtime import Envelope, if_match_number

PASSWORD_CHANGE_PATHS = ('/me/password', '/auth/logout', '/me/prefs')


def resolver(session: dict | None) -> Callable[[dict[str, str]], Any]:
    """session lookup + Ctx (the user must still exist in the tenant and not be deactivated)"""

    def resolve(headers: dict[str, str]) -> Any:
        if not session:
            return None
        s = current_scope()
        user = None
        for u in s.group('users', 'id').get(session['user_id'], ()):
            if u.get('tenant_id') == session['tenant_id']:
                user = u
                break
        if user is None or user.get('status') == 'deactivated':
            return None
        return build_ctx(user, session['tenant_id'], headers)

    return resolve


def parse_body(raw: bytes | None) -> Any:
    """a fresh copy of the request body for one attempt (UNDEF when there is none)"""
    if not raw:
        return UNDEF
    return orjson.loads(raw)


def dispatch(rt: Any, match: tuple[Any, dict[str, str]], req: Any, headers: dict[str, str], resolve_ctx: Callable[[dict[str, str]], Any]) -> Envelope:
    route, params = match
    method = req.method
    path = req.path
    ctx = resolve_ctx(headers)
    if ctx is None:
        if not route.public:
            raise ApiError(401, 'UNAUTHENTICATED', 'Your session has ended. Sign in again.')
        ctx = anon_ctx()
    if route.super_admin and not ctx.is_super:
        raise ApiError(403, 'FORBIDDEN', 'Platform administrators only.')
    if method != 'GET' and not route.public and not route.allow_view_as_write and not ctx.is_super:
        assert_writable(ctx, path)
    if method != 'GET' and not ctx.synthetic and ctx.user.get('must_change_password') and not route.public and path.split('?')[0] not in PASSWORD_CHANGE_PATHS:
        raise ApiError(403, 'PASSWORD_CHANGE_REQUIRED', 'Set a new password before you continue.')
    body0 = req.body
    if method != 'GET' and body0 is not UNDEF and body0 and isinstance(body0, (dict, list)):
        bad = find_file_problem(body0)
        if bad:
            raise ApiError(422, 'FILE_REJECTED', bad['message'], bad.get('field', UNDEF))
    body = parse_body(req.body_raw)
    if body is UNDEF or body is None:
        body = {}
    clock = REQUEST_NOW.set(request_clock_now())  # the request clock: one instant for every timestamp the handler writes
    try:
        data = route.handler(Req(params=params, query=req.query, body=body, if_match=if_match_number(headers), ctx=ctx, headers=headers))
    finally:
        REQUEST_NOW.reset(clock)
    return envelope(data, ctx.warnings)


def envelope(data: Any, warnings: list) -> Envelope:
    """{data, warnings} bytes + ETag (data is an object with a numeric version)"""
    etag = None
    if isinstance(data, dict):
        v = data.get('version', UNDEF)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            etag = f'"{js_str(v)}"'
    if data is UNDEF:
        body = b'{"warnings":' + js_json_dumps(warnings) + b'}'  # JSON.stringify drops data: undefined
    else:
        body = js_json_dumps({'data': data, 'warnings': warnings})
    return Envelope(body, etag)
