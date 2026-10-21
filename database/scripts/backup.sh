#!/usr/bin/env bash
# SeereonOS — encrypted logical backup of the ONE database (pg_dump custom format), with retention.
#
#   database/scripts/backup.sh [--out DIR] [--keep-days N] [--prefix NAME]
#
# Connection (a role that can read every row: the OWNER role — RLS is not forced on the owner — or a superuser):
#   BACKUP_DATABASE_URL   default: MIGRATION_DATABASE_URL       e.g. postgres://seereon_owner:…@db.internal:5432/seereonos
# Encryption (REQUIRED — a dump contains password hashes, sealed 2FA secrets and every tenant's data):
#   BACKUP_AGE_RECIPIENT  age public key(s), comma-separated (age1…)      → <name>.dump.age    (preferred)
#   BACKUP_GPG_RECIPIENT  gpg key id / e-mail (public key in the keyring)  → <name>.dump.gpg
#   BACKUP_ENCRYPTION=none   explicit opt-out (an already encrypted volume you fully control); a loud warning
# Output: DIR/<prefix>-<UTC timestamp>.dump.{age,gpg} + .sha256 (default DIR=/var/backups/seereonos, prefix seereonos).
# Retention: files of this prefix older than --keep-days (default 35, env BACKUP_KEEP_DAYS) are deleted after a
# SUCCESSFUL backup only. Exit code ≠ 0 on any failure (cron mails it / your scheduler alerts on it).
#
# Typical cron (as a user that can read the secrets), daily 02:15 UTC + the weekly scripted restore test:
#   15 2 * * *  BACKUP_DATABASE_URL=… BACKUP_AGE_RECIPIENT=age1… /opt/seereonos/database/scripts/backup.sh --out /var/backups/seereonos
#   45 3 * * 0  … /opt/seereonos/database/scripts/restore_test.sh --latest /var/backups/seereonos
# Copy the backup directory off the host (object storage with versioning / object lock) — see database/docs/README.md.
set -euo pipefail
umask 077

OUT=${BACKUP_DIR:-/var/backups/seereonos}
KEEP=${BACKUP_KEEP_DAYS:-35}
PREFIX=seereonos
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift ;;
    --keep-days) KEEP="$2"; shift ;;
    --prefix) PREFIX="$2"; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "backup: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
URL=${BACKUP_DATABASE_URL:-${MIGRATION_DATABASE_URL:-}}
[ -n "$URL" ] || { echo "backup: set BACKUP_DATABASE_URL (or MIGRATION_DATABASE_URL): the owner role" >&2; exit 2; }
[[ "$KEEP" =~ ^[0-9]+$ ]] && [ "$KEEP" -ge 1 ] || { echo "backup: --keep-days must be a positive integer" >&2; exit 2; }
[[ "$PREFIX" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "backup: --prefix may only contain letters, digits, . _ -" >&2; exit 2; }

if [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
  command -v age >/dev/null || { echo "backup: age is not installed" >&2; exit 2; }
  EXT=age
  AGE_ARGS=()
  IFS=',' read -ra RCPTS <<<"$BACKUP_AGE_RECIPIENT"
  for r in "${RCPTS[@]}"; do AGE_ARGS+=(-r "$(echo "$r" | tr -d ' ')"); done
  encrypt() { age "${AGE_ARGS[@]}"; }
elif [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
  command -v gpg >/dev/null || { echo "backup: gpg is not installed" >&2; exit 2; }
  EXT=gpg
  encrypt() { gpg --batch --yes --trust-model always --encrypt --recipient "$BACKUP_GPG_RECIPIENT" --output -; }
elif [ "${BACKUP_ENCRYPTION:-}" = none ]; then
  EXT=plain
  echo "backup: WARNING: BACKUP_ENCRYPTION=none — the dump is NOT encrypted (password hashes, sealed secrets, all tenant data)" >&2
  encrypt() { cat; }
else
  echo "backup: set BACKUP_AGE_RECIPIENT (preferred) or BACKUP_GPG_RECIPIENT; BACKUP_ENCRYPTION=none to opt out explicitly" >&2
  exit 2
fi

mkdir -p "$OUT"
TS=$(date -u +%Y%m%dT%H%M%SZ)
NAME="$PREFIX-$TS.dump"; [ "$EXT" = plain ] || NAME="$NAME.$EXT"
TMP="$OUT/.$NAME.partial"
trap 'rm -f "$TMP"' EXIT

START=$(date +%s)
# custom format: compressed, restorable selectively and in parallel; one consistent snapshot (repeatable read)
# BACKUP_SNAPSHOT: dump an exported snapshot (pg_export_snapshot()) — restore_test.sh uses it to count rows in the
# very same snapshot it dumps
pg_dump --format=custom --compress=6 --no-password ${BACKUP_SNAPSHOT:+--snapshot="$BACKUP_SNAPSHOT"} --dbname="$URL" | encrypt >"$TMP"
SIZE=$(stat -c %s "$TMP")
[ "$SIZE" -gt 1024 ] || { echo "backup: output is suspiciously small ($SIZE bytes)" >&2; exit 1; }
mv "$TMP" "$OUT/$NAME"
trap - EXIT
(cd "$OUT" && sha256sum "$NAME" >"$NAME.sha256")
echo "backup: $OUT/$NAME ($SIZE bytes, $(( $(date +%s) - START )) s)"

# retention: only after a successful backup, only this prefix, only our own file names
find "$OUT" -maxdepth 1 -type f \( -name "$PREFIX-*.dump" -o -name "$PREFIX-*.dump.age" -o -name "$PREFIX-*.dump.gpg" -o -name "$PREFIX-*.dump*.sha256" \) \
  -mtime +"$KEEP" -print -delete | sed 's/^/backup: retention removed /'
