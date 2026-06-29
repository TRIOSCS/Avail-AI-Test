# Pre-Rollout Checklist

Run this **before** kicking off the fresh SFDC import so it lands in a known-good system.

This checklist came out of the 2026-04-22 sourcing-engine repair. Every item here corresponds to a real failure mode we hit this session — either something that was silently broken, or a tripwire for the upcoming data import.

---

## How to use

- Work top-to-bottom. Each section is a **gate** — don't move past a failed gate without understanding the consequence.
- For every gate, paste the command output and your pass/fail verdict into a deploy ticket or chat thread so the history is auditable.
- If you can't resolve a gate, file an issue and **do not proceed** to the import step. The import touches user data, some of which will be encrypted. Once encrypted with the wrong salt, it is unrecoverable.

---

## Gate 1 — Environment integrity

The `.env` file is load-bearing. In the 2026-04-22 session, a prior edit had lost `POSTGRES_PASSWORD` and left duplicate keys, causing `docker compose` to fail parse-time on every command until we restored it.

### Gate 1a — `.env` parses cleanly

```bash
cd /root/availai
docker compose config --quiet
# ✓ no output = clean parse
# ✗ anything else — stop, fix .env, re-run
```

### Gate 1b — No duplicate keys

```bash
grep -E '^[A-Z_]+=' .env | awk -F= '{print $1}' | sort | uniq -c | awk '$1 > 1'
# ✓ no output
# ✗ any output — duplicate; remove the loser line
```

### Gate 1c — Every key docker-compose.yml interpolates is present

```bash
# List keys compose references as ${VAR}:
COMPOSE_KEYS=$(grep -oE '\$\{[A-Z_]+' docker-compose.yml | sort -u | sed 's/^\${//')
# All must be present in .env:
for key in $COMPOSE_KEYS; do
    grep -qE "^${key}=" .env || echo "MISSING: $key"
done
# ✓ no "MISSING:" output
```

### Gate 1d — Keys with non-empty values for every critical connector

```bash
# Check only presence + non-empty, not values:
for key in POSTGRES_PASSWORD SECRET_KEY ANTHROPIC_API_KEY NEXAR_CLIENT_ID NEXAR_CLIENT_SECRET DIGIKEY_CLIENT_ID DIGIKEY_CLIENT_SECRET MOUSER_API_KEY BROKERBIN_API_KEY OEMSECRETS_API_KEY; do
    value=$(grep -E "^${key}=" .env | head -1 | cut -d= -f2-)
    if [ -z "$value" ]; then
        echo "EMPTY: $key"
    fi
done
# ✓ no "EMPTY:" output
```

### Gate 1e — Container sees the expected keys

```bash
for key in POSTGRES_PASSWORD SECRET_KEY ANTHROPIC_API_KEY; do
    count=$(docker compose exec -T app printenv 2>/dev/null | grep -cE "^${key}=" || true)
    [ "$count" = "1" ] || echo "NOT-IN-CONTAINER: $key ($count)"
done
# ✓ no output = all three keys reach the container
```

---

## Gate 2 — `.env` backup & recovery

If the import populates encrypted columns and the salt (or SECRET_KEY that feeds it) is ever lost, every encrypted cell becomes unreadable Fernet ciphertext. `.env` holds the derivation inputs — it must have a documented backup.

### Gate 2a — Fresh timestamped backup exists

```bash
ls -la .env.backup.* 2>/dev/null | tail -3
# ✓ at least one backup from the last 7 days
```

If none exists, create one immediately:

```bash
cp .env ".env.backup.$(date -u +%Y%m%dT%H%M%SZ)"
```

Backup files are git-ignored via the `.env.*` pattern (PR #92).

### Gate 2b — Offsite copy

The `.env` must also exist in at least one of:
- A password manager entry (1Password / Bitwarden / etc.) with the full file contents.
- A team secrets vault.
- An encrypted file in a separate cloud storage account from the droplet.

The droplet can be destroyed; the secrets must survive that.

### Gate 2c — Recovery drill

On a clean VM (or throwaway container):
1. Start from a fresh image with no `.env`.
2. Pull `.env` from the backup source.
3. `docker compose config --quiet` parses clean.
4. `docker compose up -d` produces a healthy app container.

If you can't do this drill end-to-end, the backup strategy is theoretical and you don't have one yet.

---

## Gate 3 — TLS cert & DNS

**RESOLVED 2026-06-12:** public DNS for `app.availai.net` now returns the droplet IP (`104.248.191.152` via 8.8.8.8), so HTTP-01 renewal works — verified by a successful auto-renewal (cert expires 2026-08-17, Let's Encrypt E8). The local `dig` showing `127.0.0.1` is this host's `/etc/hosts`, not public DNS. The checks below remain as the recurring verification procedure.

### Gate 3a — Current cert has > 30 days remaining

```bash
expiry=$(timeout 5 openssl s_client -servername app.availai.net -connect 127.0.0.1:443 </dev/null 2>/dev/null \
    | openssl x509 -noout -enddate | cut -d= -f2)
days=$(( ( $(date -d "$expiry" +%s) - $(date +%s) ) / 86400 ))
echo "Cert expires: $expiry ($days days)"
# ✓ days > 30
# ⚠ days ≤ 30 — address renewal before import
```

### Gate 3b — One of (a) or (b) is true

**(a) Public DNS for `app.availai.net` points at the droplet IP (`104.248.191.152`):**

```bash
dig @8.8.8.8 +short app.availai.net
# ✓ returns 104.248.191.152
```

If yes, HTTP-01 renewal works without changes.

**(b) Caddy is configured for DNS-01 via Cloudflare API:**

```bash
grep -A3 "tls " /root/availai/Caddyfile | grep -iE "dns|cloudflare"
```

If yes, renewal works via TXT records without needing the droplet reachable by hostname.

**If neither (a) nor (b) is satisfied, renewal will fail.** Fix before the cert ages into its renewal window. Do not import data under a cert that's about to fail.

---

## Gate 4 — Encryption scope & salt

`users.refresh_token`, `users.access_token`, `users.password_hash` are `EncryptedText` columns whose Fernet key derives from `SECRET_KEY` + `ENCRYPTION_SALT` (falling back to a hard-coded legacy salt when `ENCRYPTION_SALT` is unset).

> **Blast radius — API credentials share the salt.** `ENCRYPTION_SALT` *also* keys `app/services/credential_service.py`, which encrypts `api_sources.credentials` (supplier API keys). That path **degrades gracefully** — on a decrypt miss it falls back to the env-var credential and logs — so it does **not** block a salt rotation, but the DB-stored supplier keys become unreadable until re-entered (admin → Connectors) or supplied via env vars. The three `users` token/password columns do **not** degrade gracefully (orphaned tokens force re-login; an orphaned `password_hash` breaks password login), which is why the rotation command targets them. Re-enter any DB-stored supplier keys after rotating.

### Gate 4a — Decide the rotation plan NOW, not after the import

Options:
- **Keep the legacy salt fallback** (`ENCRYPTION_SALT` unset). Simplest, already works. Defense-in-depth is weaker; acceptable for small team / internal tool.
- **Rotate to a fresh per-deployment salt** BEFORE the import, using the rotation command below to re-encrypt the existing non-null rows. After the import, you can still rotate, but the command must cover the whole `users` table plus any new encrypted columns SFDC adds.

### Gate 4b — If keeping legacy salt

Document explicitly in `STABLE.md` that `SECRET_KEY` is jointly load-bearing with the hard-coded legacy salts in `app/utils/encrypted_type.py::_LEGACY_SALT` and `app/services/credential_service.py::_LEGACY_CREDENTIAL_SALT`. Changing either `SECRET_KEY` invalidates all encrypted rows. (Already captured in `STABLE.md` → *Encryption*.)

### Gate 4c — If rotating

Use the management command **`app/management/rotate_encryption_salt.py`**. It decrypts every `users` EncryptedText cell with the OLD salt's key and re-encrypts with the NEW salt's key in one transaction. The crypto is keyed off the values you pass (**not** the live config), so you can run it before *or* after editing `.env`. It is **idempotent/resumable** (a value already on the NEW salt is detected and skipped) and **never discards** a value it can't decrypt (it reports `undecryptable` and leaves the row intact).

1. **Back up first** — confirm Gate 2 (`.env`) and Gate 9 (DB snapshot). A rotation rewrites encrypted cells.
2. **Generate the new salt:** `openssl rand -base64 32`
3. **Dry-run** (reads + reports, writes nothing). With `.env` still on the OLD salt:
   ```bash
   docker compose exec -T app python -m app.management.rotate_encryption_salt \
       --new-salt "<NEW_SALT>" --dry-run
   ```
   Confirm **`undecryptable=0`** for every column. If any value is undecryptable, **STOP** — the OLD salt / `SECRET_KEY` assumption is wrong; do not proceed.
4. **Rotate for real** (same NEW salt):
   ```bash
   docker compose exec -T app python -m app.management.rotate_encryption_salt \
       --new-salt "<NEW_SALT>"
   ```
   (The default OLD salt is the live `settings.encryption_salt`. If `.env` has already been changed to the new value, pass the previous salt explicitly with `--old-salt "<OLD_SALT>"`. `--new-salt` may also come from the `NEW_ENCRYPTION_SALT` env var.)
5. Set **`ENCRYPTION_SALT=<NEW_SALT>`** in `.env`, then recreate the `app` + `enrichment-worker` containers so the live key matches the re-encrypted data.
6. Add the new salt to the `.env` backup (Gate 2) and the offsite store **immediately**.
7. **Verify** a user can still authenticate / password-login (the three columns decrypt), then re-enter any DB-stored supplier credentials (blast-radius note above).
8. Confirm `STABLE.md` → *Encryption* still states that `SECRET_KEY` + `ENCRYPTION_SALT` are jointly load-bearing.

---

## Gate 5 — Sourcing engine health

The import triggers the same search/connector paths. If the sourcing engine has regressed, the import won't surface the issue — you'll find out later when searches return empty results.

### Gate 5a — Orchestrator budget is live

```bash
docker compose exec -T app python -c \
    "from app.config import settings; print(settings.search_total_timeout_s)"
# ✓ prints 12.0 (or whatever your current default is)
```

### Gate 5b — Per-source commit is in `run_health_checks`

```bash
grep -A1 "for source in sources" /root/availai/app/services/health_monitor.py | grep -c "db.commit"
# ✓ returns 1 — per-source commit inside the loop
```

### Gate 5c — Zero `LockNotAvailable.*api_sources` in the last hour

```bash
docker compose logs --since=1h app 2>&1 | grep -c 'LockNotAvailable.*api_sources'
# ✓ 0
```

### Gate 5d — At least two connectors returning results

```bash
docker compose logs --since=30m app 2>&1 | grep -E "(DigiKey|Element14|Nexar|BrokerBin|Mouser|OEMSecrets).*results"
# ✓ at least two distinct connectors in the last 30 min
```

If zero or one connector is working, investigate before import. The `vendor_affinity` + `material_card_history` scoring paths rely on at least partial connector coverage.

---

## Gate 6 — Alembic

Data migrations that ride along with the SFDC import must land cleanly against a DB at a single known head.

### Gate 6a — Single head

```bash
TESTING=1 alembic heads | wc -l
# ✓ 1
```

Multiple heads require `alembic merge heads -m "..."` before proceeding.

### Gate 6b — Running DB matches file head

```bash
HEAD=$(TESTING=1 alembic heads | awk '{print $1}')
CURRENT=$(docker compose exec -T app alembic current 2>/dev/null | grep -oE '^[0-9a-f_]+_[a-z_]+' | head -1)
[ "$HEAD" = "$CURRENT" ] && echo OK || echo "MISMATCH: head=$HEAD current=$CURRENT"
```

### Gate 6c — No pending migration files not yet applied

```bash
docker compose exec -T app alembic current 2>&1 | grep -c "(head)"
# ✓ 1
```

---

## Gate 7 — Disk headroom

The import itself can be large (8 years of SFDC data). Docker build caches can also regrow aggressively.

### Gate 7a — Droplet has at least 20 GB free

```bash
df -h / | tail -1
# ✓ Avail column shows ≥ 20G
```

If less, run the reclaim commands from STABLE.md before import:

```bash
docker builder prune -f
docker image prune -a -f --filter "until=72h"
```

### Gate 7b — Postgres data volume is healthy

```bash
du -sh /var/lib/docker/volumes/availai_pgdata/
# record for comparison post-import
```

### Gate 7c — Backup volume has retention headroom

```bash
du -sh /var/lib/docker/volumes/availai_pgbackups/
# ✓ < 4 GB OR retention is being enforced (BACKUP_RETENTION_DAYS in .env)
```

---

## Gate 8 — Docker + container health

```bash
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
# ✓ every container: "Up ... (healthy)"
# ✗ restart loops, unhealthy, or missing services — fix first
```

```bash
docker compose exec -T db psql -U availai -d availai -c "select count(*) from pg_stat_activity where datname='availai' and state != 'idle'"
# ✓ low number (< 20) — no runaway connections
```

---

## Gate 9 — Rollback plan for the import itself

Before you run the import command:

1. **DB snapshot now.** Manual `pg_dump` to a filename with a timestamp, independent of the automated `db-backup` service. Stored where you can reach it from outside the droplet (e.g. S3 / user's laptop).
2. **Record the Alembic head** in your runbook.
3. **Document the exact rollback procedure** — restore-from-dump command, Alembic downgrade-to-head-X command, container-recreate command. Run the rollback on a staging copy first if you can.
4. **Know who's on-call** during + for 48h after the import, in case a post-import issue surfaces only once real traffic hits encrypted or migrated data.

---

## Gate 10 — Datasheet company library (optional; non-blocking)

The auto-datasheet-capture feature stores a permanent copy of each part's datasheet in a
**company SharePoint library**, written by the app itself (app-only Graph). It is
**inert-but-safe until configured** — with no library set it skips storage and stamps the
30-day cooldown, so it never blocks rollout. To ENABLE it (do this whenever IT provisions
the library; may be after rollout):

1. **Create** a "Datasheets" document library in the chosen SharePoint site.
2. **Grant** the Azure app the **`Sites.Selected`** *write* permission on that site (admin
   consent) — least-privilege; the app can write ONLY that library.
3. **Obtain** the library's Graph **drive id** and set **`DATASHEET_LIBRARY_DRIVE_ID`** in
   `.env` (optional `DATASHEET_LIBRARY_SUBPATH`, default `Datasheets`); recreate the `app`
   + `enrichment-worker` containers so they pick it up.
4. **Verify** after setting: drive a real part search, confirm a copy lands in the library
   and the in-app download (`/v2/partials/search/dossier/datasheet/{id}/download`) streams
   the PDF.

Reuses the existing `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID`. Until
step 3, capture runs but skips storage (graceful). See `docs/APP_MAP_INTERACTIONS.md`
(datasheet flow) for the full data path.

---

## Post-import — don't forget to come back here

Once the import succeeds and the system has been stable for 48h:

- Remove `project_db_fresh_sfdc_pending` from `~/.claude/projects/-root/memory/` (or whichever memory path applies). The "DB is intentionally empty" heuristic stops applying.
- Reinstate the historic-data caution: schema changes now need full backup + tested rollback before running.
- Capture the final row counts in `docs/APP_MAP_DATABASE.md` so future diagnostics have a baseline.
- Update this checklist with anything new you learned during the import — especially any gate that was missing or insufficient.

### Re-tune `/requisitions2` column widths against real data (was issue #88)

Current defaults in `app/templates/requisitions2/page.html` (`resizableTable('rq2-list', {...})`) were tuned on the 14 seed requisitions and may not fit the real SFDC distribution:

```
{select:36, name:200, status:110, customer:220, count:60}
```

Once data has landed, run these against the live DB:

```sql
SELECT AVG(req_count) AS avg_reqs, MAX(req_count) AS max_reqs,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY req_count) AS median
FROM (SELECT requisition_id, COUNT(*) AS req_count FROM requirements GROUP BY requisition_id) t;

SELECT MAX(LENGTH(primary_mpn)) AS longest_mpn, AVG(LENGTH(primary_mpn))::int AS avg_mpn
FROM requirements WHERE primary_mpn IS NOT NULL;

SELECT MAX(LENGTH(customer_name)) AS longest_cust, AVG(LENGTH(customer_name))::int AS avg_cust
FROM requisitions WHERE customer_name IS NOT NULL;

SELECT COUNT(*) FILTER (WHERE opportunity_value > 0) AS entered,
       COUNT(*) FILTER (WHERE opportunity_value IS NULL OR opportunity_value = 0) AS missing,
       COUNT(*) AS total
FROM requisitions;
```

Tune if:
- `avg_mpn` > 14 → 200px Name col fits only 1 chip; bump the Name default.
- `avg_cust` > 27 → bump the Customer default further.
- `max_reqs` >> 3 → chip overflow fires often; verify the `+N` hover is readable.
- Deal-value `entered` ratio stays < 30% → revisit how computed/partial values render. **Note:** partly handled already by the new `deal_value()` macro (`deal_value_source`/`priced_count`/`requirement_count` in `_single_row.html` / `_table_rows.html`); only the width / chip-overflow portion remains.

Refs: PR #87 (160→220 customer bump on seed data), PR #81 v2 rollout (original distribution query, 2026-04-22). Migrated from closed issue #88.

---

## Tech debt captured during 2026-04-22 session (address post-rollout)

These are known-but-deferred items. None blocks rollout but each is real:

| Item | Why | Where |
|---|---|---|
| `test_api_health.py` duplicate tests with stale fixture assumptions | 4 pytest failures on main — fixture doesn't commit sources before `run_health_checks` opens its separate session | `tests/test_api_health.py` vs `tests/test_health_monitor.py` — dedupe |
| ESLint errors for `confirm` / `cancelAnimationFrame` | Browser globals not declared in eslint config | `.eslintrc` — add `env: { browser: true }` |
| mypy full-tree scan shows 2080 errors | Strict typing on SQLAlchemy 2.0 ORM — pre-existing, not introduced by any recent PR | Ongoing cleanup; pre-commit only scans changed files so it doesn't block PRs |
| Duplicate `ENABLE_PASSWORD_LOGIN` in `.env` (line 108 & 114) | Fragile — last-one-wins dependency | Manual dedup on droplet, no git commit (file is ignored) |
| `.env.example` drift — 35 extras in `.env`, 7 stubs missing | Template diverged from reality | Sync both ways during a calm window |
| Sourcengine + eBay connectors disabled for missing creds | Expected per current ops; not in scope for rollout | Revisit if more coverage needed post-rollout |
| TLS cert renewal strategy (Gate 3) | Currently-valid cert hides the fact that renewal will fail | Before May 2026, either repoint DNS or configure DNS-01 |

---

_Last updated: 2026-06-08 (folded in the `/requisitions2` column-width review from issue #88); originally 2026-04-22 during the sourcing-engine Phase 4 repair session._
