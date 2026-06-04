<!--
  ACTIVITY_AUDIT.md — Tracked audit & remediation backlog for the AvailAI activity-tracking feature.

  WHAT THIS IS: A standing record of every confirmed quality, correctness, and design finding
  in the activity-tracking surface (activity_log writers, services, routers, schemas, templates,
  digests, migrations, and tests), plus a prioritized backlog and a remediation plan.

  PRODUCED BY: A 21-agent adversarial audit workflow (10 lenses + dev-tool ground truth),
  with every claim re-read against the real code before confirmation.

  DATE: 2026-06-04 — captured on branch feat/activity-audit-repairs.
  SCOPE: Documentation only. This file changes no code. It is the canonical tracker for the
  activity-feature repair effort.
-->

# AvailAI Activity-Tracking Audit & Backlog

> **Tracked audit.** Update this file as backlog items are remediated (mark Rank N done, note
> the commit/PR). Source-of-truth for the activity-feature repair workstream.

This document inventories the **125 confirmed findings** in the AvailAI activity-tracking
feature, clusters them into **14 themes**, ranks the **41 distinct remediation items**, and
lays out a three-workstream plan. The full per-finding evidence dataset is referenced at the
end.

## Methodology

The audit ran a **21-agent adversarial workflow**. The pipeline was:

1. **Find** — Parallel agents swept the activity surface across **10 lenses** (one agent per
   lens, several lenses double-staffed), each producing candidate findings with proposed code
   locations.
2. **Re-read every claim against the real code** — A verification pass re-opened each cited
   file/line and confirmed (or rejected) the claim against the actual source. Claims that did
   not hold against the code were dropped.
3. **Dedup** — Overlapping findings across lenses were collapsed into single canonical items.
4. **Prioritize** — Survivors were ranked by severity × blast-radius × effort into a single
   ordered backlog.

**Result:** **125 findings confirmed, 1 false positive rejected** (in the error-handling lens).
The 10 lenses and their confirmed counts:

| # | Lens (dimension) | Confirmed | False positives |
|---|------------------|-----------|-----------------|
| 1 | Data model, schema & migrations | 13 | 0 |
| 2 | Service correctness | 14 | 0 |
| 3 | activity_service.py performance & simplification | 12 | 0 |
| 4 | Digest service & AI generation | 8 | 0 |
| 5 | Routers: HTTP contracts & duplication | 14 | 0 |
| 6 | Write-path | 13 | 0 |
| 7 | Templates: HTMX/Alpine quality & consistency (frontend) | 13 | 0 |
| 8 | Error-handling | 13 | **1** |
| 9 | Test coverage gaps & quality | 12 | 0 |
| 10 | Cross-cutting | 13 | 0 |
| | **Total** | **125** | **1** |

Alongside the agent sweep, the full **dev-tool suite** (ruff, mypy, pytest, coverage, eslint,
vitest, static-analysis) was run live against the activity surface to establish empirical
ground truth — see the next section.

## Dev-tool ground truth

Empirical results, run live on **2026-06-04** on branch **`feat/activity-audit-repairs`**:

| Tool | Scope | Result |
|------|-------|--------|
| **ruff check** | activity surface | Clean — no lint errors. |
| **mypy** | activity surface | 2 leads. `app/routers/v13_features/activity.py:152` passes `None` where `user_id: int` is expected; `:468` assigns a `VendorCard` into a `Company | None` variable. |
| **pytest** | activity tests | **289 tests pass.** |
| **coverage** | `app/services/activity_service.py` | 87% |
| **coverage** | `app/services/activity_digest_service.py` | 89% |
| **coverage** | `app/routers/v13_features/activity.py` | 100% |
| **coverage** | `app/schemas/activity.py` | 100% |
| **coverage** | `app/routers/activity.py` | 95% |
| **coverage** | `app/models/intelligence.py` | 98% |
| **eslint** | activity JS | 0 errors (6 unused-var warnings in `htmx_app.js`). |
| **vitest** | activity JS | 48 pass. |
| **static-analysis** | activity surface | 6 pass. |

**Coverage read:** the uncovered lines are concentrated in error/fallback branches (LLM-failure
paths, phone-match SQLite fallback, dedup races), **not** in breadth — the happy paths are well
exercised. This aligns with Theme 13 (the untested branches are exactly the correctness items
that lack guarding tests). The two mypy leads correspond to real defects flagged in the backlog.

## Themes

The 125 findings cluster into 14 themes. Several themes share a single root-cause fix (noted in
the remediation plan).

1. **`source_url` phantom/drift (schema declares it, ORM omits it, DB has it).**
   `ActivityLogRead` and both serializers expose `source_url` via `getattr`-default-`None`, the
   `ActivityLog` ORM model never maps the column, yet migration 058 actually created the DB
   column. A real DB column is unreachable through the ORM and the API field is permanently null.
   Root-cause fix is to map the column (it exists) or drop it fully; no writer populates it today.

2. **`activity_type` / `channel` / `direction` / `event_type` raw-literal sprawl (no StrEnum).**
   Categorical columns are written as raw string literals across ~25+ sites, bypassing
   `ActivityType` and lacking `Channel`/`EventType`/`Direction` enums. Synonyms collide
   (`phone_call`/`call_initiated`/`call_logged`; `status_change`/`status_changed`;
   `email`/`outlook`; `note`/`manual`), silently excluding rows from filters, icons, scoring, and
   meaningful-flagging; `strategic_vendor_expiring` (25 chars) overflows `String(20)` and rolls
   back the whole batch on Postgres.

3. **`occurred_at` vs `created_at` clock inconsistency (ordering + digest basis).**
   `occurred_at` is the semantic event time rendered by templates/digest, but every list query
   orders+limits by `created_at`, so backfilled events mis-sort and `limit` truncates the wrong
   end. Two outlier modules order by `occurred_at` on rows whose writers never set it. The digest
   basis keys off `created_at` while rendering `occurred_at`, causing avoidable regenerations.

4. **N+1 loads and per-row matching on hot timeline/attribution paths.**
   Every timeline serializer reads `a.user`/`a.company`/`a.vendor_card` with no eager loading
   across six read helpers (zero `selectinload`), firing O(N) queries per page. The
   auto-attribution loop calls `match_phone_to_entity` per row, and its except-fallback loads
   whole tables. `get_last_call` and req→site→company resolution add round-trips.

5. **Digest failure/race correctness (shared write-path hardening).**
   On AI failure the digest writes no row/cooldown and the tabs auto-fire `hx-trigger=load`, so
   every view re-hits Sonnet uncapped. First-time concurrent builds (or Redis fail-open) race the
   unique constraint and 500 because `db.commit` has no rollback/upsert guard. Blank/partial JSON
   persists as `ready`. All share one fix: a race-safe upsert plus an error-cooldown/state column.

6. **Digest cache-basis precision & change detection.**
   Cache freshness uses exact `==` on `basis_last_activity_at` (round-trip fragility) and
   `basis_count` saturates at `ACTIVITY_CAP=30`, missing older-row backfills. Dismissing an
   activity never invalidates the digest. Fix: monotonic `max(activity.id)` basis + filter
   `dismissed_at` from loaders.

7. **Two-router overlap & list-response contract violations.**
   One feature is split across two routers with no shared prefix/tag, duplicated serializers and
   date-parse/pagination boilerplate that drift by hand. Three v13 list endpoints return bare
   arrays violating `{items,total,limit,offset}` (tests pin the wrong shape). Three click-to-call
   writers diverge in type/shape/side-effects; the documented `call-initiated` has no live caller
   and carries a per-process limiter.

8. **Write-path bypasses the canonical `log_activity` helper.**
   ~25–30 writers construct `ActivityLog` directly, skipping company resolution, `is_meaningful`,
   `occurred_at`, `last_activity_at`, and matching. Inbox-scanned emails and buyplan/bid rows are
   unlinked or encode FK ids in notes strings. Routing through `log_activity` (and email/call
   helpers) is the shared root-cause fix.

9. **Silent write-failure & error-isolation patterns.**
   Several write paths have dead/misplaced error handling: try/except wraps `db.add()` (never
   flushes) so the guard is dead; the ACS webhook has no per-event guard so one poison event
   drops the batch; `match_phone_to_entity` swallows any Exception with no log; non-critical
   email/call logging is unguarded. click-to-call returns 500 despite a never-fail contract.

10. **Timeline-partial duplication & frontend rendering bugs.**
    Five entity timelines re-implement the `ActivityLog` row five divergent ways; only the
    requisition tab uses the canonical `activity_icon` macro — the structural driver of most
    template findings. Concrete bugs: req tab reads nonexistent `a.vendor_card.name` (blank
    label); the shared timeline renders the JSON details column as raw dict text; the log form
    `innerHTML`-swaps into its own id producing duplicate IDs and a re-firing digest. Plus a11y
    gaps and hardcoded badge dicts.

11. **Index & schema hygiene on `activity_log`.**
    Index regressions and mismatches: `ix_activity_unmatched` dropped by 049 and never recreated;
    `ix_activity_requisition` can't serve the timeline sort and `ix_activity_req_channel` is dead;
    `ix_activity_created_at` is measure-first redundant; the `is_meaningful` predicate has no
    partial index; `ix_activity_external` is non-unique. Plus model/DB nits: missing
    relationships, SET-NULL orphans, the false "at most one link" comment, `ActivityDigest`
    CHECK/default gaps, legacy `db.query`, `ConfigDict`, `_utc`.

12. **Attribution & unmatched-queue correctness.**
    `attribute_activity` sets only `company_id`/`vendor_card_id` and never clears stale
    `customer_site_id`/`site_contact_id`/`vendor_contact_id`. The unmatched queue treats
    requisition-only rows as orphans and auto-dismisses them after 30 days. `dismissed_at` gates
    the unmatched queue but is ignored by every timeline query. `meaningful_only` is duplicated
    verbatim and missing from paginated helpers.

13. **Test coverage gaps.**
    Correctness items lack guarding tests or pin the wrong contract: the v13 list-shape tests
    assert the bare array; digest `status_signal` sanitization, error/`?force=1` paths, headline
    truncation, cooldown precedence, and `source_url` round-trip are untested; `attribute_activity`
    stale-link clearing, `last_activity_at` bumps, tz boundaries, dedup scoping, company
    `meaningful_only`, and the phone Postgres branch are unverified.

14. **Retention & growth.**
    `activity_log` has no retention/cleanup/purge job; every email/call/sighting-batch row
    accumulates forever, degrading hot queries and the 15-min quality scan at scale.
    Forward-looking capacity item, low urgency given the intentionally-empty DB and no near-term
    SFDC import.

## Prioritized backlog

All 41 distinct remediation items, ranked. `Locations` shows the first two cited sites (the full
location list per item lives in the raw dataset). `Fix` is trimmed to roughly one sentence.

| Rank | Sev | Category | Effort | Title | Locations (first 2) | Fix (concise) |
|------|-----|----------|--------|-------|---------------------|---------------|
| 1 | high | bug | S | Requisition activity tab reads a.vendor_card.name (nonexistent) — blank vendor label on a core CRM tab | app/templates/htmx/partials/requisitions/tabs/activity.html:128<br>app/models/vendors.py:23-69 | Change activity.html:128 to a.vendor_card.display_name (the canonical attribute used in every other serializer). |
| 2 | high | bug | S | activity_type='strategic_vendor_expiring' (25 chars) overflows String(20) — proactive nudge write silently rolls back | app/jobs/offers_jobs.py:315<br>app/models/intelligence.py:264 | Add STRATEGIC_VENDOR_EXPIRING (<=20 chars) to ActivityType, use it at offers_jobs.py:315, and add a test asserting len(v)<=20 for every ActivityType member. |
| 3 | high | bug | S | log-activity form innerHTML-swaps a full tab partial into its own id — duplicate IDs + re-firing AI digest on every submit | app/templates/htmx/partials/requisitions/tabs/activity.html:7<br>app/templates/htmx/partials/requisitions/tabs/activity.html:50-52 | Option B (matches existing convention): change the form to hx-target="#tab-content" hx-swap="innerHTML" so the returned activity.html replaces the tab body wholesale, eliminating the nested id. |
| 4 | high | bug | M | Digest LLM failure is never cached/cooled-down — auto-load tabs re-hit Sonnet uncapped on every view | app/services/activity_digest_service.py:202-208<br>app/services/activity_digest_service.py:154-170 | On failure upsert a lightweight backoff row (cooldown_until = now + new digest_error_cooldown_seconds) without overwriting prior good content; serve stale READY if present else ERROR. |
| 5 | medium | bug | S | activity_timeline.html renders a.details (JSON column) as raw dict text on the shared sightings/parts timeline | app/templates/htmx/partials/shared/activity_timeline.html:31<br>app/models/intelligence.py:297 | Render {{ a.summary or a.notes or a.activity_type\|replace('_',' ')\|capitalize }} — drop a.details. |
| 6 | medium | bug | S | source_url ORM/DB drift — schema + serializers expose a column the model never maps | app/schemas/activity.py:48<br>app/models/intelligence.py:294-297 | Decide intent: either map source_url on ActivityLog (column exists) and populate it in a writer, or drop the schema field + both getattr lines + the v13 test and add a migration dropping the column. |
| 7 | high | bug | S | ACS webhook has no per-event guard — one poison call event silently drops the whole batch | app/routers/v13_features/activity.py:142-162<br>app/services/activity_service.py:221-241 | Wrap the per-event body in try/except Exception: logger.exception(...); continue, flushing per event inside the try so a failed event rolls back only its own work; keep the single final db.commit() so all good events persist. |
| 8 | medium | bug | M | Digest first-time concurrent build / Redis fail-open races the unique constraint and 500s (shared write-path) | app/services/activity_digest_service.py:148-152<br>app/services/activity_digest_service.py:172-187 | Wrap db.commit() in try/except IntegrityError: db.rollback(), re-SELECT by (entity_type,entity_id), return _digest_to_dict(it). |
| 9 | high | bug | M | Click-to-call vocabulary + side-effect divergence (phone_call vs call_initiated vs call_logged) | app/routers/activity.py:104<br>app/routers/htmx_views.py:5482 | Route the live log-call route (and dead call-initiated) through log_vendor_call/log_call_activity so they emit ActivityType.CALL_LOGGED with matching + last_activity_at; plus a data migration collapsing the synonyms. |
| 10 | medium | bug | M | Three v13 list endpoints return bare arrays, violating {items,total,limit,offset} (tests pin the wrong shape) | app/routers/v13_features/activity.py:231-237<br>app/routers/v13_features/activity.py:293-299 | Add bounded limit/offset Query params, have services return (items,total) (reuse get_account_timeline), return {items,total,limit,offset}, and UPDATE test_v13_activities.py to assert the envelope. |
| 11 | high | bug | M | N+1 relationship loads on every activity timeline render | app/routers/activity.py:228<br>app/routers/v13_features/activity.py:208 | Add a private _base_activity_query(db) applying .options(selectinload(user), selectinload(company), selectinload(vendor_card)) and build all six helpers on it. |
| 12 | medium | bug | M | Misplaced/dead try-except around db.add() aborts the parent transition; status/email writers unguarded | app/services/requirement_status.py:65<br>app/services/requisition_state.py:49-60 | Use db.begin_nested() (SAVEPOINT) around the activity write+flush so a failed logging write rolls back only the savepoint, or route through log_activity and let the boundary own commit. |
| 13 | medium | bug | S | Sightings status-change writes 'status_change' (not STATUS_CHANGED) — never rule-flagged meaningful | app/routers/sightings.py:842<br>app/routers/sightings.py:993 | Replace the literals with ActivityType.STATUS_CHANGED and route the batch path through log_activity for is_meaningful + company resolution; plus a data migration. |
| 14 | medium | risk | S | match_phone_to_entity swallows all exceptions as 'regex unsupported' and falls back to an unbounded scan | app/services/activity_service.py:111-127<br>app/services/activity_service.py:134-145 | Narrow the except to OperationalError/ProgrammingError (SQLite missing-function), add logger.warning before the fallback, and re-raise other faults. |
| 15 | medium | bug | M | attribute_activity leaves stale cross-entity links + unmatched queue treats req-only rows as orphans | app/services/activity_service.py:842-873<br>app/services/activity_service.py:814-825 | In attribute_activity, null the cross-entity sub-links that no longer apply (via a StrEnum, not bare literals); add requisition_id.is_(None) to the unmatched queue filters. |
| 16 | medium | bug | M | Dismissing an activity never invalidates the digest and dismissed rows still show on timelines | app/services/activity_digest_service.py:161-170<br>app/services/activity_service.py:297-300 | Add .filter(ActivityLog.dismissed_at.is_(None)) to the meaningful_only loaders and all timeline queries so a dismissal drops basis_count and forces regeneration; clear dismissed_at in attribute_activity. |
| 17 | medium | bug | M | Auto-attribution calls match_phone_to_entity per row; batch into one suffix map per job | app/services/auto_attribution_service.py:60-65<br>app/services/activity_service.py:97-156 | Add a batch entry point that pre-loads site and vendor-contact phone-suffix maps once per job and resolves all rows in memory; keep single-row match_phone_to_entity for per-event writers. |
| 18 | medium | bug | M | Inbox-scanned sent emails + buyplan/bid rows bypass matching and FK linkage | app/jobs/email_jobs.py:928<br>app/services/activity_service.py:164 | Route email_jobs through log_email_activity (extend signature to accept occurred_at); set buy_plan_id/quote_id on the buyplan/bid rows via log_activity. |
| 19 | medium | bug | S | htmx_views manual log endpoint accepts arbitrary unvalidated activity_type from the form | app/routers/htmx_views.py:2431<br>app/routers/htmx_views.py:2434-2438 | Validate the form value against an allowlist mapped to ActivityType members, return 422 with key 'error' on anything else, and move the body into a service function. |
| 20 | medium | risk | M | Dedup-by-external_id is a TOCTOU with a non-unique index — concurrent webhook+poll create duplicate rows | app/services/activity_service.py:180-183<br>app/services/activity_service.py:234-237 | Add a partial UNIQUE index on external_id WHERE external_id IS NOT NULL via Alembic (de-dup existing rows first), then wrap inserts in a SAVEPOINT catching IntegrityError → re-select-and-return. |
| 21 | medium | improvement | S | Unmatched-queue index regression: ix_activity_unmatched dropped by migration 049, never recreated | alembic/versions/049_reconcile_schema_drift.py:56<br>app/services/activity_service.py:814-838 | Recreate the partial index in both the model __table_args__ and a forward migration (with rollback), keyed on created_at WHERE company_id/vendor_card_id/dismissed_at are NULL. |
| 22 | medium | bug | M | Timelines order/limit by created_at while occurred_at is the true event time — backfills mis-sort | app/services/activity_service.py:300<br>app/services/activity_service.py:339 | Make occurred_at authoritative: backfill it NOT NULL in the email/call/note writers, order by coalesce(occurred_at,created_at), align the two outliers, and add an occurred_at index. |
| 23 | medium | bug | L | Activity-type vocabulary fragmentation across ~25 raw-literal sites (templates + writers) | app/constants.py:351-369<br>app/services/activity_service.py:187 | Extend ActivityType to cover every concept (one canonical <=20-char value), add NOTE/EMAIL_SENT members, replace all literals with constants, add icon-map keys, plus a grep-based guard test and a data migration. |
| 24 | medium | bug | M | channel / direction / event_type lack StrEnums; synonyms break the channel facet | app/services/contact_intelligence.py:173<br>app/services/activity_service.py:192 | Add ActivityChannel, ActivityDirection, and EventType StrEnums; collapse outlook→email and note→manual; normalize teams 'unknown'; add @validates and a data-only backfill migration. |
| 25 | high | simplify | L | Five entity timelines render ActivityLog five divergent ways — no shared canonical row partial | app/templates/htmx/partials/shared/activity_timeline.html:16-45<br>app/templates/htmx/partials/customers/tabs/activity_tab.html:120-176 | Promote the requisition row into a single activity_row(a) macro in shared/_macros.html plus a thin activity_timeline partial; replace all five custom rows and carry the StrEnum vocabulary. |
| 26 | medium | simplify | L | Two-router overlap with no prefix/tag convention + duplicated serializers/handlers | app/routers/activity.py:29<br>app/routers/v13_features/activity.py:31 | Centralize the ActivityLog→dict serializer, extract a shared date-parse + pagination helper, collapse the account/contact handlers onto one builder, and consolidate the endpoints under one tag. |
| 27 | medium | improvement | L | ~25-30 writers bypass the canonical log_activity helper (linkage/last_activity/is_meaningful loss) | app/services/buyplan_notifications.py:178<br>app/services/ownership_service.py:373 | Route system-event writers through log_activity() (extend kwargs) and email/call through their helpers; keep raw ActivityLog construction only inside the three canonical helpers. Sequence after enum consolidation. |
| 28 | low | improvement | S | days_since_last_activity uses .replace(tzinfo=utc) instead of the project _utc() helper | app/services/activity_service.py:420-424<br>app/services/activity_service.py:679-683 | Replace both sites with datetime.now(timezone.utc) - _utc(latest) (already imported); add <24h and exactly-N-day boundary tests. |
| 29 | low | risk | M | Digest cache basis fragile: exact datetime == and basis_count saturates at 30 | app/services/activity_digest_service.py:161<br>app/services/activity_digest_service.py:120-125 | Add basis_last_activity_id = max(a.id) (integer, precision-free, catches insert-ordered backfills) via Alembic migration; compare ids alongside basis_count. |
| 30 | low | bug | S | Timeline date_to <= on a date-only ISO string drops the entire final day | app/routers/activity.py:151-152<br>app/routers/activity.py:199-200 | In the shared date-parse helper, when date_to has no time component add timedelta(days=1) and compare with < date_to (or set 23:59:59.999999); add a same-day-included test. |
| 31 | low | bug | S | VendorContact.interaction_count increment lost when column is NULL | app/services/activity_service.py:697-703<br>app/models/vendors.py:154 | Use func.coalesce(VendorContact.interaction_count,0)+1; backfill NULLs to 0 and add server_default('0')/nullable=False via Alembic with downgrade. |
| 32 | low | simplify | S | meaningful_only filter duplicated verbatim and missing from the paginated timeline helpers | app/services/activity_service.py:299<br>app/services/activity_service.py:338 | Extract a module-level _meaningful_clause() and reuse it in all four helpers; add meaningful_only param to the two paginated helpers and thread it through the routers. |
| 33 | low | bug | M | ActivityLogRead uses a dict model_config instead of ConfigDict(); schema not authoritative | app/schemas/activity.py:29<br>app/schemas/activity.py:26-54 | Use model_config = ConfigDict(from_attributes=True) (drop extra='allow'); add the missing link fields + dismissed_at; build both responses via model_validate so the schema is the single contract. |
| 34 | low | improvement | M | Index hygiene cluster: req-timeline sort order, dead req_channel, redundant created_at, is_meaningful unindexed | app/models/intelligence.py:348-375<br>app/models/intelligence.py:369 | Reorder ix_activity_requisition to (requisition_id, created_at, vendor_card_id); DROP ix_activity_req_channel; add is_meaningful partial indexes; drop ix_activity_created_at only after confirming idx_scan=0. |
| 35 | medium | test-gap | M | Digest status_signal / headline validation and error-path coverage gaps | tests/test_activity_digest_service.py:124-166<br>tests/test_activity_digest_endpoints.py:12-101 | Add tests: invalid status_signal coerces to None; 400-char headline truncates to 300 and empty next_step→None; ?force=1 calls service with force=True; ERROR state renders the retry card; cooldown beats a changed basis. |
| 36 | low | test-gap | M | Service/router test gaps: stale-link clearing, last_activity_at bumps, company meaningful_only, phone fallback, list envelope | tests/test_services_activity.py:333-392<br>tests/test_unmatched_activities.py:219-258 | Add direct service tests for each (mirror the requisition meaningful_only twin; force the phone except branch and assert rollback recovery) and update the v13 list tests to the envelope as part of rank 10. |
| 37 | low | improvement | S | Digest model column records tier alias 'smart' instead of the resolved model id | app/services/activity_digest_service.py:226<br>app/utils/claude_client.py:42-46 | Import MODELS and set row.model = MODELS['smart'] (the resolved id). |
| 38 | medium | improvement | M | Frontend accessibility + lazy-swap polish (anchors without href, clickable divs, digest wrapper, badge dicts) | app/templates/htmx/partials/shared/activity_digest_card.html:13-14<br>app/templates/htmx/partials/customers/tabs/activity_tab.html:93-96 | Make the digest Refresh controls <button type=button>; make the quote row a real <a> matching the RFQ pattern; render the skeleton as an outerHTML-swapped placeholder; replace the inline color dicts with the shared badge macros. |
| 39 | low | improvement | M | Polymorphic-link comment false + missing relationships + SET-NULL orphans + ActivityDigest CHECK/default gaps + legacy db.query | app/models/intelligence.py:267-277<br>app/models/intelligence.py:275-314 | Rewrite the false comment; add site_contact/buy_plan relationships; add an orphaned_at marker filtered from the unmatched queue; add CHECK + server_default('0') to ActivityDigest; switch the digest lookup to select().scalar_one_or_none(). |
| 40 | low | improvement | M | Router thin-ness & observability nits: in-writer commits, last-call shaping, company-less warning, fail-open log | app/routers/v13_features/activity.py:264<br>app/routers/activity.py:251-264 | Move commit ownership into the service helpers; return is_current_user from the service; log when a req resolves no company; make the Redis fail-open warning explicit and documented. |
| 41 | low | risk | M | activity_log has no retention/cleanup job — unbounded growth | app/jobs/quality_jobs.py:16-43<br>app/jobs/email_jobs.py:927-942 | Add a daily config-gated retention job batch-deleting/archiving low-value rows past settings.activity_retention_days with Loguru reporting. Backlog priority — DB intentionally empty, no near-term SFDC import. |

## Remediation plan

Three workstreams. **A** lands fast, low-risk correctness fixes. **B** removes the structural
root causes that generate most of the findings. **C** is this tracker.

### Workstream A — Quick-win repairs

The S-effort, high/medium-severity correctness bugs that can ship immediately with a guarding
test each, no architectural prerequisites:

- **Rank 1** — Requisition activity tab reads `a.vendor_card.name` (nonexistent) → use
  `display_name`; blank vendor label on a core CRM tab.
- **Rank 2** — `activity_type='strategic_vendor_expiring'` (25 chars) overflows `String(20)` →
  add a `<=20`-char `STRATEGIC_VENDOR_EXPIRING` member; silent whole-batch rollback today.
- **Rank 3** — log-activity form `innerHTML`-swaps a full tab partial into its own id →
  retarget the swap; duplicate IDs + a re-firing paid LLM digest on every submit.
- **Rank 5** — shared `activity_timeline.html` renders `a.details` (a JSON dict) as raw text →
  render `summary or notes or activity_type`.
- **Rank 6** — `source_url` ORM/DB drift → resolve intent (map the existing column or drop the
  field + add a drop-column migration).
- **Rank 13** — sightings status-change writes `'status_change'` (not `STATUS_CHANGED`) → use the
  enum and route through `log_activity`; never rule-flagged meaningful today.

### Workstream B — Root-cause refactors

Three structural fixes that each collapse a whole theme (and several backlog items) at once:

**(i) Shared canonical timeline-row macro (Rank 25).**
The five entity timelines re-implement the `ActivityLog` row five divergent ways. Promote the
requisition row into a single `activity_row(a)` Jinja macro in `shared/_macros.html` (icon +
summary/notes precedence + who/channel chips + `(occurred_at or created_at)|timeago`) and have all
five entity timelines consume it. This is the structural driver behind Rank 1 (vendor.name),
Rank 5 (details leak), and the empty-state, timestamp, and pagination template findings.

**(ii) Type-vocabulary normalization (Ranks 2, 9, 13, 23, 24; Theme 2).**
Make the categorical vocabulary enum-backed end to end: add the missing `ActivityType` members
(`NOTE`, `EMAIL_SENT`, `STRATEGIC_VENDOR_EXPIRING`, …), introduce new `Channel`, `EventType`, and
`Direction` `StrEnum`s, route the ~25 raw-literal sites through the enums, and ship **idempotent
data migrations** that collapse the synonym sets (`phone_call`/`call_initiated` → `call_logged`;
`status_change` → `status_changed`; `outlook` → `email`; `note` → `manual`). This dissolves the
overflow bug, the meaningful-flag misses, the icon-map fallbacks, and the channel-facet gaps in
one coordinated change.

**(iii) Two-router consolidation + list-envelope fix (Ranks 10, 26).**
Consolidate the two overlapping routers under one prefix/tag with a single shared serializer,
date-parse, and pagination helper; and convert the three v13 list endpoints to the canonical
`{items, total, limit, offset}` envelope, correcting the tests that currently pin the bare-array
shape.

### Workstream C — This document

`docs/ACTIVITY_AUDIT.md` is the tracked backlog. Update it as items land (mark the rank done, note
the commit/PR), and keep the relevant `docs/APP_MAP_*` docs in sync as code changes.

## Decisions locked

Non-band-aid choices fixed for the repair effort (no half-measures, root-cause only):

- **(a) Vocabulary normalization is FULL.** We add **3 new enums** (`Channel`, `EventType`,
  `Direction`) plus the missing `ActivityType` members, route **all** raw-literal sites through
  them, **and** ship idempotent **data migrations** to collapse synonyms. This is justified by the
  root-cause rule (no band-aids) and by the DB being **intentionally empty / single-user staging**
  — the data migrations are a near-no-op today, so there is no reason to defer the clean fix.
- **(b) The canonical timeline row becomes a single Jinja macro** in `shared/_macros.html`,
  consumed by **all five** entity timelines (requisition, customer, parts, vendor-contact, shared).
  No per-page row re-implementations remain.
- **(c) v13 list endpoints adopt the `{items, total, limit, offset}` envelope** and their tests are
  **corrected** to assert that envelope (the current tests pin the wrong bare-array shape and are
  treated as the bug, not the contract).

## Implementation status — 2026-06-04 (branch `feat/activity-audit-repairs`)

| Workstream | Status | Commits |
|---|---|---|
| **C** — this doc | ✅ shipped | `944fa94e` |
| **A** — quick-win repairs (#1,#2,#3,#5,#6,#13) | ✅ shipped, TDD, 595 tests green | `944fa94e` |
| **B3** — v13 list `{items,total,limit,offset}` envelope (#10) | ✅ shipped | `5d3d4286`, `54269d89` |
| **B1** — canonical `activity_row` macro across 5 timelines (#25) | ✅ shipped, −125 lines | `6332f825` |
| **B2 core** — `Channel`/`EventType`/`Direction` enums + 15 `ActivityType` members + synonym-bug fixes (`status_change`→`status_changed` ×2 writers, stored `channel="note"`→`manual`, teams `direction="unknown"`→NULL, centralized `_normalize_direction`) | ✅ shipped, 591 tests green | `3b315c3e` |

**Remaining (follow-up) for "full" normalization:**
- Route the ~50 *no-behavior-change* raw literals (buyplan / proactive / calendar / api-quota /
  offers writers) through the new `Channel`/`EventType`/`Direction`/`ActivityType` enums —
  type-safety/consistency only; `StrEnum` value == the string, so no stored-value change and no
  test churn.
- Deeper **#9 click-to-call rework**: route `phone_call`/`call_initiated` writers through
  `log_call_activity`/`log_vendor_call` so they get contact-matching + `last_activity_at` bumps
  (not just a literal rename). Entangled with the requisition log-activity form's dropdown values.
- A backfill **data migration** is only needed if/when a non-empty DB appears (staging is
  intentionally empty; writers are canonical going forward).

> Note: this branch was worked concurrently with another session (commits `2bd1a497` dedupe,
> `dc453b2d` materials docs). All activity work above is intact and independently verified.

## Full dataset

The complete raw audit — all **125 confirmed findings** with per-finding evidence, impact,
proposed fix, locations, dimension, and verified severity — is the JSON at:

```
/tmp/claude-0/-root/efbd2ee4-292d-436f-bac3-d139f707c574/tasks/wmjmtdi7a.output
```

(`result.confirmed_findings` holds the full 125; `result.synthesis` holds the 14 themes and the 41
prioritized items summarized above.)
