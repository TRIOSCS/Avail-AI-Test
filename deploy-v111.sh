#!/bin/bash
# ──────────────────────────────────────────────────────────────
# AVAIL v1.1.1 Deployment Script
# Server: 104.248.191.152 (DigitalOcean)
# ──────────────────────────────────────────────────────────────
# 
# USAGE (from your local machine):
#   1. Upload the zip:
#      scp avail-v111.zip root@104.248.191.152:/root/
#
#   2. SSH in and run:
#      ssh root@104.248.191.152
#      bash deploy-v111.sh
#
# WHAT THIS DOES:
#   Step 1: Backup current database
#   Step 2: Extract v1.1.1 code
#   Step 3: Run duplicate user merge migration
#   Step 4: Rebuild Docker image (no cache) and restart
#   Step 5: Health check
# ──────────────────────────────────────────────────────────────

set -e  # Exit on any error

APP_DIR="/root/avail"       # Adjust if your app lives elsewhere
ZIP_FILE="/root/avail-v111.zip"
BACKUP_DIR="/root/backups"

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  AVAIL v1.1.1 Deployment"
echo "══════════════════════════════════════════════════════════"
echo ""

# ── Pre-flight checks ──────────────────────────────────────────
if [ ! -f "$ZIP_FILE" ]; then
    echo "ERROR: $ZIP_FILE not found. Upload it first:"
    echo "  scp avail-v111.zip root@104.248.191.152:/root/"
    exit 1
fi

if [ ! -d "$APP_DIR" ]; then
    echo "ERROR: App directory $APP_DIR not found."
    echo "  Update APP_DIR in this script to match your server."
    exit 1
fi

cd "$APP_DIR"

# ── Step 1: Backup database ────────────────────────────────────
echo "Step 1: Backing up database..."
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
docker compose exec -T db pg_dump -U availai availai > "$BACKUP_DIR/avail_pre_v111_$TIMESTAMP.sql"
echo "  ✓ Backup saved: $BACKUP_DIR/avail_pre_v111_$TIMESTAMP.sql"
echo "    Size: $(du -h $BACKUP_DIR/avail_pre_v111_$TIMESTAMP.sql | cut -f1)"
echo ""

# ── Step 2: Extract new code ───────────────────────────────────
echo "Step 2: Extracting v1.1.1 code..."
# Preserve .env and docker volumes
unzip -o "$ZIP_FILE" -d "$APP_DIR"
echo "  ✓ Code extracted"
echo ""

# ── Step 3: Run duplicate user merge ───────────────────────────
echo "Step 3: Running duplicate user merge migration..."
echo "  (This reassigns records and normalizes the email)"
echo ""

# Run migration inside the app container (which has DB access)
# We need the container running first, so do a quick build+up
docker compose build --no-cache app
docker compose up -d db
echo "  Waiting for database..."
sleep 5

# Run migration using a one-off container
docker compose run --rm app python migrate_merge_users.py

echo ""

# ── Step 4: Restart with new code ──────────────────────────────
echo "Step 4: Starting v1.1.1..."
docker compose up -d
echo "  ✓ Containers started"
echo ""

# ── Step 5: Health check ───────────────────────────────────────
echo "Step 5: Health check..."
sleep 5
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo "000")

if [ "$HEALTH" = "200" ]; then
    VERSION=$(curl -s http://localhost:8000/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null || echo "unknown")
    echo "  ✓ Health check passed — version: $VERSION"
else
    echo "  ⚠ Health check returned HTTP $HEALTH"
    echo "  Check logs: docker compose logs app --tail 50"
fi

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  Deployment complete"
echo ""
echo "  Backup:  $BACKUP_DIR/avail_pre_v111_$TIMESTAMP.sql"
echo "  Rollback: docker compose exec -T db psql -U availai availai < $BACKUP_DIR/avail_pre_v111_$TIMESTAMP.sql"
echo "══════════════════════════════════════════════════════════"
echo ""
