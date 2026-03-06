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
SUMMARY_FILE="$SCRIPT_DIR/test-results/final-summary.txt"
MAX_ROUNDS=5
ROUND=1

ALL_AREAS=(search requisitions rfq crm_companies crm_contacts crm_quotes prospecting vendors tagging tickets admin_api_health admin_settings notifications auth upload pipeline activity)
REMAINING=("${ALL_AREAS[@]}")

# Track cumulative results across rounds
declare -A AREA_STATUS
declare -A AREA_OUTPUT
for area in "${ALL_AREAS[@]}"; do
    AREA_STATUS[$area]="pending"
    AREA_OUTPUT[$area]=""
done

echo "=============================================="
echo "  Site Agent Deep Test — up to $MAX_ROUNDS rounds"
echo "  Areas: ${#ALL_AREAS[@]}"
echo "  Started: $(date)"
echo "=============================================="
echo ""

while [ ${#REMAINING[@]} -gt 0 ] && [ $ROUND -le $MAX_ROUNDS ]; do
    echo ">>> Round $ROUND: testing ${#REMAINING[@]} areas ($(date +%H:%M:%S))"
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
    ROUND_PASSED=()
    ROUND_FAILED=()
    for area in "${REMAINING[@]}"; do
        OUTPUT="$LATEST_DIR/${area}_output.txt"
        if [ ! -f "$OUTPUT" ] || [ ! -s "$OUTPUT" ]; then
            # Empty file = timeout
            RETRY+=("$area")
        elif grep -qi "PASS:" "$OUTPUT" 2>/dev/null; then
            ROUND_PASSED+=("$area")
            AREA_STATUS[$area]="PASS"
            AREA_OUTPUT[$area]="$OUTPUT"
        else
            ROUND_FAILED+=("$area")
            AREA_STATUS[$area]="FAIL"
            AREA_OUTPUT[$area]="$OUTPUT"
        fi
    done

    echo ""
    echo "--- Round $ROUND Summary ---"
    echo "  Passed:  ${#ROUND_PASSED[@]} (${ROUND_PASSED[*]:-none})"
    echo "  Failed:  ${#ROUND_FAILED[@]} (${ROUND_FAILED[*]:-none})"
    echo "  Retry:   ${#RETRY[@]} (${RETRY[*]:-none})"
    echo ""

    if [ ${#RETRY[@]} -eq 0 ]; then
        echo "All areas have definitive results!"
        break
    fi

    REMAINING=("${RETRY[@]}")
    ROUND=$((ROUND+1))

    if [ $ROUND -le $MAX_ROUNDS ]; then
        echo "Waiting 30s before retry..."
        sleep 30
    fi
done

# ── Final Summary ───────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════"
echo "  FINAL RESULTS ($(date))"
echo "══════════════════════════════════════════════════════════"

TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_TIMEOUT=0

{
    echo "Site Agent Deep Test — Final Summary"
    echo "Date: $(date)"
    echo "Rounds completed: $((ROUND > MAX_ROUNDS ? MAX_ROUNDS : ROUND))"
    echo ""
    echo "RESULTS:"
} > "$SUMMARY_FILE"

for area in "${ALL_AREAS[@]}"; do
    status="${AREA_STATUS[$area]}"
    case $status in
        PASS)
            echo "  [PASS]    $area"
            echo "  [PASS]    $area" >> "$SUMMARY_FILE"
            TOTAL_PASS=$((TOTAL_PASS+1))
            ;;
        FAIL)
            echo "  [FAIL]    $area"
            echo "  [FAIL]    $area" >> "$SUMMARY_FILE"
            TOTAL_FAIL=$((TOTAL_FAIL+1))
            ;;
        *)
            echo "  [TIMEOUT] $area"
            echo "  [TIMEOUT] $area" >> "$SUMMARY_FILE"
            TOTAL_TIMEOUT=$((TOTAL_TIMEOUT+1))
            ;;
    esac
done

echo ""
echo "  Total: $TOTAL_PASS pass, $TOTAL_FAIL fail, $TOTAL_TIMEOUT timeout"
echo "  Summary: $SUMMARY_FILE"
echo "══════════════════════════════════════════════════════════"

{
    echo ""
    echo "Total: $TOTAL_PASS pass, $TOTAL_FAIL fail, $TOTAL_TIMEOUT timeout"
    echo ""
    echo "ISSUES FOUND:"
    # Extract ticket references from all output files
    for area in "${ALL_AREAS[@]}"; do
        output="${AREA_OUTPUT[$area]}"
        if [ -n "$output" ] && [ -f "$output" ]; then
            tickets=$(grep -oi "TT-[0-9]\{8\}-[0-9]\{3\}" "$output" 2>/dev/null | sort -u)
            if [ -n "$tickets" ]; then
                echo "  $area: $tickets"
            fi
        fi
    done
} >> "$SUMMARY_FILE"
