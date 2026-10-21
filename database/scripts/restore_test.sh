#!/usr/bin/env bash
# SeereonOS — SCRIPTED RESTORE TEST: proves a backup can actually be restored. Restores into a throwaway database on
# the same (or a staging) cluster, checks it, drops it. Run it weekly from cron and alert on a non-zero exit.
#
#   database/scripts/restore_test.sh --from-url URL     take a fresh encrypted backup of URL (backup.sh) and restore it;
#                                                       row counts are compared EXACTLY with the source, counted in the
#                                                       very snapshot that was dumped (safe on a live database)
#   database/scripts/restore_test.sh --file FILE        restore an existing backup (.dump / .dump.age / .dump.gpg)
#   database/scripts/restore_test.sh --latest DIR       restore the newest backup in DIR (what cron should run)
#   options: --keep (do not drop the throwaway database)  --report FILE (write a JSON result for monitoring)
#
# Environment
#   ADMIN_DATABASE_URL  a role that may CREATE/DROP DATABASE with OWNER seereon_owner: a superuser, or a dedicated ops
#                       role (LOGIN CREATEDB, member of seereon_owner). Any database name in the URL (e.g. /postgres).
#   RESTORE_OWNER_URL   the owner role on the same cluster (its database name is replaced by the throwaway one);
#                       default MIGRATION_DATABASE_URL
#   RESTORE_APP_URL     optional: the runtime role (DATABASE_URL); when set, row-level security and the privilege
#                       split are checked through it on the restored copy
#   BACKUP_AGE_IDENTITY for --file / --latest with .age backups (--from-url uses a throwaway age key it generates)
# Checks: checksum, archive validity, restore in one transaction, schema_migrations vs database/migrations (checksums
# identical; pending files reported for older backups), every table of app_table_meta present, row-level security on
# every tenant table, SECURITY DEFINER functions not executable by PUBLIC, seereon_app has no direct access to
# credentials, exact row counts (--from-url), and — with RESTORE_APP_URL — RLS through the runtime role.
set -euo pipefail
umask 077
HERE=$(cd "$(dirname "$0")" && pwd)
MIGRATIONS=${MIGRATIONS_DIR:-$HERE/../migrations}

MODE="" ARG="" KEEP=0 REPORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from-url) MODE=url; ARG="$2"; shift ;;
    --file) MODE=file; ARG="$2"; shift ;;
    --latest) MODE=latest; ARG="$2"; shift ;;
    --keep) KEEP=1 ;;
    --report) REPORT="$2"; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "restore_test: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "restore_test: one of --from-url URL | --file FILE | --latest DIR" >&2; exit 2; }
ADMIN=${ADMIN_DATABASE_URL:-}
OWNER=${RESTORE_OWNER_URL:-${MIGRATION_DATABASE_URL:-}}
[ -n "$ADMIN" ] || { echo "restore_test: set ADMIN_DATABASE_URL (may create/drop databases)" >&2; exit 2; }
[ -n "$OWNER" ] || { echo "restore_test: set RESTORE_OWNER_URL (owner role on the test cluster)" >&2; exit 2; }

with_db() { local base="${1%%\?*}"; local q="${1#"$base"}"; echo "${base%/*}/$2$q"; }
q() { psql "$1" -XAtq -v ON_ERROR_STOP=1 -c "$2"; }
COUNT_SQL="SELECT coalesce(string_agg(format('SELECT %L || ''|'' || count(*) FROM public.%I', c.relname, c.relname), ' UNION ALL ' ORDER BY c.relname), 'SELECT NULL WHERE false') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') \\gexec"

START=$(date +%s)
DB="seereonos_restore_test_$(date -u +%Y%m%d%H%M%S)_$$"
TARGET=$(with_db "$OWNER" "$DB")
WORK=$(mktemp -d "${TMPDIR:-/tmp}/seereon-restore-test.XXXXXX")
FAILS=() FILE="" SNAPSHOT=""
cleanup() {
  [ -n "${SNAP_PID:-}" ] && kill "$SNAP_PID" 2>/dev/null || true
  if [ "$KEEP" = 0 ]; then q "$ADMIN" "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() { FAILS+=("$1"); echo "restore_test: FAIL $1" >&2; }
ok() { echo "restore_test: ok   $1"; }

# ---- 1. the backup to test -------------------------------------------------------------------------------------------
case "$MODE" in
  url)
    command -v age-keygen >/dev/null || { echo "restore_test: --from-url needs age (encrypts with a throwaway key)" >&2; exit 2; }
    age-keygen -o "$WORK/key.txt" 2>/dev/null
    export BACKUP_AGE_IDENTITY="$WORK/key.txt"
    # hold a repeatable-read transaction open: pg_dump dumps its exported snapshot, the counts are taken in it too
    coproc SNAP { psql "$ARG" -XAtq -v ON_ERROR_STOP=1 2>&1; }
    SNAP_PID=$SNAP_PID
    printf '%s\n' 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;' 'SELECT pg_export_snapshot();' >&"${SNAP[1]}"
    read -r -t 30 SNAPSHOT <&"${SNAP[0]}" || { echo "restore_test: could not export a snapshot from the source" >&2; exit 1; }
    BACKUP_DATABASE_URL="$ARG" BACKUP_AGE_RECIPIENT="$(age-keygen -y "$WORK/key.txt")" BACKUP_SNAPSHOT="$SNAPSHOT" \
      BACKUP_GPG_RECIPIENT="" BACKUP_ENCRYPTION="" "$HERE/backup.sh" --out "$WORK/backups" --prefix restoretest >/dev/null
    printf '%s\n' "$COUNT_SQL" "SELECT '__END__';" >&"${SNAP[1]}"
    : >"$WORK/source.counts"
    while read -r -t 600 line <&"${SNAP[0]}"; do [ "$line" = __END__ ] && break; echo "$line" >>"$WORK/source.counts"; done
    printf '%s\n' 'COMMIT;' '\q' >&"${SNAP[1]}"
    wait "$SNAP_PID" 2>/dev/null || true; SNAP_PID=""
    FILE=$(ls "$WORK"/backups/restoretest-*.dump.age)
    ok "fresh encrypted backup of the source (snapshot $SNAPSHOT, $(wc -l <"$WORK/source.counts") tables counted in it)" ;;
  file) FILE="$ARG" ;;
  latest)
    FILE=$(ls -1t "$ARG"/*.dump "$ARG"/*.dump.age "$ARG"/*.dump.gpg 2>/dev/null | head -1 || true)
    [ -n "$FILE" ] || { echo "restore_test: no backup in $ARG" >&2; exit 1; }
    AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$FILE") ) / 3600 ))
    [ "$AGE_H" -le 36 ] && ok "newest backup is ${AGE_H} h old: $FILE" || fail "newest backup is ${AGE_H} h old (> 36 h): $FILE" ;;
esac
[ -f "$FILE" ] || { echo "restore_test: backup file not found: $FILE" >&2; exit 1; }

# ---- 2. throwaway database, prepared like database/bootstrap.sql (roles must exist; their passwords are untouched) ----
[ "$(q "$ADMIN" "SELECT count(*) FROM pg_roles WHERE rolname IN ('seereon_owner', 'seereon_app')")" = 2 ] \
  || { echo "restore_test: roles seereon_owner / seereon_app missing on this cluster (run database/bootstrap.sql)" >&2; exit 1; }
q "$ADMIN" "CREATE DATABASE \"$DB\" OWNER seereon_owner ENCODING 'UTF8' TEMPLATE template0" >/dev/null
q "$ADMIN" "REVOKE ALL ON DATABASE \"$DB\" FROM PUBLIC; GRANT CONNECT, TEMPORARY ON DATABASE \"$DB\" TO seereon_owner; GRANT CONNECT ON DATABASE \"$DB\" TO seereon_app; ALTER DATABASE \"$DB\" SET timezone = 'UTC'" >/dev/null
psql "$(with_db "$ADMIN" "$DB")" -XAtq -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
ALTER SCHEMA public OWNER TO seereon_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO seereon_owner;
GRANT USAGE ON SCHEMA public TO seereon_app;
SQL
ok "throwaway database $DB"

# ---- 3. restore (one transaction) --------------------------------------------------------------------------------------
RSTART=$(date +%s)
if "$HERE/restore.sh" "$FILE" --target "$TARGET" >"$WORK/restore.log" 2>&1; then
  ok "restore.sh in $(( $(date +%s) - RSTART )) s ($(stat -c %s "$FILE") bytes)"
else
  cat "$WORK/restore.log" >&2; fail "restore.sh failed"
fi

if [ ${#FAILS[@]} = 0 ]; then
  # ---- 4. migrations: identical checksums; pending files only for an older backup ------------------------------------
  q "$TARGET" "SELECT version || ' ' || checksum FROM schema_migrations ORDER BY version" >"$WORK/applied"
  bad=0 pending=()
  while read -r v sum; do
    if [ -f "$MIGRATIONS/$v" ]; then
      [ "$(sha256sum "$MIGRATIONS/$v" | cut -d' ' -f1)" = "$sum" ] || { fail "migration $v: checksum differs from database/migrations"; bad=1; }
    fi
  done <"$WORK/applied"
  for f in "$MIGRATIONS"/V*.sql; do grep -q "^$(basename "$f") " "$WORK/applied" || pending+=("$(basename "$f")"); done
  [ $bad = 0 ] && ok "schema_migrations: $(wc -l <"$WORK/applied") applied, checksums match the repository"
  [ ${#pending[@]} = 0 ] || echo "restore_test: note  backup predates ${pending[*]}: run python -m app.db.migrate after a real restore"

  # ---- 5. structure and security -----------------------------------------------------------------------------------------
  missing=$(q "$TARGET" "SELECT string_agg(m.table_name, ', ') FROM app_table_meta m WHERE to_regclass('public.' || quote_ident(m.table_name)) IS NULL")
  [ -z "$missing" ] && ok "every table of app_table_meta exists" || fail "tables missing: $missing"
  norls=$(q "$TARGET" "SELECT string_agg(c.relname, ', ') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity AND c.relname IN (SELECT table_name FROM app_table_meta)")
  [ -z "$norls" ] && ok "row-level security enabled on every tenant table" || fail "RLS disabled on: $norls"
  ndef=$(q "$TARGET" "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef AND p.proname LIKE 'seereon\_%'")
  pub=$(q "$TARGET" "SELECT string_agg(p.proname, ', ') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef AND p.proname LIKE 'seereon\_%' AND has_function_privilege('public', p.oid, 'EXECUTE')")
  [ "$ndef" -gt 0 ] && [ -z "$pub" ] && ok "$ndef SECURITY DEFINER functions, none executable by PUBLIC" || fail "SECURITY DEFINER functions: $ndef, executable by PUBLIC: ${pub:-none}"
  priv=$(q "$TARGET" "SELECT has_table_privilege('seereon_app', 'user_credentials', 'SELECT')::text || has_table_privilege('seereon_app', 'sessions', 'SELECT')::text || has_table_privilege('seereon_app', 'tasks', 'SELECT')::text || has_function_privilege('seereon_app', 'seereon_resolve_session(text, integer)', 'EXECUTE')::text" 2>/dev/null || echo error)
  [ "$priv" = falsefalsetruetrue ] && ok "seereon_app: tenant tables yes, credentials/sessions only through functions" || fail "privileges of seereon_app unexpected ($priv)"

  # ---- 6. data -------------------------------------------------------------------------------------------------------------
  psql "$TARGET" -XAtq -v ON_ERROR_STOP=1 <<<"$COUNT_SQL" >"$WORK/restored.counts"
  total=$(awk -F'|' '{s+=$2} END {print s+0}' "$WORK/restored.counts")
  if [ "$MODE" = url ]; then
    if diff <(sort "$WORK/source.counts") <(sort "$WORK/restored.counts") >"$WORK/counts.diff"; then
      ok "row counts identical to the source snapshot: $(wc -l <"$WORK/restored.counts") tables, $total rows"
    else
      fail "row counts differ from the source snapshot:"; sed 's/^/    /' "$WORK/counts.diff" | head -20 >&2
    fi
  else
    ok "restored $(wc -l <"$WORK/restored.counts") tables, $total rows (tenants: $(q "$TARGET" 'SELECT count(*) FROM tenants'), users: $(q "$TARGET" 'SELECT count(*) FROM users'))"
  fi

  # ---- 7. through the runtime role ---------------------------------------------------------------------------------------
  if [ -n "${RESTORE_APP_URL:-}" ]; then
    APPT=$(with_db "$RESTORE_APP_URL" "$DB")
    tn=$(q "$TARGET" "SELECT tenant_id FROM users GROUP BY tenant_id ORDER BY count(*) DESC, tenant_id LIMIT 1")
    if [ -n "$tn" ]; then
      want=$(q "$TARGET" "SELECT count(*) FROM users WHERE tenant_id = '$tn'")
      none=$(q "$APPT" "SELECT count(*) FROM users")
      got=$(psql "$APPT" -XAtq -v ON_ERROR_STOP=1 -v tn="$tn" <<<"SELECT set_config('app.tenant_id', :'tn', false) \\g /dev/null
SELECT count(*) FROM users;")
      [ "$none" = 0 ] && [ "$got" = "$want" ] && ok "runtime role: 0 rows without a tenant, $got/$want users of $tn with it" \
        || fail "runtime role RLS check: without tenant $none (want 0), with tenant $got (want $want)"
    else
      ok "runtime role: connected (no users in the backup to check RLS with)"
    fi
  fi
fi

DUR=$(( $(date +%s) - START ))
RESULT=passed; [ ${#FAILS[@]} = 0 ] || RESULT=failed
if [ -n "$REPORT" ]; then
  printf '{"result":"%s","at":"%s","file":"%s","database":"%s","seconds":%s,"failures":%s}\n' "$RESULT" "$(date -u +%FT%TZ)" "$FILE" "$DB" "$DUR" "${#FAILS[@]}" >"$REPORT"
fi
if [ "$RESULT" = passed ]; then
  echo "RESTORE TEST PASSED in ${DUR} s$([ "$KEEP" = 1 ] && echo " (kept $DB)")"
else
  echo "RESTORE TEST FAILED (${#FAILS[@]}): ${FAILS[*]}" >&2; exit 1
fi
