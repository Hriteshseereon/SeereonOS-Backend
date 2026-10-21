-- V008: production hardening (v8). Hand-written and reviewed; never edit after it is applied — add V009.
--
-- 1. ONE runtime role. The API (and the worker, and the production CLI) connect with ONE connection string
--    (DATABASE_URL) as seereon_app: NOSUPERUSER, NOBYPASSRLS, no DDL, RLS enforced on every tenant table.
--    Everything that used to need the owner pool at runtime goes through the SECURITY DEFINER functions below
--    (owner: seereon_owner, the migration role; `SET search_path = pg_catalog, public`; EXECUTE revoked from PUBLIC
--    and granted to seereon_app only; every argument is a bound parameter — no dynamic SQL). The credential and
--    audit tables (user_credentials, sessions, reset_tokens, login_attempts, rate_limits, security_events,
--    idempotency_keys, email_deliveries) stay WITHOUT any privilege for seereon_app: the role can only call the
--    narrow operations, e.g. it can append to the audit trail but never read, change or delete it, and it can set the
--    password of a user of the tenant of the current transaction, but never list password hashes in bulk.
--    Tenant-scoped writes (credentials, sessions, reset tokens) check that the user belongs to
--    current_setting('app.tenant_id') — or that the platform scope (app.platform_admin = 'on', entered by the runtime
--    only for verified super-admin sessions) is active. See backend/SECURITY.md §7 "One database role".
-- 2. email_deliveries becomes the delivery queue of the worker (python -m app.worker): exclusive claims with
--    FOR UPDATE SKIP LOCKED and a lease, retries with exponential back-off, dead-letter after N attempts, and
--    platform messages (password reset links, invitations) whose body is sealed with AES-256-GCM by the API and
--    scrubbed after delivery or expiry.
-- 3. worker_heartbeats / job_runs: liveness and last run of every scheduled job (/readyz, /metrics).
-- 4. payment_orders / payment_events: Razorpay orders created by the server and verified webhook events
--    (idempotent by event id), tenant tables with the same conventions as V002–V004 (RLS, cache versions, meta).
-- 5. table_changes: seereon_app writes its own flush log rows (INSERT under RLS); the purge runs in seereon_cleanup().

-- ---------------------------------------------------------------------------------------------------------------------
-- helpers (internal: not executable by seereon_app)
-- ---------------------------------------------------------------------------------------------------------------------
CREATE FUNCTION seereon_platform_scope() RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
  SELECT coalesce(current_setting('app.platform_admin', true), '') = 'on'
$$;

CREATE FUNCTION seereon_in_tenant_scope(p_tenant text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
  SELECT seereon_platform_scope() OR (p_tenant IS NOT NULL AND p_tenant = current_setting('app.tenant_id', true))
$$;

-- the user exists and belongs to the tenant of the current transaction (or the platform scope is active)
CREATE FUNCTION seereon_user_in_scope(p_user text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT EXISTS (SELECT 1 FROM users u WHERE u.id = p_user AND seereon_in_tenant_scope(u.tenant_id))
$$;

CREATE FUNCTION seereon_require_user_in_scope(p_user text) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF NOT seereon_user_in_scope(p_user) THEN
    RAISE EXCEPTION 'user % is not in the tenant of this transaction', p_user USING ERRCODE = '42501';
  END IF;
END
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- sign-in lookups (before the tenant is known)
-- ---------------------------------------------------------------------------------------------------------------------
-- the session of a bearer token (SHA-256 hex), touched at most once a minute; NULL row set when invalid/expired
CREATE FUNCTION seereon_resolve_session(p_hash text, p_idle_hours integer)
RETURNS TABLE (token_hash text, user_id text, tenant_id text, is_super_admin boolean)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH s AS (
    SELECT x.token_hash, x.user_id, x.tenant_id, x.last_seen_at FROM sessions x
     WHERE x.token_hash = p_hash AND x.revoked_at IS NULL AND x.expires_at > now()
       AND x.last_seen_at > now() - make_interval(hours => p_idle_hours)
  ), touch AS (
    UPDATE sessions t SET last_seen_at = now()
     WHERE t.token_hash = p_hash AND EXISTS (SELECT 1 FROM s WHERE s.last_seen_at < now() - interval '60 seconds')
  )
  SELECT s.token_hash, s.user_id, s.tenant_id, coalesce(u.is_super_admin, false) FROM s LEFT JOIN users u ON u.id = s.user_id
$$;

-- login / reset request: which tenant an e-mail belongs to (e-mails are globally unique, users_email_lower_uk)
CREATE FUNCTION seereon_user_by_email(p_email text) RETURNS TABLE (id text, tenant_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT u.id, u.tenant_id FROM users u WHERE lower(u.email) = lower(p_email) LIMIT 1
$$;

-- cross-tenant e-mail uniqueness checks of the shared code (sign-up, create employee, invites): the user rows of OTHER
-- tenants with exactly these e-mails (at most 500)
CREATE FUNCTION seereon_users_by_email(p_emails text[], p_except_tenant text) RETURNS SETOF users
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT u.* FROM users u
   WHERE lower(u.email) = ANY (p_emails[1:500]) AND u.tenant_id IS DISTINCT FROM p_except_tenant
   ORDER BY u._ord
$$;

-- sign-up / create-tenant: the workspace slug, made unique across ALL tenants (acme, acme-2, acme-3 …); the new
-- tenant cannot see the others under RLS
CREATE FUNCTION seereon_free_slug(p_base text) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  b text := left(coalesce(nullif(p_base, ''), 'workspace'), 30);
  s text := b;
  n integer := 1;
BEGIN
  WHILE EXISTS (SELECT 1 FROM tenants t WHERE t.slug = s) LOOP
    n := n + 1;
    s := left(b, 30 - length(n::text) - 1) || '-' || n::text;
  END LOOP;
  RETURN s;
END
$$;

CREATE FUNCTION seereon_invite_tenant(p_token text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.tenant_id FROM invites i WHERE i.token = p_token LIMIT 1
$$;

CREATE FUNCTION seereon_reset_token_tenant(p_hash text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT u.tenant_id FROM reset_tokens r JOIN users u ON u.id = r.user_id WHERE r.token_hash = p_hash
$$;

-- the user/tenant of a pending second-step challenge (id = SHA-256 of the mfa_token), for the audit trail
CREATE FUNCTION seereon_mfa_owner(p_id text) RETURNS TABLE (user_id text, tenant_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.user_id, p.tenant_id FROM pending_tokens p WHERE p.id = p_id AND p.kind = 'mfa_login'
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- the request's virtual platform tables (app.db.uow): reads
-- ---------------------------------------------------------------------------------------------------------------------
-- password hashes of the listed users of ONE tenant (p_tenant NULL: any tenant, platform scope only)
CREATE FUNCTION seereon_password_hashes(p_tenant text, p_users text[]) RETURNS TABLE (user_id text, password_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.user_id, c.password_hash FROM user_credentials c JOIN users u ON u.id = c.user_id
   WHERE c.user_id = ANY (p_users[1:1000])
     AND ((p_tenant IS NOT NULL AND u.tenant_id = p_tenant) OR (p_tenant IS NULL AND seereon_platform_scope()))
$$;

-- live sessions of a tenant (p_tenant NULL: every tenant, platform scope only); token = SHA-256 of the bearer token
CREATE FUNCTION seereon_sessions(p_tenant text, p_idle_hours integer)
RETURNS TABLE (token_hash text, user_id text, tenant_id text, created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT s.token_hash, s.user_id, s.tenant_id, s.created_at FROM sessions s
   WHERE s.revoked_at IS NULL AND s.expires_at > now() AND s.last_seen_at > now() - make_interval(hours => p_idle_hours)
     AND ((p_tenant IS NOT NULL AND s.tenant_id = p_tenant) OR (p_tenant IS NULL AND seereon_platform_scope()))
   ORDER BY s.created_at
$$;

CREATE FUNCTION seereon_reset_tokens(p_tenant text)
RETURNS TABLE (token_hash text, user_id text, expires_ms bigint, used boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT r.token_hash, r.user_id, (extract(epoch FROM r.expires_at) * 1000)::bigint, r.used_at IS NOT NULL
    FROM reset_tokens r JOIN users u ON u.id = r.user_id
   WHERE r.expires_at > now() - interval '1 day'
     AND ((p_tenant IS NOT NULL AND u.tenant_id = p_tenant) OR (p_tenant IS NULL AND seereon_platform_scope()))
$$;

-- failed sign-ins since the last success (lockout rule), last 7 days
CREATE FUNCTION seereon_login_attempts(p_emails text[]) RETURNS TABLE (email text, at_ms bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT a.email, (extract(epoch FROM a.at) * 1000)::bigint FROM login_attempts a
   WHERE a.email = ANY (p_emails[1:500]) AND NOT a.succeeded AND a.at > now() - interval '7 days'
     AND a.at > coalesce((SELECT max(s.at) FROM login_attempts s WHERE s.email = a.email AND s.succeeded), '-infinity')
   ORDER BY a.at, a.id
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- the request's virtual platform tables: writes (called inside the flush transaction, after the tenant rows)
-- ---------------------------------------------------------------------------------------------------------------------
CREATE FUNCTION seereon_set_password(p_user text, p_hash text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM seereon_require_user_in_scope(p_user);
  IF p_hash IS NULL OR p_hash !~ '^scrypt\$[0-9]+\$[0-9]+\$[0-9]+\$[A-Za-z0-9+/=_-]+\$[A-Za-z0-9+/=_-]+$' THEN
    RAISE EXCEPTION 'not a scrypt password hash' USING ERRCODE = '22023';
  END IF;
  INSERT INTO user_credentials (user_id, password_hash, password_algo, password_changed_at) VALUES (p_user, p_hash, 'scrypt', now())
  ON CONFLICT (user_id) DO UPDATE SET password_hash = EXCLUDED.password_hash, password_algo = 'scrypt', password_changed_at = now();
END
$$;

CREATE FUNCTION seereon_delete_passwords(p_users text[]) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  DELETE FROM user_credentials c WHERE c.user_id = ANY (p_users) AND seereon_user_in_scope(c.user_id)
$$;

CREATE FUNCTION seereon_create_session(
  p_hash text, p_user text, p_tenant text, p_created_at timestamptz, p_absolute_days integer, p_ip text, p_user_agent text
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM seereon_require_user_in_scope(p_user);
  IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p_user AND u.tenant_id = p_tenant) THEN
    RAISE EXCEPTION 'session tenant does not match the user' USING ERRCODE = '42501';
  END IF;
  IF p_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'session token must be stored as a SHA-256 hex digest' USING ERRCODE = '22023';
  END IF;
  INSERT INTO sessions (token_hash, user_id, tenant_id, created_at, last_seen_at, expires_at, ip, user_agent)
  VALUES (p_hash, p_user, p_tenant, p_created_at, now(), p_created_at + make_interval(days => p_absolute_days), left(p_ip, 64), left(p_user_agent, 400));
END
$$;

CREATE FUNCTION seereon_revoke_sessions(p_hashes text[]) RETURNS integer
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH r AS (
    UPDATE sessions s SET revoked_at = now()
     WHERE s.token_hash = ANY (p_hashes) AND s.revoked_at IS NULL AND seereon_in_tenant_scope(s.tenant_id)
    RETURNING 1
  ) SELECT count(*)::integer FROM r
$$;

CREATE FUNCTION seereon_create_reset_token(p_hash text, p_user text, p_expires_ms bigint, p_used boolean) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM seereon_require_user_in_scope(p_user);
  IF p_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'reset token must be stored as a SHA-256 hex digest' USING ERRCODE = '22023';
  END IF;
  INSERT INTO reset_tokens (token_hash, user_id, expires_at, used_at)
  VALUES (p_hash, p_user, to_timestamp(p_expires_ms::double precision / 1000), CASE WHEN p_used THEN now() END);
END
$$;

CREATE FUNCTION seereon_use_reset_tokens(p_hashes text[]) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  UPDATE reset_tokens r SET used_at = now() WHERE r.token_hash = ANY (p_hashes) AND r.used_at IS NULL AND seereon_user_in_scope(r.user_id)
$$;

CREATE FUNCTION seereon_delete_reset_tokens(p_hashes text[]) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  DELETE FROM reset_tokens r WHERE r.token_hash = ANY (p_hashes) AND seereon_user_in_scope(r.user_id)
$$;

-- a failed (p_succeeded false, at p_at_ms) or successful (clears the lockout window) sign-in
CREATE FUNCTION seereon_record_login_attempt(p_email text, p_at_ms bigint, p_ip text, p_succeeded boolean) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  INSERT INTO login_attempts (email, at, ip, succeeded)
  VALUES (left(lower(p_email), 254), CASE WHEN p_at_ms IS NULL THEN now() ELSE to_timestamp(p_at_ms::double precision / 1000) END, left(p_ip, 64), p_succeeded)
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- Idempotency-Key replay (24 h)
-- ---------------------------------------------------------------------------------------------------------------------
CREATE FUNCTION seereon_idempotency_get(p_tenant text, p_key text) RETURNS TABLE (method text, path text, status integer, body text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT k.method, k.path, k.status, k.body::text FROM idempotency_keys k
   WHERE k.tenant_id = p_tenant AND k.key = p_key AND k.created_at > now() - interval '24 hours'
$$;

CREATE FUNCTION seereon_idempotency_put(p_tenant text, p_key text, p_method text, p_path text, p_status integer, p_body jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF NOT seereon_in_tenant_scope(p_tenant) THEN
    RAISE EXCEPTION 'idempotency record outside the tenant of this transaction' USING ERRCODE = '42501';
  END IF;
  INSERT INTO idempotency_keys (tenant_id, key, method, path, status, body) VALUES (p_tenant, p_key, p_method, p_path, p_status, p_body)
  ON CONFLICT (tenant_id, key) DO UPDATE SET method = EXCLUDED.method, path = EXCLUDED.path, status = EXCLUDED.status, body = EXCLUDED.body, created_at = now()
   WHERE idempotency_keys.created_at < now() - interval '24 hours';
END
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- auth rate limits (shared fixed windows) and the security audit trail (append-only for the API)
-- ---------------------------------------------------------------------------------------------------------------------
CREATE FUNCTION seereon_rate_limit_hit(p_bucket text, p_window_s integer) RETURNS TABLE (hits integer, ttl integer)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH w AS (SELECT to_timestamp(floor(extract(epoch FROM now()) / p_window_s) * p_window_s) AS start)
  INSERT INTO rate_limits AS r (bucket, window_start, hits, expires_at)
  SELECT p_bucket, w.start, 1, w.start + make_interval(secs => p_window_s) FROM w
  ON CONFLICT (bucket, window_start) DO UPDATE SET hits = r.hits + 1
  RETURNING r.hits, ceil(extract(epoch FROM (r.expires_at - now())))::integer
$$;

CREATE FUNCTION seereon_log_security_event(
  p_event text, p_outcome text, p_tenant text, p_user text, p_email text, p_ip text, p_user_agent text, p_request_id text, p_detail jsonb
) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  INSERT INTO security_events (event, outcome, tenant_id, user_id, email, ip, user_agent, request_id, detail)
  VALUES (left(p_event, 64), p_outcome, left(p_tenant, 64), left(p_user, 64), left(lower(p_email), 254), left(p_ip, 64), left(p_user_agent, 300), left(p_request_id, 128), p_detail)
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- email delivery queue (python -m app.worker)
-- ---------------------------------------------------------------------------------------------------------------------
ALTER TABLE email_deliveries DROP CONSTRAINT email_deliveries_status_check;
UPDATE email_deliveries SET status = 'dead' WHERE status = 'failed';
ALTER TABLE email_deliveries
  ADD COLUMN kind text NOT NULL DEFAULT 'outbox' CONSTRAINT email_deliveries_kind_chk CHECK (kind IN ('outbox', 'platform')),
  ADD COLUMN to_addr text,                      -- platform messages only (outbox messages are read from email_outbox)
  ADD COLUMN subject text,
  ADD COLUMN body text,                         -- platform messages: AES-256-GCM sealed by the API; NULL once sent/expired/dead
  ADD COLUMN created_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN locked_by text,                    -- worker id holding the claim
  ADD COLUMN locked_until timestamptz,          -- claim lease: a crashed worker's claim is taken over after it
  ADD COLUMN sent_at timestamptz,
  ADD COLUMN expires_at timestamptz,            -- platform messages: not sent after this (a stale reset link is useless)
  ADD CONSTRAINT email_deliveries_status_chk CHECK (status IN ('pending', 'sending', 'sent', 'dead', 'skipped', 'expired'));
DROP INDEX IF EXISTS email_deliveries_pending_ix;
CREATE INDEX email_deliveries_due_ix ON email_deliveries (next_attempt_at) WHERE status IN ('pending', 'sending');
CREATE INDEX email_deliveries_status_ix ON email_deliveries (status, updated_at);
COMMENT ON TABLE email_deliveries IS 'Delivery queue of the worker: email_outbox messages (kind outbox) and sealed platform messages (password reset, invitations)';

-- the outbox row tells the truth about delivery: 'queued' when written, 'sent' (with sent_at) or 'failed' by the worker
ALTER TABLE email_outbox DROP CONSTRAINT email_outbox_status_chk;
ALTER TABLE email_outbox ADD CONSTRAINT email_outbox_status_chk CHECK ("status" IN ('queued', 'sent', 'failed'));
ALTER TABLE email_outbox ALTER COLUMN sent_at DROP NOT NULL;
COMMENT ON COLUMN email_outbox."status" IS '''queued'' | ''sent'' | ''failed''';
UPDATE app_column_meta SET ts_type = '''queued'' | ''sent'' | ''failed''' WHERE table_name = 'email_outbox' AND column_name = 'status';
UPDATE app_column_meta SET not_null = false, ts_type = 'ISODateTime | null' WHERE table_name = 'email_outbox' AND column_name = 'sent_at';

-- queue the outbox rows the flush just inserted (same transaction)
CREATE FUNCTION seereon_email_enqueue_outbox(p_ids text[], p_tenants text[]) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM unnest(p_tenants) t WHERE NOT seereon_in_tenant_scope(t)) THEN
    RAISE EXCEPTION 'outbox message outside the tenant of this transaction' USING ERRCODE = '42501';
  END IF;
  INSERT INTO email_deliveries (message_id, tenant_id, status, kind)
  SELECT m, t, 'pending', 'outbox' FROM unnest(p_ids, p_tenants) AS x(m, t)
  ON CONFLICT (message_id) DO NOTHING;
END
$$;

-- a platform message (reset link, invitation): body sealed by the API
CREATE FUNCTION seereon_email_enqueue_platform(p_id text, p_tenant text, p_to text, p_subject text, p_sealed_body text, p_expires_at timestamptz) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF NOT seereon_in_tenant_scope(p_tenant) THEN
    RAISE EXCEPTION 'platform message outside the tenant of this transaction' USING ERRCODE = '42501';
  END IF;
  INSERT INTO email_deliveries (message_id, tenant_id, status, kind, to_addr, subject, body, expires_at)
  VALUES (p_id, p_tenant, 'pending', 'platform', left(p_to, 254), left(p_subject, 300), p_sealed_body, p_expires_at);
END
$$;

-- claim up to p_batch due messages for one worker (exclusive: FOR UPDATE SKIP LOCKED + a lease). A claim whose lease
-- ran out (the worker died mid-send) is claimed again. Expired platform messages are retired here, not sent.
CREATE FUNCTION seereon_email_claim(p_worker text, p_batch integer, p_lease_s integer)
RETURNS TABLE (message_id text, tenant_id text, kind text, to_addr text, subject text, body text, attempts integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
#variable_conflict use_column
BEGIN
  UPDATE email_deliveries d SET status = 'expired', body = NULL, locked_by = NULL, locked_until = NULL, updated_at = now()
   WHERE d.kind = 'platform' AND d.status IN ('pending', 'sending') AND d.expires_at IS NOT NULL AND d.expires_at <= now()
     AND (d.status = 'pending' OR d.locked_until < now());
  RETURN QUERY
  WITH due AS (
    SELECT e.message_id FROM email_deliveries e
     WHERE (e.status = 'pending' AND e.next_attempt_at <= now()) OR (e.status = 'sending' AND e.locked_until < now())
     ORDER BY e.next_attempt_at
     LIMIT greatest(1, least(p_batch, 500))
     FOR UPDATE SKIP LOCKED
  ), claimed AS (
    UPDATE email_deliveries d SET status = 'sending', attempts = d.attempts + 1, locked_by = p_worker,
           locked_until = now() + make_interval(secs => p_lease_s), updated_at = now()
      FROM due WHERE d.message_id = due.message_id
    RETURNING d.message_id, d.tenant_id, d.kind, d.to_addr, d.subject, d.body, d.attempts
  )
  SELECT c.message_id, c.tenant_id, c.kind,
         CASE WHEN c.kind = 'outbox' THEN o."to" ELSE c.to_addr END,
         CASE WHEN c.kind = 'outbox' THEN o.subject ELSE c.subject END,
         CASE WHEN c.kind = 'outbox' THEN o.body ELSE c.body END,
         c.attempts
    FROM claimed c LEFT JOIN email_outbox o ON c.kind = 'outbox' AND o.id = c.message_id;
END
$$;

-- delivered: only the worker holding the claim may settle it (fencing token = worker id)
CREATE FUNCTION seereon_email_sent(p_id text, p_worker text) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text;
BEGIN
  UPDATE email_deliveries d SET status = 'sent', sent_at = now(), last_error = NULL, locked_by = NULL, locked_until = NULL,
         body = CASE WHEN d.kind = 'platform' THEN NULL ELSE d.body END, updated_at = now()
   WHERE d.message_id = p_id AND d.status = 'sending' AND d.locked_by = p_worker
  RETURNING d.kind INTO k;
  IF k IS NULL THEN
    RETURN false;
  END IF;
  IF k = 'outbox' THEN  -- the workspace's sent log (a statement-level trigger bumps its cache version)
    UPDATE email_outbox o SET status = 'sent', sent_at = now(), updated_at = now(), version = o.version + 1 WHERE o.id = p_id AND o.status <> 'sent';
  END IF;
  RETURN true;
END
$$;

-- failed attempt: back to pending after p_backoff_s, or dead-letter after p_max_attempts; 'skipped' when the message
-- itself is gone (outbox row deleted)
CREATE FUNCTION seereon_email_failed(p_id text, p_worker text, p_error text, p_max_attempts integer, p_backoff_s integer, p_skip boolean)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE st text; k text;
BEGIN
  UPDATE email_deliveries d
     SET status = CASE WHEN p_skip THEN 'skipped' WHEN d.attempts >= p_max_attempts THEN 'dead' ELSE 'pending' END,
         last_error = left(p_error, 500), locked_by = NULL, locked_until = NULL,
         next_attempt_at = now() + make_interval(secs => greatest(0, p_backoff_s)),
         body = CASE WHEN d.kind = 'platform' AND (p_skip OR d.attempts >= p_max_attempts) THEN NULL ELSE d.body END,
         updated_at = now()
   WHERE d.message_id = p_id AND d.status = 'sending' AND d.locked_by = p_worker
  RETURNING d.status, d.kind INTO st, k;
  IF st = 'dead' AND k = 'outbox' THEN  -- dead-lettered: the sent log says failed
    UPDATE email_outbox o SET status = 'failed', updated_at = now(), version = o.version + 1 WHERE o.id = p_id AND o.status = 'queued';
  END IF;
  RETURN st;
END
$$;

CREATE FUNCTION seereon_email_queue_stats() RETURNS TABLE (status text, n bigint, oldest_due_s double precision)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT d.status, count(*), max(extract(epoch FROM now() - d.next_attempt_at)) FILTER (WHERE d.status = 'pending')
    FROM email_deliveries d GROUP BY d.status
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- scheduled jobs: liveness + last runs (not sensitive: plain grants)
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE worker_heartbeats (
  worker_id     text PRIMARY KEY,                -- host:pid:random
  hostname      text,
  pid           integer,
  started_at    timestamptz NOT NULL DEFAULT now(),
  last_beat_at  timestamptz NOT NULL DEFAULT now(),
  info          jsonb
);
COMMENT ON TABLE worker_heartbeats IS 'One row per running worker process (python -m app.worker); /readyz warns when the newest beat is stale';

CREATE TABLE job_runs (
  job               text PRIMARY KEY,
  last_started_at   timestamptz,
  last_finished_at  timestamptz,
  last_status       text CHECK (last_status IN ('ok', 'error', 'running')),
  last_error        text,
  last_duration_ms  integer,
  last_worker       text,
  runs              bigint NOT NULL DEFAULT 0,
  failures          bigint NOT NULL DEFAULT 0,
  last_result       jsonb
);
COMMENT ON TABLE job_runs IS 'Last run of every scheduled job (one row per job; written by the worker holding the job''s advisory lock)';
GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats TO seereon_app;
GRANT SELECT, INSERT, UPDATE ON job_runs TO seereon_app;

-- the tenants the scheduled jobs run for (every tenant that can still be used; suspended ones are skipped)
CREATE FUNCTION seereon_job_tenants() RETURNS TABLE (id text, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT t.id, t.status FROM tenants t WHERE t.status <> 'suspended' ORDER BY t._ord
$$;

-- housekeeping: expired / revoked sessions, used or expired reset tokens, old login attempts, rate-limit windows,
-- idempotency keys (24 h), the flush change log (10 min), audit retention, settled deliveries (90 days), expired
-- pending tokens (2FA setup, MFA challenges, WBS previews). Returns rows deleted per table.
CREATE FUNCTION seereon_cleanup(p_security_retention_days integer, p_session_keep_days integer DEFAULT 7)
RETURNS TABLE (what text, deleted bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n bigint;
BEGIN
  DELETE FROM sessions s WHERE (s.revoked_at IS NOT NULL AND s.revoked_at < now() - make_interval(days => p_session_keep_days))
                            OR s.expires_at < now() - make_interval(days => p_session_keep_days);
  GET DIAGNOSTICS n = ROW_COUNT; what := 'sessions'; deleted := n; RETURN NEXT;
  DELETE FROM reset_tokens r WHERE r.expires_at < now() - interval '1 day';
  GET DIAGNOSTICS n = ROW_COUNT; what := 'reset_tokens'; deleted := n; RETURN NEXT;
  DELETE FROM login_attempts a WHERE a.at < now() - interval '30 days';
  GET DIAGNOSTICS n = ROW_COUNT; what := 'login_attempts'; deleted := n; RETURN NEXT;
  DELETE FROM rate_limits r WHERE r.expires_at < now();
  GET DIAGNOSTICS n = ROW_COUNT; what := 'rate_limits'; deleted := n; RETURN NEXT;
  DELETE FROM idempotency_keys k WHERE k.created_at < now() - interval '24 hours';
  GET DIAGNOSTICS n = ROW_COUNT; what := 'idempotency_keys'; deleted := n; RETURN NEXT;
  DELETE FROM table_changes c WHERE c.created_at < now() - interval '10 minutes';
  GET DIAGNOSTICS n = ROW_COUNT; what := 'table_changes'; deleted := n; RETURN NEXT;
  DELETE FROM security_events e WHERE e.at < now() - make_interval(days => p_security_retention_days);
  GET DIAGNOSTICS n = ROW_COUNT; what := 'security_events'; deleted := n; RETURN NEXT;
  DELETE FROM email_deliveries d WHERE d.status IN ('sent', 'skipped', 'expired') AND d.updated_at < now() - interval '90 days';
  GET DIAGNOSTICS n = ROW_COUNT; what := 'email_deliveries'; deleted := n; RETURN NEXT;
  DELETE FROM pending_tokens p WHERE p.kind <> 'two_factor' AND p.expires_at < now() - interval '1 hour';
  GET DIAGNOSTICS n = ROW_COUNT; what := 'pending_tokens'; deleted := n; RETURN NEXT;
  DELETE FROM worker_heartbeats w WHERE w.last_beat_at < now() - interval '1 day';
  GET DIAGNOSTICS n = ROW_COUNT; what := 'worker_heartbeats'; deleted := n; RETURN NEXT;
END
$$;

-- table_changes: written by the API's own flush (RLS: the transaction's tenant only)
GRANT INSERT ON table_changes TO seereon_app;
-- startup check: the API refuses to run against a schema with pending migrations
GRANT SELECT ON schema_migrations TO seereon_app;

-- ---------------------------------------------------------------------------------------------------------------------
-- payments (Razorpay): tenant tables with the conventions of V002–V004
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE payment_orders (
  id text NOT NULL DEFAULT gen_random_uuid()::text,
  tenant_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_by text,
  "version" integer NOT NULL DEFAULT 1,
  provider text NOT NULL CONSTRAINT payment_orders_provider_chk CHECK (provider IN ('razorpay')),
  provider_order_id text,                       -- order_… (Razorpay Orders API), NULL until the order exists
  provider_payment_id text,                     -- pay_… once paid
  invoice_id text NOT NULL,                     -- billing_invoices.id (the amount comes from it, never from the client)
  purpose text NOT NULL CONSTRAINT payment_orders_purpose_chk CHECK (purpose IN ('plan', 'seats')),
  plan_code text NOT NULL,
  seats integer NOT NULL,
  amount_paise bigint NOT NULL CHECK (amount_paise > 0),
  currency text NOT NULL DEFAULT 'INR',
  "status" text NOT NULL CONSTRAINT payment_orders_status_chk CHECK ("status" IN ('created', 'paid', 'failed')),
  paid_at timestamptz,
  failure_reason text,
  _null_override text[],
  _ord bigint GENERATED BY DEFAULT AS IDENTITY
);
COMMENT ON TABLE payment_orders IS 'Razorpay orders created by the API for a billing invoice; paid only after a verified signature or webhook';
ALTER TABLE payment_orders ADD CONSTRAINT payment_orders_pkey PRIMARY KEY (id);
CREATE INDEX payment_orders_tenant_ord_ix ON payment_orders (tenant_id, _ord);
CREATE UNIQUE INDEX payment_orders_provider_order_uk ON payment_orders (provider, provider_order_id) WHERE provider_order_id IS NOT NULL;
CREATE INDEX payment_orders_invoice_ix ON payment_orders (tenant_id, invoice_id);
ALTER TABLE payment_orders ADD CONSTRAINT payment_orders_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenants (id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE payment_events (
  id text NOT NULL DEFAULT gen_random_uuid()::text,   -- the provider's event id (X-Razorpay-Event-Id): idempotency key
  tenant_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_by text,
  "version" integer NOT NULL DEFAULT 1,
  provider text NOT NULL CONSTRAINT payment_events_provider_chk CHECK (provider IN ('razorpay')),
  "event" text NOT NULL,                              -- payment.captured, order.paid, payment.failed, …
  provider_order_id text,
  provider_payment_id text,
  payload jsonb,                                      -- the verified webhook body without card/contact details
  outcome text NOT NULL,                              -- applied | duplicate | ignored | failed
  _null_override text[],
  _ord bigint GENERATED BY DEFAULT AS IDENTITY
);
COMMENT ON TABLE payment_events IS 'Verified Razorpay webhook events, stored once per event id';
ALTER TABLE payment_events ADD CONSTRAINT payment_events_pkey PRIMARY KEY (id);
CREATE INDEX payment_events_tenant_ord_ix ON payment_events (tenant_id, _ord);
ALTER TABLE payment_events ADD CONSTRAINT payment_events_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenants (id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE payment_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON payment_orders USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY platform_admin ON payment_orders USING (current_setting('app.platform_admin', true) = 'on') WITH CHECK (current_setting('app.platform_admin', true) = 'on');
CREATE TRIGGER payment_orders_ver_ins AFTER INSERT ON payment_orders REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER payment_orders_ver_upd AFTER UPDATE ON payment_orders REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER payment_orders_ver_del AFTER DELETE ON payment_orders REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON payment_events USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY platform_admin ON payment_events USING (current_setting('app.platform_admin', true) = 'on') WITH CHECK (current_setting('app.platform_admin', true) = 'on');
CREATE TRIGGER payment_events_ver_ins AFTER INSERT ON payment_events REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER payment_events_ver_upd AFTER UPDATE ON payment_events REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER payment_events_ver_del AFTER DELETE ON payment_events REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
GRANT SELECT, INSERT, UPDATE, DELETE ON payment_orders, payment_events TO seereon_app;

INSERT INTO app_table_meta (table_name, tenant_scoped, pk) VALUES
  ('payment_orders', true, ARRAY['id']::text[]),
  ('payment_events', true, ARRAY['id']::text[]);
INSERT INTO app_column_meta (table_name, column_name, ordinal, kind, null_as, pg_type, not_null, ts_type) VALUES
  ('payment_orders', 'id', 1, 'text', 'null', 'text', true, 'ID'),
  ('payment_orders', 'tenant_id', 2, 'text', 'null', 'text', true, 'ID'),
  ('payment_orders', 'created_at', 3, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('payment_orders', 'updated_at', 4, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('payment_orders', 'created_by', 5, 'text', 'null', 'text', false, '?: ID | null'),
  ('payment_orders', 'version', 6, 'int', 'null', 'integer', true, 'number'),
  ('payment_orders', 'provider', 7, 'text', 'null', 'text', true, '''razorpay'''),
  ('payment_orders', 'provider_order_id', 8, 'text', 'null', 'text', false, 'string | null'),
  ('payment_orders', 'provider_payment_id', 9, 'text', 'null', 'text', false, 'string | null'),
  ('payment_orders', 'invoice_id', 10, 'text', 'null', 'text', true, 'ID'),
  ('payment_orders', 'purpose', 11, 'text', 'null', 'text', true, '''plan'' | ''seats'''),
  ('payment_orders', 'plan_code', 12, 'text', 'null', 'text', true, 'string'),
  ('payment_orders', 'seats', 13, 'int', 'null', 'integer', true, 'number'),
  ('payment_orders', 'amount_paise', 14, 'bigint', 'null', 'bigint', true, 'Paise'),
  ('payment_orders', 'currency', 15, 'text', 'null', 'text', true, 'string'),
  ('payment_orders', 'status', 16, 'text', 'null', 'text', true, '''created'' | ''paid'' | ''failed'''),
  ('payment_orders', 'paid_at', 17, 'timestamp', 'null', 'timestamptz', false, 'ISODateTime | null'),
  ('payment_orders', 'failure_reason', 18, 'text', 'null', 'text', false, 'string | null'),
  ('payment_events', 'id', 1, 'text', 'null', 'text', true, 'ID'),
  ('payment_events', 'tenant_id', 2, 'text', 'null', 'text', true, 'ID'),
  ('payment_events', 'created_at', 3, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('payment_events', 'updated_at', 4, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('payment_events', 'created_by', 5, 'text', 'null', 'text', false, '?: ID | null'),
  ('payment_events', 'version', 6, 'int', 'null', 'integer', true, 'number'),
  ('payment_events', 'provider', 7, 'text', 'null', 'text', true, '''razorpay'''),
  ('payment_events', 'event', 8, 'text', 'null', 'text', true, 'string'),
  ('payment_events', 'provider_order_id', 9, 'text', 'null', 'text', false, 'string | null'),
  ('payment_events', 'provider_payment_id', 10, 'text', 'null', 'text', false, 'string | null'),
  ('payment_events', 'payload', 11, 'json', 'null', 'jsonb', false, 'unknown'),
  ('payment_events', 'outcome', 12, 'text', 'null', 'text', true, 'string');

-- which tenant a provider order belongs to (public webhook: no session; the signature is verified first)
CREATE FUNCTION seereon_payment_tenant(p_provider text, p_provider_order_id text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT o.tenant_id FROM payment_orders o WHERE o.provider = p_provider AND o.provider_order_id = p_provider_order_id
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- privileges: EXECUTE for seereon_app only, on the operations it needs (never the internal helpers)
-- ---------------------------------------------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION
  seereon_platform_scope(), seereon_in_tenant_scope(text), seereon_user_in_scope(text), seereon_require_user_in_scope(text),
  seereon_resolve_session(text, integer), seereon_user_by_email(text), seereon_users_by_email(text[], text), seereon_free_slug(text), seereon_invite_tenant(text),
  seereon_reset_token_tenant(text), seereon_mfa_owner(text), seereon_password_hashes(text, text[]), seereon_sessions(text, integer),
  seereon_reset_tokens(text), seereon_login_attempts(text[]), seereon_set_password(text, text), seereon_delete_passwords(text[]),
  seereon_create_session(text, text, text, timestamptz, integer, text, text), seereon_revoke_sessions(text[]),
  seereon_create_reset_token(text, text, bigint, boolean), seereon_use_reset_tokens(text[]), seereon_delete_reset_tokens(text[]),
  seereon_record_login_attempt(text, bigint, text, boolean), seereon_idempotency_get(text, text),
  seereon_idempotency_put(text, text, text, text, integer, jsonb), seereon_rate_limit_hit(text, integer),
  seereon_log_security_event(text, text, text, text, text, text, text, text, jsonb), seereon_email_enqueue_outbox(text[], text[]),
  seereon_email_enqueue_platform(text, text, text, text, text, timestamptz), seereon_email_claim(text, integer, integer),
  seereon_email_sent(text, text), seereon_email_failed(text, text, text, integer, integer, boolean), seereon_email_queue_stats(),
  seereon_job_tenants(), seereon_cleanup(integer, integer), seereon_payment_tenant(text, text)
FROM PUBLIC;
-- the RLS helpers are called from policies / other functions only
GRANT EXECUTE ON FUNCTION seereon_platform_scope(), seereon_in_tenant_scope(text) TO seereon_app;
GRANT EXECUTE ON FUNCTION
  seereon_resolve_session(text, integer), seereon_user_by_email(text), seereon_users_by_email(text[], text), seereon_free_slug(text), seereon_invite_tenant(text),
  seereon_reset_token_tenant(text), seereon_mfa_owner(text), seereon_password_hashes(text, text[]), seereon_sessions(text, integer),
  seereon_reset_tokens(text), seereon_login_attempts(text[]), seereon_set_password(text, text), seereon_delete_passwords(text[]),
  seereon_create_session(text, text, text, timestamptz, integer, text, text), seereon_revoke_sessions(text[]),
  seereon_create_reset_token(text, text, bigint, boolean), seereon_use_reset_tokens(text[]), seereon_delete_reset_tokens(text[]),
  seereon_record_login_attempt(text, bigint, text, boolean), seereon_idempotency_get(text, text),
  seereon_idempotency_put(text, text, text, text, integer, jsonb), seereon_rate_limit_hit(text, integer),
  seereon_log_security_event(text, text, text, text, text, text, text, text, jsonb), seereon_email_enqueue_outbox(text[], text[]),
  seereon_email_enqueue_platform(text, text, text, text, text, timestamptz), seereon_email_claim(text, integer, integer),
  seereon_email_sent(text, text), seereon_email_failed(text, text, text, integer, integer, boolean), seereon_email_queue_stats(),
  seereon_job_tenants(), seereon_cleanup(integer, integer), seereon_payment_tenant(text, text)
TO seereon_app;
-- functions created later by the owner are not executable by PUBLIC either
ALTER DEFAULT PRIVILEGES FOR ROLE seereon_owner IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
