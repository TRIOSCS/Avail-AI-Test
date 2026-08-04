#!/usr/bin/env bash
# simp-refresh-db.sh — refresh the simplification parallel instance's database
#                      from the latest production backup (Brief rule 3.7).
#
# What: pulls the newest dump out of production's db-backup container,
#       restores it into the availai-simp db, then restarts the simp app
#       (whose entrypoint runs `alembic upgrade head`) — each run is a
#       rehearsal of the cutover migration path (docs/CUTOVER.md).
# Run:  ./scripts/simp-refresh-db.sh        (from the simplification worktree)
# Touches: ONLY availai-simp-* containers. Production is read from
#       (one `docker cp` out of the backup container), never written.

set -euo pipefail

PROD_BACKUP_CONTAINER="availai-db-backup-1"
SIMP_PROJECT="availai-simp"
SIMP_DB_CONTAINER="${SIMP_PROJECT}-db-1"
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$(mktemp -d /tmp/simp-refresh.XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT

# shellcheck disable=SC1091
set -a; . "$WORKDIR/.env"; set +a

LATEST=$(docker exec "$PROD_BACKUP_CONTAINER" sh -c 'ls -t /backups/availai_*.dump.gz | head -1')
[ -n "$LATEST" ] || { echo "No backup found in $PROD_BACKUP_CONTAINER" >&2; exit 1; }
echo "==> Latest production backup: $LATEST"

docker cp "$PROD_BACKUP_CONTAINER:$LATEST" "$SCRATCH/dump.gz"
# Verify against the checksum written alongside the dump (backup.sh emits one).
if docker exec "$PROD_BACKUP_CONTAINER" sh -c "test -f $LATEST.sha256"; then
    docker cp "$PROD_BACKUP_CONTAINER:$LATEST.sha256" "$SCRATCH/dump.gz.sha256"
    EXPECTED=$(awk '{print $1}' "$SCRATCH/dump.gz.sha256")
    ACTUAL=$(sha256sum "$SCRATCH/dump.gz" | awk '{print $1}')
    [ "$EXPECTED" = "$ACTUAL" ] || { echo "CHECKSUM MISMATCH — aborting" >&2; exit 1; }
    echo "==> Checksum verified."
fi

echo "==> Ensuring simp db container is up..."
docker compose -p "$SIMP_PROJECT" -f "$WORKDIR/docker-compose.simp.yml" --project-directory "$WORKDIR" up -d --wait db

echo "==> Stopping simp app/worker during restore..."
docker compose -p "$SIMP_PROJECT" -f "$WORKDIR/docker-compose.simp.yml" --project-directory "$WORKDIR" stop app enrichment-worker 2>/dev/null || true

echo "==> Dropping and recreating database ${POSTGRES_DB:-availai}..."
docker exec "$SIMP_DB_CONTAINER" psql -U "$POSTGRES_USER" -d postgres \
  -c "DROP DATABASE IF EXISTS \"${POSTGRES_DB:-availai}\" WITH (FORCE);" \
  -c "CREATE DATABASE \"${POSTGRES_DB:-availai}\" OWNER \"$POSTGRES_USER\";"

echo "==> Restoring (pg_restore, custom format)..."
gunzip -c "$SCRATCH/dump.gz" | docker exec -i "$SIMP_DB_CONTAINER" \
  pg_restore -U "$POSTGRES_USER" -d "${POSTGRES_DB:-availai}" --no-owner --role="$POSTGRES_USER"

echo "==> Restarting app (entrypoint runs alembic upgrade head)..."
docker compose -p "$SIMP_PROJECT" -f "$WORKDIR/docker-compose.simp.yml" --project-directory "$WORKDIR" up -d --wait app enrichment-worker

echo "==> Verifying alembic head + row sanity..."
docker exec "$SIMP_DB_CONTAINER" psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-availai}" -tAc \
  "SELECT version_num FROM alembic_version;"
docker exec "$SIMP_DB_CONTAINER" psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-availai}" -tAc \
  "SELECT 'users='||count(*) FROM users;" 2>/dev/null || true

echo "==> Refresh complete."
