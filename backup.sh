#!/bin/bash
# AVAIL — Daily database backup
# Add to crontab: 0 2 * * * /root/availai/backup.sh
#
# Keeps last 30 days of backups in /root/backups/

BACKUP_DIR="/root/backups"
COMPOSE_DIR="/root/availai"
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M)
BACKUP_FILE="$BACKUP_DIR/availai-$TIMESTAMP.sql.gz"

# Dump and compress
docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T db \
    pg_dump -U availai availai | gzip > "$BACKUP_FILE"

if [ $? -eq 0 ] && [ -s "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "$(date -Iseconds) Backup OK: $BACKUP_FILE ($SIZE)"
else
    echo "$(date -Iseconds) BACKUP FAILED" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Prune old backups
find "$BACKUP_DIR" -name "availai-*.sql.gz" -mtime +$KEEP_DAYS -delete
