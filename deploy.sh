#!/bin/bash
# deploy.sh — Reliable deploy for AvailAI
# Usage: ./deploy.sh [--no-commit] [message]
set -euo pipefail
cd /root/availai

NO_COMMIT=false
if [ "${1:-}" = "--no-commit" ]; then
    NO_COMMIT=true
    shift
fi

# Step 1: Commit & push (unless --no-commit). Hardened against the silent-
# failure mode where a stale local main (behind origin/main) caused every
# `git push origin main` to be rejected as non-fast-forward — the rebuild
# steps still ran from whatever branch was checked out, so deploys appeared
# to succeed while origin/main drifted days out of sync.
if [ "$NO_COMMIT" = false ]; then
    CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$CURRENT_BRANCH" != "main" ]; then
        echo "ERROR: ./deploy.sh (without --no-commit) must run from main." >&2
        echo "Current branch: $CURRENT_BRANCH" >&2
        echo "Either: git checkout main && git pull --ff-only origin main" >&2
        echo "Or deploy the current branch without pushing: ./deploy.sh --no-commit" >&2
        exit 1
    fi

    echo "==> Syncing local main with origin/main..."
    git fetch origin
    if ! git merge --ff-only origin/main; then
        echo "ERROR: local main diverged from origin/main — cannot fast-forward." >&2
        echo "Resolve with 'git pull --rebase origin main' or investigate before re-running." >&2
        exit 2
    fi

    if git diff --quiet && git diff --cached --quiet; then
        echo "No changes to commit — skipping commit step"
    else
        # Stage only modifications/deletions to already-TRACKED files (`-u`),
        # never new untracked files. The old `git add -A` swept in everything, so
        # the moment .gitignore missed a new untracked secret / SSH key / DB dump
        # it landed in history (CRIT-DEVOPS-2). `-u` removes that entire class at
        # the source: a brand-new file is committed only if the operator
        # deliberately `git add`ed it first — already-staged paths are preserved.
        git add -u
        # Defence in depth for anything deliberately pre-staged: refuse to commit
        # files matching known-sensitive patterns even when explicitly added.
        DANGER=$(git diff --cached --name-only | grep -iE \
            '(^|/)\.env($|\.)|\.(pem|key|p12|pfx|sql|sqlite3?|dump)$|(^|/)(credentials|service-account|id_rsa|id_ed25519)[^/]*$' \
            || true)
        if [ -n "$DANGER" ]; then
            echo "ERROR: deploy aborted — refusing to commit sensitive/data files:" >&2
            echo "$DANGER" | sed 's/^/  /' >&2
            echo "Add them to .gitignore or commit deliberately outside ./deploy.sh." >&2
            echo "The working tree is unchanged; nothing was committed." >&2
            git reset -q
            exit 4
        fi
        git commit -m "${1:-deploy}"
    fi

    AHEAD=$(git rev-list --count origin/main..HEAD)
    if [ "$AHEAD" -gt 0 ]; then
        echo "==> Pushing $AHEAD commit(s) to origin/main..."
        if ! git push origin main; then
            echo "ERROR: git push origin main failed." >&2
            echo "Likely causes: non-fast-forward rejection, auth failure, or branch protection." >&2
            exit 3
        fi
    else
        echo "Local main already matches origin/main — nothing to push."
    fi
fi

# Step 1.5: Preflight — refuse to deploy a password-login backdoor without an
# explicit risk acknowledgement in .env. The app also fail-boots on this
# (app/startup.py), but asserting here fails fast with a clear message instead
# of a health-check timeout. Staging sets ALLOW_PASSWORD_LOGIN_RISK=true → passes.
if grep -qiE '^[[:space:]]*ENABLE_PASSWORD_LOGIN[[:space:]]*=[[:space:]]*true' .env 2>/dev/null \
   && ! grep -qiE '^[[:space:]]*ALLOW_PASSWORD_LOGIN_RISK[[:space:]]*=[[:space:]]*true' .env 2>/dev/null; then
    echo "ERROR: ENABLE_PASSWORD_LOGIN=true but ALLOW_PASSWORD_LOGIN_RISK is not true in .env." >&2
    echo "Password login is an auth bypass. Set ALLOW_PASSWORD_LOGIN_RISK=true to acknowledge" >&2
    echo "(non-production environments only), or disable ENABLE_PASSWORD_LOGIN." >&2
    exit 5
fi

# Step 2: Rebuild app with a unique BUILD_COMMIT each deploy.
# No --no-cache: the Dockerfile consumes BUILD_COMMIT right before the source COPYs (in
# BOTH stages), so the template/static COPYs + the Vite build + the app COPY ALWAYS
# re-run with fresh content (fresh Tailwind CSS guaranteed), while the expensive,
# input-pinned layers (apt, gh, pip, Chromium, `npm ci`) stay cached. ~4x faster deploys
# with no stale-template risk. The unique timestamp also invalidates --no-commit deploys.
BUILD_COMMIT="$(git rev-parse --short HEAD)-$(date +%s)"
echo "==> Rebuilding app + enrichment-worker images (build tag: $BUILD_COMMIT)..."
# app and enrichment-worker share `build: .` (same Dockerfile), so building both
# here reuses every cached layer — the worker image is nearly free. This stops the
# worker from silently running stale wheels after a dependency bump: it shares
# requirements.txt with the app (e.g. redis-py, anthropic), and deploy.sh used to
# rebuild only the app, leaving the worker on the old image until someone noticed.
docker compose build --build-arg BUILD_COMMIT="$BUILD_COMMIT" app enrichment-worker

# Step 3: Recreate only the app container with the new image
# Clean up any orphaned rename containers from previous deploys
echo "==> Restarting app..."
docker compose down app 2>/dev/null || true
docker container prune -f --filter "label=com.docker.compose.project=availai" 2>/dev/null || true
docker compose up -d app

# Step 4: Wait for health check to pass
echo "==> Waiting for app to become healthy..."
TRIES=0
MAX_TRIES=30
while [ $TRIES -lt $MAX_TRIES ]; do
    CONTAINER=$(docker compose ps -q app)
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "unknown")
    if [ "$STATUS" = "healthy" ]; then
        echo "==> App is healthy!"
        break
    fi
    TRIES=$((TRIES + 1))
    sleep 2
done

if [ "$STATUS" != "healthy" ]; then
    echo "==> ERROR: App did not become healthy after ${MAX_TRIES} attempts"
    echo "==> Last 50 log lines:"
    docker compose logs --tail=50 app
    exit 1
fi

# Step 4b: Log the P2.7 deferred-backfill readiness state. This is OBSERVABILITY
# ONLY — the deploy is already gated on liveness (Step 4 above) and must NEVER be
# failed by a still-running background backfill/ANALYZE phase on a prod-sized DB
# (that false-failure was exactly what P2.7 fixed). /health/ready may legitimately
# still report false here on a large DB; that's expected and not an error.
echo ""
echo "==> Checking deferred startup-backfill readiness (informational only)..."
READY_BODY=$(docker compose exec -T app curl -sf http://localhost:8000/health/ready 2>/dev/null || echo '{"ready":"unknown"}')
echo "==> /health/ready: ${READY_BODY}"

# Step 5: Verify deployed build tag matches what we just built
echo ""
echo "==> Verifying deployed build tag..."
DEPLOYED_COMMIT=$(docker compose exec app printenv BUILD_COMMIT 2>/dev/null | tr -d '[:space:]' || echo "UNKNOWN")

if [ "$DEPLOYED_COMMIT" != "$BUILD_COMMIT" ]; then
    echo "==> MISMATCH: deployed ($DEPLOYED_COMMIT) does NOT match build ($BUILD_COMMIT)"
    exit 1
fi
echo "==> MATCH: deployed build tag ($DEPLOYED_COMMIT)"

# Step 5b: Recreate the enrichment-worker on the freshly-built image.
# The worker has no HTTP health check, so confirm it is running and verify the
# BUILD_COMMIT baked into its env (same Dockerfile as app). Done after the app is
# healthy + verified so a broken app build never disrupts a working worker.
echo ""
echo "==> Recreating enrichment-worker..."
docker compose up -d enrichment-worker
sleep 3
WORKER_CONTAINER=$(docker compose ps -q enrichment-worker)
if [ "$(docker inspect --format='{{.State.Running}}' "$WORKER_CONTAINER" 2>/dev/null)" != "true" ]; then
    echo "==> ERROR: enrichment-worker is not running after recreate"
    echo "==> Last 50 log lines:"
    docker compose logs --tail=50 enrichment-worker
    exit 1
fi
# Catch a worker that builds fine but crash-loops at runtime (a bad import / a
# redis-py or anthropic API break — exactly the #227 scenario). It has no health
# check and uses restart: always, so a single snapshot can catch it mid-restart
# looking "running"; confirm RestartCount is stable over a short window.
RC1=$(docker inspect --format='{{.RestartCount}}' "$WORKER_CONTAINER" 2>/dev/null || echo 0)
sleep 8
RC2=$(docker inspect --format='{{.RestartCount}}' "$WORKER_CONTAINER" 2>/dev/null || echo 0)
if [ "${RC2:-0}" -gt "${RC1:-0}" ] \
    || [ "$(docker inspect --format='{{.State.Running}}' "$WORKER_CONTAINER" 2>/dev/null)" != "true" ]; then
    echo "==> ERROR: enrichment-worker is crash-looping (restarts ${RC1} -> ${RC2}) — not a healthy deploy"
    echo "==> Last 50 log lines:"
    docker compose logs --tail=50 enrichment-worker
    exit 1
fi
WORKER_COMMIT=$(docker compose exec -T enrichment-worker printenv BUILD_COMMIT 2>/dev/null | tr -d '[:space:]' || echo "UNKNOWN")
if [ "$WORKER_COMMIT" != "$BUILD_COMMIT" ]; then
    echo "==> MISMATCH: enrichment-worker ($WORKER_COMMIT) does NOT match build ($BUILD_COMMIT)"
    exit 1
fi
echo "==> MATCH: enrichment-worker build tag ($WORKER_COMMIT)"

# Step 6: Verify CSS covers Tailwind color classes used in templates
echo ""
echo "==> Verifying Tailwind CSS coverage..."
CSS_FILE=$(docker compose exec app sh -c 'ls /app/app/static/dist/assets/styles-*.css 2>/dev/null' | tr -d '[:space:]')
if [ -z "$CSS_FILE" ]; then
    echo "==> WARNING: Could not find CSS bundle to verify."
else
    MISSING=$(docker compose exec app sh -c "
        grep -rohP '(?:bg|text|border|hover:bg|hover:text)-[a-z]+-\d+' /app/app/templates/ 2>/dev/null \
        | sort -u \
        | while read cls; do
            base=\$(echo \"\$cls\" | sed 's/hover://')
            grep -q \"\$base\" $CSS_FILE || echo \"  MISSING: \$cls\"
        done
    ")
    if [ -n "$MISSING" ]; then
        echo "==> WARNING: Tailwind classes in templates but NOT in CSS bundle:"
        echo "$MISSING"
        echo "==> If classes are missing, Dockerfile Stage 1 may have stale template copies."
    else
        echo "==> All Tailwind color classes in templates are present in CSS bundle."
    fi
fi

# Step 6b: Refresh the host worker venv, then restart the HOST nc/ics worker units.
# nc_worker and ics_worker run on the HOST (systemd units from /root/availai), OUTSIDE
# docker — so a deploy that changes their code would otherwise leave them on stale code
# until someone manually restarts them. They run from the pinned-lockfile venv
# (/root/availai/.venv, built from requirements.txt) so they carry the SAME pinned deps
# as the docker images; this step re-syncs that venv to the current lock before restart.
# Best-effort + idempotent: only touches the venv/units that exist (a no-op on CI / boxes
# without them).
echo ""
echo "==> Refreshing host worker venv from requirements.txt..."
HOST_WORKER_WARN=""
if [ -x .venv/bin/pip ]; then
    if venv_err=$(.venv/bin/pip install -q -r requirements.txt 2>&1); then
        echo "==> host venv in sync with pinned lock (patchright $(.venv/bin/pip show patchright 2>/dev/null | awk '/^Version:/{print $2}'))"
    else
        echo "==> WARNING: host worker venv refresh FAILED (workers may run stale deps):"
        echo "${venv_err}" | tail -5
        HOST_WORKER_WARN="${HOST_WORKER_WARN} venv-refresh"
    fi
else
    echo "==> no host worker venv (.venv) here — skipping refresh"
fi
echo ""
echo "==> Restarting host worker units (nc/ics/tbf)..."
for unit in avail-nc-worker avail-ics-worker avail-tbf-worker; do
    if ! systemctl cat "${unit}.service" >/dev/null 2>&1; then
        echo "==> ${unit} not installed here — skipping"
        continue
    fi
    # Capture stderr so a failed restart reports WHY (needs-root vs broken new code vs transient),
    # instead of a generic "could not restart". Try unprivileged, then escalate to sudo.
    if restart_err=$(systemctl restart "${unit}.service" 2>&1) \
        || restart_err=$(sudo systemctl restart "${unit}.service" 2>&1); then
        echo "==> restarted ${unit} ($(systemctl is-active "${unit}.service" 2>/dev/null || true))"
    else
        echo "==> WARNING: could not restart ${unit}: ${restart_err}"
        HOST_WORKER_WARN="${HOST_WORKER_WARN} ${unit}"
    fi
done

# Step 7: Show recent logs to confirm the right code is running
echo ""
echo "==> Recent app logs:"
docker compose logs --tail=20 app

# Re-surface any host-worker venv/restart failure AFTER the log dump, so the operator's
# last line is the actionable warning — a SILENTLY stale host worker is the bug this
# prevents (stale code OR stale deps).
if [ -n "${HOST_WORKER_WARN}" ]; then
    echo ""
    echo "==> ⚠️  Deploy OK, but these host worker step(s) FAILED and may leave workers on stale code/deps:"
    echo "==>    ${HOST_WORKER_WARN}"
    echo "==>     Fix manually: 'cd /root/availai && .venv/bin/pip install -r requirements.txt' (deps),"
    echo "==>     then 'sudo systemctl restart avail-nc-worker avail-ics-worker avail-tbf-worker'."
fi

# Step 8: Surface a stale weekly backup-verification failure, if any. The
# avail-backup-verify-alert.service OnFailure= hook (scripts/systemd/
# avail-backup-verify-alert.service) writes this marker when the Sun 04:00
# verify-backup.sh timer finds a corrupt/missing newest backup — it stays until
# manually cleared, so a deploy loudly re-surfaces it instead of it quietly
# expiring off the top of `journalctl`. Informational only — never fails the deploy.
BACKUP_ALERT_MARKER="${BACKUP_ALERT_MARKER:-/root/backups/VERIFY_FAILED}"
if [ -f "${BACKUP_ALERT_MARKER}" ]; then
    echo ""
    echo "==> ⚠️  UNRESOLVED backup-verify failure marker found: ${BACKUP_ALERT_MARKER}"
    cat "${BACKUP_ALERT_MARKER}"
    echo "==>     Investigate, then clear with: rm -f ${BACKUP_ALERT_MARKER}"
fi

echo ""
echo "==> Deploy complete."
