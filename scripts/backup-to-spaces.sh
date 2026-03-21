#!/usr/bin/env bash
# Off-site backup upload to DigitalOcean Spaces (S3-compatible)
# What: Uploads the latest backup to DO Spaces for disaster recovery
# Called by: db-backup container cron (after backup.sh), or manually
# Depends on: aws CLI (s3 compatible), backup.sh must have run first
#
# Required env vars:
#   DO_SPACES_KEY       - DigitalOcean Spaces access key
#   DO_SPACES_SECRET    - DigitalOcean Spaces secret key
#   DO_SPACES_BUCKET    - Bucket name (e.g., availai-backups)
#   DO_SPACES_REGION    - Region (e.g., nyc3)
#
# Optional:
#   SPACES_RETENTION_DAYS - How long to keep remote backups (default: 90)

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/backups}"
SPACES_RETENTION_DAYS="${SPACES_RETENTION_DAYS:-90}"
ENDPOINT="https://${DO_SPACES_REGION:-nyc3}.digitaloceanspaces.com"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [spaces] $1"; }
die() { log "FATAL: $1"; exit 1; }

# ─── Pre-flight ──────────────────────────────────────────────────────────────
if [ -z "${DO_SPACES_KEY:-}" ] || [ -z "${DO_SPACES_SECRET:-}" ] || [ -z "${DO_SPACES_BUCKET:-}" ]; then
    log "SKIP: DO_SPACES_KEY, DO_SPACES_SECRET, or DO_SPACES_BUCKET not set. Off-site backup disabled."
    exit 0
fi

# Find latest backup
if [ -f "${BACKUP_DIR}/LATEST" ]; then
    BACKUP_FILE=$(cat "${BACKUP_DIR}/LATEST")
else
    die "No LATEST file found. Run backup.sh first."
fi

if [ ! -f "$BACKUP_FILE" ]; then
    die "Backup file not found: ${BACKUP_FILE}"
fi

FILENAME=$(basename "$BACKUP_FILE")

# Configure AWS CLI for DO Spaces
export AWS_ACCESS_KEY_ID="$DO_SPACES_KEY"
export AWS_SECRET_ACCESS_KEY="$DO_SPACES_SECRET"
export AWS_DEFAULT_REGION="${DO_SPACES_REGION:-nyc3}"

# ─── Upload ──────────────────────────────────────────────────────────────────
log "Uploading ${FILENAME} to s3://${DO_SPACES_BUCKET}/db-backups/"

aws s3 cp \
    "$BACKUP_FILE" \
    "s3://${DO_SPACES_BUCKET}/db-backups/${FILENAME}" \
    --endpoint-url "$ENDPOINT" \
    --storage-class STANDARD

# Also upload checksum if it exists
if [ -f "${BACKUP_FILE}.sha256" ]; then
    aws s3 cp \
        "${BACKUP_FILE}.sha256" \
        "s3://${DO_SPACES_BUCKET}/db-backups/${FILENAME}.sha256" \
        --endpoint-url "$ENDPOINT"
fi

log "Upload complete"

# ─── Verify upload ───────────────────────────────────────────────────────────
REMOTE_SIZE=$(aws s3 ls "s3://${DO_SPACES_BUCKET}/db-backups/${FILENAME}" \
    --endpoint-url "$ENDPOINT" 2>/dev/null | awk '{print $3}')
LOCAL_SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE")

if [ "$REMOTE_SIZE" != "$LOCAL_SIZE" ]; then
    die "Size mismatch! Local: ${LOCAL_SIZE}, Remote: ${REMOTE_SIZE}"
fi
log "Verified: remote size matches local (${LOCAL_SIZE} bytes)"

# ─── Remote rotation ────────────────────────────────────────────────────────
CUTOFF_DATE=$(date -d "-${SPACES_RETENTION_DAYS} days" +%Y-%m-%d 2>/dev/null || \
              date -v-${SPACES_RETENTION_DAYS}d +%Y-%m-%d 2>/dev/null || echo "")

if [ -n "$CUTOFF_DATE" ]; then
    log "Cleaning remote backups older than ${SPACES_RETENTION_DAYS} days (before ${CUTOFF_DATE})..."
    DELETED=0
    while IFS= read -r line; do
        file_date=$(echo "$line" | awk '{print $1}')
        file_name=$(echo "$line" | awk '{print $4}')
        if [ -n "$file_date" ] && [ "$file_date" \< "$CUTOFF_DATE" ]; then
            aws s3 rm "s3://${DO_SPACES_BUCKET}/${file_name}" --endpoint-url "$ENDPOINT" 2>/dev/null || true
            DELETED=$((DELETED + 1))
        fi
    done < <(aws s3 ls "s3://${DO_SPACES_BUCKET}/db-backups/" --endpoint-url "$ENDPOINT" 2>/dev/null || true)
    log "Deleted ${DELETED} old remote backups"
fi

log "Off-site backup complete: s3://${DO_SPACES_BUCKET}/db-backups/${FILENAME}"
