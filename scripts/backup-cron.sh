#!/usr/bin/env bash
# Cron-like backup scheduler for the db-backup container
# What: Runs backup.sh on a schedule, then optionally uploads to DO Spaces
# Called by: db-backup container entrypoint
# Depends on: backup.sh, backup-to-spaces.sh

set -euo pipefail

BACKUP_INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-6}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [scheduler] $1"; }

log "AvailAI Database Backup Scheduler"
log "Interval: every ${BACKUP_INTERVAL_HOURS} hours"
log "Retention: ${BACKUP_RETENTION_DAYS:-30} days local, ${SPACES_RETENTION_DAYS:-90} days remote"

# Run an immediate backup on container start
log "Running initial backup..."
/scripts/backup.sh

# Upload to Spaces if configured
if [ -n "${DO_SPACES_KEY:-}" ]; then
    log "Uploading to DigitalOcean Spaces..."
    /scripts/backup-to-spaces.sh
else
    log "DO_SPACES_KEY not set — skipping off-site upload"
fi

log "Initial backup complete. Sleeping ${BACKUP_INTERVAL_HOURS}h until next run."

# Loop forever
while true; do
    sleep "${BACKUP_INTERVAL_HOURS}h"

    log "Scheduled backup starting..."
    if /scripts/backup.sh; then
        log "Backup succeeded"

        if [ -n "${DO_SPACES_KEY:-}" ]; then
            if /scripts/backup-to-spaces.sh; then
                log "Off-site upload succeeded"
            else
                log "ERROR: Off-site upload failed (local backup is safe)"
            fi
        fi
    else
        log "ERROR: Backup failed!"
    fi

    log "Next backup in ${BACKUP_INTERVAL_HOURS} hours"
done
