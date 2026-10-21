# SeereonOS API — Python (FastAPI)

The production backend of SeereonOS: FastAPI + psycopg 3 on PostgreSQL 16, Python 3.11. Three processes from ONE
image: the **API** (gunicorn + uvicorn workers), the **worker** (`python -m app.worker`: e-mail delivery and scheduled
jobs) and the one-shot **migrate** job. The web app (`frontend/`, a static build) calls the API at `/api/v1/...`.

* **Database** (setup, roles, migrations, backups, restore, monitoring, data dictionary): [database/docs/README.md](../database/docs/README.md), [ERD](../database/docs/ERD.md)
* **Security** model and residual risks: [SECURITY.md](SECURITY.md) · **Capacity and tuning**: [PERFORMANCE.md](PERFORMANCE.md)
* **Domain logic provenance**: the business rules are a behavioural port of the TypeScript specification in
  `frontend/src/mock` (golden vectors + parity harness keep them identical): [PORTING.md](PORTING.md), [PORT_STATUS.md](PORT_STATUS.md)

## Production

### 0. Install (bare host; the Docker image in §5 does this for you)

Python 3.11 and a PostgreSQL 16 client (`psql`, `pg_dump` for backups). From the repository root:

```sh
python3.11 -m venv /opt/seereonos/venv && . /opt/seereonos/venv/bin/activate
pip install -r backend/requirements.txt          # runtime only (requirements-dev.txt adds the test/lint tools)
cd backend                                       # every python -m app.… / gunicorn command below runs from backend/
```

### 1. Database (once per environment)

```sh
psql -v ON_ERROR_STOP=1 "postgres://postgres@db.internal:5432/postgres" -v dbname=seereonos \
     -v owner_password="$SEEREON_OWNER_PASSWORD" -v app_password="$SEEREON_APP_PASSWORD" -f database/bootstrap.sql
```

(run from the repository root — the path is `database/bootstrap.sql`). One database, two roles: `seereon_owner`
(migrations only) and `seereon_app` (everything that runs). Details and TLS/PgBouncer notes: database/docs/README.md
"Setting up a database".

**Roles are cluster-wide.** `seereon_owner` / `seereon_app` belong to the PostgreSQL *cluster*, not to one database, and
re-running `bootstrap.sql` for ANY database name resets both passwords. Use one cluster per environment (production,
staging, development/test); never run the development or test bootstrap (simple passwords) on the production
cluster — it would change the passwords the running API and worker use.

### 2. Configure

Copy [.env.example](.env.example) into your secret store / orchestrator (or `backend/deploy/.env` for compose) and
replace every placeholder. The API, the worker and the CLI **refuse to start** in production when a required value is
missing, still `CHANGE_ME`, or unsafe — the error lists every problem at once (gunicorn exits with "Worker failed to
boot" and the reason in the log; it never comes up answering 500). On a plain host, load the file into the shell with
`set -a; . ./.env; set +a` — quote values that contain spaces or `<>` (e.g. `MAIL_FROM="Acme <no-reply@acme.example>"`).

| variable | required | default | meaning |
|---|---|---|---|
| `APP_ENV` | yes | `development` | `production` turns on every check below (`NODE_ENV` is still read as a fallback) |
| `DATABASE_URL` | **yes** | dev: `postgres://seereon_app:seereon_app@localhost:5432/seereonos` | THE connection string of the API, worker and CLI: role `seereon_app`. Refused in production with an empty/default/`CHANGE_ME` password |
| `MIGRATION_DATABASE_URL` | migrate job | falls back to `DATABASE_URL` | owner role `seereon_owner`; read ONLY by `python -m app.db.migrate` (and dev fixtures). Never give it to the API/worker |
| `MIGRATIONS_DIR` | no | `../database/migrations` (checkout), `/app/migrations` (image) | where the migrate job and the startup check find the SQL |
| `APP_URL` | **yes** | dev: `http://localhost:5173` | public https URL of the web app: links in password-reset (`/reset-password?token=…`), invitation (`/invite/<token>`) and notification e-mails. Not localhost, https only |
| `CORS_ORIGINS` | **yes** | dev: `http://localhost:5173` | exact origin(s) of the web app, comma-separated, https, never `*` |
| `MFA_ENCRYPTION_KEY` | **yes** | dev key (refused in prod) | ≥ 32 chars (`openssl rand -base64 48`): seals TOTP secrets and queued reset/invitation e-mails (AES-256-GCM). Rotate by moving the old key to `MFA_ENCRYPTION_KEY_PREVIOUS` (comma-separated) |
| `SMTP_URL` | **yes**, or `EMAIL_DELIVERY=disabled` | — | `smtps://user:pass@host:465` or `smtp://user:pass@host:587` (STARTTLS required when credentials are sent; `?tls=required` forces it) |
| `EMAIL_DELIVERY` | no | `smtp` when `SMTP_URL` is set, else `disabled` | `disabled` must be explicit in production and is logged loudly on every start: no reset links, invitations or notification e-mails |
| `MAIL_FROM` | no | `SeereonOS <no-reply@seereonos.com>` | sender |
| `SUPPORT_EMAIL` | no | — | shown on the billing page (`GET /billing` → `payments.support_email`) |
| `API_DOCS_ENABLED` | no | `false` in production, `true` otherwise | serves the Swagger UI at `/api/docs` and the schema at `/api/v1/openapi.json`; off, both answer 404 like any unknown route. Turn it on in production only behind an internal-network restriction |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | no | — | both or neither. Neither: payments off (`409 PAYMENTS_NOT_CONFIGURED`) |
| `RAZORPAY_WEBHOOK_SECRET` | when keys set | — | webhook `https://<api>/api/v1/billing/webhooks/razorpay`, events `payment.captured`, `order.paid`, `payment.failed` |
| `TRUST_PROXY` | no | `false` (0) | number of reverse proxies that append to `X-Forwarded-For` (`true` = 1). Only set it when clients cannot reach the API directly |
| `METRICS_TOKEN` | no | — | enables `GET /metrics` (Prometheus) with `Authorization: Bearer <token>`; ≥ 24 chars. Unset: `/metrics` is 404 |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE` | no | —, `APP_ENV`, `0` | optional error reporting; request bodies, cookies, auth headers, tokens and personal data are scrubbed |
| `STATIC_DIR` | no | dev: `../frontend/dist`; prod: off | serve a built web app from the API's origin with SPA fallback (production normally serves the web app separately) |
| `TZ` | yes | image: `Asia/Kolkata` | the business time zone ("today" for every tenant); API and worker must match |
| `LOG_LEVEL` | no | `info` | JSON lines on stdout; secrets, tokens, password hashes and URL credentials are redacted |
| `SESSION_IDLE_HOURS`, `SESSION_ABSOLUTE_DAYS` | no | 12, 30 | session expiry |
| `RATE_LIMIT_AUTH`, `RATE_LIMIT_GLOBAL` | no | 10, 600 | per minute: auth endpoints (exact, shared in PostgreSQL), per token (per worker process) |
| `SECURITY_EVENTS_RETENTION_DAYS` | no | 400 | audit trail retention (worker `cleanup`) |
| `WORKER_STALE_SECONDS` | no | 180 | `/readyz` reports the worker as stale after this |
| `EMAIL_MAX_ATTEMPTS`, `EMAIL_BATCH` | no | 8, 20 | delivery attempts before dead-letter; messages per round |
| `EMAIL_INTERVAL_SECONDS`, `JOBS_INTERVAL_SECONDS`, `CLEANUP_INTERVAL_SECONDS` | no | 5, 300, 3600 | worker schedule |
| `PORT`, `WEB_CONCURRENCY`, `WORKER_TIMEOUT`, `GRACEFUL_TIMEOUT`, `KEEPALIVE`, `MAX_REQUESTS` | no | 8000, 2, 120, 20, 75, 0 | gunicorn (`gunicorn.conf.py`) |
| `PG_POOL_MAX`, `STATEMENT_TIMEOUT_MS`, `TENANT_CACHE_MAX`, `TENANT_CACHE_MAX_MB`, `REQUEST_THREADS`, `WRITE_THREADS`, … | no | PERFORMANCE.md | tuning knobs |

Removed in v8 and refused everywhere if still set: `DATABASE_APP_URL` (one `DATABASE_URL` now), `DEMO_MODE`,
`SEED_DEMO`, `SEED_JSON` (no demo data in the product; see "Development").

**Web app build** (`frontend/`): `cd frontend && npm run build` → `frontend/dist`, with `VITE_API_BASE_URL` set to the
API's absolute base URL **including `/api/v1`** (e.g. `https://api.example.com/api/v1`) when the API is on another
origin — leave it unset when the API serves the web app or a reverse proxy routes `/api/*` on the same origin. The API
must then list the web app's origin in `CORS_ORIGINS`.

### 3. Migrate, then start

```sh
python -m app.db.migrate                       # MIGRATION_DATABASE_URL (owner). Checksummed, advisory-locked, idempotent
python -m app.db.migrate --status              # applied / pending (exit 1 when pending)
gunicorn -c gunicorn.conf.py app.main:app      # the API (DATABASE_URL)
python -m app.worker                           # the worker (DATABASE_URL); run ≥ 1 (several are safe)
```

The API never migrates on start. **Startup checks** (`app/startup.py`, API, worker and CLI): the configuration
refusals above; the database answers; the connected role is not a superuser, has no `BYPASSRLS` and does not own the
tables (production refuses — RLS would not apply); V008 is applied and no migration in `MIGRATIONS_DIR` is pending
(production refuses: "run python -m app.db.migrate"). `python -m app.cli check` runs the same checks and prints a
summary (database, worker heartbeat, e-mail, payments, app URL, CORS, proxies).

### 4. First accounts — on an empty database

There is no demo data and no default account in production. Create them with the CLI (DATABASE_URL, production config):

```sh
python -m app.cli create-superadmin --email ops@example.com --name "Platform Admin"      # prompts for the password
printf '%s\n' "$PW" | python -m app.cli create-superadmin --email ops@example.com --password-stdin --update-password  # reset it
python -m app.cli create-tenant --company "Acme Consulting" --admin-email admin@acme.example --admin-name "Asha Rao" \
  [--industry it_services|sap_consulting|product|other] [--password-stdin]
```

`create-tenant` uses exactly the sign-up provisioning (roles, masters, trial subscription). It sends no e-mail: give
the administrator their password out of band (or let them use "Forgot password?" on the sign-in page). Companies can also sign up
themselves in the web app; administrators invite colleagues (an invitation e-mail with `APP_URL/invite/<token>`);
everyone can reset a forgotten password (e-mail with `APP_URL/reset-password?token=…`). Passwords: at least 10
characters, scrypt-hashed. This whole journey on a fresh empty database in production configuration is
`tests/integration/test_credentials_journey.py`.

**First run of a new company (in the web app, as its administrator)** — a fresh workspace has roles, departments,
designations, grades, a shift and templates, but nothing company-specific. Before the first GST invoice and payroll:

1. Admin › Organisation setup › **Organisation profile**: legal name, PAN, **GSTIN**, registered address, city,
   **state**, PIN (the seller block of every tax invoice; the state decides CGST+SGST vs IGST).
2. Finance › Invoices › **Settings**: invoice prefix, **bank account number**, IFSC, account name, authorised
   signatory. (The organisation profile keeps only a masked account number, so the full number is entered here.)
3. People & users › **Create employee** for each person with their **monthly CTC** (cost rates drive costing,
   margins and payroll); they receive a welcome e-mail with a temporary password and must change it at first sign-in.
4. Admin › **Maker-checker**: turn it on if salaries, roles and payroll finalisation need a second person (needs at
   least two people holding the permission).

A draft invoice saved while 1–2 were incomplete picks up the missing seller, bank and signatory details when it is
issued.

### 5. Docker / compose

```sh
docker build -f backend/Dockerfile -t seereonos-api .          # from the REPOSITORY ROOT (copies database/migrations)
cd backend/deploy && cp ../.env.example .env && $EDITOR .env    # every CHANGE_ME
docker compose up -d --build                                   # db, migrate (one-shot), api, worker
docker compose --profile web up -d                             # + nginx serving frontend/dist (build it first)
docker compose run --rm api python -m app.cli create-superadmin --email ops@example.com
```

The image is Python only (no Node.js, no frontend, no demo data, `app/devtools` removed), non-root (uid 10001),
read-only app files, migrations at `/app/migrations`, HEALTHCHECK on `/readyz`; the worker service's healthcheck is
`python -m app.worker --healthcheck` (fresh heartbeat). The bundled `db` service runs `database/bootstrap.sql` on first
start with the passwords from `.env`. TLS terminates in front (load balancer / reverse proxy); set `TRUST_PROXY=1` there.
Client addresses come ONLY from `TRUST_PROXY` (gunicorn.conf.py sets `forwarded_allow_ips = ''`, so uvicorn never
rewrites the peer address from `X-Forwarded-For` by itself). Without a proxy, gunicorn can terminate TLS itself:
`gunicorn -c gunicorn.conf.py --certfile fullchain.pem --keyfile privkey.pem app.main:app`.

### 6. The worker

`python -m app.worker` — separate process (compose service `worker`), any number of instances:

| job | interval | what |
|---|---|---|
| `email` | `EMAIL_INTERVAL_SECONDS` (5 s) | deliver the queue over SMTP: claim with `FOR UPDATE SKIP LOCKED` + lease, retry with back-off `min(6 h, 60 s·2^(n−1))`, dead-letter after `EMAIL_MAX_ATTEMPTS` or a permanent 5xx; the in-app outbox row moves `queued` → `sent` / `failed` only on real delivery / dead-letter |
| `escalations` | `JOBS_INTERVAL_SECONDS` (300 s) | approval SLA and workflow-stage escalations |
| `timesheet_locks` | 300 s | auto-lock past timesheet weeks |
| `milestones` | 300 s | milestone deadlines; empty-milestone cleanup (never on a project still being planned) |
| `appraisal_cycles`, `exits`, `contract_renewals` | 300 s | cycle status by date; offboarding completion; renewal reminders |
| `cleanup` | `CLEANUP_INTERVAL_SECONDS` (3600 s) | expired sessions, reset tokens, rate-limit windows, idempotency keys, change log, audit retention, settled deliveries, expired pending tokens |

Every periodic job holds a PostgreSQL advisory lock and, in the scheduled loop, is skipped when another instance already
started it within the current interval (`job_runs`, database clock) — N workers run each job once per interval — and reuses the same domain
functions as the API's on-read paths. `--once [--jobs a,b]` runs a round and exits (cron, catch-up). Heartbeats in
`worker_heartbeats`, last runs in `job_runs`. SIGTERM finishes the current job.

### 7. Monitoring

* While the database is down or restarting, API calls answer `503 DB_UNAVAILABLE` with `Retry-After: 5` (never a 500);
  pooled connections are checked when handed out, so service resumes as soon as PostgreSQL is back.
* `GET /healthz` — process up. `GET /readyz` — 200 when the database answers (503 otherwise), with
  `worker: ok | stale | missing` (+ `warnings`), `email_delivery`, `payments`. Responses under `/api/`, `/healthz`,
  `/readyz`, `/metrics` are `Cache-Control: no-store` with `Vary: Origin, Authorization`.
* `GET /metrics` (with `METRICS_TOKEN`): `seereon_http_requests_total`, `seereon_http_errors_total`,
  `seereon_http_request_duration_seconds` (by method, route template, status), `seereon_db_up`, `seereon_db_pool`,
  `seereon_email_queue{status}`, `seereon_email_oldest_due_seconds`, `seereon_job_runs_total`,
  `seereon_job_failures_total`, `seereon_job_last_success`, `seereon_job_last_finished_timestamp_seconds`,
  `seereon_workers`, `seereon_worker_heartbeat_age_seconds`, `seereon_jobs_total`, `seereon_process_start_time_seconds`.
  Suggested alerts: `/readyz` not 200; worker heartbeat age > 180 s; `seereon_email_queue{status="dead"}` rising;
  oldest due e-mail > 15 min; job failures rising; 5xx rate.
* Logs: JSON lines, one `request completed` per request with `request_id` (also in the `X-Request-Id` response header
  and every error body), no secrets. Optional Sentry (`SENTRY_DSN`).
* Database-side queries (queue, locks, sizes, vacuum, security events): database/docs/README.md "Monitoring".

### 8. Backups

`database/scripts/backup.sh` (encrypted `pg_dump -Fc`, retention), `restore.sh`, and the weekly scripted restore test
`restore_test.sh`: database/docs/README.md "Backups" / "Restore".

### Payments (Razorpay)

`GET /billing` → `payments: {enabled, provider, key_id, support_email}`. `POST /billing/payments/order` (`{plan_code,
seats}` or `{invoice_id}`) creates a due billing invoice and a Razorpay order server-side (amount from the invoice, never
from the client) and returns `{order_id, provider, key_id, amount_paise, currency, invoice, purpose, plan_code, seats, …}` for Razorpay Checkout;
`POST /billing/payments/verify` checks the checkout signature (HMAC-SHA256 of `order_id|payment_id` with the key
secret) and applies the plan / seats exactly once; `POST /billing/webhooks/razorpay` (public, `X-Razorpay-Signature`
over the raw body, idempotent by `X-Razorpay-Event-Id`) does the same when the browser never comes back. A seat
increase returns `payment_required` with the order when payments are on. With payments off every payment route
answers `409 PAYMENTS_NOT_CONFIGURED`. When enabled, the CSP of the API-served web app allows
`checkout.razorpay.com` (script, frame), `api.razorpay.com` (frame, connect) and `lumberjack.razorpay.com` (connect).

### File storage

Uploads are stored in PostgreSQL (`data:` URLs, ≤ 15 MB per request, content-checked). Rationale and the path to
S3-compatible object storage: database/docs/README.md "File storage".

## Development

Prerequisites: Python 3.11, PostgreSQL 16. Unzip the three packages side by side (`frontend/`, `backend/`, `database/`):
the backend reads its migrations from `../database/migrations` and serves `../frontend/dist` when it is built.
The Python code is the single source of truth for the business rules.

```sh
pip install -r backend/requirements-dev.txt
sudo -u postgres psql -v ON_ERROR_STOP=1 -v dbname=seereonos -v owner_password=seereon_owner -v app_password=seereon_app -f database/bootstrap.sql
cd backend && cp .env.local.example .env.local && set -a && . ./.env.local && set +a
python -m app.db.migrate
python -m app.devtools.fixtures load-demo      # DEVELOPMENT demo data (refused in production) — database/fixtures/README.md
uvicorn app.main:app --port 8000 --reload      # http://localhost:8000/api/docs ; serves ../frontend/dist when built
cd ../frontend && npm run dev                  # the web app on :5173 (VITE_API_BASE_URL=http://localhost:8000/api/v1)
```

Demo accounts: `santosh@seereon.co.in`, `platform@seereon.co.in`, … password `Seereon@123` (development only).
E-mail locally: with `EMAIL_DELIVERY=disabled`, reset and invitation e-mails are still queued (sealed) — read them with
`python -m app.devtools.mailbox [--to EMAIL] [--last N]`; or run a catcher (Mailpit) and set
`SMTP_URL=smtp://localhost:1025` + run the worker. `python -m app.db.reset` drops and re-creates an empty schema
(development only).

### Tests

```sh
cd backend
TZ=Asia/Kolkata python -m pytest          # golden + unit + integration; creates its OWN database seereonos_test
ruff check .
```

* `tests/golden`: regression snapshots — the business engines replayed on frozen inputs (`tests/golden/vectors/`).
  They were produced from the original TypeScript reference; when you change a rule on purpose, update the affected
  vector together with the code and say so in the review.
* `tests/unit`: config refusals, payment signatures (fixed vectors), log redaction, secret box, …
* `tests/integration` (PostgreSQL): the credentials journey on an empty DB in production config (super admin → tenant →
  invite → accept → reset via a real SMTP sink → first project, costing, invoice draft), single-role security (the
  runtime role cannot touch credentials; RLS), the worker (SKIP LOCKED, leases, back-off, dead-letter, advisory locks,
  heartbeat), payments (orders, verify, webhooks, idempotency), ops (readyz, metrics, cache headers, Sentry scrubbing,
  no secrets in logs), auth, HTTP contract, unit of work, cache, provisioning.

### End-to-end against a real stack

```sh
backend/tests/e2e/stack.sh [--build] [--worker]      # fresh DB, demo fixture, gunicorn :8000 (+ frontend/dist with --build)
backend/tests/e2e/stack.sh --empty                   # empty DB, like a fresh production install
backend/tests/e2e/stack.sh --no-reset --port 8001    # a second instance on the same DB
backend/tests/e2e/stack.sh --stop [--port 8001]
python3 backend/tests/e2e/twofactor.py               # Playwright: two-step verification in the browser
```

## Layout

```
backend/
  app/
    main.py          app factory: security headers/CSP, CORS, request ids, JSON logs, rate limits, body limits,
                     /healthz /readyz /metrics, Razorpay webhook, OpenAPI + Swagger UI, the /api/v1 gateway
    config.py        environment + production refusals          startup.py   database/role/migration checks
    worker.py        python -m app.worker                         cli.py       check, create-superadmin, create-tenant
    observability.py Sentry (scrubbed) + Prometheus metrics       logging_setup.py  JSON logs with redaction
    runtime.py       request runtime: session → idempotency → memo → unit of work → dispatch → flush, retries
    db/              pool (one role), mapping, migrate, seed loader, reset (dev), uow, cache, backend (flush, SECURITY DEFINER calls)
    auth/            passwords (scrypt), tokens, AES-GCM secret box, auth routes (login/MFA/reset/invites)
    security/        DB-backed rate limits + security_events audit
    payments/        Razorpay client, signatures, webhook
    workers/         SMTP delivery            devtools/  DEVELOPMENT ONLY: demo fixture loader, mailbox (not in the image)
    domain/ engine/ routes/ reports/ seeds/   the business logic (ports of frontend/src/mock, frontend/src/domain)
    catalog/data/    GENERATED shared constants          static/  vendored Swagger UI assets
  deploy/            docker-compose.yml (db, migrate, api, worker, optional web), nginx.conf
  docker/initdb.sh   first-start hook of the bundled postgres container (runs database/bootstrap.sql)
  tests/             golden/, unit/, integration/, e2e/ (stack.sh, twofactor.py, dbdump.py), perf/
  Dockerfile, gunicorn.conf.py, .env.example (production), .env.local.example (development), requirements*.txt
```

## Architecture in one paragraph

Each request runs in a unit of work (`app/db/uow.py`). Authenticated tenant requests read through ONE
`REPEATABLE READ READ ONLY` snapshot as `seereon_app` (RLS on `app.tenant_id`), loading tables lazily; tables whose
version matches `tenant_table_versions` come from the per-process cache (immutable rows, deep-frozen outside
production). Handlers never mutate rows in place: writes copy on write into a request overlay, which is diffed and
flushed in one transaction on the same role: advisory lock on the tenant, read-set version check, batched
DELETE/UPDATE/INSERT, then credentials, sessions, reset tokens, login attempts, queued e-mails and the idempotency
record through the `SECURITY DEFINER` functions of V008. A lost race re-runs the request holding the tenant lock (409
CONFLICT_RETRY after 3); other processes apply the flush's change log (`table_changes`) instead of re-reading whole
tables. GET responses are memoised per worker while every table they read keeps its version (PERFORMANCE.md).
Super-admin routes run in the platform scope (`app.platform_admin`) on the same role.
