#!/usr/bin/env bash
# scripts/backup-verify-alert.sh — OnFailure= alert for avail-backup-verify.service (P1.5)
#
# What: Fires when avail-backup-verify.service exits nonzero (a corrupt/missing
# newest backup). Self-contained — no external mail/SMTP dependency, since no such
# convention exists elsewhere on the host (unlike the app's own SMTP_FROM, which is
# Microsoft Graph delegated-send only, not usable from a bare systemd unit):
#   1. Logs loudly at "err" priority via systemd-cat, so it's impossible to miss in
#      `journalctl -p err` / any journal-based alerting already watching the host.
#   2. Broadcasts via `wall` to every logged-in terminal — an operator SSH'd into
#      the box right now sees it immediately.
#   3. Writes a durable marker file (BACKUP_ALERT_MARKER, default
#      /root/backups/VERIFY_FAILED) with a timestamp — deploy.sh checks for this
#      marker on every deploy and warns loudly until it's cleared, so a failure
#      can't silently go unnoticed between Sunday verifications and the next
#      deploy. Clear it manually after investigating:
#        rm -f /root/backups/VERIFY_FAILED
#
# Called by: scripts/systemd/avail-backup-verify-alert.service (OnFailure= from
#            avail-backup-verify.service), never invoked directly by cron/CI
# Depends on: systemd-cat (util-linux), wall (util-linux, optional — best-effort),
#             journalctl (to pull the failing unit's last output into the marker)

set -euo pipefail

MARKER="${BACKUP_ALERT_MARKER:-/root/backups/VERIFY_FAILED}"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
MESSAGE="AvailAI backup verification FAILED at ${TIMESTAMP} — investigate immediately: journalctl -u avail-backup-verify.service"

echo "${MESSAGE}" | systemd-cat -t avail-backup-verify -p err || true
command -v wall >/dev/null 2>&1 && wall "${MESSAGE}" || true

mkdir -p "$(dirname "${MARKER}")"
{
    echo "${MESSAGE}"
    echo "--- last avail-backup-verify.service output ---"
    journalctl -u avail-backup-verify.service -n 40 --no-pager 2>/dev/null || echo "(journalctl output unavailable)"
} > "${MARKER}"

echo "Wrote alert marker: ${MARKER}"
