-- V013: typed reports (v9 D9). Hand-written and reviewed; never edit after it is applied — add V015.
--
-- Reports now start from a catalogue of report TYPES (Financial, Attendance, Salary, Task, Project, Timesheet). A saved
-- report made from a type keeps the type id and the viewer's configuration (fields, period, filters, group-by, sort,
-- chart); the API recomputes it on every run with the type's own rules and permissions (salary types need salary
-- access). Reports built on the generic dataset engine (and saved AI answers) leave both columns NULL and behave as
-- before. No data change: existing saved reports keep working unchanged.

ALTER TABLE saved_reports ADD COLUMN report_type text;
ALTER TABLE saved_reports ADD COLUMN report_config jsonb;
COMMENT ON COLUMN saved_reports.report_type IS '?: string | null';
COMMENT ON COLUMN saved_reports.report_config IS '?: import(''./reports'').ReportTypeConfig | null';
ALTER TABLE saved_reports ADD CONSTRAINT saved_reports_report_type_chk CHECK (report_type IS NULL OR report_type ~ '^[a-z][a-z0-9_]{1,39}$');
ALTER TABLE saved_reports ADD CONSTRAINT saved_reports_report_config_obj_chk CHECK (report_config IS NULL OR jsonb_typeof(report_config) = 'object');
ALTER TABLE saved_reports ADD CONSTRAINT saved_reports_report_config_pair_chk CHECK ((report_type IS NULL) = (report_config IS NULL));

INSERT INTO app_column_meta (table_name, column_name, ordinal, kind, null_as, pg_type, not_null, ts_type) VALUES
  ('saved_reports', 'report_type', 16, 'text', 'absent', 'text', false, 'string | null'),
  ('saved_reports', 'report_config', 17, 'json', 'absent', 'jsonb', false, 'import(''./reports'').ReportTypeConfig | null');
