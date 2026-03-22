#!/usr/bin/env bash
# Database restore script for AvailAI
# What: Restores a backup file to PostgreSQL with safety checks and pre-restore backup
# Called by: Manual — docker compose exec db-backup /scripts/restore.sh [backup_file]
# Depends on: PostgreSQL 16 (pg_restore, psql), gzip

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/backups}"
DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-availai}"
DB_USER="${POSTGRES_USER:-availai}"

# ─── Functions ───────────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"; }
die() { log "FATAL: $1"; exit 1; }

usage() {
    echo "Usage: $0 [backup_file.dump.gz]"
    echo ""
    echo "If no file specified, uses the most recent backup."
    echo ""
    echo "Options:"
    echo "  --list          List all available backups"
    echo "  --verify FILE   Verify a backup without restoring"
    echo "  --no-safety     Skip pre-restore backup (DANGEROUS)"
    echo ""
    echo "Examples:"
    echo "  $0                                          # Restore latest"
    echo "  $0 /backups/availai_20260321_060000.dump.gz # Restore specific"
    echo "  $0 --list                                   # Show available backups"
    echo "  $0 --verify /backups/availai_20260321.dump.gz"
    exit 1
}

list_backups() {
    log "Available backups in ${BACKUP_DIR}:"
    echo ""
    if [ -d "$BACKUP_DIR" ]; then
        local count=0
        while IFS= read -r f; do
            size=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f")
            size_human=$(numfmt --to=iec-i --suffix=B "$size" 2>/dev/null || echo "${size} bytes")
            date_str=$(stat -c%y "$f" 2>/dev/null | cut -d. -f1 || stat -f%Sm "$f")
            checksum_status="no checksum"
            if [ -f "${f}.sha256" ]; then
                if sha256sum -c "${f}.sha256" >/dev/null 2>&1; then
                    checksum_status="checksum OK"
                else
                    checksum_status="CHECKSUM MISMATCH"
                fi
            fi
            echo "  ${f}  (${size_human}, ${date_str}, ${checksum_status})"
            count=$((count + 1))
        done < <(find "$BACKUP_DIR" -name "${DB_NAME}_*.dump.gz" -type f | sort -r)
        echo ""
        echo "Total: ${count} backups"
    else
        echo "  No backup directory found at ${BACKUP_DIR}"
    fi
    exit 0
}

verify_backup() {
    local file="$1"
    log "Verifying backup: ${file}"

    if [ ! -f "$file" ]; then
        die "File not found: ${file}"
    fi

    # Check checksum if available
    if [ -f "${file}.sha256" ]; then
        if sha256sum -c "${file}.sha256" >/dev/null 2>&1; then
            log "Checksum: VALID"
        else
            die "Checksum: MISMATCH — file may be corrupted"
        fi
    else
        log "Checksum: no .sha256 file found (skipping)"
    fi

    # List contents
    local table_count
    table_count=$(gunzip -c "$file" | pg_restore --list 2>/dev/null | grep "TABLE " | wc -l || true)
    log "Tables in backup: ${table_count}"

    if [ "$table_count" -lt 5 ]; then
        die "Backup appears corrupt — only ${table_count} tables found"
    fi

    log "Backup verification: PASSED"
    exit 0
}

# ─── Parse arguments ─────────────────────────────────────────────────────────
BACKUP_FILE=""
SKIP_SAFETY=false

for arg in "$@"; do
    case "$arg" in
        --list) list_backups ;;
        --verify)
            shift
            verify_backup "${1:-}"
            ;;
        --no-safety) SKIP_SAFETY=true ;;
        --help|-h) usage ;;
        *) BACKUP_FILE="$arg" ;;
    esac
done

# ─── Resolve backup file ────────────────────────────────────────────────────
if [ -z "$BACKUP_FILE" ]; then
    if [ -f "${BACKUP_DIR}/LATEST" ]; then
        BACKUP_FILE=$(cat "${BACKUP_DIR}/LATEST")
    else
        BACKUP_FILE=$(find "$BACKUP_DIR" -name "${DB_NAME}_*.dump.gz" -type f | sort -r | head -1)
    fi
fi

if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    die "No backup file found. Run: $0 --list"
fi

export PGPASSWORD="${PGPASSWORD:-$POSTGRES_PASSWORD}"

# ─── Safety checks ──────────────────────────────────────────────────────────
log "╔══════════════════════════════════════════════════════════════╗"
log "║  WARNING: DATABASE RESTORE                                  ║"
log "╠══════════════════════════════════════════════════════════════╣"
log "║  This will DROP and recreate the '${DB_NAME}' database."
log "║  Backup file: ${BACKUP_FILE}"
log "║  Target: ${DB_HOST}:${DB_PORT}/${DB_NAME}"
log "╚══════════════════════════════════════════════════════════════╝"

# Verify the backup before proceeding
log "Verifying backup integrity..."
if [ -f "${BACKUP_FILE}.sha256" ]; then
    if sha256sum -c "${BACKUP_FILE}.sha256" >/dev/null 2>&1; then
        log "Checksum: VALID"
    else
        die "Checksum MISMATCH — backup file may be corrupted. Aborting restore."
    fi
fi

TABLE_COUNT=$(gunzip -c "$BACKUP_FILE" | pg_restore --list 2>/dev/null | grep "TABLE " | wc -l || true)
if [ "$TABLE_COUNT" -lt 5 ]; then
    die "Backup appears corrupt — only ${TABLE_COUNT} tables. Aborting."
fi
log "Backup contains ${TABLE_COUNT} tables — looks good"

# Interactive confirmation (skip if piped/non-interactive)
if [ -t 0 ]; then
    echo ""
    read -r -p "Type 'RESTORE' to confirm: " confirm
    if [ "$confirm" != "RESTORE" ]; then
        log "Aborted by user."
        exit 1
    fi
fi

# ─── Pre-restore safety backup ──────────────────────────────────────────────
if [ "$SKIP_SAFETY" = false ]; then
    SAFETY_FILE="${BACKUP_DIR}/${DB_NAME}_pre_restore_$(date +%Y%m%d_%H%M%S).dump.gz"
    log "Creating safety backup before restore → ${SAFETY_FILE}"
    pg_dump \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        --format=custom --no-owner --no-acl --compress=0 \
        2>/dev/null | gzip -9 > "$SAFETY_FILE" || true

    SAFETY_SIZE=$(stat -c%s "$SAFETY_FILE" 2>/dev/null || stat -f%z "$SAFETY_FILE" 2>/dev/null || echo "0")
    if [ "$SAFETY_SIZE" -gt 1024 ]; then
        log "Safety backup created: ${SAFETY_FILE} ($(numfmt --to=iec-i --suffix=B "$SAFETY_SIZE" 2>/dev/null || echo "${SAFETY_SIZE} bytes"))"
    else
        log "WARNING: Safety backup is empty or missing (current DB may be empty)"
    fi
fi

# ─── Restore ─────────────────────────────────────────────────────────────────
log "Dropping existing database..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}' AND pid <> pg_backend_pid();" \
    >/dev/null 2>&1 || true

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c \
    "DROP DATABASE IF EXISTS ${DB_NAME};" \
    2>/dev/null

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c \
    "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" \
    2>/dev/null

log "Restoring from ${BACKUP_FILE}..."
gunzip -c "$BACKUP_FILE" | pg_restore \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --no-owner \
    --no-acl \
    --jobs=4 \
    --verbose \
    2>&1 | tail -5

# ─── Post-restore verification ──────────────────────────────────────────────
log "Verifying restore..."

RESTORED_TABLES=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';" \
    2>/dev/null | tr -d ' ')

RESTORED_ROWS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c \
    "SELECT string_agg(t.name || '=' || t.cnt, ', ')
     FROM (
       SELECT 'users' AS name, count(*)::text AS cnt FROM users
       UNION ALL SELECT 'companies', count(*)::text FROM companies
       UNION ALL SELECT 'vendor_cards', count(*)::text FROM vendor_cards
       UNION ALL SELECT 'requisitions', count(*)::text FROM requisitions
       UNION ALL SELECT 'material_cards', count(*)::text FROM material_cards
     ) t;" 2>/dev/null | tr -d ' ')

log "╔══════════════════════════════════════════════════════════════╗"
log "║  RESTORE COMPLETE                                           ║"
log "╠══════════════════════════════════════════════════════════════╣"
log "║  Tables restored: ${RESTORED_TABLES}"
log "║  Row counts: ${RESTORED_ROWS}"
if [ "$SKIP_SAFETY" = false ] && [ "${SAFETY_SIZE:-0}" -gt 1024 ]; then
log "║  Safety backup: ${SAFETY_FILE}"
fi
log "╚══════════════════════════════════════════════════════════════╝"
log ""
log "NEXT STEPS:"
log "  1. Restart the app: docker compose restart app"
log "  2. Run migrations:  docker compose exec app alembic upgrade head"
log "  3. Check the app:   curl -f https://app.availai.net/health"
