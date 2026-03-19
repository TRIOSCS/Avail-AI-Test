#!/bin/bash
# Enrichment watchdog — checks if the enrichment-worker is running, restarts if not.
#
# Install as a cron job (runs twice a day at 6am and 6pm):
#   (crontab -l 2>/dev/null; echo "0 6,18 * * * /root/availai/scripts/enrichment_watchdog.sh >> /var/log/enrichment_watchdog.log 2>&1") | crontab -
#
# Called by: cron
# Depends on: docker compose, enrichment-worker service

set -e

COMPOSE_DIR="/root/availai"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')] WATCHDOG:"

cd "$COMPOSE_DIR"

# Check if the enrichment-worker container is running
WORKER_STATUS=$(docker compose ps enrichment-worker --format '{{.State}}' 2>/dev/null || echo "missing")

if [ "$WORKER_STATUS" = "running" ]; then
    echo "$LOG_PREFIX enrichment-worker is running — all good"

    # Also check if the process inside is alive (not zombie/stuck)
    LAST_LOG=$(docker compose exec -T enrichment-worker sh -c 'tail -1 /tmp/enrichment_pipeline.log 2>/dev/null' || echo "")
    if [ -n "$LAST_LOG" ]; then
        echo "$LOG_PREFIX Last log: $LAST_LOG"
    fi
else
    echo "$LOG_PREFIX enrichment-worker is $WORKER_STATUS — RESTARTING"

    # Pull latest code in case there was an update
    git pull origin main 2>/dev/null || true

    # Rebuild and restart just the enrichment worker
    docker compose up -d --build enrichment-worker

    echo "$LOG_PREFIX enrichment-worker restarted"

    # Verify it came back
    sleep 10
    NEW_STATUS=$(docker compose ps enrichment-worker --format '{{.State}}' 2>/dev/null || echo "unknown")
    echo "$LOG_PREFIX Post-restart status: $NEW_STATUS"
fi
