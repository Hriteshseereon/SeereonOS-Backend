# Fixtures — DEVELOPMENT AND TESTS ONLY

**Nothing in this directory is ever loaded into a production database.** The loader refuses to run when
`APP_ENV=production`, the Docker image does not contain it (`backend/app/devtools` is removed at build time), and every
run prints a warning.

| file | content |
|---|---|
| `demo/demo_seed.json.gz` | the demo data set: two demo workspaces (Seereon, a second tenant), a platform super admin, projects, WBS, timesheets, leave, payroll, invoices, CRM… Fake people and companies. Exported once (2026-10-19) from the frozen TypeScript seed generator (`frontend/src/mock/seed.ts` via `backend/devtools-ts/export_seed.mjs`). Keys: `generated_on`, `tz`, `source`, `dev_only`, `tables`. v8 final: `tenants.products` (V009) added for `tn_seereon` (its own 7 products) and `tn_kalinga` (1); the other demo tenants have none, like a new company (`frontend/src/mock/seed.ts` carries the same lists for a future re-export) |

Load it into an empty, migrated development database (owner role: `MIGRATION_DATABASE_URL`):

```sh
cd backend
python -m app.devtools.fixtures load-demo                    # dates shifted so the data looks current this week
python -m app.devtools.fixtures load-demo --today 2026-11-02 # …or as of a given day (golden / parity runs)
```

At load time the loader also brings the v8-era export up to the v9 data rules (`link_demo_rows`, idempotent): every
project gets its parent customer (`projects.client_id`, matched by name like V010's backfill — V010 runs before the
fixture is loaded) and every milestone with tasks gets the dates the sweep would write (start = earliest task start,
deadline = last task's due date), so every screen agrees from the first request.

Dates are shifted by **whole weeks** (export day → the latest same weekday on or before `--today`), in columns, inside
JSON values and inside ids such as `wk_<user>_<monday>`, so Mondays stay Mondays. Demo accounts use the password
`Seereon@123` (e.g. `santosh@seereon.co.in`, `platform@seereon.co.in`). `--in-place` replaces all data of a running
stack atomically (test and parity harness only); `--dump-json` writes the shifted tables (the parity harness loads the
same data into the TypeScript reference).

To change the demo data: change `frontend/src/mock/seed.ts`, re-export from the repository root with
`TZ=Asia/Kolkata node backend/devtools-ts/export_seed.mjs database/fixtures/demo/demo_seed.json.gz` (timestamps of the
generation instant change on every export), and regenerate the golden vectors (backend/README.md "Development").
