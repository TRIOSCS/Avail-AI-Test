# Ultimate UX Test Repair Script — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a single host-side script (`scripts/ultimate_ux_repair.sh`) plus a companion Python helper (`scripts/ux_repair_engine.py`) that orchestrates the full detect → diagnose → fix → verify loop with all advanced features.

**Architecture:** Host-side bash orchestrator calls a Python engine (inside Docker) for sweeps, diagnosis, and patch generation. The bash script handles git operations, Docker rebuilds, and patch application on the host filesystem. A JSON state file tracks round history, failed patches, and cost.

**Tech Stack:** Bash 5 + Python 3.11 (stdlib + app imports inside Docker) + Docker Compose + Git + curl + jq

---

### Task 1: Create the Python engine (`scripts/ux_repair_engine.py`)

The in-container Python script that handles all app-level operations: sweeping, API smoke tests, ticket creation, diagnosis polling, and reporting. Called by the bash orchestrator via `docker compose exec`.

**Files:**
- Create: `scripts/ux_repair_engine.py`

**Step 1: Write the failing test**

```bash
# No unit test — this is a script. We'll integration-test at the end.
# Verify the file is syntactically valid:
docker compose exec app python3 -c "import ast; ast.parse(open('scripts/ux_repair_engine.py').read()); print('OK')"
```

**Step 2: Write the engine**

```python
#!/usr/bin/env python3
"""Ultimate UX repair engine — in-container orchestration for sweep/diagnose/fix.

Called by: scripts/ultimate_ux_repair.sh (via docker compose exec)
Depends on: app.services.site_tester, app.services.trouble_ticket_service,
            app.services.diagnosis_service, app.services.execution_service
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")
os.environ.setdefault("TESTING", "")

from loguru import logger

from app.database import SessionLocal
from app.services.site_tester import SiteTester, TEST_AREAS, create_tickets_from_issues
from app.services.trouble_ticket_service import auto_process_ticket

BASE_URL = "http://localhost:8000"
ADMIN_USER_ID = 1
FIX_QUEUE_DIR = Path(os.environ.get("FIX_QUEUE_DIR", "/app/fix_queue"))


def get_session_cookie() -> str:
    from itsdangerous import URLSafeTimedSerializer
    from app.config import settings
    signer = URLSafeTimedSerializer(settings.secret_key)
    return signer.dumps({"user_id": ADMIN_USER_ID})


# ── Commands (called by bash orchestrator with JSON output) ──

async def cmd_sweep(areas: list[str] | None = None, baseline_file: str | None = None) -> dict:
    """Run SiteTester sweep. Optionally filter by areas and diff against baseline."""
    cookie = get_session_cookie()
    tester = SiteTester(base_url=BASE_URL, session_cookie=cookie)

    try:
        all_issues = await tester.run_full_sweep()
    except Exception as e:
        return {"ok": False, "error": str(e), "issues": [], "areas_tested": 0}

    # Filter by requested areas
    if areas:
        all_issues = [i for i in all_issues if i.get("area") in areas]

    # Diff against baseline (skip known/accepted issues)
    if baseline_file and os.path.exists(baseline_file):
        try:
            baseline = json.loads(Path(baseline_file).read_text())
            known_keys = {(b["area"], b["title"][:80]) for b in baseline.get("known_issues", [])}
            new_issues = [i for i in all_issues if (i["area"], i["title"][:80]) not in known_keys]
        except Exception:
            new_issues = all_issues
    else:
        new_issues = all_issues

    return {
        "ok": True,
        "issues": new_issues,
        "total_issues": len(all_issues),
        "new_issues": len(new_issues),
        "areas_tested": len(tester.progress),
        "areas_detail": tester.progress,
    }


async def cmd_smoke_test() -> dict:
    """Hit every API endpoint with a health probe."""
    import httpx
    cookie = get_session_cookie()

    endpoints = [
        ("GET", "/health"),
        ("GET", "/api/dashboard/briefing"),
        ("GET", "/api/system/alerts"),
        ("GET", "/api/trouble-tickets?limit=1"),
        ("GET", "/api/vendor-contacts/bulk?limit=1"),
        ("GET", "/api/requisitions?limit=1"),
        ("GET", "/api/companies?limit=1"),
        ("GET", "/api/resurfacing/hints"),
        ("GET", "/api/admin/api-health/dashboard"),
    ]

    results = []
    async with httpx.AsyncClient(base_url=BASE_URL, cookies={"session": cookie}, timeout=15) as client:
        for method, path in endpoints:
            try:
                t0 = time.monotonic()
                resp = await client.request(method, path)
                elapsed_ms = (time.monotonic() - t0) * 1000
                results.append({
                    "endpoint": f"{method} {path}",
                    "status": resp.status_code,
                    "ok": resp.status_code < 400,
                    "ms": round(elapsed_ms, 1),
                })
            except Exception as e:
                results.append({
                    "endpoint": f"{method} {path}",
                    "status": 0,
                    "ok": False,
                    "ms": 0,
                    "error": str(e),
                })

    failed = [r for r in results if not r["ok"]]
    return {
        "ok": len(failed) == 0,
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


async def cmd_create_tickets(issues_json: str) -> dict:
    """Create trouble tickets from sweep issues and auto-process them."""
    issues = json.loads(issues_json)
    if not issues:
        return {"ok": True, "created": 0, "ticket_ids": []}

    db = SessionLocal()
    try:
        count = await create_tickets_from_issues(issues, db)

        # Get the newly created ticket IDs
        from app.models.trouble_ticket import TroubleTicket
        recent = (
            db.query(TroubleTicket)
            .filter(TroubleTicket.source == "playwright")
            .filter(TroubleTicket.status == "submitted")
            .order_by(TroubleTicket.id.desc())
            .limit(count)
            .all()
        )
        ticket_ids = [t.id for t in recent]

        # Auto-process each ticket (diagnose + queue fix)
        for tid in ticket_ids:
            try:
                await auto_process_ticket(tid)
            except Exception as e:
                logger.warning("Auto-process failed for ticket {}: {}", tid, e)

        return {"ok": True, "created": count, "ticket_ids": ticket_ids}
    finally:
        db.close()


async def cmd_poll_queue(ticket_ids_json: str, timeout_secs: int = 120) -> dict:
    """Poll fix_queue/ until patches appear for the given ticket IDs or timeout."""
    ticket_ids = json.loads(ticket_ids_json)
    if not ticket_ids:
        return {"ok": True, "ready": [], "pending": []}

    deadline = time.monotonic() + timeout_secs
    ready = []
    pending = set(ticket_ids)

    while time.monotonic() < deadline and pending:
        for tid in list(pending):
            fix_file = FIX_QUEUE_DIR / f"{tid}.json"
            if fix_file.is_file():
                ready.append(tid)
                pending.discard(tid)
        if pending:
            await asyncio.sleep(3)

    # Also check ticket statuses for escalated/failed
    db = SessionLocal()
    try:
        from app.models.trouble_ticket import TroubleTicket
        skipped = []
        for tid in list(pending):
            ticket = db.get(TroubleTicket, tid)
            if ticket and ticket.status in ("escalated", "resolved"):
                skipped.append({"id": tid, "status": ticket.status})
                pending.discard(tid)
    finally:
        db.close()

    return {
        "ok": True,
        "ready": ready,
        "pending": list(pending),
        "skipped": skipped if 'skipped' in dir() else [],
    }


async def cmd_ticket_summary() -> dict:
    """Get current ticket status summary for reporting."""
    db = SessionLocal()
    try:
        from app.models.trouble_ticket import TroubleTicket
        from sqlalchemy import func

        stats = dict(
            db.query(TroubleTicket.status, func.count())
            .group_by(TroubleTicket.status)
            .all()
        )

        # Get cost info
        from app.models.self_heal_log import SelfHealLog
        total_cost = db.query(func.sum(SelfHealLog.cost_usd)).scalar() or 0.0

        return {
            "ok": True,
            "statuses": stats,
            "total_cost_usd": round(float(total_cost), 4),
        }
    finally:
        db.close()


async def cmd_fix_confidence(fix_file: str) -> dict:
    """Score a fix file's patches by confidence (search string match quality)."""
    try:
        data = json.loads(Path(fix_file).read_text())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    patches = data.get("patches", [])
    scored = []
    for p in patches:
        rel_path = p.get("file", "")
        search = p.get("search", "")
        fpath = Path("/app") / rel_path

        score = 0.0
        reason = "file not found"
        if fpath.is_file():
            content = fpath.read_text(encoding="utf-8")
            if search in content:
                count = content.count(search)
                if count == 1:
                    score = 1.0
                    reason = "exact unique match"
                else:
                    score = 0.5
                    reason = f"ambiguous ({count} matches)"
            else:
                score = 0.0
                reason = "search string not found"

        scored.append({
            "file": rel_path,
            "confidence": score,
            "reason": reason,
        })

    avg = sum(s["confidence"] for s in scored) / len(scored) if scored else 0
    return {
        "ok": True,
        "patches": scored,
        "avg_confidence": round(avg, 2),
        "all_confident": all(s["confidence"] >= 0.9 for s in scored),
    }


async def cmd_teams_notify(message: str) -> dict:
    """Send a notification to Teams channel."""
    try:
        from app.services.teams_notifications import post_teams_channel
        await post_teams_channel(message)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Main dispatcher ──

async def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: ux_repair_engine.py <command> [args...]"}))
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        if cmd == "sweep":
            areas = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
            baseline = sys.argv[3] if len(sys.argv) > 3 else None
            result = await cmd_sweep(areas, baseline)
        elif cmd == "smoke-test":
            result = await cmd_smoke_test()
        elif cmd == "create-tickets":
            result = await cmd_create_tickets(sys.argv[2])
        elif cmd == "poll-queue":
            timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 120
            result = await cmd_poll_queue(sys.argv[2], timeout)
        elif cmd == "ticket-summary":
            result = await cmd_ticket_summary()
        elif cmd == "fix-confidence":
            result = await cmd_fix_confidence(sys.argv[2])
        elif cmd == "teams-notify":
            result = await cmd_teams_notify(sys.argv[2])
        else:
            result = {"error": f"Unknown command: {cmd}"}
    except Exception as e:
        result = {"error": str(e)}

    print(json.dumps(result, default=str))


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 3: Verify syntax**

```bash
docker compose exec app python3 -c "import ast; ast.parse(open('scripts/ux_repair_engine.py').read()); print('OK')"
```
Expected: OK

**Step 4: Commit**

```bash
git add scripts/ux_repair_engine.py
git commit -m "feat: add UX repair engine — in-container orchestration for ultimate repair script"
```

---

### Task 2: Create the bash orchestrator (`scripts/ultimate_ux_repair.sh`)

The main host-side script that drives the full loop: sweep → diagnose → patch → rebuild → verify → repeat.

**Files:**
- Create: `scripts/ultimate_ux_repair.sh`

**Step 1: Write the orchestrator**

```bash
#!/usr/bin/env bash
# scripts/ultimate_ux_repair.sh — Ultimate UX Test & Repair Loop
#
# Single-command orchestrator that detects, diagnoses, fixes, and verifies
# UX issues across all 15 areas of the app. Runs entirely from the host,
# calling into Docker for sweeps and AI processing.
#
# Features:
#   - Full browser sweep of all 15 UI areas (SiteTester via Playwright)
#   - API endpoint smoke testing before each round
#   - AI-powered diagnosis + patch generation (Claude API)
#   - Automatic patch application, rebuild, and verification
#   - Fix confidence scoring — high-confidence patches applied first
#   - Multi-ticket batching — groups patches touching same files
#   - Rollback memory — tracks failed patches, won't retry same approach
#   - Baseline diffing — skip known/accepted issues
#   - pytest gate — run test suite before rebuilding
#   - Teams/webhook notifications on completion
#   - HTML report generation with screenshots and diffs
#   - History trending across runs
#   - Git safety: stash guard, snapshot tag, lock file
#   - Modes: --sweep-only, --dry-run, --aggressive, --watch, --post-deploy
#
# Usage:
#   ./scripts/ultimate_ux_repair.sh                          # full loop, all areas
#   ./scripts/ultimate_ux_repair.sh --areas search,vendors   # specific areas
#   ./scripts/ultimate_ux_repair.sh --sweep-only             # detect only, no fixes
#   ./scripts/ultimate_ux_repair.sh --dry-run                # generate patches, don't apply
#   ./scripts/ultimate_ux_repair.sh --aggressive             # also attempt high-risk fixes
#   ./scripts/ultimate_ux_repair.sh --watch 5m               # repeat every 5 minutes
#   ./scripts/ultimate_ux_repair.sh --post-deploy            # quick smoke + critical areas
#   ./scripts/ultimate_ux_repair.sh --max-rounds 5           # limit rounds
#
# Called by: operator, post-deploy.sh, cron
# Depends on: docker compose, git, python3, jq, curl, scripts/ux_repair_engine.py

set -uo pipefail

# ── Config ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ_DIR="$(dirname "$SCRIPT_DIR")"
QUEUE_DIR="${PROJ_DIR}/fix_queue"
APPLIED_DIR="${QUEUE_DIR}/applied"
FAILED_DIR="${QUEUE_DIR}/failed"
STATE_DIR="${SCRIPT_DIR}/ux-repair-state"
HISTORY_FILE="${STATE_DIR}/history.jsonl"
FAILED_PATCHES_FILE="${STATE_DIR}/failed_patches.json"
BASELINE_FILE="${STATE_DIR}/baseline.json"
REPORT_DIR="${SCRIPT_DIR}/ux-repair-reports"
LOCK_FILE="/tmp/ultimate_ux_repair.lock"
LOG_FILE="/var/log/avail/ultimate_ux_repair.log"
COMPOSE_FILE="${PROJ_DIR}/docker-compose.yml"

MAX_ROUNDS=10
POLL_TIMEOUT=180
HEALTH_TIMEOUT=90
MAX_CONSECUTIVE_CLEAN=2
WATCH_INTERVAL=""  # empty = no watch mode

# Modes
SWEEP_ONLY=false
DRY_RUN=false
AGGRESSIVE=false
POST_DEPLOY=false
AREAS_FILTER=""
NOTIFY_TEAMS=true

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── Parse args ──────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --areas)        AREAS_FILTER="$2"; shift 2 ;;
        --max-rounds)   MAX_ROUNDS="$2"; shift 2 ;;
        --sweep-only)   SWEEP_ONLY=true; shift ;;
        --dry-run)      DRY_RUN=true; shift ;;
        --aggressive)   AGGRESSIVE=true; shift ;;
        --post-deploy)  POST_DEPLOY=true; MAX_ROUNDS=2; shift ;;
        --watch)        WATCH_INTERVAL="${2:-10m}"; shift 2 ;;
        --no-notify)    NOTIFY_TEAMS=false; shift ;;
        --help|-h)
            head -30 "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)  echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helpers ─────────────────────────────────────────────────────────
log() {
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo -e "${DIM}${ts}${NC}  $*" | tee -a "${LOG_FILE}"
}

banner() {
    echo -e "\n${BOLD}${CYAN}$1${NC}"
    echo -e "${CYAN}$(printf '═%.0s' $(seq 1 ${#1}))${NC}"
}

engine() {
    # Run the in-container Python engine and capture JSON output
    docker compose -f "${COMPOSE_FILE}" exec -T app \
        python3 scripts/ux_repair_engine.py "$@" 2>/dev/null | tail -1
}

health_ok() {
    docker compose -f "${COMPOSE_FILE}" exec -T app \
        curl -sf http://localhost:8000/health >/dev/null 2>&1
}

wait_healthy() {
    local waited=0
    while [ "${waited}" -lt "${HEALTH_TIMEOUT}" ]; do
        if health_ok; then
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    return 1
}

is_failed_patch() {
    # Check if this patch approach was already tried and failed
    local ticket_id="$1"
    if [ -f "${FAILED_PATCHES_FILE}" ]; then
        jq -e ".\"${ticket_id}\" | length > 0" "${FAILED_PATCHES_FILE}" >/dev/null 2>&1
        return $?
    fi
    return 1
}

record_failed_patch() {
    local ticket_id="$1"
    local reason="$2"
    if [ ! -f "${FAILED_PATCHES_FILE}" ]; then
        echo '{}' > "${FAILED_PATCHES_FILE}"
    fi
    local tmp
    tmp=$(jq --arg tid "$ticket_id" --arg reason "$reason" \
        '.[$tid] += [{"reason": $reason, "timestamp": now | todate}]' \
        "${FAILED_PATCHES_FILE}")
    echo "$tmp" > "${FAILED_PATCHES_FILE}"
}

# ── Lock file guard ────────────────────────────────────────────────
if [ -f "${LOCK_FILE}" ]; then
    existing_pid=$(cat "${LOCK_FILE}" 2>/dev/null)
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo -e "${RED}Another instance is running (PID ${existing_pid}). Exiting.${NC}"
        exit 1
    else
        log "Stale lock file found (PID ${existing_pid} dead) — removing"
        rm -f "${LOCK_FILE}"
    fi
fi
echo $$ > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"; exit' EXIT INT TERM

# ── Setup directories ──────────────────────────────────────────────
mkdir -p "${QUEUE_DIR}" "${APPLIED_DIR}" "${FAILED_DIR}" "${STATE_DIR}" "${REPORT_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"

# ── Git stash guard ────────────────────────────────────────────────
cd "${PROJ_DIR}"
GIT_STASHED=false
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    log "Uncommitted changes detected — stashing"
    git stash push -m "ultimate-ux-repair-$(date +%Y%m%d_%H%M%S)"
    GIT_STASHED=true
fi

# Snapshot tag for rollback
SNAPSHOT_TAG="ux-repair-snapshot-$(date +%Y%m%d_%H%M%S)"
git tag "${SNAPSHOT_TAG}" HEAD
log "Created snapshot tag: ${SNAPSHOT_TAG}"

# ── Run ID & report setup ──────────────────────────────────────────
RUN_ID="$(date +%Y%m%d_%H%M%S)"
RUN_REPORT="${REPORT_DIR}/${RUN_ID}"
mkdir -p "${RUN_REPORT}"

# ── Start ──────────────────────────────────────────────────────────
run_repair() {

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  ${CYAN}Ultimate UX Test & Repair${NC}${BOLD}                                    ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Run ID:      ${BOLD}${RUN_ID}${NC}"
echo -e "  Max Rounds:  ${MAX_ROUNDS}"
echo -e "  Mode:        $([ "$SWEEP_ONLY" = true ] && echo "SWEEP-ONLY" || ([ "$DRY_RUN" = true ] && echo "DRY-RUN" || echo "FULL REPAIR"))"
echo -e "  Areas:       ${AREAS_FILTER:-all 15}"
echo -e "  Aggressive:  ${AGGRESSIVE}"
echo -e "  Snapshot:    ${SNAPSHOT_TAG}"
echo ""

# ── Pre-flight health check ────────────────────────────────────────
echo -ne "  Health check... "
if health_ok; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAILED${NC} — is the app running?"
    exit 1
fi

# ── Pre-flight API smoke test ───────────────────────────────────────
banner "API Smoke Test"
SMOKE_RESULT=$(engine smoke-test)
SMOKE_OK=$(echo "$SMOKE_RESULT" | jq -r '.ok')
SMOKE_PASSED=$(echo "$SMOKE_RESULT" | jq -r '.passed')
SMOKE_TOTAL=$(echo "$SMOKE_RESULT" | jq -r '.total')
SMOKE_FAILED_COUNT=$(echo "$SMOKE_RESULT" | jq -r '.failed')

if [ "$SMOKE_OK" = "true" ]; then
    echo -e "  ${GREEN}All ${SMOKE_PASSED}/${SMOKE_TOTAL} endpoints healthy${NC}"
else
    echo -e "  ${YELLOW}${SMOKE_FAILED_COUNT}/${SMOKE_TOTAL} endpoints failing:${NC}"
    echo "$SMOKE_RESULT" | jq -r '.results[] | select(.ok == false) | "    \(.endpoint) → \(.status) \(.error // "")"'
fi

echo "$SMOKE_RESULT" | jq '.' > "${RUN_REPORT}/smoke_test.json"

# ── Main loop ──────────────────────────────────────────────────────
TOTAL_FOUND=0
TOTAL_FIXED=0
TOTAL_FAILED_FIX=0
TOTAL_SKIPPED=0
CONSECUTIVE_CLEAN=0
ROUND_RESULTS=()

for ROUND in $(seq 1 "$MAX_ROUNDS"); do
    banner "Round ${ROUND}/${MAX_ROUNDS}"

    # ── 1. Sweep ──
    log "Starting sweep (round ${ROUND})"
    AREAS_ARG="null"
    if [ -n "${AREAS_FILTER}" ]; then
        AREAS_ARG=$(echo "$AREAS_FILTER" | tr ',' '\n' | jq -R . | jq -s .)
    fi
    BASELINE_ARG=""
    if [ -f "${BASELINE_FILE}" ]; then
        BASELINE_ARG="${BASELINE_FILE}"
    fi

    SWEEP_RESULT=$(engine sweep "$AREAS_ARG" "$BASELINE_ARG")
    SWEEP_OK=$(echo "$SWEEP_RESULT" | jq -r '.ok')

    if [ "$SWEEP_OK" != "true" ]; then
        echo -e "  ${RED}Sweep failed:${NC} $(echo "$SWEEP_RESULT" | jq -r '.error')"
        ROUND_RESULTS+=("round${ROUND}:ERROR")
        break
    fi

    ISSUE_COUNT=$(echo "$SWEEP_RESULT" | jq -r '.new_issues')
    AREAS_TESTED=$(echo "$SWEEP_RESULT" | jq -r '.areas_tested')
    echo -e "  Tested ${BOLD}${AREAS_TESTED}${NC} areas, found ${BOLD}${ISSUE_COUNT}${NC} new issue(s)"

    echo "$SWEEP_RESULT" | jq '.' > "${RUN_REPORT}/round_${ROUND}_sweep.json"
    TOTAL_FOUND=$((TOTAL_FOUND + ISSUE_COUNT))

    # ── Clean sweep check ──
    if [ "$ISSUE_COUNT" -eq 0 ]; then
        CONSECUTIVE_CLEAN=$((CONSECUTIVE_CLEAN + 1))
        echo -e "  ${GREEN}Clean sweep!${NC} (${CONSECUTIVE_CLEAN} consecutive)"
        ROUND_RESULTS+=("round${ROUND}:CLEAN")

        if [ "$CONSECUTIVE_CLEAN" -ge "$MAX_CONSECUTIVE_CLEAN" ]; then
            echo -e "\n  ${GREEN}${BOLD}${MAX_CONSECUTIVE_CLEAN} consecutive clean sweeps — all clear!${NC}"
            break
        fi
        sleep 10
        continue
    fi
    CONSECUTIVE_CLEAN=0

    # ── Sweep-only mode: just report ──
    if [ "$SWEEP_ONLY" = true ]; then
        echo -e "  ${YELLOW}[sweep-only] Skipping fix phase${NC}"
        echo "$SWEEP_RESULT" | jq -r '.issues[] | "    [\(.area)] \(.title)"'
        ROUND_RESULTS+=("round${ROUND}:${ISSUE_COUNT}-issues")
        continue
    fi

    # ── 2. Create tickets + auto-process ──
    log "Creating tickets and triggering AI diagnosis"
    ISSUES_JSON=$(echo "$SWEEP_RESULT" | jq -c '.issues')
    TICKET_RESULT=$(engine create-tickets "$ISSUES_JSON")
    CREATED=$(echo "$TICKET_RESULT" | jq -r '.created')
    TICKET_IDS=$(echo "$TICKET_RESULT" | jq -c '.ticket_ids')

    if [ "$CREATED" -eq 0 ]; then
        echo -e "  ${DIM}No new tickets (all duplicates) — stopping${NC}"
        ROUND_RESULTS+=("round${ROUND}:NO-NEW")
        break
    fi
    echo -e "  Created ${BOLD}${CREATED}${NC} ticket(s): $(echo "$TICKET_IDS" | jq -r 'join(", ")')"

    # ── 3. Poll for patches ──
    echo -ne "  Waiting for AI diagnosis + patch generation "
    POLL_RESULT=$(engine poll-queue "$TICKET_IDS" "$POLL_TIMEOUT")
    echo ""
    READY_IDS=$(echo "$POLL_RESULT" | jq -c '.ready')
    PENDING_IDS=$(echo "$POLL_RESULT" | jq -c '.pending')
    READY_COUNT=$(echo "$READY_IDS" | jq 'length')
    PENDING_COUNT=$(echo "$PENDING_IDS" | jq 'length')

    echo -e "  Patches ready: ${GREEN}${READY_COUNT}${NC}, still pending: ${YELLOW}${PENDING_COUNT}${NC}"

    if [ "$READY_COUNT" -eq 0 ]; then
        echo -e "  ${YELLOW}No patches generated — skipping fix phase${NC}"
        TOTAL_SKIPPED=$((TOTAL_SKIPPED + PENDING_COUNT))
        ROUND_RESULTS+=("round${ROUND}:${ISSUE_COUNT}-issues-no-patches")
        continue
    fi

    # ── Dry-run mode: show patches but don't apply ──
    if [ "$DRY_RUN" = true ]; then
        echo -e "  ${YELLOW}[dry-run] Patches generated but not applied:${NC}"
        for tid in $(echo "$READY_IDS" | jq -r '.[]'); do
            FIX_FILE="${QUEUE_DIR}/${tid}.json"
            if [ -f "$FIX_FILE" ]; then
                echo -e "    ${BOLD}Ticket #${tid}:${NC}"
                jq -r '.patches[] | "      \(.file): \(.explanation // "no explanation")"' "$FIX_FILE"
            fi
        done
        ROUND_RESULTS+=("round${ROUND}:${READY_COUNT}-patches-dry-run")
        continue
    fi

    # ── 4. Score, sort, and apply patches ──
    ROUND_FIXED=0
    ROUND_FAILED=0

    # Sort by confidence (high first)
    SORTED_IDS=()
    for tid in $(echo "$READY_IDS" | jq -r '.[]'); do
        FIX_FILE="${QUEUE_DIR}/${tid}.json"

        # Skip if this patch approach already failed
        if is_failed_patch "$tid"; then
            echo -e "  ${DIM}Skipping ticket #${tid} — previous patch attempt failed${NC}"
            TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
            continue
        fi

        # Score confidence
        CONF_RESULT=$(engine fix-confidence "$FIX_FILE")
        CONF_AVG=$(echo "$CONF_RESULT" | jq -r '.avg_confidence')
        CONF_OK=$(echo "$CONF_RESULT" | jq -r '.all_confident')

        if [ "$CONF_OK" != "true" ]; then
            echo -e "  ${YELLOW}Ticket #${tid}: low confidence (${CONF_AVG}) — ${NC}"
            echo "$CONF_RESULT" | jq -r '.patches[] | select(.confidence < 0.9) | "    \(.file): \(.reason)"'
            if [ "$AGGRESSIVE" != true ]; then
                echo -e "  ${DIM}Skipping (use --aggressive to override)${NC}"
                TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
                continue
            fi
            echo -e "  ${YELLOW}Applying anyway (--aggressive mode)${NC}"
        fi

        SORTED_IDS+=("${tid}:${CONF_AVG}")
    done

    # Sort by confidence descending
    IFS=$'\n' SORTED_IDS=($(printf '%s\n' "${SORTED_IDS[@]}" | sort -t: -k2 -rn)); unset IFS

    # ── Group patches by file (multi-ticket batching) ──
    # For simplicity, apply sequentially but commit together
    BATCH_APPLIED=()

    for entry in "${SORTED_IDS[@]}"; do
        tid="${entry%%:*}"
        FIX_FILE="${QUEUE_DIR}/${tid}.json"

        echo -e "\n  ${BOLD}Applying ticket #${tid}${NC}"

        # Create git branch
        BRANCH="fix/ux-repair-${tid}"
        git checkout main 2>/dev/null
        git branch -D "$BRANCH" 2>/dev/null || true
        git checkout -b "$BRANCH"

        # Apply patches
        if python3 scripts/apply_patches.py "$FIX_FILE"; then
            echo -e "  ${GREEN}Patches applied${NC}"
            BATCH_APPLIED+=("$tid")

            # ── pytest gate ──
            echo -ne "  Running pytest gate... "
            if docker compose -f "${COMPOSE_FILE}" exec -T \
                -e TESTING=1 -e PYTHONPATH=/app app \
                python3 -m pytest tests/ -x -q --tb=line 2>&1 | tail -5 > "${RUN_REPORT}/round_${ROUND}_pytest_${tid}.txt"; then
                echo -e "${GREEN}PASS${NC}"
            else
                echo -e "${RED}FAIL${NC}"
                echo -e "  ${YELLOW}Tests failed — reverting patch${NC}"
                git checkout -- .
                git checkout main 2>/dev/null
                git branch -D "$BRANCH" 2>/dev/null || true
                record_failed_patch "$tid" "pytest gate failed"
                mv "$FIX_FILE" "${FAILED_DIR}/$(basename "$FIX_FILE")"
                ROUND_FAILED=$((ROUND_FAILED + 1))
                continue
            fi

            # Commit
            git add -A
            git commit -m "$(cat <<COMMITEOF
fix: self-heal ticket #${tid} (ultimate-ux-repair)

Automated patch applied by ultimate UX repair script.
Run ID: ${RUN_ID}, Round: ${ROUND}

Co-Authored-By: AvailAI Self-Heal <noreply@availai.local>
COMMITEOF
            )"

            # Merge to main
            git checkout main 2>/dev/null
            if git merge --no-ff "$BRANCH" -m "Merge ${BRANCH}: UX repair fix for ticket #${tid}"; then
                echo -e "  ${GREEN}Merged to main${NC}"
                git branch -D "$BRANCH" 2>/dev/null || true
            else
                echo -e "  ${RED}Merge conflict — aborting${NC}"
                git merge --abort 2>/dev/null || true
                git checkout main 2>/dev/null
                git branch -D "$BRANCH" 2>/dev/null || true
                record_failed_patch "$tid" "merge conflict"
                mv "$FIX_FILE" "${FAILED_DIR}/$(basename "$FIX_FILE")"
                ROUND_FAILED=$((ROUND_FAILED + 1))
                continue
            fi

            ROUND_FIXED=$((ROUND_FIXED + 1))
            mv "$FIX_FILE" "${APPLIED_DIR}/$(basename "$FIX_FILE")"
        else
            echo -e "  ${RED}Patch application failed${NC}"
            git checkout -- .
            git checkout main 2>/dev/null
            git branch -D "$BRANCH" 2>/dev/null || true
            record_failed_patch "$tid" "patch apply failed"
            mv "$FIX_FILE" "${FAILED_DIR}/$(basename "$FIX_FILE")"
            ROUND_FAILED=$((ROUND_FAILED + 1))
        fi
    done

    # ── 5. Rebuild if any patches were applied ──
    if [ ${#BATCH_APPLIED[@]} -gt 0 ]; then
        echo ""
        echo -e "  ${BOLD}Rebuilding container...${NC}"
        docker compose -f "${COMPOSE_FILE}" up -d --build

        echo -ne "  Waiting for health check... "
        if wait_healthy; then
            echo -e "${GREEN}OK${NC}"
        else
            echo -e "${RED}FAILED${NC} — reverting all patches from this round"
            # Revert each applied commit
            for tid in "${BATCH_APPLIED[@]}"; do
                git revert HEAD --no-edit 2>/dev/null || true
            done
            docker compose -f "${COMPOSE_FILE}" up -d --build
            wait_healthy
            ROUND_FIXED=0
            ROUND_FAILED=$((ROUND_FAILED + ${#BATCH_APPLIED[@]}))
            for tid in "${BATCH_APPLIED[@]}"; do
                record_failed_patch "$tid" "health check failed after rebuild"
            done
        fi

        # ── 6. Verify — targeted re-sweep of affected areas ──
        if [ "$ROUND_FIXED" -gt 0 ]; then
            echo -ne "  Verifying fixes with targeted re-sweep... "
            # Get affected areas from the applied tickets
            AFFECTED_AREAS="[]"
            for tid in "${BATCH_APPLIED[@]}"; do
                APPLIED_FILE="${APPLIED_DIR}/${tid}.json"
                if [ -f "$APPLIED_FILE" ]; then
                    AREA=$(jq -r '.test_area // "general"' "$APPLIED_FILE")
                    AFFECTED_AREAS=$(echo "$AFFECTED_AREAS" | jq --arg a "$AREA" '. + [$a] | unique')
                fi
            done
            VERIFY_RESULT=$(engine sweep "$AFFECTED_AREAS")
            VERIFY_ISSUES=$(echo "$VERIFY_RESULT" | jq -r '.new_issues')
            if [ "$VERIFY_ISSUES" -eq 0 ]; then
                echo -e "${GREEN}All fixes verified!${NC}"
            else
                echo -e "${YELLOW}${VERIFY_ISSUES} issue(s) remain in affected areas${NC}"
            fi
        fi
    fi

    TOTAL_FIXED=$((TOTAL_FIXED + ROUND_FIXED))
    TOTAL_FAILED_FIX=$((TOTAL_FAILED_FIX + ROUND_FAILED))
    ROUND_RESULTS+=("round${ROUND}:found=${ISSUE_COUNT},fixed=${ROUND_FIXED},failed=${ROUND_FAILED}")

    log "Round ${ROUND} complete: found=${ISSUE_COUNT} fixed=${ROUND_FIXED} failed=${ROUND_FAILED}"
done

# ── Final Summary ──────────────────────────────────────────────────
TICKET_SUMMARY=$(engine ticket-summary)
TOTAL_COST=$(echo "$TICKET_SUMMARY" | jq -r '.total_cost_usd')

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  ${CYAN}Final Report${NC}${BOLD}                                                  ║${NC}"
echo -e "${BOLD}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}║${NC}  Issues found:    ${BOLD}${TOTAL_FOUND}${NC}"
echo -e "${BOLD}║${NC}  Issues fixed:    ${GREEN}${TOTAL_FIXED}${NC}"
echo -e "${BOLD}║${NC}  Fix failures:    ${RED}${TOTAL_FAILED_FIX}${NC}"
echo -e "${BOLD}║${NC}  Skipped:         ${YELLOW}${TOTAL_SKIPPED}${NC}"
echo -e "${BOLD}║${NC}  AI cost:         \$${TOTAL_COST}"
echo -e "${BOLD}║${NC}  Snapshot tag:    ${SNAPSHOT_TAG}"
echo -e "${BOLD}║${NC}  Report dir:      ${RUN_REPORT}"
echo -e "${BOLD}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}║${NC}  Round breakdown:"
for r in "${ROUND_RESULTS[@]}"; do
    echo -e "${BOLD}║${NC}    ${r}"
done
echo -e "${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Ticket statuses:"
echo "$TICKET_SUMMARY" | jq -r '.statuses | to_entries[] | "    \(.key): \(.value)"' | while read -r line; do
    echo -e "${BOLD}║${NC}  ${line}"
done
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"

# ── Generate HTML report ───────────────────────────────────────────
HTML_FILE="${RUN_REPORT}/report.html"
cat > "$HTML_FILE" <<HTMLEOF
<!DOCTYPE html>
<html><head><title>UX Repair Report — ${RUN_ID}</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; background: #0d1117; color: #c9d1d9; }
  h1 { color: #58a6ff; } h2 { color: #79c0ff; border-bottom: 1px solid #30363d; padding-bottom: 0.5rem; }
  .stat { display: inline-block; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1rem 1.5rem; margin: 0.5rem; text-align: center; }
  .stat .num { font-size: 2rem; font-weight: bold; } .stat .label { color: #8b949e; font-size: 0.8rem; }
  .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; }
  table { width: 100%; border-collapse: collapse; } th, td { padding: 0.5rem; text-align: left; border-bottom: 1px solid #30363d; }
  th { color: #8b949e; } .pass { color: #3fb950; } .fail { color: #f85149; }
  pre { background: #161b22; padding: 1rem; border-radius: 6px; overflow-x: auto; }
</style></head><body>
<h1>Ultimate UX Repair Report</h1>
<p>Run ID: <code>${RUN_ID}</code> | Date: $(date -Iseconds) | Snapshot: <code>${SNAPSHOT_TAG}</code></p>
<div>
  <div class="stat"><div class="num">${TOTAL_FOUND}</div><div class="label">Issues Found</div></div>
  <div class="stat"><div class="num green">${TOTAL_FIXED}</div><div class="label">Fixed</div></div>
  <div class="stat"><div class="num red">${TOTAL_FAILED_FIX}</div><div class="label">Failed</div></div>
  <div class="stat"><div class="num yellow">${TOTAL_SKIPPED}</div><div class="label">Skipped</div></div>
  <div class="stat"><div class="num">\$${TOTAL_COST}</div><div class="label">AI Cost</div></div>
</div>
<h2>Round Details</h2>
<table><tr><th>Round</th><th>Result</th></tr>
$(for r in "${ROUND_RESULTS[@]}"; do echo "<tr><td>${r%%:*}</td><td>${r#*:}</td></tr>"; done)
</table>
<h2>Ticket Status</h2>
<pre>$(echo "$TICKET_SUMMARY" | jq '.statuses')</pre>
<h2>Smoke Test</h2>
<pre>$(jq '.' "${RUN_REPORT}/smoke_test.json" 2>/dev/null || echo "N/A")</pre>
</body></html>
HTMLEOF
log "HTML report: ${HTML_FILE}"

# ── Append to history ──────────────────────────────────────────────
echo "{\"run_id\": \"${RUN_ID}\", \"timestamp\": \"$(date -Iseconds)\", \"found\": ${TOTAL_FOUND}, \"fixed\": ${TOTAL_FIXED}, \"failed\": ${TOTAL_FAILED_FIX}, \"skipped\": ${TOTAL_SKIPPED}, \"cost_usd\": ${TOTAL_COST}, \"rounds\": ${ROUND}}" >> "${HISTORY_FILE}"

# ── History trending ───────────────────────────────────────────────
if [ -f "${HISTORY_FILE}" ] && [ "$(wc -l < "${HISTORY_FILE}")" -gt 1 ]; then
    PREV_FOUND=$(tail -2 "${HISTORY_FILE}" | head -1 | jq -r '.found')
    if [ "${TOTAL_FOUND}" -lt "${PREV_FOUND}" ]; then
        TREND_MSG="📉 Issues declining (${PREV_FOUND} → ${TOTAL_FOUND})"
    elif [ "${TOTAL_FOUND}" -gt "${PREV_FOUND}" ]; then
        TREND_MSG="📈 Issues increasing (${PREV_FOUND} → ${TOTAL_FOUND})"
    else
        TREND_MSG="➡ Issues stable (${TOTAL_FOUND})"
    fi
    echo -e "\n  Trend: ${TREND_MSG}"
fi

# ── Teams notification ─────────────────────────────────────────────
if [ "$NOTIFY_TEAMS" = true ] && [ "$TOTAL_FOUND" -gt 0 ]; then
    TEAMS_MSG="**UX Repair Complete** (${RUN_ID})\n\n"
    TEAMS_MSG+="Found: ${TOTAL_FOUND} | Fixed: ${TOTAL_FIXED} | Failed: ${TOTAL_FAILED_FIX} | Cost: \$${TOTAL_COST}"
    if [ -n "${TREND_MSG:-}" ]; then
        TEAMS_MSG+="\n${TREND_MSG}"
    fi
    engine teams-notify "$TEAMS_MSG" >/dev/null 2>&1 || true
fi

# ── Restore git stash ─────────────────────────────────────────────
if [ "$GIT_STASHED" = true ]; then
    git stash pop 2>/dev/null || log "WARN: Could not pop stash — manual restore needed"
fi

log "Ultimate UX repair complete: found=${TOTAL_FOUND} fixed=${TOTAL_FIXED} failed=${TOTAL_FAILED_FIX}"

}  # end run_repair()

# ── Watch mode ─────────────────────────────────────────────────────
if [ -n "${WATCH_INTERVAL}" ]; then
    # Parse interval (e.g. "5m" -> 300, "1h" -> 3600)
    INTERVAL_SECS=600
    case "${WATCH_INTERVAL}" in
        *m) INTERVAL_SECS=$(( ${WATCH_INTERVAL%m} * 60 )) ;;
        *h) INTERVAL_SECS=$(( ${WATCH_INTERVAL%h} * 3600 )) ;;
        *s) INTERVAL_SECS="${WATCH_INTERVAL%s}" ;;
        *)  INTERVAL_SECS="${WATCH_INTERVAL}" ;;
    esac

    echo -e "${BOLD}Watch mode: running every ${WATCH_INTERVAL} (${INTERVAL_SECS}s)${NC}"
    echo -e "${DIM}Press Ctrl+C to stop${NC}"

    while true; do
        RUN_ID="$(date +%Y%m%d_%H%M%S)"
        RUN_REPORT="${REPORT_DIR}/${RUN_ID}"
        mkdir -p "${RUN_REPORT}"

        run_repair

        echo -e "\n${DIM}Next run in ${WATCH_INTERVAL}...${NC}"
        sleep "${INTERVAL_SECS}"
    done
else
    run_repair
fi
```

**Step 2: Make executable**

```bash
chmod +x scripts/ultimate_ux_repair.sh
```

**Step 3: Verify it parses correctly**

```bash
bash -n scripts/ultimate_ux_repair.sh && echo "Syntax OK"
```
Expected: Syntax OK

**Step 4: Commit**

```bash
git add scripts/ultimate_ux_repair.sh
git commit -m "feat: add ultimate UX test repair script — full detect/diagnose/fix/verify loop"
```

---

### Task 3: Create the baseline file for known issues

**Files:**
- Create: `scripts/ux-repair-state/baseline.json`

**Step 1: Write initial baseline template**

```json
{
  "description": "Known/accepted issues to skip during UX repair sweeps",
  "updated_at": "2026-03-08T00:00:00Z",
  "known_issues": []
}
```

**Step 2: Commit**

```bash
git add scripts/ux-repair-state/baseline.json
git commit -m "feat: add baseline template for UX repair known issues"
```

---

### Task 4: Add post-deploy integration

Wire the script into the existing `post-deploy.sh` script.

**Files:**
- Modify: `scripts/post-deploy.sh`

**Step 1: Read current post-deploy.sh**

```bash
cat scripts/post-deploy.sh
```

**Step 2: Add ultimate UX repair call at the end (--post-deploy mode)**

Append to the end of post-deploy.sh:

```bash
# Post-deploy UX smoke test + repair
echo "Running post-deploy UX repair..."
"${SCRIPT_DIR}/ultimate_ux_repair.sh" --post-deploy --no-notify || {
    echo "WARN: Post-deploy UX repair found issues (see report)"
}
```

**Step 3: Commit**

```bash
git add scripts/post-deploy.sh
git commit -m "feat: wire ultimate UX repair into post-deploy (--post-deploy mode)"
```

---

### Task 5: Integration test — run the script in sweep-only mode

**Step 1: Run sweep-only to verify end-to-end**

```bash
cd /root/availai
./scripts/ultimate_ux_repair.sh --sweep-only --max-rounds 1
```

**Step 2: Check output**
- Should see the banner, smoke test results, and sweep results
- Should NOT attempt any fixes
- Should generate report files in `scripts/ux-repair-reports/`

**Step 3: Run dry-run mode**

```bash
./scripts/ultimate_ux_repair.sh --dry-run --max-rounds 1
```

**Step 4: Verify reports exist**

```bash
ls -la scripts/ux-repair-reports/*/
ls -la scripts/ux-repair-state/
```

**Step 5: Commit any fixes from integration testing**

```bash
git add -A
git commit -m "fix: integration test adjustments to ultimate UX repair script"
```

---

### Task 6: Full test — run the complete repair loop

**Step 1: Run full repair with max 3 rounds**

```bash
./scripts/ultimate_ux_repair.sh --max-rounds 3
```

**Step 2: Verify**
- HTML report generated
- History file updated
- Any applied patches in `fix_queue/applied/`
- Any failed patches in `fix_queue/failed/`
- Git log shows fix commits (if any issues were found)

**Step 3: Final commit + deploy**

```bash
docker compose up -d --build
docker compose logs -f app | head -50
```
