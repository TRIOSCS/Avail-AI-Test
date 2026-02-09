#!/bin/bash
# AvailAI — Pull latest code and rebuild
# Run from anywhere: bash /root/availai/scripts/update.sh
set -e

cd /root/availai
echo "Pulling latest code..."
git pull

echo "Rebuilding..."
docker compose up -d --build

echo ""
echo "✓ Updated and running"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
