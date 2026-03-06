#!/usr/bin/env bash
# self_heal_watcher.sh — Host-side watcher for the self-heal fix pipeline.
#
# Monitors fix_queue/ for JSON fix files produced by the in-container
# execution_service.py, then for each file:
#   1. Creates a git branch
#   2. Applies patches via scripts/apply_patches.py
#   3. Commits, merges to main, rebuilds the Docker container
#   4. Waits for health, then triggers verify-retest
#   5. On retest failure, reverts the merge and rebuilds
#
# What calls it:
#   Cron or systemd timer on the Docker host (e.g. every 5 minutes).
#
# What it depends on:
#   - bash, git, curl, python3, docker compose
#   - scripts/apply_patches.py (stdlib-only Python script)
#   - App running at APP_URL with /health and /api/internal/verify-retest/{id}

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJ_DIR="/root/availai"
QUEUE_DIR="${PROJ_DIR}/fix_queue"
APPLIED_DIR="${PROJ_DIR}/fix_queue/applied"
FAILED_DIR="${PROJ_DIR}/fix_queue/failed"
APP_URL="http://localhost:80"
LOG_FILE="/var/log/avail/self_heal_watcher.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "${ts}  $*" | tee -a "${LOG_FILE}"
}

# ---------------------------------------------------------------------------
# Ensure directories exist
# ---------------------------------------------------------------------------
mkdir -p "${QUEUE_DIR}" "${APPLIED_DIR}" "${FAILED_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"

# ---------------------------------------------------------------------------
# process_fix — handle a single fix JSON file
# ---------------------------------------------------------------------------
process_fix() {
    local fix_file="$1"
    local filename
    filename="$(basename "${fix_file}")"

    log "Processing ${filename}"

    # 1. Extract ticket_id from JSON
    local ticket_id
    ticket_id="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['ticket_id'])" "${fix_file}" 2>/dev/null)" || {
        log "ERR  Cannot extract ticket_id from ${filename}"
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
        return
    }

    local branch="fix/ticket-${ticket_id}"
    log "Ticket #${ticket_id} — branch ${branch}"

    cd "${PROJ_DIR}"

    # 2. Create git branch from current HEAD
    git checkout main 2>/dev/null || git checkout master 2>/dev/null
    git checkout -b "${branch}"

    # 3. Apply patches
    if ! python3 scripts/apply_patches.py "${fix_file}"; then
        log "ERR  Patches failed for ticket #${ticket_id}"
        git checkout main 2>/dev/null || git checkout master 2>/dev/null
        git branch -D "${branch}" 2>/dev/null || true
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
        return
    fi

    # 4. Commit changes
    git add -A
    git commit -m "$(cat <<EOF
fix: self-heal ticket #${ticket_id}

Automated patch applied by self-heal pipeline.

Co-Authored-By: AvailAI Self-Heal <noreply@availai.local>
EOF
    )"

    # 5. Merge branch to main with --no-ff
    git checkout main 2>/dev/null || git checkout master 2>/dev/null
    if ! git merge --no-ff "${branch}" -m "Merge ${branch}: self-heal fix for ticket #${ticket_id}"; then
        log "ERR  Merge conflict for ticket #${ticket_id}"
        git merge --abort 2>/dev/null || true
        git branch -D "${branch}" 2>/dev/null || true
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
        return
    fi

    # 6. Rebuild container
    log "Rebuilding containers..."
    docker compose up -d --build

    # 7. Wait up to 60s for health check
    local waited=0
    local healthy=false
    while [ "${waited}" -lt 60 ]; do
        if curl -sf "${APP_URL}/health" >/dev/null 2>&1; then
            healthy=true
            break
        fi
        sleep 3
        waited=$((waited + 3))
    done

    if [ "${healthy}" != "true" ]; then
        log "ERR  Health check failed after 60s for ticket #${ticket_id} — reverting"
        git revert HEAD --no-edit
        docker compose up -d --build
        git branch -D "${branch}" 2>/dev/null || true
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
        return
    fi

    log "Container healthy after ${waited}s"

    # 8. Trigger verify-retest
    local retest_status
    retest_status="$(curl -sf -o /dev/null -w '%{http_code}' \
        -X POST "${APP_URL}/api/internal/verify-retest/${ticket_id}")" || retest_status="000"

    if [ "${retest_status}" = "200" ]; then
        log "OK   Ticket #${ticket_id} retest passed"
        mv "${fix_file}" "${APPLIED_DIR}/${filename}"
    else
        log "ERR  Retest failed (HTTP ${retest_status}) for ticket #${ticket_id} — reverting"
        git revert HEAD --no-edit
        docker compose up -d --build
        # Wait briefly for rebuild after revert
        local rw=0
        while [ "${rw}" -lt 60 ]; do
            if curl -sf "${APP_URL}/health" >/dev/null 2>&1; then
                break
            fi
            sleep 3
            rw=$((rw + 3))
        done
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
    fi

    # 9. Clean up branch
    git branch -D "${branch}" 2>/dev/null || true

    log "Finished processing ticket #${ticket_id}"
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
log "Self-heal watcher starting"

# Pre-flight: check app is healthy
if ! curl -sf "${APP_URL}/health" >/dev/null 2>&1; then
    log "WARN App is not healthy at ${APP_URL}/health — skipping this run"
    exit 0
fi

# Process each fix JSON in the queue
shopt -s nullglob
fix_files=("${QUEUE_DIR}"/*.json)
shopt -u nullglob

if [ ${#fix_files[@]} -eq 0 ]; then
    log "No fix files in queue"
else
    log "Found ${#fix_files[@]} fix file(s) to process"
    for fix_file in "${fix_files[@]}"; do
        # Skip files in subdirectories (applied/, failed/)
        if [ "$(dirname "${fix_file}")" != "${QUEUE_DIR}" ]; then
            continue
        fi
        process_fix "${fix_file}"
    done
fi

log "Watcher run complete"
