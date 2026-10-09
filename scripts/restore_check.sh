#!/usr/bin/env bash
# TC10 restore exercise: restore a backup into an ISOLATED database and verify it.
# Usage: RESTORE_DATABASE_URL=postgres://.../ctt_restore_check scripts/restore_check.sh backups/called_to_thrive-....dump
# Never point RESTORE_DATABASE_URL at production.
set -euo pipefail
DUMP="$1"
: "${RESTORE_DATABASE_URL:?set RESTORE_DATABASE_URL to an empty, isolated database}"
START=$(date +%s)
sha256sum --check "$DUMP.sha256"
pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$RESTORE_DATABASE_URL" "$DUMP"
export DATABASE_URL="$RESTORE_DATABASE_URL"
python manage.py migrate --check            # schema matches this release's migrations
python manage.py verify_restore             # counts, relationships, content version, triggers
END=$(date +%s)
BACKUP_AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$DUMP") ) / 3600 ))
echo "Restore verified in $((END-START))s. Backup age: ${BACKUP_AGE_H}h. Record both in the recovery log."
