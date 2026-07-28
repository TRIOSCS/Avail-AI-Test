#!/usr/bin/env bash
# Nightly full test suite + coverage + TS Playwright E2E against a CLEAN origin/main
# snapshot, with admin alerting on any non-PASS outcome.
# Called by: root crontab (30 2 * * * — 02:30 UTC) — logs to /var/log/avail/nightly_tests/
#
# Runs in a dedicated detached worktree (NOT the live checkout): the live
# /root/availai is routinely mid-branch during active development, so testing it
# at 02:30 tested an arbitrary snapshot — the 2026-06/07 red streak was partly
# stale-checkout drift. pytest runs with cwd INSIDE the worktree so template
# paths resolve against the snapshot, using the main checkout's .venv (deps
# track the host install either way).
#
# Suites:
#   1. pytest tests/ (unit/integration; coverage-gated at 80%). pytest.ini ignores
#      tests/e2e/ — and that python live-app suite must STAY excluded here: its
#      conftest targets the LIVE Docker app (docker-net IP / app.availai.net) with
#      a minted admin session cookie, so scheduling it would drive real workflows
#      against production data — a live-data hazard, not a coverage win. Re-point
#      it at a local TESTING server (local-probe-harness pattern) before ever
#      automating it.
#   2. npx playwright test (TS specs in e2e/) — SELF-CONTAINED: playwright.config.ts
#      boots its own uvicorn webServer (TESTING=1, sqlite, no redis) on port 8787,
#      so it never touches the live app or DB. Needs node deps + a fresh Vite
#      build in the worktree (npm ci + npm run build below; browsers come from the
#      host's /root/.cache/ms-playwright install).
#
# Alerting: any non-PASS STATUS asks the running app container to notify admins
# (one Teams Adaptive Card + in-app rows for active admins) via
# `docker compose exec -T app python -m app.management.notify_nightly_status`.
# Docker being down / the container missing / the command failing must NEVER
# fail this cron — the ALERT line in the log is the fallback record.
#
# Per-day logs here are pruned by /etc/logrotate.d/avail (host config).

set -euo pipefail

LOG_DIR="/var/log/avail/nightly_tests"
mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/$DATE.log"

REPO=/root/availai
WT=/root/availai-worktrees/nightly-main
PYTEST="$REPO/.venv/bin/pytest"

echo "=== Nightly Test Run: $(date) ===" > "$LOG_FILE"

# Refresh the pinned worktree to origin/main (create on first run).
git -C "$REPO" fetch -q origin main >> "$LOG_FILE" 2>&1
if [ ! -d "$WT" ]; then
    git -C "$REPO" worktree add --detach "$WT" origin/main >> "$LOG_FILE" 2>&1
fi
git -C "$WT" checkout -q --force --detach origin/main >> "$LOG_FILE" 2>&1
git -C "$WT" clean -qfd >> "$LOG_FILE" 2>&1
echo "Testing $(git -C "$WT" rev-parse --short HEAD) (origin/main)" >> "$LOG_FILE"

cd "$WT"

# ── 1) Python suite with coverage ────────────────────────────────────────
set +e
TESTING=1 PYTHONPATH="$WT" "$PYTEST" tests/ \
    --cov=app --cov-report=term --tb=short -q \
    >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
set -e

# Extract coverage percentage from output
COVERAGE=$(grep -oP 'TOTAL\s+\d+\s+\d+\s+\K\d+' "$LOG_FILE" || echo "0")

# ── 2) TS Playwright E2E (self-contained webServer — see header) ─────────
# npm ci pins deps to the snapshot's package-lock.json (fast with a warm ~/.npm
# cache; node_modules/ is gitignored so `git clean -qfd` above does not wipe it,
# but npm ci rebuilds it deterministically either way). npm run build produces
# the Vite assets (app/static/dist/) the TESTING uvicorn serves.
echo "" >> "$LOG_FILE"
echo "=== E2E (TS Playwright) ===" >> "$LOG_FILE"
set +e
(
    cd "$WT" \
        && npm ci --no-audit --no-fund \
        && npm run build \
        && npx playwright test
) >> "$LOG_FILE" 2>&1
E2E_EXIT=$?
set -e
if [ "$E2E_EXIT" -eq 0 ]; then
    E2E_RESULT="PASS"
else
    E2E_RESULT="FAIL (exit=$E2E_EXIT)"
fi

echo "" >> "$LOG_FILE"
echo "=== Summary ===" >> "$LOG_FILE"
echo "Exit code: $EXIT_CODE" >> "$LOG_FILE"
echo "Coverage: ${COVERAGE}%" >> "$LOG_FILE"
echo "E2E: ${E2E_RESULT}" >> "$LOG_FILE"
echo "Finished: $(date)" >> "$LOG_FILE"

# Classify the outcome honestly: an xdist INTERNALERROR (a worker crashed) is a
# CRASH — pytest aborts before the coverage table, so coverage reads 0% and the
# old summary conflated "suite crashed" with "coverage collapsed". The e2e result
# is folded into the same STATUS line so one alert seam covers both suites.
if grep -q '^INTERNALERROR' "$LOG_FILE"; then
    STATUS="CRASH (xdist internal error — see log; exit=$EXIT_CODE, e2e=$E2E_RESULT)"
elif [ "$EXIT_CODE" -ne 0 ]; then
    STATUS="FAIL (exit=$EXIT_CODE, coverage=${COVERAGE}%, e2e=$E2E_RESULT)"
elif [ "$COVERAGE" -lt 80 ]; then
    STATUS="FAIL (coverage dropped to ${COVERAGE}%, e2e=$E2E_RESULT)"
elif [ "$E2E_EXIT" -ne 0 ]; then
    STATUS="FAIL (e2e exit=$E2E_EXIT, coverage=${COVERAGE}%)"
else
    STATUS="PASS (coverage=${COVERAGE}%, e2e=PASS)"
fi
echo "${DATE}: ${STATUS}" > "$LOG_DIR/LAST_STATUS"
case "$STATUS" in
    PASS*) : ;;
    *)
        echo "ALERT: ${STATUS}" >> "$LOG_FILE"
        # Teams + in-app admin alert via the app container (config stays
        # single-sourced in the container env / credential store). Guarded so a
        # down docker daemon, a missing container, or a failing command never
        # fails the cron — the ALERT line above is the fallback record.
        APP_RUNNING="$(docker compose --project-directory "$REPO" ps -q --status running app 2>/dev/null || true)"
        if [ -n "$APP_RUNNING" ]; then
            docker compose --project-directory "$REPO" exec -T app \
                python -m app.management.notify_nightly_status "${DATE}: ${STATUS}" \
                >> "$LOG_FILE" 2>&1 \
                || echo "ALERT DELIVERY FAILED (notify_nightly_status non-zero — see above)" >> "$LOG_FILE"
        else
            echo "ALERT NOT DELIVERED: app container not running (docker down?)" >> "$LOG_FILE"
        fi
        ;;
esac

# Keep only last 30 days of logs
find "$LOG_DIR" -name "*.log" -mtime +30 -delete 2>/dev/null || true
