#!/bin/bash
set -euo pipefail
STAMP=$(date +%Y%m%d_%H%M%S)
DEST=/opt/backups/alfacybercup
mkdir -p "$DEST"
sqlite3 /opt/alfacybercup/data/app.db ".backup '$DEST/app_$STAMP.db'"
find "$DEST" -name 'app_*.db' -mtime +7 -delete
