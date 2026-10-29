"""FastAPI application factory.

* startup checks: configuration (app.config), database role + schema version (app.startup), Sentry (optional)
* security headers (CSP, HSTS in production) + Permissions-Policy on every response; Razorpay origins only when
  payments are enabled
* CORS allowlist (CORS_ORIGINS), request ids (x-request-id), structured JSON logs with redaction
* `Cache-Control: no-store` + `Vary: Authorization` on every API response
* per-process global rate limit (per token / IP) + DB-backed auth limits shared by all processes and instances
* body limits (1 MB; 15 MB on the data-URL routes), JSON-only bodies (application/json or text/plain)
* /healthz (process), /readyz (database + worker heartbeat), /metrics (Prometheus, METRICS_TOKEN), OpenAPI 3.1 at
  /api/v1/openapi.json, Swagger UI at /api/docs (API_DOCS_ENABLED; off by default in production)
* the /api/v1 gateway into the route registry (app.runtime.Runtime.execute) and the Razorpay webhook
* optionally a built frontend (STATIC_DIR) with SPA fallback — in production the web app is usually served separately
* NO background threads: e-mail delivery, sweeps and cleanup run in the worker process (python -m app.worker)

  uvicorn app.main:app --port 8000                      (development)
  gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w $WEB_CONCURRENCY   (production, see Dockerfile)
"""

from __future__ import annotations

import contextlib
import functools
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from typing import Any

import orjson
import psycopg
from fastapi import FastAPI
from starlette.background import BackgroundTask
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from app import runtime_state
from app.config import Config, load_config, set_settings
from app.errors import error_body
from app.logging_setup import configure_logging, redact_url
from app.router import Router, body_limit_for, build_openapi, load_modules
from app.runtime import ClientInfo, ExecRequest, Runtime

API_PREFIX = '/api/v1'
VERSION = os.environ.get('APP_VERSION') or '1.0.0'
AUTH_LIMITED = frozenset(
    {
        'POST /auth/login',
        'POST /auth/login/mfa',
        'POST /auth/signup',
        'POST /auth/reset-request',
        'POST /auth/reset',
        'POST /invites/:token/accept',
        'GET /auth/reset-info/:token',
        'GET /invites/:token',
    }
)
# v9 public careers page (no session): DB-backed fixed windows per client IP — (bucket, limit of the config, window s)
PUBLIC_LIMITED: dict[str, tuple[str, Any, int]] = {
    'POST /public/openings/:token': ('careers-apply', lambda c: c.rate_limit_careers, 600),
    'GET /public/openings/:token': ('careers-view', lambda c: max(60, c.rate_limit_careers * 24), 60),
}
API_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})
BODY_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})

CSP = (
    "default-src 'self';script-src 'self';style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;font-src 'self' data: https://fonts.gstatic.com;"
    "img-src 'self' data: blob:;connect-src 'self' data: blob:;frame-src 'self' blob: data:;worker-src 'self' blob:;object-src 'none';base-uri 'self';form-action 'self';"
    "frame-ancestors 'none'"
)
# Razorpay Checkout (only when payments are enabled): the script and its frames from checkout.razorpay.com, the
# checkout frame and API calls on api.razorpay.com, its telemetry on lumberjack.razorpay.com. connect-src data:/blob:
# lets the app fetch its own data: / blob: attachments (task panel).
CSP_PAYMENTS = (
    "default-src 'self';script-src 'self' https://checkout.razorpay.com;style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;"
    "font-src 'self' data: https://fonts.gstatic.com;img-src 'self' data: blob:;connect-src 'self' data: blob: https://api.razorpay.com https://lumberjack.razorpay.com;"
    "frame-src 'self' blob: data: https://checkout.razorpay.com https://api.razorpay.com;worker-src 'self' blob:;object-src 'none';base-uri 'self';"
    "form-action 'self';frame-ancestors 'none'"
)
SWAGGER_CSP = (
    "default-src 'self'; base-uri 'self'; font-src 'self' https: data:; frame-ancestors 'self'; img-src 'self' data: validator.swagger.io; object-src 'none'; "
    "script-src 'self' ; script-src-attr 'none'; style-src 'self' 'unsafe-inline' https: ; upgrade-insecure-requests;"
)
HELMET = [
    (b'content-security-policy', CSP.encode()),
    (b'cross-origin-opener-policy', b'same-origin'),
    (b'cross-origin-resource-policy', b'same-origin'),
    (b'origin-agent-cluster', b'?1'),
    (b'referrer-policy', b'strict-origin-when-cross-origin'),
    (b'x-content-type-options', b'nosniff'),
    (b'x-dns-prefetch-control', b'off'),
    (b'x-download-options', b'noopen'),
    (b'x-frame-options', b'SAMEORIGIN'),
    (b'x-permitted-cross-domain-policies', b'none'),
    (b'x-xss-protection', b'0'),
]
HSTS = (b'strict-transport-security', b'max-age=31536000; includeSubDomains')
PERMISSIONS_POLICY = (b'permissions-policy', b'camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()')
PERMISSIONS_POLICY_PAYMENTS = (
    b'permissions-policy',
    b'camera=(), microphone=(), geolocation=(), payment=(self "https://checkout.razorpay.com" "https://api.razorpay.com"), usb=(), interest-cohort=()',
)
NO_STORE_PREFIXES = ('/api/', '/healthz', '/readyz', '/metrics')
CORS_METHODS = 'GET,POST,PUT,PATCH,DELETE'
CORS_ALLOWED = 'Authorization,Content-Type,X-View-As,If-Match,Idempotency-Key,X-Request-Id'
CORS_EXPOSED = 'ETag,X-Request-Id,Idempotent-Replayed,Retry-After,RateLimit-Limit,RateLimit-Remaining,RateLimit-Reset'
RID_RE = re.compile(r'^[A-Za-z0-9._:-]{8,128}$')
ASSET_RE = re.compile(r'\.(m?js|css|map|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|json|txt|webmanifest)(\?|$)', re.I)
INTERNAL_MSG = 'Something went wrong on our side. Try again; if it keeps happening, contact support with this request id.'

log = logging.getLogger('seereonos')


# ---------------------------------------------------------------------------------------------------------------------
# headers / CORS / request id (pure ASGI: runs for every response, including errors and static files)
# ---------------------------------------------------------------------------------------------------------------------
class HeadersMiddleware:
    def __init__(self, app: Any, cfg: Config, metrics: Any = None) -> None:
        self.app = app
        self.cfg = cfg
        self.origins = frozenset(cfg.cors_origins)
        self.metrics = metrics
        helmet = list(HELMET)
        if cfg.payments_enabled:
            helmet[0] = (b'content-security-policy', CSP_PAYMENTS.encode())
        self.helmet = helmet
        self.permissions = PERMISSIONS_POLICY_PAYMENTS if cfg.payments_enabled else PERMISSIONS_POLICY

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        hdrs = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
        rid_in = hdrs.get('x-request-id')
        rid = rid_in if rid_in is not None and RID_RE.match(rid_in) else str(uuid.uuid4())
        scope.setdefault('state', {})['request_id'] = rid
        origin = hdrs.get('origin')
        allowed = bool(origin) and origin in self.origins
        t0 = time.monotonic()
        status_holder = {'status': 0}
        # CORS preflight (allowed origin): answered here like @fastify/cors
        if scope['method'] == 'OPTIONS' and allowed and hdrs.get('access-control-request-method'):
            headers = self._common(rid, scope['path']) + [
                (b'access-control-allow-origin', origin.encode()),
                (b'vary', b'Origin'),
                (b'access-control-allow-methods', CORS_METHODS.encode()),
                (b'access-control-allow-headers', CORS_ALLOWED.encode()),
                (b'access-control-max-age', b'600'),
                (b'content-length', b'0'),
            ]
            await send({'type': 'http.response.start', 'status': 204, 'headers': headers})
            await send({'type': 'http.response.body', 'body': b''})
            self._log(scope, 204, t0, rid)
            return

        no_store = scope['path'].startswith(NO_STORE_PREFIXES)

        async def send_wrapper(message: dict) -> None:
            if message['type'] == 'http.response.start':
                status_holder['status'] = message['status']
                existing = list(message.get('headers') or [])
                vary = [b'Origin']
                if no_store:
                    # API responses are per user and must never be cached by a browser or a proxy
                    vary.append(b'Authorization')
                    for k, v in existing:
                        if k.lower() == b'vary':
                            vary.extend(x.strip() for x in v.split(b',') if x.strip() and x.strip().lower() not in (b'origin', b'authorization'))
                    existing = [(k, v) for k, v in existing if k.lower() not in (b'cache-control', b'vary', b'pragma')]
                    # (the merged Vary is added below)
                    existing += [(b'cache-control', b'no-store'), (b'pragma', b'no-cache')]
                names = {k.lower() for k, _ in existing}
                extra = [h for h in self._common(rid, scope['path']) if h[0] not in names]
                extra.append((b'vary', b', '.join(vary)))
                if allowed:
                    extra += [(b'access-control-allow-origin', origin.encode()), (b'access-control-expose-headers', CORS_EXPOSED.encode())]
                message['headers'] = existing + extra
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self._log(scope, status_holder['status'], t0, rid)

    def _common(self, rid: str, path: str) -> list[tuple[bytes, bytes]]:
        h = list(self.helmet)
        if self.cfg.production:
            h.append(HSTS)
        h.append((b'x-request-id', rid.encode()))
        h.append(self.permissions)
        return h

    def _log(self, scope: dict, status: int, t0: float, rid: str) -> None:
        if self.metrics is not None:
            st = scope.get('state') or {}
            route = st.get('route') or ('/api/*' if scope['path'].startswith('/api/') else 'static')
            self.metrics.observe(scope['method'], route, status, time.monotonic() - t0)
        if not log.isEnabledFor(logging.INFO):
            return
        qs = scope.get('query_string', b'').decode('latin-1')
        url = scope.get('raw_path', b'').decode('latin-1') or scope['path']
        client = scope.get('client') or (None, None)
        log.info(
            'request completed',
            extra={
                'reqId': rid,
                'req': {'method': scope['method'], 'url': redact_url(url + ('?' + qs if qs else '')), 'remoteAddress': client[0]},
                'res': {'statusCode': status},
                'responseTime': round((time.monotonic() - t0) * 1000, 1),
            },
        )


# ---------------------------------------------------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------------------------------------------------
def create_app(cfg: Config | None = None, *, security: Any = None, start_workers: bool = True, check_db: bool = True) -> FastAPI:
    """`start_workers` is accepted for compatibility and ignored: the API process runs no background work (the worker
    process does)."""
    from app.auth.overrides import OverrideDeps, register_overrides
    from app.auth.secret_box import create_secret_box
    from app.db.backend import PgBackend
    from app.db.cache import TenantCache
    from app.db.pool import Pools
    from app.engine.secrets import set_secret_box
    from app.observability import Metrics, init_sentry
    from app.security.store import LocalLimiter, PgSecurityStore
    from app.startup import check_database

    cfg = cfg or load_config()
    set_settings(cfg)
    from app.auth.passwords import set_concurrency

    set_concurrency(cfg.scrypt_concurrency)
    configure_logging(cfg.log_level)
    if os.environ.get('TZ') and hasattr(time, 'tzset'):  # time.tzset does not exist on Windows
        time.tzset()
    init_sentry(cfg.sentry_dsn, cfg.sentry_environment or cfg.env, VERSION, cfg.sentry_traces_sample_rate, component='api')
    pools = Pools(cfg.database_url, cfg.pool_max, cfg.statement_timeout_ms, open=True, prepare_threshold=cfg.pg_prepare_threshold)
    if check_db:
        try:
            db_info = check_database(pools, cfg.production, 'api')
        except Exception:
            pools.close()
            raise
        log.info('database ready', extra=db_info)
    # the delivery queue is the record of what should be sent: outbox messages are queued when SMTP delivery is on
    # (platform messages — reset links, invitations — always, sealed; they expire)
    backend = PgBackend(
        pools, queue_emails=cfg.email_delivery == 'smtp', delta_max=cfg.delta_max_rows, queue_platform_mails=cfg.email_delivery == 'smtp' or not cfg.production
    )
    backend.init()
    if cfg.email_delivery == 'disabled':
        log.warning(
            'EMAIL_DELIVERY=disabled: NO e-mail will be sent (password reset links, invitations, notifications). Set SMTP_URL for production use.',
            extra={'email_delivery': 'disabled'},
        )
    if cfg.payments_enabled:
        log.info('payments: Razorpay enabled', extra={'key_id': cfg.razorpay_key_id})
    else:
        log.info('payments: not configured (billing payment endpoints answer 409 PAYMENTS_NOT_CONFIGURED)')
    router = Router()

    from app.memo import ResponseMemo

    rt = Runtime(
        backend,
        router,
        TenantCache(cfg.tenant_cache_max, cfg.tenant_cache_max_mb * 1024 * 1024),
        freeze_rows=cfg.freeze_rows,
        idle_hours=cfg.session_idle_hours,
        absolute_days=cfg.session_absolute_days,
        memo=ResponseMemo(cfg.memo_max_mb * 1024 * 1024, cfg.memo_ttl_s, verify=cfg.memo_verify) if cfg.memo_max_mb > 0 else None,
        lease_mode=cfg.lease_mode,
        contention_window_s=cfg.contention_window_s,
    )
    runtime_state.RUNTIME = rt
    set_secret_box(create_secret_box(cfg.mfa_encryption_key, list(cfg.mfa_encryption_keys_previous), allow_plain=not cfg.production))
    # ROUTE_MODULES (development / parallel porting only): load just these route modules (comma list of OpenAPI tags)
    only = [m.strip() for m in os.environ.get('ROUTE_MODULES', '').split(',') if m.strip()] if not cfg.production else []
    load_modules(router, only or None)
    register_overrides(router, OverrideDeps(rt=rt, app_url=cfg.app_url))
    if cfg.mfa_key_is_default:
        log.warning('MFA_ENCRYPTION_KEY not set: two-step secrets are sealed with the built-in development key (refused in production)')
    security = security or PgSecurityStore(pools.app, cfg.security_events_retention_days, on_error=lambda e, what: log.warning(f'{what} failed', exc_info=e))
    global_limit = LocalLimiter(cfg.rate_limit_global, 60)
    webhook_limit = LocalLimiter(max(60, cfg.rate_limit_global), 60)
    openapi_doc = build_openapi(router, VERSION)
    from app.payments.webhook import openapi_paths

    openapi_doc['paths'].update(openapi_paths())
    openapi_bytes = orjson.dumps(openapi_doc)
    metrics = Metrics()
    write_limiter: Any = None  # anyio.CapacityLimiter(cfg.write_threads), made in the event loop (lifespan)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        import anyio

        # handlers are CPU-bound Python: a few threads per process keep the GIL hand-offs low (a lock holder or a short
        # request is not starved by dozens of runnable threads); the rest wait in the event loop (cheap)
        anyio.to_thread.current_default_thread_limiter().total_tokens = cfg.request_threads
        nonlocal write_limiter
        write_limiter = anyio.CapacityLimiter(cfg.write_threads)
        yield
        stats = {'memo': rt.memo.stats() if rt.memo else None, 'cache': rt.cache.stats() if rt.cache else None, 'runtime': dict(rt.stats)}
        if cfg.memo_verify:  # test runs: always visible
            log.warning('worker stats', extra=stats)
        else:
            log.info('worker stats', extra=stats)
        pools.close()

    app = FastAPI(title='SeereonOS API', version=VERSION, openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.rt = rt
    app.state.cfg = cfg
    app.state.security = security
    app.state.router = router
    app.state.metrics = metrics
    app.state.pools = pools

    def rid_of(request: Request) -> str:
        return request.scope.get('state', {}).get('request_id') or str(uuid.uuid4())

    def json_response(status: int, body: bytes, headers: dict[str, str] | None = None) -> Response:
        return Response(content=body, status_code=status, media_type='application/json; charset=utf-8', headers=headers)

    # ---------------- health ----------------
    @app.get('/healthz')
    def healthz() -> Response:
        return json_response(200, b'{"status":"ok"}')

    @app.get('/readyz')
    def readyz() -> Response:
        """200 when the database answers; the worker heartbeat is reported (a stale or missing worker is a warning:
        the API keeps serving, but e-mail and scheduled jobs are not running)"""
        try:
            with pools.app.connection() as c:
                age, n = c.execute('SELECT extract(epoch FROM now() - max(last_beat_at)), count(*) FROM worker_heartbeats').fetchone()  # type: ignore[misc]
        except Exception:  # noqa: BLE001
            return json_response(503, b'{"status":"unavailable","database":"down"}')
        if not n or age is None:
            worker = {'status': 'missing'}
        elif float(age) > cfg.worker_stale_s:
            worker = {'status': 'stale', 'last_beat_s': int(float(age))}
        else:
            worker = {'status': 'ok', 'last_beat_s': int(float(age))}
        body = {'status': 'ready', 'database': 'ok', 'worker': worker, 'email_delivery': cfg.email_delivery, 'payments': cfg.payments_enabled}
        if worker['status'] != 'ok':
            body['warnings'] = [f'worker heartbeat {worker["status"]}: run python -m app.worker (e-mail delivery and scheduled jobs)']
        return json_response(200, orjson.dumps(body))

    @app.get('/metrics')
    def metrics_endpoint(request: Request) -> Response:
        """Prometheus text format; Authorization: Bearer METRICS_TOKEN (404 when METRICS_TOKEN is not set)"""
        import hmac

        if not cfg.metrics_token:
            return json_response(404, error_body('ROUTE_NOT_FOUND', 'No route for GET /metrics', rid_of(request)))
        auth = request.headers.get('authorization') or ''
        given = re.sub(r'^Bearer\s+', '', auth, flags=re.I).strip()
        if not given or not hmac.compare_digest(given.encode(), cfg.metrics_token.encode()):
            return json_response(401, error_body('UNAUTHENTICATED', 'Metrics need the metrics token.', rid_of(request)), {'WWW-Authenticate': 'Bearer'})
        from app.observability import db_metrics

        text = metrics.render(db_metrics(pools))
        return Response(text, media_type='text/plain; version=0.0.4; charset=utf-8')

    @app.post(API_PREFIX + '/billing/webhooks/razorpay')
    async def razorpay_webhook(request: Request) -> Response:
        """public, signed by Razorpay (X-Razorpay-Signature over the raw body); idempotent by event id"""
        import anyio.to_thread

        from app.payments.webhook import handle_webhook

        rid = rid_of(request)
        request.scope.setdefault('state', {})['route'] = '/billing/webhooks/razorpay'
        ip = request.client.host if request.client else None
        exceeded, _rem, ttl = webhook_limit.hit('wh:' + (ip or ''))
        if exceeded:
            return json_response(429, error_body('RATE_LIMITED', f'Too many requests. Try again in {max(1, ttl)} seconds.', rid), {'Retry-After': str(ttl)})
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            return json_response(413, error_body('PAYLOAD_TOO_LARGE', 'The request is too large.', rid))
        status, body = await anyio.to_thread.run_sync(
            functools.partial(handle_webhook, rt, cfg, raw, request.headers.get('x-razorpay-signature'), request.headers.get('x-razorpay-event-id'), rid)
        )
        return json_response(status, body)

    # ---------------- OpenAPI + Swagger UI ----------------
    # off by default in production (API_DOCS_ENABLED=false): the route map is not published and these paths answer 404
    # like any unknown route
    if cfg.api_docs:
        @app.get(API_PREFIX + '/openapi.json')
        def openapi_json() -> Response:
            return json_response(200, openapi_bytes)

        swagger_dir = os.environ.get('SWAGGER_UI_DIR') or os.path.join(os.path.dirname(__file__), 'static', 'swagger-ui')

        @app.get('/api/docs')
        def docs() -> Response:
            html = (
                '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Swagger UI</title>'
                '<link rel="stylesheet" type="text/css" href="/api/docs/static/swagger-ui.css" /><link rel="stylesheet" type="text/css" href="/api/docs/static/index.css" />'
                '</head><body><div id="swagger-ui"></div><script src="/api/docs/static/swagger-ui-bundle.js" charset="UTF-8"></script>'
                '<script src="/api/docs/static/swagger-ui-standalone-preset.js" charset="UTF-8"></script><script src="/api/docs/static/swagger-initializer.js" charset="UTF-8"></script>'
                '</body></html>'
            )
            return Response(html, media_type='text/html; charset=utf-8', headers={'content-security-policy': SWAGGER_CSP})

        @app.get('/api/docs/json')
        def docs_json() -> Response:
            return Response(openapi_bytes, media_type='application/json; charset=utf-8', headers={'content-security-policy': SWAGGER_CSP})

        @app.get('/api/docs/static/swagger-initializer.js')
        def docs_init() -> Response:
            js = (
                "window.onload = function () { window.ui = SwaggerUIBundle({ url: '/api/docs/json', dom_id: '#swagger-ui', deepLinking: true, docExpansion: 'none', "
                "presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset], plugins: [SwaggerUIBundle.plugins.DownloadUrl], layout: 'StandaloneLayout', validatorUrl: null }) }"
            )
            return Response(js, media_type='application/javascript; charset=utf-8', headers={'content-security-policy': SWAGGER_CSP})

        @app.get('/api/docs/static/{name}')
        def docs_static(name: str) -> Response:
            p = os.path.join(swagger_dir, os.path.basename(name))
            if not os.path.isfile(p):
                return json_response(404, error_body('ROUTE_NOT_FOUND', f'No route for GET /api/docs/static/{name}', str(uuid.uuid4())))
            return FileResponse(p, headers={'content-security-policy': SWAGGER_CSP})

    # ---------------- the API gateway ----------------
    gzip_min = int(os.environ.get('GZIP_MIN_BYTES', '1024'))
    gzip_level = int(os.environ.get('GZIP_LEVEL', '5'))

    def gzipped(res: Any) -> bytes:
        """the body gzipped like GZipMiddleware would (same threshold and level), in this worker thread instead of the
        event loop; a memoised response keeps its compressed copy for every later hit"""
        e = res.memo
        if e is not None and e.gzip is not None and e.body is res.body:
            return e.gzip
        import gzip as _gz

        z = _gz.compress(res.body, compresslevel=gzip_level)
        if e is not None and e.body is res.body:
            e.gzip = z
        return z

    def handle_api(
        method: str,
        path: str,
        query: dict[str, Any],
        raw: bytes | None,
        body: Any,
        headers: dict[str, str | None],
        ip: str | None,
        ua: str | None,
        rid: str,
        accept_gzip: bool = False,
    ) -> tuple[int, bytes, dict[str, str], Any]:
        from app.security.audit import audit_after, audit_before
        from app.security.store import SecurityEvent

        out_headers: dict[str, str] = {}
        auth = headers.get('Authorization') or ''
        bearer = re.sub(r'^Bearer\s+', '', auth, flags=re.I).strip()
        from app.auth.crypto import sha256

        key = 't:' + sha256(bearer)[:32] if bearer else 'ip:' + (ip or '')
        exceeded, remaining, ttl = global_limit.hit(key)
        out_headers.update({'RateLimit-Limit': str(cfg.rate_limit_global), 'RateLimit-Remaining': str(remaining), 'RateLimit-Reset': str(ttl)})
        if exceeded:
            out_headers['Retry-After'] = str(ttl)
            return 429, error_body('RATE_LIMITED', f'Too many requests. Try again in {max(1, ttl)} seconds.', rid), out_headers, None
        m = rt.router.match(method, path)
        route_key = f'{method} {m[0].pattern}' if m else ''
        if m and route_key in AUTH_LIMITED:
            b = body if isinstance(body, dict) else {}
            from app.js import js_str, js_trim

            if 'email' in b:  # per IP + account key (e-mail, or the MFA challenge token), like the reference
                akey = js_trim(js_str(b['email'] if b['email'] is not None else '')).lower()
            elif 'mfa_token' in b:
                akey = 'mfa:' + sha256(js_str(b['mfa_token'] if b['mfa_token'] is not None else ''))[:32]
            else:
                akey = ''
            try:
                r1 = security.hit('auth', (ip or '') + '\0' + akey, cfg.rate_limit_auth, 60)
                r2 = security.hit('auth-ip', ip or '', cfg.rate_limit_auth * 10, 60)
            except Exception as e:  # noqa: BLE001 — fail open: the auth route needs the same DB
                log.warning('rate-limit store unavailable; request allowed', exc_info=e)
                r1 = r2 = None
            if r1 is not None and r2 is not None:
                worst = r1 if not r1.allowed else r2 if not r2.allowed else None
                out_headers.update(
                    {'RateLimit-Limit': str(r1.limit), 'RateLimit-Remaining': str(max(0, r1.limit - r1.hits)), 'RateLimit-Reset': str(r1.retry_after)}
                )
                if worst is not None:
                    out_headers['Retry-After'] = str(worst.retry_after)
                    em = b.get('email')
                    security.log(
                        SecurityEvent(
                            'ratelimit.blocked',
                            'blocked',
                            email=em.strip().lower() if isinstance(em, str) else None,
                            ip=ip,
                            user_agent=ua or None,
                            request_id=rid,
                            detail={'route': route_key, 'limit': 'auth' if worst is r1 else 'auth-ip', 'hits': worst.hits},
                        )
                    )
                    return 429, error_body('RATE_LIMITED', f'Too many requests. Try again in {worst.retry_after} seconds.', rid), out_headers, None
        if m and route_key in PUBLIC_LIMITED:
            bucket, limit_of, window = PUBLIC_LIMITED[route_key]
            limit_n = limit_of(cfg)
            try:
                rr = security.hit(bucket, ip or '', limit_n, window)
            except Exception as e:  # noqa: BLE001 — fail open like the auth limits
                log.warning('rate-limit store unavailable; request allowed', exc_info=e)
                rr = None
            if rr is not None:
                out_headers.update(
                    {'RateLimit-Limit': str(rr.limit), 'RateLimit-Remaining': str(max(0, rr.limit - rr.hits)), 'RateLimit-Reset': str(rr.retry_after)}
                )
                if not rr.allowed:
                    out_headers['Retry-After'] = str(rr.retry_after)
                    security.log(
                        SecurityEvent(
                            'ratelimit.blocked',
                            'blocked',
                            ip=ip,
                            user_agent=ua or None,
                            request_id=rid,
                            detail={'route': route_key, 'limit': bucket, 'hits': rr.hits},
                        )
                    )
                    mins = max(1, -(-rr.retry_after // 60))
                    return (
                        429,
                        error_body('RATE_LIMITED', f'Too many attempts from your network. Try again in {mins} minute{"" if mins == 1 else "s"}.', rid),
                        out_headers,
                        None,
                    )
        actx = audit_before(rt, security, method, path, body, headers, ip, ua, rid)
        res = rt.execute(
            ExecRequest(
                method=method, path=path, query=query, body_raw=raw, body=body, headers=headers, client=ClientInfo(ip=ip, user_agent=ua, request_id=rid)
            )
        )
        if res.replayed:
            out_headers['Idempotent-Replayed'] = 'true'
        if res.status == 200 and res.etag:
            out_headers['ETag'] = res.etag
        out_headers['Cache-Control'] = 'no-store'
        if res.status >= 500:
            log.error('api error', extra={'reqId': rid, 'status': res.status})
        task = (lambda: audit_after(actx, res.status, res.body, security)) if actx else None
        if accept_gzip and len(res.body) >= gzip_min:
            # GZipMiddleware passes a response that already has Content-Encoding through untouched
            out_headers['Content-Encoding'] = 'gzip'
            out_headers['Vary'] = 'Accept-Encoding'
            return res.status, gzipped(res), out_headers, task
        return res.status, res.body, out_headers, task

    async def gateway(request: Request) -> Response:
        rid = rid_of(request)
        method = request.method
        raw_path = request.scope.get('raw_path') or request.url.path.encode()
        full = raw_path.decode('latin-1')
        path = full[len(API_PREFIX) :] or '/'
        if method not in API_METHODS and method != 'HEAD':  # HEAD: exposed for the GET gateway like Fastify
            return json_response(404, error_body('ROUTE_NOT_FOUND', f'No route for {method} {full}', rid))
        try:
            mm = rt.router.match(method, path)
        except Exception:  # malformed percent-encoding in a path segment
            return json_response(400, error_body('BAD_REQUEST', 'The request URL is not valid.', rid))
        request.scope.setdefault('state', {})['route'] = mm[0].pattern if mm else 'unmatched'

        limit = body_limit_for(method, path)
        raw: bytes | None = None
        body: Any = None
        from app.js import UNDEF

        body = UNDEF
        if method in BODY_METHODS:
            cl = request.headers.get('content-length')
            if cl is not None and cl.isdigit() and int(cl) > limit:
                return json_response(413, error_body('PAYLOAD_TOO_LARGE', 'The request is too large. Files must be smaller than the upload limit.', rid))
            chunks = []
            seen = 0
            async for chunk in request.stream():
                seen += len(chunk)
                if seen > limit:
                    return json_response(413, error_body('PAYLOAD_TOO_LARGE', 'The request is too large. Files must be smaller than the upload limit.', rid))
                chunks.append(chunk)
            data = b''.join(chunks)
            if data:
                ctype = (request.headers.get('content-type') or '').split(';')[0].strip().lower()
                if ctype not in ('application/json', 'text/plain'):
                    return json_response(415, error_body('UNSUPPORTED_MEDIA_TYPE', 'Send JSON (Content-Type: application/json).', rid))
                if data.strip():
                    try:
                        body = orjson.loads(data)
                    except orjson.JSONDecodeError:
                        return json_response(400, error_body('INVALID_JSON', 'The request body is not valid JSON.', rid))
                    raw = data
        query = _parse_query(request.scope.get('query_string', b''))
        h = request.headers
        headers = {
            'Authorization': h.get('authorization'),
            'X-View-As': h.get('x-view-as'),
            'If-Match': h.get('if-match'),
            'Idempotency-Key': h.get('idempotency-key'),
        }
        client = client_ip(request, cfg.trust_proxy)
        try:
            import anyio.to_thread

            call = functools.partial(
                handle_api, method, path, query, raw, body, headers, client, h.get('user-agent'), rid, 'gzip' in h.get('accept-encoding', '')
            )
            # writes run on their own small thread budget: a burst of writes queued on a tenant's lease never takes
            # every thread from the reads (memo hits stay fast)
            status, out, out_headers, task = await anyio.to_thread.run_sync(call, limiter=None if method in ('GET', 'HEAD') else write_limiter)
        except psycopg.OperationalError as e:
            # the database is down, restarting or failing over (stale pooled connections are dropped by the pool):
            # a retryable 503, not a 500 — the web app's connection banner retries by itself
            log.warning('database unavailable', extra={'reqId': rid, 'error': type(e).__name__})
            return json_response(
                503,
                error_body('DB_UNAVAILABLE', 'The database is briefly unavailable. Try again in a few seconds.', rid),
                {'Cache-Control': 'no-store', 'Retry-After': '5'},
            )
        except Exception as e:  # noqa: BLE001
            log.exception('unhandled error', extra={'reqId': rid})
            from app.observability import capture_exception

            capture_exception(e, request_id=rid, route=request.scope.get('state', {}).get('route', ''))
            return json_response(500, error_body('INTERNAL', INTERNAL_MSG, rid), {'Cache-Control': 'no-store'})
        resp = json_response(status, out, out_headers)
        if task is not None:
            resp.background = BackgroundTask(task)
        return resp

    app.add_api_route(
        API_PREFIX + '/{rest:path}', gateway, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'TRACE'], include_in_schema=False
    )

    # ---------------- frontend (dist/) with SPA fallback ----------------
    root = os.path.abspath(cfg.static_dir) if cfg.static_dir else None
    has_spa = bool(root) and os.path.isfile(os.path.join(root, 'index.html'))  # type: ignore[arg-type]

    async def fallback(request: Request) -> Response:
        rid = rid_of(request)
        method = request.method
        url = (request.scope.get('raw_path') or b'').decode('latin-1') or request.url.path
        qs = request.scope.get('query_string', b'').decode('latin-1')
        full_url = url + ('?' + qs if qs else '')
        if has_spa and method in ('GET', 'HEAD') and not url.startswith('/api/'):
            rel = os.path.normpath(request.url.path).lstrip('/')
            p = os.path.join(root, rel)  # type: ignore[arg-type]
            if rel and not rel.startswith('..') and os.path.isfile(p) and os.path.commonpath([root, os.path.abspath(p)]) == root:  # type: ignore[list-item]
                cc = 'public, max-age=31536000, immutable' if re.search(r'[\\/]assets[\\/]', p) else 'no-cache'
                mt = mimetypes.guess_type(p)[0] or 'application/octet-stream'
                if mt.startswith('text/') or mt in ('application/javascript', 'application/json'):
                    mt += '; charset=utf-8'
                return FileResponse(p, media_type=mt, headers={'Cache-Control': cc})
        if url.startswith('/api/'):
            return json_response(404, error_body('ROUTE_NOT_FOUND', f'No route for {method} {full_url.split("?")[0]}', rid))
        # a missing build asset is a real 404 (answering index.html makes the browser fail with a MIME-type error)
        is_asset = url.startswith('/assets/') or bool(ASSET_RE.search(full_url))
        if has_spa and not is_asset and method in ('GET', 'HEAD'):
            return FileResponse(os.path.join(root, 'index.html'), media_type='text/html; charset=utf-8', headers={'Cache-Control': 'no-cache'})  # type: ignore[arg-type]
        return json_response(404, error_body('ROUTE_NOT_FOUND', f'No route for {method} {full_url.split("?")[0]}', rid))

    app.add_api_route('/{rest:path}', fallback, methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE'], include_in_schema=False)

    # gzip for JSON/text above ~1 KB (browsers send Accept-Encoding: gzip; fetch() decompresses transparently). Level 5:
    # Starlette's default 9 costs ~3x the CPU for ~3% smaller bodies on these JSON payloads.
    app.add_middleware(GZipMiddleware, minimum_size=int(os.environ.get('GZIP_MIN_BYTES', '1024')), compresslevel=int(os.environ.get('GZIP_LEVEL', '5')))
    app.add_middleware(HeadersMiddleware, cfg=cfg, metrics=metrics)  # type: ignore[arg-type]
    return app


def client_ip(request: Request, trusted_proxies: int) -> str | None:
    """the client address: the socket peer, or — behind N trusted reverse proxies (TRUST_PROXY) — the N-th address
    from the right of X-Forwarded-For (the entries to its left are client-supplied and never trusted)"""
    peer = request.client.host if request.client else None
    if trusted_proxies <= 0:
        return peer
    xff = [p.strip() for p in (request.headers.get('x-forwarded-for') or '').split(',') if p.strip()]
    if len(xff) >= trusted_proxies:
        return xff[-trusted_proxies]
    return xff[0] if xff else peer


def _parse_query(qs: bytes) -> dict[str, Any]:
    """query object like Fastify's parser: values are strings; a repeated key becomes a list; '+' is a space"""
    from urllib.parse import unquote_plus

    out: dict[str, Any] = {}
    if not qs:
        return out
    for part in qs.decode('latin-1').split('&'):
        if not part:
            continue
        k, _, v = part.partition('=')
        try:
            k = unquote_plus(k, errors='strict')
        except UnicodeDecodeError:
            pass
        try:
            v = unquote_plus(v, errors='strict')
        except UnicodeDecodeError:
            pass
        if k in out:
            prev = out[k]
            out[k] = [*prev, v] if isinstance(prev, list) else [prev, v]
        else:
            out[k] = v
    return out


class _LazyApp:
    """`app.main:app` for uvicorn/gunicorn: the app (pools, config) is created in the worker process on first use."""

    def __init__(self) -> None:
        self._app: Any = None
        self._lock = threading.Lock()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if self._app is None:
            try:
                with self._lock:
                    if self._app is None:
                        self._app = create_app()
            except Exception as e:
                if scope.get('type') != 'lifespan':
                    raise
                # a configuration / startup-check failure must stop the process, not leave a server that answers 500:
                # report it through the lifespan protocol (uvicorn exits; a gunicorn worker fails to boot and the
                # arbiter halts) — in lifespan "auto" mode a raised exception would only be logged as "unsupported"
                await receive()  # lifespan.startup
                log.critical(f'startup refused: {e}')
                await send({'type': 'lifespan.startup.failed', 'message': str(e)})
                return
        await self._app(scope, receive, send)


app = _LazyApp()
