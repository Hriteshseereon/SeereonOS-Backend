#!/usr/bin/env bash
# Start the API with a production-like configuration for benchmarks (FREEZE_ROWS=false: rows are not frozen, no
# demo data), gunicorn + uvicorn workers, on a perf database.
#   backend/tests/perf/serve.sh [--db seereonos_perf] [--port 8300] [--workers 2] [--stop]
# Extra gunicorn flags: GUNICORN_EXTRA. Pass-through env: anything the app reads (TENANT_CACHE_MAX, PG_APP_POOL_MAX, ...).
set -euo pipefail
cd "$(dirname "$0")/../.."
BACKEND=$(pwd)
DB=seereonos_perf PORT=8300 WORKERS=2 STOP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --db) DB="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    --workers) WORKERS="$2"; shift ;;
    --stop) STOP=1 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
mkdir -p var
PIDF="$BACKEND/var/perf-$PORT.pid"
LOG="${PERF_LOG:-$BACKEND/var/perf-$PORT.log}"
if [ -f "$PIDF" ]; then
  pid=$(cat "$PIDF"); kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  kill -KILL "$pid" 2>/dev/null || true; rm -f "$PIDF"
fi
[ "$STOP" = 1 ] && exit 0
# runtime role only (as in production); development mode with production-like row handling (FREEZE_ROWS=false) so a
# local benchmark does not need SMTP / APP_URL / CORS / non-default role passwords
export DATABASE_URL="${PERF_DATABASE_URL:-postgres://seereon_app:seereon_app@localhost:5432/$DB}"
export APP_ENV=development FREEZE_ROWS=false TZ=Asia/Kolkata STATIC_DIR=""
export MFA_ENCRYPTION_KEY="${MFA_ENCRYPTION_KEY:-perf-benchmark-key-0123456789abcdefghijklmnopqrstuvwxyz}"
export LOG_LEVEL="${LOG_LEVEL:-info}" RATE_LIMIT_AUTH="${RATE_LIMIT_AUTH:-100000}" RATE_LIMIT_GLOBAL="${RATE_LIMIT_GLOBAL:-10000000}"
setsid nohup python3 -m gunicorn app.main:app -c "$BACKEND/gunicorn.conf.py" -w "$WORKERS" -b "0.0.0.0:$PORT" \
  --pid "$PIDF" ${GUNICORN_EXTRA:-} >>"$LOG" 2>&1 </dev/null &
for _ in $(seq 1 300); do
  if curl -sf "http://localhost:$PORT/readyz" >/dev/null; then echo "[perf] ready :$PORT ($WORKERS workers, db $DB)"; exit 0; fi
  sleep 0.2
done
echo "[perf] /readyz timeout" >&2; tail -30 "$LOG" >&2; exit 1
