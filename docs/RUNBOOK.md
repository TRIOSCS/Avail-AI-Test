# AvailAI — Go-Live & Operations Runbook

Plain-language runbook for taking AvailAI from single-user staging (`app.availai.net`)
to multi-user production for the Trio team, plus "if something breaks" basics. Written
for a non-ops owner: every step is a copy-paste command or a click-path.

_Last verified on the server: 2026-07-02 (build after the security-hardening batch)._
_The `app.availai.net` box IS the deployment — there is no separate "prod" server today._

---

## 1. What's already solid (verified on the box, don't re-do)

- **Sign-in is locked down**: the email allowlist is ON by default, so unknown emails are
  rejected on Microsoft sign-in (no open self-signup).
- **Microsoft/Azure sign-in is already configured** (`AZURE_CLIENT_ID`/`TENANT` set) — team
  sign-in just needs to be *turned on*, not built.
- **Error monitoring is live** (Sentry captures exceptions).
- **Backups run every 6h and actually restore** (proven with a real restore drill — see §4).
- **No secrets in git**; Postgres/Redis are not exposed to the network (localhost/docker only).
- **Security hardening deployed**: data-ownership (IDOR) holes closed, approval financials
  scoped to owners, CSRF gap closed, demo-data seeders can't run against prod.

---

## 2. Go-live checklist (do these to open it to the team)

Do them in this order. Items marked **[owner]** need you (or whoever owns the DigitalOcean /
Microsoft 365 accounts); everything else I do.

1. **[owner] Load the real data.** The production database is empty. Import the real
   customers/parts/history (the SFDC/data load) before onboarding users — an empty app is a
   bad first impression and some flows assume data exists.
2. **Allowlist the team's emails.** Each teammate signs in with their Trio Microsoft account;
   their email must be known to the app first (allowlisted or pre-created). I handle this.
3. **[owner decision] Turn off the shared password login.** Today you sign in with an
   email+password admin account (`ENABLE_PASSWORD_LOGIN=true`, `DEFAULT_USER_*`). That's fine
   solo, but a shared admin password isn't safe for a team. Once everyone can sign in with
   Microsoft (step 2), tell me and I'll set `ENABLE_PASSWORD_LOGIN=false` and remove the
   default account. **Don't do this before the team can sign in via Microsoft — it's your
   current way in.**
4. **[owner] Off-site backups.** Backups currently live only on this server — if the server
   were lost, so are they. In the DigitalOcean console, create a **Spaces** bucket (e.g.
   `availai-backups`) and an access key, and hand me the 4 values (`DO_SPACES_KEY`,
   `DO_SPACES_SECRET`, `DO_SPACES_BUCKET`, `DO_SPACES_REGION`). I'll wire and schedule the
   off-site copy (`scripts/backup-to-spaces.sh`).
5. **Quick multi-user smoke.** Two people click through Requisitions, a quote, a buy plan,
   CRM at the same time — confirm nothing feels wrong under real concurrent use.

Lower-priority hardening I'll do on a careful pass (not launch-blocking): rotate the
localhost-only DB password, register the global rate-limit middleware, restrict `/docs`,
add a one-command deploy rollback.

---

## 3. Deploying a change

From `/root/availai` on `main`:

```bash
./deploy.sh --no-commit    # rebuild current main, migrate, health-check, verify build tag
```

- Runs `alembic upgrade head` on startup; the app **refuses to boot if a migration fails**
  (fail-safe — it won't serve a half-migrated DB).
- Verify after: the build tag on `availai-app-1` matches `git rev-parse --short HEAD`, and
  `curl http://<app-container-ip>:8000/health` returns `200`.
- `deploy.sh` from `main` (no `--no-commit`) commits+pushes first.

---

## 4. Backups & restore (proven procedure)

**Backups:** the `availai-db-backup` container writes a checksummed Postgres dump every 6h to
the `availai_pgbackups` volume. The newest is pointed to by `/backups/LATEST`. These are the
authoritative backups (there's also an older redundant plain-SQL cron dump — ignore it).

**Verify a backup without restoring (safe):**

```bash
docker exec availai-db-backup-1 /scripts/restore.sh --verify /backups/<file>.dump.gz
# → checks the sha256 and lists the tables; prints "verification: PASSED"
```

**Restore into a THROWAWAY db to prove it (safe — never touches live):**

```bash
docker run -d --name avail-restore-drill -e POSTGRES_PASSWORD=drill \
  --network availai_default postgres:16
docker cp /mnt/volume_sfo2_1782582546660/docker/volumes/availai_pgbackups/_data/<file>.dump.gz \
  avail-restore-drill:/tmp/b.dump.gz
docker exec avail-restore-drill sh -c \
  'createdb -U postgres r; gunzip -c /tmp/b.dump.gz | pg_restore -U postgres -d r --no-owner --no-acl'
# verify: docker exec avail-restore-drill psql -U postgres -d r -c "select count(*) from users;"
docker rm -f avail-restore-drill    # tear down
```

**Real recovery (DANGER — overwrites the live DB):** use `scripts/restore.sh <file>` only in a
genuine data-loss emergency, and take a fresh backup first. Confirm a backup exists before any
destructive DB operation.

---

## 5. If something breaks

| Symptom | First check |
|---|---|
| Site down / 502 | `docker compose ps` (all `Up`/`healthy`?); `docker compose logs -f app` |
| App won't start after deploy | Logs usually show a failed `alembic upgrade` — a migration issue; the app refuses to boot rather than corrupt data |
| Crash-loop mentioning "host name db" | Stale DNS — `docker compose down && docker compose up -d` |
| Errors for users | Sentry has the exception + stack trace |
| Worker stalled (enrichment/nc/ics/tbf) | `GET /api/admin/workers/status`; the liveness watchdog alerts on real stalls |
| Health probe | `curl http://<app-container-ip>:8000/health` → `200` |

- App containers: `availai-app-1` (web), `availai-enrichment-worker-1` (background), plus
  `db`, `redis`, `caddy` (TLS), `db-backup`. Host worker units: `avail-nc-worker`,
  `avail-ics-worker`, `avail-tbf-worker` (systemd).
- Roll back a bad deploy: `git checkout <previous-good-sha>` then `./deploy.sh --no-commit`
  (a one-command rollback is on the hardening list).

---

## 6. Key config flags (in `.env`, never commit real values)

| Flag | Meaning | Launch target |
|---|---|---|
| `ENABLE_PASSWORD_LOGIN` | shared email+password login (OAuth bypass) | `false` once team is on Microsoft sign-in |
| `DEFAULT_USER_EMAIL`/`_PASSWORD`/`_ROLE` | seeds a default login account | unset in production |
| `ENABLE_USER_ALLOWLIST` | reject unknown emails on sign-in | keep `true` (the default) |
| `AZURE_CLIENT_ID`/`AZURE_TENANT_ID` | Microsoft sign-in | already set |
| `SENTRY_DSN` | error monitoring | already set |
| `DO_SPACES_*` | off-site backup | set once the bucket/key exist |
