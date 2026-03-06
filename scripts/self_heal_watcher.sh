#!/usr/bin/env bash
# self_heal_watcher.sh — Host-side watcher for the self-heal fix pipeline.
#
# Monitors fix_queue/ for JSON fix files produced by the in-container
# execution_service.py, then for each file:
#   1. Stashes uncommitted work, creates a git branch
#   2. Applies patches via scripts/apply_patches.py
#   3. Commits, merges to main, rebuilds the Docker container
#   4. Waits for health, then triggers verify-retest
#   5. On retest failure, reverts the merge and rebuilds
#
# What calls it:
#   Cron or systemd timer on the Docker host (e.g. every 2 minutes).
#
# What it depends on:
#   - bash, git, python3, docker compose
#   - scripts/apply_patches.py (stdlib-only Python script)
#   - App running inside Docker with /health and /api/internal/verify-retest/{id}

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJ_DIR="/root/availai"
QUEUE_DIR="${PROJ_DIR}/fix_queue"
APPLIED_DIR="${PROJ_DIR}/fix_queue/applied"
FAILED_DIR="${PROJ_DIR}/fix_queue/failed"
APP_URL="http://localhost:8000"
LOG_FILE="/var/log/avail/self_heal_watcher.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "${ts}  $*" | tee -a "${LOG_FILE}"
}

# Check app health via docker compose exec (port 8000 not exposed to host)
app_healthy() {
    docker compose -f "${PROJ_DIR}/docker-compose.yml" exec -T app \
        python3 -c "import urllib.request; urllib.request.urlopen('${APP_URL}/health')" \
        >/dev/null 2>&1
}

# Hit an internal API endpoint via docker compose exec
app_post() {
    local path="$1"
    docker compose -f "${PROJ_DIR}/docker-compose.yml" exec -T app \
        python3 -c "
import urllib.request
req = urllib.request.Request('${APP_URL}${path}', method='POST', data=b'')
try:
    resp = urllib.request.urlopen(req, timeout=120)
    print(resp.status)
except Exception as e:
    print('000')
" 2>/dev/null
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

    # 2. Stash any uncommitted work, create git branch from current HEAD
    local stashed=false
    if [ -n "$(git status --porcelain)" ]; then
        git stash push -m "watcher: before fix/ticket-${ticket_id}" 2>/dev/null && stashed=true
        log "Stashed uncommitted changes"
    fi
    git checkout main 2>/dev/null || git checkout master 2>/dev/null
    git checkout -b "${branch}"

    # 3. Apply patches
    if ! python3 scripts/apply_patches.py "${fix_file}"; then
        log "ERR  Patches failed for ticket #${ticket_id}"
        git checkout main 2>/dev/null || git checkout master 2>/dev/null
        git branch -D "${branch}" 2>/dev/null || true
        if [ "${stashed}" = "true" ]; then
            git stash pop 2>/dev/null || log "WARN  Could not restore stash"
        fi
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
        if [ "${stashed}" = "true" ]; then
            git stash pop 2>/dev/null || log "WARN  Could not restore stash"
        fi
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
        return
    fi

    # 6. Rebuild container
    log "Rebuilding containers..."
    docker compose up -d --build

    # 7. Wait up to 90s for health check (via docker exec)
    local waited=0
    local healthy=false
    while [ "${waited}" -lt 90 ]; do
        if app_healthy; then
            healthy=true
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done

    if [ "${healthy}" != "true" ]; then
        log "ERR  Health check failed after 90s for ticket #${ticket_id} — reverting"
        git revert HEAD --no-edit
        docker compose up -d --build
        git branch -D "${branch}" 2>/dev/null || true
        if [ "${stashed}" = "true" ]; then
            git stash pop 2>/dev/null || log "WARN  Could not restore stash"
        fi
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
        return
    fi

    log "Container healthy after ${waited}s"

    # 8. Trigger verify-retest (via docker exec)
    local retest_status
    retest_status="$(app_post "/api/internal/verify-retest/${ticket_id}")" || retest_status="000"
    retest_status="$(echo "${retest_status}" | tr -d '[:space:]')"

    if [ "${retest_status}" = "200" ]; then
        log "OK   Ticket #${ticket_id} retest passed"
        mv "${fix_file}" "${APPLIED_DIR}/${filename}"
    else
        log "ERR  Retest failed (HTTP ${retest_status}) for ticket #${ticket_id} — reverting"
        git revert HEAD --no-edit
        docker compose up -d --build
        # Wait briefly for rebuild after revert
        local rw=0
        while [ "${rw}" -lt 90 ]; do
            if app_healthy; then
                break
            fi
            sleep 5
            rw=$((rw + 5))
        done
        mv "${fix_file}" "${FAILED_DIR}/${filename}"
    fi

    # 9. Clean up branch
    git branch -D "${branch}" 2>/dev/null || true

    # 10. Restore stashed changes
    if [ "${stashed}" = "true" ]; then
        git stash pop 2>/dev/null || log "WARN  Could not restore stash"
    fi

    log "Finished processing ticket #${ticket_id}"
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
log "Self-heal watcher starting"

# Pre-flight: check app is healthy (via docker exec since port 8000 not exposed to host)
if ! app_healthy; then
    log "WARN App is not healthy — skipping this run"
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
