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

# Step 1: Commit & push (unless --no-commit). Hardened against the silent-
# failure mode where a stale local main (behind origin/main) caused every
# `git push origin main` to be rejected as non-fast-forward — the rebuild
# steps still ran from whatever branch was checked out, so deploys appeared
# to succeed while origin/main drifted days out of sync.
if [ "$NO_COMMIT" = false ]; then
    CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$CURRENT_BRANCH" != "main" ]; then
        echo "ERROR: ./deploy.sh (without --no-commit) must run from main." >&2
        echo "Current branch: $CURRENT_BRANCH" >&2
        echo "Either: git checkout main && git pull --ff-only origin main" >&2
        echo "Or deploy the current branch without pushing: ./deploy.sh --no-commit" >&2
        exit 1
    fi

    echo "==> Syncing local main with origin/main..."
    git fetch origin
    if ! git merge --ff-only origin/main; then
        echo "ERROR: local main diverged from origin/main — cannot fast-forward." >&2
        echo "Resolve with 'git pull --rebase origin main' or investigate before re-running." >&2
        exit 2
    fi

    if git diff --quiet && git diff --cached --quiet; then
        echo "No changes to commit — skipping commit step"
    else
        git add -A
        git commit -m "${1:-deploy}"
    fi

    AHEAD=$(git rev-list --count origin/main..HEAD)
    if [ "$AHEAD" -gt 0 ]; then
        echo "==> Pushing $AHEAD commit(s) to origin/main..."
        if ! git push origin main; then
            echo "ERROR: git push origin main failed." >&2
            echo "Likely causes: non-fast-forward rejection, auth failure, or branch protection." >&2
            exit 3
        fi
    else
        echo "Local main already matches origin/main — nothing to push."
    fi
fi

# Step 2: Rebuild app with unique build arg to bust Docker cache
# Note: Dockerfile Stage 1 runs npm build inside Docker, scanning
# app/templates/ for Tailwind classes. --no-cache ensures fresh rebuild.
# Append timestamp so --no-commit deploys also invalidate COPY layers
BUILD_COMMIT="$(git rev-parse --short HEAD)-$(date +%s)"
echo "==> Rebuilding app container (build tag: $BUILD_COMMIT)..."
docker compose build --no-cache --build-arg BUILD_COMMIT="$BUILD_COMMIT" app

# Step 3: Recreate only the app container with the new image
# Clean up any orphaned rename containers from previous deploys
echo "==> Restarting app..."
docker compose down app 2>/dev/null || true
docker container prune -f --filter "label=com.docker.compose.project=availai" 2>/dev/null || true
docker compose up -d app

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

# Step 5: Verify deployed build tag matches what we just built
echo ""
echo "==> Verifying deployed build tag..."
DEPLOYED_COMMIT=$(docker compose exec app printenv BUILD_COMMIT 2>/dev/null | tr -d '[:space:]' || echo "UNKNOWN")

if [ "$DEPLOYED_COMMIT" != "$BUILD_COMMIT" ]; then
    echo "==> MISMATCH: deployed ($DEPLOYED_COMMIT) does NOT match build ($BUILD_COMMIT)"
    exit 1
fi
echo "==> MATCH: deployed build tag ($DEPLOYED_COMMIT)"

# Step 6: Verify CSS covers Tailwind color classes used in templates
echo ""
echo "==> Verifying Tailwind CSS coverage..."
CSS_FILE=$(docker compose exec app sh -c 'ls /app/app/static/dist/assets/styles-*.css 2>/dev/null' | tr -d '[:space:]')
if [ -z "$CSS_FILE" ]; then
    echo "==> WARNING: Could not find CSS bundle to verify."
else
    MISSING=$(docker compose exec app sh -c "
        grep -rohP '(?:bg|text|border|hover:bg|hover:text)-[a-z]+-\d+' /app/app/templates/ 2>/dev/null \
        | sort -u \
        | while read cls; do
            base=\$(echo \"\$cls\" | sed 's/hover://')
            grep -q \"\$base\" $CSS_FILE || echo \"  MISSING: \$cls\"
        done
    ")
    if [ -n "$MISSING" ]; then
        echo "==> WARNING: Tailwind classes in templates but NOT in CSS bundle:"
        echo "$MISSING"
        echo "==> If classes are missing, Dockerfile Stage 1 may have stale template copies."
    else
        echo "==> All Tailwind color classes in templates are present in CSS bundle."
    fi
fi

# Step 7: Show recent logs to confirm the right code is running
echo ""
echo "==> Recent app logs:"
docker compose logs --tail=20 app
echo ""
echo "==> Deploy complete."
