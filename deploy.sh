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

# Step 2: Rebuild frontend assets (Tailwind CSS + JS bundle)
echo "==> Building frontend assets..."
npm run build

# Step 3: Rebuild app with unique build arg to bust Docker cache
# Append timestamp so --no-commit deploys also invalidate COPY layers
BUILD_COMMIT="$(git rev-parse --short HEAD)-$(date +%s)"
echo "==> Rebuilding app container (build tag: $BUILD_COMMIT)..."
docker compose build --no-cache --build-arg BUILD_COMMIT="$BUILD_COMMIT" app

# Step 4: Recreate only the app container with the new image
echo "==> Restarting app..."
docker compose up -d --force-recreate app

# Step 5: Wait for health check to pass
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

# Step 6: Verify deployed build tag matches what we just built
echo ""
echo "==> Verifying deployed build tag..."
DEPLOYED_COMMIT=$(docker compose exec app printenv BUILD_COMMIT 2>/dev/null | tr -d '[:space:]' || echo "UNKNOWN")

if [ "$DEPLOYED_COMMIT" != "$BUILD_COMMIT" ]; then
    echo "==> MISMATCH: deployed ($DEPLOYED_COMMIT) does NOT match build ($BUILD_COMMIT)"
    exit 1
fi
echo "==> MATCH: deployed build tag ($DEPLOYED_COMMIT)"

# Step 7: Show recent logs to confirm the right code is running
echo ""
echo "==> Recent app logs:"
docker compose logs --tail=20 app
echo ""
echo "==> Deploy complete."
