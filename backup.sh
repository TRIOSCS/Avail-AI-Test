#!/bin/bash
# AVAIL — Daily database backup with integrity verification
# Add to crontab: 0 2 * * * /root/availai/backup.sh >> /var/log/availai-backup.log 2>&1
#
# Creates a pg_dump custom-format backup (-Fc), verifies integrity with
# pg_restore --list, rotates old backups, and writes a timestamp file
# into the app container so the /health endpoint can report freshness.

set -euo pipefail

BACKUP_DIR="/root/backups"
COMPOSE_DIR="/root/availai"
KEEP_DAYS=30
TIMESTAMP_FILE="/app/uploads/.last_backup"

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M)
BACKUP_FILE="$BACKUP_DIR/availai-$TIMESTAMP.dump"

log() { echo "$(date -Iseconds) $*"; }
log_err() { echo "$(date -Iseconds) ERROR: $*" >&2; }

# ── 1. Dump (custom format — compressed, supports pg_restore --list) ──
log "Starting backup..."
docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T db \
    pg_dump -U availai -Fc availai > "$BACKUP_FILE"

if [ ! -s "$BACKUP_FILE" ]; then
    log_err "pg_dump produced an empty file"
    rm -f "$BACKUP_FILE"
    exit 1
fi

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
log "Dump complete: $BACKUP_FILE ($SIZE)"

# ── 2. Verify integrity ──
# pg_restore --list reads the TOC without restoring — catches truncated/corrupt dumps
if pg_restore --list "$BACKUP_FILE" > /dev/null 2>&1; then
    log "Integrity check PASSED (pg_restore --list)"
else
    log_err "Integrity check FAILED — backup may be corrupt: $BACKUP_FILE"
    rm -f "$BACKUP_FILE"
    exit 1
fi

# ── 3. Write timestamp into app container for /health freshness check ──
# The uploads volume is shared between host and container at /app/uploads
docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T app \
    sh -c "date -Iseconds > $TIMESTAMP_FILE" 2>/dev/null \
    && log "Wrote backup timestamp to $TIMESTAMP_FILE" \
    || log "Warning: could not write timestamp (app container may be down)"

# ── 4. Rotate old backups ──
PRUNED=$(find "$BACKUP_DIR" -name "availai-*.dump" -mtime +$KEEP_DAYS -print -delete | wc -l)
# Also clean up any legacy .sql.gz backups from old format
PRUNED_LEGACY=$(find "$BACKUP_DIR" -name "availai-*.sql.gz" -mtime +$KEEP_DAYS -print -delete | wc -l)
if [ "$PRUNED" -gt 0 ] || [ "$PRUNED_LEGACY" -gt 0 ]; then
    log "Pruned $((PRUNED + PRUNED_LEGACY)) old backup(s) (older than $KEEP_DAYS days)"
fi

log "Backup complete: $BACKUP_FILE ($SIZE, verified)"
