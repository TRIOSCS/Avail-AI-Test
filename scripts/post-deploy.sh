#!/usr/bin/env bash
# scripts/post-deploy.sh — Rebuild and wait for health
#
# Usage: ./scripts/post-deploy.sh
#
# Called by: operator (manual), cron, CI
# Depends on: docker compose

set -euo pipefail

cd /root/availai

echo "=== Rebuilding ==="
docker compose up -d --build

echo "=== Waiting for health (up to 60s) ==="
for i in $(seq 1 12); do
    sleep 5
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "Health check passed after $((i*5))s"
        exit 0
    fi
    echo "  Attempt $i/12..."
done

echo "ERROR: Health check failed after 60s"
exit 1
