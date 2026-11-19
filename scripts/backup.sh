#!/usr/bin/env bash
# Back up everything that exists only on the production server: the MySQL database
# and the runtime files that are not in git. See docs/runbooks/BACKUP_RESTORE.md.
#
#   MYSQL_HOST=... MYSQL_USER=... MYSQL_PASSWORD=... MYSQL_DB=... scripts/backup.sh /path/to/backups
#
# Writes <target>/<UTC timestamp>/{db.sql.gz,files.tar.gz,MANIFEST} and prints the
# directory. Exits non-zero if the dump fails; a missing optional path is noted in
# the manifest, not an error. Copy the directory off the server afterwards: a backup
# on the same disk protects against mistakes, not against losing the disk.
set -euo pipefail

TARGET="${1:?usage: scripts/backup.sh <target directory>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${MYSQL_HOST:?set MYSQL_HOST}" "${MYSQL_USER:?set MYSQL_USER}" "${MYSQL_DB:?set MYSQL_DB}"
MYSQLDUMP="${MYSQLDUMP:-mysqldump}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$TARGET/$STAMP"
mkdir -p "$OUT"
umask 077

# One consistent snapshot without locking the tables the app is writing to.
MYSQL_PWD="${MYSQL_PASSWORD:-}" "$MYSQLDUMP" --no-defaults \
  -h "$MYSQL_HOST" -P "${MYSQL_PORT:-3306}" -u "$MYSQL_USER" \
  --single-transaction --quick --routines --triggers --no-tablespaces \
  --set-gtid-purged=OFF --default-character-set=utf8mb4 "$MYSQL_DB" | gzip > "$OUT/db.sql.gz"

# Runtime state that a fresh clone does not have (docs/runbooks/DEPLOY.md).
PATHS=(instance static/uploads static/brand_assets app1/shopify_products_all_pages.json app1/shopify_products_augmented.json)
if [ -n "${CATALOG_SOURCE_FILE:-}" ]; then PATHS+=("$CATALOG_SOURCE_FILE"); fi
PRESENT=(); MISSING=()
for p in "${PATHS[@]}"; do
  if [ -e "$ROOT/$p" ] || [ -e "$p" ]; then PRESENT+=("$p"); else MISSING+=("$p"); fi
done
if [ "${#PRESENT[@]}" -gt 0 ]; then
  tar -czf "$OUT/files.tar.gz" -C "$ROOT" --ignore-failed-read "${PRESENT[@]}" 2>/dev/null \
    || tar -czf "$OUT/files.tar.gz" -P --ignore-failed-read "${PRESENT[@]}"
fi

{
  echo "created_utc=$STAMP"
  echo "database=$MYSQL_DB"
  echo "commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "db_bytes=$(stat -c %s "$OUT/db.sql.gz")"
  echo "files=${PRESENT[*]:-none}"
  echo "missing=${MISSING[*]:-none}"
} > "$OUT/MANIFEST"

gzip -t "$OUT/db.sql.gz"
echo "$OUT"
