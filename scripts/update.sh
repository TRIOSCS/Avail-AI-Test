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
until docker compose ps app --format '{{.Status}}' | grep -q healthy; do
    sleep 2
done

# Caddy auto-recovers via health checks — no restart needed.
# Only reload config if the Caddyfile changed.
echo "Reloading Caddy config..."
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null || true

echo ""
echo "✓ Updated and running"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
