-- V011: v9 work round (A2) — multiple assignees on a task, task-independent timesheet time, leave documents.
-- Hand-written and reviewed; never edit after it is applied — add a new migration.
--
-- 1. tasks.assignee_ids — CO-ASSIGNEES of a task (decision D4). The task keeps ONE primary owner (owner_id: accountable,
--    submits for review, drives timeliness); co-assignees see the task in My tasks / Tasks, are notified, can log time,
--    move its stage and comment. The owner is never listed here. NULL / absent = no co-assignees (every existing task).
--
-- 2. Task-independent time (decision D5) needs NO schema change: a timesheet row without a task is
--    timesheet_entries.kind = 'internal' with
--      (b) internal_code 'project_work' + project_id (required) + note (the activity description), or any other
--          activity booked to a project (project_id set)  -> counted in that project's actual hours and cost;
--      (c) internal_code meeting | learning | presales | admin | support | other (+ the legacy interview, bench,
--          breaks) with project_id NULL                   -> overhead.
--    The codes are an API-level list (backend catalogue labels.INTERNAL_CODES); internal_code stays free text here.
--
-- 3. leave_requests.attachments — optional supporting documents of a leave application (medical certificate, …):
--    [{id, file_id, name, mime, size_bytes, uploaded_by, uploaded_at}]. The file content lives in `files`
--    (entity 'leave_request', entity_id = the request id), never in this column, so leave lists stay small. Who may
--    download is enforced by the API (the applicant, the approvers on the request's route and their delegates, HR).
--
-- No data change for existing rows.

ALTER TABLE tasks ADD COLUMN assignee_ids text[];
COMMENT ON COLUMN tasks.assignee_ids IS '?: ID[]';

ALTER TABLE leave_requests ADD COLUMN attachments jsonb;
COMMENT ON COLUMN leave_requests.attachments IS '?: LeaveAttachment[]';
ALTER TABLE leave_requests ADD CONSTRAINT leave_requests_attachments_array_chk CHECK (attachments IS NULL OR jsonb_typeof(attachments) = 'array');

INSERT INTO app_column_meta (table_name, column_name, ordinal, kind, null_as, pg_type, not_null, ts_type) VALUES
  ('tasks', 'assignee_ids', 60, 'text_array', 'absent', 'text[]', false, 'ID[]'),
  ('leave_requests', 'attachments', 23, 'json', 'absent', 'jsonb', false, 'LeaveAttachment[]');
