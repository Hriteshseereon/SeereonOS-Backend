-- V014: v9 admin settings (A5, decision D10). Hand-written and reviewed; never edit after it is applied — add a new migration.
--
-- 1. Leave types & policies. leave_policies becomes the tenant's leave-type master: the built-in types keep their codes
--    (casual, sick, earned, compensatory, optional_holiday; lwp needs no policy) and a tenant may add its own
--    (e.g. 'maternity'). The fixed CHECK lists on the leave type columns are replaced by a code-format check, so a
--    custom code can be applied for, approved, recorded as leave and booked on a timesheet. New optional columns
--    (absent on every existing row = the v8 behaviour):
--      name                         display name (absent = the built-in label)
--      description                  shown to employees when they apply
--      active                       false = no longer offered (history stays); absent = true
--      encashable / encash_max_days unused balance may be encashed (cap per FY); informational for payroll
--      applicable_role_keys         only people holding one of these roles (empty / absent = everyone)
--      applicable_location_ids      only people at one of these locations
--      applicable_employment_types  only these employment types (full_time, part_time, contract, intern)
--      attachment_after_days        a supporting document is required when a request is MORE than N days
--                                   (0 = always required, absent = never)
--      is_custom                    true for tenant-defined codes
--      sort_order                   order in pickers
--    accrual gains 'annual' (the full quota at the start of every FY, also in the year someone joins); 'upfront' keeps
--    its meaning (the year's quota at the start of the FY, pro-rated for joiners) and 'monthly' too.
--
-- 2. Holiday calendars. holiday_calendars = named calendars of a tenant (one is_default). holidays.calendar_id points to
--    one; ABSENT = the tenant's base calendar, whose id is always 'hcal_' || tenant_id (it is materialised on first
--    write; every existing holiday belongs to it, so nothing changes until a second calendar exists).
--    locations.holiday_calendar_id assigns a calendar to a location (absent = the default calendar); a person's
--    working-day maths (leave days, timesheet targets, costing) uses the calendar of their location.
--    locations.holiday_calendar (text) stays as the display copy of the calendar name.
--
-- 3. Financial-year opening balances (fy_openings): one row per tenant and FY (fy_start_year = the calendar year the
--    FY starts in). lines = [{id, kind: cash|bank|ledger|receivable|payable, name, side: debit|credit, amount_paise,
--    category?, party_id?, party_name?, reference?, due_on?, note?}]. Editable until the FY has transactions (ledger
--    entries, issued invoices, payments); then locked unless reopened with a reason (maker-checker entity
--    'opening_balance' when enabled). history = [{at, by, action, reason?, totals}] (also in activity_log).
--    The cash + bank lines are the opening cash of the cash position (ledger, P&L runway, CEO dashboard).
--
-- 4. change_sets.entity gains 'opening_balance'.

-- 1 ---------------------------------------------------------------------------------------------------------------
ALTER TABLE leave_policies DROP CONSTRAINT leave_policies_type_chk;
ALTER TABLE leave_policies ADD CONSTRAINT leave_policies_type_chk CHECK ("type" ~ '^[a-z][a-z0-9_]{1,39}$');
ALTER TABLE leave_policies DROP CONSTRAINT leave_policies_accrual_chk;
ALTER TABLE leave_policies ADD CONSTRAINT leave_policies_accrual_chk CHECK (accrual IN ('monthly', 'upfront', 'annual'));
ALTER TABLE leave_policies ADD COLUMN "name" text;
ALTER TABLE leave_policies ADD COLUMN description text;
ALTER TABLE leave_policies ADD COLUMN active boolean;
ALTER TABLE leave_policies ADD COLUMN encashable boolean;
ALTER TABLE leave_policies ADD COLUMN encash_max_days double precision;
ALTER TABLE leave_policies ADD COLUMN applicable_role_keys text[];
ALTER TABLE leave_policies ADD COLUMN applicable_location_ids text[];
ALTER TABLE leave_policies ADD COLUMN applicable_employment_types text[];
ALTER TABLE leave_policies ADD COLUMN attachment_after_days integer;
ALTER TABLE leave_policies ADD COLUMN is_custom boolean;
ALTER TABLE leave_policies ADD COLUMN sort_order integer;
ALTER TABLE leave_policies ADD CONSTRAINT leave_policies_attachment_after_chk CHECK (attachment_after_days IS NULL OR (attachment_after_days >= 0 AND attachment_after_days <= 365));
ALTER TABLE leave_policies ADD CONSTRAINT leave_policies_encash_max_chk CHECK (encash_max_days IS NULL OR (encash_max_days >= 0 AND encash_max_days <= 365));
CREATE UNIQUE INDEX leave_policies_tenant_type_uk ON leave_policies (tenant_id, "type");
COMMENT ON COLUMN leave_policies."type" IS 'LeaveType | string (custom code)';
COMMENT ON COLUMN leave_policies.accrual IS '''upfront'' | ''monthly'' | ''annual''';
COMMENT ON COLUMN leave_policies."name" IS '?: string';
COMMENT ON COLUMN leave_policies.description IS '?: string';
COMMENT ON COLUMN leave_policies.active IS '?: boolean';
COMMENT ON COLUMN leave_policies.encashable IS '?: boolean';
COMMENT ON COLUMN leave_policies.encash_max_days IS '?: number | null';
COMMENT ON COLUMN leave_policies.applicable_role_keys IS '?: string[]';
COMMENT ON COLUMN leave_policies.applicable_location_ids IS '?: ID[]';
COMMENT ON COLUMN leave_policies.applicable_employment_types IS '?: EmploymentType[]';
COMMENT ON COLUMN leave_policies.attachment_after_days IS '?: number | null';
COMMENT ON COLUMN leave_policies.is_custom IS '?: boolean';
COMMENT ON COLUMN leave_policies.sort_order IS '?: number';

ALTER TABLE leave DROP CONSTRAINT leave_type_chk;
ALTER TABLE leave ADD CONSTRAINT leave_type_chk CHECK ("type" ~ '^[a-z][a-z0-9_]{1,39}$');
ALTER TABLE leave_requests DROP CONSTRAINT leave_requests_type_chk;
ALTER TABLE leave_requests ADD CONSTRAINT leave_requests_type_chk CHECK ("type" ~ '^[a-z][a-z0-9_]{1,39}$');
ALTER TABLE timesheet_entries DROP CONSTRAINT timesheet_entries_leave_type_chk;
ALTER TABLE timesheet_entries ADD CONSTRAINT timesheet_entries_leave_type_chk CHECK (leave_type ~ '^[a-z][a-z0-9_]{1,39}$');

-- 2 ---------------------------------------------------------------------------------------------------------------
CREATE TABLE holiday_calendars (
  id text NOT NULL DEFAULT gen_random_uuid()::text,
  tenant_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_by text,
  "version" integer NOT NULL DEFAULT 1,
  "name" text NOT NULL,
  description text,
  is_default boolean NOT NULL,
  active boolean NOT NULL,
  _null_override text[],
  _ord bigint GENERATED BY DEFAULT AS IDENTITY
);
COMMENT ON TABLE holiday_calendars IS 'Row type HolidayCalendar[] (v9 admin settings)';
COMMENT ON COLUMN holiday_calendars."name" IS 'string';
COMMENT ON COLUMN holiday_calendars.description IS '?: string';
COMMENT ON COLUMN holiday_calendars.is_default IS 'boolean — the calendar of locations without one (exactly one per tenant)';
COMMENT ON COLUMN holiday_calendars.active IS 'boolean';
ALTER TABLE holiday_calendars ADD CONSTRAINT holiday_calendars_pkey PRIMARY KEY (id);
CREATE INDEX holiday_calendars_tenant_ord_ix ON holiday_calendars (tenant_id, _ord);
ALTER TABLE holiday_calendars ADD CONSTRAINT holiday_calendars_tenant_id_uk UNIQUE (tenant_id, id);
ALTER TABLE holiday_calendars ADD CONSTRAINT holiday_calendars_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenants (id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE holiday_calendars ADD CONSTRAINT holiday_calendars_name_chk CHECK (length(btrim("name")) BETWEEN 2 AND 60);
ALTER TABLE holiday_calendars ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON holiday_calendars USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY platform_admin ON holiday_calendars USING (current_setting('app.platform_admin', true) = 'on') WITH CHECK (current_setting('app.platform_admin', true) = 'on');
CREATE TRIGGER holiday_calendars_ver_ins AFTER INSERT ON holiday_calendars REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER holiday_calendars_ver_upd AFTER UPDATE ON holiday_calendars REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER holiday_calendars_ver_del AFTER DELETE ON holiday_calendars REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
GRANT SELECT, INSERT, UPDATE, DELETE ON holiday_calendars TO seereon_app;

ALTER TABLE holidays ADD COLUMN calendar_id text;
COMMENT ON COLUMN holidays.calendar_id IS '?: ID (absent = the base calendar hcal_<tenant_id>)';
CREATE INDEX holidays_calendar_ix ON holidays (tenant_id, calendar_id);
-- no FK: the base calendar row may not exist yet (it is materialised on the first calendar write)

ALTER TABLE locations ADD COLUMN holiday_calendar_id text;
COMMENT ON COLUMN locations.holiday_calendar_id IS '?: ID (absent = the default calendar)';

-- 3 ---------------------------------------------------------------------------------------------------------------
CREATE TABLE fy_openings (
  id text NOT NULL DEFAULT gen_random_uuid()::text,
  tenant_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_by text,
  "version" integer NOT NULL DEFAULT 1,
  fy_start_year integer NOT NULL,
  lines jsonb NOT NULL,
  locked boolean NOT NULL,
  locked_at timestamptz,
  locked_by text,
  reopened_at timestamptz,
  reopened_by text,
  reopen_reason text,
  history jsonb NOT NULL,
  notes text,
  _null_override text[],
  _ord bigint GENERATED BY DEFAULT AS IDENTITY
);
COMMENT ON TABLE fy_openings IS 'Row type FyOpening[] (v9 admin settings)';
COMMENT ON COLUMN fy_openings.fy_start_year IS 'number — the year the FY starts in (FY 2026-27 = 2026)';
COMMENT ON COLUMN fy_openings.lines IS 'OpeningLine[]';
COMMENT ON COLUMN fy_openings.locked IS 'boolean — finalised by hand (Lock); locked also while the FY has transactions and it is not reopened';
COMMENT ON COLUMN fy_openings.history IS '{ at: ISODateTime; by: ID; action: string; reason?: string; totals?: unknown }[]';
ALTER TABLE fy_openings ADD CONSTRAINT fy_openings_pkey PRIMARY KEY (id);
CREATE INDEX fy_openings_tenant_ord_ix ON fy_openings (tenant_id, _ord);
CREATE UNIQUE INDEX fy_openings_tenant_fy_uk ON fy_openings (tenant_id, fy_start_year);
ALTER TABLE fy_openings ADD CONSTRAINT fy_openings_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenants (id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE fy_openings ADD CONSTRAINT fy_openings_lines_array_chk CHECK (jsonb_typeof(lines) = 'array');
ALTER TABLE fy_openings ADD CONSTRAINT fy_openings_history_array_chk CHECK (jsonb_typeof(history) = 'array');
ALTER TABLE fy_openings ADD CONSTRAINT fy_openings_year_chk CHECK (fy_start_year BETWEEN 2000 AND 2100);
ALTER TABLE fy_openings ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON fy_openings USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY platform_admin ON fy_openings USING (current_setting('app.platform_admin', true) = 'on') WITH CHECK (current_setting('app.platform_admin', true) = 'on');
CREATE TRIGGER fy_openings_ver_ins AFTER INSERT ON fy_openings REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER fy_openings_ver_upd AFTER UPDATE ON fy_openings REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
CREATE TRIGGER fy_openings_ver_del AFTER DELETE ON fy_openings REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION app_bump_table_versions('tenant_id');
GRANT SELECT, INSERT, UPDATE, DELETE ON fy_openings TO seereon_app;

-- 4 ---------------------------------------------------------------------------------------------------------------
ALTER TABLE change_sets DROP CONSTRAINT change_sets_entity_chk;
ALTER TABLE change_sets ADD CONSTRAINT change_sets_entity_chk CHECK (entity IN ('employee', 'role', 'user_access', 'cost_rate', 'workflow', 'org_profile', 'master_record', 'shift', 'vendor', 'budget', 'payroll_finalise', 'settings', 'makerchecker_config', 'opening_balance'));

-- meta ------------------------------------------------------------------------------------------------------------
INSERT INTO app_table_meta (table_name, tenant_scoped, pk) VALUES
  ('holiday_calendars', true, ARRAY['id']::text[]),
  ('fy_openings', true, ARRAY['id']::text[]);
UPDATE app_column_meta SET ts_type = 'LeaveType | string' WHERE table_name = 'leave_policies' AND column_name = 'type';
UPDATE app_column_meta SET ts_type = '''upfront'' | ''monthly'' | ''annual''' WHERE table_name = 'leave_policies' AND column_name = 'accrual';
INSERT INTO app_column_meta (table_name, column_name, ordinal, kind, null_as, pg_type, not_null, ts_type) VALUES
  ('leave_policies', 'name', 11, 'text', 'absent', 'text', false, 'string'),
  ('leave_policies', 'description', 12, 'text', 'absent', 'text', false, 'string'),
  ('leave_policies', 'active', 13, 'bool', 'absent', 'boolean', false, 'boolean'),
  ('leave_policies', 'encashable', 14, 'bool', 'absent', 'boolean', false, 'boolean'),
  ('leave_policies', 'encash_max_days', 15, 'float', 'absent', 'double precision', false, 'number | null'),
  ('leave_policies', 'applicable_role_keys', 16, 'text_array', 'absent', 'text[]', false, 'string[]'),
  ('leave_policies', 'applicable_location_ids', 17, 'text_array', 'absent', 'text[]', false, 'ID[]'),
  ('leave_policies', 'applicable_employment_types', 18, 'text_array', 'absent', 'text[]', false, 'EmploymentType[]'),
  ('leave_policies', 'attachment_after_days', 19, 'int', 'absent', 'integer', false, 'number | null'),
  ('leave_policies', 'is_custom', 20, 'bool', 'absent', 'boolean', false, 'boolean'),
  ('leave_policies', 'sort_order', 21, 'int', 'absent', 'integer', false, 'number'),
  ('holidays', 'calendar_id', 10, 'text', 'absent', 'text', false, 'ID'),
  ('locations', 'holiday_calendar_id', 16, 'text', 'absent', 'text', false, 'ID'),
  ('holiday_calendars', 'id', 1, 'text', 'null', 'text', true, 'ID'),
  ('holiday_calendars', 'tenant_id', 2, 'text', 'null', 'text', true, 'ID'),
  ('holiday_calendars', 'created_at', 3, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('holiday_calendars', 'updated_at', 4, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('holiday_calendars', 'created_by', 5, 'text', 'null', 'text', false, 'ID | null'),
  ('holiday_calendars', 'version', 6, 'int', 'null', 'integer', true, 'number'),
  ('holiday_calendars', 'name', 7, 'text', 'null', 'text', true, 'string'),
  ('holiday_calendars', 'description', 8, 'text', 'absent', 'text', false, 'string'),
  ('holiday_calendars', 'is_default', 9, 'bool', 'null', 'boolean', true, 'boolean'),
  ('holiday_calendars', 'active', 10, 'bool', 'null', 'boolean', true, 'boolean'),
  ('fy_openings', 'id', 1, 'text', 'null', 'text', true, 'ID'),
  ('fy_openings', 'tenant_id', 2, 'text', 'null', 'text', true, 'ID'),
  ('fy_openings', 'created_at', 3, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('fy_openings', 'updated_at', 4, 'timestamp', 'null', 'timestamptz', true, 'ISODateTime'),
  ('fy_openings', 'created_by', 5, 'text', 'null', 'text', false, 'ID | null'),
  ('fy_openings', 'version', 6, 'int', 'null', 'integer', true, 'number'),
  ('fy_openings', 'fy_start_year', 7, 'int', 'null', 'integer', true, 'number'),
  ('fy_openings', 'lines', 8, 'json', 'null', 'jsonb', true, 'OpeningLine[]'),
  ('fy_openings', 'locked', 9, 'bool', 'null', 'boolean', true, 'boolean'),
  ('fy_openings', 'locked_at', 10, 'timestamp', 'null', 'timestamptz', false, 'ISODateTime | null'),
  ('fy_openings', 'locked_by', 11, 'text', 'null', 'text', false, 'ID | null'),
  ('fy_openings', 'reopened_at', 12, 'timestamp', 'null', 'timestamptz', false, 'ISODateTime | null'),
  ('fy_openings', 'reopened_by', 13, 'text', 'null', 'text', false, 'ID | null'),
  ('fy_openings', 'reopen_reason', 14, 'text', 'null', 'text', false, 'string | null'),
  ('fy_openings', 'history', 15, 'json', 'null', 'jsonb', true, 'OpeningHistory[]'),
  ('fy_openings', 'notes', 16, 'text', 'null', 'text', false, 'string | null');
