#!/usr/bin/env bash
# Nightly full test suite + 100% coverage check
# Runs at 2am via cron — logs to /var/log/avail/nightly_tests/

set -euo pipefail

LOG_DIR="/var/log/avail/nightly_tests"
mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/$DATE.log"

echo "=== Nightly Test Run: $(date) ===" > "$LOG_FILE"

cd /root/availai

# Run full suite with coverage
set +e
TESTING=1 PYTHONPATH=/root/availai pytest tests/ \
    --cov=app --cov-report=term-missing --tb=short -q \
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

# Alert if tests failed or coverage dropped below 80%
if [ "$EXIT_CODE" -ne 0 ] || [ "$COVERAGE" -lt 80 ]; then
    echo "ALERT: Tests failed or coverage dropped to ${COVERAGE}%" >> "$LOG_FILE"
    # Write a flag file for easy monitoring
    echo "${DATE}: FAIL (exit=$EXIT_CODE, coverage=${COVERAGE}%)" > "$LOG_DIR/LAST_STATUS"
else
    echo "${DATE}: PASS (coverage=${COVERAGE}%)" > "$LOG_DIR/LAST_STATUS"
fi

# Keep only last 30 days of logs
find "$LOG_DIR" -name "*.log" -mtime +30 -delete 2>/dev/null || true
