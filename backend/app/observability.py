"""Error monitoring (Sentry, optional) and metrics (Prometheus text format at /metrics, protected by METRICS_TOKEN).

Sentry: SENTRY_DSN enables it (API and worker). send_default_pii=False, no request bodies, no cookies, and every
event is scrubbed with the log redaction rules (Authorization / passwords / tokens / secrets / payment fields) before it
leaves the process.

Metrics are per process (each gunicorn worker counts its own requests; the `pid` label tells them apart — sum them in
PromQL). Database-derived values (pool, e-mail queue, job runs, worker heartbeat) are read at scrape time and are the
same for every process.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger('seereonos')

# ---------------------------------------------------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------------------------------------------------
_SCRUB_KEYS = ('authorization', 'cookie', 'password', 'token', 'secret', 'signature', 'card', 'cvv', 'upi', 'vpa', 'otp', 'recovery', 'dsn')


def _scrub(v: Any, depth: int = 0) -> Any:
    from app.logging_setup import redact_text

    if depth > 8:
        return '[truncated]'
    if isinstance(v, dict):
        return {k: '[redacted]' if any(s in str(k).lower() for s in _SCRUB_KEYS) else _scrub(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x, depth + 1) for x in v]
    if isinstance(v, str):
        return redact_text(v)
    return v


def scrub_event(event: dict, _hint: Any = None) -> dict:
    """Sentry before_send: no request body / cookies / query string, headers and every value redacted"""
    req = event.get('request')
    if isinstance(req, dict):
        req.pop('data', None)
        req.pop('cookies', None)
        req.pop('query_string', None)
        if isinstance(req.get('headers'), dict):
            req['headers'] = {k: v for k, v in req['headers'].items() if k.lower() in ('user-agent', 'content-type', 'x-request-id', 'accept')}
    event.pop('user', None)
    return _scrub(event)


_sentry_on = False


def init_sentry(dsn: str | None, environment: str, release: str, traces_sample_rate: float = 0.0, component: str = 'api') -> bool:
    global _sentry_on
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - the dependency is pinned in requirements.txt
        log.warning('SENTRY_DSN is set but sentry-sdk is not installed; error monitoring is off')
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=f'seereonos@{release}',
        send_default_pii=False,
        traces_sample_rate=traces_sample_rate,
        before_send=scrub_event,
        before_send_transaction=scrub_event,
        max_request_body_size='never',
        include_local_variables=False,
    )
    sentry_sdk.set_tag('component', component)
    _sentry_on = True
    log.info('error monitoring on (Sentry)', extra={'environment': environment, 'component': component})
    return True


def capture_exception(e: BaseException, **tags: Any) -> None:
    if not _sentry_on:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, v)
            sentry_sdk.capture_exception(e)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------------------------------------------------
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, str], int] = {}  # (method, route, status class) -> count
        self.errors: dict[tuple[str, str], int] = {}  # (route, code class 5xx) -> count
        self.hist: dict[str, list[int]] = {}  # route -> bucket counts (+inf last)
        self.hist_sum: dict[str, float] = {}
        self.hist_count: dict[str, int] = {}
        self.jobs: dict[tuple[str, str], int] = {}  # (job, outcome) -> count (worker process)
        self.started = time.time()

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        cls = f'{status // 100}xx' if status else 'none'
        with self._lock:
            k = (method, route, cls)
            self.requests[k] = self.requests.get(k, 0) + 1
            if status >= 500 or status == 0:
                ek = (route, cls)
                self.errors[ek] = self.errors.get(ek, 0) + 1
            b = self.hist.get(route)
            if b is None:
                b = self.hist[route] = [0] * (len(BUCKETS) + 1)
            for i, le in enumerate(BUCKETS):
                if seconds <= le:
                    b[i] += 1
            b[-1] += 1
            self.hist_sum[route] = self.hist_sum.get(route, 0.0) + seconds
            self.hist_count[route] = self.hist_count.get(route, 0) + 1

    def job(self, name: str, outcome: str) -> None:
        with self._lock:
            k = (name, outcome)
            self.jobs[k] = self.jobs.get(k, 0) + 1

    def render(self, extra: list[str] | None = None) -> str:
        pid = str(os.getpid())
        out: list[str] = []

        def lbl(**kv: str) -> str:
            kv = {'pid': pid, **kv}
            return '{' + ','.join(f'{k}="{_esc(v)}"' for k, v in kv.items()) + '}'

        with self._lock:
            out += ['# HELP seereon_http_requests_total HTTP requests by method, route pattern and status class.', '# TYPE seereon_http_requests_total counter']
            for (m, r, c), n in sorted(self.requests.items()):
                out.append(f'seereon_http_requests_total{lbl(method=m, route=r, status=c)} {n}')
            out += ['# HELP seereon_http_errors_total Responses with status 5xx (or no response) by route pattern.', '# TYPE seereon_http_errors_total counter']
            for (r, c), n in sorted(self.errors.items()):
                out.append(f'seereon_http_errors_total{lbl(route=r, status=c)} {n}')
            out += ['# HELP seereon_http_request_duration_seconds Request latency by route pattern.', '# TYPE seereon_http_request_duration_seconds histogram']
            for r, b in sorted(self.hist.items()):
                for i, le in enumerate(BUCKETS):
                    out.append(f'seereon_http_request_duration_seconds_bucket{lbl(route=r, le=str(le))} {b[i]}')
                out.append(f'seereon_http_request_duration_seconds_bucket{lbl(route=r, le="+Inf")} {b[-1]}')
                out.append(f'seereon_http_request_duration_seconds_sum{lbl(route=r)} {self.hist_sum[r]:.6f}')
                out.append(f'seereon_http_request_duration_seconds_count{lbl(route=r)} {self.hist_count[r]}')
            if self.jobs:
                out += ['# HELP seereon_jobs_total Scheduled job runs in this worker process by outcome.', '# TYPE seereon_jobs_total counter']
                for (j, o), n in sorted(self.jobs.items()):
                    out.append(f'seereon_jobs_total{lbl(job=j, outcome=o)} {n}')
        out += ['# HELP seereon_process_start_time_seconds Start time of the process.', '# TYPE seereon_process_start_time_seconds gauge']
        out.append(f'seereon_process_start_time_seconds{lbl()} {self.started:.0f}')
        out += extra or []
        return '\n'.join(out) + '\n'


def _esc(v: str) -> str:
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def db_metrics(pools: Any) -> list[str]:
    """pool + queue + jobs + worker gauges (read at scrape time; errors become a db_up 0 gauge)"""
    out: list[str] = []
    st = pools.stats() if hasattr(pools, 'stats') else {}
    pid = os.getpid()
    out += ['# HELP seereon_db_pool Connection pool of this process (psycopg_pool stats).', '# TYPE seereon_db_pool gauge']
    for k in ('pool_size', 'pool_available', 'requests_waiting', 'pool_max', 'connections_num', 'requests_num', 'requests_errors', 'connections_errors'):
        if k in st:
            out.append(f'seereon_db_pool{{pid="{pid}",stat="{k}"}} {st[k]}')
    try:
        with pools.app.connection() as c:
            q = c.execute('SELECT status, n, oldest_due_s FROM seereon_email_queue_stats()').fetchall()
            jobs = c.execute(
                'SELECT job, runs, failures, extract(epoch FROM last_finished_at), last_duration_ms, last_status FROM job_runs ORDER BY job'
            ).fetchall()
            beat = c.execute('SELECT extract(epoch FROM now() - max(last_beat_at)), count(*) FROM worker_heartbeats').fetchone()
        out += ['# HELP seereon_db_up 1 when the database answered this scrape.', '# TYPE seereon_db_up gauge', 'seereon_db_up 1']
        out += ['# HELP seereon_email_queue Messages in the delivery queue by status.', '# TYPE seereon_email_queue gauge']
        oldest = 0.0
        for status, n, due in q:
            out.append(f'seereon_email_queue{{status="{status}"}} {int(n)}')
            if status == 'pending' and due is not None:
                oldest = max(oldest, float(due))
        out += ['# HELP seereon_email_oldest_due_seconds Age of the oldest due pending message.', '# TYPE seereon_email_oldest_due_seconds gauge']
        out.append(f'seereon_email_oldest_due_seconds {oldest:.0f}')
        out += [
            '# HELP seereon_job_runs_total Runs of each scheduled job (all workers).',
            '# TYPE seereon_job_runs_total counter',
        ]
        for j, runs, _f, _fin, _dur, _st in jobs:
            out.append(f'seereon_job_runs_total{{job="{j}"}} {int(runs)}')
        out += ['# HELP seereon_job_failures_total Failed runs of each scheduled job.', '# TYPE seereon_job_failures_total counter']
        for j, _r, failures, _fin, _dur, _st in jobs:
            out.append(f'seereon_job_failures_total{{job="{j}"}} {int(failures)}')
        out += ['# HELP seereon_job_last_success Last run of the job ended ok (1) or with an error (0).', '# TYPE seereon_job_last_success gauge']
        for j, _r, _f, _fin, _dur, st2 in jobs:
            out.append(f'seereon_job_last_success{{job="{j}"}} {1 if st2 == "ok" else 0}')
        out += ['# HELP seereon_job_last_finished_timestamp_seconds When each job last finished.', '# TYPE seereon_job_last_finished_timestamp_seconds gauge']
        for j, _r, _f, fin, _dur, _st in jobs:
            if fin is not None:
                out.append(f'seereon_job_last_finished_timestamp_seconds{{job="{j}"}} {float(fin):.0f}')
        age, n = beat if beat else (None, 0)
        out += [
            '# HELP seereon_worker_heartbeat_age_seconds Age of the newest worker heartbeat (-1: no worker).',
            '# TYPE seereon_worker_heartbeat_age_seconds gauge',
        ]
        out.append(f'seereon_worker_heartbeat_age_seconds {float(age):.0f}' if age is not None else 'seereon_worker_heartbeat_age_seconds -1')
        out.append(f'seereon_workers {int(n or 0)}')
    except Exception:  # noqa: BLE001
        out += ['# HELP seereon_db_up 1 when the database answered this scrape.', '# TYPE seereon_db_up gauge', 'seereon_db_up 0']
    return out
