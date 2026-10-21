# SeereonOS API — security

Scope: the Python API (`backend/app`: FastAPI on gunicorn/uvicorn workers), the business rules it ports from the
shared TypeScript spec (`frontend/src/mock/routes`, `frontend/src/mock/server.ts`, `frontend/src/domain`), the
PostgreSQL schema (`database/migrations`), the worker (`app/worker.py`) and the web app's auth screens. Reviewed against **OWASP ASVS 4.0.3, level 2** (B5 hardening,
October 2026, on the Node reference; every control below was re-verified in the Python implementation by the G1
integration gate — the module that implements it is cited; v8 production hardening, October 2026: one database role,
`SECURITY DEFINER` boundary, sealed e-mail queue, worker, payments, metrics — §7, §8, §9).

## 1. Threat model (summary)

| | |
|---|---|
| **Assets** | tenant business data (projects, money, payroll, salaries, HR documents, deals); credentials and second factors; session tokens; the audit trail |
| **Actors** | anonymous internet users; signed-in users of a tenant with different roles (employee → CEO); administrators of a tenant; users of *another* tenant; the platform super admin; an operator with database or backup access |
| **Trust boundaries** | browser ↔ API (TLS, bearer token); API / worker ↔ PostgreSQL (ONE runtime role under RLS, credentials only through `SECURITY DEFINER` functions); Razorpay → API (signed webhooks); worker → SMTP (TLS); tenant ↔ tenant (row ownership, RLS); role ↔ role inside a tenant (permission catalogue, maker-checker, View-as read-only) |
| **Main threats** | credential stuffing / brute force; stolen password; session theft or fixation; cross-tenant data access; horizontal / vertical privilege escalation inside a tenant (IDOR, mass assignment, permission escalation); malicious uploads (stored XSS through files); injection; account enumeration; information leakage through errors and logs; secrets in the repository or at rest in backups; abuse at scale (multi-instance rate-limit bypass) |

## 2. Controls, mapped to ASVS

Paths are relative to `backend/app/`.

| ASVS | Control | Where (Python) |
|---|---|---|
| V1.4, V4.1.1 | Authorisation is enforced server-side in one router core for every route: session → Ctx rebuilt from the database on **every** request (role, permissions, tenant status), platform-only routes (403), View-as is read-only (403), lapsed tenants are read-only (402), a forced password change blocks writes | `dispatch.py`, `ctx.py`, `runtime.py` |
| V2.1.1–2.1.9 | Password ≥ 8 (workspace minimum, default 10; max 128), no composition rules, common/breached passwords refused, e-mail-as-password refused; same rule in the forms (strength meter, `frontend/src/domain/passwordPolicy.ts`) | `domain/password_policy.py`, `auth/overrides.py`, `routes/auth.py` |
| V2.2.1 | Anti-automation: per-account lockout (tenant rule, default 5 failures / 15 min, counted in `login_attempts`, persists across restarts and instances) **and** DB-backed rate limits on every auth endpoint (§5); second-step failures count toward the lockout | `auth/overrides.py`, `security/store.py`, `main.py` (auth limiter) |
| V2.4.1–2.4.5 | scrypt (N = 2^15, r = 8, p = 1, 64-byte key, 16-byte per-user salt, NFKC), constant-time comparison (`hmac.compare_digest`); format `scrypt$N$r$p$salt$hash`, interchangeable with the Node-era hashes | `auth/passwords.py` |
| V2.5.1–2.5.6 | Password reset: 32-byte random token, stored as SHA-256, single use, 60 min, revokes all sessions; the link (`APP_URL/reset-password?token=…`) is sent ONLY by e-mail — the API answers `{accepted: true}` for known and unknown e-mails alike, every answer takes ≥ 400 ms, and the queued message body is sealed (AES-256-GCM) until the worker delivers it, then erased; an unknown e-mail at sign-in still spends a real scrypt verification (dummy hash) | `auth/overrides.py` (`_not_before`, `_dummy`, `reset_mail`), `workers/email.py` |
| V2.7, V2.8 | Real two-step verification: RFC 6238 TOTP, single-use codes, 10 single-use recovery codes (hashed), short-lived single-use sign-in challenge (§4) | `domain/totp.py`, `engine/two_factor.py`, `auth/overrides.py` |
| V3.1.1 | Tokens never travel in URLs (bearer header only); reset / invite tokens that must sit in links are redacted from logs | `logging_setup.py` (`redact_url`, `redact`) |
| V3.2.1–3.2.3 | Opaque 256-bit session tokens from the CSPRNG (`secrets.token_bytes`), a new one on every sign-in (no fixation), stored only as SHA-256 | `auth/crypto.py`, `auth/overrides.py` |
| V3.3.1–3.3.4 | Logout revokes server-side; idle (12 h, `SESSION_IDLE_HOURS`) and absolute (30 d) expiry checked in SQL on every request; password change and turning on 2FA sign out the user's other sessions; admin password reset, admin 2FA reset and deactivation end every session; users can list and revoke sessions | `db/backend.py` (`resolve_session`), `auth/overrides.py`, `routes/people.py` |
| V3.4, V3.5 | No cookies: bearer tokens are not sent automatically, so CSRF does not apply; CORS is an exact allowlist (`CORS_ORIGINS`, required and https in production; development default `http://localhost:5173`), `Access-Control-Allow-Credentials` is never sent | `main.py` (CORS middleware) |
| V4.1.3, V4.2.1 | Least privilege by permission catalogue; IDOR protection by tenant-scoped lookups (`find()` → 404 for other tenants) and per-record visibility checks; permission escalation guarded (`PERMISSION_ESCALATION`, `assert_no_lockout`), maker-checker for sensitive changes | `routes/*`, `routes/access.py`, `engine/maker_checker.py` |
| V4.2.1, V5.1.2 | Mass assignment: `update()` drops id / tenant_id / version / created_* / updated_at from every patch; handlers whitelist fields (`/users/:id` JOB/ACCESS lists, theme keys); the row mapper refuses unknown columns (`UNKNOWN_COLUMN`) | `ctx.py`, `routes/auth.py`, `routes/people.py`, `db/mapping.py` |
| V4.3.1 | Admin interfaces need the user-admin permission (`users.manage`); platform routes require the super-admin flag and run in a separate global scope (`app.platform_admin`) on the same runtime role | `dispatch.py`, `router.py` (`super_admin`), `db/uow.py` |
| V5.1.4, V13.1.3 | JSON only (415 `UNSUPPORTED_MEDIA_TYPE` otherwise), 1 MB body (15 MB on the data-URL upload routes, decided by path), malformed JSON → 400 envelope | `main.py`, `router.py` (`body_limit_for`) |
| V5.3.4, V5.3.5 | All SQL is parameterised (psycopg placeholders); identifiers are quoted (`ident()`) and come from schema metadata (`app_table_meta` / `app_column_meta`), never from requests; the `SECURITY DEFINER` functions use no dynamic SQL and pin `search_path` | `db/backend.py`, `db/mapping.py`, `database/migrations/V008__single_role_runtime.sql` |
| V5.3.3 | React escapes output; the only raw HTML (issued letters) is rendered server-side by an escaping markup renderer | `engine/doc_markup.py` |
| V7.1, V7.2 | Structured JSON logs (pino-compatible lines) with request id; `Authorization`, cookies, `Idempotency-Key`, passwords, tokens, secrets, data URLs and temporary passwords redacted by key, and bearer tokens, URL credentials, `token=` parameters and scrypt hashes redacted inside any message text (`tests/integration/test_ops.py`: no secret reaches the logs); optional Sentry with the same scrubbing (no request bodies, cookies, auth headers or user e-mails); authentication audit trail in `security_events` (§6) | `logging_setup.py`, `observability.py`, `security/audit.py`, `security/store.py` |
| V7.4.1 | Errors: generic message + request id, no stacks or internals in responses (500 `INTERNAL`); framework-level errors (404 route, 413, 415, 400 JSON) use the same envelope | `errors.py`, `main.py` |
| V8.1, V8.3 | Tenant isolation (RLS, §7); salary, margin, contract value and project costing redacted for roles without the money permissions; tenant export excludes credentials, sessions, tokens and 2FA state; `Cache-Control: no-store` + `Vary: Origin, Authorization` on every API, health and metrics response | `routes/*`, `routes/settings.py`, `main.py` |
| V6.2, V8.3.7 | TOTP secrets sealed with AES-256-GCM (key from `MFA_ENCRYPTION_KEY`, rotation via `MFA_ENCRYPTION_KEY_PREVIOUS`; byte-compatible with the Node-era sealed values — golden test); recovery codes and challenge tokens stored as SHA-256 | `auth/secret_box.py`, `engine/secrets.py` |
| V9.1 | TLS terminates at the load balancer / reverse proxy; HSTS (1 year, subdomains) when `APP_ENV=production` | `main.py`, deployment |
| V12.1–12.5 | Uploads (data URLs in JSON): size from the real decoded length (≤ 20 MB; declared size may not understate it), active types refused (HTML, XHTML, JS, XML), content sniffed against the declared type, SVG with scripts / event handlers / external references refused; files are only downloaded (`<a download>`), never rendered on the app origin | `domain/file_safety.py`, `dispatch.py` |
| V13.2 | Optimistic concurrency (If-Match → 409 `STALE`), optional Idempotency-Key replay (24 h, stored in `idempotency_keys`), 429 with `Retry-After`, `RateLimit-*` and the standard error envelope | `runtime.py`, `ctx.py`, `main.py` |
| V14.4 | Security headers: CSP `default-src 'self'; script-src 'self'` (no inline or eval — verified in the browser by `tests/e2e/twofactor.py`), `connect-src 'self' data: blob:`, `object-src 'none'`, `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'`; only when Razorpay is configured: `checkout.razorpay.com` (script, frame), `api.razorpay.com` (frame, connect), `lumberjack.razorpay.com` (connect) and Permissions-Policy `payment=(self "https://checkout.razorpay.com" "https://api.razorpay.com")`; X-Content-Type-Options, Referrer-Policy strict-origin-when-cross-origin, COOP/CORP same-origin, Permissions-Policy (camera, microphone, geolocation, payment, usb off) | `main.py` (`HELMET`, `CSP`, `CSP_PAYMENTS`, `PERMISSIONS_POLICY`) |
| V14.1, V14.2 | Config validated at start-up; production (`APP_ENV=production`, or `NODE_ENV=production`) refuses a missing/placeholder `MFA_ENCRYPTION_KEY`, a missing/default/empty database password, a non-https or localhost `APP_URL`, missing or non-https `CORS_ORIGINS`, missing SMTP (unless `EMAIL_DELIVERY=disabled` explicitly), Razorpay keys without a webhook secret, a short `METRICS_TOKEN`, and `CHANGE_ME` placeholders; the removed `DEMO_MODE` / `SEED_DEMO` / `SEED_JSON` / `DATABASE_APP_URL` are refused everywhere; the database role is checked (not superuser, no BYPASSRLS, not the table owner) and pending migrations refuse the start; docker compose refuses to start without the secrets; dependency audit (§8) | `config.py`, `startup.py`, `deploy/docker-compose.yml` |

## 3. Authentication and sessions

- **Sign-in** `POST /auth/login {email, password}` → `{token, user, tenant}`; with two-step verification on →
  `{mfa_required: true, mfa_token, expires_in: 300}` and **no session**. Unknown e-mail and wrong password give the same
  401 `LOGIN_FAILED` after the same scrypt work. Lockout: the tenant's rule (default 5 / 15 min) → 429 `LOGIN_LOCKED`.
- **Session token**: 32 random bytes (base64url), sent as `Authorization: Bearer …`. The database stores
  `sha256(token)` only, with `created_at`, `last_seen_at`, `expires_at` (30 days), IP and user agent. A session ends on
  logout, idle 12 h, absolute 30 days, password change (others), 2FA enabled (others), admin password / 2FA reset,
  deactivation, and revocation from Settings → Security.
- **No session fixation**: every successful sign-in (password, second step, sign-up, invite acceptance) issues a new
  token; a token is never accepted from a URL or re-used across users. **Privilege changes need no rotation**: the
  token carries no privileges — role, permissions, View-as and tenant status are rebuilt from the database on every
  request, so a role change or deactivation takes effect on the next call (proven across two API instances by
  `devtools-ts/e2e/multi.mjs`).
- **Bearer, not cookies**: nothing is sent automatically by the browser, so there is no CSRF surface; CORS is
  same-origin by default (`CORS_ORIGINS` = exact origins), `Access-Control-Allow-Credentials` is never sent, and the web
  app fetches with `credentials: 'same-origin'`. The token lives in `localStorage` (see residual risks).
- **Invites**: 24 random bytes (`inv_…`), single use, expire (7 days), looked up across tenants by exact match;
  accepting sets a password under the policy and signs in with a fresh token. Invite links are shown to user admins
  (they share them), so the token is stored as issued.

## 4. Two-step verification

- **Algorithm**: RFC 6238 TOTP, HMAC-SHA-1, 30-second step, 6 digits, ±1 step accepted, 160-bit base32 secret,
  `otpauth://totp/SeereonOS:<email>?secret=…&issuer=SeereonOS&algorithm=SHA1&digits=6&period=30` for the authenticator
  app. `domain/totp.py` is the port of `frontend/src/domain/totp.ts` (golden-tested against it, which in turn is verified against
  the RFC 6238 appendix B vectors). A code is accepted **once** (the last used time step is stored).
- **Enable**: `POST /me/2fa/setup` → secret (sealed, 30 min, 5 wrong codes then start again) →
  `POST /me/2fa/verify {code}` with a correct current code → `{user, recovery_codes}`: 10 single-use codes
  (`xxxxx-xxxxx`, ~50 bits each), shown once (copy / download), stored as salted SHA-256. Other sessions are signed out.
- **Sign-in**: `POST /auth/login/mfa {mfa_token, code | recovery_code}`. The challenge is 256-bit random with the
  tenant id as a routing prefix, stored as SHA-256, valid 5 minutes, single-use, burnt after 5 wrong codes; every wrong
  code also counts toward the account lockout; rate limited per IP. A recovery-code sign-in returns
  `recovery_codes_left` and the app warns.
- **Manage**: `GET /me/2fa`; `POST /me/2fa/recovery-codes {code}` (new set, old ones die);
  `POST /me/2fa/disable {password} | {code}` (5 wrong codes in a row → password only); the workspace rule
  "require two-step for admins" blocks disabling for access admins. Lost phone and codes:
  `POST /users/:id/2fa/reset` by someone with `users.manage` (never for yourself; ends every session; audited).
- **Storage**: `pending_tokens` rows (`two_factor`, `two_factor_setup`, `mfa_login`), tenant-scoped under RLS; never
  returned by any route and excluded from the tenant export. Secrets: AES-256-GCM, 96-bit random IV, auth tag, AAD,
  key id in the value (`v1.<kid>:…`) for rotation. The development demo fixture has two-step off for every account.

## 5. Rate limits

| Limit | Key | Default | Store |
|---|---|---|---|
| Auth endpoints: sign-in, second step, sign-up, reset request / info / set, invite info / accept | IP + e-mail (or challenge) | `RATE_LIMIT_AUTH` = 10 / min | PostgreSQL `rate_limits` (shared) |
| Same endpoints, per IP | IP | 10 × `RATE_LIMIT_AUTH` / min | PostgreSQL `rate_limits` (shared) |
| Account lockout | e-mail | tenant rule (5 / 15 min) | `login_attempts` (shared) |
| Second-step attempts | challenge | 5, then burnt | `pending_tokens` (shared) |
| Global | session token (IP when signed out) | `RATE_LIMIT_GLOBAL` = 600 / min | in process (per gunicorn worker) |

The shared counters are fixed 60-second windows keyed by `sha256(limit, key)` (no e-mails or IPs at rest), counted with
one atomic upsert per hit using the database clock (`INSERT … ON CONFLICT DO UPDATE … RETURNING`, `security/store.py`),
so N processes and N instances enforce one limit (`tests/integration/test_http.py::test_auth_rate_limit_is_shared_across_processes`);
expired rows are deleted by the worker's `cleanup` job (`seereon_cleanup()`). Over the limit: **429** with
`Retry-After`, `RateLimit-*` headers and `{error: {code: 'RATE_LIMITED', message: 'Too many requests. Try again in N
seconds.', request_id}}`; the web app shows that message. The global limit stays in memory on purpose: it is a coarse
DoS guard on authenticated traffic, not a security boundary — with W workers × N instances it allows up to W × N × the
value; put a limit at the load balancer if you need a hard ceiling. If the database is unreachable the auth limiter
fails open (the auth routes need the same database and fail anyway). Behind a proxy set `TRUST_PROXY` to the number
of proxies that append to `X-Forwarded-For` (`true` = 1; default `false`): the client IP is then taken that many hops
from the right, so a client-supplied left-most entry is never trusted; with `false` the header is ignored.

## 6. Logging and audit

- **Request logs** (JSON lines, pino-compatible levels: 30 info, 40 warn, 50 error): request id (echoed as
  `x-request-id`), method, URL with reset/invite/`token=` values redacted, status, latency. Redacted: `Authorization`,
  cookies, `Idempotency-Key`, passwords, tokens, data URLs, temporary passwords. 5xx are logged with the stack server-side
  only (`logging_setup.py`).
- **Authentication audit trail** — table `security_events` (append-only for the runtime role through
  `seereon_log_security_event`; it cannot read, change or delete rows; retention
  `SECURITY_EVENTS_RETENTION_DAYS` = 400): `login.success` (method password / totp / recovery_code), `login.failure`,
  `login.locked`, `login.mfa_required`, `mfa.failure`, `mfa.challenge_burned`, `mfa.enabled` / `mfa.disabled` /
  `mfa.recovery_codes_regenerated` (+ failures), `password.changed` / `password.change_failure`,
  `password.reset_requested` / `password.reset`, `logout`, `session.revoked` / `session.revoke_others`, `signup`,
  `invite.accepted`, `admin.password_reset`, `admin.mfa_reset`, `admin.user_deactivate`, `ratelimit.blocked`
  (`security/audit.py`). Each row: time, outcome, tenant, user, e-mail as typed, IP, user agent, request id, small detail
  (error code, method). Never passwords, codes or tokens. Business changes keep going to `activity_log` / `admin_audit`.
- Suggested alerts: `login.failure` or `ratelimit.blocked` spikes per IP, `mfa.challenge_burned`, `admin.mfa_reset`,
  any 5xx burst (`"level":50` log lines).

## 7. Data protection

- **Tenant isolation, three layers**: (1) every lookup is tenant-scoped (`rows()`, `find()` → 404 for another
  tenant's id, `ctx.py`); (2) the unit of work refuses to flush a row whose `tenant_id` differs from the request's
  tenant (`db/uow.py`, "Cross-tenant row"); (3) PostgreSQL RLS on every tenant table
  (`tenant_id = current_setting('app.tenant_id')`): reads run in a read-only snapshot and the flush in one transaction,
  both as `seereon_app` (NOBYPASSRLS) with `set_config('app.tenant_id', …, true)` (`db/backend.py`). RLS is the
  safety net for application bugs: a missed filter yields no rows instead of another tenant's rows.
- **One database role (v8).** The API, the worker and the production CLI have exactly ONE credential, `DATABASE_URL`
  for `seereon_app`; the owner credential exists only in the migrate job. `seereon_app` has **no privileges** on
  `user_credentials`, `sessions`, `reset_tokens`, `login_attempts`, `rate_limits`, `security_events`,
  `idempotency_keys` and `email_deliveries`; it reaches them only through `SECURITY DEFINER` functions owned by
  `seereon_owner` (`database/migrations/V008__single_role_runtime.sql`; catalogue in database/docs/README.md "Roles and
  the security boundary"). Each function does one narrow thing: resolve ONE session by its SHA-256 hash, check ONE
  user's password hash, set the password of a user **of the transaction's tenant**, append an audit event, increment
  a rate-limit bucket, claim e-mails for one worker id… It pins `search_path = pg_catalog, public`, uses no dynamic SQL,
  validates formats (scrypt hash, hex SHA-256), and EXECUTE is revoked from PUBLIC (also by default privileges in
  `bootstrap.sql`) and granted to `seereon_app` only. Why: until v7 the API also held the owner credential (an owner
  pool bypasses RLS); any injection or logic bug on that path could read every tenant and every password hash. Now
  even arbitrary SQL as `seereon_app` cannot list password hashes, sessions or tokens, nor read or erase the audit
  trail. The API and worker refuse to start in production if their role is a superuser, has BYPASSRLS or owns the
  tables (`startup.py`); `tests/integration/test_single_role.py` proves the privilege split.
- **Inside a tenant**: salaries / CTC only for `salaries`-capable roles (payroll, people, estimates redact them);
  margins, costs, contract value and the saved project costing (price, markup) only for finance / margins / project
  editors; employees see their own records plus what is shared by design (directory, company documents, their tasks
  and projects); the swept IDOR test below proves the rest is 403/404.
- **E-mail** (`workers/email.py`, `app/worker.py`): the API only queues; the worker delivers over SMTP with STARTTLS
  (credentials are never sent over a clear connection, except to localhost) or SMTPS. Password-reset and invitation
  messages carry live links, so their bodies are sealed with AES-256-GCM (`MFA_ENCRYPTION_KEY`) in the queue, expire
  (a reset link is not sent after it lapsed), and are erased once sent, expired or dead-lettered. Notification e-mails
  are in the tenant's `email_outbox` (visible in the app as before) with an honest status: `queued` → `sent` / `failed`.
- **Payments** (`payments/`, `routes/billing.py`): amounts are computed server-side from the billing invoice and the
  Razorpay order is created server-side; the browser's success callback is trusted only after the HMAC-SHA256 checkout
  signature (`order_id|payment_id`, key secret) verifies, and webhooks only after the HMAC-SHA256 of the raw body
  (webhook secret) verifies, both compared in constant time; a paid order is applied exactly once (verify and webhook
  may both arrive; webhook events are stored once per event id); no card data ever reaches the API, and stored webhook
  payloads are reduced to ids, amounts and status. Fixed-vector tests: `tests/unit/test_payments_signatures.py`.
- **At rest**: passwords scrypt; session, reset, challenge tokens and recovery codes SHA-256; TOTP secrets and queued
  reset/invite e-mails AES-256-GCM. Files are stored as data URLs in the tenant's rows (path to object storage with
  signed URLs: database/docs/README.md "File storage"). Backups contain everything above in that form: `backup.sh`
  encrypts every dump (age or gpg; the private key is not on the database or backup host), and the MFA key is not in
  the backup.
- **Metrics and errors**: `/metrics` is off unless `METRICS_TOKEN` is set, then needs it as a bearer token (constant
  time compare) and exposes only counters, latencies by route template, pool, queue and job state — no tenant ids,
  e-mails or paths with ids. Sentry (optional) receives scrubbed events: no request bodies, headers with credentials,
  cookies, query strings with tokens or user e-mails.

## 8. Deployment checklist

1. **TLS** at the load balancer / ingress (TLS 1.2+), HTTP → HTTPS redirect; `APP_ENV=production` (enables HSTS and
   the production refusals); `TRUST_PROXY=<number of proxies>` only behind proxies that clients cannot bypass;
   `APP_URL` = the public https URL of the web app; `CORS_ORIGINS` = its exact origin.
2. **Secrets** from a secret manager / orchestrator, never the repository: `DATABASE_URL` (runtime role, real
   password), `MIGRATION_DATABASE_URL` (migrate job only), `MFA_ENCRYPTION_KEY` (`openssl rand -base64 48`; rotate via
   `MFA_ENCRYPTION_KEY_PREVIOUS`), `SMTP_URL`, `RAZORPAY_KEY_SECRET` + `RAZORPAY_WEBHOOK_SECRET`, `METRICS_TOKEN`,
   `SENTRY_DSN`. `.env` / `.env.local` are git-ignored; `backend/.env.example` holds placeholders only (production
   refuses any `CHANGE_ME`). Losing `MFA_ENCRYPTION_KEY` disables every second factor (admins can reset them per user)
   and makes queued reset/invite e-mails undeliverable (users request new ones).
3. **Database roles**: `database/bootstrap.sql` with passwords passed as psql variables (`-v owner_password=… -v
   app_password=…`; no defaults exist); the running system uses only `seereon_app`; no application access as
   `postgres`. Restrict network access to the database to the API / worker hosts; TLS to the database outside a
   private network.
4. **No demo data**: production has no demo mode, no demo accounts and no seed; the fixture loader refuses
   `APP_ENV=production` and is not in the image. First accounts: `python -m app.cli create-superadmin` / `create-tenant`.
5. **Backups**: `database/scripts/backup.sh` daily (encrypted), off-host copies, the weekly scripted restore test
   (`restore_test.sh`), PITR if 24 h RPO is too much — database/docs/README.md.
6. **Monitoring**: ship the JSON logs; `/readyz`, `/metrics` (worker heartbeat, e-mail dead letters, job failures, 5xx);
   alert on the audit events above.
7. `/api/docs` (Swagger UI, assets vendored in `app/static/swagger-ui`) and `/api/v1/openapi.json` are OFF in
   production by default (`API_DOCS_ENABLED=false`; both answer 404). If you turn them on, restrict them to internal
   networks at the proxy.
8. In CI before each release: `pip-audit -r backend/requirements.txt`, `npm audit --omit=dev` (frontend), the Python
   test suite (`TZ=Asia/Kolkata python -m pytest`), and the e2e security checks `backend/devtools-ts/e2e/idor.mjs`,
   `backend/tests/e2e/twofactor.py`, `backend/devtools-ts/e2e/multi.mjs` against `backend/tests/e2e/stack.sh`.

## 9. Residual risks and follow-ups

| Risk | Rating | Note / recommendation |
|---|---|---|
| **Workspace "Sign out after idle" setting is not enforced** | Medium | Settings → Company shows `security.session_idle_minutes` (default 60), but the server applies the fixed `SESSION_IDLE_HOURS` (12 h) and the web app has no idle timer. Fix in `PgBackend.resolve_session` (`db/backend.py`): join `tenants` and use `least(make_interval(hours => idle), make_interval(mins => coalesce((t.settings->>'security.session_idle_minutes')::int, 720)))`. |
| Token in `localStorage` | Medium | Any XSS could read it. Mitigated by a strict CSP (no inline/eval scripts, self only), React escaping, upload sanitising, server-rendered escaped HTML. Moving to an HttpOnly, SameSite=Strict cookie would need CSRF protection and a frontend change. |
| "Require two-step for admins" is advisory | Medium | The rule blocks admins from turning 2FA off and warns, but an admin without 2FA can still sign in. Enforce at sign-in (force the setup step) when the policy should be mandatory. |
| No WebAuthn / passkeys, no QR code | Low | TOTP with key entry and an `otpauth://` link; add a QR renderer and WebAuthn later. |
| Global request limit per worker process | Low | Documented above; use the load balancer for a hard ceiling. |
| Platform scope is a session setting | Medium (defence in depth) | `app.platform_admin` is set by the runtime only for verified super-admin requests, but `seereon_app` can set it itself. It guards against application bugs, not against an attacker who already executes arbitrary SQL as `seereon_app`: such an attacker could call the credential functions in platform scope (e.g. replace one user's password hash, revoke sessions) — still never read hashes in bulk, sessions or tokens, or alter the audit trail. A stronger split needs a second, separately-credentialled role for the platform paths. |
| `EMAIL_DELIVERY=disabled` in production | Operational | Allowed when explicit (logged on every start): users then cannot reset passwords or receive invitations by e-mail; admins must use the in-app reset / copy-link flows. |
| Sign-up reveals that an e-mail is registered (409) | Low | Inherent in self-service sign-up; rate limited per IP and e-mail. |
| Files in database rows as data URLs | Low | Validated and download-only; no antivirus scan. Move to object storage with signed URLs and AV scanning for scale (database/docs/README.md "File storage"). |
| Milestone billing amounts visible to project members | Low | Revenue figures on milestones are shown to the project team by the current product rules; review with the business. |
| Invite tokens stored as issued | Low | Needed for "copy invite link"; tokens are random (192 bits), single-use and expire. |
| Frontend dependencies (`jspdf` < 4.2.1 + `dompurify`, `exceljs` → `uuid`, `react-router` 6.30.6) | Low in this app | Browser-only PDF/Excel export of the app's own data; nothing is bundled into the API. Upgrades are breaking majors — schedule with an export regression run. |

## 10. Verification

- `TZ=Asia/Kolkata python -m pytest` (its own PostgreSQL database `seereonos_test`, API on the runtime role only): `tests/integration/test_auth.py`
  (login, lockout and its reset, MFA with RFC 6238 + replay + recovery codes, challenge burn after 5 wrong codes, reset
  flow revoking sessions, invites, sign-up provisioning, idle / absolute expiry, password change revoking other sessions,
  logout, deactivation, security events recorded without secrets), `tests/integration/test_http.py` (headers / CSP /
  envelopes vs recorded Node responses, request ids, body limits, file safety, idempotency replay, ETag / If-Match,
  View-as and read-only tenants, forced password change, cross-tenant 404 and super-admin 403, CORS allowlist, global
  limit, auth limit shared by two processes, OpenAPI / docs), `tests/unit/test_config.py` (production refusals),
  `tests/golden/test_secret_box.py` (AES-GCM byte-compatible with the Node-era values, tamper, rotation),
  `tests/golden/test_domain.py` (TOTP, password policy and file safety vs the TypeScript),
  `tests/integration/test_single_role.py` (the runtime role cannot read or write credentials, sessions, tokens or the
  audit trail; functions refuse users of another tenant; RLS), `tests/integration/test_credentials_journey.py` (fresh
  empty database, production configuration: CLI super admin → tenant → invite e-mail → accept → reset e-mail through a
  real SMTP sink → sign in), `tests/integration/test_email_worker.py` + `test_worker.py` (claims, leases, fencing, back-off, dead letter, job locks),
  `tests/integration/test_payments.py` + `tests/unit/test_payments_signatures.py` (forged signatures refused, replayed
  webhooks applied once), `tests/integration/test_ops.py` (metrics token, no-store / Vary headers, no secrets in logs,
  Sentry scrubbing).
- `backend/devtools-ts/e2e/idor.mjs` (over HTTP + PostgreSQL, driven by the OpenAPI document): every id route × every method
  as another tenant's admin (must be 403/404, tenant data byte-identical afterwards), every GET/DELETE id route as an
  employee on other people's records, and every PATCH/PUT with protected fields (no row takes them, nothing moves
  tenant).
- `backend/tests/e2e/twofactor.py` (Playwright, built frontend): enable → recovery codes → sign out → sign in with code →
  recovery code (once) → turn off; no page errors, no CSP violations, layout audit of the sign-in steps and the
  security page at 360 / 768 / 1280.
- `backend/devtools-ts/e2e/multi.mjs`: two API instances on one database — sessions, cache coherence, If-Match, parallel
  writes, out-of-band SQL, 2FA setup / challenge on different instances, secrets sealed at rest, expiry, revocation,
  lockout surviving a restart.
