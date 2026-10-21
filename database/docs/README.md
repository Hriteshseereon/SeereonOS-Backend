# SeereonOS database (PostgreSQL 16)

This directory is the database deliverable: the schema (migrations), the cluster bootstrap, operations scripts
(backup, restore, scripted restore test), documentation (this file, `ERD.md`) and development-only fixtures.

| path | purpose |
|---|---|
| `bootstrap.sql` | idempotent, once per environment, as a superuser: the ONE database, the roles `seereon_owner` / `seereon_app`, their grants. Passwords are psql variables, never defaults |
| `migrations/V001__platform.sql` | hand-written: `schema_migrations`, `tenant_table_versions`, metadata, `user_credentials`, `sessions`, `login_attempts`, `reset_tokens`, `idempotency_keys`, `email_deliveries` |
| `migrations/V002__tables.sql` | the 79 domain tables (generated in the Node phase) + `app_column_meta` / `app_table_meta` rows |
| `migrations/V003__constraints_indexes.sql` | PKs, tenant-safe composite FKs, business keys, indexes (generated) |
| `migrations/V004__rls_triggers.sql` | RLS policies, cache-version triggers, grants to `seereon_app` (generated) |
| `migrations/V005__pending_tokens.sql` | `pending_tokens` (state between two requests: 2FA setup, WBS import preview) |
| `migrations/V006__security.sql` | `pending_tokens` kinds `two_factor` / `mfa_login`, `rate_limits`, `security_events` |
| `migrations/V007__table_changes.sql` | `table_changes`, the flush change log for incremental cache refresh |
| `migrations/V008__single_role_runtime.sql` | v8 production hardening: one runtime role + `SECURITY DEFINER` functions, the e-mail delivery queue, `worker_heartbeats` / `job_runs`, `payment_orders` / `payment_events`, `seereon_cleanup()` |
| `migrations/V009__tenant_products.sql` | `tenants.products`: the per-company product master (was a fixed vendor catalogue for every company) |
| `scripts/backup.sh` | encrypted `pg_dump -Fc` (age or gpg) with retention |
| `scripts/restore.sh` | restore a backup into an EMPTY database prepared by `bootstrap.sql` (one transaction, checksum verified) |
| `scripts/restore_test.sh` | scripted restore test: restore into a throwaway database, verify, drop (run weekly) |
| `fixtures/` | **development / test only**: the demo data set (`fixtures/README.md`). Never loaded in production |
| `docs/ERD.md` | Mermaid ERDs: core entities; platform, delivery, payments and jobs |

The runtime pieces live in the backend: `backend/app/db/migrate.py` (runner), `backend/app/db/mapping.py` (row
mapping), `backend/app/db/backend.py` (unit-of-work flush, the `SECURITY DEFINER` calls), `backend/app/devtools/fixtures.py`
(development fixture loader).

The schema stores exactly the rows that the shared domain code (the TypeScript spec in `frontend/src/mock`, ported to
`backend/app`) works with: one table per key of `interface DB` in `frontend/src/mock/server.ts`, columns = that row
type's fields. V002–V004 were derived mechanically during the Node phase (TypeScript compiler API over the row types,
checked against the mock seed and 29 snapshots of the mock test suites); the generators (`server/db/tools/*.ts`) are in
the git history (commit `2fe2415`). Fidelity is proven by tests: every fixture row goes into PostgreSQL and comes back
key-for-key identical (`backend/tests/integration/test_uow.py`), and the parity harness compares the Python API with
the in-process TypeScript spec over HTTP. Schema changes are now hand-reviewed migrations (policy below).

## Setting up a database (production)

One PostgreSQL 16 cluster, **one database**, **two roles** (roles, not databases). The API, the worker and the
production CLI connect with ONE connection string as the runtime role; only the migrate job uses the owner role.

Roles are **cluster-wide**: `bootstrap.sql` run for *any* database name on a cluster resets the passwords of
`seereon_owner` and `seereon_app` for every database on it. Give each environment its own cluster (production,
staging, development/test) and never run the development/test bootstrap below against the production cluster.

```sh
# 1. once per environment, as a superuser (managed PostgreSQL: the admin user). Passwords come from your secret store
#    as psql variables — they are REQUIRED (no defaults) and never written to a file:
psql -v ON_ERROR_STOP=1 "postgres://postgres@db.internal:5432/postgres" \
     -v dbname=seereonos \
     -v owner_password="$SEEREON_OWNER_PASSWORD" \
     -v app_password="$SEEREON_APP_PASSWORD" \
     -f database/bootstrap.sql
# (re-running it is safe; it also RESETS both passwords to the values given — that is how you rotate them)

# 2. on every release, BEFORE the new API/worker start (backend/deploy/docker-compose.yml runs it as the `migrate` job):
MIGRATION_DATABASE_URL="postgres://seereon_owner:$SEEREON_OWNER_PASSWORD@db.internal:5432/seereonos" \
  python -m app.db.migrate            # apply pending migrations (checksummed, advisory-locked) — safe to re-run
python -m app.db.migrate --status     # applied / pending; exit 1 when something is pending

# 3. the running system (API, worker, CLI): ONE variable
DATABASE_URL="postgres://seereon_app:$SEEREON_APP_PASSWORD@db.internal:5432/seereonos"

# 4. first accounts on an empty database (no demo data in production, ever):
python -m app.cli create-superadmin --email ops@example.com --name "Ops" --password-stdin
python -m app.cli create-tenant --company "Acme" --admin-email admin@acme.example --admin-name "A. Admin" --password-stdin
```

`MIGRATION_DATABASE_URL` is read only by `python -m app.db.migrate` (and the development fixture loader); without it
the runner falls back to `DATABASE_URL`. `MIGRATIONS_DIR` points the runner at the migrations: default
`database/migrations` in a checkout, `/app/migrations` in the Docker image. The API and the worker refuse to start in
production when the runtime role is a superuser, has `BYPASSRLS` or owns the tables, or when a migration is pending
(`backend/app/startup.py`).

Use TLS to the database outside a private network (`?sslmode=verify-full&sslrootcert=…` in both URLs). PgBouncer in
transaction mode works: tenant and platform scope are `SET LOCAL` / `set_config(…, true)` per transaction.

**Migration policy.** Files are `V<n>__<name>.sql`, applied in order, each in its own transaction, recorded with its
sha256 in `schema_migrations`; an advisory lock serialises concurrent runners; a changed applied file makes the runner
refuse. Never edit an applied migration: add a new file — the next one is `V009__…`. A schema change that touches a
domain table updates its `app_column_meta` / `app_table_meta` rows in the same file and keeps the conventions below
(RLS policies, the three version triggers, grants). Every new function is `SECURITY INVOKER` unless it must cross the
privilege boundary; a `SECURITY DEFINER` one must `SET search_path = pg_catalog, public`, use bound parameters only,
and have EXECUTE revoked from PUBLIC and granted to `seereon_app` explicitly (bootstrap.sql already revokes the default).

### Development and tests

```sh
# local roles + database (simple local passwords are fine here)
sudo -u postgres psql -v ON_ERROR_STOP=1 -v dbname=seereonos -v owner_password=seereon_owner -v app_password=seereon_app -f database/bootstrap.sql
cd backend
export MIGRATION_DATABASE_URL=postgres://seereon_owner:seereon_owner@localhost:5432/seereonos
python -m app.db.migrate
python -m app.devtools.fixtures load-demo     # DEVELOPMENT demo data (refused when APP_ENV=production) — fixtures/README.md
python -m app.db.reset                        # development only: DROP SCHEMA public → migrate → empty schema
TZ=Asia/Kolkata python -m pytest              # the suite creates and uses its OWN database (seereonos_test)
```

The defaults of the development configuration are `DATABASE_URL=postgres://seereon_app:seereon_app@localhost:5432/seereonos`
(runtime role) — `backend/.env.local.example` lists them. The test suite never touches `seereonos`: it creates
`seereonos_test` (`TEST_DATABASE_URL` / `TEST_MIGRATION_DATABASE_URL` override the URLs).

## Roles and the security boundary

| role | can | cannot |
|---|---|---|
| `seereon_owner` | owns the schema, every table and function; applies migrations; loads development fixtures | superuser, `BYPASSRLS`, create roles/databases. Never used by the running system |
| `seereon_app` | the ONLY runtime role. `SELECT/INSERT/UPDATE/DELETE` on the domain tables under RLS (incl. `payment_orders`, `payment_events`, `pending_tokens`); `SELECT` on `tenant_table_versions`, `app_column_meta`, `app_table_meta`, `schema_migrations`; `SELECT/INSERT` on `table_changes`; `worker_heartbeats` / `job_runs`; `EXECUTE` on the V008 functions | any direct access to `user_credentials`, `sessions`, `reset_tokens`, `login_attempts`, `rate_limits`, `security_events`, `idempotency_keys`, `email_deliveries`; DDL; `BYPASSRLS`. `statement_timeout = 30s`, `idle_in_transaction_session_timeout = 60s` |

**Why `SECURITY DEFINER` functions and not a second pool.** Until v7 the API held two connection strings, and the owner
pool (which bypasses RLS because it owns the tables) did the auth and platform work. A bug or an injection on that
path could read every tenant and every password hash. In v8 the running system has no owner credential at all. The
operations that must cross the boundary are narrow functions owned by `seereon_owner` (V008):

| group | functions | guard |
|---|---|---|
| sign-in / session | `seereon_resolve_session(hash, idle_hours)`, `seereon_user_by_email`, `seereon_login_attempts(emails)`, `seereon_record_login_attempt` | input is a sha256 token hash or an e-mail; returns one row |
| tenant-scoped credentials | `seereon_password_hashes(tenant, users)`, `seereon_set_password`, `seereon_delete_passwords`, `seereon_sessions`, `seereon_create_session`, `seereon_revoke_sessions`, `seereon_reset_tokens`, `seereon_create_reset_token`, `seereon_use_reset_tokens`, `seereon_delete_reset_tokens` | the user / tenant must be the tenant of the current transaction (`app.tenant_id`) or the platform scope must be active; hash formats are checked (scrypt, hex sha256) |
| lookups across tenants | `seereon_users_by_email`, `seereon_free_slug`, `seereon_invite_tenant(token)`, `seereon_reset_token_tenant(hash)`, `seereon_mfa_owner`, `seereon_payment_tenant(provider, order_id)` | return the minimum (a tenant id, a slug, a boolean-like row) needed to enter the right tenant |
| security plumbing | `seereon_rate_limit_hit`, `seereon_log_security_event`, `seereon_idempotency_get/put` | append/increment only: the audit trail cannot be read, changed or deleted by the runtime role |
| e-mail queue | `seereon_email_enqueue_outbox`, `seereon_email_enqueue_platform`, `seereon_email_claim`, `seereon_email_sent`, `seereon_email_failed`, `seereon_email_queue_stats` | claims are leased per worker id; a late worker cannot complete another worker's claim (fencing) |
| jobs | `seereon_job_tenants()`, `seereon_cleanup(retention_days, keep_days)` | fixed statements, no arguments that select data |

Every function has `SET search_path = pg_catalog, public`, uses bound parameters only (no dynamic SQL), and has EXECUTE
revoked from PUBLIC and granted to `seereon_app` alone. Residual risk (also in backend/SECURITY.md): the platform scope
is a session setting (`app.platform_admin`) that the runtime role can set itself, so it guards against application
bugs, not against an attacker who already runs arbitrary SQL as `seereon_app` — such an attacker could still call the
functions with the platform scope on (e.g. replace a password hash), but could never read hashes in bulk, read
sessions or tokens (only SHA-256 hashes are stored anyway), or alter the audit trail.

The database time zone is UTC.

## Row-level security

Every domain table has RLS enabled and two permissive policies, each with both `USING` and `WITH CHECK`:

- `tenant_isolation`: `tenant_id = current_setting('app.tenant_id', true)`. On `tenants` the column is `id`.
- `platform_admin`: `current_setting('app.platform_admin', true) = 'on'`, set by the runtime only for verified
  platform super-admin requests (`/admin/*`).

The runtime sets these with `set_config(…, true)` inside the transaction (PgBouncer transaction-pooling safe). With no
tenant set, queries return 0 rows; a row for another tenant cannot be inserted, and an UPDATE cannot move a row to
another tenant (tested). RLS is not `FORCE`d, so the owner role is unfiltered — only the migrate job and the
development fixture loader connect as the owner. RLS is the second line of defence: the application scopes every query
by tenant itself, and RLS turns a missed filter into "no rows" instead of a cross-tenant leak.
`tenant_table_versions` has no RLS because it holds counters only.

## Conventions

- Table and column names are the mock's snake_case names. Some are SQL keywords (`to`, `from`, `key`, `by`, `at`,
  `values`, `version`…), so **always quote identifiers** (`app.db.mapping.ident`).
- PK `id text`, an opaque string. `number_sequences` has PK `(tenant_id, scope)` and no `id`. Out-of-band inserts
  default to `gen_random_uuid()::text`; the app always supplies ids.
- `created_at`, `updated_at` and `version` are set by the app (`insert()`/`update()` in `frontend/src/mock/server.ts`), not by
  triggers. Their defaults only help out-of-band SQL.
- Column order: base columns (`id, tenant_id, created_at, updated_at, created_by, version`), then the declared
  fields in declaration order, then untyped fields. `row_from_tuple` returns keys in this order. JSON key order carries no
  meaning, and the tests compare key sets.
- Hidden columns on every mock table, never part of a JS row:
  - `_ord bigint GENERATED BY DEFAULT AS IDENTITY`: insertion order. The mock arrays are insertion-ordered and many
    handlers rely on that, so loads `ORDER BY _ord`. The seed inserts in array order. The app never writes it.
  - `_null_override text[]`: see null semantics. The app always writes what `values_to_pg` returns.
- CHECK constraints `<table>_<column>_chk` enforce every string-literal union of the row types (108).
- `NOT NULL` is set only where the field is required and non-nullable in TypeScript, and is never null or absent
  in any data set.

### Types

| TypeScript / data | PostgreSQL | JS value on read |
|---|---|---|
| `ISODate` / `*_on` / `date` fields, data always `YYYY-MM-DD` | `date` | the same `'YYYY-MM-DD'` string (the psycopg loader returns the raw text) |
| `ISODateTime` / `*_at` / `at`, data always UTC `…Z` | `timestamptz` | `toISOString()` (UTC, ms). Values stored without milliseconds (`…:00Z`, as the seed writes them) come back as `…:00.000Z`: same instant, same ordering. This is the only normalisation. |
| `Paise` / `*_paise` | `bigint` | number. Float noise below 1e-6 is rounded on write. A real fraction is rejected (`NON_INTEGER`) |
| counters, ordinals, versions, sizes (`no`, `round`, `seats`, `size_bytes`, `wf_step`, …) | `integer` | number |
| every other `number` (hours, pct, ratings, days, rates…) | `double precision` | number (exact round-trip: PG ≥12 prints the shortest exact digits) |
| `boolean` | `boolean` | boolean |
| `string[]` | `text[]` | string[] |
| objects, arrays of objects/numbers, `Record<…>` | `jsonb` | parsed JSON (array order kept; object key order not significant) |
| string-literal union | `text` + CHECK | string |
| any other `string` | `text` | string |

`values_to_pg` is strict on purpose, so drift between the shared code and the schema fails loudly in tests instead of
silently changing data:

- `UNKNOWN_COLUMN`: a field has no column.
- `BAD_VALUE`: the JS type is wrong for the column (e.g. `'4'` in a number column, `''` in a date column).
- `NON_INTEGER`: a fractional number was written to an integer column.

### Null semantics

TypeScript separates `x: T | null`, which is always present and may be null, from `x?: T`, where the key may be
absent. The data uses both, and often within one column: `tasks.approved_by` is absent on 1,083 rows and explicitly
`null` on 306 rows across the data sets. A single SQL NULL cannot hold that difference, so:

- `app_column_meta.null_as` gives the default meaning of NULL per column:
  - `'null'` returns `null`. Used for required fields and optional fields whose data is mostly explicit null.
  - `'absent'` omits the key. Used for optional fields, `x?: T`.
- `_null_override` lists the columns of that row where NULL means the opposite. `values_to_pg` fills it, and it is NULL
  when the list is empty, which is almost always the case.
- A JS key whose value is `undefined` counts as absent (JSON semantics).

Rows inserted by out-of-band SQL get sensible defaults. Rows the app writes round-trip exactly.

### Untyped fields

The shared code writes these fields through casts (`as never`, `Partial<…>` spreads, `wfPatch()`), so they are not
in the entity types. They are real columns, and the schema derivation found them from the `insert()`/`update()` object literals
using the type checker:

| table | fields |
|---|---|
| tasks | `wbs_code`, `wf_process`, `wf_chain`, `wf_step`, `wf_step_at`, `wf_approver_id` |
| timesheet_weeks, leave_requests | `wf_process`, `wf_chain`, `wf_step`, `wf_step_at` |
| expense_claims | `wf_*`, `submitted_at`, `first_approved_by` |
| purchase_orders | `wf_*`, `title`, `justification`, `over_budget`, `budget_remaining_paise` |
| vendor_bills | `wf_*`, `approver_id`, `approved_by`, `dispute_reason`, `category`, `description` |
| payroll_lines | `employee_code`, `unapproved_detail`, `would_deduct_paise`, `deduct_applied`, `sales`, `no_rate`, `pending_days`, `approver_name`, `self_approves`, `flags` |
| vendors | `bank_name`, `bank_account`, `ifsc` |
| clients | `city`, `state`, `state_code`, `pincode`, `pan`, `notes`, `status_manual` |
| contracts | `reminded_on`, `renewed_from` |
| ops_items | `history` (seen in data only; declared on the local `OpsItemX` type) |

Computed fields that are never stored (`users.permissions`, `users.role_name`) have no columns.

## Multi-instance state

The API is stateless between requests. Everything that has to outlive a request lives in PostgreSQL, so any instance
can serve the next call:

- sessions, login attempts (lockout), reset tokens, and idempotency keys are in the platform tables (V001);
- `pending_tokens` (V005) holds short-lived state that sits between two requests of one user. It is tenant-scoped,
  under RLS, and version-triggered like every mock table. Before V005 this state was kept in process `Map`s in `frontend/src/mock`:
  - `two_factor_setup`: the secret from `POST /me/2fa/setup`, kept until `/me/2fa/verify` (expires after 30 min);
  - `wbs_import`: the parsed preview from `POST /projects/:id/import`, kept until `?mode=commit` (expires after 15 min).
    The row id is the preview token, and the token is single-use.

  Expired rows are ignored, and the next write of the same kind deletes them. The tenant data export skips this table.
- B5 (V006) adds kinds to `pending_tokens`: `two_factor` (a user's active TOTP factor: AES-GCM-sealed secret, hashed
  recovery codes, last used time step; no expiry) and `mfa_login` (a sign-in waiting for its second step; id = SHA-256
  of the challenge token; 5 min; burnt after 5 wrong codes). Also `rate_limits` (fixed-window counters of the auth
  endpoints, shared by all instances; keys are SHA-256 hashes) and `security_events` (authentication audit trail).
  Both are platform tables (no RLS, no privileges for `seereon_app`: reached only through `seereon_rate_limit_hit` /
  `seereon_log_security_event`); see backend/SECURITY.md.
- What stays per process (by design): the tenant cache, which stays coherent through `tenant_table_versions`; the
  global per-token request limit (`RATE_LIMIT_GLOBAL`, in-memory per worker process: a coarse DoS guard, so behind
  N worker processes it is up to N × the configured value — the auth limits are exact because they are counted in
  `rate_limits`). Request memos (`scope().memo`) live only for one request attempt.

Proven by `backend/devtools-ts/e2e/multi.mjs`, which runs two API instances against one DB: a write on one instance is read on
the other, If-Match works across instances, parallel writes cause no lost updates, an out-of-band SQL write is seen,
2FA setup and verify work on different instances, WBS preview and commit work on different instances, logout, idle
and absolute expiry, a password change revokes the user's other sessions, and data and lockout survive a restart.

## Cache versions (contract with the API runtime)

`tenant_table_versions(tenant_id, table_name, version)` is maintained by three statement-level triggers on every mock
table (AFTER INSERT / UPDATE / DELETE, with transition tables). A statement that touches rows of a tenant adds
exactly **1** to that tenant's counter for the table. A statement that touches 0 rows adds nothing. One statement
touching two tenants bumps both. `tenants` is keyed by `tenants.id`. The trigger function is `SECURITY DEFINER`, so
`seereon_app` only needs `SELECT` on the table. Writes from outside the app (psql, jobs, other instances) also
invalidate every API cache.

ON DELETE CASCADE deletes children inside the parent's DELETE. That bumps the child table once, and a later
explicit DELETE of the same children touches 0 rows and does not bump.

`table_changes(tenant_id, table_name, version_before, version_after, upserted_ids, deleted_ids)` (V007) is written by
the API's flush, inside the flush transaction and under the tenant's advisory lock, for each changed table with an
`id` primary key: the version step the flush made and the ids it inserted/updated and deleted. A process holding an
older version applies the chain of steps (reading only those rows) when it is gapless and small (`DELTA_MAX_ROWS`),
and re-reads the table otherwise — out-of-band writes bump the version without a log row, so they always force the full
read. RLS like the tenant tables; `seereon_app` has `SELECT` and `INSERT` (its own flush); rows older than 10 minutes
are purged by the worker's `cleanup` job (`seereon_cleanup()`). The log is an optimisation and never a source of truth.

## Backups

A dump contains every tenant's data, password hashes (scrypt), sealed 2FA secrets and sealed queued e-mails, so backups
are **always encrypted** and the decryption key never lives on the database or backup host.

**What and how often.** `scripts/backup.sh` takes a logical backup with `pg_dump --format=custom` (one consistent
snapshot, compressed, restorable selectively or in parallel), encrypts it on the fly (nothing unencrypted touches the
disk), writes `<prefix>-<UTC timestamp>.dump.age` plus a `.sha256`, and deletes this prefix's files older than
`--keep-days` (default 35) — only after a successful backup. Run it daily. Logical dumps give an RPO of up to 24 h;
if that is too much, add continuous WAL archiving / point-in-time recovery (managed PostgreSQL PITR, pgBackRest or
WAL-G) and keep the daily dumps as the independent, portable copy.

```sh
# one-time: an age key pair. The PRIVATE key goes to your secret vault / offline (two people can reach it);
# only the PUBLIC key (age1…) is configured on the backup host.
age-keygen -o seereonos-backup.key        # prints "Public key: age1…"

# /etc/cron.d/seereonos-backup (owner-role URL from the secret store; MAILTO gets any failure)
MAILTO=ops@example.com
15 2 * * *  seereon  BACKUP_DATABASE_URL=… BACKUP_AGE_RECIPIENT=age1… /opt/seereonos/database/scripts/backup.sh --out /var/backups/seereonos --keep-days 35
# off-host copy (object storage with versioning / object lock, another account or region); monthly copies kept 12 months
45 2 * * *  seereon  rclone copy /var/backups/seereonos remote:seereonos-backups/daily --max-age 26h
50 2 1 * *  seereon  rclone copy /var/backups/seereonos remote:seereonos-backups/monthly --max-age 26h
# weekly scripted restore test of the newest backup (see below)
45 3 * * 0  seereon  ADMIN_DATABASE_URL=… RESTORE_OWNER_URL=… RESTORE_APP_URL=… BACKUP_AGE_IDENTITY=/run/secrets/backup.key \
                     /opt/seereonos/database/scripts/restore_test.sh --latest /var/backups/seereonos --report /var/lib/seereonos/restore-test.json
```

| variable | used by | meaning |
|---|---|---|
| `BACKUP_DATABASE_URL` | backup.sh | owner role (RLS is not forced on the owner, so it sees every row) or a superuser; default `MIGRATION_DATABASE_URL` |
| `BACKUP_AGE_RECIPIENT` | backup.sh | age public key(s), comma-separated — preferred |
| `BACKUP_GPG_RECIPIENT` | backup.sh | alternative: gpg key id whose public key is in the keyring |
| `BACKUP_ENCRYPTION=none` | backup.sh | explicit opt-out (only for an encrypted volume you fully control); prints a warning |
| `BACKUP_DIR`, `BACKUP_KEEP_DAYS` | backup.sh | defaults for `--out` (`/var/backups/seereonos`) and `--keep-days` (35) |
| `BACKUP_AGE_IDENTITY` | restore.sh, restore_test.sh | path of the age private key file (mounted only when restoring) |
| `ADMIN_DATABASE_URL` | restore_test.sh | may CREATE/DROP DATABASE … OWNER seereon_owner: a superuser, or an ops role `LOGIN CREATEDB` that is a member of `seereon_owner` |
| `RESTORE_OWNER_URL` / `RESTORE_APP_URL` | restore_test.sh | owner / runtime role on that cluster (the database name in the URL is replaced) |

The restore test needs the decryption key, so run it on a host that may hold it (or a staging cluster), not on the
web/API hosts. Plain `pg_dump` uses the owner's password from the URL; `~/.pgpass` (mode 600) or `PGPASSFILE` keep it
out of the crontab.

## Restore

```sh
# 1. a NEW, EMPTY database with the roles (same passwords as the secret store, or new ones you then roll out):
psql -v ON_ERROR_STOP=1 "postgres://postgres@db-new:5432/postgres" -v dbname=seereonos \
     -v owner_password="$SEEREON_OWNER_PASSWORD" -v app_password="$SEEREON_APP_PASSWORD" -f database/bootstrap.sql
# 2. restore (verifies the .sha256, decrypts into a private temp dir, ONE transaction; refuses a non-empty target):
BACKUP_AGE_IDENTITY=/path/to/seereonos-backup.key \
  database/scripts/restore.sh /var/backups/seereonos/seereonos-20261019T021500Z.dump.age \
  --target "postgres://seereon_owner:$SEEREON_OWNER_PASSWORD@db-new:5432/seereonos"      # --jobs 4 for a big database
# 3. bring the schema to the running release and check:
MIGRATION_DATABASE_URL=postgres://seereon_owner:…@db-new:5432/seereonos python -m app.db.migrate
DATABASE_URL=postgres://seereon_app:…@db-new:5432/seereonos python -m app.cli check
# 4. point DATABASE_URL of the API and the worker at it; restart them.
```

After a restore every API process starts with an empty cache, so stale data cannot be served. Sessions in the backup
are valid again (they were valid at backup time); to force everyone to sign in again run, as the owner,
`UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL;`. E-mails that were queued at backup time are sent
again by the worker unless you mark them: `UPDATE email_deliveries SET status = 'skipped' WHERE status IN ('pending', 'sending');`.
The backup also carries the worker heartbeats of that moment, so `/readyz` and `python -m app.cli check` report a
worker until those rows age out; clear them before you start the worker on the restored database:
`DELETE FROM worker_heartbeats;`.

**Scripted restore test.** `scripts/restore_test.sh` restores a backup into a throwaway database, checks it and drops
it: checksum and archive validity, the restore itself (one transaction), `schema_migrations` against
`database/migrations` (identical checksums; files newer than the backup are reported), every table of `app_table_meta`
present, RLS on every tenant table, the `SECURITY DEFINER` functions not executable by PUBLIC, `seereon_app` without
direct access to credentials and sessions, row counts, and — with `RESTORE_APP_URL` — RLS through the runtime role (0
rows without a tenant, the tenant's rows with it). `--from-url` takes a fresh backup itself and compares row counts
EXACTLY with the source, counted inside the very snapshot it dumped (safe on a live database). Exit code ≠ 0 and a
`--report` JSON for monitoring.

Recorded run (2026-10-19, PostgreSQL 16.13, development database with the demo fixture):

```
restore_test: ok   fresh encrypted backup of the source (snapshot 00000003-0000415E-1, 97 tables counted in it)
restore_test: ok   throwaway database seereonos_restore_test_20261019095412_13020
restore_test: ok   restore.sh in 2 s (1223375 bytes)
restore_test: ok   schema_migrations: 9 applied, checksums match the repository
restore_test: ok   every table of app_table_meta exists
restore_test: ok   row-level security enabled on every tenant table
restore_test: ok   34 SECURITY DEFINER functions, none executable by PUBLIC
restore_test: ok   seereon_app: tenant tables yes, credentials/sessions only through functions
restore_test: ok   row counts identical to the source snapshot: 97 tables, 7630 rows
restore_test: ok   runtime role: 0 rows without a tenant, 14/14 users of tn_seereon with it
RESTORE TEST PASSED in 4 s
```

## Monitoring

The API exposes `/readyz` (database, worker heartbeat) and, with `METRICS_TOKEN`, Prometheus `/metrics` (pool, e-mail
queue, job runs, worker heartbeat; backend/README.md). For the database itself, alert on these (run as the owner or a
`pg_monitor` member):

```sql
-- e-mail queue: backlog, oldest due message, dead letters (alert: dead > 0 new, oldest due > 15 min)
SELECT * FROM seereon_email_queue_stats();
SELECT status, kind, count(*), min(next_attempt_at) FROM email_deliveries GROUP BY 1, 2 ORDER BY 1, 2;
SELECT message_id, kind, attempts, last_error, updated_at FROM email_deliveries WHERE status = 'dead' ORDER BY updated_at DESC LIMIT 20;

-- worker liveness and scheduled jobs (alert: newest beat > 3 min; a job not finished for 2× its interval; failures rising)
SELECT worker_id, last_beat_at, now() - last_beat_at AS age FROM worker_heartbeats ORDER BY last_beat_at DESC;
SELECT job, last_status, last_finished_at, now() - last_finished_at AS since, runs, failures, left(last_error, 120)
FROM job_runs ORDER BY job;

-- connections by role and state (alert: near max_connections; many 'idle in transaction')
SELECT usename, state, count(*) FROM pg_stat_activity WHERE datname = current_database() GROUP BY 1, 2 ORDER BY 1, 2;

-- long-running statements and lock waits (the runtime role is capped at 30 s by statement_timeout)
SELECT pid, usename, now() - query_start AS running, wait_event_type, left(query, 120)
FROM pg_stat_activity WHERE state <> 'idle' AND now() - query_start > interval '10 seconds' ORDER BY running DESC;
SELECT pid, pg_blocking_pids(pid) AS blocked_by, left(query, 120) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;

-- size, biggest tables, vacuum health
SELECT pg_size_pretty(pg_database_size(current_database()));
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total, n_live_tup, n_dead_tup, last_autovacuum
FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 15;

-- cache hit ratio (expect > 0.99) and transaction ID wraparound headroom (alert: age > 1.5e9)
SELECT round(sum(blks_hit)::numeric / nullif(sum(blks_hit) + sum(blks_read), 0), 4) FROM pg_stat_database WHERE datname = current_database();
SELECT datname, age(datfrozenxid) FROM pg_database ORDER BY 2 DESC;

-- security: failed sign-ins and blocked attempts in the last hour (alert on spikes)
SELECT event, outcome, count(*) FROM security_events WHERE at > now() - interval '1 hour' GROUP BY 1, 2 ORDER BY 3 DESC;

-- schema: applied migrations (the API refuses to start with pending ones)
SELECT version, applied_at FROM schema_migrations ORDER BY version;
```

Also alert on the backup itself: the cron job's exit code, the age of the newest file in the backup directory
(> 26 h) and the `result` of the restore test report.

## File storage

Uploaded files (task attachments, documents, employee documents, logos, receipts) are stored **in PostgreSQL** as
`data:` URLs in text columns: `files.data_url` (documents, document versions, employee documents and expense receipts
reference `files` by `file_id`), `tasks.attachments[].data_url` and `tenants.logo_data_url` — at most 15 MB per upload request (`UPLOAD_LIMIT`, `backend/app/router.py`; some routes
5 MB). Content is checked server-side (`backend/app/domain/file_safety.py`). This keeps backups, tenant isolation (RLS),
the tenant data export and deletion in one place, and is fine for the expected volume (attachments of a services
company: a few GB per tenant at most). It costs database size, backup size and cache memory for the tables that hold
files.

**Path to object storage (S3-compatible), when the database grows past ~50 GB or files dominate it:** add a
`storage_key` column next to each `data_url` (new migration), write new uploads to a private bucket under
`<tenant_id>/<file id>` with server-side encryption and keep `data_url` NULL, serve downloads through the API (it
checks access, then streams or redirects to a short-lived pre-signed URL), migrate old rows in batches from the worker
(upload, verify the checksum, set `storage_key`, clear `data_url`), and add the bucket (versioning on) to the backup
plan — the database backup then no longer contains the files. The API code paths that read `data_url` are the upload,
download and export handlers; nothing else depends on where the bytes live.

## Platform and runtime tables (data dictionary)

The generated dictionary below covers the domain tables. These are the hand-written ones (V001, V005–V008):

#### user_credentials (V001) — no RLS, no privileges for `seereon_app`

| column | type | meaning |
|---|---|---|
| user_id | text PK | `users.id` (no FK: survives for audit) |
| password_hash | text NOT NULL | `scrypt$N$r$p$salt_b64$hash_b64` (N=32768, r=8, p=1); format checked by `seereon_set_password` |
| password_algo | text NOT NULL | `scrypt` |
| password_changed_at | timestamptz NOT NULL | last change |

#### sessions (V001) — no RLS, no privileges for `seereon_app`

| column | type | meaning |
|---|---|---|
| token_hash | text PK | hex SHA-256 of the opaque bearer token (the token itself is never stored) |
| user_id, tenant_id | text NOT NULL | owner of the session |
| created_at, last_seen_at | timestamptz NOT NULL | idle expiry is checked against `last_seen_at` (`SESSION_IDLE_HOURS`) |
| expires_at | timestamptz NOT NULL | absolute expiry (`SESSION_ABSOLUTE_DAYS`) |
| revoked_at | timestamptz | sign-out, password change, deactivation |
| ip, user_agent | text | at creation |

#### reset_tokens (V001), login_attempts (V001), idempotency_keys (V001) — no RLS, no privileges for `seereon_app`

`reset_tokens(token_hash PK, user_id, expires_at, used_at)`: password-reset and welcome links (hash only, single use).
`login_attempts(id, email, at, ip, succeeded)`: lockout counting. `idempotency_keys(tenant_id, key, method, path,
status, body, created_at)`: `Idempotency-Key` replays of POST for 24 h.

#### email_deliveries (V001, reshaped in V008) — the worker's delivery queue; no privileges for `seereon_app`

| column | type | meaning |
|---|---|---|
| message_id | text PK | `email_outbox.id` (kind `outbox`) or `pm_…` (kind `platform`) |
| tenant_id | text NOT NULL | tenant of the message |
| kind | text NOT NULL | `outbox` (notification e-mails; content read from `email_outbox`) or `platform` (reset links, invitations) |
| to_addr, subject | text | platform messages only |
| body | text | platform messages: AES-256-GCM sealed by the API with the `MFA_ENCRYPTION_KEY`; set to NULL once sent, expired or dead |
| status | text NOT NULL | `pending` → `sending` (claimed) → `sent`; `pending` again with back-off after a temporary failure; `dead` after `EMAIL_MAX_ATTEMPTS` or a permanent 5xx; `skipped` (no recipient); `expired` (platform message past `expires_at`) |
| attempts | integer NOT NULL | delivery attempts so far |
| last_error | text | last SMTP error (no message content) |
| created_at, updated_at | timestamptz NOT NULL | |
| next_attempt_at | timestamptz NOT NULL | due time; back-off `min(6 h, 60 s × 2^(attempts−1))` |
| locked_by, locked_until | text, timestamptz | claim lease of a worker (`FOR UPDATE SKIP LOCKED`); a crashed worker's claim is taken over after `locked_until`, and a late worker cannot complete a claim it no longer holds |
| sent_at | timestamptz | delivered |
| expires_at | timestamptz | platform messages are not sent after this (a stale reset link is useless) |

`email_outbox` (domain table, the in-app "Sent e-mails" list): v8 changed `status` to `queued` | `sent` | `failed` and
made `sent_at` nullable — a row is written `queued` with `sent_at` NULL and becomes `sent` (with `sent_at`) or `failed`
only when the worker has actually delivered it or given up (dead letter). With `EMAIL_DELIVERY=disabled` rows stay
`queued`.

#### rate_limits, security_events (V006) — no RLS, no privileges for `seereon_app`

`rate_limits(bucket, window_start, hits, expires_at)`: fixed-window counters of the auth endpoints shared by all
instances (bucket = SHA-256 of limit name + key). `security_events(id, at, event, outcome, tenant_id, user_id, email,
ip, user_agent, request_id, detail)`: authentication audit trail, append-only for the runtime role, kept
`SECURITY_EVENTS_RETENTION_DAYS` (default 400).

#### worker_heartbeats, job_runs (V008) — plain grants to `seereon_app` (not sensitive)

| table | columns | meaning |
|---|---|---|
| worker_heartbeats | worker_id PK (`host:pid:random`), hostname, pid, started_at, last_beat_at, info | one row per running `python -m app.worker`; `/readyz` warns when the newest beat is older than `WORKER_STALE_SECONDS`; removed on clean exit, purged after 1 day |
| job_runs | job PK, last_started_at, last_finished_at, last_status (`ok` / `error` / `running`), last_error, last_duration_ms, last_worker, runs, failures, last_result | last run of each scheduled job (`email`, `escalations`, `timesheet_locks`, `milestones`, `appraisal_cycles`, `exits`, `contract_renewals`, `cleanup`), written by the worker that holds the job's advisory lock |

#### payment_orders, payment_events (V008) — tenant tables (RLS, version triggers, metadata rows)

| column | type | meaning |
|---|---|---|
| payment_orders.provider | text NOT NULL | `razorpay` |
| payment_orders.provider_order_id | text, unique per provider | Razorpay `order_…`; NULL until created |
| payment_orders.provider_payment_id | text | Razorpay `pay_…` once paid |
| payment_orders.invoice_id | text NOT NULL | the `billing_invoices` row being paid — the amount always comes from it, never from the client |
| payment_orders.purpose | text NOT NULL | `plan` (plan change) or `seats` (seat increase) |
| payment_orders.plan_code, seats | text, integer NOT NULL | what is applied when paid |
| payment_orders.amount_paise, currency | bigint > 0, text `INR` | |
| payment_orders.status | text NOT NULL | `created` → `paid` (verified checkout signature or webhook; applied exactly once) or `failed` |
| payment_orders.paid_at, failure_reason | timestamptz, text | |
| payment_events.id | text PK | the provider's event id (`X-Razorpay-Event-Id`): a replayed webhook is stored once |
| payment_events.event | text NOT NULL | `payment.captured`, `order.paid`, `payment.failed`, … |
| payment_events.provider_order_id, provider_payment_id | text | |
| payment_events.payload | jsonb | the verified webhook body reduced to ids, amounts and status (no card or contact data) |
| payment_events.outcome | text NOT NULL | `applied`, `duplicate`, `ignored`, `failed` |

Plus the base columns of every domain table (`id`, `tenant_id`, `created_at`, `updated_at`, `created_by`, `version`,
hidden `_ord`, `_null_override`).

## Performance

The Node phase's `db:perf` tool built a synthetic tenant: 200 users, 50 projects, 500 milestones, 20k tasks, 31.2k timesheet
weeks, **149.8k timesheet entries** (3 years), **100k activity rows**, 50k notifications, 2k allocations, 1.5k
invoices and 10k sessions. Results on this machine:

| access path | plan | exec |
|---|---|---|
| tenant table load (timesheet_entries, 150k rows) | Index Scan `timesheet_entries_tenant_ord_ix` (no sort) | 65 ms in PG |
| timesheet by user + date range | Index Scan `(tenant_id, user_id, work_date)` | 0.10 ms |
| tasks by project | Bitmap Index Scan `(tenant_id, project_id)` | 1.0 ms |
| tasks by owner + status | Index Scan | 0.12 ms |
| notifications unread by user | Index Scan `(tenant_id, user_id, read_at)` | 0.01 ms |
| activity by entity | Index Scan `(tenant_id, entity, entity_id)` | 0.03 ms |
| invoices by project | Bitmap Index Scan | 0.10 ms |
| users by lower(email) | Index Scan `users_email_lower_uk` | 0.03 ms |
| sessions by token_hash | Index Scan `sessions_pkey` | 0.02 ms |
| allocations by user | Index Scan | 0.09 ms |
| cache versions of a tenant | Seq Scan (the table has a few hundred rows; the PK is used once it grows) | 0.02 ms |

Full tenant-table load as `seereon_app` under RLS, including row mapping (median of 5):

| table | rows | query (incl. driver parsing, Node phase) | mapping |
|---|---|---|---|
| timesheet_entries | 149,760 | 885 ms | 154 ms |
| activity_log | 100,000 | 653 ms | 87 ms |
| notifications | 50,000 | 273 ms | 33 ms |
| timesheet_weeks | 31,200 | 284 ms | 44 ms |
| tasks | 20,000 | 222 ms | 101 ms |

**Partitioning: not now.**

- Every hot path is an index scan in under 1 ms, and the full-tenant load reads the `(tenant_id, _ord)` index in
  order, about 65 ms of server time for 150k rows. At this size the time goes to moving and parsing rows in the
  API process, which partitioning cannot reduce.
- The biggest relation is about 50–90 MB including indexes.
- Partitioning would force the partition key into every PK and unique key, breaking the global `id` PK and the
  tenant-safe composite FKs. It would also add planning overhead to every query.

Revisit when one of these applies:

- One table passes roughly 50–100M rows.
- Retention needs to drop old data cheaply. The candidates are `activity_log` and `notifications`, range-partitioned
  by month on `at` / `created_at`, with the PK widened to `(id, at)`.

The measurements of the Python API (load tests, memory, capacity per instance, scaling playbook) are in
backend/PERFORMANCE.md; the paragraph below is the Node-phase analysis it addresses.

The real scaling limit is the architecture's full-table tenant cache. A tenant with about 150k timesheet entries
costs about 1 s to fill that cache once per table version, plus a 45 MB JSON-sized working set. For tenants of that
size, the runtime should push date-range filters down to SQL for `timesheet_entries`, `activity_log` and
`notifications`; the indexes above already support that.

<!-- GENERATED:start (server/db/tools/gen-docs.ts, Node phase) -->
### FK decisions

Composite `(tenant_id, column) → parent (tenant_id, id)`, `DEFERRABLE INITIALLY DEFERRED`. Every FK was validated against the seed and 29 harvested mock-API snapshots (`fkcheck.ts`, Node phase). In addition every tenant table except `users` (the platform admin has tenant `platform`) and `admin_audit` has `tenant_id → tenants(id)`, and `user_credentials` / `sessions` / `reset_tokens` reference `users(id)` ON DELETE CASCADE.

| child column | parent | ON DELETE | why |
|---|---|---|---|
| users.reports_to_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| users.department_id | departments | NO ACTION | master data is deactivated, never deleted |
| users.designation_id | designations | NO ACTION | master data is deactivated, never deleted |
| users.grade_id | grades | NO ACTION | master data is deactivated, never deleted |
| users.location_id | locations | NO ACTION | master data is deactivated, never deleted |
| users.shift_id | shifts | NO ACTION | master data is deactivated, never deleted |
| users.cost_centre_id | cost_centres | NO ACTION | master data is deactivated, never deleted |
| delegations.from_user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| delegations.to_user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| delegations.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| cost_rates.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| leave.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| daily_updates.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| leave_requests.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| appraisals.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| appraisals.cycle_id | appraisal_cycles | NO ACTION | appraisal cycles are never deleted |
| goals.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| goals.cycle_id | appraisal_cycles | NO ACTION | appraisal cycles are never deleted |
| feedback.to_user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| feedback.from_user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| interviews.candidate_id | candidates | NO ACTION | candidates are never deleted (stage rejected/withdrawn) |
| employee_documents.employee_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| issued_documents.employee_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| issued_documents.template_id | doc_templates | NO ACTION | docsV7.ts refuses to delete a template with issued documents (409 TEMPLATE_IN_USE) |
| projects.pm_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| projects.deal_id | deals | SET NULL (column) | sales.ts DELETE /sales/deals nulls projects.deal_id itself |
| project_status_history.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| milestones.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| tasks.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| tasks.milestone_id | milestones | NO ACTION | milestones are soft-deleted (deleted_at) |
| task_dependencies.task_id | tasks | CASCADE | DELETE /tasks removes the task’s dependencies in the same request |
| task_dependencies.depends_on_task_id | tasks | CASCADE | DELETE /tasks removes dependencies on the task in the same request |
| comments.task_id | tasks | CASCADE | DELETE /tasks removes the task’s comments in the same request |
| comments.parent_id | comments | CASCADE | comments are only deleted together with their task (whole thread) |
| notifications.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| timesheet_weeks.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| timesheet_entries.week_id | timesheet_weeks | NO ACTION | timesheet weeks are never deleted |
| timesheet_entries.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| timesheet_entries.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| allocations.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| allocations.milestone_id | milestones | NO ACTION | milestones are soft-deleted (deleted_at) |
| allocations.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| change_requests.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| change_requests.milestone_id | milestones | NO ACTION | milestones are soft-deleted (deleted_at) |
| openings.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| invoices.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| invoices.client_id | clients | NO ACTION | clients are never hard-deleted (status churned) |
| payments.invoice_id | invoices | NO ACTION | only draft invoices can be deleted (409 INV_LOCKED) and drafts take no payments |
| payroll_lines.run_id | payroll_runs | NO ACTION | payroll runs are never deleted |
| payroll_lines.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| ledger_entries.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| ledger_entries.milestone_id | milestones | NO ACTION | milestones are soft-deleted (deleted_at) |
| expense_claims.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| expense_claims.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| purchase_orders.vendor_id | vendors | NO ACTION | vendors are never deleted (status blocked) |
| purchase_orders.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| vendor_bills.vendor_id | vendors | NO ACTION | vendors are never deleted (status blocked) |
| vendor_bills.po_id | purchase_orders | NO ACTION | only draft POs can be deleted (409 PO_LOCKED); bills reference raised POs |
| contracts.client_id | clients | NO ACTION | clients are never hard-deleted |
| contracts.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| revenue_leakage.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| deals.owner_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| deals.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| deal_activities.deal_id | deals | CASCADE | DELETE /sales/deals removes the deal’s activities in the same request |
| deal_stage_history.deal_id | deals | CASCADE | DELETE /sales/deals removes the deal’s stage history in the same request |
| cost_estimates.client_id | clients | NO ACTION | clients are never hard-deleted |
| cost_estimates.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| documents.project_id | projects | NO ACTION | projects are never hard-deleted (status closed) |
| document_versions.document_id | documents | NO ACTION | documents are archived, never deleted |
| document_acks.document_id | documents | NO ACTION | documents are archived, never deleted |
| document_acks.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| document_views.document_id | documents | NO ACTION | documents are archived, never deleted |
| document_views.user_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| objectives.parent_id | objectives | NO ACTION | company.ts refuses to delete an objective with aligned children (409 OKR_HAS_CHILDREN) |
| key_results.objective_id | objectives | CASCADE | DELETE /okrs/objectives removes its key results in the same request |
| departments.parent_id | departments | NO ACTION | master data is deactivated, never deleted |
| departments.cost_centre_id | cost_centres | NO ACTION | master data is deactivated, never deleted |
| departments.head_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| designations.department_id | departments | NO ACTION | master data is deactivated, never deleted |
| designations.grade_id | grades | NO ACTION | master data is deactivated, never deleted |
| cost_centres.owner_id | users | NO ACTION | users are never hard-deleted (deactivated instead) |
| wf_instances.template_id | wf_templates | NO ACTION | workflow templates are deactivated, never deleted |

**References deliberately without a FK**

| table | columns | why |
|---|---|---|
| * | created_by, updated_by | audit actor; a platform super admin (tenant "platform") may act inside a tenant |
| activity_log | actor_id, entity_id | audit actor + polymorphic entity reference |
| admin_audit | actor_id, tenant_id | platform audit; actor is the platform admin |
| files | entity_id | polymorphic (entity, entity_id) |
| change_sets | entity_id, result_id | polymorphic; may name a record that a later change deleted |
| email_outbox | related_id | polymorphic (related_entity, related_id) |
| task_reviews | task_id | DELETE /tasks keeps the review history (only dependencies and comments are removed) |
| tasks | parent_task_id, recurrence_of, cr_id | a deleted task leaves follow-ups/recurrences pointing at it (mock keeps the id) |
| timesheet_entries | task_id | DELETE /tasks is guarded only by the SUM of logged hours > 0; the mock keeps any zero-hour entry |
| invoices | milestone_id, milestone_ids, file_id, duplicated_from | milestone_id is '' for ad-hoc/draft invoices; duplicated_from may name a discarded draft |
| ledger_entries | invoice_id | kept as a plain id: only drafts are deletable and drafts create no ledger rows, but the column is also used for cancelled invoices |
| expense_claims | ledger_entry_id, receipt_file_id | ledger rows can be deleted when not locked; files are polymorphic attachments |
| vendor_bills | ledger_entry_id, file_id | same as expense_claims |
| cost_estimates | deal_id, scenario_of | DELETE /sales/deals and DELETE /estimates leave these ids in place |
| revenue_leakage | deal_id | DELETE /sales/deals leaves leakage rows pointing at the deal |
| candidates | opening_id, resume_file_id, user_id | DELETE /openings does not check candidates; the mock keeps them |
| designations | default_role_id | a custom role can be deleted while designations still default to it |
| issued_documents | replaces_id, replaced_by_id, revoked_by, issued_by | issued documents can be deleted (drafts) leaving re-issue links |
| contracts | renewed_from, file_id | a renewed contract may be deleted (DELETE /contracts) leaving the link |
| lifecycle_checklists | user_id | the seed has an offboarding checklist for a person who is not a user row (r_000x) — fkcheck.ts |
| wf_instances | subject_user_id, current_assignee_id, initiated_by | the seed has an instance about a person who is not a user row (r_000x) — fkcheck.ts |

### Unique business keys

| name | table | key | why |
|---|---|---|---|
| users_email_lower_uk | users | (lower(email)) | login is by email only (auth.ts), so email is globally unique, case-insensitive |
| users_employee_code_uk | users | (tenant_id, employee_code) WHERE employee_code IS NOT NULL | employee codes are unique per tenant (EMPLOYEE_CODE_TAKEN) |
| roles_key_uk | roles | (tenant_id, key) | roleDef(tenant, key) resolves one role |
| invoices_number_uk | invoices | (tenant_id, number) WHERE number <> '' | invoice numbers are unique per tenant once issued (drafts carry '') |
| projects_code_uk | projects | (tenant_id, code) | project codes are unique per tenant |
| timesheet_weeks_user_week_uk | timesheet_weeks | (tenant_id, user_id, week_start) | one timesheet week per person per Monday |
| tenants_slug_uk | tenants | (slug) | workspace slug |

### Index rationale

Every tenant table: `(tenant_id, _ord)` — the runtime cache fill `SELECT * FROM t WHERE tenant_id = $1 ORDER BY _ord` is an index scan with no sort. FK columns without a covering hot-path index get `(tenant_id, column)`. Hot paths read from the route code:

| table | index | where | why |
|---|---|---|---|
| timesheet_entries | (tenant_id, user_id, work_date) |  | timesheet grid, payroll and analytics: one person over a date range |
| timesheet_entries | (tenant_id, week_id) |  | week detail / submit |
| timesheet_entries | (tenant_id, task_id) | task_id IS NOT NULL | hours per task (task delete guard, earned value) |
| timesheet_entries | (tenant_id, work_date) |  | company-wide date-range reports |
| timesheet_weeks | (tenant_id, status, week_start) |  | approval inbox / compliance |
| tasks | (tenant_id, project_id) |  | WBS / project board |
| tasks | (tenant_id, owner_id, status) |  | my tasks / workload |
| tasks | (tenant_id, milestone_id) | milestone_id IS NOT NULL | milestone progress |
| milestones | (tenant_id, project_id) |  | project plan |
| allocations | (tenant_id, user_id) |  | person utilisation |
| allocations | (tenant_id, project_id) |  | project staffing / costing |
| notifications | (tenant_id, user_id, read_at) |  | bell: unread for a user |
| activity_log | (tenant_id, entity, entity_id) |  | record history panels |
| activity_log | (tenant_id, at) |  | recent activity feeds |
| invoices | (tenant_id, project_id) |  | project billing |
| payments | (tenant_id, invoice_id) |  | invoice settlement |
| ledger_entries | (tenant_id, date) |  | cash-flow by period |
| leave | (tenant_id, user_id, date) |  | calendar (leave days per person) |
| leave_requests | (tenant_id, user_id) |  | my leave |
| leave_requests | (tenant_id, approver_id, status) |  | approval inbox |
| expense_claims | (tenant_id, user_id) |  | my expenses |
| expense_claims | (tenant_id, approver_id, status) |  | approval inbox |
| deals | (tenant_id, owner_id, stage) |  | pipeline |
| deal_activities | (tenant_id, deal_id) |  | deal timeline |
| deal_stage_history | (tenant_id, deal_id) |  | deal timeline / funnel |
| comments | (tenant_id, task_id) |  | task thread |
| task_dependencies | (tenant_id, task_id) |  | blockers |
| task_dependencies | (tenant_id, depends_on_task_id) |  | reverse blockers |
| task_reviews | (tenant_id, task_id) |  | review rounds |
| daily_updates | (tenant_id, user_id, date) |  | daily stand-up per person |
| cost_rates | (tenant_id, user_id, effective_from) |  | rate as of a date |
| files | (tenant_id, entity, entity_id) |  | attachments of a record |
| document_acks | (tenant_id, document_id) |  | must-read compliance |
| document_views | (tenant_id, document_id) |  | document analytics |
| document_versions | (tenant_id, document_id) |  | version list |
| appraisals | (tenant_id, cycle_id) |  | cycle dashboard |
| goals | (tenant_id, user_id) |  | my goals |
| email_outbox | (tenant_id, status) |  | delivery worker |
| change_sets | (tenant_id, status) |  | maker-checker inbox |
| wf_instances | (tenant_id, current_assignee_id, status) |  | my workflow tasks |
| issued_documents | (tenant_id, employee_id) |  | my documents |
| employee_documents | (tenant_id, employee_id) |  | employee file |

### Data dictionary

79 tables, 1356 API columns (plus hidden `_null_override`, `_ord` on each). "null→" is the default meaning of SQL NULL (`null` or `absent` key).

#### tenants

Row type `E.Tenant[]` · PK (id) · platform (RLS on id) · 4 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| name | text NOT NULL |  | string |  |
| slug | text NOT NULL |  | string |  |
| status | text NOT NULL |  | TenantStatus | one of: trial, active, read_only, suspended |
| industry | text NOT NULL |  | Industry | one of: it_services, sap_consulting, product, other |
| trial_ends_on | date | null | ISODate \| null |  |
| time_zone | text NOT NULL |  | string |  |
| plan_code | text NOT NULL |  | string |  |
| seats | integer NOT NULL |  | number |  |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| logo_data_url | text | null | string \| null |  |
| theme | jsonb NOT NULL |  | ThemeConfig |  |
| settings | jsonb NOT NULL |  | TenantSettings |  |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| org | jsonb | absent | ?: import('./access').OrgProfile |  |
| maker_checker | jsonb | absent | ?: import('./access').MakerCheckerConfig |  |
| invoicing | jsonb | absent | ?: import('./v7').InvoiceSettings |  |
| products | jsonb (V009) | absent | ?: TenantProduct[] | the company's own product master (Sales › Products): `[{id, name, stream, active}]`; absent = none yet (a new company starts empty). Deals keep the product name; renames are carried over by the API |

#### users

Row type `E.User[]` · PK (id) · tenant-scoped · 18 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| email | text NOT NULL |  | string |  |
| name | text NOT NULL |  | string |  |
| title | text NOT NULL |  | string |  |
| role | text NOT NULL |  | Role | one of: admin, pm, sales, ops, employee |
| reports_to_id | text | null | ID \| null | → users.id (FK, restrict) |
| grants | text[] NOT NULL |  | Grant[] |  |
| status | text NOT NULL |  | 'active' \| 'deactivated' \| 'invited' | one of: active, deactivated, invited |
| last_login_at | timestamptz | null | ISODateTime \| null |  |
| avatar_hue | integer NOT NULL |  | number |  |
| phone | text | null | string \| null |  |
| joined_on | date NOT NULL |  | ISODate |  |
| department | text NOT NULL |  | 'management' \| 'delivery' \| 'sales' \| 'operations' | one of: sales, management, delivery, operations |
| two_factor | boolean NOT NULL |  | boolean |  |
| prefs | jsonb NOT NULL |  | { theme_mode?: 'system' \| 'light' \| 'dark'; home_view?: string; emai |  |
| is_super_admin | boolean | absent | ?: boolean |  |
| role_key | text NOT NULL |  | string | RoleDef.key |
| extra_permissions | text[] NOT NULL |  | string[] | granted on top of the role |
| revoked_permissions | text[] NOT NULL |  | string[] | removed from the role for this person |
| must_change_password | boolean | absent | ?: boolean | set by Create Employee; forces a change on first login |
| employee_code | text | absent | ?: string \| null |  |
| department_id | text | absent | ?: ID \| null | → departments.id (FK, restrict) |
| designation_id | text | absent | ?: ID \| null | → designations.id (FK, restrict) |
| grade_id | text | absent | ?: ID \| null | → grades.id (FK, restrict) |
| location_id | text | absent | ?: ID \| null | → locations.id (FK, restrict) |
| shift_id | text | absent | ?: ID \| null | → shifts.id (FK, restrict) |
| cost_centre_id | text | absent | ?: ID \| null | → cost_centres.id (FK, restrict) |
| employment_type | text | absent | ?: 'full_time' \| 'part_time' \| 'contract' \| 'intern' \| null | one of: full_time, part_time, contract, intern |
| productive_hours | double precision | absent | ?: number \| null | hours of task work per day planning/forecasts assume (Feature Guide §16); null = tenant default |
| date_of_birth | date | absent | ?: ISODate \| null |  |
| personal_email | text | absent | ?: string \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### invites

Row type `E.Invite[]` · PK (id) · tenant-scoped · 1 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| email | text NOT NULL |  | string |  |
| name | text NOT NULL |  | string |  |
| role | text NOT NULL |  | Role | one of: admin, pm, sales, ops, employee |
| role_key | text | absent | ?: string |  |
| reports_to_id | text | null | ID \| null | reference id (no FK) |
| token | text NOT NULL |  | string |  |
| expires_at | timestamptz NOT NULL |  | ISODateTime |  |
| accepted_at | timestamptz | null | ISODateTime \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### subscriptions

Row type `E.Subscription[]` · PK (id) · tenant-scoped · 2 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| plan_code | text NOT NULL |  | string |  |
| status | text NOT NULL |  | 'trialing' \| 'active' \| 'past_due' \| 'cancelled' | one of: active, trialing, past_due, cancelled |
| current_period_end | date NOT NULL |  | ISODate |  |
| seats | integer NOT NULL |  | number |  |
| razorpay_subscription_id | text | null | string \| null | reference id (no FK) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### billing_invoices

Row type `E.BillingInvoice[]` · PK (id) · tenant-scoped · 4 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| number | text NOT NULL |  | string |  |
| period | text NOT NULL |  | string |  |
| amount_paise | bigint NOT NULL |  | Paise | money in paise |
| status | text NOT NULL |  | 'paid' \| 'due' \| 'failed' | one of: paid, due, failed |
| issued_on | date NOT NULL |  | ISODate |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### holidays

Row type `E.Holiday[]` · PK (id) · tenant-scoped · 15 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| date | date NOT NULL |  | ISODate |  |
| name | text NOT NULL |  | string |  |
| optional | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### delegations

Row type `E.Delegation[]` · PK (id) · tenant-scoped · 3 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| from_user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| to_user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| from_on | date NOT NULL |  | ISODate |  |
| to_on | date NOT NULL |  | ISODate |  |
| reason | text NOT NULL |  | string |  |
| project_id | text | null | ?: ID \| null | scope: null = all projects; → projects.id (FK, restrict) |
| include_tasks | boolean | absent | ?: boolean | also hand over open-task follow-ups (default true) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### projects

Row type `E.Project[]` · PK (id) · tenant-scoped · 10 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| code | text NOT NULL |  | string |  |
| name | text NOT NULL |  | string |  |
| client | text NOT NULL |  | string |  |
| type | text NOT NULL |  | ProjectType | one of: product, service, saas, ams |
| status | text NOT NULL |  | ProjectStatus | one of: active, initiated, on_hold, npa, completed, closed |
| pm_id | text | null | ID \| null | → users.id (FK, restrict) |
| start_on | date NOT NULL |  | ISODate |  |
| end_on | date NOT NULL |  | ISODate |  |
| contract_value_paise | bigint NOT NULL |  | Paise | money in paise |
| billing_model | text NOT NULL |  | BillingModel | one of: fixed, tm, subscription, ams_retainer |
| stream | text NOT NULL |  | RevenueStream | one of: sap_consulting, saas, ams, products, services |
| description | text NOT NULL |  | string |  |
| priority | integer NOT NULL |  | number | 1 (highest) … 5, see PROJECT_PRIORITY |
| deal_id | text | null | ID \| null | → deals.id (FK, set_null) |
| baseline_end_on | date | null | ISODate \| null |  |
| color_hue | integer NOT NULL |  | number |  |
| costing | jsonb | absent | ?: ProjectCosting \| null | the saved project cost calculator (Feature Guide §3, §6) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### project_status_history

Row type `E.ProjectStatusHistory[]` · PK (id) · tenant-scoped · 16 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| project_id | text NOT NULL |  | ID | → projects.id (FK, restrict) |
| from_status | text | null | ProjectStatus \| null | one of: active, initiated, on_hold, npa, completed, closed |
| to_status | text NOT NULL |  | ProjectStatus | one of: active, initiated, on_hold, npa, completed, closed |
| reason | text | null | string \| null |  |
| changed_by | text NOT NULL |  | ID |  |
| changed_at | timestamptz NOT NULL |  | ISODateTime |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### milestones

Row type `E.Milestone[]` · PK (id) · tenant-scoped · 23 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| project_id | text NOT NULL |  | ID | → projects.id (FK, restrict) |
| no | integer NOT NULL |  | number |  |
| name | text NOT NULL |  | string |  |
| planned_start | date NOT NULL |  | ISODate |  |
| planned_end | date NOT NULL |  | ISODate |  |
| baseline_end | date NOT NULL |  | ISODate |  |
| billing_amount_paise | bigint NOT NULL |  | Paise | money in paise |
| billing_status | text NOT NULL |  | MilestoneBilling | one of: paid, none, invoiced |
| achieved_on | date | null | ISODate \| null |  |
| empty_since | date | null | ISODate \| null |  |
| deleted_at | timestamptz | null | ISODateTime \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### tasks

Row type `E.Task[]` · PK (id) · tenant-scoped · 69 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| project_id | text NOT NULL |  | ID | → projects.id (FK, restrict) |
| milestone_id | text | null | ID \| null | → milestones.id (FK, restrict) |
| no | integer NOT NULL |  | number |  |
| title | text NOT NULL |  | string |  |
| owner_id | text | null | ID \| null | reference id (no FK) |
| backup_id | text | null | ID \| null | reference id (no FK) |
| est_hours | double precision NOT NULL |  | number |  |
| start_on | date NOT NULL |  | ISODate |  |
| due_on | date NOT NULL |  | ISODate |  |
| done_on | date | null | ISODate \| null |  |
| complexity | text NOT NULL |  | Complexity | one of: low, medium, high, very_high |
| status | text NOT NULL |  | TaskStatus | one of: todo, in_progress, blocked, in_review, done |
| description | text NOT NULL |  | string |  |
| blocked_reason | text | null | string \| null |  |
| kind | text | absent | ?: 'project' \| 'general' | general = assigned by a line manager outside client projects; one of: project, general |
| assigned_by | text | absent | ?: ID \| null |  |
| reviewer_id | text | null | ?: ID \| null | who approves completion (PM for project tasks, assigner/manager otherwise); reference id (no FK) |
| submitted_on | date | absent | ?: ISODate \| null | last submission for review |
| approved_by | text | absent | ?: ID \| null |  |
| approved_on | date | absent | ?: ISODate \| null |  |
| review_round | integer | absent | ?: number |  |
| timeliness | text | absent | ?: 'early' \| 'on_time' \| 'late' \| null | set on approval: done_on vs due_on; one of: early, on_time, late |
| variance_days | double precision | absent | ?: number \| null | working days, negative = early |
| delay_reason_code | text | absent | ?: string \| null |  |
| delay_reason | text | absent | ?: string \| null |  |
| stage | text | absent | ?: TaskStage | detailed workflow stage; status is derived from it; one of: on_hold, completed, blocked, new, selected, in_development, unit_testing, functional_testing, uat, waiting_feedback |
| progress | double precision | absent | ?: number | 0–100 % complete (earned hours = est × progress) |
| tag | text | null | ?: TaskTag \| null | e.g. Bug; one of: feature, bug, improvement, documentation, support, meeting |
| actual_start_on | date | null | ?: ISODate \| null | first day work started (first hour logged or moved into development) |
| target_revisions | jsonb | absent | ?: { from: ISODate; to: ISODate; at: ISODateTime; by: ID; reason: stri | every move of due_on after baseline |
| baseline_due_on | date | absent | ?: ISODate \| null | the original target end |
| slip_flagged_on | date | null | ?: ISODate \| null | owner/PM flagged a likely slip early (early warning) |
| slip_note | text | null | ?: string \| null |  |
| cancelled_on | date | absent | ?: ISODate \| null | cancelled with hours logged (kept on cost, out of delivery stats) |
| cancel_reason | text | absent | ?: string \| null |  |
| cr_id | text | absent | ?: ID \| null | created from an approved change request; reference id (no FK) |
| quality_rating | double precision | absent | ?: number \| null | 1–5 from the reviewer |
| priority | text | absent | ?: 'low' \| 'normal' \| 'high' \| 'urgent' | one of: low, high, normal, urgent |
| concerned_manager_id | text | absent | ?: ID \| null | accountable line manager (default: owner's reporting manager); reference id (no FK) |
| watcher_ids | text[] | absent | ?: ID[] | followers notified of changes |
| acceptance_criteria | jsonb | absent | ?: { text: string; done: boolean }[] | definition of done checklist |
| labels | text[] | absent | ?: string[] |  |
| billable | boolean | absent | ?: boolean |  |
| recurrence | text | absent | ?: 'none' \| 'weekly' \| 'monthly' \| null | creates the next occurrence on completion; one of: none, weekly, monthly |
| recurrence_of | text | absent | ?: ID \| null | the task this one repeats |
| attachments | jsonb | absent | ?: { id: string; name: string; size: number; type: string; data_url?:  |  |
| parent_task_id | text | absent | ?: ID \| null | sub-task / follow-up of another task; reference id (no FK) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| wbs_code | text | absent | (untyped) | untyped field written by the shared code |
| wf_process | text | absent | (untyped) | untyped field written by the shared code |
| wf_chain | jsonb | absent | (untyped) | untyped field written by the shared code |
| wf_step | integer | absent | (untyped) | untyped field written by the shared code |
| wf_step_at | timestamptz | absent | (untyped) | untyped field written by the shared code |
| wf_approver_id | text | absent | (untyped) | reference id (no FK); untyped field written by the shared code |

#### task_dependencies

Row type `E.TaskDependency[]` · PK (id) · tenant-scoped · 16 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| task_id | text NOT NULL |  | ID | → tasks.id (FK, cascade) |
| depends_on_task_id | text NOT NULL |  | ID | → tasks.id (FK, cascade) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### comments

Row type `E.Comment[]` · PK (id) · tenant-scoped · 5 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| task_id | text NOT NULL |  | ID | → tasks.id (FK, cascade) |
| body | text NOT NULL |  | string |  |
| mentions | text[] NOT NULL |  | ID[] |  |
| author_id | text NOT NULL |  | ID | reference id (no FK) |
| parent_id | text | null | ID \| null | → comments.id (FK, cascade) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### notifications

Row type `E.Notification[]` · PK (id) · tenant-scoped · 10 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| type | text NOT NULL |  | NotificationType | one of: NTF-01, NTF-02, NTF-03, NTF-04, NTF-05, NTF-06, NTF-07, NTF-08, NTF-09, NTF-10, NTF-11, NTF-12, NTF-13, NTF-14, NTF-15, NTF-16, NTF-17 |
| title | text NOT NULL |  | string |  |
| body | text NOT NULL |  | string |  |
| link | text | null | string \| null |  |
| read_at | timestamptz | null | ISODateTime \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### timesheet_weeks

Row type `E.TimesheetWeek[]` · PK (id) · tenant-scoped · 160 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| week_start | date NOT NULL |  | ISODate | Monday |
| status | text NOT NULL |  | WeekStatus | one of: open, submitted, approved, rejected, locked |
| submitted_at | timestamptz | null | ISODateTime \| null |  |
| approver_id | text | null | ID \| null | reference id (no FK) |
| decided_by | text | null | ID \| null |  |
| decided_at | timestamptz | null | ISODateTime \| null |  |
| late | boolean NOT NULL |  | boolean |  |
| reject_reason | text | null | string \| null |  |
| unlock_requested_at | timestamptz | null | ISODateTime \| null |  |
| unlock_reason | text | null | string \| null |  |
| unlock_until | date | null | ISODate \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| wf_process | text | absent | (untyped) | untyped field written by the shared code |
| wf_chain | jsonb | absent | (untyped) | untyped field written by the shared code |
| wf_step | integer | absent | (untyped) | untyped field written by the shared code |
| wf_step_at | timestamptz | absent | (untyped) | untyped field written by the shared code |

#### timesheet_entries

Row type `E.TimesheetEntry[]` · PK (id) · tenant-scoped · 1701 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| week_id | text NOT NULL |  | ID | → timesheet_weeks.id (FK, restrict) |
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| work_date | date NOT NULL |  | ISODate |  |
| kind | text NOT NULL |  | EntryKind | one of: task, leave, internal |
| task_id | text | null | ID \| null | reference id (no FK) |
| internal_code | text | null | string \| null | e.g. 'meeting' \| 'learning' \| 'presales' \| 'admin' |
| leave_type | text | null | LeaveType \| null | one of: casual, sick, earned, compensatory, lwp, optional_holiday |
| hours | double precision NOT NULL |  | number |  |
| note | text NOT NULL |  | string |  |
| project_id | text | absent | ?: ID \| null | activities (internal time) may name the project they were for (v5 timesheet); → projects.id (FK, restrict) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### leave

Row type `E.Leave[]` · PK (id) · tenant-scoped · 17 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| date | date NOT NULL |  | ISODate |  |
| type | text NOT NULL |  | LeaveType | one of: casual, sick, earned, compensatory, lwp, optional_holiday |
| portion | text NOT NULL |  | 'full' \| 'half' | one of: full, half |
| source | text NOT NULL |  | 'timesheet' \| 'hr' | one of: timesheet, hr |
| approved_by | text | null | ID \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### daily_updates

Row type `E.DailyUpdate[]` · PK (id) · tenant-scoped · 18 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| date | date NOT NULL |  | ISODate |  |
| done | text NOT NULL |  | string |  |
| proof_url | text NOT NULL |  | string |  |
| blocker | text NOT NULL |  | string |  |
| demo_shown | boolean NOT NULL |  | boolean |  |
| demo_note | text | absent | ?: string | what was demoed (Friday demo) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### cost_rates

Row type `E.CostRate[]` · PK (id) · tenant-scoped · 27 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| monthly_ctc_paise | bigint NOT NULL |  | Paise | money in paise |
| variable_pct | double precision NOT NULL |  | number |  |
| sales_target_paise | bigint | null | Paise \| null | money in paise |
| effective_from | date NOT NULL |  | ISODate |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### allocations

Row type `E.Allocation[]` · PK (id) · tenant-scoped · 49 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| project_id | text NOT NULL |  | ID | → projects.id (FK, restrict) |
| milestone_id | text | null | ID \| null | null = all milestones (follows the project's forecast end); → milestones.id (FK, restrict) |
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| pct | double precision NOT NULL |  | number |  |
| from_on | date NOT NULL |  | ISODate |  |
| to_on | date NOT NULL |  | ISODate |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### files

Row type `E.FileRef[]` · PK (id) · tenant-scoped · 101 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| mime | text NOT NULL |  | string |  |
| size_bytes | integer NOT NULL |  | number |  |
| entity | text NOT NULL |  | string |  |
| entity_id | text NOT NULL |  | ID | reference id (no FK) |
| data_url | text | null | string \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### invoices

Row type `E.Invoice[]` · PK (id) · tenant-scoped · 9 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| project_id | text NOT NULL |  | ID | → projects.id (FK, restrict) |
| milestone_id | text NOT NULL |  | ID | First milestone billed; '' for ad-hoc invoices, drafts and cancelled invoices (full list in milestone_ids).; reference id (no FK) |
| number | text NOT NULL |  | string |  |
| invoiced_on | date NOT NULL |  | ISODate |  |
| amount_paise | bigint NOT NULL |  | Paise | Revenue = taxable value (ex-GST). 0 while draft or after cancellation. Every revenue/margin calc reads this.; money in paise |
| paid_amount_paise | bigint NOT NULL |  | Paise | Revenue share of what has been settled (cash received + TDS), on the same ex-GST basis as amount_paise.; money in paise |
| paid_on | date | null | ISODate \| null |  |
| due_on | date NOT NULL |  | ISODate |  |
| file_id | text | null | ID \| null | reference id (no FK) |
| status | text | absent | ?: 'draft' \| 'issued' \| 'partially_paid' \| 'paid' \| 'cancelled' | one of: cancelled, paid, draft, issued, partially_paid |
| client_id | text | absent | ?: ID \| null | → clients.id (FK, restrict) |
| milestone_ids | text[] | absent | ?: ID[] |  |
| bill_to | jsonb | absent | ?: import('./v7').InvoiceParty \| null |  |
| ship_to | jsonb | null | ?: import('./v7').InvoiceParty \| null |  |
| seller | jsonb | absent | ?: import('./v7').InvoiceParty \| null |  |
| bank | jsonb | absent | ?: { bank_name: string; account_name: string; account_number: string;  |  |
| place_of_supply | text | absent | ?: string \| null | GST state code, e.g. "21" (Odisha); "96" = outside India |
| tax_mode | text | absent | ?: 'intra' \| 'inter' \| 'export' \| null | CGST+SGST vs IGST vs zero-rated export (LUT); one of: intra, inter, export |
| reverse_charge | boolean | absent | ?: boolean |  |
| lines | jsonb | absent | ?: import('./v7').InvoiceLine[] |  |
| subtotal_paise | bigint | absent | ?: Paise | sum of qty × rate before discount; money in paise |
| discount_paise | bigint | absent | ?: Paise | money in paise |
| taxable_paise | bigint | absent | ?: Paise | kept after cancellation (amount_paise drops to 0); money in paise |
| cgst_paise | bigint | absent | ?: Paise | money in paise |
| sgst_paise | bigint | absent | ?: Paise | money in paise |
| igst_paise | bigint | absent | ?: Paise | money in paise |
| round_off_paise | bigint | absent | ?: Paise | money in paise |
| tds_pct | double precision | absent | ?: number |  |
| total_paise | bigint | absent | ?: Paise | grand total incl. GST, rounded to the rupee; money in paise |
| received_paise | bigint | absent | ?: Paise | cash received against total_paise; money in paise |
| tds_received_paise | bigint | absent | ?: Paise | TDS the client deducted (counts as settled); money in paise |
| notes | text | absent | ?: string |  |
| terms | text | absent | ?: string |  |
| po_reference | text | null | ?: string \| null |  |
| po_date | date | null | ?: ISODate \| null |  |
| signatory | jsonb | absent | ?: { name: string; title: string } \| null |  |
| draft_no | integer | absent | ?: number |  |
| issued_on | date | absent | ?: ISODate \| null |  |
| issued_by | text | absent | ?: ID \| null |  |
| cancelled_on | date | absent | ?: ISODate \| null |  |
| cancel_reason | text | absent | ?: string \| null |  |
| credit_note | jsonb | absent | ?: { number: string; date: ISODate; taxable_paise: Paise; total_paise: |  |
| emails | jsonb | absent | ?: { to: string; at: ISODateTime; by: ID; outbox_id: ID \| null }[] |  |
| duplicated_from | text | absent | ?: ID \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### payments

Row type `E.Payment[]` · PK (id) · tenant-scoped · 7 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| invoice_id | text NOT NULL |  | ID | → invoices.id (FK, restrict) |
| amount_paise | bigint NOT NULL |  | Paise | Revenue share settled by this payment (ex-GST basis, sums to invoice.amount_paise when paid in full).; money in paise |
| paid_on | date NOT NULL |  | ISODate |  |
| reference | text NOT NULL |  | string |  |
| gross_paise | bigint | absent | ?: Paise | money in paise |
| tds_paise | bigint | absent | ?: Paise | money in paise |
| mode | text | absent | ?: string |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### payroll_runs

Row type `E.PayrollRun[]` · PK (id) · tenant-scoped · 2 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| month | text NOT NULL |  | string | YYYY-MM |
| status | text NOT NULL |  | 'draft' \| 'final' | one of: draft, final |
| finalised_by | text | null | ID \| null |  |
| finalised_at | timestamptz | null | ISODateTime \| null |  |
| warnings | text[] NOT NULL |  | string[] |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### payroll_lines

Row type `E.PayrollLine[]` · PK (id) · tenant-scoped · 0 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| run_id | text NOT NULL |  | ID | → payroll_runs.id (FK, restrict) |
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| monthly_ctc_paise | bigint NOT NULL |  | Paise | money in paise |
| base_paise | bigint NOT NULL |  | Paise | money in paise |
| working_days | double precision NOT NULL |  | number |  |
| approved_days | double precision NOT NULL |  | number |  |
| unapproved_days | double precision NOT NULL |  | number |  |
| unapproved_weeks | text[] NOT NULL |  | ISODate[] |  |
| lwp_days | double precision NOT NULL |  | number |  |
| deduction_paise | bigint NOT NULL |  | Paise | money in paise |
| variable_paise | bigint NOT NULL |  | Paise | money in paise |
| net_paise | bigint NOT NULL |  | Paise | money in paise |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| unapproved_detail | jsonb | absent | (untyped) | untyped field written by the shared code |
| would_deduct_paise | bigint | absent | (untyped) | money in paise; untyped field written by the shared code |
| deduct_applied | boolean | absent | (untyped) | untyped field written by the shared code |
| sales | jsonb | null | (untyped) | untyped field written by the shared code |
| no_rate | boolean | absent | (untyped) | untyped field written by the shared code |
| pending_days | double precision | absent | (untyped) | untyped field written by the shared code |
| approver_name | text | null | (untyped) | untyped field written by the shared code |
| self_approves | boolean | absent | (untyped) | untyped field written by the shared code |
| employee_code | text | absent | (untyped) | untyped field written by the shared code |
| flags | text[] | absent | (untyped) | untyped field written by the shared code |

#### ledger_entries

Row type `E.LedgerEntry[]` · PK (id) · tenant-scoped · 73 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| kind | text NOT NULL |  | LedgerKind | one of: income, expense |
| category | text NOT NULL |  | string |  |
| description | text NOT NULL |  | string |  |
| amount_paise | bigint NOT NULL |  | Paise | money in paise |
| date | date NOT NULL |  | ISODate |  |
| recurring | boolean NOT NULL |  | boolean |  |
| until_on | date | null | ISODate \| null |  |
| status | text NOT NULL |  | 'expected' \| 'received' \| 'paid' | one of: paid, expected, received |
| project_id | text | null | ID \| null | → projects.id (FK, restrict) |
| milestone_id | text | null | ID \| null | → milestones.id (FK, restrict) |
| invoice_id | text | null | ID \| null | reference id (no FK) |
| stream | text | null | RevenueStream \| null | one of: sap_consulting, saas, ams, products, services |
| locked | boolean NOT NULL |  | boolean | auto-created from invoice: cannot delete |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### deals

Row type `E.Deal[]` · PK (id) · tenant-scoped · 24 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| title | text NOT NULL |  | string |  |
| client | text NOT NULL |  | string |  |
| contact | text NOT NULL |  | string |  |
| phone | text NOT NULL |  | string |  |
| email | text NOT NULL |  | string |  |
| product | text NOT NULL |  | string |  |
| source | text NOT NULL |  | DealSource | one of: other, referral, bni, website, cold_call, event, partner, existing_client |
| owner_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| stage | text NOT NULL |  | DealStage | one of: lead, qualified, demo, proposal, negotiation, won, lost |
| value_paise | bigint NOT NULL |  | Paise | money in paise |
| mrr_paise | bigint NOT NULL |  | Paise | money in paise |
| expected_close | date NOT NULL |  | ISODate |  |
| won_on | date | null | ISODate \| null |  |
| lost_on | date | null | ISODate \| null |  |
| lost_reason | text | null | string \| null |  |
| project_id | text | null | ID \| null | → projects.id (FK, restrict) |
| last_activity_on | date NOT NULL |  | ISODate |  |
| notes | text NOT NULL |  | string |  |
| first_conversation | text | null | ?: string \| null | how the lead came in / first conversation |
| next_step | text | null | ?: string \| null |  |
| next_step_on | date | null | ?: ISODate \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### deal_activities

Row type `E.DealActivity[]` · PK (id) · tenant-scoped · 62 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| deal_id | text NOT NULL |  | ID | → deals.id (FK, cascade) |
| date | date NOT NULL |  | ISODate |  |
| type | text NOT NULL |  | ActivityType | one of: meeting, demo, proposal, call, email, follow_up, whatsapp |
| note | text NOT NULL |  | string |  |
| by | text NOT NULL |  | ID |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### deal_stage_history

Row type `E.DealStageHistory[]` · PK (id) · tenant-scoped · 93 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| deal_id | text NOT NULL |  | ID | → deals.id (FK, cascade) |
| from_stage | text | null | DealStage \| null | one of: lead, qualified, demo, proposal, negotiation, won, lost |
| to_stage | text NOT NULL |  | DealStage | one of: lead, qualified, demo, proposal, negotiation, won, lost |
| by | text NOT NULL |  | ID |  |
| at | timestamptz NOT NULL |  | ISODateTime |  |
| reason | text | null | string \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### change_requests

Row type `E.ChangeRequest[]` · PK (id) · tenant-scoped · 4 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| project_id | text NOT NULL |  | ID | → projects.id (FK, restrict) |
| title | text NOT NULL |  | string |  |
| description | text NOT NULL |  | string |  |
| hours | double precision NOT NULL |  | number |  |
| value_paise | bigint NOT NULL |  | Paise | money in paise |
| status | text NOT NULL |  | CRStatus | one of: done, approved, rejected, raised, estimated |
| raised_by | text NOT NULL |  | ID |  |
| milestone_id | text | null | ID \| null | → milestones.id (FK, restrict) |
| decided_by | text | null | ID \| null |  |
| decided_at | timestamptz | null | ISODateTime \| null |  |
| number | integer | absent | ?: number | CR-n per project |
| requested_by_client | text | absent | ?: string | who at the client asked |
| requested_on | date | absent | ?: ISODate \| null |  |
| impact_days | double precision | absent | ?: number | working days impact on the schedule |
| note | text | absent | ?: string |  |
| proposed_tasks | jsonb | absent | ?: { title: string; est_hours: number; owner_id: ID \| null }[] | added to the WBS on approval |
| tasks_added_on | timestamptz | null | ?: ISODateTime \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### openings

Row type `E.Opening[]` · PK (id) · tenant-scoped · 3 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| role | text NOT NULL |  | string |  |
| project_id | text | null | ID \| null | → projects.id (FK, restrict) |
| count | integer NOT NULL |  | number |  |
| status | text NOT NULL |  | 'open' \| 'interviewing' \| 'offered' \| 'filled' \| 'on_hold' | one of: on_hold, open, interviewing, offered, filled |
| owner_id | text NOT NULL |  | ID | reference id (no FK) |
| needed_by | date | null | ISODate \| null |  |
| notes | text NOT NULL |  | string |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### ops_items

Row type `E.OpsItem[]` · PK (id) · tenant-scoped · 12 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| item | text NOT NULL |  | string |  |
| category | text NOT NULL |  | 'payroll' \| 'compliance' \| 'finance' \| 'admin' \| 'hr' \| 'it' | one of: admin, hr, payroll, compliance, finance, it |
| owner_id | text NOT NULL |  | ID | reference id (no FK) |
| due_on | date NOT NULL |  | ISODate |  |
| recurring | text NOT NULL |  | Recurrence | one of: none, weekly, monthly, quarterly, yearly |
| status | text NOT NULL |  | 'open' \| 'done' | one of: done, open |
| last_done_on | date | null | ISODate \| null |  |
| notes | text NOT NULL |  | string |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| history | jsonb | absent | (untyped) | untyped field written by the shared code |

#### activity_log

Row type `E.ActivityLog[]` · PK (id) · tenant-scoped · 141 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| entity | text NOT NULL |  | string |  |
| entity_id | text NOT NULL |  | ID | reference id (no FK) |
| action | text NOT NULL |  | string |  |
| summary | text NOT NULL |  | string |  |
| actor_id | text NOT NULL |  | ID | reference id (no FK) |
| at | timestamptz NOT NULL |  | ISODateTime |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### saved_reports

Row type `E.SavedReport[]` · PK (id) · tenant-scoped · 5 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| description | text NOT NULL |  | string |  |
| spec | jsonb NOT NULL |  | import('./reports').ReportSpec |  |
| owner_id | text NOT NULL |  | ID | reference id (no FK) |
| shared | boolean NOT NULL |  | boolean |  |
| pinned | boolean NOT NULL |  | boolean |  |
| source | text NOT NULL |  | 'builder' \| 'ai' \| 'system' | one of: builder, ai, system |
| schedule | text NOT NULL |  | 'none' \| 'weekly' \| 'monthly' | one of: none, weekly, monthly |
| last_run_at | timestamptz | null | ISODateTime \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### task_reviews

Row type `B.TaskReview[]` · PK (id) · tenant-scoped · 31 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| task_id | text NOT NULL |  | ID | reference id (no FK) |
| round | integer NOT NULL |  | number |  |
| submitted_by | text NOT NULL |  | ID |  |
| submitted_at | timestamptz NOT NULL |  | ISODateTime |  |
| submission_note | text NOT NULL |  | string |  |
| proof_url | text | null | string \| null |  |
| delay_reason_code | text | null | DelayReasonCode \| null | one of: other, scope_change, dependency, client_delay, underestimated, leave_absence, priority_change, technical_issue, resource_shortage |
| delay_reason | text | null | string \| null |  |
| decision | text | null | 'approved' \| 'sent_back' \| null | one of: approved, sent_back |
| decided_by | text | null | ID \| null |  |
| decided_at | timestamptz | null | ISODateTime \| null |  |
| decision_reason | text | null | string \| null | required when sent back |
| quality_rating | double precision | null | number \| null | 1–5, set on approval |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### documents

Row type `B.DocumentRec[]` · PK (id) · tenant-scoped · 22 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| project_id | text | null | ID \| null | null = company library; → projects.id (FK, restrict) |
| title | text NOT NULL |  | string |  |
| description | text NOT NULL |  | string |  |
| category | text NOT NULL |  | DocCategory | one of: other, contract, requirements, design, technical, test, meeting_notes, process, policy, training |
| visibility | text NOT NULL |  | DocVisibility | one of: project, company, managers |
| must_read | boolean NOT NULL |  | boolean |  |
| tags | text[] NOT NULL |  | string[] |  |
| owner_id | text NOT NULL |  | ID | reference id (no FK) |
| current_version | integer NOT NULL |  | number |  |
| archived | boolean NOT NULL |  | boolean |  |
| effective_on | date | absent | ?: string \| null | v7: policy effective date (company library only) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### document_versions

Row type `B.DocumentVersion[]` · PK (id) · tenant-scoped · 26 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| document_id | text NOT NULL |  | ID | → documents.id (FK, restrict) |
| version | integer NOT NULL |  | number |  |
| file_id | text | null | ID \| null | files table (data_url) or null when a link; reference id (no FK) |
| url | text | null | string \| null | external link (Drive/SharePoint) instead of upload |
| file_name | text NOT NULL |  | string |  |
| size_bytes | integer NOT NULL |  | number |  |
| mime | text NOT NULL |  | string |  |
| note | text NOT NULL |  | string |  |
| uploaded_by | text NOT NULL |  | ID |  |
| uploaded_at | timestamptz NOT NULL |  | ISODateTime |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |

#### document_acks

Row type `B.DocumentAck[]` · PK (id) · tenant-scoped · 37 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| document_id | text NOT NULL |  | ID | → documents.id (FK, restrict) |
| version | integer NOT NULL |  | number |  |
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| at | timestamptz NOT NULL |  | ISODateTime |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |

#### document_views

Row type `B.DocumentView[]` · PK (id) · tenant-scoped · 23 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| document_id | text NOT NULL |  | ID | → documents.id (FK, restrict) |
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| at | timestamptz NOT NULL |  | ISODateTime |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number |  |

#### leave_requests

Row type `B.LeaveRequest[]` · PK (id) · tenant-scoped · 7 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| from_on | date NOT NULL |  | ISODate |  |
| to_on | date NOT NULL |  | ISODate |  |
| type | text NOT NULL |  | LeaveType | one of: casual, sick, earned, compensatory, lwp, optional_holiday |
| portion | text NOT NULL |  | 'full' \| 'half' | one of: full, half |
| days | double precision NOT NULL |  | number |  |
| reason | text NOT NULL |  | string |  |
| status | text NOT NULL |  | 'pending' \| 'approved' \| 'rejected' \| 'cancelled' | one of: cancelled, approved, rejected, pending |
| approver_id | text | null | ID \| null | reference id (no FK) |
| decided_by | text | null | ID \| null |  |
| decided_at | timestamptz | null | ISODateTime \| null |  |
| decision_reason | text | null | string \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| wf_process | text | absent | (untyped) | untyped field written by the shared code |
| wf_chain | jsonb | absent | (untyped) | untyped field written by the shared code |
| wf_step | integer | absent | (untyped) | untyped field written by the shared code |
| wf_step_at | timestamptz | absent | (untyped) | untyped field written by the shared code |

#### leave_policies

Row type `B.LeavePolicy[]` · PK (id) · tenant-scoped · 10 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| type | text NOT NULL |  | LeaveType | one of: casual, sick, earned, compensatory, lwp, optional_holiday |
| annual_days | double precision NOT NULL |  | number |  |
| carry_forward_max | double precision NOT NULL |  | number |  |
| accrual | text NOT NULL |  | 'upfront' \| 'monthly' | one of: monthly, upfront |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### appraisal_cycles

Row type `B.AppraisalCycle[]` · PK (id) · tenant-scoped · 2 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string | e.g. "H1 FY26-27" |
| period_from | date NOT NULL |  | ISODate |  |
| period_to | date NOT NULL |  | ISODate |  |
| status | text NOT NULL |  | AppraisalCycleStatus | one of: closed, draft, goal_setting, self_review, manager_review, hr_review |
| self_due | date NOT NULL |  | ISODate |  |
| manager_due | date NOT NULL |  | ISODate |  |
| hr_due | date NOT NULL |  | ISODate |  |
| weights | jsonb NOT NULL |  | { goals: number; tasks: number; timesheets: number; behaviour: number  | percent, sum 100 |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### appraisals

Row type `B.Appraisal[]` · PK (id) · tenant-scoped · 18 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| cycle_id | text NOT NULL |  | ID | → appraisal_cycles.id (FK, restrict) |
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| manager_id | text | null | ID \| null | reference id (no FK) |
| status | text NOT NULL |  | AppraisalStatus | one of: completed, not_started, self_pending, manager_pending, hr_pending |
| self_rating | double precision | null | number \| null | 1–5 |
| self_comments | text NOT NULL |  | string |  |
| manager_rating | double precision | null | number \| null |  |
| manager_comments | text NOT NULL |  | string |  |
| hr_rating | double precision | null | number \| null |  |
| hr_comments | text NOT NULL |  | string |  |
| final_rating | double precision | null | number \| null |  |
| increment_pct | double precision | null | number \| null |  |
| promotion_to | text | null | string \| null |  |
| metrics | jsonb NOT NULL |  | Record<string, number \| null> | snapshot: on_time_pct, avg_quality, timesheet_compliance, goals_score… |
| submitted_self_at | timestamptz | null | ISODateTime \| null |  |
| submitted_manager_at | timestamptz | null | ISODateTime \| null |  |
| closed_at | timestamptz | null | ISODateTime \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### goals

Row type `B.Goal[]` · PK (id) · tenant-scoped · 36 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| cycle_id | text | null | ID \| null | → appraisal_cycles.id (FK, restrict) |
| title | text NOT NULL |  | string |  |
| kra | text NOT NULL |  | string | key result area |
| weight | double precision NOT NULL |  | number | % within the person's goals |
| measure | text NOT NULL |  | string |  |
| target | double precision NOT NULL |  | number |  |
| current | double precision NOT NULL |  | number |  |
| unit | text NOT NULL |  | string |  |
| due_on | date NOT NULL |  | ISODate |  |
| status | text NOT NULL |  | 'draft' \| 'agreed' \| 'on_track' \| 'at_risk' \| 'achieved' \| 'misse | one of: draft, agreed, on_track, at_risk, achieved, missed |
| manager_approved | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### feedback

Row type `B.Feedback[]` · PK (id) · tenant-scoped · 8 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| to_user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| from_user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| kind | text NOT NULL |  | 'praise' \| 'improvement' \| 'one_on_one' | one of: improvement, praise, one_on_one |
| body | text NOT NULL |  | string |  |
| visibility | text NOT NULL |  | 'private' \| 'manager' \| 'public' | one of: private, manager, public |
| at | timestamptz NOT NULL |  | ISODateTime |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### candidates

Row type `B.Candidate[]` · PK (id) · tenant-scoped · 6 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| opening_id | text NOT NULL |  | ID | reference id (no FK) |
| name | text NOT NULL |  | string |  |
| email | text NOT NULL |  | string |  |
| phone | text NOT NULL |  | string |  |
| source | text NOT NULL |  | 'referral' \| 'job_portal' \| 'linkedin' \| 'campus' \| 'agency' \| 'w | one of: other, referral, website, job_portal, linkedin, campus, agency |
| stage | text NOT NULL |  | CandidateStage | one of: rejected, applied, screening, interview, offer, hired, withdrawn |
| experience_years | double precision NOT NULL |  | number |  |
| current_ctc_paise | bigint | null | Paise \| null | money in paise |
| expected_ctc_paise | bigint | null | Paise \| null | money in paise |
| offer_ctc_paise | bigint | null | Paise \| null | money in paise |
| notice_days | integer | null | number \| null |  |
| resume_file_id | text | null | ID \| null | reference id (no FK) |
| rating | double precision | null | number \| null |  |
| notes | text NOT NULL |  | string |  |
| rejected_reason | text | null | string \| null |  |
| joining_on | date | null | ISODate \| null |  |
| user_id | text | null | ID \| null | set when hired and converted to a user; reference id (no FK) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### interviews

Row type `B.Interview[]` · PK (id) · tenant-scoped · 7 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| candidate_id | text NOT NULL |  | ID | → candidates.id (FK, restrict) |
| round | integer NOT NULL |  | number |  |
| kind | text NOT NULL |  | 'technical' \| 'hr' \| 'manager' \| 'assignment' \| 'final' | one of: hr, final, technical, manager, assignment |
| scheduled_at | timestamptz NOT NULL |  | ISODateTime |  |
| interviewer_id | text NOT NULL |  | ID | reference id (no FK) |
| result | text NOT NULL |  | 'pending' \| 'pass' \| 'fail' \| 'no_show' | one of: pending, pass, fail, no_show |
| rating | double precision | null | number \| null |  |
| feedback | text NOT NULL |  | string |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### lifecycle_checklists

Row type `B.LifecycleChecklist[]` · PK (id) · tenant-scoped · 2 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | reference id (no FK) |
| kind | text NOT NULL |  | 'onboarding' \| 'offboarding' | one of: onboarding, offboarding |
| status | text NOT NULL |  | 'open' \| 'completed' | one of: completed, open |
| start_on | date NOT NULL |  | ISODate | joining date or resignation date |
| last_day | date | null | ISODate \| null |  |
| exit_reason | text | null | string \| null |  |
| exit_type | text | null | 'resignation' \| 'termination' \| 'contract_end' \| 'retirement' \| nu | one of: resignation, termination, contract_end, retirement |
| rehire_eligible | boolean | null | boolean \| null |  |
| items | jsonb NOT NULL |  | ChecklistItem[] |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### assets

Row type `B.Asset[]` · PK (id) · tenant-scoped · 16 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| tag | text NOT NULL |  | string | e.g. SEE-LAP-012 |
| name | text NOT NULL |  | string |  |
| category | text NOT NULL |  | 'laptop' \| 'monitor' \| 'phone' \| 'licence' \| 'furniture' \| 'other | one of: other, laptop, monitor, phone, licence, furniture |
| serial | text NOT NULL |  | string |  |
| status | text NOT NULL |  | 'in_stock' \| 'assigned' \| 'repair' \| 'retired' \| 'lost' | one of: lost, in_stock, assigned, repair, retired |
| assigned_to | text | null | ID \| null |  |
| assigned_on | date | null | ISODate \| null |  |
| purchase_on | date NOT NULL |  | ISODate |  |
| purchase_cost_paise | bigint NOT NULL |  | Paise | money in paise |
| warranty_until | date | null | ISODate \| null |  |
| notes | text NOT NULL |  | string |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### expense_claims

Row type `B.ExpenseClaim[]` · PK (id) · tenant-scoped · 20 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| user_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| project_id | text | null | ID \| null | → projects.id (FK, restrict) |
| category | text NOT NULL |  | 'travel' \| 'food' \| 'lodging' \| 'client_entertainment' \| 'software | one of: other, training, travel, food, lodging, client_entertainment, software, hardware, internet_phone, office |
| description | text NOT NULL |  | string |  |
| amount_paise | bigint NOT NULL |  | Paise | money in paise |
| spent_on | date NOT NULL |  | ISODate |  |
| receipt_file_id | text | null | ID \| null | reference id (no FK) |
| billable_to_client | boolean NOT NULL |  | boolean |  |
| status | text NOT NULL |  | ExpenseStatus | one of: paid, submitted, approved, rejected, draft |
| approver_id | text | null | ID \| null | reference id (no FK) |
| decided_by | text | null | ID \| null |  |
| decided_at | timestamptz | null | ISODateTime \| null |  |
| decision_reason | text | null | string \| null |  |
| paid_on | date | null | ISODate \| null |  |
| ledger_entry_id | text | null | ID \| null | reference id (no FK) |
| in_budget | boolean | null | boolean \| null | computed at approval against budgets |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| first_approved_by | text | null | (untyped) | untyped field written by the shared code |
| submitted_at | timestamptz | absent | (untyped) | untyped field written by the shared code |
| wf_process | text | absent | (untyped) | untyped field written by the shared code |
| wf_chain | jsonb | absent | (untyped) | untyped field written by the shared code |
| wf_step | integer | absent | (untyped) | untyped field written by the shared code |
| wf_step_at | timestamptz | absent | (untyped) | untyped field written by the shared code |

#### vendors

Row type `B.Vendor[]` · PK (id) · tenant-scoped · 6 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| category | text NOT NULL |  | string |  |
| gstin | text NOT NULL |  | string |  |
| pan | text NOT NULL |  | string |  |
| contact | text NOT NULL |  | string |  |
| email | text NOT NULL |  | string |  |
| phone | text NOT NULL |  | string |  |
| payment_terms_days | integer NOT NULL |  | number |  |
| status | text NOT NULL |  | 'active' \| 'blocked' | one of: active, blocked |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| bank_name | text | absent | (untyped) | untyped field written by the shared code |
| bank_account | text | absent | (untyped) | untyped field written by the shared code |
| ifsc | text | absent | (untyped) | untyped field written by the shared code |

#### purchase_orders

Row type `B.PurchaseOrder[]` · PK (id) · tenant-scoped · 6 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| number | text NOT NULL |  | string |  |
| vendor_id | text NOT NULL |  | ID | → vendors.id (FK, restrict) |
| project_id | text | null | ID \| null | → projects.id (FK, restrict) |
| budget_category | text NOT NULL |  | string |  |
| lines | jsonb NOT NULL |  | POLine[] |  |
| amount_paise | bigint NOT NULL |  | Paise | money in paise |
| gst_pct | double precision NOT NULL |  | number |  |
| status | text NOT NULL |  | 'draft' \| 'pending' \| 'approved' \| 'rejected' \| 'closed' | one of: closed, approved, rejected, draft, pending |
| requested_by | text NOT NULL |  | ID |  |
| approver_id | text | null | ID \| null | reference id (no FK) |
| decided_by | text | null | ID \| null |  |
| decided_at | timestamptz | null | ISODateTime \| null |  |
| decision_reason | text | null | string \| null |  |
| needed_by | date | null | ISODate \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| title | text | absent | (untyped) | untyped field written by the shared code |
| over_budget | boolean | absent | (untyped) | untyped field written by the shared code |
| justification | text | null | (untyped) | untyped field written by the shared code |
| budget_remaining_paise | bigint | null | (untyped) | money in paise; untyped field written by the shared code |
| wf_process | text | absent | (untyped) | untyped field written by the shared code |
| wf_chain | jsonb | absent | (untyped) | untyped field written by the shared code |
| wf_step | integer | absent | (untyped) | untyped field written by the shared code |
| wf_step_at | timestamptz | absent | (untyped) | untyped field written by the shared code |

#### vendor_bills

Row type `B.VendorBill[]` · PK (id) · tenant-scoped · 6 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| vendor_id | text NOT NULL |  | ID | → vendors.id (FK, restrict) |
| po_id | text | null | ID \| null | → purchase_orders.id (FK, restrict) |
| number | text NOT NULL |  | string |  |
| bill_date | date NOT NULL |  | ISODate |  |
| due_on | date NOT NULL |  | ISODate |  |
| amount_paise | bigint NOT NULL |  | Paise | money in paise |
| gst_paise | bigint NOT NULL |  | Paise | money in paise |
| tds_paise | bigint NOT NULL |  | Paise | money in paise |
| status | text NOT NULL |  | 'received' \| 'approved' \| 'paid' \| 'disputed' | one of: paid, approved, received, disputed |
| paid_on | date | null | ISODate \| null |  |
| file_id | text | null | ID \| null | reference id (no FK) |
| ledger_entry_id | text | null | ID \| null | reference id (no FK) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| approved_by | text | null | (untyped) | untyped field written by the shared code |
| dispute_reason | text | null | (untyped) | untyped field written by the shared code |
| category | text | null | (untyped) | untyped field written by the shared code |
| description | text | absent | (untyped) | untyped field written by the shared code |
| wf_process | text | absent | (untyped) | untyped field written by the shared code |
| wf_chain | jsonb | absent | (untyped) | untyped field written by the shared code |
| wf_step | integer | absent | (untyped) | untyped field written by the shared code |
| wf_step_at | timestamptz | absent | (untyped) | untyped field written by the shared code |
| approver_id | text | absent | (untyped) | reference id (no FK); untyped field written by the shared code |

#### budgets

Row type `B.Budget[]` · PK (id) · tenant-scoped · 24 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| fy_start_year | integer NOT NULL |  | number |  |
| kind | text NOT NULL |  | 'expense' \| 'revenue' | one of: expense, revenue |
| category | text NOT NULL |  | string | matches ledger categories / expense categories / revenue streams |
| owner_id | text | null | ID \| null | reference id (no FK) |
| monthly_paise | jsonb NOT NULL |  | Paise[] | 12 entries, FY order; money in paise |
| notes | text NOT NULL |  | string |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### clients

Row type `B.Client[]` · PK (id) · tenant-scoped · 11 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| industry | text NOT NULL |  | string |  |
| country | text NOT NULL |  | string |  |
| gstin | text NOT NULL |  | string |  |
| billing_address | text NOT NULL |  | string |  |
| primary_contact | text NOT NULL |  | string |  |
| email | text NOT NULL |  | string |  |
| phone | text NOT NULL |  | string |  |
| owner_id | text | null | ID \| null | account manager; reference id (no FK) |
| status | text NOT NULL |  | 'prospect' \| 'active' \| 'dormant' \| 'churned' | one of: active, prospect, dormant, churned |
| since | date | null | ISODate \| null |  |
| payment_terms_days | integer NOT NULL |  | number |  |
| health | text | null | 'good' \| 'watch' \| 'at_risk' \| null | one of: at_risk, good, watch |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| city | text | absent | (untyped) | untyped field written by the shared code |
| state | text | absent | (untyped) | untyped field written by the shared code |
| state_code | text | absent | (untyped) | untyped field written by the shared code |
| pincode | text | absent | (untyped) | untyped field written by the shared code |
| pan | text | absent | (untyped) | untyped field written by the shared code |
| notes | text | absent | (untyped) | untyped field written by the shared code |
| status_manual | boolean | absent | (untyped) | untyped field written by the shared code |

#### contracts

Row type `B.Contract[]` · PK (id) · tenant-scoped · 10 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| client_id | text NOT NULL |  | ID | → clients.id (FK, restrict) |
| project_id | text | null | ID \| null | → projects.id (FK, restrict) |
| title | text NOT NULL |  | string |  |
| kind | text NOT NULL |  | 'msa' \| 'sow' \| 'amc' \| 'licence' \| 'nda' \| 'retainer' | one of: licence, msa, sow, amc, nda, retainer |
| value_paise | bigint NOT NULL |  | Paise | money in paise |
| start_on | date NOT NULL |  | ISODate |  |
| end_on | date NOT NULL |  | ISODate |  |
| renewal_on | date | null | ISODate \| null |  |
| auto_renew | boolean NOT NULL |  | boolean |  |
| status | text NOT NULL |  | 'draft' \| 'active' \| 'expiring' \| 'expired' \| 'terminated' \| 'ren | one of: active, draft, expiring, expired, terminated, renewed |
| file_id | text | null | ID \| null | reference id (no FK) |
| notes | text NOT NULL |  | string |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |
| reminded_on | date | absent | (untyped) | untyped field written by the shared code |
| renewed_from | text | absent | (untyped) | untyped field written by the shared code |

#### revenue_leakage

Row type `B.RevenueLeakage[]` · PK (id) · tenant-scoped · 5 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| kind | text NOT NULL |  | LeakageKind | one of: lost_deal, write_off, discount, credit_note, penalty, unbilled_hours, scope_creep, bad_debt, churn |
| amount_paise | bigint NOT NULL |  | Paise | money in paise |
| date | date NOT NULL |  | ISODate |  |
| client | text NOT NULL |  | string |  |
| project_id | text | null | ID \| null | → projects.id (FK, restrict) |
| deal_id | text | null | ID \| null | reference id (no FK) |
| reason | text NOT NULL |  | string |  |
| recorded_by | text NOT NULL |  | ID |  |
| recoverable | boolean NOT NULL |  | boolean |  |
| status | text NOT NULL |  | 'open' \| 'recovered' \| 'accepted' | one of: open, recovered, accepted |
| recovered_paise | bigint NOT NULL |  | Paise | money in paise |
| auto | boolean NOT NULL |  | boolean | generated from system data (lost deals, unbilled hours) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### objectives

Row type `B.Objective[]` · PK (id) · tenant-scoped · 15 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| title | text NOT NULL |  | string |  |
| level | text NOT NULL |  | 'company' \| 'team' \| 'person' | one of: company, team, person |
| owner_id | text NOT NULL |  | ID | reference id (no FK) |
| period | text NOT NULL |  | string | e.g. "Q3 FY26-27" |
| period_from | date NOT NULL |  | ISODate |  |
| period_to | date NOT NULL |  | ISODate |  |
| parent_id | text | null | ID \| null | → objectives.id (FK, restrict) |
| status | text NOT NULL |  | 'on_track' \| 'at_risk' \| 'off_track' \| 'done' | one of: done, on_track, at_risk, off_track |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### key_results

Row type `B.KeyResult[]` · PK (id) · tenant-scoped · 30 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| objective_id | text NOT NULL |  | ID | → objectives.id (FK, cascade) |
| title | text NOT NULL |  | string |  |
| metric | text NOT NULL |  | 'manual' \| 'revenue_billed' \| 'revenue_collected' \| 'deals_won_valu | one of: manual, revenue_billed, revenue_collected, deals_won_value, on_time_task_pct, timesheet_compliance, margin_pct, headcount, leakage |
| start_value | double precision NOT NULL |  | number |  |
| target | double precision NOT NULL |  | number |  |
| current | double precision NOT NULL |  | number |  |
| unit | text NOT NULL |  | string |  |
| owner_id | text NOT NULL |  | ID | reference id (no FK) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### data_exports

Row type `E.DataExport[]` · PK (id) · tenant-scoped · 0 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| requested_by | text NOT NULL |  | ID |  |
| status | text NOT NULL |  | 'queued' \| 'ready' \| 'expired' | one of: expired, queued, ready |
| ready_at | timestamptz | null | ISODateTime \| null |  |
| expires_at | timestamptz | null | ISODateTime \| null |  |
| size_bytes | integer NOT NULL |  | number |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### number_sequences

Row type `{ tenant_id: string; scope: string; next_value: number }[]` · PK (tenant_id, scope) · tenant-scoped · 12 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| tenant_id | text NOT NULL |  | string | Owning tenant (RLS key) |
| scope | text NOT NULL |  | string |  |
| next_value | integer NOT NULL |  | number |  |

#### admin_audit

Row type `{ id: string; actor_id: string; tenant_id: string; action: string; reason: string; at: string }[]` · PK (id) · tenant-scoped · 0 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| id | text NOT NULL |  | string | Primary key (opaque string; generated by the server) |
| actor_id | text NOT NULL |  | string | reference id (no FK) |
| tenant_id | text NOT NULL |  | string | Owning tenant (RLS key) |
| action | text NOT NULL |  | string |  |
| reason | text NOT NULL |  | string |  |
| at | timestamptz NOT NULL |  | string |  |

#### roles

Row type `A.RoleDef[]` · PK (id) · tenant-scoped · 36 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| key | text NOT NULL |  | string | stable slug, e.g. "hr_manager" |
| name | text NOT NULL |  | string | "HR Manager" |
| description | text NOT NULL |  | string |  |
| persona | text NOT NULL |  | Role | which home dashboard and default data views the role gets; one of: admin, pm, sales, ops, employee |
| permissions | text[] NOT NULL |  | string[] | permission keys from domain/permissionCatalog |
| is_system | boolean NOT NULL |  | boolean | shipped default (can be edited, not deleted) |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### change_sets

Row type `A.ChangeSet[]` · PK (id) · tenant-scoped · 1 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| entity | text NOT NULL |  | ChangeEntity | one of: employee, role, user_access, cost_rate, workflow, org_profile, master_record, shift, vendor, budget, payroll_finalise, settings, makerchecker_config |
| entity_id | text | null | ID \| null | null for creates; reference id (no FK) |
| action | text NOT NULL |  | 'create' \| 'update' \| 'delete' \| 'execute' | one of: create, update, delete, execute |
| summary | text NOT NULL |  | string | human sentence: "Change HR Executive permissions: +expenses.pay" |
| before | jsonb | null | Record<string, unknown> \| null |  |
| after | jsonb | null | Record<string, unknown> \| null |  |
| payload | jsonb NOT NULL |  | Record<string, unknown> | what the applier needs to perform the change |
| maker_id | text NOT NULL |  | ID | reference id (no FK) |
| required_permission | text NOT NULL |  | string | The checker must also hold this permission (you can only check what you could have done). |
| base_version | integer | null | number \| null | Version of the target when the maker proposed it; a newer version at approval time fails the change. |
| status | text NOT NULL |  | 'pending' \| 'approved' \| 'rejected' \| 'withdrawn' \| 'failed' | one of: failed, approved, rejected, pending, withdrawn |
| checker_id | text | null | ID \| null | reference id (no FK) |
| decided_at | timestamptz | null | ISODateTime \| null |  |
| reason | text | null | string \| null | rejection reason (required) or approval note |
| applied_at | timestamptz | null | ISODateTime \| null |  |
| result_id | text | null | ID \| null | id of the record created/updated on apply; reference id (no FK) |
| error | text | null | string \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### approval_workflows

Row type `A.ApprovalWorkflow[]` · PK (id) · tenant-scoped · 24 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| process | text NOT NULL |  | WorkflowProcess | one of: leave, timesheet, expense, purchase_order, vendor_bill, task_review |
| name | text NOT NULL |  | string |  |
| description | text NOT NULL |  | string |  |
| active | boolean NOT NULL |  | boolean |  |
| steps | jsonb NOT NULL |  | WorkflowStep[] |  |
| require_reason_on_reject | boolean NOT NULL |  | boolean |  |
| notify_requester | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### locations

Row type `A.Location[]` · PK (id) · tenant-scoped · 5 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| code | text NOT NULL |  | string |  |
| address | text NOT NULL |  | string |  |
| city | text NOT NULL |  | string |  |
| state | text NOT NULL |  | string |  |
| timezone | text NOT NULL |  | string |  |
| holiday_calendar | text NOT NULL |  | string | name; holidays table is tenant-wide in v1 |
| is_head_office | boolean NOT NULL |  | boolean |  |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### departments

Row type `A.Department[]` · PK (id) · tenant-scoped · 17 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| code | text NOT NULL |  | string |  |
| head_id | text | null | ID \| null | → users.id (FK, restrict) |
| parent_id | text | null | ID \| null | → departments.id (FK, restrict) |
| cost_centre_id | text | null | ID \| null | → cost_centres.id (FK, restrict) |
| kind | text NOT NULL |  | 'management' \| 'delivery' \| 'sales' \| 'operations' | maps to User.department for analytics; one of: sales, management, delivery, operations |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### designations

Row type `A.Designation[]` · PK (id) · tenant-scoped · 27 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| department_id | text | null | ID \| null | → departments.id (FK, restrict) |
| grade_id | text | null | ID \| null | → grades.id (FK, restrict) |
| default_role_id | text | null | ID \| null | reference id (no FK) |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### grades

Row type `A.Grade[]` · PK (id) · tenant-scoped · 21 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| code | text NOT NULL |  | string | L1…L7 |
| name | text NOT NULL |  | string |  |
| min_ctc_paise | bigint | null | Paise \| null | money in paise |
| max_ctc_paise | bigint | null | Paise \| null | money in paise |
| leave_multiplier | double precision NOT NULL |  | number |  |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### cost_centres

Row type `A.CostCentre[]` · PK (id) · tenant-scoped · 17 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| code | text NOT NULL |  | string |  |
| name | text NOT NULL |  | string |  |
| owner_id | text | null | ID \| null | → users.id (FK, restrict) |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### shifts

Row type `A.Shift[]` · PK (id) · tenant-scoped · 5 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| code | text NOT NULL |  | string |  |
| start_time | text NOT NULL |  | string | "09:30" |
| end_time | text NOT NULL |  | string | "18:30" |
| break_minutes | integer NOT NULL |  | number |  |
| grace_minutes | integer NOT NULL |  | number |  |
| working_days | jsonb NOT NULL |  | number[] | 0=Sun..6=Sat |
| night_shift | boolean NOT NULL |  | boolean |  |
| min_hours | double precision NOT NULL |  | number |  |
| is_default | boolean NOT NULL |  | boolean |  |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### email_templates

Row type `A.EmailTemplate[]` · PK (id) · tenant-scoped · 36 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| key | text NOT NULL |  | 'welcome' \| 'password_reset' \| 'leave_decision' \| 'expense_decision | one of: welcome, password_reset, leave_decision, expense_decision, task_assigned, payslip, appraisal_completed, appraisal_letter, custom |
| name | text NOT NULL |  | string |  |
| subject | text NOT NULL |  | string |  |
| body | text NOT NULL |  | string | supports {{employee_name}}, {{company}}, {{login_url}}, {{username}}, {{temp_password}}, {{manager_name}}, {{joining_date}}… |
| active | boolean NOT NULL |  | boolean |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### email_outbox

Row type `A.EmailMessage[]` · PK (id) · tenant-scoped · 4 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| to | text NOT NULL |  | string |  |
| to_user_id | text | null | ID \| null | reference id (no FK) |
| subject | text NOT NULL |  | string |  |
| body | text NOT NULL |  | string |  |
| template_key | text NOT NULL |  | string |  |
| related_entity | text | null | string \| null |  |
| related_id | text | null | ID \| null | reference id (no FK) |
| status | text NOT NULL |  | 'queued' \| 'sent' \| 'failed' | one of: queued, sent, failed (V008: written `queued`, set by the worker) |
| sent_at | timestamptz | null | ISODateTime \| null | V008: NULL until delivered |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### cost_estimates

Row type `V7.CostEstimate[]` · PK (id) · tenant-scoped · 5 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| client | text NOT NULL |  | string |  |
| deal_id | text | null | ID \| null | prospect in the pipeline (optional); reference id (no FK) |
| project_id | text | null | ID \| null | set once converted into a project; → projects.id (FK, restrict) |
| status | text NOT NULL |  | 'draft' \| 'shared' \| 'won' \| 'lost' \| 'converted' | one of: draft, won, lost, shared, converted |
| start_on | date NOT NULL |  | ISODate |  |
| end_on | date NOT NULL |  | ISODate |  |
| type | text NOT NULL |  | string |  |
| billing_model | text NOT NULL |  | string |  |
| milestones | jsonb NOT NULL |  | { key: string; name: string; billing_amount_paise: Paise; start_on: IS |  |
| resources | jsonb NOT NULL |  | { id: string; user_id: ID \| null; role_label: string \| null; monthly | user_id = a named person (CTC from cost rates); otherwise role_label + an assumed monthly CTC (not hired yet). from_on / to_on null = follow the milestone's dates. |
| additional_costs | jsonb NOT NULL |  | { id: string; category: string; description: string; amount_paise: Pai |  |
| maintenance_paise | bigint NOT NULL |  | Paise | money in paise |
| contingency_pct | double precision NOT NULL |  | number |  |
| markup_pct | double precision | null | number \| null | null only in responses redacted for viewers without salary access |
| price_paise | bigint NOT NULL |  | Paise | money in paise |
| notes | text NOT NULL |  | string |  |
| owner_id | text NOT NULL |  | ID | reference id (no FK) |
| number | text | absent | ?: string | EST-001 |
| client_id | text | null | ?: ID \| null | picked from the clients master (client keeps the display name); → clients.id (FK, restrict) |
| stream | text | null | ?: string \| null | revenue stream suggested for the project |
| description | text | absent | ?: string |  |
| scenario_of | text | null | ?: ID \| null | root estimate this scenario was duplicated from |
| scenario_label | text | null | ?: string \| null | e.g. "Scenario B" |
| shared_on | date | null | ?: ISODate \| null |  |
| decided_on | date | null | ?: ISODate \| null | won / lost |
| lost_reason | text | null | ?: string \| null |  |
| converted_on | date | null | ?: ISODate \| null |  |
| revisions | jsonb | absent | ?: { version: number; at: ISODateTime; by: ID; note: string; price_pai | one entry per save: what the numbers were at that version (internal; never sent to viewers without estimates.manage) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | absent | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### wf_templates

Row type `V7.WfTemplate[]` · PK (id) · tenant-scoped · 24 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| name | text NOT NULL |  | string |  |
| process | text NOT NULL |  | WfProcess | one of: general, leave, expense, onboarding, appraisal, exit, confirmation, promotion, document_approval |
| description | text NOT NULL |  | string |  |
| active | boolean NOT NULL |  | boolean |  |
| stages | jsonb NOT NULL |  | WfStage[] |  |
| on_complete | jsonb NOT NULL |  | WfAction[] |  |
| on_reject | jsonb NOT NULL |  | WfAction[] |  |
| rev | integer | absent | ?: number | structural revision; bumps on every saved change. Running instances keep the revision they started with. |
| key | text | null | ?: string \| null | stable slug for built-in templates (e.g. annual_appraisal) |
| self_service | boolean | absent | ?: boolean | employees may start it about themselves without workflows.start |
| updated_by | text | absent | ?: ID \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### wf_instances

Row type `V7.WfInstance[]` · PK (id) · tenant-scoped · 4 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| template_id | text NOT NULL |  | ID | → wf_templates.id (FK, restrict) |
| title | text NOT NULL |  | string |  |
| subject_user_id | text | null | ID \| null | the employee the workflow is about (e.g. appraisee); reference id (no FK) |
| initiated_by | text NOT NULL |  | ID |  |
| status | text NOT NULL |  | 'in_progress' \| 'completed' \| 'rejected' \| 'cancelled' | one of: cancelled, completed, in_progress, rejected |
| current_stage | integer NOT NULL |  | number |  |
| current_assignee_id | text | null | ID \| null | reference id (no FK) |
| stage_due_on | date | null | ISODate \| null |  |
| values | jsonb NOT NULL |  | Record<string, Record<string, unknown>> | stage key → field key → value |
| history | jsonb NOT NULL |  | { stage_key: string; by: ID; action: 'submitted' \| 'approved' \| 'sen |  |
| context | jsonb NOT NULL |  | Record<string, unknown> | e.g. { cycle_id, appraisal_id, current_ctc_paise } |
| outputs | jsonb NOT NULL |  | { kind: string; ref_id: ID; label: string }[] | generated letters, emails, rate changes |
| template_rev | integer | absent | ?: number |  |
| snapshot | jsonb | absent | ?: WfSnapshot | the template as it was when the instance started |
| stage_started_at | timestamptz | absent | ?: ISODateTime \| null |  |
| escalated_stage | integer | null | ?: number \| null | stage index already escalated for SLA |
| comments | jsonb | absent | ?: { id: string; by: ID; at: ISODateTime; body: string }[] |  |
| completed_at | timestamptz | null | ?: ISODateTime \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### doc_templates

Row type `V7.DocTemplate[]` · PK (id) · tenant-scoped · 44 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| key | text NOT NULL |  | string | stable slug e.g. offer_letter |
| name | text NOT NULL |  | string |  |
| category | text NOT NULL |  | 'payslip' \| 'offer_letter' \| 'joining_letter' \| 'appraisal_letter'  | one of: other, policy, nda, payslip, appraisal_letter, offer_letter, joining_letter, relieving_letter, experience_letter, confirmation_letter, warning_letter |
| body | text NOT NULL |  | string | rich text with {{placeholders}} |
| placeholders | text[] NOT NULL |  | string[] |  |
| letterhead | boolean NOT NULL |  | boolean |  |
| active | boolean NOT NULL |  | boolean |  |
| description | text | absent | ?: string | v7 documents: optional extras |
| custom_placeholders | text[] | absent | ?: string[] | issuer-filled placeholders the author declared beyond the catalogue |
| updated_by | text | null | ?: ID \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### issued_documents

Row type `V7.IssuedDocument[]` · PK (id) · tenant-scoped · 25 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| template_id | text | null | ID \| null | → doc_templates.id (FK, restrict) |
| category | text NOT NULL |  | DocTemplate['category'] | one of: other, policy, nda, payslip, appraisal_letter, offer_letter, joining_letter, relieving_letter, experience_letter, confirmation_letter, warning_letter |
| title | text NOT NULL |  | string |  |
| employee_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| body_html | text NOT NULL |  | string | rendered content |
| period | text | null | string \| null | e.g. payslip month "2026-09" |
| status | text NOT NULL |  | 'draft' \| 'issued' \| 'acknowledged' \| 'revoked' | one of: draft, issued, acknowledged, revoked |
| issued_by | text NOT NULL |  | ID |  |
| issued_on | date | null | ISODate \| null |  |
| acknowledged_on | date | null | ISODate \| null |  |
| source | jsonb NOT NULL |  | { kind: 'manual' \| 'payroll' \| 'workflow' \| 'onboarding'; ref_id: I |  |
| ref_no | text | absent | ?: string | v7 documents: optional extras e.g. SCI/HR/2026/0012 |
| body_md | text | absent | ?: string | rendered markup (placeholders filled) — the PDF renderer lays this out |
| vars | jsonb | absent | ?: Record<string, string> | values used to render (re-issue / edit draft) |
| letterhead | boolean | absent | ?: boolean |  |
| revoked_on | date | null | ?: ISODate \| null |  |
| revoked_by | text | null | ?: ID \| null |  |
| revoke_reason | text | null | ?: string \| null |  |
| replaces_id | text | null | ?: ID \| null | re-issued from this document; reference id (no FK) |
| replaced_by_id | text | null | ?: ID \| null | reference id (no FK) |
| emailed_to | text | null | ?: string \| null |  |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

#### employee_documents

Row type `V7.EmployeeDocument[]` · PK (id) · tenant-scoped · 45 seed rows

| column | type | null→ | TS type | meaning |
|---|---|---|---|---|
| employee_id | text NOT NULL |  | ID | → users.id (FK, restrict) |
| kind | text NOT NULL |  | 'id_proof' \| 'address_proof' \| 'pan' \| 'aadhaar' \| 'passport' \| ' | one of: other, id_proof, address_proof, pan, aadhaar, passport, education, experience, bank, photo |
| title | text NOT NULL |  | string |  |
| file_id | text | null | ID \| null | reference id (no FK) |
| uploaded_by | text NOT NULL |  | ID |  |
| verified_by | text | null | ID \| null |  |
| verified_on | date | null | ISODate \| null |  |
| status | text NOT NULL |  | 'pending' \| 'verified' \| 'rejected' | one of: rejected, pending, verified |
| note | text NOT NULL |  | string |  |
| expires_on | date | null | ISODate \| null |  |
| doc_number | text | null | ?: string \| null | v7 documents: optional extras e.g. PAN / passport number (masked in lists) |
| file_name | text | absent | ?: string |  |
| mime | text | absent | ?: string |  |
| size_bytes | integer | absent | ?: number |  |
| uploaded_on | date | absent | ?: ISODate |  |
| review_note | text | null | ?: string \| null | verifier's note (reason for rejection) |
| id | text NOT NULL |  | ID | Primary key (opaque string; generated by the server) |
| tenant_id | text NOT NULL |  | ID | Owning tenant (RLS key) |
| created_at | timestamptz NOT NULL |  | ISODateTime | Created (set by the app) |
| updated_at | timestamptz NOT NULL |  | ISODateTime | Last changed (set by the app on every update) |
| created_by | text | null | ?: ID \| null | User who created the row (audit; no FK) |
| version | integer NOT NULL |  | number | Optimistic-concurrency version (If-Match), +1 per update |

<!-- GENERATED:end -->
