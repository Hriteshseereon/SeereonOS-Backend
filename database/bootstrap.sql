-- SeereonOS — cluster bootstrap: ONE database and its TWO ROLES (roles, not databases). Idempotent; run as a
-- PostgreSQL superuser (or a role with CREATEROLE + CREATEDB) once per environment, then run the migrations.
--
--   psql -v ON_ERROR_STOP=1 -U postgres \
--        -v dbname=seereonos \
--        -v owner_password="$SEEREON_OWNER_PASSWORD" \
--        -v app_password="$SEEREON_APP_PASSWORD" \
--        -f database/bootstrap.sql
--
-- Passwords are REQUIRED (no defaults): pass them as psql variables from your secret store; they never appear in this
-- file or in the repository. For a throwaway local/test database you may pass simple values.
--
-- Roles
--   seereon_owner  owns the schema and every table/function. Used ONLY to apply migrations
--                  (MIGRATION_DATABASE_URL for `python -m app.db.migrate`) and by the development fixture loader.
--                  NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOBYPASSRLS.
--   seereon_app    the ONLY role of the running system (API, worker, production CLI): DATABASE_URL.
--                  NOSUPERUSER, NOBYPASSRLS, NOINHERIT, no DDL, statement_timeout 30 s. Reads/writes tenant tables under
--                  row-level security; reaches credentials, sessions, tokens, rate limits, the audit trail and the
--                  delivery queue only through the SECURITY DEFINER functions of V008 (see database/docs/README.md).
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
ALTER ROLE seereon_owner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'owner_password';
ALTER ROLE seereon_app   WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD :'app_password';
ALTER ROLE seereon_app SET statement_timeout = '30s';
ALTER ROLE seereon_app SET idle_in_transaction_session_timeout = '60s';

SELECT format('CREATE DATABASE %I OWNER seereon_owner ENCODING ''UTF8'' TEMPLATE template0', :'dbname') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec
SELECT format('ALTER DATABASE %I OWNER TO seereon_owner', :'dbname') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'dbname') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO seereon_owner', :'dbname') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO seereon_app', :'dbname') \gexec
SELECT format('ALTER DATABASE %I SET timezone = ''UTC''', :'dbname') \gexec

\connect :dbname
ALTER SCHEMA public OWNER TO seereon_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO seereon_owner;
GRANT USAGE ON SCHEMA public TO seereon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE seereon_owner IN SCHEMA public GRANT USAGE ON SEQUENCES TO seereon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE seereon_owner IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
-- gen_random_uuid() is built in (PostgreSQL 13+): no extensions are needed.
