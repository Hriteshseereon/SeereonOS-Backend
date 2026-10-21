"""Environment configuration, validated once at startup with clear messages (the process refuses to start on any error).

ONE database connection string for everything that runs: DATABASE_URL (role seereon_app, RLS enforced). The owner
credential (MIGRATION_DATABASE_URL) is read only by `python -m app.db.migrate` and the development fixture loader —
never by the API, the worker or the production CLI (see app/db/migrate.py).

APP_ENV = development | production | test (NODE_ENV is still read when APP_ENV is not set). API_DOCS_ENABLED (default: false
in production, true elsewhere) serves the Swagger UI at /api/docs and the schema at /api/v1/openapi.json. Production refuses to start
without: an https APP_URL, an explicit CORS_ORIGINS list (never `*`), SMTP (SMTP_URL, or EMAIL_DELIVERY=disabled set
explicitly), MFA_ENCRYPTION_KEY, a DATABASE_URL with a real password, Razorpay webhook secret when payments are on.
Variables of removed features (DATABASE_APP_URL, DEMO_MODE, SEED_DEMO, SEED_JSON) are refused everywhere with a
message saying what replaced them, so a stale .env cannot silently change behaviour.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

DEV_MFA_KEY = 'development-only-mfa-key--never-use-in-production--0000'
DEV_DATABASE_URL = 'postgres://seereon_app:seereon_app@localhost:5432/seereonos'
DEV_CORS_ORIGINS = ('http://localhost:5173',)  # the Vite dev server (frontend/), development only

REMOVED = {
    'DATABASE_APP_URL': 'DATABASE_APP_URL was removed: the API uses ONE connection string, DATABASE_URL (role seereon_app). '
    'Migrations use MIGRATION_DATABASE_URL (owner role) in `python -m app.db.migrate` only.',
    'DEMO_MODE': 'DEMO_MODE was removed (no demo reset, no demo tokens). Local sample data: python -m app.devtools.fixtures load-demo.',
    'SEED_DEMO': 'SEED_DEMO was removed: nothing is seeded at startup. Local sample data: python -m app.devtools.fixtures load-demo.',
    'SEED_JSON': 'SEED_JSON was removed. Local sample data: python -m app.devtools.fixtures load-demo [--file …].',
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    env: str = 'development'
    host: str = '0.0.0.0'
    port: int = 8000
    database_url: str = DEV_DATABASE_URL
    pool_max: int = 20
    cors_origins: tuple[str, ...] = ()
    trust_proxy: int = 0  # number of trusted reverse proxies in front of the API (0 = use the socket peer address)
    email_delivery: str = 'disabled'  # smtp | disabled
    smtp_url: str | None = None
    mail_from: str = 'SeereonOS <no-reply@seereonos.com>'
    app_url: str = 'http://localhost:5173'
    static_dir: str | None = None
    log_level: str = 'info'
    tenant_cache_max: int = 50
    tenant_cache_max_mb: int = 512
    rate_limit_global: int = 600
    rate_limit_auth: int = 10
    session_idle_hours: int = 12
    session_absolute_days: int = 30
    shutdown_grace_ms: int = 10000
    mfa_encryption_key: str = DEV_MFA_KEY
    mfa_encryption_keys_previous: tuple[str, ...] = ()
    mfa_key_is_default: bool = True
    security_events_retention_days: int = 400
    freeze_rows: bool = True  # deep-freeze cached rows (development/test): illegal in-place mutations fail loudly
    statement_timeout_ms: int = 30000
    # payments (Razorpay): enabled when the key id and secret are set
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    razorpay_api_url: str = 'https://api.razorpay.com'
    # monitoring
    sentry_dsn: str | None = None
    sentry_environment: str | None = None
    sentry_traces_sample_rate: float = 0.0
    metrics_token: str | None = None
    support_email: str | None = None  # shown on the billing page (e.g. when online payments are off)
    api_docs: bool = True  # /api/docs (Swagger UI) + /api/v1/openapi.json; API_DOCS_ENABLED, default off in production
    worker_stale_s: int = 180  # /readyz warns when the newest worker heartbeat is older
    # worker (python -m app.worker)
    email_max_attempts: int = 8
    email_batch: int = 20
    email_interval_s: int = 5
    jobs_interval_s: int = 300
    cleanup_interval_s: int = 3600
    # performance (PERFORMANCE.md "Tuning knobs")
    memo_max_mb: int = 64  # response memo per worker (0 = off)
    memo_ttl_s: int = 60
    memo_verify: bool = False  # every memo hit is also computed and compared (tests / parity)
    request_threads: int = 4  # handler threads per worker process (CPU-bound handlers: more only adds GIL contention)
    scrypt_concurrency: int = 1  # password hashes computed at once per worker (scrypt: ~100 ms CPU + 32 MB each)
    pg_prepare_threshold: int | None = 5  # server-side prepared statements after N runs; None (PG_PREPARE_THRESHOLD=off) for PgBouncer < 1.21
    delta_max_rows: int = 2000  # incremental cache refresh from the change log up to this many changed rows (0 = off)
    write_threads: int = 2  # handler threads per worker for non-GET requests (separate from REQUEST_THREADS: reads never wait behind writes)
    lease_mode: str = 'adaptive'  # write requests take the tenant lease first: adaptive (after a lost race) | optimistic | pessimistic
    contention_window_s: int = 10
    extra: dict = field(default_factory=dict)

    @property
    def production(self) -> bool:
        return self.env == 'production'

    @property
    def payments_enabled(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def app_origin(self) -> str:
        u = urlparse(self.app_url)
        return f'{u.scheme}://{u.netloc}'


def _int(env: dict, key: str, default: int, lo: int = 0, hi: int = 2**53 - 1) -> int:
    raw = env.get(key)
    if raw is None or raw == '':
        return default
    from app.js import is_integer, js_number

    n = js_number(raw)  # Number(raw): ' 12 ', '1e3' are accepted
    if not is_integer(n) or n < lo or n > hi:
        raise ConfigError(f'{key} must be an integer between {lo} and {hi} (got "{raw}").')
    return int(n)


def _float(env: dict, key: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(key)
    if raw is None or raw == '':
        return default
    try:
        v = float(raw)
    except ValueError:
        v = float('nan')
    if not (lo <= v <= hi):
        raise ConfigError(f'{key} must be a number between {lo} and {hi} (got "{raw}").')
    return v


def _bool(env: dict, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == '':
        return default
    if re.fullmatch(r'(1|true|yes|on)', raw, re.I):
        return True
    if re.fullmatch(r'(0|false|no|off)', raw, re.I):
        return False
    raise ConfigError(f'{key} must be true or false (got "{raw}").')


def _trust_proxy(env: dict) -> int:
    raw = (env.get('TRUST_PROXY') or '').strip()
    if raw == '' or re.fullmatch(r'(0|false|no|off)', raw, re.I):
        return 0
    if re.fullmatch(r'(true|yes|on)', raw, re.I):
        return 1
    if re.fullmatch(r'[1-9]', raw):
        return int(raw)
    raise ConfigError(f'TRUST_PROXY must be false, true (one proxy) or the number of trusted proxies 1-9 (got "{raw}").')


def redact_url(raw: str) -> str:
    return re.sub(r'//[^@/]*@', '//***@', raw)


def _url(env: dict, key: str, default: str | None, schemes: tuple[str, ...]) -> str | None:
    raw = env.get(key) or default
    if not raw:
        return None
    try:
        u = urlparse(raw)
        ok = u.scheme in schemes and bool(u.netloc or u.path)
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError(f'{key} must be a {" or ".join(s + ":" for s in schemes)} URL (got "{redact_url(raw)}").')
    return raw


def _weak_db_password(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return True
    q = parse_qs(u.query)
    pw = u.password or (q.get('password') or [''])[0]
    user = u.username or (q.get('user') or [''])[0]
    return pw == '' or pw == user or bool(re.search(r'CHANGE_ME|^password$|^postgres$|^seereon_(app|owner)$', pw, re.I))


def load_config(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    errors: list[str] = []

    def get(f, fallback):  # type: ignore[no-untyped-def]
        try:
            return f()
        except ConfigError as e:
            errors.append(str(e))
            return fallback

    for k, msg in REMOVED.items():
        if (env.get(k) or '').strip():
            errors.append(msg)
    app_env = env.get('APP_ENV') or env.get('NODE_ENV') or 'development'
    if app_env not in ('development', 'production', 'test'):
        errors.append(f'APP_ENV must be development, production or test (got "{app_env}").')
    production = app_env == 'production'
    pg = ('postgres', 'postgresql')
    mfa_key = env.get('MFA_ENCRYPTION_KEY') or DEV_MFA_KEY
    static = env.get('STATIC_DIR')
    smtp_url = get(lambda: _url(env, 'SMTP_URL', None, ('smtp', 'smtps')), None)
    delivery_raw = (env.get('EMAIL_DELIVERY') or '').strip().lower()
    if delivery_raw not in ('', 'smtp', 'disabled'):
        errors.append(f'EMAIL_DELIVERY must be smtp or disabled (got "{delivery_raw}").')
    email_delivery = delivery_raw or ('smtp' if smtp_url else 'disabled')
    cors_raw = env.get('CORS_ORIGINS')
    cors = tuple(s.strip() for s in (cors_raw or '').split(',') if s.strip())
    if not cors and not production and cors_raw is None and app_env == 'development':
        cors = DEV_CORS_ORIGINS
    cfg = Config(
        env=app_env,
        host=env.get('HOST') or '0.0.0.0',
        port=get(lambda: _int(env, 'PORT', 8000, 1, 65535), 8000),
        database_url=get(lambda: _url(env, 'DATABASE_URL', None if production else DEV_DATABASE_URL, pg), '') or '',
        pool_max=get(lambda: _int(env, 'PG_POOL_MAX', 20, 2, 500), 20),
        cors_origins=cors,
        trust_proxy=get(lambda: _trust_proxy(env), 0),
        email_delivery=email_delivery,
        smtp_url=smtp_url,
        mail_from=env.get('MAIL_FROM') or 'SeereonOS <no-reply@seereonos.com>',
        app_url=(get(lambda: _url(env, 'APP_URL', None if production else 'http://localhost:5173', ('http', 'https')), '') or '').rstrip('/'),
        # development: serve the frontend build (cd frontend && npm run build) when it exists; elsewhere only when set
        static_dir=(static or None) if static is not None else ('../frontend/dist' if app_env == 'development' else None),
        log_level=env.get('LOG_LEVEL') or ('silent' if app_env == 'test' else 'info'),
        tenant_cache_max=get(lambda: _int(env, 'TENANT_CACHE_MAX', 50, 0, 100000), 50),
        tenant_cache_max_mb=get(lambda: _int(env, 'TENANT_CACHE_MAX_MB', 512, 0, 1_000_000), 512),
        rate_limit_global=get(lambda: _int(env, 'RATE_LIMIT_GLOBAL', 600, 1), 600),
        rate_limit_auth=get(lambda: _int(env, 'RATE_LIMIT_AUTH', 10, 1), 10),
        session_idle_hours=get(lambda: _int(env, 'SESSION_IDLE_HOURS', 12, 1, 24 * 365), 12),
        session_absolute_days=get(lambda: _int(env, 'SESSION_ABSOLUTE_DAYS', 30, 1, 3650), 30),
        shutdown_grace_ms=get(lambda: _int(env, 'SHUTDOWN_GRACE_MS', 10000, 0, 600000), 10000),
        mfa_encryption_key=mfa_key,
        mfa_encryption_keys_previous=tuple(s.strip() for s in (env.get('MFA_ENCRYPTION_KEY_PREVIOUS') or '').split(',') if s.strip()),
        mfa_key_is_default=not env.get('MFA_ENCRYPTION_KEY'),
        security_events_retention_days=get(lambda: _int(env, 'SECURITY_EVENTS_RETENTION_DAYS', 400, 1, 3650), 400),
        freeze_rows=get(lambda: _bool(env, 'FREEZE_ROWS', not production), not production),
        statement_timeout_ms=get(lambda: _int(env, 'STATEMENT_TIMEOUT_MS', 30000, 100, 3_600_000), 30000),
        razorpay_key_id=(env.get('RAZORPAY_KEY_ID') or '').strip() or None,
        razorpay_key_secret=(env.get('RAZORPAY_KEY_SECRET') or '').strip() or None,
        razorpay_webhook_secret=(env.get('RAZORPAY_WEBHOOK_SECRET') or '').strip() or None,
        razorpay_api_url=(get(lambda: _url(env, 'RAZORPAY_API_URL', 'https://api.razorpay.com', ('https', 'http')), 'https://api.razorpay.com') or '').rstrip(
            '/'
        ),
        sentry_dsn=(env.get('SENTRY_DSN') or '').strip() or None,
        sentry_environment=(env.get('SENTRY_ENVIRONMENT') or '').strip() or None,
        sentry_traces_sample_rate=get(lambda: _float(env, 'SENTRY_TRACES_SAMPLE_RATE', 0.0, 0.0, 1.0), 0.0),
        metrics_token=(env.get('METRICS_TOKEN') or '').strip() or None,
        support_email=(env.get('SUPPORT_EMAIL') or '').strip() or None,
        api_docs=get(lambda: _bool(env, 'API_DOCS_ENABLED', not production), not production),
        worker_stale_s=get(lambda: _int(env, 'WORKER_STALE_SECONDS', 180, 10, 86400), 180),
        email_max_attempts=get(lambda: _int(env, 'EMAIL_MAX_ATTEMPTS', 8, 1, 100), 8),
        email_batch=get(lambda: _int(env, 'EMAIL_BATCH', 20, 1, 500), 20),
        email_interval_s=get(lambda: _int(env, 'EMAIL_INTERVAL_SECONDS', 5, 1, 3600), 5),
        jobs_interval_s=get(lambda: _int(env, 'JOBS_INTERVAL_SECONDS', 300, 10, 86400), 300),
        cleanup_interval_s=get(lambda: _int(env, 'CLEANUP_INTERVAL_SECONDS', 3600, 60, 86400), 3600),
        memo_max_mb=get(lambda: _int(env, 'MEMO_MAX_MB', 64, 0, 100_000), 64),
        memo_ttl_s=get(lambda: _int(env, 'MEMO_TTL_S', 60, 1, 60), 60),
        memo_verify=get(lambda: _bool(env, 'MEMO_VERIFY', False), False),
        request_threads=get(lambda: _int(env, 'REQUEST_THREADS', 4, 1, 256), 4),
        scrypt_concurrency=get(lambda: _int(env, 'SCRYPT_CONCURRENCY', 1, 1, 64), 1),
        pg_prepare_threshold=None
        if (env.get('PG_PREPARE_THRESHOLD') or '').strip().lower() in ('off', 'none', 'never')
        else get(lambda: _int(env, 'PG_PREPARE_THRESHOLD', 5, 0, 1000), 5),
        delta_max_rows=get(lambda: _int(env, 'DELTA_MAX_ROWS', 2000, 0, 1_000_000), 2000),
        write_threads=get(lambda: _int(env, 'WRITE_THREADS', 2, 1, 256), 2),
        lease_mode=env.get('LEASE_MODE') or 'adaptive',
        contention_window_s=get(lambda: _int(env, 'CONTENTION_WINDOW_S', 10, 0, 3600), 10),
    )
    if cfg.lease_mode not in ('adaptive', 'optimistic', 'pessimistic'):
        errors.append(f'LEASE_MODE must be adaptive, optimistic or pessimistic (got "{cfg.lease_mode}").')
    if len(cfg.mfa_encryption_key) < 32:
        errors.append('MFA_ENCRYPTION_KEY must be at least 32 characters (e.g. openssl rand -base64 48).')
    for o in cfg.cors_origins:
        if o == '*' or '*' in o:
            errors.append('CORS_ORIGINS must list exact origins; "*" is never allowed.')
        elif not re.fullmatch(r'https?://[^/*]+', o):
            errors.append(f'CORS_ORIGINS entries must be origins like https://app.example.com (got "{o}").')
    if cfg.support_email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', cfg.support_email):
        errors.append(f'SUPPORT_EMAIL must be an e-mail address (got "{cfg.support_email}").')
    if email_delivery == 'smtp' and not smtp_url:
        errors.append('EMAIL_DELIVERY=smtp needs SMTP_URL (smtp://user:pass@host:587 or smtps://user:pass@host:465).')
    if cfg.payments_enabled is False and (cfg.razorpay_key_id or cfg.razorpay_key_secret):
        errors.append('Razorpay needs both RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (or neither: payments off).')
    if production:
        if not cfg.database_url:
            errors.append('DATABASE_URL is required when APP_ENV=production (role seereon_app; see database/docs/README.md).')
        elif _weak_db_password(cfg.database_url):
            errors.append('DATABASE_URL uses a default, empty or placeholder password; set a real one in production.')
        if cfg.mfa_key_is_default or re.search('CHANGE_ME', cfg.mfa_encryption_key, re.I):
            errors.append('MFA_ENCRYPTION_KEY is required when APP_ENV=production and must not be the placeholder (e.g. openssl rand -base64 48).')
        u = urlparse(cfg.app_url) if cfg.app_url else None
        if not cfg.app_url:
            errors.append('APP_URL is required when APP_ENV=production (the public https URL of the web app, used in e-mail links).')
        elif u is None or u.scheme != 'https' or u.hostname in ('localhost', '127.0.0.1', '0.0.0.0'):
            errors.append(f'APP_URL must be the public https URL of the web app when APP_ENV=production (got "{cfg.app_url}").')
        if not cfg.cors_origins:
            errors.append('CORS_ORIGINS is required when APP_ENV=production: the exact origin(s) of the web app, e.g. https://app.example.com.')
        for o in cfg.cors_origins:
            if o.startswith('http://'):
                errors.append(f'CORS_ORIGINS must use https origins in production (got "{o}").')
        if not delivery_raw and not smtp_url:
            errors.append(
                'SMTP is required when APP_ENV=production: set SMTP_URL (and MAIL_FROM), or set EMAIL_DELIVERY=disabled explicitly '
                '(no password-reset, invitation or notification e-mail will be sent).'
            )
        if cfg.payments_enabled and not cfg.razorpay_webhook_secret:
            errors.append('RAZORPAY_WEBHOOK_SECRET is required when Razorpay is enabled in production (webhooks confirm payments).')
        for k in ('SMTP_URL', 'APP_URL', 'CORS_ORIGINS', 'RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET', 'RAZORPAY_WEBHOOK_SECRET', 'METRICS_TOKEN', 'SENTRY_DSN'):
            if re.search('CHANGE_ME', env.get(k) or '', re.I):
                errors.append(f'{k} still contains a CHANGE_ME placeholder.')
        if cfg.metrics_token is not None and len(cfg.metrics_token) < 24:
            errors.append('METRICS_TOKEN must be at least 24 characters.')
    if errors:
        raise ConfigError('Invalid configuration:\n  - ' + '\n  - '.join(errors))
    return cfg


_CFG: Config | None = None


def settings() -> Config:
    """the process configuration (loaded on first use)"""
    global _CFG
    if _CFG is None:
        _CFG = load_config()
    return _CFG


def set_settings(cfg: Config | None) -> None:
    """tests / app factory: install a configuration"""
    global _CFG
    _CFG = cfg
