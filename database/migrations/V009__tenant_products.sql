-- V009: per-tenant product master (v8 final). Hand-written and reviewed; never edit after it is applied — add V010.
--
-- The sales "Product" list used to be a fixed catalogue (the vendor's own products) for every company. Products are now
-- master data of each tenant: tenants.products holds [{id, name, stream, active}] (stream = the revenue stream a project
-- created from a won deal defaults to). NULL / absent = no products yet: a new company starts empty and adds its own
-- under Sales › Products (sales manager or admin). Deals keep the product name; renames are carried over by the API.
-- No data change for existing tenants (the development demo fixture brings its own list).

ALTER TABLE tenants ADD COLUMN products jsonb;
COMMENT ON COLUMN tenants.products IS 'import(''./entities'').TenantProduct[] | undefined';
ALTER TABLE tenants ADD CONSTRAINT tenants_products_array_chk CHECK (products IS NULL OR jsonb_typeof(products) = 'array');

INSERT INTO app_column_meta (table_name, column_name, ordinal, kind, null_as, pg_type, not_null, ts_type) VALUES
  ('tenants', 'products', 18, 'json', 'absent', 'jsonb', false, 'import(''./entities'').TenantProduct[]');
