-- SeereonOS — bootstrap for MANAGED PostgreSQL (Neon, AWS RDS, Azure, Supabase, …) where you do NOT have a superuser.
-- Same result as bootstrap.sql: ONE database and its TWO ROLES. Run it ONCE as the provider's admin role
-- (on Neon: neondb_owner, connected to the default database `neondb` through the DIRECT — non-pooled — host):
--
--   psql "postgresql://neondb_owner:<password>@<endpoint>.neon.tech/neondb?sslmode=require" \
--        -v ON_ERROR_STOP=1 -v dbname=seereonos \
--        -v owner_password="<strong password 1>" -v app_password="<strong password 2>" \
--        -f database/bootstrap_managed.sql
--
-- Passwords are REQUIRED (no defaults) and never stored in this file. Use long random values (Neon rejects weak ones),
-- e.g.  python -c "import secrets; print(secrets.token_urlsafe(24))"   (URL-safe: no escaping needed in connection URLs).
-- Re-running is safe: it re-applies grants and RESETS both passwords to the values you pass.
--
-- Roles (created here with SQL, so they are plain roles: not superuser, no BYPASSRLS, no CREATEDB/CREATEROLE)
--   seereon_owner  owns the schema; used ONLY to apply migrations (MIGRATION_DATABASE_URL).
--   seereon_app    the ONLY role of the running API/worker (DATABASE_URL); row-level security applies to it.
\set ON_ERROR_STOP on
\if :{?dbname}
\else
  \set dbname seereonos
\endif
\if :{?owner_password}
\else
  \echo 'ERROR: pass -v owner_password=... (no default password)'
  \quit 3
\endif
\if :{?app_password}
\else
  \echo 'ERROR: pass -v app_password=... (no default password)'
  \quit 3
\endif

SELECT 'CREATE ROLE seereon_owner LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'seereon_owner') \gexec
SELECT 'CREATE ROLE seereon_app LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'seereon_app') \gexec
ALTER ROLE seereon_owner WITH LOGIN PASSWORD :'owner_password';
ALTER ROLE seereon_app   WITH LOGIN NOINHERIT PASSWORD :'app_password';
ALTER ROLE seereon_app SET statement_timeout = '30s';
ALTER ROLE seereon_app SET idle_in_transaction_session_timeout = '60s';
-- the admin role may hand the new database to seereon_owner and act as it below (PostgreSQL 16+ rule)
SELECT format('GRANT seereon_owner TO %I WITH SET TRUE, INHERIT FALSE', current_user) \gexec

SELECT format('CREATE DATABASE %I OWNER seereon_owner ENCODING ''UTF8'' TEMPLATE template0', :'dbname') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec
-- keep the admin able to connect (re-runs, support) after PUBLIC loses CONNECT below
SELECT current_user AS admin_role \gset
SET ROLE seereon_owner;
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'dbname', :'admin_role') \gexec
RESET ROLE;

\connect :dbname
SET ROLE seereon_owner;
SELECT format('ALTER DATABASE %I OWNER TO seereon_owner', current_database()) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO seereon_owner', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO seereon_app', current_database()) \gexec
SELECT format('ALTER DATABASE %I SET timezone = ''UTC''', current_database()) \gexec
ALTER SCHEMA public OWNER TO seereon_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO seereon_owner;
GRANT USAGE ON SCHEMA public TO seereon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE seereon_owner IN SCHEMA public GRANT USAGE ON SEQUENCES TO seereon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE seereon_owner IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;
SELECT rolname, rolsuper, rolbypassrls, rolinherit FROM pg_roles WHERE rolname IN ('seereon_owner', 'seereon_app') ORDER BY 1;
\echo 'Bootstrap done. Next: python -m app.db.migrate with MIGRATION_DATABASE_URL (seereon_owner, direct host).'
