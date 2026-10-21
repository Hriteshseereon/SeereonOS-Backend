#!/usr/bin/env bash
# One-command local integration stack (development): fresh schema -> development demo fixture -> API (gunicorn, runtime
# role only) [+ worker] [+ the built frontend served from the same origin] -> wait for /readyz.
#   backend/tests/e2e/stack.sh [--no-reset] [--empty] [--port 8000] [--db seereonos] [--workers 2] [--worker]
#                              [--static frontend/dist] [--build] [--log FILE]
#   backend/tests/e2e/stack.sh --stop [--port 8000]
#   --empty   no demo fixture: an empty database, like a fresh production install (sign up to start)
#   --worker  also start python -m app.worker (e-mail delivery is disabled unless SMTP_URL is set)
#   --build   build the frontend first (npm --prefix frontend run build) and serve it (STATIC_DIR)
# Passwords of the local roles: STACK_OWNER_PASSWORD / STACK_APP_PASSWORD (default: the role names — local only).
# Several ports may share one DB (multi-instance tests): start the second one with --no-reset --port 8001.
set -euo pipefail
cd "$(dirname "$0")/../../.."
ROOT=$(pwd)
PORT=8000 DB=${STACK_DB:-seereonos} RESET=1 EMPTY=0 STOP=0 LOG="" WORKERS=${WEB_CONCURRENCY:-2} WORKER=0 STATIC="" BUILD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-reset) RESET=0 ;;
    --empty) EMPTY=1 ;;
    --stop) STOP=1 ;;
    --port) PORT="$2"; shift ;;
    --db) DB="$2"; shift ;;
    --workers) WORKERS="$2"; shift ;;
    --worker) WORKER=1 ;;
    --static) STATIC="$2"; shift ;;
    --build) BUILD=1; STATIC="${STATIC:-$ROOT/frontend/dist}" ;;
    --log) LOG="$2"; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
mkdir -p backend/var
PIDF="$ROOT/backend/var/stack-$PORT.pid"
WPIDF="$ROOT/backend/var/stack-$PORT.worker.pid"
LOG="${LOG:-$ROOT/backend/var/stack-$PORT.log}"
stop() {
  for f in "$PIDF" "$WPIDF"; do
    if [ -f "$f" ]; then
      local pid; pid=$(cat "$f")
      kill -TERM "$pid" 2>/dev/null || true   # gunicorn / worker: graceful
      for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
      kill -KILL "$pid" 2>/dev/null || true
      rm -f "$f"
    fi
  done
}
stop
[ "$STOP" = 1 ] && { echo "stopped :$PORT"; exit 0; }

pg_isready -q -h localhost -p 5432 || { pg_ctlcluster 16 main start 2>/dev/null || true; sleep 2; }
export MIGRATION_DATABASE_URL="postgres://seereon_owner:${STACK_OWNER_PASSWORD:-seereon_owner}@localhost:5432/$DB"
export DATABASE_URL="postgres://seereon_app:${STACK_APP_PASSWORD:-seereon_app}@localhost:5432/$DB"
export TZ=Asia/Kolkata APP_ENV=development APP_URL="${APP_URL:-http://localhost:$PORT}"
export LOG_LEVEL="${LOG_LEVEL:-info}" RATE_LIMIT_AUTH="${RATE_LIMIT_AUTH:-1000}" RATE_LIMIT_GLOBAL="${RATE_LIMIT_GLOBAL:-100000}"
[ -n "$STATIC" ] && export STATIC_DIR="$STATIC"

if [ "$RESET" = 1 ]; then
  echo "[stack] fresh schema ($DB)"; (cd backend && python3 -m app.db.reset >/dev/null)
  if [ "$EMPTY" = 0 ]; then echo "[stack] development demo fixture"; (cd backend && python3 -m app.devtools.fixtures load-demo >/dev/null); fi
fi
if [ "$BUILD" = 1 ]; then echo "[stack] build frontend"; NODE_ENV=production npm --prefix frontend run -s build >/dev/null; fi
echo "[stack] start API on :$PORT, $WORKERS workers (log $LOG)"
cd backend
setsid nohup python3 -m gunicorn app.main:app -c gunicorn.conf.py -w "$WORKERS" -b "0.0.0.0:$PORT" --pid "$PIDF" >>"$LOG" 2>&1 </dev/null &
if [ "$WORKER" = 1 ]; then
  setsid nohup python3 -m app.worker >>"$LOG" 2>&1 </dev/null &
  echo $! >"$WPIDF"
fi
cd "$ROOT"
for _ in $(seq 1 200); do
  if curl -sf "http://localhost:$PORT/readyz" >/dev/null; then echo "[stack] ready :$PORT pid $(cat "$PIDF")"; exit 0; fi
  sleep 0.2
done
echo "[stack] /readyz timeout" >&2; tail -30 "$LOG" >&2; exit 1
