#!/bin/bash
# AvailAI — Pull latest code, migrate DB, and rebuild
# Run from anywhere: bash /root/availai/scripts/update.sh
set -e

cd /root/availai
echo "Pulling latest code..."
git pull

echo "Rebuilding..."
docker compose up -d --build

# Run migration if migrate script exists
if [ -f "migrate_v105.py" ]; then
    echo "Running v1.0.5 migration..."
    docker compose exec -T web python migrate_v105.py || echo "Migration may have already been applied"
fi

echo ""
echo "✓ Updated and running"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
