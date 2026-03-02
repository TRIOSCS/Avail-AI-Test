#!/bin/bash
# Weekly disk cleanup for AvailAI server
# Runs every Sunday at 3am via cron
# Called by: crontab (0 3 * * 0)
# Depends on: docker, journalctl
set -euo pipefail

LOG="/var/log/avail/weekly_cleanup.log"
mkdir -p /var/log/avail
echo "=== Cleanup started $(date -u) ===" >> "$LOG"

# 1. Docker build cache — keep only last 7 days
docker builder prune -af --filter "until=168h" >> "$LOG" 2>&1 || true

# 2. Rotate backups — keep last 3 SQL dumps and last 3 .dump files
cd /root/backups 2>/dev/null && {
    ls -t *.sql 2>/dev/null | tail -n +4 | xargs -r rm -v >> "$LOG" 2>&1
    ls -t *.dump 2>/dev/null | tail -n +4 | xargs -r rm -v >> "$LOG" 2>&1
    # Remove old code backup dirs (keep last 2)
    ls -dt code_* 2>/dev/null | tail -n +3 | xargs -r rm -rf
}

# 3. Cap journal logs at 200MB
journalctl --vacuum-size=200M >> "$LOG" 2>&1 || true

# 4. Clean Claude debug logs older than 7 days
find /root/.claude/debug -type f -mtime +7 -delete 2>/dev/null || true

# 5. Clean old Claude CLI versions (keep latest)
if [ -d /root/.local/share/claude/versions ]; then
    cd /root/.local/share/claude/versions
    ls -t 2>/dev/null | tail -n +2 | xargs -r rm -v >> "$LOG" 2>&1
fi

# 6. Docker image prune (dangling only)
docker image prune -f >> "$LOG" 2>&1 || true

echo "=== Cleanup finished $(date -u) ===" >> "$LOG"
echo "Disk: $(df -h / | tail -1)" >> "$LOG"
