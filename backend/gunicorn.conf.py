"""gunicorn settings for the API — used by the Docker image (backend/Dockerfile) and the perf / e2e scripts; any
command-line flag overrides them. Every value reads an environment variable (PERFORMANCE.md, "Tuning knobs").

Workers are processes (CPU-bound Python): about one per vCPU. Each worker keeps its own tenant cache, so recycling a
worker (max_requests) throws its cache away — off by default; set MAX_REQUESTS only to contain a suspected leak.
"""

import os

worker_class = 'uvicorn.workers.UvicornWorker'  # uvloop + httptools (installed with uvicorn[standard])
workers = int(os.environ.get('WEB_CONCURRENCY', '2'))
bind = f'0.0.0.0:{os.environ.get("PORT", "8000")}'
timeout = int(os.environ.get('WORKER_TIMEOUT', '120'))  # a worker silent this long is restarted
graceful_timeout = int(os.environ.get('GRACEFUL_TIMEOUT', '20'))  # SIGTERM: finish in-flight requests
keepalive = int(os.environ.get('KEEPALIVE', '75'))  # > the load balancer's idle timeout (ALB 60 s)
max_requests = int(os.environ.get('MAX_REQUESTS', '0'))
max_requests_jitter = int(os.environ.get('MAX_REQUESTS_JITTER', '0'))
backlog = int(os.environ.get('BACKLOG', '2048'))
if os.path.isdir('/dev/shm'):
    worker_tmp_dir = '/dev/shm'  # heartbeat file in memory (a slow disk must not make the arbiter kill busy workers)
# Client addresses come from the app's own TRUST_PROXY rule (app/main.py client_ip): uvicorn must not rewrite the peer
# address from X-Forwarded-For itself (its default trusts any proxy on 127.0.0.1), or a same-host proxy / a forged
# header would bypass TRUST_PROXY=0 in the audit trail and the per-IP rate limits.
forwarded_allow_ips = ''
