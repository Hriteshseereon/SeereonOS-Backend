#!/usr/bin/env bash
# SeereonOS — restore a backup made by backup.sh into an EMPTY database prepared by database/bootstrap.sql.
#
#   database/scripts/restore.sh FILE --target URL [--jobs N]
#
#   FILE      <name>.dump.age | <name>.dump.gpg | <name>.dump   (a <FILE>.sha256 next to it is verified when present)
#   --target  the OWNER-role URL of the target database, e.g. postgres://seereon_owner:…@db-new:5432/seereonos.
#             The database must exist (bootstrap.sql creates it, the roles and their grants) and be EMPTY: the script
#             refuses to restore over existing tables. Never point it at the live database.
#   --jobs N  parallel restore (faster for big databases; not one transaction). Default: ONE transaction — all or
#             nothing.
# Decryption keys:
#   BACKUP_AGE_IDENTITY   path of the age identity (private key) file for .age backups
#   gpg                   the private key must be in the keyring (gpg-agent / --pinentry-mode as you run it)
# Decrypted data is written only to a private temp directory (RESTORE_TMPDIR, default $TMPDIR) and deleted on exit.
#
# After a restore: run `python -m app.db.migrate --status` (MIGRATION_DATABASE_URL = the target) — a backup from an
# older release is migrated forward with `python -m app.db.migrate` — then point DATABASE_URL of the API and the worker
# at it. Full runbook: database/docs/README.md "Restore".
set -euo pipefail
umask 077

FILE="" TARGET="" JOBS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="$2"; shift ;;
    --jobs) JOBS="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "restore: unknown option $1" >&2; exit 2 ;;
    *) [ -z "$FILE" ] || { echo "restore: one FILE only" >&2; exit 2; }; FILE="$1" ;;
  esac
  shift
done
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "restore: FILE missing or not found: '$FILE'" >&2; exit 2; }
[ -n "$TARGET" ] || { echo "restore: --target URL (owner role of the EMPTY target database) is required" >&2; exit 2; }
[ -z "$JOBS" ] || [[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || { echo "restore: --jobs must be 1-99" >&2; exit 2; }

if [ -f "$FILE.sha256" ]; then
  (cd "$(dirname "$FILE")" && sha256sum --quiet -c "$(basename "$FILE").sha256") || { echo "restore: CHECKSUM MISMATCH for $FILE" >&2; exit 1; }
  echo "restore: checksum ok"
fi

n=$(psql "$TARGET" -XAtqc "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S')")
[ "$n" = 0 ] || { echo "restore: the target database is not empty ($n relations in schema public) — refusing" >&2; exit 1; }

WORK=$(mktemp -d "${RESTORE_TMPDIR:-${TMPDIR:-/tmp}}/seereon-restore.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
DUMP="$WORK/db.dump"
case "$FILE" in
  *.age)
    [ -n "${BACKUP_AGE_IDENTITY:-}" ] && [ -r "$BACKUP_AGE_IDENTITY" ] || { echo "restore: set BACKUP_AGE_IDENTITY to the age identity file" >&2; exit 2; }
    age -d -i "$BACKUP_AGE_IDENTITY" -o "$DUMP" "$FILE" ;;
  *.gpg) gpg --batch --quiet --decrypt --output "$DUMP" "$FILE" ;;
  *.dump) cp "$FILE" "$DUMP" ;;
  *) echo "restore: unknown file type (expected .dump, .dump.age or .dump.gpg)" >&2; exit 2 ;;
esac
pg_restore --list "$DUMP" >/dev/null || { echo "restore: not a valid pg_dump custom-format archive" >&2; exit 1; }

START=$(date +%s)
# --no-owner: objects are owned by the connecting owner role (bootstrap.sql made it own the database and schema);
# grants to seereon_app, RLS policies, SECURITY DEFINER functions and default privileges come from the archive.
if [ -n "$JOBS" ]; then
  pg_restore --exit-on-error --no-owner --no-password --jobs="$JOBS" --dbname="$TARGET" "$DUMP"
else
  pg_restore --exit-on-error --no-owner --no-password --single-transaction --dbname="$TARGET" "$DUMP"
fi
psql "$TARGET" -XAtq -c 'SET client_min_messages = error' -c 'ANALYZE' >/dev/null  # catalog tables it may not analyze are skipped quietly
echo "restore: done in $(( $(date +%s) - START )) s; latest migration: $(psql "$TARGET" -XAtqc 'SELECT max(version) FROM schema_migrations')"
