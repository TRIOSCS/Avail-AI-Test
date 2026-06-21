#!/usr/bin/env bash
# =============================================================================
# recover_prod.sh — Restore AvailAI production after the failed v2.5.1 deploy.
#
# WHAT HAPPENED: the v2.5.1 release deploy built the new image, but the app
# container came up UNHEALTHY at runtime. The deploy script uses `set -e`, so it
# aborted at `docker compose up -d` BEFORE its own rollback ran — leaving the
# broken version live. app.availai.net is down.
#
# WHAT THIS DOES (safe, code-only — your DATABASE is NOT touched; the v2.5.1
# changes added no migrations):
#   1. Saves the current app logs to a file  ← send this to me to find the cause
#   2. Restores the last-good code backup the failed deploy made
#   3. Rebuilds + brings the stack up, then health-checks
#
# HOW TO RUN (on the droplet, or via SSH from your work PC):
#   ssh <your-droplet>            # the host in your GitHub SERVER_HOST secret
#   curl -fsSL https://raw.githubusercontent.com/TRIOSCS/Avail-AI-Test/main/scripts/recover_prod.sh -o /tmp/recover_prod.sh
#   sudo bash /tmp/recover_prod.sh
#
# After it finishes, send me /root/backups/recover_appfail_*.log so I can fix the
# root cause on main BEFORE we redeploy (otherwise the next deploy re-breaks prod).
# =============================================================================
set -uo pipefail

APP_DIR=/root/availai
BACKUPS=/root/backups
HEALTH_URL=http://localhost:8000/health
STAMP=$(date +%Y%m%d_%H%M%S)

echo "============================================================"
echo " AvailAI production recovery — $(date)"
echo "============================================================"

if [ ! -d "$APP_DIR" ]; then
  echo "ERROR: $APP_DIR not found — are you on the droplet?" >&2
  exit 1
fi
cd "$APP_DIR" || exit 1

echo
echo "=== 0) Current state ==="
docker compose ps 2>&1 | sed 's/^/    /' || true
if curl -sf -m 8 "$HEALTH_URL" >/dev/null 2>&1; then
  echo "    NOTE: app is ALREADY healthy — prod may have recovered. Nothing to do."
  echo "    (Re-run only if you still see app.availai.net down.)"
fi

echo
echo "=== 1) Capture app logs (diagnosis — SEND THIS FILE TO CLAUDE) ==="
LOG="$BACKUPS/recover_appfail_${STAMP}.log"
mkdir -p "$BACKUPS"
docker compose logs --tail=400 app > "$LOG" 2>&1 || true
echo "    Saved: $LOG"
echo "    ---- last 30 lines of app log ----"
tail -30 "$LOG" 2>/dev/null | sed 's/^/    /'
echo "    ----------------------------------"

echo
echo "=== 2) Locate last-good code backup ==="
BK=$(ls -dt "$BACKUPS"/code_* 2>/dev/null | head -1)
if [ -z "${BK:-}" ] || [ ! -d "$BK" ]; then
  echo "ERROR: no code backup found in $BACKUPS (expected code_<timestamp>/)." >&2
  echo "       Do NOT delete anything. Send me the log above and stop here." >&2
  exit 1
fi
echo "    Will restore: $BK"
echo "    Backup taken: $(stat -c '%y' "$BK" 2>/dev/null || echo unknown)"

echo
read -r -p ">>> Restore this code backup and rebuild? Type 'yes' to proceed: " ANS
if [ "$ANS" != "yes" ]; then
  echo "Aborted — no changes made. (Logs were still saved to $LOG.)"
  exit 0
fi

echo
echo "=== 3) Restore code (DB left untouched) ==="
# Keep the broken tree for reference instead of deleting it.
mv "$APP_DIR" "$BACKUPS/failed_v2.5.1_${STAMP}" || { echo "ERROR: could not move $APP_DIR" >&2; exit 1; }
cp -r "$BK" "$APP_DIR" || { echo "ERROR: restore copy failed — broken tree is at $BACKUPS/failed_v2.5.1_${STAMP}" >&2; exit 1; }
cd "$APP_DIR" || exit 1
echo "    Restored $BK -> $APP_DIR  (broken tree kept at $BACKUPS/failed_v2.5.1_${STAMP})"

echo
echo "=== 4) Rebuild + bring up ==="
docker compose build app
docker compose up -d

echo
echo "=== 5) Health check (up to 120s) ==="
for i in $(seq 1 24); do
  sleep 5
  if curl -sf -m 8 "$HEALTH_URL" >/dev/null 2>&1; then
    echo
    echo "  ✅ RESTORED — app healthy after $((i*5))s."
    docker compose ps 2>&1 | sed 's/^/    /' || true
    echo
    echo "  NEXT: send me $LOG so I can fix the v2.5.1 startup bug on main."
    echo "        Do NOT cut another release / run update.sh until that fix lands —"
    echo "        main still has the code that crashed prod."
    exit 0
  fi
  echo "    waiting... ($((i*5))s)"
done

echo
echo "  ❌ Still unhealthy after restore. The failure may be environmental (DB/redis/disk)."
echo "     Send me both:  $LOG   and:  docker compose logs --tail=120"
echo "     The broken v2.5.1 tree is preserved at $BACKUPS/failed_v2.5.1_${STAMP}."
exit 1
