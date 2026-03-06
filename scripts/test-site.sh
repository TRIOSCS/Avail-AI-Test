#!/usr/bin/env bash
# scripts/test-site.sh — AvailAI Site Agent Test Runner
#
# Launches up to 15 parallel Claude Code subagents with Playwright MCP
# to browser-test all 17 areas of app.availai.net. Each agent navigates
# the real site, checks functionality, and files trouble tickets via API.
#
# Usage:
#   ./scripts/test-site.sh                     # test all 17 areas
#   ./scripts/test-site.sh search crm_companies # test specific areas
#   ./scripts/test-site.sh --help
#
# Called by: operator, post-deploy.sh
# Depends on: claude CLI, agent-prompts/*.md, AGENT_API_KEY in .env

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROMPTS_DIR="$SCRIPT_DIR/agent-prompts"
RESULTS_DIR="$SCRIPT_DIR/test-results"
HISTORY_FILE="$SCRIPT_DIR/test-history.jsonl"
MAX_PARALLEL=15
TIMEOUT_SECS=300  # 5 min per agent

BASE_URL="${BASE_URL:-https://app.availai.net}"
AGENT_KEY="${AGENT_KEY:-$(grep AGENT_API_KEY "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2)}"

# All 17 areas
ALL_AREAS=(search requisitions rfq crm_companies crm_contacts crm_quotes prospecting vendors tagging tickets admin_api_health admin_settings notifications auth upload pipeline activity)

# ── Parse args ──────────────────────────────────────────────────────
if [ $# -gt 0 ]; then
    if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
        echo "Usage: $0 [area1 area2 ...] [--after-deploy]"
        echo "       $0                    # test all 17 areas"
        echo "       $0 search crm_companies  # test specific areas"
        echo ""
        echo "Areas: ${ALL_AREAS[*]}"
        exit 0
    fi
    AREAS=("$@")
else
    AREAS=("${ALL_AREAS[@]}")
fi

# ── Pre-flight ──────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════╗"
echo "║  AvailAI Site Agent Test Runner                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Target:   $BASE_URL"
echo "  Areas:    ${#AREAS[@]}"
echo "  Parallel: $MAX_PARALLEL"
echo ""

# Health check
echo -n "  Health check... "
if curl -sf "$BASE_URL/health" > /dev/null 2>&1; then
    echo "OK"
else
    echo "FAILED -- is the app running?"
    exit 1
fi

# Get agent session cookie
echo -n "  Agent auth... "
COOKIE_JAR=$(mktemp)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -c "$COOKIE_JAR" \
    -X POST "$BASE_URL/auth/agent-session" \
    -H "x-agent-key: $AGENT_KEY")
if [ "$HTTP_CODE" = "200" ]; then
    SESSION_COOKIE=$(grep session "$COOKIE_JAR" | awk '{print $NF}')
    echo "OK"
else
    echo "FAILED (HTTP $HTTP_CODE) -- check AGENT_API_KEY"
    rm -f "$COOKIE_JAR"
    exit 1
fi
rm -f "$COOKIE_JAR"

# Prepare results dir
RUN_ID=$(date +%Y%m%d_%H%M%S)
RUN_DIR="$RESULTS_DIR/$RUN_ID"
mkdir -p "$RUN_DIR"

echo ""
echo "  Run ID:  $RUN_ID"
echo "  Results: $RUN_DIR"
echo ""
echo "─────────────────────────────────────────────────────────"

# ── Build agent prompts ─────────────────────────────────────────────
BASE_PROMPT=$(cat "$PROMPTS_DIR/_base.md" \
    | sed "s|{{BASE_URL}}|$BASE_URL|g" \
    | sed "s|{{AGENT_KEY}}|$AGENT_KEY|g")

# ── Launch agents ───────────────────────────────────────────────────
PIDS=()
declare -A PID_AREA

for area in "${AREAS[@]}"; do
    AREA_FILE="$PROMPTS_DIR/${area}.md"
    if [ ! -f "$AREA_FILE" ]; then
        echo "  WARNING: No prompt file for '$area' -- skipping"
        continue
    fi

    AREA_PROMPT=$(cat "$AREA_FILE" | sed "s|{{BASE_URL}}|$BASE_URL|g" | sed "s|{{AREA}}|$area|g")
    FULL_PROMPT="$BASE_PROMPT

---

$AREA_PROMPT"
    FULL_PROMPT=$(echo "$FULL_PROMPT" | sed "s|{{AREA}}|$area|g")

    # Write prompt to file for claude -p
    PROMPT_FILE="$RUN_DIR/${area}_prompt.md"
    echo "$FULL_PROMPT" > "$PROMPT_FILE"

    # Launch claude in background
    echo "  >> Launching agent: $area"
    timeout "$TIMEOUT_SECS" claude -p "$(cat "$PROMPT_FILE")" \
        --allowedTools "mcp__plugin_playwright_playwright__*,Bash" \
        > "$RUN_DIR/${area}_output.txt" 2>&1 &
    PID=$!
    PIDS+=($PID)
    PID_AREA[$PID]=$area

    # Throttle: wait if at max parallel
    while [ $(jobs -r | wc -l) -ge $MAX_PARALLEL ]; do
        sleep 1
    done
done

# ── Wait and collect results ────────────────────────────────────────
echo ""
echo "  Waiting for ${#PIDS[@]} agents to complete..."
echo ""

PASS=0
FAIL=0
ERROR=0
RESULTS=()

for pid in "${PIDS[@]}"; do
    area="${PID_AREA[$pid]}"
    if wait "$pid" 2>/dev/null; then
        EXIT_CODE=0
    else
        EXIT_CODE=$?
    fi

    OUTPUT="$RUN_DIR/${area}_output.txt"

    if [ $EXIT_CODE -eq 124 ]; then
        STATUS="TIMEOUT"
        ((ERROR++))
    elif grep -q "PASS: $area" "$OUTPUT" 2>/dev/null; then
        STATUS="PASS"
        ((PASS++))
    elif grep -q "trouble-ticket" "$OUTPUT" 2>/dev/null || grep -q "filed ticket" "$OUTPUT" 2>/dev/null; then
        STATUS="FAIL"
        ((FAIL++))
    elif [ $EXIT_CODE -ne 0 ]; then
        STATUS="ERROR"
        ((ERROR++))
    else
        STATUS="PASS"
        ((PASS++))
    fi

    RESULTS+=("$area:$STATUS")

    case $STATUS in
        PASS)    echo "  [PASS] $area" ;;
        FAIL)    echo "  [FAIL] $area -- issues found (see $OUTPUT)" ;;
        TIMEOUT) echo "  [TIME] $area -- timed out after ${TIMEOUT_SECS}s" ;;
        ERROR)   echo "  [ERR]  $area -- agent error (exit $EXIT_CODE)" ;;
    esac
done

# ── Summary ─────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  RESULTS: $PASS pass, $FAIL fail, $ERROR error"
echo "  Output:  $RUN_DIR/"
echo "═══════════════════════════════════════════════════════"

# ── Append to history ───────────────────────────────────────────────
RESULTS_JSON=$(printf '%s\n' "${RESULTS[@]}" | jq -R -s 'split("\n") | map(select(. != "")) | map(split(":") | {area: .[0], status: .[1]})')
echo "{\"run_id\": \"$RUN_ID\", \"timestamp\": \"$(date -Iseconds)\", \"areas_tested\": ${#AREAS[@]}, \"pass\": $PASS, \"fail\": $FAIL, \"error\": $ERROR, \"results\": $RESULTS_JSON}" >> "$HISTORY_FILE"

# ── Diff from last run ──────────────────────────────────────────────
PREV_RUN=$(tail -2 "$HISTORY_FILE" 2>/dev/null | head -1)
if [ -n "$PREV_RUN" ] && [ "$(echo "$PREV_RUN" | jq -r '.run_id')" != "$RUN_ID" ]; then
    echo ""
    echo "  Changes from last run:"
    for r in "${RESULTS[@]}"; do
        area=$(echo "$r" | cut -d: -f1)
        status=$(echo "$r" | cut -d: -f2)
        prev_status=$(echo "$PREV_RUN" | jq -r ".results[] | select(.area == \"$area\") | .status" 2>/dev/null)
        if [ -n "$prev_status" ] && [ "$prev_status" != "$status" ]; then
            echo "    $area: $prev_status -> $status"
        fi
    done
fi

exit $FAIL  # non-zero if any failures
