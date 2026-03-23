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

# Step 2: Rebuild app with build commit arg (Docker caching enabled)
BUILD_COMMIT=$(git rev-parse --short HEAD)
echo "==> Rebuilding app container (commit: $BUILD_COMMIT)..."
docker compose build --build-arg BUILD_COMMIT="$BUILD_COMMIT" app

# Step 3: Recreate only the app container with the new image
echo "==> Restarting app..."
docker compose up -d --force-recreate app

# Step 4: Wait for health check to pass
echo "==> Waiting for app to become healthy..."
TRIES=0
MAX_TRIES=30
while [ $TRIES -lt $MAX_TRIES ]; do
    CONTAINER=$(docker compose ps -q app)
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "unknown")
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

# Step 5: Verify deployed commit matches local HEAD
echo ""
echo "==> Verifying deployed build commit..."
DEPLOYED_COMMIT=$(docker compose exec app printenv BUILD_COMMIT 2>/dev/null | tr -d '[:space:]' || echo "UNKNOWN")

if [ "$DEPLOYED_COMMIT" != "$BUILD_COMMIT" ]; then
    echo "==> MISMATCH: deployed commit ($DEPLOYED_COMMIT) does NOT match local HEAD ($BUILD_COMMIT)"
    exit 1
fi
echo "==> MATCH: deployed commit ($DEPLOYED_COMMIT) matches local HEAD ($BUILD_COMMIT)"

# Step 6: Show recent logs to confirm the right code is running
echo ""
echo "==> Recent app logs:"
docker compose logs --tail=20 app
echo ""
echo "==> Deploy complete."
