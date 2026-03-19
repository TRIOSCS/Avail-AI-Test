#!/bin/sh
# Enrichment worker entrypoint — waits for DB, runs migrations, starts loop.
#
# Called by: docker-compose enrichment-worker service
# Depends on: database being healthy, alembic migrations

set -e

echo "Enrichment worker starting..."

# Wait for DB (already handled by depends_on healthcheck, but belt-and-suspenders)
echo "Running alembic upgrade head..."
if ! runuser -u appuser -- alembic upgrade head; then
    echo "WARNING: alembic upgrade failed — continuing anyway (app service handles migrations)"
fi

# Drop to non-root and run the enrichment loop
exec runuser -u appuser -- python scripts/enrich_orchestrator.py --loop --interval "${ENRICHMENT_INTERVAL_HOURS:-6}"
