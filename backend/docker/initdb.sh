#!/bin/sh
# postgres image init hook (first start of an empty data volume): ONE database and its two roles via
# database/bootstrap.sql, passwords from the environment (never defaults). Migrations run afterwards in the
# `migrate` job (backend/deploy/docker-compose.yml).
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
  -v dbname="${SEEREON_DB:-seereonos}" \
  -v owner_password="${SEEREON_OWNER_PASSWORD:?set SEEREON_OWNER_PASSWORD}" \
  -v app_password="${SEEREON_APP_PASSWORD:?set SEEREON_APP_PASSWORD}" \
  -f /seereon/bootstrap.sql
