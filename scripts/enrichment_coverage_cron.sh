#!/bin/bash
# Daily enrichment-coverage telemetry for AvailAI
# Runs daily at 06:10 UTC via cron
# Called by: crontab (10 6 * * *) — install once with:
#   (crontab -l 2>/dev/null; echo "10 6 * * * /root/availai/scripts/enrichment_coverage_cron.sh") | crontab -
# Depends on: docker compose (app container), app/management/enrichment_coverage_report.py
#
# The JSONL history lives at /var/log/avail/enrichment_coverage_history.jsonl INSIDE
# the app container — that path is the `applogs` named volume (docker-compose.yml),
# so run-over-run deltas survive container recreation. The human-readable block is
# appended host-side to /var/log/avail/enrichment_coverage.log.
set -euo pipefail

cd /root/availai
LOG="/var/log/avail/enrichment_coverage.log"
mkdir -p /var/log/avail
{
    echo "=== Enrichment coverage $(date -u +%FT%TZ) ==="
    docker compose exec -T app python -m app.management.enrichment_coverage_report \
        --log-file /var/log/avail/enrichment_coverage_history.jsonl
} >> "$LOG" 2>&1
