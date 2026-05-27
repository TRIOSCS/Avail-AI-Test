# Phase 1, PR-1.1 — UTCDateTime Migration Audit

**Parent plan:** [`2026-05-27-deferred-high-tier-roadmap.md`](2026-05-27-deferred-high-tier-roadmap.md) → Phase 1: HIGH-DB-2 UTCDateTime migration.

**This doc:** the audit that PR-1.2 (model file updates) and PR-1.3 (Alembic migration) consume. Maps every legacy `Column(DateTime)` declaration in `app/models/` to its target type. No code changes here — this is the planning artifact.

**Verified against:** main HEAD `0f7d6495` (Merge PR #154 from `feat/activity-timeline-6`).

---

## Scope

**Total:** 197 legacy `Column(DateTime)` instances across **36 model files** in `app/models/`.

**`UTCDateTime` already adopted in:** `app/models/crm.py` (3 of 14 columns), `app/models/sourcing.py` (2 of 13 columns). Both files have *mixed* state — the remaining legacy columns get migrated alongside the unmigrated files.

**Decision: 100% of in-scope columns migrate to `UTCDateTime`.** No exceptions surfaced during audit — every legacy column is a timestamp (created_at / updated_at / *_at / *_dt). Zero "this column is a pure date, leave alone" candidates. Zero "this column is intentionally TZ-naive" candidates.

---

## Per-file inventory

Format: `file [count] columns`

```
auth                       [6]   token_expires_at, last_email_scan, last_inbox_scan,
                                 last_contacts_sync, m365_last_healthy, created_at
buy_plan                  [13]   approved_at, so_verified_at, submitted_at, completed_at,
                                 cancelled_at, halted_at, token_expires_at, created_at×3,
                                 estimated_ship_date, po_confirmed_at, po_verified_at, added_at
config                     [8]   last_success, last_error_at, last_ping_at, last_deep_test_at,
                                 created_at×2, expiration_dt, timestamp
crm (mixed)               [11]   last_enriched_at×2, ownership_cleared_at×2,
                                 material_tags_updated_at, deep_enrichment_at,
                                 customer_enrichment_at, created_at×3, email_verified_at
discovery_batch            [3]   started_at, completed_at, created_at
email_intelligence         [2]   received_at, created_at
enrichment                [10]   started_at, completed_at, created_at×5, reviewed_at,
                                 found_at, expires_at
enrichment_run             [3]   started_at, completed_at, created_at
excess                     [9]   created_at×3, updated_at×2, response_received_at, sent_at,
                                 created_at, updated_at
ics_search_log             [1]   searched_at
ics_search_queue           [3]   last_searched_at, created_at, updated_at
ics_worker_status          [3]   last_heartbeat, last_search_at, updated_at
intelligence              [20]   last_searched_at, enriched_at, deleted_at, created_at×7,
                                 first_seen, last_seen, customer_last_purchased_at, sent_at,
                                 converted_at, last_offered_at, dismissed_at, occurred_at,
                                 quality_assessed_at
knowledge                  [4]   expires_at, nudged_at, delivered_at, created_at
nc_search_log              [1]   searched_at
nc_search_queue            [3]   last_searched_at, created_at, updated_at
nc_worker_status           [3]   last_heartbeat, last_search_at, updated_at
notification               [1]   created_at
offers                    [12]   promoted_at, created_at×3, updated_at, approved_at,
                                 selected_at, expires_at, reconfirmed_at, status_updated_at,
                                 received_at
performance                [8]   created_at×4, first_seen_at, last_seen_at, last_contact_at,
                                 created_at
pipeline                   [5]   processed_at, last_sync_at, created_at, submitted_at,
                                 completed_at
price_snapshot             [1]   recorded_at
prospect_account           [4]   claimed_at, dismissed_at, last_enriched_at, created_at
purchase_history           [2]   last_purchased_at, created_at
quotes                     [4]   sent_at, followup_alert_sent_at, result_at, created_at
root_cause_group           [2]   created_at, updated_at
sourcing (mixed)          [11]   created_at×5, last_searched_at, offers_viewed_at,
                                 claimed_at, updated_at, source_searched_at
sourcing_lead              [8]   source_first_seen_at, source_last_seen_at,
                                 vendor_safety_last_checked_at, last_buyer_action_at,
                                 created_at×3, observed_at
strategic                  [3]   last_offer_at, expires_at, released_at
sync                       [3]   started_at, finished_at, created_at
tags                       [4]   created_at, classified_at, first_seen_at, last_seen_at
task                       [3]   due_at, completed_at, created_at
trouble_ticket             [4]   created_at, updated_at, diagnosed_at, resolved_at
unified_score              [2]   ai_blurb_generated_at, created_at
vendor_sighting_summary    [2]   updated_at, newest_sighting_at
vendors                   [15]   last_enriched_at, last_contact_at, engagement_computed_at,
                                 vendor_score_computed_at, last_activity_at,
                                 material_tags_updated_at, email_health_computed_at,
                                 deep_enrichment_at, created_at×2, last_interaction_at,
                                 first_seen_at, last_seen_at, score_computed_at,
                                 ooo_return_date
```

**Total:** 197 columns across 36 files.

---

## Decisions baked into this audit

| Decision | Choice | Reason |
|---|---|---|
| Target type | `UTCDateTime` (already exists in `app/database.py:15`) | Already adopted in 2 files; pattern is proven. `TypeDecorator(impl=DateTime)` that injects `tzinfo=UTC` on load. |
| Migration scope | **All 197 columns.** | No outliers found — every legacy column is a timestamp. Mixed-file migration cleans up `crm.py` and `sourcing.py` along the way. |
| PR-1.2 shape | **One PR with all 36 model files.** | Mechanical change. Splitting into per-file PRs creates 36 PRs of trivial reviews; one PR with a clean diff is easier to scan. |
| `Mapped[datetime]` declarations | Leave alone if NOT bound to a `Column(DateTime)`. | None found in the audit — all `datetime` Mapped declarations attach to a `Column(...)`. Audit-confirmed clean. |
| Alembic migration | **One migration that ALTERs all affected columns to `TIMESTAMPTZ`.** Reversible via `AT TIME ZONE 'UTC'` cast. | Simpler than per-table migrations. Postgres handles batch `ALTER TYPE` atomically. |
| Migration ordering | PR-1.2 (model files) → PR-1.3 (Alembic) — **NOT the reverse.** | Model-only change is no-op at runtime (UTCDateTime still maps to `DateTime` SQL type until the column is `TIMESTAMPTZ`). Once PR-1.2 is on main, PR-1.3 flips the column types in place. Reverse order would create a window where reads succeed but the application reads naive datetimes from TZ-aware columns and writes lose precision. |
| Callsite cleanup (`datetime.utcnow()` → `datetime.now(timezone.utc)`) | **Separate PR-1.4.** | Distinct file set (tests + a few app files). Not blocking PR-1.2/1.3. |
| Pre-existing `default=lambda: datetime.now(timezone.utc)` | Leave as-is. | Already TZ-aware; UTCDateTime accepts it without modification. Audit-confirmed: every `default=` we touch already uses `datetime.now(timezone.utc)`. |

---

## PR-1.2 — Model file updates (the next deliverable)

**Mechanical edit pattern, repeated 197 times across 36 files:**

```python
# Before
from sqlalchemy import Column, DateTime
created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

# After
from sqlalchemy import Column
from ..database import UTCDateTime
created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
```

**Sed-equivalent edit per file:**
1. Add `from ..database import UTCDateTime` (or `from .database import UTCDateTime` for files at `app/models/` root — check current import depth per file).
2. Remove `DateTime` from the `from sqlalchemy import ...` line if it's no longer used elsewhere in the file (some files import `DateTime` for `Column(DateTime)` only; check after substitution).
3. Replace `Column(DateTime` with `Column(UTCDateTime` (preserve any trailing args like `nullable=False`, `index=True`, `default=...`).

**Estimated diff size:** ~400 lines (197 column lines × ~2 line changes per column + 36 import-line edits).

**Test plan:** Existing tests cover the column reads/writes via integration. Running the full pytest suite after PR-1.2 catches any regression. No new tests required — the change is type-decoration only.

---

## PR-1.3 — Alembic migration

**Single migration file** at `alembic/versions/<rev>_utcdatetime_migration.py`:

```python
def upgrade() -> None:
    # All 197 columns. Grouped by table for readability.
    op.execute("ALTER TABLE users ALTER COLUMN token_expires_at TYPE TIMESTAMPTZ USING token_expires_at AT TIME ZONE 'UTC'")
    op.execute("ALTER TABLE users ALTER COLUMN last_email_scan TYPE TIMESTAMPTZ USING last_email_scan AT TIME ZONE 'UTC'")
    # ... (197 total)

def downgrade() -> None:
    # Reverse: TIMESTAMPTZ → TIMESTAMP WITHOUT TIME ZONE.
    op.execute("ALTER TABLE users ALTER COLUMN token_expires_at TYPE TIMESTAMP USING token_expires_at AT TIME ZONE 'UTC'")
    # ... (197 total)
```

**Why raw SQL via `op.execute()` rather than `op.alter_column(...)`:** `alter_column` with `type_=TIMESTAMPTZ` issues a default cast that doesn't preserve UTC interpretation correctly when the source column is TZ-naive. The explicit `AT TIME ZONE 'UTC'` cast in raw SQL is correct.

**Deploy ordering:**
1. Merge PR-1.2 to main (model code now declares `UTCDateTime`; at runtime equivalent to legacy `DateTime` because the column type in Postgres is still `TIMESTAMP`).
2. Run `./deploy.sh` — application restarts on new code. **No DB change yet.** Verify the app is healthy.
3. Merge PR-1.3 to main.
4. Run `./deploy.sh` — Alembic upgrade runs as part of the entrypoint; column types flip in place to `TIMESTAMPTZ`. UTCDateTime's `process_result_value` now sees TZ-aware values directly and stops needing to apply `tzinfo=UTC` (its existing logic is a no-op for already-aware values).

**Rollback:** `alembic downgrade -1` reverses the column types. Pair with a `git revert` of PR-1.3.

---

## PR-1.4 — Callsite cleanup (deferred, not blocking)

**Audit (separate ripgrep pass):**
```
$ grep -rn "datetime\.utcnow()" app/ tests/ | wc -l
```

Current count not measured in this audit — out of scope until PR-1.2/1.3 land. The known fact from CODE_REVIEW_NOTES.md is that several test files use the deprecated `datetime.utcnow()` API (DeprecationWarning emitted during pytest runs). PR-1.4 substitutes:

```python
# Before
due = datetime.utcnow() + timedelta(hours=5)

# After
due = datetime.now(timezone.utc) + timedelta(hours=5)
```

Mechanical replacement. Once PR-1.2 + PR-1.3 are live, any naive-datetime write to a TZ-aware column will raise at runtime — that surfaces remaining callsites without needing the static audit.

---

## Risks

1. **Production data integrity during PR-1.3 deploy.** If any row has a value that's *not* actually UTC (e.g., a naive value written in local time), the `AT TIME ZONE 'UTC'` cast preserves the wall-clock string but interprets it as UTC — which is what we want for application-written timestamps (because the app already writes `datetime.now(timezone.utc).replace(tzinfo=None)` in places). If any column was populated by an external tool in another TZ, that wall-clock value is now "wrong" by the TZ offset. **Mitigation:** AVAIL is single-user staging (per `project_app_stage_single_user` memory); audit confirms all `default=` callsites already use `datetime.now(timezone.utc)`; risk is low. Verify by running `SELECT name, EXTRACT(TIMEZONE FROM ...) FROM ... LIMIT 1` post-migration to confirm column type is TZ-aware.

2. **Alembic migration runtime on large tables.** Postgres `ALTER COLUMN TYPE` rewrites the table. On `requisitions` / `requirements` / `vendor_cards` tables this could be seconds-to-minutes if rows count is high. **Mitigation:** AVAIL is currently empty (per `project_db_fresh_sfdc_pending` memory) — runtime is sub-second.

3. **Concurrent reads during PR-1.3 migration.** `ALTER COLUMN TYPE` takes `ACCESS EXCLUSIVE` lock. Brief downtime window during deploy. **Mitigation:** `./deploy.sh` already gates app restart on migration completion; existing pattern.

4. **`from sqlalchemy import DateTime` still imported but unused.** After substitution, some files may import `DateTime` but not use it. Ruff `F401` will catch + auto-fix.

---

## Checkpoints for user review

- **Before PR-1.2:** confirm this audit doc matches your expectations. In particular, scan the per-file inventory for any column you'd want to *exclude* from migration (audit found zero exclusion candidates — that's the explicit decision).
- **Before PR-1.3 deploy:** sanity-check the migration on a fresh database — `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` cycle must be clean.
- **After PR-1.3 deploy:** spot-check 2-3 tables — `\d+ <table>` in psql to confirm the column type is `timestamp with time zone`.

---

## Next deliverable

PR-1.2 (model file edits) — the mechanical sed pass + ruff cleanup. ~400 LOC across 36 files. Estimated effort: ~30 minutes once authorized.
