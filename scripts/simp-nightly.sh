#!/usr/bin/env bash
# simp-nightly.sh — nightly E2E + smoke for the simplification parallel instance
#
# What: (1) smoke-checks the DEPLOYED parallel instance at
#       https://app.availai.net:8443 (health, ready, auth chain, static,
#       instance header); (2) runs the TS Playwright suite from this worktree
#       (npm ci + build, self-contained webServer on BRANCH code, same as prod
#       nightly); (3) W1.11: runs the spec §3 kernel walk (e2e/kernel-walk.spec.ts
#       via playwright.kernel.config.ts) against the DEPLOYED instance — gated on
#       the deployed-instance smoke having passed; a red walk fails the nightly.
# Cron: 0 4 * * * (host crontab; prod nightly at 02:30 is untouched)
# Logs: /var/log/avail_simp_nightly/nightly_YYYYMMDD.log (14 kept)
# Paging: W1.11b — on RED, reuses the prod nightly's seam
#       (python -m app.management.notify_nightly_status, see
#       scripts/nightly_tests.sh) with a SIMPLIFICATION-tagged status line so
#       the admin alert says which instance failed.

set -uo pipefail

WT="/root/availai-worktrees/simplification"
BASE="https://app.availai.net:8443"
LOG_DIR="/var/log/avail_simp_nightly"
PYTHON="/root/availai/.venv/bin/python"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/nightly_$(date +%Y%m%d).log"
STATUS_FILE="$LOG_DIR/LAST_STATUS"
FAIL=0

log() { echo "[$(date '+%H:%M:%S')] $1" >> "$LOG"; }
check() { # check <label> <expected-code> <url>
    local code
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "$3")
    if [ "$code" = "$2" ]; then log "OK   $1 ($code)"; else log "FAIL $1 (got $code, want $2)"; FAIL=1; fi
}

log "=== simp nightly start ==="

# The whole run happens from the worktree — a missing worktree is fatal.
if ! cd "$WT"; then
    log "FAIL cd worktree ($WT missing?)"
    echo "RED $(date -u +%FT%TZ)" > "$STATUS_FILE"
    log "=== RED ==="
    exit 1
fi

# 1) Deployed-instance smoke
check "health"      200 "$BASE/health"
check "ready"       200 "$BASE/health/ready"
check "root->auth"  302 "$BASE/"
check "auth login"  307 "$BASE/auth/login"
HDR=$(curl -sk -D- -o /dev/null --max-time 15 "$BASE/health" | grep -ci 'x-avail-instance: simplification')
if [ "$HDR" -ge 1 ]; then log "OK   instance header"; else log "FAIL instance header missing"; FAIL=1; fi
SMOKE_FAIL=$FAIL

# 2) Branch-code Playwright suite (self-contained webServer, like prod nightly)
# npm ci first so a package-lock bump on the branch is honored; then the Vite
# build — the webServer serves from the source tree, and without
# app/static/dist/ every page renders unstyled (found S1 — the visual spec
# fails on a bare-HTML login page). Prod nightly does the same build.
log "=== npm ci (honor lockfile) ==="
if ! npm ci --no-audit --no-fund >> "$LOG" 2>&1; then log "FAIL npm ci"; FAIL=1; fi
log "=== npm run build (assets for webServer) ==="
if ! npm run build >> "$LOG" 2>&1; then log "FAIL npm run build"; FAIL=1; fi
# Browser install is idempotent — run it up front instead of an
# install-and-rerun-the-whole-suite retry after a red run.
log "=== npx playwright install chromium (idempotent) ==="
if ! npx playwright install chromium >> "$LOG" 2>&1; then log "FAIL playwright install chromium"; FAIL=1; fi
log "=== E2E (TS Playwright, branch code) ==="
if ! npx playwright test >> "$LOG" 2>&1; then log "FAIL playwright suite"; FAIL=1; else log "OK   playwright suite"; fi

# 3) Kernel walk (spec §3/§10) against the DEPLOYED instance — the nightly's real
#    acceptance signal. Gated on the smoke: a down/unhealthy deployment would only
#    turn one red into two.
if [ "$SMOKE_FAIL" -ne 0 ]; then
    log "SKIP kernel walk — deployed-instance smoke failed"
else
    log "=== E2E (kernel walk vs deployed) ==="
    if ! npx playwright test --config=playwright.kernel.config.ts >> "$LOG" 2>&1; then
        log "FAIL kernel walk (deployed instance)"; FAIL=1
    else
        log "OK   kernel walk (deployed instance)"
    fi
fi

# 4) Verdict + paging + retention
if [ "$FAIL" -eq 0 ]; then
    echo "GREEN $(date -u +%FT%TZ)" > "$STATUS_FILE"
    log "=== GREEN ==="
else
    echo "RED $(date -u +%FT%TZ)" > "$STATUS_FILE"
    log "=== RED ==="
    # W1.11b: page the admin through the same seam the prod nightly uses
    # (scripts/nightly_tests.sh → app.management.notify_nightly_status, which
    # takes one status-line argument). The SIMPLIFICATION tag in the line tells
    # the admin which instance failed. Guarded: a failing pager never masks the
    # RED exit — the LAST_STATUS line above is the fallback record.
    STATUS_LINE="$(date +%F): RED (SIMPLIFICATION nightly — see $LOG)"
    if ! (cd "$WT" && PYTHONPATH="$WT" "$PYTHON" -m app.management.notify_nightly_status "$STATUS_LINE") >> "$LOG" 2>&1; then
        log "ALERT DELIVERY FAILED (notify_nightly_status non-zero — see above)"
    fi
fi
ls -t "$LOG_DIR"/nightly_*.log 2>/dev/null | tail -n +15 | xargs -r rm --
exit "$FAIL"
