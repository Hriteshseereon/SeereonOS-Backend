"""API errors and the exact error envelope of the reference: {"error": {"code", "message", "field"?, "request_id"}}."""

from __future__ import annotations

from typing import Any, NoReturn

import orjson

from app.js import UNDEF


class ApiError(Exception):
    """An expected API error (src/api/http.ts ApiError): status + code + message (+ field)."""

    def __init__(self, status: int, code: str, message: str, field: Any = UNDEF) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field

    def __repr__(self) -> str:
        return f'ApiError({self.status}, {self.code!r}, {self.message!r}{", " + repr(self.field) if self.field is not UNDEF else ""})'


def err(status: int, code: str, message: str, field: Any = UNDEF) -> NoReturn:
    """err() of src/mock/server.ts: raise an ApiError"""
    raise ApiError(status, code, message, field)


def error_body(code: str, message: str, request_id: str, field: Any = None) -> bytes:
    """errBody() of server/src/app.ts (framework-level errors): field only when truthy"""
    e: dict[str, Any] = {'code': code, 'message': message}
    if field:
        e['field'] = field
    e['request_id'] = request_id
    return orjson.dumps({'error': e})


def fail_body(e: ApiError, request_id: str) -> bytes:
    """the runtime's fail(): field included whenever it is defined (even '')"""
    out: dict[str, Any] = {'code': e.code, 'message': e.message}
    if e.field is not UNDEF:
        out['field'] = e.field
    out['request_id'] = request_id
    return orjson.dumps({'error': out})
