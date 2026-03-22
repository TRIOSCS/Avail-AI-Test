#!/usr/bin/env bash
# Database backup script for AvailAI
# What: Creates compressed, verified PostgreSQL backups with automatic rotation
# Called by: db-backup container cron, or manually via: docker compose exec db-backup /scripts/backup.sh
# Depends on: PostgreSQL 16 (pg_dump, pg_restore), gzip, environment vars from .env

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-availai}"
DB_USER="${POSTGRES_USER:-availai}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_${TIMESTAMP}.dump"
BACKUP_FILE_GZ="${BACKUP_FILE}.gz"
CHECKSUM_FILE="${BACKUP_FILE_GZ}.sha256"
LOG_FILE="${BACKUP_DIR}/backup.log"

# ─── Functions ───────────────────────────────────────────────────────────────
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

die() {
    log "FATAL: $1"
    exit 1
}

# ─── Pre-flight checks ──────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

if [ -z "${POSTGRES_PASSWORD:-}" ] && [ -z "${PGPASSWORD:-}" ]; then
    die "POSTGRES_PASSWORD or PGPASSWORD must be set"
fi
export PGPASSWORD="${PGPASSWORD:-$POSTGRES_PASSWORD}"

# Wait for database to be ready (up to 60 seconds)
log "Waiting for database at ${DB_HOST}:${DB_PORT}..."
for i in $(seq 1 12); do
    if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
        break
    fi
    if [ "$i" -eq 12 ]; then
        die "Database not ready after 60 seconds"
    fi
    sleep 5
done

# ─── Backup ──────────────────────────────────────────────────────────────────
log "Starting backup of '${DB_NAME}' → ${BACKUP_FILE_GZ}"

# Get pre-backup table count for verification
TABLE_COUNT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';" \
    2>/dev/null | tr -d ' ')
log "Pre-backup: ${TABLE_COUNT} tables in public schema"

# Get pre-backup row counts for critical tables
ROW_SUMMARY=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c \
    "SELECT string_agg(t.name || '=' || t.cnt, ', ')
     FROM (
       SELECT 'users' AS name, count(*)::text AS cnt FROM users
       UNION ALL SELECT 'companies', count(*)::text FROM companies
       UNION ALL SELECT 'vendor_cards', count(*)::text FROM vendor_cards
       UNION ALL SELECT 'requisitions', count(*)::text FROM requisitions
       UNION ALL SELECT 'material_cards', count(*)::text FROM material_cards
     ) t;" 2>/dev/null | tr -d ' ')
log "Row counts: ${ROW_SUMMARY}"

# pg_dump in custom format (supports parallel restore, selective restore)
pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --format=custom \
    --verbose \
    --no-owner \
    --no-acl \
    --compress=0 \
    --file="$BACKUP_FILE" \
    2>>"$LOG_FILE"

if [ ! -f "$BACKUP_FILE" ]; then
    die "pg_dump produced no output file"
fi

DUMP_SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE")
if [ "$DUMP_SIZE" -lt 1024 ]; then
    die "Backup file suspiciously small (${DUMP_SIZE} bytes). Aborting."
fi

# Compress
gzip -9 "$BACKUP_FILE"

if [ ! -f "$BACKUP_FILE_GZ" ]; then
    die "Compression failed — gzip output missing"
fi

GZ_SIZE=$(stat -c%s "$BACKUP_FILE_GZ" 2>/dev/null || stat -f%z "$BACKUP_FILE_GZ")

# ─── Verification ────────────────────────────────────────────────────────────
log "Verifying backup integrity..."

# 1. SHA-256 checksum
sha256sum "$BACKUP_FILE_GZ" > "$CHECKSUM_FILE"
log "Checksum: $(cat "$CHECKSUM_FILE")"

# 2. Verify the dump can be listed (proves format is valid)
RESTORE_TABLE_COUNT=$(gunzip -c "$BACKUP_FILE_GZ" | pg_restore --list 2>/dev/null | grep "TABLE " | wc -l || true)
log "Verified: backup contains ${RESTORE_TABLE_COUNT} TABLE entries"

if [ "$RESTORE_TABLE_COUNT" -lt 5 ]; then
    die "Backup verification failed — only ${RESTORE_TABLE_COUNT} tables found (expected many more)"
fi

# ─── Rotation ────────────────────────────────────────────────────────────────
DELETED_COUNT=0
if [ "$RETENTION_DAYS" -gt 0 ]; then
    while IFS= read -r old_file; do
        rm -f "$old_file" "${old_file}.sha256"
        DELETED_COUNT=$((DELETED_COUNT + 1))
    done < <(find "$BACKUP_DIR" -name "${DB_NAME}_*.dump.gz" -mtime +"$RETENTION_DAYS" -type f 2>/dev/null)
fi

# Count remaining backups
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "${DB_NAME}_*.dump.gz" -type f 2>/dev/null | wc -l)

# ─── Summary ─────────────────────────────────────────────────────────────────
log "╔══════════════════════════════════════════════════════════════╗"
log "║  BACKUP COMPLETE                                            ║"
log "╠══════════════════════════════════════════════════════════════╣"
log "║  File: ${BACKUP_FILE_GZ}"
log "║  Size: $(numfmt --to=iec-i --suffix=B "$GZ_SIZE" 2>/dev/null || echo "${GZ_SIZE} bytes")"
log "║  Tables: ${TABLE_COUNT} (${RESTORE_TABLE_COUNT} verified in dump)"
log "║  Rows: ${ROW_SUMMARY}"
log "║  Checksum: $(cut -d' ' -f1 "$CHECKSUM_FILE")"
log "║  Deleted: ${DELETED_COUNT} old backups (>${RETENTION_DAYS} days)"
log "║  Remaining: ${BACKUP_COUNT} backups on disk"
log "╚══════════════════════════════════════════════════════════════╝"

# Write latest backup path for other scripts to reference
echo "$BACKUP_FILE_GZ" > "${BACKUP_DIR}/LATEST"
