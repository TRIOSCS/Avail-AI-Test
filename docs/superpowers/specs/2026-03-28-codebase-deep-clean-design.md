# Codebase Deep Clean — Design Spec

**Date:** 2026-03-28
**Scope:** Dead code removal, database cleanup, test fixes, coverage to 85%, code quality fixes
**Approach:** Parallel subagents in worktrees with verification gates

## Current State

- **10,351 tests** — 216 failing, 10,126 passing, 9 skipped
- **~101K lines** of Python in `app/`
- **Ruff:** All checks passed (0 violations)
- **Coverage:** ~50% threshold in CI (exact number pending)

---

## Phase 1: Dead Service Files (14 files + 10 test files)

Delete 14 service files that are imported by zero production code. Also delete their companion test files.

| Service File | Lines | Companion Test File |
|---|---|---|
| `app/services/enrichment_orchestrator.py` | 457 | `tests/test_enrichment_orchestrator.py`, `tests/test_integration_phase4.py` |
| `app/services/sourcing_lead_engine.py` | 440 | `tests/test_sourcing_lead_engine.py` |
| `app/services/vendor_spec_enrichment.py` | 427 | `tests/test_vendor_spec_enrichment.py` |
| `app/services/salesperson_scorecard.py` | 269 | (none) |
| `app/services/buyplan_migration.py` | 223 | `tests/test_buyplan_migration.py` |
| `app/services/website_scraper.py` | 218 | `tests/test_website_scraper.py` |
| `app/services/free_text_parser.py` | 206 | `tests/test_free_text_parser.py` |
| `app/services/self_repair_service.py` | 149 | (none) |
| `app/services/tagging_nexar.py` | 149 | `tests/test_tagging_nexar.py` |
| `app/services/material_search_service.py` | 130 | `tests/test_material_search_service.py` |
| `app/services/rfq_compose_service.py` | 111 | `tests/test_rfq_compose_service.py` |
| `app/services/vendor_detail_service.py` | 92 | (none) |
| `app/services/company_detail_service.py` | 48 | (none) |
| `app/services/credit_manager.py` | ~100 | `tests/test_credit_manager.py` |

**Validation:** All 14 verified by grep — zero production imports. 10 have test-only imports.

**Note for `test_integration_phase4.py`:** Contains 13 tests. Only `test_enrich_company_pipeline` and its imports depend on `enrichment_orchestrator`. Remove that test class + the import line, keep the remaining 12 tests (search/scoring).

---

## Phase 2: Dead Database Tables (7 tables + 1 column)

Create a single Alembic migration to drop all 7 tables and 1 column.

| Table | Model File | Reason Dead |
|---|---|---|
| `error_reports` | `app/models/error_report.py` | Superseded by TroubleTicket (migration 043) |
| `reactivation_signals` | `app/models/intelligence.py` (class only) | Never wired up |
| `ics_classification_cache` | `app/models/ics_classification_cache.py` | Workers use in-memory dict |
| `nc_classification_cache` | `app/models/nc_classification_cache.py` | Workers use in-memory dict |
| `teams_notification_log` | `app/models/teams_notification_log.py` | Never wired up |
| `teams_alert_config` | `app/models/teams_alert_config.py` | Never wired up |
| `risk_flags` | `app/models/risk_flag.py` | Risk flags handled as JSON lists |

**Column:** `trouble_tickets.legacy_error_report_id` — migration artifact, zero reads/writes.

**Required cleanup in `app/models/__init__.py`:** Remove 7 import lines (lines 40, 49, 63, 70, 105, 132, 135).

**Validation:** All 7 verified — no FKs from other tables, no production imports, no raw SQL dependencies.

**Migration template:**
```python
def upgrade():
    op.drop_table("error_reports")
    op.drop_table("reactivation_signals")
    op.drop_table("ics_classification_cache")
    op.drop_table("nc_classification_cache")
    op.drop_table("teams_notification_log")
    op.drop_table("teams_alert_config")
    op.drop_table("risk_flags")
    op.drop_column("trouble_tickets", "legacy_error_report_id")

def downgrade():
    # Tables are dead — no downgrade needed for cleanup migration
    pass
```

---

## Phase 3: Dead Pydantic Schemas (48 schemas)

Remove 48 schema classes that have zero references outside their definition.

**Heaviest deletions (may empty entire files):**
- `app/schemas/apollo.py` — 7 of 10 schemas dead (keep `ApolloSearchRequest`, `ApolloSearchResponse`, `EnrichedContact`)
- `app/schemas/enrichment.py` — 9 of 12 dead (keep `EnrichmentJobRead`, `EnrichmentQueueRead`, `EnrichmentJobCreate`)
- `app/schemas/explorium.py` — 4 of 6 dead (keep `ExploriumEnrichRequest`, `ExploriumEnrichResponse`)
- `app/schemas/prospect_account.py` — 5 of 9 dead

**Caution:** When removing `OkResponse` from `responses.py`, do NOT remove `SimpleOkResponse` (actively used by `routers/ai.py`).

**Validation:** Top 20 verified by exact class name grep — all confirmed zero references.

---

## Phase 4: Dead Templates (16 files + 1 bug fix)

Delete 16 unreferenced template files:

1. `app/templates/base.html` — superseded by `htmx/base.html`
2. `app/templates/htmx/partials/ai/` — entire directory (5 files)
3. `app/templates/htmx/partials/follow_ups/follow_ups/batch_result.html` — misplaced duplicate
4. `app/templates/htmx/partials/sightings/_constraints.html`
5. `app/templates/htmx/partials/sightings/_enrichment_bar.html`
6. `app/templates/htmx/partials/sightings/_suggested_action.html`
7. `app/templates/htmx/partials/shared/column_picker.html`
8. `app/templates/htmx/partials/shared/quote_builder_shell.html`
9. `app/templates/htmx/partials/shared/split_panel.html`
10. `app/templates/htmx/partials/requisitions/rfq_draft_result.html`
11. `app/templates/htmx/partials/search/results.html`
12. `app/templates/htmx/partials/sourcing/search_progress.html`

**Bug fix:** `app/routers/excess.py:178` renders `htmx/partials/excess/import_preview.html` which doesn't exist. Either create the template or remove the dead endpoint.

**Validation:** All 16 verified — no Python TemplateResponse, no Jinja2 include/extends references.

---

## Phase 5: Dead Functions, Jobs, and Enums (28 items)

### Dead utility functions (15)

All verified safe. 10 require companion test cleanup.

| Function | File | Test Cleanup |
|---|---|---|
| `invalidate()` | `cache/intel_cache.py:140` | `tests/test_cache_intel.py` |
| `flush_enrichment_cache()` | `cache/intel_cache.py:162` | None |
| `_set_timezone()` | `database.py:58` | None |
| `_make_datetimes_aware()` | `database.py:65` | None |
| `require_sales()` | `dependencies.py:103` | `tests/conftest.py`, `tests/test_role_permissions.py` |
| `user_reqs_query()` | `dependencies.py:114` | `tests/test_dependencies.py` |
| `wants_html()` | `dependencies.py:198` | None |
| `is_htmx_boosted()` | `dependencies.py:203` | None |
| `get_requirement_or_404()` | `routers/_lookup_helpers.py:20` | None |
| `get_offer_or_404()` | `routers/_lookup_helpers.py:29` | None |
| `get_last_quoted_price()` | `routers/crm/_helpers.py:42` | `tests/test_routers_crm.py`, `crm/__init__.py` re-export |
| `deep_scan_inbox()` | `connectors/email_mining.py:555` | 3 test files (~12 tests) |
| `_is_offer_email()` | `connectors/email_mining.py:809` | 2 test files |
| `clear_classification_cache()` | `search_worker_base/ai_gate.py:244` | `tests/conftest.py:121`, 1 test file |
| `reset()` | `search_worker_base/circuit_breaker.py:61` | 4 test files (5 call sites) |

### Dead job functions (9)

All verified safe. 6 require companion test cleanup.

| Function | File | Test Cleanup |
|---|---|---|
| `_job_batch_enrich_materials()` | `jobs/core_jobs.py:261` | `tests/test_core_jobs.py` |
| `_job_poll_material_batch()` | `jobs/core_jobs.py:284` | `tests/test_core_jobs.py` |
| `_compute_vendor_scores_job()` | `jobs/email_jobs.py:645` | 2 test files |
| `_job_lifecycle_sweep()` | `jobs/lifecycle_jobs.py:56` | None |
| `register_discovery_jobs()` | `jobs/part_discovery_jobs.py:17` | `tests/test_jobs_part_discovery.py` |
| `_job_connector_enrichment()` | `jobs/tagging_jobs.py:74` | `tests/test_jobs_tagging.py` |
| `_job_nexar_backfill()` | `jobs/tagging_jobs.py:190` | `tests/test_jobs_tagging.py` |
| `_job_tagging_backfill()` | `jobs/tagging_jobs.py:314` | None |
| `run_8x8_poll_dry_run()` | `jobs/eight_by_eight_jobs.py:228` | None |

### Dead enum classes (4 of 5)

Remove class definitions only. Keep `ExcessListStatus` (has real import in seed script).

- `MatchMethod` — values used as raw strings, class never imported
- `OfferSource` — same pattern
- `ResponseClassification` — same pattern
- `DiscoveryBatchStatus` — same pattern

---

## Phase 6: Dead htmx Refactor Files (3 files)

The `app/routers/htmx/` directory has 3 files from a stalled refactor (~2-3% complete). The router in `page.py` is not mounted in `main.py`. All 3 are dead code:

- `app/routers/htmx/_shared.py` (122 lines) — duplicate helpers from htmx_views.py
- `app/routers/htmx/_constants.py` (111 lines) — unused column picker constants
- `app/routers/htmx/page.py` (125 lines) — unmounted router

---

## Phase 7: Duplicate Function Consolidation (2 true duplicates)

| Duplicate | Keep | Remove | Action |
|---|---|---|---|
| `_clean_email_body` | `ai_email_parser.py` (preserves newlines) | `response_parser.py` copy | Extract to shared util, update callers |
| `_compute_sourcing_score` | `requisition_list_service.py` | `requisitions/core.py` copy | Delete wrapper, import from service |

**NOT duplicates (keep both):**
- `_get_fernet` — different salts for different encryption domains
- `_parse_csv/_parse_excel` — different return types for different use cases

---

## Phase 8: Fix 216 Failing Tests

Root cause categorization pending (agent running pytest). Known failure clusters:

- `test_prospect_free_enrichment.py` — Session | None mypy false positive pattern
- `test_materials_router.py` — AttributeError (likely model/schema change)
- `test_rfq.py` — AttributeError in follow-up endpoints
- `test_requirements.py` — assertion failures (logic/behavior changed)
- `test_htmx_views.py` — 8+ failures across various endpoints
- `test_health_monitor.py` — 7 failures
- `test_knowledge_service_comprehensive.py` — 4 failures
- `test_ownership_service.py` — 4 failures
- `test_sightings_router_comprehensive.py` — 5 failures

Fix approach: Group by root cause, fix cause not symptom, verify each batch.

---

## Phase 9: New Tests to 85% Coverage

Priority test files to write (ordered by coverage impact):

1. **`app/routers/htmx_views.py`** — 9,888 lines, ~30% covered. Biggest single target.
2. **`app/services/sourcing_leads.py`** — 936 lines, 0 tests. Core business logic.
3. **`app/routers/quote_builder.py`** — 276 lines, 0 tests. Primary workflow.
4. **`app/services/prospect_pool_service.py`** + `app/routers/prospect_pool.py` — 270 lines combined, 0 tests.
5. **`app/services/buyplan_workflow.py`** — 979 lines, ~15% test ratio.
6. **`app/routers/nc_admin.py`** — 136 lines, 0 tests.
7. **`app/services/tagging_ai_classify.py`** — 137 lines, 0 tests.
8. **`app/services/enrichment_utils.py`** — 132 lines, 0 tests.

**Test patterns** (per pytest skill):
- Use `db_session` fixture, `client` fixture with auth overrides
- `TESTING=1 PYTHONPATH=/root/availai` env
- Mock external APIs at source (`patch("app.services.ai_service.claude_json")`)
- Group related tests in classes
- Error responses use `{"error": "..."}` format, NOT `{"detail": "..."}`

---

## Phase 10: Code Quality Fixes (5 items)

1. **`prospect_free_enrichment.py:195`** — Add `assert db is not None` after Session guard clause
2. **`nc_worker/session_manager.py`** — Add `Page | None` type annotation + None guard
3. **`ics_worker/session_manager.py`** — Same as above
4. **`requisitions.customer_name`** — Remove misleading "Legacy" comment (column is heavily used)
5. **`excess.py:178` bug** — Create missing `import_preview.html` or remove dead endpoint
6. **`wants_html()` / `is_htmx_boosted()`** — Update fastapi skill docs after removal

---

## Execution Strategy: Parallel Blitz

### Ordering (dependency-aware)

```
Phase 1 (dead services) ─────────┐
Phase 3 (dead schemas)  ─────────┤
Phase 4 (dead templates) ────────┤── Can run in parallel (independent)
Phase 5 (dead functions) ────────┤
Phase 6 (dead htmx refactor) ───┤
Phase 7 (duplicate consolidation)┘
         │
         ▼
Phase 2 (Alembic migration) ──── Depends on Phase 1 (models/__init__.py clean)
         │
         ▼
Phase 8 (fix 216 failing tests) ─ Must run after dead code removal (some tests deleted)
         │
         ▼
Phase 9 (new tests to 85%) ────── Must run after test fixes (clean baseline)
         │
         ▼
Phase 10 (quality fixes) ─────── Last (minor, low risk)
```

### Verification gates

After each merge:
1. `ruff check app/` — must pass
2. `python3 -m pytest tests/ -q` — count failures must decrease or stay at 0
3. `python3 -m pytest tests/ --cov=app --cov-report=term` — coverage must not decrease

### Estimated impact

- **~5,500 lines of dead code removed** (services, models, schemas, templates, functions, htmx refactor)
- **~10 dead test files removed** (tests for dead services)
- **216 test failures fixed**
- **~3,000-5,000 lines of new tests added**
- **Coverage: 50% → 85%**
- **7 dead DB tables dropped**
