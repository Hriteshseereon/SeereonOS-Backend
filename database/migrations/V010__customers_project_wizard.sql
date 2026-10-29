-- V010: customers master, New Project wizard, escalation matrix, change requests sent to the team (v9 items 1, 2, 7;
-- decisions D2, D3). Hand-written and reviewed; never edit after it is applied — add V011+.
--
-- Customers (D2) = the existing clients master, extended. Every new field is optional (NULL = absent in the API row):
--   clients.registered_name     the registered business name (legal entity); clients.name stays the display name
--   clients.display_name        an optional short name for lists
--   clients.cin                 corporate identification number (21 characters)
--   clients.registered_address  {line, city, state, state_code, pincode} — the billing address keeps its own columns
--   clients.contacts            [{name, role, email, phone, primary}]
--   clients.payments_profile    {bank_name, account_name, account_no, ifsc, upi, payment_terms_days, billing_email};
--                               the API masks account_no unless the reader holds finance.edit
-- Projects get the parent customer: projects.client_id → clients.id; projects.client stays the display copy. Existing
-- projects are linked by matching the client name (case-insensitive); a project whose client is not in the master
-- yet (and is not an internal project) gets its customer row created first, exactly like the API's client sync does.
--   projects.client_details     {contact_name, contact_email, contact_phone, po_reference, notes} for this project
--   projects.escalation         {rows: [{level, user_id, name, role, email, phone, after_value, after_unit, scope}],
--                                notes, updated_at, updated_by} — the escalation matrix (wizard step 6, project tab)
--   projects.create_key         the wizard's request key: one wizard submission creates one project (idempotent)
--   milestones.details          additional details of a milestone (acceptance, scope notes) from the wizard
--   change_requests.sent_to     [{user_id, sent_at, sent_by, seen_at}] — team members the CR was sent to (item 7)
--   change_requests.sent_at / sent_by / send_note   the last send

ALTER TABLE clients ADD COLUMN registered_name text;
ALTER TABLE clients ADD COLUMN display_name text;
ALTER TABLE clients ADD COLUMN cin text;
ALTER TABLE clients ADD COLUMN registered_address jsonb;
ALTER TABLE clients ADD COLUMN contacts jsonb;
ALTER TABLE clients ADD COLUMN payments_profile jsonb;
ALTER TABLE clients ADD CONSTRAINT clients_registered_address_obj_chk CHECK (registered_address IS NULL OR jsonb_typeof(registered_address) = 'object');
ALTER TABLE clients ADD CONSTRAINT clients_contacts_array_chk CHECK (contacts IS NULL OR jsonb_typeof(contacts) = 'array');
ALTER TABLE clients ADD CONSTRAINT clients_payments_profile_obj_chk CHECK (payments_profile IS NULL OR jsonb_typeof(payments_profile) = 'object');
COMMENT ON COLUMN clients.registered_name IS '?: string — registered business name (V010)';
COMMENT ON COLUMN clients.display_name IS '?: string — short display name (V010)';
COMMENT ON COLUMN clients.cin IS '?: string — corporate identification number (V010)';
COMMENT ON COLUMN clients.registered_address IS '?: CustomerAddress (V010)';
COMMENT ON COLUMN clients.contacts IS '?: CustomerContact[] (V010)';
COMMENT ON COLUMN clients.payments_profile IS '?: CustomerPaymentsProfile (V010; account_no masked by the API without finance.edit)';

ALTER TABLE projects ADD COLUMN client_id text;
ALTER TABLE projects ADD COLUMN client_details jsonb;
ALTER TABLE projects ADD COLUMN escalation jsonb;
ALTER TABLE projects ADD COLUMN create_key text;
ALTER TABLE projects ADD CONSTRAINT projects_client_details_obj_chk CHECK (client_details IS NULL OR jsonb_typeof(client_details) = 'object');
ALTER TABLE projects ADD CONSTRAINT projects_escalation_obj_chk CHECK (escalation IS NULL OR jsonb_typeof(escalation) = 'object');
ALTER TABLE projects ADD CONSTRAINT projects_client_id_fk FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id) DEFERRABLE INITIALLY DEFERRED; -- clients are never hard-deleted (status churned)
CREATE INDEX projects_client_id_ix ON projects (tenant_id, client_id) WHERE client_id IS NOT NULL;
CREATE UNIQUE INDEX projects_create_key_uk ON projects (tenant_id, create_key) WHERE create_key IS NOT NULL;
COMMENT ON COLUMN projects.client_id IS '?: ID | null — the parent customer (V010); projects.client is the display copy';
COMMENT ON COLUMN projects.client_details IS '?: ProjectClientDetails (V010)';
COMMENT ON COLUMN projects.escalation IS '?: EscalationMatrix (V010)';
COMMENT ON COLUMN projects.create_key IS '?: string — New Project wizard request key (idempotent create, V010)';

ALTER TABLE milestones ADD COLUMN details text;
COMMENT ON COLUMN milestones.details IS '?: string — additional details from the New Project wizard (V010)';

ALTER TABLE change_requests ADD COLUMN sent_to jsonb;
ALTER TABLE change_requests ADD COLUMN sent_at timestamptz;
ALTER TABLE change_requests ADD COLUMN sent_by text;
ALTER TABLE change_requests ADD COLUMN send_note text;
ALTER TABLE change_requests ADD CONSTRAINT change_requests_sent_to_array_chk CHECK (sent_to IS NULL OR jsonb_typeof(sent_to) = 'array');
ALTER TABLE change_requests ADD CONSTRAINT change_requests_sent_by_fk FOREIGN KEY (tenant_id, sent_by) REFERENCES users (tenant_id, id) DEFERRABLE INITIALLY DEFERRED; -- users are never hard-deleted
COMMENT ON COLUMN change_requests.sent_to IS '?: { user_id: ID; sent_at: ISODateTime; sent_by: ID; seen_at: ISODateTime | null }[] (V010)';
COMMENT ON COLUMN change_requests.sent_at IS '?: ISODateTime — last time the CR was sent to the team (V010)';
COMMENT ON COLUMN change_requests.sent_by IS '?: ID — who sent it last (V010)';
COMMENT ON COLUMN change_requests.send_note IS '?: string — the note of the last send (V010)';

-- E-mail template keys were a fixed list; the change-request e-mail (key change_request_sent) is added by the API when
-- a CR is first sent. The key check becomes a format rule so later features can add templates without another
-- constraint rewrite (later migrations: do NOT recreate this check as a fixed list).
ALTER TABLE email_templates DROP CONSTRAINT email_templates_key_chk;
ALTER TABLE email_templates ADD CONSTRAINT email_templates_key_chk CHECK ("key" ~ '^[a-z][a-z0-9_]{1,63}$');

INSERT INTO app_column_meta (table_name, column_name, ordinal, kind, null_as, pg_type, not_null, ts_type) VALUES
  ('clients', 'registered_name', 27, 'text', 'absent', 'text', false, 'string'),
  ('clients', 'display_name', 28, 'text', 'absent', 'text', false, 'string'),
  ('clients', 'cin', 29, 'text', 'absent', 'text', false, 'string'),
  ('clients', 'registered_address', 30, 'json', 'absent', 'jsonb', false, 'CustomerAddress'),
  ('clients', 'contacts', 31, 'json', 'absent', 'jsonb', false, 'CustomerContact[]'),
  ('clients', 'payments_profile', 32, 'json', 'absent', 'jsonb', false, 'CustomerPaymentsProfile'),
  ('projects', 'client_id', 24, 'text', 'absent', 'text', false, 'ID | null'),
  ('projects', 'client_details', 25, 'json', 'absent', 'jsonb', false, 'ProjectClientDetails'),
  ('projects', 'escalation', 26, 'json', 'absent', 'jsonb', false, 'EscalationMatrix'),
  ('projects', 'create_key', 27, 'text', 'absent', 'text', false, 'string'),
  ('milestones', 'details', 18, 'text', 'absent', 'text', false, 'string'),
  ('change_requests', 'sent_to', 24, 'json', 'absent', 'jsonb', false, '{ user_id: ID; sent_at: ISODateTime; sent_by: ID; seen_at: ISODateTime | null }[]'),
  ('change_requests', 'sent_at', 25, 'timestamp', 'absent', 'timestamptz', false, 'ISODateTime'),
  ('change_requests', 'sent_by', 26, 'text', 'absent', 'text', false, 'ID'),
  ('change_requests', 'send_note', 27, 'text', 'absent', 'text', false, 'string');

-- ---------------------------------------------------------------------------------------------------------------------
-- Data: every external project gets its customer (created when missing, like the API's client sync), then the link.
-- Internal work (client "Internal" / "Seereon", the General & admin project) has no customer — the same rule as the
-- API's client sync (routes/clients.py INTERNAL).
-- ---------------------------------------------------------------------------------------------------------------------
INSERT INTO clients (tenant_id, "name", industry, country, gstin, billing_address, primary_contact, email, phone, owner_id, "status", since, payment_terms_days, health)
SELECT DISTINCT ON (p.tenant_id, lower(btrim(p.client)))
       p.tenant_id, btrim(p.client), '', 'India', '', '', '', '', '', p.pm_id, 'prospect', NULL, 30, NULL
  FROM projects p
 WHERE btrim(p.client) <> ''
   AND lower(btrim(p.client)) NOT IN ('internal', 'seereon', 'general & admin')
   AND p.code <> 'GEN' AND p.code NOT LIKE '%GEN'
   AND NOT EXISTS (SELECT 1 FROM clients c WHERE c.tenant_id = p.tenant_id AND lower(btrim(c."name")) = lower(btrim(p.client)))
 ORDER BY p.tenant_id, lower(btrim(p.client)), p._ord;

UPDATE projects p
   SET client_id = m.id
  FROM (SELECT DISTINCT ON (tenant_id, lower(btrim("name"))) tenant_id, id, lower(btrim("name")) AS k
          FROM clients ORDER BY tenant_id, lower(btrim("name")), _ord) m
 WHERE m.tenant_id = p.tenant_id AND m.k = lower(btrim(p.client)) AND p.client_id IS NULL;
