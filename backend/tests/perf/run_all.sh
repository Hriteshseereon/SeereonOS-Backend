#!/usr/bin/env bash
# Full benchmark: for each tenant, restore the perf DB from its template, start the production-config server
# (gunicorn, 2 workers), run every scenario at concurrency 1/10/50; then cold start + memory (1 worker).
#   backend/tests/perf/run_all.sh <label> [tenants="demo big"] [conc=1,10,50]
# Results: backend/var/perf/<label>/{demo,big,cold}.json + logs. Needs the template DB seereonos_perf_tpl
# (python -m tests.perf.synthetic, then CREATE DATABASE seereonos_perf_tpl TEMPLATE seereonos_perf as a superuser);
# PERF_TEMPLATE picks another template (e.g. one with newer migrations applied).
set -euo pipefail
cd "$(dirname "$0")/../.."
LABEL=${1:?label}; TENANTS=${2:-"demo big"}; CONC=${3:-1,10,50}
OUT=${PERF_OUT_ROOT:-var/perf}/$LABEL; mkdir -p "$OUT"
restore() {
  tests/perf/serve.sh --stop --port 8300 || true
  tests/perf/serve.sh --stop --port 8301 || true
  su postgres -c "psql -q -c 'DROP DATABASE IF EXISTS seereonos_perf WITH (FORCE)' -c 'CREATE DATABASE seereonos_perf TEMPLATE ${PERF_TEMPLATE:-seereonos_perf_tpl} OWNER seereon_owner' -c 'GRANT CONNECT ON DATABASE seereonos_perf TO seereon_app'"
}
for T in $TENANTS; do
  restore
  PERF_LOG=$OUT/server-$T.log tests/perf/serve.sh --port 8300 --workers 2
  python3 -m tests.perf.bench --url http://localhost:8300 --tenant "$T" --conc "$CONC" --out "$OUT/$T.json" ${BENCH_ARGS:-} | tee "$OUT/$T.out"
  tests/perf/serve.sh --stop --port 8300
done
if [ "${COLD:-1}" = 1 ]; then
  restore
  PERF_LOG=$OUT/server-cold.log python3 -m tests.perf.coldstart --port 8301 --out "$OUT/cold.json" | tee "$OUT/cold.out"
fi
python3 -m tests.perf.report "$OUT" > "$OUT/summary.md" && cat "$OUT/summary.md"
