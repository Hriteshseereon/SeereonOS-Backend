# SeereonOS database deliverable (PostgreSQL 16)

ONE database, TWO roles: `seereon_owner` (owns the schema; used only by the migrate job) and `seereon_app` (the only
role of the running API, worker and CLI; row-level security on every tenant table; credentials reachable only through
`SECURITY DEFINER` functions). Roles belong to the PostgreSQL cluster: use one cluster per environment, because running
`bootstrap.sql` again on a cluster (for any database name) resets both role passwords there.

```sh
# 1. once per environment, as a superuser — passwords are REQUIRED psql variables (no defaults, never in a file)
psql -v ON_ERROR_STOP=1 "postgres://postgres@db.internal:5432/postgres" -v dbname=seereonos \
     -v owner_password="$SEEREON_OWNER_PASSWORD" -v app_password="$SEEREON_APP_PASSWORD" -f database/bootstrap.sql
# 2. every release, before the new API/worker start (from backend/, or the image's migrate job)
MIGRATION_DATABASE_URL=postgres://seereon_owner:…@db.internal:5432/seereonos python -m app.db.migrate
# 3. the running system: DATABASE_URL=postgres://seereon_app:…@db.internal:5432/seereonos
# 4. daily encrypted backup + weekly scripted restore test
BACKUP_DATABASE_URL=… BACKUP_AGE_RECIPIENT=age1… database/scripts/backup.sh --out /var/backups/seereonos
ADMIN_DATABASE_URL=… RESTORE_OWNER_URL=… RESTORE_APP_URL=… BACKUP_AGE_IDENTITY=… database/scripts/restore_test.sh --latest /var/backups/seereonos
```

| path | |
|---|---|
| [docs/README.md](docs/README.md) | **the manual**: setup, roles and the security boundary, RLS, conventions, migration policy, backups, restore, monitoring queries, file storage, data dictionary, performance |
| [docs/ERD.md](docs/ERD.md) | entity-relationship diagrams |
| `bootstrap.sql` | roles + database + grants (idempotent; re-run to rotate the passwords) |
| `migrations/V001…V009` | the schema; applied in order, checksummed — never edit an applied file, add `V010__…` |
| `scripts/backup.sh`, `restore.sh`, `restore_test.sh` | encrypted `pg_dump -Fc` with retention; restore into an empty database; scripted restore test |
| `fixtures/` | **development / test only** demo data ([fixtures/README.md](fixtures/README.md)); never loaded in production |
