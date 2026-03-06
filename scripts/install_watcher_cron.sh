#!/usr/bin/env bash
# Install cron job for the self-heal watcher (runs every 2 minutes).
#
# Called by: manual setup after deploy
# Depends on: scripts/self_heal_watcher.sh
set -euo pipefail

CRON_LINE="*/2 * * * * /bin/bash /root/availai/scripts/self_heal_watcher.sh >> /var/log/avail/self_heal_watcher.log 2>&1"

# Check if already installed
if crontab -l 2>/dev/null | grep -q "self_heal_watcher"; then
    echo "Watcher cron already installed"
    exit 0
fi

# Add to crontab
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo "Watcher cron installed: runs every 2 minutes"
