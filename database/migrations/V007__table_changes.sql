-- V007 — change log of the unit of work's flushes (performance; see backend/PERFORMANCE.md "Cache coherence").
--
-- Every API flush (one transaction, holding the tenant's advisory lock) records, per changed tenant table with an
-- `id` primary key, the tenant_table_versions it moved the table from and to, and the ids it inserted/updated and
-- deleted. A worker process holding the table at an older version applies the chain of changes (fetching only those
-- rows) instead of re-reading the whole table. The chain must be gapless: any version step without a row here (an
-- out-of-band SQL write, a demo reset, a purged row) makes the reader fall back to the full read, so the log is an
-- optimisation only — never a source of truth. Rows are purged after a few minutes by the API's sweeper.

CREATE TABLE table_changes (
  tenant_id       text        NOT NULL,
  table_name      text        NOT NULL,
  version_before  bigint      NOT NULL,
  version_after   bigint      NOT NULL,
  upserted_ids    text[]      NOT NULL DEFAULT '{}',   -- inserted or updated rows (read them again)
  deleted_ids     text[]      NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, table_name, version_after),
  CHECK (version_after > version_before)
);
CREATE INDEX table_changes_created_ix ON table_changes (created_at);
COMMENT ON TABLE table_changes IS 'Flush change log for incremental cache refresh across API processes (ids only; purged after minutes)';

ALTER TABLE table_changes ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON table_changes USING (tenant_id = current_setting('app.tenant_id', true));
GRANT SELECT ON table_changes TO seereon_app;  -- read inside the tenant's read snapshot; written by the owner (flush)
