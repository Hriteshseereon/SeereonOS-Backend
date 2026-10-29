# SeereonOS — API and database

| folder | what |
|---|---|
| [backend/](backend/README.md) | the API (FastAPI, Python 3.11): `gunicorn` API process, `python -m app.worker`, `python -m app.db.migrate`, `python -m app.cli` |
| [database/](database/README.md) | PostgreSQL 16: migrations `V001…`, `bootstrap.sql` (superuser) / `bootstrap_managed.sql` (Neon and other managed PostgreSQL), backups, docs, development fixtures |

Every release: run the migrations **before** the new API starts (`cd backend && python -m app.db.migrate` with
`MIGRATION_DATABASE_URL` = the `seereon_owner` role on the direct, non-pooled host). The API refuses to start in
production while a migration is pending. Never commit `.env` / `.env.local` files or any credential.
