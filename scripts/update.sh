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

echo "Restarting Caddy to clear stale upstream connections..."
docker compose restart caddy
sleep 3

echo ""
echo "✓ Updated and running"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
