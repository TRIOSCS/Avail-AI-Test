#!/usr/bin/env bash
# ultimate_ux_repair.sh — Full UX test & repair orchestrator (host-side).
#
# Detects UI/API issues via Playwright sweeps, creates trouble tickets,
# auto-generates patches, applies them, runs tests, commits, rebuilds,
# and verifies — all in a loop until the site is clean or MAX_ROUNDS hit.
#
# What calls it:
#   - Manual invocation: bash scripts/ultimate_ux_repair.sh [options]
#   - Cron/systemd for scheduled runs: --watch 1h
#   - Post-deploy hook: --post-deploy --max-rounds 3
#
# What it depends on:
#   - bash, git, curl, python3, jq, docker compose
#   - scripts/ux_repair_engine.py (in-container CLI, JSON output)
#   - scripts/apply_patches.py (stdlib-only patch applicator)
#   - App running at localhost:8000 inside Docker

set -uo pipefail
# Note: -e intentionally omitted so one failure doesn't abort the entire run

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_DIR="/root/availai"
QUEUE_DIR="${PROJ_DIR}/fix_queue"
APPLIED_DIR="${QUEUE_DIR}/applied"
FAILED_DIR="${QUEUE_DIR}/failed"
STATE_DIR="${SCRIPT_DIR}/ux-repair-state"
REPORT_DIR="${SCRIPT_DIR}/ux-repair-reports"
LOG_FILE="/var/log/avail/ultimate_ux_repair.log"
LOCK_FILE="/tmp/ultimate_ux_repair.lock"
HISTORY_FILE="${STATE_DIR}/history.jsonl"
ROLLBACK_MEMORY="${STATE_DIR}/failed_patches.json"
BASELINE_FILE="${STATE_DIR}/baseline.json"
APP_URL="http://localhost:8000"

# Defaults
MAX_ROUNDS=10
AREAS=""
SWEEP_ONLY=false
DRY_RUN=false
AGGRESSIVE=false
POST_DEPLOY=false
WATCH_INTERVAL=""
NO_NOTIFY=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# Runtime state
ROUND_RESULTS=()
RUN_START=""
SNAPSHOT_TAG=""
STASHED=false

# ---------------------------------------------------------------------------
# CLI Parsing
# ---------------------------------------------------------------------------
usage() {
    cat <<USAGE
${BOLD}ultimate_ux_repair.sh${NC} — Full UX test & repair loop

${BOLD}USAGE${NC}
    $0 [options]

${BOLD}OPTIONS${NC}
    --areas AREA1,AREA2     Comma-separated area names to sweep (default: all)
    --max-rounds N          Maximum repair rounds (default: 10)
    --sweep-only            Run sweep + report only — no patching or commits
    --dry-run               Show what would happen without making changes
    --aggressive            Lower confidence threshold, attempt riskier patches
    --post-deploy           Post-deploy mode: max 3 rounds, sweep-only if clean
    --watch INTERVAL        Repeat on interval (e.g. 5m, 1h, 30s)
    --no-notify             Skip Teams notification
    --help                  Show this help

${BOLD}EXAMPLES${NC}
    $0                                # Full repair loop, all areas
    $0 --areas login,search           # Only sweep login and search
    $0 --sweep-only                   # Report issues, don't fix
    $0 --post-deploy                  # Quick post-deploy check
    $0 --watch 1h --no-notify         # Repeat every hour, no Teams
USAGE
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --areas)      AREAS="$2"; shift 2 ;;
        --max-rounds) MAX_ROUNDS="$2"; shift 2 ;;
        --sweep-only) SWEEP_ONLY=true; shift ;;
        --dry-run)    DRY_RUN=true; shift ;;
        --aggressive) AGGRESSIVE=true; shift ;;
        --post-deploy)
            POST_DEPLOY=true
            MAX_ROUNDS=3
            shift ;;
        --watch)      WATCH_INTERVAL="$2"; shift 2 ;;
        --no-notify)  NO_NOTIFY=true; shift ;;
        --help|-h)    usage ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            usage ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "${ts}  $*" | tee -a "${LOG_FILE}"
}

clog() {
    # Colored log — print to terminal with color, log without
    local color="$1"; shift
    echo -e "${color}$*${NC}"
    log "$*"
}

health_ok() {
    docker compose -f "${PROJ_DIR}/docker-compose.yml" exec -T app \
        curl -sf http://localhost:8000/health >/dev/null 2>&1
}

engine() {
    # Call the in-container Python engine and capture JSON output (last line)
    local output
    output="$(docker compose -f "${PROJ_DIR}/docker-compose.yml" exec -T app \
        python3 scripts/ux_repair_engine.py "$@" 2>/dev/null)" || true
    # Engine prints JSON on the last line; earlier lines may be debug output
    echo "${output}" | tail -1
}

jq_val() {
    # Extract a value from JSON via python3 (no jq dependency required)
    local json="$1"
    local key="$2"
    echo "${json}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('${key}',''))" 2>/dev/null
}

jq_arr() {
    # Extract a JSON array as compact JSON string
    local json="$1"
    local key="$2"
    echo "${json}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('${key}',[])))" 2>/dev/null
}

jq_bool() {
    local json="$1"
    local key="$2"
    local val
    val="$(echo "${json}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('${key}',False)).lower())" 2>/dev/null)"
    [[ "${val}" == "true" ]]
}

jq_int() {
    local json="$1"
    local key="$2"
    echo "${json}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(int(d.get('${key}',0)))" 2>/dev/null
}

parse_interval() {
    # Parse interval string like 5m, 1h, 30s into seconds
    local input="$1"
    local num="${input%[smh]*}"
    local unit="${input##*[0-9]}"
    case "${unit}" in
        s) echo "${num}" ;;
        m) echo $((num * 60)) ;;
        h) echo $((num * 3600)) ;;
        *) echo "${num}" ;;
    esac
}

load_rollback_memory() {
    if [[ -f "${ROLLBACK_MEMORY}" ]]; then
        cat "${ROLLBACK_MEMORY}"
    else
        echo "[]"
    fi
}

save_rollback_memory() {
    local memory="$1"
    echo "${memory}" > "${ROLLBACK_MEMORY}"
}

is_in_rollback_memory() {
    local memory="$1"
    local patch_file="$2"
    echo "${memory}" | python3 -c "
import json, sys
mem = json.load(sys.stdin)
fname = '${patch_file}'
sys.exit(0 if fname in mem else 1)
" 2>/dev/null
}

add_to_rollback_memory() {
    local memory="$1"
    local patch_file="$2"
    echo "${memory}" | python3 -c "
import json, sys
mem = json.load(sys.stdin)
mem.append('${patch_file}')
print(json.dumps(list(set(mem))))
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Lock file guard
# ---------------------------------------------------------------------------
acquire_lock() {
    if [[ -f "${LOCK_FILE}" ]]; then
        local pid
        pid="$(cat "${LOCK_FILE}" 2>/dev/null)"
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            clog "${RED}" "Another instance is running (PID ${pid}). Exiting."
            exit 1
        fi
        # Stale lock — remove it
        clog "${YELLOW}" "Removing stale lock (PID ${pid} not running)"
        rm -f "${LOCK_FILE}"
    fi
    echo $$ > "${LOCK_FILE}"
    trap release_lock EXIT
}

release_lock() {
    rm -f "${LOCK_FILE}"
}

# ---------------------------------------------------------------------------
# Git stash guard
# ---------------------------------------------------------------------------
stash_guard() {
    cd "${PROJ_DIR}"
    if ! git diff --quiet HEAD 2>/dev/null || ! git diff --cached --quiet HEAD 2>/dev/null; then
        clog "${YELLOW}" "Stashing uncommitted changes..."
        git stash push -m "ultimate-ux-repair-$(date +%s)" --quiet
        STASHED=true
    fi
}

stash_restore() {
    if [[ "${STASHED}" == "true" ]]; then
        cd "${PROJ_DIR}"
        clog "${CYAN}" "Restoring stashed changes..."
        git stash pop --quiet 2>/dev/null || {
            clog "${YELLOW}" "WARN: Could not pop stash — may need manual restore (git stash list)"
        }
        STASHED=false
    fi
}

# ---------------------------------------------------------------------------
# Snapshot tag
# ---------------------------------------------------------------------------
create_snapshot() {
    cd "${PROJ_DIR}"
    SNAPSHOT_TAG="ux-repair-snapshot-$(date +%s)"
    git tag "${SNAPSHOT_TAG}" HEAD 2>/dev/null || true
    clog "${DIM}" "Snapshot tag: ${SNAPSHOT_TAG}"
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
preflight() {
    clog "${BOLD}" "========== Pre-flight Checks =========="

    # Check Docker is running
    if ! docker compose -f "${PROJ_DIR}/docker-compose.yml" ps --status running 2>/dev/null | grep -q app; then
        clog "${RED}" "FAIL: App container is not running"
        return 1
    fi
    clog "${GREEN}" "  [OK] App container running"

    # Health check
    if ! health_ok; then
        clog "${RED}" "FAIL: Health check failed at ${APP_URL}/health"
        return 1
    fi
    clog "${GREEN}" "  [OK] Health endpoint responding"

    # Smoke test via engine
    clog "${CYAN}" "  Running API smoke test..."
    local smoke_result
    smoke_result="$(engine smoke-test)"
    if jq_bool "${smoke_result}" "ok"; then
        clog "${GREEN}" "  [OK] Smoke test passed"
    else
        clog "${YELLOW}" "  [WARN] Smoke test had failures — continuing anyway"
    fi

    # Save smoke result for report
    SMOKE_RESULT="${smoke_result}"

    return 0
}

# ---------------------------------------------------------------------------
# Main repair logic
# ---------------------------------------------------------------------------
run_repair() {
    local run_ts
    run_ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    RUN_START="$(date +%s)"
    ROUND_RESULTS=()
    SMOKE_RESULT="{}"
    local total_fixed=0
    local total_found=0
    local total_failed=0
    local consecutive_clean=0

    clog "${BOLD}" ""
    clog "${BLUE}" "╔══════════════════════════════════════════════════════════╗"
    clog "${BLUE}" "║          Ultimate UX Repair — $(date '+%Y-%m-%d %H:%M')            ║"
    clog "${BLUE}" "╚══════════════════════════════════════════════════════════╝"
    echo ""

    # Setup
    mkdir -p "${QUEUE_DIR}" "${APPLIED_DIR}" "${FAILED_DIR}" "${STATE_DIR}" "${REPORT_DIR}"
    mkdir -p "$(dirname "${LOG_FILE}")"

    # Lock
    acquire_lock

    # Pre-flight
    if ! preflight; then
        clog "${RED}" "Pre-flight failed — aborting"
        return 1
    fi

    # Git guards
    if [[ "${DRY_RUN}" != "true" && "${SWEEP_ONLY}" != "true" ]]; then
        stash_guard
        create_snapshot
    fi

    # Load rollback memory
    local rollback_mem
    rollback_mem="$(load_rollback_memory)"

    # Build areas JSON for engine
    local areas_arg="null"
    if [[ -n "${AREAS}" ]]; then
        areas_arg="$(echo "${AREAS}" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read().strip().split(',')))")"
    fi

    # Confidence threshold
    local min_confidence=0.6
    if [[ "${AGGRESSIVE}" == "true" ]]; then
        min_confidence=0.3
    fi

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    for ((round=1; round<=MAX_ROUNDS; round++)); do
        local round_start
        round_start="$(date +%s)"
        local round_fixed=0
        local round_found=0
        local round_failed=0
        local round_skipped=0

        clog "${BOLD}" ""
        clog "${CYAN}" "━━━ Round ${round}/${MAX_ROUNDS} ━━━"

        # -------------------------------------------------------------------
        # 1. Sweep
        # -------------------------------------------------------------------
        clog "${BLUE}" "  Sweeping..."
        local sweep_result
        if [[ -f "${BASELINE_FILE}" ]]; then
            sweep_result="$(engine sweep "${areas_arg}" "${BASELINE_FILE}")"
        else
            sweep_result="$(engine sweep "${areas_arg}")"
        fi

        local sweep_ok
        sweep_ok="$(jq_bool "${sweep_result}" "ok" && echo true || echo false)"
        if [[ "${sweep_ok}" != "true" ]]; then
            clog "${RED}" "  Sweep failed: $(jq_val "${sweep_result}" "error")"
            ROUND_RESULTS+=("{\"round\":${round},\"status\":\"sweep_failed\",\"found\":0,\"fixed\":0,\"failed\":0,\"skipped\":0,\"duration_s\":$(($(date +%s)-round_start))}")
            continue
        fi

        local new_issues
        new_issues="$(jq_int "${sweep_result}" "new_issues")"
        local total_issues
        total_issues="$(jq_int "${sweep_result}" "total_issues")"
        local areas_tested
        areas_tested="$(jq_int "${sweep_result}" "areas_tested")"
        round_found="${new_issues}"
        total_found=$((total_found + new_issues))

        clog "${CYAN}" "  Areas tested: ${areas_tested} | Total issues: ${total_issues} | New issues: ${new_issues}"

        # -------------------------------------------------------------------
        # 2. Clean sweep detection
        # -------------------------------------------------------------------
        if [[ "${new_issues}" -eq 0 ]]; then
            consecutive_clean=$((consecutive_clean + 1))
            clog "${GREEN}" "  Clean sweep! (${consecutive_clean} consecutive)"

            if [[ "${consecutive_clean}" -ge 2 ]]; then
                clog "${GREEN}" "  Two consecutive clean sweeps — stopping."
                ROUND_RESULTS+=("{\"round\":${round},\"status\":\"clean\",\"found\":0,\"fixed\":0,\"failed\":0,\"skipped\":0,\"duration_s\":$(($(date +%s)-round_start))}")
                break
            fi

            ROUND_RESULTS+=("{\"round\":${round},\"status\":\"clean\",\"found\":0,\"fixed\":0,\"failed\":0,\"skipped\":0,\"duration_s\":$(($(date +%s)-round_start))}")
            continue
        fi

        consecutive_clean=0

        # -------------------------------------------------------------------
        # Sweep-only / dry-run: stop here
        # -------------------------------------------------------------------
        if [[ "${SWEEP_ONLY}" == "true" || "${DRY_RUN}" == "true" ]]; then
            clog "${YELLOW}" "  [sweep-only/dry-run] Skipping repair"
            ROUND_RESULTS+=("{\"round\":${round},\"status\":\"sweep_only\",\"found\":${new_issues},\"fixed\":0,\"failed\":0,\"skipped\":0,\"duration_s\":$(($(date +%s)-round_start))}")
            break
        fi

        # -------------------------------------------------------------------
        # 3. Create tickets
        # -------------------------------------------------------------------
        clog "${BLUE}" "  Creating tickets for ${new_issues} issue(s)..."
        local issues_json
        issues_json="$(jq_arr "${sweep_result}" "issues")"
        local ticket_result
        ticket_result="$(engine create-tickets "${issues_json}")"

        local created
        created="$(jq_int "${ticket_result}" "created")"
        local ticket_ids_json
        ticket_ids_json="$(jq_arr "${ticket_result}" "ticket_ids")"
        clog "${CYAN}" "  Created ${created} ticket(s)"

        if [[ "${created}" -eq 0 ]]; then
            clog "${YELLOW}" "  No new tickets created (all duplicates?) — skipping round"
            ROUND_RESULTS+=("{\"round\":${round},\"status\":\"no_new_tickets\",\"found\":${new_issues},\"fixed\":0,\"failed\":0,\"skipped\":0,\"duration_s\":$(($(date +%s)-round_start))}")
            continue
        fi

        # -------------------------------------------------------------------
        # 4. Poll for patches
        # -------------------------------------------------------------------
        clog "${BLUE}" "  Polling for patches (up to 120s)..."
        local poll_result
        poll_result="$(engine poll-queue "${ticket_ids_json}" 120)"
        local found_patches
        found_patches="$(jq_arr "${poll_result}" "found")"
        local missing_patches
        missing_patches="$(jq_arr "${poll_result}" "missing")"

        local found_count
        found_count="$(echo "${found_patches}" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null)"
        local missing_count
        missing_count="$(echo "${missing_patches}" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null)"
        clog "${CYAN}" "  Patches ready: ${found_count} | Missing: ${missing_count}"

        if [[ "${found_count}" -eq 0 ]]; then
            clog "${YELLOW}" "  No patches generated — skipping round"
            ROUND_RESULTS+=("{\"round\":${round},\"status\":\"no_patches\",\"found\":${new_issues},\"fixed\":0,\"failed\":0,\"skipped\":0,\"duration_s\":$(($(date +%s)-round_start))}")
            continue
        fi

        # -------------------------------------------------------------------
        # 5. Score and sort patches by confidence
        # -------------------------------------------------------------------
        clog "${BLUE}" "  Scoring patch confidence..."
        local patch_entries
        patch_entries="$(echo "${found_patches}" | python3 -c "
import json, sys
patches = json.load(sys.stdin)
for p in patches:
    print(str(p.get('ticket_id','')) + '|' + p.get('file',''))
" 2>/dev/null)"

        # Build scored list: ticket_id:confidence:file
        local SCORED_LIST=()
        while IFS='|' read -r tid fpath; do
            [[ -z "${tid}" ]] && continue

            # Check rollback memory — skip previously failed patches
            local fname
            fname="$(basename "${fpath}")"
            if is_in_rollback_memory "${rollback_mem}" "${fname}"; then
                clog "${YELLOW}" "    Skipping ${fname} (previously failed)"
                round_skipped=$((round_skipped + 1))
                continue
            fi

            # Score via engine
            local conf_result
            conf_result="$(engine fix-confidence "${fpath}")"
            local score
            score="$(jq_val "${conf_result}" "score")"
            [[ -z "${score}" ]] && score="0.0"

            SCORED_LIST+=("${tid}:${score}:${fpath}")
            clog "${DIM}" "    Ticket #${tid}: confidence=${score}"
        done <<< "${patch_entries}"

        # Sort by confidence descending
        local SORTED_IDS=()
        while IFS= read -r line; do
            SORTED_IDS+=("${line}")
        done < <(printf '%s\n' "${SCORED_LIST[@]}" | sort -t: -k2 -rn)

        # -------------------------------------------------------------------
        # 6. Apply patches, test, commit, rebuild
        # -------------------------------------------------------------------
        for entry in "${SORTED_IDS[@]}"; do
            local tid score fpath
            tid="$(echo "${entry}" | cut -d: -f1)"
            score="$(echo "${entry}" | cut -d: -f2)"
            fpath="$(echo "${entry}" | cut -d: -f3)"
            local fname
            fname="$(basename "${fpath}")"

            # Check confidence threshold
            local passes_threshold
            passes_threshold="$(python3 -c "print('yes' if float('${score}') >= ${min_confidence} else 'no')" 2>/dev/null)"
            if [[ "${passes_threshold}" != "yes" ]]; then
                clog "${YELLOW}" "    Skipping ticket #${tid} (confidence ${score} < ${min_confidence})"
                round_skipped=$((round_skipped + 1))
                continue
            fi

            clog "${BLUE}" "    Applying ticket #${tid} (confidence: ${score})..."

            cd "${PROJ_DIR}"
            local branch="fix/ux-repair-${tid}-r${round}"

            # Create branch
            git checkout main 2>/dev/null || git checkout master 2>/dev/null
            git branch -D "${branch}" 2>/dev/null || true
            git checkout -b "${branch}" 2>/dev/null

            # Apply patch
            # The fpath is a container path — map to host path
            local host_fpath="${fpath}"
            # If path starts with /app/, map to PROJ_DIR
            host_fpath="$(echo "${fpath}" | sed "s|^/app/|${PROJ_DIR}/|")"
            if [[ ! -f "${host_fpath}" ]]; then
                # Try the queue directory
                host_fpath="${QUEUE_DIR}/${fname}"
            fi

            if ! python3 scripts/apply_patches.py "${host_fpath}"; then
                clog "${RED}" "    Patch apply failed for ticket #${tid}"
                git checkout main 2>/dev/null || git checkout master 2>/dev/null
                git branch -D "${branch}" 2>/dev/null || true
                rollback_mem="$(add_to_rollback_memory "${rollback_mem}" "${fname}")"
                round_failed=$((round_failed + 1))
                total_failed=$((total_failed + 1))
                continue
            fi

            # ---------------------------------------------------------------
            # Pytest gate
            # ---------------------------------------------------------------
            clog "${BLUE}" "    Running pytest gate..."
            local test_exit=0
            docker compose -f "${PROJ_DIR}/docker-compose.yml" exec -T \
                -e TESTING=1 -e PYTHONPATH=/app app \
                python3 -m pytest tests/ -x -q --tb=line 2>&1 | tail -5 || test_exit=$?

            if [[ "${test_exit}" -ne 0 ]]; then
                clog "${RED}" "    Pytest failed for ticket #${tid} — reverting"
                git checkout -- . 2>/dev/null
                git checkout main 2>/dev/null || git checkout master 2>/dev/null
                git branch -D "${branch}" 2>/dev/null || true
                rollback_mem="$(add_to_rollback_memory "${rollback_mem}" "${fname}")"
                round_failed=$((round_failed + 1))
                total_failed=$((total_failed + 1))
                continue
            fi
            clog "${GREEN}" "    Pytest passed"

            # ---------------------------------------------------------------
            # Git commit + merge
            # ---------------------------------------------------------------
            git add -A
            git commit -m "$(cat <<EOF
fix: UX repair ticket #${tid} (round ${round}, confidence ${score})

Automated patch applied by ultimate_ux_repair.sh

Co-Authored-By: AvailAI UX Repair <noreply@availai.local>
EOF
            )" 2>/dev/null || true

            git checkout main 2>/dev/null || git checkout master 2>/dev/null
            if ! git merge --no-ff "${branch}" -m "Merge ${branch}: UX repair ticket #${tid}" 2>/dev/null; then
                clog "${RED}" "    Merge conflict for ticket #${tid} — reverting"
                git merge --abort 2>/dev/null || true
                git branch -D "${branch}" 2>/dev/null || true
                rollback_mem="$(add_to_rollback_memory "${rollback_mem}" "${fname}")"
                round_failed=$((round_failed + 1))
                total_failed=$((total_failed + 1))
                continue
            fi

            git branch -D "${branch}" 2>/dev/null || true

            # ---------------------------------------------------------------
            # Rebuild + health check
            # ---------------------------------------------------------------
            clog "${BLUE}" "    Rebuilding containers..."
            docker compose -f "${PROJ_DIR}/docker-compose.yml" up -d --build 2>&1 | tail -3

            local waited=0
            local healthy=false
            while [[ "${waited}" -lt 90 ]]; do
                if health_ok; then
                    healthy=true
                    break
                fi
                sleep 3
                waited=$((waited + 3))
            done

            if [[ "${healthy}" != "true" ]]; then
                clog "${RED}" "    Health check failed after rebuild — reverting merge"
                cd "${PROJ_DIR}"
                git revert HEAD --no-edit 2>/dev/null
                docker compose -f "${PROJ_DIR}/docker-compose.yml" up -d --build 2>&1 | tail -2
                # Wait for recovery
                local rw=0
                while [[ "${rw}" -lt 60 ]]; do
                    health_ok && break
                    sleep 3
                    rw=$((rw + 3))
                done
                rollback_mem="$(add_to_rollback_memory "${rollback_mem}" "${fname}")"
                round_failed=$((round_failed + 1))
                total_failed=$((total_failed + 1))
                continue
            fi

            clog "${GREEN}" "    Healthy after ${waited}s"

            # Move patch to applied
            if [[ -f "${host_fpath}" ]]; then
                mv "${host_fpath}" "${APPLIED_DIR}/${fname}" 2>/dev/null || true
            fi

            round_fixed=$((round_fixed + 1))
            total_fixed=$((total_fixed + 1))

            clog "${GREEN}" "    Ticket #${tid} applied successfully"
        done

        # -------------------------------------------------------------------
        # 7. Targeted re-sweep of affected areas
        # -------------------------------------------------------------------
        if [[ "${round_fixed}" -gt 0 && -n "${AREAS}" ]]; then
            clog "${BLUE}" "  Running targeted re-sweep for verification..."
            local verify_result
            verify_result="$(engine sweep "${areas_arg}")"
            local verify_issues
            verify_issues="$(jq_int "${verify_result}" "total_issues")"
            clog "${CYAN}" "  Verification sweep: ${verify_issues} issue(s) remaining"
        fi

        # Save round result
        local round_duration=$(($(date +%s) - round_start))
        local round_status="completed"
        [[ "${round_fixed}" -eq 0 && "${round_failed}" -gt 0 ]] && round_status="all_failed"
        [[ "${round_fixed}" -gt 0 && "${round_failed}" -gt 0 ]] && round_status="partial"
        [[ "${round_fixed}" -gt 0 && "${round_failed}" -eq 0 ]] && round_status="all_fixed"

        ROUND_RESULTS+=("{\"round\":${round},\"status\":\"${round_status}\",\"found\":${round_found},\"fixed\":${round_fixed},\"failed\":${round_failed},\"skipped\":${round_skipped},\"duration_s\":${round_duration}}")
        total_fixed=$((total_fixed))  # already updated in loop
    done

    # Save rollback memory
    save_rollback_memory "${rollback_mem}"

    # -----------------------------------------------------------------------
    # Ticket summary
    # -----------------------------------------------------------------------
    local ticket_summary
    ticket_summary="$(engine ticket-summary)"

    # -----------------------------------------------------------------------
    # Final summary (terminal)
    # -----------------------------------------------------------------------
    local run_duration=$(( $(date +%s) - RUN_START ))
    local run_mins=$(( run_duration / 60 ))
    local run_secs=$(( run_duration % 60 ))

    echo ""
    clog "${BOLD}" "╔══════════════════════════════════════════════════════════╗"
    clog "${BOLD}" "║                    REPAIR SUMMARY                       ║"
    clog "${BOLD}" "╚══════════════════════════════════════════════════════════╝"
    echo ""
    clog "${CYAN}" "  Duration:     ${run_mins}m ${run_secs}s"
    clog "${CYAN}" "  Rounds:       ${#ROUND_RESULTS[@]}"
    clog "${GREEN}" "  Issues found: ${total_found}"
    clog "${GREEN}" "  Fixed:        ${total_fixed}"
    [[ "${total_failed}" -gt 0 ]] && clog "${RED}" "  Failed:       ${total_failed}"
    echo ""

    # Per-round breakdown
    clog "${BOLD}" "  Round | Status       | Found | Fixed | Failed | Duration"
    clog "${DIM}" "  ------+--------------+-------+-------+--------+---------"
    for rr in "${ROUND_RESULTS[@]}"; do
        local r_round r_status r_found r_fixed r_failed r_dur
        r_round="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['round'])")"
        r_status="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")"
        r_found="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['found'])")"
        r_fixed="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['fixed'])")"
        r_failed="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['failed'])")"
        r_dur="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['duration_s'])")"
        printf "  %5s | %-12s | %5s | %5s | %6s | %5ss\n" "${r_round}" "${r_status}" "${r_found}" "${r_fixed}" "${r_failed}" "${r_dur}"
    done
    echo ""

    # Ticket cost
    local ai_cost
    ai_cost="$(jq_val "${ticket_summary}" "total_ai_cost_usd")"
    [[ -n "${ai_cost}" ]] && clog "${CYAN}" "  AI cost: \$${ai_cost}"

    # Snapshot tag
    [[ -n "${SNAPSHOT_TAG}" ]] && clog "${DIM}" "  Rollback tag: ${SNAPSHOT_TAG}"
    echo ""

    # -----------------------------------------------------------------------
    # History trending (JSONL)
    # -----------------------------------------------------------------------
    local history_entry
    history_entry="$(python3 -c "
import json
entry = {
    'timestamp': '${run_ts}',
    'duration_s': ${run_duration},
    'rounds': ${#ROUND_RESULTS[@]},
    'found': ${total_found},
    'fixed': ${total_fixed},
    'failed': ${total_failed},
    'ai_cost_usd': '${ai_cost:-0}',
    'sweep_only': $(${SWEEP_ONLY} && echo 'True' || echo 'False'),
    'dry_run': $(${DRY_RUN} && echo 'True' || echo 'False'),
}
print(json.dumps(entry))
" 2>/dev/null)"
    echo "${history_entry}" >> "${HISTORY_FILE}"

    # Compare with previous run
    local prev_found=""
    if [[ -f "${HISTORY_FILE}" ]]; then
        local line_count
        line_count="$(wc -l < "${HISTORY_FILE}")"
        if [[ "${line_count}" -ge 2 ]]; then
            prev_found="$(python3 -c "
import json
lines = open('${HISTORY_FILE}').readlines()
if len(lines) >= 2:
    prev = json.loads(lines[-2])
    curr = json.loads(lines[-1])
    delta = curr['found'] - prev['found']
    if delta < 0:
        print(f'Trending better: {abs(delta)} fewer issues than last run')
    elif delta > 0:
        print(f'Trending worse: {delta} more issues than last run')
    else:
        print('Same issue count as last run')
" 2>/dev/null)"
            [[ -n "${prev_found}" ]] && clog "${CYAN}" "  ${prev_found}"
        fi
    fi

    # -----------------------------------------------------------------------
    # HTML report
    # -----------------------------------------------------------------------
    local report_file="${REPORT_DIR}/ux-repair-$(date +%Y%m%d-%H%M%S).html"
    generate_html_report "${report_file}" "${total_found}" "${total_fixed}" "${total_failed}" \
        "${run_duration}" "${ai_cost:-0}"
    clog "${CYAN}" "  Report: ${report_file}"

    # -----------------------------------------------------------------------
    # Teams notification
    # -----------------------------------------------------------------------
    if [[ "${NO_NOTIFY}" != "true" && "${DRY_RUN}" != "true" ]]; then
        local emoji="✅"
        [[ "${total_failed}" -gt 0 ]] && emoji="⚠️"
        [[ "${total_fixed}" -eq 0 && "${total_found}" -gt 0 ]] && emoji="❌"
        local teams_msg="${emoji} UX Repair: ${total_found} found, ${total_fixed} fixed, ${total_failed} failed (${run_mins}m ${run_secs}s)"
        engine teams-notify "${teams_msg}" >/dev/null 2>&1 || true
        clog "${DIM}" "  Teams notified"
    fi

    # Restore stash
    stash_restore

    echo ""
    clog "${GREEN}" "Done."
}

# ---------------------------------------------------------------------------
# HTML report generator
# ---------------------------------------------------------------------------
generate_html_report() {
    local file="$1"
    local found="$2"
    local fixed="$3"
    local failed="$4"
    local duration="$5"
    local cost="$6"
    local mins=$(( duration / 60 ))
    local secs=$(( duration % 60 ))

    # Build round rows
    local round_rows=""
    for rr in "${ROUND_RESULTS[@]}"; do
        local r_round r_status r_found r_fixed r_failed r_dur
        r_round="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['round'])" 2>/dev/null)"
        r_status="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" 2>/dev/null)"
        r_found="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['found'])" 2>/dev/null)"
        r_fixed="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['fixed'])" 2>/dev/null)"
        r_failed="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['failed'])" 2>/dev/null)"
        r_dur="$(echo "${rr}" | python3 -c "import json,sys; print(json.load(sys.stdin)['duration_s'])" 2>/dev/null)"

        local status_color="#4caf50"
        [[ "${r_status}" == "all_failed" ]] && status_color="#f44336"
        [[ "${r_status}" == "partial" ]] && status_color="#ff9800"
        [[ "${r_status}" == "sweep_only" ]] && status_color="#2196f3"
        [[ "${r_status}" == "clean" ]] && status_color="#4caf50"

        round_rows+="<tr>
            <td>${r_round}</td>
            <td><span style=\"color:${status_color};font-weight:bold\">${r_status}</span></td>
            <td>${r_found}</td>
            <td>${r_fixed}</td>
            <td>${r_failed}</td>
            <td>${r_dur}s</td>
        </tr>"
    done

    # Build smoke test rows
    local smoke_rows=""
    smoke_rows="$(echo "${SMOKE_RESULT}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for r in data.get('results', []):
        color = '#4caf50' if r.get('ok') else '#f44336'
        status = r.get('status', '?')
        ms = r.get('ms', '?')
        ep = r.get('endpoint', '?')
        print(f'<tr><td>{ep}</td><td style=\"color:{color}\">{status}</td><td>{ms}ms</td></tr>')
except:
    print('<tr><td colspan=\"3\">No smoke test data</td></tr>')
" 2>/dev/null)"

    cat > "${file}" <<HTMLEOF
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UX Repair Report — $(date '+%Y-%m-%d %H:%M')</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0; padding: 24px;
  }
  h1 { color: #00d4ff; margin-bottom: 8px; }
  .subtitle { color: #888; margin-bottom: 24px; font-size: 14px; }
  .cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px; margin-bottom: 32px;
  }
  .card {
    background: #16213e; border-radius: 12px; padding: 20px;
    text-align: center; border: 1px solid #0f3460;
  }
  .card .value { font-size: 36px; font-weight: bold; }
  .card .label { font-size: 12px; color: #888; margin-top: 4px; text-transform: uppercase; }
  .card.green .value { color: #4caf50; }
  .card.red .value { color: #f44336; }
  .card.blue .value { color: #2196f3; }
  .card.yellow .value { color: #ff9800; }
  .card.cyan .value { color: #00d4ff; }
  table {
    width: 100%; border-collapse: collapse; margin-bottom: 32px;
    background: #16213e; border-radius: 8px; overflow: hidden;
  }
  th { background: #0f3460; padding: 12px; text-align: left; font-size: 13px; color: #00d4ff; }
  td { padding: 10px 12px; border-bottom: 1px solid #1a1a2e; font-size: 13px; }
  tr:hover { background: #1a2744; }
  h2 { color: #00d4ff; margin: 24px 0 12px; font-size: 18px; }
  .snapshot { color: #666; font-size: 12px; margin-top: 16px; }
</style>
</head>
<body>
<h1>UX Repair Report</h1>
<p class="subtitle">Generated $(date -u '+%Y-%m-%d %H:%M:%S UTC') | Snapshot: ${SNAPSHOT_TAG:-none}</p>

<div class="cards">
  <div class="card blue"><div class="value">${found}</div><div class="label">Issues Found</div></div>
  <div class="card green"><div class="value">${fixed}</div><div class="label">Fixed</div></div>
  <div class="card red"><div class="value">${failed}</div><div class="label">Failed</div></div>
  <div class="card cyan"><div class="value">${#ROUND_RESULTS[@]}</div><div class="label">Rounds</div></div>
  <div class="card yellow"><div class="value">${mins}m${secs}s</div><div class="label">Duration</div></div>
  <div class="card cyan"><div class="value">\$${cost}</div><div class="label">AI Cost</div></div>
</div>

<h2>Round Breakdown</h2>
<table>
  <thead><tr><th>Round</th><th>Status</th><th>Found</th><th>Fixed</th><th>Failed</th><th>Duration</th></tr></thead>
  <tbody>${round_rows}</tbody>
</table>

<h2>Smoke Test Results</h2>
<table>
  <thead><tr><th>Endpoint</th><th>Status</th><th>Latency</th></tr></thead>
  <tbody>${smoke_rows}</tbody>
</table>

<p class="snapshot">Rollback tag: <code>${SNAPSHOT_TAG:-none}</code> | Areas: ${AREAS:-all}</p>
</body>
</html>
HTMLEOF
    log "HTML report written to ${file}"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if [[ -n "${WATCH_INTERVAL}" ]]; then
    # Watch mode — repeat on interval
    interval_secs="$(parse_interval "${WATCH_INTERVAL}")"
    clog "${BOLD}" "Watch mode: repeating every ${WATCH_INTERVAL} (${interval_secs}s)"
    while true; do
        run_repair || true
        # Release lock between runs so state is clean
        release_lock
        clog "${DIM}" "Sleeping ${WATCH_INTERVAL} until next run..."
        sleep "${interval_secs}"
    done
else
    run_repair
fi
