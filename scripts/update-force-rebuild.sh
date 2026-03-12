#!/bin/bash
# AvailAI — Force full rebuild (no cache) and restart app. Use when new code isn't showing.
# Run from server: bash /root/availai/scripts/update-force-rebuild.sh
set -e

cd /root/availai
echo "Pulling latest code..."
git pull

echo "Force-rebuilding app image (no cache)..."
docker compose build --no-cache app

echo "Recreating app container..."
docker compose up -d app

echo "Waiting for app to be healthy..."
until docker compose ps app --format '{{.Status}}' | grep -q healthy; do
    sleep 2
done

echo ""
echo "✓ Rebuilt and running. Do a hard refresh in the browser (Ctrl+Shift+R or Cmd+Shift+R)."
docker compose ps --format "table {{.Name}}\t{{.Status}}"
