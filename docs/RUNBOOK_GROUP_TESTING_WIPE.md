# Runbook — Pre-Group-Testing Data Wipe

Owner-run, one command, rehearsed 2026-08-17 on a full copy of the live DB.
Wipes transactional/test data; keeps customer accounts, staff users, and system
configuration. Script: `scripts/wipe_for_group_testing.py`.

## The one decision: `--corpus keep` or `--corpus wipe`

| | keeps | wipes |
|---|---|---|
| `--corpus keep` **(recommended)** | customer accounts + users/config **+ the part/vendor intelligence corpus** (744k material cards, tags, spec facets, FRU links, vendor cards/metrics, knowledge entries — months of enrichment with no test junk in it) | all transactional data (84 tables) |
| `--corpus wipe` | customer accounts + users/config only | transactional data **+ the whole corpus** |

"Customer accounts" = companies, customer_sites, site_contacts (+ their
attachments), prospect accounts/contacts, CRM field history, saved views,
discovery batches. Staff `users` and config (system_config, api_sources,
manufacturers, tag thresholds, commodity schemas) always stay.

## Procedure (wipe day)

```bash
# 1. Fresh backup FIRST (this is the rollback):
docker exec availai-db-1 sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > ~/pre-wipe-$(date +%Y%m%d).dump

# 2. Stop the app + workers so nothing writes mid-wipe:
cd /root/availai && docker compose stop app enrichment-worker

# 3. Dry run — review the table list and row counts, nothing changes:
.venv/bin/python scripts/wipe_for_group_testing.py \
  --dsn "postgresql://<user>:<pass>@127.0.0.1:5432/<db>" --corpus keep --dry-run

# 4. The wipe (one transaction; any error rolls the whole thing back):
.venv/bin/python scripts/wipe_for_group_testing.py \
  --dsn "postgresql://<user>:<pass>@127.0.0.1:5432/<db>" --corpus keep \
  --i-understand-this-deletes-data

# 5. Restart and verify:
docker compose start app enrichment-worker
# app healthy, login works, Companies list intact, Requisitions list empty.
```

Rollback: `pg_restore` the step-1 dump (drop/recreate DB first). The wipe runs
in ONE transaction — a mid-run failure leaves the DB untouched.

## Safety properties (all rehearsal-verified)

- **Explicit DSN only** — the script never reads `DATABASE_URL`, so it cannot
  default onto prod; running it is always a deliberate act, plus the
  `--i-understand-this-deletes-data` flag.
- **Full coverage gate** — every table in the target DB must be classified
  keep/corpus/wipe or the script refuses (caught 10 unlisted tables in
  rehearsal before touching anything).
- **CASCADE guard** — refuses if any KEPT table has an `ON DELETE CASCADE` FK
  into the wipe set (a kept row silently deleting is a classification bug).
- **DELETE, not TRUNCATE** — kept intelligence rows keep themselves and their
  provenance pointers into wiped deals null out via `ON DELETE SET NULL`
  (e.g. knowledge_entries.requirement_id). Iterative drain handles intra-set
  RESTRICT edges (prepayments → buy plans) without a manual ordering.
- **Post-verification** — errors out if any wipe table still has rows or any
  keep table's count changed; identity sequences restart at 1.

## Rehearsal evidence (2026-08-17)

Full `pg_dump` of live (124 MB) restored into a throwaway Postgres 16, script
run with `--corpus keep`:

- Wiped **84 tables** to 0 rows (requisitions 66, offers 1,838, sightings 1,743,
  activity_log 14,642, api_usage_log 131k, prepayments 2, quotes 5, …).
- Kept **46 tables** byte-count-identical to live: material_cards **743,985**,
  material_tags 231k, vendor_cards 1,403, companies 34, customer_sites 33,
  site_contacts 17, users 18, …
- Coverage gate and FK guards each fired once during development and were
  resolved by reclassification — exactly the designed failure mode.
