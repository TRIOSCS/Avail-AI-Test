#!/bin/bash
# deploy.sh — Reliable deploy for AvailAI
# Usage: ./deploy.sh [--no-commit] [message]
set -euo pipefail
cd /root/availai

NO_COMMIT=false
if [ "${1:-}" = "--no-commit" ]; then
    NO_COMMIT=true
    shift
fi

# Step 1: Commit & push (unless --no-commit)
if [ "$NO_COMMIT" = false ]; then
    if git diff --quiet && git diff --cached --quiet; then
        echo "No changes to commit — skipping git steps"
    else
        git add -A
        git commit -m "${1:-deploy}"
        git push origin main
    fi
fi

# Step 2: Force rebuild — no cache for the app service
# This prevents stale Python/JS code from surviving in cached layers
echo "==> Rebuilding app container (no cache)..."
docker compose build --no-cache app

# Step 3: Recreate only the app container with the new image
echo "==> Restarting app..."
docker compose up -d --force-recreate app

# Step 4: Wait for health check to pass
echo "==> Waiting for app to become healthy..."
TRIES=0
MAX_TRIES=30
while [ $TRIES -lt $MAX_TRIES ]; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' availai-app-1 2>/dev/null || echo "unknown")
    if [ "$STATUS" = "healthy" ]; then
        echo "==> App is healthy!"
        break
    fi
    TRIES=$((TRIES + 1))
    sleep 2
done

if [ "$STATUS" != "healthy" ]; then
    echo "==> ERROR: App did not become healthy after ${MAX_TRIES} attempts"
    echo "==> Last 50 log lines:"
    docker compose logs --tail=50 app
    exit 1
fi

# Step 5: Show recent logs to confirm the right code is running
echo ""
echo "==> Recent app logs:"
docker compose logs --tail=20 app
echo ""
echo "==> Deploy complete. Verify the version at /health"
