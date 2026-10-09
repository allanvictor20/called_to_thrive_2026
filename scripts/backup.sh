#!/usr/bin/env bash
# Logical backup of the Called to Thrive database (NFR05).
# Usage: DATABASE_URL=postgres://... scripts/backup.sh [output-dir]
# Store the output in an encrypted, access-controlled location with a named retention owner.
set -euo pipefail
OUT_DIR="${1:-backups}"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT_DIR/called_to_thrive-$STAMP.dump"
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" --file "$FILE"
sha256sum "$FILE" > "$FILE.sha256"
echo "Backup written: $FILE"
