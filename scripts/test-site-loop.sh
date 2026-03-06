#!/usr/bin/env bash
# scripts/test-site-loop.sh — Run site agents, retry timeouts, until clean
#
# Runs all 17 areas, then re-runs any that timed out or errored,
# repeating until all areas have a definitive PASS/FAIL result.
# Designed to run unattended in screen/tmux.
#
# Usage:
#   screen -S agents ./scripts/test-site-loop.sh   # detachable
#   ./scripts/test-site-loop.sh                     # foreground
#
# Called by: operator
# Depends on: test-site.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/test-results"
MAX_ROUNDS=3
ROUND=1

ALL_AREAS=(search requisitions rfq crm_companies crm_contacts crm_quotes prospecting vendors tagging tickets admin_api_health admin_settings notifications auth upload pipeline activity)
REMAINING=("${ALL_AREAS[@]}")

echo "=============================================="
echo "  Site Agent Loop — up to $MAX_ROUNDS rounds"
echo "  Areas: ${#ALL_AREAS[@]}"
echo "=============================================="
echo ""

while [ ${#REMAINING[@]} -gt 0 ] && [ $ROUND -le $MAX_ROUNDS ]; do
    echo ">>> Round $ROUND: testing ${#REMAINING[@]} areas"
    echo ""

    # Run test-site.sh with remaining areas
    "$SCRIPT_DIR/test-site.sh" "${REMAINING[@]}" || true

    # Find the latest results dir
    LATEST_DIR=$(ls -td "$RESULTS_DIR"/20* 2>/dev/null | head -1)
    if [ -z "$LATEST_DIR" ]; then
        echo "ERROR: No results directory found"
        exit 1
    fi

    # Check which areas need retry (timed out = 0 bytes output)
    RETRY=()
    PASSED=()
    FAILED=()
    for area in "${REMAINING[@]}"; do
        OUTPUT="$LATEST_DIR/${area}_output.txt"
        if [ ! -f "$OUTPUT" ]; then
            RETRY+=("$area")
        elif [ ! -s "$OUTPUT" ]; then
            # Empty file = timeout
            RETRY+=("$area")
        elif grep -qi "PASS:" "$OUTPUT" 2>/dev/null; then
            PASSED+=("$area")
        else
            FAILED+=("$area")
        fi
    done

    echo ""
    echo "--- Round $ROUND Summary ---"
    echo "  Passed:  ${#PASSED[@]} (${PASSED[*]:-none})"
    echo "  Failed:  ${#FAILED[@]} (${FAILED[*]:-none})"
    echo "  Retry:   ${#RETRY[@]} (${RETRY[*]:-none})"
    echo ""

    if [ ${#RETRY[@]} -eq 0 ]; then
        echo "All areas have definitive results. Done!"
        break
    fi

    REMAINING=("${RETRY[@]}")
    ROUND=$((ROUND+1))

    if [ $ROUND -le $MAX_ROUNDS ]; then
        echo "Waiting 30s before retry..."
        sleep 30
    fi
done

if [ ${#REMAINING[@]} -gt 0 ] && [ $ROUND -gt $MAX_ROUNDS ]; then
    echo ""
    echo "WARNING: ${#REMAINING[@]} areas still inconclusive after $MAX_ROUNDS rounds:"
    echo "  ${REMAINING[*]}"
fi

echo ""
echo "=============================================="
echo "  Loop complete. Check scripts/test-results/"
echo "=============================================="
