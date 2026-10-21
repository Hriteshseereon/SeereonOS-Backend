-- V006: security hardening (B5). Hand-written.
--  1. pending_tokens gains two kinds for real two-step verification:
--       two_factor  the user's active TOTP factor: payload {secret (AES-256-GCM sealed), salt, recovery (SHA-256
--                   hashes of the unused single-use recovery codes), last_step (replay guard), fails, enabled_at};
--                   expires_at = 9999-12-31 (does not expire)
--       mfa_login   a sign-in that passed the password and waits for the code: id = SHA-256 of the mfa_token,
--                   payload {attempts}; 5 minutes, burnt after 5 wrong codes, deleted when used
--  2. rate_limits: fixed-window counters shared by every API instance (auth endpoints). Keys are SHA-256 hashes
--     (no e-mail addresses or IPs at rest). Rows expire with their window; the API deletes expired rows every minute.
--  3. security_events: append-only audit trail of authentication events (sign-in success/failure, 2FA, password,
--     sessions, rate-limit blocks). Platform table (no RLS, owner role only); retention enforced by the API.

ALTER TABLE pending_tokens DROP CONSTRAINT pending_tokens_kind_chk;
ALTER TABLE pending_tokens ADD CONSTRAINT pending_tokens_kind_chk CHECK (kind IN ('two_factor_setup', 'two_factor', 'mfa_login', 'wbs_import'));
COMMENT ON COLUMN pending_tokens.kind IS '''two_factor_setup'' | ''two_factor'' | ''mfa_login'' | ''wbs_import''';
UPDATE app_column_meta SET ts_type = '''two_factor_setup'' | ''two_factor'' | ''mfa_login'' | ''wbs_import''' WHERE table_name = 'pending_tokens' AND column_name = 'kind';
-- (no unique index on the active factor per user: the Unit of Work may apply a replace as insert-then-delete in one
-- transaction; the shared logic removes the old row before inserting the new one, and lookups use
-- pending_tokens_tenant_user_kind_ix from V005)

CREATE TABLE rate_limits (
  bucket        text        NOT NULL,          -- sha256(limit name + key), hex
  window_start  timestamptz NOT NULL,
  hits          integer     NOT NULL,
  expires_at    timestamptz NOT NULL,          -- window end
  PRIMARY KEY (bucket, window_start)
);
CREATE INDEX rate_limits_expires_ix ON rate_limits (expires_at);
COMMENT ON TABLE rate_limits IS 'Fixed-window rate-limit counters shared across API instances (auth endpoints)';

CREATE TABLE security_events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  event       text        NOT NULL,            -- login.success, login.failure, mfa.enabled, password.change, …
  outcome     text        NOT NULL CHECK (outcome IN ('success', 'failure', 'blocked', 'info')),
  tenant_id   text,
  user_id     text,
  email       text,                            -- as typed at sign-in (lower-cased); null when unknown
  ip          text,
  user_agent  text,
  request_id  text,
  detail      jsonb
);
CREATE INDEX security_events_tenant_at_ix ON security_events (tenant_id, at DESC);
CREATE INDEX security_events_user_at_ix ON security_events (user_id, at DESC);
CREATE INDEX security_events_event_at_ix ON security_events (event, at DESC);
CREATE INDEX security_events_at_ix ON security_events (at);
COMMENT ON TABLE security_events IS 'Authentication audit trail (append-only for the API; retention SECURITY_EVENTS_RETENTION_DAYS)';
-- seereon_app gets no privileges on either table (they are written through the owner pool)
