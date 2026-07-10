#!/usr/bin/env bash
# scripts/verify-backup.sh — Scheduled database backup verification (P1.5)
#
# What: Confirms the newest AvailAI database backup is actually restorable by
# running `restore.sh --verify` (checksum + `pg_restore --list` sanity check —
# read-only, never touches the live DB) against it, executed inside the
# `db-backup` container where the backup volume and postgres client tools
# already live. Exits nonzero with a clear FATAL message on any failure
# (no backup found, checksum mismatch, corrupt/truncated dump), so a systemd
# `OnFailure=` hook, cron mail, or a journal-tailing monitor can alert on it
# instead of a corrupt backup sitting unnoticed for the full 30-day retention.
#
# Run manually (from the repo root on the server):
#   ./scripts/verify-backup.sh
#
# Install as a weekly systemd timer (Sun 04:00) — from the repo root on the
# target server:
#   sudo cp scripts/systemd/avail-backup-verify.service \
#           scripts/systemd/avail-backup-verify.timer /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now avail-backup-verify.timer
#   systemctl list-timers avail-backup-verify.timer   # confirm the next run
#   journalctl -u avail-backup-verify.service         # check output on demand
#
# Called by: scripts/systemd/avail-backup-verify.timer (weekly), or manually
# Depends on: docker compose (db-backup service must be up), scripts/restore.sh --verify

set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/availai}"
cd "$REPO_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [verify-backup] $1"; }
die() { log "FATAL: $1"; exit 1; }

if ! docker compose ps db-backup >/dev/null 2>&1; then
    die "db-backup service not found — is docker compose running from ${REPO_DIR}?"
fi

# backup.sh writes /backups/LATEST with the exact path of the newest backup
# (plaintext .dump.gz, or .dump.gz.gpg when BACKUP_GPG_PASSPHRASE is set) —
# reuse that instead of re-deriving "newest" ourselves.
LATEST=$(docker compose exec -T db-backup cat /backups/LATEST 2>/dev/null | tr -d '\r\n' || true)
if [ -z "$LATEST" ]; then
    die "No /backups/LATEST marker found inside db-backup — has a backup ever run? (docker compose exec db-backup /scripts/backup.sh)"
fi

log "Verifying newest backup: ${LATEST}"
if docker compose exec -T db-backup /scripts/restore.sh --verify "$LATEST"; then
    log "PASSED — ${LATEST} is a valid, restorable backup."
    exit 0
else
    die "Backup verification FAILED for ${LATEST} — investigate immediately (see restore.sh output above). A corrupt newest backup means the next-newest good one is your actual recovery point; check with: docker compose exec db-backup /scripts/restore.sh --list"
fi
