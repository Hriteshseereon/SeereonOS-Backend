"""The worker process: scheduled jobs and e-mail delivery, separate from the API (its own compose service).

    python -m app.worker                 run forever (SIGTERM / SIGINT: finish the current job, then exit)
    python -m app.worker --once          run every job once and exit (cron, tests, a manual catch-up)
    python -m app.worker --once --jobs email,cleanup

Jobs (interval from the environment; every one is idempotent and reuses the SAME domain functions as the lazy
on-read paths of the API, inside the same unit of work — so the behaviour is identical whether a user opened the
page first or the worker got there first):
  email               deliver the queue (SMTP, retries with back-off, dead-letter)   EMAIL_INTERVAL_SECONDS (5)
  escalations         approval SLA escalations (leave, expenses, POs, bills, timesheets, task reviews) and
                      workflow-stage escalations                                    JOBS_INTERVAL_SECONDS (300)
  timesheet_locks     auto-lock open timesheet weeks past the lock rule               JOBS_INTERVAL_SECONDS
  milestones          milestone deadlines from tasks, empty-milestone cleanup         JOBS_INTERVAL_SECONDS
  appraisal_cycles    appraisal-cycle status by date                                  JOBS_INTERVAL_SECONDS
  exits               completed offboarding past the last day -> deactivate           JOBS_INTERVAL_SECONDS
  contract_renewals   renewal reminders when a contract enters the 60-day window      JOBS_INTERVAL_SECONDS
  cleanup             expired sessions / reset tokens / rate-limit windows / idempotency keys / change log /
                      audit retention / settled deliveries / expired pending tokens   CLEANUP_INTERVAL_SECONDS (3600)

No duplicates across instances: every periodic job runs under a PostgreSQL session advisory lock
(pg_try_advisory_lock; another worker that wants the same job at the same time skips that round) and, in the
scheduled loop, is skipped when another worker already started it within the current interval (job_runs, database
clock) — so N workers still run each job once per interval; the e-mail
queue is claimed with FOR UPDATE SKIP LOCKED. Liveness: a heartbeat row (worker_heartbeats) every 30 s, reported by
the API's /readyz; the last run of every job is in job_runs (and /metrics).
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import orjson

log = logging.getLogger('seereonos.worker')

HEARTBEAT_S = 30


@dataclass
class Job:
    name: str
    interval_s: int
    fn: Callable[[Worker], dict[str, Any]]
    locked: bool = True  # run under the job's advisory lock (one worker at a time across instances)
    next_at: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)


def _lock_key(name: str) -> str:
    return 'seereon.job.' + name


# ---------------------------------------------------------------------------------------------------------------------
# per-tenant sweeps: the lazy-on-read functions, run in a unit of work of each tenant
# ---------------------------------------------------------------------------------------------------------------------
def _escalations(ctx: Any) -> None:
    from app.routes.company import _sweep_escalations
    from app.routes.workflows_v7 import _sweep

    _sweep_escalations(ctx)
    _sweep(ctx)


def _timesheet_locks(ctx: Any) -> None:
    from app.routes.timesheets import sweep_locks

    sweep_locks(ctx)


def _milestones(ctx: Any) -> None:
    from app.engine.analytics import sweep_all

    sweep_all(ctx)


def _appraisal_cycles(ctx: Any) -> None:
    from app.routes.hr import _sweep_cycles

    _sweep_cycles(ctx)


def _exits(ctx: Any) -> None:
    from app.routes.hr import _sweep_exits

    _sweep_exits(ctx)


def _contract_renewals(ctx: Any) -> None:
    from app.routes.clients import _remind_renewals

    _remind_renewals(ctx)


TENANT_SWEEPS: dict[str, Callable[[Any], None]] = {
    'escalations': _escalations,
    'timesheet_locks': _timesheet_locks,
    'milestones': _milestones,
    'appraisal_cycles': _appraisal_cycles,
    'exits': _exits,
    'contract_renewals': _contract_renewals,
}


def run_tenant_sweep(rt: Any, tenant_id: str, sweep: Callable[[Any], None]) -> bool:
    """one sweep for one tenant in its own unit of work (flushed only when it changed something); True = it wrote"""
    from app.ctx import Ctx, find_by
    from app.js import REQUEST_NOW, request_clock_now

    wrote = {'v': False}

    def fn(scope: Any) -> None:
        tenant = find_by('tenants', 'id', tenant_id)
        if tenant is None:
            return
        ctx = Ctx.synthetic_ctx(tenant, {'id': 'system'})
        clock = REQUEST_NOW.set(request_clock_now())  # one instant for every timestamp written, like a request
        try:
            sweep(ctx)
        finally:
            REQUEST_NOW.reset(clock)
        wrote['v'] = bool(scope.write_intent)

    rt.run_scoped('tenant', tenant_id, fn, is_write=False)
    return wrote['v']


# ---------------------------------------------------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------------------------------------------------
class Worker:
    def __init__(self, cfg: Any, pools: Any = None, mailer: Any = None, worker_id: str | None = None) -> None:
        from app.auth.secret_box import create_secret_box
        from app.db.backend import PgBackend
        from app.db.cache import TenantCache
        from app.db.pool import Pools
        from app.engine.secrets import set_secret_box
        from app.router import Router, load_modules
        from app.runtime import Runtime
        from app.workers.email import EmailDelivery, create_mailer

        self.cfg = cfg
        self.id = worker_id or f'{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}'
        self.pools = pools or Pools(cfg.database_url, max(4, min(cfg.pool_max, 8)), cfg.statement_timeout_ms, application_name='seereonos-worker')
        backend = PgBackend(
            self.pools,
            queue_emails=cfg.email_delivery == 'smtp',
            delta_max=cfg.delta_max_rows,
            queue_platform_mails=cfg.email_delivery == 'smtp' or not cfg.production,
        )
        backend.init()
        router = Router()
        load_modules(router)
        # a small tenant cache: the worker reads each tenant once per round
        self.rt = Runtime(backend, router, TenantCache(max(1, min(cfg.tenant_cache_max, 10)), 256 * 1024 * 1024), freeze_rows=cfg.freeze_rows)
        set_secret_box(create_secret_box(cfg.mfa_encryption_key, list(cfg.mfa_encryption_keys_previous), allow_plain=not cfg.production))
        self.mailer = mailer if mailer is not None else (create_mailer(cfg.smtp_url, cfg.mail_from) if cfg.email_delivery == 'smtp' else None)
        self.delivery = EmailDelivery(self.pools.app, self.mailer, self.id, cfg.email_max_attempts, cfg.email_batch) if self.mailer is not None else None
        self.stop = threading.Event()
        self.started = time.time()
        from app.observability import Metrics

        self.metrics = Metrics()
        js = cfg.jobs_interval_s
        self.jobs: dict[str, Job] = {
            'email': Job('email', cfg.email_interval_s, Worker.job_email, locked=False),
            **{name: Job(name, js, (lambda w, n=name: w.job_sweep(n))) for name in TENANT_SWEEPS},
            'cleanup': Job('cleanup', cfg.cleanup_interval_s, Worker.job_cleanup),
        }

    # ---------------- jobs ----------------
    def job_email(self) -> dict[str, Any]:
        if self.delivery is None:
            return {'skipped': 'EMAIL_DELIVERY=disabled'}
        total: dict[str, int] = {}
        for _ in range(50):  # drain in batches while there is work (bounded per round)
            r = self.delivery.tick()
            for k, v in r.items():
                total[k] = total.get(k, 0) + v
            if r['claimed'] < self.delivery.batch or self.stop.is_set():
                break
        return total

    def job_sweep(self, name: str) -> dict[str, Any]:
        sweep = TENANT_SWEEPS[name]
        with self.pools.app.connection() as c:
            tenants = [r[0] for r in c.execute('SELECT id FROM seereon_job_tenants()').fetchall()]
        changed = errors = 0
        for tid in tenants:
            if self.stop.is_set():
                break
            try:
                if run_tenant_sweep(self.rt, tid, sweep):
                    changed += 1
            except Exception as e:  # noqa: BLE001 — one tenant's failure never stops the others
                errors += 1
                log.exception('tenant sweep failed', extra={'job': name, 'tenant_id': tid})
                from app.observability import capture_exception

                capture_exception(e, job=name, tenant_id=tid)
        out = {'tenants': len(tenants), 'changed': changed, 'errors': errors}
        if errors:
            raise JobPartialFailure(out)
        return out

    def job_cleanup(self) -> dict[str, Any]:
        with self.pools.app.connection() as c:
            rows = c.execute('SELECT what, deleted FROM seereon_cleanup(%s)', [self.cfg.security_events_retention_days]).fetchall()
        return {w: int(n) for w, n in rows}

    # ---------------- running ----------------
    def run_job(self, job: Job, scheduled: bool = False) -> dict[str, Any] | None:
        """run one job now (under its advisory lock when `locked`); None when another worker holds the lock, or — for a
        `scheduled` round of the run_forever loop — when another worker already ran it within this interval: the lock
        alone only keeps two runs from overlapping, so N workers would otherwise each run every job once per interval"""
        lock_conn = None
        if job.locked:
            lock_conn = self.pools.app.getconn()
            got = lock_conn.execute('SELECT pg_try_advisory_lock(hashtext(%s))', [_lock_key(job.name)]).fetchone()[0]  # type: ignore[index]
            if not got:
                self.pools.app.putconn(lock_conn)
                self.metrics.job(job.name, 'skipped_locked')
                return None
            if scheduled and self._ran_recently_elsewhere(lock_conn, job):
                try:
                    lock_conn.execute('SELECT pg_advisory_unlock(hashtext(%s))', [_lock_key(job.name)])
                finally:
                    self.pools.app.putconn(lock_conn)
                self.metrics.job(job.name, 'skipped_recent')
                return None
        t0 = time.monotonic()
        self._job_started(job.name)
        status, err, result = 'ok', None, None
        try:
            result = job.fn(self)
        except JobPartialFailure as e:
            status, err, result = 'error', 'some tenants failed (see logs)', e.result
        except Exception as e:  # noqa: BLE001
            status, err = 'error', f'{type(e).__name__}: {e}'
            log.exception('job failed', extra={'job': job.name})
            from app.observability import capture_exception

            capture_exception(e, job=job.name)
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.execute('SELECT pg_advisory_unlock(hashtext(%s))', [_lock_key(job.name)])
                finally:
                    self.pools.app.putconn(lock_conn)
        ms = int((time.monotonic() - t0) * 1000)
        self._job_finished(job.name, status, err, ms, result)
        self.metrics.job(job.name, status)
        job.stats = {'status': status, 'ms': ms, 'result': result}
        if status == 'ok' and job.name != 'email':
            log.info('job done', extra={'job': job.name, 'ms': ms, 'result': result})
        return result if result is not None else {}

    def _ran_recently_elsewhere(self, conn: Any, job: Job) -> bool:
        """another worker started this job less than ~one interval ago (database clock; 10% slack for timer jitter)"""
        try:
            row = conn.execute(
                'SELECT last_started_at > now() - make_interval(secs => %s) AND last_worker IS DISTINCT FROM %s FROM job_runs WHERE job = %s',
                [job.interval_s * 0.9, self.id, job.name],
            ).fetchone()
        except Exception:  # noqa: BLE001 — no job_runs row / transient error: run (the jobs are idempotent)
            log.warning('job_runs read failed', exc_info=True)
            return False
        return bool(row and row[0])

    def _job_started(self, name: str) -> None:
        try:
            with self.pools.app.connection() as c:
                c.execute(
                    """INSERT INTO job_runs (job, last_started_at, last_status, last_worker) VALUES (%s, now(), 'running', %s)
                       ON CONFLICT (job) DO UPDATE SET last_started_at = now(), last_status = 'running', last_worker = EXCLUDED.last_worker""",
                    [name, self.id],
                )
        except Exception:  # noqa: BLE001
            log.warning('job_runs update failed', exc_info=True)

    def _job_finished(self, name: str, status: str, err: str | None, ms: int, result: Any) -> None:
        from app.logging_setup import redact_text

        try:
            with self.pools.app.connection() as c:
                c.execute(
                    """UPDATE job_runs SET last_finished_at = now(), last_status = %s, last_error = %s, last_duration_ms = %s,
                              runs = runs + 1, failures = failures + CASE WHEN %s = 'error' THEN 1 ELSE 0 END, last_result = %s::jsonb
                        WHERE job = %s""",
                    [status, redact_text(err)[:1000] if err else None, ms, status, orjson.dumps(result).decode() if result is not None else None, name],
                )
        except Exception:  # noqa: BLE001
            log.warning('job_runs update failed', exc_info=True)

    def heartbeat(self) -> None:
        info = {'jobs': {k: j.stats for k, j in self.jobs.items() if j.stats}, 'email_delivery': self.cfg.email_delivery}
        with self.pools.app.connection() as c:
            c.execute(
                """INSERT INTO worker_heartbeats (worker_id, hostname, pid, started_at, last_beat_at, info)
                   VALUES (%s, %s, %s, to_timestamp(%s), now(), %s::jsonb)
                   ON CONFLICT (worker_id) DO UPDATE SET last_beat_at = now(), info = EXCLUDED.info""",
                [self.id, socket.gethostname(), os.getpid(), self.started, orjson.dumps(info, default=str).decode()],
            )

    def retire(self) -> None:
        try:
            with self.pools.app.connection() as c:
                c.execute('DELETE FROM worker_heartbeats WHERE worker_id = %s', [self.id])
        except Exception:  # noqa: BLE001
            pass

    def run_once(self, only: list[str] | None = None) -> dict[str, Any]:
        self.heartbeat()
        out: dict[str, Any] = {}
        for name, job in self.jobs.items():
            if only and name not in only:
                continue
            out[name] = self.run_job(job)
        return out

    def run_forever(self) -> None:
        last_beat = 0.0
        now = time.monotonic()
        for i, job in enumerate(self.jobs.values()):
            job.next_at = now + (0 if job.name in ('email', 'cleanup') else 5 + i)  # spread the first round
        log.info(
            'worker started', extra={'worker_id': self.id, 'jobs': {k: j.interval_s for k, j in self.jobs.items()}, 'email_delivery': self.cfg.email_delivery}
        )
        while not self.stop.is_set():
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_S:
                try:
                    self.heartbeat()
                    last_beat = now
                except Exception:  # noqa: BLE001
                    log.warning('heartbeat failed (database unavailable?)', exc_info=True)
            due = [j for j in self.jobs.values() if j.next_at <= now]
            for job in due:
                if self.stop.is_set():
                    break
                try:
                    self.run_job(job, scheduled=True)
                except Exception:  # noqa: BLE001 — e.g. the database is down: try again next round
                    log.exception('job round failed', extra={'job': job.name})
                job.next_at = time.monotonic() + job.interval_s
            nxt = min(j.next_at for j in self.jobs.values())
            self.stop.wait(max(0.2, min(HEARTBEAT_S, nxt - time.monotonic())))
        self.retire()
        log.info('worker stopped', extra={'worker_id': self.id})


def healthcheck(cfg: Any) -> int:
    """container health: a worker of THIS host beat recently (the DB is reachable and the loop is alive)"""
    import psycopg

    try:
        with psycopg.connect(cfg.database_url, connect_timeout=3) as c:
            age = c.execute(
                'SELECT extract(epoch FROM now() - max(last_beat_at)) FROM worker_heartbeats WHERE hostname = %s', [socket.gethostname()]
            ).fetchone()[0]  # type: ignore[index]
    except Exception as e:  # noqa: BLE001
        print(f'unhealthy: {type(e).__name__}', file=sys.stderr)
        return 1
    if age is None or float(age) > cfg.worker_stale_s:
        print(f'unhealthy: last heartbeat {age!r} s ago', file=sys.stderr)
        return 1
    return 0


class JobPartialFailure(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__('partial failure')
        self.result = result


def main(argv: list[str] | None = None) -> int:
    from app.config import ConfigError, load_config, set_settings
    from app.logging_setup import configure_logging
    from app.observability import init_sentry
    from app.startup import StartupError, check_database

    p = argparse.ArgumentParser(prog='python -m app.worker', description='SeereonOS worker: scheduled jobs and e-mail delivery')
    p.add_argument('--once', action='store_true', help='run every job once and exit')
    p.add_argument('--jobs', help='comma list of jobs (default: all): ' + ', '.join(['email', *TENANT_SWEEPS, 'cleanup']))
    p.add_argument('--healthcheck', action='store_true', help='exit 0 when a worker on this host beat within WORKER_STALE_SECONDS')
    a = p.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    set_settings(cfg)
    if a.healthcheck:
        return healthcheck(cfg)
    configure_logging(cfg.log_level)
    if os.environ.get('TZ') and hasattr(time, "tzset"):
        time.tzset()
    from app.main import VERSION

    init_sentry(cfg.sentry_dsn, cfg.sentry_environment or cfg.env, VERSION, 0.0, component='worker')
    from app.db.pool import Pools

    pools = Pools(cfg.database_url, max(4, min(cfg.pool_max, 8)), cfg.statement_timeout_ms, application_name='seereonos-worker')
    try:
        check_database(pools, cfg.production, 'worker')
    except StartupError as e:
        print(e, file=sys.stderr)
        pools.close()
        return 2
    if cfg.email_delivery == 'disabled':
        log.warning('EMAIL_DELIVERY=disabled: the e-mail job will not send anything')
    w = Worker(cfg, pools)
    only = [j.strip() for j in (a.jobs or '').split(',') if j.strip()] or None
    if only:
        unknown = [j for j in only if j not in w.jobs]
        if unknown:
            print(f'unknown job(s): {", ".join(unknown)}', file=sys.stderr)
            return 2
    try:
        if a.once:
            out = w.run_once(only)
            print(orjson.dumps(out, default=str, option=orjson.OPT_INDENT_2).decode())
            w.retire()
            return 0

        def _stop(_sig: int, _frm: Any) -> None:
            w.stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        if only:
            w.jobs = {k: v for k, v in w.jobs.items() if k in only}
        w.run_forever()
        return 0
    finally:
        pools.close()


if __name__ == '__main__':
    sys.exit(main())
