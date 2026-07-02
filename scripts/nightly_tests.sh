#!/usr/bin/env bash
# Nightly full test suite + coverage check against a CLEAN origin/main snapshot.
# Called by: root crontab (30 2 * * * — 02:30 UTC) — logs to /var/log/avail/nightly_tests/
#
# Runs in a dedicated detached worktree (NOT the live checkout): the live
# /root/availai is routinely mid-branch during active development, so testing it
# at 02:30 tested an arbitrary snapshot — the 2026-06/07 red streak was partly
# stale-checkout drift. pytest runs with cwd INSIDE the worktree so template
# paths resolve against the snapshot, using the main checkout's .venv (deps
# track the host install either way).
#
# Per-day logs here are pruned by /etc/logrotate.d/avail (host config).

set -euo pipefail

LOG_DIR="/var/log/avail/nightly_tests"
mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/$DATE.log"

REPO=/root/availai
WT=/root/availai-worktrees/nightly-main
PYTEST="$REPO/.venv/bin/pytest"

echo "=== Nightly Test Run: $(date) ===" > "$LOG_FILE"

# Refresh the pinned worktree to origin/main (create on first run).
git -C "$REPO" fetch -q origin main >> "$LOG_FILE" 2>&1
if [ ! -d "$WT" ]; then
    git -C "$REPO" worktree add --detach "$WT" origin/main >> "$LOG_FILE" 2>&1
fi
git -C "$WT" checkout -q --force --detach origin/main >> "$LOG_FILE" 2>&1
git -C "$WT" clean -qfd >> "$LOG_FILE" 2>&1
echo "Testing $(git -C "$WT" rev-parse --short HEAD) (origin/main)" >> "$LOG_FILE"

cd "$WT"

# Run full suite with coverage
set +e
TESTING=1 PYTHONPATH="$WT" "$PYTEST" tests/ \
    --cov=app --cov-report=term --tb=short -q \
    >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
set -e

# Extract coverage percentage from output
COVERAGE=$(grep -oP 'TOTAL\s+\d+\s+\d+\s+\K\d+' "$LOG_FILE" || echo "0")

echo "" >> "$LOG_FILE"
echo "=== Summary ===" >> "$LOG_FILE"
echo "Exit code: $EXIT_CODE" >> "$LOG_FILE"
echo "Coverage: ${COVERAGE}%" >> "$LOG_FILE"
echo "Finished: $(date)" >> "$LOG_FILE"

# Classify the outcome honestly: an xdist INTERNALERROR (a worker crashed) is a
# CRASH — pytest aborts before the coverage table, so coverage reads 0% and the
# old summary conflated "suite crashed" with "coverage collapsed".
if grep -q '^INTERNALERROR' "$LOG_FILE"; then
    STATUS="CRASH (xdist internal error — see log; exit=$EXIT_CODE)"
elif [ "$EXIT_CODE" -ne 0 ]; then
    STATUS="FAIL (exit=$EXIT_CODE, coverage=${COVERAGE}%)"
elif [ "$COVERAGE" -lt 80 ]; then
    STATUS="FAIL (coverage dropped to ${COVERAGE}%)"
else
    STATUS="PASS (coverage=${COVERAGE}%)"
fi
echo "${DATE}: ${STATUS}" > "$LOG_DIR/LAST_STATUS"
case "$STATUS" in
    PASS*) : ;;
    *) echo "ALERT: ${STATUS}" >> "$LOG_FILE" ;;
esac

# Keep only last 30 days of logs
find "$LOG_DIR" -name "*.log" -mtime +30 -delete 2>/dev/null || true
