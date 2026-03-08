#!/usr/bin/env bash
# scripts/post-deploy.sh — Rebuild, wait for health, then run site agents
#
# One-command deploy + test: rebuilds Docker containers, waits for the
# health endpoint, then launches the site agent test runner.
#
# Usage: ./scripts/post-deploy.sh
#
# Called by: operator (manual), cron, CI
# Depends on: docker compose, test-site.sh

set -euo pipefail

cd /root/availai

echo "=== Rebuilding ==="
docker compose up -d --build

echo "=== Waiting for health (up to 60s) ==="
for i in $(seq 1 12); do
    sleep 5
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "Health check passed after $((i*5))s"
        echo ""
        echo "=== Running site agents ==="
        ./scripts/test-site.sh
        AGENT_EXIT=$?

        # Post-deploy UX smoke test + repair
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        echo ""
        echo "=== Running post-deploy UX repair ==="
        "${SCRIPT_DIR}/ultimate_ux_repair.sh" --post-deploy --no-notify || {
            echo "WARN: Post-deploy UX repair found issues (see report)"
        }

        exit $AGENT_EXIT
    fi
    echo "  Attempt $i/12..."
done

echo "ERROR: Health check failed after 60s"
exit 1
