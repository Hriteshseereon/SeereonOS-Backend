-- V001: platform, security and runtime-support tables (not plain mock tables). Hand-written.
-- Owned by seereon_owner. None of these has RLS: seereon_app gets no privileges on the auth tables
-- (they are used through the owner pool by the auth/session layer), and read-only access to the
-- cache-version and metadata tables (granted in V004).

-- Applied migrations (also created by migrate.ts before it runs V001; kept here so the schema is self-describing).
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     text PRIMARY KEY,           -- file name, e.g. V001__platform.sql
  checksum    text NOT NULL,              -- sha256 of the file contents (hex)
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- Per-tenant, per-table change counter. Bumped by the statement-level triggers of V004 (one bump per statement
-- per tenant touched). The API keeps a per-process cache of tenant tables and reuses an entry only while its
-- version is unchanged; a flush verifies the versions of every table it read before committing.
CREATE TABLE tenant_table_versions (
  tenant_id   text   NOT NULL,
  table_name  text   NOT NULL,
  version     bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, table_name)
);

-- Row-mapping metadata for server/db/mapping.ts: how each API column maps between PostgreSQL and the JS row.
--   kind     text | date | timestamp | int | bigint | float | bool | json | text_array
--   null_as  'null'   SQL NULL is returned as null
--            'absent' SQL NULL means the key is absent from the JS row (field declared `x?: T`)
--            (per-row exceptions are listed in the row's hidden _null_override column)
CREATE TABLE app_column_meta (
  table_name   text    NOT NULL,
  column_name  text    NOT NULL,
  ordinal      integer NOT NULL,
  kind         text    NOT NULL CHECK (kind IN ('text', 'date', 'timestamp', 'int', 'bigint', 'float', 'bool', 'json', 'text_array')),
  null_as      text    NOT NULL CHECK (null_as IN ('null', 'absent')),
  pg_type      text    NOT NULL,
  not_null     boolean NOT NULL,
  ts_type      text,                       -- declared TypeScript type (NULL = untyped field written by the code)
  PRIMARY KEY (table_name, column_name)
);
CREATE TABLE app_table_meta (
  table_name     text    PRIMARY KEY,
  tenant_scoped  boolean NOT NULL,         -- false only for tenants (keyed by id)
  pk             text[]  NOT NULL
);

-- Credentials (the mock's `passwords` table). Format: scrypt$N$r$p$salt_b64$hash_b64 (N=32768, r=8, p=1, 64-byte key).
CREATE TABLE user_credentials (
  user_id              text PRIMARY KEY,
  password_hash        text NOT NULL,
  password_algo        text NOT NULL DEFAULT 'scrypt' CHECK (password_algo IN ('scrypt')),
  password_changed_at  timestamptz NOT NULL DEFAULT now()
);

-- Opaque session tokens: only SHA-256(token) is stored. Idle and absolute expiry are enforced by the API.
CREATE TABLE sessions (
  token_hash    text PRIMARY KEY,          -- hex sha256 of the bearer token
  user_id       text NOT NULL,
  tenant_id     text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,      -- absolute expiry
  revoked_at    timestamptz,
  ip            text,
  user_agent    text
);
CREATE INDEX sessions_user_ix ON sessions (user_id) WHERE revoked_at IS NULL;   -- revoke-all on logout-everywhere / password change / deactivation
CREATE INDEX sessions_expires_ix ON sessions (expires_at);                     -- purge job

-- Login attempts for the lockout rule (security.lockout_attempts within security.lockout_minutes).
CREATE TABLE login_attempts (
  id         bigserial PRIMARY KEY,
  email      text NOT NULL,                -- lower-cased
  at         timestamptz NOT NULL DEFAULT now(),
  ip         text,
  succeeded  boolean NOT NULL DEFAULT false
);
CREATE INDEX login_attempts_email_at_ix ON login_attempts (email, at DESC);

-- Password-reset tokens (hash only).
CREATE TABLE reset_tokens (
  token_hash  text PRIMARY KEY,
  user_id     text NOT NULL,
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz
);
CREATE INDEX reset_tokens_user_ix ON reset_tokens (user_id);

-- Idempotency-Key replay store for POST (24 h).
CREATE TABLE idempotency_keys (
  tenant_id   text NOT NULL,
  key         text NOT NULL,
  method      text NOT NULL,
  path        text NOT NULL,
  status      integer NOT NULL,
  body        jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);
CREATE INDEX idempotency_keys_created_ix ON idempotency_keys (created_at);       -- 24 h purge

-- SMTP delivery state for email_outbox messages (the outbox row itself stays exactly as the mock writes it).
CREATE TABLE email_deliveries (
  message_id  text PRIMARY KEY,            -- email_outbox.id
  tenant_id   text NOT NULL,
  status      text NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
  attempts    integer NOT NULL DEFAULT 0,
  last_error  text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_deliveries_pending_ix ON email_deliveries (updated_at) WHERE status = 'pending';
