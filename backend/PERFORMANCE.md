# SeereonOS API — performance and scale

How the API reads and writes data, what it costs, how it was measured, what was changed, and how to run and scale it.
Every number below was measured on the development VM: **2 vCPU**, PostgreSQL 16 on the same machine, the API in its
production configuration (`APP_ENV=production`, gunicorn + uvicorn workers, `gunicorn.conf.py`), `TZ=Asia/Kolkata`.
Raw results: `backend/var/perf/<run>/` (not committed; re-create them with the commands in "Method").

## 1. Data path

```
client ─ HTTP/1.1 keep-alive ─ gunicorn master ─ N uvicorn workers (one process per vCPU)
   worker: asyncio loop (uvloop, httptools) → middleware (request id, CORS, security headers, rate limits, gzip)
           → /api/v1 gateway → thread pool (REQUEST_THREADS; writes also pass a WRITE_THREADS gate)
           → runtime.execute: session → idempotency → [response memo] → unit of work → handler → flush → envelope
```

* **Unit of work** (`app/db/uow.py`). An authenticated tenant request opens ONE `REPEATABLE READ READ ONLY` snapshot on
  the app role (RLS, `app.tenant_id`). The `BEGIN`, the `set_config` and the read of the tenant's
  `tenant_table_versions` go to PostgreSQL as one pipeline (one round trip). Tables load lazily, the first time the
  handler touches them.
* **Tenant cache** (`app/db/cache.py`, one per worker process). A table whose cached version equals the snapshot's
  version comes from the cache. Cached rows are immutable and shared by every request of the process, and are
  deep-frozen outside production. The cache is an LRU over tenants (`TENANT_CACHE_MAX`, `TENANT_CACHE_MAX_MB`).
* **Stale tables** are refreshed from the **change log** (`table_changes`, migration V007). Every flush records, per
  table, the version step it made and the ids it upserted or deleted. A worker holding version 41 of a table that is now
  at 44 reads only the rows named by steps 42–44 (`DELTA_MAX_ROWS`). If the chain has a gap (out-of-band SQL, a reset,
  a purged step), it re-reads the whole table. The log is an optimisation, never a source of truth.
* **Row mapping** (`app/db/mapping.py`). Each table gets a mapper compiled from its column metadata (a generated
  function: no per-cell type dispatch). `timestamptz` is rendered to the JS ISO form by PostgreSQL in the `SELECT`.
* **Derived values** (`Scope.shared`). Pure functions of whole tables are computed once per process and table
  versions, then shared: costed timesheet entries, task actuals, schedules, project metrics, approval KPIs, p50
  durations, costing analyses. Each declares the tables it depends on. In development and tests, reading an undeclared
  table raises an error. Costed entries survive a version change: rows that are the same objects as before keep their
  costed row, so a one-row write re-costs one row.
* **Writes.** Handlers never mutate rows in place: copy on write into a request overlay, with incrementally maintained
  overlay indexes. At the end the overlay is diffed by identity, then by value. It is flushed in one transaction on
  the same (single, v8) runtime role: advisory lock on the tenant, read-set version check, batched DELETE/UPDATE/INSERT
  under RLS, the change log, then the platform rows through the `SECURITY DEFINER` functions. Appends to tables the request never read (activity
  log, notifications, outbox) are inserted without loading those tables. After the commit, the cache is refilled from
  the request's own rows when the versions prove nobody else wrote in between. The table is not re-read.
* **Conflicts.** A lost race (another flush moved a table the request read) re-runs the request while holding the
  tenant lock; after 3 tries it returns 409 `CONFLICT_RETRY`. Under contention ("adaptive" lease mode) a worker that
  lost a race in the last `CONTENTION_WINDOW_S` takes the tenant lock *before* computing its next writes, so writers
  queue instead of computing, failing and computing again.
* **Response memo** (`app/memo.py`). This is a per-worker LRU of serialised GET responses, keyed by: tenant, route
  pattern, path, sorted query, user, `X-View-As`, `If-Match` and the date. An entry stores the **read set** of the
  request that produced it: every table its snapshot loaded, with versions. A hit requires all of those versions to be
  unchanged in the current snapshot, so any write to any of them, from any process or instance, makes the entry
  unreachable. The following are never stored:
  * requests that read the clock or randomness (`now_iso`, `now_ms`, `new_id`, `new_token`, the random helpers mark
    the request *volatile*);
  * requests that wrote or failed;
  * requests that touched sessions or credentials;
  * public, super-admin and auth-override routes.

  Entries also expire after a TTL (≤ 60 s) and are bounded by bytes (`MEMO_MAX_MB`). Concurrent identical misses are
  computed once (single flight). `MEMO_VERIFY=true` computes every hit fresh as well and logs any difference. The
  parity and equivalence runs use it.
* **Serialisation** is orjson in one pass. Bodies of 1 KB or more are gzipped once when the client accepts gzip, and
  a memo hit reuses the stored gzip bytes.
* **Large append-only tables** (`notifications`, `activity_log`) are not loaded just to filter them by user or entity.
  The notification list reads the user's rows with an index-backed query (`rows_where_sql`) when the table is not
  already loaded or cached. Activity lookups use the cached per-field index.

## 2. Method

```sh
cd backend
python -m tests.perf.synthetic --db seereonos_perf          # demo seed + tn_big + 18 demo-sized clones (~3 min)
su postgres -c "psql -c 'CREATE DATABASE seereonos_perf_tpl TEMPLATE seereonos_perf OWNER seereon_owner'"
tests/perf/run_all.sh <label> ["demo big"] [1,10,50]        # restore, serve (2 workers), bench, cold start, summary
python -m tests.perf.report var/perf/<before> var/perf/<after>   # before → after p95 table
python -m tests.perf.timeit [--only dash_ceo] [--profile dash_ceo]  # in-process compute time (memo off), cProfile
python -m tests.perf.profile GET /dashboards/ceo              # cProfile of one call
python -m tests.perf.equiv --ref <reference backend dir> [--no-twice]   # same calls on two builds, compared
```

* **Data.** `tn_seereon` is the demo seed: 11 users. `tn_big` ("Bigco", `tests/perf/synthetic.py`, deterministic) is
  the demo tenant cloned and amplified:
  * 200 users (10 PMs), 50 projects, 500 milestones, 19.8k tasks, 1.8k allocations;
  * 26k timesheet weeks with **146k entries over 3 years**;
  * 3.9k leave days and 1.5k leave requests;
  * invoices, payments and ledger rows for 3 years;
  * 523 deals with their activity;
  * 320 documents with versions;
  * **100k activity rows** and **50k notifications**, plus 5k outbox rows.

  Logins: `santosh@bigco.example` (CEO), `subhankar@bigco.example` (PM), `e0001@bigco.example` (employee), password
  `Seereon@123`. The 18 clones `tn_c01..18` are copies of the demo tenant for the memory test.
* **Load.** `tests/perf/bench.py` is an asyncio + httpx closed loop. At concurrency 1/10/50 each virtual user sends
  its next request when the previous one returns. A scenario runs up to 400 requests or 12 s, after a warm-up.
  Latency is measured at the client, so it includes queueing in the server.
  * Read scenarios: `/me`, the CEO/PM/employee dashboards, `/tasks` as the CEO and as an employee, the timesheet week,
    project detail, project costing, `/finance/margins`, `/finance/invoices`, `/insights`, `/scorecard`.
  * `POST /reports/run`: the hours dataset, last 90 days, by project.
  * Writes:
    * **log hours** (`PUT /timesheets/<monday>`, one employee per virtual user);
    * **approve week** (`POST /approvals/:id/approve` as the PM, on weeks reopened out of band);
    * **task create** (`POST /tasks` as the PM).
* **Server.** `tests/perf/serve.sh` runs gunicorn with 2 workers on 2 vCPU and `APP_ENV=production`, the same
  settings as the Docker image. The production checks forbid the default DB passwords in the URL; the script passes
  them as libpq parameters, for this local benchmark only.
* **Cold start and memory** (`tests/perf/coldstart.py`) use a single worker, so every number is one process:
  * RSS at boot;
  * first sign-in;
  * the first (cold) and second (warm) GET of 7 pages on the large tenant, then RSS;
  * a fresh worker that opens the same 7 pages on 20 demo-sized tenants, then RSS.
* **Before** is the code at the start of this work (commit `a748d2a`) on the same database template. **After** is
  this code with migration V007 applied.

## 3. Results

### 3.1 Large tenant (tn_big), 2 workers: p95 ms before → after

Target: read p95 < 300 ms at c=10, write p95 < 300 ms, zero errors at c=50.

| scenario | c=1 | c=10 | c=50 (errors) |
|---|---:|---:|---:|
| login | 121 → 145 | 1,364 → 1,812 | 64,953 (20) → 6,470 (0) |
| me | 11 → **4** | 65 → **74** | 738 → 682 |
| dashboard CEO | 6,214 → **4** | 42,695 → **64** | 84,165 (12) → 700 (0) |
| dashboard PM | 2,757 → **5** | 29,477 → **50** | 69,027 (8) → 748 (0) |
| dashboard employee | 899 → **3** | 4,113 → **40** | 26,291 → 687 |
| tasks (CEO) | 2,648 → **6** | 29,390 → **61** | 60,926 (9) → 1,022 (0) |
| tasks (employee) | 2,344 → **4** | 11,865 → **58** | 70,285 (8) → 548 (0) |
| timesheet week | 437 → **4** | 3,575 → **48** | 19,823 → 821 |
| project detail | 2,441 → **4** | 12,190 → **54** | 49,999 (10) → 625 (0) |
| project costing | 16 → **3** | 141 → **30** | 596 → 598 |
| finance margins | 1,340 → **3** | 6,637 → **50** | 40,379 → 654 |
| invoices list | 39 → **3** | 236 → **61** | 1,260 → 705 |
| insights | 652 → **3** | 3,680 → **35** | 29,929 → 612 |
| scorecard | 2,714 → **3** | 28,098 → **44** | 55,426 (10) → 626 (0) |
| report run (hours, 90 days) | 1,089 → **154** | 12,844 → 798 ✗ | 31,906 (25) → 4,862 (0) |
| log hours | 1,654 → 445 | 45,485 → 7,868 ✗ | 68,970 (30) → 27,430 (0) |
| approve week | 2,478 → **150** | 26,205 → 553 ✗ | 38,119 (50) → 3,393 (0) |
| task create | 2,607 → **44** | 28,013 → 415 ✗ | 34,994 (19) → 2,681 (0) |

The read rows above are what users see when the same view is opened repeatedly: 98% of the benchmark's reads were
memo hits (worker stats: 10,876 hits, 190 misses). **Every request is a memo miss after a write to a table it read.**
The compute cost of a miss, in process, warm cache, memo off (`tests.perf.timeit`, median of 3–5):

| endpoint (large tenant) | before | after | after, as a server miss at c=1 / c=10 (p95, memo off) |
|---|---:|---:|---:|
| dashboard CEO | 3,489 ms | **231 ms** | 326 / 2,293 ✗ |
| dashboard PM | 2,552 | **118** | 136 / 1,523 ✗ |
| dashboard employee | 579 | **73** | 68 / 479 ✗ |
| tasks (CEO, 1.4 MB) | 2,506 | **231** | 305 / 2,209 ✗ |
| tasks (employee) | 2,205 | **149** | 137 / 1,079 ✗ |
| timesheet week | 386 | **8** | 14 / 93 |
| project detail | 2,205 | **97** | 93 / 939 ✗ |
| project costing | 8 | 8 | 14 / 99 |
| finance margins | 1,146 | **37** | 51 / 299 |
| invoices list | 20 | 18 | 23 / 191 |
| insights | 535 | **13** | 20 / 166 |
| scorecard | 2,153 | **412** | 465 / 3,192 ✗ |
| report run | 911 | **120** | 140 / 1,051 ✗ |
| log hours (write) | 1,405 | **401** | |
| approve week (write) | 514 | **46** | |
| task create (write) | 592 | **33** | |

### 3.2 Demo tenant, 2 workers: p95 ms before → after

| scenario | c=1 | c=10 | c=50 (errors) |
|---|---:|---:|---:|
| login | 145 → 132 | 1,552 → 1,330 | 36,619 (18) → 7,445 (0) |
| me | 9 → **5** | 74 → **59** | 960 → 667 |
| dashboard CEO | 80 → **4** | 476 → **48** | 2,593 → 594 |
| dashboard PM | 55 → **4** | 327 → **41** | 1,624 → 698 |
| dashboard employee | 15 → **4** | 122 → **44** | 717 → 683 |
| tasks (CEO) | 42 → **3** | 305 → **37** | 1,263 → 702 |
| tasks (employee) | 18 → **3** | 200 → **47** | 768 → 690 |
| timesheet week | 12 → **3** | 91 → **37** | 960 → 700 |
| project detail | 22 → **3** | 222 → **52** | 779 → 825 |
| project costing | 8 → **3** | 60 → **39** | 715 → 685 |
| finance margins | 25 → **3** | 334 → **39** | 1,465 → 729 |
| invoices list | 5 → 5 | 51 → **41** | 676 → 719 |
| insights | 22 → **3** | 129 → **48** | 920 → 548 |
| scorecard | 29 → **4** | 200 → **44** | 1,338 → 599 |
| report run | 26 → **13** | 151 → 105 ✗ | 1,108 → 522 |
| log hours | 28 → **19** | 666 → 217 ✗ | 3,743 → 924 |
| approve week | 14 → **11** | 374 → 121 ✗ | 1,150 → 412 |
| task create | 19 → **12** | 650 → 214 ✗ | 5,615 → 685 |

At c=50 every scenario queues: 50 clients share 2 vCPU. The p95 there is the queue length times the service time. The
point of that column is **zero errors**: before, 18–50 requests per scenario failed with 500s (advisory-lock waits hit
`statement_timeout`) and worker timeouts. After, there are none.

### 3.3 Cold start and memory (one worker)

| | before | after |
|---|---:|---:|
| RSS at boot | 82 MB | 83 MB |
| RSS with the large tenant cached (7 pages opened) | 529 MB | 546 MB (peak under load: 577 MB) |
| large tenant, first open: `/dashboards/ceo` | 6.7 s (warm: 3.4 s) | 2.4 s (warm: 0.5 s, then memo) |
| large tenant, first open: `/tasks`, `/dashboards/pm`, `/scorecard` | 2.6 / 3.9 / 2.0 s | 0.24 / 0.40 / 0.36 s |
| RSS with 20 demo-sized tenants cached | 148 MB | 173 MB (~4.5 MB per tenant) |
| 7 pages per demo-sized tenant, cold / warm | — | 35–154 ms / 16–78 ms |
| first sign-in | 286 ms | 134 ms (scrypt ~100 ms by design) |

### 3.4 Equivalence and parity

* `backend/tests/e2e` parity (TypeScript mock vs API, demo seed), full SWEEP with every group scenario: 4,966/4,966
  equal. `PARITY_GET_TWICE=1 MEMO_VERIFY=1` calls every GET twice on both sides: 0 memo diffs, 2,105 verified memo
  hits, 0 mismatches.
* Large tenant (the mock cannot hold it): `tests/perf/equiv.py` runs the reference build (`a748d2a`) and this build,
  each on its own copy of the same database. It sends the same calls to both and compares the normalised responses:
  * every GET route of the registry as the CEO, the PM and an employee of `tn_big` and of `tn_seereon`;
  * the writes (log hours, approve 2 weeks, create a task, run a report);
  * re-reads of 10 pages after the writes.

  Result: **1,118/1,118 equal** (`--no-twice`). With every GET called twice on the new build, the second answer
  always equals the first (0 differences). The only cross-build differences are the extra side effects of the extra
  GET: document `view_count` and a notification.
* pytest, including `tests/integration/test_perf_paths.py`, covers:
  * delta refresh equals a full read, and a gap falls back to the full read;
  * overlay indexes equal a rebuild (property test);
  * the flush diff equals the reference diff;
  * generated mappers equal the generic mapper;
  * SQL timestamp rendering equals the loader on edge values;
  * memo hits, staleness, keys and volatility;
  * undeclared shared reads raise;
  * costed-row reuse equals a recompute;
  * appends without loading;
  * conflict semantics and lease-first after a lost race;
  * pushdown reads equal table reads.

### 3.5 Targets: met and missed

| target | result |
|---|---|
| zero errors at c=50 | **met** on both tenants, every scenario (before: 8–50 errors per heavy scenario) |
| demo p95 < 100 ms (reads, c=10) | **met**: 37–59 ms |
| large-tenant read p95 < 300 ms at c=10 | **met for repeated views** (30–74 ms, memo hits). **Missed for memo misses** on the heaviest pages: the CEO dashboard, `/tasks` (CEO), the scorecard and project detail compute in 100–410 ms, so 10 concurrent misses on 2 vCPU queue to 0.9–3.2 s. Also missed for `POST /reports/run` (798 ms; it is a POST and is never memoised). |
| write p95 < 300 ms (c=10) | **Missed on the large tenant**: task create 415 ms, approve 553 ms, log hours 7.9 s. **Missed on the demo tenant**: 121–217 ms against the 100 ms demo target. At c=1: 44 / 150 / 445 ms. |

Why the large-tenant writes miss: writes to one tenant are serialised (the tenant advisory lock plus the read-set
check), so p95 at c=10 is about 10 × the single-write cost. Logging hours re-derives costed entries and task actuals
(about 146k rows) and diffs the timesheet table. The work is O(rows in the tenant's timesheet), about 400 ms in
process. The next levers, in order of effect:

1. **Incremental derived values for writes**: apply a write's changed entries to task actuals / costed entries as a
   delta instead of a pass over 146k rows. Float sums would have to be kept order-identical to stay byte-equal with the
   reference.
2. **Row-level read sets for writes**: a timesheet PUT reads only the user's week. Recording `(table, ids)` instead
   of `(table, version)` would let different users' writes run in parallel instead of conflicting.
3. **Move the costed/actuals aggregates to SQL** (a materialised `task_actuals` maintained by the flush), see §6.
4. For the memo-miss reads: the CEO dashboard and `/tasks` (CEO) spend most of their time building per-task view
   objects for ~20k tasks. Paginating `/tasks` server-side (an API change: the frontend loads every task) would make it
   O(page).

## 4. What changed (behaviour unchanged)

| area | change | where |
|---|---|---|
| round trips | `BEGIN` + tenant setting + versions in one pipeline; server-side prepared statements after 5 runs | `db/backend.py`, `db/pool.py` |
| mapping | generated per-table row mappers; timestamps rendered in SQL; `date` read as text | `db/mapping.py`, `db/pool.py` |
| version check / refresh | change log (V007) + gapless delta refresh; refill after own flush without re-read; no cache invalidation on conflicts | `db/backend.py`, `runtime.py`, `migrations/V007__table_changes.sql` |
| O(n·m) → indexes | `rows_where` via group indexes (analytics, costing, dashboards, reports, hr, spend, invoices, taskflow); overlay indexes maintained per write op | `ctx.py`, `db/uow.py`, engines/routes |
| memoised pure computations | shared derived values keyed by table versions with declared dependencies; costed-row reuse across versions; per-request memos (rates, SLA due dates, working days) | `db/uow.py`, `db/cache.py`, `engine/analytics.py`, `engine/costing.py`, `engine/approval_kpi.py`, `routes/dashboards.py` |
| SQL pushdown | notification lists read by an index-backed query; appends to unread tables flushed without loading them; report period prefilter | `db/uow.py`, `routes/notifications.py`, `reports/engine.py` |
| response memo | exact, version-validated GET memo; single flight | `memo.py`, `runtime.py` |
| responses | orjson once, gzip ≥ 1 KB, cached gzip on memo hits | `main.py` |
| concurrency | request thread budget, write gate, scrypt gate (logins cannot starve the workers), adaptive lease-first after conflicts, single flight for table loads and shared values | `main.py`, `auth/passwords.py`, `runtime.py`, `db/uow.py` |
| server | `gunicorn.conf.py` (uvloop/httptools, timeouts, keep-alive, backlog, heartbeat in /dev/shm); Dockerfile uses it | `gunicorn.conf.py`, `Dockerfile` |

Volatility is explicit: `now_iso` / `now_ms` / `new_id` / `new_token` / the random helpers call `mark_volatile()`.
The dispatcher's request clock uses `request_clock_now()`, which does not mark, because the request start instant
reaching a response is itself a clock read inside the handler. A new handler that reads time some other way (for
example `datetime.now()` directly) **must** call `app.js.mark_volatile()`, or its GET responses could be reused for up
to `MEMO_TTL_S`. `MEMO_VERIFY=true` in the parity run catches that.

## 5. Tuning knobs

All are environment variables (`.env.example`); gunicorn's are read by `gunicorn.conf.py`.

| variable | default | meaning / guidance |
|---|---|---|
| `WEB_CONCURRENCY` | 2 | worker processes. CPU-bound Python: one per vCPU. Each worker has its own tenant cache and memo (RSS below). |
| `REQUEST_THREADS` | 4 | handler threads per worker. More only adds GIL contention; 4 lets I/O (PG) overlap. |
| `WRITE_THREADS` | 2 | of those, how many may run writes at once (reads keep flowing during write bursts) |
| `LEASE_MODE` / `CONTENTION_WINDOW_S` | adaptive / 10 | `adaptive`: lock-first only after a recent conflict; `optimistic`: never; `pessimistic`: every write |
| `MEMO_MAX_MB` / `MEMO_TTL_S` | 64 / 60 | response memo per worker (0 = off); TTL ≤ 60 s |
| `MEMO_VERIFY` | false | tests only: compute every hit too and compare |
| `TENANT_CACHE_MAX` / `TENANT_CACHE_MAX_MB` | 50 / 512 | tenants per worker cache (LRU) and the estimated row-byte budget (the large tenant: ~190 MB estimated) |
| `DELTA_MAX_ROWS` | 2000 | change-log refresh up to this many changed rows, else full re-read |
| `PG_POOL_MAX` | 20 | connections per API worker process (one pool, runtime role; ≥ `REQUEST_THREADS` + `WRITE_THREADS` + 2). The `python -m app.worker` process uses 4–8 |
| `PG_PREPARE_THRESHOLD` | 5 | psycopg server-side prepare after N runs; `off` for PgBouncer < 1.21 in transaction mode |
| `STATEMENT_TIMEOUT_MS` | 30000 | per statement on the API's pool; the `seereon_app` role also has `statement_timeout = 30s` |
| `SCRYPT_CONCURRENCY` | 1 | password hashes at once per worker (~100 ms CPU + 32 MB each) |
| `GZIP_MIN_BYTES` / `GZIP_LEVEL` | 1024 / 5 | response compression |
| `WORKER_TIMEOUT` / `GRACEFUL_TIMEOUT` / `KEEPALIVE` | 120 / 20 / 75 | gunicorn; keep-alive above the load balancer's idle timeout (ALB 60 s) |
| `MAX_REQUESTS` / `MAX_REQUESTS_JITTER` | 0 / 0 | recycle workers (throws the cache away: leave off unless containing a leak; then e.g. 20000 / 2000) |

PostgreSQL: `max_connections` ≥ instances × workers × (`PG_POOL_MAX` + `PG_APP_POOL_MAX`) + headroom. That is 60 for one
2-worker instance at the defaults; above about 4 instances, use PgBouncer. `shared_buffers` should hold the hot tenants'
tables; the whole perf database (demo + large tenant + 18 clones) is ~180 MB with indexes.

## 6. Capacity and scaling playbook

**Per instance (2 vCPU, 2 workers), measured:**

* repeated views (memo hits): mostly **~450–680 req/s** across the dashboards and lists (150–230 req/s for the CEO dashboard and `/tasks` (CEO): their runs include misses, e.g. at TTL expiry, that cost ~230 ms of CPU each), p95 < 75 ms at c=10;
* compute-bound misses on the large tenant: ~4–8 req/s for the CEO dashboard, `/tasks` (CEO) and the scorecard; 13–90
  req/s for project/margins/insights/costing; demo-sized tenants are ~10× cheaper;
* writes, per tenant (serialised): large tenant ~2.3 log-hours/s, ~24 approvals/s, ~29 task creates/s; demo-sized
  ~65 / 145 / 96 per second. Writes to *different* tenants run in parallel;
* sign-ins: ~9/s (scrypt, deliberately slow; `SCRYPT_CONCURRENCY` caps the CPU it can take);
* memory: ~85 MB per idle worker, +~4.5 MB per demo-sized tenant, ~470 MB for a 150k-entry tenant. A 2-worker
  instance serving a dozen ordinary tenants and one very large one needs ~1.5 GB.

**Scaling, in order:**

1. **Workers = vCPUs.** Every worker caches the tenants it serves, so RAM grows with workers × hot tenants.
2. **More instances** behind the load balancer. Everything shared is in PostgreSQL: sessions, rate limits,
   idempotency, the outbox and the change log. Caches stay coherent through `tenant_table_versions`, and the change
   log keeps refreshes cheap. **Tenant-affinity routing** (hash the tenant, or the token's tenant, to an instance)
   multiplies the effective cache and memo capacity: each instance then holds only its tenants.
3. **PgBouncer** in transaction mode once instances × workers × pool size approaches `max_connections`. It is
   compatible because each request's transaction is self-contained: `set_config(..., true)` and `SET LOCAL ROLE` are
   transaction-scoped, and the advisory lock is `pg_advisory_xact_lock`. Use `PG_PREPARE_THRESHOLD=off` with PgBouncer
   older than 1.21, or enable `max_prepared_statements` on 1.21+.
4. **Read replica**: possible for the snapshot reads (the app pool). The flush must stay on the primary, and the version
   check and change log must be read from the primary or a synchronous replica, or a stale snapshot would be cached as
   current. Not needed below ~10 instances: cached reads barely touch PostgreSQL.
5. **When to move analytics to SQL.** Do it when a tenant's derived values cost more than ~300 ms to compute after a
   write, or more than ~1 GB per worker; the 150k-entry tenant is at about that level today. Candidates:
   * costed timesheet entries and task actuals as tables maintained by the flush (or materialised views refreshed per
     write);
   * report datasets as SQL aggregates over `(tenant_id, work_date)`;
   * the per-task view objects of `/tasks`, paginated.

   Do these one engine at a time, behind the parity and equivalence harnesses.
6. **Partitioning: not yet.** Every hot path is an index scan; the biggest table (150k timesheet rows per large tenant)
   loads in ~65 ms of PostgreSQL time, and the cost is in the API. Revisit when a table passes ~50–100M rows, or when
   retention needs cheap drops: range-partition `activity_log` / `notifications` (and `table_changes`, already purged
   every few minutes) by month, with the PK widened to `(id, at)` (database/docs/README.md "Performance").

## 7. Operating notes

* Worker stats (memo hits/misses/stale, cache hits/misses/bytes, conflicts, lease-first flushes) are logged at
  shutdown as `worker stats`. A low memo hit ratio with a high stale count means writes are invalidating the read sets.
  That is expected after writes, and is the case the compute numbers in §3.1 describe.
* `table_changes` holds about 10 minutes of history: the sweeper purges older rows. A worker idle for longer re-reads
  its stale tables once.
* Out-of-band SQL writes are safe: they bump `tenant_table_versions` but write no change-log rows, so readers detect
  the gap and re-read the whole table.
