#!/usr/bin/env bash
# canary-monitor.sh — Lightweight production health check.
#
# Hits key endpoints and reports pass/fail. Designed to run via cron
# every 5 minutes. Logs failures to /var/log/availai-canary.log.
#
# Called by: cron (*/5 * * * * /root/availai/scripts/canary-monitor.sh)
# Depends on: curl, running AvailAI app

set -euo pipefail

BASE_URL="${CANARY_URL:-http://127.0.0.1:8000}"
LOG_FILE="${CANARY_LOG:-/var/log/availai-canary.log}"
TIMEOUT=10
FAILURES=0

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >> "$LOG_FILE"
}

check() {
    local name="$1"
    local url="$2"
    local expected_status="${3:-200}"
    local extra_header="${4:-}"

    local curl_args=(-s -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT")
    if [ -n "$extra_header" ]; then
        curl_args+=(-H "$extra_header")
    fi

    local status
    status=$(curl "${curl_args[@]}" "$url" 2>/dev/null || echo "000")

    if [ "$status" = "$expected_status" ]; then
        return 0
    else
        log "FAIL: $name — expected $expected_status, got $status ($url)"
        FAILURES=$((FAILURES + 1))
        return 1
    fi
}

# Health endpoint
check "Health" "$BASE_URL/health"

# App shell loads
check "App Shell" "$BASE_URL/v2"

# Key partials respond (with HX-Request header)
check "Requisitions List" "$BASE_URL/v2/partials/requisitions" "200" "HX-Request: true"
check "Vendors List" "$BASE_URL/v2/partials/vendors" "200" "HX-Request: true"
check "Dashboard" "$BASE_URL/v2/partials/dashboard" "200" "HX-Request: true"
check "Search Form" "$BASE_URL/v2/partials/search" "200" "HX-Request: true"

# API endpoints
check "API Sources" "$BASE_URL/api/v1/sources"

if [ "$FAILURES" -gt 0 ]; then
    log "CANARY: $FAILURES checks failed"
    exit 1
else
    # Only log failures to keep log small. Uncomment below for verbose:
    # log "CANARY: all checks passed"
    exit 0
fi
