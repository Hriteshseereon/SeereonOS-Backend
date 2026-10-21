"""Route registry and matching — port of the router of src/mock/server.ts (+ server/src/openapi.ts, limits.ts).

Route modules (app/routes/<ts name in snake_case>.py, app/engine/maker_checker.py) expose `register(r: Router)` and
register their routes in the SAME ORDER and with the SAME patterns/options as the TypeScript module:

    def register(r: Router) -> None:
        @r.get('/notifications')
        def list_notifications(req: Req): ...

        r.post('/notifications/:id/read', read_one, allow_view_as_write=True)

Matching is identical to the mock: first registered route of the method whose segment count matches and whose
literal segments are equal; `:param` segments are decodeURIComponent-decoded. Server overrides (auth) are matched
before everything else. Modules are registered in the TS registerAll() order; a module that is not ported yet is
simply absent (its paths answer 404 ROUTE_NOT_FOUND).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.js import decode_uri_component

Handler = Callable[..., Any]

# TS registerAll() order: (OpenAPI tag, python module)
MODULES: list[tuple[str, str]] = [
    ('auth', 'app.routes.auth'),
    ('people', 'app.routes.people'),
    ('settings', 'app.routes.settings'),
    ('admin', 'app.routes.admin'),
    ('billing', 'app.routes.billing'),
    ('projects', 'app.routes.projects'),
    ('tasks', 'app.routes.tasks'),
    ('notifications', 'app.routes.notifications'),
    ('timesheets', 'app.routes.timesheets'),
    ('payroll', 'app.routes.payroll'),
    ('sales', 'app.routes.sales'),
    ('finance', 'app.routes.finance'),
    ('operations', 'app.routes.operations'),
    ('dashboards', 'app.routes.dashboards'),
    ('reports', 'app.routes.reports'),
    ('taskflow', 'app.routes.taskflow'),
    ('documents', 'app.routes.documents'),
    ('hr', 'app.routes.hr'),
    ('spend', 'app.routes.spend'),
    ('clients', 'app.routes.clients'),
    ('company', 'app.routes.company'),
    ('makerChecker', 'app.engine.maker_checker'),
    ('access', 'app.routes.access'),
    ('master', 'app.routes.master'),
    ('estimates', 'app.routes.estimates'),
    ('invoices', 'app.routes.invoices'),
    ('docs', 'app.routes.docs_v7'),
    ('workflows', 'app.routes.workflows_v7'),
]

# request bodies may carry files as data: URLs (15 MB) on these routes; every other route: 1 MB (server/src/limits.ts)
JSON_LIMIT = 1024 * 1024
UPLOAD_LIMIT = 15 * 1024 * 1024
UPLOAD_ROUTES = frozenset(
    {
        'PUT /tenant/logo',
        'POST /projects',
        'POST /milestones/:id/invoice',
        'POST /tasks',
        'POST /tasks/:id/attachments',
        'POST /documents',
        'POST /documents/:id/versions',
        'PATCH /documents/:id',
        'POST /candidates/:id/resume',
        'POST /expenses',
        'PATCH /expenses/:id',
        'POST /purchase-orders',
        'PATCH /purchase-orders/:id',
        'POST /vendor-bills',
        'PATCH /vendor-bills/:id',
        'POST /clients/:id/contracts',
        'PATCH /contracts/:id',
        'POST /contracts/:id/renew',
        'POST /doc-templates',
        'PATCH /doc-templates/:id',
        'POST /employee-docs',
        'PUT /finance/invoices/settings',
        'POST /holidays/import',
        'POST /projects/:id/import',
        'POST /users/bulk-invite',
    }
)


def is_upload_route(method: str, pattern: str) -> bool:
    return f'{method} {pattern}' in UPLOAD_ROUTES


_UPLOAD_SHAPES = [(k.split(' ', 1)[0], [p for p in k.split(' ', 1)[1].split('/') if p]) for k in sorted(UPLOAD_ROUTES)]


def body_limit_for(method: str, path: str) -> int:
    """the body limit of a request by its path (independent of whether the route module is ported yet)"""
    parts = [p for p in path.split('?')[0].split('/') if p]
    for m, pat in _UPLOAD_SHAPES:
        if m == method and len(pat) == len(parts) and all(a.startswith(':') or a == b for a, b in zip(pat, parts)):
            return UPLOAD_LIMIT
    return JSON_LIMIT


@dataclass
class Route:
    method: str
    pattern: str
    parts: list[str]
    handler: Handler
    public: bool = False
    super_admin: bool = False
    allow_view_as_write: bool = False
    module: str = 'core'
    override: bool = False
    tag: str | None = None  # OpenAPI tag of an override (the module of the shared route it replaces)
    params_at: list[tuple[int, str]] = field(default_factory=list)
    literals: list[tuple[int, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f'{self.method} {self.pattern}'


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []
        self.module = 'core'
        self.loaded: list[str] = []
        self.missing: list[str] = []
        self._by_shape: dict[tuple[str, int], list[Route]] | None = None

    # ------------------------------------------------------------------ registration
    def _mk(
        self,
        method: str,
        pattern: str,
        handler: Handler,
        public: bool,
        super_admin: bool,
        allow_view_as_write: bool,
        module: str,
        override: bool = False,
        tag: str | None = None,
    ) -> Route:
        parts = [p for p in pattern.split('/') if p]
        r = Route(method, pattern, parts, handler, public, super_admin, allow_view_as_write, module, override, tag)
        r.params_at = [(i, p[1:]) for i, p in enumerate(parts) if p.startswith(':')]
        r.literals = [(i, p) for i, p in enumerate(parts) if not p.startswith(':')]
        return r

    def add(
        self, method: str, pattern: str, handler: Handler | None = None, *, public: bool = False, super_admin: bool = False, allow_view_as_write: bool = False
    ) -> Any:
        def deco(h: Handler) -> Handler:
            self.routes.append(self._mk(method, pattern, h, public, super_admin, allow_view_as_write, self.module))
            self._by_shape = None
            return h

        return deco(handler) if handler is not None else deco

    def override(
        self,
        method: str,
        pattern: str,
        handler: Handler | None = None,
        *,
        public: bool = False,
        super_admin: bool = False,
        allow_view_as_write: bool = False,
        module: str = 'auth',
        tag: str | None = None,
    ) -> Any:
        """server-side replacement of a shared route (matched first; same contract, different storage)"""

        def deco(h: Handler) -> Handler:
            self.routes.insert(0, self._mk(method, pattern, h, public, super_admin, allow_view_as_write, module, True, tag))
            self._by_shape = None
            return h

        return deco(handler) if handler is not None else deco

    def get(self, p: str, h: Handler | None = None, **o: Any) -> Any:
        return self.add('GET', p, h, **o)

    def post(self, p: str, h: Handler | None = None, **o: Any) -> Any:
        return self.add('POST', p, h, **o)

    def patch(self, p: str, h: Handler | None = None, **o: Any) -> Any:
        return self.add('PATCH', p, h, **o)

    def put(self, p: str, h: Handler | None = None, **o: Any) -> Any:
        return self.add('PUT', p, h, **o)

    def delete(self, p: str, h: Handler | None = None, **o: Any) -> Any:
        return self.add('DELETE', p, h, **o)

    del_ = delete

    def shared(self, method: str, pattern: str) -> Route | None:
        """the shared (non-override) route of a method + pattern"""
        return next((r for r in self.routes if not r.override and r.method == method and r.pattern == pattern), None)

    # ------------------------------------------------------------------ matching
    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        parts = [p for p in path.split('?')[0].split('/') if p]
        if self._by_shape is None:
            shapes: dict[tuple[str, int], list[Route]] = {}
            for r in self.routes:
                shapes.setdefault((r.method, len(r.parts)), []).append(r)
            self._by_shape = shapes
        for r in self._by_shape.get((method, len(parts)), ()):
            ok = True
            for i, lit in r.literals:
                if parts[i] != lit:
                    ok = False
                    break
            if ok:
                return r, {name: decode_uri_component(parts[i]) for i, name in r.params_at}
        return None


def load_modules(r: Router, only: list[str] | None = None) -> Router:
    """registerAll(): every ported route module in the TS order (unported modules are absent)."""
    for tag, modname in MODULES:
        if only is not None and tag not in only:
            continue
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as e:
            if e.name == modname:
                r.missing.append(tag)
                continue
            raise
        reg = getattr(mod, 'register', None)
        if reg is None:
            r.missing.append(tag)
            continue
        r.module = tag
        reg(r)
        r.loaded.append(tag)
    r.module = 'core'
    return r


# ---------------------------------------------------------------------------------------------------------------------
# OpenAPI 3.1 (port of server/src/openapi.ts)
# ---------------------------------------------------------------------------------------------------------------------
def _op_id(method: str, pattern: str) -> str:
    segs = []
    for s in [x for x in pattern.split('/') if x]:
        if s.startswith(':'):
            s = 'By' + s[1:2].upper() + s[2:]
        s = re.sub(r'[-_]([a-z0-9])', lambda m: m.group(1).upper(), s)
        segs.append(s[:1].upper() + s[1:])
    return method.lower() + ''.join(segs)


def build_openapi(r: Router, version: str) -> dict[str, Any]:
    seen: dict[str, Route] = {}
    module_of: dict[str, str] = {}
    for rt in r.routes:
        if not rt.override:
            module_of[rt.key] = rt.module
        if rt.key not in seen:
            seen[rt.key] = rt
    paths: dict[str, dict[str, Any]] = {}
    tags: set[str] = set()
    for k, rt in seen.items():
        tag = module_of.get(k) or rt.tag or rt.module
        tags.add(tag)
        p = re.sub(r':([A-Za-z0-9_]+)', r'{\1}', rt.pattern)
        params: list[Any] = [{'name': m, 'in': 'path', 'required': True, 'schema': {'type': 'string'}} for m in re.findall(r':([A-Za-z0-9_]+)', rt.pattern)]
        if not rt.public:
            params.append({'$ref': '#/components/parameters/ViewAs'})
        if rt.method != 'GET':
            params.append({'$ref': '#/components/parameters/IfMatch'})
        if rt.method == 'POST' and not rt.public:
            params.append({'$ref': '#/components/parameters/IdempotencyKey'})
        if rt.method == 'GET':
            params.extend([{'$ref': '#/components/parameters/Page'}, {'$ref': '#/components/parameters/PageSize'}])
        notes = []
        if rt.super_admin:
            notes.append('Platform administrators only.')
        if rt.allow_view_as_write:
            notes.append('Allowed while viewing as another role.')
        if rt.public:
            notes.append('No session required.')
        upload = is_upload_route(rt.method, rt.pattern)
        notes.append(f'Request body limit: {"15 MB (accepts data: URLs)" if upload else "1 MB"}.')
        responses: dict[str, Any] = {
            '200': {
                'description': 'Success envelope',
                'headers': {'ETag': {'description': 'Version of the returned record (when it has one)', 'schema': {'type': 'string'}}},
                'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Envelope'}}},
            },
        }
        if not rt.public:
            responses.update(
                {
                    '401': {'$ref': '#/components/responses/Error'},
                    '402': {'$ref': '#/components/responses/Error'},
                    '403': {'$ref': '#/components/responses/Error'},
                }
            )
        responses['404'] = {'$ref': '#/components/responses/Error'}
        if rt.method != 'GET':
            responses.update({'409': {'$ref': '#/components/responses/Error'}, '413': {'$ref': '#/components/responses/Error'}})
        responses.update(
            {'422': {'$ref': '#/components/responses/Error'}, '429': {'$ref': '#/components/responses/Error'}, '500': {'$ref': '#/components/responses/Error'}}
        )
        op: dict[str, Any] = {
            'tags': [tag],
            'operationId': _op_id(rt.method, rt.pattern),
            'summary': f'{rt.method} {rt.pattern}',
            'description': ' '.join(notes),
            'parameters': params,
            'security': [] if rt.public else [{'bearer': []}],
            'responses': responses,
            'x-body-limit-bytes': UPLOAD_LIMIT if upload else JSON_LIMIT,
        }
        if rt.method not in ('GET', 'DELETE'):
            op['requestBody'] = {'required': False, 'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': True}}}}
        paths.setdefault('/api/v1' + p, {})[rt.method.lower()] = op
    return {
        'openapi': '3.1.0',
        'info': {
            'title': 'SeereonOS API',
            'version': version,
            'description': 'REST API of SeereonOS. Every success response is `{ data, warnings[] }`; every error is `{ error: { code, message, field?, request_id } }`. '
            'Tenant isolation: records of other tenants are 404. Optimistic concurrency: send the record version as `If-Match`; a mismatch is 409 STALE. '
            'Read-only (lapsed) workspaces answer writes with 402 TENANT_READ_ONLY. Query values are strings.',
        },
        'servers': [{'url': '/'}],
        'tags': [{'name': n} for n in sorted(tags)],
        'paths': paths,
        'components': {
            'securitySchemes': {
                'bearer': {
                    'type': 'http',
                    'scheme': 'bearer',
                    'description': 'Opaque session token from POST /api/v1/auth/login (idle expiry 12 h, absolute 30 days).',
                }
            },
            'parameters': {
                'ViewAs': {
                    'name': 'X-View-As',
                    'in': 'header',
                    'required': False,
                    'description': 'Preview the app as another role (read-only). Needs the view_as permission.',
                    'schema': {'type': 'string'},
                },
                'IfMatch': {
                    'name': 'If-Match',
                    'in': 'header',
                    'required': False,
                    'description': 'Expected record version; mismatch → 409 STALE.',
                    'schema': {'type': 'string'},
                },
                'IdempotencyKey': {
                    'name': 'Idempotency-Key',
                    'in': 'header',
                    'required': False,
                    'description': 'Replays the stored 2xx response of the same POST for 24 h.',
                    'schema': {'type': 'string', 'maxLength': 200},
                },
                'Page': {'name': 'page', 'in': 'query', 'required': False, 'schema': {'type': 'string'}, 'description': 'Page number on paginated lists.'},
                'PageSize': {
                    'name': 'page_size',
                    'in': 'query',
                    'required': False,
                    'schema': {'type': 'string'},
                    'description': 'Page size (1–200) on paginated lists.',
                },
            },
            'schemas': {
                'Warning': {'type': 'object', 'required': ['code', 'message'], 'properties': {'code': {'type': 'string'}, 'message': {'type': 'string'}}},
                'Envelope': {
                    'type': 'object',
                    'required': ['data', 'warnings'],
                    'properties': {'data': {}, 'warnings': {'type': 'array', 'items': {'$ref': '#/components/schemas/Warning'}}},
                },
                'Error': {
                    'type': 'object',
                    'required': ['error'],
                    'properties': {
                        'error': {
                            'type': 'object',
                            'required': ['code', 'message'],
                            'properties': {
                                'code': {'type': 'string'},
                                'message': {'type': 'string'},
                                'field': {'type': 'string'},
                                'request_id': {'type': 'string'},
                            },
                        }
                    },
                },
            },
            'responses': {'Error': {'description': 'Error envelope', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}},
        },
    }
