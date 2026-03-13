#!/bin/bash
# AvailAI — Pull latest code, migrate DB, and rebuild
# Run from anywhere: bash /root/availai/scripts/update.sh
set -e

cd /root/availai
echo "Pulling latest code..."
git pull

echo "Rebuilding..."
docker compose up -d --build

echo "Waiting for app to be healthy..."
MAX_CHECKS=150  # 5 minutes at 2s interval
for i in $(seq 1 "$MAX_CHECKS"); do
    if docker compose ps app --format '{{.Status}}' | grep -q healthy; then
        break
    fi
    sleep 2
    if [ "$i" -eq "$MAX_CHECKS" ]; then
        echo "✗ App did not become healthy within 5 minutes"
        exit 1
    fi
done

# Caddy auto-recovers via health checks — no restart needed.
# Only reload config if the Caddyfile changed.
echo "Reloading Caddy config..."
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null || true

echo ""
echo "✓ Updated and running"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
