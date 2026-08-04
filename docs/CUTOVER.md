# CUTOVER.md — v0 (written Session 1, 2026-08-04)

The runbook for switching production to the simplified build. Every
wave-start DB refresh (`scripts/simp-refresh-db.sh`) rehearses the
restore + migrate path below. Executed only on Mike's explicit word,
after the launch deal completes clean (Brief §5). v0 = commands as
proven during Session 1 stand-up; refined each wave.

## 0. Preconditions

- All four waves complete; nightly kernel E2E green on the parallel
  instance; launch deal (real money) completed clean on it.
- Mike has said the word. No cutover on inference.

## 1. Pre-cutover snapshot (rollback anchor)

```bash
# Fresh prod backup, on top of the 6-hourly cycle
docker exec availai-db-backup-1 /scripts/backup.sh
docker exec availai-db-backup-1 sh -c 'ls -t /backups/availai_*.dump.gz | head -1'   # note filename
# Note current prod commit + image
git -C /root/availai rev-parse HEAD
docker inspect -f '{{.Image}}' availai-app-1
```

## 2. Ship the branch

```bash
# Merge simplification -> main via PR (normal review flow), then on the droplet:
cd /root/availai && git checkout main && git pull --ff-only origin main
./deploy.sh          # builds, recreates app (entrypoint runs alembic upgrade head),
                     # health-gates, auto-rolls-back on failed health
```

The alembic path (prod data -> branch head) is exactly what every
wave-start refresh rehearsed. SECRET_KEY must NOT change at cutover —
encrypted columns (M365 tokens, API keys) are keyed to it; the app's
encryption canary refuses to boot on a mismatch (verified S1 when the
parallel instance was first stood up).

## 3. Verify

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://app.availai.net/health        # 200
docker exec availai-db-1 psql -U availai -d availai -tAc 'SELECT version_num FROM alembic_version;'
# Kernel smoke: login page 302/307 chain matches, one requisition opens,
# Approvals workspace renders. (Wave 1 replaces this line with the scripted
# kernel walk run against production.)
```

## 4. Rollback (if verify fails)

```bash
# deploy.sh already auto-rolls the app image back on failed health checks.
# If the schema itself must go back (migration damage), restore the §1 dump:
docker compose -p availai stop app enrichment-worker
gunzip -c <dump-from-step-1> | docker exec -i availai-db-1 pg_restore -U availai -d availai --clean --if-exists --no-owner
git checkout <commit-from-step-1> && ./deploy.sh --no-commit
```

## 5. Prod-side cleanup deferred to cutover day

Deviation-logged in STATE.md — these touch production, so they wait:

- Remove legacy host-cron backup (`crontab -e`: delete the
  `backup_postgres.sh` line) — the db-backup container is the ONE
  backup system (spec §6); install/enable its verify-timer units.
- Re-point the `/health` backup-freshness probe at the container
  backup path (spec §6).
- Remove `daily_coverage_report.sh` cron if the coverage farm was
  deleted in Wave 2 (spec §9 Decision K).
- Re-enable in prod .env whatever launch actually needs from the
  keys-off set (Azure/Graph stays as-is in prod .env — it was never
  changed; only the PARALLEL instance's copy was blanked).
- Untracked `specs/simplification-*.md` files in /root/availai:
  delete (canonical copies live in docs/ on the branch).

## 6. Decommission the parallel instance (after stable cutover)

```bash
docker compose -p availai-simp -f /root/availai-worktrees/simplification/docker-compose.simp.yml down -v
ufw delete allow 8443/tcp
rm -rf /root/availai-simp-secrets
crontab -e   # remove the simp nightly E2E line
git -C /root/availai worktree remove /root/availai-worktrees/simplification
```

## Appendix: parallel-instance rituals (used during the waves)

- **Wave-start DB refresh:** `./scripts/simp-refresh-db.sh` from the
  worktree — pulls newest prod dump (checksum-verified), drop/create,
  pg_restore, restarts app (entrypoint = alembic upgrade head = the
  rehearsal), prints alembic head + row sanity.
- **Cert refresh** (prod LE cert rotates ~60d; current one expires
  2026-10-16):
  ```bash
  docker cp availai-caddy-1:/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/app.availai.net/app.availai.net.crt /root/availai-simp-secrets/certs/
  docker cp availai-caddy-1:/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/app.availai.net/app.availai.net.key /root/availai-simp-secrets/certs/
  docker restart availai-simp-caddy-1
  ```
- **Simp .env:** regenerated from prod .env with outbound credentials
  blanked (Azure/Graph, Anthropic, connectors, 8x8, Sentry, Spaces,
  GitHub) and flags forced off (self-heal, email mining, sweeps that
  notify). SECRET_KEY stays = prod (encryption canary). Cookie note:
  browsers ignore ports, so app.availai.net:443 and :8443 share
  session cookies — log out when switching instances if sessions act
  confused.
- **Deploy branch changes to the parallel instance:**
  ```bash
  cd /root/availai-worktrees/simplification
  docker compose -p availai-simp -f docker-compose.simp.yml build app
  docker compose -p availai-simp -f docker-compose.simp.yml up -d --wait app enrichment-worker
  ```
